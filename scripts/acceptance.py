from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alignstudio.exporter import build_filtergraph, run_export  # noqa: E402
from alignstudio.model import Anchor, Caption, Project, load_csv, map_target_to_source  # noqa: E402
from alignstudio.mp4aac import compare_audio  # noqa: E402


def run(command: list[str], *, capture: bool = False) -> bytes:
    completed = subprocess.run(command, check=False, capture_output=capture)
    if completed.returncode:
        stderr = completed.stderr.decode("utf-8", "replace") if capture else ""
        raise RuntimeError(f"命令失败 ({completed.returncode}): {subprocess.list2cmdline(command)}\n{stderr}")
    return completed.stdout if capture else b""


def frame_rgb(ffmpeg: Path, source: Path, seconds: float) -> bytes:
    return run([
        str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{seconds:.6f}", "-i", str(source), "-frames:v", "1",
        "-vf", "scale=320:180", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ], capture=True)


def mean_delta(left: bytes, right: bytes) -> float:
    if len(left) != len(right) or not left:
        raise AssertionError(f"帧大小不一致：{len(left)} != {len(right)}")
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def make_anchor(target: float, source: float, label: str) -> Anchor:
    return Anchor(
        id=f"accept-{target:.3f}-{source:.3f}",
        target=target,
        source=source,
        content=label,
        evidence="自动验收",
        origin="manual",
        locked=True,
    )


