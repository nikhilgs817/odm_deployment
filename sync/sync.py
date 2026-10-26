#!/usr/bin/env python3
"""Capture Decision Center rule exports in Git without changing the rules.

Jenkins supplies DEPLOYMENT_TRACK, optional APP, and credentials through the
environment. The selected installation's API contracts must be confirmed in
configuration before preflight or run. Run from the repository root with python -m sync.sync.
This entry point never deploys a JAR.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterator, Mapping
from urllib.parse import quote, urlsplit, urlunsplit

from odm_common import (
    HttpClient, OdmError, atomic_json, inspect_zip, load_config, resolve,
    read_json, sha256_bytes,
)


_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_MAX_EXPORT = 256 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise OdmError(f"The {what} is invalid.")
    return value


def _required_string(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise OdmError(f"The {what} is missing or invalid.")
    if re.search(r"<[^>]+>|(?:REPLACE_ME|CHANGE_ME|CHANGEME)(?:_|$)|\bTODO\b", value, re.I):
        raise OdmError(f"The {what} still contains a placeholder.")
    return value


def _relative_path(value: Any, what: str) -> PurePosixPath:
    text = _required_string(value, what)
    path = PurePosixPath(text)
    if (path.is_absolute() or "\\" in text or ":" in text or
            any(part in {"", ".", ".."} for part in text.split("/")) or
            any(part.casefold() == ".git" for part in path.parts)):
        raise OdmError(f"The {what} must be a safe relative path.")
    return path


def _json_pointer(value: Any, pointer: Any) -> Any:
    if pointer == "":
        return value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise OdmError("The source revision JSON pointer is invalid.")
    try:
        for part in pointer[1:].split("/"):
            if re.search(r"~(?![01])", part):
                raise ValueError("Invalid JSON pointer escape")
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not part.isdigit() or (len(part) > 1 and part.startswith("0")):
                    raise ValueError("Invalid list index")
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                raise ValueError("Invalid JSON structure")
        return value
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise OdmError("Decision Center did not return the configured source identity.") from exc


def _render(value: Any, variables: Mapping[str, str], *, path: bool = False) -> Any:
    if isinstance(value, str):
        replacements = {key: quote(str(item), safe="") if path else str(item)
                        for key, item in variables.items()}
        try:
            return value.format_map(replacements)
        except (KeyError, ValueError, IndexError) as exc:
            raise OdmError("A Decision Center endpoint contains an unknown placeholder.") from exc
    if isinstance(value, dict):
        return {key: _render(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, variables) for item in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise OdmError("A Decision Center query value is invalid.")


def _operation(dc: Mapping[str, Any], name: str, variables: Mapping[str, str]) -> tuple[str, dict[str, Any]]:
    operations = dc.get("operations", {})
    spec = operations.get(name) if isinstance(operations, dict) else None
    if (not isinstance(spec, dict) or not isinstance(spec.get("method", "GET"), str)
            or spec.get("method", "GET").upper() != "GET"):
        raise OdmError(f"The Decision Center {name} GET endpoint is not configured.")
    path = _required_string(spec.get("path"), f"Decision Center {name} path")
    params = spec.get("params", {})
    if not isinstance(params, dict):
        raise OdmError("Decision Center query parameters must be named values.")
    return _render(path, variables, path=True), _render(params, variables)


def content_digest(files: Mapping[str, bytes]) -> str:
    """Hash exact filenames and contents, excluding ZIP container metadata."""
    digest = hashlib.sha256()
    for name in sorted(files):
        encoded = name.encode("utf-8")
        body = files[name]
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _client(settings: Mapping[str, Any], http: Mapping[str, Any]) -> HttpClient:
    if not isinstance(http, dict):
        raise OdmError("The HTTP configuration is invalid.")
    return HttpClient(
        _required_string(settings.get("url"), "Decision Center URL"),
        username_env=settings.get("username_env"),
        password_env=settings.get("password_env"),
        token_env=settings.get("token_env"),
        timeout=http.get("timeout_seconds", 30),
        ca_bundle=http.get("ca_bundle") or None,
        allow_http=http.get("allow_http", False),
    )


def _revision(client: HttpClient, dc: Mapping[str, Any], variables: Mapping[str, str]) -> str:
    path, params = _operation(dc, "branch_revision", variables)
    payload = client.request("GET", path, params=params,
                             headers={"Accept": "application/json"},
                             expected=(200,), max_bytes=4 * 1024 * 1024).json()
    spec = dc["operations"]["branch_revision"]
    if "revision_pointer" not in spec:
        raise OdmError("The source revision JSON pointer is not configured.")
    revision = _json_pointer(payload, spec["revision_pointer"])
    if isinstance(revision, bool) or not isinstance(revision, (str, int)) or not str(revision).strip():
        raise OdmError("Decision Center returned an empty or invalid source revision.")
    for selector, variable in (("service_id_pointer", "service_id"), ("branch_id_pointer", "branch_id")):
        if selector in spec and str(_json_pointer(payload, spec[selector])) != variables[variable]:
            raise OdmError("Decision Center returned a different service or branch.")
    return str(revision)


def capture_source(selected: Mapping[str, Any]) -> dict[str, Any]:
    """Read a stable branch export without creating a baseline or editing DC."""
    dc = selected.get("decision_center", {})
    if not isinstance(dc, dict) or dc.get("contracts_verified") is not True:
        raise OdmError("The Decision Center sync API contract has not been verified.")
    application = selected.get("application", {})
    source = application.get("decision_center", {}) if isinstance(application, dict) else {}
    if not isinstance(source, dict):
        raise OdmError("The application source configuration is invalid.")
    variables = {"service_id": _required_string(source.get("service_id"), "decision service ID"),
                 "branch_id": _required_string(source.get("branch_id"), "source branch ID")}
    client = _client(dc, selected.get("http", {}))
    before = _revision(client, dc, variables)
    path, params = _operation(dc, "export_branch", variables)
    archive = client.request("GET", path, params=params,
                             headers={"Accept": "application/zip"},
                             expected=(200,), max_bytes=_MAX_EXPORT).body
    files = inspect_zip(archive)
    if not files:
        raise OdmError("Decision Center returned an empty rule export. Git was not changed.")
    if any(any(part.casefold() == ".git" for part in PurePosixPath(name).parts)
           for name in files):
        raise OdmError("The rule export contains a Git control directory.")
    after = _revision(client, dc, variables)
    if before != after:
        raise OdmError("Rules changed during the export. Git was not changed. Run sync again.")
    return {"app": selected["app"], "service_id": variables["service_id"],
            "branch_id": variables["branch_id"], "source_revision": before,
            "content_sha256": content_digest(files), "archive_sha256": sha256_bytes(archive),
            "files": files}


def _git_settings(selected: Mapping[str, Any]) -> dict[str, Any]:
    sync = selected.get("sync", {})
    git = sync.get("git", {}) if isinstance(sync, dict) else {}
    if not isinstance(git, dict):
        raise OdmError("The Git configuration is invalid.")
    remote = _required_string(git.get("url"), "Git repository URL")
    if remote.startswith("-") or any(c.isspace() for c in remote):
        raise OdmError("The Git repository URL is invalid.")
    parsed = urlsplit(remote)
    if parsed.scheme == "https":
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise OdmError("The Git HTTPS URL must not contain credentials or a query.")
    elif parsed.scheme == "ssh":
        if not parsed.hostname or parsed.password or parsed.query or parsed.fragment:
            raise OdmError("The Git SSH URL is invalid.")
    elif re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+", remote):
        pass
    elif not (git.get("allow_local_for_tests") is True and
              (parsed.scheme == "file" or Path(remote).is_absolute())):
        raise OdmError("Use a Git HTTPS or SSH URL without an embedded password.")
    branch = _required_string(git.get("branch"), "Git branch")
    if branch.startswith("-") or any(c.isspace() for c in branch):
        raise OdmError("The Git branch is invalid.")
    if bool(git.get("username_env")) != bool(git.get("password_env")):
        raise OdmError("The Git credential configuration is incomplete.")
    target_root = _relative_path(sync.get("target_root"), "Git export root")
    if target_root.parts[0].casefold() == ".odm-sync":
        raise OdmError("The Git export root conflicts with sync metadata.")
    attempts = sync.get("max_push_attempts", 3)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 5:
        raise OdmError("The Git push attempt limit must be between 1 and 5.")
    return dict(git, url=remote, branch=branch, target_root=str(target_root),
                max_push_attempts=attempts)


def _remote_identity(remote: str) -> str:
    parsed = urlsplit(remote)
    if parsed.scheme == "https" and parsed.hostname:
        host = parsed.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port not in (None, 443):
            host += ":" + str(parsed.port)
        return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))
    return remote.rstrip("/")


def _validate_destinations(config: Mapping[str, Any], track: str,
                           settings: Mapping[str, Any], apps: list[str]) -> None:
    """Do not let distinct capture jobs replace overlapping Git directories."""
    wanted_remote = _remote_identity(settings["url"])
    ours = [PurePosixPath(settings["target_root"], app) for app in apps]
    for other_track in config.get("tracks", {}):
        if other_track == track:
            continue
        other = resolve(config, other_track)
        sync_settings = other.get("sync", {})
        git_settings = sync_settings.get("git", {}) if isinstance(sync_settings, dict) else {}
        if (not isinstance(git_settings, dict) or not isinstance(git_settings.get("url"), str)
                or _remote_identity(git_settings["url"]) != wanted_remote
                or git_settings.get("branch") != settings["branch"]):
            continue
        root = _relative_path(sync_settings.get("target_root"), "Git export root")
        other_apps = other.get("sync_apps", [])
        if not isinstance(other_apps, list):
            raise OdmError("A track sharing the Git branch has an invalid application list.")
        theirs = [PurePosixPath(root, _name(name, "application name")) for name in other_apps]
        for left in ours:
            for right in theirs:
                lparts = tuple(part.casefold() for part in left.parts)
                rparts = tuple(part.casefold() for part in right.parts)
                common = min(len(lparts), len(rparts))
                if lparts[:common] == rparts[:common]:
                    raise OdmError("Sync tracks share overlapping Git export folders. Give each source a separate location.")


@contextmanager
def git_environment(settings: Mapping[str, Any]) -> Iterator[dict[str, str]]:
    """Keep credentials in environment variables, never a URL or helper file."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSL_NO_VERIFY"] = "false"
    env["GIT_CONFIG_COUNT"] = "3"
    env["GIT_CONFIG_KEY_0"] = "credential.helper"
    env["GIT_CONFIG_VALUE_0"] = ""
    env["GIT_CONFIG_KEY_1"] = "http.followRedirects"
    env["GIT_CONFIG_VALUE_1"] = "false"
    env["GIT_CONFIG_KEY_2"] = "http.sslVerify"
    env["GIT_CONFIG_VALUE_2"] = "true"
    username_env, password_env = settings.get("username_env"), settings.get("password_env")
    if not username_env:
        yield env
        return
    username, password = os.environ.get(username_env, ""), os.environ.get(password_env, "")
    if not username or not password:
        raise OdmError("The Git username or password is missing from Jenkins credentials.")
    env["ODM_SYNC_GIT_PASSWORD"] = password
    env["GIT_CONFIG_COUNT"] = "4"
    env["GIT_CONFIG_KEY_3"] = "credential.username"
    env["GIT_CONFIG_VALUE_3"] = username
    with tempfile.TemporaryDirectory(prefix="odm-git-auth-") as temporary:
        directory = Path(temporary)
        helper = directory / "askpass.py"
        helper.write_text("import os, sys\nsys.stdout.write(os.environ['ODM_SYNC_GIT_PASSWORD'])\n", encoding="utf-8")
        if os.name == "nt":
            wrapper = directory / "askpass.cmd"
            wrapper.write_text(f'@echo off\r\n"{sys.executable}" "%~dp0askpass.py"\r\n', encoding="utf-8")
        else:
            wrapper = directory / "askpass.sh"
            wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper))}\n", encoding="utf-8")
            wrapper.chmod(0o700)
        env["GIT_ASKPASS"] = str(wrapper)
        yield env


