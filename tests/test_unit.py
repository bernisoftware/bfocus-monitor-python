"""Comportamento do pacote além dos casos de conformidade."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import unittest
from typing import Any, List

from _support import FakeServer, accepted, nulls, reset_state

import bfocus_monitor
from bfocus_monitor import _core


def _calcular() -> None:
    _dividir(1, 0)


def _dividir(a: int, b: int) -> float:
    return a / b


class Base(unittest.TestCase):
    server: FakeServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = FakeServer().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        reset_state()
        self.server.reset([accepted() for _ in range(20)])

    def tearDown(self) -> None:
        reset_state()

    def init(self, **kw: Any) -> bfocus_monitor.Client:
        kw.setdefault("key", "bf_mon_unit")
        kw.setdefault("release", "1.0.0")
        kw.setdefault("auto_capture", False)
        kw.setdefault("_retry_delay", 0.05)
        kw.setdefault("_heartbeat", False)
        return bfocus_monitor.init(base_url=self.server.base_url + "/", **kw)

    def events(self) -> List[Any]:
        bfocus_monitor.flush(5.0)
        return self.server.events()


class InitTest(Base):
    def test_sem_chave_e_erro_de_argumento(self) -> None:
        with self.assertRaises(ValueError):
            bfocus_monitor.init(key="")

    def test_init_nao_faz_rede_nem_cria_thread(self) -> None:
        client = self.init()
        self.assertIsNone(client._thread)
        self.assertEqual(self.server.requests, [])
        self.assertTrue(bfocus_monitor.flush(0.1))

    def test_sem_init_nada_quebra(self) -> None:
        bfocus_monitor.capture_exception(ValueError("x"))
        bfocus_monitor.capture_message("x")
        bfocus_monitor.set_tag("a", "b")
        self.assertTrue(bfocus_monitor.flush(0.1))
        self.assertEqual(self.server.requests, [])

    def test_base_url_sem_barra_final(self) -> None:
        client = self.init()
        self.assertEqual(client.endpoint, self.server.base_url + "/api/v1/monitor/events")


class EventTest(Base):
    def test_frames_de_fora_para_dentro_e_relativos(self) -> None:
        self.init()
        try:
            _calcular()
        except ZeroDivisionError as exc:
            bfocus_monitor.capture_exception(exc)
        [event] = self.events()
        self.assertEqual(nulls(event), [])
        self.assertEqual(event["exception"]["type"], "ZeroDivisionError")
        self.assertEqual(event["exception"]["message"], "division by zero")
        frames = event["exception"]["frames"]
        self.assertEqual([f["function"] for f in frames][-2:], ["_calcular", "_dividir"])
        self.assertTrue(frames[-1]["inApp"])
        self.assertIsInstance(frames[-1]["line"], int)
        self.assertFalse(os.path.isabs(frames[-1]["file"]) and os.getcwd() in frames[-1]["file"])
        self.assertEqual(event["sdk"], {"name": "bfocus-monitor-python", "version": bfocus_monitor.__version__})
        self.assertEqual(event["contexts"]["runtime"]["name"], "python")
        self.assertTrue(event["timestamp"].endswith("Z"))

    def test_sem_exc_usa_a_excecao_em_tratamento(self) -> None:
        self.init()
        try:
            raise KeyError("pedido")
        except KeyError:
            bfocus_monitor.capture_exception()
        [event] = self.events()
        self.assertEqual(event["exception"]["type"], "KeyError")

    def test_biblioteca_nao_e_do_sistema(self) -> None:
        self.init()
        try:
            json.loads("{quebrado")
        except ValueError as exc:
            bfocus_monitor.capture_exception(exc)
        [event] = self.events()
        frames = event["exception"]["frames"]
        self.assertTrue(frames[0]["inApp"])  # este teste
        self.assertFalse(frames[-1]["inApp"])  # json/decoder.py (stdlib)
        self.assertIn("json", frames[-1]["file"])

    def test_proprio_pacote_nao_e_do_sistema(self) -> None:
        f = _core.frame_dict(os.path.join(_core._PKG_DIR, "_core.py"), "x", 1)
        self.assertFalse(f["inApp"])

    def test_in_app_prefixes(self) -> None:
        lib = "/srv/venv/lib/python3.12/site-packages/minha_lib/mod.py"
        self.assertFalse(_core.frame_dict(lib, "f", 1, module="minha_lib.mod")["inApp"])
        self.assertTrue(_core.frame_dict(lib, "f", 1, module="minha_lib.mod", in_app_prefixes=["minha_lib"])["inApp"])

    def test_excecao_encadeada_manda_a_causa_raiz(self) -> None:
        self.init()
        try:
            try:
                {}["pedido"]
            except KeyError as inner:
                raise RuntimeError("falha ao salvar") from inner
        except RuntimeError as exc:
            bfocus_monitor.capture_exception(exc)
        [event] = self.events()
        self.assertEqual(event["exception"]["type"], "KeyError")
        self.assertEqual(event["exception"]["message"], "'pedido' (dentro de: RuntimeError: falha ao salvar)")
        self.assertTrue(event["exception"]["frames"])

    def test_contexto_implicito_tambem_encadeia(self) -> None:
        self.init()
        try:
            try:
                1 / 0
            except ZeroDivisionError:
                raise ValueError("conta")
        except ValueError as exc:
            bfocus_monitor.capture_exception(exc)
        [event] = self.events()
        self.assertEqual(event["exception"]["type"], "ZeroDivisionError")

    def test_tags_globais_e_da_captura(self) -> None:
        self.init()
        bfocus_monitor.set_tag("modulo", "fiscal")
        bfocus_monitor.capture_exception(ValueError("a"), tags={"tela": "nf"})
        [event] = self.events()
        self.assertEqual(event["tags"], {"modulo": "fiscal", "tela": "nf"})

    def test_mensagem(self) -> None:
        self.init()
        bfocus_monitor.capture_message("estoque negativo", "warning")
        [event] = self.events()
        self.assertEqual(event["level"], "warning")
        self.assertEqual(event["exception"]["type"], "Message")
        self.assertEqual(event["fingerprint"], ["estoque negativo"])

    def test_nivel_invalido_vira_error(self) -> None:
        self.init()
        bfocus_monitor.capture_exception(ValueError("a"), level="critico")
        [event] = self.events()
        self.assertEqual(event["level"], "error")

    def test_before_send_altera_ou_descarta(self) -> None:
        def bs(event: Any) -> Any:
            if event["exception"]["message"] == "descarta":
                return None
            event["tags"] = {"alterado": "sim"}
            return event

        self.init(before_send=bs)
        bfocus_monitor.capture_exception(ValueError("descarta"))
        bfocus_monitor.capture_exception(ValueError("fica"))
        [event] = self.events()
        self.assertEqual(event["tags"], {"alterado": "sim"})

    def test_before_send_com_erro_manda_como_esta(self) -> None:
        self.init(before_send=lambda e: 1 / 0)
        bfocus_monitor.capture_exception(ValueError("a"))
        self.assertEqual(len(self.events()), 1)

    def test_ignore_com_regex(self) -> None:
        import re

        self.init(ignore=[re.compile(r"^timeout \d+")])
        bfocus_monitor.capture_exception(ValueError("timeout 30s"))
        self.assertEqual(self.events(), [])

    def test_sample_rate_zero(self) -> None:
        self.init(sample_rate=0)
        bfocus_monitor.capture_exception(ValueError("a"))
        self.assertEqual(self.events(), [])

    def test_evento_gigante_e_cortado(self) -> None:
        event = {"exception": {"type": "E", "message": "x" * 2000,
                               "frames": [{"file": "a" * 1000, "function": "f" * 190, "line": 1, "inApp": True}] * 60},
                 "breadcrumbs": [{"message": "m" * 300}] * 30, "level": "error"}
        out = _core._fit(event)
        assert out is not None
        self.assertLessEqual(len(json.dumps(out).encode()), _core.MAX_EVENT_BYTES)
        self.assertNotIn("breadcrumbs", out)


class IdentityTest(Base):
    def test_assinatura_recalculada_depois_de_6_dias(self) -> None:
        now = [1760000000]
        self.init(signing_secret="whs_secret_A", _clock=lambda: now[0])
        bfocus_monitor.set_user("u-123", "cliente-9")
        bfocus_monitor.capture_exception(ValueError("a"))
        now[0] += 7 * 86400
        bfocus_monitor.capture_exception(ValueError("b"))
        a, b = self.events()
        self.assertEqual(a["user"]["userHash"], bfocus_monitor.sign_user("whs_secret_A", "u-123", "cliente-9", 1760000000))
        self.assertEqual(b["user"]["userHash"], bfocus_monitor.sign_user("whs_secret_A", "u-123", "cliente-9", now[0]))

    def test_ids_numericos_viram_texto_e_none_limpa(self) -> None:
        self.init()
        bfocus_monitor.set_user(42, 7)
        bfocus_monitor.capture_exception(ValueError("a"))
        bfocus_monitor.set_user(None)
        bfocus_monitor.capture_exception(ValueError("b"))
        a, b = self.events()
        self.assertEqual(a["user"], {"externalId": "42"})
        self.assertEqual(a["customer"], {"externalId": "7"})
        self.assertNotIn("user", b)

    def test_escopo_por_requisicao_isola_usuarios_simultaneos(self) -> None:
        self.init()
        barrier = threading.Barrier(2)

        def request(uid: str) -> None:
            token = _core.push_scope(transaction=f"GET /{uid}", url=f"https://app.example/{uid}?token=segredo")
            try:
                bfocus_monitor.set_user(uid, "c")
                barrier.wait()
                bfocus_monitor.capture_exception(ValueError(uid))
            finally:
                _core.pop_scope(token)

        threads = [threading.Thread(target=request, args=(u,)) for u in ("ana", "bia")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        events = {e["exception"]["message"]: e for e in self.events()}
        for uid in ("ana", "bia"):
            self.assertEqual(events[uid]["user"]["externalId"], uid)
            self.assertEqual(events[uid]["transaction"], f"GET /{uid}")
            self.assertEqual(events[uid]["url"], f"https://app.example/{uid}")
        self.assertIsNone(_core._global_scope.identity)


class SendTest(Base):
    def test_lote_unico_e_headers(self) -> None:
        self.init()
        for i in range(5):
            bfocus_monitor.capture_exception(ValueError(f"e{i}"))
        bfocus_monitor.flush(5.0)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(self.server.events()), 5)
        h = {k.lower(): v for k, v in self.server.requests[0]["headers"].items()}
        self.assertEqual(h["user-agent"], f"bfocus-monitor-python/{bfocus_monitor.__version__}")

    def test_5xx_tenta_de_novo_uma_vez_e_desiste(self) -> None:
        self.server.reset([{"status": 503, "body": {}}, {"status": 500, "body": {}}])
        client = self.init()
        bfocus_monitor.capture_exception(ValueError("a"))
        bfocus_monitor.flush(5.0)
        self.assertEqual(len(self.server.requests), 2)
        self.assertFalse(client.disabled)

    def test_403_desliga_e_novo_init_religa(self) -> None:
        self.server.reset([{"status": 403, "body": {}}, accepted()])
        client = self.init()
        bfocus_monitor.capture_exception(ValueError("a"))
        bfocus_monitor.flush(5.0)
        self.assertTrue(client.disabled)
        self.init()
        bfocus_monitor.capture_exception(ValueError("b"))
        bfocus_monitor.flush(5.0)
        self.assertEqual(len(self.server.requests), 2)

    def test_400_descarta_sem_desligar(self) -> None:
        self.server.reset([{"status": 400, "body": {}}, accepted()])
        client = self.init()
        bfocus_monitor.capture_exception(ValueError("a"))
        bfocus_monitor.flush(5.0)
        self.assertFalse(client.disabled)
        self.assertEqual(len(self.server.requests), 1)

    def test_rede_fora_nao_derruba(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        bfocus_monitor.init(key="k", base_url=f"http://127.0.0.1:{port}", auto_capture=False, _retry_delay=0.01,
                            _heartbeat=False)
        bfocus_monitor.capture_exception(ValueError("a"))
        self.assertTrue(bfocus_monitor.flush(5.0))

    def test_teto_por_minuto_e_fila_limitada(self) -> None:
        client = self.init()
        client._ensure_worker = lambda: None  # type: ignore[method-assign]
        for i in range(150):
            bfocus_monitor.capture_exception(ValueError(f"e{i}"))
        self.assertEqual(len(client._queue), 100)
        client._queue.clear()

    def test_fila_cheia_descarta_o_mais_novo(self) -> None:
        client = self.init()
        client._ensure_worker = lambda: None  # type: ignore[method-assign]
        client._allow = lambda key: True  # type: ignore[method-assign]
        for i in range(120):
            bfocus_monitor.capture_message(f"m{i}")
        self.assertEqual(len(client._queue), 100)
        self.assertEqual(client._queue[-1]["exception"]["message"], "m99")
        client._queue.clear()

    def test_close_desliga(self) -> None:
        self.init()
        bfocus_monitor.close()
        bfocus_monitor.capture_exception(ValueError("a"))
        bfocus_monitor.flush(0.2)
        self.assertEqual(self.server.requests, [])


class HeartbeatTest(Base):
    def wait_heartbeats(self, n: int) -> None:
        import time

        deadline = time.monotonic() + 5
        while len(self.server.heartbeats()) < n and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_init_manda_e_repete_no_intervalo(self) -> None:
        client = self.init(_heartbeat=True)
        client._heartbeat_interval = 0.05
        self.wait_heartbeats(3)
        hbs = self.server.heartbeats()
        self.assertGreaterEqual(len(hbs), 3)
        bodies = [json.loads(h["body"]) for h in hbs]
        self.assertEqual(len({b["instance"] for b in bodies}), 1, "instance estável no processo")
        self.assertEqual(len(bodies[0]["instance"]), 12)
        self.assertEqual(bodies[0]["release"], "1.0.0")
        self.assertIn("host", bodies[0])
        self.assertEqual(bodies[0]["sdk"]["name"], "bfocus-monitor-python")
        self.assertTrue(client._hb_thread is not None and client._hb_thread.daemon)
        bfocus_monitor.close(0.5)
        client._hb_thread.join(2)
        self.assertFalse(client._hb_thread.is_alive(), "close para o sinal de vida")

    def test_401_no_sinal_de_vida_desliga_tudo(self) -> None:
        self.server.reset([{"status": 401, "body": {}}])
        client = self.init(_heartbeat=True)
        self.wait_heartbeats(1)
        client._hb_thread.join(2)
        self.assertTrue(client.disabled)
        bfocus_monitor.capture_exception(ValueError("depois"))
        bfocus_monitor.flush(0.5)
        self.assertEqual(len(self.server.requests), 1)

    def test_rede_fora_nao_desliga(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        client = bfocus_monitor.init(key="k", base_url=f"http://127.0.0.1:{port}", auto_capture=False)
        self.assertIsNone(client.send_heartbeat())
        self.assertFalse(client.disabled)


class HooksTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self._saved = (bfocus_monitor._prev_excepthook, bfocus_monitor._prev_threading_hook)

    def tearDown(self) -> None:
        bfocus_monitor._prev_excepthook, bfocus_monitor._prev_threading_hook = self._saved
        super().tearDown()

    def test_excepthook_fatal_e_encadeia(self) -> None:
        calls: List[Any] = []
        self.init(auto_capture=True)
        bfocus_monitor._prev_excepthook = lambda *a: calls.append(a)
        try:
            _calcular()
        except ZeroDivisionError as exc:
            bfocus_monitor._excepthook(type(exc), exc, exc.__traceback__)
        self.assertEqual(len(calls), 1)
        [event] = self.server.events()  # o gancho já fez o flush
        self.assertEqual(event["level"], "fatal")

    def test_excepthook_instalado_no_init(self) -> None:
        import sys

        self.init(auto_capture=True)
        self.assertIs(sys.excepthook, bfocus_monitor._excepthook)
        self.assertIs(threading.excepthook, bfocus_monitor._threading_excepthook)

    def test_thread_que_morre(self) -> None:
        calls: List[Any] = []
        self.init(auto_capture=True)
        bfocus_monitor._prev_threading_hook = lambda args: calls.append(args)

        def worker() -> None:
            raise RuntimeError("na thread")

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        [event] = self.events()
        self.assertEqual(event["exception"]["message"], "na thread")
        self.assertEqual(event["level"], "error")
        self.assertEqual(len(calls), 1)

    def test_auto_capture_false_nao_captura_pelo_gancho(self) -> None:
        calls: List[Any] = []
        self.init(auto_capture=False)
        bfocus_monitor._prev_excepthook = lambda *a: calls.append(a)
        exc = ValueError("x")
        bfocus_monitor._excepthook(ValueError, exc, None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.events(), [])

    def test_asyncio_encadeia_o_handler_anterior(self) -> None:
        self.init()
        seen: List[Any] = []

        async def main() -> None:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda lp, ctx: seen.append(ctx))
            self.assertTrue(bfocus_monitor.install_asyncio_handler())
            self.assertTrue(bfocus_monitor.install_asyncio_handler(loop))  # idempotente

            def callback() -> None:
                raise LookupError("no callback")

            loop.call_soon(callback)
            await asyncio.sleep(0.05)

        asyncio.run(main())
        self.assertEqual(len(seen), 1)
        [event] = self.events()
        self.assertEqual(event["exception"]["type"], "LookupError")

    def test_init_dentro_do_loop_instala_o_handler(self) -> None:
        async def main() -> bool:
            self.init(auto_capture=True)
            handler = asyncio.get_running_loop().get_exception_handler()
            return bool(getattr(handler, "_bfocus_monitor", False))

        self.assertTrue(asyncio.run(main()))


class ProcessoDeVerdadeTest(Base):
    """Erro não tratado no topo de um processo real: evento fatal, e o Python quebra como sempre."""

    def test_excepthook_real(self) -> None:
        import subprocess
        import sys

        from _support import PACKAGE_ROOT

        code = (
            "import bfocus_monitor\n"
            f"bfocus_monitor.init(key='bf_mon_proc', release='9.9.9', base_url={self.server.base_url!r})\n"
            "def pedido():\n"
            "    raise ZeroDivisionError('division by zero')\n"
            "pedido()\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(PACKAGE_ROOT), capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ZeroDivisionError: division by zero", proc.stderr)  # o handler original rodou
        [event] = self.server.events()
        self.assertEqual(event["level"], "fatal")
        self.assertEqual(event["release"], "9.9.9")
        self.assertEqual(event["exception"]["frames"][-1]["function"], "pedido")

    def test_atexit_envia_o_que_ficou_na_fila(self) -> None:
        import subprocess
        import sys

        from _support import PACKAGE_ROOT

        code = (
            "import bfocus_monitor\n"
            f"bfocus_monitor.init(key='bf_mon_proc', base_url={self.server.base_url!r})\n"
            "bfocus_monitor.capture_message('fim do job', 'warning')\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(PACKAGE_ROOT), capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        [event] = self.server.events()
        self.assertEqual(event["exception"]["message"], "fim do job")


class DjangoMiddlewareTest(Base):
    """Sem instalar o Django: o middleware só usa atributos da requisição."""

    class Req:
        method = "POST"
        path = "/pedidos/9"

        class resolver_match:
            route = "pedidos/<int:pk>"

        def build_absolute_uri(self, path: str) -> str:
            return "https://app.example" + path

    def test_sincrono(self) -> None:
        from bfocus_monitor.django import BfocusMiddleware

        self.init()
        req = self.Req()

        def get_response(request: Any) -> str:
            bfocus_monitor.set_user("u-1", "c-1")
            try:
                raise ValueError("na view")
            except ValueError as exc:
                self.assertIsNone(mw.process_exception(request, exc))
            return "500"

        mw = BfocusMiddleware(get_response)
        self.assertEqual(mw(req), "500")
        [event] = self.events()
        self.assertEqual(event["transaction"], "POST /pedidos/<int:pk>")
        self.assertEqual(event["url"], "https://app.example/pedidos/9")
        self.assertEqual(event["user"]["externalId"], "u-1")
        self.assertIsNone(_core._request_scope.get())

    def test_assincrono(self) -> None:
        import inspect

        from bfocus_monitor.django import BfocusMiddleware

        self.init()

        async def get_response(request: Any) -> str:
            return "ok"

        mw = BfocusMiddleware(get_response)
        self.assertTrue(inspect.iscoroutinefunction(mw) or getattr(mw, "_is_coroutine", None))
        self.assertEqual(asyncio.run(mw(self.Req())), "ok")


class FlaskTest(Base):
    def test_flask(self) -> None:
        try:
            import flask
        except ImportError:
            self.skipTest("flask não instalado")
        from bfocus_monitor.flask import init_app

        self.init()
        app = flask.Flask("t")

        @app.route("/pedidos/<int:pk>")
        def pedido(pk: int) -> str:
            raise ValueError("na rota")

        init_app(app)
        resp = app.test_client().get("/pedidos/3?token=x")
        self.assertEqual(resp.status_code, 500)
        [event] = self.events()
        self.assertEqual(event["transaction"], "GET /pedidos/<int:pk>")
        self.assertNotIn("?", event["url"])


if __name__ == "__main__":
    unittest.main()
