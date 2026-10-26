"""Ruled-table topology and explicit header/unit lineage over existing table IDs."""
import re
from collections import defaultdict
from statistics import median

from cad_agent.contracts import stable_id

TABLE_VERSION = 'table-semantics-v1.8-ruled-source'
HEADER_WORDS = re.compile(r'编号|名称|型号|规格|宽度|高度|尺寸|数量|单位|材料|备注|序号|厚度|长度|面积|做法|位置|类别|等级|洞口|门窗|开启|房间|楼层|项目|性能|耐火|适用|部位|类型')
UNITS = re.compile(r'(?<![A-Za-z])(?:mm[²2]?|cm[²2]?|m[²2]?|MPa|kN|kPa|kg/m[³3]|毫米|厘米|平方米|㎡|米|个|樘|扇)(?![A-Za-z])')


def explicit_units(text):
    # Only unit-only cells and explicitly parenthesized/单位 expressions supply units.
    parts = re.findall(r'[（(\[]([^）)\]]+)[）)\]]', text)
    if '单位' in text:
        parts.append(text.split('单位',1)[1].lstrip('：: '))
    if UNITS.fullmatch(text.strip()):
        parts.append(text.strip())
    numeric = re.fullmatch(r'\s*[+\-]?\d+(?:\.\d+)?\s*(\S+)\s*', text)
    if numeric and UNITS.fullmatch(numeric[1]):parts.append(numeric[1])
    aliases = {'毫米':'mm','厘米':'cm','平方米':'m2','㎡':'m2','米':'m','m²':'m2','mm²':'mm2','cm²':'cm2','kg/m³':'kg/m3'}
    # A parenthetical material instruction containing g/平方米 is not a unit
    # declaration for a layer thickness or the whole cell.
    declarations = [re.sub(r'^\s*单位\s*[:：]?\s*', '', part).strip() for part in parts]
    return sorted({aliases.get(part,part) for part in declarations if UNITS.fullmatch(part)})



# Only short, explicit field labels establish a role. Material prose containing
# “做法” or “部位” is not itself a header.
FIELD_LABELS = {
    'code': r'(?:设计|构造|做法|类型)?编号|型号',
    'ordinal': r'序号',
    'name': r'(?:构造|做法|材料|项目)?名称|项目|类别|类型|分类',
    'applicability': r'(?:适用|使用|应用)?(?:部位|位置|范围)(?:名称)?|房间(?:名称)?|楼层',
    'layers': r'(?:构造|材料|具体|建筑)?[做作]法|构造层次|材料及[做作]法|[做作]法及用料|用料及[做作]法',
    'notes': r'备注|说明',
    'requirements': r'(?:技术|材料|性能|构造)要求|技术性能',
}


def field_roles(header_path):
    return sorted({role for label in header_path for role, pattern in FIELD_LABELS.items()
                   if re.fullmatch(pattern, re.sub(r'\s+', '', label))})


