"""Lossless page tables and same-request exact references, including dimensions.

Source/algorithm/status/pagination differences remain distinct. The reverse
function is used by regression tests to verify every original field survives.
"""
import json
from copy import deepcopy


def table(items):
    common={k:v for k,v in items[0].items() if k not in ('entity_id','entity_path','native') and all(k in r and r[k]==v for r in items)}
    columns=sorted(set().union(*(r.keys() for r in items))-common.keys())
    return dict(common_fields=common,columns=columns,rows=[[r.get(k) for k in columns] for r in items],
        missing_fields=[[k for k in columns if k not in r] for r in items])


def untabulate(t):
    return [deepcopy(t['common_fields'])|{k:v for k,v in zip(t['columns'],row) if k not in missing}
            for row,missing in zip(t['rows'],t['missing_fields'])]


def project_geometry_messages(messages):
    result=deepcopy(messages);seen={};shared={};stats=dict(tables=0,duplicate_pages=0,characters_saved=0,shared_fields=0)
    for message in result:
        key_name='output' if message.get('type')=='function_call_output' else 'content'
        if message.get('role')!='tool' and message.get('type')!='function_call_output':continue
        if not isinstance(message.get(key_name),str):continue
        original=message[key_name]
        try:data=json.loads(original)
        except (ValueError,TypeError):continue
        if not isinstance(data,dict) or not isinstance(data.get('items'),list):continue
        items=data['items']
        dimension=data.get('kind')=='dimensions' and data.get('record_id')
        native=data.get('bundle_id') and items and all(isinstance(r,dict) and 'native' in r and 'entity_id' in r for r in items)
        if not (dimension or native) or not items or not all(isinstance(r,dict) for r in items):continue
        if any(k in data for k in ('entity_table','dimension_table','geometry_page_reference','shared_field_references')):continue
        meta={k:data[k] for k in ('observation_id','observation_ids') if k in data}
        payload={k:v for k,v in data.items() if k not in meta}
        key=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'))
        call_id=message.get('tool_call_id') or message.get('call_id')
        if key in seen:
            owner={k:data[k] for k in ('bundle_id','record_id') if k in data}
            projected=dict(geometry_page_reference=seen[key],**owner,
                note='Exact duplicate of the earlier tool-call page in this request; no new read or coverage credit.',**meta)
            stats['duplicate_pages']+=1
        else:
            if call_id:seen[key]=call_id
            projected={k:v for k,v in data.items() if k!='items'}
            projected['dimension_table' if dimension else 'entity_table']=table(items)
            refs={}
            for field in ('diagnostics','source_diagnostics','coverage'):
                if field not in projected:continue
                signature=json.dumps(projected[field],sort_keys=True,ensure_ascii=False,separators=(',',':'))
                if len(signature)<800:continue
                if signature in shared:
                    refs[field]=shared[signature];del projected[field];stats['shared_fields']+=1
                elif call_id:shared[signature]=dict(tool_call_id=call_id,field=field)
            if refs:projected['shared_field_references']=refs
            stats['tables']+=1
        encoded=json.dumps(projected,ensure_ascii=False,separators=(',',':'))
        message[key_name]=encoded;stats['characters_saved']+=len(original)-len(encoded)
    return result,stats


def restore_geometry_messages(messages):
    result=deepcopy(messages);seen={}
    for message in result:
        key='output' if message.get('type')=='function_call_output' else 'content'
        if message.get('role')!='tool' and message.get('type')!='function_call_output':continue
        if not isinstance(message.get(key),str):continue
        try:data=json.loads(message[key])
        except (ValueError,TypeError):continue
        if not isinstance(data,dict):continue
        if 'geometry_page_reference' in data:
            meta={k:v for k,v in data.items() if k in ('observation_id','observation_ids')}
            data=deepcopy(seen[data['geometry_page_reference']])
            data.pop('observation_id',None);data.pop('observation_ids',None);data.update(meta)
        for field,reference in data.pop('shared_field_references',{}).items():
            data[field]=deepcopy(seen[reference['tool_call_id']][reference['field']])
        for field in ('entity_table','dimension_table'):
            if field in data:data['items']=untabulate(data.pop(field))
        call_id=message.get('tool_call_id') or message.get('call_id')
        if call_id:seen[call_id]=data
        message[key]=json.dumps(data,ensure_ascii=False)
    return result
