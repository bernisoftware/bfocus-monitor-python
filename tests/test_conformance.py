"""Conformidade (monitor/BRIEF.md §8): todos os casos de ``cases.json`` contra um servidor local."""

from __future__ import annotations

import json
import time
import unittest
from typing import Any, Dict

from _support import FakeServer, PACKAGE_ROOT, get_path, load_cases, nulls, reset_state

import bfocus_monitor
from bfocus_monitor import _core

CASES = load_cases()


def _boom(cls: type, message: str) -> None:
    raise cls(message)  # mesma linha sempre: o "mesmo erro" do caso de repetição


def _capture(spec: Dict[str, Any]) -> None:
    if spec["kind"] == "message":
        bfocus_monitor.capture_message(spec["message"], level=spec.get("level", "info"))
        return
    cls = type(spec["type"], (Exception,), {})
    try:
        _boom(cls, spec["message"])
    except Exception as exc:
        bfocus_monitor.capture_exception(
            exc, level=spec.get("level", "error"), tags=spec.get("tags"), fingerprint=spec.get("fingerprint"),
        )


class SendConformanceTest(unittest.TestCase):
    server: FakeServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = FakeServer().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def tearDown(self) -> None:
        reset_state()

    def test_todos_os_casos_de_envio(self) -> None:
        self.assertGreaterEqual(len(CASES["send"]), 10)
        for case in CASES["send"]:
            with self.subTest(case["name"]):
                self._run_case(case)

    def _run_case(self, case: Dict[str, Any]) -> None:
        reset_state()
        self.server.reset([r["respond"] for r in case["requests"]])
        opts = case["init"]
        su = case.get("set_user") or {}
        client = bfocus_monitor.init(
            key=opts["key"],
            release=opts.get("release"),
            environment=opts.get("environment", "production"),
            signing_secret=opts.get("signing_secret"),
            ignore=opts.get("ignore"),
            base_url=self.server.base_url,
            auto_capture=False,
            _retry_delay=0.05,
            _heartbeat=False,  # o sinal de vida tem caso próprio (HeartbeatConformanceTest)
            _clock=(lambda: su["ts"]) if "ts" in su else None,
        )
        if su:
            bfocus_monitor.set_user(su["user_external_id"], su["customer_external_id"], su.get("user_hash"))
        for crumb in case.get("breadcrumbs") or []:
            bfocus_monitor.add_breadcrumb(crumb["category"], crumb["message"], crumb.get("level", "info"))
        for _ in range(int(case.get("repeat", 1))):
            _capture(case["capture"])
        self.assertTrue(bfocus_monitor.flush(5.0), "flush não esvaziou a fila")
        if case.get("then_capture"):
            _capture(case["then_capture"])
            bfocus_monitor.flush(2.0)

        got = self.server.requests
        self.assertEqual(len(got), len(case["requests"]),
                         f"requisições: esperava {len(case['requests'])}, chegaram {len(got)}")
        bodies = []
        for record, step in zip(got, case["requests"]):
            exp = step["expect"]
            self.assertEqual(record["method"], exp["method"])
            self.assertEqual(record["path"], exp["path"])
            self.assertEqual(record["raw_path"], exp["path"], "a chave vai no header, nunca na URL")
            headers = {k.lower(): v for k, v in record["headers"].items()}
            for name, value in exp["headers"].items():
                self.assertEqual(headers.get(name.lower()), value, name)
            for name, prefix in exp["header_prefix"].items():
                self.assertTrue((headers.get(name.lower()) or "").startswith(prefix), name)
            self.assertEqual(headers.get("x-bfocus-client"), f"bfocus-monitor-python/{bfocus_monitor.__version__}")
            body = json.loads(record["body"].decode("utf-8"))
            bodies.append(body)
            self.assertEqual(list(body.keys()), ["events"])
            self.assertEqual(len(body["events"]), 1)
            event = body["events"][0]
            self.assertEqual(nulls(event), [], "evento com campo nulo")
            for path, expected in exp["event"].items():
                if expected == "$version":
                    expected = bfocus_monitor.__version__
                self.assertEqual(get_path(event, path), expected, path)
        if len(bodies) == 2:  # nova tentativa: mesmo corpo
            self.assertEqual(got[0]["body"], got[1]["body"])

        if case["after"] == "disabled":
            self.assertTrue(client.disabled)
        else:
            self.assertFalse(client.disabled)


