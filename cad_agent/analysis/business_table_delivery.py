"""Typed ordinal bindings from already read table cells, never numeric guesses."""
import re


def cited_ordinals(observations, evidence, cited_ids):
    cited=set(cited_ids); values=set()
    for observation in observations.values():
        if observation.get('tool')!='extract_building_schedule':continue
        data=observation.get('data',{})
        for row in data.get('records',[]):
            for field in row.get('fields',[]):
                raw=field.get('raw_text','').strip()
                if field.get('field')!='ordinal' or field.get('state')!='observed' or not re.fullmatch(r'\d+',raw):continue
                cells=field.get('evidence',[]);headers=field.get('header_evidence',[])
                refs=[e['evidence_id'] for e in cells+headers]
                if not cells or not headers or not set(refs)<=cited:continue
                if any(eid not in evidence for eid in refs):continue
                if not any(evidence[e['evidence_id']]['quote'].strip()==raw for e in cells):continue
                if not any(re.search('编号|序号',evidence[e['evidence_id']]['quote']) for e in headers):continue
                values.add(raw)
    return values