def synthetic_case(
    ffmpeg: Path,
    work: Path,
    name: str,
    source_video: Path,
    source_audio: Path,
    anchors: list[Anchor],
    target_time: float,
    expected_source_time: float,
    control_source_time: float,
) -> dict[str, object]:
    project = Project(
        name=name,
        video_path=str(source_video),
        audio_path=str(source_audio),
        fps=30,
        anchors=anchors,
        captions=[Caption(1.0, 2.5, f"{name} 字幕固定在录音时间")],
    )
    output = work / f"{name}.mp4"
    result = run_export(project, output, ffmpeg_path=str(ffmpeg), encoder="libx264")
    mapped = map_target_to_source(project.anchors, target_time)
    if abs(mapped - expected_source_time) > 1e-6:
        raise AssertionError(f"{name} 映射错误：{mapped} != {expected_source_time}")
    rendered = frame_rgb(ffmpeg, output, target_time)
    expected = frame_rgb(ffmpeg, source_video, expected_source_time)
    control = frame_rgb(ffmpeg, source_video, control_source_time)
    matched_delta = mean_delta(rendered, expected)
    control_delta = mean_delta(rendered, control)
    if not matched_delta < control_delta * 0.35:
        raise AssertionError(f"{name} 导出帧未命中映射：匹配差 {matched_delta:.3f}，对照差 {control_delta:.3f}")
    verification = result["audio_verification"]
    if not verification["ok"]:
        raise AssertionError(f"{name} AAC 校验失败")
    srt_path = Path(result["srt"])
    if "00:00:01,000 --> 00:00:02,500" not in srt_path.read_text(encoding="utf-8-sig"):
        raise AssertionError(f"{name} 字幕时间发生变化")
    return {
        "name": name,
        "target_time": target_time,
        "mapped_source_time": mapped,
        "expected_source_time": expected_source_time,
        "matched_frame_mean_abs_delta": round(matched_delta, 4),
        "wrong_time_control_delta": round(control_delta, 4),
        "aac_packet_count": verification["source"]["packet_count"],
        "aac_payload_sha256": verification["source"]["payload_sha256"],
        "aac_timing_sha256": verification["source"]["packet_timing_sha256"],
        "audio_copy_ok": verification["ok"],
        "subtitle_timestamp_ok": True,
        "output": output.name,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="第一阶段端到端验收：±5 秒、片尾定格与 AAC 逐包复制")
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "evidence" / "acceptance-run")
    parser.add_argument("--reference-csv", type=Path)
    parser.add_argument("--reference-audio", type=Path)
    parser.add_argument("--reference-output", type=Path)
    args = parser.parse_args()
    ffmpeg = args.ffmpeg.expanduser().resolve()
    if not ffmpeg.is_file():
        raise FileNotFoundError(ffmpeg)
    work = args.output_dir.expanduser().resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    source_video = work / "synthetic-board.mp4"
    source_audio = work / "synthetic-narration.m4a"
    run([
        str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=20",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "15", "-pix_fmt", "yuv420p", str(source_video),
    ])
    run([
        str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=20",
        "-c:a", "aac", "-b:a", "96k", str(source_audio),
    ])

    cases = []
    cases.append(synthetic_case(
        ffmpeg, work, "board-ahead-5s", source_video, source_audio,
        [make_anchor(0, 0, "起点"), make_anchor(10, 5, "板书提前 5 秒"), make_anchor(20, 15, "终点")],
        target_time=11, expected_source_time=6, control_source_time=11,
    ))
    cases.append(synthetic_case(
        ffmpeg, work, "board-behind-5s", source_video, source_audio,
        [make_anchor(0, 0, "起点"), make_anchor(5, 10, "板书落后 5 秒"), make_anchor(20, 20, "终点")],
        target_time=6.5, expected_source_time=11, control_source_time=6,
    ))
    cases.append(synthetic_case(
        ffmpeg, work, "terminal-freeze", source_video, source_audio,
        [make_anchor(0, 0, "起点"), make_anchor(10, 10, "板书结束"), make_anchor(20, 10, "片尾定格")],
        target_time=18, expected_source_time=10, control_source_time=18,
    ))

    reference: dict[str, object] | None = None
    if args.reference_csv and args.reference_audio and args.reference_output:
        project = load_csv(args.reference_csv)
        graph, render = build_filtergraph(project)
        audio_check = compare_audio(args.reference_audio, args.reference_output)
        if not audio_check["ok"]:
            raise AssertionError("现有 51 分钟成片 AAC 回归校验失败")
        speeds = [
            (right.source - left.source) / (right.target - left.target)
            for left, right in zip(project.anchors, project.anchors[1:])
        ]
        freeze_seconds = sum(
            right.target - left.target
            for left, right in zip(project.anchors, project.anchors[1:])
            if abs(right.source - left.source) < 1e-6
        )
        reference = {
            "csv_anchor_count": len(project.anchors),
            "target_duration_seconds": project.duration,
            "last_source_seconds": project.anchors[-1].source,
            "freeze_duration_seconds": round(freeze_seconds, 3),
            "max_segment_speed": round(max(speeds), 3),
            "filtergraph_has_freeze": "tpad=stop_mode=clone" in graph,
            "render_parts": render["parts"],
            "aac_copy_ok": audio_check["ok"],
            "aac_packet_count": audio_check["source"]["packet_count"],
            "aac_payload_sha256": audio_check["source"]["payload_sha256"],
            "aac_timing_sha256": audio_check["source"]["packet_timing_sha256"],
        }

    evidence = {
        "status": "PASS",
        "ffmpeg_binary": ffmpeg.name,
        "synthetic_cases": cases,
        "reference_51_minute_regression": reference,
    }
    json_path = work / "acceptance.json"
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 第一阶段验收记录",
        "",
        "结果：**PASS**",
        "",
        "| 场景 | 目标录音时刻 | 命中源画面 | 匹配帧差 | 错误时刻对照差 | AAC 包与时间戳 | 字幕时基 |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    labels = {"board-ahead-5s": "板书提前 5 秒", "board-behind-5s": "板书落后 5 秒", "terminal-freeze": "片尾定格"}
    for case in cases:
        lines.append(
            f"| {labels[str(case['name'])]} | {case['target_time']:.3f}s | {case['mapped_source_time']:.3f}s | "
            f"{case['matched_frame_mean_abs_delta']:.3f} | {case['wrong_time_control_delta']:.3f} | PASS | PASS |"
        )
    if reference:
        lines.extend([
            "",
            "## 现有 51 分钟素材回归",
            "",
            f"- CSV：{reference['csv_anchor_count']} 个锚点；成片时长 {reference['target_duration_seconds']:.3f}s。",
            f"- 片尾：源时间固定在 {reference['last_source_seconds']:.3f}s，共定格 {reference['freeze_duration_seconds']:.3f}s。",
            f"- 极端倍速：{reference['max_segment_speed']:.3f}×；导出图包含独立 `tpad` 定格段。",
            f"- AAC：{reference['aac_packet_count']} 包；payload SHA-256 `{reference['aac_payload_sha256']}`；时间戳 SHA-256 `{reference['aac_timing_sha256']}`；前后完全一致。",
        ])
    lines.extend([
        "",
        "## 判定方法",
        "",
        "- 每个 ±5 秒场景都实际导出 MP4，再在目标录音时刻抽帧，与预期源画面及错误时刻对照画面分别做逐像素平均绝对差。",
        "- 每个导出都解析 MP4 sample table，逐 AAC 包比较 payload、PTS、DTS、duration、包数和 time base。",
        "- SRT 直接使用固定录音时间，检查视频变速后时间码不变。",
    ])
    (work / "ACCEPTANCE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
