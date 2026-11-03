from __future__ import annotations

import asyncio
import contextlib
import enum
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.test_triage_intent import (
    APP,
    ASSIGNEE,
    BRANCH,
    DELEGATOR,
    ISSUE_ID,
    NOW,
    OPERATOR,
    OTHER,
    USERS,
    FakeLinearClient,
    Intent,
    card,
    delegation,
    id_line,
    intent_module,
    issue,
    person,
    plugin,
)

History = plugin.triage_exit.History
Park = plugin.triage_exit.Park

COMMENT_ID = "6c1f0a52-3a8e-4f43-9d0b-1f5c2a7d9e01"
BOT = "user-bot"
TED = "https://linear.app/example/profiles/ted"
GOODYEAR = "https://linear.app/example/profiles/goodyear"
COMMENT_URL = f"https://linear.app/example/issue/PAT-1/fix-it#comment-{COMMENT_ID[:8]}"
PEOPLE = {
    **USERS,
    ASSIGNEE: person(ASSIGNEE, "Ann Assignee"),
    BOT: person(BOT, "Factory", app=True),
}
PROFILES = {APP: GOODYEAR}
SESSION_LINE = "linear_agent_session_id: sess-card\n"
NEEDS_INPUT = History([Park("Needs input: which screen?", 100)], [])
NOBODY_WAITS = "no card of the issue waits for a person"


def comment_event(author=OPERATOR, body="Use the blue screen.", parent=None, action="create", comment_id=COMMENT_ID):
    return {
        "action": action,
        "type": "Comment",
        "actor": {"id": author, "type": "user", "name": "Olive Operator"},
        "createdAt": "2026-10-27T11:59:58.000Z",
        "data": {
            "id": comment_id,
            "createdAt": "2026-10-27T11:59:58.000Z",
            "updatedAt": "2026-10-27T11:59:58.000Z",
            "body": body,
            "issueId": ISSUE_ID,
            "userId": author,
            "parentId": parent,
            "reactionData": [],
        },
        "url": COMMENT_URL,
        "organizationId": "org-1",
        "webhookTimestamp": int(NOW.timestamp() * 1000),
        "webhookId": "hook-1",
    }


def triage_card(status="scheduled", body=None):
    return card("t_triage", status=status, body=id_line() + SESSION_LINE if body is None else body)


def work_card(status="blocked", body=""):
    return card("t_work", status=status, branch=BRANCH, body=body)


def boards(triage=None, work=None):
    return {"triage": [triage] if triage else [], None: [work] if work else []}


def run_compute(payload, cards, histories=None, live=None, operators=(OPERATOR,), **client_options):
    client = FakeLinearClient(live or issue(), **{"users": PEOPLE, "profiles": PROFILES, **client_options})
    return intent_module.compute(
        payload,
        app_user_id=lambda: APP,
        operator_user_ids=lambda: list(operators),
        client=lambda: client,
        cards=lambda board: cards[board],
        history=lambda board, task_id: (histories or {}).get(task_id, NEEDS_INPUT),
        now=NOW,
        redact=lambda text: text,
    )


def resumed(**overrides):
    return replace(
        Intent(
            action="comment_resume",
            issue={"id": ISSUE_ID, "identifier": "PAT-1", "state_type": "backlog"},
            delegated=True,
            triage_card={"id": "t_triage", "status": "scheduled"},
            agent_session_id="sess-card",
            reply="Use the blue screen.",
            reply_source="payload",
            reply_author_id=OPERATOR,
            reply_author_kind="person",
            assignee={"id": ASSIGNEE, "name": "Ann Assignee", "gitHubUserId": "1001"},
            from_assignee=True,
            authorized_as="operator",
            approvers=[
                {"id": ASSIGNEE, "name": "Ann Assignee", "role": "assignee"},
                {"id": OPERATOR, "name": "Olive Operator", "role": "operator"},
            ],
            comment_id=COMMENT_ID,
            comment_url=COMMENT_URL,
        ),
        **overrides,
    )


def skipped(reason):
    return Intent(action="skip_comment", skip_reason=reason)


