"""Explicit label/value declarations in a single source text entity.

No proximity joins, cross-entity totals, inferred units or paragraph completion.
These candidates use the business API's paging contract without pretending to
be ruled tables. Every field cites the original, unmodified source entity.
"""
import re
from cad_agent.contracts import stable_id


def candidates(index):
    from cad_agent.analysis.business_tables import ALIASES, METRICS, field_key, parse_value
    labels=sorted({s for k in METRICS for s in ALIASES[k]}-{'总面积','基底','计容','不计容'},key=len,reverse=True)
    # Modifiers stay in the source label: 地上面积 is never promoted to a
    # project total, nor 地下面积 to 不计容. Matching requires an adjacent
    # scalar, so normative verbs and blank templates cannot supply a value.
    area=r'(?:住宅/配套|住宅|配套)?(?:地上|地下室?)(?:总)?建筑面积'
    parking=r'(?:地上|地下室?|地面|室内|室外)?(?:非机动车|机动车|小汽车|装卸车|装卸|汽车|自行车)?(?:停车位|停车泊位|车位)(?:数量|数)?'
    number=r'[+\-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
    unit=r'(?:万平方米|平方米|平米|万㎡|㎡|m[²2]|公顷|亩|%%%|%|％|辆|个|位)'
    pattern=re.compile('('+area+'|'+parking+'|'+'|'.join(map(re.escape,labels))+
        r')\s*(?:[：:]|为[：:]?|共)?\s*((?:约合|约|[≤≥<>≈])?'+number+r'\s*(?:'+unit+r')?)(?![\d~～/]|[.]\d|至\d)')
    result=[]
    for text in index.scene['texts']:
        if not 8<=len(text['text'])<=10000:continue
        fields=[]
        for match in pattern.finditer(text['text']):
            key=field_key([match[1]],METRICS)
            value,qualifier,units=parse_value(match[2].strip(),key)
            if not key or value is None or (not units and key!='floor_area_ratio'):continue
            expected=({'m2','万m2','hm2','亩'} if key.endswith('_area') else
                      {'%'} if key in ('green_ratio','building_density') else
                      {'辆','个','位'} if key=='parking_spaces' else set())
            if not set(units)<=expected:continue
            before=text['text'][max(0,match.start()-24):match.start()]
            after=text['text'][match.end():match.end()+16]
            if re.search(r'不少于|不小于|不大于|不超过|不得|不应|凡',before) or re.match(r'\s*[（(]含[）)]\s*(?:以上|以下)',after):
                continue
            # Retain the surrounding clause as scope, including e.g. a
            # parenthetical phasing note. It is never used to fill a value.
            left=max(text['text'].rfind(s,0,match.start()) for s in ('；',';','。','\n'))+1
            right=min([p for s in ('；',';','。','\n') if (p:=text['text'].find(s,match.end()))>=0] or [len(text['text'])])
            prefix_start=max(text['text'].rfind(s,0,match.start()) for s in ('；',';','。','\n','，',',','（','('))+1
            fields.append(dict(label=match[1],raw_value=match[2].strip(),field=key,
                               source_character_span=[match.start(),match.end()],
                               source_prefix=text['text'][prefix_start:match.start()].strip(),
                               source_clause=text['text'][left:right]))
        if not fields:continue
        evidence=index.entity_evidence(text['entity_id'])
        if not evidence:continue
        result.append(dict(table_id=stable_id('metric-prose',index.snapshot,text['entity_id']),
            source_kind='explicit_metric_paragraph',layout_id=text['layout_id'],bbox=text['bbox'],
            source_text=text['text'],entity_id=text['entity_id'],evidence=evidence,entries=fields))
    return result


def extract(index,table,args,scope):
    from cad_agent.analysis.business_tables import METRICS, VERSION, _value, _completion, _conflicts
    requested=args.requested_fields or METRICS
    entries=table['entries'];fields=[]
    end=min(len(entries),args.row_offset+args.row_limit)
    for i in range(args.row_offset,end):
        entry=entries[i]
        if entry['field'] not in requested:continue
        cell=dict(text=entry['raw_value'],header_path=[],header_evidence=[],evidence=table['evidence'],
                  cell_id=stable_id('prose-field',table['table_id'],str(i)),row_index=i,column_index=0,
                  row_span=1,column_span=1,continuation_of=None,bbox=table['bbox'])
        field=_value(cell,entry['field'],[dict(text=entry['label'],evidence=table['evidence'])])
        field.update(record_id=cell['cell_id'],value_kind='explicit_declaration',
                     qualifiers=[entry['source_prefix'],entry['source_clause']],
                     scope_evidence=table['evidence'],
                     source_character_span=entry['source_character_span'],source_kind=table['source_kind'])
        fields.append(field)
    _conflicts(fields)
    more=end<len(entries)
    return dict(version=VERSION,snapshot_id=index.snapshot,source_sha256=index.drawing['sha256'],
        kind='planning_metrics',mode='extraction',source_kind=table['source_kind'],source_scope=scope,
        table_id=table['table_id'],fields=fields,records=[],summary_rows=[],unresolved_rows=[],diagnostics=[],
        header_selection='explicit_paragraph_labels',row_count=len(entries),row_offset=args.row_offset,
        row_end=end,has_more=more,next_offset=end if more else None,page_state='complete',cache_hit=False,
        table_read_complete=not more and args.row_offset==0,scope_exhaustive=False,
        completeness_scope='returned_page_only',**_completion(fields,requested),
        next_actions=[dict(tool='extract_planning_metrics',arguments=args.model_dump()|{'row_offset':end})] if more else [],
        note='单个原文实体内明确的标签:数值声明；无跨段归属、计算补全或工程身份认定。')
