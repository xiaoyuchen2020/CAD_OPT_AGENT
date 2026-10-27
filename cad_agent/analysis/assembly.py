"""Source-bound assembly reading; line structure offers candidates, never scope proof."""
import re
from statistics import median
from copy import deepcopy
from functools import wraps
from bisect import bisect_left, bisect_right
from cad_agent.extensions.programs import kernels

from cad_agent.contracts import stable_id
from cad_agent.analysis.text_units import read_unit_candidates
from cad_agent.semantics.tables import explicit_units, local_assembly_headers

VERSION = 'assembly-source-v13-alternative-binding-proofs'
from cad_agent.cad.numbers import evidence_readings, format_numbered_text, without_claim_list_markers, ordinals
from cad_agent.cad.assembly_headings import FAMILY, HEADING, CHAPTER, source_titles
from cad_agent.cad.source_vocabulary import record_type, canonical, expand, identities, normalized_code


SECTION_BREAK = re.compile(r'^[ \t]*[一二三四五六七八九十]+[、．.][^\n]{1,80}', re.MULTILINE)


def _action(tool, **arguments):
    return dict(tool=tool, arguments=arguments)


def _geometry(index):
    """Index native rails once; target-dependent tolerances stay in ruled_band."""
    cached=getattr(index,'_assembly_geometry',None)
    if cached is not None:return cached
    def build():
        texts={};rails={}
        for text in index.scene['texts']:
            texts.setdefault(text['entity_id'],[]).append(text)
        for line in index.scene['lines']:
            hs,vs=rails.setdefault(line['layout_id'],([],[]))
            for p,q in zip(line['points'],line['points'][1:]):
                if abs(p[1]-q[1])<=.01:
                    hs.append((p[1],min(p[0],q[0]),max(p[0],q[0]),line['entity_id']))
                elif abs(p[0]-q[0])<=.01:
                    vs.append((p[0],min(p[1],q[1]),max(p[1],q[1]),line['entity_id']))
        for hs,vs in rails.values():hs.sort();vs.sort()
        return texts,rails
    if hasattr(index,'w') and hasattr(index,'signature'):
        from cad_agent.cache import workspace_cache
        cached=workspace_cache(index.w,'_assembly_geometry',max_entries=4).get_or_build(index.signature,build)
    else:cached=build()
    index._assembly_geometry=cached
    return cached


def _line_layers(index):
    cached=getattr(index,'_assembly_line_layers',None)
    if cached is None:
        def build():return {l['entity_id']:l.get('layer','') for l in index.scene['lines']}
        if hasattr(index,'w') and hasattr(index,'signature'):
            from cad_agent.cache import workspace_cache
            cached=workspace_cache(index.w,'_assembly_line_layers',max_entries=4).get_or_build(index.signature,build)
        else:cached=build()
        index._assembly_line_layers=cached
    return cached


def _cached(kind, key):
    """Bounded, source-versioned local geometry cache, including negative reads."""
    def decorate(fn):
        @wraps(fn)
        def call(index, *args):
            if not hasattr(index,'w') or not hasattr(index,'signature'):return fn(index,*args)
            from cad_agent.cache import workspace_cache
            cache=workspace_cache(index.w,'_assembly_'+kind,max_entries=2048,max_bytes=64*1024*1024)
            value=cache.get_or_build((VERSION,kernels.version(),index.signature,key(*args)),lambda:fn(index,*args),
                lambda v:len(repr(v))*2)
            return deepcopy(value) if kind!='spans' else value
        return call
    return decorate


@_cached('bands', lambda target:(target.region_id,tuple(target.entity_ids),tuple(target.bbox)))
def ruled_band(index, target):
    original=_ruled_band(index,target)
    if not hasattr(target,'bbox') or not hasattr(index,'scope'):return original
    # Independent rail layers can encode drafting baselines over merged cells.
    # A wider envelope is usable only with explicit headers and one identity;
    # layer names/colors never determine its semantics.
    _, rails=_geometry(index)
    line_layers=_line_layers(index)
    if original[0]:
        layers={line_layers[l['entity_id']] for l in original[0]['source_lines']}
    else:
        vs=rails.get(target.layout_id,([],[]))[1];at=bisect_left(vs,(target.bbox[0],))
        layers={line_layers[v[3]] for v in vs[max(0,at-24):at+24]
                if v[1]<=target.bbox[1] and v[2]>=target.bbox[3]}
    candidates=[]
    for layer in sorted(layers)[:16]:
        bands,_=_ruled_band(index,target,layer,collect=True)
        for band in bands:
            if original[0] and band['bbox']==original[0]['bbox']:continue
            sources=_source_spans(index,target,band)
            binding=_row_binding(index,target,band,sources)
            if binding['state']=='source_bound':
                titles={m[1]+m['code'] for f in binding['fields'] if set(f['roles'])&{'code','name'} for m in HEADING.finditer(f['text'])}
                if len(titles)==1:candidates.append((band,[]))
    unique={tuple(b['bbox']):(b,d) for b,d in candidates}
    if len(unique)==1:return next(iter(unique.values()))
    if len(unique)>1:return original[0],original[1]+['ROW_LAYER_ENVELOPES_AMBIGUOUS']
    return original


