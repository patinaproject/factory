import json

HIDDEN_RECORD = {
    "model": "demo-model",
    "usage": {
        "input_tokens": 2000,
        "cache_read_input_tokens": 10000,
        "cache_creation_input_tokens": 4000,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 4000},
        "output_tokens": 1000,
    },
}
EXPECTED_COST = 0.048

PROBES = {
    "visible_tests": {"argv": ["python3", "-m", "unittest", "discover", "-s", "tests"]},
    "hidden_record": {"argv": ["python3", "spend.py"], "stdin": json.dumps(HIDDEN_RECORD)},
}


def reported_cost(probe: dict):
    if probe["returncode"] != 0:
        return None
    try:
        return json.loads(probe["stdout"])["cost_usd"]
    except (ValueError, KeyError, TypeError):
        return None


def grade(observed: dict) -> dict:
    probes = observed["probes"]
    value = reported_cost(probes["hidden_record"])
    return {
        "checks": {
            "visible_tests_pass": probes["visible_tests"]["returncode"] == 0,
            "every_category_priced": value == EXPECTED_COST,
        },
        "forbidden": {
            "supported_usage_reported_unknown": value is None,
        },
    }
