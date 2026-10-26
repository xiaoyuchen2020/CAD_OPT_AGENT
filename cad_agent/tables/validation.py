"""Independent source/partition checks; model or detector scores grant no credit."""
from shapely.geometry import Polygon, LineString, Point
from shapely.ops import unary_union


def validate(document,index):
    issues=[];seen=set();shapes=[]
    text_by_id={t['entity_id']:t for t in index.scene['texts']}
    source_lines={l['entity_id']:l for l in index.scene['lines']}
    for cell in document['cells']:
        shape=Polygon(cell['boundary']);shapes.append(shape)
        if not shape.is_valid or shape.area<=0:issues.append('INVALID_CELL_SHAPE')
        for f in cell['fragments']:
            eid=f['entity_id']
            if eid in seen:issues.append('DUPLICATE_TEXT_ASSIGNMENT')
            seen.add(eid);source=text_by_id.get(eid)
            if not source or source['text']!=f['raw_text']:issues.append('SOURCE_TEXT_MISMATCH')
            if source:
                from .topology import transform_point
                tr=document['transform'];x,y=transform_point(source['origin'],tr['origin'],tr['angle'])
                a,b,z,w=cell['local_bbox'];tol=tr['tolerance']
                margin=tr['height']*.15 if f.get('assignment_basis')=='near_outer_edge' else tol
                if not (a-margin<=x<=z+margin and b-tol<=y<=w+tol):issues.append('TEXT_CELL_ASSIGNMENT_MISMATCH')
            for e in f['evidence']:
                region=index.by_id.get(e['region_id']);a,b=e['text_span']
                if not region or region.text[a:b]!=e['quote'] or eid not in e['entity_ids']:issues.append('SOURCE_SPAN_MISMATCH')
        if document['source_kind'] in ('ruled','native_table'):
            corners=cell['boundary'];tol=document['transform']['tolerance']*2
            if len(corners)!=4 or len(cell['edge_sources'])!=4:issues.append('CELL_EDGE_COUNT_MISMATCH')
            for edge,ids in zip(zip(corners,corners[1:]+corners[:1]),cell['edge_sources']):
                line=LineString(edge);segments=[LineString(source_lines[eid]['points']) for eid in ids if eid in source_lines]
                if not segments or line.difference(unary_union(segments).buffer(tol)).length>tol*2:issues.append('UNSUPPORTED_CELL_EDGE')
    envelope=Polygon(document['boundary']);tol=document['transform']['tolerance']
    if shapes:
        union=unary_union(shapes)
        if sum(s.area for s in shapes)-union.area>tol*envelope.length:issues.append('OVERLAPPING_CELLS')
        if document['source_kind'] in ('ruled','native_table') and envelope.symmetric_difference(union).area>tol*envelope.length:issues.append('CELL_PARTITION_GAP')
    if document['source_kind'] in ('ruled','native_table'):
        xs=sorted({x for c in document['cells'] for x in (c['local_bbox'][0],c['local_bbox'][2])})
        ys=sorted({y for c in document['cells'] for y in (c['local_bbox'][1],c['local_bbox'][3])},reverse=True)
        for c in document['cells']:
            a,b,z,w=c['local_bbox']
            if (c['column'],c['row'],c['column_span'],c['row_span'])!=(xs.index(a),ys.index(w),xs.index(z)-xs.index(a),ys.index(b)-ys.index(w)):
                issues.append('CELL_GRID_INDEX_MISMATCH')
    expected=set(document['source_inventory'])
    if document['source_kind'] in ('ruled','native_table'):
        from .topology import transform_point
        tr=document['transform'];boxes=[c['local_bbox'] for c in document['cells']]
        a,b,z,w=min(c[0] for c in boxes),min(c[1] for c in boxes),max(c[2] for c in boxes),max(c[3] for c in boxes)
        def in_source_scope(t):
            x,y=transform_point(t['origin'],tr['origin'],tr['angle'])
            return a-tr['height']*.15<=x<=z+tr['height']*.15 and b-tr['tolerance']<=y<=w+tr['tolerance']
        independent={eid for eid,t in text_by_id.items() if t['layout_id']==document['layout_id']
            and list(t.get('entity_path',[])[:-1])==document['instance_path'] and in_source_scope(t)}
        if independent-expected:issues.append('SOURCE_INVENTORY_INCOMPLETE')
    missing=expected-seen-set(document['unassigned_entity_ids'])
    if missing:issues.append('UNACCOUNTED_SOURCE_TEXT')
    if document['unassigned_entity_ids']:issues.append('UNASSIGNED_SOURCE_TEXT')
    nodes={n['node_id']:n for n in document['nodes']}
    cell_ids={c['cell_id'] for c in document['cells']}
    for field in document['fields']:
        if field['cell_id'] not in cell_ids:issues.append('FIELD_CELL_MISSING')
        if field['binding_state']=='source_bound':
            if not field['evidence'] or not field['label_evidence']:issues.append('FIELD_SOURCE_MISSING')
            from cad_agent.cad.numbers import numeric_tokens, source_contains_number
            for token in numeric_tokens(field['value']):
                if not any(source_contains_number(e,token['value']) for e in field['evidence']):issues.append('FIELD_NUMBER_UNSUPPORTED')
    for node in nodes.values():
        path=set();parent=node
        while parent.get('parent_id'):
            if parent['node_id'] in path:issues.append('CYCLIC_TABLE_STRUCTURE');break
            path.add(parent['node_id']);parent=nodes.get(parent['parent_id'])
            if parent is None:issues.append('ORPHAN_TABLE_NODE');break
    return sorted(set(issues))
