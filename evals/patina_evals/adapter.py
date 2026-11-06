"""Thin factory adapter: builds a trial's worker configuration with the worker's own code.

It loads `profiles/claude-worker/scripts/claude-session` and calls the same
functions the worker calls to build its prompt, settings, hooks, and session
plan. `construction("worker")` feeds them the committed profile's values;
`construction("trial")` feeds them the trial's mock services. Parity compares
the two. Values that name machine paths are normalized so both sides compare.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPO_ROOT / "profiles" / "claude-worker"
MOCK_TRACKER_SERVER = {"type": "stdio", "command": "python3", "args": ["<trial>/services/mock_tracker.py"]}
MOCK_API_KEY = "mock-linear-key"
MODES = ("worker", "trial")


class Unknown:
    """A dimension value the adapter could not read. It never equals anything, so parity marks it invalid."""

    def __init__(self, reason: str):
        self.reason = reason

    def __repr__(self) -> str:
        return f"<unknown: {self.reason}>"


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_script(path: Path, name: str):
    scripts = str(path.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def load_plugin_module(repo: Path, plugin: str, name: str):
    spec = importlib.util.spec_from_file_location(f"{plugin.replace('-', '_')}_{name}", repo / "plugins" / plugin / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def normalize(value, replacements: list[tuple[str, str]]):
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, dict):
        return {k: normalize(v, replacements) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v, replacements) for v in value]
    return value


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return yaml.safe_load(match.group(1)) if match else {}


def resume_contract(session, home: Path) -> dict:
    """Run the worker's session planner on fixed transcripts and report what it decides."""
    worktree = home / "worktree"
    worktree.mkdir()
    key = session.session_key("issue-under-test", False)
    first = session.plan_session(key, worktree, 100_000, time.time())
    transcript = home / ".claude" / "projects" / "p" / f"{first.session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"cwd": str(worktree)}) + "\n", encoding="utf-8")
    warm = session.plan_session(key, worktree, 100_000, time.time())
    old = time.time() - session.CACHE_TTL_SECONDS - 60
    os.utime(transcript, (old, old))
    idle = session.plan_session(key, worktree, 100_000, time.time())
    return {
        "resume_prompt": session.RESUME_PROMPT,
        "cache_ttl_seconds": session.CACHE_TTL_SECONDS,
        "session_id_is_uuid5_of_key": first.session_id == str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
        "no_transcript": {"resumed": first.resumed, "reason": first.reason},
        "warm_transcript": {"resumed": warm.resumed, "reason": warm.reason, "same_session": warm.session_id == first.session_id},
        "idle_unreadable_transcript": {"resumed": idle.resumed, "reason": idle.reason},
    }


def dimension(source: list[str], read):
    try:
        return {"source": source, "value": read()}
    except Exception as error:
        return {"source": source, "value": Unknown(f"{type(error).__name__}: {error}")}


