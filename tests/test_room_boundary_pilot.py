"""Safety gates for opt-in experiment, independent of generated implementation."""
import copy
import ezdxf
from scripts.room_boundary_pilot import visibility_map
from scripts.room_boundary_pilot_validation import validate
from cad_agent.extensions.table_recovery.bundle import digest

def seal(b):
    b={k:v for k,v in b.items() if k not in ('input_id','bundle_sha256')};h=digest(b);return b|dict(input_id='room_'+h,bundle_sha256=h)
def fixture():
    points=[[0,0],[1000,0],[1000,2000],[0,2000]];outer=[[-200,-200],[1200,-200],[1200,2200],[-200,2200]]
    segs=[]
    for group,ps in [('i',points),('o',outer)]:
        for n,(a,b) in enumerate(zip(ps,ps[1:]+ps[:1])):segs.append(dict(segment_id=f'{group}{n}',entity_path=[f'{group}{n}'],a=a,b=b,visibility=dict(effective_layer='WALL',visible=True,supported=True)))
    segs.append(dict(segment_id='axis',entity_path=['axis'],a=[-300,1000],b=[1300,1000],visibility=dict(effective_layer='AXIS',visible=True,supported=True)))
    b=seal(dict(source_sha256='s',bbox=[-500,-500,1500,2500],segments=segs,texts=[dict(entity_id='text',text='room',origin=[500,1000],visibility=dict(visible=True,supported=True))],
      target=dict(label_entity_id='text',label='room',seed=[500,1000]),unit_evidence=dict(insunits=4,effective_unit='mm',metres_per_unit=.001,override_used=False,source_sha256='s'),coverage=dict(blocking_diagnostics=[]),policy=dict(tolerance_units=1,wall_layers=['WALL','COLU'],opening_layers=['WINDOW'],minimum_parallel_support_fraction=.5)))
    o=dict(input_id=b['input_id'],status='candidate',vertices=points,edge_segment_ids=[[f'i{i}'] for i in range(4)],excluded_crossing_ids=['axis'],area=2000000)
    return b,o

def test_valid_source_face():
    b,o=fixture();v=validate(b,o);assert v['validation_status']=='passed',v;assert v['checks']['observed_envelope_area_m2']==2

def test_wrong_and_partial_candidates():
    b,o=fixture()
    mutations=[('WRONG_INPUT_ID',{'input_id':'foreign'}),('AREA_MISMATCH',{'area':123}),('CROSSING_EXCLUSIONS_INCOMPLETE_OR_FALSE',{'excluded_crossing_ids':[]}),('FOREIGN_OR_DUPLICATE_SEGMENT',{'edge_segment_ids':[['invented'],['i1'],['i2'],['i3']]}),('NONSTRUCTURAL_BOUNDARY',{'edge_segment_ids':[['axis'],['i1'],['i2'],['i3']]}),('UNSUPPORTED_EDGE_OR_BRIDGE',{'vertices':[[0,0],[1000,0],[1000,1800],[0,2000]]}),('NOT_COMPLETE_MINIMAL_STRUCTURAL_FACE',{'vertices':[[-200,-200],[1200,-200],[1200,2200],[-200,2200]],'edge_segment_ids':[[f'o{i}'] for i in range(4)]})]
    for error,change in mutations:
        result=validate(b,o|change);assert result['errors']==[error],result

def test_evidence_fail_closed():
    for label in ['hidden','unknown_unit','unsupported_visibility','target','incomplete','tamper']:
        b,o=fixture()
        if label=='hidden':b['segments'][0]['visibility']['visible']=False
        elif label=='unknown_unit':b['unit_evidence']['insunits']=0
        elif label=='unsupported_visibility':b['segments'][0]['visibility']['supported']=False
        elif label=='target':b['target']['seed']=[9000,9000];b['texts'][0]['origin']=[9000,9000]
        elif label=='incomplete':b['coverage']['blocking_diagnostics']=[{'code':'UNLOCATED_GEOMETRY'}]
        else:b['segments'][0]['a']=[123,456]
        if label!='tamper':b=seal(b);o['input_id']=b['input_id']
        v=validate(b,o);assert v['validation_status']=='rejected',v

def test_nested_insert_layer_zero_and_visibility():
    d=ezdxf.new();d.layers.new('WALL');d.layers.new('HIDDEN');d.layers.get('HIDDEN').freeze()
    inner=d.blocks.new('INNER');inner.add_line((0,0),(1,0),dxfattribs={'layer':'0'})
    outer=d.blocks.new('OUTER');outer.add_blockref('INNER',(0,0),dxfattribs={'layer':'0'})
    root=d.modelspace().add_blockref('OUTER',(0,0),dxfattribs={'layer':'WALL'})
    result=visibility_map(d,[root.dxf.handle]);v=next(iter(result.values()));assert len(v['states'])==3 and v['effective_layer']=='WALL' and v['visible']
    root.dxf.layer='HIDDEN';assert not next(iter(visibility_map(d,[root.dxf.handle]).values()))['visible']
    root.dxf.layer='WALL';root.dxf.invisible=1;assert not next(iter(visibility_map(d,[root.dxf.handle]).values()))['visible']

def test_native_near_coincident_roundoff_and_candidate_backtrack():
    b,o=fixture()
    # Native redundant spur with 1e-9 width must not create a new face.
    b['segments'].extend([dict(segment_id='jamb',entity_path=['jamb'],a=[600,0],b=[600,60],visibility=dict(effective_layer='WALL',visible=True,supported=True)),dict(segment_id='jamb2',entity_path=['jamb2'],a=[600+1e-9,60],b=[600+1e-9,0],visibility=dict(effective_layer='WALL',visible=True,supported=True))])
    b=seal(b);o['input_id']=b['input_id']
    assert validate(b,o)['validation_status']=='passed'
    bad=copy.deepcopy(o);bad['vertices']=[[0,0],[600,0],[600,60],[600+1e-9,60],[600+1e-9,0],[1000,0],[1000,2000],[0,2000]]
    # A fake spur is rejected before any area acceptance, even if its area is negligible.
    bad['edge_segment_ids']=[['i0'],['jamb'],['jamb'],['jamb2'],['i0'],['i1'],['i2'],['i3']]
    assert validate(b,bad)['validation_status']=='rejected'
