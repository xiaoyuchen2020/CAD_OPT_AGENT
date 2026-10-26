"""Server compiled original-source delivery with a session-owned aggregate permit."""
import json
import re
from cad_agent.analysis.source_record_store import digest
from cad_agent.analysis.source_records import RecordError
from cad_agent.analysis.tool_registry import FinalAnswer, Claim
from pydantic import Field


class SourceRecordAnswer(FinalAnswer):
    # Only aggregate size changes. Every individual Claim keeps the original
    # length, provenance, number, unit, calculation and ownership validators.
    claims: list[Claim] = Field(min_length=1,max_length=256)
    missing_information: list[str] = Field(default_factory=list,max_length=128)


def permitted(session,content):
    try:
        return getattr(session,'_source_record_delivery_digest',None)==digest(json.loads(content))
    except (ValueError,TypeError):
        return False


def expected(record):
    return 'exclude' if not record['selected_parts'] else 'deliver' if record['ready'] else 'uncertain'


def decisions(records,content,aliases,evidence,accepted=None):
    """Field-local checks retain valid choices, require all records, forbid omissions."""
    accepted=dict(accepted or {})
    errors=[]
    try:
        raw=json.loads(content)
    except (ValueError,TypeError):
        return accepted,[dict(path='decisions',message='必须返回有效 JSON。')]
    if not isinstance(raw,dict) or set(raw)!={'decisions'} or not isinstance(raw['decisions'],list):
        return accepted,[dict(path='decisions',message='只允许 decisions 数组。')]
    lookup={r['key']:r for r in records}; seen=set()
    reverse={v:k for k,v in aliases.items()}
    for i,item in enumerate(raw['decisions']):
        path=f'decisions.{i}'
        if not isinstance(item,dict) or not {'record','action'}<=item.keys() or set(item)-{'record','action','notes'}:
            errors.append(dict(path=path,message='仅允许 record、action 和可选 notes。'));continue
        key=item['record']
        if not isinstance(key,str) or key not in lookup or key in seen:
            errors.append(dict(path=path,message='未知或重复记录。'));continue
        seen.add(key);record=lookup[key]
        if item['action']!=expected(record):
            errors.append(dict(path=path+'.action',record=key,message='选择必须遵守已核查的字面条件和来源状态。',expected=expected(record)));continue
        notes=item.get('notes',[])
        valid_notes=[]
        if not isinstance(notes,list) or len(notes)>4:
            errors.append(dict(path=path+'.notes',record=key,message='解释最多四条引用。'));notes=[]
        for j,note in enumerate(notes):
            if (not isinstance(note,dict) or set(note)!={'ref','quote'} or not isinstance(note['quote'],str)
                    or not isinstance(note['ref'],str) or reverse.get(note['ref']) not in record['source_ids']
                    or not note['quote'] or len(note['quote'])>500
                    or note['quote'] not in evidence[reverse[note['ref']]]['quote']):
                errors.append(dict(path=path+f'.notes.{j}',record=key,message='解释必须是本记录的逐字引文；不得改写材料或数值。'))
            else:
                valid_notes.append(dict(evidence_id=reverse[note['ref']],quote=note['quote']))
        # A rejected explanation cannot erase the validated original-source choice.
        accepted[key]=dict(record=key,action=item['action'],notes=valid_notes)
    for key in lookup.keys()-accepted.keys():
        errors.append(dict(path='decisions',record=key,message='缺少该记录的决定。'))
    return accepted,errors


def _chunks(record, evidence):
    title_id = record['title_id']; title = evidence[title_id]['quote']
    prefix = title+'\n' if len(title)<500 else ''
    buffer=prefix; ids=[title_id]
    spans=[dict(claim_span=[0,len(title)], evidence_id=title_id, source_span=[0,len(title)])] if prefix else []

    def chunk():
        text=buffer.rstrip()
        clipped=[]
        for span in spans:
            lo, hi=span['claim_span']; end=min(hi,len(text))
            if end>lo:
                clipped.append(span | dict(claim_span=[lo,end], source_span=[span['source_span'][0],span['source_span'][0]+end-lo]))
        return text,list(ids),clipped

    for eid in record['body_ids']:
        text=evidence[eid]['quote']; pos=0
        if prefix and text.startswith(prefix):
            pos=len(prefix); text=text[pos:]
        if eid==title_id and prefix:continue
        for line in text.splitlines(keepends=True) or [text]:
            while line:
                available=1490-len(buffer)
                if available<=0 or (eid not in ids and len(ids)>=20):
                    yield chunk()
                    buffer=prefix;ids=[title_id]
                    spans=[dict(claim_span=[0,len(title)],evidence_id=title_id,source_span=[0,len(title)])] if prefix else []
                    available=1490-len(buffer)
                take=min(len(line),available)
                if take<len(line):
                    while take>0 and re.match(r'[A-Za-z0-9.−\-]',line[take-1]) and re.match(r'[A-Za-z0-9.−\-]',line[take]):take-=1
                    if take==0:
                        if len(buffer)>len(prefix):
                            yield chunk()
                            buffer=prefix;ids=[title_id]
                            spans=[dict(claim_span=[0,len(title)],evidence_id=title_id,source_span=[0,len(title)])] if prefix else []
                            continue
                        raise RecordError('An indivisible source token exceeds claim limits.')
                spans.append(dict(claim_span=[len(buffer),len(buffer)+take],evidence_id=eid,source_span=[pos,pos+take]))
                buffer+=line[:take];line=line[take:];pos+=take
                if eid not in ids:ids.append(eid)
            if not buffer.endswith('\n'):buffer+='\n'
    if buffer.strip():yield chunk()


