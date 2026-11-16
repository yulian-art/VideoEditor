"""Browser acceptance for automatic analysis, manual confirmation, rerun, cancel, failure."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alignstudio.server import AppServer, Handler
from alignstudio.model import Project, load_csv, parse_timestamp


class QuietHandler(Handler):
    def log_message(self, *_args):
        pass


class CDP:
    def __init__(self, url):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n")
        self.socket.sendall(request.encode())
        headers = b""
        while not headers.endswith(b"\r\n\r\n"):
            headers += self.socket.recv(1)
        if not headers.startswith(b"HTTP/1.1 101 "):
            raise RuntimeError(headers.decode())
        self.sequence = 0

    def exact(self, length):
        result = b""
        while len(result) < length:
            block = self.socket.recv(length - len(result))
            if not block:
                raise RuntimeError("Chrome CDP disconnected")
            result += block
        return result

    def send(self, payload, opcode=1):
        length = len(payload)
        header = bytes([0x80 | opcode])
        header += bytes([0x80 | length]) if length < 126 else (
            bytes([0x80 | 126]) + struct.pack(">H", length) if length <= 65535 else
            bytes([0x80 | 127]) + struct.pack(">Q", length))
        mask = os.urandom(4)
        self.socket.sendall(header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))

    def receive(self):
        fragments = []
        while True:
            first, second = self.exact(2)
            length = second & 127
            if length == 126:
                length = struct.unpack(">H", self.exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self.exact(8))[0]
            mask = self.exact(4) if second & 128 else None
            payload = self.exact(length)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if first & 15 == 9:
                self.send(payload, 10)
                continue
            if first & 15 == 8:
                raise RuntimeError("Chrome closed CDP")
            fragments.append(payload)
            if first & 0x80:
                return json.loads(b"".join(fragments))

    def call(self, method, params=None):
        self.sequence += 1
        self.send(json.dumps({"id": self.sequence, "method": method, "params": params or {}}).encode())
        while True:
            row = self.receive()
            if row.get("id") == self.sequence:
                if "error" in row:
                    raise RuntimeError(row["error"])
                return row.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", dict(expression=expression, awaitPromise=True, returnByValue=True))
        if "exceptionDetails" in result:
            raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False))
        return result.get("result", {}).get("value")

    def wait(self, expression, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = self.evaluate(expression)
            if value:
                return value
            time.sleep(0.15)
        raise RuntimeError("Browser condition timed out: " + expression)

    def close(self):
        self.socket.close()


def main():
    parser = argparse.ArgumentParser()
    for name in ("chrome", "ffmpeg", "video", "audio", "reference", "settings"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    reference = load_csv(args.reference)
    fixture = Project(name="自动粗调 UI 验收", video_path=args.video, audio_path=args.audio,
                      anchors=[reference.anchors[0], reference.anchors[10],
                               reference.anchors[-2], reference.anchors[-1]])
    fixture.anchors[1].locked = True
    original = {anchor.id: (anchor.target, anchor.source, anchor.locked) for anchor in fixture.anchors}
    fixture_path = args.output / "ui-fixture.align.json"
    fixture_path.write_text(json.dumps(fixture.to_dict(), ensure_ascii=False), encoding="utf-8")
    settings = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    server = AppServer(("127.0.0.1", 0), QuietHandler, token="auto-acceptance", ffmpeg=args.ffmpeg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/?token=auto-acceptance"
    browser = cdp = None
    profile_context = tempfile.TemporaryDirectory(prefix="align-ui-")
    result = {"status": "FAIL"}
    try:
        profile = profile_context.name
        if True:
            with (args.output / "chrome.log").open("wb") as log:
                browser = subprocess.Popen([
                    args.chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--disable-extensions", "--disable-background-networking",
                    "--remote-debugging-port=0", "--window-size=1600,1200",
                    "--user-data-dir=" + profile, url,
                ], stdout=log, stderr=subprocess.STDOUT)
                active = Path(profile) / "DevToolsActivePort"
                deadline = time.monotonic() + 15
                while not active.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                port = int(active.read_text().splitlines()[0])
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
                    pages = json.load(response)
                cdp = CDP(next(page["webSocketDebuggerUrl"] for page in pages if page["type"] == "page"))
                cdp.wait("document.getElementById('appStatus')?.textContent.includes('FFmpeg 已就绪')")
                cdp.evaluate("window.confirm=()=>true; window.originalFetch=window.fetch; window.fetch=(url,opts)=>"
                             "(url==='/api/dialog'?Promise.resolve(new Response(JSON.stringify({ok:true,path:" +
                             json.dumps(str(fixture_path.resolve())) + "}),{headers:{'Content-Type':'application/json'}}))"
                             ":window.originalFetch(url,opts)); document.getElementById('openProjectBtn').click();")
                cdp.wait("document.getElementById('projectName').textContent.includes('自动粗调 UI 验收')")
                cdp.wait("document.getElementById('appStatus').textContent.includes('项目已载入')", 60)
                values = {
                    "analysisTranscript": settings["transcript_path"], "analysisOcr": settings["ocr_path"],
                    "analysisPython": settings["python"], "analysisPaths": ";".join(settings["module_paths"]),
                    "analysisCrop": ",".join(str(value) for value in settings["crop"]),
                    "analysisCacheDir": settings["cache_dir"], "refineBudget": "0", "sampleInterval": "60",
                }
                cdp.evaluate("for (const [key,value] of Object.entries(" + json.dumps(values) +
                             ")) document.getElementById(key).value=value;"
                             "document.getElementById('analysisCacheOnly').checked=true;"
                             "document.getElementById('startAnalysisBtn').click();")
                cdp.wait("document.getElementById('analysisMessage').textContent.startsWith('已完成')", 60)
                cdp.wait("!!document.querySelector('#candidateList .button')", 10)
                cdp.evaluate("document.querySelector('#candidateList .button').click();")
                cdp.wait("document.querySelector('#candidateList .button').textContent.includes('已确认')")
                cdp.evaluate("document.getElementById('saveProjectBtn').click()")
                cdp.wait("document.getElementById('appStatus').textContent.includes('项目已保存')")
                saved = json.loads(fixture_path.read_text(encoding="utf-8"))
                preserved = {anchor["id"]: (anchor["target"], anchor["source"], anchor["locked"])
                             for anchor in saved["anchors"]}
                assert all(preserved[key] == value for key, value in original.items())
                confirmed = [anchor for anchor in saved["anchors"] if anchor["id"] not in original
                             and anchor["locked"] and anchor["origin"] == "manual"]
                assert confirmed, "Candidate confirmation was not saved"
                cdp.evaluate("document.getElementById('startAnalysisBtn').click()")
                cdp.wait("document.getElementById('cancelAnalysisBtn').hidden===false")
                cdp.wait("document.getElementById('analysisMessage').textContent.startsWith('已完成')", 60)
                cdp.evaluate("document.getElementById('saveProjectBtn').click()")
                cdp.wait("document.getElementById('appStatus').textContent.includes('项目已保存')")
                rerun = json.loads(fixture_path.read_text(encoding="utf-8"))
                assert all(any(row == anchor for row in rerun["anchors"]) for anchor in confirmed)
                anchors_before = rerun["anchors"]
                assert cdp.evaluate("getComputedStyle(document.getElementById('emptyPlayer')).display==='none'")
                cdp.wait("document.getElementById('boardVideo').readyState>=2 || !!document.getElementById('boardVideo').error", 15)
                preview = cdp.evaluate("(()=>{const v=document.getElementById('boardVideo');return {readyState:v.readyState,width:v.videoWidth,height:v.videoHeight,error:v.error?.message||null}})()")
                if preview["width"] == 0:
                    cdp.wait("document.getElementById('framePreview').naturalWidth>0 && !!document.getElementById('framePreview').dataset.sourceTime", 20)
                    preview["compatibility_frame_width"] = cdp.evaluate("document.getElementById('framePreview').naturalWidth")
                    preview["displayed_source_time"] = cdp.evaluate("Number(document.getElementById('framePreview').dataset.sourceTime)")
                    cdp.evaluate("document.getElementById('markBoardBtn').click()")
                    marker = cdp.evaluate("document.getElementById('boardMarkerValue').textContent")
                    assert abs(parse_timestamp(marker) - preview["displayed_source_time"]) <= 0.001
                    preview["mark_uses_displayed_frame_pts"] = True
                cdp.evaluate("document.querySelectorAll('.path-grid input').forEach(el=>el.type='password'); document.getElementById('toastRegion').replaceChildren();document.getElementById('candidateDetails').open=true;")
                shot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
                (args.output / "auto-ui.png").write_bytes(base64.b64decode(shot["data"]))
                if preview["width"] == 0:
                    source = rerun["anchors"][2]["source"]
                    cdp.evaluate("document.querySelectorAll('#anchorTable .source-link')[2].click()")
                    cdp.wait("Math.abs(Number(document.getElementById('framePreview').dataset.sourceTime)-" + str(source) + ")<0.1", 15)
                    preview["source_table_inspection"] = True
                # Cancel a new dense sampling request; current editor data must survive.
                cdp.evaluate("document.getElementById('sampleInterval').value='1';"
                             "document.getElementById('startAnalysisBtn').click();")
                cdp.wait("document.getElementById('cancelAnalysisBtn').hidden===false")
                cdp.evaluate("document.getElementById('cancelAnalysisBtn').click()")
                cdp.wait("document.getElementById('analysisMessage').textContent.includes('已取消')", 30)
                # Missing cache is a visible failure, not a successful empty analysis.
                cdp.evaluate("document.getElementById('analysisTranscript').value='missing-transcript.jsonl';"
                             "document.getElementById('sampleInterval').value='60';"
                             "document.getElementById('startAnalysisBtn').click()")
                cdp.wait("document.getElementById('analysisMessage').textContent.includes('分析失败')", 30)
                cdp.evaluate("document.getElementById('saveProjectBtn').click()")
                cdp.wait("document.getElementById('appStatus').textContent.includes('项目已保存')")
                after_failure = json.loads(fixture_path.read_text(encoding="utf-8"))
                assert after_failure["anchors"] == anchors_before, "Cancel/failure changed editor anchors"
                result = {
                    "status": "PASS", "source_preview": preview, "empty_overlay_hidden": True, "analysis_started_from_ui": True, "candidates_and_reviews_rendered": True,
                    "candidate_confirmation_saved": True, "manual_anchors_preserved": True,
                    "confirmed_candidate_survived_rerun": True, "cancel_preserved_editor": True,
                    "failure_preserved_editor": True, "jobs": [job.snapshot() for job in server.jobs.values()],
                }
                print(json.dumps({key: value for key, value in result.items() if key != "jobs"}, ensure_ascii=False, indent=2))
                try:
                    cdp.call("Browser.close")
                except (OSError, RuntimeError):
                    pass
                cdp.close()
                cdp = None
                browser.wait(timeout=10)
                browser = None
    finally:
        for job in server.jobs.values():
            if job.snapshot()["state"] in ("running", "queued"):
                job.cancel()
            if getattr(job, "thread", None):
                job.thread.join(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if cdp:
            try:
                cdp.call("Browser.close")
            except (OSError, RuntimeError):
                pass
            cdp.close()
        if browser:
            browser.terminate()
            browser.wait(timeout=10)
        profile_context.cleanup()
        (args.output / "auto-ui.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