class ResumeTest(unittest.TestCase):
    def test_an_operators_comment_resumes_a_triage_card_scheduled_for_a_person(self):
        self.assertEqual(run_compute(comment_event(), boards(triage_card())), resumed())

    def test_a_delegators_comment_resumes_a_blocked_work_card_on_a_delegated_issue(self):
        open_session = {
            "id": "sess-open", "status": "active", "createdAt": "2026-10-27T10:00:00.000Z", "appUserId": APP, "creatorId": DELEGATOR,
        }
        result = run_compute(
            comment_event(author=DELEGATOR),
            boards(work=work_card()),
            history=[delegation(DELEGATOR)],
            live=issue(agentSessions=[open_session]),
        )
        self.assertEqual(
            (result.action, result.card, result.triage_card, result.authorized_as, result.agent_session_id),
            ("comment_resume", {"id": "t_work", "status": "blocked", "matched_by": "branch"}, None, "delegator", "sess-open"),
        )

    def test_the_assignees_comment_resumes_a_work_card_in_triage_status(self):
        result = run_compute(comment_event(author=ASSIGNEE), boards(work=work_card("triage", SESSION_LINE)))
        self.assertEqual(
            (result.action, result.card["status"], result.authorized_as, result.agent_session_id),
            ("comment_resume", "triage", "assignee", "sess-card"),
        )

    def test_no_session_on_the_card_or_the_issue_leaves_the_session_null(self):
        result = run_compute(comment_event(author=ASSIGNEE), boards(work=work_card()))
        self.assertEqual((result.action, result.agent_session_id), ("comment_resume", None))

    def test_the_triage_card_wins_when_both_cards_wait(self):
        result = run_compute(comment_event(), boards(triage_card("blocked"), work_card()))
        self.assertEqual((result.triage_card, result.card), ({"id": "t_triage", "status": "blocked"}, None))

    def test_a_work_card_waits_when_the_triage_card_is_done(self):
        result = run_compute(comment_event(), boards(triage_card("done"), work_card()))
        self.assertEqual((result.action, result.card["id"], result.triage_card), ("comment_resume", "t_work", None))

    def test_a_stop_before_the_park_does_not_end_the_wait(self):
        history = History([Park("Needs input: which?", 100)], [99])
        self.assertEqual(run_compute(comment_event(), boards(triage_card()), {"t_triage": history}).action, "comment_resume")

    def test_a_payload_url_that_names_the_comment_is_kept(self):
        self.assertEqual(run_compute(comment_event(), boards(triage_card())).comment_url, COMMENT_URL)

    def test_the_comment_url_is_built_from_the_issue_when_the_payload_has_none(self):
        payload = {**comment_event(), "url": None}
        live = issue(url="https://linear.app/example/issue/PAT-1/fix-it")
        result = run_compute(payload, boards(triage_card()), live=live)
        self.assertEqual(result.comment_url, f"https://linear.app/example/issue/PAT-1/fix-it#comment-{COMMENT_ID}")


