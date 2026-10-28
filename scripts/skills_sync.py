"""Keep the vendored skills and their bundled agents equal to the latest stable release of the skills repository.

`scripts/skills-sync` runs `main`. The sync reads `.agents/skills-sync.json` for the source repository and the
skills to vendor, selects a release, reconciles a checkout of this repository to it, and either reports the
result (`--dry-run`) or publishes it as one signed commit on `bot/bump-skills` with a pull request.
Every GitHub call goes through the `GitHub` class, which tests replace.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BRANCH = "bot/bump-skills"
CONFIG = ".agents/skills-sync.json"
LOCK = "skills-lock.json"
AGENTS_DIR = ".claude/agents"
BUNDLED_AGENTS = ".agents/skills/setup-code-skills/assets/agents"
MACHINERY = ".agents/skills/setup-code-skills/scripts/install-machinery.sh"
STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
CHECKS = (("pnpm test", ["pnpm", "test"]), ("pnpm lint:md", ["pnpm", "lint:md"]))

# The skills CLI hashes with JavaScript's localeCompare ordering, which Python cannot reproduce, so node computes it.
HASH_PROGRAM = """
import {createHash} from 'node:crypto';
import {readdirSync, readFileSync} from 'node:fs';
import {join, relative} from 'node:path';
function walk(base, dir, out) {
  for (const entry of readdirSync(dir, {withFileTypes: true})) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== '.git' && entry.name !== 'node_modules') walk(base, path, out);
    } else if (entry.isFile()) out.push({name: relative(base, path), bytes: readFileSync(path)});
  }
}
const hashes = {};
for (const dir of process.argv.slice(1)) {
  const files = [];
  walk(dir, dir, files);
  files.sort((a, b) => a.name.localeCompare(b.name));
  const hash = createHash('sha256');
  for (const file of files) { hash.update(file.name); hash.update(file.bytes); }
  hashes[dir] = hash.digest('hex');
}
console.log(JSON.stringify(hashes));
"""


class SyncError(Exception):
    """A refusal or a failure. The message is the one-line reason."""


@dataclass(frozen=True)
class Release:
    tag: str
    commit: str
    url: str


class GitHub:
    """The only place that calls GitHub."""

    def __init__(self, token: str | None) -> None:
        self.token = token

    def call(self, method: str, path: str, body: Any = None, missing_ok: bool = False) -> Any:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"https://api.github.com/{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise SyncError(f"GitHub {method} {path} returned {error.code}: {error.read()[:300].decode(errors='replace')}")
        except urllib.error.URLError as error:
            raise SyncError(f"GitHub {method} {path} failed: {error.reason}")
        return json.loads(payload) if payload else None

    def releases(self, repo: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for page in range(1, 11):
            batch = self.call("GET", f"repos/{repo}/releases?per_page=100&page={page}")
            found += batch
            if len(batch) < 100:
                break
        return found

    def tag_commit(self, repo: str, tag: str) -> str:
        obj = self.call("GET", f"repos/{repo}/git/ref/tags/{urllib.parse.quote(tag)}")["object"]
        while obj["type"] == "tag":
            obj = self.call("GET", f"repos/{repo}/git/tags/{obj['sha']}")["object"]
        if obj["type"] != "commit":
            raise SyncError(f"tag {tag} of {repo} points at a {obj['type']}, not a commit")
        return obj["sha"]

    def default_branch(self, repo: str) -> str:
        return self.call("GET", f"repos/{repo}")["default_branch"]

    def branch_head(self, repo: str, branch: str) -> str | None:
        ref = self.call("GET", f"repos/{repo}/git/ref/heads/{branch}", missing_ok=True)
        return ref["object"]["sha"] if ref else None

    def commit_info(self, repo: str, sha: str) -> dict[str, Any]:
        commit = self.call("GET", f"repos/{repo}/git/commits/{sha}")
        return {"tree": commit["tree"]["sha"], "parents": [parent["sha"] for parent in commit["parents"]]}

    def create_tree(self, repo: str, base_commit: str, entries: list[dict[str, Any]]) -> str:
        """Entries are `{"path", "mode", "content"}`, with `content` None to delete the path."""
        tree = []
        for entry in entries:
            sha = None
            if entry["content"] is not None:
                blob = {"content": base64.b64encode(entry["content"]).decode(), "encoding": "base64"}
                sha = self.call("POST", f"repos/{repo}/git/blobs", blob)["sha"]
            tree.append({"path": entry["path"], "mode": entry["mode"], "type": "blob", "sha": sha})
        base_tree = self.commit_info(repo, base_commit)["tree"]
        return self.call("POST", f"repos/{repo}/git/trees", {"base_tree": base_tree, "tree": tree})["sha"]

    def create_commit(self, repo: str, tree: str, parent: str, message: str) -> dict[str, Any]:
        commit = self.call("POST", f"repos/{repo}/git/commits", {"message": message, "tree": tree, "parents": [parent]})
        return {"sha": commit["sha"], "verified": commit["verification"]["verified"],
                "reason": commit["verification"]["reason"]}

    def set_branch(self, repo: str, branch: str, sha: str, observed: str | None, checkout: Path) -> None:
        """Move the branch to `sha` only while it still points at `observed`, or does not exist when that is None."""
        # The REST refs API has no expected-head check, so a git push with a lease moves the branch atomically.
        credential = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        env = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
               "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}"}
        url = f"https://github.com/{repo}.git"
        run(["git", "fetch", "-q", url, sha], checkout, env=env)
        run(["git", "push", "-q", f"--force-with-lease=refs/heads/{branch}:{observed or ''}", url,
             f"{sha}:refs/heads/{branch}"], checkout, env=env)

    def open_pull(self, repo: str, branch: str) -> dict[str, Any] | None:
        owner = repo.split("/")[0]
        pulls = self.call("GET", f"repos/{repo}/pulls?state=open&head={owner}:{urllib.parse.quote(branch, safe='')}")
        return {"number": pulls[0]["number"], "url": pulls[0]["html_url"]} if pulls else None

    def create_pull(self, repo: str, title: str, body: str, head: str, base: str) -> dict[str, Any]:
        pull = self.call("POST", f"repos/{repo}/pulls", {"title": title, "body": body, "head": head, "base": base})
        return {"number": pull["number"], "url": pull["html_url"]}

    def update_pull(self, repo: str, number: int, title: str, body: str) -> None:
        self.call("PATCH", f"repos/{repo}/pulls/{number}", {"title": title, "body": body})

    def close_pull(self, repo: str, number: int, comment: str) -> None:
        self.call("POST", f"repos/{repo}/issues/{number}/comments", {"body": comment})
        self.call("PATCH", f"repos/{repo}/pulls/{number}", {"state": "closed"})


def run(command: list[str], cwd: Path | str, binary: bool = False, check: bool = True,
        env: dict[str, str] | None = None) -> Any:
    try:
        proc = subprocess.run(command, cwd=cwd, capture_output=True, text=not binary, timeout=1800, env=env)
    except OSError as error:
        raise SyncError(f"cannot run {command[0]}: {error}")
    if check and proc.returncode != 0:
        error = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        raise SyncError(f"{' '.join(command[:3])} failed: {error.strip()[-300:]}")
    return proc


def git(cwd: Path | str, *args: str, binary: bool = False) -> Any:
    out = run(["git", *args], cwd, binary=binary).stdout
    return out if binary else out.strip()


def load_config(target: Path) -> dict[str, Any]:
    path = target / CONFIG
    if not path.is_file():
        raise SyncError(f"{CONFIG} is missing from {target}")
    config = json.loads(path.read_text())
    if not (isinstance(config.get("source"), str) and isinstance(config.get("plugins"), list)
            and isinstance(config.get("skills"), list)):
        raise SyncError(f"{CONFIG} needs a source string and plugins and skills lists")
    return config


def version(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in STABLE_TAG.match(tag).groups())


def select_release(releases: list[dict[str, Any]], source: str, tag: str | None) -> dict[str, Any] | None:
    """The release to vendor, or None when the source has no published stable release."""
    if tag:
        named = [release for release in releases if release["tag_name"] == tag]
        if not named:
            raise SyncError(f"{tag} is not a release of {source}")
        if named[0]["draft"]:
            raise SyncError(f"{tag} is a draft release of {source}")
        if named[0]["prerelease"]:
            raise SyncError(f"{tag} is a prerelease of {source}")
        if not STABLE_TAG.match(tag):
            raise SyncError(f"{tag} is not a stable vX.Y.Z tag")
        return named[0]
    stable = [r for r in releases if not r["draft"] and not r["prerelease"] and STABLE_TAG.match(r["tag_name"])]
    return max(stable, key=lambda release: version(release["tag_name"])) if stable else None


def skipped_releases(releases: list[dict[str, Any]]) -> str:
    kinds = [f"{r['tag_name']} {'draft' if r['draft'] else 'prerelease'}" for r in releases
             if r["draft"] or r["prerelease"]]
    return ", ".join(kinds) or "none"


def refuse_regression(config: dict[str, Any], release: Release) -> None:
    vendored = config.get("release")
    if not vendored:
        return
    if version(release.tag) < version(vendored["tag"]):
        raise SyncError(f"{release.tag} is older than the vendored {vendored['tag']}")
    if release.tag == vendored["tag"] and release.commit != vendored["commit"]:
        raise SyncError(f"tag {release.tag} now points at {release.commit}, not the vendored {vendored['commit']}")


def selected_skills(checkout: Path, config: dict[str, Any]) -> dict[str, str]:
    """Skill name to its path in the source repository."""
    found: dict[str, str] = {}

    def add(name: str, path: str) -> None:
        if found.setdefault(name, path) != path:
            raise SyncError(f"skill {name} is in both {found[name]} and {path}")

    for plugin in config["plugins"]:
        skills = sorted((checkout / "plugins" / plugin / "skills").glob("*/SKILL.md"))
        if not skills:
            raise SyncError(f"plugin {plugin} has no skills in the release")
        for skill in skills:
            add(skill.parent.name, f"plugins/{plugin}/skills/{skill.parent.name}/SKILL.md")
    for name in config["skills"]:
        paths = sorted(checkout.glob(f"plugins/*/skills/{name}/SKILL.md"))
        if not paths:
            raise SyncError(f"skill {name} is not in the release")
        for path in paths:
            add(name, str(path.relative_to(checkout)))
    if not found:
        raise SyncError(f"{CONFIG} selects no skills")
    return found


def computed_hashes(dirs: list[Path]) -> dict[str, str]:
    proc = run(["node", "--input-type=module", "-e", HASH_PROGRAM, *map(str, dirs)], dirs[0].parent)
    return json.loads(proc.stdout)


def fetch(source_url: str, commit: str, into: Path) -> None:
    git(into, "init", "-q")
    git(into, "fetch", "-q", "--depth=1", source_url, commit)
    git(into, "checkout", "-q", "--detach", "FETCH_HEAD")


def drop_skill(target: Path, name: str) -> None:
    shutil.rmtree(target / ".agents" / "skills" / name, ignore_errors=True)
    (target / ".claude" / "skills" / name).unlink(missing_ok=True)


def vendor_skills(target: Path, checkout: Path, config: dict[str, Any], commit: str) -> None:
    lock = json.loads((target / LOCK).read_text())
    skills = lock["skills"]
    wanted = selected_skills(checkout, config)
    for name in sorted(n for n, e in skills.items() if e.get("source") == config["source"] and n not in wanted):
        drop_skill(target, name)
        del skills[name]
    for name, path in wanted.items():
        if name in skills and skills[name].get("source") != config["source"]:
            raise SyncError(f"skill {name} is locked from {skills[name].get('source')}, not {config['source']}")
        destination = target / ".agents" / "skills" / name
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(checkout / Path(path).parent, destination, symlinks=True)
        link = target / ".claude" / "skills" / name
        want = f"../../.agents/skills/{name}"
        if link.is_symlink() and os.readlink(link) != want:
            link.unlink()
        if not link.is_symlink():
            if link.exists():
                raise SyncError(f".claude/skills/{name} exists and is not a symlink")
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(want)
    hashes = computed_hashes([target / ".agents" / "skills" / name for name in wanted])
    for name, path in wanted.items():
        digest = hashes[str(target / ".agents" / "skills" / name)]
        skills[name] = {"source": config["source"], "sourceType": "github", "ref": commit, "skillPath": path,
                        "computedHash": digest}
    lock["skills"] = dict(sorted(skills.items()))
    (target / LOCK).write_text(json.dumps(lock, indent=2) + "\n")


def bundled_agents(root: Path) -> set[str]:
    return {path.name for path in (root / BUNDLED_AGENTS).glob("*.md")}


def agent_files(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in (root / AGENTS_DIR).glob("*.md")}


def lock_hashes(root: Path) -> dict[str, str]:
    return {name: entry["computedHash"] for name, entry in json.loads((root / LOCK).read_text())["skills"].items()}


def install_agents(target: Path, dropped: set[str]) -> None:
    machinery = target / MACHINERY
    if not machinery.is_file():
        raise SyncError(f"the release vendors no {MACHINERY}, so its agents cannot be installed")
    run(["bash", str(machinery), "--repo", str(target)], target)
    for name in dropped - bundled_agents(target):
        (target / AGENTS_DIR / name).unlink(missing_ok=True)


def delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    def split(old: dict[str, Any], new: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
        return (sorted(new.keys() - old.keys()), sorted(n for n in old.keys() & new.keys() if old[n] != new[n]),
                sorted(old.keys() - new.keys()))

    result = {}
    for kind, (added, changed, removed) in (("skills", split(before["skills"], after["skills"])),
                                            ("agents", split(before["agents"], after["agents"]))):
        result[f"{kind} added"], result[f"{kind} changed"], result[f"{kind} removed"] = added, changed, removed
    return result


def run_checks(target: Path) -> list[tuple[str, bool]]:
    return [(label, run(command, target, check=False).returncode == 0) for label, command in CHECKS]


def other_changes(target: Path) -> list[str]:
    """Changed paths outside the skills, the lock, the agents, and the config, such as the installer's hooks."""
    managed = (".agents/skills/", ".claude/skills/", f"{AGENTS_DIR}/", LOCK, CONFIG)
    paths = git(target, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames", binary=True)
    return sorted(path for path in (entry[3:] for entry in paths.decode().split("\0") if entry)
                  if not path.startswith(managed))


def render_report(release: Release, changes: dict[str, list[str]], checks: list[tuple[str, bool]] | None) -> str:
    lines = [f"Release [{release.tag}]({release.url}) of the skills repository.", "",
             f"- Tag: `{release.tag}`", f"- Commit: `{release.commit}`", ""]
    for heading, names in changes.items():
        lines += [f"### {heading.capitalize()}", ""] + ([f"- `{name}`" for name in names] or ["none"]) + [""]
    lines += ["### Checks", ""]
    if checks is None:
        lines.append("not run")
    else:
        lines += [f"- `{label}`: {'pass' if passed else 'fail'}" for label, passed in checks]
    return "\n".join(lines) + "\n"


def staged_changes(target: Path) -> list[dict[str, Any]]:
    """The reconciled tree against HEAD as API tree entries."""
    git(target, "add", "-A")
    tokens = git(target, "diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev", "HEAD",
                 binary=True).decode().split("\0")[:-1]
    entries = []
    for meta, path in zip(tokens[0::2], tokens[1::2]):
        old_mode, new_mode, _, sha, status = meta.split()
        if status == "D":
            # GitHub refuses a removal whose mode differs from the removed blob's, such as a skill symlink.
            entries.append({"path": path, "mode": old_mode.lstrip(":"), "content": None})
        else:
            entries.append({"path": path, "mode": new_mode, "content": git(target, "cat-file", "blob", sha, binary=True)})
    return entries


def publish(gh: GitHub, repo: str, target: Path, release: Release, body: str, observed: str | None) -> str:
    title = f"chore: sync skills {release.tag}"
    parent = git(target, "rev-parse", "HEAD")
    base = gh.default_branch(repo)
    if gh.branch_head(repo, base) != parent:
        raise SyncError(f"the checkout is at {parent}, not the head of {base}")
    tree = gh.create_tree(repo, parent, staged_changes(target))
    pull = gh.open_pull(repo, BRANCH)
    head = gh.branch_head(repo, BRANCH)
    if head != observed:
        raise SyncError(f"{BRANCH} moved from {observed} to {head} during the run")
    current = head is not None and gh.commit_info(repo, head) == {"tree": tree, "parents": [parent]}
    if not current:
        commit = gh.create_commit(repo, tree, parent, f"{title}\n\nRelease: {release.url}")
        if not commit["verified"]:
            raise SyncError(f"the sync commit is not verified ({commit['reason']}); is the token a GitHub App's?")
        gh.set_branch(repo, BRANCH, commit["sha"], head, target)
    if pull:
        gh.update_pull(repo, pull["number"], title, body)
        return pull["url"]
    return gh.create_pull(repo, title, body, BRANCH, base)["url"]


def sync(args: argparse.Namespace, gh: GitHub) -> int:
    target = Path(args.target).resolve()
    config = load_config(target)
    if git(target, "status", "--porcelain"):
        raise SyncError(f"{target} has uncommitted changes")
    repo = args.repo
    if not args.dry_run and not repo:
        raise SyncError("--repo or GITHUB_REPOSITORY names the repository to publish to")
    observed = None if args.dry_run else gh.branch_head(repo, BRANCH)
    source = config["source"]
    releases = gh.releases(source)
    chosen = select_release(releases, source, args.tag)
    if chosen is None:
        print(f"no change: {source} has no published stable release (ignored: {skipped_releases(releases)})")
        return 0
    release = Release(chosen["tag_name"], gh.tag_commit(source, chosen["tag_name"]), chosen["html_url"])
    refuse_regression(config, release)

    before = {"skills": lock_hashes(target), "agents": agent_files(target)}
    previous_agents = bundled_agents(target)
    with tempfile.TemporaryDirectory() as scratch:
        fetch(args.source_url or f"https://github.com/{source}.git", release.commit, Path(scratch))
        vendor_skills(target, Path(scratch), config, release.commit)
    install_agents(target, previous_agents)
    config["release"] = {"tag": release.tag, "commit": release.commit, "url": release.url}
    (target / CONFIG).write_text(json.dumps(config, indent=2) + "\n")
    changes = delta(before, {"skills": lock_hashes(target), "agents": agent_files(target)})
    changes["other files changed"] = other_changes(target)

    if not git(target, "status", "--porcelain", "--untracked-files=all"):
        print(f"no change: the vendored skills and agents already equal {release.tag}")
        pull = None if args.dry_run else gh.open_pull(repo, BRANCH)
        if pull:
            gh.close_pull(repo, pull["number"], f"Closed as obsolete: the default branch already equals {release.tag}.")
            print(f"closed the obsolete pull request {pull['url']}")
        return 0

    checks = None if args.no_checks else run_checks(target)
    report = render_report(release, changes, checks)
    if args.dry_run:
        print(f"would publish {release.tag}\n\n{report}", end="")
    else:
        url = publish(gh, repo, target, release, report, observed)
        print(f"published {release.tag}: {url}\n\n{report}", end="")
    failed = [label for label, passed in checks or [] if not passed]
    if failed:
        print(f"skills-sync: {', '.join(failed)} failed on {release.tag}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None, github: GitHub | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skills-sync", description=__doc__.splitlines()[0])
    parser.add_argument("--target", default=".", help="the checkout to reconcile")
    parser.add_argument("--dry-run", action="store_true", help="reconcile and report without publishing")
    parser.add_argument("--tag", help="vendor this published stable release instead of the greatest")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="the repository to publish to")
    parser.add_argument("--source-url", default=os.environ.get("SKILLS_SYNC_SOURCE_URL"),
                        help="git URL of the source repository, for tests")
    parser.add_argument("--no-checks", action="store_true", help="skip pnpm test and pnpm lint:md")
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    try:
        if github is None and not (token or args.dry_run):
            raise SyncError("GH_TOKEN or GITHUB_TOKEN is not set")
        return sync(args, github or GitHub(token))
    except SyncError as error:
        print(f"skills-sync: {error}", file=sys.stderr)
        return 1
