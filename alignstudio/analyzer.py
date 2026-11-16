"""Persistent, cancellable local ASR/OCR pipeline with parameterized caches."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from .exporter import locate_ffmpeg
from .alignment import prepare as prepare_semantic, request_for as semantic_request
from .matching import match, merge_result
from .model import Anchor, Project, ProjectError

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("analysis_worker.py")
SEMANTIC_WORKER = Path(__file__).with_name("semantic_worker.py")
ASR_WORKER = Path(__file__).with_name("asr_worker.py")
CACHE_VERSION = "analysis-v2"


class AnalysisCancelled(Exception):
    pass


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex[:8] + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]


def sample_times(duration: float, interval: float) -> list[float]:
    if not math.isfinite(interval) or not 1 <= interval <= 3600:
        raise ProjectError("截图间隔必须为 1–3600 秒的有限数")
    if not math.isfinite(duration) or duration <= 0:
        raise ProjectError("视频时长无效")
    count = math.ceil(duration / interval)
    if count > 20000:
        raise ProjectError("截图数量超过 20000，请增大采样间隔")
    return [round(index * interval, 6) for index in range(count)]


def load_rows(path: str, kind: str, duration: float) -> list[dict]:
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() == ".json":
        data = read_json(source)
        rows = data if isinstance(data, list) else data.get("segments" if kind == "asr" else "frames", [])
    else:
        rows = [json.loads(line) for line in source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not isinstance(rows, list) or not rows:
        raise ProjectError(f"{kind} 缓存没有有效数据")
    seen = set()
    for row in rows:
        keys = ("start", "end") if kind == "asr" else ("board_time",)
        for key in keys:
            value = float(row[key])
            if not math.isfinite(value) or not 0 <= value <= duration + 1:
                raise ProjectError(f"{kind} 缓存时间超出媒体：{key}={value}")
            row[key] = value
        if kind == "asr":
            if row["end"] <= row["start"] or not isinstance(row.get("text"), str):
                raise ProjectError("ASR 缓存起止时间或文字无效")
        else:
            if row["board_time"] in seen:
                raise ProjectError("OCR 缓存有重复源时间")
            seen.add(row["board_time"])
            if not isinstance(row.get("findings"), list):
                raise ProjectError("OCR findings 必须为列表")
            for item in row["findings"]:
                score = float(item.get("score", 0))
                if not math.isfinite(score) or not 0 <= score <= 1 or not isinstance(item.get("text"), str):
                    raise ProjectError("OCR 文字或置信度无效")
    return sorted(rows, key=lambda row: row["start" if kind == "asr" else "board_time"])


def probe_duration(path: Path, ffmpeg: Path) -> float:
    completed = subprocess.run([str(ffmpeg), "-nostdin", "-hide_banner", "-i", str(path)],
                               capture_output=True, timeout=30)
    text = completed.stderr.decode("utf-8", "replace")
    found = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not found:
        raise ProjectError("无法读取媒体时长：" + text[-1000:])
    hours, minutes, seconds = map(float, found.groups())
    return hours * 3600 + minutes * 60 + seconds


def refinement_times(result, base_times, duration, interval, budget):
    """Spend the same budget across the whole source, rather than its first peaks."""
    if not budget:
        return []
    # The physical last frame can contain a recording/control overlay. Inspect
    # the final seconds within the same budget before proposing a held frame.
    terminal = [round(max(0, duration-offset), 6) for offset in (.12, 1, 2, 4)][:min(4, budget)]
    terminal = [value for value in dict.fromkeys(terminal) if value not in set(base_times)]
    remaining = budget-len(terminal)
    count = min(8, max(1, remaining // 4))
    pools = [[] for _ in range(count)]
    for row in result.get("candidates", []):
        bucket = min(count-1, int(row["source"]/max(duration, 1)*count))
        pools[bucket].append(row)
    for pool in pools:
        pool.sort(key=lambda row: row.get("confidence", 0), reverse=True)
    step = max(1.0, min(5.0, interval/4))
    found, seen = list(terminal), set(base_times) | set(terminal)
    while any(pools) and len(found) < budget:
        for pool in pools:
            if not pool:
                continue
            candidate = pool.pop(0)
            for offset in (step, step*2, step*3, interval/2):
                point = round(max(0, candidate["source"]-offset), 6)
                if 0 <= point < duration and point not in seen:
                    found.append(point); seen.add(point)
                if len(found) >= budget:
                    break
            if len(found) >= budget:
                break
    return found


class AnalysisJob:
    def __init__(self, project: Project, settings: dict | None = None, ffmpeg_path: str | None = None):
        self.id = uuid.uuid4().hex
        self.project = copy.deepcopy(project)
        self.settings = dict(settings or {})
        self.interval = float(self.settings.get("interval", 60))
        sample_times(1, self.interval)  # validate before starting background work
        self.refine_budget = int(self.settings.get("refine_budget", 24))
        self.width = int(self.settings.get("frame_width", 1180))
        self.threads = int(self.settings.get("threads", 4))
        if not 0 <= self.refine_budget <= 120 or not 320 <= self.width <= 1920 or not 1 <= self.threads <= 32:
            raise ProjectError("加密预算、缩略图宽度或线程数越界")
        self.crop = [int(value) for value in self.settings.get("crop", [0, 0, 0, 0])]
        if len(self.crop) != 4 or min(self.crop) < 0:
            raise ProjectError("OCR 裁边必须为四个非负像素数")
        self.ffmpeg = locate_ffmpeg(ffmpeg_path)
        self.cache_root = Path(self.settings.get("cache_dir") or ROOT / ".analysis-cache").expanduser().resolve()
        self.folder = self.cache_root / "jobs" / self.id[:12]
        self.folder.mkdir(parents=True, exist_ok=True)
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.thread = None
        self.semantic_runs = []
        self.data = {"id": self.id, "kind": "analysis", "state": "queued", "progress": 0.0,
                     "message": "准备自动粗调", "folder": str(self.folder), "error": "",
                     "stats": {}, "result_path": ""}
        atomic_json(self.folder / "project-before.align.json", project.to_dict())
        self._update()

    def _update(self, **changes):
        with self.lock:
            self.data.update(changes)
            snapshot = dict(self.data)
        atomic_json(self.folder / "status.json", snapshot)

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="analysis-" + self.id[:8], daemon=True)
        self.thread.start()

    def cancel(self):
        self.cancelled.set()

    def check(self):
        if self.cancelled.is_set():
            raise AnalysisCancelled()

    def file_hash(self, path: Path) -> str:
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(8 * 1024 * 1024):
                self.check()
                sha.update(block)
        return sha.hexdigest()

    def command(self, command, name, progress=None):
        self.check()
        logfile = self.folder / (name + ".log")
        with logfile.open("wb") as log:
            self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                while self.process.poll() is None:
                    self.check()
                    if progress:
                        try:
                            lines = logfile.read_text(encoding="utf-8", errors="replace").splitlines()
                            row = json.loads(lines[-1]) if lines else {}
                            progress(row)
                        except (ValueError, OSError):
                            pass
                    self.cancelled.wait(0.2)
                if self.process.returncode:
                    detail = logfile.read_text(encoding="utf-8", errors="replace")[-5000:]
                    raise ProjectError(f"{name} 失败：{detail}")
            finally:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
                self.process = None

    def worker(self, task, **kwargs):
        request_file = self.folder / (task + "-request.json")
        output = self.folder / (task + "-output-" + uuid.uuid4().hex[:8] + ".json")
        request = dict(task=task, output=str(output), threads=self.threads,
                       module_paths=(list(self.settings.get("semantic_module_paths", [])) if task == "semantic" else []) + list(self.settings.get("module_paths", [])), **kwargs)
        atomic_json(request_file, request)
        executable = str(self.settings.get("python") or sys.executable)
        def report(row):
            if task == "ocr" and row.get("total"):
                self._update(message=f"OCR {row['done']}/{row['total']}")
            elif task == "semantic" and row.get("total"):
                self._update(message=f"本地语义向量 {row.get('done',0)}/{row['total']}")
            elif task == "asr" and row.get("time"):
                self._update(message=f"转写至 {float(row['time']):.1f} 秒")
        self.command([executable, str(ASR_WORKER if task == "asr" else SEMANTIC_WORKER if task == "semantic" else WORKER), str(request_file)], task, report)
        return read_json(output)

    def match_frames(self, frames, transcript, stats):
        model = str(self.settings.get("semantic_model") or "")
        if not model and (ROOT / "models" / "e5-small" / "manifest.json").exists():
            model = str(ROOT / "models" / "e5-small")
        if not model:
            return match(self.project, frames, transcript, self.interval, check=self.check)
        data = prepare_semantic(frames, transcript)
        payload = self.worker("semantic", **semantic_request(data), model=str(Path(model).expanduser().resolve()),
                              cache=str(self.cache_root / "semantic" / "embeddings.sqlite"),
                              cache_only=bool(self.settings.get("cache_only")))
        self.semantic_runs.append({"phase": len(self.semantic_runs)+1, "cache_hits": payload["cache_hits"],
                                   "computed": payload["computed"], **payload["cache_audit"]})
        stats["semantic_hits"] = stats.get("semantic_hits", 0) + payload["cache_hits"]
        stats["semantic_computed"] = stats.get("semantic_computed", 0) + payload["computed"]
        return match(self.project, frames, transcript, self.interval, check=self.check, semantic=payload, prepared=data)

    def _extract_ocr(self, times, video, frame_root, ocr_root, stats, phase):
        missing = []
        for index, seconds in enumerate(times):
            self.check()
            name = f"{seconds:.6f}"
            thumbnail = frame_root / (name + ".jpg")
            destination = ocr_root / (name + ".json")
            # Repair malformed partial entries rather than silently accepting them.
            if destination.exists():
                try:
                    row = read_json(destination)
                    if float(row["board_time"]) != seconds or not isinstance(row["findings"], list):
                        raise ValueError("invalid")
                except (ValueError, KeyError, OSError):
                    destination.unlink()
            if thumbnail.exists() and thumbnail.stat().st_size:
                stats["thumbnail_hits"] += 1
            else:
                temporary = thumbnail.with_name(name + "." + self.id + ".jpg")
                self.command([
                    str(self.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{seconds:.6f}", "-i", str(video), "-frames:v", "1",
                    "-vf", f"scale={self.width}:-2", "-q:v", "3", str(temporary),
                ], "frame")
                if not temporary.is_file() or not temporary.stat().st_size:
                    raise ProjectError(f"无法提取 {seconds:.3f}s 源帧")
                temporary.replace(thumbnail)
                stats["thumbnail_computed"] += 1
            if destination.exists():
                stats["ocr_hits"] += 1
            else:
                missing.append({"path": str(thumbnail), "time": seconds, "output": str(destination)})
            if index % 5 == 0:
                self._update(progress=(0.7 if phase == "候选加密" else 0.25) + 0.15 * (index + 1) / len(times), message=f"{phase}抽帧 {index + 1}/{len(times)}")
        if missing:
            if self.settings.get("cache_only"):
                raise ProjectError(f"仅缓存模式缺少 {len(missing)} 张 OCR；请关闭仅缓存模式并配置分析 Python")
            self.worker("ocr", frames=missing, crop=self.crop)
            stats["ocr_computed"] += len(missing)
        return [read_json(ocr_root / (f"{seconds:.6f}.json")) for seconds in times]

    def run(self):
        stats = {"asr_hits": 0, "asr_computed": 0, "ocr_hits": 0, "ocr_computed": 0,
                 "thumbnail_hits": 0, "thumbnail_computed": 0, "imported_asr": 0, "imported_ocr": 0}
        started = time.monotonic()
        try:
            self._update(state="running", message="读取素材指纹与时长", progress=0.01)
            audio = Path(self.project.audio_path).expanduser().resolve()
            video = Path(self.project.video_path).expanduser().resolve()
            if not audio.is_file() or not video.is_file():
                raise ProjectError("请先选择存在的录音和板书视频")
            audio_duration = probe_duration(audio, self.ffmpeg)
            video_duration = probe_duration(video, self.ffmpeg)
            # For AAC use exact media duration, avoiding 10ms display rounding.
            try:
                from .mp4aac import audio_signature
                signature = audio_signature(audio)
                numerator, denominator = map(int, signature["time_base"].split("/"))
                audio_duration = signature.get("declared_duration_ticks", signature["duration_ticks"]) * numerator / denominator
            except (ValueError, KeyError, OSError):
                pass
            provenance = {"audio_sha256": self.file_hash(audio), "video_sha256": self.file_hash(video),
                          "audio_duration": audio_duration, "video_duration": video_duration,
                          "worker_sha256": self.file_hash(WORKER),
                          "semantic_worker_sha256": self.file_hash(SEMANTIC_WORKER),
                          "analysis_code_sha256": hashlib.sha256(json.dumps({path.name: self.file_hash(path)
                              for path in sorted(Path(__file__).parent.glob("*.py"))},sort_keys=True).encode()).hexdigest()}
            imported_asr = str(self.settings.get("transcript_path") or "")
            imported_ocr = str(self.settings.get("ocr_path") or "")
            for name, path in (("asr_import", imported_asr), ("ocr_import", imported_ocr)):
                provenance[name] = self.file_hash(Path(path)) if path else ""
            model = str(self.settings.get("model") or "")
            runtime = {"python": self.settings.get("python") or sys.executable,
                       "paths": self.settings.get("module_paths", []), "worker": provenance["worker_sha256"]}
            model_path = Path(model)
            model_stamp = [(str(path.name), path.stat().st_size, path.stat().st_mtime_ns)
                           for path in model_path.glob("*") if path.is_file()] if model and model_path.is_dir() else model
            asr_key = digest([CACHE_VERSION, provenance["audio_sha256"], provenance["asr_import"],
                              model_stamp, self.settings.get("language", "zh"), dict(runtime, worker=self.file_hash(ASR_WORKER))])
            ocr_key = digest([CACHE_VERSION, provenance["video_sha256"], provenance["ocr_import"],
                              self.width, self.crop, runtime])
            asr_path = self.cache_root / "asr" / (asr_key + ".json")
            frame_root = self.cache_root / "frames" / digest([provenance["video_sha256"], self.width])
            ocr_root = self.cache_root / "ocr" / ocr_key
            frame_root.mkdir(parents=True, exist_ok=True)
            ocr_root.mkdir(parents=True, exist_ok=True)
            self._update(progress=0.1, message="复用或生成录音转写")
            if asr_path.exists():
                transcript = read_json(asr_path)
                stats["asr_hits"] = len(transcript)
            elif imported_asr:
                transcript = load_rows(imported_asr, "asr", audio_duration)
                atomic_json(asr_path, transcript)
                stats["imported_asr"] = len(transcript)
            elif self.settings.get("cache_only"):
                raise ProjectError("仅缓存模式缺少 ASR 转写")
            else:
                if not model or not model_path.is_dir():
                    raise ProjectError("请配置本地 faster-whisper 模型目录，或显式导入已有转写 JSONL")
                pcm_path = self.folder / "narration16k.wav"
                self.command([str(self.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                              "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                              str(pcm_path)], "asr-pcm")
                transcript = self.worker("asr", audio=str(pcm_path), model=model,
                                         language=self.settings.get("language", "zh"))
                if not transcript:
                    raise ProjectError("ASR 未识别到文字，请检查录音或导入转写")
                atomic_json(asr_path, transcript)
                stats["asr_computed"] = len(transcript)
            if imported_ocr:
                for row in load_rows(imported_ocr, "ocr", video_duration):
                    target = ocr_root / (f"{row['board_time']:.6f}.json")
                    if not target.exists():
                        atomic_json(target, row)
                        stats["imported_ocr"] += 1
            created_boundaries = not self.project.anchors
            if created_boundaries:
                last_frame = max(0, math.floor((video_duration - 0.05) * self.project.fps) / self.project.fps)
                self.project.anchors = [
                    Anchor(id="boundary-start", target=0, source=0, content="素材起点", origin="boundary", locked=True),
                    Anchor(id="boundary-end", target=audio_duration, source=last_frame,
                           content="素材终点（暂按全片映射，请核对末段）", origin="boundary", locked=True),
                ]
            elif abs(self.project.duration - audio_duration) > 0.1:
                raise ProjectError("项目末锚点与录音时长不符，请先校正边界；粗调不会改变人工时基")
            if any(anchor.source > video_duration for anchor in self.project.anchors):
                raise ProjectError("人工锚点超出源视频时长")
            times = sample_times(video_duration, self.interval)
            frames = self._extract_ocr(times, video, frame_root, ocr_root, stats, "基础采样")
            self.check()
            self._update(progress=0.60, message="关键词、上下文和顺序匹配")
            result = self.match_frames(frames, transcript, stats)
            # Refine only near promising candidates, at a bounded cost.
            refined = refinement_times(result, times, video_duration, self.interval, self.refine_budget)
            if self.refine_budget and refined:
                self._update(progress=0.70, message=f"候选附近加密 {len(refined)} 张")
                dense_frames = self._extract_ocr(refined, video, frame_root, ocr_root, stats, "候选加密")
                frames = sorted(frames + dense_frames, key=lambda row: row["board_time"])
                result = self.match_frames(frames, transcript, stats)
            self.check()
            # Only boundaries created by this run may receive a proposed terminal freeze.
            # This boundary is structural and is excluded from semantic coverage.
            if created_boundaries and not self.project.cuts and result.get("unmatchable_intervals"):
                tail = result["unmatchable_intervals"][-1]
                at = tail["target_start"]
                if 0 < at < audio_duration and all(row["target"] < at for row in result["candidates"] if row["status"] == "candidate"):
                    end = self.project.anchors[-1]
                    clear_source = result.get("source_quality", {}).get("last_clear_source")
                    if clear_source is not None and clear_source >= video_duration*.95:
                        end.source = clear_source
                    self.project.anchors.insert(-1, Anchor(id="boundary-inferred-tail", target=at, source=end.source,
                        content="缺板书尾段起点（自动推测，需确认）", evidence=tail["reason"],
                        origin="boundary", locked=False, match={"structural_only": True, "boundary_uncertainty": tail["boundary_uncertainty"]}))
                    result["proposed_terminal_freeze"] = {"target": at, "source": end.source, "requires_review": True}
            result.update({
                "settings": dict(self.settings, interval=self.interval), "provenance": provenance,
                "sampling": {"interval": self.interval, "base_times": times,
                             "refined_times": sorted(refined) if self.refine_budget else []},
                "cache": stats, "captions": transcript, "semantic_runs": self.semantic_runs,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            })
            # A machine can be interrupted after completion without losing the result.
            result["project"] = self.project.to_dict()
            atomic_json(self.folder / "result.json", result)
            merged = merge_result(self.project, result)
            atomic_json(self.folder / "project-after.align.json", merged.to_dict())
            self._update(state="completed", progress=1.0, message="自动粗调完成，请检查待核对列表",
                         stats={**result["stats"], **stats}, result_path=str(self.folder / "result.json"))
        except AnalysisCancelled:
            self._update(state="cancelled", message="分析已取消；已完成的转写、OCR、缩略图可在重跑时复用", stats=stats)
        except Exception as exc:
            (self.folder / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            self._update(state="failed", message="分析失败；原项目保留，可修正设置后重跑", error=str(exc), stats=stats)

    def result(self):
        if self.snapshot()["state"] != "completed":
            raise ProjectError("分析尚未成功完成")
        return read_json(self.folder / "result.json")
