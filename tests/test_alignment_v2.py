from __future__ import annotations
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from alignstudio.alignment import prepare, semantic_match, repair_reviews, _topic_tail, upgrade_legacy_reviews
from alignstudio.analyzer import refinement_times
from alignstudio.model import Anchor, Project, Cut, map_target_to_source
from alignstudio.semantic_worker import validate_model


def project(duration=600):
    return Project(anchors=[Anchor("start", 0, 0, origin="boundary", locked=True),
                            Anchor("end", duration, duration, origin="boundary", locked=True)])


def point(source, target):
    return dict(id=f"auto-{source}", source=source, target=target, origin="auto",
                locked=False, status="candidate", confidence=.8)


class ReviewProjectionTests(unittest.TestCase):
    def test_source_range_projects_through_its_bracket_and_keeps_inspection_frame(self):
        p=project()
        result=dict(candidates=[point(100, 50), point(200, 110)], stats={}, review_intervals=[
            dict(kind="review", source_start=140, source_end=160, reason="核对")])
        repair_reviews(p,result,20)
        item=result["review_intervals"][0]
        self.assertEqual(item["inspection_source"],160)
        self.assertEqual((item["target_start"],item["target_end"]),(64,96))
        self.assertLessEqual(item["target_start"],80)
        self.assertGreaterEqual(item["target_end"],80)

    def test_unsupported_ranges_are_unknown_not_clamped_to_zero_or_movie_end(self):
        p=project()
        result=dict(candidates=[point(10,10),point(500,500)],stats={},review_intervals=[
            dict(kind="review",source_start=200,source_end=220,reason="证据不足")])
        repair_reviews(p,result,20)
        item=result["review_intervals"][0]
        self.assertEqual(item["inspection_source"],220)
        self.assertIsNone(item["target_start"])
        self.assertIsNone(item["target_end"])
        self.assertEqual(item["target_status"],"unknown")
        self.assertEqual(result["coverage"]["supported_seconds"],0)

    def test_cut_projection_is_right_continuous_and_skips_deleted_source(self):
        p=project(120)
        p.cuts=[Cut(id="cut",target=60,source_start=60,source_end=80)]
        p.anchors[-1].source=140
        result=dict(candidates=[point(40,40),point(100,80)],stats={},review_intervals=[
            dict(kind="review",source_start=80,source_end=90,reason="跳变右侧")])
        repair_reviews(p,result,20)
        item=result["review_intervals"][0]
        self.assertEqual(item["inspection_source"],90)
        self.assertEqual(item["target_start"],50)
        self.assertEqual(item["target_end"],80)
        self.assertEqual(map_target_to_source(p.anchors,60,p.cuts),80)

    def test_missing_tail_is_separate_from_general_unsupported_gap(self):
        p=project()
        tail=dict(kind="missing_board",target_start=450,target_end=600,source_start=None,source_end=None,
                  context_source=590,context_only=True,reason="新话题",status="pending")
        result=dict(candidates=[point(10,10),point(200,200)],stats={},unmatchable_intervals=[tail],
                    review_intervals=[tail])
        repair_reviews(p,result,20)
        unsupported=[r for r in result["review_intervals"] if r["kind"]=="unsupported_recording"]
        self.assertTrue(any(r["target_end"]==450 for r in unsupported))
        self.assertFalse(any(r["target_end"]>450 for r in unsupported))
        self.assertEqual(tail["target_start"],450)


class LegacyMigrationTests(unittest.TestCase):
    def test_old_report_is_upgraded_without_moving_any_edit_or_source_item(self):
        p=project()
        p.analysis=dict(method="old-lexical",settings=dict(interval=20),stats=dict(frames=2),
                        candidates=[point(10,10),point(500,500)],
                        review_intervals=[dict(id="old-review",kind="low",source_start=200,source_end=220,
                                               target_start=0,target_end=20,status="resolved",reason="重复讲解")])
        before=copy.deepcopy(p.to_dict())
        self.assertTrue(upgrade_legacy_reviews(p))
        row=p.analysis["review_intervals"][0]
        self.assertEqual(row["id"],"old-review")
        self.assertEqual(row["status"],"resolved")
        self.assertEqual(row["inspection_source"],220)
        self.assertIsNone(row["target_start"])
        for key in ("anchors","cuts","captions"):
            self.assertEqual(before[key],p.to_dict()[key])
        report=copy.deepcopy(p.analysis)
        self.assertFalse(upgrade_legacy_reviews(p))
        self.assertEqual(report,p.analysis)

    def test_current_report_and_unanalysed_project_are_left_alone(self):
        p=project()
        self.assertFalse(upgrade_legacy_reviews(p))
        p.analysis=dict(method="e5-phonetic-context-monotone-v2",stats=dict(frames=2),candidates=[])
        before=copy.deepcopy(p.analysis)
        self.assertFalse(upgrade_legacy_reviews(p))
        self.assertEqual(before,p.analysis)


