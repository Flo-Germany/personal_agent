"""Phase 30: token-authenticated LAN bridge for the Android companion.

The desktop API stays on loopback. This module runs a second, deliberately
narrow ASGI app on ``0.0.0.0`` that exposes only:

* ``/ws``        authenticated WebSocket for tasks, approvals and push events
* ``/api/poll``  HTTP fallback used by Android WorkManager
* ``/health``    unauthenticated liveness probe

Every payload endpoint requires the pairing token stored next to the settings.
"""

from __future__ import annotations

import asyncio
import collections
import hmac
import json
import logging
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import uvicorn

from settings import BridgeSettings

logger = logging.getLogger(__name__)

BRIDGE_VERSION = 1


def _lan_addresses() -> list[str]:
    addresses: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = info[4][0]
            if address not in addresses and not address.startswith("127."):
                addresses.append(address)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if address not in addresses and not address.startswith("127."):
                addresses.insert(0, address)
        finally:
            probe.close()
    except OSError:
        pass
    return addresses


class LanBridge:
    """Owns the bridge app, pairing token, client registry and event ring."""

    def __init__(
        self,
        settings: BridgeSettings,
        chat_service: Any,
        store: Any,
    ) -> None:
        self.settings = settings
        self._chat = chat_service
        self._store = store
        self.app = self._build_app()
        self._clients: set[WebSocket] = set()
        self._events: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=settings.poll_retention
        )
        self._cursor = 0
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._error = ""
        self._token = ""
        self._token_guard = threading.Lock()

    # ------------------------------------------------------------- pairing
    def token(self) -> str:
        """Read or create the persistent pairing token."""
        with self._token_guard:
            if self._token:
                return self._token
            path = self.settings.resolved_token_path()
            try:
                if path.is_file():
                    value = path.read_text(encoding="utf-8").strip()
                    if value:
                        self._token = value
                        return value
            except OSError as exc:
                logger.warning("Could not read bridge token: %s", exc)
            value = secrets.token_urlsafe(32)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(value + "\n", encoding="utf-8")
                temp.replace(path)
            except OSError as exc:
                logger.warning("Could not persist bridge token: %s", exc)
            self._token = value
            return value

    def _authorized(self, value: str | None) -> bool:
        if not value:
            return False
        return hmac.compare_digest(value.strip(), self.token())

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if not self.settings.enabled or self._running:
            return self._running
        self.token()
        config = uvicorn.Config(
            self.app,
            host=self.settings.host,
            port=self.settings.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._serve,
            name="lan-bridge",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if self._server.started:
                self._running = True
                self._error = ""
                logger.info(
                    "LAN bridge listening on %s:%s",
                    self.settings.host,
                    self.settings.port,
                )
                return True
            if self._thread is not None and not self._thread.is_alive():
                break
            time.sleep(0.05)
        self._error = "The bridge port could not be opened (already in use?)"
        logger.warning("LAN bridge failed to start on port %s", self.settings.port)
        return False

    def _serve(self) -> None:
        assert self._server is not None
        try:
            self._server.run()
        except Exception as exc:  # noqa: BLE001 - status reports the failure
            self._error = str(exc)
            logger.warning("LAN bridge stopped: %s", exc)
        finally:
            self._running = False

    def stop(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=4)
        self._server = None
        self._thread = None
        self._running = False
        self._loop = None
        with self._lock:
            self._clients.clear()

    # --------------------------------------------------------------- events
    def broadcast(self, event: dict[str, Any]) -> dict[str, Any]:
        """Store and push one event. Safe to call from any thread."""
        with self._lock:
            self._cursor += 1
            payload = {
                **event,
                "seq": self._cursor,
                "at": time.time(),
                "origin": event.get("origin", "desktop"),
            }
            self._events.append(payload)
            loop = self._loop
            clients = list(self._clients)
        if loop is not None and clients:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._send_payload(payload),
                    loop,
                )
            except Exception:  # noqa: BLE001 - polling still has the event
                logger.debug("Bridge push failed", exc_info=True)
        return payload

    def notify(self, title: str, body: str, kind: str = "info") -> dict[str, Any]:
        return self.broadcast(
            {
                "type": "notification",
                "title": title,
                "body": body,
                "kind": kind,
                "origin": "desktop",
            }
        )

    async def _send_payload(self, payload: dict[str, Any]) -> None:
        with self._lock:
            clients = list(self._clients)
        dead: list[WebSocket] = []
        for client in clients:
            try:
                await client.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(client)
        if dead:
            with self._lock:
                for client in dead:
                    self._clients.discard(client)

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        addresses = _lan_addresses()
        with self._lock:
            clients = len(self._clients)
            cursor = self._cursor
        return {
            "enabled": self.settings.enabled,
            "running": self._running,
            "host": self.settings.host,
            "port": self.settings.port,
            "addresses": addresses,
            "urls": [
                f"ws://{address}:{self.settings.port}/ws" for address in addresses
            ],
            "clients": clients,
            "cursor": cursor,
            "token": self.token() if self.settings.enabled else "",
            "error": self._error,
            "version": BRIDGE_VERSION,
        }

    # ----------------------------------------------------------------- tasks
    def _run_turn(self, conversation_id: str, text: str) -> None:
        try:
            self.broadcast(
                {
                    "type": "task_accepted",
                    "conversation_id": conversation_id,
                    "text": text,
                    "origin": "mobile",
                }
            )
            for event in self._chat.start_turn(conversation_id, text):
                event = {**event, "origin": "mobile"}
                self.broadcast(event)
        except Exception as exc:  # noqa: BLE001 - report instead of dying
            logger.warning("Mobile task failed: %s", exc)
            self.broadcast(
                {
                    "type": "error",
                    "conversation_id": conversation_id,
                    "message": f"Mobile task failed: {exc}",
                    "origin": "mobile",
                }
            )

    def _run_resume(self, approval_id: str, approved: bool) -> None:
        try:
            for event in self._chat.resume(approval_id, approved):
                self.broadcast({**event, "origin": "mobile"})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mobile approval failed: %s", exc)
            self.broadcast(
                {
                    "type": "error",
                    "message": f"Approval failed: {exc}",
                    "origin": "mobile",
                }
            )

    # ------------------------------------------------------------------- app
    def _build_app(self) -> FastAPI:
        app = FastAPI(
            title="Compa LAN Bridge",
            version=str(BRIDGE_VERSION),
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {
                "status": "ok",
                "service": "compa-bridge",
                "version": BRIDGE_VERSION,
            }

        def require_token(request: Request, token: str | None = None) -> str:
            value = (
                token
                or request.query_params.get("token")
                or request.headers.get("x-compa-token")
            )
            if not self._authorized(value):
                raise HTTPException(status_code=401, detail="Invalid pairing token")
            return self.token()

        @app.get("/api/status")
        async def api_status(request: Request) -> JSONResponse:
            require_token(request)
            payload = self.status()
            payload.pop("token", None)
            return JSONResponse(payload)

        @app.get("/api/poll")
        async def api_poll(
            request: Request,
            since: int = 0,
            limit: int = 100,
        ) -> JSONResponse:
            require_token(request)
            limit = max(1, min(limit, 500))
            with self._lock:
                events = [
                    event for event in self._events if int(event.get("seq", 0)) > since
                ][-limit:]
                cursor = self._cursor
            return JSONResponse({"events": events, "cursor": cursor})

        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket) -> None:
            query_token = websocket.query_params.get("token") or websocket.headers.get(
                "x-compa-token"
            )
            await websocket.accept()
            authorized = self._authorized(query_token)
            if not authorized:
                try:
                    first = await asyncio.wait_for(
                        websocket.receive_text(), timeout=10.0
                    )
                    message = json.loads(first)
                    authorized = self._authorized(str(message.get("token") or ""))
                except Exception:  # noqa: BLE001 - malformed hello
                    authorized = False
            if not authorized:
                await websocket.send_json(
                    {"type": "error", "error": "unauthorized", "code": 4401}
                )
                await websocket.close(code=4401)
                return

            with self._lock:
                self._loop = asyncio.get_running_loop()
                self._clients.add(websocket)
                cursor = self._cursor
            try:
                await websocket.send_json(
                    {
                        "type": "hello",
                        "ok": True,
                        "version": BRIDGE_VERSION,
                        "cursor": cursor,
                        "addresses": _lan_addresses(),
                    }
                )
                while True:
                    raw = await websocket.receive_text()
                    if len(raw) > 16_000:
                        await websocket.send_json(
                            {"type": "error", "error": "message too large"}
                        )
                        continue
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        await websocket.send_json(
                            {"type": "error", "error": "invalid json"}
                        )
                        continue
                    kind = str(message.get("type") or "")
                    if kind in {"ping", "hello"}:
                        await websocket.send_json(
                            {"type": "pong", "at": time.time(), "cursor": self._cursor}
                        )
                    elif kind == "task":
                        text = str(message.get("text") or "").strip()
                        if not text:
                            await websocket.send_json(
                                {"type": "error", "error": "empty task"}
                            )
                            continue
                        if len(text) > 8000:
                            await websocket.send_json(
                                {"type": "error", "error": "task too long"}
                            )
                            continue
                        conversation_id = str(
                            message.get("conversation_id")
                            or self._store.new_conversation_id()
                        )
                        threading.Thread(
                            target=self._run_turn,
                            args=(conversation_id, text),
                            name="bridge-task",
                            daemon=True,
                        ).start()
                    elif kind == "approve":
                        approval_id = str(message.get("approval_id") or "")
                        if not approval_id:
                            await websocket.send_json(
                                {"type": "error", "error": "missing approval_id"}
                            )
                            continue
                        threading.Thread(
                            target=self._run_resume,
                            args=(approval_id, bool(message.get("approved"))),
                            name="bridge-approval",
                            daemon=True,
                        ).start()
                    elif kind == "cancel":
                        conversation_id = str(
                            message.get("conversation_id") or ""
                        )
                        interrupt = getattr(self._chat, "interrupt", None)
                        cancelled = bool(
                            interrupt(conversation_id) if interrupt else False
                        )
                        await websocket.send_json(
                            {
                                "type": "cancelled",
                                "conversation_id": conversation_id,
                                "ok": cancelled,
                            }
                        )
                    else:
                        await websocket.send_json(
                            {"type": "error", "error": f"unknown type '{kind}'"}
                        )
            except WebSocketDisconnect:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.debug("Bridge websocket closed: %s", exc)
            finally:
                with self._lock:
                    self._clients.discard(websocket)

        return app

    # --------------------------------------------------------------- helpers
    def events_since(self, cursor: int) -> Iterator[dict[str, Any]]:
        with self._lock:
            snapshot = list(self._events)
        return iter(event for event in snapshot if int(event.get("seq", 0)) > cursor)