class HeartbeatConformanceTest(unittest.TestCase):
    def test_init_manda_o_primeiro_sinal_de_vida(self) -> None:
        server = FakeServer().start()
        try:
            for case in CASES["heartbeat"]:
                with self.subTest(case["name"]):
                    reset_state()
                    server.reset([case["respond"]])
                    opts = case["init"]
                    client = bfocus_monitor.init(
                        key=opts["key"], release=opts.get("release"),
                        environment=opts.get("environment", "production"),
                        base_url=server.base_url, auto_capture=False,
                    )
                    deadline = time.monotonic() + 5
                    while not server.requests and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(len(server.requests), 1, "um sinal de vida no init")
                    record = server.requests[0]
                    exp = case["expect"]
                    self.assertEqual(record["method"], exp["method"])
                    self.assertEqual(record["raw_path"], exp["path"])
                    headers = {k.lower(): v for k, v in record["headers"].items()}
                    for name, value in exp["headers"].items():
                        self.assertEqual(headers.get(name.lower()), value, name)
                    for name, prefix in exp["header_prefix"].items():
                        self.assertTrue((headers.get(name.lower()) or "").startswith(prefix), name)
                    body = json.loads(record["body"].decode("utf-8"))
                    self.assertEqual(nulls(body), [])
                    for path, expected in exp["body"].items():
                        if expected == "$version":
                            expected = bfocus_monitor.__version__
                        self.assertEqual(get_path(body, path), expected, path)
                    for path in exp["body_present"]:
                        self.assertTrue(get_path(body, path), path)
                    self.assertEqual(body["sdk"]["name"], "bfocus-monitor-python")
                    self.assertEqual(body["runtime"]["name"], "python")
                    self.assertFalse(client.disabled)
                    # flush/close não mandam sinal de vida
                    bfocus_monitor.flush(0.5)
                    bfocus_monitor.close(0.5)
                    self.assertEqual(len(server.requests), 1)
        finally:
            reset_state()
            server.stop()


class UserHashVectorsTest(unittest.TestCase):
    def test_vetores(self) -> None:
        for v in CASES["user_hash"]:
            with self.subTest(v["user_external_id"]):
                self.assertEqual(
                    bfocus_monitor.sign_user(v["secret"], v["user_external_id"], v["customer_external_id"], v["ts"]),
                    v["expected"],
                )


class FramesConformanceTest(unittest.TestCase):
    def test_ordem_e_in_app(self) -> None:
        for case in CASES["frames"]:
            with self.subTest(case["name"]):
                # O traceback do Python já vem de FORA para DENTRO: o rastro neutro (de dentro para
                # fora) vira a ordem natural do Python invertendo.
                python_order = list(reversed(case["runtime_order"]))
                got = [_core.frame_dict(f["file"], f["function"], f["line"]) for f in python_order]
                self.assertEqual(got, case["expected"])


class VendoredCasesTest(unittest.TestCase):
    def test_copia_igual_a_fonte(self) -> None:
        from _support import MONOREPO_CASES, VENDORED_CASES

        if not MONOREPO_CASES.is_file():
            self.skipTest("fora do monorepo: só existe a cópia tests/cases.json")
        self.assertEqual(
            VENDORED_CASES.read_text(encoding="utf-8"), MONOREPO_CASES.read_text(encoding="utf-8"),
            "tests/cases.json desatualizado: rode python monitor/conformance/generate.py",
        )
        self.assertTrue(PACKAGE_ROOT.joinpath("bfocus_monitor").is_dir())


if __name__ == "__main__":
    unittest.main()
