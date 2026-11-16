"""Interrupt the real semantic subprocess after a committed batch, then resume it."""
from __future__ import annotations
from contextlib import closing
import argparse,json,sqlite3,sys,threading,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from alignstudio.analyzer import AnalysisJob,AnalysisCancelled,atomic_json
from alignstudio.model import Anchor,Project
from alignstudio.semantic_worker import CACHE_TABLE

def main():
    p=argparse.ArgumentParser()
    for name in ("python","model","ffmpeg"):p.add_argument("--"+name,required=True)
    p.add_argument("--module-path",action="append",default=[])
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    settings=dict(python=args.python,module_paths=args.module_path,cache_dir=str(args.output/"cache"))
    project=Project(anchors=[Anchor("start",0,0,locked=True),Anchor("manual",50,55,locked=True),Anchor("end",120,120,locked=True)])
    before=project.to_dict()
    nonce=uuid.uuid4().hex[:10]
    left=[f"query: 恢复测试{nonce}编号{i} 操作系统线程与队列调度" for i in range(240)]
    right=["passage: 本地运行队列"]
    cache=args.output/"semantic-resume.sqlite"
    payload=dict(left=left,right=right,phonetic_texts=[],model=args.model,cache=str(cache.resolve()))
    job=AnalysisJob(project,settings,args.ffmpeg)
    def work():
        try:
            job._update(state="running")
            job.worker("semantic",**payload)
            job._update(state="completed")
        except AnalysisCancelled:job._update(state="cancelled")
        except Exception as exc:job._update(state="failed",error=str(exc))
    worker=threading.Thread(target=work);worker.start()
    deadline=time.monotonic()+40
    try:
        while worker.is_alive() and time.monotonic()<deadline:
            log=job.folder/"semantic.log"
            if log.exists():
                lines=log.read_text(encoding="utf-8",errors="replace").splitlines()
                try:row=json.loads(lines[-1]) if lines else {}
                except ValueError:row={}
                if row.get("done",0)>0:
                    job.cancel();break
            time.sleep(.02)
    finally:
        if worker.is_alive():job.cancel()
        worker.join(10)
    assert not worker.is_alive()
    assert job.snapshot()["state"]=="cancelled",job.snapshot()
    with closing(sqlite3.connect(cache)) as db:committed=db.execute("SELECT COUNT(*) FROM "+CACHE_TABLE).fetchone()[0]
    assert 0<committed<len(left)+len(right),committed
    assert project.to_dict()==before and job.project.to_dict()==before
    assert json.loads((job.folder/"project-before.align.json").read_text(encoding="utf-8"))==before
    resumed=AnalysisJob(project,settings,args.ffmpeg)
    result=resumed.worker("semantic",**payload)
    assert result["cache_hits"]>=committed
    assert result["computed"]+result["cache_hits"]==len(left)+len(right)
    report=dict(status="PASS",interrupted_state=job.snapshot()["state"],committed_vectors=committed,
                resumed_cache_hits=result["cache_hits"],resumed_computed=result["computed"],
                project_and_backup_unchanged=True,child_process_collected=job.process is None and resumed.process is None)
    atomic_json(args.output/"semantic-recovery.json",report);print(json.dumps(report))
if __name__=="__main__":main()