class SkipTest(unittest.TestCase):
    def test_a_person_who_is_not_an_approver_gets_a_skip(self):
        result = run_compute(comment_event(author=OTHER), boards(triage_card()))
        self.assertEqual(result, skipped("the comment's author is not an approver"))

    def test_a_card_that_waits_for_no_one_gets_a_skip(self):
        for status in ("running", "ready", "review", "done", "todo", "archived"):
            with self.subTest(status):
                self.assertEqual(run_compute(comment_event(), boards(work=work_card(status))), skipped(NOBODY_WAITS))
                self.assertEqual(run_compute(comment_event(), boards(triage_card(status))), skipped(NOBODY_WAITS))
        self.assertEqual(run_compute(comment_event(), boards()), skipped(NOBODY_WAITS))

    def test_a_scheduled_card_parked_for_a_machine_condition_gets_a_skip(self):
        for park in plugin.triage_intent.comment_resume.MACHINE_PARKS:
            with self.subTest(park):
                history = History([Park("Needs input: earlier", 50), Park(f"{park} detail", 100)], [])
                result = run_compute(comment_event(), boards(triage_card()), {"t_triage": history})
                self.assertEqual(result, skipped(NOBODY_WAITS))

    def test_a_card_parked_for_the_worktree_or_a_server_error_does_not_wait_for_a_person(self):
        for reason in ("Waiting for the worktree: t_other holds it", "Waiting after a server error: 502"):
            with self.subTest(reason):
                history = History([Park("Needs input: earlier", 50), Park(reason, 100)], [])
                self.assertFalse(plugin.triage_intent.comment_resume.scheduled_waits_for_person(history))

    def test_a_card_parked_for_a_hermes_pause_gets_a_skip(self):
        history = History([Park("Waiting for hermes resume: claude-session was stopped by SIGTERM", 100)], [])

        result = run_compute(comment_event(), boards(triage_card()), {"t_triage": history})

        self.assertEqual(result, skipped(NOBODY_WAITS))

    def test_a_person_who_stopped_the_card_after_its_park_gets_a_skip(self):
        for stopped_at in (100, 150):
            with self.subTest(stopped_at):
                history = History([Park("Needs input: which?", 100)], [stopped_at])
                result = run_compute(comment_event(), boards(triage_card()), {"t_triage": history})
                self.assertEqual(result, skipped(NOBODY_WAITS))

    def test_a_blocked_work_card_on_an_undelegated_issue_gets_a_skip(self):
        result = run_compute(comment_event(), boards(work=work_card()), live=issue(delegate=None))
        self.assertEqual(result, skipped(NOBODY_WAITS))

    def test_an_app_account_or_an_unreadable_author_gets_a_skip(self):
        cases = {
            "app": (comment_event(author=BOT), "app"),
            "unreadable": (comment_event(author="user-ghost"), "unknown"),
            "null": (comment_event(author=None), "unknown"),
        }
        for name, (payload, kind) in cases.items():
            with self.subTest(name):
                self.assertEqual(run_compute(payload, boards(triage_card())), skipped(f"the comment's author is {kind}, not a person"))

    def test_a_failed_author_read_is_unknown_and_gets_a_skip(self):
        result = run_compute(comment_event(), boards(triage_card()), lookup_error=RuntimeError("down"))
        self.assertEqual(result, skipped("the comment's author is unknown, not a person"))

    def test_a_reply_in_a_thread_gets_a_skip(self):
        result = run_compute(comment_event(parent="parent-comment"), boards(triage_card()))
        self.assertEqual(result, skipped("the comment is a reply in a thread"))

    def test_an_edit_or_a_removal_gets_a_skip(self):
        for action in ("update", "remove"):
            with self.subTest(action):
                result = run_compute(comment_event(action=action), boards(triage_card()))
                self.assertEqual(result, skipped("the delivery is not a created comment"))

    def test_a_comment_without_an_issue_gets_a_skip(self):
        payload = comment_event()
        del payload["data"]["issueId"]
        self.assertEqual(run_compute(payload, boards(triage_card())), skipped("the comment names no id, issue, or text"))

    def test_a_failed_issue_read_is_the_error_intent(self):
        result = run_compute(comment_event(), boards(triage_card()), error=RuntimeError("Linear down"))
        self.assertEqual(result, Intent(action="error", error="RuntimeError: Linear down"))


class MentionTest(unittest.TestCase):
    def action_for(self, body, **client_options):
        return run_compute(comment_event(body=body), boards(triage_card()), **client_options).action

    def test_a_comment_that_mentions_only_other_people_gets_a_skip(self):
        self.assertEqual(self.action_for(f"{TED} can you check this?"), "skip_comment")
        self.assertEqual(self.action_for(f"{TED} and https://linear.app/example/profiles/luke please look"), "skip_comment")

    def test_a_comment_that_mentions_the_agent_app_resumes(self):
        self.assertEqual(self.action_for(f"{GOODYEAR} use the blue screen"), "comment_resume")
        self.assertEqual(self.action_for(f"{TED} {GOODYEAR} use the blue screen"), "comment_resume")

    def test_a_comment_that_mentions_nobody_resumes(self):
        self.assertEqual(self.action_for("Use the blue screen. See https://example.com/profiles/ted"), "comment_resume")

    def test_the_profile_comparison_ignores_case(self):
        self.assertEqual(self.action_for("https://linear.app/example/profiles/GoodYear use blue"), "comment_resume")

    def test_an_unreadable_app_profile_is_the_error_intent_only_when_the_comment_mentions_someone(self):
        self.assertEqual(self.action_for(f"{TED} hi", profiles={APP: None}), "error")
        self.assertEqual(self.action_for("hi", profiles={APP: None}), "comment_resume")


class Platform(enum.Enum):
    WEBHOOK = "webhook"


