"""Compact context correctness and frozen-acceptance guards for the second pilot."""
import copy,json
from scripts.room_boundary_revision import OLD,OUT,compact,validator,build_payload,encoded,MAX_OUTPUT,SLOT,VALIDATOR_SHA,sha

def test_compact_projection_keeps_every_source_and_original_order():
    b=json.loads((OLD/'input.json').read_text());original=copy.deepcopy(b);v,m=compact(b)
    assert b==original and len(m)==len(v['segments'])==584 and len(v['texts'])==len(b['texts'])
    assert len({i['short_id'] for i in m})==len(m) and len({i['segment_id'] for i in m})==len(m)
    for i,(row,s) in enumerate(zip(v['segments'],b['segments'])):
        assert m[i]['segment_id']==s['segment_id'] and m[i]['entity_path']==s['entity_path']
        assert v['layers'][row[2]]==s['visibility']['effective_layer']
        assert row[3]==int(s['visibility']['visible'])+2*int(s['visibility']['supported'])
        for j,key in [(0,'a'),(1,'b')]:
            xi,yi=v['points'][row[j]];p=[v['origin'][0]+v['x'][xi]*v['quantum_mm'],v['origin'][1]+v['y'][yi]*v['quantum_mm']]
            assert max(abs(a-c) for a,c in zip(p,s[key]))<=5e-6+1e-9
    for i,t in enumerate(b['texts']):assert v['texts'][i][0]==t['text']
    assert v['texts'][v['target_text_index']][0]==b['target']['label']

def test_projection_retains_hidden_and_unsupported_states():
    b=json.loads((OLD/'input.json').read_text());b['segments'][0]['visibility'].update(visible=False,supported=False)
    b['texts'][0]['visibility']['visible']=False;v,m=compact(b)
    assert v['segments'][0][3]==0 and v['texts'][0][2]==2

def test_frozen_validator_still_rejects_old_candidate():
    b=json.loads((OLD/'input.json').read_text());raw=json.loads((OLD/'execution-2.json').read_text())['output']
    assert sha(OLD/'validator-v2.py')==VALIDATOR_SHA
    assert validator().validate(b,raw)['errors']==['BOUNDARY_NUMERICAL_BACKTRACK']

def test_initial_and_repair_requests_reserve_other_slots():
    first=build_payload(None);repair=build_payload(dict(code=(OLD/'candidate-2.py').read_text(),errors=['BOUNDARY_NUMERICAL_BACKTRACK']))
    assert first['model']==repair['model']=='gpt-6.1-sol'
    assert first['max_output_tokens']==repair['max_output_tokens']==6000
    for p in [first,repair]:assert len(encoded(p))+MAX_OUTPUT+512<=SLOT
    assert 'segments' in json.loads(first['input'][0]['content'])['bundle_view']
    assert 'segments' not in json.loads(repair['input'][0]['content'])['bundle_view']
    assert 'repair_projection' in json.loads(repair['input'][0]['content'])['bundle_view']