def _ruled_band(index, target, layer=None, collect=False):
    """Find the smallest enclosing ruled row, including slightly open CAD endpoints.

    Tolerance is capped by both source text height and row width. All gaps and
    source line IDs remain visible. Competing row envelopes are not selected.
    """
    text_index,rail_index=_geometry(index)
    texts = [t for eid in target.entity_ids for t in text_index.get(eid,[])]
    height = median(t['height'] for t in texts if t['height'] > 0) if any(t['height'] > 0 for t in texts) else 0
    if not height:
        return [] if collect else None, ['ROW_GEOMETRY_UNAVAILABLE']
    x = min(t['origin'][0] for t in texts)
    low = min(t['origin'][1] for t in texts)
    high = max(t['origin'][1] for t in texts)
    raw_hs,vs=rail_index.get(target.layout_id,([],[]))
    raw_hs=raw_hs[bisect_left(raw_hs,(low-height*80,)):bisect_right(raw_hs,(high+height*80,float('inf')))]
    if layer is not None:
        layers=_line_layers(index)
        raw_hs=[h for h in raw_hs if layers[h[3]]==layer]
        vs=[v for v in vs if layers[v[3]]==layer]
    hs=[h for h in raw_hs if h[2]-h[1]>height*8]
    # Nearest separator candidates only. Never use an unbounded all-pairs search.
    above = sorted([h for h in hs if h[1] <= x <= h[2] and h[0] >= high], key=lambda h:h[0])[:12]
    below = sorted([h for h in hs if h[1] <= x <= h[2] and h[0] < low], key=lambda h:-h[0])[:12]
    found = {}
    for top in above:
        for bottom in below:
            width = min(top[2],bottom[2])-max(top[1],bottom[1])
            tol = min(height*.15, width*.005)
            if width <= height*8 or not height < top[0]-bottom[0] < height*80:
                continue
            if layer is None and (abs(top[1]-bottom[1])>tol or abs(top[2]-bottom[2])>tol):continue
            left, right = max(top[1],bottom[1]), min(top[2],bottom[2])
            rails = [v for v in vs[bisect_left(vs,(left-tol,)):bisect_right(vs,(right+tol,float('inf')))] if v[1] <= bottom[0]+tol and v[2] >= top[0]-tol]
            # Deduplicate coincident source rails, retaining all line provenance.
            xs = sorted({round(v[0], 5) for v in rails})
            if len(xs) < 3 or abs(xs[0]-left) > tol or abs(xs[-1]-right) > tol:
                continue
            if max(b-a for a,b in zip(xs,xs[1:])) > width*.85:
                continue
            # A nearer full-width separator inside this candidate forbids merging rows.
            if any(bottom[0]+.01 < h[0] < top[0]-.01 and h[1]<=left+tol and h[2]>=right-tol for h in hs):
                continue
            box = [xs[0], bottom[0], xs[-1], top[0]]
            key = tuple(round(v, 4) for v in box)
            gaps = [max(0,top[1]-xs[0]),max(0,bottom[1]-xs[0]),max(0,xs[-1]-top[2]),max(0,xs[-1]-bottom[2])]
            gaps += [max(0,v[1]-bottom[0],top[0]-v[2]) for v in rails]
            found[key] = dict(bbox=box, column_edges=xs, relation_state='candidate',
                method='source_ruled_band', endpoint_tolerance=tol, maximum_endpoint_gap=max(gaps),
                source_lines=[dict(entity_id=h[3], points=[[h[1],h[0]],[h[2],h[0]]]) for h in (top,bottom)] +
                    [dict(entity_id=v[3],points=[[v[0],v[1]],[v[0],v[2]]]) for v in rails])
    if collect:
        return [band for band in found.values() if len({round(h[0],3) for h in hs
            if band['bbox'][1]<h[0]<band['bbox'][3] and band['bbox'][0]<=h[1] and h[2]<=band['bbox'][2]})<=8],[]
    if len(found) != 1:
        return None, ['ROW_ENVELOPE_AMBIGUOUS' if found else 'ROW_ENVELOPE_UNRESOLVED']
    band = next(iter(found.values()))
    a,b,c,d = band['bbox']
    # Dense internal grids/graphics do not establish a single construction row.
    internal = [h for h in hs if b < h[0] < d and a <= h[1] and h[2] <= c]
    if len({round(h[0],3) for h in internal}) > 8:
        return None, ['ROW_GRAPHICS_OR_SUBROWS_REQUIRE_REVIEW']
    return band, ['ROW_ENDPOINT_GAPS_REQUIRE_REVIEW'] if band['maximum_endpoint_gap'] > .11 else []


@_cached('spans', lambda target,band:(target.layout_id,target.frame_id,tuple(band['bbox']),tuple(band['column_edges'])))
def _source_spans(index, target, band):
    from cad_agent.extensions.programs.kernels import read
    return read(index, target, band)


