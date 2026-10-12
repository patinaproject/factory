from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import unittest

from tests import FakeLinear, activity_ok, load_plugin, session_update_ok

plugin = load_plugin()
linear = plugin.linear
tools = plugin.tools


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_client(fake, clock=None, token_cache_path=None):
    return linear.LinearClient(
        "client-id", "client-secret", urlopen=fake, clock=clock or Clock(), token_cache_path=token_cache_path
    )


class ActivityTest(unittest.TestCase):
    def post(self, args):
        fake = FakeLinear([activity_ok("activity-42"), session_update_ok()])
        output = json.loads(tools.run_activity(lambda: make_client(fake), args))
        return fake, output

    def test_body_types_send_type_and_body(self):
        for activity_type in ("thought", "response", "elicitation", "error"):
            with self.subTest(activity_type):
                fake, output = self.post({"agent_session_id": "session-1", "type": activity_type, "body": "Hello"})
                self.assertEqual(output, {"ok": True, "agent_activity_id": "activity-42"})
                self.assertEqual(
                    fake.graphql_requests[0]["variables"],
                    {"input": {"agentSessionId": "session-1", "content": {"type": activity_type, "body": "Hello"}}},
                )
                self.assertIn("agentActivityCreate(input: $input)", fake.graphql_requests[0]["query"])

    def test_ephemeral_thought(self):
        fake, _ = self.post({"agent_session_id": "session-1", "type": "thought", "body": "Hi", "ephemeral": True})
        self.assertEqual(
            fake.graphql_requests[0]["variables"],
            {"input": {"agentSessionId": "session-1", "content": {"type": "thought", "body": "Hi"}, "ephemeral": True}},
        )

    def test_action_without_and_with_result(self):
        fake, _ = self.post(
            {"agent_session_id": "session-1", "type": "action", "action": "Queued", "parameter": "t_1"}
        )
        self.assertEqual(
            fake.graphql_requests[0]["variables"]["input"]["content"],
            {"type": "action", "action": "Queued", "parameter": "t_1"},
        )
        fake, _ = self.post(
            {"agent_session_id": "session-1", "type": "action", "action": "Ran", "parameter": "tests", "result": "ok"}
        )
        self.assertEqual(
            fake.graphql_requests[0]["variables"]["input"]["content"],
            {"type": "action", "action": "Ran", "parameter": "tests", "result": "ok"},
        )

    def test_external_urls_replace_session_urls(self):
        urls = [{"label": "Pull request", "url": "https://github.com/example-org/example-repo/pull/1"}]
        fake, output = self.post(
            {"agent_session_id": "session-1", "type": "response", "body": "Done", "external_urls": urls}
        )
        self.assertEqual(output["external_urls"], ["https://github.com/example-org/example-repo/pull/1"])
        update = fake.graphql_requests[1]
        self.assertIn("agentSessionUpdate(id: $id, input: $input)", update["query"])
        self.assertEqual(update["variables"], {"id": "session-1", "input": {"externalUrls": urls}})

    def test_invalid_activity_is_an_error_without_calling_linear(self):
        fake, output = self.post({"agent_session_id": "session-1", "type": "thought"})
        self.assertEqual(output, {"error": "a thought activity needs body"})
        fake, output = self.post({"agent_session_id": "session-1", "type": "prompt", "body": "x"})
        self.assertTrue(output["error"].startswith("unknown activity type 'prompt'"))
        self.assertEqual(fake.graphql_requests, [])

    def test_graphql_error_becomes_error_json(self):
        fake = FakeLinear([(200, {"errors": [{"message": "Entity not found: AgentSession"}], "data": None})])
        output = json.loads(
            tools.run_activity(
                lambda: make_client(fake), {"agent_session_id": "missing", "type": "thought", "body": "x"}
            )
        )
        self.assertEqual(output, {"error": "Entity not found: AgentSession"})

    def test_graphql_error_on_http_400(self):
        fake = FakeLinear([(400, {"errors": [{"message": "Argument Validation Error"}]})])
        with self.assertRaisesRegex(linear.LinearError, "Argument Validation Error"):
            make_client(fake).create_activity("session-1", {"type": "thought", "body": "x"})


