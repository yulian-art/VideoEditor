"""Reversible board-only cuts with explicit policies for affected anchors."""
from __future__ import annotations
from .model import (EPSILON, Anchor, Cut, Project, ProjectError, map_source_to_target,
                    parse_timestamp, stable_anchor_id, validate_project)


def ensure_valid(project: Project):
    errors = [row["message"] for row in validate_project(project) if row["level"] == "error"]
    if errors:
        raise ProjectError("；".join(errors))


def add_cut(project: Project, source_start: float, source_end: float,
            target: float | None = None, manual_policy: str = "reject", note: str = "") -> Project:
    updated = Project.from_dict(project.to_dict())
    start, end = parse_timestamp(source_start), parse_timestamp(source_end)
    if end - start <= EPSILON:
        raise ProjectError("裁剪终点必须晚于起点")
    first, last = updated.anchors[0], updated.anchors[-1]
    if start < first.source - EPSILON or end > last.source + EPSILON:
        raise ProjectError("裁剪必须位于当前有效板书源范围内")
    if abs(start - first.source) <= EPSILON and abs(end - last.source) <= EPSILON:
        raise ProjectError("不能裁掉整个板书；需要静止画面时请建立定格")
    if any(start < cut.source_end - EPSILON and end > cut.source_start + EPSILON for cut in updated.cuts):
        raise ProjectError("裁剪源区间重叠")
    if target is None:
        target = (0.0 if abs(start - first.source) <= EPSILON else
                  updated.duration if abs(end - last.source) <= EPSILON else
                  map_source_to_target(updated.anchors, start, updated.cuts))
    target = parse_timestamp(target)
    if target > updated.duration:
        raise ProjectError("裁剪录音位置超出项目")
    head = target <= EPSILON
    tail = abs(target - updated.duration) <= EPSILON
    if head and abs(start - first.source) > EPSILON:
        raise ProjectError("片头裁剪必须从当前源起点开始")
    if tail and abs(end - last.source) > EPSILON:
        raise ProjectError("片尾裁剪必须结束于当前源终点")
    affected = [anchor for anchor in updated.anchors
                if start - EPSILON <= anchor.source < end - EPSILON
                or (tail and abs(anchor.source - end) <= EPSILON)]
    locked = [anchor for anchor in affected if anchor.locked]
    if locked:
        raise ProjectError("被裁区间含锁定锚点，请先在检查器明确解锁：" + "、".join(f"{anchor.id}（录音 {anchor.target:.3f}s，源 {anchor.source:.3f}s）" for anchor in locked))
    manual = [anchor for anchor in affected if anchor.origin != "auto"]
    if manual and manual_policy != "suspend":
        raise ProjectError(f"被裁区间含 {len(manual)} 个未锁定人工点；勾选允许暂存人工点后才会提交")
    if manual_policy not in ("reject", "suspend"):
        raise ProjectError("未知裁剪人工点策略")
    cut_id = stable_anchor_id(0, target, start, f"cut:{end:.9f}")
    cut = Cut(cut_id, start, end, target, excluded_anchors=affected, note=note)
    removed = {anchor.id for anchor in affected}
    updated.anchors = [anchor for anchor in updated.anchors if anchor.id not in removed]
    if head and (not updated.anchors or updated.anchors[0].target > EPSILON):
        boundary = Anchor(cut_id + ":start", 0, end, "裁剪后的源起点", origin="boundary", locked=True)
        updated.anchors.insert(0, boundary)
        cut.generated_ids.append(boundary.id)
    if tail and (not updated.anchors or updated.anchors[-1].target < project.duration - EPSILON):
        boundary = Anchor(cut_id + ":end", project.duration, start, "裁剪后的源终点", origin="boundary", locked=True)
        updated.anchors.append(boundary)
        cut.generated_ids.append(boundary.id)
    updated.cuts.append(cut)
    updated.cuts.sort(key=lambda row: row.target)
    updated.version = max(2, updated.version)
    ensure_valid(updated)
    return updated


def remove_cut(project: Project, cut_id: str) -> Project:
    updated = Project.from_dict(project.to_dict())
    cut = next((cut for cut in updated.cuts if cut.id == cut_id), None)
    if cut is None:
        raise ProjectError("找不到裁剪记录")
    generated = set(cut.generated_ids)
    updated.anchors = [anchor for anchor in updated.anchors if anchor.id not in generated]
    updated.anchors += cut.excluded_anchors
    updated.anchors.sort(key=lambda row: row.target)
    updated.cuts = [row for row in updated.cuts if row.id != cut_id]
    try:
        ensure_valid(updated)
    except ProjectError as exc:
        raise ProjectError("恢复裁剪与当前锚点冲突；请先调整冲突点，当前项目保持不变：" + str(exc)) from exc
    return updated
