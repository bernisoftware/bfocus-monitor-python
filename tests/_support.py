"""Apoio dos testes: servidor HTTP falso (stdlib), casos de conformidade e reset do estado global.

Os casos moram em ``monitor/conformance/cases.json`` no monorepo. O espelho público recebe só
``monitor/python`` — por isso existe a cópia ``tests/cases.json``, escrita pelo
``monitor/conformance/generate.py`` (não edite à mão).
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

TESTS_DIR = pathlib.Path(__file__).resolve().parent
PACKAGE_ROOT = TESTS_DIR.parent  # monitor/python (ou a raiz do espelho público)
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import bfocus_monitor  # noqa: E402
from bfocus_monitor import _core  # noqa: E402

VENDORED_CASES = TESTS_DIR / "cases.json"
MONOREPO_CASES = PACKAGE_ROOT.parent / "conformance" / "cases.json"


def cases_path() -> pathlib.Path:
    return MONOREPO_CASES if MONOREPO_CASES.is_file() else VENDORED_CASES


def load_cases() -> Dict[str, Any]:
    with cases_path().open(encoding="utf-8") as fh:
        return json.load(fh)


class FakeServer:
    """Responde, em ordem, as respostas enfileiradas e grava as requisições.

    Requisição a mais (fila vazia) recebe 418 — o pacote não repete 4xx — e fica gravada para o
    teste acusar.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._responses: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                record = {
                    "method": self.command,
                    "path": self.path.partition("?")[0],
                    "raw_path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": raw,
                }
                with outer._lock:
                    outer.requests.append(record)
                    resp = outer._responses.pop(0) if outer._responses else None
                if resp is None:
                    resp = {"status": 418, "body": {"error": "UNEXPECTED_REQUEST"}}
                payload = json.dumps(resp.get("body")).encode("utf-8")
                self.send_response(int(resp["status"]))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def reset(self, responses: Optional[List[Dict[str, Any]]] = None) -> None:
        with self._lock:
            self._responses = list(responses or [])
            self.requests = []

    def events(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for r in self.requests:
            if r["path"] != "/api/v1/monitor/events":
                continue
            out.extend(json.loads(r["body"].decode("utf-8"))["events"])
        return out


    def heartbeats(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["path"] == "/api/v1/monitor/heartbeat"]


def accepted(n: int = 1) -> Dict[str, Any]:
    return {"status": 202, "body": {"accepted": n, "dropped": 0, "invalid": 0, "ignored": 0}}


def reset_state() -> None:
    """Estado global limpo entre testes: sem cliente, escopo global novo, sem escopo de requisição."""
    bfocus_monitor.close(timeout=0.5)
    _core._global_scope = _core.Scope()
    _core._request_scope.set(None)


def get_path(obj: Any, dotted: str) -> Any:
    """``"breadcrumbs.0.category"`` → valor (KeyError/IndexError se não existir)."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def nulls(obj: Any, prefix: str = "") -> List[str]:
    """Caminhos com valor nulo (o contrato proíbe)."""
    out: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend([f"{prefix}{k}"] if v is None else nulls(v, f"{prefix}{k}."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend([f"{prefix}{i}"] if v is None else nulls(v, f"{prefix}{i}."))
    return out
