"""Chrome acceptance: cut policy, exact jump preview, fixed captions, reload and undo/redo."""
from __future__ import annotations
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from acceptance_auto_ui import CDP, QuietHandler
from alignstudio.server import AppServer
from alignstudio.model import Anchor, Caption, Project, load_csv, map_target_to_source, parse_timestamp


def main():
    parser=argparse.ArgumentParser()
    for name in ("chrome","ffmpeg","video","audio","reference"):
        parser.add_argument("--"+name,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    ref=load_csv(args.reference)
    fixture=Project(name="独立裁剪 UI 验收",video_path=args.video,audio_path=args.audio,
                    anchors=[Anchor("start",0,0,locked=True),
                             Anchor("auto8",8,8,origin="auto",confidence=0.8),
                             Anchor("manual9",9,9,locked=True,content="人工证据"),
                             Anchor("end",ref.duration,ref.anchors[-1].source,locked=True)],
                    captions=[Caption(5.5,6.5,"字幕仍绑定六秒录音")])
    fixture_path=(args.output/"cut-fixture.align.json").resolve()
    relative_fixture=fixture.to_dict()
    relative_fixture["video_path"]=os.path.relpath(args.video,fixture_path.parent)
    relative_fixture["audio_path"]=os.path.relpath(args.audio,fixture_path.parent)
    fixture_path.write_text(json.dumps(relative_fixture,ensure_ascii=False),encoding="utf-8")
    server=AppServer(("127.0.0.1",0),QuietHandler,"cut-acceptance",args.ffmpeg)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f"http://127.0.0.1:{server.server_address[1]}/?token=cut-acceptance"
    profile=tempfile.TemporaryDirectory(prefix="cut-ui-")
    browser=cdp=None
    result={"status":"FAIL"}
    try:
        with (args.output/"chrome.log").open("wb") as log:
            browser=subprocess.Popen([args.chrome,"--headless=new","--disable-gpu","--no-sandbox",
                  "--disable-extensions","--disable-background-networking","--remote-debugging-port=0",
                  "--window-size=1600,1500","--user-data-dir="+profile.name,url],stdout=log,stderr=subprocess.STDOUT)
            active=Path(profile.name)/"DevToolsActivePort"
            deadline=time.monotonic()+15
            while not active.exists() and time.monotonic()<deadline: time.sleep(0.1)
            port=int(active.read_text().splitlines()[0])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list",timeout=10) as response:
                pages=json.load(response)
            cdp=CDP(next(row["webSocketDebuggerUrl"] for row in pages if row["type"]=="page"))
            cdp.wait("document.getElementById('appStatus')?.textContent.includes('FFmpeg 已就绪')")
            cdp.evaluate("window.originalFetch=window.fetch;window.fetch=(url,opts)=>(url==='/api/dialog'?"
                "Promise.resolve(new Response(JSON.stringify({ok:true,path:"+json.dumps(str(fixture_path))+
                "}),{headers:{'Content-Type':'application/json'}})):window.originalFetch(url,opts));"
                "document.getElementById('openProjectBtn').click()")
            cdp.wait("document.getElementById('appStatus').textContent.includes('项目已载入')",60)
            cdp.evaluate("for(const [key,value] of Object.entries({cutSourceStart:6,cutSourceEnd:10,cutTarget:6}))document.getElementById(key).value=value;"
                         "document.getElementById('createCutBtn').click()")
            cdp.wait("document.getElementById('toastRegion').textContent.includes('锁定')")
            assert cdp.evaluate("document.querySelectorAll('#cutList [data-cut-id]').length")==0
            # Unlock explicitly through the real inspector, retaining its stable ID.
            cdp.evaluate("document.querySelectorAll('#anchorTable tr')[2].click();document.getElementById('anchorLocked').checked=false;document.getElementById('applyAnchorBtn').click();document.getElementById('toastRegion').replaceChildren();document.getElementById('createCutBtn').click()")
            cdp.wait("document.getElementById('toastRegion').textContent.includes('人工点')")
            assert cdp.evaluate("document.querySelectorAll('#cutList [data-cut-id]').length")==0
            def save():
                cdp.evaluate("document.getElementById('appStatus').lastElementChild.textContent='等待保存';document.getElementById('saveProjectBtn').click()")
                cdp.wait("document.getElementById('appStatus').textContent.includes('项目已保存')")
                return json.loads(fixture_path.read_text(encoding="utf-8"))
            before_cut=save()
            cdp.evaluate("document.getElementById('cutSuspendManual').checked=true;document.getElementById('createCutBtn').click()")
            cdp.wait("document.querySelectorAll('#cutList [data-cut-id]').length===1")
            saved=save()
            p=Project.from_dict(saved)
            assert {row.id for row in p.cuts[0].excluded_anchors}=={"auto8","manual9"}
            assert p.captions==fixture.captions and p.duration==fixture.duration
            preview=[]
            def seek_check(target):
                expected=map_target_to_source(p.anchors,target,p.cuts)
                cdp.evaluate("document.getElementById('scrubber').value="+str(target)+";document.getElementById('scrubber').dispatchEvent(new Event('input',{bubbles:true}))")
                cdp.wait("(()=>{const f=document.getElementById('framePreview'),v=document.getElementById('boardVideo');return !f.hidden?"
                         "(Math.abs(Number(f.dataset.requestedSource)-"+str(expected)+")<0.001&&Math.abs(Number(f.dataset.sourceTime)-"+str(expected)+")<0.1):"
                         "(!v.seeking&&Math.abs(v.currentTime-"+str(expected)+")<0.1)})()",20)
                cdp.evaluate("document.getElementById('markBoardBtn').click()")
                actual=parse_timestamp(cdp.evaluate("document.getElementById('boardMarkerValue').textContent"))
                assert abs(actual-expected)<0.1
                preview.append({"target":target,"mapped_source":expected,"displayed_source":actual})
            for target in (5.9,6,6.1): seek_check(target)
            assert cdp.evaluate("document.getElementById('captionOverlay').textContent")=="字幕仍绑定六秒录音"
            cdp.evaluate("document.getElementById('openProjectBtn').click()")
            cdp.wait("document.getElementById('appStatus').textContent.includes('项目已载入')",60)
            assert cdp.evaluate("document.querySelectorAll('#cutList [data-cut-id]').length")==1
            seek_check(6)
            cdp.evaluate("document.querySelectorAll('.source-card input').forEach(el=>el.type='password');document.getElementById('toastRegion').replaceChildren()")
            shot=cdp.call("Page.captureScreenshot",{"format":"png","captureBeyondViewport":False})
            (args.output/"cuts-ui.png").write_bytes(base64.b64decode(shot["data"]))
            cdp.evaluate("document.querySelector('#cutList [data-restore-cut]').click()")
            cdp.wait("document.querySelectorAll('#cutList [data-cut-id]').length===0")
            cdp.evaluate("document.getElementById('undoBtn').click()")
            cdp.wait("document.querySelectorAll('#cutList [data-cut-id]').length===1")
            cdp.evaluate("document.getElementById('redoBtn').click()")
            cdp.wait("document.querySelectorAll('#cutList [data-cut-id]').length===0")
            restored=save()
            assert restored["anchors"]==before_cut["anchors"]
            assert restored["captions"]==before_cut["captions"]

            # Drag across dense unlocked automatic points, with a real release choice.
            drag_project=Project(name="自动点释放验收",video_path=args.video,audio_path=args.audio,
                anchors=[Anchor("start",0,0,locked=True),Anchor("drag",6,6),
                         Anchor("auto7",7,7,origin="auto"),Anchor("auto8",8,8,origin="auto"),
                         Anchor("end",ref.duration,ref.anchors[-1].source,locked=True)])
            fixture_path.write_text(json.dumps(drag_project.to_dict(),ensure_ascii=False),encoding="utf-8")
            cdp.evaluate("document.getElementById('appStatus').lastElementChild.textContent='等待打开';document.getElementById('openProjectBtn').click()")
            cdp.wait("document.getElementById('appStatus').textContent.includes('项目已载入')",60)
            drag_script = """(()=>{
              const button=document.querySelector('#anchorLayer [data-id="drag"]');
              const b=button.getBoundingClientRect(),r=document.getElementById('boardTrack').getBoundingClientRect();
              const y=b.top+b.height/2;
              button.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:b.left+b.width/2,clientY:y,pointerId:1,button:0,buttons:1}));
              window.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,clientX:r.left+r.width*9/120,clientY:y,pointerId:1,buttons:1}));
              window.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:r.left+r.width*9/120,clientY:y,pointerId:1,button:0}));
            })()"""
            cdp.evaluate("window.confirm=(message)=>{window.releaseMessage=message;return false}")
            cdp.evaluate(drag_script)
            rejected_drag=save()
            assert rejected_drag["anchors"]==drag_project.to_dict()["anchors"]
            assert "2" in cdp.evaluate("window.releaseMessage")
            cdp.evaluate("window.confirm=(message)=>{window.releaseMessage=message;return true}")
            cdp.evaluate(drag_script)
            accepted_drag=save()
            moved=next(row for row in accepted_drag["anchors"] if row["id"]=="drag")
            assert abs(moved["target"]-9)<0.01 and moved["source"]==6
            assert {row["id"] for row in accepted_drag["anchors"]}=={"start","drag","end"}

            # Pair the same held frame at another narration time without moving its old boundary.
            freeze_project=Project(name="定格多时刻配对验收",video_path=args.video,audio_path=args.audio,
                anchors=[Anchor("start",0,0,locked=True),Anchor("freeze-left",10,12),
                         Anchor("end",ref.duration,12,locked=True)])
            fixture_path.write_text(json.dumps(freeze_project.to_dict(),ensure_ascii=False),encoding="utf-8")
            cdp.evaluate("document.getElementById('appStatus').lastElementChild.textContent='等待打开';document.getElementById('openProjectBtn').click()")
            cdp.wait("document.getElementById('appStatus').textContent.includes('项目已载入')",60)
            cdp.evaluate("document.getElementById('scrubber').value=20;document.getElementById('scrubber').dispatchEvent(new Event('input',{bubbles:true}))")
            cdp.wait("Number(document.getElementById('framePreview').dataset.requestedSource)===12&&document.getElementById('framePreview').naturalWidth>0",20)
            cdp.evaluate("document.getElementById('markBoardBtn').click()")
            freeze_pts=parse_timestamp(cdp.evaluate("document.getElementById('boardMarkerValue').textContent"))
            cdp.evaluate("document.getElementById('scrubber').value=25;document.getElementById('scrubber').dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('markAudioBtn').click();document.getElementById('pairBtn').click()")
            paired_freeze=save()
            assert next(row for row in paired_freeze["anchors"] if row["id"]=="freeze-left")["target"]==10
            paired=next(row for row in paired_freeze["anchors"] if abs(row["target"]-25)<0.001)
            assert paired["source"]==12 and paired["locked"]
            assert abs(paired["match"]["displayed_frame_pts"]-freeze_pts)<0.001

            # Head restoration must keep the user's explicit unlocked state.
            cdp.evaluate("document.querySelectorAll('#anchorTable tr')[0].click();document.getElementById('anchorLocked').checked=false;document.getElementById('applyAnchorBtn').click()")
            before_head=save()
            assert before_head["anchors"][0]["locked"] is False
            cdp.evaluate("for(const [key,value] of Object.entries({cutSourceStart:0,cutSourceEnd:2,cutTarget:0}))document.getElementById(key).value=value;document.getElementById('cutSuspendManual').checked=true;document.getElementById('createCutBtn').click()")
            cdp.wait("document.querySelectorAll('#cutList [data-cut-id]').length===1")
            cdp.evaluate("document.querySelector('#cutList [data-restore-cut]').click()")
            cdp.wait("document.querySelectorAll('#cutList [data-cut-id]').length===0")
            after_head=save()
            assert after_head["anchors"]==before_head["anchors"]


            pair_drag_results=[]
            for case,board_time,audio_time in (("ahead-5s",6,11),("behind-5s",11,6)):
                pair_project=Project(name="五秒拖点验收",video_path=args.video,audio_path=args.audio,
                    anchors=[Anchor("start",0,0,locked=True),Anchor("end",ref.duration,ref.duration,locked=True)])
                fixture_path.write_text(json.dumps(pair_project.to_dict(),ensure_ascii=False),encoding="utf-8")
                cdp.evaluate("document.getElementById('appStatus').lastElementChild.textContent='等待打开';document.getElementById('openProjectBtn').click()")
                cdp.wait("document.getElementById('appStatus').textContent.includes('项目已载入')",60)
                def initial_seek(target):
                    cdp.evaluate("document.getElementById('scrubber').value="+str(target)+";document.getElementById('scrubber').dispatchEvent(new Event('input',{bubbles:true}))")
                    cdp.wait("Math.abs(Number(document.getElementById('framePreview').dataset.requestedSource)-"+str(target)+")<0.001&&document.getElementById('framePreview').naturalWidth>0",20)
                if board_time<audio_time:
                    initial_seek(board_time)
                    cdp.evaluate("document.getElementById('markBoardBtn').click()")
                    displayed=parse_timestamp(cdp.evaluate("document.getElementById('boardMarkerValue').textContent"))
                    initial_seek(audio_time)
                    cdp.evaluate("document.getElementById('markAudioBtn').click()")
                else:
                    initial_seek(audio_time)
                    cdp.evaluate("document.getElementById('markAudioBtn').click()")
                    initial_seek(board_time)
                    cdp.evaluate("document.getElementById('markBoardBtn').click()")
                    displayed=parse_timestamp(cdp.evaluate("document.getElementById('boardMarkerValue').textContent"))
                cdp.evaluate("(()=>{const b=document.getElementById('boardMarkerLine'),r=document.getElementById('boardTrack').getBoundingClientRect(),a=b.getBoundingClientRect();const x=r.left+r.width*"+str(audio_time)+"/120,y=r.top+r.height/2;b.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:a.left,clientY:y,pointerId:1,buttons:1}));window.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,clientX:x,clientY:y,pointerId:1,buttons:1}));window.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,button:0}));})()")
                cdp.wait("document.getElementById('pairBtn').disabled")
                paired_data=save()
                anchor=next(row for row in paired_data["anchors"] if row["id"] not in ("start","end"))
                assert anchor["target"]==audio_time and abs(anchor["source"]-displayed)<0.001
                assert anchor["locked"] and anchor["origin"]=="manual"
                assert ("提前 5.000" if case=="ahead-5s" else "落后 5.000") in anchor["evidence"]
                cdp.evaluate("document.getElementById('scrubber').value="+str(audio_time)+";document.getElementById('scrubber').dispatchEvent(new Event('input',{bubbles:true}))")
                cdp.wait("Math.abs(Number(document.getElementById('framePreview').dataset.requestedSource)-"+str(anchor["source"])+")<0.001",20)
                actual=float(cdp.evaluate("document.getElementById('framePreview').dataset.sourceTime"))
                assert abs(actual-anchor["source"])<0.1
                pair_drag_results.append({"case":case,"board_mark_target":board_time,"audio_mark_target":audio_time,
                    "captured_source":anchor["source"],"preview_source":actual,"evidence":anchor["evidence"]})
            # Long-media PTS must use the exact integer/time-base ratio, not rounded log text.
            from urllib.parse import urlencode
            query=urlencode({"token":"cut-acceptance","path":args.video,"time":3300.123,"width":160,"exact":1})
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/frame?"+query,timeout=20) as response:
                frame_time=float(response.headers["X-Source-Time"])
                pts=int(response.headers["X-Source-PTS"])
                numerator,denominator=map(int,response.headers["X-Source-Time-Base"].split("/"))
                response.read()
            assert abs(frame_time-pts*numerator/denominator)<1e-9
            assert 3300.123<=frame_time<3301.123

            result={"status":"PASS","locked_anchor_blocks_cut":True,"manual_requires_explicit_suspend":True,
                    "automatic_and_manual_stashed":True,"preview_across_jump":preview,
                    "captions_fixed_to_audio":True,"saved_project_reloaded":True,"relative_media_refs_resolved":True,
                    "cut_restoration_undo_redo":True,"dense_auto_release_accept_reject":True,
                    "freeze_pair_preserves_original_boundary":True,"head_restores_unlocked_state":True,
                    "ahead_behind_5s_marker_drags":pair_drag_results,
                    "long_source_frame_pts":{"requested":3300.123,"pts":pts,"time_base":[numerator,denominator],"source_time":frame_time},"stable_anchor_ids_times_restored":True}
            print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        if cdp:
            try: cdp.call("Browser.close")
            except (OSError,RuntimeError): pass
            cdp.close()
        if browser:
            try: browser.wait(timeout=10)
            except subprocess.TimeoutExpired:
                browser.terminate();browser.wait(timeout=5)
        profile.cleanup()
        server.shutdown();server.server_close();thread.join(timeout=5)
        (args.output/"cuts-ui.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
