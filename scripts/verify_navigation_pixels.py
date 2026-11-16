import argparse,json,subprocess,hashlib
from pathlib import Path
def raw(ffmpeg,path):
    return subprocess.run([ffmpeg,"-nostdin","-hide_banner","-loglevel","error","-i",str(path),"-frames:v","1",
        "-pix_fmt","rgb24","-f","rawvideo","pipe:1"],check=True,capture_output=True).stdout
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--ffmpeg",required=True);p.add_argument("--video",required=True);p.add_argument("--folder",type=Path,required=True)
    args=p.parse_args();records=[]
    nav=json.loads((args.folder/"source-navigation.json").read_text(encoding="utf-8"))
    for case in nav["after"]["legacy_clicks"]:
        kind=case["kind"];target=args.folder/(kind+"-control.png")
        subprocess.run([args.ffmpeg,"-nostdin","-hide_banner","-loglevel","error","-y","-ss",str(case["source"]),
                        "-i",args.video,"-frames:v","1","-vf","scale=960:-2",str(target)],check=True)
        control=raw(args.ffmpeg,target);after=raw(args.ffmpeg,args.folder/f"after-{kind}-frame.png")
        before=raw(args.ffmpeg,args.folder/f"before-{kind}-frame.png")
        assert len(control)==len(after)==len(before)
        mae=lambda x,y:sum(abs(a-b) for a,b in zip(x,y))/len(x)
        same,wrong=mae(control,after),mae(control,before)
        assert same<3 and wrong>same*3,(same,wrong)
        records.append(dict(kind=kind,stored_source=case["source"],actual_source=case["actual"],
                            corrected_frame_vs_independent_ffmpeg_mae=round(same,5),
                            old_wrong_frame_vs_independent_ffmpeg_mae=round(wrong,5),
                            control_sha256=hashlib.sha256(target.read_bytes()).hexdigest()))
    (args.folder/"pixel-controls.json").write_text(json.dumps(dict(status="PASS",cases=records),ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(records,ensure_ascii=False))
if __name__=="__main__":main()
