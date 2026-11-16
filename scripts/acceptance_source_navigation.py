"""Real Chrome/HEVC source-navigation regression and v0.3 click reproduction."""
from __future__ import annotations
import argparse
import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
TOKEN="source-navigation-acceptance"


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--serve-root",type=Path)
    parser.add_argument("--port-file",type=Path)
    parser.add_argument("--before-root",type=Path)
    parser.add_argument("--before-result",type=Path)
    parser.add_argument("--after-60",type=Path)
    parser.add_argument("--after-20",type=Path)
    parser.add_argument("--legacy-only",action="store_true")
    parser.add_argument("--video")
    parser.add_argument("--audio")
    parser.add_argument("--chrome")
    parser.add_argument("--ffmpeg",required=True)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    if args.serve_root:
        sys.path.insert(0,str(args.serve_root.resolve()))
        from alignstudio.server import AppServer,Handler
        class Quiet(Handler):
            def log_message(self,*_args): pass
        server=AppServer(("127.0.0.1",0),Quiet,TOKEN,args.ffmpeg)
        args.port_file.write_text(str(server.server_address[1]),encoding="ascii")
        server.serve_forever()
        return
    sys.path[:0]=[str(ROOT),str(ROOT/"scripts")]
    from acceptance_auto_ui import CDP
    from alignstudio.model import Project,map_target_to_source
    from alignstudio.matching import merge_result
    args.output.mkdir(parents=True,exist_ok=True)
    records={"status":"FAIL","before":[],"after":{}}
    old=json.loads(args.before_result.read_text(encoding="utf-8"))
    baseline=merge_result(Project.from_dict(old["project"]),old)
    # Choose reproducible failures using stored source vs current mapping, no reference.
    review_index=max((i for i,r in enumerate(old["review_intervals"]) if r.get("source_end") is not None),
                     key=lambda i:abs(old["review_intervals"][i]["source_end"]-
                         map_target_to_source(baseline.anchors,old["review_intervals"][i]["target_start"])))
    low_index=max((i for i,r in enumerate(old["candidates"]) if r["status"]=="review"),
                  key=lambda i:abs(old["candidates"][i]["source"]-
                      map_target_to_source(baseline.anchors,old["candidates"][i]["target"])))
    cases=[dict(kind="candidate",index=low_index,source=old["candidates"][low_index]["source"],
                target=old["candidates"][low_index]["target"]),
           dict(kind="review",index=review_index,source=old["review_intervals"][review_index]["source_end"],
                target=old["review_intervals"][review_index]["target_start"])]
    artifacts=[]
    for version,app_root,runs in (
        ("before",args.before_root.resolve(),[("legacy",old,baseline)]),
        ("after",ROOT,[("legacy",old,baseline)]+[(str(interval),r,merge_result(Project.from_dict(r["project"]),r))
          for interval,path in ((60,args.after_60),(20,args.after_20))
          for r in [json.loads(path.read_text(encoding="utf-8"))]])):
        if args.legacy_only:runs=[run for run in runs if run[0]=="legacy"]
        server_proc=browser=cdp=None
        profile=tempfile.TemporaryDirectory(prefix="source-nav-")
        portfile=args.output/(version+"-port.txt")
        portfile.unlink(missing_ok=True)
        try:
            with (args.output/(version+"-server.log")).open("wb") as log:
                server_proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),
                    "--serve-root",str(app_root),"--port-file",str(portfile),"--ffmpeg",args.ffmpeg],
                    stdout=log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+20
            while not portfile.exists() and time.monotonic()<deadline:
                if server_proc.poll() is not None:raise RuntimeError("server exited")
                time.sleep(.1)
            port=int(portfile.read_text())
            url=f"http://127.0.0.1:{port}/?token={TOKEN}"
            with (args.output/(version+"-chrome.log")).open("wb") as log:
                browser=subprocess.Popen([args.chrome,"--headless=new","--disable-gpu","--no-sandbox",
                    "--disable-extensions","--disable-background-networking","--autoplay-policy=no-user-gesture-required",
                    "--remote-debugging-port=0","--window-size=1660,1350","--user-data-dir="+profile.name,url],
                    stdout=log,stderr=subprocess.STDOUT)
            active=Path(profile.name)/"DevToolsActivePort"
            deadline=time.monotonic()+20
            while not active.exists() and time.monotonic()<deadline:time.sleep(.1)
            debug_port=int(active.read_text().splitlines()[0])
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list",timeout=10) as response:
                pages=json.load(response)
            cdp=CDP(next(r["webSocketDebuggerUrl"] for r in pages if r["type"]=="page"))
            cdp.wait("document.getElementById('appStatus')?.textContent.includes('FFmpeg 已就绪')",30)
            cdp.evaluate("window.confirm=()=>true;window.originalFetch=window.fetch;window.dialogPath='';"
                         "window.fetch=(url,opts)=>(url==='/api/dialog'?Promise.resolve(new Response(JSON.stringify({ok:true,path:window.dialogPath}),"
                         "{headers:{'Content-Type':'application/json'}})):window.originalFetch(url,opts));")

            for label,result,project in runs:
                if args.video:project.video_path=args.video
                if args.audio:project.audio_path=args.audio
                fixture=(args.output/f"{version}-{label}.align.json").resolve()
                fixture.write_text(json.dumps(project.to_dict(),ensure_ascii=False),encoding="utf-8")
                cdp.evaluate("window.dialogPath="+json.dumps(str(fixture))+";"
                    "document.getElementById('appStatus').lastElementChild.textContent='等待打开';"
                    "document.getElementById('openProjectBtn').click()")
                cdp.wait("document.getElementById('appStatus').textContent.includes('项目已载入')",60)
                cdp.wait("!document.getElementById('framePreview').hidden&&document.getElementById('framePreview').naturalWidth>0",30)
                def click(selector,source,require_source):
                    cdp.evaluate("document.querySelector("+json.dumps(selector)+").click()")
                    if require_source:
                        condition="Math.abs(Number(f.dataset.requestedSource)-"+str(source)+")<.000001&&Math.abs(Number(f.dataset.sourceTime)-"+str(source)+")<.1"
                    else:
                        condition="Math.abs(Number(f.dataset.requestedSource)-"+str(source)+")<.1"
                    cdp.wait("(()=>{const f=document.getElementById('framePreview');return f.naturalWidth>0&&"+condition+"})()",25)
                    return cdp.evaluate("(()=>{const f=document.getElementById('framePreview');return {requested:Number(f.dataset.requestedSource),"
                                        "actual:Number(f.dataset.sourceTime),audio:document.getElementById('narrationAudio').currentTime,"
                                        "caption:document.getElementById('captionOverlay').textContent,inspection:document.getElementById('inspectionStatus')?.textContent}})()")
                if label=="legacy":
                    for case in cases:
                        selector=f"#{'candidateList' if case['kind']=='candidate' else 'reviewList'} li:nth-child({case['index']+1}) .time-link"
                        mapped=map_target_to_source(baseline.anchors,case["target"])
                        frame=click(selector,case["source"] if version=="after" else mapped,version=="after")
                        row={**case,**frame,"mapped_in_0_3":mapped,"distance_from_saved_source":round(abs(frame["actual"]-case["source"]),6)}
                        if version=="before":
                            assert row["distance_from_saved_source"]>100,row
                            records["before"].append(row)
                        else:
                            assert row["distance_from_saved_source"]<.1,row
                            records["after"].setdefault("legacy_clicks",[]).append(row)
                        # Keep the decoded frame for independent visual/pixel comparison.
                        raw=cdp.evaluate("(()=>{const f=document.getElementById('framePreview'),c=document.createElement('canvas');c.width=f.naturalWidth;c.height=f.naturalHeight;c.getContext('2d').drawImage(f,0,0);return c.toDataURL('image/png').split(',')[1]})()")
                        frame_path=args.output/f"{version}-{case['kind']}-frame.png"
                        frame_path.write_bytes(base64.b64decode(raw));artifacts.append(frame_path.name)
                        cdp.evaluate("document.querySelectorAll('.source-card input').forEach(el=>el.type='password');"
                                     "document.getElementById('toastRegion').replaceChildren();window.scrollTo(0,0)")
                        shot=cdp.call("Page.captureScreenshot",{"format":"png","captureBeyondViewport":False})
                        (args.output/f"{version}-{case['kind']}.png").write_bytes(base64.b64decode(shot["data"]))
                    if args.legacy_only and version=="after":
                        cdp.evaluate("document.getElementById('saveProjectBtn').click()")
                        cdp.wait("document.getElementById('appStatus').textContent.includes('项目已保存')",30)
                        saved=json.loads(fixture.read_text(encoding="utf-8"))
                        assert saved["analysis"]["review_projection_version"]==2
                        for key in ("anchors","cuts","captions"):
                            assert saved[key]==project.to_dict()[key],key
                        records["legacy_migration_saved_without_edit_changes"]=True
                    continue

                rows=[]
                candidates=result["candidates"];reviews=result["review_intervals"]
                for kind,items in (("candidate",candidates),("review",reviews)):
                    for index,item in enumerate(items):
                        source=item.get("source") if kind=="candidate" else item.get("inspection_source")
                        if source is None and kind=="review":
                            source=item.get("source_end")
                            if source is None:source=item.get("source_start")
                            if source is None:source=item.get("context_source")
                        if source is None:
                            continue
                        selector=f"#{'candidateList' if kind=='candidate' else 'reviewList'} li:nth-child({index+1}) .time-link"
                        frame=click(selector,source,True)
                        assert abs(frame["actual"]-source)<.1
                        rows.append(dict(kind=kind,id=item["id"],stored_source=source,**frame))
                    print("clicks",label,kind,len(rows),flush=True)
                unknown=[(i,r) for i,r in enumerate(reviews) if r.get("source_end") is not None and r.get("target_start") is None]
                assert unknown
                index,item=unknown[0]
                audio_before=cdp.evaluate("document.getElementById('narrationAudio').currentTime")
                frame=click(f"#reviewList li:nth-child({index+1}) .time-link",item["source_end"],True)
                assert abs(frame["audio"]-audio_before)<.001
                assert "录音位置待确定" in cdp.evaluate(f"document.querySelector('#reviewList li:nth-child({index+1}) .time-link').textContent")
                # Listening while inspecting must hold the saved frame, with captions on audio time.
                cdp.evaluate("document.getElementById('playBtn').click()")
                cdp.wait("!document.getElementById('narrationAudio').paused",10)
                time.sleep(1.3)
                observed=cdp.evaluate("({audio:document.getElementById('narrationAudio').currentTime,"
                                    "source:Number(document.getElementById('framePreview').dataset.requestedSource),"
                                    "caption:document.getElementById('captionOverlay').textContent})")
                assert observed["audio"]>audio_before+.2
                assert abs(observed["source"]-item["source_end"])<1e-6
                expected_caption=next((c.text for c in project.captions if c.start<=observed["audio"]<c.end),"")
                assert observed["caption"]==expected_caption,(observed,expected_caption)
                cdp.evaluate("document.getElementById('playBtn').click();document.getElementById('returnMappingBtn').click()")
                audio=cdp.evaluate("document.getElementById('narrationAudio').currentTime")
                expected=map_target_to_source(project.anchors,audio,project.cuts)
                cdp.wait("Math.abs(Number(document.getElementById('framePreview').dataset.requestedSource)-"+str(expected)+")<.01",25)
                assert cdp.evaluate("document.getElementById('inspectionPanel').hidden")
                # The save operation is real, and navigation must not mutate any edit.
                cdp.evaluate("document.getElementById('saveProjectBtn').click()")
                cdp.wait("document.getElementById('appStatus').textContent.includes('项目已保存')",30)
                saved=json.loads(fixture.read_text(encoding="utf-8"))
                for key in ("anchors","cuts","captions"):
                    assert saved[key]==project.to_dict()[key],key
                records["after"][label]=dict(status="PASS",all_source_buttons=len(rows),candidate_buttons=len(candidates),
                    review_buttons=len(reviews),unknown_source_review_items=len(unknown),frame_tolerance_seconds=.1,
                    unknown_target_preserves_audio_time=True,inspection_holds_while_audio_and_captions_advance=True,
                    return_to_mapping=True,anchors_cuts_and_captions_unchanged=True,clicks=rows)
                if label=="20":
                    index,tail=next((i,r) for i,r in enumerate(reviews) if r["kind"]=="missing_board")
                    source=tail["context_source"]
                    frame=click(f"#reviewList li:nth-child({index+1}) .time-link",source,True)
                    assert "仅作上下文" in frame["inspection"]
                    records["after"][label]["missing_tail_click"]=frame
                    cdp.evaluate("document.querySelectorAll('.source-card input').forEach(el=>el.type='password');"
                                 "document.getElementById('toastRegion').replaceChildren();window.scrollTo(0,0);"
                                 "document.getElementById('reviewList').scrollTop=document.querySelector('#reviewList li:nth-child("+str(index+1)+")').offsetTop-document.getElementById('reviewList').offsetTop")
                    shot=cdp.call("Page.captureScreenshot",{"format":"png","captureBeyondViewport":False})
                    (args.output/"final-20-missing-tail.png").write_bytes(base64.b64decode(shot["data"]))
                    cdp.evaluate("document.getElementById('returnMappingBtn').click()")
                    expected=map_target_to_source(project.anchors,tail["target_start"]+1,project.cuts)
                    cdp.evaluate("document.getElementById('scrubber').value="+str(tail["target_start"]+1)+";document.getElementById('scrubber').dispatchEvent(new Event('input',{bubbles:true}))")
                    cdp.wait("Math.abs(Number(document.getElementById('framePreview').dataset.requestedSource)-"+str(expected)+")<.001",25)
                    assert abs(expected-source)<.001
                    records["after"][label]["terminal_freeze_after_return"]=dict(source=expected,requires_review=True,overlay_sources=result.get("source_quality",{}).get("overlay_sources"))
        finally:
            if cdp:cdp.close()
            for process in (browser,server_proc):
                if process and process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
            try:profile.cleanup()
            except PermissionError:pass
    records["status"]="PASS"
    records["artifacts"]=artifacts
    (args.output/"source-navigation.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in records.items() if k!="after"},ensure_ascii=False))
if __name__=="__main__":main()
