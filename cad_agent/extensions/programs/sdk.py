"""Pure helpers copied into the isolated program; never used as validator."""
import copy
import unicodedata
from decimal import Decimal


def records(bundle, source=0):
    return [dict(value=t['text'],unit='',refs=[dict(source=source,entity_id=t['entity_id'],start=0,end=len(t['text']))])
            for t in bundle['sources'][source]['texts'] if t['text']]


def select(rows, keywords):
    return [copy.deepcopy(r) for r in rows if any(k in r['value'] for k in keywords)]


def field(rows, prefix='', suffix=''):
    out=[]
    for r in rows:
        text=r['value'];start=text.find(prefix) if prefix else 0
        if start<0:continue
        start+=len(prefix);end=text.find(suffix,start) if suffix else len(text)
        if end<0 or end==start:continue
        if len(r['refs'])!=1 or r.get('transformed'):raise ValueError('FIELD_REQUIRES_SOURCE')
        item=copy.deepcopy(r);item['value']=text[start:end]
        item['refs'][0]['start']+=start;item['refs'][0]['end']=item['refs'][0]['start']+end-start
        out.append(item)
    return out


def normalize(rows):
    out=copy.deepcopy(rows)
    for r in out:r.update(value=''.join(unicodedata.normalize('NFKC',r['value']).split()).casefold(),transformed=True)
    return out


def number(rows, unit=''):
    out=copy.deepcopy(rows)
    for r in out:
        text=r['value'].strip()
        if unit:
            if not text.endswith(unit):raise ValueError('UNIT_MISSING')
            text=text[:-len(unit)].strip()
        value=Decimal(text)
        if not value.is_finite():raise ValueError('NONFINITE')
        r.update(value=format(value,'f'),unit=unit,transformed=True,numeric=True)
    return out


def refs(rows):
    out=[]
    for r in rows:
        for ref in r['refs']:
            if ref not in out:out.append(ref)
    return out


def count(rows):
    return [dict(value=str(len(rows)),unit='条',refs=refs(rows),transformed=True,numeric=True)]


def sum_values(rows):
    if not rows or any(not r.get('numeric') for r in rows) or len({r['unit'] for r in rows})!=1:raise ValueError('SUM_INPUT')
    return [dict(value=format(sum((Decimal(r['value']) for r in rows),Decimal(0)),'f'),
                 unit=rows[0]['unit'],refs=refs(rows),transformed=True,numeric=True)]


def compare(left,right):
    return [dict(value='相同' if [(r['value'],r['unit']) for r in left]==[(r['value'],r['unit']) for r in right] else '不同',
                 unit='',refs=refs(left+right),transformed=True)]


def join(left,right):
    out=[]
    for a in left:
        for b in right:
            if (a['value'],a['unit'])==(b['value'],b['unit']):
                out.append(dict(value=a['value'],unit=a['unit'],refs=refs([a,b]),transformed=True))
                if len(out)>1000:raise ValueError('ROW_LIMIT')
    return out


def sort(rows):
    return sorted(copy.deepcopy(rows),key=lambda r:(r['value'],r['unit']))


def distinct(rows):
    out=[];seen=set()
    for r in rows:
        key=(r['value'],r['unit'])
        if key not in seen:out.append(copy.deepcopy(r));seen.add(key)
    return out
