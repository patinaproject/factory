from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
MANIFEST = (PLUGIN / "plugin.yaml").read_text()
# Hermes counts a declared platform variable as a secret by these suffixes, unless the entry says otherwise.
SECRET_SUFFIXES = ("_TOKEN", "_SECRET", "_KEY", "_PASSWORD", "_JSON")
NAME = re.compile(r"^  - name: ([A-Za-z_][A-Za-z0-9_]*)$", re.MULTILINE)


class ManifestTest(unittest.TestCase):
    def test_hermes_reads_it_as_a_platform_manifest(self):
        self.assertRegex(MANIFEST, r"(?m)^kind: platform$")
        self.assertRegex(MANIFEST, r"(?m)^name: factory-secret-names$")

    def test_every_declared_name_is_a_secret_hermes_strips(self):
        names = NAME.findall(MANIFEST)

        self.assertTrue(names)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(name.endswith(SECRET_SUFFIXES))
        # A category other than messaging, or password: false, would keep a name out of the strip list.
        self.assertNotRegex(MANIFEST, r"(?m)^\s+(category|password|secret):")

    def test_the_proxy_token_variable_the_runbook_prescribes_is_declared(self):
        self.assertIn("CLAUDE_PROXY_TOKEN", NAME.findall(MANIFEST))

    def test_each_mcp_token_variable_the_runbook_names_is_declared(self):
        names = NAME.findall(MANIFEST)

        for name in (
            "SENTRY_ACCESS_TOKEN",
            "POSTHOG_PERSONAL_API_KEY",
            "REVENUECAT_API_KEY",
            "DATAFORSEO_BASIC_TOKEN",
            "CLOUDFLARE_API_TOKEN",
            "FEATUREBASE_API_KEY",
        ):
            with self.subTest(name=name):
                self.assertIn(name, names)

    def test_register_registers_nothing(self):
        spec = importlib.util.spec_from_file_location("factory_secret_names", PLUGIN / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []

        class Context:
            def __getattr__(self, name):
                return lambda *args, **kwargs: calls.append(name)

        module.register(Context())

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
