from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "webhook_retry", os.path.join(PLUGIN_DIR, "__init__.py"), submodule_search_locations=[PLUGIN_DIR]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def chat_key(delivery: str, route: str = "linear", profile: str = "default") -> str:
    body = base64.urlsafe_b64encode(json.dumps([profile, route, delivery]).encode()).decode().rstrip("=")
    return f"webhook:v2:{body}"
