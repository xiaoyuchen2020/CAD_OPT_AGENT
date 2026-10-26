"""Cell-local text and explicit field lineage; never concatenate across rails."""
from collections import defaultdict
from copy import deepcopy
import re
from statistics import median
from cad_agent.contracts import stable_id
from .topology import transform_point
from ezdxf.tools.text import plain_text

NUMBER = re.compile(r'^\s*([≤≥<>~≈]?\s*[+−-]?\d+(?:\.\d+)?)(\s*(?:m[²³23]?|mm|cm|%|平方米|㎡|米|个|层|位|樘|F))?\s*$', re.I)
HEADER = re.compile(r'^(?:序号|编号|名称|项目|指标|单位|数量|面积|备注|说明|层数|高度|类型|部位|使用性质|设计值|控制值|技术要求|建筑面积|用地面积|地上|地下|计容|不计容|建筑物名称|[\u4e00-\u9fff]{0,8}(?:面积|数量|层数|高度|单位|名称|编号|指标|比例|宽度))(?:[（(][^）)]+[）)])?$')
UNIT = re.compile(r'^(?:m[²³23]?|mm|cm|%|平方米|㎡|米|个|层|位|樘|F)$',re.I)


def number_match(text):
    return NUMBER.fullmatch(plain_text(text))


def display_text(text):
    return '\n'.join(plain_text(line) for line in text.split('\n'))


def normalized(text):
    return re.sub(r'\s+', '', text).replace('：',':').replace('（','(').replace('）',')')


def evidence_for(index, entity_id):
    # Quote just the original entity's span, not a region that has concatenated
    # adjacent cells. A region can contain "1933" while these sources are 1/933.
    return index.entity_evidence(entity_id)


def _lines(fragments,height):
    lines=[]
    for f in sorted(fragments,key=lambda f:(-f['local_origin'][1],f['local_origin'][0])):
        line=next((v for v in lines if abs(v[0]['local_origin'][1]-f['local_origin'][1])<height*.4),None)
        if line is None:lines.append([f])
        else:line.append(f)
    return [sorted(line,key=lambda f:f['local_origin'][0]) for line in lines]


def compose(fragments,height):
    """Return display text plus source-backed, ordered fill-in relations.

    A blank is filled only with a one-to-one baseline-ordered mapping in the
    same cell. Literal fragments always remain separately inspectable.
    """
    output=[];fields=[]
    for line in _lines(fragments,height):
        templates=[f for f in line if re.search(r' {2,}',f['raw_text']) and re.search(r'[\u4e00-\u9fff]',f['raw_text'])]
        values=[f for f in line if number_match(f['raw_text'])]
        if len(templates)==1 and len(values)==len(list(re.finditer(r' {2,}',templates[0]['raw_text']))) and len(line)==len(values)+1:
            template=templates[0];raw=template['raw_text'];matches=list(re.finditer(r' {2,}',raw));parts=[];at=0
            # Fill-ins must lie after their label's insertion point. Ordering
            # alone never binds another row or a numeric fragment across a rail.
            if all(v['local_origin'][0]>template['local_origin'][0] for v in values):
                for m,v in zip(matches,values):
                    parts += [raw[at:m.start()],v['raw_text']];at=m.end()
                    label=re.split('[，,；;。]| {2,}',raw[:m.start()])[-1].strip(' ：:')
                    suffix=raw[m.end():];unit=re.match(r'(平方米|㎡|米|个|层|位|樘|m[²2]?)',suffix)
                    num=number_match(v['raw_text']);explicit=(num[2] or '').strip()
                    fields.append(dict(label=label,raw_value=v['raw_text'],value=num[1].replace(' ',''),
                        unit=explicit or (unit[1] if unit else ''),qualifier='',
                        binding_basis='same_cell_unique_ordered_fill_ins',binding_state='source_bound',
                        label_evidence=template['evidence'],evidence=v['evidence'],
                        unit_evidence=v['evidence'] if explicit else template['evidence'] if unit else [],
                        source_entity_ids=[template['entity_id'],v['entity_id']]))
                parts.append(raw[at:]);output.append(''.join(parts));continue
        if len(values)==1 and len(line)==2:
            label=next((f for f in line if f not in values),None)
            value=values[0]
            if label and re.search(r'[:：≤≥]$',label['raw_text'].strip()) and label['local_origin'][0]<value['local_origin'][0]:
                m=number_match(value['raw_text']);qualifier=label['raw_text'].strip()[-1]
                output.append(label['raw_text']+value['raw_text'])
                fields.append(dict(label=label['raw_text'].rstrip('：:≤≥ '),raw_value=value['raw_text'],value=m[1].replace(' ',''),
                    unit=(m[2] or '').strip(),qualifier=qualifier if qualifier in '≤≥' else '',binding_basis='same_cell_explicit_suffix',
                    binding_state='source_bound',label_evidence=label['evidence'],evidence=value['evidence'],
                    unit_evidence=value['evidence'],source_entity_ids=[label['entity_id'],value['entity_id']]))
                continue
        # Separate numeric fragments cannot become a new number even within a
        # cell. This also preserves independent glyphs until explicitly reviewed.
        output.append(' | '.join(f.get('display_text',f['raw_text']) for f in line))
    return '\n'.join(output),fields