def _fragments(index, region, start, end):
    """Each logical source line is retained verbatim, including substitutions."""
    fragments = []
    for match in re.finditer(r'[^\n]+', region.text[start:end]):
        lo,hi = start+match.start(),start+match.end()
        quote = region.text[lo:hi]
        roles = []
        if re.search(r'砂浆|混凝土|涂料|界面剂|保温|面砖|防水|腻子|基层',quote): roles.append('material_or_layer')
        if re.search(r'用于|适用|部位|房间|楼梯|走道|阳台|侧墙|顶棚',quote): roles.append('applicability_text')
        if re.search(r'改为|改用|替代|代替|或采用|或者|\b或\b|或[0-9]',quote): roles.append('alternative')
        if re.search(r'型号|耐火|等级|极限|性能',quote): roles.append('model_or_performance')
        if re.search(r'由表及里|由上[至及]下|由下[至及]上|由内[至及]外|由外[至及]内',quote): roles.append('order_basis')
        evidence = index.evidence(region,lo,hi)
        atoms = evidence_readings(evidence)
        markers = [t for t in atoms if t['kind']=='ordinal']
        marker = next((t for t in markers if not quote[:t['text_span'][0]].strip()), None)
        thicknesses = []
        for token in atoms:
            if token['kind'] not in ('quantity','ambiguous') or token.get('role')!='厚度': continue
            tail = quote[token['text_span'][1]:]
            unit = re.match(r'\s*(mm|毫米)',tail)
            suffix = re.match(r'\s*(?:mm|毫米)?\s*厚',tail)
            stop = token['text_span'][1]+(suffix.end() if suffix else 0)
            thicknesses.append(dict(text=quote[token['text_span'][0]:stop], value=token['value'],
                unit='mm' if unit else None,unit_state='explicit' if unit else 'unknown',
                quantity_id=token['number_id'],state=token['status'],text_span=token['text_span']))
        fragments.append(dict(text=quote,roles=roles or ['unclassified'],state='candidate',thickness_candidates=thicknesses,
            source_layer_marker=marker['raw'].strip() if marker else None,
            layer_number=int(marker['value']) if marker else None,
            display_text=format_numbered_text(quote, atoms),
            continuation_state='explicit_layer_start' if marker else 'source_line_only',
            branch_state='nonadditive_alternative' if 'alternative' in roles else 'not_classified',
            explicit_units=explicit_units(quote),unit_state='explicit' if explicit_units(quote) else 'unknown',
            parentheses_present=bool(re.search('[（(）)]',quote)),
            thickness_tokens=[t['text'] for t in thicknesses],
            evidence=evidence))
    return fragments


def _catalog(index,target,heading_start=None):
    explicit=next((m for m in HEADING.finditer(target.text)
                   if heading_start is None or m.start()==heading_start),None)
    match=FAMILY.search(target.text)
    family=explicit[1] if explicit else match[0] if match else None
    if not family:return []
    headings=[]
    for region in index.scope(target.layout_id,target.frame_id):
        for match in HEADING.finditer(region.text):
            if canonical(match[1]) != canonical(family) and not (explicit is None and family in expand(match[1])):continue
            end=region.text.find('\n',match.start())
            headings.append((region,match.start(),len(region.text) if end<0 else end,None,'explicit_heading_candidate'))
    return headings


def _bounded_section(rows,target,heading_start=None):
    """Clip at explicit line-start headings, including alphanumeric codes.

    The parser may combine a previous record's last layer and the new heading.
    Character spans, rather than region membership, define the section boundary.
    """
    headings=source_titles(target.text)
    if heading_start is not None:
        headings=[m for m in headings if m.start()==heading_start]
    if len(headings)!=1:return None
    heading=headings[0]
    at=next(i for i,r in enumerate(rows) if r.region_id==target.region_id)
    out=[]; stop=None
    for i,r in enumerate(rows[at:]):
        if any(getattr(r, name, None) != getattr(target, name, None)
               for name in ('layout_id', 'frame_id', 'column_key')):
            # Parser ancestry does not grant cross-column/page ownership.
            # No closing title in this scope means the section stays candidate.
            break
        start=heading.start() if i==0 else 0
        stops=sorted([m for m in source_titles(r.text)+list(SECTION_BREAK.finditer(r.text)) if i>0 or m.start()>start],key=lambda m:m.start())
        end=stops[0].start() if stops else len(r.text)
        if end>start:out.append((r,start,end,None,'explicit_section_candidate'))
        if stops:
            stop=(r,stops[0].start(),min(len(r.text),r.text.find('\n',stops[0].start()) if '\n' in r.text[stops[0].start():] else len(r.text)))
            break
    return out,stop


