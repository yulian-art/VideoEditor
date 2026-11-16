from __future__ import annotations

import csv
import io
import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

CSV_HEADERS = [
    "录音时间（成片）",
    "原板书时间",
    "内容",
    "核对方式",
    "至下一锚点的画面倍速",
    "锚点ID", "来源", "锁定", "置信度",
    "记录类型", "裁剪源起点", "裁剪源终点", "裁剪录音位置", "裁剪数据JSON",
]
ANCHOR_NAMESPACE = uuid.UUID("ad359106-aeaf-49ea-9cb1-bad7fc4c86c3")
EPSILON = 1e-6


class ProjectError(ValueError):
    pass


def parse_timestamp(value: str | float | int) -> float:
    if isinstance(value, (float, int)):
        result = float(value)
    else:
        text = value.strip().replace(",", ".")
        if not text:
            raise ProjectError("时间不能为空")
        parts = text.split(":")
        try:
            if len(parts) == 1:
                result = float(parts[0])
            elif len(parts) == 2:
                result = float(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3:
                result = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            else:
                raise ValueError
        except ValueError as exc:
            raise ProjectError(f"无法识别时间：{value!r}") from exc
    if not math.isfinite(result) or result < 0:
        raise ProjectError(f"时间必须是非负有限数：{value!r}")
    return result


def format_timestamp(seconds: float, srt: bool = False) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    separator = "," if srt else "."
    return (
        f"{milliseconds // 3_600_000:02d}:"
        f"{milliseconds // 60_000 % 60:02d}:"
        f"{milliseconds // 1_000 % 60:02d}{separator}"
        f"{milliseconds % 1_000:03d}"
    )


@dataclass(slots=True)
class Anchor:
    id: str
    target: float
    source: float
    content: str = ""
    evidence: str = "人工"
    origin: str = "manual"
    locked: bool = False
    confidence: float | None = None
    match: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int = 0) -> "Anchor":
        target = parse_timestamp(data.get("target", data.get("targetTime", 0)))
        source = parse_timestamp(data.get("source", data.get("sourceTime", 0)))
        content = str(data.get("content", ""))
        anchor_id = str(data.get("id") or stable_anchor_id(index, target, source, content))
        return cls(
            id=anchor_id,
            target=target,
            source=source,
            content=content,
            evidence=str(data.get("evidence", "人工")),
            origin=str(data.get("origin", "manual")),
            locked=bool(data.get("locked", False)),
            confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
            match=dict(data.get("match", {})),
        )


@dataclass(slots=True)
class Caption:
    start: float
    end: float
    text: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Caption":
        return cls(
            start=parse_timestamp(data["start"]),
            end=parse_timestamp(data["end"]),
            text=str(data.get("text", "")),
        )



@dataclass(slots=True)
class Cut:
    id: str
    source_start: float
    source_end: float
    target: float
    excluded_anchors: list[Anchor] = field(default_factory=list)
    generated_ids: list[str] = field(default_factory=list)
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Cut":
        start = parse_timestamp(data["source_start"])
        end = parse_timestamp(data["source_end"])
        target = parse_timestamp(data["target"])
        return cls(
            id=str(data.get("id") or stable_anchor_id(0, target, start, f"cut:{end}")),
            source_start=start, source_end=end, target=target,
            excluded_anchors=[Anchor.from_dict(row, i) for i, row in enumerate(data.get("excluded_anchors", []))],
            generated_ids=[str(value) for value in data.get("generated_ids", [])],
            note=str(data.get("note", "")),
        )


@dataclass(slots=True)
class Project:
    version: int = 1
    name: str = "未命名对齐项目"
    video_path: str = ""
    audio_path: str = ""
    csv_path: str = ""
    captions_path: str = ""
    fps: int = 30
    anchors: list[Anchor] = field(default_factory=list)
    captions: list[Caption] = field(default_factory=list)
    editor: dict[str, Any] = field(default_factory=dict)
    analysis: dict[str, Any] = field(default_factory=dict)
    cuts: list[Cut] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.anchors[-1].target if self.anchors else 0.0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["duration"] = self.duration
        result["validation"] = validate_project(self)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        project = cls(
            version=int(data.get("version", 1)),
            name=str(data.get("name", "未命名对齐项目")),
            video_path=str(data.get("video_path", data.get("videoPath", ""))),
            audio_path=str(data.get("audio_path", data.get("audioPath", ""))),
            csv_path=str(data.get("csv_path", data.get("csvPath", ""))),
            captions_path=str(data.get("captions_path", data.get("captionsPath", ""))),
            fps=int(data.get("fps", 30)),
            anchors=[Anchor.from_dict(item, i) for i, item in enumerate(data.get("anchors", []))],
            captions=[Caption.from_dict(item) for item in data.get("captions", [])],
            editor=dict(data.get("editor", {})),
            analysis=dict(data.get("analysis", {})),
            cuts=[Cut.from_dict(row) for row in data.get("cuts", [])],
        )
        if not 1 <= project.fps <= 120:
            raise ProjectError("帧率必须在 1–120 之间")
        errors = [item["message"] for item in validate_project(project) if item["level"] == "error"]
        if errors:
            raise ProjectError("；".join(errors))
        return project


