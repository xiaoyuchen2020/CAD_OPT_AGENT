"""Authenticated LAN conversion; never use JSON geometry as a substitute for DXF."""
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from cad_agent.config import Settings


class ConversionError(Exception):
    def __init__(self, code, message, status_code=502):
        self.code = code
        self.message = message + " 本次导入已终止。"
        self.status_code = status_code
        super().__init__(self.message)


@dataclass(frozen=True)
class ConvertedDrawing:
    dxf: bytes
    json_bytes: bytes
    metadata: dict


class OdaConverter:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport

    def status(self):
        return {"configured": bool(self.settings.oda_base_url and self.settings.oda_api_key),
                "automatic_dwg_to_dxf": True, "outputs": ["dxf", "json"]}

    def convert(self, filename: str, content: bytes) -> ConvertedDrawing:
        settings = self.settings
        if not re.match(rb"AC\d{4}", content[:6]):
            raise ValueError("DWG 文件头无效，无法转换；本次导入已终止。")
        url = urlsplit(settings.oda_base_url)
        if not self.status()["configured"]:
            raise ConversionError("ODA_NOT_CONFIGURED", "尚未配置内网 DWG 转换服务的访问密钥。", 503)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ConversionError("ODA_CONFIGURATION_INVALID", "内网 DWG 转换服务地址配置无效。", 503)
        if (not math.isfinite(settings.oda_timeout_seconds) or settings.oda_timeout_seconds <= 0
                or not math.isfinite(settings.oda_poll_seconds) or settings.oda_poll_seconds <= 0):
            raise ConversionError("ODA_CONFIGURATION_INVALID", "内网转换等待时间配置无效。", 503)
        deadline = time.monotonic() + settings.oda_timeout_seconds

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise ConversionError("ODA_TIMEOUT", "等待内网 DWG 转换或下载超时，请稍后重试。", 504)
            return value

        def request(client, method, path, *, limit, body=None, headers=None):
            budget = remaining()
            with client.stream(method, path, content=body, headers=headers,
                               timeout=httpx.Timeout(min(30, budget), connect=min(5, budget))) as response:
                if response.status_code in (401, 403):
                    raise ConversionError("ODA_AUTH_FAILED", "内网转换服务拒绝了访问密钥，请检查后端配置。", 503)
                if response.status_code == 429:
                    raise ConversionError("ODA_BUSY", "内网转换服务正在限流，请稍后重试。", 503)
                if not 200 <= response.status_code < 300:
                    raise ConversionError("ODA_HTTP_ERROR", f"内网转换服务返回 HTTP {response.status_code}，未取得转换结果。")
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    remaining()
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ConversionError("ODA_RESULT_TOO_LARGE", "内网转换结果超过允许大小。")
                remaining()
                return bytes(data)

        def object_response(raw):
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ConversionError("ODA_INVALID_RESPONSE", "内网转换服务返回了无效 JSON。") from None
            if not isinstance(value, dict):
                raise ConversionError("ODA_INVALID_RESPONSE", "内网转换服务响应格式不正确。")
            return value

        def invalid():
            raise ConversionError("ODA_RESULT_MISMATCH", "转换结果的来源或完整性校验失败，不能作为当前图纸导入。")

        digest = sha256(content).hexdigest()
        try:
            # No environment proxy, redirects or automatic POST retries: the key
            # and company drawing must go only to this server-configured origin.
            with httpx.Client(base_url=settings.oda_base_url.rstrip("/") + "/",
                              headers={"X-API-Key": settings.oda_api_key},
                              trust_env=False, follow_redirects=False, transport=self.transport) as client:
                job = object_response(request(client, "POST", "jobs", limit=65536, body=content,
                    headers={"Content-Type": "application/octet-stream", "X-Filename": quote(filename, safe="")}))
                job_id = job.get("id")
                if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
                    invalid()
                if job.get("sha256") != digest:
                    invalid()
                while True:
                    state = object_response(request(client, "GET", f"jobs/{job_id}", limit=65536))
                    if state.get("id") != job_id or state.get("sha256") != digest:
                        invalid()
                    if state.get("status") == "succeeded":
                        break
                    if state.get("status") == "failed":
                        # Classify known failures without echoing arbitrary native logs.
                        error = str(state.get("error", "")).lower()
                        if "429" in error:
                            raise ConversionError("ODA_TRIAL_RATE_LIMITED", "ODA 试用授权暂时限流，请冷却后重试。", 503)
                        if "authorization" in error:
                            raise ConversionError("ODA_TRIAL_UNAVAILABLE", "ODA 试用授权不可用，请管理员刷新官方授权。", 503)
                        raise ConversionError("ODA_CONVERSION_FAILED", "ODA 转换失败，图纸可能损坏、不受支持或超出服务资源限制。")
                    if state.get("status") not in ("queued", "running", "uploading"):
                        invalid()
                    time.sleep(min(settings.oda_poll_seconds, remaining()))
                raw_json = request(client, "GET", f"jobs/{job_id}/result", limit=settings.oda_max_result_bytes)
                result = object_response(raw_json)
                source, companion = result.get("input"), result.get("companion_dxf")
                if not isinstance(source, dict) or not isinstance(companion, dict):
                    invalid()
                if (source.get("sha256") != digest or source.get("bytes") != len(content)
                        or result.get("schema_version") != "2.0"
                        or not isinstance(result.get("entities"), list)
                        or len(result["entities"]) != result.get("entity_records_in_all_block_definitions")):
                    invalid()
                export = result.get("dxf_export", {})
                if not isinstance(export, dict):
                    invalid()
                omitted = export.get("omitted_null_block_reference_handles", [])
                if (not isinstance(omitted, list) or len(omitted) > len(result["entities"])
                        or any(not isinstance(h, str) or not re.fullmatch(r"[A-Fa-f0-9]{1,16}", h)
                               for h in omitted)):
                    invalid()
                if len(set(omitted)) != len(omitted):
                    invalid()
                if omitted:
                    eligible = {e.get("handle") for e in result["entities"]
                                if isinstance(e, dict) and isinstance(e.get("handle"), str)
                                and e.get("type") in ("AcDbBlockReference", "AcDbMInsertBlock")
                                and "referenced_block" in e and e["referenced_block"] is None
                                and e.get("attributes") == []}
                    if not set(omitted) <= eligible:
                        invalid()
                if (not isinstance(companion.get("bytes"), int) or companion["bytes"] <= 0
                        or companion["bytes"] > settings.oda_max_result_bytes
                        or not re.fullmatch(r"[a-f0-9]{64}", str(companion.get("sha256", "")))):
                    invalid()
                # Ignore response-supplied URLs to prevent cross-origin key leakage.
                dxf = request(client, "GET", f"jobs/{job_id}/dxf", limit=settings.oda_max_result_bytes)
                if len(dxf) != companion["bytes"] or sha256(dxf).hexdigest() != companion["sha256"]:
                    invalid()
                return ConvertedDrawing(dxf, raw_json, {
                    "provider": "oda_lan", "job_id": job_id,
                    "schema_version": result["schema_version"], "sdk_version": result.get("sdk_version"),
                    "extractor_revision": result.get("extractor_revision"),
                    "input_sha256": digest, "dxf_sha256": companion["sha256"],
                    "json_sha256": sha256(raw_json).hexdigest(),
                    "omitted_null_block_reference_handles": omitted,
                })
        except httpx.TimeoutException:
            raise ConversionError("ODA_TIMEOUT", "连接内网转换服务或等待响应超时，请检查网络后重试。", 504) from None
        except httpx.HTTPError:
            raise ConversionError("ODA_UNAVAILABLE", "无法连接内网 DWG 转换服务，请确认已接入公司内网且服务正在运行。", 503) from None
