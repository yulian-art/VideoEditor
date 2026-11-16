"""Actual ONNX cache contract: cold/hot/order/partial fills, identity misses and v1 invalidation."""
from __future__ import annotations
from contextlib import closing
import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from alignstudio.semantic_worker import CACHE_TABLE


def main():
    parser=argparse.ArgumentParser()
    for name in ("python","model"):parser.add_argument("--"+name,required=True)
    parser.add_argument("--module-path",action="append",default=[])
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    folder=args.output.resolve()
    if (folder/"cold.sqlite").exists():raise FileExistsError("Choose a new cache test output directory")
    worker=Path(__file__).resolve().parents[1]/"alignstudio/semantic_worker.py"
    original=worker.read_bytes()
    left=["query: 内存分配", "query: "+("线程调度与内存管理"*90)]+[
        "query: "+("任务队列与处理器，"*(index+1))+f"第{index}段" for index in range(17)]
    right=["passage: 线程调度维护本地与全局队列","passage: 内存分配采用多级缓存"]
    unique=len(set(left+right));records={}
    def run(label,cache,left_arg=left,right_arg=right,worker_arg=worker,threads=4,cache_only=False,fail=False):
        request=dict(left=left_arg,right=right_arg,phonetic_texts=[],threads=threads,
                     model=str(Path(args.model).resolve()),cache=str(folder/cache),
                     output=str(folder/(label+"-output.json")),module_paths=args.module_path,cache_only=cache_only)
        path=folder/(label+"-request.json");path.write_text(json.dumps(request,ensure_ascii=False),encoding="utf-8")
        completed=subprocess.run([args.python,str(worker_arg),str(path)],capture_output=True)
        (folder/(label+".log")).write_bytes(completed.stdout+completed.stderr)
        if fail:
            assert completed.returncode!=0 and "旧版缓存已失效" in completed.stderr.decode("utf-8","replace")
            records[label]=dict(status="PASS",expected_cache_only_failure=True)
            return None
        assert completed.returncode==0,completed.stderr.decode("utf-8","replace")
        result=json.loads(Path(request["output"]).read_text(encoding="utf-8"))
        audit=result["cache_audit"]
        records[label]=dict(status="PASS",hits=result["cache_hits"],computed=result["computed"],
                            namespace=audit["namespace"],used_vectors_sha256=audit["used_vectors_sha256"],
                            legacy_rows_ignored=audit["legacy_rows_ignored"],
                            other_namespace_rows_ignored=audit["other_namespace_rows_ignored"],
                            invalid_rows_recomputed=audit["invalid_rows_recomputed"])
        return result
    def vectors(cache,namespace):
        with closing(sqlite3.connect(folder/cache)) as db:
            return dict(db.execute("SELECT text_sha256,vector FROM "+CACHE_TABLE+" WHERE namespace=?",(namespace,)))

    cold=run("cold","cold.sqlite");ns=cold["cache_audit"]["namespace"]
    assert cold["cache_hits"]==0 and cold["computed"]==unique
    initial=vectors("cold.sqlite",ns)
    hot=run("hot","cold.sqlite",cache_only=True)
    assert hot["cache_hits"]==unique and hot["computed"]==0
    assert hot["similarities"]==cold["similarities"]
    assert hot["cache_audit"]["used_vectors_sha256"]==cold["cache_audit"]["used_vectors_sha256"]
    reverse=run("reordered-cold","reordered.sqlite",list(reversed(left)),list(reversed(right)))
    assert reverse["cache_audit"]["namespace"]==ns
    assert vectors("reordered.sqlite",ns)==initial
    prefix=run("partial-prefix","partial.sqlite",left[:3],right[:1])
    partial=run("partial-fill","partial.sqlite")
    assert partial["cache_hits"]==len(set(left[:3]+right[:1]))
    assert vectors("partial.sqlite",ns)==initial
    added=run("one-text-added","cold.sqlite",left+["query: 新增但不影响其他向量"],right)
    assert added["computed"]==1 and added["cache_hits"]==unique
    assert all(vectors("cold.sqlite",ns)[key]==value for key,value in initial.items())
    changed=folder/"worker-implementation-change.py"
    changed.write_bytes(original+b"\n# implementation invalidation fixture\n")
    impl=run("implementation-miss","cold.sqlite",worker_arg=changed)
    assert impl["cache_hits"]==0 and impl["computed"]==unique
    assert impl["cache_audit"]["namespace"]!=ns
    assert impl["cache_audit"]["used_vectors_sha256"]==cold["cache_audit"]["used_vectors_sha256"]
    changed_algorithm=folder/"worker-algorithm-change.py"
    changed_algorithm.write_text(original.decode().replace('"max_length": 256','"max_length": 128',1),encoding="utf-8")
    algorithm=run("algorithm-miss","cold.sqlite",worker_arg=changed_algorithm)
    assert algorithm["cache_hits"]==0 and algorithm["computed"]==unique
    assert algorithm["cache_audit"]["namespace"]!=ns
    assert algorithm["cache_audit"]["used_vectors_sha256"]!=cold["cache_audit"]["used_vectors_sha256"]
    runtime=run("runtime-threads-miss","cold.sqlite",threads=1)
    assert runtime["cache_hits"]==0 and runtime["computed"]==unique
    assert runtime["cache_audit"]["namespace"]!=ns
    # Populate a real v1 table using its exact old key format and valid vectors.
    model_key=hashlib.sha256(json.dumps(cold["model"],sort_keys=True).encode()).hexdigest()
    with closing(sqlite3.connect(folder/"legacy.sqlite")) as db:
        db.execute("CREATE TABLE embeddings (key TEXT PRIMARY KEY, vector BLOB NOT NULL)")
        for text in dict.fromkeys(left+right):
            key=hashlib.sha256((model_key+"|pool-v1|256|"+text).encode()).hexdigest()
            vector=initial[hashlib.sha256(text.encode()).hexdigest()]
            db.execute("INSERT INTO embeddings VALUES (?,?)",(key,vector))
        db.commit()
    run("legacy-cache-only","legacy.sqlite",cache_only=True,fail=True)
    legacy=run("legacy-regenerated","legacy.sqlite")
    assert legacy["cache_hits"]==0 and legacy["computed"]==unique
    assert legacy["cache_audit"]["legacy_rows_ignored"]==unique
    assert vectors("legacy.sqlite",ns)==initial
    with closing(sqlite3.connect(folder/"legacy.sqlite")) as db:
        assert db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]==unique
    with closing(sqlite3.connect(folder/"cold.sqlite")) as db:
        key=next(iter(initial));db.execute("UPDATE embeddings_v2 SET vector=? WHERE namespace=? AND text_sha256=?",
                                        (b"corrupt",ns,key))
        db.commit()
    repaired=run("corrupt-recomputed","cold.sqlite")
    assert repaired["computed"]==1 and repaired["cache_audit"]["invalid_rows_recomputed"]==1
    assert repaired["cache_hits"]==unique-1
    assert all(vectors("cold.sqlite",ns)[key]==value for key,value in initial.items())
    assert worker.read_bytes()==original
    report=dict(status="PASS",unique_test_texts=unique,worker_sha256=hashlib.sha256(original).hexdigest(),
                runtime_identity=cold["cache_audit"]["identity"]["runtime"],
                exact_vector_equality_across_order_partial_and_hot=True,cases=records)
    (folder/"semantic-cache-contract.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))
if __name__=="__main__":main()
