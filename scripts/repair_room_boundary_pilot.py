"""Third call: bounded generic cleanup patch, no additional drawing content sent."""
import json,time,hashlib
from types import SimpleNamespace
from scripts.room_boundary_pilot import *
from scripts.room_boundary_pilot_validation import validate,VERSION
from cad_agent.extensions.adaptation.contracts import Proposal
from cad_agent.extensions.adaptation.model import check_code
from cad_agent.extensions.adaptation.sandbox import execute
from cad_agent.adapters.siwc import SiwcAccount
from cad_agent.runtimes.responses_subscription import ResponsesTransport

def main():
    state=read('run.json');assert state['model_calls']==2
    used=sum(a['usage']['total_tokens'] for a in state['attempts']);remaining=state['max_total_tokens']-used
    instruction='Return JSON {"strategy":"code","explanation":str,"code":str}. Define adapt(bundle), call _prior_adapt(bundle) (already defined). It returns status=candidate, vertices (unclosed ring), edge_segment_ids, excluded_crossing_ids, area,input_id. Fix near-zero-width collinear backtracking spikes (60 mm excursion, <1e-5 mm width). Iteratively remove redundant collinear vertices, including reversals. Rebuild each edge_segment_ids from bundle.segments: a,b,segment_id,visibility={visible,supported,effective_layer}. Allowed layers: bundle.policy.wall_layers+opening_layers. Match collinearity within 1e-5 and positive projected overlap, never add geometry. Recompute local-origin area, preserve other fields. Pure stdlib, no files/network/hardcoded geometry. Return unchanged unresolved result. Independent validation rejects unsupported edges/incomplete face.'
    max_output=1100;assert len(instruction.encode())+max_output+100<=remaining,(len(instruction),remaining)
    w,r=workspace()
    try:
        settings=w.model_catalog['siwc_sol'];assert settings.model=='gpt-6.1-sol' and settings.provider=='responses_subscription'
        w.siwc_account.close();w.siwc_account=SiwcAccount(ROOT/'artifacts/hybrid-retrieval-20261015/data')
        with w.siwc_account.lease():pass
        b=read('input.json');dump(OUT/'validation-2-v2.json',validate(b,read('execution-2.json')['output']))
        payload=dict(model=settings.model,instructions=instruction,input=[dict(role='user',content='Generate the cleanup wrapper.')],tools=[],tool_choice='none',store=False,stream=True,reasoning={'effort':'low'},max_output_tokens=max_output)
        dump(OUT/'prompt-3.json',payload)
        entry=dict(number=3,status='requesting',prompt_sha256=digest(payload),max_output_tokens=max_output,remaining_total_tokens_before=remaining,
          prompt_scope='generic cleanup contract only; no new drawing contents',origin='llm_cleanup_composed_with_unchanged_candidate_2',validator=VERSION,
          validator_sha256=sha(Path(__file__).with_name('room_boundary_pilot_validation.py')))
        state['attempts'].append(entry);state['model_calls']=3;dump(OUT/'run.json',state)
        deadline=time.monotonic()+100
        session=SimpleNamespace(w=SimpleNamespace(settings=settings),usage=[],trace=[],runtime_diagnostics={'model_requests':[{}]},_remaining=lambda:max(.1,deadline-time.monotonic()),progress=lambda **kw:None)
        try:
            with w.siwc_account.lease():response=ResponsesTransport(w.siwc_account).request(session,payload)
            content=''.join(p['text'] for i in response['output'] if i.get('type')=='message' for p in i['content'] if p.get('type')=='output_text')
            usage={}
            for v in session.usage:
                for k,n in v.items():
                    if type(n) in (int,float):usage[k]=usage.get(k,0)+n
            entry.update(usage=usage,transport_diagnostics=session.runtime_diagnostics);dump(OUT/'response-3.json',dict(content=content))
            if content.startswith('```'):content=content.split('\n',1)[1].rsplit('```',1)[0]
            proposal=Proposal.model_validate_json(content,strict=True);dump(OUT/'proposal-3.json',proposal.model_dump());assert proposal.strategy=='code';check_code(proposal.code)
            code=(OUT/'candidate-2.py').read_text()+'\n\n_prior_adapt = adapt\n\n'+proposal.code
            check_code(code);(OUT/'candidate-3.py').write_text(code);entry.update(code_path='candidate-3.py',code_sha256=hashlib.sha256(code.encode()).hexdigest())
            ex=execute(OUT/'data',b,code,timeout=20);dump(OUT/'execution-3.json',ex);v=validate(b,ex['output']);dump(OUT/'validation-3.json',v);entry.update(status='completed',validation=v)
            print('ATTEMPT 3',v['validation_status'],v['errors'],v.get('checks',{}).get('observed_envelope_area_m2'),flush=True)
        except Exception as exc:
            usage={}
            for v in session.usage:
                for k,n in v.items():
                    if type(n) in (int,float):usage[k]=usage.get(k,0)+n
            entry.update(status='failed',usage=usage,transport_diagnostics=session.runtime_diagnostics,error=getattr(exc,'code',type(exc).__name__),message=str(exc)[:200]);print(entry['error'],entry['message'],flush=True)
        dump(OUT/'run.json',state)
    finally:w.close()
if __name__=='__main__':main()
