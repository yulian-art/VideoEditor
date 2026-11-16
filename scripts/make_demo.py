"""Generate authored patterns and a tone; no user recordings or course text are read."""
from __future__ import annotations
import argparse,hashlib,json,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from alignstudio.model import Anchor,Caption,Project,save_csv,srt_text
from alignstudio.editing import add_cut
from alignstudio.exporter import locate_ffmpeg
from alignstudio.mp4aac import audio_signature

ROOT=Path(__file__).resolve().parents[1]

def projects(duration=20.0,prefix=""):
    captions=[Caption(4,7,"Synthetic demo: captions use narration time."),
              Caption(12,15,"Video speed changes; narration keeps its own clock.")]
    def project(name,anchors):
        return Project(name=name,video_path=prefix+"board.mp4",audio_path=prefix+"narration.m4a",
                       fps=30,anchors=anchors,captions=captions)
    start=lambda:Anchor("start",0,0,content="Synthetic start",origin="boundary",locked=True)
    end=lambda source:Anchor("end",duration,source,content="Synthetic end",origin="boundary",locked=True)
    base=project("合成演示：变速与定格",[start(),Anchor("middle",6,6,content="Manual control"),
                 Anchor("freeze",16,20,content="Hold this source frame"),end(20)])
    ahead=project("合成演示：板书提前5秒",[start(),Anchor("ahead",11,6,content="Source 6 at narration 11"),end(22)])
    behind=project("合成演示：板书落后5秒",[start(),Anchor("behind",6,11,content="Source 11 at narration 6"),end(22)])
    before=project("合成演示：裁剪与恢复",[start(),Anchor("before",4,4,content="Before cut"),
        Anchor("auto7",7,7,content="Synthetic auto point",origin="auto"),
        Anchor("manual9",9,9,content="Synthetic manual point"),
        Anchor("freeze",16,20,content="Hold frame"),end(20)])
    cut=add_cut(before,6,10,6,manual_policy="suspend",note="Authored synthetic cut")
    return {"demo":base,"ahead-5s":ahead,"behind-5s":behind,"cut":cut}

def generate(folder,ffmpeg):
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True)
    marker=folder/"SYNTHETIC_DEMO.json"
    if marker.exists():
        saved=json.loads(marker.read_text(encoding="utf-8"))
        if saved.get("kind")!="authored-synthetic-demo":raise ValueError("Unexpected output directory")
        for name,sha in saved["files"].items():
            if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=sha:
                raise ValueError("Demo files changed; choose another output directory")
        return folder
    if any(folder.iterdir()):raise ValueError("Choose an empty demo output directory")
    binary=str(locate_ffmpeg(ffmpeg))
    commands=[
        [binary,"-nostdin","-hide_banner","-loglevel","error","-y","-f","lavfi","-i",
         "testsrc2=size=640x360:rate=30:duration=24","-an","-c:v","libx264","-preset","ultrafast",
         "-crf","24","-pix_fmt","yuv420p","-movflags","+faststart",str(folder/"board.mp4")],
        [binary,"-nostdin","-hide_banner","-loglevel","error","-y","-f","lavfi","-i",
         "sine=frequency=440:sample_rate=48000:duration=20","-af","volume=0.08","-c:a","aac",
         "-b:a","96k",str(folder/"narration.m4a")]]
    for command in commands:
        result=subprocess.run(command,capture_output=True)
        if result.returncode:raise RuntimeError(result.stderr.decode("utf-8","replace"))
    signature=audio_signature(folder/"narration.m4a")
    numerator,denominator=map(int,signature["time_base"].split("/"))
    duration=signature["declared_duration_ticks"]*numerator/denominator
    names=["board.mp4","narration.m4a"]
    for stem,project in projects(duration).items():
        (folder/(stem+".align.json")).write_text(json.dumps(project.to_dict(),ensure_ascii=False,indent=2),encoding="utf-8")
        save_csv(project,folder/(stem+".csv"))
        (folder/(stem+".srt")).write_text("\ufeff"+srt_text(project.captions),encoding="utf-8")
        names += [stem+".align.json",stem+".csv",stem+".srt"]
    data={"kind":"authored-synthetic-demo","generator_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          "description":"FFmpeg test patterns and tone; no speech, user data or third-party course material.",
          "files":{name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in sorted(names)}}
    marker.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    return folder

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--ffmpeg",default="")
    parser.add_argument("--output",type=Path,default=ROOT/"examples/generated")
    args=parser.parse_args()
    print(generate(args.output,args.ffmpeg or None))
if __name__=="__main__":main()