def stable_anchor_id(index: int, target: float, source: float, content: str) -> str:
    seed = f"{index}|{target:.6f}|{source:.6f}|{content}"
    return str(uuid.uuid5(ANCHOR_NAMESPACE, seed))


def validate_anchors(anchors: Iterable[Anchor]) -> list[dict[str, Any]]:
    items = list(anchors)
    issues: list[dict[str, Any]] = []
    if len(items) < 2:
        return [{"level": "error", "index": None, "message": "至少需要两个锚点"}]
    if abs(items[0].target) > EPSILON:
        issues.append({"level": "error", "index": 0, "message": "第一个录音时间必须为 00:00:00.000"})
    for index, anchor in enumerate(items):
        if anchor.confidence is not None and (not math.isfinite(anchor.confidence) or not 0 <= anchor.confidence <= 1):
            issues.append({"level": "error", "index": index, "message": "锚点置信度必须在 0–1 之间"})
        if not math.isfinite(anchor.target) or not math.isfinite(anchor.source):
            issues.append({"level": "error", "index": index, "message": f"第 {index + 1} 个锚点时间无效"})
        if anchor.target < 0 or anchor.source < 0:
            issues.append({"level": "error", "index": index, "message": f"第 {index + 1} 个锚点时间不能为负"})
        if index == 0:
            continue
        previous = items[index - 1]
        target_delta = anchor.target - previous.target
        source_delta = anchor.source - previous.source
        if target_delta <= EPSILON:
            issues.append({
                "level": "error",
                "index": index,
                "message": f"第 {index + 1} 个锚点的录音时间必须晚于前一个锚点",
            })
            continue
        if source_delta < -EPSILON:
            issues.append({
                "level": "error",
                "index": index,
                "message": f"第 {index + 1} 个锚点的原板书时间倒退",
            })
        elif abs(source_delta) <= EPSILON:
            issues.append({
                "level": "info",
                "index": index,
                "message": f"第 {index}–{index + 1} 个锚点构成 {target_delta:.3f} 秒定格段",
            })
        else:
            speed = source_delta / target_delta
            if speed > 8 or speed < 0.125:
                issues.append({
                    "level": "warning",
                    "index": index,
                    "message": f"第 {index}–{index + 1} 段倍速为 {speed:.3f}×，预览可能使用逐帧定位",
                })
    return issues


def segment_speed(left: Anchor, right: Anchor) -> float:
    target_delta = right.target - left.target
    if target_delta <= EPSILON:
        raise ProjectError("锚点录音时间必须严格递增")
    source_delta = right.source - left.source
    return 0.0 if abs(source_delta) <= EPSILON else source_delta / target_delta



def mapping_segments(anchors: list[Anchor], cuts: Iterable[Cut] = ()) -> list[tuple[Anchor, Anchor]]:
    """Positive-duration segments; a cut is a discontinuity, never a zero-duration segment."""
    if len(anchors) < 2:
        return []
    duration = anchors[-1].target
    events = [(anchor.target, 1, anchor) for anchor in anchors]
    events += [(cut.target, 0, cut) for cut in cuts]
    events.sort(key=lambda row: (row[0], row[1]))
    segments = []
    current = None
    for _target, kind, item in events:
        if kind == 0:
            left = Anchor(item.id + ":left", item.target, item.source_start, origin="cut", locked=True)
            right = Anchor(item.id + ":right", item.target, item.source_end, origin="cut", locked=True)
            if current is not None and left.target > current.target + EPSILON:
                segments.append((current, left))
            current = left if item.target >= duration - EPSILON else right
        else:
            if current is None:
                current = item
                continue
            delta = item.target - current.target
            if abs(delta) <= EPSILON:
                if abs(item.source - current.source) > EPSILON:
                    raise ProjectError("普通锚点与裁剪跳变边界冲突")
            elif delta > EPSILON:
                segments.append((current, item))
            else:
                raise ProjectError("映射时间顺序冲突")
            current = item
    return segments


def source_is_cut(project: Project, source: float) -> bool:
    return any(cut.source_start - EPSILON <= source < cut.source_end - EPSILON for cut in project.cuts)


