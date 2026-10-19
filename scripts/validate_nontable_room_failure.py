"""Revisit an independently rejected real room, without claiming contour area is net area."""
import json
from cad_agent.analysis.general import DrawingIndex
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.contracts import RoomMeasure
from cad_agent.extensions.adaptation.contracts import Start
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.table_recovery.bundle import digest
from scripts.validate_nontable_real import ROOT,OUT,PRIOR,workspace,dump

def main():
    w=workspace()
    try:
        scan=json.loads((OUT/'11816-scan.json').read_text());idx=DrawingIndex(w,scan['snapshot_id'])
        old=json.loads((ROOT/'artifacts/ar030-real-room-validation-20261015/after/3552_01_1.json').read_text())
        audit=next(r for r in json.loads((ROOT/'artifacts/ar030-real-room-validation-20261015/room-audit.json').read_text()) if r['key']=='3552_01_1')
        request=dict(old['record']['request'],layout_id=idx.drawing['layouts'][0]['layout_id'])
        args=RoomMeasure(**request);before=GeometryService(w,idx.snapshot).room(args)
        x,y=request['seed'];bounds=[x-15000,y-15000,x+15000,y+15000]
        origin=json.loads((PRIOR/'closed_contour-generated.json').read_text())['run']['run_id']
        service=AdaptationService(w,idx.snapshot);r=service.start(Start(method='closed_contour',layout_id=args.layout_id,bbox=bounds,mode='replay',reuse_run_id=origin,model_profile='siwc_sol',idempotency_key='real-room-3552-01-1'),background=False)
        after=GeometryService(w,idx.snapshot).room(args);assert digest(before)==digest(after)
        result=dict(case=dict(case_id='11816-room-3552_01_1',source=scan['source'],snapshot_id=idx.snapshot,layout_id=args.layout_id,region_bbox=bounds,classification='historically_rejected_room'),method='closed_contour',
            baseline=before,baseline_before_sha256=digest(before),baseline_after_sha256=digest(after),baseline_unchanged=True,run=r,model_calls=r['model_calls'],historical_visual_review=audit['review'],
            assessment='room_semantic_gap_not_fixed_by_simple_contour',engineering_semantic_acceptance=False)
        if r.get('accepted_attempt'):result['result']=service.result(r['run_id'])
        if r.get('input_artifact_id'):result['input']=service.artifact('input',r['input_artifact_id'])
        dump(OUT/'11816-room-3552_01_1-closed_contour.json',result)
        print(before['status'],before['diagnostics'],r['status'],r.get('error'),flush=True)
    finally:w.close()
if __name__=='__main__':main()
