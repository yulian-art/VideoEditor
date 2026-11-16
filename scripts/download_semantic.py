"""Download the pinned E5 ONNX model; editor/analysis never upload media or text."""
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path
MANIFEST = {'repo': 'Xenova/multilingual-e5-small', 'revision': '761b726dd34fb83930e26aab4e9ac3899aa1fa78', 'files': [{'filename': 'config.json', 'bytes': 658, 'sha256': 'cb99455288675345e1a4f411438d5d0adbba5fbd3a67ea4fb03c015433b996c1'}, {'filename': 'tokenizer.json', 'bytes': 17082730, 'sha256': '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39'}, {'filename': 'tokenizer_config.json', 'bytes': 443, 'sha256': 'a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b'}, {'filename': 'special_tokens_map.json', 'bytes': 167, 'sha256': 'd05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7'}, {'filename': 'model_quantized.onnx', 'bytes': 118308185, 'sha256': 'f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193'}], 'source': 'https://huggingface.co/intfloat/multilingual-e5-small'}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1]/"models"/"e5-small")
    args=parser.parse_args()
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=True)
    for item in MANIFEST["files"]:
        target=root/item["filename"]
        valid=target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==item["sha256"]
        if not valid:
            filename=("onnx/" if item["filename"].endswith(".onnx") else "")+item["filename"]
            url=f"https://huggingface.co/{MANIFEST['repo']}/resolve/{MANIFEST['revision']}/{filename}"
            print("Downloading",filename,flush=True)
            part=target.with_suffix(target.suffix+".part")
            with urllib.request.urlopen(url,timeout=60) as response,part.open("wb") as stream:
                while block:=response.read(1024*1024):stream.write(block)
            if hashlib.sha256(part.read_bytes()).hexdigest()!=item["sha256"]:
                raise ValueError("Model SHA-256 mismatch: "+filename)
            part.replace(target)
    (root/"manifest.json").write_text(json.dumps(MANIFEST,indent=2),encoding="utf-8")
    print("Verified semantic model:",root)
if __name__=="__main__":main()
