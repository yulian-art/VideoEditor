"""Independent whole-film and review-interval recomputation. Reference is evaluation-only."""
from __future__ import annotations
import argparse
import copy
import csv
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from alignstudio.model import Project, load_csv, map_target_to_source, map_source_to_target
from alignstudio.matching import merge_result
from alignstudio.alignment import repair_reviews, MAX_SUPPORTED_GAP


def summary(values):
    values=sorted(values)
    return dict(n=len(values),median=round(statistics.median(values),3) if values else None,
                p90=round(values[max(0,math.ceil(len(values)*.9)-1)],3) if values else None,
                maximum=round(max(values),3) if values else None)


def source_reviews(result,reference):
    items, errors, widths=[],[],[]
    low_source,high_source=min(a.source for a in reference.anchors),max(a.source for a in reference.anchors)
    for row in result.get("review_intervals",[]):
        if row.get("source_start") is None or row.get("source_end") is None:
            continue
        midpoint=(row["source_start"]+row["source_end"])/2
        scorable=low_source<=midpoint<=high_source
        expected=map_source_to_target(reference.anchors,midpoint,reference.cuts) if scorable else None
        low,high=row.get("target_start"),row.get("target_end")
        known=low is not None and high is not None
        distance=max(low-expected,expected-high,0) if known and scorable else None
        if known and scorable:
            errors.append(distance);widths.append(high-low)
        items.append(dict(id=row.get("id"),source_start=row["source_start"],source_end=row["source_end"],
                          inspection_source=row.get("inspection_source",row["source_end"]),
                          midpoint=midpoint,reference_target=round(expected,3) if expected is not None else None,reference_scorable=scorable,
                          target_start=low,target_end=high,distance_outside=None if distance is None else round(distance,3)))
    metrics=dict(total=len(items),known=len(errors),unknown=len(items)-len(errors),
                 outside=sum(value>0 for value in errors),outside_gt300=sum(value>300 for value in errors),
                 distance_outside_seconds=summary(errors),interval_width_seconds=summary(widths),
                 reference_unscorable=sum(not row["reference_scorable"] for row in items),
                 unknowns_are_not_scored_as_zero=True)
    return metrics,items


def inverse_error(reference,source,target):
    first,last=min(a.source for a in reference.anchors),max(a.source for a in reference.anchors)
    if not first<=source<=last:
        return None
    intervals=[]
    for left,right in zip(reference.anchors,reference.anchors[1:]):
        if abs(source-left.source)<1e-8 and abs(right.source-left.source)<1e-8:
            intervals.append((left.target,right.target))
        elif left.source<=source<=right.source and right.source>left.source:
            t=left.target+(source-left.source)/(right.source-left.source)*(right.target-left.target)
            intervals.append((t,t))
    return min(max(a-target,target-b,0) for a,b in intervals) if intervals else None


