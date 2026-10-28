"""Classify unsettled calls; never retry or fabricate successful results."""
import json


def close_unsettled(c, run_id, append):
    calls = {}
    for (raw,) in c.execute('SELECT data FROM harness_events WHERE run_id=? ORDER BY sequence', (run_id,)):
        event = json.loads(raw)
        call_id = event.get('call_id') or event.get('payload',{}).get('call_id')
        if call_id and event['kind'].startswith('tool.'):
            calls[call_id] = event['kind'].split('.',1)[1]
    for call_id, state in calls.items():
        if state in ('received','admitted','dispatched'):
            outcome = 'unknown' if state == 'dispatched' else 'not_started'
            append(c,run_id,'tool.'+outcome,{'reason':'execution_interrupted','automatic_retry':False},call_id)