def _section_binding(index,target,sources,stop,heading_start=None):
    heading=next(m for m in source_titles(target.text) if heading_start is None or m.start()==heading_start)
    numbered=HEADING.match(target.text,heading.start())
    end=target.text.find('\n',heading.start())
    title=index.evidence(target,heading.start(),len(target.text) if end<0 else end)
    numbers=[]; diagnostics=[]; previous=None
    for region,start,end,_,_ in sources:
        if any(getattr(region, name, None) != getattr(target, name, None)
               for name in ('layout_id', 'frame_id', 'column_key')):
            diagnostics.append('SECTION_SOURCE_SCOPE_MISMATCH')
        numbers.extend(int(t['value']) for t in evidence_readings(index.evidence(region,start,end)) if t['kind']=='ordinal')
        if not region.parts:diagnostics.append('SECTION_SOURCE_GEOMETRY_UNAVAILABLE')
        if previous and previous.region_id!=region.region_id:
            heights=[p.bbox[3]-p.bbox[1] for p in previous.parts+region.parts if p.bbox[3]>p.bbox[1]]
            if heights and previous.bbox[1]-region.bbox[3]>median(heights)*6:
                diagnostics.append('SECTION_READING_GAP_REQUIRES_REVIEW')
        previous=region
    if not stop:diagnostics.append('SECTION_END_UNRESOLVED')
    if numbered and record_type(heading[0])=='layer_assembly' and (not numbers or numbers!=list(range(1,len(numbers)+1))):
        diagnostics.append('LAYER_NUMBER_SEQUENCE_UNRESOLVED')
    text='\n'.join(r.text[a:b] for r,a,b,_,_ in sources)
    references=bool(re.search(r'以下做法同|同.{0,16}第[0-9一二三四五六七八九十]|详(?:见|单体|二次)',text))
    if references:diagnostics.append('EXTERNAL_ASSEMBLY_REFERENCE_REQUIRES_READ')
    warnings=[]
    paragraph=False
    if numbered and not numbers and stop and 'LAYER_NUMBER_SEQUENCE_UNRESOLVED' in diagnostics and set(diagnostics) <= {'LAYER_NUMBER_SEQUENCE_UNRESOLVED', 'EXTERNAL_ASSEMBLY_REFERENCE_REQUIRES_READ'}:
        from cad_agent.analysis.source_binding_proofs import unnumbered_practice_paragraph, unread_dependency
        paragraph=unnumbered_practice_paragraph(text)
        if paragraph:
            diagnostics=[d for d in diagnostics if d!='LAYER_NUMBER_SEQUENCE_UNRESOLVED']
            warnings=['UNNUMBERED_SOURCE_PARAGRAPH']
            if unread_dependency(text) and not references:
                references=True
                diagnostics.append('EXTERNAL_ASSEMBLY_REFERENCE_REQUIRES_READ')
    blockers=[d for d in diagnostics if d!='EXTERNAL_ASSEMBLY_REFERENCE_REQUIRES_READ']
    excluded = [index.evidence(target, 0, heading.start())] if target.text[:heading.start()].strip() else []
    return dict(state='source_bound' if not blockers else 'candidate',basis='explicit_heading_bounded_source_paragraph' if paragraph else 'explicit_heading_bounded_numbered_section' if numbered else 'explicit_bounded_source_section',
                code=numbered[1]+numbered['code'] if numbered else None,title_evidence=title,
                record_type='source_section' if paragraph else record_type(heading[0]) if numbered else 'source_section',
                excluded_preceding_evidence=excluded,
                boundary_evidence=[index.evidence(*stop)] if stop else [],
                layer_numbers=numbers,layer_order='source_reading_order_only' if paragraph else 'explicit_source_numbering',
                layer_completeness='requires_reference_read' if references else 'bounded_source_paragraph' if paragraph else 'bounded_source_sequence' if not blockers else 'unverified',
                warnings=warnings,
                object_instance_state='unverified',engineering_applicability_state='unverified',
                diagnostics=sorted(set(diagnostics)))