def bind_cells(index,candidate):
    cells=deepcopy(candidate['cells']);transform=candidate['transform'];height=transform['height'];tol=transform['tolerance']
    assigned={};unassigned=[];fields=[]
    for c in cells:c.update(fragments=[],diagnostics=[],structure_state='source_bound',source_state='source_bound',binding_state='source_bound')
    for t in candidate['texts']:
        x,y=t['local_origin'];matches=[]
        outer=candidate['local_bbox'];overhang=not outer[0]<=x<=outer[2]
        if overhang:
            if outer[0]-height*.15<=x<outer[0]:x=outer[0]+tol
            elif outer[2]<x<=outer[2]+height*.15:x=outer[2]-2*tol
        for c in cells:
            a,b,z,w=c['local_bbox']
            if a-tol<=x<z-tol and b-tol<=y<w-tol:matches.append(c)
        if len(matches)!=1:
            unassigned.append(t['entity_id']);continue
        c=matches[0];evidence=evidence_for(index,t['entity_id'])
        if overhang:c['diagnostics'].append('TEXT_INSERTION_NEAR_OUTER_EDGE')
        if not evidence:
            unassigned.append(t['entity_id']);c['diagnostics'].append('TEXT_SOURCE_UNBOUND');continue
        f=dict(entity_id=t['entity_id'],raw_text=t['text'],display_text=display_text(t['text']),origin=t['origin'],bbox=t['bbox'],evidence=evidence,source_kind='native_text',local_origin=t['local_origin'],assignment_basis='near_outer_edge' if overhang else 'native_insertion_inside')
        c['fragments'].append(f);assigned[t['entity_id']]=c['cell_id']
    for c in cells:
        c['text'],own=compose(c['fragments'],height)
        for f in own:
            f.update(cell_id=c['cell_id'],header_path=[],field_id=stable_id('tfield',c['cell_id'],f['label'],*f['source_entity_ids']))
            if any(p['assignment_basis']=='near_outer_edge' and p['entity_id'] in f['source_entity_ids'] for p in c['fragments']):f['binding_state']='candidate'
        fields.extend(own)
        for f in c['fragments']:f.pop('local_origin',None)
        if c['diagnostics']:c['binding_state']='candidate' if set(c['diagnostics'])=={'TEXT_INSERTION_NEAR_OUTER_EDGE'} else 'unresolved'
    return cells,fields,unassigned


def title(index,candidate,cells):
    tr=candidate['transform'];a,b,c,d=candidate['local_bbox'];h=tr['height'];options=[]
    for t in index.scene['texts']:
        if t['layout_id']!=candidate['layout_id'] or list(t.get('entity_path',[])[:-1])!=candidate['instance_path']:continue
        x,y=transform_point(t['origin'],tr['origin'],tr['angle'])
        raw=t['text'].strip()
        if a<=x<c and d<=y<=d+6*h and len(raw)<=100 and re.search('表|计算书|清单|指标|明细|说明',raw):
            options.append((y-d,t))
    named=[v for v in options if not re.fullmatch(r'附?表\s*[A-Za-z一二三四五六七八九十\d]+',v[1]['text'].strip())]
    options=named or options
    if options:
        _,t=min(options,key=lambda v:v[0]);return t['text'].strip(),evidence_for(index,t['entity_id'])
    width=c-a
    top=[v for v in cells if v['row']==0 and v['local_bbox'][2]-v['local_bbox'][0]>=width*.8 and 0<len(v['text'])<100]
    if len(top)==1:
        v=top[0];return v['text'],[e for f in v['fragments'] for e in f['evidence']]
    return '未命名表格',[]


