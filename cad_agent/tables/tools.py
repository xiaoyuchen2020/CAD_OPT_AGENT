"""Session authorization for table tools, independent of model transport."""
from .service import TableService
from .reading import read

NAMES={'search_tables','inspect_table','read_table_content','recover_table_region','review_table_region'}


def execute(session,name,args):
    sid=args.snapshot_id or session.request.snapshot_id
    if sid not in session.allowed_snapshots:raise ValueError('TABLE_SNAPSHOT_NOT_AUTHORIZED')
    service=TableService(session.w,sid,session._remaining)
    owner=session.task.data['task_id']
    if name=='search_tables':result=service.search(args)
    elif name=='inspect_table':result=service.inspect(args)
    elif name=='read_table_content':
        result=read(service,args,owner)
        session.task.data.setdefault('table_reads',{})[result['read_plan_id']]={k:result[k] for k in
            ('table_id','snapshot_id','signature','read_plan_id','next_cursor','coverage')}
    elif name=='recover_table_region':
        from .recovery import prepare
        result=prepare(service,args,owner)
        if result.get('image'):
            image=result['image'];session.visual_images[image['image_id']]=image
    else:
        from .recovery import review
        result=review(service,args,owner,session.sent_image_ids)
    return session._register_tree(result)