class TokenTest(unittest.TestCase):
    def test_token_request_uses_client_credentials(self):
        fake = FakeLinear([activity_ok()])
        make_client(fake).create_activity("session-1", {"type": "thought", "body": "x"})
        expected_auth = "Basic " + base64.b64encode(b"client-id:client-secret").decode()
        self.assertEqual(
            fake.token_requests,
            [
                {
                    "form": {"grant_type": "client_credentials", "scope": "read,write,app:assignable,app:mentionable"},
                    "authorization": expected_auth,
                    "content_type": "application/x-www-form-urlencoded",
                }
            ],
        )
        self.assertEqual(fake.graphql_requests[0]["authorization"], "Bearer token-1")

    def test_token_is_reused_until_expiry_then_refreshed(self):
        clock = Clock()
        fake = FakeLinear([activity_ok(), activity_ok(), activity_ok()], expires_in=3600)
        client = make_client(fake, clock)
        client.create_activity("session-1", {"type": "thought", "body": "x"})
        clock.now += 3600 - 61
        client.create_activity("session-1", {"type": "thought", "body": "x"})
        self.assertEqual(len(fake.token_requests), 1)
        clock.now += 2
        client.create_activity("session-1", {"type": "thought", "body": "x"})
        self.assertEqual(len(fake.token_requests), 2)
        self.assertEqual(
            [r["authorization"] for r in fake.graphql_requests], ["Bearer token-1", "Bearer token-1", "Bearer token-2"]
        )

    def test_unauthorized_mints_a_new_token_and_retries_once(self):
        fake = FakeLinear([(401, {"errors": [{"message": "Authentication required"}]}), activity_ok("activity-7")])
        activity_id = make_client(fake).create_activity("session-1", {"type": "thought", "body": "x"})
        self.assertEqual(activity_id, "activity-7")
        self.assertEqual([r["authorization"] for r in fake.graphql_requests], ["Bearer token-1", "Bearer token-2"])

    def test_persisted_token_is_private_and_shared_across_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state", "token.json")
            fake = FakeLinear([activity_ok(), activity_ok()])
            make_client(fake, token_cache_path=path).create_activity("session-1", {"type": "thought", "body": "x"})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            make_client(fake, token_cache_path=path).create_activity("session-1", {"type": "thought", "body": "x"})
            self.assertEqual(len(fake.token_requests), 1)
            self.assertEqual(fake.graphql_requests[1]["authorization"], "Bearer token-1")

    def test_corrupt_token_cache_mints_a_new_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token.json")
            with open(path, "w") as f:
                f.write("{not json")
            fake = FakeLinear([activity_ok()])
            make_client(fake, token_cache_path=path).create_activity("session-1", {"type": "thought", "body": "x"})
            self.assertEqual(len(fake.token_requests), 1)
            with open(path) as f:
                self.assertEqual(json.load(f)["access_token"], "token-1")

    def test_token_failure_is_reported(self):
        def refuse(request, timeout=None):
            import io
            import urllib.error

            body = b'{"error":"Error","error_description":"Client does not support the client_credentials grant type"}'
            raise urllib.error.HTTPError(request.full_url, 400, "error", {}, io.BytesIO(body))

        output = json.loads(tools.run_issue(lambda: make_client(refuse), {"issue": "ABC-1"}))
        self.assertEqual(
            output,
            {"error": "client credentials token request failed: Client does not support the client_credentials grant type"},
        )

    def test_missing_credentials(self):
        with self.assertRaisesRegex(linear.LinearError, "set LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET"):
            linear.client_from_env({})

    def test_token_cache_path_resolution(self):
        self.assertIsNone(linear.token_cache_path({}))
        self.assertEqual(
            linear.token_cache_path({"HERMES_HOME": "/h"}), "/h/plugin-data/linear-agent-session/token.json"
        )


class SessionCreateTest(unittest.TestCase):
    def test_creates_a_session_on_the_issue(self):
        fake = FakeLinear(
            [(200, {"data": {"agentSessionCreateOnIssue": {"success": True, "agentSession": {"id": "session-77"}}}})]
        )
        output = json.loads(tools.run_session_create(lambda: make_client(fake), {"issue": "ABC-123"}))
        self.assertEqual(output, {"agent_session_id": "session-77"})
        self.assertEqual(fake.graphql_requests[0]["variables"], {"input": {"issueId": "ABC-123"}})
        self.assertIn("agentSessionCreateOnIssue", fake.graphql_requests[0]["query"])

    def test_unsuccessful_create_is_an_error(self):
        fake = FakeLinear([(200, {"data": {"agentSessionCreateOnIssue": {"success": False, "agentSession": None}}})])
        output = json.loads(tools.run_session_create(lambda: make_client(fake), {"issue": "ABC-123"}))
        self.assertEqual(output, {"error": "agentSessionCreateOnIssue reported success: false"})