class Event:
    def __init__(self, payload):
        self.source = SimpleNamespace(platform=Platform.WEBHOOK, user_id="webhook:linear")
        self.raw_message = payload
        self.text = "rendered prompt"


class RecordingClient:
    def __init__(self):
        self.activities = []

    def create_activity(self, *args, **kwargs):
        self.activities.append((args, kwargs))


CONFIG = {"platforms": {"webhook": {"extra": {"routes": {"linear": {"secret": "resolved"}}}}}}


class HookTest(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.TemporaryDirectory()
        self.addCleanup(self.store.cleanup)
        self.client = RecordingClient()
        self.cards = boards(triage_card())

    def deliver(self, payload):
        def triage_exit(_):
            raise AssertionError("a Comment never reaches the triage-exit step")

        hook = plugin.make_dispatch_hook(
            lambda: self.client,
            lambda: "linear",
            lambda: CONFIG,
            triage_exit,
            lambda delivery: run_compute(delivery, self.cards),
            comment_store=lambda: self.store.name,
        )
        return asyncio.run(hook(event=Event(payload), gateway=object(), session_store=None))

    def test_a_resuming_comment_gets_the_intent_block_and_no_acknowledgement(self):
        result = self.deliver(comment_event())
        self.assertEqual(result["action"], "rewrite")
        self.assertTrue(result["text"].startswith("rendered prompt\n\n## Computed triage intent\n\n<triage_intent>\n{"))
        self.assertTrue(result["text"].endswith("</triage_intent>\n\n"))
        self.assertIn('"action": "comment_resume"', result["text"])
        self.assertIn(f'"comment_id": "{COMMENT_ID}"', result["text"])
        self.assertEqual(self.client.activities, [])

    def test_a_comment_that_resumes_nothing_is_skipped_with_its_reason(self):
        result = self.deliver(comment_event(author=OTHER))
        self.assertEqual(result, {"action": "skip", "reason": "the comment's author is not an approver"})
        self.assertEqual(self.client.activities, [])

    def test_the_same_comment_delivered_twice_resumes_once(self):
        first, second = self.deliver(comment_event()), self.deliver(comment_event())
        self.assertEqual(first["action"], "rewrite")
        self.assertEqual(second, {"action": "skip", "reason": "the comment already resumed its card"})

    def test_another_comment_resumes_again(self):
        self.deliver(comment_event())
        other = "0b3f7d9e-8c1a-4e55-a2b6-4d7e9f01c3a2"
        self.assertEqual(self.deliver(comment_event(comment_id=other))["action"], "rewrite")

    def test_a_skipped_comment_does_not_use_up_its_id(self):
        self.cards = boards()
        self.assertEqual(self.deliver(comment_event())["action"], "skip")
        self.cards = boards(triage_card())
        self.assertEqual(self.deliver(comment_event())["action"], "rewrite")

    def test_a_comment_whose_id_a_crashed_hook_reserved_is_skipped(self):
        def crash():
            raise RuntimeError("killed")

        with mock.patch.object(plugin.idempotency.os, "unlink"), self.assertRaises(RuntimeError):
            plugin.idempotency.once(self.store.name, "comment_resume", COMMENT_ID, {}, crash)
        path = os.path.join(self.store.name, hashlib.sha256(COMMENT_ID.encode()).hexdigest() + ".json")
        with open(path) as record:
            self.assertEqual(json.load(record)["state"], "reserved")
        self.assertEqual(
            self.deliver(comment_event()),
            {
                "action": "skip",
                "reason": f"the comment's id could not be recorded: a call with idempotency_key '{COMMENT_ID}' is still running or did not finish; nothing was sent again",
            },
        )

    def test_a_comment_whose_intent_fails_is_skipped_without_a_turn(self):
        hook = plugin.make_dispatch_hook(
            lambda: self.client,
            lambda: "linear",
            lambda: CONFIG,
            lambda _: None,
            lambda delivery: run_compute(delivery, self.cards, error=RuntimeError("Linear down")),
            comment_store=lambda: self.store.name,
        )
        result = asyncio.run(hook(event=Event(comment_event()), gateway=object(), session_store=None))
        self.assertEqual(result, {"action": "skip", "reason": "the comment's intent failed: RuntimeError: Linear down"})
        self.assertEqual(self.client.activities, [])

    def test_a_comment_after_the_thread_reply_released_the_card_gets_a_skip(self):
        self.cards = boards(triage_card("ready"))
        self.assertEqual(self.deliver(comment_event()), {"action": "skip", "reason": NOBODY_WAITS})

    def test_another_route_gets_no_comment_handling(self):
        hook = plugin.make_dispatch_hook(lambda: self.client, lambda: "github", lambda: CONFIG, lambda _: None, lambda _: None)
        self.assertIsNone(asyncio.run(hook(event=Event(comment_event()), gateway=object(), session_store=None)))


def load_cli():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli.py")
    spec = importlib.util.spec_from_file_location("linear_agent_session_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReplayTest(unittest.TestCase):
    def run_cli(self, command, payload, cards):
        cli = load_cli()
        client = FakeLinearClient(issue(), users=PEOPLE, profiles=PROFILES)
        shim = SimpleNamespace(
            linear=SimpleNamespace(client_from_env=lambda: client, ACTIVITY_TYPES=[]),
            triage_exit=SimpleNamespace(
                configured_app_user_id=lambda: APP,
                configured_operator_user_ids=lambda: [OPERATOR],
                listed_cards=lambda board: cards[board],
                listed_history=lambda board, task_id: NEEDS_INPUT,
            ),
            triage_intent=intent_module,
        )
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cli, "_load_hyphenated_plugin_dir_as_package", lambda: shim):
            path = os.path.join(tmp, "payload.json")
            with open(path, "w") as handle:
                json.dump(payload, handle)
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main([command, "--payload-file", path]), 0)
        return out.getvalue()

    def test_a_replayed_comment_gets_the_block_the_live_hook_computes(self):
        cards = boards(triage_card())
        replayed = self.run_cli("triage-intent", comment_event(), cards)
        self.assertEqual(replayed, intent_module.render(run_compute(comment_event(), cards)))
        self.assertIn('"action": "comment_resume"', replayed)

    def test_comment_skip_says_whether_a_replay_needs_a_turn(self):
        waits = boards(triage_card())
        self.assertEqual(json.loads(self.run_cli("comment-skip", comment_event(), waits)), {"skip": False})
        self.assertEqual(json.loads(self.run_cli("comment-skip", comment_event(), boards())), {"skip": True})
        self.assertEqual(json.loads(self.run_cli("comment-skip", comment_event(parent="p"), waits)), {"skip": True})

    def test_comment_skip_skips_a_comment_whose_intent_fails(self):
        self.assertEqual(json.loads(self.run_cli("comment-skip", {"type": "Comment"}, boards(triage_card()))), {"skip": True})


