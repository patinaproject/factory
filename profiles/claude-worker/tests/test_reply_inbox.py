from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import SCRIPTS, GitFixture

sys.path.insert(0, str(SCRIPTS))
import reply_inbox  # noqa: E402

SESSION = "live-session"
COMMAND = "python3 /scripts/session-reply --inbox /inbox"


def comment(at: int, author: str, body: str) -> dict:
    return {"created_at": at, "author": author, "body": body}


def key(c: dict) -> list:
    return [c["created_at"], c["author"], c["body"]]


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class InboxCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.inbox = self.tmp / "replies.json"
        reply_inbox.reset(self.inbox, COMMAND, SESSION)

    def add(self, *comments: dict, session_id: str | None = None, now: float = 1000.0) -> None:
        replies = [{"key": key(c), "author": c["author"], "body": c["body"]} for c in comments]
        reply_inbox.update(self.inbox, lambda inbox: reply_inbox.add(inbox, replies, session_id, now))

    def states(self) -> list[str]:
        return [r["state"] for r in reply_inbox.load(self.inbox)["replies"]]

    def hook(self, mode: str, event: dict | None = None, inbox: Path | None = None):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "reply-hook"), mode, "--inbox", str(inbox or self.inbox)],
            input=json.dumps(event or {}), capture_output=True, text=True, timeout=30,
        )


class InboxFileTest(InboxCase):
    def test_a_fresh_inbox_is_private_and_drops_the_earlier_runs_replies(self) -> None:
        self.add(comment(1, "default", "Linear reply: old"))

        reply_inbox.reset(self.inbox, COMMAND, "next-session")

        self.assertEqual(stat.S_IMODE(self.inbox.stat().st_mode), 0o600)
        self.assertEqual(reply_inbox.load(self.inbox), {
            "session_id": "next-session", "activity_at": None, "answer_command": COMMAND, "replies": [],
        })

    def test_parallel_writers_lose_no_update(self) -> None:
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; import reply_inbox\n"
            "path = Path(sys.argv[2]); n = sys.argv[3]\n"
            "for i in range(15):\n"
            "    reply_inbox.update(path, lambda inbox: reply_inbox.add(inbox, "
            "[{'key': [n, i], 'author': 'a', 'body': 'Linear reply: x'}], None, 0.0))\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", code, str(SCRIPTS), str(self.inbox), str(n)]) for n in range(4)]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)

        self.assertEqual(len(reply_inbox.load(self.inbox)["replies"]), 60)


