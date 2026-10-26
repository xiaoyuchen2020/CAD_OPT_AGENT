"""Fit final synthesis using the representation actually sent to the model."""
from cad_agent.analysis.delivery_context import bounded_packet
from cad_agent.analysis.efficiency import dumps
from cad_agent.analysis.model_context import encode_messages
from cad_agent.adapters.siwc import fail


def fit_messages(session, packet, limit):
    # Rebuild each candidate from the same complete input. Repeatedly trimming an
    # already trimmed packet loses omission counts and source-binding groups.
    original = packet
    budget = len(dumps(packet))
    attempts = []
    if not hasattr(session, 'runtime_diagnostics'):
        session.runtime_diagnostics = {}
    diagnostics = session.runtime_diagnostics.setdefault('final_context_fit', [])
    record = dict(limit=limit, attempts=attempts)
    diagnostics.append(record)
    for _ in range(10):
        messages = encode_messages(session, [{'role': 'user', 'content': dumps(packet)}],
                                   restore_read_context=False)
        size = sum(len(m.get('content', '')) for m in messages)
        attempts.append(dict(raw_chars=len(dumps(packet)), encoded_chars=size,
                             evidence_count=len(packet.get('evidence', [])),
                             observation_count=len(packet.get('observations', []))))
        if size <= limit:
            record.update(status='fits', omissions=packet.get('context_omissions', {}))
            return messages
        # The ratio is a search hint, never an admission decision. Check the
        # actual encoded result again, including codec-added developer messages.
        budget = min(budget - 1, int(budget * min(.8, limit / size * .9)))
        if budget <= 0:
            break
        packet = bounded_packet(original, budget, session.task.data['subtasks'], include_templates=False)
    record.update(status='minimal_packet_exceeds_limit')
    raise fail('CONTEXT_BUDGET', '最终整理包的必要问题/来源信息仍超过限额；已保留尺寸诊断。')
