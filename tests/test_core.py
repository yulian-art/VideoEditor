from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from alignstudio.exporter import build_filtergraph, render_parts
from alignstudio.model import (
    Anchor,
    Project,
    csv_text,
    format_timestamp,
    load_csv,
    map_target_to_source,
    parse_timestamp,
    srt_text,
    Caption,
    validate_anchors,
)


def anchor(target: float, source: float, name: str = "") -> Anchor:
    return Anchor(id=f"a-{target}-{source}", target=target, source=source, content=name)


class TimestampTests(unittest.TestCase):
    def test_parse_and_format_round_trip(self) -> None:
        value = parse_timestamp("00:51:18.395")
        self.assertAlmostEqual(value, 3078.395)
        self.assertEqual(format_timestamp(value), "00:51:18.395")


class CsvTests(unittest.TestCase):
    def test_existing_schema_and_terminal_freeze(self) -> None:
        text = (
            "录音时间（成片）,原板书时间,内容,核对方式,至下一锚点的画面倍速\n"
            "00:00:00.000,00:00:00.000,起点,区间边界,1.000\n"
            "00:00:10.000,00:00:10.000,板书结束,人工,0.000\n"
            "00:00:15.000,00:00:10.000,录音结束,最后一帧定格,\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "对齐.csv"
            path.write_text("﻿" + text, encoding="utf-8")
            project = load_csv(path)
        self.assertEqual(len(project.anchors), 3)
        self.assertEqual(project.anchors[-1].source, project.anchors[-2].source)
        issues = validate_anchors(project.anchors)
        self.assertTrue(any(item["level"] == "info" and "定格" in item["message"] for item in issues))
        output = csv_text(project)
        self.assertIn("最后一帧定格", output)
        self.assertTrue(output.rstrip().endswith(","))


class MappingTests(unittest.TestCase):
    def test_board_five_seconds_ahead(self) -> None:
        anchors = [anchor(0, 0), anchor(10, 5), anchor(20, 15)]
        self.assertAlmostEqual(map_target_to_source(anchors, 10), 5)
        self.assertAlmostEqual((anchors[1].source - anchors[0].source) / 10, 0.5)

    def test_board_five_seconds_behind(self) -> None:
        anchors = [anchor(0, 0), anchor(5, 10), anchor(15, 20)]
        self.assertAlmostEqual(map_target_to_source(anchors, 5), 10)
        self.assertAlmostEqual((anchors[1].source - anchors[0].source) / 5, 2.0)

    def test_freeze_maps_every_target_to_one_source_frame(self) -> None:
        anchors = [anchor(0, 0), anchor(10, 10), anchor(15, 10)]
        self.assertAlmostEqual(map_target_to_source(anchors, 12.5), 10)


class FiltergraphTests(unittest.TestCase):
    def test_linear_runs_and_freeze_are_distinct_parts(self) -> None:
        project = Project(fps=30, anchors=[anchor(0, 0), anchor(5, 10), anchor(10, 15), anchor(15, 15)])
        parts = render_parts(project)
        self.assertEqual([part.kind for part in parts], ["linear", "freeze"])
        graph, metadata = build_filtergraph(project)
        self.assertIn("tpad=stop_mode=clone", graph)
        self.assertIn("concat=n=2", graph)
        self.assertEqual(metadata["frame_count"], 450)
        self.assertEqual(metadata["parts"][-1]["frames"], 150)

    def test_extreme_speed_is_preserved(self) -> None:
        project = Project(fps=30, anchors=[anchor(0, 0), anchor(3.35, 0), anchor(6.70, 40)])
        graph, _ = build_filtergraph(project)
        self.assertIn("0.083750000000000", graph)


class CaptionTests(unittest.TestCase):
    def test_srt_keeps_audio_timestamps(self) -> None:
        text = srt_text([Caption(5.0, 7.25, "固定录音字幕")])
        self.assertIn("00:00:05,000 --> 00:00:07,250", text)
        self.assertIn("固定录音字幕", text)


if __name__ == "__main__":
    unittest.main()