def hierarchy(candidate,cells,title_text):
    tid=candidate['table_id'];root=dict(node_id=tid,kind='table',parent_id=None,title=title_text,cell_ids=[],children=[],binding_state='source_bound')
    nodes=[root];groups=defaultdict(list)
    for c in cells:
        t=c['text'].strip()
        if c['local_bbox'][2]-c['local_bbox'][0]>=(candidate['local_bbox'][2]-candidate['local_bbox'][0])*.4 and t and len(t)<60 and '\n' not in t and re.search('[\u4e00-\u9fff]',t) and re.search(r'\d',t) and not re.search(r'[:：]|平方米|㎡|mm|\d[mM%]',t):
            stem=re.sub(r'[\d\s.]+','',t)
            groups[stem].append(c)
    headings=[c for members in groups.values() if len(members)>=2 for c in members]
    owner={}
    for heading in headings:
        a,b,z,w=heading['local_bbox']
        nxt=[c['local_bbox'][3] for c in headings if c!=heading and abs(c['local_bbox'][0]-a)<candidate['transform']['tolerance'] and c['local_bbox'][3]<b]
        bottom=max(nxt,default=candidate['local_bbox'][1]);gid=stable_id('tgroup',tid,heading['cell_id'])
        members=[c for c in cells if c['local_bbox'][0]>=a-.001 and c['local_bbox'][2]<=z+.001 and c['local_bbox'][3]<=w+.001 and c['local_bbox'][1]>=bottom-.001]
        node=dict(node_id=gid,kind='group',parent_id=tid,title=heading['text'],cell_ids=[],children=[],binding_state='source_bound')
        nodes.append(node);root['children'].append(gid)
        # Multiple narrow columns in a repeated field group form a child grid.
        narrow=[c for c in members if c['local_bbox'][2]-c['local_bbox'][0]<(z-a)*.75]
        if len(narrow)>=4 and len({c['column'] for c in narrow})>=2 and len({c['row'] for c in narrow})>=2:
            subid=stable_id('tsubtable',gid,*(c['cell_id'] for c in narrow));sub=dict(node_id=subid,kind='subtable',parent_id=gid,title='内部子表',cell_ids=[],children=[],binding_state='candidate')
            nodes.append(sub);node['children'].append(subid)
            for c in narrow:owner[c['cell_id']]=subid
        for c in members:owner.setdefault(c['cell_id'],gid)
    by_id={n['node_id']:n for n in nodes}
    for c in cells:
        parent=owner.get(c['cell_id'],tid);by_id[parent]['cell_ids'].append(c['cell_id'])
        leaf=dict(node_id=c['cell_id'],kind='cell',parent_id=parent,title=c['text'][:100],cell_ids=[c['cell_id']],children=[],binding_state=c['binding_state'])
        nodes.append(leaf);by_id[parent]['children'].append(c['cell_id'])
    return nodes


