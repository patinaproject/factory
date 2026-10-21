from __future__ import annotations

import unittest

from tests import load_plugin

plugin = load_plugin()

from linear_agent_session import missing_secret  # noqa: E402

PROXY_CONFIG = {
    "model": {"provider": "custom:proxy", "default": "model-alias"},
    "providers": {"proxy": {"api": "http://127.0.0.1:8317", "key_env": "PROXY_TOKEN"}},
}
WORKER = {"HERMES_KANBAN_TASK": "t_1234"}
TRANSIENT = {
    "reason": "timeout",
    "retryable": False,
    "should_fallback": False,
    "should_rotate_credential": False,
    "message": "PROXY_TOKEN was empty when this worker started; the card goes back to the queue",
}


def classify(environ, status_code=401, config=PROXY_CONFIG):
    hook = missing_secret.make_classification_hook(lambda: config, environ)
    return hook(provider="custom", model="model-alias", status_code=status_code, error=Exception("Invalid API key"))


class ClassificationHookTest(unittest.TestCase):
    def test_empty_token_in_a_worker_is_transient(self):
        self.assertEqual(classify({**WORKER, "PROXY_TOKEN": ""}), TRANSIENT)

    def test_unset_token_in_a_worker_is_transient(self):
        self.assertEqual(classify(WORKER), TRANSIENT)
        self.assertEqual(classify({**WORKER, "PROXY_TOKEN": "  "}, status_code=403), TRANSIENT)

    def test_present_token_keeps_hermes_classification(self):
        self.assertIsNone(classify({**WORKER, "PROXY_TOKEN": "a-real-token"}))

    def test_outside_a_worker_keeps_hermes_classification(self):
        self.assertIsNone(classify({"PROXY_TOKEN": ""}))
        self.assertIsNone(classify({"HERMES_KANBAN_TASK": " ", "PROXY_TOKEN": ""}))

    def test_other_statuses_keep_hermes_classification(self):
        for status in (None, 400, 404, 429, 500):
            self.assertIsNone(classify(WORKER, status_code=status))

    def test_provider_without_a_key_env_keeps_hermes_classification(self):
        keyless = {"model": {"provider": "custom:proxy"}, "providers": {"proxy": {"api": "http://127.0.0.1:8317"}}}
        self.assertIsNone(classify(WORKER, config=keyless))
        self.assertIsNone(classify(WORKER, config={}))

    def test_model_key_env_counts_for_a_bare_provider(self):
        config = {"model": {"provider": "custom", "api_key_env": "PROXY_TOKEN"}}
        self.assertEqual(classify(WORKER, config=config), TRANSIENT)


if __name__ == "__main__":
    unittest.main()
