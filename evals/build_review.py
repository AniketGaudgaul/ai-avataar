"""Build a human review worksheet from a stored Langfuse experiment run.

Pulls each item's question + actual answer + route/plan back from the run's
traces (read-only — spends NO model tokens), recomputes the deterministic
auto-scores, and writes a Markdown worksheet with a "Your notes:" field per case
so you can walk all 65 cases in your editor and jot issues to discuss later.

    python -m evals.build_review --run full-baseline-2026-07-15
    python -m evals.build_review --run <run> --out evals/review.md --flagged-only

Review flow:
  1. Open the printed Langfuse URL to see full traces when a case needs digging.
  2. Read the worksheet top-to-bottom (or jump via the FLAGGED list at the top).
  3. Write under "Your notes:" for anything worth discussing.
  4. Tell me the file is ready — I'll read your notes back and we triage fixes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.tracing import get_langfuse, tracing_enabled
from evals.run_experiment import EVALUATORS
from evals.upload_to_langfuse import DATASET_FILE, DEFAULT_NAME


def _order() -> dict[str, int]:
    """dataset.jsonl line order, so the worksheet reads in authored order."""
    ids = [json.loads(l)["id"] for l in DATASET_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    return {i: n for n, i in enumerate(ids)}


def _dataset_rows() -> dict[str, dict]:
    return {
        json.loads(l)["id"]: json.loads(l)
        for l in DATASET_FILE.read_text(encoding="utf-8").splitlines() if l.strip()
    }


def _as_dict(v):
    """Trace output may come back as a dict or a JSON string."""
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return {}


def _score(output: dict, row: dict) -> list[tuple[str, float, str]]:
    """Run the deterministic evaluators; return (name, value, comment) triples."""
    expected = {k: row[k] for k in (
        "expected_route", "expected_plan", "expected_depth",
        "must_include", "must_not_include", "expected_behavior", "grounding",
    ) if k in row}
    metadata = {"id": row["id"], "category": row.get("category"), "targets": row.get("targets", [])}
    out: list[tuple[str, float, str]] = []
    for ev in EVALUATORS:
        res = ev(input=row.get("question"), output=output, expected_output=expected, metadata=metadata)
        for e in (res if isinstance(res, list) else [res]):
            if e is None:
                continue
            out.append((e.name, float(e.value), e.comment or ""))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a review worksheet from a Langfuse run.")
    ap.add_argument("--run", required=True, help="Dataset run name (e.g. full-baseline-2026-07-15).")
    ap.add_argument("--dataset", default=DEFAULT_NAME)
    ap.add_argument("--out", default=None, help="Output path (default evals/review-<run>.md).")
    ap.add_argument("--flagged-only", action="store_true", help="Only include cases with an auto-fail.")
    args = ap.parse_args()

    if not tracing_enabled():
        raise SystemExit("Langfuse keys not configured — set them in .env first.")

    lf = get_langfuse()
    run = lf.get_dataset_run(dataset_name=args.dataset, run_name=args.run)
    rows = _dataset_rows()
    order = _order()
    items = sorted(run.dataset_run_items, key=lambda it: order.get(it.dataset_item_id, 999))

    project_url = f"https://us.cloud.langfuse.com"  # host; run URL printed by run_experiment
    lines: list[str] = [
        f"# Eval review — `{args.run}`",
        "",
        f"{len(items)} cases from dataset **{args.dataset}**. Legend: ✓ auto-pass · ✗ auto-fail. "
        "The auto-scores are a *cheap pre-filter* (README §2) — your judgement on the answer is the real grader. "
        "Write under **Your notes:** for anything to discuss; tell me when it's ready.",
        "",
        "> Note: this run predates the guardrail location fix — `fact-08` shows the old refusal bug (already fixed).",
        "",
    ]

    # First pass: compute everything so we can build the FLAGGED quick-list.
    cases = []
    for it in items:
        row = rows.get(it.dataset_item_id)
        if not row:
            continue
        tr = lf.api.trace.get(it.trace_id)
        output = _as_dict(tr.output)
        scores = _score(output, row)
        flagged = any(v < 1.0 for _, v, _ in scores)
        cases.append((it, row, output, scores, flagged, tr))

    flagged_ids = [c[0].dataset_item_id for c in cases if c[4]]
    lines += [f"**Flagged ({len(flagged_ids)}):** " + " · ".join(f"`{i}`" for i in flagged_ids) or "none", "", "---", ""]

    for it, row, output, scores, flagged, tr in cases:
        if args.flagged_only and not flagged:
            continue
        mark = "⚠️" if flagged else "✅"
        exp_r, exp_p = row.get("expected_route"), row.get("expected_plan")
        act_r, act_p = output.get("route"), output.get("plan")
        r_flag = "" if act_r == exp_r else "  ← route differs"
        p_flag = "" if act_p == exp_p else "  ← plan differs"
        score_str = " · ".join(f"{n} {'✓' if v >= 1.0 else '✗'}" + (f" ({c})" if c else "")
                               for n, v, c in scores)
        answer = (output.get("answer") or "").strip() or "(empty)"
        answer_md = "\n".join("> " + ln for ln in answer.splitlines())

        lines += [
            f"### {mark} [{it.dataset_item_id}] — {row.get('category','')}",
            f"**Q:** {row.get('question')}",
            "",
            f"**Expected:** route=`{exp_r}` plan=`{exp_p}` depth=`{row.get('expected_depth')}`",
            f"**Actual:** route=`{act_r}`{r_flag}  plan=`{act_p}`{p_flag}",
            f"**Auto:** {score_str}",
            f"**Expected behavior:** {row.get('expected_behavior','')}",
            "",
            "**Answer:**",
            answer_md,
            "",
            f"[trace](https://us.cloud.langfuse.com{tr.html_path})" if getattr(tr, "html_path", None) else "",
            "**Your notes:**",
            "",
            "",
            "---",
            "",
        ]

    out_path = Path(args.out) if args.out else DATASET_FILE.with_name(f"review-{args.run}.md")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_path} — {len(cases)} cases ({len(flagged_ids)} flagged).")
    print(f"Langfuse run traces: open the run URL from your experiment output.")


if __name__ == "__main__":
    main()
