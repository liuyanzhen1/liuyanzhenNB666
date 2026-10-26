from __future__ import annotations

import asyncio
import json
import mimetypes
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class DashboardServer:
    def __init__(
        self,
        runner: Any,
        loop: asyncio.AbstractEventLoop,
        web_root: Path,
        *,
        port: int = 8765,
    ):
        self.runner = runner
        self.loop = loop
        self.web_root = web_root.resolve()
        self.token = secrets.token_urlsafe(24)
        self.requested_port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url: str | None = None

    def start(self) -> str:
        handler = self._handler_class()
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", self.requested_port), handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        actual_port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{actual_port}/"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="local-dashboard",
            daemon=True,
        )
        self.thread.start()
        return self.url

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=2)

    def _call_runner(self, method_name: str, *args: Any) -> Any:
        method = getattr(self.runner, method_name)
        future = asyncio.run_coroutine_threadsafe(method(*args), self.loop)
        try:
            return future.result(timeout=70)
        except TimeoutError:
            future.cancel()
            raise ValueError("Telegram 响应超时，请稍后重试") from None

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LocalBetDashboard/1.0"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _security_headers(self) -> None:
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
                )

            def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self._security_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _error(self, message: str, status: int) -> None:
                self._json({"ok": False, "error": message}, status)

            def _same_origin(self) -> bool:
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                expected = f"http://{self.headers.get('Host', '')}"
                return origin == expected

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/api/bootstrap":
                    self._json({"ok": True, "token": owner.token})
                    return
                if parsed.path == "/api/dashboard":
                    try:
                        payload = owner._call_runner("dashboard_state")
                    except Exception as exc:
                        self._error(f"读取状态失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
                        return
                    self._json({"ok": True, "data": payload})
                    return
                self._serve_static(parsed.path)

            def _serve_static(self, url_path: str) -> None:
                relative = "index.html" if url_path in {"", "/"} else url_path.lstrip("/")
                target = (owner.web_root / relative).resolve()
                try:
                    target.relative_to(owner.web_root)
                except ValueError:
                    self._error("无效路径", HTTPStatus.BAD_REQUEST)
                    return
                if not target.is_file():
                    self._error("页面不存在", HTTPStatus.NOT_FOUND)
                    return
                body = target.read_bytes()
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self._security_headers()
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != "/api/action":
                    self._error("接口不存在", HTTPStatus.NOT_FOUND)
                    return
                if not self._same_origin():
                    self._error("来源校验失败", HTTPStatus.FORBIDDEN)
                    return
                if not secrets.compare_digest(
                    self.headers.get("X-Local-Token", ""), owner.token
                ):
                    self._error("控制令牌无效", HTTPStatus.FORBIDDEN)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._error("请求长度无效", HTTPStatus.BAD_REQUEST)
                    return
                if length <= 0 or length > 16_384:
                    self._error("请求大小无效", HTTPStatus.BAD_REQUEST)
                    return
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    action = str(payload.get("action", ""))
                    result = owner._call_runner("dashboard_action", action, payload)
                except ValueError as exc:
                    self._error(str(exc), HTTPStatus.BAD_REQUEST)
                    return
                except Exception as exc:
                    self._error(f"操作失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._json({"ok": True, "data": result})

        return Handler
