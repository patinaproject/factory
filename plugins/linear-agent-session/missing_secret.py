from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

# Hermes requeues a Kanban worker that fails with a transient reason without counting a failure,
# and parks a 401 or 403 as a terminal provider error. Hermes still retries a non-retryable rate_limit in the
# turn, so timeout is the transient reason that ends it after the one refused request.
TRANSIENT_REASON = "timeout"
REJECTED_STATUSES = (401, 403)


def provider_key_env(config: Mapping[str, Any]) -> Optional[str]:
    model = config.get("model") or {}
    provider = str(model.get("provider") or "")
    entry: Mapping[str, Any] = model
    if provider.startswith("custom:"):
        entry = (config.get("providers") or {}).get(provider.removeprefix("custom:")) or {}
    name = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    return name or None


def worker_empty_key_env(config: Mapping[str, Any], environ: Mapping[str, str]) -> Optional[str]:
    if not environ.get("HERMES_KANBAN_TASK", "").strip():
        return None
    name = provider_key_env(config)
    if name is None or environ.get(name, "").strip():
        return None
    return name


def make_classification_hook(
    load_config: Callable[[], Mapping[str, Any]], environ: Mapping[str, str]
) -> Callable[..., Optional[dict]]:
    def classify(status_code: Any = None, **_: Any) -> Optional[dict]:
        if status_code not in REJECTED_STATUSES:
            return None
        name = worker_empty_key_env(load_config(), environ)
        if name is None:
            return None
        return {
            "reason": TRANSIENT_REASON,
            "retryable": False,
            "should_fallback": False,
            "should_rotate_credential": False,
            "message": f"{name} was empty when this worker started; the card goes back to the queue",
        }

    return classify