class WatchTest(InboxCase):
    def watch(self, card: dict | Exception, posts: list, clock: Clock | None = None, **kwargs):
        def read():
            if isinstance(card, Exception):
                raise card
            return card

        return reply_inbox.Watch(
            self.inbox, read, lambda session_id, body: posts.append((session_id, body)), key,
            start_keys=[key(comment(100, "operator", "Before."))], own="claude-worker", clock=clock or Clock(), **kwargs,
        )

    @staticmethod
    def card(*comments: dict, body: str = f"linear_agent_session_id: {SESSION}") -> dict:
        return {"task": {"body": body}, "comments": list(comments)}

    def test_only_a_new_prefixed_comment_by_someone_else_is_a_reply(self) -> None:
        reply = comment(300, "default", "Linear reply: Why SQLite?")
        card = self.card(
            comment(100, "operator", "Before."), comment(150, "default", "Linear reply: asked before the run"),
            comment(200, "operator", "No prefix here."), comment(250, "claude-worker", "Linear reply: my own"),
            reply,
        )
        card["comments"][1] = comment(100, "operator", "Before.")
        watch = self.watch(card, [])

        watch.collect()
        watch.collect()

        self.assertEqual([r["key"] for r in reply_inbox.load(self.inbox)["replies"]], [key(reply)])

    def test_a_comment_in_the_start_snapshot_is_never_a_reply(self) -> None:
        old = comment(100, "operator", "Linear reply: from before the run")
        watch = reply_inbox.Watch(
            self.inbox, lambda: self.card(old), lambda *_: None, key, start_keys=[key(old)], own="x",
        )

        watch.collect()

        self.assertEqual(reply_inbox.load(self.inbox)["replies"], [])

    def test_the_current_session_id_is_recorded_on_every_read(self) -> None:
        self.watch(self.card(body="linear_agent_session_id: second"), []).collect()

        self.assertEqual(reply_inbox.load(self.inbox)["session_id"], "second")

    def test_a_failed_read_is_logged_and_the_next_tick_retries(self) -> None:
        posts: list = []
        failing = self.watch(RuntimeError("kanban down"), posts)

        failing.tick()

        self.assertEqual(reply_inbox.load(self.inbox)["replies"], [])
        working = self.watch(self.card(comment(300, "default", "Linear reply: hi")), posts)
        working.tick()
        self.assertEqual(self.states(), ["pending"])

    def test_keepalive_posts_20_minutes_after_the_reply_and_not_before(self) -> None:
        clock, posts = Clock(1000.0), []
        watch = self.watch(self.card(comment(300, "default", "Linear reply: hi")), posts, clock)

        watch.tick()
        clock.now += 19 * 60
        watch.tick()
        self.assertEqual(posts, [])
        clock.now += 60
        watch.tick()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][0], SESSION)
        clock.now += 19 * 60
        watch.tick()
        self.assertEqual(len(posts), 1)
        clock.now += 60
        watch.tick()
        self.assertEqual(len(posts), 2)

    def test_an_answer_resets_the_keepalive_clock(self) -> None:
        clock, posts = Clock(1000.0), []
        watch = self.watch(self.card(comment(300, "default", "Linear reply: hi")), posts, clock)
        watch.tick()
        clock.now += 15 * 60
        reply_inbox.update(self.inbox, lambda inbox: reply_inbox.answered(inbox, clock.now))

        clock.now += 19 * 60
        watch.tick()

        self.assertEqual(posts, [])

    def test_a_run_with_no_reply_posts_no_keepalive(self) -> None:
        clock, posts = Clock(), []
        watch = self.watch(self.card(), posts, clock)

        watch.tick()
        clock.now += 3 * 3600
        watch.tick()

        self.assertEqual(posts, [])

    def test_a_failed_keepalive_post_does_not_raise_and_is_retried(self) -> None:
        clock, posts = Clock(), []
        attempts = []

        def post(session_id, body):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("Linear down")
            posts.append(body)

        watch = reply_inbox.Watch(
            self.inbox, lambda: self.card(comment(300, "default", "Linear reply: hi")), post, key,
            start_keys=[], own="x", clock=clock,
        )
        watch.tick()
        clock.now += 20 * 60

        watch.tick()
        watch.tick()

        self.assertEqual(len(posts), 1)

    def test_the_thread_collects_replies_and_posts_nothing_once_the_run_returns(self) -> None:
        clock, posts = Clock(), []
        watch = self.watch(self.card(comment(300, "default", "Linear reply: hi")), posts, clock, poll_seconds=0.01)

        with watch:
            deadline = time.time() + 10
            while not reply_inbox.load(self.inbox)["replies"] and time.time() < deadline:
                time.sleep(0.01)
            clock.now += 3600
        settled = len(posts)
        time.sleep(0.1)

        self.assertEqual(self.states(), ["pending"])
        self.assertEqual(len(posts), settled)
        self.assertFalse(watch.thread.is_alive())


