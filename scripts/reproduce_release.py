"""Actual release-code reproduction: two empty caches and a cache-only hot repeat."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import time
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from alignstudio.analyzer import AnalysisJob, atomic_json
from alignstudio.model import Project

CORE_FIELDS=("candidates","review_intervals","stats","coverage","sampling","captions","project","method",
             "semantic_model","unmatchable_intervals","source_quality","proposed_terminal_freeze",
             "review_projection_version","provenance")


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()


def semantic_core(result):
    core={key:copy.deepcopy(result[key]) for key in CORE_FIELDS if key in result}
    for key in ("video_path","audio_path","csv_path","captions_path"):
        if core["project"].get(key):
            core["project"][key]=Path(core["project"][key]).name
    core["semantic_audit"]=[{key:row[key] for key in
        ("phase","namespace","identity","unique_texts","text_set_sha256","used_vectors_sha256","similarities_sha256")}
        for row in result["semantic_runs"]]
    return core


def portable(result):
    data=copy.deepcopy(result)
    data["runtime_result_sha256"]=canonical_hash(result)
    for key in ("video_path","audio_path","csv_path","captions_path"):
        if data["project"].get(key):data["project"][key]=Path(data["project"][key]).name
    settings=data["settings"]
    for key,value in {"python":".analysis-venv/Scripts/python.exe","module_paths":[],"semantic_module_paths":[],
                      "transcript_path":"analysis-inputs/transcript.json",
                      "ocr_path":"analysis-inputs/ocr.jsonl","cache_dir":".analysis-cache",
                      "semantic_model":"models/e5-small"}.items():
        settings[key]=value
    data["portable_copy_of_runtime_result"]=True
    data["semantic_core_sha256"]=canonical_hash(semantic_core(result))
    assert data["semantic_core_sha256"]==canonical_hash(semantic_core(data))
    return data


def main():
    parser=argparse.ArgumentParser()
    for name in ("python","model","ffmpeg","video","audio"):
        parser.add_argument("--"+name,required=True)
    parser.add_argument("--inputs",type=Path,required=True,help="User-owned directory containing transcript.json and ocr.jsonl")
    parser.add_argument("--module-path",action="append",default=[])
    parser.add_argument("--semantic-module-path",action="append",default=[])
    parser.add_argument("--interval",type=int,choices=(60,20),required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--one-cold-only",action="store_true",help="Only for preliminary sampling/input checks")
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    if (args.output/"reproduction.json").exists():
        raise FileExistsError("Choose a new output directory; cache history must not be reused")
    settings=dict(interval=args.interval,refine_budget=24,frame_width=1180,threads=4,crop=[0,115,0,8],
                  transcript_path=str((args.inputs/"transcript.json").resolve()),
                  ocr_path=str((args.inputs/"ocr.jsonl").resolve()),python=str(Path(args.python).resolve()),
                  model="",semantic_model=str(Path(args.model).resolve()),module_paths=args.module_path,
                  semantic_module_paths=args.semantic_module_path)
    results={}
    def run(label,cache,warm=False):
        if not warm and cache.exists():raise FileExistsError("Cold cache already exists: "+str(cache))
        project=Project(name=f"release-reproduction-{args.interval}s",video_path=str(Path(args.video).resolve()),
                        audio_path=str(Path(args.audio).resolve()))
        config=dict(settings,cache_dir=str(cache.resolve()),cache_only=warm)
        job=AnalysisJob(project,config,args.ffmpeg)
        job.start()
        while job.thread.is_alive():
            row=job.snapshot()
            print(json.dumps(dict(run=label,state=row["state"],message=row["message"]),ensure_ascii=False),flush=True)
            job.thread.join(5)
        state=job.snapshot()
        if state["state"]!="completed":
            raise RuntimeError(json.dumps(state,ensure_ascii=False))
        data=job.result()
        if warm:
            assert all(data["cache"].get(key,0)==0 for key in
                       ("asr_computed","ocr_computed","thumbnail_computed","semantic_computed"))
        else:
            first=data["semantic_runs"][0]
            assert first["cache_hits"]==0 and first["legacy_rows_ignored"]==0 and first["other_namespace_rows_ignored"]==0
            assert data["cache"]["asr_hits"]==0 and data["cache"]["thumbnail_hits"]==0
        atomic_json(args.output/(label+".json"),data)
        atomic_json(args.output/(label+"-portable.json"),portable(data))
        results[label]=data
        print(json.dumps(dict(run=label,accepted=data["stats"]["accepted_candidates"],cache=data["cache"],
                              core=canonical_hash(semantic_core(data))),ensure_ascii=False),flush=True)
        return data
    first=run("cold-a",args.output/"empty-a-cache")
    if args.one_cold_only:return
    hot=run("hot-a",args.output/"empty-a-cache",True)
    second=run("cold-b",args.output/"empty-b-cache")
    expected=semantic_core(first)
    assert semantic_core(hot)==expected,"Hot recomputation changed semantic results"
    assert semantic_core(second)==expected,"Independent cold recomputation changed semantic results"
    summary=dict(status="PASS",interval=args.interval,
                 two_independent_empty_full_caches=True,declared_asr_ocr_inputs=True,
                 core_sha256=canonical_hash(expected),cold_hot_and_second_cold_identical=True,
                 runs={label:dict(cache=row["cache"],elapsed_seconds=row["elapsed_seconds"],
                                 semantic_runs=row["semantic_runs"],
                                 semantic_core_sha256=canonical_hash(semantic_core(row)))
                       for label,row in results.items()})
    atomic_json(args.output/"reproduction.json",summary)
    print(json.dumps(dict(status="PASS",interval=args.interval,core_sha256=summary["core_sha256"]),ensure_ascii=False))
if __name__=="__main__":main()
