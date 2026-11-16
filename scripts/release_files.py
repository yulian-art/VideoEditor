"""Explicit public source inventory and checks for accidental local/private content."""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
from pathlib import Path

TOP = ("app.py", "run.cmd", "setup-analysis.cmd", "verify-audio.cmd", "pyproject.toml",
       "requirements-analysis.txt", "requirements-semantic.txt", ".gitignore", ".gitattributes",
       "README.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md")
DOCS = ("USER_GUIDE.zh-CN.md", "VERIFYING.md", "RELEASING.md", "RELEASE_NOTES_0.4.1.md",
        "RUNTIME_IDENTITY.json")
EXAMPLES = ("README.md", "anchors.csv", "captions.srt", "demo.align.json")
RULES = {
    "personal_home_path": re.compile(r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|/(?:home|Users)/)\S+", re.I),
    "private_workspace": re.compile(r"multica" + r"_workspaces_|mention" + r"://|multica" + r"\.ai/api/attachments", re.I),
    "personal_email": re.compile(r"[A-Z0-9_.+-]+@(?:gmail\.com|qq\.com|outlook\.com|hotmail\.com)", re.I),
    "github_credential": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "private_recording_name": re.compile(r"RPReplay_Final\d{6,}|\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}点"),
    "retired_evidence_path": re.compile(r"evidence/v(?:4|041)/|V0_4_ACCEPTANCE\.md"),
}

def inventory(root):
    root=Path(root).resolve()
    names=list(TOP)+["docs/"+name for name in DOCS]+["examples/"+name for name in EXAMPLES]
    names+=[".github/workflows/checks.yml"]
    for folder,extensions in (("alignstudio",{".py"}),("static",{".html",".css",".js"}),
                              ("scripts",{".py"}),("tests",{".py"})):
        names += [p.relative_to(root).as_posix() for p in (root/folder).glob("*") if p.suffix in extensions]
    files={}
    for name in sorted(set(names)):
        path=root/name
        if not path.is_file():raise ValueError("Required public source file missing: "+name)
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Source path escapes release root: "+name)
        files[name]=path
    return files

def payload(path):
    path=Path(path)
    value=path.read_text(encoding="utf-8").replace("\r\n","\n").replace("\r","\n")
    if path.suffix==".cmd":value=value.replace("\n","\r\n")
    return value.encode("utf-8")

def audit(root,tracked=False):
    root=Path(root).resolve();files=inventory(root);findings=[]
    for name,path in files.items():
        text=payload(path).decode("utf-8")
        for rule,expression in RULES.items():
            if expression.search(text):findings.append({"file":name,"rule":rule})
    expected=json.loads((root/"docs/RUNTIME_IDENTITY.json").read_text(encoding="utf-8"))["files_sha256"]
    for name,sha in expected.items():
        if name not in files or hashlib.sha256(files[name].read_bytes()).hexdigest()!=sha:
            findings.append({"file":name,"rule":"validated_runtime_changed"})
    project=json.loads((root/"examples/demo.align.json").read_text(encoding="utf-8"))
    if project["video_path"]!="generated/board.mp4" or project["audio_path"]!="generated/narration.m4a":
        findings.append({"file":"examples/demo.align.json","rule":"example_is_not_synthetic"})
    if tracked:
        run=subprocess.run(["git","ls-files","-z"],cwd=root,capture_output=True,check=True)
        for name in run.stdout.decode().split("\0"):
            if name and name not in files:findings.append({"file":name,"rule":"unexpected_tracked_file"})
    return {"ok":not findings,"source_files":len(files),"runtime_files":len(expected),"findings":findings}

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--tracked",action="store_true")
    args=parser.parse_args()
    report=audit(Path(__file__).resolve().parents[1],args.tracked)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(0 if report["ok"] else 1)
