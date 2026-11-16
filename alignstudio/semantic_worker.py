"""Local E5 embeddings with implementation/runtime identities and per-text inference."""
from __future__ import annotations

import hashlib
import importlib
import json
import platform
import re
import sqlite3
import sys
from pathlib import Path

CACHE_SCHEMA = "semantic-v2"
CACHE_TABLE = "embeddings_v2"
ALGORITHM = {
    "name": "e5-int8-single-text-mean-l2-v2",
    "max_length": 256,
    "inference_batch_size": 1,
    "padding": "none-single-sequence",
    "pooling": "attention-mask-mean-including-special-tokens",
    "normalization": "l2-float32",
    "vector_encoding": "little-endian-float32",
    "provider": "CPUExecutionProvider",
    "deterministic_compute": True,
    "execution_mode": "sequential",
    "graph_optimization": "all",
    "inter_op_threads": 1,
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            sha.update(block)
    return sha.hexdigest()


def validate_model(model, manifest):
    for item in manifest["files"]:
        path = model / item["filename"]
        if not path.is_file() or path.stat().st_size != item["bytes"]:
            raise ValueError("语义模型文件缺失或尺寸不符：" + item["filename"])
        if file_digest(path) != item["sha256"]:
            raise ValueError("语义模型 SHA-256 不符：" + item["filename"])


def runtime_identity(np, ort, tokenizers, pypinyin):
    """Versions plus native build bytes; no installation paths enter the key."""
    binaries = {}
    for name in ("numpy._core._multiarray_umath", "numpy.linalg._umath_linalg",
                 "onnxruntime.capi.onnxruntime_pybind11_state", "tokenizers.tokenizers"):
        module = importlib.import_module(name)
        binaries[name] = file_digest(module.__file__)
    for label, folder in (("numpy", Path(np.__file__).parent.parent / "numpy.libs"),
                          ("ort", Path(ort.__file__).parent / "capi")):
        for path in sorted(folder.glob("*.dll")):
            binaries[label + "/" + path.name] = file_digest(path)
    return {
        "python": sys.version,
        "python_implementation": platform.python_implementation(),
        "numpy": np.__version__,
        "onnxruntime": ort.__version__,
        "onnxruntime_build": ort.get_build_info(),
        "tokenizers": tokenizers.__version__,
        "pypinyin": pypinyin.__version__,
        "system": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "native_binaries_sha256": binaries,
    }


def cache_identity(manifest, runtime, threads, *, implementation_sha=None, algorithm=None):
    identity = {
        "schema": CACHE_SCHEMA,
        "worker_sha256": implementation_sha or file_digest(__file__),
        "algorithm": dict(ALGORITHM if algorithm is None else algorithm),
        "runtime": runtime,
        "intra_op_threads": threads,
        "model_manifest_sha256": digest(manifest),
    }
    return digest(identity), identity


def open_cache(path, namespace, identity):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS semantic_namespaces "
               "(namespace TEXT PRIMARY KEY, identity_json TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS embeddings_v2 "
               "(namespace TEXT NOT NULL, text_sha256 TEXT NOT NULL, vector BLOB NOT NULL, "
               "PRIMARY KEY(namespace, text_sha256))")
    db.execute("INSERT OR IGNORE INTO semantic_namespaces VALUES (?, ?)", (namespace, canonical(identity)))
    db.commit()
    has_v1 = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'").fetchone()
    legacy = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] if has_v1 else 0
    other = db.execute("SELECT COUNT(*) FROM embeddings_v2 WHERE namespace<>?", (namespace,)).fetchone()[0]
    return db, legacy, other


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    sys.path[:0] = request.get("module_paths", [])
    import numpy as np
    import onnxruntime as ort
    import tokenizers
    import pypinyin
    from pypinyin import lazy_pinyin

    threads = int(request.get("threads", 4))
    if not 1 <= threads <= 32:
        raise ValueError("语义分析线程数必须为1–32")
    model = Path(request["model"])
    manifest = json.loads((model / "manifest.json").read_text(encoding="utf-8"))
    validate_model(model, manifest)
    config = json.loads((model / "config.json").read_text(encoding="utf-8"))
    dimensions = int(config["hidden_size"])
    namespace, identity = cache_identity(manifest, runtime_identity(np, ort, tokenizers, pypinyin), threads)
    db, legacy, other = open_cache(request["cache"], namespace, identity)
    try:
        texts = list(dict.fromkeys(request["left"] + request["right"]))
        vectors, missing = {}, []
        hits = invalid = 0
        for text in texts:
            key = hashlib.sha256(text.encode("utf-8")).hexdigest()
            row = db.execute("SELECT vector FROM embeddings_v2 WHERE namespace=? AND text_sha256=?",
                             (namespace, key)).fetchone()
            if row and len(row[0]) == dimensions * 4:
                vector = np.frombuffer(row[0], dtype="<f4")
                if np.isfinite(vector).all() and abs(float(np.linalg.norm(vector)) - 1) < .001:
                    vectors[text] = vector
                    hits += 1
                    continue
            if row:
                invalid += 1
            missing.append((text, key))
        if missing and request.get("cache_only"):
            raise ValueError("仅缓存模式缺少当前实现/运行时的语义向量；旧版缓存已失效，请关闭仅缓存后重新生成")
        if missing:
            tokenizer = tokenizers.Tokenizer.from_file(str(model / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=ALGORITHM["max_length"])
            tokenizer.no_padding()
            options = ort.SessionOptions()
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = ALGORITHM["inter_op_threads"]
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            options.use_deterministic_compute = True
            session = ort.InferenceSession(str(model / "model_quantized.onnx"), sess_options=options,
                                           providers=[ALGORITHM["provider"]])
            names = {entry.name for entry in session.get_inputs()}
            # Commit batches of eight, but infer every text on its own. Dynamic INT8
            # activations must not depend on which other texts happened to miss cache.
            for start in range(0, len(missing), 8):
                for text, key in missing[start:start + 8]:
                    encoded = tokenizer.encode(text)
                    feeds = {
                        "input_ids": np.array([encoded.ids], dtype=np.int64),
                        "attention_mask": np.array([encoded.attention_mask], dtype=np.int64),
                        "token_type_ids": np.array([encoded.type_ids], dtype=np.int64),
                    }
                    output = session.run(None, {name: value for name, value in feeds.items() if name in names})[0]
                    if output.ndim == 3:
                        mask = feeds["attention_mask"][:, :, None].astype(np.float32)
                        output = (output * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
                    output = output / np.maximum(np.linalg.norm(output, axis=1, keepdims=True), 1e-12)
                    vector = np.asarray(output[0], dtype="<f4")
                    if vector.shape != (dimensions,) or not np.isfinite(vector).all():
                        raise ValueError("语义模型输出向量无效")
                    vectors[text] = vector
                    db.execute("INSERT OR REPLACE INTO embeddings_v2 VALUES (?, ?, ?)",
                               (namespace, key, vector.tobytes()))
                db.commit()
                print(json.dumps({"stage": "semantic", "done": min(start + 8, len(missing)),
                                  "total": len(missing)}), flush=True)
        left = np.array([vectors[text] for text in request["left"]], dtype=np.float32)
        right = np.array([vectors[text] for text in request["right"]], dtype=np.float32)
        scores = (left @ right.T).astype(float).round(7).tolist()
        stop = set("我们 你们 他们 这个 那个 然后 就是 一个 可以 进行 时候 这里 所以 因为 如果 这样 现在 什么 对于 以及 还是 已经 通过 需要 其实 这种".split())
        phonetics = {}
        for text in request.get("phonetic_texts", []):
            features = set()
            for run in re.findall(r"[\u4e00-\u9fff]+", text):
                sounds = lazy_pinyin(run)
                for size in (2, 3):
                    for i in range(len(run) - size + 1):
                        if run[i:i + size] not in stop:
                            features.add("py:" + "_".join(sounds[i:i + size]))
            phonetics[text] = sorted(features)
        vector_sha = hashlib.sha256()
        for text in sorted(texts, key=lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()):
            vector_sha.update(hashlib.sha256(text.encode("utf-8")).digest())
            vector_sha.update(vectors[text].tobytes())
        audit = {
            "namespace": namespace, "identity": identity, "unique_texts": len(texts),
            "text_set_sha256": digest(sorted(hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts)),
            "used_vectors_sha256": vector_sha.hexdigest(), "similarities_sha256": digest(scores),
            "legacy_rows_ignored": legacy, "other_namespace_rows_ignored": other,
            "invalid_rows_recomputed": invalid, "legacy_policy": "v1 rows are never read or promoted",
        }
        result = {"similarities": scores, "phonetics": phonetics, "cache_hits": hits,
                  "computed": len(missing), "model": manifest, "dimensions": dimensions, "cache_audit": audit}
        Path(request["output"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"stage": "semantic", "done": len(missing), "total": len(missing), "hits": hits}), flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
