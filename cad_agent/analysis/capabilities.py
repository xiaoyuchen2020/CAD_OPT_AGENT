"""Versioned initial exposure; registry and execution authorization stay separate."""
import re

VERSION = 'cad-execution-v2'
ASSEMBLY_TOOLS = {'search_source_records', 'search_sources', 'read_assembly', 'read_source_context',
                  'read_table_semantics', 'get_drawing_overview', 'resolve_drawing_scope',
                  'finish_analysis', 'request_toolset', 'read_evidence_record'}


def enabled(session):
    return (getattr(session.w.settings, 'execution_policy', 'v2') == 'v2'
            and getattr(session.request, 'agent_policy', 'efficient') != 'legacy'
            and getattr(getattr(session, 'task', None), 'data', {}).get('execution_policy_version', VERSION) == VERSION)


def profile(session):
    if not enabled(session):
        return 'legacy'
    q = session.request.question
    if (re.search('做法|构造|材料表', q) and
            not re.search('面积|周长|净宽|连通|几何|房间|比较|对比|计算|总厚|合计|差值', q) and
            not getattr(session.request, 'parent_run_id', None) and
            not getattr(session.request, 'related_drawings', []) and
            not getattr(session.task, 'complex', False)):
        return 'assembly_lookup'
    return 'mixed_task'


def requirement_spec(text):
    locate = bool(re.search('仅.*(?:位置|目录)|只.*(?:位置|目录)|在哪里|在哪张', text))
    exhaustive = bool(re.search('所有|全部|完整|逐项|各个', text))
    return dict(intent='locate' if locate else 'enumerate_all' if exhaustive else 'describe',
                original_requirement=text, completion_conditions=(
                    ['source_title', 'source_location'] if locate else
                    ['requested_content', 'source_ownership', 'conditions_and_gaps', 'requested_page_coverage']),
                version=VERSION)
