"""Opt-in adapter from existing CAD geometry records to vector proposals.

This does not change room discovery or automatically close any opening.
All source binding belongs to the caller's snapshot; entity provenance survives
polyline decomposition. The recognizer receives no layer or native label fields.
"""
import math

from ezdxf.math import OCS, Vec3

from .vector_recognition import Primitive, recognize_vectors


def recognize_cad_records(records, options=None, *, method='v1', refinements=None):
    if method not in ('v1','v3'):
        raise ValueError('Unknown vector recognition method')
    if method=='v1' and refinements is not None:
        raise ValueError('Refinements apply to the experimental v3 method only')
    if len({r['layout_id'] for r in records}) > 1:
        raise ValueError('Recognition scope cannot span layouts')
    if len({r['entity_id'] for r in records}) != len(records):
        raise ValueError('CAD entity IDs must be unique')
    primitives=[];mapping={};diagnostics=[]
    for rec in records:
        eid=rec['entity_id']
        if rec.get('coordinate_space')!='dxf_wcs' or rec.get('status')!='ready':
            diagnostics.append(dict(entity_id=eid,reason='geometry_not_ready_in_wcs'))
            continue
        paths=rec.get('paths',[])
        if rec['kind'] in ('TEXT','MTEXT','ATTRIB','ATTDEF','DIMENSION','LEADER','MLEADER'):
            continue
        if rec.get('linear') and rec['kind'] in ('LINE','LWPOLYLINE','POLYLINE'):
            for pi,path in enumerate(paths):
                for si,(a,b) in enumerate(zip(path,path[1:])):
                    if math.dist(a,b)==0:continue
                    pid=f'{eid}/path/{pi}/segment/{si}'
                    primitives.append(Primitive(pid,'line',(tuple(a),tuple(b))))
                    mapping[pid]=dict(entity_id=eid,entity_path=rec.get('entity_path',[]),path_index=pi,segment_index=si)
        elif rec['kind']=='ARC':
            n=rec.get('native',{});extrusion=n.get('extrusion',[0,0,1])
            if not paths or abs(extrusion[0])+abs(extrusion[1])>1e-8 or not {'center','radius','start_angle','end_angle'}<=n.keys():
                diagnostics.append(dict(entity_id=eid,reason='unsupported_arc_plane_or_parameters'))
                continue
            center=OCS(Vec3(extrusion)).to_wcs(Vec3(n['center']))
            primitives.append(Primitive(eid,'arc',(tuple(paths[0][0]),tuple(paths[0][-1])),
                (center.x,center.y),abs(n['radius']),(n['end_angle']-n['start_angle'])%360))
            mapping[eid]=dict(entity_id=eid,entity_path=rec.get('entity_path',[]))
        else:
            diagnostics.append(dict(entity_id=eid,reason='unsupported_primitive_kind'))
    if method=='v3':
        from .vector_recognition_v3 import recognize_vectors_v3, Refinements
        # Development ablations rejected the window-cap extension. Keep it off
        # for CAD callers unless an explicit experimental configuration is given.
        result=recognize_vectors_v3(primitives,options,refinements or Refinements(bounded_caps=False))
    else:
        result=recognize_vectors(primitives,options)
    for candidate in result['candidates']:
        ids=candidate['entity_ids']
        candidate['primitive_ids']=ids
        candidate['entity_ids']=sorted({mapping[pid]['entity_id'] for pid in ids})
        candidate['source_parts']=[mapping[pid] for pid in ids]
        evidence=candidate['evidence']
        if 'return_entity_ids' in evidence:
            evidence['return_primitive_ids']=evidence['return_entity_ids']
            evidence['return_entity_ids']=sorted({mapping[pid]['entity_id'] for pid in evidence['return_primitive_ids']})
        if 'cap_support_ids' in evidence:
            evidence['cap_support_primitive_ids']=evidence['cap_support_ids']
            evidence['cap_support_ids']=sorted({mapping[pid]['entity_id'] for pid in evidence['cap_support_primitive_ids']})
    for rejected in result.get('wall_rejections',[]):
        for key in ('entity_ids','outside_support_ids'):
            rejected['primitive_'+key]=rejected[key]
            rejected[key]=sorted({mapping[pid]['entity_id'] for pid in rejected[key]})
    result['primitive_roles']=result.pop('entity_roles')
    result['source_mapping']=mapping
    result['adapter_diagnostics']=diagnostics
    result['geometry_coverage_complete']=not diagnostics
    result['coordinate_space']='dxf_wcs'
    result['layout_id']=records[0]['layout_id'] if records else None
    result['automatic_room_closure']=False
    result['requested_method']=method
    return result
