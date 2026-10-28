from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import gh_login  # noqa: E402

_spec = importlib.util.spec_from_file_location("github_app", REPO / "plugins" / "factory-uploads" / "github_app.py")
github_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(github_app)


class LoginWindowTest(unittest.TestCase):
    def test_a_token_a_run_accepts_outlives_the_next_refresh_and_the_broker_margin(self) -> None:
        # The broker mints a new token once less than REFRESH_MARGIN is left, so the worst-case login a run
        # starts on has to stay under it: a run needs MIN_REMAINING_SECONDS, and a refresh can be one interval late.
        self.assertLess(gh_login.MIN_REMAINING_SECONDS + gh_login.REFRESH_INTERVAL_SECONDS, github_app.REFRESH_MARGIN)


class SecondsLeftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_a_written_expiry_reads_back_with_its_distance_from_now(self) -> None:
        gh_login.write_expiry(self.dir, 1000.0)

        self.assertEqual(gh_login.seconds_left(self.dir, 400.0), 600.0)
        self.assertEqual(json.loads((self.dir / "token-expiry.json").read_text()), {"expires_at": 1000.0})

    def test_an_unusable_file_raises_value_error_naming_the_file(self) -> None:
        path = self.dir / "token-expiry.json"
        for content in (None, "", "[]", '{"expires_at": null}', '{"expires_at": true}'):
            with self.subTest(content=content):
                path.unlink(missing_ok=True)
                if content is not None:
                    path.write_text(content)
                with self.assertRaisesRegex(ValueError, "token-expiry.json"):
                    gh_login.seconds_left(self.dir, 0.0)


if __name__ == "__main__":
    unittest.main()
