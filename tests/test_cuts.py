from __future__ import annotations
import copy
import json
import tempfile
import unittest
from pathlib import Path
from alignstudio.editing import add_cut, remove_cut
from alignstudio.exporter import build_filtergraph, render_parts
from alignstudio.matching import match, merge_result
from alignstudio.model import (Anchor, Caption, Project, ProjectError, load_csv, save_csv,
                               map_target_to_source, map_source_to_target, validate_project)


def project():
    return Project(anchors=[Anchor("start",0,0,locked=True), Anchor("end",20,20,locked=True)],
                   captions=[Caption(5.5,6.5,"跨越裁剪时刻的固定录音字幕")])


class CutTests(unittest.TestCase):
    def test_tail_cut_suspends_all_repeated_last_frame_points(self):
        p=Project(anchors=[Anchor("start",0,0,locked=True),Anchor("freeze-start",10,16),
                          Anchor("end",20,16,locked=False)])
        original=copy.deepcopy(p.anchors)
        cut=add_cut(p,12,16,manual_policy="suspend")
        self.assertEqual({row.id for row in cut.cuts[0].excluded_anchors},{"freeze-start","end"})
        self.assertEqual(remove_cut(cut,cut.cuts[0].id).anchors,original)

    def test_interior_jump_is_right_continuous_and_never_reads_removed_source(self):
        p=add_cut(project(),6,10,target=6)
        self.assertAlmostEqual(map_target_to_source(p.anchors,5.9,p.cuts),5.9)
        self.assertEqual(map_target_to_source(p.anchors,6,p.cuts),10)
        self.assertAlmostEqual(map_target_to_source(p.anchors,6.1,p.cuts),10+0.1*10/14)
        with self.assertRaises(ProjectError):
            map_source_to_target(p.anchors,8,p.cuts)
        self.assertEqual(map_source_to_target(p.anchors,10,p.cuts),6)
        for i in range(200):
            value=map_target_to_source(p.anchors,i/10,p.cuts)
            self.assertFalse(6 <= value < 10)
        self.assertEqual(p.duration,20)
        self.assertEqual(p.captions,project().captions)

    def test_automatic_points_are_stashed_and_restored_with_same_ids(self):
        p=project()
        p.anchors.insert(1,Anchor("auto8",8,8,origin="auto",confidence=0.8,match={"term":"内存"}))
        original=copy.deepcopy(p)
        cut=add_cut(p,6,10,6)
        self.assertEqual(p,original)
        self.assertNotIn("auto8",[row.id for row in cut.anchors])
        self.assertEqual(cut.cuts[0].excluded_anchors,[p.anchors[1]])
        restored=remove_cut(Project.from_dict(cut.to_dict()),cut.cuts[0].id)
        self.assertEqual(restored.anchors,original.anchors)
        self.assertEqual(restored.captions,original.captions)
        self.assertEqual(restored.cuts,[])

    def test_manual_points_require_explicit_policy_and_locked_points_block(self):
        p=project()
        p.anchors.insert(1,Anchor("manual8",8,8,content="人工证据"))
        before=p.to_dict()
        with self.assertRaisesRegex(ProjectError,"人工点"):
            add_cut(p,6,10,6)
        cut=add_cut(p,6,10,6,manual_policy="suspend")
        self.assertEqual(cut.cuts[0].excluded_anchors[0].content,"人工证据")
        self.assertEqual(p.to_dict(),before)
        for origin in ("manual","auto"):
            p.anchors[1].origin=origin
            p.anchors[1].locked=True
            with self.assertRaisesRegex(ProjectError,"锁定"):
                add_cut(p,6,10,6,manual_policy="suspend")

    def test_head_and_tail_trim_keep_audio_duration_and_restore_boundaries(self):
        for start,end in ((0,4),(16,20)):
            p=project()
            index=0 if start==0 else -1
            p.anchors[index].locked=False
            original=copy.deepcopy(p.anchors)
            cut=add_cut(p,start,end,manual_policy="suspend")
            self.assertEqual(cut.duration,20)
            self.assertEqual(cut.anchors[0].target,0)
            self.assertEqual(cut.anchors[-1].target,20)
            self.assertEqual(map_target_to_source(cut.anchors,0,cut.cuts),4 if start==0 else 0)
            self.assertEqual(map_target_to_source(cut.anchors,20,cut.cuts),20 if start==0 else 16)
            self.assertEqual(remove_cut(cut,cut.cuts[0].id).anchors,original)

    def test_multiple_cuts_and_freeze_render_as_independent_parts(self):
        p=project()
        p.anchors.insert(1,Anchor("freeze-left",14,16))
        p.anchors.insert(2,Anchor("freeze-right",16,16))
        p=add_cut(p,4,6,4)
        p=add_cut(p,10,12,10)
        parts=render_parts(p)
        self.assertEqual(sum(part.frames for part in parts),600)
        self.assertEqual(len(parts),5)
        self.assertEqual([part.kind for part in parts],["linear","linear","linear","freeze","linear"])
        self.assertEqual(map_target_to_source(p.anchors,4,p.cuts),6)
        self.assertEqual(map_target_to_source(p.anchors,10,p.cuts),12)
        self.assertEqual(map_target_to_source(p.anchors,15,p.cuts),16)
        graph,metadata=build_filtergraph(p)
        self.assertIn("concat=n=5",graph)
        self.assertEqual(len(metadata["cuts"]),2)

    def test_project_json_and_csv_preserve_cut_and_stashed_metadata(self):
        p=project()
        p.anchors.insert(1,Anchor("auto8",8,8,origin="auto",confidence=0.8))
        p=add_cut(p,6,10,6,note="跳过重复板书")
        self.assertEqual(Project.from_dict(json.loads(json.dumps(p.to_dict()))),p)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"cut.csv"
            save_csv(p,path)
            loaded=load_csv(path)
        self.assertEqual(loaded.anchors,p.anchors)
        self.assertEqual(loaded.cuts,p.cuts)
        self.assertEqual(map_target_to_source(loaded.anchors,6,loaded.cuts),10)

    def test_restoration_conflict_is_atomic(self):
        p=project()
        p.anchors.insert(1,Anchor("auto8",8,8,origin="auto"))
        p=add_cut(p,6,10,6)
        p.anchors.insert(1,Anchor("new-manual",8,12,locked=True))
        before=p.to_dict()
        with self.assertRaisesRegex(ProjectError,"保持不变"):
            remove_cut(p,p.cuts[0].id)
        self.assertEqual(p.to_dict(),before)

    def test_reanalysis_does_not_reintroduce_cut_anchors(self):
        p=add_cut(project(),6,10,6)
        result=match(p,[{"board_time":8,"findings":[{"text":"操作系统 内存分配器","score":0.99}]}],
                     [{"start":8,"end":9,"text":"操作系统 内存分配器"}],20)
        self.assertEqual(result["candidates"],[])
        merged=merge_result(p,{"stats":{},"candidates":[
            {"id":"old-auto","source":8,"target":8,"status":"candidate","origin":"auto","confidence":0.9}
        ]})
        self.assertNotIn("old-auto",[row.id for row in merged.anchors])
        self.assertEqual(merged.cuts,p.cuts)

    def test_overlap_invalid_target_and_whole_video_are_rejected(self):
        p=add_cut(project(),6,10,6)
        for start,end,target in ((8,12,8),(6,10,21),(10,8,10)):
            with self.assertRaises(ProjectError):
                add_cut(p,start,end,target,manual_policy="suspend")
        with self.assertRaisesRegex(ProjectError,"整个"):
            add_cut(project(),0,20,0,"suspend")

if __name__=="__main__":
    unittest.main()
