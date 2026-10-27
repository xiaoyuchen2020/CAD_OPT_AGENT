"""Independent host replay. No imports of generated modules or program SDK."""
import copy
import re
import unicodedata
from decimal import Decimal, InvalidOperation, localcontext
from .contracts import Pipeline
from ..table_recovery.bundle import digest, encoded

VERSION='program-contract-validator@1'
MAX_ROWS=1000


def _refs(rows):
    refs=[]
    for row in rows:
        for ref in row['refs']:
            if ref not in refs:refs.append(copy.deepcopy(ref))
    return refs


def expected(bundle, contract):
    contract=Pipeline.model_validate(contract)
    nodes={}
    with localcontext() as ctx:
        ctx.prec=48
        for node in contract.nodes:
            inputs=[nodes[k] for k in node.inputs]
            rows=copy.deepcopy(inputs[0]) if inputs else []
            op=node.op
            if op=='source':
                if node.source>=len(bundle['sources']):raise ValueError('PROGRAM_SOURCE_MISSING')
                rows=[dict(value=t['text'],unit='',refs=[dict(source=node.source,entity_id=t['entity_id'],start=0,end=len(t['text']))])
                      for t in bundle['sources'][node.source]['texts'] if t['text']]
            elif op=='select':rows=[r for r in rows if any(k in r['value'] for k in node.keywords)]
            elif op=='field':
                output=[]
                for r in rows:
                    if len(r['refs'])!=1 or r.get('transformed'):raise ValueError('FIELD_REQUIRES_SOURCE')
                    value=r['value'];a=value.find(node.prefix) if node.prefix else 0
                    if a<0:continue
                    a+=len(node.prefix);b=value.find(node.suffix,a) if node.suffix else len(value)
                    if b<=a:continue
                    old=r['refs'][0]['start'];r['refs'][0].update(start=old+a,end=old+b)
                    r['value']=value[a:b];output.append(r)
                rows=output
            elif op=='normalize':
                for r in rows:r.update(value=''.join(unicodedata.normalize('NFKC',r['value']).split()).casefold(),transformed=True)
            elif op=='number':
                for r in rows:
                    value=r['value'].strip()
                    if node.unit:
                        if not value.endswith(node.unit):raise ValueError('PROGRAM_UNIT_MISSING')
                        value=value[:-len(node.unit)].strip()
                    if not re.fullmatch(r'[+-]?[0-9]{1,20}(?:\.[0-9]{1,12})?',value):raise ValueError('PROGRAM_NUMBER_INVALID')
                    try:r.update(value=format(Decimal(value),'f'),unit=node.unit,transformed=True,numeric=True)
                    except InvalidOperation:raise ValueError('PROGRAM_NUMBER_INVALID')
            elif op=='count':rows=[dict(value=str(len(rows)),unit='条',refs=_refs(rows),transformed=True,numeric=True)]
            elif op=='sum':
                if not rows or any(not r.get('numeric') for r in rows) or len({r['unit'] for r in rows})!=1:raise ValueError('PROGRAM_SUM_INPUT')
                value=sum((Decimal(r['value']) for r in rows),Decimal(0))
                rows=[dict(value=format(value,'f'),unit=rows[0]['unit'],refs=_refs(rows),transformed=True,numeric=True)]
            elif op=='compare':
                equal=[(r['value'],r['unit']) for r in inputs[0]]==[(r['value'],r['unit']) for r in inputs[1]]
                rows=[dict(value='相同' if equal else '不同',unit='',refs=_refs(inputs[0]+inputs[1]),transformed=True)]
            elif op=='join':
                rows=[]
                for a in inputs[0]:
                    for b in inputs[1]:
                        if (a['value'],a['unit'])==(b['value'],b['unit']):
                            rows.append(dict(value=a['value'],unit=a['unit'],refs=_refs([a,b]),transformed=True))
                            if len(rows)>MAX_ROWS:raise ValueError('PROGRAM_ROW_LIMIT')
            elif op=='sort':rows=sorted(rows,key=lambda r:(r['value'],r['unit']))
            elif op=='distinct':
                output=[];seen=set()
                for r in rows:
                    key=(r['value'],r['unit'])
                    if key not in seen:output.append(r);seen.add(key)
                rows=output
            if len(rows)>MAX_ROWS or len(encoded(rows))>350000:raise ValueError('PROGRAM_ROW_LIMIT')
            nodes[node.id]=rows
    return dict(input_id=bundle['input_id'],nodes=nodes,output=contract.output)


def validate(bundle, contract, raw):
    proof=expected(bundle,contract)
    # Canonical JSON equality also rejects bool-as-number and extra claims.
    if digest(proof)!=digest(raw):raise ValueError('PROGRAM_CONTRACT_MISMATCH')
    return dict(validator=VERSION,contract_sha256=digest(contract),input_id=bundle['input_id'],
                output=proof['output'],nodes=proof['nodes'],semantic_acceptance=False,
                coverage='complete_in_bound_inputs_only')
