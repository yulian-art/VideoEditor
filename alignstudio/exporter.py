from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .model import EPSILON, Anchor, Project, ProjectError, save_csv, srt_text, validate_anchors, validate_project, mapping_segments
from .mp4aac import audio_signature, compare_audio

ProgressCallback = Callable[[dict[str, Any]], None]


class ExportError(RuntimeError):
    pass


def locate_ffmpeg(configured: str | None = None) -> Path:
    candidates: list[str | Path | None] = [
        configured,
        os.environ.get("FFMPEG_PATH"),
        shutil.which("ffmpeg"),
        Path(__file__).resolve().parents[1] / "tools" / "ffmpeg.exe",
        Path(__file__).resolve().parents[1] / "ffmpeg.exe",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if path.is_file():
            return path
    raise ExportError("未找到 FFmpeg。请在设置中选择 ffmpeg.exe，或设置 FFMPEG_PATH。")


def _linear_expression(points: list[Anchor], first: int, last: int) -> str:
    if first + 1 == last:
        left = points[first]
        right = points[last]
        ratio = (right.target - left.target) / (right.source - left.source)
        return f"({left.target:.9f}+(T-{left.source:.9f})*{ratio:.15f})"
    middle = (first + last) // 2
    return (
        f"if(lt(T,{points[middle].source:.9f}),"
        f"{_linear_expression(points, first, middle)},"
        f"{_linear_expression(points, middle, last)})"
    )


@dataclass(slots=True)
class RenderPart:
    kind: str
    anchors: list[Anchor]
    target_start: float
    target_end: float
    frame_start: int = 0
    frame_end: int = 0

    @property
    def frames(self) -> int:
        return self.frame_end - self.frame_start


def render_parts(project: Project) -> list[RenderPart]:
    issues = validate_project(project)
    errors = [item["message"] for item in issues if item["level"] == "error"]
    if errors:
        raise ProjectError("；".join(errors))
    parts: list[RenderPart] = []
    for left, right in mapping_segments(project.anchors, project.cuts):
        kind = "freeze" if abs(right.source - left.source) <= EPSILON else "linear"
        previous = parts[-1] if parts else None
        contiguous = (previous is not None and abs(previous.target_end - left.target) <= EPSILON
                      and abs(previous.anchors[-1].source - left.source) <= EPSILON)
        if kind == "linear" and contiguous and previous.kind == "linear":
            previous.anchors.append(right)
            previous.target_end = right.target
        else:
            parts.append(RenderPart(kind=kind, anchors=[left, right],
                                    target_start=left.target, target_end=right.target))
    frame_cursor = 0
    for part_index, part in enumerate(parts):
        part.frame_start = frame_cursor
        if part_index + 1 == len(parts):
            frame_cursor = math.ceil(part.target_end * project.fps)
        else:
            frame_cursor = round(part.target_end * project.fps)
        part.frame_end = frame_cursor
        if part.frames <= 0:
            raise ProjectError(
                f"{part.target_start:.6f}–{part.target_end:.6f} 秒区间不足一帧；请拉开锚点"
            )
    return parts


def build_filtergraph(project: Project) -> tuple[str, dict[str, Any]]:
    parts = render_parts(project)
    fps = project.fps
    graph: list[str] = []
    if len(parts) == 1:
        inputs = ["[0:v:0]"]
    else:
        labels = "".join(f"[src{index}]" for index in range(len(parts)))
        graph.append(f"[0:v:0]split={len(parts)}{labels};")
        inputs = [f"[src{index}]" for index in range(len(parts))]

    for index, (part, input_label) in enumerate(zip(parts, inputs)):
        output_label = f"[part{index}]"
        if part.kind == "linear":
            expression = _linear_expression(part.anchors, 0, len(part.anchors) - 1)
            source_start = part.anchors[0].source
            source_end = part.anchors[-1].source
            chain = (
                f"{input_label}trim=start={source_start:.9f}:end={source_end:.9f},"
                f"setpts='({expression}-{part.target_start:.9f})/TB',"
                f"fps={fps}:start_time=0,"
                f"trim=end_frame={part.frames},setpts=N/({fps}*TB),"
                f"format=yuv420p{output_label};"
            )
        else:
            source = part.anchors[0].source
            duration = part.target_end - part.target_start
            chain = (
                f"{input_label}trim=start={source:.9f}:end={source + 1.0:.9f},"
                f"setpts=PTS-STARTPTS,fps={fps}:start_time=0,trim=end_frame=1,"
                f"tpad=stop_mode=clone:stop_duration={duration + 1.0:.9f},"
                f"trim=end_frame={part.frames},setpts=N/({fps}*TB),"
                f"format=yuv420p{output_label};"
            )
        graph.append(chain)

    labels = "".join(f"[part{index}]" for index in range(len(parts)))
    if len(parts) == 1:
        graph.append(f"{labels}null[aligned]")
    else:
        graph.append(f"{labels}concat=n={len(parts)}:v=1:a=0[aligned]")
    metadata = {
        "fps": fps,
        "target_duration": project.duration,
        "cuts": [{"id": cut.id, "target": cut.target, "source_start": cut.source_start,
                  "source_end": cut.source_end, "excluded_count": len(cut.excluded_anchors)} for cut in project.cuts],
        "frame_count": parts[-1].frame_end,
        "parts": [
            {
                "kind": part.kind,
                "target_start": part.target_start,
                "target_end": part.target_end,
                "source_start": part.anchors[0].source,
                "source_end": part.anchors[-1].source,
                "frames": part.frames,
            }
            for part in parts
        ],
    }
    return "\n".join(graph) + "\n", metadata


def _encoder_options(encoder: str) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "22", "-b:v", "0"]
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "0"]
    raise ExportError(f"不支持的视频编码器：{encoder}")


