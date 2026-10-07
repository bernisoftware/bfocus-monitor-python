"""Middleware ASGI puro — FastAPI, Starlette e qualquer app ASGI.

    from bfocus_monitor.asgi import BfocusMiddleware
    app.add_middleware(BfocusMiddleware)

Abre um escopo por requisição (``set_user`` dentro da rota vale só para ela), captura a exceção
não tratada e RELANÇA: o framework responde 500 exatamente como responderia sem o monitor.
Não importa nenhum framework.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, MutableMapping, Optional

from . import _core

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _header(scope: Scope, name: bytes) -> Optional[str]:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return None
    return None


def request_url(scope: Scope) -> Optional[str]:
    """URL da requisição SEM query string."""
    try:
        scheme = scope.get("scheme") or ("wss" if scope.get("type") == "websocket" else "http")
        host = _header(scope, b"host")
        if not host:
            server = scope.get("server")
            if server and server[0]:
                host = f"{server[0]}:{server[1]}" if server[1] not in (None, 80, 443) else str(server[0])
        path = (scope.get("root_path") or "") + (scope.get("path") or "")
        return f"{scheme}://{host}{path}" if host else path or None
    except Exception:
        return None


class BfocusMiddleware:
    """Captura as exceções não tratadas da app ASGI e as relança."""

    def __init__(self, app: ASGIApp, **_: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = None
        try:
            method = scope.get("method") or ("WS" if scope.get("type") == "websocket" else "")
            transaction = f"{method} {(scope.get('root_path') or '') + (scope.get('path') or '/')}".strip()
            token = _core.push_scope(transaction=transaction, url=request_url(scope))
        except Exception:
            token = None
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            try:
                import bfocus_monitor

                bfocus_monitor.capture_exception(exc)
            except Exception:
                pass
            raise
        finally:
            if token is not None:
                _core.pop_scope(token)


__all__ = ["BfocusMiddleware", "request_url"]