@_cached('bindings',lambda target,band,sources:(target.layout_id,target.frame_id,tuple(band['bbox']),tuple(band['column_edges'])))
def _row_binding(index,target,band,sources):
    headers=local_assembly_headers(index,target,band)
    fields=[]
    for col in range(len(band['column_edges'])-1):
        parts=[(r,a,b) for r,a,b,c,_ in sources if c==col]
        header=next((h for h in headers['fields'] if h['column_index']==col),None)
        text='\n'.join(r.text[a:b] for r,a,b in parts)
        roles=header['roles'] if header else []
        fields.append(dict(column_index=col,text=text if set(roles)&{'code','name','ordinal','applicability'} else '',roles=roles,
            has_value=bool(text.strip()),
            source_spans=[dict(region_id=r.region_id,text_span=[a,b]) for r,a,b in parts],
            evidence=[index.evidence(r,a,b) for r,a,b in parts] if set(roles)&{'code','name','ordinal','applicability'} else [],
            header_evidence=header['evidence'] if header else [],value_state='observed' if text.strip() else 'unknown'))
    identities=[f for f in fields if set(f['roles'])&{'code','name'} and f['text'].strip()]
    if not identities:
        identities=[f for f in fields if 'applicability' in f['roles'] and len(f['text'].strip())<=160 and FAMILY.search(f['text'])]
    layers=[f for f in fields if set(f['roles'])&{'layers','requirements'} and f['has_value']]
    diagnostics=list(headers['diagnostics'])
    if not identities:diagnostics.append('ASSEMBLY_IDENTITY_FIELD_UNRESOLVED')
    if not layers:diagnostics.append('ASSEMBLY_LAYER_FIELD_UNRESOLVED')
    primary_markers=[]
    code_titles={m[0] for f in fields if 'code' in f['roles'] for m in re.finditer(r'(?m)^[^\n]{1,18}[0-9０-９]+[-—－]',f['text'])}
    if not identities:
        # Without trustworthy identity columns, distinct explicit titles inside
        # the envelope prove a group, never a single construction's ownership.
        code_titles.update(normalized_code(m[1]+m['code']) for r,a,b,_,_ in sources
                           for m in HEADING.finditer(r.text[a:b]))
    grouped=len(code_titles)>1
    if grouped:diagnostics.append('SUBRECORD_BOUNDARIES_REQUIRE_REVIEW')
    for field in layers:
        if 'layers' not in field['roles']:continue
        text='\n'.join(r.text[a:b] for r,a,b,c,_ in sources if c==field['column_index'])
        markers=[int(t['value']) for t in ordinals(text)]
        if markers.count(1)>1:
            grouped=True
            diagnostics.append('SUBRECORD_BOUNDARIES_REQUIRE_REVIEW')
        alternative=bool(primary_markers and re.search(r'改为|改用|替代|替换',text) and set(markers)<=set(primary_markers))
        if alternative:field['branch_state']='explicit_nonadditive_alternative'
        elif markers and markers!=list(range(1,len(markers)+1)):
            diagnostics.append('ROW_LAYER_SEQUENCE_REQUIRES_REVIEW')
        if markers and not primary_markers:primary_markers=markers
    if band['maximum_endpoint_gap']>.11:diagnostics.append('ROW_ENDPOINT_GAPS_REQUIRE_REVIEW')
    return dict(state='source_bound' if not diagnostics else 'candidate',basis='explicit_header_and_closed_source_row',
        record_type='table_group' if grouped else 'material_clause' if any('requirements' in f['roles'] for f in layers) else 'layer_assembly',
        fields=fields,headers=headers,identity_columns=[f['column_index'] for f in identities],
        layer_columns=[f['column_index'] for f in layers],
        layer_order='explicit_header_direction' if headers.get('order_evidence') else 'source_reading_order_only',
        order_evidence=headers.get('order_evidence',[]),
        object_instance_state='unverified',engineering_applicability_state='unverified',diagnostics=diagnostics)


def _ruled_catalog(index,target,band,binding):
    headers=binding['headers']
    if headers['state']!='source_bound':return []
    cols=binding['identity_columns']
    a,b,c,d=headers['table_bbox']
    selected={t['entity_id']:t for t in index.scene['texts'] if t['layout_id']==target.layout_id
              and b<=t['origin'][1]<headers['bbox'][1]
              and any(band['column_edges'][col]<=t['origin'][0]<band['column_edges'][col+1] for col in cols)}
    sources=[]
    for region in index.scope(target.layout_id,target.frame_id):
        parts=[p for p in region.parts if p.entity_id in selected]
        if not parts:continue
        # Keep source fragments separate; a paragraph can straddle two rows.
        for part in parts:
            lo,hi=part.text_span
            if sources and sources[-1][0].region_id==region.region_id and not region.text[sources[-1][2]:lo].strip():
                previous=sources.pop();sources.append((region,previous[1],hi,None,'table_identity_candidate'))
            else:sources.append((region,lo,hi,None,'table_identity_candidate'))
    return sorted(sources,key=lambda x:(-max(p.bbox[3] for p in x[0].parts if p.text_span[0]<x[2] and p.text_span[1]>x[1]),x[0].bbox[0]))


@_cached('local_codes',lambda target,band,binding:(target.layout_id,target.frame_id,
    tuple(binding['headers'].get('table_bbox',[])),tuple(band['column_edges']),tuple(binding['identity_columns'])))
def local_code_families(index,target,band,binding):
    """Infer a code prefix only from explicitly labelled cells in this table.

    No global W/L/屋/内 dictionary; conflicting local definitions stay unresolved.
    Evidence for the local mapping travels with the record.
    """
    headers=binding['headers']
    if headers['state']!='source_bound':return {}
    identity_cols=set(binding['identity_columns'])
    usage_cols={f['column_index'] for f in binding['fields'] if 'applicability' in f['roles']}
    area=band|{'bbox':[headers['table_bbox'][0],headers['table_bbox'][1],headers['table_bbox'][2],headers['bbox'][1]]}
    rows={};ys=sorted(headers['row_separators'])
    for region,a,b,col,_ in _source_spans(index,target,area):
        if col not in identity_cols|usage_cols:continue
        for part in region.parts:
            lo,hi=max(a,part.text_span[0]),min(b,part.text_span[1])
            if hi<=lo:continue
            y=(part.bbox[1]+part.bbox[3])/2
            lower=max((v for v in ys if v<=y),default=None)
            upper=min((v for v in ys if v>y),default=None)
            if lower is None or upper is None:continue
            rows.setdefault((lower,upper),[]).append((col,index.evidence(region,lo,hi)))
    definitions={}
    for values in rows.values():
        codes={m[1] for col,e in values if col in identity_cols
               for m in re.finditer(r'(?m)^\s*([A-Za-zＡ-Ｚａ-ｚ]{1,4}|[\u4e00-\u9fff]{1,3})\s*[0-9０-９]+\s*$',e['quote'])}
        for prefix in codes:
            candidates=[]
            for col,e in values:
                found=identities(e['quote'])
                for family in found:
                    if family.startswith(prefix) or family.endswith(prefix) or (prefix.isascii() and col in identity_cols):
                        candidates.append((family,e))
            definitions.setdefault(prefix,[]).extend(candidates)
    result={}
    for prefix,values in definitions.items():
        found={f for f,_ in values}
        roots=[f for f in found if all(v in {canonical(x) for x in expand(f)} for v in found)]
        if len(roots)==1:
            result[prefix]=dict(families=roots,evidence=list({e['evidence_id']:e for _,e in values}.values()),
                basis='same_table_explicit_identity_or_usage_cells')
    return result


