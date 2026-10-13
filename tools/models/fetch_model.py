"""Fetch Qwen's official ModelScope release with pinned revisions and checksums."""

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = "https://modelscope.cn/api/v1/models/Qwen/Qwen3-Embedding-0.6B/"
FILES = {
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
}


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    target = Path(os.getenv("EMBEDDING_PATH", "/models/embedding"))
    target.mkdir(parents=True, exist_ok=True)
    manifest = target.with_name("embedding-download-manifest.json")
    if manifest.exists():
        files = json.loads(manifest.read_text())
    else:
        with urllib.request.urlopen(
            ROOT + "repo/files?Revision=master&Recursive=true", timeout=30
        ) as r:
            info = json.load(r)
        assert info["Code"] == 200
        files = [
            {k: f[k] for k in ("Path", "Sha256", "Size", "Revision")}
            for f in info["Data"]["Files"]
            if f.get("Path") in FILES
        ]
        assert {f["Path"] for f in files} == FILES
        manifest.write_text(json.dumps(files, indent=2))
    for f in files:
        path = target / f["Path"]
        if path.exists() and digest(path) == f["Sha256"]:
            continue
        query = urllib.parse.urlencode({"Revision": f["Revision"], "FilePath": f["Path"]})
        temp = path.with_suffix(path.suffix + ".partial")
        for attempt in range(3):
            try:
                with (
                    urllib.request.urlopen(ROOT + "repo?" + query, timeout=60) as response,
                    temp.open("wb") as out,
                ):
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        out.write(chunk)
                assert temp.stat().st_size == f["Size"] and digest(temp) == f["Sha256"], (
                    "Official checksum mismatch"
                )
                temp.replace(path)
                print("Verified:", f["Path"], flush=True)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)
    revision = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    record = {
        "repository": "Qwen/Qwen3-Embedding-0.6B",
        "source": "official_modelscope",
        "revision": revision,
        "files": files,
        "files_sha256": {f["Path"]: f["Sha256"] for f in files},
    }
    target.with_name("embedding-provenance.json").write_text(json.dumps(record, indent=2))
    print(json.dumps({"verified": True, "repository": record["repository"], "revision": revision}))


if __name__ == "__main__":
    main()