def literal_claim(session, claim, index):
    """Verify every non-whitespace character against compiler-owned source spans.

    Used only with the aggregate digest permit. It validates transcription, not
    arithmetic: ordinal punctuation and local code digits keep their source role.
    """
    mapping=getattr(session,'_source_record_claim_sources',{}).get(index)
    if not mapping or mapping['text']!=claim.text or mapping['ids']!=claim.evidence_ids or claim.evidence_basis!='source':return False
    end=0
    for span in mapping['spans']:
        lo,hi=span['claim_span']; a,b=span['source_span'];eid=span['evidence_id']
        if eid not in claim.evidence_ids or lo<end or claim.text[end:lo].strip():return False
        quote=session.evidence.get(eid,{}).get('quote','')
        if not 0<=a<b<=len(quote) or quote[a:b]!=claim.text[lo:hi]:return False
        end=hi
    return bool(mapping['spans']) and not claim.text[end:].strip()


def compile_answer(session,packet,choices):
    if set(choices)!={r['key'] for r in packet['records']}:
        raise RecordError('All records need decisions before publication.')
    claims,groups,excluded=[],[],[]
    session._source_record_claim_sources={}
    gaps=[packet['scope']+'。未明示的单位保留原文，不补齐。']
    by_key={r['key']:r for r in packet['records']}
    for key in choices:
        record=by_key[key]
        choice=choices[key]
        if choice['action']!=expected(record):
            raise RecordError('Selection state changed before compilation.')
        if choice['action']=='exclude':
            excluded.append(dict(record=record['key'],title=record.get('display_title') or session.evidence[record['title_id']]['quote'],
                source_scope=record['source_scope'],reason='不满足本轮记录身份或明确字面筛选条件',source_ids=record['source_ids']))
            continue
        indexes=[]
        for text,ids,spans in _chunks(record,session.evidence):
            session._source_record_claim_sources[len(claims)]=dict(text=text,ids=ids,spans=spans)
            indexes.append(len(claims))
            claims.append(dict(text=text,state='observed' if record['ready'] else 'unknown',
                evidence_ids=ids,question_part_ids=record['selected_parts']))
        if not record['ready']:
            gaps.append(session.evidence[record['title_id']]['quote']+'：候选原文的归属或结束边界未确认；不能作为完整做法。')
        if record['reference_state']=='unresolved':
            gaps.append(session.evidence[record['title_id']]['quote']+'：显式外部引用仍未解决；本记录原文已保留，未自动叠加引用层次。')
        groups.append(dict(record=record['key'],canonical_record=record['record_id'],claim_indexes=indexes,
            source_scope=record['source_scope'],snapshot_id=record['snapshot_id'],
            record_type=record.get('record_type','layer_assembly'),
            binding_basis=record.get('binding_basis'),binding_warnings=record.get('binding_warnings',[]),
            source_segments={str(i):session._source_record_claim_sources[i]['spans'] for i in indexes},
            title=record.get('display_title') or session.evidence[record['title_id']]['quote'],
            states={k:record[k] for k in ('page_state','binding_state','reference_state','engineering_applicability_state')},
            notes=choice.get('notes',[]),reference_targets=record['reference_targets'],
            reference_only=record['reference_only'],diagnostics=record['diagnostics'],next_actions=record['next_actions']+[dict(reason='显式引用尚待核查',next_action=r['repair_action']) for r in record.get('references',[]) if r.get('repair_action')]))
    addressed={q for c in claims for q in c['question_part_ids']}
    for part in packet['query']['parts']:
        if part['id'] not in addressed:
            claims.append(dict(text='在本轮已读取的来源记录中，没有满足该项明确字面条件的完整做法；未据此断言整图不存在。',
                state='unknown',evidence_ids=[],question_part_ids=[part['id']]))
    if packet['unresolved_hits']:
        gaps.append('尚有未归入完整来源记录的字面命中，需要按保留位置继续核查。')
    draft=dict(claims=claims,missing_information=gaps)
    SourceRecordAnswer.model_validate(draft)
    session._source_record_delivery_digest=digest(draft)
    return draft,groups,excluded
