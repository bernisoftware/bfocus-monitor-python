"""Middleware do Django (síncrono e assíncrono).

    MIDDLEWARE = [..., "bfocus_monitor.django.BfocusMiddleware"]

Abre um escopo por requisição e captura em ``process_exception`` — o Django continua tratando a
exceção como sempre (página 500, ``handler500``, log). Não importa o Django no import.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from . import _core


def _transaction(request: Any) -> str:
    method = getattr(request, "method", "") or ""
    match = getattr(request, "resolver_match", None)
    route = getattr(match, "route", None) if match is not None else None
    path = ("/" + str(route).lstrip("/")) if route else (getattr(request, "path", "") or "/")
    return f"{method} {path}".strip()


def _url(request: Any) -> Optional[str]:
    try:
        # build_absolute_uri(path) sem a query string.
        return _core.strip_query(request.build_absolute_uri(request.path))
    except Exception:
        return _core.strip_query(getattr(request, "path", None))


def _is_async(fn: Any) -> bool:
    try:
        from asgiref.sync import iscoroutinefunction

        return bool(iscoroutinefunction(fn))
    except Exception:
        return inspect.iscoroutinefunction(fn)


class BfocusMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response
        self._async = _is_async(get_response)
        if self._async:
            try:
                from asgiref.sync import markcoroutinefunction

                markcoroutinefunction(self)
            except Exception:
                if hasattr(inspect, "markcoroutinefunction"):  # 3.12+
                    inspect.markcoroutinefunction(self)
                else:  # 3.9–3.11: a marca que o asyncio.iscoroutinefunction procura
                    import asyncio.coroutines as _aco

                    self._is_coroutine = getattr(_aco, "_is_coroutine", None)

    def __call__(self, request: Any) -> Any:
        if self._async:
            return self.__acall__(request)
        token = self._open(request)
        try:
            return self.get_response(request)
        finally:
            if token is not None:
                _core.pop_scope(token)

    async def __acall__(self, request: Any) -> Any:
        token = self._open(request)
        try:
            return await self.get_response(request)
        finally:
            if token is not None:
                _core.pop_scope(token)

    @staticmethod
    def _open(request: Any) -> Any:
        try:
            return _core.push_scope(transaction=_transaction(request), url=_url(request))
        except Exception:
            return None

    def process_exception(self, request: Any, exception: BaseException) -> None:
        try:
            scope = _core.current_scope()
            scope.transaction = _transaction(request)  # a rota já foi resolvida aqui
            import bfocus_monitor

            bfocus_monitor.capture_exception(exception)
        except Exception:
            pass
        return None  # o Django segue tratando


__all__ = ["BfocusMiddleware"]
