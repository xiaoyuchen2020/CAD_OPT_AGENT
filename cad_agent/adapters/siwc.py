"""Independent public-client SIWC authorization. Never reads Codex credentials."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs
from uuid import uuid4
import base64
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time

import httpx
import jwt
from cad_agent.adapters.model import ModelError

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE


def fail(code, message):
    return ModelError("SIWC_" + code, message)


class SiwcAccount:
    def __init__(self, data_dir):
        self.home = Path(data_dir).resolve() / "siwc-subscription"
        self.lock = threading.RLock()
        self.pending = None
        self.listener = None
        self.timer = None
        self.active_runs = 0
        self.last_error = None

    def client(self):
        return httpx.Client(timeout=20, follow_redirects=False, trust_env=False)

    @contextmanager
    def storage(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        fd = os.open(self.home / "storage.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def load(self):
        p = self.home / "accounts.json"
        return json.loads(p.read_text()) if p.exists() else {"host_id": "urn:uuid:" + str(uuid4()), "accounts": {}, "active": None}

    def save(self, data):
        fd, name = tempfile.mkstemp(dir=self.home, prefix=".credentials-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f); f.flush(); os.fsync(f.fileno())
            os.replace(name, self.home / "accounts.json")
        finally:
            if os.path.exists(name): os.unlink(name)

    def status(self):
        with self.lock, self.storage():
            data = self.load(); record = data["accounts"].get(data["active"], {})
            connected = bool(record.get("access_token") and PLAN_SCOPE in record.get("scopes", []))
            return {"connected": connected, "active_account": data["active"],
                    "accounts": [{"id": k, "label": v["label"], "connected": bool(v.get("access_token"))}
                                 for k, v in data["accounts"].items()],
                    "login_pending": bool(self.pending), "last_error": self.last_error,
                    "active_runs": self.active_runs, "billing": "subscription",
                    "model_access_verified": False}

    def _stop_listener(self):
        if self.timer:
            self.timer.cancel(); self.timer = None
        if self.listener:
            server, self.listener = self.listener, None
            def stop():
                server.shutdown(); server.server_close()
            threading.Thread(target=stop, daemon=True).start()

    def close(self):
        with self.lock:
            self.pending = None
            self._stop_listener()

    def login(self, account_id=None):
        with self.lock, self.storage():
            if self.active_runs: raise ValueError("请等待订阅问答完成后再切换账户。")
            data = self.load()
            if account_id and account_id not in data["accounts"]: raise ValueError("未知账户。")
            record = data["accounts"].get(account_id, {})
            self._stop_listener()
            owner = self
            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args): pass  # Authorization codes must never enter access logs.
                def do_GET(self):
                    if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                        self.send_error(400); return
                    parsed = urlsplit(self.path)
                    if parsed.path != "/auth/callback": self.send_error(404); return
                    try:
                        args = parse_qs(parsed.query, max_num_fields=16)
                        if any(len(v) != 1 for v in args.values()): raise ValueError("Duplicate OAuth parameter")
                        owner.callback({k:v[0] for k,v in args.items()})
                        message = "授权已完成。请回到 CAD Atlas 刷新登录状态。"
                    except Exception:
                        message = "授权未完成。请回到 CAD Atlas 查看状态并重新登录。"
                    body = ("<!doctype html><meta charset=utf-8><title>CAD Atlas</title><p>" + message + "</p>").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
            self.listener = server
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            verifier = secrets.token_urlsafe(48)
            self.pending = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                            "verifier": verifier, "redirect_uri": redirect, "expires": time.time()+600,
                            "client_id": record.get("client_id"), "subject": record.get("subject"),
                            "account_id": account_id}
            self.last_error = None
            self.save(data)  # Persist stable host before first authorization.
            params = {"client_id": record.get("client_id", "dynamic_agent_client"),
                      "ext_agent_host_id": data["host_id"], "response_type": "code",
                      "redirect_uri": redirect, "scope": SCOPES, "resource": RESOURCE,
                      "state": self.pending["state"], "nonce": self.pending["nonce"],
                      "code_challenge_method": "S256",
                      "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()}
            if not record: params["agent_name_hint"] = "CAD Atlas"
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.timer = threading.Timer(600, self.close); self.timer.daemon = True; self.timer.start()
            return {"auth_url": AUTHORIZE + "?" + urlencode(params), "expires_in": 600}

    def validate_identity(self, token, client_id, nonce=None):
        with self.client() as c:
            jwks = c.get(ISSUER + "/.well-known/jwks.json").raise_for_status().json()
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in ("RS256", "ES256"): raise ValueError("Unexpected signature algorithm")
        keys = [k for k in jwks["keys"] if k.get("kid") == header.get("kid")]
        if len(keys) != 1: raise ValueError("Unknown signing key")
        claims = jwt.decode(token, jwt.PyJWK.from_dict(keys[0]).key, algorithms=[header["alg"]],
                            audience=client_id, issuer=ISSUER, options={"require": ["iss", "aud", "exp", "sub"]})
        if claims.get("azp", client_id) != client_id: raise ValueError("Authorized party mismatch")
        if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id:
            raise ValueError("Missing authorized party")
        if nonce and not secrets.compare_digest(claims.get("nonce", ""), nonce): raise ValueError("Nonce mismatch")
        if not isinstance(claims["sub"], str) or not claims["sub"]: raise ValueError("Missing identity")
        return claims

    def token_record(self, response, previous=None):
        previous = previous or {}
        scopes = response.get("scope")
        scopes = scopes.split() if isinstance(scopes, str) else previous.get("scopes", [])
        if PLAN_SCOPE not in scopes or "resource.invoke" not in scopes:
            raise fail("PLAN_PERMISSION", "未授权 ChatGPT 订阅使用权限，请重新授权。")
        if response.get("token_type", "").lower() != "bearer" or not response.get("access_token"):
            raise ValueError("Invalid token response")
        expires = int(response["expires_in"])
        if expires <= 0: raise ValueError("Invalid expiry")
        return previous | {"access_token": response["access_token"],
            "refresh_token": response.get("refresh_token", previous.get("refresh_token")),
            "id_token": response.get("id_token", previous.get("id_token")),
            "scopes": scopes, "expires_at": time.time()+expires}

    def callback(self, args):
        with self.lock, self.storage():
            p = self.pending
            if not p or p["expires"] < time.time() or not secrets.compare_digest(args.get("state", ""), p["state"]):
                raise fail("STATE", "登录状态校验失败或已过期。")
            self.pending = None  # Consume exactly once before any network call.
            try:
                if args.get("error"): raise fail("CONSENT", "授权未完成或被取消。")
                cid = args.get("client_id") or p["client_id"]
                if not cid or cid == "dynamic_agent_client" or (p["client_id"] and cid != p["client_id"]):
                    raise ValueError("Client registration mismatch")
                if not args.get("code"): raise ValueError("Missing code")
                with self.client() as c:
                    response = c.post(TOKEN, data={"grant_type": "authorization_code", "client_id": cid,
                        "code": args["code"], "code_verifier": p["verifier"],
                        "redirect_uri": p["redirect_uri"], "resource": RESOURCE}).raise_for_status().json()
                claims = self.validate_identity(response["id_token"], cid, p["nonce"])
                if p["subject"] and claims["sub"] != p["subject"]: raise ValueError("Account identity mismatch")
                data = self.load()
                key = hashlib.sha256((cid + "\0" + claims["sub"]).encode()).hexdigest()[:24]
                record = self.token_record(response)
                record.update(client_id=cid, subject=claims["sub"], label=f"ChatGPT 账户 {key[:6]}")
                data["accounts"][key] = record; data["active"] = key
                self.save(data); self.last_error = None
            except Exception as error:
                self.last_error = error.code if isinstance(error, ModelError) else "SIWC_AUTH_FAILED"
                raise fail("AUTH_FAILED", "授权校验或交换失败；请重新登录。") from None
            finally:
                self._stop_listener()

    def access_token(self):
        with self.lock, self.storage():
            data = self.load(); key = data["active"]; r = data["accounts"].get(key, {})
            if not r.get("access_token") or PLAN_SCOPE not in r.get("scopes", []):
                raise fail("LOGIN_REQUIRED", "请先完成独立的 Sign in with ChatGPT 授权。")
            if r["expires_at"] <= time.time()+60:
                if not r.get("refresh_token"): raise fail("LOGIN_REQUIRED", "订阅授权已过期，请重新登录。")
                try:
                    with self.client() as c:
                        response = c.post(TOKEN, data={"grant_type": "refresh_token", "client_id": r["client_id"],
                            "refresh_token": r["refresh_token"], "resource": RESOURCE}).raise_for_status().json()
                    if response.get("id_token"):
                        claims = self.validate_identity(response["id_token"], r["client_id"])
                        if claims["sub"] != r["subject"]: raise ValueError("Identity changed")
                    r = self.token_record(response, r); data["accounts"][key] = r; self.save(data)
                except Exception:
                    raise fail("REFRESH_FAILED", "订阅授权刷新失败；未发送推理请求，请重新登录。") from None
            return r["access_token"]

    def models(self):
        try:
            with self.client() as c:
                response = c.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + self.access_token()}).raise_for_status().json()
            return {"items": [{"id": m["slug"], "label": m.get("display_name", m["slug"])}
                              for m in response["models"] if m.get("visibility") == "list"]}
        except ModelError: raise
        except Exception: raise fail("CATALOG_FAILED", "订阅模型目录读取失败；未调用模型。") from None

    @contextmanager
    def lease(self):
        with self.lock:
            if self.active_runs: raise fail("BUSY", "已有直接订阅问答运行，请等待完成。")
            if not self.status()["connected"]: raise fail("LOGIN_REQUIRED", "请先完成独立的 Sign in with ChatGPT 授权。")
            fd = os.open(self.home / "run.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise fail("BUSY", "另一个进程正在运行直接订阅问答。") from None
            self.active_runs += 1
        try: yield
        finally:
            with self.lock:
                self.active_runs -= 1
                os.close(fd)

    def logout(self):
        with self.lock, self.storage():
            if self.active_runs: raise ValueError("请等待问答完成后再断开连接。")
            self.pending = None; self._stop_listener()
            data = self.load(); key = data["active"]; record = data["accounts"].get(key, {})
            revoked = not bool(record.get("refresh_token"))
            try:
                if record.get("refresh_token"):
                    with self.client() as c:
                        discovery = c.get(ISSUER + "/.well-known/openid-configuration").raise_for_status().json()
                        endpoint = discovery["revocation_endpoint"]
                        url = urlsplit(endpoint)
                        if url.scheme != "https" or url.netloc != "auth.openai.com": raise ValueError("Invalid endpoint")
                        revoked = c.post(endpoint, data={"token": record["refresh_token"],
                            "token_type_hint": "refresh_token", "client_id": record["client_id"]}).status_code == 200
            except Exception: revoked = False
            for field in ("access_token", "refresh_token", "id_token", "expires_at", "scopes"):
                record.pop(field, None)
            if key: data["accounts"][key] = record
            self.save(data)
            return {"connected": False, "remote_revocation_confirmed": revoked}
