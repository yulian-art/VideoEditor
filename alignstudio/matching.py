"""Content alignment with explicit evidence, support intervals and source inspection."""
from __future__ import annotations

from .matching_legacy import normalize, tokens, board_events, protected_anchors, merge_result
from .matching_legacy import match as lexical_match
from .alignment import semantic_match, repair_reviews


def match(project, frames, transcript, interval, confidence_floor=0.58, check=None, semantic=None, prepared=None):
    if semantic is not None:
        result = semantic_match(project, prepared, semantic, interval, confidence_floor, check)
    else:
        result = lexical_match(project, frames, transcript, interval, confidence_floor, check)
        result["method"] = "词汇兼容模式；未启用语义模型，覆盖可能很低"
    repair_reviews(project, result, interval)
    return result
