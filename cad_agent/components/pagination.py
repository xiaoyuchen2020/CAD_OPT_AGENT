"""Byte-bounded relation pages; one page is still exactly one tool call."""
import json
import math

PAGE_CHAR_LIMIT = 40000
# Leave room for pagination explanations and the observation receipt. The normal
# tool boundary independently enforces its final 50k response limit.
PAGE_METADATA_RESERVE = 2000
MAX_PAGE_ITEMS = 20


def relation_page(header, items, offset, requested_limit, page_call_budget=None):
    if offset < 0 or not 1 <= requested_limit <= MAX_PAGE_ITEMS:
        raise ValueError('组件分页offset或limit无效。')
    base = len(json.dumps(header, ensure_ascii=False)) + PAGE_METADATA_RESERVE
    capacity = PAGE_CHAR_LIMIT - base
    costs = [len(json.dumps(item, ensure_ascii=False)) + 2 for item in items[offset:]]
    if capacity <= 0 or (costs and costs[0] > capacity):
        raise ValueError('COMPONENT_ITEM_TOO_LARGE：单个对象或来源诊断超过安全响应上限；未跳过或标记已读，请缩小几何查询范围。')

    def fitting(start):
        used = 0
        count = 0
        for cost in costs[start:start+MAX_PAGE_ITEMS]:
            if used+cost > capacity: break
            used += cost
            count += 1
        return count

    safe_limit = fitting(0)
    available = None if page_call_budget is None else max(0, page_call_budget)
    minimum_limit = 1 if not costs or available is None else math.ceil(len(costs)/max(1,available))
    desired = max(requested_limit, min(MAX_PAGE_ITEMS,minimum_limit)) if available is not None else requested_limit
    count = min(desired, safe_limit)
    next_offset = offset+count if offset+count < len(items) else None
    def pages_needed(start, limit=MAX_PAGE_ITEMS):
        count = 0
        cursor = start
        while cursor < len(costs):
            step = min(limit, fitting(cursor))
            if not step: return None, offset+cursor
            cursor += step
            count += 1
        return count, None

    minimum_calls, blocked_offset = pages_needed(0)
    requested_calls, requested_blocked = pages_needed(0, requested_limit)
    suffix_calls, _ = pages_needed(count)
    if available is not None and (suffix_calls is None or suffix_calls > max(0,available-1)):
        count = safe_limit
        next_offset = offset+count if offset+count<len(items) else None
    header.update(items=items[offset:offset+count], offset=offset, total=len(items),
                  requested_limit=requested_limit, effective_limit=count,
                  safe_limit=safe_limit, next_offset=next_offset, has_more=next_offset is not None)
    header['pagination'] = dict(
        response_char_limit=PAGE_CHAR_LIMIT, items_truncated=False, returned_range=[offset,offset+count], coverage_scope="returned_page_only",
        adjustment='reduced_for_response_size' if count<min(requested_limit,len(costs)) else
                   'increased_for_call_budget' if count>requested_limit else 'none',
        note='下一页必须使用next_offset；effective_limit是实际返回数量。只收到的对象才算已读；候选与unknown状态不因分页改变。')
    header['pagination_budget'] = dict(
        available_page_calls_including_current=available,
        minimum_required_page_calls=minimum_calls,
        fits_remaining_call_budget=None if available is None else blocked_offset is None and minimum_calls<=available,
        requested_limit_fits_budget=None if available is None else requested_blocked is None and requested_calls<=available,
        unread_after_page=max(0,len(items)-offset-count), blocked_item_offset=blocked_offset,
        note='预算仅用于调整本次页大小，不增加调用额度；若预算不足，必须报告未读部分。')
    if len(json.dumps(header,ensure_ascii=False)) > PAGE_CHAR_LIMIT:
        raise ValueError('COMPONENT_ITEM_TOO_LARGE：分页元数据与对象超过安全响应上限；本页未标记已读。')
    return header
