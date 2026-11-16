"""Offline multiview alignment; the human reference is never an input here."""
from __future__ import annotations
import math
import re
from collections import Counter
from difflib import SequenceMatcher
from statistics import mean
from .matching_legacy import normalize, tokens, protected_anchors
from .model import Anchor, stable_anchor_id, mapping_segments, source_is_cut, validate_project

METHOD = "e5-phonetic-context-monotone-v2"
MAX_SUPPORTED_GAP = 180.0


def terms(text):
    # Single capital identifiers carry meaning in diagrams (G/M/P, etc.).
    return tokens(text) | {"id:" + word.lower() for word in re.findall(r"\b[A-Z]\b", text)}


def prepare(frames, transcript):
    queries, events, history = [], [], []
    suppressed = 0
    previous_time = 0.0
    for frame in sorted(frames, key=lambda row: row["board_time"]):
        source = float(frame["board_time"])
        raw_text = " ".join(row.get("text", "") for row in frame.get("findings", []))
        overlay = bool(re.search("未在播放|专注模式|屏幕镜像", raw_text))
        lines = [row for row in frame.get("findings", []) if float(row.get("score", 0)) >= .55
                 and len(normalize(row.get("text", ""))) >= 2 and "%" not in row["text"]
                 and not re.search("未在播放|专注模式", row["text"])]
        labels = [row["text"] for row in lines]
        state_index = len(queries)
        queries.append("query: " + "；".join(labels))
        line_queries = []
        novel = []
        for row in lines:
            label = normalize(row["text"])
            old = any(label == value or (min(len(label), len(value)) >= 4
                      and SequenceMatcher(None, label, value).ratio() >= .84)
                      for time, value in history if source - time <= 300)
            index = len(queries)
            queries.append("query: " + row["text"])
            if old:
                suppressed += 1
            else:
                novel.append(row)
                line_queries.append(index)
        history = [(time, value) for time, value in history if source - time <= 300]
        history += [(source, normalize(label)) for label in labels]
        events.append(dict(source=source, source_start=previous_time, overlay=overlay, text=" | ".join(row["text"] for row in novel),
                           context=" | ".join(labels), line_indices=line_queries, state_index=state_index,
                           ocr_score=mean(float(row["score"]) for row in novel) if novel else 0))
        previous_time = source
    windows = []
    transcript = sorted(transcript, key=lambda row: row["start"])
    for i, row in enumerate(transcript):
        # Input ASR is already ordered; only its immutable timestamps determine captions.
        nearby = [other["text"] for other in transcript[max(0, i-1):i+2]
                  if other["end"] >= row["start"]-8 and other["start"] <= row["end"]+8]
        windows.append(dict(start=float(row["start"]), end=float(row["end"]), text=row["text"],
                            context="".join(nearby), words=row.get("words", [])))
    return dict(queries=queries, events=events, windows=windows, suppressed=suppressed)


def request_for(data):
    return dict(left=data["queries"],
                right=["passage: " + row["context"] for row in data["windows"]] +
                      ["passage: " + row["text"] for row in data["windows"]],
                phonetic_texts=list(dict.fromkeys([row["text"] for row in data["events"]] +
                                                  [row["context"] for row in data["windows"]])))


def _review(event, reason):
    return dict(kind="source_review", reason=reason, source_start=event["source_start"],
                source_end=event["source"], inspection_source=event["source"],
                target_start=None, target_end=None, content=event["text"] or event["context"], status="pending")


def _constraints(project, source):
    for first, second in mapping_segments(protected_anchors(project), project.cuts):
        if first.source < source < second.source:
            return first.target, second.target
    return None


