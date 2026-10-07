"""bFocus Monitor para Python — os erros não tratados do seu sistema viram demanda no bFocus.

    import bfocus_monitor

    bfocus_monitor.init(key="bf_mon_…", release="1.4.2", environment="production",
                        signing_secret=os.environ.get("BFOCUS_SIGNING_SECRET"))

Uma linha liga tudo: ``sys.excepthook``, ``threading.excepthook``, o loop do asyncio (quando o
``init`` roda dentro dele; fora, use ``install_asyncio_handler(loop)``) e um flush de até 2 s no
encerramento. Os ganchos ENCADEIAM o que já existia: o app quebra exatamente como quebraria sem o
monitor. Integrações: ``bfocus_monitor.asgi`` (FastAPI/Starlette), ``bfocus_monitor.django``,
``bfocus_monitor.flask``.

Nenhuma função daqui levanta exceção por causa do monitor (só ``init`` sem chave).
"""

from __future__ import annotations

import asyncio
import atexit
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

from ._core import (
    CLIENT_ID,
    DEFAULT_BASE_URL,
    SDK_NAME,
    BeforeSend,
    Client,
    IgnoreRule,
    _Identity,
    current_scope,
    pop_scope,
    push_scope,
    sign_user,
)
from ._version import __version__

__all__ = [
    "__version__",
    "SDK_NAME",
    "CLIENT_ID",
    "Client",
    "init",
    "capture_exception",
    "capture_message",
    "set_user",
    "set_tag",
    "add_breadcrumb",
    "flush",
    "close",
    "sign_user",
    "install_asyncio_handler",
    "get_client",
]

_client: Optional[Client] = None
_hooks_installed = False
_init_lock = threading.Lock()


def get_client() -> Optional[Client]:
    """O cliente do último ``init`` (ou ``None``)."""
    return _client


def init(
    key: str,
    release: Optional[str] = None,
    environment: str = "production",
    base_url: str = DEFAULT_BASE_URL,
    sample_rate: float = 1.0,
    ignore: Optional[Iterable[IgnoreRule]] = None,
    before_send: Optional[BeforeSend] = None,
    signing_secret: Optional[str] = None,
    auto_capture: bool = True,
    in_app_prefixes: Optional[Iterable[str]] = None,
    **_internal: Any,
) -> Client:
    """Liga o monitor. O ``init`` não espera a rede: o sinal de vida sai numa thread daemon (e
    depois a cada 5 min) e a thread de envio dos erros nasce no primeiro evento.

    - ``ignore``: textos (contidos na mensagem) ou ``re.Pattern``.
    - ``before_send``: recebe o evento (dict) e devolve o evento alterado ou ``None`` (descarta).
    - ``signing_secret``: segredo da chave de assinatura do sistema — com ele, ``set_user`` assina
      a identidade sozinho (a mesma assinatura v2 do widget). Só em servidor.
    - ``in_app_prefixes``: módulos/caminhos que são do seu sistema mesmo instalados em site-packages.
    """
    global _client
    new = Client(
        key, release=release, environment=environment, base_url=base_url, sample_rate=sample_rate,
        ignore=ignore, before_send=before_send, signing_secret=signing_secret,
        auto_capture=auto_capture, in_app_prefixes=in_app_prefixes, **_internal,
    )
    with _init_lock:
        old, _client = _client, new
    if old is not None:
        try:
            old.close(timeout=0.5)
        except Exception:
            pass
    new.start_heartbeat()  # sinal de vida em segundo plano: o init não espera a rede
    if auto_capture:
        _install_hooks()
        try:
            install_asyncio_handler(asyncio.get_running_loop())
        except RuntimeError:
            pass  # sem loop rodando agora
    return new


def capture_exception(
    exc: Optional[BaseException] = None,
    level: str = "error",
    tags: Optional[Dict[str, Any]] = None,
    fingerprint: Optional[Sequence[str]] = None,
) -> None:
    """Manda uma exceção. Sem ``exc``, usa a que está sendo tratada (``sys.exc_info()``)."""
    c = _client
    if c is None:
        return
    if exc is None:
        exc = sys.exc_info()[1]
        if exc is None:
            return
    if not isinstance(exc, BaseException):
        c.capture_message(str(exc), level=level, tags=tags, fingerprint=fingerprint)
        return
    c.capture_exception(exc, level=level, tags=tags, fingerprint=fingerprint)


def capture_message(message: str, level: str = "info", tags: Optional[Dict[str, Any]] = None,
                    fingerprint: Optional[Sequence[str]] = None) -> None:
    """Manda uma mensagem (sem exceção) com o nível pedido."""
    c = _client
    if c is not None:
        c.capture_message(message, level=level, tags=tags, fingerprint=fingerprint)


