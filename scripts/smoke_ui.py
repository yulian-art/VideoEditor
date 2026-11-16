from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="启动本地服务并用无头 Chrome 检查界面脚本")
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--csv")
    args = parser.parse_args()
    evidence_dir = ROOT / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "app.py"), "--port", "0", "--no-browser", "--ffmpeg", args.ffmpeg],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    result: dict[str, object] = {"status": "FAIL"}
    try:
        deadline = time.monotonic() + 15
        url = ""
        lines = []
        assert process.stdout is not None
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if line:
                lines.append(line.rstrip())
                match = re.search(r"http://127\.0\.0\.1:\d+/\?token=[^\s]+", line)
                if match:
                    url = match.group(0)
                    break
            elif process.poll() is not None:
                break
        if not url:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError("服务未启动：" + "\n".join(lines) + "\n" + stderr)
        with urllib.request.urlopen(url, timeout=10) as response:
            html = response.read().decode("utf-8")
        if "板书对齐工作台" not in html:
            raise AssertionError("首页标题缺失")
        with tempfile.TemporaryDirectory() as profile:
            completed = subprocess.run(
                [
                    args.chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-extensions",
                    "--disable-background-networking",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=3500",
                    "--dump-dom",
                    url,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
            )
        if completed.returncode:
            raise RuntimeError(f"Chrome 退出码 {completed.returncode}: {completed.stderr[-2000:]}")
        if "FFmpeg 已就绪" not in completed.stdout:
            raise AssertionError("前端 JavaScript 未完成健康检查（可能存在语法或运行错误）")
        screenshot = evidence_dir / "ui-smoke.png"
        with tempfile.TemporaryDirectory() as profile:
            shot = subprocess.run(
                [
                    args.chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-extensions",
                    "--disable-background-networking",
                    f"--user-data-dir={profile}",
                    "--window-size=1600,1000",
                    "--virtual-time-budget=3000",
                    f"--screenshot={screenshot}",
                    url,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
            )
        if shot.returncode or not screenshot.is_file():
            raise RuntimeError(f"Chrome 截图失败：{shot.stderr[-2000:]}")
        api_result = None
        if args.csv:
            token = url.split("token=", 1)[1]
            request = urllib.request.Request(
                url.split("/?", 1)[0] + "/api/load-csv",
                data=json.dumps({"path": args.csv}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Align-Token": token},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                api_result = json.loads(response.read().decode("utf-8"))
            if len(api_result["project"]["anchors"]) < 2:
                raise AssertionError("CSV API 未返回锚点")
        result = {
            "status": "PASS",
            "page_title_present": True,
            "javascript_health_check": "FFmpeg 已就绪",
            "csv_anchor_count": len(api_result["project"]["anchors"]) if api_result else None,
            "chrome_return_code": completed.returncode,
            "screenshot": "evidence/ui-smoke.png",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        (evidence_dir / "ui-smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
