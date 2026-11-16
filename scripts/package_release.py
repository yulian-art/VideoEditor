"""Build public source ZIPs; private datasets are never release inputs."""
from __future__ import annotations
import argparse,hashlib,json,zipfile
from pathlib import Path
from release_files import audit,inventory,payload

ROOT=Path(__file__).resolve().parents[1]
VERSION="0.4.1"

def write_zip(output,entries,prefix):
    output.parent.mkdir(parents=True,exist_ok=True)
    records=[]
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name,data in sorted(entries.items()):
            info=zipfile.ZipInfo(prefix+name,(1980,1,1,0,0,0))
            info.create_system=3;info.external_attr=0o100644<<16;info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,data,compresslevel=9)
            records.append(hashlib.sha256(data).hexdigest()+"  "+name)
        info=zipfile.ZipInfo(prefix+"MANIFEST.sha256",(1980,1,1,0,0,0))
        info.create_system=3;info.external_attr=0o100644<<16;info.compress_type=zipfile.ZIP_DEFLATED
        archive.writestr(info,("\n".join(records)+"\n").encode())
    return output

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output-dir",type=Path,default=ROOT/"dist")
    parser.add_argument("--with-demo",action="store_true")
    args=parser.parse_args()
    report=audit(ROOT)
    if not report["ok"]:raise ValueError("Public release audit failed: "+json.dumps(report))
    dest=args.output_dir.resolve();dest.mkdir(parents=True,exist_ok=True)
    entries={name:payload(path) for name,path in inventory(ROOT).items()}
    outputs=[write_zip(dest/f"board-align-studio-{VERSION}-source.zip",entries,"board-align-studio/")]
    for name in ("USER_GUIDE.zh-CN.md","RELEASE_NOTES_0.4.1.md"):
        target=dest/name;target.write_bytes(payload(ROOT/"docs"/name));outputs.append(target)
    if args.with_demo:
        folder=ROOT/"examples/generated"
        marker=json.loads((folder/"SYNTHETIC_DEMO.json").read_text(encoding="utf-8"))
        if marker.get("kind")!="authored-synthetic-demo":raise ValueError("Synthetic demo marker required")
        demo={}
        allowed={"board.mp4","narration.m4a"}
        for stem in ("demo","ahead-5s","behind-5s","cut"):
            allowed.update({stem+".align.json",stem+".csv",stem+".srt"})
        if set(marker["files"])!=allowed:raise ValueError("Unexpected demo payload")
        for name,sha in marker["files"].items():
            path=folder/name
            if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):raise ValueError("Invalid demo path")
            data=path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=sha:raise ValueError("Demo content changed: "+name)
            demo[name]=data
        demo["SYNTHETIC_DEMO.json"]=(folder/"SYNTHETIC_DEMO.json").read_bytes()
        demo["README.md"]=payload(ROOT/"examples/README.md")
        outputs.append(write_zip(dest/f"board-align-studio-{VERSION}-demo.zip",demo,"board-align-demo/"))
    sums=[hashlib.sha256(path.read_bytes()).hexdigest()+"  "+path.name for path in outputs]
    (dest/"SHA256SUMS").write_text("\n".join(sums)+"\n",encoding="ascii")
    print(json.dumps({"version":VERSION,"audit":report,"assets":[p.name for p in outputs],
                      "sha256":sums},ensure_ascii=False,indent=2))
if __name__=="__main__":main()
