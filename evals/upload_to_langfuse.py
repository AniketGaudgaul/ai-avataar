"""Push evals/dataset.jsonl into a Langfuse Dataset (idempotent).

Reuses the app's configured Langfuse client (app.core.tracing), so it picks up
the same keys/host from settings. Run once, then create Experiments/Runs against
the "ai-avatar-eval" dataset from your experiment script (see evals/README.md §4).

    python -m evals.upload_to_langfuse
    python -m evals.upload_to_langfuse --name my-eval --smoke   # only smoke subset

Dataset-item mapping:
    input           = {"question": ..., "history": [...]}   # what the system sees
    expected_output = {expected_route, expected_plan, expected_depth,
                       must_include, must_not_include, expected_behavior, grounding}
    metadata        = {id, category, targets}

`create_dataset` / `create_dataset_item` MERGE by (dataset, id), so re-running
updates rather than duplicating.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.tracing import get_langfuse, tracing_enabled

DATASET_FILE = Path(__file__).with_name("dataset.jsonl")
DEFAULT_NAME = "ai-avatar-eval"

# A cheap subset to run live under free-tier quota (spans every lane + the
# highest-value failure probes). Full set is for deliberate/billed runs.
SMOKE_IDS = {
    "fact-01", "fact-04", "expl-01", "deep-01", "deep-04", "compare-01",
    "recruit-01", "recruit-04", "meta-01", "visual-01", "oos-01", "oos-04",
    "adv-01", "adv-02", "adv-04", "edge-05", "edge-07", "status-01",
}


def load_items(smoke: bool) -> list[dict]:
    items = [json.loads(line) for line in DATASET_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if smoke:
        items = [it for it in items if it["id"] in SMOKE_IDS]
    return items


def to_dataset_item(it: dict) -> dict:
    """Split one dataset row into Langfuse's input / expected_output / metadata."""
    input_payload = {"question": it["question"]}
    if it.get("history"):
        input_payload["history"] = it["history"]

    expected = {
        k: it[k]
        for k in (
            "expected_route",
            "expected_plan",
            "expected_depth",
            "must_include",
            "must_not_include",
            "expected_behavior",
            "grounding",
        )
        if k in it
    }
    metadata = {"id": it["id"], "category": it.get("category"), "targets": it.get("targets", [])}
    return {"input": input_payload, "expected_output": expected, "metadata": metadata}


def main() -> None:
    ap = argparse.ArgumentParser(description="Upload the eval dataset to Langfuse.")
    ap.add_argument("--name", default=DEFAULT_NAME, help="Langfuse dataset name.")
    ap.add_argument("--smoke", action="store_true", help="Upload only the smoke subset.")
    args = ap.parse_args()

    if not tracing_enabled():
        raise SystemExit(
            "Langfuse keys not configured (langfuse_public_key / langfuse_secret_key). "
            "Set them in .env first — see app/config.py."
        )

    lf = get_langfuse()
    items = load_items(args.smoke)

    lf.create_dataset(
        name=args.name,
        description="AI Avatar career-twin eval set: router/retrieval/answer across "
        "factual, deep-dive, synthesis, recruiter, meta, multimodal, out-of-scope, "
        "adversarial, and coverage-gap probes.",
        metadata={"source": "evals/dataset.jsonl", "count": len(items)},
    )

    for it in items:
        di = to_dataset_item(it)
        lf.create_dataset_item(
            dataset_name=args.name,
            id=it["id"],  # stable id -> idempotent upsert
            input=di["input"],
            expected_output=di["expected_output"],
            metadata=di["metadata"],
        )

    lf.flush()
    print(f"Uploaded {len(items)} item(s) to Langfuse dataset '{args.name}'"
          f"{' (smoke subset)' if args.smoke else ''}.")


if __name__ == "__main__":
    main()