def local_assembly_headers(index, target, band):
    """Read headers on the same continuous side rails, without a global table scan.

    Source text can consist of separate CAD glyphs. Reassemble only one baseline
    at a time, retain each glyph's evidence, and accept a one-to-one, ordered
    column mapping only. A label spilling slightly over a rail is kept explicit.
    """
    a, b, c, d = band['bbox']; xs = band['column_edges']; tol = .11
    rails = [line['points'] for line in band['source_lines']
             if abs(line['points'][0][0]-line['points'][1][0]) < .01
             and any(abs(line['points'][0][0]-side) <= tol for side in (a,c))]
    if len(rails) < 2:
        return dict(state='unresolved', fields=[], diagnostics=['HEADER_SIDE_RAILS_UNRESOLVED'])
    top = min(max(p[1] for p in rail) for rail in rails)
    bottom = max(min(p[1] for p in rail) for rail in rails)
    hs = []; horizontal_sources=[]
    for line in index.scene['lines']:
        if line['layout_id'] != target.layout_id: continue
        for p,q in zip(line['points'],line['points'][1:]):
            if abs(p[1]-q[1]) < .01 and min(p[0],q[0])<=a+tol and max(p[0],q[0])>=c-tol:
                if bottom-tol <= p[1] <= top+tol:
                    hs.append(p[1]);horizontal_sources.append(dict(entity_id=line['entity_id'],points=[p,q]))
    ys = sorted({round(y,5) for y in hs if y >= d-tol})
    ts = [t for t in index.scene['texts'] if t['layout_id']==target.layout_id
          and a<=t['origin'][0]<c and d<=t['origin'][1]<top and abs(t.get('rotation',0))<.1]
    for lo,hi in list(zip(ys,ys[1:]))[:64]:
        chosen = sorted([t for t in ts if lo<=t['origin'][1]<hi],key=lambda t:(-t['origin'][1],t['origin'][0]))
        if not chosen: continue
        height = median(t['height'] for t in chosen)
        if not height or hi-lo > 8*height: continue
        baselines=[]
        for t in chosen:
            line=next((line for line in baselines if abs(line[0]['origin'][1]-t['origin'][1])<height*.35),None)
            if line is None: baselines.append([t])
            else: line.append(t)
        header_xs=[xs[0]]+[x for x in xs[1:-1] if any(abs(line['points'][0][0]-x)<=tol
            and abs(line['points'][1][0]-x)<=tol and min(p[1] for p in line['points'])<=lo+tol
            and max(p[1] for p in line['points'])>=hi-tol for line in band['source_lines'])]+[xs[-1]]
        possibilities=[]
        for line in baselines:
            groups=[]
            for t in sorted(line,key=lambda t:t['origin'][0]):
                if groups and t['origin'][0]-groups[-1][-1]['bbox'][2] <= height*2:
                    groups[-1].append(t)
                else: groups.append([t])
            labels=[(''.join(t['text'] for t in group).strip(),group) for group in groups]
            mapping_xs=xs if len(labels)==len(xs)-1 else header_xs
            if len(labels)!=len(mapping_xs)-1 or any(not field_roles([text]) for text,_ in labels): continue
            if any(not mapping_xs[i]-height*2 <= (group[0]['origin'][0]+group[-1]['bbox'][2])/2 <= mapping_xs[i+1]+height*2
                   for i,(_,group) in enumerate(labels)): continue
            possibilities.append((labels,mapping_xs))
        if len(possibilities)!=1: continue
        labels,header_xs=possibilities[0]
        fields=[]
        for i in range(len(xs)-1):
            parent=next(j for j in range(len(header_xs)-1) if header_xs[j]<=xs[i] and xs[i+1]<=header_xs[j+1])
            text,group=labels[parent]
            fields.append(dict(column_index=i,text=text,roles=field_roles([text]),
                evidence=[e for t in group for e in index.entity_evidence(t['entity_id'])],
                entity_ids=[t['entity_id'] for t in group],state='source_bound'))
        # A merged title strip immediately under the field labels may declare
        # the layer direction (e.g. a roof-family heading). Never inherit it
        # through an intervening data row.
        lower=max((y for y in hs if y<lo-tol),default=lo)
        title_strip=lower<lo and lo-lower<=height*8 and not any(
            abs(line['points'][0][0]-line['points'][1][0])<.01
            and a+tol<line['points'][0][0]<c-tol
            and min(p[1] for p in line['points'])<=lower+tol
            and max(p[1] for p in line['points'])>=lo-tol for line in band['source_lines'])
        order_texts=chosen+([t for t in ts if lower<=t['origin'][1]<lo] if title_strip else [])
        return dict(state='source_bound',method='same_side_rails_ordered_header_labels',fields=fields,
                    bbox=[a,lo,c,hi],table_bbox=[a,bottom,c,hi],row_separators=sorted(set(hs),reverse=True),
                    source_lines=[line for line in horizontal_sources if any(abs(line['points'][0][1]-y)<=tol for y in (lo,hi))],
                    order_evidence=[e for t in order_texts if re.search(r'由上[至及]下|由下[至及]上|由内[至及]外|由外[至及]内',t['text']) for e in index.entity_evidence(t['entity_id'])],
                    diagnostics=[],note='字段来自同一连续边框内完整表头；适用条件仍按原文保留。')
    return dict(state='unresolved',fields=[],diagnostics=['HEADER_UNRESOLVED'])


