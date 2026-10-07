"""Integração com o Flask.

    from bfocus_monitor.flask import init_app
    init_app(app)

Escopo por requisição (``before_request``/``teardown_request``) e captura pelo sinal
``got_request_exception`` — o Flask continua respondendo 500 como sempre. O Flask só é importado
dentro de ``init_app``.
"""

from __future__ import annotations

from typing import Any

from . import _core


def init_app(app: Any) -> Any:
    from flask import got_request_exception, request

    def _open() -> None:
        try:
            rule = getattr(request, "url_rule", None)
            path = getattr(rule, "rule", None) or request.path
            _core._request_scope.set(_core.Scope(
                transaction=f"{request.method} {path}",
                url=_core.strip_query(request.base_url),
            ))
        except Exception:
            pass

    def _close(_exc: Any = None) -> None:
        try:
            _core._request_scope.set(None)
        except Exception:
            pass

    def _on_exception(sender: Any, exception: BaseException, **_: Any) -> None:
        try:
            import bfocus_monitor

            bfocus_monitor.capture_exception(exception)
        except Exception:
            pass

    app.before_request(_open)
    app.teardown_request(_close)
    got_request_exception.connect(_on_exception, app, weak=False)
    return app


__all__ = ["init_app"]