class EvidenceTests(unittest.TestCase):
    def test_semantic_only_peak_cannot_inflate_accepted_coverage(self):
        p=project(200)
        frames=[dict(board_time=100,findings=[dict(text="unrelated foreign technical heading",score=.99)])]
        transcript=[dict(start=i*8,end=i*8+3,text=f"说明第{i}种中文原理") for i in range(20)]
        data=prepare(frames,transcript)
        n=len(transcript)
        matrix=[[.70]*(n*2) for _ in data["queries"]]
        for row in matrix: row[10]=row[n+10]=.96
        result=semantic_match(p,data,dict(similarities=matrix,phonetics={}),20)
        self.assertEqual(len(result["candidates"]),1)
        self.assertEqual(result["candidates"][0]["status"],"review")
        self.assertLess(result["candidates"][0]["confidence"],.58)
        self.assertEqual(result["stats"]["accepted_candidates"],0)

    def test_semantic_peak_observes_manual_lock_and_independent_source_cut(self):
        frames=[dict(board_time=40,findings=[dict(text="操作系统内存分配",score=.99)])]
        transcript=[dict(start=i*8,end=i*8+3,text="操作系统内存分配") for i in range(20)]
        data=prepare(frames,transcript)
        matrix=[[.70]*40 for _ in data["queries"]]
        for row in matrix:row[16]=row[36]=.98
        payload=dict(similarities=matrix,phonetics={})
        self.assertTrue(semantic_match(project(200),data,payload,20)["candidates"])
        p=project(200);p.anchors.insert(1,Anchor("manual-lock",60,60,locked=True))
        self.assertFalse(semantic_match(p,data,payload,20)["candidates"])
        p=project(200);p.cuts=[Cut(id="cut",target=30,source_start=30,source_end=50)]
        self.assertFalse(semantic_match(p,data,payload,20)["candidates"])

    def test_flat_unrelated_semantics_does_not_force_path(self):
        p=project(200)
        frames=[dict(board_time=100,findings=[dict(text="操作系统",score=.99)])]
        transcript=[dict(start=i*8,end=i*8+3,text="无关的烹饪菜谱") for i in range(20)]
        data=prepare(frames,transcript)
        result=semantic_match(p,data,dict(similarities=[[.72]*40 for _ in data["queries"]],phonetics={}),20)
        self.assertEqual(result["candidates"],[])
        self.assertTrue(result["review_intervals"])

    def test_recap_does_not_reuse_old_material_for_false_late_matches(self):
        p=project(200)
        frames=[dict(board_time=100,findings=[dict(text="内存分配",score=.99)])]
        transcript=[dict(start=i*8,end=i*8+3,text="普通说明") for i in range(20)]
        transcript[18]["text"]="最后总结一下内存分配"
        data=prepare(frames,transcript);n=len(transcript)
        matrix=[[.70]*40 for _ in data["queries"]]
        for row in matrix:row[18]=row[n+18]=.99
        result=semantic_match(p,data,dict(similarities=matrix,phonetics={}),20)
        self.assertEqual(result["stats"]["recap_excluded_from"],144)
        self.assertFalse(result["candidates"])

    def test_refinement_budget_is_spread_across_movie_and_zero_is_zero(self):
        candidates=[dict(source=50+i*100,confidence=.9-i*.001) for i in range(12)]
        refined=refinement_times(dict(candidates=candidates),list(range(0,1200,60)),1200,60,24)
        self.assertEqual(len(refined),24)
        self.assertGreaterEqual(len({int(t/240) for t in refined}),5)
        self.assertTrue(any(t>1199 for t in refined))
        self.assertEqual(refinement_times(dict(candidates=candidates),[],1200,60,0),[])

    def test_system_overlay_is_excluded_from_matching_and_freeze_selection(self):
        p=project(200)
        frames=[dict(board_time=100,findings=[dict(text="系统内存分配",score=.99)]),
                dict(board_time=198,findings=[dict(text="未在播放 专注模式 系统内存分配",score=.99)])]
        transcript=[dict(start=i*8,end=i*8+3,text="系统内存分配") for i in range(20)]
        data=prepare(frames,transcript)
        result=semantic_match(p,data,dict(similarities=[[.72]*40 for _ in data["queries"]],phonetics={}),20)
        self.assertEqual(result["source_quality"]["last_clear_source"],100)
        self.assertEqual(result["source_quality"]["overlay_sources"],[198])
        self.assertFalse(any(c["source"]==198 for c in result["candidates"]))

    def test_no_reference_information_is_accepted_in_preparation(self):
        data=prepare([dict(board_time=20,findings=[])],[dict(start=10,end=15,text="原始录音")])
        self.assertNotIn("reference",data)
        self.assertEqual(data["windows"][0]["start"],10)

    def test_model_corruption_is_detected_before_cache_can_mask_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/"weights";p.write_bytes(b"abc")
            manifest=dict(files=[dict(filename="weights",bytes=3,sha256=hashlib.sha256(b"abc").hexdigest())])
            validate_model(root,manifest)
            p.write_bytes(b"def")
            with self.assertRaisesRegex(ValueError,"SHA-256"):
                validate_model(root,manifest)

    def test_topic_transition_uses_generic_cue_word_times_and_requires_board_end(self):
        text="前一个主题结束并切换,让我们看看新的上下文切换寄存器保存成本。"
        words=[[100+i*.1,100+(i+1)*.1,ch,.9] for i,ch in enumerate(text)]
        data=dict(events=[dict(source=100,context="队列调度")],
                  windows=[dict(start=100,end=110,text=text,words=words),
                           dict(start=111,end=114,text="上下文寄存器需要保存",words=[]),
                           dict(start=115,end=118,text="寄存器保存有成本",words=[])])
        result=_topic_tail(data,[dict(source=100,target=80)],120)
        self.assertEqual(len(result),1)
        self.assertAlmostEqual(result[0]["target_start"],100+text.index("切换")*.1)
        self.assertTrue(result[0]["context_only"])
        self.assertEqual(_topic_tail(data,[dict(source=50,target=80)],120),[])


if __name__=="__main__":unittest.main()
