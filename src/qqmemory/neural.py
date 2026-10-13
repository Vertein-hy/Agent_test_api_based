"""A small relation-aware graph network. It scores evidence, never creates facts."""

import json
import hashlib
from pathlib import Path
import numpy as np
import torch
from torch import nn
from qqbot.memory.policy import KINDS, RELATIONS

EMBED = 96
FEATURES = EMBED + len(KINDS) + 2
REL = len(RELATIONS) * 2


def projection(dimension):
    rng = np.random.default_rng(20261005)
    return (rng.standard_normal((dimension, EMBED)) / np.sqrt(EMBED)).astype("float32")


def features(nodes, vectors, now, project):
    semantic = vectors @ project
    semantic /= np.maximum(np.linalg.norm(semantic, axis=1, keepdims=True), 1e-8)
    extra = np.zeros((len(nodes), len(KINDS) + 2), dtype="float32")
    for i, node in enumerate(nodes):
        extra[i, KINDS[node["kind"]]] = 1
        extra[i, -2] = min(max(now - node.get("created", now), 0) / (86400 * 60), 1)
        extra[i, -1] = min(node.get("access", 0) / 20, 1)
    return torch.from_numpy(np.concatenate((semantic, extra), axis=1))


def edges_tensor(nodes, edges):
    lookup = {n["id"]: i for i, n in enumerate(nodes)}
    triples = []
    for edge in edges:
        if edge["src"] not in lookup or edge["dst"] not in lookup:
            continue
        src, dst, rel = lookup[edge["src"]], lookup[edge["dst"]], edge["relation"]
        triples.extend([(src, dst, rel), (dst, src, rel + len(RELATIONS))])
    return (
        torch.tensor(triples, dtype=torch.long).T
        if triples
        else torch.empty((3, 0), dtype=torch.long)
    )


class RelationalLayer(nn.Module):
    def __init__(self, input_dim, hidden):
        super().__init__()
        self.self_weight = nn.Linear(input_dim, hidden)
        self.weights = nn.Parameter(torch.empty(REL, input_dim, hidden))
        nn.init.xavier_uniform_(self.weights)

    def forward(self, x, edges):
        result = self.self_weight(x)
        for rel in range(REL):
            selected = edges[:, edges[2] == rel]
            if not selected.shape[1]:
                continue
            src, dst = selected[:2]
            messages = x[src] @ self.weights[rel]
            summed = torch.zeros_like(result).index_add_(0, dst, messages)
            degree = torch.zeros(len(x), device=x.device).index_add_(
                0, dst, torch.ones(len(dst), device=x.device)
            )
            result = result + summed / degree.clamp_min(1).unsqueeze(1)
        return torch.relu(result)


class MemoryRGCN(nn.Module):
    def __init__(self, hidden=64):
        super().__init__()
        self.first = RelationalLayer(FEATURES, hidden)
        self.second = RelationalLayer(hidden, hidden)
        self.query = nn.Linear(EMBED, hidden)
        self.temperature = nn.Parameter(torch.tensor(2.0))

    def forward(self, x, edges, query):
        nodes = self.second(self.first(x, edges), edges)
        nodes = nn.functional.normalize(nodes, dim=-1)
        query = nn.functional.normalize(self.query(query), dim=-1)
        return nodes @ query * self.temperature.exp().clamp_max(30)


class Ranker:
    """Load a frozen scorer with checked metadata; runtime conversations never train it."""

    def __init__(self, model_dir, dimension):
        self.project = projection(dimension)
        self.model = MemoryRGCN()
        path = Path(model_dir)
        self.metadata = json.loads((path / "rgcn-metadata.json").read_text())
        assert self.metadata["features"] == FEATURES and self.metadata["relations"] == REL
        assert self.metadata["training_source"] == "authored_synthetic_only"
        assert (
            hashlib.sha256((path / "rgcn.pt").read_bytes()).hexdigest()
            == self.metadata["weights_sha256"]
        )
        self.model.load_state_dict(
            torch.load(path / "rgcn.pt", map_location="cpu", weights_only=True)
        )
        self.model.eval()
        self.calls = 0

    def score(self, nodes, edges, vectors, query_vector, now):
        x = features(nodes, vectors, now, self.project)
        q = query_vector @ self.project
        q /= max(np.linalg.norm(q), 1e-8)
        with torch.inference_mode():
            scores = self.model(
                x, edges_tensor(nodes, edges), torch.from_numpy(q.astype("float32"))
            ).numpy()
        self.calls += 1
        return scores