class PostToolUseTest(InboxCase):
    def test_a_pending_reply_is_printed_as_additional_context_once(self) -> None:
        self.add(comment(300, "default", "Linear reply: Why SQLite?"))

        first = self.hook("post-tool-use")
        second = self.hook("post-tool-use")

        output = json.loads(first.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PostToolUse")
        self.assertIn("Why SQLite?", output["additionalContext"])
        self.assertNotIn("Linear reply:", output["additionalContext"])
        self.assertIn(COMMAND, output["additionalContext"])
        self.assertEqual((first.returncode, second.returncode, second.stdout), (0, 0, ""))
        self.assertEqual(self.states(), ["delivered"])

    def test_a_subagent_event_prints_nothing_and_delivers_nothing(self) -> None:
        self.add(comment(300, "default", "Linear reply: hi"))

        result = self.hook("post-tool-use", {"agent_id": "sub-1", "tool_name": "Bash"})

        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(self.states(), ["pending"])

    def test_an_empty_inbox_prints_nothing(self) -> None:
        self.assertEqual(self.hook("post-tool-use").stdout, "")

    def test_replies_over_the_limit_keep_the_start_of_each_and_say_they_were_cut(self) -> None:
        self.add(*(comment(300 + n, "default", f"Linear reply: {n}" + "x" * 6000) for n in range(3)))

        context = json.loads(self.hook("post-tool-use").stdout)["hookSpecificOutput"]["additionalContext"]

        self.assertLessEqual(len(context), 8000)
        self.assertIn("cut", context)
        self.assertIn("Reply 1:\n0xxx", context)
        self.assertIn("Reply 3:\n2xxx", context)
        self.assertEqual(self.states(), ["delivered"] * 3)

    def test_many_replies_drop_the_oldest_whole_replies_first(self) -> None:
        self.add(*(comment(300 + n, "default", f"Linear reply: number {n:02d} " + "z" * 100) for n in range(20)))

        context = json.loads(self.hook("post-tool-use").stdout)["hookSpecificOutput"]["additionalContext"]

        self.assertLessEqual(len(context), 8000)
        self.assertIn("number 19", context)
        self.assertNotIn("number 00", context)
        self.assertIn("older", context)

    def test_one_long_reply_keeps_its_start(self) -> None:
        self.add(comment(300, "default", "Linear reply: start" + "y" * 20000))

        context = json.loads(self.hook("post-tool-use").stdout)["hookSpecificOutput"]["additionalContext"]

        self.assertLessEqual(len(context), 8000)
        self.assertIn("starty", context)

    def test_an_unreadable_inbox_prints_nothing_and_exits_zero(self) -> None:
        self.inbox.write_text("{not json")

        for mode in ("post-tool-use", "stop"):
            result = self.hook(mode)
            self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(self.hook("bogus").returncode, 0)


class StopTest(InboxCase):
    def test_an_undelivered_reply_blocks_the_stop_and_is_quoted(self) -> None:
        self.add(comment(300, "default", "Linear reply: Why SQLite?"))

        output = json.loads(self.hook("stop", {"stop_hook_active": False}).stdout)

        self.assertEqual(output["decision"], "block")
        self.assertIn("Why SQLite?", output["reason"])
        self.assertIn(COMMAND, output["reason"])
        self.assertEqual(self.states(), ["delivered"])

    def test_a_delivered_unanswered_reply_blocks_once_then_allows(self) -> None:
        self.add(comment(300, "default", "Linear reply: Why SQLite?"))
        self.hook("post-tool-use")

        first = self.hook("stop")
        second = self.hook("stop", {"stop_hook_active": True})

        self.assertEqual(json.loads(first.stdout)["decision"], "block")
        self.assertIn("Why SQLite?", json.loads(first.stdout)["reason"])
        self.assertEqual((second.returncode, second.stdout), (0, ""))
        self.assertEqual(self.states(), ["asked"])

    def test_an_answered_reply_allows_the_stop(self) -> None:
        self.add(comment(300, "default", "Linear reply: Why SQLite?"))
        self.hook("post-tool-use")
        reply_inbox.update(self.inbox, lambda inbox: reply_inbox.answered(inbox, 2000.0))

        result = self.hook("stop")

        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_no_reply_allows_the_stop(self) -> None:
        self.assertEqual(self.hook("stop").stdout, "")

    def test_a_reply_that_arrives_after_an_ask_blocks_again(self) -> None:
        self.add(comment(300, "default", "Linear reply: one"))
        self.hook("post-tool-use")
        self.hook("stop")
        self.add(comment(400, "default", "Linear reply: two"))

        output = json.loads(self.hook("stop").stdout)

        self.assertIn("two", output["reason"])
        self.assertNotIn("one", output["reason"])


class SessionReplyTest(InboxCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        GitFixture.install_linear_plugin(GitFixture, self.home, cached_token=None, accepted=("run-token",))

    def reply(self, text: str, token: str | None = "run-token", home: Path | None = None):
        env = {k: v for k, v in os.environ.items() if k not in ("LINEAR_API_KEY", "HERMES_HOME")}
        env["HERMES_HOME"] = str(home or self.home)
        if token:
            env["LINEAR_API_KEY"] = token
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "session-reply"), "--inbox", str(self.inbox)],
            input=text, capture_output=True, text=True, env=env, timeout=60,
        )

    def activities(self) -> list[dict]:
        return [e["activity"] for e in GitFixture.linear_requests(self.home) if "activity" in e]

    def test_the_answer_is_one_non_ephemeral_thought_to_the_current_session_and_the_reply_is_answered(self) -> None:
        self.add(comment(300, "default", "Linear reply: one"), comment(310, "default", "Linear reply: two"))
        self.hook("post-tool-use")
        self.add(comment(320, "default", "Linear reply: three"), session_id="newer-session")

        result = self.reply("Because it is embedded.\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.activities(), [{
            "agentSessionId": "newer-session", "content": {"type": "thought", "body": "Because it is embedded."},
        }])
        self.assertEqual(self.states(), ["answered", "answered", "pending"])
        self.assertGreater(reply_inbox.load(self.inbox)["activity_at"], 1000.0)

    def test_an_empty_answer_or_a_missing_key_fails_and_posts_nothing(self) -> None:
        self.add(comment(300, "default", "Linear reply: one"))
        self.hook("post-tool-use")

        empty = self.reply("  \n")
        keyless = self.reply("Answer.", token=None)

        for result in (empty, keyless):
            self.assertEqual(result.returncode, 1)
            self.assertIn("session-reply:", result.stderr)
        self.assertEqual(self.activities(), [])
        self.assertEqual(self.states(), ["delivered"])

    def test_a_linear_that_rejects_the_key_fails_and_leaves_the_reply_unanswered(self) -> None:
        self.add(comment(300, "default", "Linear reply: one"))
        self.hook("post-tool-use")

        result = self.reply("Answer.", token="wrong-token")

        self.assertEqual(result.returncode, 1)
        self.assertIn("posting to Linear failed", result.stderr)
        self.assertEqual(self.states(), ["delivered"])


if __name__ == "__main__":
    unittest.main()
