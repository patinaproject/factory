from __future__ import annotations

import importlib
import unittest

from tests import load_plugin

repository = importlib.import_module(load_plugin().__name__ + ".repository")

REPOSITORIES = [
    {"full_name": "Example-Org/Main", "path": "/srv/main", "routing": "default"},
    {"full_name": "Example-Org/Api", "path": "/srv/api", "routing": "linked"},
    {"full_name": "Example-Org/Web", "path": "/srv/web", "routing": "linked"},
    {"full_name": "Example-Org/Site", "path": "/srv/site", "routing": "synced_github"},
]
MAIN, API, WEB, SITE = (repo["full_name"] for repo in REPOSITORIES)
API_ROOT = "https://github.com/example-org/api"
SITE_ISSUE = "https://github.com/example-org/site/issues/12"


class ResolveRepositoryTest(unittest.TestCase):
    PICKS = {
        "no attachments": ([], MAIN),
        "synced issue alone": ([SITE_ISSUE], SITE),
        "synced issue beside a linked link": ([API_ROOT, SITE_ISSUE], SITE),
        "repo root": ([API_ROOT], API),
        "blob file": (["https://github.com/example-org/api/blob/main/src/app.py"], API),
        "dependabot alert": (["https://github.com/example-org/api/security/dependabot/7"], API),
        "pull request": (["https://github.com/example-org/api/pull/31"], API),
        "http": (["http://github.com/example-org/api"], API),
        "www": (["https://www.github.com/example-org/api"], API),
        "mixed case": (["https://github.com/EXAMPLE-ORG/Api"], API),
        "trailing slash": ([API_ROOT + "/"], API),
        ".git": ([API_ROOT + ".git"], API),
        "query": ([API_ROOT + "?tab=readme-ov-file"], API),
        "fragment": ([API_ROOT + "#readme"], API),
        "default link alone": (["https://github.com/example-org/main"], MAIN),
        "same repo linked twice": ([API_ROOT, API_ROOT + "/pull/3"], API),
        "synced repo root is ignored": (["https://github.com/example-org/site"], MAIN),
        "synced repo pull request is ignored": (["https://github.com/example-org/site/pull/4"], MAIN),
        "non-GitHub URL": (["https://example.com/example-org/api"], MAIN),
        "unconfigured repository": (["https://github.com/example-org/other"], MAIN),
        "longer repository name": (["https://github.com/example-org/api-extra"], MAIN),
        "other owner": (["https://github.com/other-org/api"], MAIN),
    }

    def test_each_attachment_set_picks_its_repository(self):
        for name, (urls, expected) in self.PICKS.items():
            with self.subTest(name):
                self.assertEqual(repository.resolve_repository(REPOSITORIES, urls), (expected, None))

    def test_two_different_repositories_conflict(self):
        for name, urls, pair in (
            ("linked and default", [API_ROOT, "https://github.com/example-org/main"], (API, MAIN)),
            ("two linked", [API_ROOT, "https://github.com/example-org/web/pull/2"], (API, WEB)),
        ):
            with self.subTest(name):
                self.assertEqual(repository.resolve_repository(REPOSITORIES, urls), (None, pair))

    def test_settings_without_linked_repositories_resolve_as_before(self):
        settings = [REPOSITORIES[0], REPOSITORIES[3]]
        cases = {
            "synced issue": ([SITE_ISSUE], SITE),
            "default link": (["https://github.com/example-org/main"], MAIN),
            "a repository the settings lack": ([API_ROOT], MAIN),
            "nothing": ([], MAIN),
        }
        for name, (urls, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(repository.resolve_repository(settings, urls), (expected, None))
