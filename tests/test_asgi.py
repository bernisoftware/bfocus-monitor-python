"""Middleware ASGI com uma app ASGI feita à mão (sem Starlette) e, se houver, com Starlette/FastAPI."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List

from _support import FakeServer, accepted, reset_state

import bfocus_monitor
from bfocus_monitor import _core
from bfocus_monitor.asgi import BfocusMiddleware


def _scope(path: str = "/pedidos/7", query: bytes = b"token=segredo") -> Dict[str, Any]:
    return {
        "type": "http", "method": "POST", "scheme": "https", "path": path, "root_path": "",
        "query_string": query, "headers": [(b"host", b"app.example"), (b"cookie", b"s=1")],
        "server": ("app.example", 443),
    }


async def _receive() -> Dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


class AsgiTest(unittest.TestCase):
    server: FakeServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = FakeServer().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        reset_state()
        self.server.reset([accepted() for _ in range(5)])
        bfocus_monitor.init(key="bf_mon_asgi", release="2.0.0", base_url=self.server.base_url,
                            auto_capture=False, _heartbeat=False)

    def tearDown(self) -> None:
        reset_state()

    def test_captura_e_relanca(self) -> None:
        async def app(scope: Any, receive: Any, send: Any) -> None:
            bfocus_monitor.set_user("u-7", "c-7")
            bfocus_monitor.add_breadcrumb("db", "SELECT pedidos")
            raise ZeroDivisionError("division by zero")

        sent: List[Any] = []

        async def send(msg: Any) -> None:
            sent.append(msg)

        with self.assertRaises(ZeroDivisionError):
            asyncio.run(BfocusMiddleware(app)(_scope(), _receive, send))
        bfocus_monitor.flush(5.0)
        [event] = self.server.events()
        self.assertEqual(event["transaction"], "POST /pedidos/7")
        self.assertEqual(event["url"], "https://app.example/pedidos/7")
        self.assertEqual(event["user"], {"externalId": "u-7"})
        self.assertEqual(event["breadcrumbs"][0]["message"], "SELECT pedidos")
        self.assertEqual(event["exception"]["frames"][-1]["function"], "AsgiTest.test_captura_e_relanca.<locals>.app"
                         if hasattr(app.__code__, "co_qualname") else "app")
        # O escopo da requisição fechou: nada vazou para o global.
        self.assertIsNone(_core._global_scope.identity)
        self.assertEqual(len(_core._global_scope.breadcrumbs), 0)

    def test_requisicao_sem_erro_passa_intacta(self) -> None:
        async def app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        sent: List[Any] = []

        async def send(msg: Any) -> None:
            sent.append(msg)

        asyncio.run(BfocusMiddleware(app)(_scope(), _receive, send))
        self.assertEqual(sent[-1]["body"], b"ok")
        bfocus_monitor.flush(1.0)
        self.assertEqual(self.server.requests, [])

    def test_lifespan_passa_direto(self) -> None:
        seen: List[str] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            seen.append(scope["type"])

        async def send(msg: Any) -> None:
            pass

        asyncio.run(BfocusMiddleware(app)({"type": "lifespan"}, _receive, send))
        self.assertEqual(seen, ["lifespan"])

    def test_requisicoes_simultaneas_nao_trocam_identidade(self) -> None:
        async def app(scope: Any, receive: Any, send: Any) -> None:
            uid = scope["path"].strip("/")
            bfocus_monitor.set_user(uid, "c")
            await asyncio.sleep(0.01)
            raise ValueError(uid)

        mw = BfocusMiddleware(app)

        async def one(uid: str) -> None:
            try:
                await mw(_scope("/" + uid, b""), _receive, lambda m: asyncio.sleep(0))
            except ValueError:
                pass

        async def main() -> None:
            await asyncio.gather(one("ana"), one("bia"), one("caio"))

        asyncio.run(main())
        bfocus_monitor.flush(5.0)
        got = {e["exception"]["message"]: e["user"]["externalId"] for e in self.server.events()}
        self.assertEqual(got, {"ana": "ana", "bia": "bia", "caio": "caio"})


class StarletteTest(unittest.TestCase):
    """Teste extra com o Starlette/FastAPI de verdade — pulado se não estiverem instalados."""

    def test_fastapi_add_middleware(self) -> None:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except Exception:
            self.skipTest("fastapi/starlette (e httpx) não instalados")
        server = FakeServer().start()
        try:
            reset_state()
            server.reset([accepted()])
            bfocus_monitor.init(key="bf_mon_fastapi", base_url=server.base_url, auto_capture=False, _heartbeat=False)
            app = FastAPI()
            app.add_middleware(BfocusMiddleware)

            @app.get("/pedidos/{pk}")
            def pedido(pk: int) -> Any:
                bfocus_monitor.set_user(external_id="u-1", customer_external_id="c-1")
                raise RuntimeError("na rota")

            resp = TestClient(app, raise_server_exceptions=False).get("/pedidos/3?token=x")
            self.assertEqual(resp.status_code, 500)
            bfocus_monitor.flush(5.0)
            [event] = server.events()
            self.assertEqual(event["transaction"], "GET /pedidos/3")
            self.assertEqual(event["user"]["externalId"], "u-1")
            self.assertNotIn("?", event["url"])
        finally:
            reset_state()
            server.stop()


if __name__ == "__main__":
    unittest.main()
