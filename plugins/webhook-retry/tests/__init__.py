from __future__ import annotations

import importlib.util
import json
import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)

import queue_store  # noqa: E402


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "webhook_retry", os.path.join(PLUGIN_DIR, "__init__.py"), submodule_search_locations=[PLUGIN_DIR]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def chat_key(delivery: str, route: str = "linear", profile: str = "default") -> str:
    return queue_store.encode_key(profile, route, delivery)


SKILL_PREFIX = '[IMPORTANT: The user has invoked the "factory-linear-triage" skill, indicating they want you to follow its instructions. '
SKILL_MARKER = "The user has provided the following instruction alongside the skill invocation: "


def skill_turn(instruction: str, body: str = "static triage rules\n" * 400) -> str:
    """A turn the way Hermes builds it from a route skill: the cached scaffold, then the per-event instruction."""
    return f"{SKILL_PREFIX}The full skill content is loaded below.]\n\n{body}\n[Skill directory: /x]\n{SKILL_MARKER}{instruction}"


def skill_commands_module():
    """Stands in for Hermes' `agent.skill_commands`, whose extractor cuts after the last instruction marker."""
    import types

    def extract(content):
        if not isinstance(content, str) or not content.startswith("[IMPORTANT: The user has invoked the "):
            return content
        return content.rsplit(SKILL_MARKER, 1)[1].split("\n\n[Runtime note:", 1)[0]

    module = types.ModuleType("agent.skill_commands")
    module.extract_user_instruction_from_skill_message = extract
    return {"agent": types.ModuleType("agent"), "agent.skill_commands": module}