def evaluate(result,reference,step):
    project=merge_result(Project.from_dict(result["project"]),result)
    max_source=max(a.source for a in reference.anchors)
    board_end=min(a.target for a in reference.anchors if abs(a.source-max_source)<1e-6)
    all_grid=[float(t) for t in range(0,math.ceil(project.duration),step) if t<project.duration]
    details=[]
    for target in all_grid:
        actual=map_target_to_source(project.anchors,target,project.cuts)
        expected=map_target_to_source(reference.anchors,target,reference.cuts)
        scope="board_available" if target<board_end else "missing_board"
        equivalent=inverse_error(reference,actual,target) if scope=="board_available" else None
        details.append(dict(target=target,scope=scope,predicted_source=round(actual,6),
                            reference_source=round(expected,6),source_error=round(abs(actual-expected),6),
                            audio_equivalent_error=None if equivalent is None else round(equivalent,6)))
    comparable=[r for r in details if r["scope"]=="board_available"]
    valid=[r["audio_equivalent_error"] for r in comparable if r["audio_equivalent_error"] is not None]
    auto=[r for r in result["candidates"] if r.get("status")=="candidate"]
    candidates=[]
    for row in result["candidates"]:
        scorable=min(a.source for a in reference.anchors)<=row["source"]<=max_source
        expected=map_source_to_target(reference.anchors,row["source"],reference.cuts) if scorable else None
        candidates.append(dict(id=row["id"],source=row["source"],target=row["target"],status=row["status"],
                               content=row["content"],confidence=row["confidence"],
                               reference_target=round(expected,4) if scorable else None,error=round(abs(row["target"]-expected),4) if scorable else None,reference_scorable=scorable))
    accepted=[r["error"] for r in candidates if r["status"]=="candidate" and r["error"] is not None]
    nodes=sorted([row["target"] for row in auto])
    gaps=[dict(start=a,end=b,seconds=round(b-a,3)) for a,b in zip([0]+nodes,nodes+[board_end])
          if b-a>MAX_SUPPORTED_GAP and a<board_end]
    for row in gaps:
        row["end"]=min(board_end,row["end"])
        row["seconds"]=round(row["end"]-row["start"],3)
    known_reviews,review_rows=source_reviews(result,reference)
    detected=[]
    for gap in result.get("unmatchable_intervals",[]):
        a,b=gap["target_start"],gap["target_end"]
        overlap=max(0,min(b,reference.duration)-max(a,board_end))
        detected.append(dict(kind=gap["kind"],start=a,end=b,reference_start=board_end,
                             boundary_error=round(abs(a-board_end),3),uncertainty=gap.get("boundary_uncertainty"),
                             precision=round(overlap/max(b-a,1e-9),6),
                             recall=round(overlap/max(reference.duration-board_end,1e-9),6)))
    within=lambda tolerance:sum(value<=tolerance for value in valid)
    spans=result.get("coverage",{}).get("supported_spans")
    if spans is None:
        spans=[[a,b] for a,b in zip(nodes,nodes[1:]) if b-a<=MAX_SUPPORTED_GAP]
    support=sum(b-a for a,b in spans)
    metrics=dict(
        samples=dict(base=len(result["sampling"]["base_times"]),refined=len(result["sampling"]["refined_times"])),
        duration=project.duration,board_available_until=board_end,missing_board_reference_seconds=round(project.duration-board_end,3),
        accepted_content_points=len(auto),review_candidates=sum(row["status"]=="review" for row in result["candidates"]),
        accepted_point_error_seconds=summary(accepted),accepted_points_gt20=sum(x>20 for x in accepted),
        accepted_points_gt60=sum(x>60 for x in accepted),last_accepted_target=max(nodes) if nodes else None,
        unsupported_brackets=gaps,maximum_unsupported_seconds=max((row["seconds"] for row in gaps),default=0),
        evidence_support=dict(seconds=round(support,3),fraction_of_whole_film=round(support/project.duration,6),
                              max_bracket_seconds=MAX_SUPPORTED_GAP,structural_boundaries_excluded=True),
        whole_grid=dict(step_seconds=step,total=len(all_grid),board_available=len(comparable),
                        no_board=len(all_grid)-len(comparable),out_of_reference_source=len(comparable)-len(valid),
                        source_error_seconds=summary([r["source_error"] for r in comparable]),
                        audio_equivalent_error_seconds=summary(valid),
                        within20=within(20),within60=within(60),
                        within20_fraction_of_board_prefix=round(within(20)/max(len(comparable),1),6),
                        within60_fraction_of_board_prefix=round(within(60)/max(len(comparable),1),6),
                        within60_fraction_of_whole_film=round(within(60)/max(len(all_grid),1),6),
                        tail_excluded_from_error_not_counted_as_match=True),
        native_review_intervals=known_reviews,missing_board_detection=detected,
        cache=result.get("cache",{}),elapsed_seconds=result.get("elapsed_seconds"),
        method=result.get("method"),confidence_floor=result.get("stats",{}).get("confidence_floor",.58))
    return metrics,dict(grid=details,candidates=candidates,reviews=review_rows)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--before",type=Path,required=True)
    parser.add_argument("--after",type=Path,required=True)
    parser.add_argument("--reference",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--step",type=int,default=10)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    reference=load_csv(args.reference)
    report=dict(reference_used_by_matcher=False,
                reference_sha256=hashlib.sha256(args.reference.read_bytes()).hexdigest(),
                review_method="源区间中点经人工CSV反算录音位置，计算在建议区间外的距离；未知另计",
                grid_method="整片固定10秒网格；有板书区计算源误差和反算录音等效误差；缺板书区独立统计",
                rows={})
    details={}
    for interval in (60,20):
        before=json.loads((args.before/f"result-{interval}.json").read_text(encoding="utf-8"))
        after=json.loads((args.after/f"result-{interval}.json").read_text(encoding="utf-8"))
        for name,result in (("before",before),("after",after)):
            label=f"{name}_{interval}"
            report["rows"][label],details[label]=evaluate(result,reference,args.step)
            with (args.output/f"{label}-grid.csv").open("w",encoding="utf-8-sig",newline="") as stream:
                writer=csv.DictWriter(stream,fieldnames=list(details[label]["grid"][0]));writer.writeheader()
                writer.writerows(details[label]["grid"])
        # Score exactly the reviewer's original item set, as well as each version's native set.
        fixed=dict(candidates=copy.deepcopy(after["candidates"]),stats={},
                   unmatchable_intervals=copy.deepcopy(after.get("unmatchable_intervals",[])),
                   review_intervals=copy.deepcopy([row for row in before["review_intervals"] if row.get("source_start") is not None]))
        for row in fixed["review_intervals"]:
            row.pop("target_start",None);row.pop("target_end",None)
        repair_reviews(Project.from_dict(after["project"]),fixed,interval)
        metrics,rows=source_reviews(fixed,reference)
        report["rows"][f"after_{interval}"]["same_original_review_set"]=metrics
        details[f"after_{interval}"]["same_original_review_set"]=rows
    (args.output/"comparison.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    (args.output/"comparison-details.json").write_text(json.dumps(details,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