def construction(mode: str, repo: Path = REPO_ROOT, blocked_url: str = "http://127.0.0.1:9") -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    profile = repo / "profiles" / "claude-worker"
    scripts = profile / "scripts"
    with tempfile.TemporaryDirectory() as scratch:
        home = Path(scratch)
        saved = {k: os.environ.get(k) for k in ("HERMES_HOME", "CLAUDE_CONFIG_DIR")}
        os.environ["HERMES_HOME"] = str(home / "hermes")
        os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        try:
            return build(mode, repo, profile, scripts, home, blocked_url)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def build(mode: str, repo: Path, profile: Path, scripts: Path, home: Path, blocked_url: str) -> dict:
    replacements = [(str(scripts.resolve()), "<scripts>"), (str(home), "<home>"), (sys.executable, "<python>")]
    config = dimension(["profiles/claude-worker/config.yaml"], lambda: yaml.safe_load((profile / "config.yaml").read_text()))
    session = load_script(scripts / "claude-session", f"claude_session_{uuid.uuid4().hex}")
    factory_settings = sys.modules["factory_settings"]
    result_line = load_plugin_module(repo, "blocker-order", "result_line")
    plugin_transport = lambda: config["value"]["plugins"]["entries"]["linear-agent-session"]["settings"]["claude_session"]
    if mode == "worker":
        transport = lambda: dict(plugin_transport())
        api_key, servers = "<linear api key>", lambda: plugin_transport().get("mcp_servers") or {}
    else:
        transport = lambda: dict(plugin_transport(), base_url=blocked_url)
        api_key, servers = MOCK_API_KEY, lambda: {}

    def settings():
        built = session.claude_settings(transport(), api_key, home / "settings.json")
        built["env"]["LINEAR_API_KEY"] = "<redacted>"
        return normalize(built, replacements)

    def mcp():
        built = json.loads(session.mcp_config(servers()))
        if mode == "trial":
            built["mcpServers"]["linear"] = MOCK_TRACKER_SERVER
        return built

    def agents():
        return {
            p.name: {"sha": digest(p.read_text(encoding="utf-8")), "effort": frontmatter(p).get("effort")}
            for p in sorted((repo / ".claude" / "agents").glob("*.md"))
        }

    def effort():
        source = (scripts / "claude-session").read_text(encoding="utf-8")
        return {
            "worker_flag": "--effort" in source,
            "worker_env": "CLAUDE_CODE_EFFORT" in source or "MAX_THINKING_TOKENS" in source,
            "agent_frontmatter": {name: info["effort"] for name, info in agents().items()},
        }

    def role_models():
        t = transport()
        structure = {"model": "<set>" if t.get("model") else "<placeholder>", "tier_models": sorted(t.get("tier_models") or {})}
        return {"committed": structure, "tiers": list(factory_settings.MODEL_TIERS)}

    def instructions():
        names = ["AGENTS.md", "CLAUDE.md"]
        return {
            "SOUL.md": digest((profile / "SOUL.md").read_text(encoding="utf-8")),
            **{n: digest((repo / n).read_text(encoding="utf-8")) for n in names},
        }

    with tempfile.TemporaryDirectory() as contract_home:
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(contract_home) / ".claude")
        resume = dimension(["profiles/claude-worker/scripts/claude-session"], lambda: resume_contract(session, Path(contract_home)))

    return {
        "worker_prompt": dimension(["profiles/claude-worker/scripts/claude-session", "plugins/blocker-order/result_line.py"], lambda: {
            "implementation": digest(session.implementation_prompt(result_line)),
            "triage": digest(session.TRIAGE_SYSTEM_PROMPT),
        }),
        "project_instructions": dimension(["profiles/claude-worker/SOUL.md", "AGENTS.md", "CLAUDE.md"], instructions),
        "tool_search": dimension(["profiles/claude-worker/config.yaml", "profiles/claude-worker/scripts/claude-session"], lambda: {
            "hermes_tool_search": config["value"]["tools"]["tool_search"]["enabled"],
            "claude_env": settings()["env"].get("ENABLE_TOOL_SEARCH"),
        }),
        "agents_and_hooks": dimension(["profiles/claude-worker/scripts/claude-session", ".claude/agents"], lambda: {
            "hooks": settings()["hooks"], "permissions": settings()["permissions"], "mcp": mcp(), "agents": agents(),
        }),
        "skill_and_dependency_pins": dimension(
            ["profiles/claude-worker/config.yaml", "profiles/claude-worker/distribution.yaml", "skills-lock.json"],
            lambda: {
                "auto_load_skills": config["value"]["skills"]["auto_load"],
                "plugins": config["value"]["plugins"]["enabled"],
                "hermes_requires": yaml.safe_load((profile / "distribution.yaml").read_text())["hermes_requires"],
                "skills_lock": digest((repo / "skills-lock.json").read_text(encoding="utf-8")),
            },
        ),
        "role_models": dimension(["profiles/claude-worker/config.yaml", "profiles/claude-worker/scripts/factory_settings.py"], role_models),
        "effort": dimension(["profiles/claude-worker/scripts/claude-session", ".claude/agents"], effort),
        "resume": resume,
        "environment": dimension(["profiles/claude-worker/scripts/claude-session"], lambda: {
            k: v for k, v in settings()["env"].items() if k != "ENABLE_TOOL_SEARCH"
        }),
    }
