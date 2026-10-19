"""Rerun historical immutable adapters in the sandbox, with no model calls."""
import json
import sqlite3
from pathlib import Path
from cad_agent.config import ROOT
from cad_agent.extensions.adaptation.sandbox import execute
from cad_agent.extensions.adaptation.validation import validate
from cad_agent.extensions.table_recovery.bundle import digest,verify_bundle
from scripts.regress_adaptation import dump,sha
from scripts.validate_adaptation_multimethod import OUT


def main():
    old=ROOT/'artifacts/dynamic-adaptation-20261016';near=ROOT/'artifacts/dynamic-shadow-near-axis-20261016'
    files=[(near,p) for p in sorted(near.glob('*-table_grid-replay.json'))]
    files += [(old,old/name) for name in ('11816-27-text_spans-dynamic.json','11816-27-segment_measurements-dynamic.json','11816-28-closed_contour-dynamic.json')]
    rows=[]
    for folder,path in files:
        r=json.loads(path.read_text())['run'];con=sqlite3.connect(f'file:{folder}/data/index.sqlite3?mode=ro',uri=True)
        def artifact(kind,key):
            v=json.loads(con.execute('select data from records where kind=? and id=?',('adaptation_'+kind,key)).fetchone()[0])
            assert key=='ad_'+kind+'_'+digest(v);return v
        bundle=artifact('input',r['input_artifact_id']);verify_bundle(bundle)
        source=folder/'data/drawings'/r['snapshot_id']/'source.dxf';assert sha(source)==bundle['source_sha256']
        for name,h in bundle['parsed_artifact_sha256'].items():assert sha(source.parent/name)==h
        row=dict(case=path.stem,method=r['request']['method'],source_sha256=bundle['source_sha256'],input_id=bundle['input_id'],model_calls=0)
        if not r['attempts']:
            assert bundle['coverage']['blocking_diagnostics'];row.update(status='input_refusal_preserved',unchanged=True)
        else:
            a=r['attempts'][r.get('accepted_attempt',len(r['attempts']))-1]
            code=artifact('code',a['code_artifact_id']);oldexecution=artifact('execution',a['execution_id']);oldvalidation=artifact('validation',a['validation_id'])
            execution=execute(OUT/'data',bundle,code['code']);report=validate(r['request']['method'],bundle,execution['output'])
            legacy_metadata=(r['request']['method']=='segment_measurements' and all('basis' not in i for i in oldvalidation['items']))
            normalized=dict(report,items=[{k:v for k,v in i.items() if k!='basis'} for i in report['items']]) if legacy_metadata else report
            row.update(code_sha256=code['code_sha256'],execution=execution,validation=report,
                status=report['validation_status'],raw_output_equal=execution['output']==oldexecution['output'],
                validation_equal=report==oldvalidation,preexisting_metadata_added=['items[].basis'] if legacy_metadata else [],
                unchanged=execution['output']==oldexecution['output'] and normalized==oldvalidation)
            assert row['unchanged'],path.name
        assert sha(source)==bundle['source_sha256'];rows.append(row);con.close()
        dump(OUT/'historical-regression.json',dict(model_calls=0,cases=rows,unchanged_count=sum(r['unchanged'] for r in rows)))
        print(row['case'],row['status'],'unchanged',flush=True)
    dump(OUT/'historical-regression.json',dict(model_calls=0,cases=rows,unchanged_count=sum(r['unchanged'] for r in rows)))

if __name__=='__main__':main()
