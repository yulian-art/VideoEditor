"""Run the real pipeline and score independently against a human CSV."""
from __future__ import annotations
import argparse
import json
import math
import statistics
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alignstudio.analyzer import AnalysisJob, atomic_json
from alignstudio.matching import merge_result
from alignstudio.model import Project, load_csv, map_source_to_target


def main():
    parser = argparse.ArgumentParser()
    for name in ("video", "audio", "transcript", "ocr", "ffmpeg", "python", "reference"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--semantic-model", default="")
    parser.add_argument("--semantic-module-path", action="append", default=[])
    parser.add_argument("--module-path", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--refine-budget", type=int, default=24)
    parser.add_argument("--crop", default="0,115,0,8")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    project = Project(name="独立自动匹配评估", video_path=args.video, audio_path=args.audio)
    settings = dict(interval=args.interval, cache_dir=str(args.output / "cache"),
                    transcript_path=args.transcript, ocr_path=args.ocr,
                    python=args.python, module_paths=args.module_path,
                    crop=[int(value) for value in args.crop.split(",")],
                    refine_budget=args.refine_budget, semantic_model=args.semantic_model,
                    semantic_module_paths=args.semantic_module_path)
    job = AnalysisJob(project, settings, args.ffmpeg)
    job.run()
    snapshot = job.snapshot()
    print(json.dumps(snapshot, ensure_ascii=False, indent=2), flush=True)
    if snapshot["state"] != "completed":
        raise RuntimeError(snapshot["error"])
    result = job.result()
    reference = load_csv(args.reference)
    scored = []
    for candidate in result["candidates"]:
        expected = map_source_to_target(reference.anchors, candidate["source"])
        scored.append(dict(source=candidate["source"], target=candidate["target"],
                           expected_target=round(expected, 3),
                           error_seconds=round(abs(candidate["target"] - expected), 3),
                           confidence=candidate["confidence"], status=candidate["status"],
                           content=candidate["content"], evidence=candidate["evidence"]))
    errors = sorted(row["error_seconds"] for row in scored if row["status"] == "candidate")
    percentile = lambda p: errors[min(len(errors) - 1, max(0, math.ceil(len(errors) * p) - 1))] if errors else None
    # Reapply against a project containing real manually confirmed anchors.
    manual = Project.from_dict(result["project"])
    manual.anchors = [reference.anchors[0], reference.anchors[10], reference.anchors[-2], reference.anchors[-1]]
    manual.anchors[1].locked = True
    before = [anchor.id for anchor in manual.anchors]
    original = [anchor.__class__.from_dict(
        {"id": anchor.id, "source": anchor.source, "target": anchor.target, "content": anchor.content,
         "evidence": anchor.evidence, "origin": anchor.origin, "locked": anchor.locked}
    ) for anchor in manual.anchors]
    merged = merge_result(manual, result)
    retained = {anchor.id: anchor for anchor in merged.anchors}
    preserved = all(anchor.id in retained and anchor == retained[anchor.id] for anchor in original)
    assert preserved, "Manual anchors changed"
    summary = {
        "interval": args.interval, "base_samples": len(result["sampling"]["base_times"]),
        "refined_samples": len(result["sampling"]["refined_times"]), "stats": result["stats"],
        "cache": result["cache"], "elapsed_seconds": result["elapsed_seconds"],
        "comparison": {"reference_used_by_matcher": False, "accepted_count": len(errors),
                       "median_error_seconds": statistics.median(errors) if errors else None, "p90_error_seconds": percentile(0.9),
                       "within_5s": sum(value <= 5 for value in errors),
                       "within_20s": sum(value <= 20 for value in errors),
                       "within_60s": sum(value <= 60 for value in errors)},
        "manual_anchors_preserved": preserved, "preserved_ids": before,
        "review_intervals": result["review_intervals"], "scored_candidates": scored,
    }
    label = str(int(args.interval)) if args.interval.is_integer() else str(args.interval)
    atomic_json(args.output / ("evaluation-" + label + ".json"), summary)
    (args.output / ("result-" + label + ".json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items()
                      if key not in ("review_intervals", "scored_candidates")}, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
