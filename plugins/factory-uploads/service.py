"""It runs on the macOS system Python, 3.9, so it uses nothing newer."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factory_uploads.broker import Broker  # noqa: E402
from factory_uploads.credentials import UPLOAD_TOKEN, GatewayCredentials  # noqa: E402

logger = logging.getLogger("factory_uploads.service")

LINEAR_MODULE = Path(__file__).resolve().parent.parent / "linear.py"
IDENTITY = ("BROKER_INFISICAL_CLIENT_ID", "BROKER_INFISICAL_CLIENT_SECRET")
INFISICAL_TIMEOUT = 15
INFISICAL_ATTEMPTS = 3
INFISICAL_PAUSE = 2


class ConfigError(Exception):
    pass


def load_config(path: Path) -> dict:
    config = json.loads(Path(path).read_text())
    for key, kind in (("socket", str), ("home", str), ("allowed_uids", list), ("repositories", dict)):
        if not isinstance(config.get(key), kind):
            raise ConfigError(f"{path}: {key} must be a {kind.__name__}")
    if not config["allowed_uids"] or not all(isinstance(uid, int) and uid > 0 for uid in config["allowed_uids"]):
        raise ConfigError(f"{path}: allowed_uids must list positive uids")
    if not all(isinstance(rid, int) for rid in config["repositories"].values()):
        raise ConfigError(f"{path}: each repository maps to its numeric GitHub id")
    return config


def read_env_file(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name] = value
    return values


def _infisical_login(api: str, client_id: str, client_secret: str) -> str:
    login = urllib.request.Request(
        f"{api}/api/v1/auth/universal-auth/login",
        data=json.dumps({"clientId": client_id, "clientSecret": client_secret}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(login, timeout=INFISICAL_TIMEOUT) as response:
        return json.loads(response.read())["accessToken"]


def _infisical_folder(api: str, access_token: str, settings: dict, folder: str) -> dict:
    """The folder's own keys win over its imports, and a later import wins over an earlier one, as `infisical export` does."""
    query = urllib.parse.urlencode({
        "projectId": settings["project_id"], "environment": settings["environment"], "secretPath": folder,
        "includeImports": "true", "recursive": "false", "expandSecretReferences": "true", "includePersonalOverrides": "true",
    })
    request = urllib.request.Request(f"{api}/api/v4/secrets?{query}", headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(request, timeout=INFISICAL_TIMEOUT) as response:
        document = json.loads(response.read())
    values = {secret["secretKey"]: secret["secretValue"] for secret in document["secrets"]}
    for imported in reversed(document.get("imports") or []):
        for secret in imported["secrets"]:
            values.setdefault(secret["secretKey"], secret["secretValue"])
    return values


def _infisical_fetch(api: str, settings: dict, client_id: str, client_secret: str) -> dict:
    access_token = _infisical_login(api, client_id, client_secret)
    values = {}
    for folder in settings["paths"]:
        values.update(_infisical_folder(api, access_token, settings, folder))
    return values


def infisical_secrets(settings: dict, client_id: str, client_secret: str) -> dict:
    api = settings["api_url"].rstrip("/")
    for attempt in range(1, INFISICAL_ATTEMPTS):
        try:
            return _infisical_fetch(api, settings, client_id, client_secret)
        except OSError as error:
            logger.warning("factory-broker: Infisical attempt %d of %d failed: %s", attempt, INFISICAL_ATTEMPTS, type(error).__name__)
            time.sleep(INFISICAL_PAUSE)
    return _infisical_fetch(api, settings, client_id, client_secret)


def load_secrets(config: dict) -> dict:
    env_path = Path(config["home"]) / ".env"
    local = read_env_file(env_path) if env_path.exists() else {}
    settings = config.get("infisical")
    if not settings:
        return local
    identity = [local.pop(name, None) for name in IDENTITY]
    if not all(identity):
        raise ConfigError(f"{env_path} lacks {' and '.join(IDENTITY)}")
    return {**infisical_secrets(settings, *identity), **local}


def make_broker(config: dict, secrets: dict) -> Broker:
    home = Path(config["home"])
    ids = dict(config["repositories"])
    return Broker(
        home,
        list(ids),
        lambda: secrets.get(UPLOAD_TOKEN),
        resolve_repository_id=ids.__getitem__,
        credentials=GatewayCredentials(secrets.get, config.get("claude_session") or {}, LINEAR_MODULE, home),
        socket_path=Path(config["socket"]),
        state_dir=home / "state",
        socket_mode=0o660,
        allowed_uids=frozenset(config["allowed_uids"]),
    )


async def serve(broker: Broker, stopping: asyncio.Event | None = None) -> int:
    if not await broker.start():
        logger.error("factory-broker: %s already answers; another broker is running", broker.socket_path)
        return 1
    if stopping is None:
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopping.set)
    logger.info("factory-broker: listening on %s", broker.socket_path)
    await stopping.wait()
    await broker.stop()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the factory's broker as its own service.")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        secrets = load_secrets(config)
    except KeyError as error:
        logger.error("factory-broker: cannot start: a reply or the config lacks %s", error)
        return 1
    except (OSError, ValueError, ConfigError) as error:
        logger.error("factory-broker: cannot start: %s", error)
        return 1
    return asyncio.run(serve(make_broker(config, secrets)))


if __name__ == "__main__":
    sys.exit(main())
