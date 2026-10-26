"""Versioned table documents. All recovery starts at the immutable CAD source."""
from hashlib import sha256
import json
from cad_agent.analysis.general import DrawingIndex, intersects
from cad_agent.cache import workspace_cache
from cad_agent.contracts import stable_id
from . import VERSION
from .contracts import TableDocument
from .topology import recover
from .binding import bind_cells, title, hierarchy, grid_fields, normalized
from .validation import validate


def fingerprint(index):
    h = sha256(VERSION.encode())
    source=sha256(index.w.store.artifact(index.snapshot,'source.dxf').read_bytes()).hexdigest()
    if source!=index.drawing['sha256']:raise ValueError('TABLE_SOURCE_CHANGED：源文件校验不一致。')
    for name in ('scene.json', 'regions.json'):
        h.update(index.w.store.artifact(index.snapshot, name).read_bytes())
    h.update(index.drawing['sha256'].encode())
    return h.hexdigest()


class TableService:
    def __init__(self, workspace, snapshot, check=lambda: None):
        self.w, self.snapshot, self.check = workspace, snapshot, check
        self.index = DrawingIndex(workspace, snapshot)
        self.signature = fingerprint(self.index)

    def documents(self):
        def build():
            candidates, diagnostics = recover(self.index.scene, self.snapshot, check=self.check,signature=self.signature)
            docs = [self.document(c) for c in candidates]
            from .candidates import borderless
            proposals = borderless(self.index, docs)
            return dict(documents=docs, candidates=proposals, diagnostics=diagnostics)
        cache = workspace_cache(self.w, '_table_document_cache', max_entries=4)
        return cache.get_or_build((VERSION, self.snapshot, self.signature), build,
                                 lambda value: len(json.dumps(value, ensure_ascii=False))*3)

    def document(self, candidate):
        cells, fields, missing = bind_cells(self.index, candidate)
        name, evidence = title(self.index, candidate, cells)
        nodes = hierarchy(candidate, cells, name)
        fields = grid_fields(cells, fields, nodes, candidate['transform'])
        node_by_id = {n['node_id']: n for n in nodes}
        for field in fields:
            node = node_by_id[field['cell_id']]
            ancestors = []
            while node.get('parent_id'):
                node = node_by_id[node['parent_id']]
                ancestors.append(dict(node_id=node['node_id'], kind=node['kind'], title=node['title']))
            field['lineage'] = list(reversed(ancestors))
        doc = dict(version=VERSION, table_id=candidate['table_id'], snapshot_id=self.snapshot,
                   source_sha256=self.index.drawing['sha256'], signature=self.signature,
                   layout_id=candidate['layout_id'], instance_path=candidate['instance_path'],
                   source_kind=candidate['source_kind'], title=name, title_evidence=evidence,
                   bbox=candidate['bbox'], boundary=candidate['boundary'], transform=candidate['transform'],
                   cells=cells, nodes=nodes, fields=fields, source_inventory=[t['entity_id'] for t in candidate['texts']],
                   unassigned_entity_ids=missing, scope_exhaustive=False)
        doc = TableDocument.model_validate(doc).model_dump()
        doc['diagnostics'] = validate(doc, self.index)
        natives=[n for n in self.index.scene.get('native_tables',[]) if n['layout_id']==doc['layout_id'] and n['entity_path']==doc['instance_path']]
        if len(natives)==1:
            native=natives[0];doc['native_metadata']=native
            native_text=[str(v) for row in native.get('rows',[]) for v in row if str(v).strip()]
            graphical='\n'.join(f['raw_text'] for c in cells for f in c['fragments'])
            if any(normalized(v) not in normalized(graphical) for v in native_text):doc['diagnostics'].append('NATIVE_TABLE_GRAPHICS_DISAGREE')
            doc['diagnostics'].extend(native['diagnostics']);doc['source_kind']='native_table'
        doc['structure_state'] = 'source_bound' if not doc['diagnostics'] else 'unresolved'
        doc['binding_state'] = ('source_bound' if not doc['diagnostics'] and
            all(f['binding_state']=='source_bound' for f in fields) and all(c['binding_state']=='source_bound' for c in cells) else 'unresolved')
        if doc['source_kind']=='borderless':
            doc['structure_state']=doc['binding_state']='candidate'
            doc['diagnostics'].append('INFERRED_CELL_BOUNDARIES')
            for c in doc['cells']:c['structure_state']='candidate'
            for f in doc['fields']:f['binding_state']='candidate'
        frames = {self.index.by_id[e['region_id']].frame_id for e in evidence if e['region_id'] in self.index.by_id}
        if len(frames)==1: doc['frame_id'] = frames.pop()
        self.w.store.put('table_document', stable_id('tdoc', self.snapshot, self.signature, doc['table_id']), doc)
        return doc

    def get(self, table_id):
        doc = next((d for d in self.documents()['documents'] if d['table_id']==table_id), None)
        if doc is None:
            try:
                doc=self.w.store.get('table_document',stable_id('tdoc',self.snapshot,self.signature,table_id))
            except KeyError:
                pass
        if doc is None:
            raise ValueError('TABLE_NOT_IN_SNAPSHOT：请在当前图纸重新搜索表格。')
        return doc

    def archived_view(self,table_id):
        """History can display its frozen document, never resume a stale cursor."""
        saved=[d for d in self.w.store.list('table_document',filters={'snapshot_id':self.snapshot}) if d['table_id']==table_id]
        if len(saved)!=1:return self.get(table_id)
        doc=saved[0]
        if doc['source_sha256']!=self.index.drawing['sha256']:raise ValueError('TABLE_SOURCE_CHANGED')
        for c in doc['cells']:
            for f in c['fragments']:
                for e in f['evidence']:
                    region=self.index.by_id.get(e['region_id'])
                    if not region or region.text[slice(*e['text_span'])]!=e['quote']:raise ValueError('TABLE_SOURCE_CHANGED')
        return doc | {'historical_version':doc['signature']!=self.signature}

    def search(self, args):
        self.index.scope(args.layout_id, args.frame_id)
        data = self.documents(); hits = []; query = normalized(args.query).casefold()
        for d in data['documents']:
            if args.layout_id and d['layout_id']!=args.layout_id: continue
            if args.frame_id and d['frame_id']!=args.frame_id: continue
            if args.bbox and not intersects(args.bbox, d['bbox']): continue
            common = {k:d[k] for k in ('table_id','snapshot_id','title','layout_id','bbox','source_kind','structure_state','binding_state')}
            if args.level in ('all','table') and (not query or query in normalized(d['title']).casefold()):
                hits.append(common | dict(level='table', matched_text=d['title'], cell_count=len(d['cells']),
                    group_count=sum(n['kind']=='group' for n in d['nodes']), evidence=d['title_evidence']))
            if args.level in ('all','group'):
                for n in d['nodes']:
                    if n['kind'] in ('group','subtable') and (not query or query in normalized(n['title']).casefold()):
                        hits.append(common | dict(level='group',node_id=n['node_id'],matched_text=n['title']))
            if args.level in ('all','field'):
                for f in d['fields']:
                    text=' / '.join([*(n['title'] for n in f['lineage']),*f['header_path'],f['label'],f['raw_value'],
                                     *(r['text'] for r in f.get('row_labels',[])),*(n['text'] for n in f.get('notes',[]))])
                    if query and query not in normalized(text).casefold(): continue
                    hits.append(common | dict(level='field',field_id=f['field_id'],cell_id=f['cell_id'],matched_text=text,evidence=f['evidence']))
        proposals=[p for p in data['candidates'] if (not args.layout_id or p['layout_id']==args.layout_id)
                   and not args.frame_id and (not args.bbox or intersects(args.bbox,p['bbox']))
                   and (not query or query in normalized(p['preview']).casefold())]
        return dict(items=hits[args.offset:args.offset+args.limit],total=len(hits),offset=args.offset,
                    has_more=args.offset+args.limit<len(hits),candidates=proposals[:args.limit],
                    candidate_total=len(proposals),diagnostics=data['diagnostics'],scope_exhaustive=False,
                    match_basis='literal_normalized',signature=self.signature,
                    note='表格、分组、字段分别检索；候选与视觉转录不等于已确认结构。零命中不证明原图没有。')

    def inspect(self, args):
        d = self.get(args.table_id)
        base = {k:d[k] for k in ('table_id','snapshot_id','title','signature','structure_state','binding_state','scope_exhaustive')}
        if args.view=='diagnostics':
            return base | dict(diagnostics=d['diagnostics'],unassigned_entity_ids=d['unassigned_entity_ids'])
        if args.view=='coverage':
            return base | dict(cell_count=len(d['cells']),source_count=len(d['source_inventory']),
                               assigned_source_count=len(d['source_inventory'])-len(d['unassigned_entity_ids']),
                               note='源图元归位率不是本次读取覆盖率；后者由 read_plan_id 返回。')
        nodes=[n for n in d['nodes'] if n['kind']!='cell']
        return base | dict(items=nodes[args.offset:args.offset+args.limit],total=len(nodes),
                           has_more=args.offset+args.limit<len(nodes),offset=args.offset)
