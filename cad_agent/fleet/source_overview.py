"""Read-only source projection. No queue/store constructors or write transactions.

This module can run beside an active centre. Viewing evidence must never expire a
lease, initialize a schema, publish a package, or interrupt a rule workflow.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from collections import OrderedDict
import json
import sqlite3
import shutil
import tempfile
import threading
import time

from .source_overview_support import anchor, archived_location, mapping, published, package_leaves, source_read_atoms, export_measurements
from cad_agent.exploration.contracts import digest


@contextmanager
def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()


class SnapshotReader:
    """Read stable DB+WAL copies without creating SHM files in production.

    File identity/size/mtime/ctime must remain unchanged throughout the copy.
    A concurrent append, checkpoint, rename, WAL creation or deletion retries.
    SQLite opens only the private copy, which owns its own ephemeral SHM file.
    No immutable=1: committed WAL frames must be included in the read view.
    """
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.cache={};self.lock=threading.Lock()

    @staticmethod
    def signature(path):
        try:
            s=path.stat();return s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns
        except FileNotFoundError:return None

    def rows(self,path,query,args):
        with self.lock:
            old=self.cache.get(str(path))
            if old and time.monotonic()-old[2]<10:
                with readonly(old[1]/'snapshot.sqlite3') as db:return [dict(r) for r in db.execute(query,args).fetchall()]
            wal=Path(str(path)+'-wal');paths=(path,wal)
            for _ in range(4):
                before=tuple(self.signature(p) for p in paths)
                if before[0] is None:raise FileNotFoundError(path)
                old=self.cache.get(str(path))
                if old and old[0]==before:
                    with readonly(old[1]/'snapshot.sqlite3') as db:return [dict(r) for r in db.execute(query,args).fetchall()]
                folder=Path(tempfile.mkdtemp(prefix='read-',dir=self.root))
                try:
                    for i,p in enumerate(paths):
                        if before[i] is None:continue
                        if before[i][2]>128*1024*1024:raise ValueError('数据库超过只读快照上限')
                        shutil.copyfile(p,folder/('snapshot.sqlite3'+('-wal' if i else '')))
                    if before!=tuple(self.signature(p) for p in paths):continue
                    with readonly(folder/'snapshot.sqlite3') as db:
                        if db.execute('PRAGMA quick_check(1)').fetchone()[0]!='ok':continue
                        rows=[dict(r) for r in db.execute(query,args).fetchall()]
                    self.cache[str(path)]=(before,folder,time.monotonic())
                    if old:shutil.rmtree(old[1])
                    return rows
                except FileNotFoundError:
                    continue
                finally:
                    if self.cache.get(str(path),(None,None))[1]!=folder:shutil.rmtree(folder,ignore_errors=True)
            raise sqlite3.OperationalError('数据库正在更新，未取得一致只读快照，请稍后刷新')


def records_from_archive(archive):
    sources = {d['id']: dict(d, source_sha256=d['sha256'])
               for d in mapping(archive.get('artifacts')).get('drawings', [])}
    valid, warnings, reads = [], [], []
    for record in mapping(archive.get('artifacts')).get('evidence_records', []):
        try:
            content = record['content']; refs = record['sources']
            if (sha256(content.encode()).hexdigest() != record['ref']['sha256'] or not refs
                or any(s not in sources or sources[s]['source_sha256'] != h for s,h in refs.items())):
                raise ValueError('记录哈希或图纸版本不一致')
            payload = json.loads(content)
            valid.append((record, payload))
            reads.extend(source_read_atoms(payload, record))
        except (ValueError, KeyError, TypeError) as exc:
            warnings.append('读取记录未纳入：'+str(exc)[:160])
    try:
        geometry = export_measurements(valid, sources)
    except (ValueError, KeyError, TypeError) as exc:
        geometry = {}; warnings.append('量测记录校验失败：'+str(exc)[:160])
    return reads, geometry, warnings


def measurement_source(atom):
    raw = atom.get('raw', {})
    line = raw.get('measurement_line') or atom.get('measurement_line')
    box = atom.get('bbox')
    if not box and line:
        box = [min(p[0] for p in line), min(p[1] for p in line), max(p[0] for p in line), max(p[1] for p in line)]
    return dict(atom, evidence_id=atom['measurement_id'],
                entity_ids=raw.get('source_entity_ids', []), bbox=box,
                measurement_line=line, unit=mapping(raw.get('unit')).get('unit', 'unknown'),
                quote='局部源线对量测：'+str(mapping(raw.get('measurements')).get('width_drawing_units', '未知'))+' 图纸单位',
                precision='measurement_line', conditions=raw.get('conditions', []))


def source_identity(source):
    # IDs are task-local. Deduplicate physical source locations only when version,
    # layout, span, text and native entities all agree. Different spans stay separate.
    return digest({k:source.get(k) for k in ('source_sha256','layout_id','region_id','quote',
                   'text_span','entity_ids','bbox','measurement_line','source_claimed_sha256')})


class SourceRepository:
    def __init__(self, root, snapshot_dir=None):
        self.root = Path(root).resolve()
        self.cache = OrderedDict()
        self.snapshots=SnapshotReader(snapshot_dir) if snapshot_dir else None

    def rows(self, name, query, args=()):
        path = self.root/name
        if not path.exists(): return []
        if self.snapshots:return self.snapshots.rows(path,query,args)
        with readonly(path) as db:
            return [dict(r) for r in db.execute(query,args).fetchall()]

    def projects(self):
        rows = self.rows('exploration/exploration.sqlite3','SELECT id,project_id,data FROM exploration_runs')
        return {r['id']:dict(project_id=r['project_id'],label=json.loads(r['data']).get('label',r['project_id'])) for r in rows}

    def jobs(self, refresh=False):
        if refresh and self.snapshots:
            with self.snapshots.lock:
                key=str(self.root/'fleet.sqlite3');old=self.snapshots.cache.get(key)
                if old:self.snapshots.cache[key]=(old[0],old[1],0)
        projects = self.projects()
        rows = self.rows('fleet.sqlite3','SELECT id,status,payload,node_id,attempt_id,run_id,snapshot_id,created_at,updated_at,delivery_status FROM jobs ORDER BY created_at DESC')
        for row in rows:
            payload = json.loads(row.pop('payload')); row['question'] = payload.get('question','')
            row.update(projects.get(payload.get('exploration_run_id'), dict(project_id=None)))
        return rows

    def attempts(self, job_id):
        return self.rows('fleet.sqlite3','SELECT id,job_id,node_id,status,started_at,archive_path FROM attempts WHERE job_id=? ORDER BY started_at',(job_id,))

    def archive(self, job_id, attempt_id):
        rows = self.rows('fleet.sqlite3','SELECT archive_path FROM attempts WHERE job_id=? AND id=?',(job_id,attempt_id))
        if not rows or not rows[0]['archive_path']: raise KeyError('来源归档尚不可用')
        path = (self.root/rows[0]['archive_path']).resolve()
        if not path.is_relative_to(self.root/'archives'): raise ValueError('归档路径越界')
        st = path.stat(); key=(str(path),st.st_mtime_ns,st.st_size)
        if key not in self.cache:
            if st.st_size > 128*1024*1024: raise ValueError('归档过大，请单独复核')
            value = json.loads(path.read_text())
            if (value.get('job_id'),value.get('attempt_id')) != (job_id,attempt_id): raise ValueError('归档身份不一致')
            self.cache[key]=value
            while len(self.cache)>1 and (len(self.cache)>4 or sum(k[2] for k in self.cache)>32*1024*1024):
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        return self.cache[key]

    def workflows(self, project):
        return [json.loads(r['data']) for r in self.rows('rule-workflow/rule_workflow.sqlite3',
                'SELECT data FROM workflows WHERE project=? ORDER BY rowid DESC',(project,))]

    def packages(self, project):
        return {r['id']:json.loads(r['data']) for r in self.rows('rule-workflow/rule_workflow.sqlite3',
                "SELECT id,data FROM objects WHERE project=? AND kind='fact_package'",(project,))}

    def demands(self, project):
        path=self.root/'rule-workflow/rule_workflow.sqlite3'
        if not path.exists():return []
        name='rule-workflow/rule_workflow.sqlite3'
        if not self.rows(name,"SELECT 1 FROM sqlite_master WHERE name='fact_demands'"):return []
        result=[]
        for row in self.rows(name,'SELECT * FROM fact_demands WHERE project=?',(project,)):
            subs=self.rows(name,'SELECT rule_ref,status FROM fact_subscriptions WHERE demand_id=?',(row['id'],))
            result.append(dict(request_id=row['id'],demand=json.loads(row['data']),
                selection=json.loads(row['selection']) if row['selection'] else None,subscribers=subs))
        return result


class Projection:
    def __init__(self):
        self.items={};self.warnings=[];self.gaps=[];self.rules={};self.drawings={}

    def add(self, source, archive, stage, association=None, data=None, issues=()):
        claimed=source.get('source_sha256')
        source=anchor(source,archive)
        if claimed and claimed!=source.get('source_sha256'):
            source['source_claimed_sha256']=claimed
            issues=(*issues,'来源声明的图纸版本与归档不一致')
        if source.get('reference_state') in ('unresolved','missing','ambiguous'):
            issues=(*issues,'引用目标尚未确认')
        if not source.get('source_sha256'):
            key=digest([archive.get('job_id'),archive.get('attempt_id'),source])
        else:key=source_identity(source)
        row=self.items.setdefault(key,dict(source_id=key,source=source,stages=[],origins=[],associations=[],data=[],issues=[],related_ids=[]))
        if source['locatable'] and not row['source']['locatable']:row['source']=source
        origin={k:source.get(k) for k in ('job_id','attempt_id','node_id','snapshot_id','evidence_id')}
        if origin not in row['origins']:row['origins'].append(origin)
        if stage not in row['stages']:row['stages'].append(stage)
        for name,value in [('associations',association),('data',data)]:
            if value and value not in row[name]:row[name].append(value)
        for issue in issues:
            if issue and issue not in row['issues']:row['issues'].append(issue)
        for d in mapping(archive.get('artifacts')).get('drawings',[]):
            if d['id']==source.get('snapshot_id'):
                dk=digest([d['sha256'],d['id'],archive.get('node_id')])
                self.drawings[dk]=dict(snapshot_id=d['id'],source_sha256=d['sha256'],node_id=archive.get('node_id'),name=d.get('name',d['id']))
        return row

    def ingest(self, archive, live=False):
        result=mapping(archive.get('result')) or mapping(mapping(archive.get('artifacts')).get('run')).get('result') or {}
        result=mapping(result)
        reads,geometry,warnings=records_from_archive(archive);self.warnings.extend(warnings)
        for e in reads:self.add(e,archive,'read')
        for e in geometry.values():self.add(measurement_source(e),archive,'read',issues=['局部源线对；工程身份、单位口径与完整覆盖需确认'])
        for e in published(result):
            claims=[c for c in result.get('claims',[]) if e['evidence_id'] in c.get('evidence_ids',[])]
            row=self.add(e,archive,'read' if live else 'delivered')
            for c in claims:
                data={k:c[k] for k in ('text','state','conditions','unit','value') if k in c}
                if data not in row['data']:row['data'].append(data)
                if c.get('state') in ('unknown','conflict'):row['issues'].append('原交付状态：'+c['state'])
        for gap in result.get('missing_information',[]) or []:
            self.gaps.append(dict(job_id=archive['job_id'],attempt_id=archive['attempt_id'],description=gap))

    def finish(self, **metadata):
        rows=list(self.items.values())
        for r in rows:
            r['selected']=any(a.get('selection_kind') in ('workflow_input','field_selection') for a in r['associations'])
            r['locatable']=r['source']['locatable']
        body=dict(schema_version='source-overview@1',items=rows,drawings=list(self.drawings.values()),
                  rules=list(self.rules.values()),gaps=self.gaps,warnings=list(dict.fromkeys(self.warnings)),
                  counts=dict(sources=len(rows),locatable=sum(r['locatable'] for r in rows),
                              selected=sum(r['selected'] for r in rows),pending=sum(bool(r['issues']) for r in rows),
                              data_items=sum(len(r['data']) for r in rows)),exhaustive=False,**metadata)
        body['revision']=digest(body)
        body['updated_at']=datetime.now(timezone.utc).isoformat()
        return body


def build_overview(repo, job_id, *, attempt_id=None, project_scope=False, history=False, live_reader=None):
    jobs=repo.jobs();job=next((j for j in jobs if j['id']==job_id),None)
    if job is None:raise KeyError('任务不存在')
    project=job.get('project_id');p=Projection();selected_origins=set();attempts=repo.attempts(job_id)
    selected_jobs=[j for j in jobs if project and j.get('project_id')==project] if project_scope and project else [job]
    active=False;live_status=[]
    for j in selected_jobs:
        aa=repo.attempts(j['id'])
        if j['id']==job_id and attempt_id:
            aa=[a for a in aa if a['id']==attempt_id]
            if not aa:raise KeyError('执行批次不属于当前任务')
        elif not history:aa=aa[-1:]
        for a in aa:
            selected_origins.add((j['id'],a['id']))
            try:
                if a.get('archive_path'):p.ingest(repo.archive(j['id'],a['id']))
                elif a['id']==j['attempt_id'] and j['status'] in ('running','cancel_requested'):
                    active=True
                    if live_reader:
                        archive,status=live_reader(j,a);live_status.append(status)
                        # Fence responses against lease reassignments, including slow reads.
                        current=next((x for x in repo.jobs(refresh=True) if x['id']==j['id']),{})
                        if (current.get('attempt_id'),current.get('run_id'),current.get('node_id')) != (j['attempt_id'],j['run_id'],j['node_id']):
                            p.warnings.append('执行批次已变化，丢弃旧节点的实时响应');continue
                        if archive:p.ingest(archive,live=True)
                        if status.get('message'):p.warnings.append(status['message'])
                    else:p.warnings.append('运行中来源暂未连接')
            except (KeyError,ValueError,OSError) as exc:p.warnings.append(f"任务 {j['id'][:8]}：{str(exc)[:180]}")
    if project:
        packages=repo.packages(project)
        def add_package(package, stage, assoc=None, ids=None):
            if package.get('project_id')!=project:return
            try:leaves=package_leaves(package,lambda ident:packages[ident])
            except (KeyError,RecursionError):p.warnings.append('事实包组成来源不完整');return
            package_origins={(x.get('origin',{}).get('job_id'),x.get('origin',{}).get('attempt_id')) for x in leaves}
            if not project_scope and not (package_origins & selected_origins):return
            for field in ('atoms','measurements'):
                for eid,atom in package.get(field,{}).items():
                    if ids is not None and eid not in ids:continue
                    origins=[leaf for leaf in leaves if leaf.get(field,{}).get(eid)==atom]
                    for leaf in origins:
                        origin=leaf.get('origin',{})
                        if not project_scope and (origin.get('job_id'),origin.get('attempt_id')) not in selected_origins:continue
                        # Do not let a malformed cross-project package expose unrelated archives.
                        origin_job=next((j for j in jobs if j['id']==origin.get('job_id')),None)
                        if not origin_job or origin_job.get('project_id')!=project:continue
                        try:archive=repo.archive(origin['job_id'],origin['attempt_id'])
                        except (KeyError,ValueError,OSError):
                            archive=dict(job_id=origin.get('job_id'),attempt_id=origin.get('attempt_id'),artifacts={})
                        source=measurement_source(atom) if field=='measurements' else archived_location(atom,archive)
                        link=dict(assoc or {},package_id=package['package_id'])
                        issue=['局部量测；工程身份及单位口径待确认'] if field=='measurements' else []
                        row=p.add(source,archive,stage,association=link,issues=issue)
                        for assertion in package.get('assertions',[]):
                            if eid in assertion.get('evidence_ids',[]):
                                data={k:assertion[k] for k in ('text','state','conditions') if k in assertion}
                                if data not in row['data']:row['data'].append(data)
        for package in packages.values():add_package(package,'packaged')
        for run in repo.workflows(project):
            inputs=run.get('inputs',{});rule=inputs.get('rule',{});rid=rule.get('rule_id','未知规则')
            association=dict(rule_id=rid,rule_ref=':'.join(str(rule.get(k,'')) for k in ('library_id','version','rule_id')),
                workflow_id=run['run_id'],workflow_status=run['status'],selection_kind='workflow_input',created_at=run.get('created_at'))
            before=sum(len(r['associations']) for r in p.items.values())
            add_package(inputs.get('package',{}),'analysis_input',association)
            if project_scope or sum(len(r['associations']) for r in p.items.values())>before:
                p.rules[run['run_id']]=association
                for gap in mapping(run.get('result')).get('gaps',[]):p.gaps.append(dict(rule_id=rid,workflow_id=run['run_id'],description=gap))
                for finding in mapping(run.get('result')).get('findings',[]):
                    ids=finding.get('design',{}).get('measurement_ids',[]) or [finding.get('design',{}).get('evidence_id')]
                    for row in p.items.values():
                        if any(a.get('workflow_id')==run['run_id'] for a in row['associations']) and any(o.get('evidence_id') in ids for o in row['origins']):
                            conditions=mapping(finding.get('proposed_change')).get('conditions',[])
                            row['issues']=list(dict.fromkeys(row['issues']+conditions))
        for demand in repo.demands(project):
            selection=demand.get('selection')
            active_subs=[s for s in demand['subscribers'] if s['status']=='active']
            if not selection:
                if project_scope:p.gaps.append(dict(request_id=demand['request_id'],description=demand['demand'].get('fields',[]),state='needs_evidence'))
                continue
            for field,values in selection.get('fields',{}).items():
                for value in values:
                    package=packages.get(value['package_id'])
                    if not package:continue
                    atom=package.get('atoms',{}).get(value['source_id']) or package.get('measurements',{}).get(value['source_id'])
                    if atom!=value.get('source') or digest(atom)!=value.get('source_digest'):
                        p.warnings.append('字段选择与事实包原子不一致，未标记为分析输入');continue
                    for sub in active_subs:
                        assoc=dict(rule_id=sub['rule_ref'].split(':')[-1],rule_ref=sub['rule_ref'],request_id=demand['request_id'],
                                   field=field,selection_kind='field_selection',review_reason=selection.get('review_reason'))
                        add_package(package,'analysis_input',assoc,{value['source_id']})
        # Explicit source-ID references only: no keyword/nearest-geometry inference.
        by_eid={}
        for row in p.items.values():
            for origin in row['origins']:by_eid.setdefault(origin.get('evidence_id'),[]).append(row)
        for row in p.items.values():
            for ref in row['source'].get('reference_targets',[]) or []:
                eid=ref.get('evidence_id') if isinstance(ref,dict) else ref
                for target in by_eid.get(eid,[]):
                    same_origin=any((a['job_id'],a['attempt_id'])==(b['job_id'],b['attempt_id']) for a in row['origins'] for b in target['origins'])
                    explicit_target=isinstance(ref,dict) and ref.get('snapshot_id')==target['source'].get('snapshot_id') and ref.get('source_sha256')==target['source'].get('source_sha256')
                    if target['source_id']!=row['source_id'] and (same_origin or explicit_target):row['related_ids'].append(target['source_id'])
    return p.finish(job_id=job_id,question=job['question'],project_id=project,scope='project' if project_scope and project else 'task',
        attempt_id=attempt_id or job.get('attempt_id'),attempts=[{k:v for k,v in a.items() if k!='archive_path'} for a in attempts],
        execution_status=job['status'],delivery_status=job.get('delivery_status'),active=active,live_status=live_status)
