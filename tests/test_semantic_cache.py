from contextlib import closing
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from alignstudio.semantic_worker import ALGORITHM, CACHE_TABLE, cache_identity, open_cache

class SemanticIdentityTests(unittest.TestCase):
    def setUp(self):
        self.model={"files":[{"filename":"weights","sha256":"model-a","bytes":3}]}
        self.runtime={"python":"3.12.10","numpy":"2.5.4","onnxruntime":"1.31.1","tokenizers":"0.23.3",
                      "native_binaries_sha256":{"kernel":"bytes-a"},"processor":"cpu-a"}

    def key(self, **changes):
        return cache_identity(changes.get("model",self.model),changes.get("runtime",self.runtime),
                              changes.get("threads",4),implementation_sha=changes.get("worker","worker-a"),
                              algorithm=changes.get("algorithm",ALGORITHM))[0]

    def test_worker_implementation_changes_invalidate(self):
        self.assertNotEqual(self.key(),self.key(worker="worker-b"))

    def test_each_vector_runtime_version_and_native_build_invalidates(self):
        for field in ("python","numpy","onnxruntime","tokenizers"):
            changed=copy.deepcopy(self.runtime);changed[field]+="-changed"
            with self.subTest(field=field):self.assertNotEqual(self.key(),self.key(runtime=changed))
        changed=copy.deepcopy(self.runtime);changed["native_binaries_sha256"]["kernel"]="bytes-b"
        self.assertNotEqual(self.key(),self.key(runtime=changed))

    def test_algorithm_model_cpu_and_threads_invalidate(self):
        changed=copy.deepcopy(ALGORITHM);changed["max_length"]=128
        self.assertNotEqual(self.key(),self.key(algorithm=changed))
        self.assertNotEqual(self.key(),self.key(model={"files":[]}))
        self.assertNotEqual(self.key(),self.key(threads=1))
        changed=copy.deepcopy(self.runtime);changed["processor"]="cpu-b"
        self.assertNotEqual(self.key(),self.key(runtime=changed))

    def test_identity_is_stable_without_installation_paths_or_dict_order(self):
        a,_=cache_identity(self.model,self.runtime,4,implementation_sha="worker-a")
        b,_=cache_identity(dict(reversed(list(self.model.items()))),dict(reversed(list(self.runtime.items()))),
                           4,implementation_sha="worker-a")
        self.assertEqual(a,b)
        self.assertEqual(len(a),64)

    def test_v1_rows_are_preserved_but_never_promoted(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"cache.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE embeddings (key TEXT PRIMARY KEY, vector BLOB NOT NULL)")
                db.execute("INSERT INTO embeddings VALUES ('old-key',?)",(b"old-vector",))
                db.commit()
            key,identity=cache_identity(self.model,self.runtime,4,implementation_sha="worker-a")
            db,legacy,other=open_cache(path,key,identity)
            try:
                self.assertEqual((legacy,other),(1,0))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM "+CACHE_TABLE).fetchone()[0],0)
                self.assertEqual(db.execute("SELECT vector FROM embeddings").fetchone()[0],b"old-vector")
            finally:db.close()

    def test_namespaces_do_not_reuse_each_others_text_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"cache.sqlite"
            a,identity=cache_identity(self.model,self.runtime,4,implementation_sha="worker-a")
            db,_,_=open_cache(path,a,identity)
            db.execute("INSERT INTO embeddings_v2 VALUES (?,?,?)",(a,"same-text",b"old"));db.commit();db.close()
            b,identity=cache_identity(self.model,self.runtime,4,implementation_sha="worker-b")
            db,legacy,other=open_cache(path,b,identity)
            try:
                self.assertEqual(other,1)
                self.assertIsNone(db.execute("SELECT vector FROM embeddings_v2 WHERE namespace=? AND text_sha256=?",
                                            (b,"same-text")).fetchone())
            finally:db.close()

if __name__=="__main__":unittest.main()
