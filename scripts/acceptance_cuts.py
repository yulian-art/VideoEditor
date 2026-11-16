"""Actual FFmpeg exports across cuts, plus subtitle/AAC and project restoration checks."""
from __future__ import annotations
import argparse
import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from acceptance import frame_rgb, mean_delta
from alignstudio.editing import add_cut, remove_cut
from alignstudio.exporter import run_export
from alignstudio.model import (Anchor, Caption, Project, ProjectError, load_csv, load_captions,
                               map_target_to_source)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--ffmpeg",type=Path,required=True)
    parser.add_argument("--video",type=Path,required=True)
    parser.add_argument("--audio",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    def base():
        return Project(name="裁剪导出验收",video_path=str(args.video.resolve()),audio_path=str(args.audio.resolve()),
                       anchors=[Anchor("start",0,0,locked=True),Anchor("end",20,20,locked=True)],
                       captions=[Caption(5.5,6.5,"字幕跨过源跳变，仍绑定录音"),Caption(10,11,"十秒录音字幕")])
    p=base()
    p.anchors.insert(1,Anchor("auto8",8,8,origin="auto",confidence=0.8))
    p.anchors.insert(2,Anchor("manual9",9,9,content="人工点完整恢复"))
    middle=add_cut(p,6,10,6,manual_policy="suspend")
    head=base();head.anchors[0].locked=False
    head=add_cut(head,0,4,manual_policy="suspend")
    tail=base();tail.anchors[-1].locked=False
    tail=add_cut(tail,16,20,manual_policy="suspend")
    multi=base()
    multi.anchors[1:1]=[Anchor("freeze1",14,16),Anchor("freeze2",16,16)]
    multi=add_cut(add_cut(multi,4,6,4),10,12,10)
    repeated_tail=base()
    repeated_tail.anchors=[Anchor("start",0,0,locked=True),Anchor("freeze-start",10,16),
                           Anchor("end",20,16,locked=False)]
    repeated_tail=add_cut(repeated_tail,12,16,manual_policy="suspend")
    assert len(repeated_tail.cuts[0].excluded_anchors)==2
    cases=[
        ("middle",middle,[(5,5,9),(6,10,6),(7,10+10/14,7),(19,10+13*10/14,17)]),
        ("head",head,[(0,4,0),(5,8,5),(19,19.2,15)]),
        ("tail",tail,[(5,4,8),(10,8,18),(19,15.2,19)]),
        ("multi-freeze",multi,[(4,6,4),(10,12,10),(15,16,15),(17,17,13)]),
        ("repeated-tail",repeated_tail,[(10,6,16),(19,11.4,18)]),
    ]
    records=[]
    for name,project,samples in cases:
        exported=run_export(project,args.output/(name+".mp4"),ffmpeg_path=str(args.ffmpeg))
        assert exported["audio_verification"]["ok"]
        counted=subprocess.run([str(args.ffmpeg),"-nostdin","-hide_banner","-loglevel","error",
            "-i",exported["output"],"-map","0:v:0","-c:v","copy","-an",
            "-progress","pipe:1","-f","null",os.devnull],capture_output=True,check=True)
        counts=re.findall(rb"^frame=(\d+)",counted.stdout,re.MULTILINE)
        actual_frames=int(counts[-1])
        assert actual_frames==exported["render"]["frame_count"],(name,actual_frames,exported["render"]["frame_count"])
        assert load_captions(exported["srt"])==project.captions
        csv=load_csv(exported["csv"])
        saved=Project.from_dict(json.loads(Path(exported["project"]).read_text(encoding="utf-8")))
        assert csv.anchors==project.anchors and csv.cuts==project.cuts
        assert saved.cuts==project.cuts and saved.captions==project.captions
        comparisons=[]
        for target,expected,control in samples:
            assert abs(map_target_to_source(project.anchors,target,project.cuts)-expected)<1e-6
            output=frame_rgb(args.ffmpeg,Path(exported["output"]),target)
            samples_to_check = [expected, max(0, expected - 1/30), expected + 1/30]
            samples_to_check = [source for source in samples_to_check
                                if not any(cut.source_start <= source < cut.source_end for cut in project.cuts)]
            comparisons_to_expected = [(mean_delta(output,frame_rgb(args.ffmpeg,args.video,source)),source)
                                       for source in samples_to_check]
            delta,nearest_source = min(comparisons_to_expected)
            wrong=frame_rgb(args.ffmpeg,args.video,control)
            wrong_delta=mean_delta(output,wrong)
            if not (delta<8 and delta<wrong_delta*0.6):
                raise AssertionError(f"{name}@{target}: expected delta={delta}, control={wrong_delta}")
            comparisons.append({"target":target,"expected_source":expected,"source_sample_within_one_frame":nearest_source,"frame_tolerance_seconds":1/30,"frame_mean_delta":round(delta,4),
                                "wrong_source":control,"wrong_frame_delta":round(wrong_delta,4)})
        records.append({"name":name,"cuts":len(project.cuts),"parts":exported["render"]["parts"],
                        "frame_count":exported["render"]["frame_count"],"actual_video_packet_count":actual_frames,"audio_copy_ok":True,
                        "audio_verification":exported["audio_verification"],"subtitle_times_unchanged":True,
                        "json_csv_restored":True,"frame_checks":comparisons})
    saved_middle=Project.from_dict(json.loads(Path(args.output/"middle.align.json").read_text(encoding="utf-8"))) if (args.output/"middle.align.json").exists() else Project.from_dict(middle.to_dict())
    restored=remove_cut(saved_middle,saved_middle.cuts[0].id)
    assert restored.anchors==p.anchors and restored.captions==p.captions
    result={"status":"PASS","cases":records,"archived_manual_auto_ids_restored":True}
    (args.output/"cuts-acceptance.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"status":"PASS","cases":[{"name":row["name"],"frame_checks":len(row["frame_checks"]),
          "audio_copy_ok":row["audio_copy_ok"]} for row in records],"archived_ids_restored":True},ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
