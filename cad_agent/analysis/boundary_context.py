"""Final delivery uses reviewed boundaries, not superseded candidate menus.

Full observations remain in the session/evidence store. Only the final model
packet is projected; selections, per-item uncertainty and provenance stay.
"""
from copy import deepcopy


def numeric_evidence(value):
    """Model-authored choice rationales cannot become numeric evidence by echo."""
    if isinstance(value,list):return [numeric_evidence(v) for v in value]
    if not isinstance(value,dict):return value
    return {k:numeric_evidence(v) for k,v in value.items()
            if k not in ('model_choice','decision','instruction')}


def observation_payload(value):
    """Keep conclusions and their IDs, not duplicate source/coordinate payloads."""
    if isinstance(value,list):return [observation_payload(v) for v in value]
    if not isinstance(value,dict):return value
    if 'evidence_id' in value and 'quote' in value:
        return {'evidence_id':value['evidence_id']}
    return {k:observation_payload(v) for k,v in value.items()
            if k not in ('scope_evidence','label_evidence','scope_choices','scope_diagnostics','instruction','note','identity_note','source_classification')}


def delivery_observations(observations):
    values=list(observations.values())
    reviewed=[o for o in values if o['tool']=='select_room_boundaries' and o['data'].get('boundary_review_complete')]
    # Images uniquely identify a candidate menu. Do not hide another unfinished review.
    spatial_reviewed={o['data'].get('parent_review_id') for o in values if o['tool'] in ('review_space_connections','review_discovered_spaces')}
    prior_spatial_reviews={o['data'].get('prior_review_id') for o in values if o['tool']=='review_discovered_spaces'}
    images={i for o in reviewed for i in o['data'].get('image_ids',[])}
    result=[]
    for o in values:
        if o['tool']=='review_discovered_spaces' and o['data'].get('review_id') in prior_spatial_reviews:continue
        if o['tool'] in ('prepare_space_connections','prepare_discovery_review') and o['data'].get('review_id') in spatial_reviewed:continue
        if o['tool']=='prepare_room_boundaries' and set(o['data'].get('image_ids',[]))<=images:continue
        if o['tool']=='select_room_boundaries':
            o=deepcopy(o)
            for row in o['data'].get('items',[]):
                row['selected_geometry']=[{k:c[k] for k in ('candidate_id','kind','eligible','gap_count','issues') if k in c}
                                           for c in row.pop('candidates',[])]
        if o['tool'] in ('analyze_room_batch','select_room_boundaries','resolve_room_units'):
            o=deepcopy(o)
            o['data']=observation_payload(o['data'])
        result.append(o)
    return result


