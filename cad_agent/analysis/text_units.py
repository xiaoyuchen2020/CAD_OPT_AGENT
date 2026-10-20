"""Derived multi-entity unit readings, never replacement source quotations.

Scene text placement is source-derived, but estimated glyph boxes and absent
native style/owner metadata keep the association a candidate, not a fact.
"""
import re
import math

from cad_agent.contracts import stable_id

VERSION = 'source-superscript-v2-native-audit'


def native_text_metadata(index, text):
    """Verify a root TEXT against the immutable DXF, not scene-only metadata.

    Nested/transformed text needs an instance-transform audit and stays
    unresolved here. The cache key includes the source file stamp.
    """
    if not getattr(index,'w',None) or len(text.get('entity_path',[]))!=1:
        return None
    from cad_agent.cache import workspace_cache, file_stamp
    from cad_agent.geometry.source import source_text_metadata
    from ezdxf.lldxf.const import DXFError
    path=index.w.store.artifact(index.snapshot,'source.dxf')
    try:
        stamp=file_stamp(path)
        cache=workspace_cache(index.w,'_unit_text_native',max_entries=2)
        handle=text['entity_path'][0]
        metadata=cache.get_or_build((VERSION,index.snapshot,stamp,handle),
            lambda:source_text_metadata(index.w,index.snapshot,[handle]),lambda _:2048)
        if file_stamp(path)!=stamp:return None
        native=metadata.get(text['entity_path'][0])
        if not native or stable_id('ent',index.snapshot,text['layout_id'],'/'.join(text['entity_path']))!=text['entity_id']:
            return None
        if any(native[k]!=text.get(k) for k in ('text','layout_id','layer')):
            return None
        if not all(math.isclose(native['origin'][i],text['origin'][i],abs_tol=1e-7) for i in (0,1)):
            return None
        if any(not math.isclose(native[k],text.get(k,0),rel_tol=1e-9,abs_tol=1e-7) for k in ('height','rotation')):
            return None
        # Existing scene native attributes, when present, may not conflict.
        if any(text.get(k) and text[k]!=native[k] for k in ('owner','style')):
            return None
        return native
    except (OSError,ValueError,TypeError,AttributeError,DXFError):
        return None