def grid(index, table):
    """Reuse candidate table envelope; recover rectangular merges from missing edges.

    Reject nonrectangular merges rather than silently inventing cells.
    """
    a,b,c,d=table['bbox']; lid=table['layout_id']; tol=.11
    # Existing row/cell edges are source-backed grid anchors. Do not promote every
    # orthogonal segment inside a title block (logos/signatures) into a grid line.
    anchor_x=sorted({v for row in table['rows'] for cell in row['cells'] for v in (cell['bbox'][0],cell['bbox'][2])})
    anchor_y=sorted({v for row in table['rows'] for v in (row['bbox'][1],row['bbox'][3])})
    all_h,all_v=[],[]
    for line in index.scene['lines']:
        if line['layout_id']!=lid:continue
        for p,q in zip(line['points'],line['points'][1:]):
            if abs(p[1]-q[1])<.01:
                all_h.append((round(p[1],2),min(p[0],q[0]),max(p[0],q[0])))
            elif abs(p[0]-q[0])<.01:
                all_v.append((round(p[0],2),min(p[1],q[1]),max(p[1],q[1])))
    texts=[t for t in index.scene['texts'] if t['layout_id']==lid and a<=t['origin'][0]<c and b<=t['origin'][1]<d]
    source=table.get('ruled_source')
    if source:
        all_h,all_v=source['horizontal'],source['vertical']
        if source['instance_path']:
            texts=[t for t in texts if t.get('entity_path',[])[:len(source['instance_path'])]==source['instance_path']]
    height=median(t['height'] for t in texts) if texts else 1
    raw_cells=[cell for row in table['rows'] for cell in row['cells']]
    cluttered=0
    for cell in raw_cells:
        x0,y0,x1,y1=cell['bbox']
        internal=sum(x0+tol<lo and hi<x1-tol and y0+tol<y<y1-tol and hi-lo>2*height for y,lo,hi in all_h)
        internal+=sum(y0+tol<lo and hi<y1-tol and x0+tol<x<x1-tol and hi-lo>2*height for x,lo,hi in all_v)
        cluttered+=internal>=8
    if cluttered>=3 and cluttered>=len(raw_cells)*.25:
        raise ValueError('TABLE_GRAPHICS_DOMINATE: 多个候选格内存在密集长线，可能是平面图误候选；保留原文，不确认网格。')
    def near(v,anchors):return any(abs(v-x)<=tol for x in anchors)
    # A candidate can be a data-only band of a longer table. Recover the shared
    # envelope from source side rails, so through-going column lines survive.
    full_y=sorted({y for y,lo,hi in all_h if abs(lo-a)<=tol and abs(hi-c)<=tol}|set(anchor_y))
    max_gap=max(hi-lo for lo,hi in zip(anchor_y,anchor_y[1:]))*2
    extent_b=b;extent_d=d
    for y in reversed([v for v in full_y if v<b]):
        if extent_b-y>max_gap:break
        extent_b=y
    for y in [v for v in full_y if v>d]:
        if y-extent_d>max_gap:break
        extent_d=y
    rails=[]
    for side in (a,c):
        spans=[(lo,hi) for x,lo,hi in all_v if abs(x-side)<=tol and lo<=b+tol and hi>=d-tol]
        if spans:rails.append(min(spans,key=lambda v:v[1]-v[0]))
    if len(rails)==2:
        extent_b=min(extent_b,max(r[0] for r in rails))
        extent_d=max(extent_d,min(r[1] for r in rails))
    vs=[(x,lo,hi) for x,lo,hi in all_v if near(x,anchor_x) and hi>=b-tol and lo<=d+tol and
        (lo>=extent_b-tol or hi<=extent_d+tol or abs(x-a)<=tol or abs(x-c)<=tol)]
    xs=sorted({a,c}|{min(anchor_x,key=lambda v:abs(v-x)) for x,lo,hi in vs})
    # Interior row splits need endpoints on actual column rails. This retains
    # partial separators in merged rows while rejecting cell-internal graphics.
    hs=[(y,lo,hi) for y,lo,hi in all_h if b-tol<=y<=d+tol and lo>=a-tol and hi<=c+tol and
        (near(y,anchor_y) or (near(lo,xs) and near(hi,xs) and
          any(abs(x-lo)<=tol and v0-tol<=y<=v1+tol for x,v0,v1 in vs) and
          any(abs(x-hi)<=tol and v0-tol<=y<=v1+tol for x,v0,v1 in vs)))]
    ys=sorted(set(anchor_y)|{y for y,lo,hi in hs},reverse=True)
    n,m=len(ys)-1,len(xs)-1
    if m<2 and table['column_count']>1:
        raise ValueError('候选表格边界不足以恢复列关系，请使用 read_table 原文视图。')
    if n*m>4000 or n<1 or m<1:
        raise ValueError('表格网格超过有界分析范围。')
    parent=list(range(n*m))
    def root(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    def merge(i,j):
        parent[root(j)]=root(i)
    def covered(edges,at,lo,hi):
        end=lo
        for left,right in sorted((l,r) for p,l,r in edges if abs(p-at)<=tol):
            if right<end:continue
            if left>end+tol:break
            end=max(end,right)
        return end>=hi-tol
    for r in range(n):
        for col in range(m):
            if col+1<m and not covered(vs,xs[col+1],ys[r+1],ys[r]):merge(r*m+col,r*m+col+1)
            if r+1<n and not covered(hs,ys[r+1],xs[col],xs[col+1]):merge(r*m+col,(r+1)*m+col)
    groups=defaultdict(list)
    for i in range(n*m):groups[root(i)].append(divmod(i,m))
    cells=[];diagnostics=[]
    for coords in groups.values():
        r0=min(r for r,col in coords);r1=max(r for r,col in coords)+1
        c0=min(col for r,col in coords);c1=max(col for r,col in coords)+1
        if len(coords)!=(r1-r0)*(c1-c0):
            raise ValueError('NON_RECTANGULAR_CELL_COMPONENT: 缺边形成非矩形连通区域，不丢弃格子或推定合并关系。')
        bounds=[xs[c0],ys[r1],xs[c1],ys[r0]]
        def anchor_x(text):
            x=text['origin'][0]
            # Centered captions can start just across a rail. Reassign only a
            # small overhang with most of the text in the adjacent column.
            center=(text['bbox'][0]+text['bbox'][2])/2
            if source and any(0 < rail-x <= text['height']*.3 and center>rail for rail in xs[1:-1]):
                return center
            return x
        chosen=sorted([t for t in texts if bounds[0]<=anchor_x(t)<bounds[2] and bounds[1]<=t['origin'][1]<bounds[3]],
                      key=lambda t:(-t['origin'][1],t['origin'][0],t['entity_id']))
        rotated=[t for t in chosen if abs(t.get('rotation',0))>.1]
        if rotated:diagnostics.append('ROTATED_CELL_TEXT_REQUIRES_REVIEW')
        ids=[t['entity_id'] for t in chosen]
        cells.append(dict(cell_id=stable_id('semanticcell',table['table_id'],TABLE_VERSION,str(bounds)),
            row_index=r0,column_index=c0,row_span=r1-r0,column_span=c1-c0,bbox=bounds,
            text='\n'.join(t['text'] for t in chosen),entity_ids=ids,
            evidence=[e for eid in ids for e in index.entity_evidence(eid)],state='candidate'))
    return cells,n,m,sorted(set(diagnostics))


def read_semantic_table(index,args):
    table=next((t for t in index.tables if t['table_id']==args.table_id),None)
    if table is None:raise ValueError('表格不属于当前快照。')
    cache=getattr(index,'_semantic_grids',{})
    key=(TABLE_VERSION,table['table_id'],tuple(table['bbox']))
    if key not in cache:cache[key]=grid(index,table)
    index._semantic_grids=cache
    cells,n,m,cached_diagnostics=cache[key]
    diagnostics=list(cached_diagnostics)
    header_rows=args.header_rows
    if header_rows and max(header_rows)>=n:raise ValueError('表头行超出表格。')
    if header_rows is None:
        header_rows=[]
        for r in range(min(4,n)):
            row=[c for c in cells if c['row_index']==r]
            ordinal_headers = [c for c in cells if c['row_index'] in header_rows
                               and re.fullmatch(r'\s*序\s*号\s*', c['text'])]
            if any(c['column_index'] == h['column_index'] and re.fullmatch(r'\s*\d+\s*', c['text'])
                   for c in row for h in ordinal_headers):
                break
            words=[c for c in row if HEADER_WORDS.search(c['text'])]
            values=[c for c in row if re.fullmatch(r'\s*[+\-]?\d+(?:\.\d+)?\s*',c['text'])]
            if words and (not values or (len(words)>=3 and len(words)*2>=len(row))) and (len(words)>=2 or (len(row)==1 and row[0]['column_span']==m)):
                header_rows.append(r)
            else:break
    headers=[c for c in cells if c['row_index'] in header_rows]
    if not header_rows:diagnostics.append('HEADER_UNRESOLVED')
    elif len(header_rows)>=n:diagnostics.append('NO_DATA_ROWS')
    title_headers=[c for c in headers if c['column_span']==m and m>1]
    rows=[]
    for r in range(args.row_offset,min(n,args.row_offset+args.row_limit)):
        output=[]
        for cell in cells:
            if not cell['row_index']<=r<cell['row_index']+cell['row_span']:continue
            ancestry=[h for h in headers if h['row_index']<cell['row_index'] and h['column_index']<=cell['column_index'] and
                      h['column_index']+h['column_span']>=cell['column_index']+cell['column_span']]
            ancestry.sort(key=lambda h:h['row_index'])
            units=sorted({u for h in ancestry if h not in title_headers for u in explicit_units(h['text'])})
            own=explicit_units(cell['text'])
            unit_set=sorted(set(units+own))
            unit_state='conflict' if len(unit_set)>1 else 'derived' if len(unit_set)==1 else 'unknown'
            value=cell['text'].strip()
            numeric=re.fullmatch(r'([+\-]?\d+(?:\.\d+)?)\s*(.*)',value)
            number=float(numeric[1]) if numeric and (not numeric[2] or UNITS.fullmatch(numeric[2])) else None
            output.append(cell | dict(is_header=cell['row_index'] in header_rows,
                continuation_of=cell['cell_id'] if r!=cell['row_index'] else None,
                header_path=[h['text'] for h in ancestry if h not in title_headers],
                header_cell_ids=[h['cell_id'] for h in ancestry],
                header_evidence=[e for h in ancestry for e in h['evidence']],
                header_relation='user_selected' if args.header_rows is not None else 'candidate',
                unit=unit_set[0] if len(unit_set)==1 else None,unit_candidates=unit_set,unit_state=unit_state,
                unit_evidence=[e for h in ancestry if h not in title_headers and explicit_units(h['text']) for e in h['evidence']]+(cell['evidence'] if own else []),
                numeric_value=number,value_state='observed' if value else 'unknown'))
        output.sort(key=lambda c:c['column_index'])
        rows.append(dict(row_index=r,cells=output))
    # Match a code only through an explicit 编号/型号 column, never the first column by default.
    links=[]
    for row in rows:
        for cell in row['cells']:
            if cell['is_header'] or cell['continuation_of'] or not any(re.search('编号|型号',p) for p in cell['header_path']):continue
            code=re.sub(r'\s+','',cell['text'])
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9\-]{1,30}',code):continue
            matches=[t for t in index.scene['texts'] if t['layout_id']==table['layout_id'] and
                re.sub(r'\s+','',t['text'])==code and not (table['bbox'][0]<=t['origin'][0]<table['bbox'][2] and table['bbox'][1]<=t['origin'][1]<table['bbox'][3])]
            links.append(dict(cell_id=cell['cell_id'],code=code,state='candidate',method='exact_code_text',
                total=len(matches),truncated=len(matches)>12,
                targets=[dict(entity_id=t['entity_id'],bbox=t['bbox'],evidence=index.entity_evidence(t['entity_id'])) for t in matches[:12]],
                note='同编号文字候选，不证明门窗实例身份或实例数量。'))
    # Expose same-row field candidates with explicit header lineage, without
    # filling blank cells or turning repeated/merged codes into new instances.
    records=[]
    for row in rows:
        data_cells=[c for c in row['cells'] if not c['is_header']]
        if not data_cells:continue
        identities=[c for c in data_cells if any(re.search('编号|型号|名称',p) for p in c['header_path'])]
        records.append(dict(row_index=row['row_index'],state='candidate',
            relation_basis='source_grid_row',identity_cell_ids=[c['cell_id'] for c in identities],
            identity_state='candidate' if identities else 'unresolved',
            fields=[dict(cell_id=c['cell_id'],header_path=c['header_path'],header_cell_ids=c['header_cell_ids'],
                         text=c['text'],state=c['value_state'],field_roles=field_roles(c['header_path']),unit=c['unit'],unit_state=c['unit_state'],
                         row_span=c['row_span'],column_span=c['column_span'],continuation_of=c['continuation_of'],
                         evidence=c['evidence'],header_evidence=c['header_evidence']) for c in data_cells],
            missing_cell_ids=[c['cell_id'] for c in data_cells if c['value_state']=='unknown']))
    for record in records:
        fields=record['fields']
        identity=[f for f in fields if set(f['field_roles']) & {'code','name','applicability'} and f['text'].strip()]
        conflicts=[]
        for field in identity:
            if 'code' in field['field_roles'] and len([part for part in re.split(r'[\s,，;；、/]+',field['text'].strip()) if re.search(r'[0-9０-９]',part)])>1:
                conflicts.append('MULTIPLE_CODES_IN_IDENTITY_CELL')
        for role in ('code','name'):
            if len([f for f in identity if role in f['field_roles']])>1:
                conflicts.append('MULTIPLE_IDENTITY_FIELDS_REQUIRE_REVIEW')
        if any(f['column_span']>1 and not f['field_roles'] for f in fields):
            conflicts.append('MERGED_FIELD_ROLE_UNRESOLVED')
        record['binding']=dict(state='source_bound' if identity and not conflicts and not cached_diagnostics else 'unresolved',
            basis='explicit_header_and_source_grid',identity_cell_ids=[f['cell_id'] for f in identity],
            applicability_cell_ids=[f['cell_id'] for f in fields if 'applicability' in f['field_roles']],
            layer_cell_ids=[f['cell_id'] for f in fields if 'layers' in f['field_roles']],
            shared_cell_ids=[f['cell_id'] for f in fields if f['row_span']>1],
            diagnostics=conflicts,object_instance_state='unverified',
            note='仅绑定原表行字段；合并格为同一来源，空格不继承，不能据此确认图中实例。')
    more=args.row_offset+args.row_limit<n
    next_offset=args.row_offset+args.row_limit if more else None
    actions=[]
    if more:
        arguments=dict(table_id=table['table_id'],row_offset=next_offset,row_limit=args.row_limit)
        if args.header_rows is not None:arguments['header_rows']=args.header_rows
        actions.append(dict(tool='read_table_semantics',arguments=arguments))
    result = dict(snapshot_id=index.snapshot,table_id=table['table_id'],layout_id=table['layout_id'],bbox=table['bbox'],
                version=TABLE_VERSION,row_count=n,column_count=m,header_rows=header_rows,
                header_state='user_selected' if args.header_rows is not None else 'candidate' if header_rows else 'unresolved',
                items=rows,records=records,instance_links=links,diagnostics=sorted(set(diagnostics)),coverage='partial',
                next_offset=next_offset,next_actions=actions,
                checks=dict(source_rows_exhausted=not more and args.row_offset<n,earlier_rows_not_in_page=args.row_offset,
                            header_relation='candidate' if header_rows else 'unresolved',object_completeness='unverified'),
                has_more=args.row_offset+args.row_limit<n,row_offset=args.row_offset,
                note='表头关系和编号关联为候选；合并格保留同一 cell_id 与跨度，空白不向下填充。单位仅来自明确表头或单元格，不采用 DWG 绘图单位。')

    from cad_agent.analysis.efficiency import compact, dumps
    if len(dumps(compact(result))) > 38000 and len(rows) > 1:
        smaller = read_semantic_table(index, args.model_copy(update={'row_limit': len(rows)-1}))
        smaller['requested_row_limit'] = args.row_limit
        return smaller
    result['effective_row_limit'] = len(rows)
    return result