def validate_project(project: Project) -> list[dict[str, Any]]:
    issues = validate_anchors(project.anchors)
    active_ids = [anchor.id for anchor in project.anchors]
    if len(active_ids) != len(set(active_ids)):
        issues.append({"level": "error", "index": None, "message": "活动锚点 ID 重复"})
    if not project.cuts:
        return issues
    issues = [row for row in issues if row["level"] == "error"]
    seen_ids = set(active_ids)
    cuts = sorted(project.cuts, key=lambda cut: cut.target)
    def error(message):
        issues.append({"level": "error", "index": None, "message": message})
    for index, cut in enumerate(cuts):
        values = (cut.source_start, cut.source_end, cut.target)
        if not all(math.isfinite(value) and value >= 0 for value in values):
            error("裁剪时间必须为非负有限数")
            continue
        if cut.source_end - cut.source_start <= EPSILON or not 0 <= cut.target <= project.duration:
            error("裁剪源区间或录音位置无效")
        if cut.id in seen_ids:
            error("裁剪或锚点 ID 重复")
        seen_ids.add(cut.id)
        if index:
            older = cuts[index - 1]
            if cut.target - older.target <= EPSILON or cut.source_start < older.source_end - EPSILON:
                error("裁剪区间重叠或顺序冲突")
        for anchor in cut.excluded_anchors:
            if anchor.id in seen_ids:
                error("暂存锚点 ID 与活动点或其他裁剪重复")
            seen_ids.add(anchor.id)
            if anchor.locked:
                error("裁剪暂存区包含锁定锚点；请先明确解锁再裁剪")
        for anchor in project.anchors:
            tail_boundary = abs(cut.target - project.duration) <= EPSILON and abs(anchor.target - cut.target) <= EPSILON and abs(anchor.source - cut.source_start) <= EPSILON
            if cut.source_start - EPSILON <= anchor.source < cut.source_end - EPSILON and not tail_boundary:
                error(f"锚点 {anchor.id} 位于被裁源区间")
            if anchor.target < cut.target - EPSILON and anchor.source > cut.source_start + EPSILON:
                error(f"锚点 {anchor.id} 跨越裁剪左边界")
            if anchor.target > cut.target + EPSILON and anchor.source < cut.source_end - EPSILON:
                error(f"锚点 {anchor.id} 跨越裁剪右边界")
            if abs(anchor.target - cut.target) <= EPSILON:
                boundary = cut.source_start if abs(cut.target - project.duration) <= EPSILON else cut.source_end
                if abs(anchor.source - boundary) > EPSILON:
                    error(f"锚点 {anchor.id} 与裁剪跳变同刻冲突")
    if any(row["level"] == "error" for row in issues):
        return issues
    try:
        segments = mapping_segments(project.anchors, project.cuts)
        for left, right in segments:
            speed = segment_speed(left, right)
            if speed < -EPSILON:
                error("裁剪后的源时间倒退")
            elif speed == 0:
                issues.append({"level": "info", "index": None, "message": f"{left.target:.3f}–{right.target:.3f}s 为定格"})
            elif speed > 8 or speed < 0.125:
                issues.append({"level": "warning", "index": None, "message": f"{left.target:.3f}–{right.target:.3f}s 为 {speed:.3f}×"})
    except ProjectError as exc:
        error(str(exc))
    return issues


def map_target_to_source(anchors: list[Anchor], target: float, cuts: Iterable[Cut] = ()) -> float:
    if not anchors:
        return 0.0
    if target >= anchors[-1].target:
        return anchors[-1].source
    segments = mapping_segments(anchors, cuts)
    # Reverse search makes jumps right-continuous at their exact target timestamp.
    for left, right in reversed(segments):
        if target >= left.target:
            fraction = (target - left.target) / (right.target - left.target)
            return left.source + fraction * (right.source - left.source)
    return segments[0][0].source if segments else anchors[0].source


def map_source_to_target(anchors: list[Anchor], source: float, cuts: Iterable[Cut] = ()) -> float:
    if not anchors:
        return 0.0
    items = list(cuts)
    if any(cut.source_start - EPSILON <= source < cut.source_end - EPSILON for cut in items):
        raise ProjectError("该源时间位于裁剪段，没有成片时间")
    if source <= anchors[0].source:
        return anchors[0].target
    for left, right in mapping_segments(anchors, items):
        if right.source > left.source + EPSILON and left.source <= source <= right.source:
            return left.target + (source - left.source) / (right.source - left.source) * (right.target - left.target)
    return anchors[0].target if source <= anchors[0].source else anchors[-1].target


def _field(row: dict[str, str], candidates: tuple[str, ...]) -> str:
    normalized = {str(key).strip(): value for key, value in row.items() if key is not None}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate] or ""
    return ""