def _topic_tail(data, path, duration):
    """Infer a new-topic tail only after the board's last region has been reached.
    Linguistic evidence + word times supply the boundary; no reference times/terms.
    A suggested boundary is never counted as a content match.
    """
    if not path or not data["events"]:
        return []
    max_source = max(row["source"] for row in data["events"])
    near_end = [node for node in path if node["source"] >= max_source * .95]
    if not near_end:
        return []
    after = max(node["target"] for node in near_end)
    board_terms = tokens(" ".join(row["context"] for row in data["events"]))
    cue = re.compile(r"(?:让我们|我们(?:来|再)?)(?:瞅一眼|看一看|看看|简要聊聊|聊聊|探讨|讨论)")
    for index, row in enumerate(data["windows"]):
        if row["start"] < max(after, duration*.75):
            continue
        found = cue.search(row["text"])
        if not found:
            continue
        body = row["text"][found.end():]
        new = tokens(body) - board_terms
        if len(new) < 3 or len(new)/max(1, len(tokens(body))) < .6:
            continue
        following = data["windows"][index+1:index+7]
        if sum(bool(new & tokens(other["text"])) for other in following) < 2:
            continue
        # Prefer the immediately preceding shared topic phrase (e.g. a transition
        # word ending the preceding clause), then the cue itself.
        prefix = row["text"][:found.start()]
        common = sorted(tokens(prefix) & tokens(body), key=lambda word: prefix.rfind(word), reverse=True)
        phrase = common[0] if common else row["text"][found.start():found.start()+2]
        position = prefix.rfind(phrase) if common else found.start()
        word_chars, char_times = "", []
        for word in row["words"]:
            start, end, text = (word["start"], word["end"], word.get("word", word.get("text", ""))) if isinstance(word, dict) else word[:3]
            value = normalize(str(text))
            word_chars += value
            char_times += [float(start)]*len(value)
        normalized_before = normalize(row["text"][:max(0, position)])
        char_index = min(len(char_times)-1, len(normalized_before))
        boundary = char_times[char_index] if char_times else row["start"]
        return [dict(kind="missing_board", target_start=round(boundary, 3), target_end=duration,
                     boundary_uncertainty=[round(max(row["start"], boundary-1), 3),
                                           round(min(duration, following[0]["start"] if following else row["end"]), 3)],
                     inspection_source=None, source_start=None, source_end=None,
                     context_source=max((e["source"] for e in data["events"] if not e.get("overlay")), default=max_source), context_only=True, status="pending",
                     reason="板书已到末区；录音出现未见于板书的新话题，后续继续讲解并进入总结。边界为自动推测，需人工确认。",
                     content=body, evidence=dict(cue=row["text"], novel_terms=sorted(new),
                                                 following=[other["text"] for other in following[:3]],
                                                 method="source-exhaustion-topic-transition-v1"))]
    return []


