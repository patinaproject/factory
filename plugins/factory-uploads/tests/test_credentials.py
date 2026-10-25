from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support import REAL_MANIFEST, secret_names_manifest, upload_token  # noqa: F401
from tests.test_service import service  # noqa: F401

from factory_uploads.credentials import GATEWAY_ONLY, servable_names

SERVABLE_TODAY = frozenset({
    "CLAUDE_PROXY_TOKEN",
    "SENTRY_ACCESS_TOKEN",
    "POSTHOG_PERSONAL_API_KEY",
    "REVENUECAT_API_KEY",
    "DATAFORSEO_BASIC_TOKEN",
    "CLOUDFLARE_API_TOKEN",
    "FEATUREBASE_API_KEY",
})


class ServableSetTest(unittest.TestCase):
    def manifest(self, *names: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "plugin.yaml"
        path.write_text(secret_names_manifest(*names))
        return path

    def test_the_real_manifest_serves_exactly_the_proxy_and_mcp_service_tokens(self):
        self.assertEqual(servable_names(REAL_MANIFEST), SERVABLE_TODAY)

    def test_a_name_added_to_the_manifest_and_not_to_gateway_only_becomes_servable(self):
        names = SERVABLE_TODAY | {"EXAMPLE_NEW_GATEWAY_SECRET"}

        self.assertNotEqual(servable_names(self.manifest(*names)), SERVABLE_TODAY)

    def test_a_manifest_with_no_servable_name_is_refused_and_named(self):
        for names in ((), tuple(sorted(GATEWAY_ONLY))):
            with self.subTest(names=names):
                path = self.manifest(*names)

                with self.assertRaises(ValueError) as caught:
                    servable_names(path)

                self.assertIn(str(path), str(caught.exception))

    def test_a_manifest_that_parses_to_nothing_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "plugin.yaml"
        path.write_text("optional_env: [\n")

        with self.assertRaisesRegex(ValueError, str(path)):
            servable_names(path)


if __name__ == "__main__":
    unittest.main()
