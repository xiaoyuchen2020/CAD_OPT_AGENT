"""Host-owned revision canaries; never sent to the generating model."""
from copy import deepcopy
from ..table_recovery.bundle import digest


def cases(bundle):
    values=[deepcopy(bundle)]
    for shift,scale in ((137.25,2.0),(-83.5,.5)):
        b=deepcopy(bundle)
        for t in b['texts']:
            t['entity_id']='check_'+t['entity_id']
            t['origin']=[v*scale+shift for v in t['origin']]
            t['height']*=scale
        for r in b['regions']:
            r['region_id']='check_'+r['region_id']
            for p in r['parts']:p['entity_id']='check_'+p['entity_id']
        b['column_edges']=[x*scale+shift for x in b['column_edges']]
        b['bbox']=[v*scale+shift for v in b['bbox']]
        b['texts'].reverse();b['regions'].reverse();b['input_id']=digest(b)
        values.append(b)
    # Insertion x order disagrees with strict y order within quantized baseline.
    for height in (1,5,20):
        b=dict(texts=[dict(entity_id='b',origin=[2,10.01],height=height),
                      dict(entity_id='a',origin=[1,10],height=height),
                      dict(entity_id='c',origin=[12,10],height=height)],
               regions=[dict(region_id=x,text=x,parts=[dict(entity_id=x,text_span=[0,1])]) for x in ('a','b','c')],
               column_edges=[0,10,20],bbox=[0,0,20,20])
        b['input_id']=digest(b);values.append(b)
    return values
