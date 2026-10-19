"""Per-question, deterministic exploration controls; never judges source meaning."""

import json


GUIDANCE = """
构造做法用read_assembly关联有序原文与表格入口；用户参考值通过user_inputs绑定问题原句，算术用calculate_facts区分source/user_input/measurement，不作优化或合规判断。楼层范围不明用resolve_drawing_scope，跨图只用read_project_sources访问用户选择的资料。
本次采用精简探索：具体问题直接检索问题里的对象和属性，不必先浏览全图概览或抽取事实。
首次检索建议 limit=5；按各分项读取对应原文、结构父标题及例外。复杂问题须完成所有可执行分项，单条引用不代表整题完成。
只为一个明确缺口补查（例如缺表头、缺对象、存在冲突或需要比较另一对象），不要为了“更全面”遍历相邻条款。
read_source_context/read_table 已直接提供证据，无需再 get_evidence 重读；已读证据可从之前工具消息引用。
不要重复同样的工具参数。分页使用 next_start/offset；检索词改写但候选相同，也不应继续换词搜索。
若语义候选不相关，可用对象的核心词做一次 match=all 字面核查；仍没有相关依据就回答 unknown。
有限检索只能说明当前已读取证据不足，不能证明整图不存在该信息。预算收束不等于证据充分。
简单问题通常1–2条；多分项问题逐项覆盖，保留条件和例外；不补充用户未问的相邻要求或其他部位。
"""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def evidence_ids(value):
    if isinstance(value, dict):
        if "evidence_id" in value and "quote" in value:
            return {value["evidence_id"]}
        return set().union(*(evidence_ids(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(evidence_ids(v) for v in value)) if value else set()
    return set()


def compact(value, seen=None):
    """Remove duplicate text/coordinates, never shorten a quote or change its span.

    Source entity IDs/boxes remain in the session evidence registry for UI
    localization. Repeated quotes reference an earlier model-visible message.
    """
    if seen is None:
        seen = set()
    if isinstance(value, list):
        return [compact(v, seen) for v in value]
    if not isinstance(value, dict):
        return value
    if "evidence_id" in value and "quote" in value:
        eid = value["evidence_id"]
        if eid in seen:
            return {"evidence_id": eid, "already_read": True}
        seen.add(eid)
        return {k: value[k] for k in ("evidence_id", "snapshot_id", "region_id", "quote", "text_span") if k in value}
    result = {}
    evidence = value.get("evidence", {})
    for k, v in value.items():
        if isinstance(evidence, dict) and k in ("text", "title", "text_span"):
            if v == evidence.get("quote" if k != "text_span" else "text_span"):
                continue
        if k in ("chunk_id", "retrieval_ranks"):
            continue
        if k == "retrieval" and isinstance(v, dict):
            v = {key: v[key] for key in (
                "requested_mode", "effective_mode", "fallback_reason", "score_is_confidence"
            ) if key in v}
        result[k] = compact(v, seen)
    return result


class ExplorationPolicy:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.cache = {}
        self.seen_evidence = set()
        self.seen_results = set()
        self.stalled = 0
        self.searches = 0
        self.response_chars = 0
        self.stop_reason = None
        self.model_requests = []

    def key(self, name, args):
        # Validated defaults, scope, offsets, and ordering are preserved. Do not
        # assume two differently worded searches have equivalent semantics.
        return name + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True)

    def record(self, name, result, new_evidence, duplicate, chars):
        if not self.enabled:
            return
        signatures = set()
        for item in result.get("items", []):
            identity = next((item[k] for k in (
                "region_id", "source_region_id", "entity_id", "fact_id", "practice_id", "object_id"
            ) if item.get(k)), None)
            if identity:
                signatures.add((name, identity, item.get("snippet_start", 0)))
        new_results = signatures - self.seen_results
        self.seen_results.update(signatures)
        self.stalled = 0 if new_evidence or new_results else self.stalled + 1
        if name in ("extract_facts", "measure_geometry", "measure_room", "get_measurement_evidence", "discover_rooms", "get_room_contours", "measure_room_area", "render_region", "bind_visual_objects", "analyze_measurements", "get_analysis_results", "read_dimensions", "check_dimension_chain") and not duplicate:
            self.stalled = 0
            # Extraction changes query availability, even for generic fact sets.
            self.cache.clear()
        if name == "search_sources":
            self.searches += 1
        self.response_chars += chars
        if self.stalled >= 3:
            self.stop_reason = "no_new_information"
        elif self.response_chars >= (100000 if getattr(self,'geometry_work',False) else getattr(self,'context_limit',28000)):
            self.stop_reason = "tool_context_budget"
        # A search limit is guidance only: leave tools open to read the candidates
        # just retrieved. The normal runtime round cap bounds exploration.

    def hint(self):
        if self.stop_reason:
            return "探索收束：" + self.stop_reason + "。仅依据已读证据回答；不足标 unknown，不能声称已穷尽图纸。"
        if self.searches >= 3:
            return "已多次检索，请读取已有候选或回答；只有明确缺口才继续检索。"
        return "核对对象、属性值与必要范围后立即回答；仅为明确缺口补查。"
