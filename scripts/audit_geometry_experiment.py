"""Offline source/numerical audit and paired checker replay, never model input.

Native DXF handles and INSERT instance children are read independently with
ezdxf. Other compound-source types remain explicitly unaudited.
This does not provide a gold label for object ownership or opposing faces.
"""
from pathlib import Path
import json,math,sys
import ezdxf
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cad_agent.geometry.workbench_checks import GeometryClaim,check_claim
from cad_agent.analysis.geometry_context import project_geometry_messages


def audit(root):
    doc=ezdxf.readfile(root/'input/drawing.dxf')
    workset=json.loads((root/'geometry-workset.json').read_text())
    bundles={p.stem:json.loads(p.read_text()) for p in (root/'input/bundles').glob('*.json')}
    rows=[];checks=[]
    for mid,result in workset.get('measurements',{}).items():
        if result['status']!='local_pair_measured':continue
        by_id={r['entity_id']:r for r in bundles[result['bundle_id']]['entities']}
        paths=[by_id[e]['entity_path'] for e in result['source_entity_ids']]
        row=dict(measurement_id=mid,paths=paths,reported=result['measurements']['width_drawing_units'])
        def native(path):
            entity=doc.entitydb[path[0]]
            for token in path[1:]:
                if entity.dxftype()!='INSERT' or ':' not in token:return None
                index,kind=token.split(':',1)
                children=list(entity.virtual_entities())
                entity=children[int(index)]
                if entity.dxftype()!=kind:return None
            return entity
        pair=[native(p) for p in paths]
        if any(e is None for e in pair):row['audit']='compound_source_not_covered_by_this_auditor'
        else:
            a,b=pair
            if a.dxftype()==b.dxftype()=='LINE':
                p,q,r=a.dxf.start,a.dxf.end,b.dxf.start
                delta=q-p
                independent=abs(delta.x*(r.y-p.y)-delta.y*(r.x-p.x))/math.hypot(delta.x,delta.y)
                error=abs(independent-row['reported'])
                row.update(audit='native_line_distance',independent_value=independent,absolute_error=error,agrees=error<=max(result.get('error_bound',0),1e-6))
            elif a.dxftype()==b.dxftype()=='ARC':
                independent=abs(a.dxf.radius-b.dxf.radius)
                error=abs(independent-row['reported'])
                row.update(audit='native_radius_difference',independent_value=independent,absolute_error=error,agrees=error<=max(result.get('error_bound',0),1e-6))
            else:row['audit']='native_type_not_covered'
        rows.append(row)
        for kind,value,unit in [('local_distance',row['reported'],'drawing_unit'),('local_distance',row['reported']+7.123,'drawing_unit'),
                ('local_distance',row['reported'],'mm'),('whole_route_minimum',row['reported'],'drawing_unit'),('finished_width',row['reported'],'drawing_unit')]:
            claim=GeometryClaim(claim_id=str(len(checks)),reference_id=mid,kind=kind,value=value,unit=unit)
            checked=check_claim(claim,result)
            checks.append(dict(claim=claim.model_dump(),checks=checked,
                shadow_disposition='supported_local' if checked['supported'] else 'flagged_candidate',
                enforce_disposition='supported_local' if checked['supported'] else 'blocked_claim'))
    projections=[]
    for path in sorted((root/'logs').glob('request-*.json')):
        request=json.loads(path.read_text());messages=request['messages']
        projected,stats=project_geometry_messages(messages)
        compact=lambda obj:len(json.dumps(obj,ensure_ascii=False,separators=(',',':')))
        projections.append(dict(request=path.name,before_chars=compact(messages),after_chars=compact(projected),**stats))
    report=dict(numeric_audit=rows,paired_check_replay=checks,context_replay=projections,
        limitations=['Numerical agreement does not establish object, opposite faces, unit, obstacle census or whole route.',
            'Checker replay holds the same recorded results fixed; it is not an end-to-end validation-mode A/B run.',
            'Context replay reports characters only. Actual API token/caching totals are separate.'])
    (root/'independent-audit-not-model-input.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(dict(stage=root.name,numerical_results=len(rows),agree=sum(r.get('agrees',False) for r in rows),
        checker_cases=len(checks),supported=sum(c['checks']['supported'] for c in checks)),ensure_ascii=False))


if __name__=='__main__':audit(Path(sys.argv[1]))
