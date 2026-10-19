"""Bounded stdio transport for trusted, pinned harness executables."""

import json
import queue
import subprocess
import threading
from collections import deque

from cad_agent.adapters.model import ModelError


class RpcProcess:
    def __init__(self, argv, env, cwd, remaining):
        self.remaining = remaining
        self.queue = queue.Queue()
        self.stderr = deque(maxlen=30)
        self.sequence = 0
        self.pending = []
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

        def read():
            for line in self.process.stdout:
                try:
                    self.queue.put(json.loads(line))
                except ValueError:
                    self.queue.put({"transport_error": True})
            self.queue.put({"eof": True})

        threading.Thread(target=read, daemon=True).start()
        threading.Thread(
            target=lambda: self.stderr.extend(self.process.stderr), daemon=True
        ).start()

    def send(self, value):
        try:
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, BrokenPipeError):
            raise ModelError("HARNESS_EXITED", "Harness 子进程已退出。") from None

    def receive(self):
        while True:
            try:
                message = self.queue.get(timeout=min(1, self.remaining()))
                break
            except queue.Empty:
                self.remaining()
        if message.get("eof") or message.get("transport_error"):
            raise ModelError(
                "HARNESS_PROTOCOL_ERROR", "Harness 启动失败或 stdio 协议不兼容。"
            )
        return message

    def request(self, method, params):
        self.sequence += 1
        identifier = self.sequence
        self.send(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        )
        while True:
            message = self.receive()
            if message.get("id") == identifier and "method" not in message:
                if "error" in message:
                    self.stderr.append(json.dumps(message["error"]))
                    raise ModelError(
                        "HARNESS_RPC_ERROR",
                        f"Harness 接口 {method} 拒绝请求；请核查固定版本配置。",
                    )
                return message.get("result")
            self.pending.append(message)

    def events(self):
        while self.pending:
            yield self.pending.pop(0)
        while True:
            yield self.receive()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()