def set_user(external_id: Any, customer_external_id: Any = None, user_hash: Optional[str] = None) -> None:
    """Quem foi afetado. Dentro de uma requisição (integração de framework) vale só para ela.

    Com ``signing_secret`` no ``init`` o pacote assina sozinho; sem ele, passe o ``user_hash`` que
    o seu servidor já gera para o widget. ``set_user(None)`` limpa.
    """
    try:
        scope = current_scope()
        if external_id is None or str(external_id) == "":
            scope.identity = None
            return
        customer = None if customer_external_id in (None, "") else str(customer_external_id)
        scope.identity = _Identity(str(external_id), customer, str(user_hash) if user_hash else None)
    except Exception:
        pass


def set_tag(key: str, value: Any) -> None:
    try:
        if key is None or value is None:
            return
        current_scope().tags[str(key)[:64]] = str(value)[:200]
    except Exception:
        pass


def add_breadcrumb(category: str, message: str, level: str = "info") -> None:
    """Um passo antes do erro (até 30, os mais recentes)."""
    try:
        current_scope().breadcrumbs.append({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "category": str(category)[:40],
            "message": str(message)[:300],
            "level": level if level in ("fatal", "error", "warning", "info") else "info",
        })
    except Exception:
        pass


def flush(timeout: float = 2.0) -> bool:
    """Bloqueia até a fila esvaziar ou o ``timeout`` (CLI, serverless, fim de job)."""
    c = _client
    return c.flush(timeout) if c is not None else True


def close(timeout: float = 2.0) -> None:
    """Envia o que falta (até ``timeout``) e desliga o monitor."""
    global _client
    with _init_lock:
        c, _client = _client, None
    if c is not None:
        c.close(timeout)


# ── ganchos globais (instalados uma vez; olham o cliente corrente) ───────────

_prev_excepthook: Optional[Callable[..., Any]] = None
_prev_threading_hook: Optional[Callable[..., Any]] = None


def _active() -> Optional[Client]:
    c = _client
    return c if c is not None and c.auto_capture else None


def _excepthook(exc_type: Any, exc: Any, tb: Any) -> None:
    try:
        c = _active()
        if c is not None and isinstance(exc, BaseException) and not isinstance(exc, KeyboardInterrupt):
            if exc.__traceback__ is None and tb is not None:
                exc = exc.with_traceback(tb)
            c.capture_exception(exc, level="fatal")
            c.flush(2.0)
    except Exception:
        pass
    prev = _prev_excepthook or sys.__excepthook__
    prev(exc_type, exc, tb)


def _threading_excepthook(args: Any) -> None:
    try:
        c = _active()
        exc = getattr(args, "exc_value", None)
        if c is not None and isinstance(exc, BaseException) and not isinstance(exc, SystemExit):
            c.capture_exception(exc, level="error")
    except Exception:
        pass
    prev = _prev_threading_hook or getattr(threading, "__excepthook__", None)
    if prev is not None:
        prev(args)


def _at_exit() -> None:
    try:
        c = _client
        if c is not None:
            c.flush(2.0)
    except Exception:
        pass


def _after_fork_in_child() -> None:
    try:
        c = _client
        if c is not None:
            c._after_fork()
    except Exception:
        pass


def _install_hooks() -> None:
    global _hooks_installed, _prev_excepthook, _prev_threading_hook
    with _init_lock:
        if _hooks_installed:
            return
        _hooks_installed = True
    _prev_excepthook = sys.excepthook
    sys.excepthook = _excepthook
    _prev_threading_hook = threading.excepthook
    threading.excepthook = _threading_excepthook
    atexit.register(_at_exit)
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_after_fork_in_child)


def install_asyncio_handler(loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
    """Captura as exceções que o loop do asyncio só registraria (tarefa sem ``await``, callbacks).

    Encadeia o handler que o loop já tinha (ou o padrão). Sem ``loop``, usa o que está rodando.
    """
    try:
        if loop is None:
            loop = asyncio.get_running_loop()
        prev = loop.get_exception_handler()
        if getattr(prev, "_bfocus_monitor", False):
            return True

        def handler(lp: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
            try:
                c = _client  # instalado de propósito: vale mesmo com auto_capture=False
                exc = context.get("exception")
                if c is not None and isinstance(exc, BaseException) and not isinstance(exc, asyncio.CancelledError):
                    c.capture_exception(exc, level="error")
            except Exception:
                pass
            if prev is not None:
                prev(lp, context)
            else:
                lp.default_exception_handler(context)

        handler._bfocus_monitor = True  # type: ignore[attr-defined]
        loop.set_exception_handler(handler)
        return True
    except Exception:
        return False


# usado pelas integrações
_push_scope = push_scope
_pop_scope = pop_scope
