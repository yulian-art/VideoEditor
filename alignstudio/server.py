from __future__ import annotations

import argparse
import array
import hashlib
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .analyzer import AnalysisJob
from .matching import merge_result
from .alignment import upgrade_legacy_reviews
from .editing import add_cut, remove_cut
from .exporter import ExportJob, locate_ffmpeg
from .model import Project, ProjectError, load_captions, load_csv, save_csv
from .mp4aac import compare_audio

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "static"
DIALOG_HELPER = Path(__file__).with_name("dialog_helper.py")
CACHE_ROOT = Path(tempfile.gettempdir()) / "board-align-studio-cache"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], token: str, ffmpeg: str | None):
        super().__init__(address, handler)
        self.token = token
        self.ffmpeg = ffmpeg
        self.jobs: dict[str, ExportJob | AnalysisJob] = {}
        self.jobs_lock = threading.Lock()
        self.waveform_cache: dict[tuple[str, int, int], list[float]] = {}
        self.waveform_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def log_message(self, format_string: str, *args: object) -> None:
        sys.stderr.write(f"[HTTP] {self.address_string()} {format_string % args}\n")

    def _parsed(self) -> urllib.parse.ParseResult:
        return urllib.parse.urlparse(self.path)

    def _query(self) -> dict[str, str]:
        values = urllib.parse.parse_qs(self._parsed().query)
        return {key: rows[-1] for key, rows in values.items() if rows}

    def _authorized(self) -> bool:
        query_token = self._query().get("token", "")
        header_token = self.headers.get("X-Align-Token", "")
        return secrets.compare_digest(query_token or header_token, self.server.token)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _body_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ProjectError("无效 Content-Length") from exc
        if length > 20 * 1024 * 1024:
            raise ProjectError("请求体超过 20 MiB")
        data = self.rfile.read(length)
        if not data:
            return {}
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ProjectError("请求体必须是 JSON 对象")
        return parsed

    def do_GET(self) -> None:  # noqa: N802
        path = self._parsed().path
        try:
            if path.startswith("/api/"):
                if not self._authorized():
                    self._send_error_json(HTTPStatus.FORBIDDEN, "令牌无效")
                    return
                self._handle_api_get(path)
            elif path == "/media":
                if not self._authorized():
                    self._send_error_json(HTTPStatus.FORBIDDEN, "令牌无效")
                    return
                self._serve_media()
            else:
                self._serve_static(path)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except FileNotFoundError as exc:
            self._send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except (ProjectError, ValueError, OSError, subprocess.SubprocessError) as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务器错误：{exc}")

    def do_POST(self) -> None:  # noqa: N802
        path = self._parsed().path
        if not path.startswith("/api/") or not self._authorized():
            self._send_error_json(HTTPStatus.FORBIDDEN, "令牌无效")
            return
        try:
            body = self._body_json()
            self._handle_api_post(path, body)
        except (ProjectError, ValueError, KeyError, json.JSONDecodeError, OSError, subprocess.SubprocessError) as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务器错误：{exc}")

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in target.parents and target != STATIC_ROOT.resolve():
            raise FileNotFoundError("文件不存在")
        if not target.is_file():
            raise FileNotFoundError("页面不存在")
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'",
        )
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_api_get(self, path: str) -> None:
        query = self._query()
        if path == "/api/health":
            try:
                ffmpeg = str(locate_ffmpeg(self.server.ffmpeg))
            except Exception:
                ffmpeg = ""
            self._send_json({"ok": True, "version": __version__, "ffmpeg": ffmpeg})
            return
        if path == "/api/job":
            job_id = query.get("id", "")
            with self.server.jobs_lock:
                job = self.server.jobs.get(job_id)
            if job is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "找不到导出任务")
            else:
                self._send_json({"ok": True, "job": job.snapshot()})
            return
        if path == "/api/frame":
            self._serve_frame(query)
            return
        if path == "/api/waveform":
            self._serve_waveform(query)
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "API 不存在")

    def _handle_api_post(self, path: str, body: dict[str, Any]) -> None:
        if path == "/api/dialog":
            self._send_json({"ok": True, "path": self._open_dialog(body)})
            return
        if path == "/api/load-csv":
            project = load_csv(body["path"])
            project.video_path = str(body.get("video_path", ""))
            project.audio_path = str(body.get("audio_path", ""))
            project.captions_path = str(body.get("captions_path", ""))
            if project.captions_path:
                project.captions = load_captions(project.captions_path)
            self._send_json({"ok": True, "project": project.to_dict()})
            return
        if path == "/api/load-captions":
            captions = load_captions(body["path"])
            self._send_json({"ok": True, "captions": [
                {"start": item.start, "end": item.end, "text": item.text} for item in captions
            ]})
            return
        if path == "/api/load-project":
            source = Path(body["path"]).expanduser().resolve()
            project = Project.from_dict(json.loads(source.read_text(encoding="utf-8-sig")))
            upgrade_legacy_reviews(project)
            for field in ("video_path", "audio_path", "csv_path", "captions_path"):
                value = getattr(project, field)
                if value and not Path(value).is_absolute():
                    setattr(project, field, str((source.parent / value).resolve()))
            self._send_json({"ok": True, "project": project.to_dict()})
            return
        if path == "/api/save-project":
            project = Project.from_dict(body["project"])
            destination = Path(body["path"]).expanduser().resolve()
            if destination.suffix.lower() != ".json":
                destination = destination.with_suffix(".align.json")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(project.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            self._send_json({"ok": True, "path": str(destination)})
            return
        if path == "/api/save-csv":
            project = Project.from_dict(body["project"])
            destination = save_csv(project, body["path"])
            self._send_json({"ok": True, "path": str(destination)})
            return
        if path == "/api/verify-audio":
            self._send_json({"ok": True, "verification": compare_audio(body["source"], body["output"])})
            return
        if path == "/api/add-cut":
            project = Project.from_dict(body["project"])
            updated = add_cut(project, body["source_start"], body["source_end"], body.get("target"),
                              str(body.get("manual_policy", "reject")), str(body.get("note", "")))
            self._send_json({"ok": True, "project": updated.to_dict()})
            return
        if path == "/api/remove-cut":
            updated = remove_cut(Project.from_dict(body["project"]), str(body["id"]))
            self._send_json({"ok": True, "project": updated.to_dict()})
            return
        if path == "/api/analyze":
            raw = body.get("project") or {}
            if raw.get("anchors"):
                project = Project.from_dict(raw)
            else:
                project = Project(name=str(raw.get("name") or "自动粗调项目"),
                                  video_path=str(raw.get("video_path", "")),
                                  audio_path=str(raw.get("audio_path", "")))
            with self.server.jobs_lock:
                if any(isinstance(job, AnalysisJob) and job.snapshot()["state"] in ("queued", "running")
                       for job in self.server.jobs.values()):
                    raise ProjectError("已有分析任务运行，请先等待或取消")
                job = AnalysisJob(project, body.get("settings"),
                                  str(body.get("ffmpeg_path") or self.server.ffmpeg or "") or None)
                self.server.jobs[job.id] = job
            job.start()
            self._send_json({"ok": True, "job": job.snapshot()})
            return
        if path == "/api/apply-analysis":
            with self.server.jobs_lock:
                job = self.server.jobs.get(str(body["id"]))
            if not isinstance(job, AnalysisJob):
                raise ProjectError("找不到分析任务")
            result = job.result()
            current = body.get("project") or result["project"]
            project = Project.from_dict(current)
            for key in ("audio_path", "video_path"):
                if Path(getattr(project, key)).resolve() != Path(result["project"][key]).resolve():
                    raise ProjectError("素材已改变，请重新分析，不能应用旧候选")
            for key, fingerprint in (("audio_path", "audio_sha256"), ("video_path", "video_sha256")):
                if job.file_hash(Path(getattr(project, key))) != result["provenance"][fingerprint]:
                    raise ProjectError("素材内容已改变，请重新分析")
            merged = merge_result(project, result)
            self._send_json({"ok": True, "project": merged.to_dict()})
            return
        if path == "/api/export":
            project = Project.from_dict(body["project"])
            job = ExportJob(
                project=project,
                output_path=str(body["output_path"]),
                ffmpeg_path=str(body.get("ffmpeg_path") or self.server.ffmpeg or "") or None,
                encoder=str(body.get("encoder", "libx264")),
            )
            with self.server.jobs_lock:
                self.server.jobs[job.id] = job
            job.start()
            self._send_json({"ok": True, "job": job.snapshot()})
            return
        if path == "/api/cancel-job":
            job_id = str(body["id"])
            with self.server.jobs_lock:
                job = self.server.jobs.get(job_id)
            if job is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "找不到导出任务")
            else:
                job.cancel()
                self._send_json({"ok": True, "job": job.snapshot()})
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "API 不存在")

    def _open_dialog(self, body: dict[str, Any]) -> str:
        kind = str(body.get("kind", "project"))
        save = bool(body.get("save", False))
        definitions: dict[str, tuple[str, list[tuple[str, str]]]] = {
            "video": ("选择无声板书视频", [("视频", "*.mp4 *.mov *.mkv *.m4v *.webm"), ("所有文件", "*.*")]),
            "audio": ("选择旁白录音", [("音频", "*.m4a *.mp4 *.aac"), ("所有文件", "*.*")]),
            "csv": ("选择对齐 CSV", [("CSV", "*.csv"), ("所有文件", "*.*")]),
            "captions": ("选择字幕或转写", [("字幕", "*.jsonl *.json *.srt"), ("所有文件", "*.*")]),
            "project": ("选择对齐项目", [("对齐项目", "*.align.json *.json"), ("所有文件", "*.*")]),
            "output": ("选择成片输出位置", [("MP4 视频", "*.mp4")]),
            "ocr": ("选择 OCR 缓存", [("JSONL / JSON", "*.jsonl *.json"), ("所有文件", "*.*")]),
            "python": ("选择分析 Python", [("Python", "*.exe"), ("所有文件", "*.*")]),
            "model": ("选择 Whisper 模型目录", [("所有文件", "*.*")]),
            "ffmpeg": ("选择 ffmpeg.exe", [("FFmpeg", "ffmpeg.exe *.exe"), ("所有文件", "*.*")]),
        }
        title, filetypes = definitions.get(kind, definitions["project"])
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        command = [
            sys.executable,
            "-I",
            str(DIALOG_HELPER),
            "directory" if kind == "model" else ("save" if save else "open"),
            title,
            json.dumps(filetypes, ensure_ascii=False),
            str(body.get("initial", "")),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
        if completed.returncode != 0:
            raise ProjectError(completed.stderr.strip() or "文件选择器失败")
        return completed.stdout.strip()

    def _serve_frame(self, query: dict[str, str]) -> None:
        source = Path(query.get("path", "")).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"找不到视频：{source}")
        seconds = max(0.0, float(query.get("time", "0")))
        width = min(1920, max(80, int(query.get("width", "480"))))
        ffmpeg = locate_ffmpeg(query.get("ffmpeg") or self.server.ffmpeg)
        exact = query.get("exact") == "1"
        cache_key = f"{source}|{source.stat().st_mtime_ns}|{seconds:.9f}|{width}|{exact}" + ("|pts-v2" if exact else "")
        target = CACHE_ROOT / (hashlib.sha256(cache_key.encode("utf-8")).hexdigest() + ".jpg")
        metadata_path = target.with_suffix(".json")
        if not target.is_file() or (exact and not metadata_path.is_file()):
            command = [
                str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{seconds:.6f}", "-i", str(source), "-frames:v", "1",
                "-vf", f"scale={width}:-2", "-q:v", "3", str(target),
            ]
            if exact:
                command = [
                    str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "info", "-y",
                    "-copyts", "-start_at_zero", "-ss", f"{seconds:.6f}", "-i", str(source),
                    "-frames:v", "1", "-vf", f"scale={width}:-2,showinfo",
                    "-fps_mode", "passthrough", "-update", "1", "-q:v", "3", str(target),
                ]
            completed = subprocess.run(command, capture_output=True, timeout=60)
            if completed.returncode != 0:
                raise ProjectError(completed.stderr.decode("utf-8", "replace"))
            if exact:
                diagnostic = completed.stderr.decode("utf-8", "replace")
                pts = re.search(r"\bn:\s*0\s+pts:\s*(-?\d+)\s+pts_time:", diagnostic)
                time_base = re.search(r"config in time_base:\s*(\d+)/(\d+)", diagnostic)
                if not pts or not time_base or int(time_base.group(2)) == 0:
                    raise ProjectError("FFmpeg 未返回源帧 PTS/time base")
                numerator, denominator = map(int, time_base.groups())
                metadata_path.write_text(json.dumps({
                    "source_time": int(pts.group(1)) * numerator / denominator,
                    "pts": int(pts.group(1)), "time_base": [numerator, denominator],
                }), encoding="utf-8")
        payload = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        if exact:
            frame_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.send_header("X-Source-Time", str(frame_metadata["source_time"]))
            self.send_header("X-Source-PTS", str(frame_metadata["pts"]))
            self.send_header("X-Source-Time-Base", "/".join(map(str, frame_metadata["time_base"])))
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_waveform(self, query: dict[str, str]) -> None:
        source = Path(query.get("path", "")).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"找不到音频：{source}")
        points = min(5000, max(200, int(query.get("points", "1600"))))
        key = (str(source), source.stat().st_mtime_ns, points)
        with self.server.waveform_lock:
            cached = self.server.waveform_cache.get(key)
        if cached is None:
            ffmpeg = locate_ffmpeg(query.get("ffmpeg") or self.server.ffmpeg)
            command = [
                str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(source), "-vn", "-ac", "1", "-ar", "1000",
                "-f", "s16le", "pipe:1",
            ]
            completed = subprocess.run(command, capture_output=True, timeout=300)
            if completed.returncode != 0:
                raise ProjectError(completed.stderr.decode("utf-8", "replace"))
            samples = array.array("h")
            samples.frombytes(completed.stdout[: len(completed.stdout) // 2 * 2])
            if sys.byteorder != "little":
                samples.byteswap()
            bucket = max(1, (len(samples) + points - 1) // points)
            peaks = []
            for start in range(0, len(samples), bucket):
                peak = max((abs(value) for value in samples[start : start + bucket]), default=0)
                peaks.append(round(min(1.0, peak / 32768), 4))
            if len(peaks) < points:
                peaks.extend([0.0] * (points - len(peaks)))
            cached = peaks[:points]
            with self.server.waveform_lock:
                self.server.waveform_cache[key] = cached
        self._send_json({"ok": True, "peaks": cached})

    def _serve_media(self) -> None:
        source = Path(self._query().get("path", "")).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"找不到媒体：{source}")
        size = source.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes="):
            spec = range_header[6:].split(",", 1)[0]
            left, _, right = spec.partition("-")
            if left:
                start = int(left)
                end = int(right) if right else end
            elif right:
                length = int(right)
                start = max(0, size - length)
            start = min(max(0, start), size - 1)
            end = min(max(start, end), size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        if source.suffix.lower() == ".m4a":
            content_type = "audio/mp4"
        length = end - start + 1
        self.send_response(status)
        self._security_headers()
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with source.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                try:
                    self.wfile.write(block)
                except ConnectionError:
                    break
                remaining -= len(block)


def run(host: str = "127.0.0.1", port: int = 0, ffmpeg: str | None = None, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(24)
    server = AppServer((host, port), Handler, token=token, ffmpeg=ffmpeg)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/?token={urllib.parse.quote(token)}"
    print(f"Board Align Studio {__version__}")
    print(f"本地地址：{url}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 ASR/OCR 粗调、双轨细调与可恢复板书裁剪工具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ffmpeg", default=os.environ.get("FFMPEG_PATH", ""))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    run(args.host, args.port, args.ffmpeg or None, not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
