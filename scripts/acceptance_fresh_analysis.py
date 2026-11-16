"""Exercise fresh ASR/OCR, cache invalidation and export on short real narration."""
from __future__ import annotations
import argparse
import json
import shutil
import struct
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alignstudio.analyzer import AnalysisJob, atomic_json
from alignstudio.matching import merge_result
from alignstudio.model import Project
from alignstudio.exporter import run_export


def main():
    parser=argparse.ArgumentParser()
    for name in ("video","audio","ffmpeg","python","model","cache"):
        parser.add_argument("--"+name, required=True)
    parser.add_argument("--module-path", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    settings=dict(interval=20, refine_budget=0, model=args.model, python=args.python,
                  module_paths=args.module_path, cache_dir=args.cache)
    def run(audio):
        job=AnalysisJob(Project(name="无缓存分析验收", video_path=args.video, audio_path=audio),
                        settings,args.ffmpeg)
        job.run()
        if job.snapshot()["state"]!="completed":
            raise RuntimeError(job.snapshot()["error"])
        return job
    first=run(args.audio)
    result=first.result()
    assert result["cache"]["asr_computed"] > 0
    assert result["cache"]["ocr_computed"] > 0
    assert all(row["words"] for row in result["captions"])
    merged=merge_result(Project.from_dict(result["project"]), result)
    exported=run_export(merged,args.output/"fresh.mp4",ffmpeg_path=args.ffmpeg)
    repeated=run(args.audio)
    assert repeated.result()["cache"]["asr_hits"]==len(result["captions"])
    assert repeated.result()["cache"]["ocr_computed"]==0
    assert repeated.result()["cache"]["thumbnail_computed"]==0
    changed_audio=args.output/"changed.m4a"
    shutil.copyfile(args.audio,changed_audio)
    with changed_audio.open("ab") as stream:
        stream.write(struct.pack(">I4s",8,b"free"))
    changed=run(str(changed_audio.resolve()))
    assert changed.result()["provenance"]["audio_sha256"]!=result["provenance"]["audio_sha256"]
    assert changed.result()["cache"]["asr_hits"]==0
    assert changed.result()["cache"]["asr_computed"]>0
    summary=dict(status="PASS", fresh_asr_segments=len(result["captions"]),
                 word_timestamps=True, fresh_cache=result["cache"], rerun_cache=repeated.result()["cache"],
                 changed_media_invalidates_asr=changed.result()["cache"]["asr_computed"]>0,
                 export_aac_copy_ok=exported["audio_verification"]["ok"],
                 export_result=exported)
    atomic_json(args.output/"fresh-analysis.json",summary)
    print(json.dumps({key:value for key,value in summary.items() if key!="export_result"},ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
