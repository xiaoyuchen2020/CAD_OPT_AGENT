"""Local-only Codex subscription adapter using official managed authentication.

The application never reads tokens or desktop credentials. Codex owns login and
refresh inside an isolated application home. Only fixed CAD functions are exposed.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from cad_agent.adapters.model import ModelError
from cad_agent.analysis.efficiency import compact, dumps
from cad_agent.analysis.session import tool_definitions, validation_feedback
from cad_agent.runtimes.external import CODEX_VERSION
from cad_agent.runtimes.rpc import RpcProcess

MODELS = {"codex_sol_56": "gpt-5.6-sol", "codex_sol": "gpt-6.1-sol", "codex_astra": "gpt-6-astra"}
CONFIG = '''model_provider = "openai"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
cli_auth_credentials_store = "file"
[features]
shell_tool = false
unified_exec = false
apply_patch_freeform = false
multi_agent = false
js_repl = false
[analytics]
enabled = false
'''


def initialize(rpc):
    rpc.request("initialize", {
        "clientInfo": {"name": "cad_opt_agent", "title": "CAD Atlas", "version": "0.1.0"},
        "capabilities": {"experimentalApi": True},
    })
    rpc.send({"jsonrpc": "2.0", "method": "initialized"})


class CodexAccount:
    def __init__(self, data_dir):
        self.home = Path(data_dir).resolve() / "codex-subscription"
        self.lock = threading.RLock()
        self.rpc = None
        self.deadline = 0
        self.pending_login = None
        self.last_login_failed = False
        self.active_runs = 0

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ModelError("CODEX_ACCOUNT_TIMEOUT", "Codex 登录状态查询超时，请稍后刷新。")
        return left

    def launch(self, cwd, remaining):
        binary = shutil.which("codex")
        if not binary:
            raise ModelError("HARNESS_NOT_INSTALLED", "未找到 Codex CLI。")
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        if version != "codex-cli " + CODEX_VERSION:
            raise ModelError("HARNESS_VERSION_MISMATCH", "Codex CLI 版本与本地适配版本不一致。")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        (self.home / "config.toml").write_text(CONFIG)
        # Intentionally do not inherit API keys, proxy providers, the desktop's
        # CODEX_HOME or user configuration. Credentials are managed by Codex.
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if k in os.environ}
        env["CODEX_HOME"] = str(self.home)
        env["TMPDIR"] = str(self.home)
        cwd.mkdir(parents=True, exist_ok=True)
        rpc = RpcProcess([binary, "app-server", "--listen", "stdio://"], env, str(cwd), remaining)
        try:
            initialize(rpc)
        except Exception:
            rpc.close()
            raise
        return rpc

    def client(self):
        self.deadline = time.monotonic() + 20
        if self.rpc is None or self.rpc.process.poll() is not None:
            if self.rpc:
                self.rpc.close()
            self.rpc = self.launch(self.home / "account-workspace", self.remaining)
            self.pending_login = None
        return self.rpc

    def status(self):
        with self.lock:
            if not shutil.which("codex"):
                return {"available": False, "connected": False, "models": []}
            rpc = self.client()
            data = rpc.request("account/read", {"refreshToken": False})
            for event in rpc.pending:
                params = event.get("params") or {}
                if event.get("method") == "account/login/completed" and self.pending_login and params.get("loginId") == self.pending_login.get("loginId"):
                    self.last_login_failed = not params.get("success", False)
                    self.pending_login = None
            account = data.get("account") or {}
            connected = account.get("type") == "chatgpt"
            if connected:
                self.pending_login = None
            auth = self.home / "auth.json"
            if auth.is_file():
                auth.chmod(0o600)
            # Account notifications can contain identity details. Do not persist
            # or return them, and never return credentials or raw stderr.
            rpc.pending.clear()
            return {"available": True, "connected": connected,
                    "plan_type": account.get("planType") if connected else None,
                    "login_pending": bool(self.pending_login),
                    "login_failed": self.last_login_failed,
                    "models": list(MODELS.values()), "model_access_verified": False,
                    "billing": "codex_subscription", "active_runs": self.active_runs}

    def login(self, flow="browser"):
        with self.lock:
            if flow not in ("browser", "device"):
                raise ValueError("不支持的登录方式。")
            if self.active_runs:
                raise ValueError("请等待订阅问答结束后再切换登录。")
            if self.status()["connected"]:
                return {"connected": True}
            rpc = self.client()
            kind = "chatgptDeviceCode" if flow == "device" else "chatgpt"
            if self.pending_login and self.pending_login.get("type") != kind:
                rpc.request("account/login/cancel", {"loginId": self.pending_login["loginId"]})
                self.pending_login = None
            result = self.pending_login or rpc.request("account/login/start", {"type": kind})
            url = urlsplit(result.get("verificationUrl" if flow == "device" else "authUrl", ""))
            if url.scheme != "https" or url.hostname not in ("auth.openai.com", "chatgpt.com", "auth0.openai.com"):
                raise ModelError("CODEX_LOGIN_URL", "Codex 未返回可识别的官方登录地址。")
            self.pending_login = result
            self.last_login_failed = False
            if flow == "device":
                return {"auth_url": result["verificationUrl"], "user_code": result["userCode"], "flow": "device", "connected": False}
            return {"auth_url": result["authUrl"], "connected": False}

    def logout(self):
        with self.lock:
            if self.active_runs:
                raise ValueError("请等待订阅问答结束后再断开连接。")
            rpc = self.client()
            if self.pending_login:
                try:
                    rpc.request("account/login/cancel", {"loginId": self.pending_login["loginId"]})
                except ModelError:
                    pass  # It may have completed since the last status read.
            rpc.request("account/logout", {})
            self.pending_login = None
            rpc.pending.clear()
            return {"connected": False}

    @contextmanager
    def lease(self):
        with self.lock:
            if not self.status()["connected"]:
                raise ModelError("CODEX_LOGIN_REQUIRED", "请先在页面的 Codex 订阅入口完成官方 ChatGPT 登录。")
            if self.active_runs:
                raise ModelError("CODEX_SUBSCRIPTION_BUSY", "暂时只允许一个订阅问答同时执行，以控制用量。")
            self.active_runs += 1
        try:
            yield
        finally:
            with self.lock:
                self.active_runs -= 1

    def close(self):
        with self.lock:
            if self.rpc:
                self.rpc.close()
                self.rpc = None


class CodexSubscriptionRuntime:
    name = "codex_subscription"
    # Usage arrives after generation, so these are soft thresholds, not a
    # per-request token cap. Reserve room for one bounded, tool-free answer.
    EVIDENCE_STOP_TOKENS = 30000
    EXPLORATION_STOP_TOKENS = 40000
    TOTAL_SOFT_TOKENS = 50000
    FINAL_CONTEXT_CHARS = 24000

    def __init__(self):
        self.source_contexts = []

    def exploration_budget(self, session):
        operations={op for s in session.task.data['subtasks'] for op in s.get('operations',[])}
        return min(120000,self.EXPLORATION_STOP_TOKENS+10000*len(operations)+8000*max(0,len(session.task.data["subtasks"])-1))

    def total_budget(self, session):
        # Stateless final synthesis replays a bounded evidence packet. Reserve
        # BOTH final delivery and its one allowed correction before exploration
        # starts; a large last tool request must not silently erase correction.
        # Usage is reported after generation, so this remains a soft threshold.
        return self.exploration_budget(session)+2*(self.FINAL_CONTEXT_CHARS+len(session.system_prompt))

    def stop_reason(self, session):
        tokens = sum(u.get("total_tokens", 0) for u in session.usage)
        if session.policy.stop_reason:
            return session.policy.stop_reason
        pending=session.task.pending_work()
        exploration_limit=self.exploration_budget(session)
        pending_images=set(getattr(session,'visual_images',{}))-set(getattr(session,'reviewed_image_ids',set()))
        # Fetching a source image creates an atomic review obligation. Reserve
        # one bounded decision turn instead of discarding an unseen image at
        # the exploration threshold. The total/time/tool limits still apply.
        prepared={'prepare_space_connections','prepare_discovery_review','prepare_room_boundaries','render_region'}
        last_index=next((i for i in range(len(session.trace)-1,-1,-1) if session.trace[i].get('status')=='completed'),-1)
        last=session.trace[last_index] if last_index>=0 else {}
        # One decision and one input repair only. A failed review must not
        # repeatedly inherit the same preparation's budget exemption.
        attempts=len(session.trace)-last_index-1
        discovery_ready=last.get('tool')=='discover_rooms' and any(o['tool']=='discover_rooms' and o['data'].get('total',0)>0 for o in getattr(session,'observations',{}).values())
        if (pending_images and last.get('tool') in prepared or discovery_ready) and attempts<2 and tokens < self.total_budget(session)-self.FINAL_CONTEXT_CHARS and session.calls<session.w.settings.agent_max_tool_calls and session._remaining()>45:
            return None
        if tokens >= exploration_limit:
            return "subscription_exploration_budget"
        has_evidence = session.evidence or any(o['tool'] in ('measure_geometry', 'measure_room', 'get_measurement_evidence') for o in getattr(session, 'observations', {}).values())
        if has_evidence and tokens >= self.EVIDENCE_STOP_TOKENS and not pending and (not session.task.complex or not session.focused_enabled or session.focus.review_ready()):
            return "subscription_answer_reserve"
        if session.calls >= session.w.settings.agent_max_tool_calls:
            return "tool_call_budget"
        if has_evidence and session._remaining() <= (90 if session.task.complex else 45):
            return "subscription_answer_time_reserve"
        return None

    def remember_context(self, tool, value):
        if tool != "read_source_context" or not value.get("target_region_id"):
            return
        self.source_contexts.append({
            "target_region_id": value["target_region_id"],
            "items": [{k: item[k] for k in (
                "region_id", "relationship", "relation_kind", "relation_basis",
                "relation_state", "position_relative_to_target",
                "parser_context_region_ids", "truncated", "next_start",
            ) if k in item} for item in value.get("items", [])],
        })

    def start_thread(self, rpc, session, cwd, final=False):
        result = rpc.request("thread/start", {
            "model": session.w.settings.model, "modelProvider": "openai",
            "cwd": str(cwd), "sandbox": "read-only", "approvalPolicy": "never",
            "baseInstructions": session.system_prompt,
            "developerInstructions": "仅使用应用提供的 CAD 函数；不执行代码或读取本机文件。",
            "ephemeral": True, "environments": [],
            "dynamicTools": [] if final else [{
                "type": "function", "name": t["function"]["name"],
                "description": t["function"]["description"], "inputSchema": t["function"]["parameters"],
            } for t in session.offered_tools()],
        })
        if result.get("model", session.w.settings.model) != session.w.settings.model:
            raise ModelError("MODEL_MISMATCH", "Codex 未采用所选模型，已停止。")
        return result["thread"]["id"]

    @staticmethod
    def usage(session, params, totals):
        thread = params.get("threadId", "")
        total = params.get("tokenUsage", {}).get("total", {})
        mapping = {"total_tokens": "totalTokens", "prompt_tokens": "inputTokens",
                   "completion_tokens": "outputTokens", "prompt_cache_hit_tokens": "cachedInputTokens",
                   "reasoning_tokens": "reasoningOutputTokens"}
        old = totals.get(thread, {})
        delta = {k: max(0, total.get(v, 0) - old.get(v, 0)) for k, v in mapping.items()}
        if delta["total_tokens"]:
            session.usage.append(delta | {"source": "codex_app_server", "billing": "subscription"})
            totals[thread] = total

    def turn(self, rpc, session, sid, prompt, totals, final=False):
        response = rpc.request("turn/start", {"threadId": sid,
            "input": [{"type": "text", "text": prompt}], "effort": "low"})
        turn_id = response["turn"]["id"]
        text = ""
        stopping = False
        allowed = {t["function"]["name"] for t in tool_definitions()}
        while True:
            session._remaining()
            event = rpc.pending.pop(0) if rpc.pending else rpc.receive()
            method = event.get("method")
            params = event.get("params") or {}
            if params.get("threadId", sid) != sid:
                continue
            if "id" in event and method:
                if method != "item/tool/call" or params.get("tool") not in allowed or final:
                    rpc.send({"id": event["id"], "error": {"code": -32601, "message": "Only registered CAD tools allowed"}})
                    raise ModelError("HARNESS_PERMISSION_DENIED", "Codex 请求了未授权操作，已拒绝。")
                if stopping:
                    value = dumps({"error": {"code": "EXPLORATION_STOPPED",
                        "message": "探索已停止；不会再执行工具。"}})
                else:
                    value = session.invoke_tool(params["tool"], params["arguments"])
                    self.remember_context(params["tool"], json.loads(value))
                rpc.send({"id": event["id"], "result": {"contentItems": [{"type": "inputText", "text": value}],
                                                        "success": "error" not in json.loads(value)}})
            if method == "thread/tokenUsage/updated":
                self.usage(session, params, totals)
                session.progress(stage="Codex 订阅 · 正在分析", usage=session.usage, trace=session.trace)
            if method == "model/rerouted":
                raise ModelError("MODEL_MISMATCH", "服务尝试切换所选模型，已停止；请重新选择模型。")
            if method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                text = params["item"].get("text", "")
            if method == "turn/completed":
                status = params["turn"]["status"]
                if status == "interrupted" and stopping:
                    return text or None
                if status != "completed":
                    raise ModelError("CODEX_TURN_FAILED", "Codex 订阅回合未完成；请检查登录、模型权限和剩余额度。")
                return text
            # Never throw away a completed answer because its usage event comes
            # before turn/completed. The final turn has no tools or retry loop.
            reason = self.stop_reason(session) if not final and not stopping else None
            if reason and not text:
                session.policy.stop_reason = reason
                stopping = True
                session.progress(stage="Codex 订阅 · 停止探索，准备最终回答",
                                 usage=session.usage, trace=session.trace)
                rpc.request("turn/interrupt", {"threadId": sid, "turnId": turn_id})

    def run(self, session, history=None):
        if session.w.settings.model not in MODELS.values() or session.w.settings.provider != "codex_subscription":
            raise ModelError("MODEL_MISMATCH", "Codex 订阅入口只能选择已配置的订阅模型。")
        account = session.w.codex_account
        with account.lease():
            cwd = account.home / "runs" / uuid4().hex
            rpc = account.launch(cwd, session._remaining)
            try:
                sid = self.start_thread(rpc, session, cwd)
                messages = session.build_messages(history)
                prompt = dumps(messages[1:])
                totals = {}
                content = self.turn(rpc, session, sid, prompt, totals)
                issue = None
                if content is not None:
                    try:
                        return self.finish(session, content)
                    except ValueError as error:
                        issue = validation_feedback(error)
                # At most one dedicated synthesis/repair turn, with no tools.
                # It gets only verified evidence already read during exploration.
                session.policy.stop_reason = session.policy.stop_reason or "answer_repair"
                prompt = dumps({"question": session.request.question,
                    "evidence": compact(list(session.evidence.values())),
                    "source_contexts": self.source_contexts,
                    "observations": list(session.observations.values()),
                    "validation_issue": issue,
                    "stop_reason": session.policy.stop_reason,
                    "instruction": "仅依据以上已读来源返回最终 JSON，通常1–2条结论；预算收束不代表证据充分，不足标 unknown。保留适用范围与来源引用，不调用工具。"})
                if len(prompt) > self.FINAL_CONTEXT_CHARS:
                    raise ModelError("CODEX_CONTEXT_BUDGET", "已有证据超过最终整理预算，未继续消耗订阅额度。")
                sid = self.start_thread(rpc, session, cwd, final=True)
                content = self.turn(rpc, session, sid, prompt, totals, final=True)
                try:
                    return self.finish(session, content)
                except ValueError:
                    raise ModelError("ANSWER_VALIDATION_FAILED", "订阅模型答案未通过引用校验，未发布；没有再次重试。") from None
            finally:
                # Do not persist raw authentication or runtime stderr output.
                rpc.close()

    def finish(self, session, content):
        tokens = sum(u.get("total_tokens", 0) for u in session.usage)
        return session.finish(content) | {"billing": "codex_subscription",
            "harness_version": CODEX_VERSION, "transport": "codex_managed_chatgpt",
            "model_access_verified": True,
            "subscription_budget": {
                "evidence_stop_tokens": self.EVIDENCE_STOP_TOKENS,
                "exploration_stop_tokens": self.EXPLORATION_STOP_TOKENS,
                "total_soft_tokens": self.TOTAL_SOFT_TOKENS,
                "reported_tokens": tokens,
                "soft_limit_exceeded": tokens > self.TOTAL_SOFT_TOKENS,
                "final_turn_limit": 1,
            },
            "usage_note": "Codex 报告的 Token；消耗账户订阅额度，不等同于 API 美元费用。内部模型请求次数未逐次计量。"}