def grid_fields(cells,own_fields,nodes=None,transform=None):
    """Column headers are a leading band, never earlier data-row labels.

    Row labels, adjacent unit cells and notes keep explicit source identities.
    Header text split across a merged cell is projected only by its native
    insertion position into a unique existing data column.
    """
    fields=list(own_fields);own_ids={f['cell_id'] for f in fields}
    parents={n['node_id']:n.get('parent_id') for n in (nodes or [])}
    data_rows={c['row'] for c in cells if any(number_match(f['raw_text']) for f in c['fragments'])}
    first_data=min(data_rows,default=1)
    headers=[c for c in cells if c['row']<first_data and HEADER.fullmatch(normalized(c['text']))]
    note_headers=[c for c in cells if c['row']==0 and normalized(c['text']) in ('备注','说明')]
    def evidence(cs):return [e for c in cs for f in c['fragments'] for e in f['evidence']]
    for c in cells:
        if c['cell_id'] in own_ids or not c['text'].strip() or c in headers or c in note_headers or UNIT.fullmatch(c['text'].strip()):continue
        numeric=[f for f in c['fragments'] if number_match(f['raw_text'])]
        units=[f for f in c['fragments'] if UNIT.fullmatch(f['raw_text'].strip())]
        above=[h for h in headers if parents.get(h['cell_id'])==parents.get(c['cell_id']) and h['row']<c['row'] and h['column']<=c['column'] and h['column']+h['column_span']>=c['column']+c['column_span']]
        projected=[];ambiguous=False;header_qualifiers=[]
        for h in above:
            if h['column_span']>c['column_span'] and transform:
                parts=[f for f in h['fragments'] if c['local_bbox'][0]<=transform_point(f['origin'],transform['origin'],transform['angle'])[0]<c['local_bbox'][2]]
                qualifiers=[f for f in h['fragments'] if re.fullmatch(r'[（(][^）)]+[）)]',f['raw_text'].strip()) and not UNIT.fullmatch(f['raw_text'].strip('（）() '))]
                header_qualifiers.extend(dict(text=f['raw_text'],binding_state='candidate',evidence=f['evidence']) for f in qualifiers)
                parts=[f for f in parts if f not in qualifiers]
                text=''.join(f['raw_text'] for f in parts)
                if not parts or not HEADER.fullmatch(normalized(text)):
                    ambiguous=True;continue
                h=h|dict(text=text,fragments=parts)
            projected.append(h)
        above=sorted(projected,key=lambda h:h['row'])
        left=[h for h in cells if parents.get(h['cell_id'])==parents.get(c['cell_id']) and h['row']<=c['row']<h['row']+h['row_span'] and h['column']+h['column_span']<=c['column'] and h['text'].strip() and re.search('[\u4e00-\u9fff]',h['text']) and not NUMBER.fullmatch(h['text']) and not UNIT.fullmatch(h['text'].strip()) and normalized(h['text']) not in ('其中','备注','说明')]
        left.sort(key=lambda h:h['column'])
        label=above[-1] if above else left[-1] if left else None
        if label is None:continue
        # Only the immediate neighboring unit column can qualify this value.
        unit_cells=[u for u in cells if u['row']<=c['row']<u['row']+u['row_span'] and u['column']==c['column']+c['column_span'] and UNIT.fullmatch(u['text'].strip())]
        note_cells=[n for n in cells if n not in note_headers and n['row']==c['row'] and n['column']>c['column'] and n['text'].strip() and any(h['column']<=n['column']<h['column']+h['column_span'] for h in note_headers)]
        explicit_units=[u for h in above for u in re.findall(r'[（(]([^）)]+)[）)]',h['text']) if UNIT.fullmatch(u)]
        value=c['text'];unit='';state='ambiguous' if ambiguous else 'source_bound' if above or (numeric and left) else 'candidate'
        if len(numeric)==1:
            m=number_match(numeric[0]['raw_text']);value=m[1].replace(' ','');unit=(m[2] or '').strip()
            candidates=set(([unit] if unit else [])+[u['raw_text'].strip() for u in units]+explicit_units+[u['text'].strip() for u in unit_cells])
            if len(candidates)>1:state='conflict'
            unit=next(iter(candidates)) if len(candidates)==1 else ''
        elif len(numeric)>1:state='ambiguous'
        fields.append(dict(field_id=stable_id('tfield',c['cell_id'],'grid'),cell_id=c['cell_id'],
            label=label['text'],raw_value=c['text'],value=value,unit=unit,
            qualifier='',header_path=[h['text'] for h in above],header_qualifiers=header_qualifiers,binding_state=state,
            binding_basis='source_grid_header_and_row',evidence=evidence([c]),
            label_evidence=evidence([label]),unit_evidence=[e for f in units for e in f['evidence']]+evidence(unit_cells)+evidence([h for h in above if re.search(r'[（(]',h['text'])]),
            row_labels=[dict(text=h['text'],cell_id=h['cell_id'],evidence=evidence([h])) for h in left],
            notes=[dict(text=n['text'],cell_id=n['cell_id'],evidence=evidence([n])) for n in note_cells],
            source_entity_ids=[f['entity_id'] for f in c['fragments']]))
    return fields