def _reference_reads(index,target,sources):
    refs=[]
    pattern=re.compile(r'(?:同|详见)[^\n]{0,8}?(楼地面|屋面|外墙面|内墙面|外墙|内墙|顶棚|地面|墙面|踢脚|顶板)\s*([A-Za-z]?[0-9０-９]+[A-Za-z]?)')
    for region,lo,hi,_,_ in sources:
        for match in pattern.finditer(region.text[lo:hi]):
            code=match[1]+match[2]
            matches=[]
            for r in index.scope(target.layout_id,target.frame_id):
                headings=list(HEADING.finditer(r.text))
                for m in headings:
                    if m[1]+m['code']==code:
                        matches.append(_action('read_assembly',snapshot_id=index.snapshot,region_id=r.region_id,view='record',offset=0,limit=4,
                            **({'heading_start':m.start()} if len(headings)>1 else {})))
            refs.append(dict(code=code,state='candidate',evidence=index.evidence(region,lo+match.start(),lo+match.end()),
                             next_actions=matches[:8],target_count=len(matches),has_more=len(matches)>8,
                             note='显式引用需独立补读；不自动展开或与当前层次叠加。'))
    return refs


def read(index, args):
    if args.region_id not in index.by_id:
        raise ValueError('来源区域不属于当前图纸。')
    target = index.by_id[args.region_id]
    heading_start=getattr(args,'heading_start',None)
    if heading_start is not None and not any(m.start()==heading_start for m in source_titles(target.text)):
        raise ValueError('标题起点必须对应原文中实际存在的完整标题。')
    anchor=getattr(args,'anchor_entity_id',None)
    geometry_target=target
    if anchor:
        parts=[p for p in target.parts if p.entity_id==anchor]
        if not parts:raise ValueError('读取锚点必须属于来源区域。')
        geometry_target=target.model_copy(update={'entity_ids':[anchor],
            'bbox':[min(p.bbox[0] for p in parts),min(p.bbox[1] for p in parts),max(p.bbox[2] for p in parts),max(p.bbox[3] for p in parts)]})
    catalog=_catalog(index,target,heading_start)
    binding=None
    band,diagnostics = (None,[]) if args.view=='catalog' and catalog else ruled_band(index,geometry_target)
    if not anchor and heading_start is not None and len(source_titles(target.text))>1:
        band,diagnostics=None,[]
    if not catalog and band:
        row_sources=_source_spans(index,target,band)
        binding=_row_binding(index,target,band,row_sources)
        catalog=_ruled_catalog(index,target,band,binding)
    if args.view=='catalog':
        sources=catalog
        basis='explicit_heading_catalog' if catalog and catalog[0][4]=='explicit_heading_candidate' else 'source_table_identity_catalog'
        binding=None
    elif band:
        sources = row_sources if binding is not None else _source_spans(index,target,band)
        basis = 'source_ruled_band'
        if binding is None:binding=_row_binding(index,target,band,sources)
    else:
        rows = index.scope(target.layout_id,target.frame_id,target.region_id)
        basis = 'parser_descendants'
        if len(rows) == 1 or (source_titles(target.text) and target.column_key is not None):
            rows = sorted([r for r in index.scope(target.layout_id,target.frame_id) if r.column_key == target.column_key],
                          key=lambda r:(-r.bbox[3],r.bbox[0],r.region_id))
            # Keep the remaining column pageable; never wrap its bottom to its top.
            basis = 'same_column_neighbors'
            at = next(i for i,r in enumerate(rows) if r.region_id == target.region_id)
            rows = rows[at:]
        sources = [(r,0,len(r.text),None,'target' if r.region_id == target.region_id else basis) for r in rows]
        section=_bounded_section(rows,target,heading_start)
        if section:
            sources,stop=section
            basis='explicit_section_candidate'
            binding=_section_binding(index,target,sources,stop,heading_start)
            # A bounded numbered section does not need a ruled-table envelope.
            if binding['state']=='source_bound':
                diagnostics=[d for d in diagnostics if d not in ('ROW_ENVELOPE_UNRESOLVED','ROW_GEOMETRY_UNAVAILABLE')]
        else:
            # An unresolved neighbor queue is not a construction record.
            sources = [(target, 0, len(target.text), None, 'target')]
            diagnostics.append('SOURCE_OWNERSHIP_UNRESOLVED')
    if binding and band and args.view=='record':
        from cad_agent.analysis.source_binding_proofs import closed_title_and_layers
        proof=closed_title_and_layers(index,target,sources,band,binding,diagnostics,heading_start)
        if proof:
            binding=deepcopy(binding)
            binding.update(state='source_bound',basis=proof['basis'],binding_proof=proof,
                warnings=list(binding['diagnostics'])+(['UNCLASSIFIED_ROW_COLUMNS'] if proof['untyped_columns'] else []),
                diagnostics=['EXTERNAL_ASSEMBLY_REFERENCE_REQUIRES_READ'] if proof['requires_reference_read'] else [],layer_numbers=proof['layer_numbers'],
                layer_completeness='requires_reference_read' if proof['requires_reference_read'] else 'bounded_source_sequence')
    if binding and band and binding.get('state')=='source_bound':
        local=local_code_families(index,target,band,binding)
        identity='\n'.join(f['text'] for f in binding['fields'] if f['column_index'] in binding['identity_columns'])
        prefixes={m[1] for m in re.finditer(r'(?m)^\s*([A-Za-zＡ-Ｚａ-ｚ]{1,4}|[\u4e00-\u9fff]{1,3})\s*[0-9０-９]+\s*$',identity)}
        mappings=[local[p] for p in prefixes if p in local]
        binding['local_identity_families']=sorted({f for m in mappings for f in m['families']})
        binding['local_identity_evidence']=list({e['evidence_id']:e for m in mappings for e in m['evidence']}.values())
    if binding:diagnostics += binding['diagnostics']
    references=_reference_reads(index,target,sources) if args.view=='record' else []
    # Character paging shares the same executable cursor as row paging.
    segments = [(r,start,min(end,start+1800),col,rel,end) for r,lo,end,col,rel in sources for start in range(lo,end,1800)]
    page = segments[args.offset:args.offset+args.limit]
    items = []
    for i,(r,start,end,col,rel,scope_end) in enumerate(page,args.offset):
        evidence = index.evidence(r,start,end)
        fragments=_fragments(index,r,start,end)
        for fragment in fragments:
            fragment['source_binding_state']=binding['state'] if binding else 'unresolved'
        items.append(dict(region_id=r.region_id,order=i,kind=r.kind,column_index=col,
            source_binding_state=binding['state'] if binding else 'unresolved',
            source_binding_basis=binding['basis'] if binding else None,
            source_field_roles=next((f['roles'] for f in binding.get('fields',[]) if f['column_index']==col),[]) if binding else [],
            relation=rel,relation_state='candidate',
            source_ownership_state=binding['state'] if binding else 'unresolved',
            ownership_state='source_bound' if binding and binding['state']=='source_bound' else 'unresolved',
            object_instance_state='unverified',engineering_applicability_state='unverified',
            text_span=[start,end],evidence=evidence,fragments=fragments,
            source_region_partial=end < len(r.text) or start > 0,
            truncated=end < scope_end,next_start=end if end < scope_end else None))
    more = args.offset+args.limit < len(segments)
    next_offset = args.offset+args.limit if more else None
    actions = [_action('read_assembly',region_id=target.region_id,view=args.view,offset=next_offset,limit=args.limit)] if more else []
    if args.view=='record' and catalog:
        actions.append(_action('read_assembly',region_id=target.region_id,view='catalog',offset=0,limit=args.limit))
    if args.view=='catalog':
        actions += [_action('read_assembly',region_id=r.region_id,view='record',offset=0,limit=args.limit,
            **({'heading_start':start} if len(list(HEADING.finditer(r.text)))>1 else {})) for r,start,_,_,_,_ in page]
    table_candidates = []
    ids = set(target.entity_ids)
    # A local source read must not implicitly build every table in a large DXF.
    # Reuse an existing catalog, or return an explicit, schema-valid discovery call.
    cached_tables = getattr(index,'_tables',None)
    if cached_tables is None and args.view=='record' and (not binding or binding['state']!='source_bound'):
        actions.append(_action('get_drawing_overview',view='tables',layout_id=target.layout_id,
                               frame_id=target.frame_id,offset=0,limit=5))
    for table in cached_tables or []:
        if table['layout_id'] != target.layout_id: continue
        matched = [row['row_index'] for row in table['rows'] if any(ids.intersection(c['entity_ids']) for c in row['cells'])]
        if matched:
            call = _action('read_table_semantics',table_id=table['table_id'],row_offset=matched[0],row_limit=3)
            table_candidates.append(dict(table_id=table['table_id'],matched_rows=matched,next_action=call))
            actions.append(call)
    for reference in references:actions.extend(reference['next_actions'])
    repair_reads=[]
    if binding and binding['state']=='candidate' and sources:
        # Inspect the unresolved boundary, not merely the already-read title.
        for diagnostic in binding['diagnostics']:
            if diagnostic=='SECTION_END_UNRESOLVED':
                r,start,_,_,_=sources[-1]
                action=_action('read_source_context',region_id=r.region_id,start=start,neighbors=2)
            elif diagnostic in ('HEADER_UNRESOLVED','HEADER_SIDE_RAILS_UNRESOLVED'):
                action=_action('get_drawing_overview',view='tables',layout_id=target.layout_id,
                               frame_id=target.frame_id,offset=0,limit=5)
            else:
                r,start,_,_,_=sources[0]
                if diagnostic=='LAYER_NUMBER_SEQUENCE_UNRESOLVED':
                    expected=1
                    for candidate,lo,hi,_,_ in sources:
                        mismatch=False
                        for m in re.finditer(r'^\s*(\d+)[、．.]',candidate.text[lo:hi],re.MULTILINE):
                            if int(m[1])!=expected:
                                r,start=candidate,lo+m.start();mismatch=True;break
                            expected+=1
                        if mismatch:break
                action=_action('read_source_context',region_id=r.region_id,start=start,neighbors=1)
            repair_reads.append(dict(reason=diagnostic,next_action=action))
            actions.append(action)
    unique_actions=[]
    for action in actions:
        if action not in unique_actions:unique_actions.append(action)
    actions=unique_actions
    for action in actions:
        if action['tool']=='read_assembly':
            action['arguments']['snapshot_id']=index.snapshot
            if action['arguments'].get('region_id')==target.region_id and action['arguments'].get('view')==args.view and heading_start is not None:
                action['arguments']['heading_start']=heading_start
    # Ownership/units cannot be filled merely because all candidate rows were read.
    checks = dict(source_sequence_exhausted=not more and args.offset < len(segments),
        source_segment_count=len(segments),returned_segment_count=len(items),
        complete_in_this_response=not more and args.offset==0,
        earlier_segments_not_in_page=args.offset,unread_after_page=max(0,len(segments)-args.offset-len(items)),
        source_ownership_state=binding['state'] if binding else 'unresolved',
        ownership='source_bound' if binding and binding['state']=='source_bound' else 'unresolved',
        object_instance_state='unverified',engineering_applicability_state='unverified',
        source_field_binding=binding['state'] if binding else 'unresolved',
        layer_order=binding.get('layer_order','source_reading_order_only') if binding else 'source_reading_order_only',
        units='per_fragment_explicit_only',object_completeness='unverified')
    from cad_agent.analysis.default_units import candidates as unit_clauses, declaration
    default_unit_clauses = [dict(unit=declaration(row.text), evidence=index.evidence(row),
        applicability='source_declared_linear_dimensions_only',
        excludes=['elevations', 'coordinates', 'areas', 'material_grades'])
        for row in unit_clauses(index, target)[:3]]
    target_headings=source_titles(target.text)
    title_start=heading_start if heading_start is not None else target_headings[0].start() if len(target_headings)==1 else 0
    title_end=target.text.find('\n',title_start) if target_headings else len(target.text)
    if title_end<0:title_end=len(target.text)
    from cad_agent.analysis.source_sections import context as section_context, narrowed_families
    chapter=section_context(index,target,title_start) if hasattr(index,'regions') else None
    return dict(snapshot_id=index.snapshot,target_region_id=target.region_id,heading_start=heading_start,anchor_entity_id=anchor,version=VERSION,
        section_context=chapter,section_families=narrowed_families(chapter,target.text[title_start:title_end]),
        record_type=(binding or {}).get('record_type',record_type(target.text[title_start:title_end])),
        source_scope=dict(layout_id=target.layout_id, frame_id=target.frame_id, frame_name=getattr(target, "frame_name", ""), basis="parser_frame_metadata_not_engineering_identity"),
        record_id=stable_id('assembly',index.snapshot,target.region_id,VERSION,*([str(heading_start)] if heading_start is not None else []),*([str(band['bbox'])] if band else [])),view=args.view,relation_basis=basis,
        related_record_count=len(catalog),
        title_evidence=index.evidence(target,title_start,min(title_end,title_start+1800)),row_structure=band,
        unit_candidates=read_unit_candidates(index,target),
        default_unit_clauses=default_unit_clauses,
        source_binding=binding,reference_reads=references,repair_reads=repair_reads,items=items,table_candidates=table_candidates,table_catalog_state='loaded' if cached_tables is not None else 'not_loaded',
        total=len(segments),offset=args.offset,
        has_more=more,next_offset=next_offset,next_actions=actions,checks=checks,
        completeness='candidate_sequence',diagnostics=sorted(set(diagnostics)),
        note='source_bound仅表示有可追溯依据的原文归属；依据见source_binding，可能为标题及结束边界、表头与闭合行，或明确标题与连续层次的闭合行。未编号段落不声称是完整分层做法；不确认对象实例或工程适用性。层号为原文顺序，括号替代不得叠加，缺单位不默认毫米，空白不继承。')
