"""Replay immutable real sources against a new frozen implementation, then SIWC.

Does not reconvert, crop, relabel or edit the drawing. Credentials stay in the
existing account directory. Existing pre-fix artifacts are never overwritten.
"""
import argparse
import json
from pathlib import Path
import shutil

import validate_business_tables_real as harness


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--sample',choices=['primary','schedule','hospital'],required=True)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    suffix='' if args.sample=='primary' else '-'+args.sample
    old=harness.OUT.with_name(harness.OUT.name+suffix)
    harness.OUT=harness.ROOT/('artifacts/business-tables-fixed-final-v4-20261026'+suffix)
    w=harness.setup()
    try:
        source=json.loads((old/'input.json').read_text());sid=source['drawing']['id']
        target=w.store.artifact_dir(sid)
        if not (target/'source.dxf').exists():
            shutil.copytree(old/'data/drawings'/sid,target,dirs_exist_ok=True)
        assert harness.sha(target/'source.dxf')==source['dxf_sha256']
        w.store.put('drawing',sid,source['drawing'])
        harness.save('input.json',source)
        harness.save('replay-provenance.json',{'baseline_folder':str(old),'source_sha256_verified':True,
            'native_source_reused':True,'drawing_modified':False})
        from cad_agent.analysis.general import DrawingIndex
        from cad_agent.analysis.business_tables import extract_planning_metrics,extract_building_schedule
        from cad_agent.analysis.business_table_contracts import PlanningMetricsRequest,BuildingScheduleRequest
        from cad_agent.analysis.source_record_store import verify_sources
        method,schema,kind=(extract_building_schedule,BuildingScheduleRequest,'building_schedule') if args.sample=='schedule' else (
            extract_planning_metrics,PlanningMetricsRequest,'planning_metrics')
        index=DrawingIndex(w,sid)
        discovery=method(w,sid,schema(limit=12),index)
        harness.save(kind+'/discovery.json',discovery)
        collected=[]
        for candidate in discovery['candidates']:
            request=schema(table_id=candidate['table_id']);pages=[]
            while True:
                result=method(w,sid,request,index);verify_sources(result,index);pages.append(result)
                if not result.get('has_more'):break
                request=schema(**result['next_actions'][0]['arguments'])
            collected.extend(pages)
            harness.save(kind+'/'+candidate['table_id']+'.json',pages)
        if args.sample=='primary':
            reference=json.loads((old/'reference.json').read_text())
            fields=[f for p in collected for f in p['fields']]
            comparisons=[]
            for expected in reference['fields']:
                found=[f for f in fields if f['field']==expected['field'] and f['value']==expected['value'] and f['state']=='observed']
                expected_unit={'平方米':'m2','':'dimensionless'}.get(expected['unit'],expected['unit'])
                assert any((f['unit'] or f['unit_state'])==expected_unit for f in found),expected
                comparisons.append(dict(field=expected['field'],value=expected['value'],passed=True))
            for label,value in reference['parking'].items():
                assert any(label in f['source_labels'] and f['value']==value and f['state']=='observed' for f in fields),label
            assert not any(f['field']=='green_area' for f in fields)
            harness.save('acceptance.json',dict(passed=True,categories=len(comparisons),
                parking_categories=len(reference['parking']),comparisons=comparisons,
                field_states={f['field']+':'+','.join(f['source_labels']):f['state'] for f in fields},
                source_absent=['green_area'],source_name_ambiguous=['footprint_area']))
        elif args.sample=='schedule':
            reference=json.loads((old/'reference.json').read_text());rows=[r for p in collected for r in p['records']]
            assert len(rows)==len(reference['rows'])==6
            comparisons=[]
            for expected,row in zip(reference['rows'],rows):
                fields={f['field']:f for f in row['fields']}
                for key,value in expected['values'].items():
                    actual=fields[key]
                    # F is an explicit storey suffix; pair notation remains raw.
                    normal=value[:-1] if key=='storeys' and value and value[:-1].isdigit() and value.endswith('F') else value
                    assert actual['value']==normal,(key,value,actual['value'])
                    assert actual['state']==('missing' if value is None else 'observed'),actual
                    if value is not None:
                        comparisons.append(dict(ordinal=expected['values']['ordinal'],field=key,value=normal,passed=True))
                assert not set(reference['source_absent_columns']) & fields.keys()
            summaries=[r for p in collected for r in p['summary_rows']]
            assert len(summaries)==1
            assert {f['field']:f['value'] for f in summaries[0]['fields']}['total_building_area']=='188875.69'
            harness.save('acceptance.json',dict(passed=True,rows=len(rows),explicit_target_cells=len(comparisons),
                summary_rows=1,comparisons=comparisons,source_absent_columns=reference['source_absent_columns']))
        else:
            # Held-out checks transcribed from the previously reviewed native
            # hospital/corridor/green-space crops, not from this parser output.
            grouped={}
            for page in collected:grouped.setdefault(page['table_id'],[]).extend(page.get('fields',[]))
            expected=[('63896.11',{'land_area':'11479','floor_area_ratio':'3.93','footprint_area':'4774.31',
                'building_density':'41.59','green_ratio':'20'},['259','1356']),
                ('9680.63',{'land_area':'8937','footprint_area':'158.74','building_density':'2.51'},['285','0']),
                ('3194.19',{'footprint_area':'1004.34'},['0','0'])]
            checked=0
            for total,values,parking in expected:
                matches=[fields for fields in grouped.values() if any(f['field']=='total_building_area' and
                         f['value']==total and f['state']=='observed' for f in fields)]
                assert len(matches)==1,(total,len(matches))
                fields=matches[0]
                for field,value in values.items():
                    assert any(f['field']==field and f['value']==value and f['state']=='observed' for f in fields)
                assert [f['value'] for f in fields if f['field']=='parking_spaces']==parking
                assert not any(f['field'] in ('counted_area','excluded_area') for f in fields)
                checked+=1+len(values)+len(parking)
            harness.save('acceptance.json',dict(passed=True,scope='three_manually_reviewed_table_ranges',
                tables=3,checked_present_values=checked,
                unresolved_candidates=sum('HEADER_UNRESOLVED' in p.get('diagnostics',[]) for p in collected),
                limitation='One additional polluted table remains unresolved; not a whole-drawing completeness pass.'))
        print('EXTRACTED' if args.sample=='hospital' else 'ACCEPTED',args.sample,len(collected),'pages',flush=True)
        if args.live:harness.live(w,kind)
    finally:
        w.close()


if __name__=='__main__':main()