class IssueArgumentTest(unittest.TestCase):
    """Hermes defers these tools, so models call them without the schema and guess the argument name."""

    RUNNERS = {
        "linear_issue": lambda fake, args: tools.run_issue(lambda: make_client(fake), args),
        "linear_agent_session_create": lambda fake, args: tools.run_session_create(lambda: make_client(fake), args),
        "linear_issue_undelegate": lambda fake, args: tools.run_undelegate(lambda: make_client(fake), args),
    }

    def test_issue_tools_take_the_names_models_guess(self):
        for tool, run in self.RUNNERS.items():
            for name in ("issue", "id", "issue_id"):
                with self.subTest(tool=tool, name=name):
                    fake = FakeLinear([(200, {"errors": [{"message": "stop after the request"}]})])
                    run(fake, {name: "ABC-9"})
                    self.assertIn("ABC-9", json.dumps(fake.graphql_requests[0]["variables"]))

    def test_issue_tools_without_an_issue_name_the_argument(self):
        for tool, run in self.RUNNERS.items():
            with self.subTest(tool=tool):
                fake = FakeLinear([])
                output = json.loads(run(fake, {}))
                self.assertEqual(output, {"error": "pass issue: the issue UUID or identifier such as ABC-123"})
                self.assertEqual(fake.graphql_requests, [])

    def test_hermes_lets_every_guessed_name_reach_the_handler(self):
        # Hermes refuses a deferred tool_call that lacks a name in the schema's "required" list.
        for schema in (tools.ISSUE_SCHEMA, tools.SESSION_CREATE_SCHEMA, tools.UNDELEGATE_SCHEMA):
            for name in ("issue", "id", "issue_id"):
                with self.subTest(tool=schema["name"], name=name):
                    missing = [r for r in schema["parameters"].get("required", []) if r not in {name: "ABC-9"}]
                    self.assertEqual(missing, [])

    def test_the_deferred_catalog_line_names_the_argument(self):
        # Hermes lists a deferred tool by the first 60 characters of its description.
        for schema in (tools.ISSUE_SCHEMA, tools.SESSION_CREATE_SCHEMA, tools.UNDELEGATE_SCHEMA):
            with self.subTest(tool=schema["name"]):
                self.assertIn("`issue`", schema["description"][:60])


class IssueTest(unittest.TestCase):
    RESPONSE = {
        "data": {
            "issue": {
                "id": "2f9a6f0e-0000-4000-8000-000000000001",
                "identifier": "ABC-123",
                "title": "Fix checkout",
                "url": "https://linear.app/example/issue/ABC-123/fix-checkout",
                "branchName": "abc-123-fix-checkout",
                "priority": 2.0,
                "state": {"name": "Todo", "type": "unstarted"},
                "delegate": {"id": "app-user-1"},
                "attachments": {
                    "nodes": [
                        {"url": "https://github.com/example-org/example-repo/issues/9", "sourceType": "github"}
                    ]
                },
                "agentSessions": {
                    "nodes": [
                        {"id": "s-old", "status": "complete", "createdAt": "2026-01-01T00:00:00.000Z",
                         "appUser": {"id": "app-user-1"}},
                        {"id": "s-new", "status": "active", "createdAt": "2026-01-02T00:00:00.000Z",
                         "appUser": {"id": "app-user-1"}},
                    ]
                },
            }
        }
    }

    def test_issue_fields(self):
        fake = FakeLinear([(200, self.RESPONSE)])
        output = json.loads(tools.run_issue(lambda: make_client(fake), {"issue": "ABC-123"}))
        self.assertEqual(
            output,
            {
                "id": "2f9a6f0e-0000-4000-8000-000000000001",
                "identifier": "ABC-123",
                "title": "Fix checkout",
                "url": "https://linear.app/example/issue/ABC-123/fix-checkout",
                "gitBranchName": "abc-123-fix-checkout",
                "priority": 2,
                "priorityName": "High",
                "cardPriority": 3,
                "state": "Todo",
                "stateType": "unstarted",
                "delegate": "app-user-1",
                "attachments": [
                    {"url": "https://github.com/example-org/example-repo/issues/9", "sourceType": "github"}
                ],
                "agentSessions": [
                    {"id": "s-new", "status": "active", "createdAt": "2026-01-02T00:00:00.000Z", "appUserId": "app-user-1"},
                    {"id": "s-old", "status": "complete", "createdAt": "2026-01-01T00:00:00.000Z", "appUserId": "app-user-1"},
                ],
            },
        )
        self.assertEqual(fake.graphql_requests[0]["variables"], {"id": "ABC-123"})

    def test_card_priority_inverts_the_linear_scale(self):
        expected = {0: ("No priority", 0), 1: ("Urgent", 4), 2: ("High", 3), 3: ("Medium", 2), 4: ("Low", 1)}
        for linear_priority, (name, card_priority) in expected.items():
            with self.subTest(linear_priority=linear_priority):
                response = json.loads(json.dumps(self.RESPONSE))
                response["data"]["issue"]["priority"] = float(linear_priority)
                issue = make_client(FakeLinear([(200, response)])).get_issue("ABC-123")
                self.assertEqual((issue["priorityName"], issue["cardPriority"]), (name, card_priority))

    def test_issue_without_delegate(self):
        response = json.loads(json.dumps(self.RESPONSE))
        response["data"]["issue"]["delegate"] = None
        fake = FakeLinear([(200, response)])
        self.assertIsNone(make_client(fake).get_issue("ABC-123")["delegate"])