def semantic_match(project, data, semantic, interval, floor=.58, check=None):
    issues = [row["message"] for row in validate_project(project) if row["level"] == "error"]
    if issues:
        raise ValueError("人工锚点冲突：" + "；".join(issues))
    windows, events = data["windows"], data["events"]
    if not windows:
        raise ValueError("转写为空")
    scores = semantic["similarities"]
    phonetics = semantic.get("phonetics", {})
    docs = [terms(row["context"]) for row in windows]
    sounds = [set(phonetics.get(row["context"], [])) for row in windows]
    phonetic_df = Counter(word for doc in sounds for word in doc)
    df = Counter(word for doc in docs for word in doc)
    weights = {word: math.log(1+len(windows)/(1+count)) for word, count in df.items()}
    recap = next((row["start"] for row in windows if row["start"] > project.duration*.7
                  and re.search(r"最后.{0,4}总结|总结一下", row["text"])), project.duration)
    n = len(windows)
    nodes, reviews, alternatives = [], [], {}
    for event in events:
        if check: check()
        if source_is_cut(project, event["source"]):
            continue
        if event.get("overlay"):
            reviews.append(_review(event, "录屏系统控件遮挡，已排除匹配和末帧选择；可查看源帧确认"))
            continue
        bounds = _constraints(project, event["source"])
        if not bounds:
            continue
        if not event["line_indices"]:
            if not event["context"]:
                reviews.append(_review(event, "纯图形、空白或 OCR 无可靠文字；录音位置可能未知"))
            continue
        state_sim = scores[event["state_index"]]
        similarities = [.30*state_sim[j] + .70*max(.65*scores[k][n+j] + .35*scores[k][j]
                                                for k in event["line_indices"]) for j in range(n)]
        eligible = [value for value, window in zip(similarities, windows) if window["start"] < recap]
        avg = mean(eligible)
        std = max(1e-4, math.sqrt(mean((value-avg)**2 for value in eligible)))
        keys, phone = terms(event["text"]), set(phonetics.get(event["text"], []))
        total_weight = sum(weights.get(word, math.log(1+n)) for word in keys)
        features = []
        for j, window in enumerate(windows):
            target = (window["start"]+window["end"])/2
            if not bounds[0]+.001 < target < min(bounds[1]-.001, recap):
                continue
            shared = keys & docs[j]
            lex = sum(weights.get(word, 1) for word in shared)/max(1e-9, total_weight)
            pshared = phone & sounds[j]
            ph = len(pshared)/max(1, len(phone))
            z = (similarities[j]-avg)/std
            corroborated = (len(shared) >= 2 or len(pshared) >= 2 or
                            any(math.log(1+len(windows)/(1+phonetic_df[word])) >= 3.5 for word in pshared) or
                            any((len(word) >= 6 or weights.get(word, 0) >= 3.5) and not word.startswith("id:")
                                for word in shared))
            good = ((similarities[j] >= .80 and z >= 2 and bool(shared or pshared)) or
                    (z >= 1 and ((lex >= .65 and len(shared) >= 2) or (ph >= .70 and len(pshared) >= 2))) or
                    (z >= 3 and similarities[j] >= .82))
            if not good:
                continue
            strength = z+1.2*lex+1.2*ph
            features.append(dict(source=event["source"], target=target, strength=strength, z=z,
                                 similarity=similarities[j], lex=lex, phonetic=ph,
                                 shared=sorted(shared), phonetic_shared=sorted(pshared),
                                 corroborated=corroborated, event=event, window=window))
        features.sort(key=lambda node: node["strength"], reverse=True)
        peaks = []
        for node in features:
            if all(abs(node["target"]-other["target"]) > 15 for other in peaks):
                peaks.append(node)
                if len(peaks) >= 8: break
        alternatives[event["source"]] = peaks
        nodes += peaks
        if not peaks:
            reviews.append(_review(event, "文字与语义证据不足，未强行匹配"))

    # The path chooses between evidence-bearing peaks. It can skip any frame.
    nodes.sort(key=lambda node: (node["source"], node["target"]))
    dp, parents = [], []
    for i, node in enumerate(nodes):
        if check: check()
        best, parent = node["strength"]-1, -1
        for j, older in enumerate(nodes[:i]):
            ds, dt = node["source"]-older["source"], node["target"]-older["target"]
            if ds < 1 or dt < 1: continue
            value = dp[j]+node["strength"]-1-min(.5, abs(math.log(ds/dt))*.08)
            if value > best:
                best, parent = value, j
        dp.append(best); parents.append(parent)
    path = []
    if dp:
        i = max(range(len(dp)), key=dp.__getitem__)
        while i >= 0:
            path.append(nodes[i]); i = parents[i]
        path.reverse()
    candidates = []
    for index, node in enumerate(path):
        low = path[index-1]["target"] if index else 0
        high = path[index+1]["target"] if index+1 < len(path) else recap
        other = [row["strength"] for row in alternatives[node["source"]]
                 if low < row["target"] < high and abs(row["target"]-node["target"]) > 25]
        margin = node["strength"]-max(other) if other else 10
        confidence = min(.97, .48+.07*min(node["z"], 5)+.13*node["lex"]+.10*node["phonetic"])
        if not node["corroborated"] or margin < .25:
            confidence = min(confidence, .57)
        event = node["event"]
        candidate = dict(id=stable_anchor_id(0, node["target"], node["source"], METHOD),
                         source=node["source"], target=round(node["target"], 4),
                         content=event["text"], origin="auto", locked=False, confidence=round(confidence, 4),
                         status="candidate" if confidence >= floor else "review",
                         evidence="本地语义 + 字词/读音 + 上下文顺序；仅取有证据的路径峰，采样偏差需核对",
                         match=dict(method=METHOD, shared_terms=node["shared"], phonetic_shared=node["phonetic_shared"],
                                    narration=node["window"]["text"], source_uncertainty=[event["source_start"], node["source"]],
                                    target_uncertainty=[node["window"]["start"], node["window"]["end"]],
                                    semantic_cosine=round(node["similarity"], 5), corpus_z=round(node["z"], 4),
                                    lexical_coverage=round(node["lex"], 4), phonetic_coverage=round(node["phonetic"], 4),
                                    path_alternative_margin=round(margin, 4), corroborated=node["corroborated"]))
        candidates.append(candidate)
        if candidate["status"] == "review":
            reviews.append(_review(event, "顺序候选仍缺独立字词/读音证据或存在近似替代，未写入映射"))
    selected_sources = {row["source"] for row in path}
    for event in events:
        if event["source"] in alternatives and event["source"] not in selected_sources and alternatives[event["source"]]:
            reviews.append(_review(event, "候选与全局顺序冲突或证据较弱，未强行插入"))
    missing = _topic_tail(data, path, project.duration)
    reviews += missing
    return dict(candidates=candidates, review_intervals=reviews, unmatchable_intervals=missing,
                semantic_model=semantic.get("model", {}), method=METHOD,
                source_quality=dict(last_clear_source=max((e["source"] for e in events if e["context"] and not e.get("overlay")), default=None),
                                    overlay_sources=[e["source"] for e in events if e.get("overlay")]),
                stats=dict(frames=len(events), events=sum(bool(row["line_indices"]) for row in events),
                           persistent_lines_suppressed=data["suppressed"], sequence_candidates=len(candidates),
                           accepted_candidates=sum(row["status"] == "candidate" for row in candidates),
                           protected_anchors=len(protected_anchors(project)), confidence_floor=floor,
                           recap_excluded_from=recap, semantic_only_candidates_require_review=True))


