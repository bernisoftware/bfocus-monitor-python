"""A versão existe em dois lugares e eles não podem divergir.

``bfocus_monitor/_version.py`` vai no header ``X-bFocus-Client`` e em ``sdk.version`` de todo
evento — é por ele que o bFocus sabe qual versão do pacote cada sistema usa.
"""

from __future__ import annotations

import json
import re
import unittest

from _support import PACKAGE_ROOT

import bfocus_monitor

PYPROJECT_RE = r'(?m)^(version\s*=\s*")([^"]+)"'
VERSION_PY_RE = r'(__version__\s*=\s*")([^"]+)"'


class VersionTest(unittest.TestCase):
    def test_versao_bate_com_o_pyproject(self) -> None:
        matches = re.findall(PYPROJECT_RE, (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(len(matches), 1)
        self.assertEqual(bfocus_monitor.__version__, matches[0][1])

    def test_padrao_casa_em_version_py(self) -> None:
        text = (PACKAGE_ROOT / "bfocus_monitor" / "_version.py").read_text(encoding="utf-8")
        matches = re.findall(VERSION_PY_RE, text)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1], bfocus_monitor.__version__)

    def test_identificacao(self) -> None:
        self.assertEqual(bfocus_monitor.SDK_NAME, "bfocus-monitor-python")
        self.assertEqual(bfocus_monitor.CLIENT_ID, f"bfocus-monitor-python/{bfocus_monitor.__version__}")

    def test_versao_do_monorepo(self) -> None:
        release = PACKAGE_ROOT.parent / "release.json"
        if not release.is_file():
            self.skipTest("fora do monorepo")
        self.assertEqual(json.loads(release.read_text(encoding="utf-8"))["version"], bfocus_monitor.__version__)


if __name__ == "__main__":
    unittest.main()
