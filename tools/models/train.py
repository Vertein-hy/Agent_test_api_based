"""Train a relational scorer on authored fictional examples, with held-out rewired graphs."""

import gc
import hashlib
import json
import os
from pathlib import Path
import random
import numpy as np
import torch
from qqmemory.encoding import Encoder
from qqmemory.neural import MemoryRGCN, projection, features, edges_tensor, FEATURES, REL, EMBED
from qqbot.memory.policy import RELATIONS

PLANS = [
    ("摄影", "周末去公园拍树", "周末公园拍树", "公园里拍树的周末安排"),
    ("英语口语", "每天跟读十分钟", "每天跟读十分钟", "每天十分钟跟读练习"),
    ("跑步", "每周三慢跑半小时", "周三慢跑半小时", "周三半小时慢跑"),
    ("菜谱", "星期六做番茄面", "周六做番茄面", "星期六的番茄面"),
    ("阅读", "睡前读十页小说", "睡前读十页小说", "睡觉前的十页小说"),
    ("睡眠", "晚上十一点关灯", "晚上十一点关灯", "十一点熄灯的安排"),
    ("绘画", "周日下午画水彩", "周日下午画水彩", "周日下午的水彩练习"),
    ("音乐", "每天练钢琴二十分钟", "每天练钢琴二十分钟", "每日二十分钟钢琴练习"),
    ("游泳", "星期五去泳池练习", "星期五去泳池", "周五泳池练习"),
    ("写作", "月底完成一篇短篇故事", "月底完成短篇故事", "月底写完那篇故事"),
]


def scene(seed, encoder, project, evaluation=False):
    rng = random.Random(seed)
    choices = rng.sample(PLANS, 6)
    positions = list(range(6))
    rng.shuffle(positions)
    nodes, edges = [], []

    def node(kind, text):
        n = {"id": str(len(nodes)), "kind": kind, "text": text, "created": 0, "access": 0}
        nodes.append(n)
        return n["id"]

    def edge(a, b, rel):
        edges.append({"src": a, "dst": b, "relation": RELATIONS[rel]})

    person = node("person", "这段对话的用户")
    episodes = [node("episode", "一段生活计划讨论，记录" + str(i + 1)) for i in range(6)]
    for ep in episodes:
        edge(person, ep, "PARTICIPATED")
    for i, (topic, plan, _, _) in enumerate(choices):
        fact = node("task", plan)
        concept = node("topic", topic)
        edge(fact, episodes[positions[i]], "SUPPORTED_BY")
        edge(fact, person, "SUBJECT")
        edge(episodes[positions[i]], concept, "ABOUT")
    vectors = encoder.encode([n["text"] for n in nodes])
    target = rng.randrange(6)
    wording = choices[target][3 if evaluation else 2]
    question = (
        ("之前说到的" + wording + "，来自哪一次讨论？")
        if evaluation
        else ("我提过的" + wording + "是在哪段对话安排的？")
    )
    query = encoder.encode([question], query=True)[0]
    q = query @ project
    q /= max(np.linalg.norm(q), 1e-8)
    labels = [int(x) for x in episodes]
    baseline = int(np.argmax(vectors[labels] @ query))
    return {
        "x": features(nodes, vectors, 0, project),
        "edges": edges_tensor(nodes, edges),
        "q": torch.from_numpy(q.astype("float32")),
        "candidates": labels,
        "gold": positions[target],
        "baseline": baseline,
    }


def accuracy(model, examples):
    with torch.inference_mode():
        return sum(
            int(model(e["x"], e["edges"], e["q"])[e["candidates"]].argmax()) == e["gold"]
            for e in examples
        ) / len(examples)


def main():
    torch.set_num_threads(2)
    torch.manual_seed(20261005)
    random.seed(20261005)
    target = Path(os.getenv("MODEL_DIR", "/models"))
    encoder = Encoder(target / "embedding", target / "synthetic-vectors.sqlite3")
    project = projection(encoder.dimension)
    print("Encoding authored synthetic training/evaluation cases", flush=True)
    training = [scene(i, encoder, project) for i in range(100)]
    evaluation = [scene(10000 + i, encoder, project, True) for i in range(40)]
    revision = encoder.revision
    del encoder
    gc.collect()
    model = MemoryRGCN()
    initial = accuracy(model, evaluation)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.003, weight_decay=0.0001)
    best, best_state = 0, None
    for epoch in range(35):
        random.shuffle(training)
        model.train()
        total = 0
        for e in training:
            optimizer.zero_grad()
            logits = model(e["x"], e["edges"], e["q"])[e["candidates"]]
            loss = torch.nn.functional.cross_entropy(logits.unsqueeze(0), torch.tensor([e["gold"]]))
            loss.backward()
            optimizer.step()
            total += float(loss.detach())
        model.eval()
        current = accuracy(model, evaluation)
        if current > best:
            best = current
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        if epoch % 5 == 0:
            print(
                json.dumps(
                    {"epoch": epoch, "loss": total / len(training), "validation_accuracy": current}
                ),
                flush=True,
            )
        if best >= 0.975 and epoch >= 8:
            break
    assert best_state is not None and best >= 0.80, (
        "Relational scorer did not meet synthetic acceptance threshold"
    )
    model.load_state_dict(best_state)
    # Relations absent from authored training must have no arbitrary contribution.
    for layer in (model.first, model.second):
        for r in (4, 5, 10, 11):
            layer.weights.data[r].zero_()
    # A fresh test set is used only after model selection.
    encoder = Encoder(target / "embedding", target / "synthetic-vectors.sqlite3")
    test = [scene(20000 + i, encoder, project, True) for i in range(60)]
    final = accuracy(model, test)
    assert final >= 0.80, "Held-out relational test failed"
    torch.save(model.state_dict(), target / "rgcn.pt")
    metadata = {
        "architecture": "two_layer_RGCN",
        "hidden": 64,
        "features": FEATURES,
        "relations": REL,
        "projection_dimension": EMBED,
        "projection_seed": 20261005,
        "encoder_revision": revision,
        "training_source": "authored_synthetic_only",
        "training_examples": 100,
        "validation_examples": 40,
        "test_examples": 60,
        "parameters": sum(p.numel() for p in model.parameters()),
        "weights_sha256": hashlib.sha256((target / "rgcn.pt").read_bytes()).hexdigest(),
        "evaluation": {
            "task": "synthetic_evidence_link_recall",
            "test_top1": final,
            "semantic_only_top1": sum(e["baseline"] == e["gold"] for e in test) / len(test),
            "untrained_validation_top1": initial,
            "scope": "fictional_rewired_graphs_not_real_chat_accuracy",
        },
    }
    (target / "rgcn-metadata.json").write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