def repair_reviews(project, result, interval):
    """Project source intervals through reliable brackets, never nearest endpoints.
    Long unsupported brackets remain unknown, rather than presenting a false time.
    """
    accepted = [Anchor.from_dict(row) for row in result["candidates"] if row["status"] == "candidate"]
    protected = protected_anchors(project)
    # Cut boundaries remain in the model used by preview/export.
    points = sorted(protected+accepted, key=lambda row: row.target)
    unique = []
    for point in points:
        if unique and (point.target <= unique[-1].target+.001 or point.source < unique[-1].source):
            continue
        unique.append(point)
    brackets = mapping_segments(unique, project.cuts) if len(unique) >= 2 else []
    blocked = result.get("unmatchable_intervals", [])
    spans = []
    for left, right in brackets:
        if right.target-left.target <= MAX_SUPPORTED_GAP and right.source > left.source:
            start, end = left.target, right.target
            for gap in blocked:
                if gap["target_start"] < end and gap["target_end"] > start:
                    end = min(end, gap["target_start"])
            if end > start and left.origin != "boundary" and right.origin != "boundary":
                spans.append([round(start, 3), round(end, 3)])
    seconds = sum(end-start for start, end in spans)
    result["coverage"] = dict(definition="相邻有内容证据点间隔≤180秒的插值支撑；非逐帧正确率；不含结构边界和缺板书区",
                              max_bracket_seconds=MAX_SUPPORTED_GAP, supported_spans=spans,
                              supported_seconds=round(seconds, 3), duration=project.duration,
                              fraction=round(seconds/max(project.duration, 1), 6))
    def project_source(source):
        for left, right in brackets:
            if left.source <= source <= right.source and right.source > left.source:
                if right.target-left.target > MAX_SUPPORTED_GAP:
                    return None
                target = left.target+(source-left.source)/(right.source-left.source)*(right.target-left.target)
                if any(gap["target_start"] <= target <= gap["target_end"] for gap in blocked):
                    return None
                return target
        return None
    for item in result["review_intervals"]:
        if item.get("source_start") is not None:
            start, end = float(item["source_start"]), float(item["source_end"])
            item["inspection_source"] = item.get("inspection_source", end)
            # Record midpoint separately for independent interval scoring.
            projected = [project_source(value) for value in (start, (start+end)/2, end)]
            if all(value is not None for value in projected):
                pad = min(20.0, max(5.0, interval/2))
                item["target_start"] = round(max(0, min(projected)-pad), 3)
                item["target_end"] = round(min(project.duration, max(projected)+pad), 3)
                item["target_status"] = "estimated_from_supported_mapping"
                item["target_estimate_uncertainty_seconds"] = pad
            else:
                item["target_start"] = item["target_end"] = None
                item["target_status"] = "unknown"
        if "id" not in item:
            item["id"] = stable_anchor_id(0, item.get("target_start") or 0,
                                         item.get("inspection_source") or item.get("context_source") or 0,
                                         str(item["kind"])+str(item.get("source_start")))
    # Complement of support is visible as recording review spans, including explicit
    # missing-board tails; their illustrative source is labelled as context only.
    boundaries = sorted({0.0, project.duration, *(value for span in spans for value in span),
                         *(value for gap in blocked for value in (gap["target_start"], gap["target_end"]))})
    for start, end in zip(boundaries, boundaries[1:]):
        middle = (start+end)/2
        if end-start < 1 or any(a <= middle <= b for a, b in spans) or any(g["target_start"] <= middle <= g["target_end"] for g in blocked):
            continue
        closest = min(accepted, key=lambda row: abs(row.target-middle)) if accepted else (protected[0] if protected else None)
        result["review_intervals"].append(dict(
            id=stable_anchor_id(0, start, end, "unsupported-v2"), kind="unsupported_recording",
            target_start=round(start, 3), target_end=round(end, 3), source_start=None, source_end=None,
            inspection_source=None, context_source=closest.source if closest else None, context_only=True,
            content="", status="pending", reason="此录音区间缺可靠自动点支撑；源帧仅供上下文，不能视为已匹配"))
    result["stats"]["pending_reviews"] = len(result["review_intervals"])
    result["review_projection_version"] = 2


def upgrade_legacy_reviews(project):
    """Correct persisted v0.2/0.3 time suggestions in memory on project open."""
    report = project.analysis
    if not isinstance(report, dict) or not report.get("stats") or not isinstance(report.get("candidates"), list):
        return False
    if report.get("method") == METHOD or report.get("review_projection_version") == 2:
        return False
    # Old general recording gaps are regenerated. Preserve source item IDs and
    # resolution state; nothing in anchors/cuts/captions is changed.
    report["review_intervals"] = [item for item in report.get("review_intervals", [])
                                  if item.get("source_start") is not None or item.get("kind") == "missing_board"]
    repair_reviews(project, report, float(report.get("settings", {}).get("interval", 60)))
    report["legacy_review_migration"] = "历史录音建议已按现有证据点修正；未知位置请听录音核对"
    return True