def load_csv(path: str | Path) -> Project:
    csv_path = Path(path).expanduser().resolve()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ProjectError("CSV 中没有锚点")
    anchors: list[Anchor] = []
    cuts: list[Cut] = []
    for index, row in enumerate(rows):
        if _field(row, ("记录类型",)) == "cut":
            payload = json.loads(_field(row, ("裁剪数据JSON",)) or "{}")
            for key, column in (("source_start", "裁剪源起点"), ("source_end", "裁剪源终点"), ("target", "裁剪录音位置")):
                if _field(row, (column,)):
                    payload[key] = parse_timestamp(_field(row, (column,)))
            payload["id"] = _field(row, ("锚点ID",)) or payload.get("id")
            cuts.append(Cut.from_dict(payload))
            continue
        target_text = _field(row, ("录音时间（成片）", "录音时间", "target", "target_time"))
        source_text = _field(row, ("原板书时间", "板书时间", "source", "source_time"))
        if not target_text and not source_text:
            continue
        target = parse_timestamp(target_text)
        source = parse_timestamp(source_text)
        content = _field(row, ("内容", "content", "说明"))
        evidence = _field(row, ("核对方式", "evidence", "来源")) or "CSV 导入"
        origin = _field(row, ("来源", "origin")) or ("auto" if "自动" in evidence else "manual")
        anchors.append(
            Anchor(
                id=_field(row, ("锚点ID", "id")) or stable_anchor_id(index, target, source, content),
                target=target,
                source=source,
                content=content,
                evidence=evidence,
                origin=origin,
                locked=(_field(row, ("锁定", "locked")).lower() in ("true", "1", "是")
                        if _field(row, ("锁定", "locked")) else origin == "manual" and index in (0, len(rows) - 1)),
                confidence=float(_field(row, ("置信度", "confidence"))) if _field(row, ("置信度", "confidence")) else None,
            )
        )
    project = Project(name=csv_path.stem, csv_path=str(csv_path), anchors=anchors, cuts=cuts, version=2 if cuts else 1)
    errors = [item["message"] for item in validate_project(project) if item["level"] == "error"]
    if errors:
        raise ProjectError("；".join(errors))
    return project


def csv_text(project: Project) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADERS)
    for index, anchor in enumerate(project.anchors):
        speed = ""
        if index + 1 < len(project.anchors):
            value = segment_speed(anchor, project.anchors[index + 1])
            speed = "含裁剪（见裁剪记录）" if any(anchor.target < cut.target <= project.anchors[index + 1].target for cut in project.cuts) else f"{value:.3f}"
        writer.writerow(
            [
                format_timestamp(anchor.target),
                format_timestamp(anchor.source),
                anchor.content,
                anchor.evidence,
                speed,
                anchor.id, anchor.origin, str(anchor.locked).lower(),
                "" if anchor.confidence is None else anchor.confidence,
                "anchor", "", "", "", "",
            ]
        )
    for cut in project.cuts:
        writer.writerow(["", "", cut.note or "独立裁剪", "裁剪记录", "", cut.id, "cut", "", "", "cut",
                         format_timestamp(cut.source_start), format_timestamp(cut.source_end),
                         format_timestamp(cut.target), json.dumps(asdict(cut), ensure_ascii=False)])
    return buffer.getvalue()


def save_csv(project: Project, path: str | Path) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("﻿" + csv_text(project), encoding="utf-8", newline="")
    return destination


def load_captions(path: str | Path) -> list[Caption]:
    source = Path(path).expanduser().resolve()
    suffix = source.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        captions = []
        for line_number, line in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                captions.append(Caption.from_dict(json.loads(line)))
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                raise ProjectError(f"字幕第 {line_number} 行无效：{exc}") from exc
        return captions
    if suffix == ".json":
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        rows = data if isinstance(data, list) else data.get("captions", data.get("segments", []))
        return [Caption.from_dict(item) for item in rows]
    if suffix == ".srt":
        return parse_srt(source.read_text(encoding="utf-8-sig"))
    raise ProjectError("字幕仅支持 JSONL、JSON 或 SRT")


def parse_srt(text: str) -> list[Caption]:
    captions: list[Caption] = []
    blocks = text.replace("\r\n", "\n").strip().split("\n\n")
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 2:
            continue
        timing_index = 1 if lines[0].strip().isdigit() else 0
        if "-->" not in lines[timing_index]:
            continue
        start_text, end_text = (part.strip() for part in lines[timing_index].split("-->", 1))
        captions.append(
            Caption(
                start=parse_timestamp(start_text),
                end=parse_timestamp(end_text),
                text="\n".join(lines[timing_index + 1 :]),
            )
        )
    return captions


def srt_text(captions: Iterable[Caption]) -> str:
    blocks = []
    for index, caption in enumerate(captions, 1):
        blocks.append(
            f"{index}\n{format_timestamp(caption.start, srt=True)} --> "
            f"{format_timestamp(caption.end, srt=True)}\n{caption.text}\n"
        )
    return "\n".join(blocks)
