"""Núcleo do monitor: monta o evento, segura a fila e envia em segundo plano.

Contrato do evento e regras de envio: ``monitor/BRIEF.md`` (no monorepo do bFocus).

- Nunca derruba o app: toda falha do monitor (rede, serialização, bug nosso) é engolida.
- Nenhuma chamada de rede no import, e o ``init`` não espera a rede: o sinal de vida (heartbeat)
  sai numa thread daemon (no init e a cada 5 min); a thread dos erros nasce no primeiro evento.
- Fila limitada (100 eventos; cheia → descarta o mais novo), lote a cada 1 s ou 20 eventos.
- 429/5xx/rede → uma nova tentativa depois de 2 s; 401/403 → para de enviar até o próximo ``init``.
- O mesmo erro no máximo 1 vez a cada 30 s, e no máximo 100 eventos por minuto.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import platform
import socket
import random
import re
import sys
import sysconfig
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Pattern, Sequence, Tuple, Union

from ._version import __version__

SDK_NAME = "bfocus-monitor-python"
CLIENT_ID = f"{SDK_NAME}/{__version__}"
DEFAULT_BASE_URL = "https://api.bfocus.com.br"
EVENTS_PATH = "/api/v1/monitor/events"
HEARTBEAT_PATH = "/api/v1/monitor/heartbeat"
HEARTBEAT_INTERVAL = 300.0

LEVELS = ("fatal", "error", "warning", "info")
MAX_QUEUE = 100
BATCH_SIZE = 20
FLUSH_INTERVAL = 1.0
RETRY_DELAY = 2.0
DEDUPE_SECONDS = 30.0
MAX_PER_MINUTE = 100
MAX_CRUMBS = 30
MAX_FRAMES = 60
MAX_TAGS = 20
MAX_MESSAGE = 2000
MAX_EVENT_BYTES = 64 * 1024
MAX_CHAIN = 10
SIGN_MAX_AGE = 6 * 86400
HTTP_TIMEOUT = 5.0

IgnoreRule = Union[str, Pattern[str]]
BeforeSend = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]


# ── identidade assinada (a mesma do widget, v2) ─────────────────────────────


def sign_user(secret: str, user_external_id: str, customer_external_id: str, ts: Optional[int] = None) -> str:
    """``v2.<ts>.<hex(HMAC_SHA256(secret, "v2:<ts>:<user>:<customer>"))>`` — a assinatura do widget.

    Útil também para entregar o ``userHash`` ao front (widget, ``@bfocus/monitor``).
    """
    if ts is None:
        ts = int(time.time())
    msg = f"v2:{int(ts)}:{user_external_id}:{customer_external_id}".encode("utf-8")
    digest = hmac.new(str(secret).encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"v2.{int(ts)}.{digest}"


class _Identity:
    __slots__ = ("user_id", "customer_id", "user_hash", "_signed")

    def __init__(self, user_id: str, customer_id: Optional[str], user_hash: Optional[str]) -> None:
        self.user_id = user_id
        self.customer_id = customer_id
        self.user_hash = user_hash
        self._signed: Optional[Tuple[str, int, str]] = None  # (segredo, ts, hash)

    def hash_for(self, secret: Optional[str], now: int) -> Optional[str]:
        if self.user_hash:
            return self.user_hash
        if not secret:
            return None
        cached = self._signed
        # Recalcula se o segredo mudou ou se a assinatura passou de 6 dias.
        if cached is None or cached[0] != secret or now - cached[1] > SIGN_MAX_AGE or cached[1] > now + 300:
            cached = (secret, now, sign_user(secret, self.user_id, self.customer_id or "", now))
            self._signed = cached
        return cached[2]


# ── escopo: global e por requisição (contextvars) ───────────────────────────


class Scope:
    """O que vai junto de todo evento: identidade, tags, passos (breadcrumbs), transação e URL."""

    __slots__ = ("identity", "tags", "breadcrumbs", "transaction", "url")

    def __init__(self, transaction: Optional[str] = None, url: Optional[str] = None) -> None:
        self.identity: Optional[_Identity] = None
        self.tags: Dict[str, str] = {}
        self.breadcrumbs: Deque[Dict[str, str]] = deque(maxlen=MAX_CRUMBS)
        self.transaction = transaction
        self.url = url


_global_scope = Scope()
_request_scope: "contextvars.ContextVar[Optional[Scope]]" = contextvars.ContextVar("bfocus_monitor_scope", default=None)


def current_scope() -> Scope:
    """O escopo da requisição corrente (integração de framework) ou o global."""
    return _request_scope.get() or _global_scope


def push_scope(transaction: Optional[str] = None, url: Optional[str] = None) -> "contextvars.Token[Optional[Scope]]":
    """Abre um escopo de requisição: dois usuários simultâneos nunca trocam de identidade."""
    return _request_scope.set(Scope(transaction=transaction, url=strip_query(url)))


def pop_scope(token: "contextvars.Token[Optional[Scope]]") -> None:
    try:
        _request_scope.reset(token)
    except (ValueError, RuntimeError):  # token de outro contexto: só limpa
        _request_scope.set(None)


def strip_query(url: Optional[str]) -> Optional[str]:
    """A query string e o fragmento são onde mora token, e-mail e CPF: nunca saem."""
    if not url:
        return None
    return re.split(r"[?#]", str(url), maxsplit=1)[0][:1000] or None


# ── frames ──────────────────────────────────────────────────────────────────

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _canon(path: str) -> str:
    # realpath: no Homebrew/pyenv o stdlib é visto por link simbólico e pelo caminho real.
    return os.path.normcase(os.path.realpath(path))


def _library_dirs() -> Tuple[str, ...]:
    cands = [os.path.dirname(os.__file__)]  # o stdlib de verdade, seja qual for a instalação
    try:
        paths = sysconfig.get_paths()
        cands += [paths.get(n) or "" for n in ("stdlib", "platstdlib", "purelib", "platlib")]
    except Exception:
        pass
    for p in (getattr(sys, "base_prefix", None), getattr(sys, "base_exec_prefix", None)):
        if p:
            cands += [os.path.join(p, "lib"), os.path.join(p, "Lib")]
    dirs = set()
    for c in cands:
        if c and os.path.isdir(c):
            dirs.add(c)
            dirs.add(_canon(c))
    return tuple(sorted(os.path.normcase(os.path.abspath(d)) for d in dirs))


_LIB_DIRS = _library_dirs()
_LIB_MARKERS = ("site-packages", "dist-packages")


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def is_library_file(filename: str) -> bool:
    """Biblioteca (stdlib, site-packages, o próprio monitor) não é código do sistema."""
    if not filename or filename.startswith("<"):
        return True
    norm = filename.replace("\\", "/")
    if any(f"/{m}/" in norm for m in _LIB_MARKERS):
        return True
    if not os.path.isabs(filename):
        return False
    if _own_file(filename):
        return True
    cwd = os.path.normcase(os.getcwd())
    for path in {os.path.normcase(os.path.abspath(filename)), _canon(filename)}:
        for base in _LIB_DIRS:
            # Projeto dentro do prefixo do Python (raro): o cwd vence a heurística.
            if _under(path, base) and not (_under(path, cwd) and not _under(cwd, base)):
                return True
    return False


def _own_file(filename: str) -> bool:
    if not filename or not os.path.isabs(filename):
        return False
    return _under(os.path.normcase(os.path.abspath(filename)), os.path.normcase(_PKG_DIR)) or \
        _under(_canon(filename), _canon(_PKG_DIR))


def relative_path(filename: str, cwd: Optional[str] = None) -> str:
    """Caminho relativo à raiz do projeto (o diretório atual) quando der."""
    if not filename or filename.startswith("<") or not os.path.isabs(filename):
        return filename
    base = (cwd or os.getcwd()).rstrip(os.sep) + os.sep
    out = filename[len(base):] if filename.startswith(base) else filename
    return out.replace("\\", "/") if os.sep == "\\" else out


def frame_dict(
    filename: str,
    function: Optional[str],
    line: Optional[int],
    col: Optional[int] = None,
    module: Optional[str] = None,
    in_app_prefixes: Sequence[str] = (),
) -> Dict[str, Any]:
    """Um frame do contrato. ``col`` já em base 1."""
    rel = relative_path(filename)
    own = _own_file(filename)
    in_app = not is_library_file(filename)
    if not own and in_app_prefixes:
        for p in in_app_prefixes:
            if (module and (module == p or module.startswith(p.rstrip(".") + "."))) or rel.startswith(p):
                in_app = True
                break
    out: Dict[str, Any] = {"file": rel, "function": function, "line": line, "col": col, "inApp": in_app}
    return {k: v for k, v in out.items() if v is not None and v != ""}


def frames_from_traceback(tb: Any, in_app_prefixes: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Traceback do Python (já de FORA para DENTRO) → frames do contrato, o último é onde estourou."""
    if tb is None:
        return []
    walked = list(traceback.walk_tb(tb))
    if len(walked) > MAX_FRAMES:
        walked = walked[-MAX_FRAMES:]
    summary = traceback.StackSummary.extract(iter(walked), lookup_lines=False)
    out = []
    for (frame, _lineno), fs in zip(walked, summary):
        code = frame.f_code
        func = getattr(code, "co_qualname", None) or code.co_name
        colno = getattr(fs, "colno", None)
        module = frame.f_globals.get("__name__") if isinstance(frame.f_globals, dict) else None
        out.append(frame_dict(
            fs.filename, func, fs.lineno,
            col=(colno + 1) if isinstance(colno, int) else None,
            module=module if isinstance(module, str) else None,
            in_app_prefixes=in_app_prefixes,
        ))
    return out


