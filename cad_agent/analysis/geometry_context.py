"""Lossless table projection and exact duplicate references within one request.

No result, page or failure is dropped. References only target a complete earlier
definition in this same request. Source/algorithm differences cannot coalesce.
"""
import json
from copy import deepcopy


def project_geometry_messages(messages):
    result=deepcopy(messages);seen={};stats=dict(tables=0,duplicate_pages=0,characters_saved=0)
    for message in result:
        key_name='output' if message.get('type')=='function_call_output' else 'content'
        if message.get('role')!='tool' and message.get('type')!='function_call_output':continue
        if not isinstance(message.get(key_name),str):continue
        original=message[key_name]
        try:data=json.loads(original)
        except (ValueError,TypeError):continue
        if not isinstance(data,dict) or not data.get('bundle_id') or not isinstance(data.get('items'),list):continue
        items=data['items']
        if not items or not all(isinstance(r,dict) and 'native' in r and 'entity_id' in r for r in items):continue
        # Observation/call identities remain local to each occurrence.
        meta={k:data[k] for k in ('observation_id','observation_ids') if k in data}
        payload={k:v for k,v in data.items() if k not in meta}
        key=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'))
        if key in seen:
            projected=dict(geometry_page_reference=seen[key],bundle_id=data['bundle_id'],
                note='Exact duplicate of the earlier tool-call page in this request; no new read or coverage credit.',**meta)
            stats['duplicate_pages']+=1
        else:
            call_id=message.get('tool_call_id') or message.get('call_id')
            if call_id:seen[key]=call_id
            common={k:v for k,v in items[0].items() if k not in ('entity_id','entity_path','native') and all(k in r and r[k]==v for r in items)}
            columns=sorted(set().union(*(r.keys() for r in items))-common.keys())
            # Presence mask preserves missing fields versus explicit null.
            projected={k:v for k,v in data.items() if k!='items'}
            projected['entity_table']=dict(common_fields=common,columns=columns,rows=[[r.get(k) for k in columns] for r in items],
                missing_fields=[[k for k in columns if k not in r] for r in items])
            stats['tables']+=1
        encoded=json.dumps(projected,ensure_ascii=False,separators=(',',':'))
        message[key_name]=encoded;stats['characters_saved']+=len(original)-len(encoded)
    return result,stats