def synthesis_observations(observations, final=False):
    """Keep source references and selected values without repeated geometry menus."""
    result=[]
    reviewed=any(o['tool']=='select_room_boundaries' for o in observations.values())
    for o in delivery_observations(observations):
        name=o['tool'];d=deepcopy(o['data'])
        if final:
            # Every observation has already been checked against this snapshot;
            # record_id/observation_id resolve its full provenance in the UI.
            for k in ('snapshot_id','source_sha256','component_version','coordinate_space','evidence_url','claim_contract'):
                d.pop(k,None)
        if reviewed and name in ('prepare_room_boundaries','analyze_room_batch','resolve_room_units'):continue
        if name in ('query_entities','locate_space_labels'):
            if d.get('scope'):d['scope']={k:v for k,v in d['scope'].items() if k in ('frame_id','name','bbox','layout_id','method')}
            d['items']=[{k:v for k,v in i.items() if k in ('text','source_region_ids','layout_id','bbox','table_candidate_ids')} for i in d.get('items',[])]
        if name=='get_drawing_context':
            d={k:d[k] for k in ('record_id','view','total','has_more','offset','diagnostics','items') if k in d}
            d['items']=[{k:i[k] for k in ('object_id','title','name','title_source','layout_id','bbox','title_bbox','frame_ids','boundary_basis','diagnostics','model_geometry_scope','model_viewport_candidates') if k in i} for i in d.get('items',[])]
            if final:
                for item in d['items']:
                    if item.get('title_source'):
                        item['title_source']={k:item['title_source'][k] for k in ('region_id','text') if k in item['title_source']}
                items=d.pop('items');fields=list(dict.fromkeys(k for i in items for k in i))
                d['context_fields']=fields
                d['context_rows']=[[i.get(k) for k in fields] for i in items]
        if name=='select_room_boundaries':
            d={k:d[k] for k in ('batch_id','boundary_review_complete','image_ids','measurement') if k in d}
        if name in ('prepare_space_connections','review_space_connections','prepare_discovery_review','review_discovered_spaces'):
            d.pop('highlight_action',None);d.pop('images',None)
            for row in d.get('items',[]):
                for key in ('source_entity_ids','overlays','boundary','probe'):row.pop(key,None)
                if final:
                    row.pop('barriers',None);row.pop('openings',None);row.pop('crop_bbox',None)
        if name=='render_region':
            d={k:d[k] for k in ('image_id','image_sha256','layout_id','bbox','render_truncated','evidence_url') if k in d}
        if name=='discover_rooms':
            d.pop('source_diagnostics',None)
            d['items']=[{k:i[k] for k in ('candidate_id','candidate_role','candidate_shell_area_m2','status','bbox','boundary_support','diagnostics','text_regions') if k in i} for i in d.get('items',[])]
        if name=='analyze_measurements':
            d.pop('values',None) # ordered contains the same typed refs for selected values
            d.pop('highlight_action',None)
        if name=='read_dimensions' or (name=='get_component_evidence' and d.get('view')=='dimensions'):
            # A table keeps every read dimension and exact numeric/text value,
            # without repeating a large field schema for each row. The complete
            # artifact (including endpoint association candidates) remains stored.
            fields=('object_id','dimension_type','bbox','text_override','text_state',
                    'rendered_texts','actual_measurement','stored_measurement','style',
                    'geometry_recomputed','geometry_length_m','definition_points',
                    'dimension_line_point','applied_unit','association_status','diagnostics')
            d['dimension_fields']=list(fields)
            d['dimension_rows']=[[i.get(k) for k in fields] for i in d.pop('items',[])]
        if name=='query_building_relations' or (name=='get_component_evidence' and d.get('view') in ('rooms','walls','openings','relations')):
            for item in d.get('items',[]):
                if final:
                    # No more tools are allowed in final synthesis. Object IDs
                    # still resolve the complete artifact; repeating every raw
                    # constituent entity ID adds no answer evidence here.
                    item.pop('entity_ids',None)
                    item.pop('faces',None)
                    item.pop('bbox',None)
                    item.pop('wall_ids',None)
                    item.pop('room_ids',None) # per-side room IDs below are more precise
                    item['sides']=[{k:s[k] for k in ('side','room_ids','status') if k in s} for s in item.get('sides',[])]
                detail=item.pop('classification_evidence',None)
                if detail is not None:
                    item['classification_basis']={k:detail[k] for k in ('method','labels') if k in detail}
                    if final:
                        item['classification_basis'].pop('method',None)
                        item['classification_basis']['labels']=[{k:v[k] for k in ('kind','text','association','nominal_width_hint_m') if k in v} for v in detail.get('labels',[])]
                    item['classification_detail_in_artifact']=True
            items=[compact_geometry(i) for i in d.pop('items',[])]
            fields=list(dict.fromkeys(k for i in items for k in i))
            common={k:items[0][k] for k in fields if all(k in i and i[k]==items[0].get(k) for i in items)} if items else {}
            d['component_defaults']=common
            fields=[k for k in fields if k not in common]
            d['component_fields']=fields
            d['component_rows']=[[i.get(k) for k in fields] for i in items]
        result.append(dict(observation_id=o['observation_id'],tool=name,data=compact_geometry(d)))
    return result


def compact_geometry(value):
    """Coordinates remain in immutable artifacts; keep identities and measured fields."""
    if isinstance(value,list):return [compact_geometry(v) for v in value]
    if not isinstance(value,dict):return value
    omitted={'boundary','holes','edge_evidence','native','paths','entity_path','model_choice',
             'wcs_to_pixel','pixel_to_wcs','source_entity_ids','selected_geometry','source_parts','target_candidates'}
    out={k:compact_geometry(v) for k,v in value.items() if k not in omitted}
    for key in ('diagnostics','source_diagnostics'):
        if isinstance(out.get(key),list):
            # Entity-specific repetition is in the source artifact. Preserve
            # unique diagnostic types and the original count, not a false empty.
            values=out[key]
            kinds=[v.get('code',str(v)) if isinstance(v,dict) else str(v) for v in values]
            out[key]=list(dict.fromkeys(kinds))
            out[key+'_count']=value.get(key+'_count',len(values))
    if 'scale_candidates' in out:
        scales=out['scale_candidates']
        out['scale_candidates']=[next(s for s in scales if s['value']==v) for v in dict.fromkeys(s['value'] for s in scales)]
        out['scale_source_count']=len(scales)
    return out