class MachineParksTest(unittest.TestCase):
    def test_the_machine_parks_are_the_ones_blocker_order_parks_for(self):
        path = Path(__file__).resolve().parents[2] / "blocker-order" / "result_line.py"
        spec = importlib.util.spec_from_file_location("result_line_for_machine_parks", path)
        result_line = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {spec.name: result_line}):
            spec.loader.exec_module(result_line)
        self.assertEqual(plugin.triage_intent.comment_resume.MACHINE_PARKS, result_line.MACHINE_PARKS)


class StoppedTest(unittest.TestCase):
    def test_the_stop_comment_is_the_one_card_transition_writes(self):
        path = Path(__file__).resolve().parents[3] / "profiles" / "claude-worker" / "scripts" / "card_transition.py"
        written = re.search(r'^STOPPED = "([^"]+)"$', path.read_text(), re.MULTILINE).group(1)
        self.assertEqual(plugin.triage_exit.STOPPED, written)


class HistoryTest(unittest.TestCase):
    def test_a_boards_events_and_comments_become_the_waiting_rules_history(self):
        events = [
            {"kind": "scheduled", "payload": {"reason": "Needs input: one"}, "created_at": 10},
            {"kind": "commented", "payload": None, "created_at": 11},
            {"kind": "scheduled", "payload": None, "created_at": 20},
            {"kind": "scheduled", "payload": {"reason": 7}, "created_at": 30},
        ]
        comments = [{"body": "stopped", "created_at": 25}, {"body": "Linear reply: hi", "created_at": 26}]
        self.assertEqual(
            plugin.triage_exit.history_of(events, comments),
            History([Park("Needs input: one", 10), Park(None, 20), Park(None, 30)], [25]),
        )


if __name__ == "__main__":
    unittest.main()
