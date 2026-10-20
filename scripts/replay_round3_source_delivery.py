"""Offline Q04/Q10 replay. Frozen inputs are immutable; no model or store writes."""
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path

from cad_agent.analysis.assembly import read
from cad_agent.analysis.assembly_delivery import ownership_issues
from cad_agent.analysis.delivery import AnswerValidationError
from cad_agent.analysis.efficiency import dumps
from cad_agent.analysis.fact_tools import ReadAssembly
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.tool_registry import Claim
from cad_agent.contracts import SourceRegion
from scripts.replay_b03_answers import ReplaySession

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT/'artifacts/designer-facts-unlimited-final-20261020'
OUTPUT = ROOT/'artifacts/bottleneck-round3-20261020'


class FrozenIndex(DrawingIndex):
    def __init__(self, snapshot):
        folder = FROZEN/'inputs'/snapshot
        self.snapshot = snapshot
        self.regions = [SourceRegion.model_validate(r) for r in json.loads((folder/'regions.json').read_text())]
        self.by_id = {r.region_id:r for r in self.regions}
        self._scene = json.loads((folder/'scene.json').read_text())
        self.drawing = self._scene
        self._tables = None
        self.entity_sources = defaultdict(list)
        for r in self.regions:
            for part in r.parts:
                self.entity_sources[part.entity_id].append((r,part))

    @property
    def scene(self):
        return self._scene


def main():
    evaluated = json.loads((FROZEN/'evaluated-results.json').read_text())
    records = []
    hashes = {}
    for case, rid, forbidden, expected in [
        ('Q04-B-luna-high','region_06a8f3c3d653564fea8853e7','以下做法同楼地面9第3~9条','2、最薄处20厚C25'),
        ('Q10-B-luna-high','region_64b01541bc9087a6b5c4a5e3','7、外墙仿石涂料','7、外墙反射隔热涂料层'),
    ]:
        selected = next(row for row in evaluated if row['id']==case)
        folder = Path(selected['evidence_path'])
        for filename in ('run.json','task.json'):
            path=folder/filename
            hashes[str(path.relative_to(ROOT))]=sha256(path.read_bytes()).hexdigest()
        run=json.loads((folder/'run.json').read_text())
        task=json.loads((folder/'task.json').read_text())
        session=ReplaySession(run,task)
        claims=[Claim.model_validate({k:v for k,v in c.items() if k in Claim.model_fields})
                for c in run['result']['claims']]
        rejected=[i for i,c in enumerate(claims) if ownership_issues(c,task['evidence'])]
        assert 3 in rejected
        payload=dumps(dict(claims=[c.model_dump() for c in claims],
                           requirement_outcomes=run['result']['requirement_outcomes']))
        try:
            session.validate_answer(payload)
            raise AssertionError('Historical source attribution should fail')
        except AnswerValidationError as error:
            session.record_validation_issue(error)
            checked=error.validated_claims
        recovered=session.recover_valid_claims(payload)
        assert recovered and claims[0].text in recovered['answer']
        assert claims[3].text not in [c['text'] for c in recovered['claims']]
        assert all(o['status']!='fulfilled' for o in recovered['requirement_outcomes'])
        index=FrozenIndex(run['snapshot_id'])
        for filename in ('regions.json','scene.json'):
            path=FROZEN/'inputs'/run['snapshot_id']/filename
            hashes[str(path.relative_to(ROOT))]=sha256(path.read_bytes()).hexdigest()
        args=ReadAssembly(region_id=rid,limit=1)
        pages=[]; exact=0
        while True:
            result=read(index,args); pages.append(result)
            for item in result['items']:
                e=item['evidence']
                assert index.by_id[e['region_id']].text[slice(*e['text_span'])]==e['quote']
                exact+=1
            if not result['has_more']:
                break
            args=args.model_copy(update={'offset':result['next_offset']})
        text=''.join(i['evidence']['quote'] for p in pages for i in p['items'])
        assert forbidden not in text and expected in text
        records.append(dict(case=case,selected_evidence_path=str(folder.relative_to(ROOT)),
            historical_bad_claim=claims[3].model_dump(),boundary_rejected_claim_indices=rejected,
            independent_validated_claims=len(checked),recovered_fact_count=recovered['recovery']['kept_claims'],
            independent_first_fact_retained=True,unknown_not_promoted=True,
            source_replay=dict(region_id=rid,page_count=len(pages),exact_source_spans_verified=exact,
                               title=pages[0]['title_evidence'],binding=pages[0]['source_binding'],
                               correct_source_text=text,forbidden_prefix_absent=True),
            validation_scope='Frozen historical clauses, current local validators, and exact source re-read; not a new model run'))
    paths=['cad_agent/analysis/'+name+'.py' for name in
           ('assembly','assembly_delivery','delivery','delivery_context','answer_protocol','spatial_delivery')]
    paths += ['tests/test_round3_source_delivery.py','scripts/replay_round3_source_delivery.py']
    changes=[]
    for name in paths:
        path=ROOT/name; before=OUTPUT/'before'/name
        changes.append(dict(path=name,sha256=sha256(path.read_bytes()).hexdigest(),
                            before_sha256=sha256(before.read_bytes()).hexdigest() if before.exists() else None))
    report=dict(bottleneck=5,kind='offline_frozen_source_and_delivery_replay',model_calls=0,
        frozen_sources=hashes,changes=changes,cases=records,
        implementation=[
            'Reject pre-heading source fragments affirmatively attributed to the following construction, including smaller citations inside already-read spans.',
            'Clip explicit construction sections at layout/frame/column changes; expose excluded prefix evidence while retaining sequence/end diagnostics.',
            'Offer executable source-bound field claim templates with subject/property evidence and separately labelled candidate requirement IDs; never auto fulfil requirements.',
            'Preserve independent positive fields sharing a source paragraph; property-specific repair cannot retract another property sharing only the subject.',
            'Apply generic spatial identity/numeric gates to spatial object and spatial_fact_scope results without a tool-name allowlist.'],
        tests_log='artifacts/bottleneck-round3-20261020/source-delivery-tests.log',
        tests_output=(OUTPUT/'source-delivery-tests.log').read_text() if (OUTPUT/'source-delivery-tests.log').exists() else None,
        remaining_semantic_gaps=[
            'The lexical boundary guard is not a complete semantic attribution verifier; paraphrases without exact layer text or quoted witnesses can remain unrecognized.',
            'Q04 18B retains LAYER_NUMBER_SEQUENCE_UNRESOLVED; Q10 1B retains SECTION_END_UNRESOLVED. Correct extracted text is not complete source ownership or engineering acceptance.',
            'Structured fields are available only for source_bound assembly records, are limited to already-read pages, and must still pass normal numeric/unit/requirement validation.',
            'Templates do not infer instance applicability, inheritance, omitted units, external references, scope census or full question completion.',
            'Spatial delivery gates do not prove boundary semantics or dimensions; their identity and measurement producers remain responsible for source geometry correctness.',
            'No model rerun was performed; reduction in the historical 61 finish errors or overall semantic success rate is not claimed.'])
    for name, digest in hashes.items():
        assert sha256((ROOT/name).read_bytes()).hexdigest()==digest
    OUTPUT.mkdir(parents=True,exist_ok=True)
    (OUTPUT/'source-delivery-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(report=str(OUTPUT/'source-delivery-report.json'),cases=len(records),
                          historical_wrong_attributions_rejected=2,source_files_unchanged=len(hashes)),ensure_ascii=False))


if __name__=='__main__':
    main()
