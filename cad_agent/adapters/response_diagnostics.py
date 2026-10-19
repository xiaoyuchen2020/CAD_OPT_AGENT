"""Bounded response metadata; never retain raw bodies, cookies or bearer credentials."""
import json
import re


KNOWN_CODES = {
    "subscription_sharing_user_not_eligible", "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_usage_unavailable", "subscription_sharing_unsupported_capability",
    "subscription_sharing_route_not_supported", "subscription_sharing_invalid_user",
    "subscription_sharing_user_unavailable", "chatpass_v2_scope_not_authorized",
    "chatpass_v2_invalid_authorization_context", "invalid_request_error", "invalid_api_key",
    "model_not_found", "rate_limit_exceeded", "server_error", "unsupported_parameter",
    "unsupported_value", "invalid_value", "insufficient_quota",
}
KNOWN_PARAMS = {"model", "input", "instructions", "tools", "tool_choice", "stream", "store",
                "reasoning", "reasoning.effort", "include", "parallel_tool_calls"}


def media_type(response):
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def response_metadata(response):
    kind = media_type(response)
    diagnostic = {"http_status": response.status_code,
        "content_type": kind if kind in {"", "text/event-stream", "application/json", "text/html",
            "text/plain", "application/octet-stream", "application/problem+json"} else "other",
        "body_shape": "not_read"}
    request_id = response.headers.get("x-request-id", "")
    if re.fullmatch(r"(?:req_[a-zA-Z0-9_-]{1,100}|[a-fA-F0-9-]{32,36})", request_id):
        diagnostic["request_id"] = request_id
    elif request_id:
        diagnostic["request_id"] = "redacted_unrecognized_format"
    return diagnostic


def error_shape(value):
    """Keep recognized machine fields only; server messages may echo user content."""
    if not isinstance(value, dict): return {}
    error = value.get("error")
    if not isinstance(error, dict): return {}
    result = {}
    for key, allowed in (("code", KNOWN_CODES), ("param", KNOWN_PARAMS)):
        item = error.get(key)
        if item is not None:
            result["error_" + key] = item if isinstance(item, str) and item in allowed else "other_redacted"
    return result


def inspect_failure_body(response, diagnostic, remaining):
    """Inspect at most 8 KiB, retaining classification rather than response text."""
    data = bytearray()
    diagnostic["body_truncated"] = False
    try:
        for chunk in response.iter_bytes(chunk_size=1024):
            remaining()
            data.extend(chunk[:8192-len(data)])
            if len(data) >= 8192:
                diagnostic["body_truncated"] = True
                break
        diagnostic["body_bytes_inspected"] = len(data)
        stripped = bytes(data).lstrip()
        if not stripped:
            diagnostic["body_shape"] = "empty"
        elif stripped.lower().startswith((b"<!doctype html", b"<html")):
            diagnostic["body_shape"] = "html"
        elif stripped.startswith((b"data:", b"event:", b":")):
            diagnostic["body_shape"] = "possible_sse_with_wrong_content_type"
        else:
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError):
                diagnostic["body_shape"] = "unparsed_or_truncated"
            else:
                diagnostic["body_shape"] = "json_object" if isinstance(value, dict) else "json_other"
                if isinstance(value, dict):
                    diagnostic["recognized_fields"] = [k for k in ("error", "detail", "message", "status", "object", "output", "usage") if k in value]
                    diagnostic.update(error_shape(value))
    except Exception:
        # Diagnostic failures must not replace the original HTTP/format error.
        diagnostic["body_shape"] = "read_interrupted"