def read_unit_candidates(index, target):
    """Read cross-region superscripts adjacent to a target's terminal unit.

    Bounds are deliberately narrow: horizontal TEXT, matching layout, layer
    and INSERT ancestry, a smaller elevated 2/3 beside an estimated unit end.
    Ambiguity is retained. A recovered m² never establishes area ownership.
    """
    if not re.search(r'\d\s*(?:mm|cm|m)\s*(?:$|\n)',target.text):
        return []
    ids = set(getattr(target, 'entity_ids', None) or [])
    if not ids:
        # Legacy/lightweight source rows may not bind native entities. Keep
        # their unit incomplete; proximity alone cannot create provenance.
        return []
    anchors = [t for t in index.scene['texts'] if t['entity_id'] in ids
               and t.get('kind') == 'TEXT' and re.search(r'\d\s*(?:mm|cm|m)\s*$', t['text'])]
    results = []
    for anchor in anchors[:12]:
        h = anchor.get('height', 0)
        if h <= 0 or abs(anchor.get('rotation', 0)) > .01:
            continue
        for other in index.scene['texts']:
            if other['entity_id'] == anchor['entity_id'] or other.get('kind') != 'TEXT' or other['text'].strip() not in ('2','3'):
                continue
            if (other.get('layout_id') != anchor.get('layout_id') or other.get('layer') != anchor.get('layer')
                    or other.get('entity_path', [])[:-1] != anchor.get('entity_path', [])[:-1]
                    or abs(other.get('rotation',0)-anchor.get('rotation',0)) > .01):
                continue
            ratio = other.get('height',0)/h
            dx = (other['origin'][0]-anchor['bbox'][2])/h
            dy = (other['origin'][1]-anchor['origin'][1])/h
            if not (.3 <= ratio <= .7 and -.9 <= dx <= .65 and .35 <= dy <= 1.2):
                continue
            # Any available native metadata must agree; missing values are
            # explicitly unverified instead of being treated as equality.
            metadata = {key: ('matched' if anchor.get(key) and other.get(key) == anchor[key] else 'unverified')
                        for key in ('owner','style')}
            if any(anchor.get(k) and other.get(k) and anchor[k] != other[k] for k in metadata):
                continue
            sources = [index.entity_evidence(t['entity_id']) for t in (anchor,other)]
            if not all(sources):
                continue
            native_anchor,native_other=(native_text_metadata(index,t) for t in (anchor,other))
            native_pair=bool(native_anchor and native_other)
            if native_pair and any(native_anchor[k]!=native_other[k] for k in ('owner','style')):
                continue
            if native_pair:
                metadata={k:'matched_native_dxf' for k in ('owner','style')}
            exp = other['text'].strip()
            unit = re.search(r'(mm|cm|m)\s*$',anchor['text'])[1] + exp
            results.append(dict(candidate_id=stable_id('unit',index.snapshot,VERSION,anchor['entity_id'],other['entity_id']),
                version=VERSION,state='candidate',normalized_text_candidate=anchor['text'].rstrip()+('²' if exp=='2' else '³'),
                native_relation_evidence=[dict(entity_id=t['entity_id'],entity_path=t['entity_path'],**n)
                    for t,n in ((anchor,native_anchor),(other,native_other)) if n],
                normalized_unit_candidate=unit,source_entity_ids=[anchor['entity_id'],other['entity_id']],
                source_evidence=sources[0]+sources[1],relation='smaller_elevated_text_at_unit_end',
                relation_checks=dict(same_layout=True,same_layer=True,same_insert_ancestry=True,
                    height_ratio=ratio,baseline_offset_in_heights=dy,end_offset_in_heights=dx,**metadata),
                area_basis='independent_source_annotation' if exp=='2' else 'unclassified_annotation',
                object_identity='unconfirmed',measurement_comparability='unconfirmed',
                diagnostics=['SOURCE_RELATION_CANDIDATE','ESTIMATED_TEXT_EXTENT']+
                    (['NATIVE_STYLE_OWNER_UNVERIFIED'] if 'unverified' in metadata.values() else [])))
    for result in results:
        result['ambiguous'] = sum(r['source_entity_ids'][0] == result['source_entity_ids'][0] for r in results) > 1
        # One exponent cannot be confidently assigned to two competing units.
        exponent=next(t for t in index.scene['texts'] if t['entity_id']==result['source_entity_ids'][1])
        competitors=[]
        for t in index.scene['texts']:
            height=t.get('height',0)
            if t.get('kind')!='TEXT' or not height or not re.search(r'\d\s*(?:mm|cm|m)\s*$',t['text']):continue
            if any(t.get(k)!=exponent.get(k) for k in ('layout_id','layer')):continue
            if t.get('entity_path',[])[:-1]!=exponent.get('entity_path',[])[:-1]:continue
            if abs(t.get('rotation',0))>.01 or abs(exponent.get('rotation',0)-t.get('rotation',0))>.01:continue
            if any(t.get(k) and exponent.get(k) and t[k]!=exponent[k] for k in ('owner','style')):continue
            if (.3<=exponent.get('height',0)/height<=.7 and
                -.9<=(exponent['origin'][0]-t['bbox'][2])/height<=.65 and
                .35<=(exponent['origin'][1]-t['origin'][1])/height<=1.2):competitors.append(t['entity_id'])
        result['competing_anchor_count']=len(set(competitors))
        result['competing_anchor_entity_ids']=sorted(set(competitors))
        result['ambiguous']=result['ambiguous'] or result['competing_anchor_count']>1
        result['unit_resolution']='source_supported_reading' if len(result['native_relation_evidence'])==2 and not result['ambiguous'] else 'candidate'
        if result['ambiguous']:result['diagnostics'].append('AMBIGUOUS_SUPERSCRIPT_RELATION')
    return results[:24]


def read_elevation_annotation(target):
    """Read literal elevation tokens and surface hints without pairing points.

    A signed number alone carries neither datum, unit, surface nor engineering
    role. This intentionally never subtracts unrelated nearby annotations.
    """
    text=target.text
    matches=list(re.finditer(r'(?<![\d.])(?:±|[+−-])\s*\d+\.\d{2,3}(?!\d)',text))
    if not matches or not getattr(target,'entity_ids',None):return None
    surfaces=[]
    for pattern,name in ((r'结构(?:面|板顶|标高)|板顶结构','structure'),
                         (r'建筑完成面|完成面|建筑标高','finished_surface'),
                         (r'梁底','beam_soffit'),(r'板底','slab_soffit')):
        if re.search(pattern,text):surfaces.append(name)
    return dict(state='candidate',region_id=target.region_id,source_entity_ids=list(target.entity_ids),
        quote=target.text,values=[dict(raw=m.group(),value=float(m.group().replace('±','').replace('−','-').replace(' ','')),
            text_span=[m.start(),m.end()]) for m in matches],
        unit='unknown',datum='unresolved',surface=surfaces[0] if len(surfaces)==1 else 'unresolved',
        surface_basis='same_source_text' if len(surfaces)==1 else 'unresolved',
        control_point_binding='unresolved',applicable_scope='source_annotation_only',
        pairability='unresolved',derived_height=None,
        diagnostics=['ELEVATION_DATUM_UNRESOLVED','ELEVATION_UNIT_UNRESOLVED','ELEVATION_CONTROL_POINT_UNRESOLVED']+
                    ([] if len(surfaces)==1 else ['ELEVATION_SURFACE_UNRESOLVED']))
