"""Public, synthetic end-to-end export checks; no private course fixture required."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from make_demo import generate
from alignstudio.model import Project,validate_project,map_target_to_source,load_csv
from alignstudio.editing import remove_cut
from alignstudio.exporter import run_export
from alignstudio.mp4aac import compare_audio

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--ffmpeg",required=True)
    parser.add_argument("--output",type=Path,default=ROOT/"artifacts/demo-check")
    args=parser.parse_args()
    demo=generate(ROOT/"examples/generated",args.ffmpeg)
    args.output.mkdir(parents=True,exist_ok=True)
    cases={}
    for stem in ("demo","ahead-5s","behind-5s","cut"):
        data=json.loads((demo/(stem+".align.json")).read_text(encoding="utf-8"))
        p=Project.from_dict(data)
        p.video_path=str(demo/p.video_path);p.audio_path=str(demo/p.audio_path)
        assert not [row for row in validate_project(p) if row["level"]=="error"]
        imported=load_csv(demo/(stem+".csv"))
        assert [(a.id,round(a.target,3),round(a.source,3)) for a in imported.anchors]==[(a.id,round(a.target,3),round(a.source,3)) for a in p.anchors]
        if stem=="ahead-5s":assert map_target_to_source(p.anchors,11,p.cuts)==6
        if stem=="behind-5s":assert map_target_to_source(p.anchors,6,p.cuts)==11
        if stem=="demo":assert map_target_to_source(p.anchors,18,p.cuts)==20
        if stem=="cut":
            assert map_target_to_source(p.anchors,6,p.cuts)==10
            restored=remove_cut(p,p.cuts[0].id)
            assert {"auto7","manual9"}.issubset({a.id for a in restored.anchors})
        result=run_export(p,args.output/(stem+".mp4"),ffmpeg_path=args.ffmpeg,encoder="libx264")
        audio=compare_audio(p.audio_path,result["output"])
        assert audio["ok"]
        assert Path(result["srt"]).read_text(encoding="utf-8-sig")==(demo/(stem+".srt")).read_text(encoding="utf-8-sig")
        cases[stem]={"audio_copy_ok":True,"aac_packets":audio["source"]["packet_count"],
                     "captions_fixed":True,"mapping_and_csv_checked":True}
    report={"status":"PASS","synthetic_cases":cases}
    (args.output/"validation.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report))
if __name__=="__main__":main()
