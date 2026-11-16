"""Conservative, explainable lexical + context + monotone sequence matching."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from .model import Anchor, Project, stable_anchor_id, validate_anchors, validate_project, mapping_segments, source_is_cut

STOP = set("我们 你们 他们 这个 那个 然后 就是 一个 可以 进行 时候 这里 所以 因为 如果 这样 现在 什么 对于 以及 还是 已经 通过 需要 其实 这种".split())


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", text).lower())


def tokens(text: str) -> set[str]:
    value = unicodedata.normalize("NFKC", text).lower()
    result = set(re.findall(r"[a-z][a-z0-9_]{1,}|\d{2,}", value))
    for run in re.findall(r"[\u4e00-\u9fff]+", value):
        result.update(run[i:i + 2] for i in range(len(run) - 1) if run[i:i + 2] not in STOP)
    return result


def _lines(frame):
    return [row for row in frame.get("findings", [])
            if float(row.get("score", 0)) >= 0.55 and len(normalize(str(row.get("text", "")))) >= 2]


def board_events(frames: list[dict]) -> tuple[list[dict], int]:
    """Suppress persistent text even when minor OCR spelling changes."""
    events, previous, duplicates = [], [], 0
    for frame in sorted(frames, key=lambda row: row["board_time"]):
        lines = _lines(frame)
        novel = []
        for row in lines:
            label = normalize(row["text"])
            if any(label == old or (min(len(label), len(old)) >= 4
                                   and SequenceMatcher(None, label, old).ratio() >= 0.84)
                   for old in previous):
                duplicates += 1
            else:
                novel.append(row)
        events.append({
            "source": float(frame["board_time"]),
            "text": " | ".join(row["text"] for row in novel),
            "context": " | ".join(row["text"] for row in lines),
            "ocr_score": sum(float(row["score"]) for row in novel) / max(1, len(novel)),
            "tokens": tokens(" ".join(row["text"] for row in novel)),
            "all_tokens": tokens(" ".join(row["text"] for row in lines)),
            "novel_lines": len(novel),
        })
        previous = [normalize(row["text"]) for row in lines]
    return events, duplicates


def protected_anchors(project: Project) -> list[Anchor]:
    return [anchor for index, anchor in enumerate(project.anchors)
            if anchor.locked or anchor.origin != "auto" or index in (0, len(project.anchors) - 1)]


def match(project: Project, frames: list[dict], transcript: list[dict], interval: float,
          confidence_floor: float = 0.58, check=None) -> dict[str, Any]:
    protected = sorted(protected_anchors(project), key=lambda anchor: anchor.target)
    issues = validate_project(project)
    errors = [row["message"] for row in issues if row["level"] == "error"]
    if errors:
        raise ValueError("人工锚点冲突：" + "；".join(errors))
    constraints = mapping_segments(protected, project.cuts)
    events, duplicates = board_events(frames)
    transcript = sorted(transcript, key=lambda row: row["start"])
    windows = []
    for index, row in enumerate(transcript):
        # Short ASR segments need nearby context; the center segment supplies time.
        nearby = [other["text"] for other in transcript[max(0, index - 1):index + 2]
                  if other["start"] <= row["end"] + 8 and other["end"] >= row["start"] - 8]
        windows.append({
            "target": float(row["start"]), "end": float(row["end"]),
            "text": row["text"], "tokens": tokens(" ".join(nearby)),
            "center_tokens": tokens(row["text"]),
        })
    documents = [event["all_tokens"] for event in events] + [window["tokens"] for window in windows]
    df = Counter(token for document in documents for token in document)
    weights = {token: math.log(1 + len(documents) / (1 + count)) for token, count in df.items()}
    nodes, reviews = [], []

    def review(event, reason, target=None):
        nearest = min(protected, key=lambda anchor: abs(anchor.source - event["source"]))
        reviews.append({
            "kind": reason, "target_start": max(0.0, (nearest.target if target is None else target) - 10),
            "target_end": min(project.duration, (nearest.target if target is None else target) + 20),
            "source_start": max(0.0, event["source"] - interval),
            "source_end": event["source"], "content": event["text"] or event["context"],
            "status": "pending", "reason": reason,
        })

    for event in events:
        if check:
            check()
        if source_is_cut(project, event["source"]):
            continue
        if not event["tokens"]:
            if not event["all_tokens"]:
                review(event, "纯图形、空白或 OCR 无可靠文字")
            continue  # persistent text is deliberately not another event
        left = right = None
        for first, second in constraints:
            if first.source < event["source"] < second.source:
                left, right = first, second
                break
        if left is None:
            continue
        possible = []
        key_tokens = event["tokens"]
        key_weight = sum(weights.get(token, 1) for token in key_tokens)
        for window in windows:
            if not left.target + 1 < window["target"] < right.target - 1:
                continue
            shared = key_tokens & window["tokens"]
            if not shared:
                continue
            matched_weight = sum(weights.get(token, 1) for token in shared)
            coverage = matched_weight / max(key_weight, 1e-9)
            precision = matched_weight / max(sum(weights.get(token, 1) for token in window["tokens"]), 1e-9)
            center = sum(weights.get(token, 1) for token in shared & window["center_tokens"]) / max(matched_weight, 1e-9)
            context_tokens = event["all_tokens"] & window["tokens"]
            context = sum(weights.get(token, 1) for token in context_tokens) / max(
                sum(weights.get(token, 1) for token in event["all_tokens"]), 1e-9)
            score = 0.50 * coverage + 0.15 * precision + 0.20 * center + 0.15 * context
            score *= 0.75 + 0.25 * event["ocr_score"]
            if len(shared) < 2 and not any(re.fullmatch("[a-z][a-z0-9_]{5,}", word) for word in shared):
                score = min(score, 0.40)
            possible.append({
                "target": window["target"], "source": event["source"], "score": score,
                "shared": sorted(shared), "text": event["text"], "narration": window["text"],
                "left_id": left.id, "right_id": right.id, "ocr_score": event["ocr_score"],
            })
        possible.sort(key=lambda node: node["score"], reverse=True)
        if not possible:
            review(event, "无词汇对应，需听录音核对")
            continue
        best = possible[0]
        alternative = next((node for node in possible if abs(node["target"] - best["target"]) > 25), None)
        margin = best["score"] - alternative["score"] if alternative else best["score"]
        for node in possible[:4]:
            node["margin"] = margin if node is best else 0
            node["confidence"] = round(max(0.0, min(0.97, node["score"] * (0.8 + min(0.2, max(0, margin))))), 4)
            nodes.append(node)
        if best["score"] < confidence_floor or margin < 0.025:
            review(event, "重复讲解或文字歧义" if margin < 0.025 else "词汇证据不足", best["target"])

    # Maximum weight increasing subsequence. Skipping is allowed, never force a match.
    nodes.sort(key=lambda node: (node["source"], node["target"]))
    values, previous = [], []
    for index, node in enumerate(nodes):
        if check:
            check()
        value, prev = max(0.0, node["score"] - 0.38), -1
        for j, older in enumerate(nodes[:index]):
            if node["source"] - older["source"] < 1 or node["target"] - older["target"] < 1:
                continue
            # A weak continuity preference; no restriction on real extreme speeds.
            rate = (node["source"] - older["source"]) / (node["target"] - older["target"])
            penalty = min(0.12, abs(math.log(max(rate, 0.001))) * 0.025)
            candidate = values[j] + max(0.0, node["score"] - 0.38) - penalty
            if candidate > value:
                value, prev = candidate, j
        values.append(value)
        previous.append(prev)
    selected = set()
    if values:
        index = max(range(len(values)), key=values.__getitem__)
        while index >= 0:
            selected.add(index)
            index = previous[index]
    candidates, accepted = [], []
    for index, node in enumerate(nodes):
        if check:
            check()
        if index not in selected:
            continue
        reliable = node["confidence"] >= confidence_floor and node["margin"] >= 0.025
        candidate_id = stable_anchor_id(0, node["target"], node["source"], "auto-content-v1")
        candidate = {
            "id": candidate_id, "target": node["target"], "source": node["source"],
            "content": node["text"], "confidence": node["confidence"],
            "evidence": ("自动候选；关键词：" + "、".join(node["shared"][:10])
                         + "；相邻转写上下文 + 单调顺序；采样误差需人工核对"),
            "origin": "auto", "locked": False,
            "match": {"shared_terms": node["shared"], "score": round(node["score"], 4),
                      "margin": round(node["margin"], 4), "narration": node["narration"],
                      "source_uncertainty": [max(0, node["source"] - interval), node["source"]],
                      "method": "lexical-context-monotone-v1"},
            "status": "candidate" if reliable else "review",
        }
        candidates.append(candidate)
        if reliable:
            accepted.append(candidate)
        else:
            event = {"source": node["source"], "text": node["text"], "context": ""}
            review(event, "顺序候选置信度不足，未写入映射", node["target"])
    # Explicit narration gaps, including unsupported tail; no invented freeze anchor.
    points = sorted([0.0, project.duration] + [row["target"] for row in accepted])
    for start, end in zip(points, points[1:]):
        if end - start > max(60, interval * 2):
            reviews.append({
                "kind": "未覆盖录音区间", "target_start": round(start, 3), "target_end": round(end, 3),
                "source_start": None, "source_end": None, "status": "pending",
                "content": "", "reason": "没有可靠自动对应，可能为图形、重复讲解或缺少板书；请人工核对",
            })
    unique = {}
    for item in reviews:
        key = (item["kind"], item["source_start"], item["target_start"], item["target_end"])
        item["id"] = stable_anchor_id(0, item["target_start"], item["source_start"] or 0, item["kind"])
        unique[key] = item
    return {
        "candidates": candidates, "review_intervals": list(unique.values()),
        "stats": {"frames": len(frames), "events": sum(bool(row["tokens"]) for row in events),
                  "persistent_lines_suppressed": duplicates, "sequence_candidates": len(candidates),
                  "accepted_candidates": len(accepted), "protected_anchors": len(protected),
                  "pending_reviews": len(unique)},
        "method": "离线词汇、相邻上下文与顺序约束；非通用语义模型，结果需人工检查",
    }


def merge_result(project: Project, result: dict) -> Project:
    """Always merge against the current editor project, including edits during analysis."""
    updated = Project.from_dict(project.to_dict())
    kept = protected_anchors(updated)
    inserted, conflicts = [], []
    for candidate in result.get("candidates", []):
        if candidate.get("status") != "candidate":
            continue
        anchor = Anchor.from_dict(candidate)
        crosses_cut = source_is_cut(updated, anchor.source) or any(
            (anchor.target < cut.target and anchor.source > cut.source_start) or
            (anchor.target > cut.target and anchor.source < cut.source_end) or
            (abs(anchor.target - cut.target) < 0.001 and abs(anchor.source - cut.source_end) > 0.001)
            for cut in updated.cuts)
        if crosses_cut:
            conflicts.append(candidate["id"])
            continue
        if any(abs(anchor.target - item.target) <= 0.001 or
               (anchor.target - item.target) * (anchor.source - item.source) <= 0
               for item in kept + inserted):
            conflicts.append(candidate["id"])
            continue
        inserted.append(anchor)
    updated.anchors = sorted(kept + inserted, key=lambda anchor: anchor.target)
    report = {key: value for key, value in result.items() if key not in ("project", "captions")}
    report["stats"] = dict(report.get("stats", {}), inserted_candidates=len(inserted), merge_conflicts=len(conflicts))
    report["merge_conflicts"] = conflicts
    for candidate in report.get("candidates", []):
        if candidate["id"] in conflicts:
            report.setdefault("review_intervals", []).append({
                "id": candidate["id"], "kind": "与当前锚点或裁剪冲突",
                "reason": "分析期间的人工编辑优先保留；候选未合并",
                "target_start": candidate["target"], "target_end": candidate["target"],
                "source_start": candidate["source"], "source_end": candidate["source"], "status": "pending",
            })
    errors = [row["message"] for row in validate_project(updated) if row["level"] == "error"]
    if errors:
        raise ValueError("自动合并产生无效映射：" + "；".join(errors))
    updated.analysis = report
    if result.get("captions") and not updated.captions:
        from .model import Caption
        updated.captions = [Caption.from_dict(row) for row in result["captions"]]
    return updated