def exception_chain(exc: BaseException) -> List[BaseException]:
    """De fora para dentro: a exceção capturada, a causa dela, a causa da causa…"""
    chain = [exc]
    seen = {id(exc)}
    cur = exc
    while len(chain) < MAX_CHAIN:
        nxt = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)
        if nxt is None or id(nxt) in seen:
            break
        chain.append(nxt)
        seen.add(id(nxt))
        cur = nxt
    return chain


def _exc_message(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:
        return "<mensagem ilegível>"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _contexts() -> Dict[str, Dict[str, str]]:
    ctx: Dict[str, Dict[str, str]] = {}
    try:
        ctx["runtime"] = {"name": "python", "version": platform.python_version()}
        osname = platform.system()
        if osname:
            ctx["os"] = {"name": osname, "version": platform.release()}
    except Exception:
        pass
    return ctx


# ── o cliente ───────────────────────────────────────────────────────────────


class Client:
    """Uma instância por ``init``. Use as funções do módulo ``bfocus_monitor``."""

    def __init__(
        self,
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
        _retry_delay: float = RETRY_DELAY,
        _clock: Optional[Callable[[], float]] = None,
        _heartbeat: bool = True,
    ) -> None:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("bfocus_monitor.init: `key` é obrigatória (a chave do agente, bf_mon_…)")
        self.key = key.strip()
        self.release = str(release) if release not in (None, "") else None
        self.environment = str(environment) if environment else "production"
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.endpoint = self.base_url + EVENTS_PATH
        try:
            rate = float(sample_rate)
        except (TypeError, ValueError):
            rate = 1.0
        self.sample_rate = min(1.0, max(0.0, rate))
        self.ignore: List[IgnoreRule] = list(ignore or [])
        self.before_send = before_send
        self.signing_secret = signing_secret or None
        self.auto_capture = bool(auto_capture)
        self.in_app_prefixes: Tuple[str, ...] = tuple(p for p in (in_app_prefixes or []) if p)
        self.retry_delay = float(_retry_delay)
        self._clock = _clock or time.time
        self.contexts = _contexts()

        self.disabled = False  # 401/403: até o próximo init
        self.closed = False
        self._seen: Dict[str, float] = {}
        self._minute: Deque[float] = deque()
        self._queue: Deque[Dict[str, Any]] = deque()
        self._inflight = 0
        self._flush_now = False
        self._stopping = False
        self._thread: Optional[threading.Thread] = None
        self.heartbeat_url = self.base_url + HEARTBEAT_PATH
        self._heartbeat_enabled = bool(_heartbeat)
        self._heartbeat_interval = HEARTBEAT_INTERVAL
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._reset_locks()

    def start_heartbeat(self) -> None:
        """Sinal de vida: um já (em segundo plano, o init não espera) e depois a cada 5 min.

        Thread daemon: não segura o processo vivo.
        """
        if not self._heartbeat_enabled or self.closed or self.disabled:
            return
        try:
            t = self._hb_thread
            if t is not None and t.is_alive():
                return
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="bfocus-monitor-heartbeat", daemon=True)
            self._hb_thread.start()
        except Exception:
            pass

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.is_set() and not self.closed and not self.disabled:
            try:
                self.send_heartbeat()
            except Exception:
                pass
            if self._hb_stop.wait(self._heartbeat_interval):
                return

    def heartbeat_body(self) -> Dict[str, Any]:
        try:
            host = socket.gethostname() or None
        except Exception:
            host = None
        instance = hashlib.sha1(f"{host or ''}:{os.getpid()}".encode("utf-8")).hexdigest()[:12]
        body: Dict[str, Any] = {
            "instance": instance,
            "release": self.release,
            "environment": self.environment,
            "host": host[:200] if host else None,
            "runtime": self.contexts.get("runtime"),
            "sdk": {"name": SDK_NAME, "version": __version__},
        }
        return {k: v for k, v in body.items() if v is not None}

    def send_heartbeat(self) -> Optional[int]:
        """Um sinal de vida. 401/403 desliga o envio (como nos eventos); rede: o próximo tenta."""
        if self.closed or self.disabled:
            return None
        body = json.dumps(self.heartbeat_body(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        status = self._post(body, self.heartbeat_url)
        if status in (401, 403):
            self.disabled = True
            self._hb_stop.set()
            with self._cond:
                self._queue.clear()
                self._cond.notify_all()
        return status

    def _reset_locks(self) -> None:
        self._lock = threading.Lock()  # dedupe e teto por minuto
        self._cond = threading.Condition(threading.Lock())

    def _after_fork(self) -> None:
        # gunicorn/uwsgi com preload: a thread não atravessa o fork, e um lock pode ter ficado preso.
        self._reset_locks()
        self._thread = None
        self._inflight = 0
        self._hb_thread = None
        self._hb_stop = threading.Event()
        self.start_heartbeat()  # o filho é outro processo (outro pid, outra instância)

    # ── captura ──

    def capture_exception(
        self,
        exc: BaseException,
        level: str = "error",
        tags: Optional[Dict[str, Any]] = None,
        fingerprint: Optional[Sequence[str]] = None,
    ) -> None:
        try:
            chain = exception_chain(exc)
            inner = chain[-1]
            message = _exc_message(inner)
            if len(chain) > 1:
                outer = chain[0]
                message = f"{message} (dentro de: {type(outer).__name__}: {_exc_message(outer)})"
            frames = frames_from_traceback(inner.__traceback__, self.in_app_prefixes)
            if not frames and inner is not exc:
                frames = frames_from_traceback(exc.__traceback__, self.in_app_prefixes)
            self._enqueue(type(inner).__name__, message, frames, level, tags, fingerprint)
        except Exception:
            pass

    def capture_message(self, message: str, level: str = "info", tags: Optional[Dict[str, Any]] = None,
                        fingerprint: Optional[Sequence[str]] = None) -> None:
        try:
            text = str(message)
            self._enqueue("Message", text, [], level, tags, list(fingerprint) if fingerprint else [text[:200]])
        except Exception:
            pass

    def _ignored(self, message: str) -> bool:
        for rule in self.ignore:
            try:
                if isinstance(rule, str):
                    if rule and rule in message:
                        return True
                elif hasattr(rule, "search") and rule.search(message):
                    return True
            except Exception:
                continue
        return False

    def _allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last < DEDUPE_SECONDS:
                return False
            while self._minute and now - self._minute[0] >= 60.0:
                self._minute.popleft()
            if len(self._minute) >= MAX_PER_MINUTE:
                return False
            self._seen[key] = now
            self._minute.append(now)
            if len(self._seen) > 1000:  # não cresce sem fim num processo longo
                for k in [k for k, t in self._seen.items() if now - t >= DEDUPE_SECONDS]:
                    del self._seen[k]
            return True

    def _enqueue(self, exc_type: str, message: str, frames: List[Dict[str, Any]], level: str,
                 tags: Optional[Dict[str, Any]], fingerprint: Optional[Sequence[str]]) -> None:
        if self.closed or self.disabled:
            return
        if self._ignored(message):
            return
        if self.sample_rate < 1.0 and random.random() >= self.sample_rate:
            return
        top = next((f for f in reversed(frames) if f.get("inApp")), frames[-1] if frames else {})
        if not self._allow(f"{exc_type}|{message}|{top.get('file')}:{top.get('line')}"):
            return
        event: Optional[Dict[str, Any]] = self._build(exc_type, message, frames, level, tags, fingerprint)
        if self.before_send is not None:
            try:
                event = self.before_send(event)  # type: ignore[arg-type]
            except Exception:
                pass  # beforeSend com erro: manda como está
        if not isinstance(event, dict):
            return
        event = _fit(event)
        if event is None:
            return
        with self._cond:
            if len(self._queue) >= MAX_QUEUE:
                return  # cheia: descarta o mais novo
            self._queue.append(event)
            self._ensure_worker()
            self._cond.notify_all()

    def _build(self, exc_type: str, message: str, frames: List[Dict[str, Any]], level: str,
               tags: Optional[Dict[str, Any]], fingerprint: Optional[Sequence[str]]) -> Dict[str, Any]:
        req = _request_scope.get()
        scopes = [_global_scope] + ([req] if req is not None else [])
        merged_tags: Dict[str, str] = {}
        crumbs: List[Dict[str, str]] = []
        identity: Optional[_Identity] = None
        for s in scopes:
            merged_tags.update(s.tags)
            crumbs.extend(s.breadcrumbs)
            if s.identity is not None:
                identity = s.identity
        for k, v in (tags or {}).items():
            if k is not None and v is not None:
                merged_tags[str(k)[:64]] = str(v)[:200]
        event: Dict[str, Any] = {
            "timestamp": _now_iso(),
            "level": level if level in LEVELS else "error",
            "release": self.release,
            "environment": self.environment,
            "exception": {"type": exc_type, "message": message[:MAX_MESSAGE], "frames": frames},
            "transaction": req.transaction if req is not None else None,
            "url": req.url if req is not None else None,
        }
        if identity is not None:
            user: Dict[str, str] = {"externalId": identity.user_id}
            h = identity.hash_for(self.signing_secret, int(self._clock()))
            if h:
                user["userHash"] = h
            event["user"] = user
            if identity.customer_id:
                event["customer"] = {"externalId": identity.customer_id}
        if merged_tags:
            event["tags"] = dict(list(merged_tags.items())[:MAX_TAGS])
        if crumbs:
            event["breadcrumbs"] = crumbs[-MAX_CRUMBS:]
        if fingerprint:
            event["fingerprint"] = [str(x)[:200] for x in list(fingerprint)[:10]]
        if self.contexts:
            event["contexts"] = self.contexts
        event["sdk"] = {"name": SDK_NAME, "version": __version__}
        return {k: v for k, v in event.items() if v is not None}

    # ── envio ──

    def _ensure_worker(self) -> None:
        # Chamado com self._cond preso.
        t = self._thread
        if t is not None and t.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="bfocus-monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                with self._cond:
                    while not self._queue and not self._stopping:
                        self._cond.wait()
                    if not self._queue:
                        return
                    deadline = time.monotonic() + FLUSH_INTERVAL
                    while len(self._queue) < BATCH_SIZE and not self._flush_now and not self._stopping:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._cond.wait(remaining)
                    batch = [self._queue.popleft() for _ in range(min(BATCH_SIZE, len(self._queue)))]
                    if not self._queue:
                        self._flush_now = False
                    self._inflight += len(batch)
                try:
                    if batch and not self.disabled:
                        self._send(batch)
                finally:
                    with self._cond:
                        self._inflight -= len(batch)
                        self._cond.notify_all()
            except Exception:
                time.sleep(0.05)  # nunca morre por bug nosso

    def _send(self, batch: List[Dict[str, Any]]) -> None:
        body = json.dumps({"events": batch}, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        status = self._post(body)
        if status is None or status == 429 or status >= 500:
            time.sleep(self.retry_delay)
            status = self._post(body)
        if status in (401, 403):
            # Chave errada/revogada, módulo desligado: nunca martelar a API.
            self.disabled = True
            with self._cond:
                self._queue.clear()
                self._cond.notify_all()

    def _post(self, body: bytes, url: Optional[str] = None) -> Optional[int]:
        req = urllib.request.Request(url or self.endpoint, data=body, method="POST", headers={
            "X-bFocus-Monitor-Key": self.key,
            "Content-Type": "application/json",
            "X-bFocus-Client": CLIENT_ID,
            "User-Agent": CLIENT_ID,
        })
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                resp.read()
                return int(resp.status)
        except urllib.error.HTTPError as e:
            try:
                e.read()
                e.close()
            except Exception:
                pass
            return int(e.code)
        except Exception:
            return None

    def flush(self, timeout: float = 2.0) -> bool:
        """Envia o que está na fila e espera até ``timeout`` segundos. True se esvaziou."""
        try:
            deadline = time.monotonic() + max(0.0, float(timeout))
            with self._cond:
                if not self._queue and not self._inflight:
                    return True
                if self._queue:
                    self._ensure_worker()
                self._flush_now = True
                self._cond.notify_all()
                while self._queue or self._inflight:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(remaining)
                return True
        except Exception:
            return False

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        self.closed = True
        self._hb_stop.set()
        try:
            with self._cond:
                self._stopping = True
                self._cond.notify_all()
        except Exception:
            pass


def _fit(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Cada evento ≤ 64 KB: corta passos, frames e mensagem antes de passar disso."""

    def size(e: Dict[str, Any]) -> int:
        return len(json.dumps(e, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))

    try:
        if size(event) <= MAX_EVENT_BYTES:
            return event
        exc = event.get("exception") or {}
        steps = (
            lambda: event.pop("breadcrumbs", None),
            lambda: exc.__setitem__("frames", (exc.get("frames") or [])[-20:]),
            lambda: exc.__setitem__("message", str(exc.get("message") or "")[:500]),
            lambda: exc.__setitem__("frames", (exc.get("frames") or [])[-5:]),
            lambda: event.pop("tags", None),
            lambda: event.pop("contexts", None),
        )
        for step in steps:
            step()
            if size(event) <= MAX_EVENT_BYTES:
                return event
    except Exception:
        return None
    return None
