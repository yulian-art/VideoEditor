from __future__ import annotations
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from alignstudio.analyzer import sample_times, load_rows, atomic_json
from alignstudio.matching import match, merge_result, board_events
from alignstudio.model import Anchor, Project, load_csv, save_csv


def project():
    return Project(anchors=[
        Anchor("start", 0, 0, origin="boundary", locked=True),
        Anchor("end", 120, 120, origin="boundary", locked=True),
    ])


def frame(time, text):
    return {"board_time": time, "findings": [{"text": text, "score": 0.99, "box": []}]}


class SamplingTests(unittest.TestCase):
    def test_60_20_grid_and_invalid_intervals(self):
        coarse, fine = sample_times(3332.03, 60), sample_times(3332.03, 20)
        self.assertEqual(len(coarse), 56)
        self.assertEqual(len(fine), 167)
        self.assertTrue(set(coarse) <= set(fine))
        for value in (0, -1, math.nan, math.inf, 0.1, 3601):
            with self.assertRaises(ValueError):
                sample_times(10, value)

    def test_import_rejects_foreign_out_of_range_and_invalid_confidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ocr.jsonl"
            path.write_text(json.dumps(frame(200, "队列分配")), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_rows(str(path), "ocr", 120)
            data = frame(20, "队列分配")
            data["findings"][0]["score"] = math.nan
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_rows(str(path), "ocr", 120)


class MatchingTests(unittest.TestCase):
    def test_matching_observes_cancel_during_cpu_stage(self):
        def cancelled():
            raise InterruptedError("cancel")
        with self.assertRaises(InterruptedError):
            match(project(), [frame(10, "操作系统 内存分配器")],
                  [{"start": 15, "end": 20, "text": "操作系统 内存分配器"}], 20, check=cancelled)

    def test_shift_and_persistent_text_are_not_duplicate_anchors(self):
        frames = [frame(10, "操作系统 内存分配器"), frame(20, "操作系统 内存分配器"),
                  frame(50, "全局队列 任务调度"), frame(80, "网络轮询 线程休眠")]
        transcript = [{"start": 15, "end": 20, "text": "操作系统 内存分配器"},
                      {"start": 55, "end": 60, "text": "全局队列 任务调度"},
                      {"start": 85, "end": 90, "text": "网络轮询 线程休眠"}]
        result = match(project(), frames, transcript, 20)
        reliable = [row for row in result["candidates"] if row["status"] == "candidate"]
        self.assertEqual([(row["source"], row["target"]) for row in reliable],
                         [(10, 15), (50, 55), (80, 85)])
        self.assertGreater(result["stats"]["persistent_lines_suppressed"], 0)

    def test_ambiguous_repetition_and_empty_diagrams_need_review(self):
        result = match(project(), [frame(10, "操作系统 内存分配器"), frame(50, "")], [
            {"start": 15, "end": 20, "text": "操作系统 内存分配器"},
            {"start": 75, "end": 80, "text": "操作系统 内存分配器"},
        ], 20)
        self.assertFalse(any(row["status"] == "candidate" for row in result["candidates"]))
        self.assertTrue(any("图形" in row["reason"] for row in result["review_intervals"]))
        self.assertTrue(any("重复" in row["reason"] for row in result["review_intervals"]))

    def test_manual_bounds_stop_order_conflicts(self):
        p = project()
        p.anchors.insert(1, Anchor("confirmed", 60, 60, locked=True))
        result = match(p, [frame(30, "网络轮询 线程休眠")], [
            {"start": 80, "end": 85, "text": "网络轮询 线程休眠"},
        ], 20)
        self.assertEqual(result["candidates"], [])


class PreservationTests(unittest.TestCase):
    def test_reanalysis_preserves_manual_unlocked_and_locked_auto_and_freeze(self):
        p = project()
        p.anchors = [
            p.anchors[0], Anchor("manual", 20, 25, locked=False, content="已人工修改"),
            Anchor("locked-auto", 40, 45, origin="auto", locked=True, confidence=0.7),
            Anchor("replace-auto", 60, 65, origin="auto"),
            Anchor("freeze", 100, 110, locked=True), Anchor("end", 120, 110, locked=True),
        ]
        original = copy.deepcopy(p.to_dict())
        result = {"candidates": [
            {"id": "new", "source": 75, "target": 70, "status": "candidate", "origin": "auto", "confidence": 0.8},
            {"id": "conflict", "source": 15, "target": 30, "status": "candidate", "origin": "auto", "confidence": 0.8},
        ], "stats": {}, "review_intervals": []}
        merged = merge_result(p, result)
        self.assertEqual(p.to_dict(), original)
        retained = {anchor.id: anchor for anchor in merged.anchors}
        for anchor in p.anchors:
            if anchor.id != "replace-auto":
                self.assertEqual(retained[anchor.id], anchor)
        self.assertNotIn("replace-auto", retained)
        self.assertIn("new", retained)
        self.assertNotIn("conflict", retained)
        self.assertEqual(merged.anchors[-1].source, merged.anchors[-2].source)

    def test_project_and_csv_keep_stable_identity_confidence_and_locks(self):
        p = project()
        p.anchors.insert(1, Anchor("chosen", 30, 35, origin="auto", locked=True,
                                  confidence=0.81, match={"shared_terms": ["队列"]}))
        p.analysis = {"review_intervals": [{"id": "review", "status": "resolved"}]}
        restored = Project.from_dict(json.loads(json.dumps(p.to_dict())))
        self.assertEqual(restored, p)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roundtrip.csv"
            save_csv(p, path)
            imported = load_csv(path)
        self.assertEqual([(row.id, row.origin, row.locked, row.confidence) for row in imported.anchors],
                         [(row.id, row.origin, row.locked, row.confidence) for row in p.anchors])

    def test_atomic_result_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "result.json"
            atomic_json(path, {"state": "completed", "review": "需核对"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["review"], "需核对")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
