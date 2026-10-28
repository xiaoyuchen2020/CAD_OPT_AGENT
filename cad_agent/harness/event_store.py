"""Append-only, public-safe execution events; CAD records remain authoritative."""
import hashlib
import json
from uuid import uuid4
from cad_agent.adapters.store import now


def safe_payload(payload):
    from cad_agent.analysis.run_journal import public_value
    return public_value(payload)


class EventStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS harness_events (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(run_id,sequence))')
            c.execute('CREATE TABLE IF NOT EXISTS harness_payloads (sha256 TEXT PRIMARY KEY, data TEXT NOT NULL)')
            c.execute("CREATE TRIGGER IF NOT EXISTS harness_payloads_no_update BEFORE UPDATE ON harness_payloads BEGIN SELECT RAISE(ABORT, 'harness payloads are immutable'); END")
            c.execute("CREATE TRIGGER IF NOT EXISTS harness_payloads_no_delete BEFORE DELETE ON harness_payloads BEGIN SELECT RAISE(ABORT, 'harness payloads are immutable'); END")
            c.execute("CREATE TRIGGER IF NOT EXISTS harness_events_no_update BEFORE UPDATE ON harness_events BEGIN SELECT RAISE(ABORT, 'harness events are append-only'); END")
            c.execute("CREATE TRIGGER IF NOT EXISTS harness_events_no_delete BEFORE DELETE ON harness_events BEGIN SELECT RAISE(ABORT, 'harness events are append-only'); END")

    @staticmethod
    def append_in(c, run_id, kind, payload=None, call_id=None):
        seq = c.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM harness_events WHERE run_id=?', (run_id,)).fetchone()[0]
        clean = safe_payload(payload or {})
        call_id = call_id or (clean.get('call_id') if isinstance(clean,dict) else None)
        encoded = json.dumps(clean,ensure_ascii=False,sort_keys=True)
        stored = len(encoded) > 16000
        if stored:
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            c.execute('INSERT OR IGNORE INTO harness_payloads VALUES (?,?)',(digest,encoded))
            clean = {'payload_ref':digest,'chars':len(encoded),'complete':True}
        value = dict(event_id='he_'+uuid4().hex, version=1, run_id=run_id, sequence=seq,
                     kind=kind, created_at=now(), payload=clean)
        if stored: value['payload_storage'] = 'sha256'
        if call_id: value['call_id'] = call_id
        c.execute('INSERT INTO harness_events VALUES (?,?,?,?,?)',
                  (value['event_id'],run_id,seq,kind,json.dumps(value,ensure_ascii=False)))
        return value

    def read(self, run_id, after=0, limit=100):
        if after < 0 or not 1 <= limit <= 200: raise ValueError('事件分页范围无效。')
        with self.store.connect() as c:
            rows = c.execute('SELECT data FROM harness_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?', (run_id,after,limit+1)).fetchall()
        values = [json.loads(r[0]) for r in rows[:limit]]
        return dict(items=values, has_more=len(rows)>limit,
                    next_after=values[-1]['sequence'] if values else after)

    def read_payload(self, run_id, event_id):
        with self.store.connect() as c:
            row=c.execute('SELECT data FROM harness_events WHERE run_id=? AND event_id=?',(run_id,event_id)).fetchone()
            if row is None: raise KeyError(event_id)
            event=json.loads(row[0])
            value=event['payload']
            if event.get('payload_storage') != 'sha256': return value
            raw=c.execute('SELECT data FROM harness_payloads WHERE sha256=?',(value['payload_ref'],)).fetchone()
        if raw is None or hashlib.sha256(raw[0].encode()).hexdigest()!=value['payload_ref']:
            raise ValueError('执行事件附件完整性校验失败。')
        return json.loads(raw[0])

    def calls(self, run_id):
        with self.store.connect() as c:
            rows=c.execute('SELECT data FROM harness_events WHERE run_id=? ORDER BY sequence',(run_id,)).fetchall()
        calls={}
        for (raw,) in rows:
            event=json.loads(raw)
            if event.get('call_id') and event['kind'].startswith('tool.'):
                calls[event['call_id']] = {'status':event['kind'].split('.',1)[1], 'event_id':event['event_id']}
        return calls
