from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from tests.support import SCRIPTS

_loader = SourceFileLoader("probe_sandbox", str(SCRIPTS / "probe-sandbox"))
probe = module_from_spec(spec_from_loader("probe_sandbox", _loader))
_loader.exec_module(probe)

CALL = {"name": "Read", "input": {"file_path": "/decoy/.env"}}


def user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


class FakeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = probe.FakeApi({"Read": [CALL, CALL]})
        self.addCleanup(self.api.server_close)

    def test_a_step_gets_its_scripted_calls_in_order_then_ends(self) -> None:
        first = self.api.respond({"messages": [user("<reminder>"), user("PROBE-STEP:Read")]})
        self.assertEqual((first["type"], first["name"], first["input"]), ("tool_use", "Read", {"file_path": "/decoy/.env"}))
        made = {"role": "assistant", "content": [first]}
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": first["id"], "content": "x"}]}
        second = self.api.respond({"messages": [user("PROBE-STEP:Read"), made, result]})
        self.assertEqual(second["name"], "Read")
        third = self.api.respond({"messages": [user("PROBE-STEP:Read"), made, result, {"role": "assistant", "content": [second]}]})
        self.assertEqual(third, {"type": "text", "text": "done"})
        self.assertEqual(self.api.calls, ["Read", "Read"])

    def test_tool_results_are_recorded_from_any_message_and_tools_offered(self) -> None:
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "DECOY1"}]}
        system = {"role": "system", "content": "reminder"}
        self.api.respond({"messages": [user("PROBE-STEP:none"), result, system], "tools": [{"name": "Bash"}]})
        self.assertEqual(self.api.results, ['"DECOY1"'])
        self.assertEqual(self.api.offered, {"Bash"})

    def test_a_conversation_without_a_step_ends_at_once(self) -> None:
        self.assertEqual(self.api.respond({"messages": [user("Write a title")]}), {"type": "text", "text": "done"})

    def test_a_streamed_answer_carries_the_tool_call(self) -> None:
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self.api.shutdown)
        body = json.dumps({"stream": True, "model": "m", "messages": [user("PROBE-STEP:Read")]}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.api.server_port}/v1/messages", data=body, headers={"x-api-key": "k"},
        )
        with urllib.request.urlopen(request) as response:
            events = [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events], [
            "message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop",
        ])
        self.assertEqual(json.loads(events[2]["delta"]["partial_json"]), {"file_path": "/decoy/.env"})
        self.assertEqual(events[4]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(self.api.keys, {"k"})


class SecretPathsTest(unittest.TestCase):
    def test_each_home_and_the_identity_and_key_are_covered(self) -> None:
        paths = probe.secret_paths(Path("/h/.hermes"), Path("/h"), Path("/h/key.pem"))
        self.assertEqual(paths, [
            "/h/.hermes/.env", "/h/.hermes/config.yaml*", "/h/.hermes/auth.json", "/h/.hermes/state.db*",
            "/h/.hermes/profiles/*/.env", "/h/.hermes/profiles/*/config.yaml*", "/h/.hermes/profiles/*/auth.json",
            "/h/.hermes/profiles/*/state.db*", "/h/.config/infisical", "/h/key.pem",
        ])

    def test_sandbox_settings_deny_bash_and_every_tool(self) -> None:
        settings = probe.sandbox_settings(["/h/.env"])
        self.assertEqual(settings["sandbox"]["filesystem"]["denyRead"], ["/h/.env"])
        self.assertIs(settings["sandbox"]["allowUnsandboxedCommands"], False)
        self.assertEqual(settings["permissions"]["deny"], ["Read(//h/.env)", "Read(//h/.env/**)"])


if __name__ == "__main__":
    unittest.main()