class Git:
    def __init__(self, env: Mapping[str, str], executable: str | None = None):
        self.env = dict(env)
        self.executable = executable or os.environ.get("GIT_EXECUTABLE", "git")

    def run(self, arguments: list[str], *, cwd: Path | None = None,
            check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run([self.executable, *arguments], cwd=cwd,
                                    env=self.env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", timeout=180)
        except FileNotFoundError as exc:
            raise OdmError("Git is not installed on this agent.") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OdmError("The Git operation did not complete. Check access and try again.") from exc
        if check and result.returncode:
            # Server output can contain credentials or private URLs. Do not echo it.
            raise OdmError("The Git operation failed. Check repository access and branch settings.")
        return result

    def remote_head(self, settings: Mapping[str, Any]) -> str | None:
        self.run(["check-ref-format", "--branch", settings["branch"]])
        result = self.run(["ls-remote", "--heads", "--", settings["url"]])
        heads = [line.split("\t", 1) for line in result.stdout.splitlines() if line.strip()]
        if any(len(item) != 2 for item in heads):
            raise OdmError("Git returned an invalid branch reference.")
        wanted = "refs/heads/" + settings["branch"]
        matched = [sha for sha, ref in heads if ref == wanted]
        if len(matched) > 1 or any(not re.fullmatch(r"[0-9a-f]{40,64}", sha) for sha in matched):
            raise OdmError("Git returned an invalid branch reference.")
        if matched:
            return matched[0]
        if heads:
            raise OdmError("The configured Git branch does not exist.")
        return None


def _local_path(repo: Path, relative: str) -> Path:
    target = repo.joinpath(*PurePosixPath(relative).parts)
    if not target.resolve().is_relative_to(repo.resolve()) or target.resolve() == repo.resolve():
        raise OdmError("The Git export location points outside the working folder.")
    current = target
    while current != repo:
        if current.is_symlink():
            raise OdmError("The Git export location contains a symbolic link.")
        current = current.parent
    return target


def _read_tree(target: Path) -> dict[str, bytes]:
    if not target.exists():
        return {}
    if not target.is_dir() or target.is_symlink():
        raise OdmError("The existing Git export is not a regular folder.")
    files: dict[str, bytes] = {}
    for parent, directories, names in os.walk(target, followlinks=False):
        for name in directories + names:
            item = Path(parent) / name
            if item.is_symlink():
                raise OdmError("The existing Git export contains a symbolic link.")
        for name in names:
            item = Path(parent) / name
            files[item.relative_to(target).as_posix()] = item.read_bytes()
    return files


def _same_provenance(path: Path, captured: Mapping[str, Any]) -> bool:
    if not path.is_file():
        return False
    try:
        saved = read_json(path)
    except OdmError:
        return False
    return isinstance(saved, dict) and all(
        saved.get(key) == captured[key]
        for key in ("app", "service_id", "branch_id", "content_sha256")
    )


def _write_tree(repo: Path, relative: str, files: Mapping[str, bytes]) -> None:
    target = _local_path(repo, relative)
    if target.exists():
        if not target.is_dir():
            raise OdmError("The existing Git export is not a folder.")
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        path = _local_path(repo, relative + "/" + name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def _prepare_checkout(git: Git, repo: Path) -> None:
    """Do not run hooks or change export bytes through Git attributes."""
    hooks = repo / ".git" / "odm-disabled-hooks"
    hooks.mkdir()
    git.run(["config", "--local", "core.hooksPath", str(hooks)], cwd=repo)
    git.run(["config", "--local", "core.autocrlf", "false"], cwd=repo)
    git.run(["config", "--local", "core.safecrlf", "false"], cwd=repo)
    git.run(["config", "--local", "commit.gpgsign", "false"], cwd=repo)
    # info/attributes takes precedence over repository .gitattributes files.
    # Set it before checkout as well as add so the comparison is byte-exact.
    attributes = repo / ".git" / "info" / "attributes"
    attributes.parent.mkdir(exist_ok=True)
    attributes.write_text("* -text -filter -ident -working-tree-encoding -eol\n", encoding="utf-8")


def _object_id(git: Git, repo: Path, revision: str | None, relative: str) -> str | None:
    if not revision:
        return None
    result = git.run(["rev-parse", "--verify", f"{revision}:{relative}"], cwd=repo, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def push_commit(git: Git, repo: Path, settings: Mapping[str, Any],
                base_head: str | None, protected_paths: list[str]) -> None:
    """Rebase only over unrelated remote changes; never overwrite a rule tree."""
    ref = "refs/heads/" + settings["branch"]
    for attempt in range(settings["max_push_attempts"]):
        pushed = git.run(["push", "origin", "HEAD:" + ref], cwd=repo, check=False)
        if pushed.returncode == 0:
            return
        if attempt + 1 == settings["max_push_attempts"]:
            break
        git.run(["fetch", "--no-tags", "origin", ref], cwd=repo)
        remote_head = git.run(["rev-parse", "FETCH_HEAD"], cwd=repo).stdout.strip()
        if base_head:
            descendant = git.run(["merge-base", "--is-ancestor", base_head, remote_head],
                                 cwd=repo, check=False)
            if descendant.returncode != 0:
                raise OdmError("The Git branch history changed. Run sync again.")
        if any(_object_id(git, repo, base_head, path) != _object_id(git, repo, remote_head, path)
               for path in protected_paths):
            raise OdmError("The same rule export changed in Git during this run. Run sync again.")
        arguments = ["rebase", remote_head] if base_head else ["rebase", "--onto", remote_head, "--root"]
        rebased = git.run(arguments, cwd=repo, check=False)
        if rebased.returncode:
            raise OdmError("The Git updates could not be combined safely. Run sync again.")
        base_head = remote_head
    raise OdmError("The Git update was not confirmed. Check Git before running sync again.")


def _select(config: Mapping[str, Any], track: str, app: str | None,
            job_name: str | None, *, require_job: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _name(track, "deployment track")
    selected = resolve(config, track)
    if require_job or job_name:
        allowed = selected.get("sync_jobs", [])
        if not job_name or not isinstance(allowed, list) or job_name not in allowed:
            raise OdmError("This Jenkins job is not allowed to sync the selected track.")
    configured = selected.get("sync_apps", [])
    if not isinstance(configured, list) or not configured:
        raise OdmError("No sync applications are configured for this track.")
    apps = [app] if app else configured
    if any(not isinstance(item, str) for item in apps):
        raise OdmError("The sync application list is invalid.")
    if len(set(apps)) != len(apps):
        raise OdmError("The sync application list contains duplicates.")
    sources = []
    for name in apps:
        _name(name, "application name")
        if name not in configured:
            raise OdmError("This application is not enabled for sync on the selected track.")
        sources.append(resolve(config, track, name))
    return selected, sources


def plan(config: Mapping[str, Any], track: str, app: str | None = None,
         job_name: str | None = None) -> dict[str, Any]:
    selected, sources = _select(config, track, app, job_name, require_job=False)
    settings = _git_settings(selected)
    _validate_destinations(config, track, settings, [source["app"] for source in sources])
    lock = sha256_bytes((_remote_identity(settings["url"]) + "\n" + settings["branch"]).encode("utf-8"))[:20]
    return {"status": "planned", "track": track, "apps": [s["app"] for s in sources],
            "git_branch": settings["branch"], "target_root": settings["target_root"],
            "lock_name": "odm-sync-" + lock,
            "message": "The sync plan is ready. No server was changed."}


def preflight(config: Mapping[str, Any], track: str, app: str | None = None,
              job_name: str | None = None, *, git_executable: str | None = None) -> dict[str, Any]:
    selected, sources = _select(config, track, app, job_name, require_job=False)
    result = plan(config, track, app, job_name)
    settings = _git_settings(selected)
    captures = [capture_source(source) for source in sources]
    with git_environment(settings) as env:
        Git(env, git_executable).remote_head(settings)
    result.update(status="preflight_passed", observed_at=_utc_now(),
                  sources=[{k: v for k, v in item.items() if k != "files"} for item in captures],
                  message="Sync access and exports passed. Git was not changed.")
    return result


def run_sync(config: Mapping[str, Any], track: str, app: str | None = None,
             job_name: str | None = None, *, git_executable: str | None = None) -> dict[str, Any]:
    selected, sources = _select(config, track, app, job_name, require_job=True)
    settings = _git_settings(selected)
    result = plan(config, track, app, job_name)
    if os.environ.get("ODM_SYNC_LOCK") != result["lock_name"]:
        raise OdmError("The Git sync lock is missing. Start sync through its Jenkins job.")
    # Validate every app before changing even the temporary Git checkout.
    captures = [capture_source(source) for source in sources]
    with git_environment(settings) as env, tempfile.TemporaryDirectory(prefix="odm-sync-") as temporary:
        git = Git(env, git_executable)
        remote = git.remote_head(settings)
        repo = Path(temporary) / "repo"
        repo.mkdir()
        git.run(["init", "--quiet"], cwd=repo)
        _prepare_checkout(git, repo)
        git.run(["symbolic-ref", "HEAD", "refs/heads/" + settings["branch"]], cwd=repo)
        git.run(["remote", "add", "origin", settings["url"]], cwd=repo)
        if remote:
            git.run(["fetch", "--no-tags", "origin", "refs/heads/" + settings["branch"]], cwd=repo)
            git.run(["checkout", "--quiet", "-B", settings["branch"], "FETCH_HEAD"], cwd=repo)
        author_name = _required_string(settings.get("author_name", "ODM Sync"), "Git author name")
        author_email = _required_string(settings.get("author_email"), "Git author email")
        git.run(["config", "--local", "user.name", author_name], cwd=repo)
        git.run(["config", "--local", "user.email", author_email], cwd=repo)
        changed: list[str] = []
        protected: list[str] = []
        for captured in captures:
            app_name = captured["app"]
            relative = settings["target_root"] + "/" + app_name
            metadata_relative = f".odm-sync/{track}/{app_name}.json"
            target = _local_path(repo, relative)
            metadata = _local_path(repo, metadata_relative)
            if _read_tree(target) == captured["files"] and _same_provenance(metadata, captured):
                continue
            _write_tree(repo, relative, captured["files"])
            atomic_json(metadata, {key: value for key, value in captured.items() if key != "files"})
            git.run(["add", "-f", "-A", "--", relative, metadata_relative], cwd=repo)
            changed.append(app_name)
            protected.extend([relative, metadata_relative])
        result["observed_at"] = _utc_now()
        result["sources"] = [{k: v for k, v in item.items() if k != "files"} for item in captures]
        if not changed:
            result.update(status="unchanged", changed_apps=[], message="No rule changes were found.")
            return result
        staged = git.run(["diff", "--cached", "--quiet"], cwd=repo, check=False)
        if staged.returncode not in (0, 1):
            raise OdmError("Git could not compare the rule export.")
        if staged.returncode == 0:
            result.update(status="unchanged", changed_apps=[], message="No rule changes were found.")
            return result
        base = git.run(["rev-parse", "--verify", "HEAD"], cwd=repo, check=False)
        base_head = base.stdout.strip() if base.returncode == 0 else None
        git.run(["commit", "--quiet", "-m", f"[odm-sync] {track}: {', '.join(changed)}"], cwd=repo)
        push_commit(git, repo, settings, base_head, protected)
        commit = git.run(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        result.update(status="synced", changed_apps=changed, commit=commit,
                      message="Rule changes were saved in Git.")
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture configured Decision Center rules in Git.")
    parser.add_argument("command", choices=("plan", "preflight", "run"))
    parser.add_argument("--config", default="config/odm.yaml")
    parser.add_argument("--output", default="output/sync-result.json")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        track = os.environ.get("DEPLOYMENT_TRACK", "").strip()
        app = os.environ.get("APP", "").strip() or None
        job = os.environ.get("JOB_NAME", "").strip() or None
        operation = {"plan": plan, "preflight": preflight, "run": run_sync}[args.command]
        result = operation(config, track, app, job)
        atomic_json(args.output, result)
        print(result["message"])
        return 0
    except OdmError as exc:
        result = {"status": "failed", "message": str(exc), "observed_at": _utc_now()}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        result = {"status": "failed", "message": "Sync could not complete. Check the configuration and agent access.",
                  "observed_at": _utc_now()}
    try:
        atomic_json(args.output, result)
    except (OSError, OdmError):
        print("The sync result could not be saved.", file=sys.stderr)
    print(result["message"], file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
