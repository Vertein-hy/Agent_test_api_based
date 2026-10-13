"""Offline CPU embedding and derived vector cache; authoritative data stays in the graph."""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

QUERY_PREFIX = "Instruct: Find relevant evidence in the permitted conversation records.\nQuery: "


class Encoder:
    """Load the pinned local CPU embedding model and own a locked derived vector cache."""

    def __init__(self, model_path, cache_path):
        torch.set_num_threads(2)
        self.lock = threading.Lock()
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path, local_files_only=True, padding_side="left"
        )
        self.model = AutoModel.from_pretrained(
            model_path, local_files_only=True, torch_dtype=torch.float32, trust_remote_code=False
        ).eval()
        self.db = sqlite3.connect(cache_path, check_same_thread=False)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL)"
        )
        self.db.commit()
        self.dimension = self.model.config.hidden_size
        provenance = Path(model_path).with_name("embedding-provenance.json")
        self.revision = (
            json.loads(provenance.read_text())["revision"] if provenance.exists() else "unknown"
        )

    def encode(self, texts, query=False):
        texts = [t[:2000] for t in texts]
        keys = [
            hashlib.sha256(
                (self.revision + (QUERY_PREFIX if query else "document:") + t).encode()
            ).hexdigest()
            for t in texts
        ]
        with self.lock:
            rows = [
                self.db.execute("SELECT vector FROM vectors WHERE key=?", (k,)).fetchone()
                for k in keys
            ]
            missing = list(dict.fromkeys(i for i, r in enumerate(rows) if r is None))
            for start in range(0, len(missing), 4):
                indices = missing[start : start + 4]
                inputs = [(QUERY_PREFIX + texts[i]) if query else texts[i] for i in indices]
                batch = self.tokenizer(
                    inputs, padding=True, truncation=True, max_length=256, return_tensors="pt"
                )
                with torch.inference_mode():
                    h = self.model(**batch).last_hidden_state[:, -1, :]
                    h = torch.nn.functional.normalize(h, p=2, dim=1).numpy().astype("float32")
                for i, vector in zip(indices, h):
                    self.db.execute(
                        "INSERT OR REPLACE INTO vectors VALUES (?,?)", (keys[i], vector.tobytes())
                    )
                    rows[i] = (vector.tobytes(),)
            self.db.commit()
            return (
                np.stack([np.frombuffer(r[0], dtype="float32").copy() for r in rows])
                if rows
                else np.empty((0, self.dimension), dtype="float32")
            )

    def purge(self, keys):
        with self.lock:
            self.db.executemany("DELETE FROM vectors WHERE key=?", [(k,) for k in keys])
            self.db.commit()

    def purge_all(self):
        # Deleting one actor also clears this derived cache, including query vectors.
        # Other users' authoritative graphs remain intact and can be re-encoded.
        with self.lock:
            self.db.execute("DELETE FROM vectors")
            self.db.commit()
            self.db.execute("VACUUM")
