"""
Offline eval runner used by CI.

Loads the golden dataset, calls the orchestrator for each question, scores each
answer via the evaluation service, aggregates, and exits non-zero if the gate
fails. This is the ML-specific CI step: prompt/model/code changes must clear
quality thresholds before deploy.

Usage:
    python eval/run_eval.py --orchestrator http://localhost:8000 \
                            --evaluation http://localhost:8003
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orchestrator", default="http://localhost:8000")
    ap.add_argument("--evaluation", default="http://localhost:8003")
    ap.add_argument(
        "--dataset", default=str(Path(__file__).parent / "datasets/golden.jsonl")
    )
    args = ap.parse_args()

    rows = [
        json.loads(line)
        for line in Path(args.dataset).read_text().splitlines()
        if line.strip()
    ]

    aggregate: dict[str, list[float]] = {}
    any_gate_fail = False
    failures: list[str] = []

    with httpx.Client(timeout=180) as hc:
        for row in rows:
            res = hc.post(
                f"{args.orchestrator}/research",
                json={"question": row["question"], "recency_days": 3650},
            ).json()
            answer = res["answer"]

            ev = hc.post(
                f"{args.evaluation}/evaluate",
                json={
                    "question": row["question"],
                    "answer": answer,
                    "retrieved": answer.get("sources_used", []),
                    "ground_truth": row.get("ground_truth"),
                    "relevant_source_ids": row.get("relevant_source_ids"),
                },
            ).json()

            for k, v in ev["scores"].items():
                if isinstance(v, (int, float)):
                    aggregate.setdefault(k, []).append(float(v))

            if not ev["passed_gate"]:
                any_gate_fail = True
                failures.append(f"{row['id']}: {ev['gate_failures']}")

    print("\n=== Aggregate eval scores ===")
    for metric, vals in sorted(aggregate.items()):
        print(f"  {metric:22s} mean={statistics.mean(vals):.3f} n={len(vals)}")

    if any_gate_fail:
        print("\nGATE FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nGATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