def _ensure_exportable(project: Project) -> None:
    if not Path(project.video_path).is_file():
        raise ExportError(f"找不到板书视频：{project.video_path}")
    if not Path(project.audio_path).is_file():
        raise ExportError(f"找不到旁白录音：{project.audio_path}")
    if project.duration <= 0:
        raise ExportError("项目没有有效时长")
    signature = audio_signature(project.audio_path)
    if signature["codec"] != "aac":
        raise ExportError(
            f"当前无损音频流复制闭环要求 AAC 输入；检测到 {signature['codec']}。请先准备 AAC/M4A 旁白。"
        )
    try:
        timescale = int(str(signature["time_base"]).split("/", 1)[1])
        audio_duration = int(signature["duration_ticks"]) / timescale
    except (IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ExportError("无法读取旁白音频时长") from exc
    tolerance = 1 / project.fps + 0.001
    if abs(project.duration - audio_duration) > tolerance:
        raise ExportError(
            f"CSV 终点 {project.duration:.6f}s 与旁白时长 {audio_duration:.6f}s 相差超过一帧；"
            "请先修正终点锚点，避免成片音画总时长不一致。"
        )


def run_export(
    project: Project,
    output_path: str | Path,
    *,
    ffmpeg_path: str | None = None,
    encoder: str = "libx264",
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    _ensure_exportable(project)
    ffmpeg = locate_ffmpeg(ffmpeg_path)
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() not in {".mp4", ".mov", ".m4v"}:
        output = output.with_suffix(".mp4")
    output.parent.mkdir(parents=True, exist_ok=True)

    graph_text, graph_metadata = build_filtergraph(project)
    graph_path = output.with_suffix(".ffgraph")
    log_path = output.with_suffix(".export.log")
    csv_path = output.with_suffix(".csv")
    srt_path = output.with_suffix(".srt")
    project_path = output.with_suffix(".align.json")
    graph_path.write_text(graph_text, encoding="ascii")
    save_csv(project, csv_path)
    srt_path.write_text("﻿" + srt_text(project.captions), encoding="utf-8", newline="")
    project_path.write_text(json.dumps(project.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-y",
        "-i",
        str(Path(project.video_path).resolve()),
        "-i",
        str(Path(project.audio_path).resolve()),
        "-filter_complex_script",
        str(graph_path),
        "-map",
        "[aligned]",
        "-map",
        "1:a:0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        *_encoder_options(encoder),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output),
    ]
    if progress:
        progress({"state": "running", "progress": 0.0, "message": "FFmpeg 正在导出"})

    started = time.monotonic()
    with log_path.open("w", encoding="utf-8", newline="") as log:
        log.write("COMMAND\n" + subprocess.list2cmdline(command) + "\n\nOUTPUT\n")
        log.flush()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                if cancel_event is not None and cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise ExportError("导出已取消；项目及中间证据已保留")
                key, separator, value = line.strip().partition("=")
                if separator and key in {"out_time_us", "out_time_ms"}:
                    try:
                        seconds = int(value) / 1_000_000
                        fraction = min(0.999, seconds / project.duration)
                    except (ValueError, ZeroDivisionError):
                        continue
                    if progress:
                        progress(
                            {
                                "state": "running",
                                "progress": fraction,
                                "message": f"已导出 {seconds:.1f}/{project.duration:.1f} 秒",
                            }
                        )
            return_code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    if return_code != 0:
        raise ExportError(f"FFmpeg 导出失败（退出码 {return_code}），详见 {log_path.name}")

    if progress:
        progress({"state": "verifying", "progress": 0.999, "message": "正在逐包校验 AAC"})
    verification = compare_audio(project.audio_path, output)
    if not verification["ok"]:
        raise ExportError(
            "视频已生成，但 AAC 包内容或时间戳校验失败："
            + json.dumps(verification["mismatches"], ensure_ascii=False)
        )
    result = {
        "state": "completed",
        "progress": 1.0,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "output": str(output),
        "csv": str(csv_path),
        "srt": str(srt_path),
        "project": str(project_path),
        "filtergraph": str(graph_path),
        "log": str(log_path),
        "render": graph_metadata,
        "audio_verification": verification,
    }
    if progress:
        progress({"state": "completed", "progress": 1.0, "message": "导出及 AAC 校验通过", "result": result})
    return result


@dataclass
class ExportJob:
    project: Project
    output_path: str
    ffmpeg_path: str | None = None
    encoder: str = "libx264"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: str = "queued"
    progress_value: float = 0.0
    message: str = "等待导出"
    result: dict[str, Any] | None = None
    error: str | None = None
    traceback_text: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "state": self.state,
                "progress": self.progress_value,
                "message": self.message,
                "result": self.result,
                "error": self.error,
            }

    def _update(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.state = str(data.get("state", self.state))
            self.progress_value = float(data.get("progress", self.progress_value))
            self.message = str(data.get("message", self.message))

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("导出任务已启动")
        self._thread = threading.Thread(target=self._run, name=f"export-{self.id}", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            self.message = "正在取消"

    def _run(self) -> None:
        try:
            self._update({"state": "running", "progress": 0.0, "message": "准备导出"})
            result = run_export(
                self.project,
                self.output_path,
                ffmpeg_path=self.ffmpeg_path,
                encoder=self.encoder,
                cancel_event=self.cancel_event,
                progress=self._update,
            )
            with self._lock:
                self.result = result
                self.state = "completed"
                self.progress_value = 1.0
                self.message = "导出及 AAC 校验通过"
        except Exception as exc:  # The job boundary must preserve a readable failure.
            with self._lock:
                self.state = "cancelled" if self.cancel_event.is_set() else "failed"
                self.error = str(exc)
                self.message = str(exc)
                self.traceback_text = traceback.format_exc()