def issue_node(identifier, attachments=()):
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title {identifier}",
        "url": f"https://linear.app/example/issue/{identifier}",
        "branchName": identifier.lower(),
        "state": {"name": "In Progress", "type": "started"},
        "attachments": {"nodes": [{"url": u, "sourceType": "github"} for u in attachments]},
    }


def issues_page(nodes, end_cursor=None):
    page_info = {"hasNextPage": end_cursor is not None, "endCursor": end_cursor}
    return 200, {"data": {"issues": {"nodes": nodes, "pageInfo": page_info}}}


class DelegatedIssuesTest(unittest.TestCase):
    def test_lists_open_delegated_issues_across_pages(self):
        pr = "https://github.com/example-org/example-repo/pull/3"
        fake = FakeLinear([issues_page([issue_node("ABC-1", [pr])], "cursor-1"), issues_page([issue_node("ABC-2")])])
        output = json.loads(tools.run_delegated_issues(lambda: make_client(fake), {}))
        self.assertEqual([i["identifier"] for i in output["issues"]], ["ABC-1", "ABC-2"])
        self.assertEqual(
            output["issues"][0],
            {
                "id": "id-ABC-1",
                "identifier": "ABC-1",
                "title": "Title ABC-1",
                "url": "https://linear.app/example/issue/ABC-1",
                "gitBranchName": "abc-1",
                "state": "In Progress",
                "stateType": "started",
                "attachments": [{"url": pr, "sourceType": "github"}],
            },
        )
        open_filter = {"delegate": {"isMe": {"eq": True}}, "state": {"type": {"nin": ["completed", "canceled"]}}}
        self.assertEqual(
            [r["variables"] for r in fake.graphql_requests],
            [{"filter": open_filter, "after": None}, {"filter": open_filter, "after": "cursor-1"}],
        )

    def test_include_closed_drops_the_state_filter(self):
        fake = FakeLinear([issues_page([])])
        output = json.loads(tools.run_delegated_issues(lambda: make_client(fake), {"include_closed": True}))
        self.assertEqual(output, {"issues": []})
        self.assertEqual(fake.graphql_requests[0]["variables"]["filter"], {"delegate": {"isMe": {"eq": True}}})


def issue_delegate(delegate_id, viewer_id="app-user-1"):
    delegate = {"id": delegate_id} if delegate_id else None
    issue = {"id": "issue-uuid", "identifier": "ABC-1", "delegate": delegate}
    return 200, {"data": {"viewer": {"id": viewer_id}, "issue": issue}}


class UndelegateTest(unittest.TestCase):
    def run_undelegate(self, responses):
        fake = FakeLinear(responses)
        return fake, json.loads(tools.run_undelegate(lambda: make_client(fake), {"issue": "ABC-1"}))

    def test_clears_its_own_delegation(self):
        fake, output = self.run_undelegate([issue_delegate("app-user-1"), (200, {"data": {"issueUpdate": {"success": True}}})])
        self.assertEqual(output, {"identifier": "ABC-1", "removed": True})
        self.assertEqual(fake.graphql_requests[0]["variables"], {"id": "ABC-1"})
        self.assertIn("issueUpdate(id: $id, input: $input)", fake.graphql_requests[1]["query"])
        self.assertEqual(fake.graphql_requests[1]["variables"], {"id": "issue-uuid", "input": {"delegateId": None}})

    def test_no_delegate_is_a_no_op(self):
        fake, output = self.run_undelegate([issue_delegate(None)])
        self.assertEqual(output, {"identifier": "ABC-1", "removed": False})
        self.assertEqual(len(fake.graphql_requests), 1)

    def test_refuses_another_agents_delegation(self):
        fake, output = self.run_undelegate([issue_delegate("other-agent")])
        self.assertIn("delegated to another agent", output["error"])
        self.assertEqual(len(fake.graphql_requests), 1)

    def test_unsuccessful_update_is_an_error(self):
        _, output = self.run_undelegate([issue_delegate("app-user-1"), (200, {"data": {"issueUpdate": {"success": False}}})])
        self.assertEqual(output, {"error": "issueUpdate reported success: false"})


if __name__ == "__main__":
    unittest.main()
