#!/usr/bin/env python3
"""二级语义门禁（StartLux）离线准确率统计。

读取 gate_dumps 里的 meta.json（每窗一个目录），输出：
- 未标注：窗口数、VLM 实际判定率、分数分位、特殊放行命中、时延；
- 有标注：默认问题 / 默认+特殊放行 / 仅特殊放行 三套判据的混淆矩阵、P/R/F1、
  AUC（按 semantic_score 排序）。

标注方式（二选一）：
1. 在每个窗口目录放 label.json：{"event": true, "note": "可选"}
2. 传 --labels labels.jsonl，每行：
   {"did": "1143771099", "start_ms": 1793001205481, "event": true}

用法：
  python3 gate_accuracy.py /Users/cat/miloco-gate-dumps
  python3 gate_accuracy.py /Users/cat/miloco-gate-dumps --labels labels.jsonl
  python3 gate_accuracy.py ... --threshold 0.14 --extra-threshold 0.14
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    xs = sorted(values)
    def p(q: float) -> float:
        idx = min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))
        return xs[idx]
    return {"p50": p(0.5), "p90": p(0.9), "p99": p(0.99), "max": xs[-1]}


def _auc(scores: list[float], labels: list[bool]) -> float | None:
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    pairs = sorted(zip(scores, labels), key=lambda x: x[0])
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j + 2) / 2  # 1-based, ties share average rank
        rank_sum += avg_rank * sum(1 for k in range(i, j + 1) if pairs[k][1])
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def _metrics(pred: list[bool], labels: list[bool]) -> dict[str, float | int]:
    tp = sum(1 for p, y in zip(pred, labels) if p and y)
    fp = sum(1 for p, y in zip(pred, labels) if p and not y)
    fn = sum(1 for p, y in zip(pred, labels) if not p and y)
    tn = sum(1 for p, y in zip(pred, labels) if not p and not y)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = (tp + tn) / len(labels) if labels else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "accuracy": round(accuracy, 4),
    }


def _load_labels(path: Path | None) -> dict[tuple[str, int], bool]:
    out: dict[tuple[str, int], bool] = {}
    if path is None:
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        out[(str(item["did"]), int(item["start_ms"]))] = bool(item["event"])
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dump_root", type=Path)
    parser.add_argument("--labels", type=Path, default=None)
    parser.add_argument("--threshold", type=float, default=0.14)
    parser.add_argument("--extra-threshold", type=float, default=0.14)
    args = parser.parse_args()

    central = _load_labels(args.labels)
    rows: list[dict] = []
    for meta_path in sorted(args.dump_root.rglob("meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        did = str(meta.get("did") or "")
        start_ms = int(float(meta.get("start_ms") or 0))
        label = central.get((did, start_ms))
        label_file = meta_path.parent / "label.json"
        if label is None and label_file.exists():
            try:
                label = bool(json.loads(label_file.read_text(encoding="utf-8"))["event"])
            except (OSError, KeyError, json.JSONDecodeError):
                label = None
        rows.append({
            "did": did,
            "room": str(meta.get("room") or ""),
            "start_ms": start_ms,
            "applied": bool(meta.get("semantic_applied")),
            "score": float(meta.get("semantic_score") or 0.0),
            "pass_score": float(meta.get("semantic_pass_score") or 0.0),
            "hits": str(meta.get("semantic_pass_hits") or ""),
            "pass_count": int(meta.get("semantic_pass_count") or 0),
            "questions": int(meta.get("semantic_questions") or 0),
            "latency_ms": float(meta.get("semantic_ms") or 0.0),
            "label": label,
        })

    print(f"dump_root: {args.dump_root}")
    print(f"windows: {len(rows)}")
    applied = [r for r in rows if r["applied"]]
    print(
        f"semantic_applied: {len(applied)} "
        f"({len(applied) / len(rows) * 100:.1f}% of windows)"
        if rows else "semantic_applied: 0"
    )
    if applied:
        print("score percentiles:", _percentiles([r["score"] for r in applied]))
        print(
            "latency_ms:",
            {
                "mean": round(statistics.mean(r["latency_ms"] for r in applied), 1),
                **_percentiles([r["latency_ms"] for r in applied]),
            },
        )
        print(
            "special_pass: windows_with_items=%d hits=%d questions_max=%d"
            % (
                sum(1 for r in applied if r["pass_count"] > 0),
                sum(1 for r in applied if r["hits"]),
                max((r["questions"] for r in applied), default=0),
            )
        )
    rooms: dict[str, list[dict]] = {}
    for r in rows:
        rooms.setdefault(r["room"] or r["did"], []).append(r)
    print("per room:", {k: len(v) for k, v in sorted(rooms.items())})

    labeled = [r for r in applied if r["label"] is not None]
    print(f"\nlabeled (semantic applied): {len(labeled)}")
    if not labeled:
        print("没有标注：在每个窗口目录放 label.json，或用 --labels 传 JSONL。")
        return
    labels = [bool(r["label"]) for r in labeled]
    default_pred = [r["score"] >= args.threshold for r in labeled]
    combined_pred = [
        r["score"] >= args.threshold or r["pass_score"] >= args.extra_threshold
        for r in labeled
    ]
    special_pred = [r["pass_score"] >= args.extra_threshold for r in labeled]
    print(
        "default  :",
        _metrics(default_pred, labels),
        "auc=", _auc([r["score"] for r in labeled], labels),
    )
    print(
        "combined :",
        _metrics(combined_pred, labels),
        "auc(default_score)=", _auc([r["score"] for r in labeled], labels),
    )
    print("special  :", _metrics(special_pred, labels))
    positives = [r["score"] for r in labeled if r["label"]]
    negatives = [r["score"] for r in labeled if not r["label"]]
    if positives and negatives:
        print("score positives:", _percentiles(positives))
        print("score negatives:", _percentiles(negatives))


if __name__ == "__main__":
    main()
