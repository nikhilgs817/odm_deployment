#!/usr/bin/env python3
"""Build once in the first track; promote an explicitly selected ODM release.

Jenkins owns credentials, approval and locks. This script never changes job
parameters. API operation contracts must be checked on the installed servers.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
from typing import Any
from urllib.parse import quote, urlsplit
import uuid
import xml.etree.ElementTree as ET
import zipfile

from odm_common import (HttpClient, OdmError, atomic_json, canonical_zip_digest,
                        inspect_zip, load_config, read_json, resolve, run_git,
                        sha256_bytes, sha256_file)

VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
MAX_PACKAGE = 256 * 1024 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return sha256_bytes(canonical(value))


def save_result(path: Path, value: dict, *, required: bool = False) -> None:
    """A reporting failure must never prevent recovery of active rules."""
    try:
        atomic_json(path, value)
    except (OSError, OdmError) as exc:
        value["local_result_saved"] = False
        if required:
            raise OdmError("The deployment result file could not be saved. No active rules were changed.") from exc
        print("The local result file could not be saved. Check the deployment record in Nexus.", file=sys.stderr)


def pointer(value: Any, path: str) -> Any:
    if path == "":
        return value
    if not isinstance(path, str) or not path.startswith("/"):
        raise OdmError("An API response field is not configured correctly.")
    try:
        for key in path[1:].split("/"):
            key = key.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise OdmError("The server response is missing a required field.") from exc


def clean_setting(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(
        marker in value.upper() for marker in ("REPLACE_ME", "EXAMPLE.INVALID", "<", ">")
    ):
        raise OdmError(f"{label} is not configured.")
    return value


def check_url(value: str) -> str:
    clean_setting(value, "The server URL")
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise OdmError("The server URL must not contain credentials or query values.")
    return value.rstrip("/")


def client(settings: dict, cfg: dict) -> HttpClient:
    http = cfg.get("http", {})
    return HttpClient(check_url(settings["url"]), settings.get("username_env"),
                      settings.get("password_env"), settings.get("token_env"),
                      timeout=http.get("timeout_seconds", 30),
                      ca_bundle=http.get("ca_bundle"), allow_http=http.get("allow_http", False))


def variables(cfg: dict, plan: dict, **extra: str) -> dict:
    app = cfg["application"]
    result = {k: str(v) for k, v in app.items() if isinstance(v, (str, int))}
    result.update({k: str(v) for k, v in app.get("decision_center", {}).items()})
    result.update({"baseline_id": plan.get("baseline_id", ""), "jar_version": plan["jar_version"],
                   "app": plan["app"], "track": plan["track"]})
    result.update(extra)
    return result


def render(value: str, values: dict, encode: bool = False) -> str:
    try:
        fields = {k: quote(str(v), safe="") if encode else str(v) for k, v in values.items()}
        return value.format_map(fields)
    except (KeyError, ValueError, AttributeError) as exc:
        raise OdmError("An API operation contains an unknown placeholder.") from exc


def operation(http: HttpClient, settings: dict, name: str, values: dict,
              data: bytes | None = None, *, read_only: bool = False):
    spec = settings.get("operations", {}).get(name)
    if not isinstance(spec, dict) or not spec.get("path") or not spec.get("method"):
        raise OdmError(f"The {name} API operation is not configured.")
    clean_setting(spec["path"], f"The {name} API path")
    method = spec["method"].upper()
    if read_only and method not in {"GET", "HEAD"}:
        raise OdmError("A read-only check tried to change the server.")
    if method not in {"GET", "HEAD", "POST", "PUT"}:
        raise OdmError("The API operation uses an unsupported method.")
    expected = tuple(spec.get("expected", [200]))
    if "retry_safe" in spec and type(spec["retry_safe"]) is not bool:
        raise OdmError("The API retry_safe setting must be true or false.")
    if method in {"POST", "PUT"} and 202 in expected:
        raise OdmError("Asynchronous writes need an explicit completion check before use.")
    params = {k: render(str(v), values) for k, v in spec.get("params", {}).items()}
    headers = {"Accept": spec.get("accept", "application/json")}
    if data is not None:
        headers["Content-Type"] = spec.get("content_type", "application/octet-stream")
    response = http.request(method, render(spec["path"], values, True), params=params,
                            data=data, headers=headers, expected=expected,
                            retry=spec.get("retry_safe", method in {"GET", "HEAD"} and name not in {"download_ruleapp", "download_xom", "build"}),
                            max_bytes=spec.get("max_bytes", MAX_PACKAGE))
    for assertion in spec.get("success_assertions", []):
        if canonical(pointer(response.json(), assertion["pointer"])) != canonical(assertion["equals"]):
            raise OdmError(f"The {name} operation did not report success.")
    return response


def fixture(data: bytes, expected: bool = False) -> Any:
    if b"REPLACE_ME" in data.upper():
        raise OdmError("The business test files still contain example values.")
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise OdmError("A business test file is not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise OdmError("A business test must contain a JSON object.")
    if expected:
        assertions = parsed.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise OdmError("A business test needs at least one expected result.")
        for item in assertions:
            if not isinstance(item, dict) or not isinstance(item.get("pointer"), str) or "equals" not in item:
                raise OdmError("An expected business result is not configured correctly.")
            if item["pointer"] != "" and not item["pointer"].startswith("/"):
                raise OdmError("An expected business result has an invalid JSON pointer.")
    return parsed


def check_jar(data: bytes, app: dict) -> str:
    files = inspect_zip(data)
    identity = app.get("archive_identity", {})
    member = clean_setting(identity.get("member"), "The RuleApp archive identity file")
    if member not in files:
        raise OdmError("The RuleApp archive is missing its identity file.")
    content = files[member]
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise OdmError("The RuleApp identity XML contains an unsupported declaration.")
    try:
        root = ET.fromstring(content)
        checks = identity.get("checks", [])
        if not checks:
            raise OdmError("The RuleApp archive identity checks are missing.")
        roles = {check.get("role") for check in checks}
        if not {"ruleapp_name", "ruleapp_version", "ruleset_name", "ruleset_version"}.issubset(roles):
            raise OdmError("The archive identity checks must cover the RuleApp and ruleset names and versions.")
        for check in checks:
            clean_setting(check.get("xpath", "."), "The archive identity XPath")
            if "attribute" in check:
                clean_setting(check["attribute"], "The archive identity attribute")
            node = root if check.get("xpath", ".") == "." else root.find(check["xpath"])
            if node is None:
                raise OdmError("The RuleApp archive does not match the configured application.")
            actual = node.get(check["attribute"]) if "attribute" in check else node.text
            wanted = render(str(check["equals"]), {k: str(v) for k, v in app.items()})
            if actual != wanted:
                raise OdmError("The RuleApp archive does not match the configured application.")
    except (ET.ParseError, SyntaxError) as exc:
        raise OdmError("The RuleApp identity file is not valid XML.") from exc
    return canonical_zip_digest(data)


def automation_digest() -> str:
    source = Path(__file__).resolve()
    files = {"deployment/deploy.py": source,
             "odm_common.py": source.parent.parent / "odm_common.py"}
    return digest({name: sha256_file(path) for name, path in files.items()})


def plan_hash(plan: dict) -> str:
    return digest({k: v for k, v in plan.items() if k != "plan_hash"})


def check_inputs(cfg: dict) -> tuple[str, str, str]:
    if not os.environ.get("JOB_NAME") or os.environ["JOB_NAME"] not in cfg.get("deployment_jobs", []):
        raise OdmError("This Jenkins job is not allowed to deploy to the selected track.")
    version = os.environ.get("JAR_VERSION", "").strip()
    if not VERSION.fullmatch(version) or version.lower() in {"latest", "highest", "select"} or "snapshot" in version.lower():
        raise OdmError("Select an explicit release version. Latest and snapshot versions are not allowed.")
    action = os.environ.get("ACTION", "DEPLOY").strip().upper()
    if action not in {"DEPLOY", "ROLLBACK"}:
        raise OdmError("ACTION must be DEPLOY or ROLLBACK.")
    baseline = os.environ.get("BASELINE_ID", "").strip()
    if action == "DEPLOY" and cfg.get("build_release") is True and not baseline:
        raise OdmError("Select the baseline to build in the first deployment track.")
    if cfg.get("build_release") is not True and baseline:
        raise OdmError("Higher deployment tracks must use a Nexus release, not a baseline.")
    return version, action, baseline


def create_plan(config_path: Path) -> tuple[dict, dict]:
    config = load_config(config_path)
    track, app = os.environ.get("DEPLOYMENT_TRACK", ""), os.environ.get("APP", "")
    cfg = resolve(config, track, app, os.environ.get("JOB_NAME"))
    for flag in ("build_release", "require_approval"):
        if type(cfg.get(flag)) is not bool:
            raise OdmError(f"The {flag} setting must be true or false.")
    version, action, baseline = check_inputs(cfg)
    application = cfg["application"]
    for key in ("ruleapp_name", "ruleapp_version", "xom_name", "library_name", "library_version", "artifact_id"):
        clean_setting(application.get(key), f"The {key} setting for {app}")
    res_url = check_url(cfg["res"]["url"])
    for other_name, other in cfg["apps"].items():
        if other_name != app and any(application[k] == other.get(k) for k in ("ruleapp_name", "library_name", "xom_name")):
            raise OdmError("The applications share a RuleApp, XOM or library name. Check the configuration.")
    build = cfg.get("build_release") is True and action == "DEPLOY"
    predecessor = cfg.get("predecessor")
    if not build and action == "DEPLOY" and (not predecessor or predecessor not in config["tracks"] or predecessor == track):
        raise OdmError("The preceding deployment track is not configured.")
    requires = cfg.get("require_approval") is True
    if requires:
        clean_setting(cfg.get("approval_submitters"), "The deployment approver list")
    target_key = digest({"res": res_url, "ruleapp": application["ruleapp_name"]})[:24]
    plan = {"schema_version": 1, "phase": "planned", "app": app, "track": track,
            "jar_version": version, "action": action, "baseline_id": baseline,
            "job_name": os.environ["JOB_NAME"], "run_id": uuid.uuid4().hex,
            "build_url": os.environ.get("BUILD_URL", ""), "build_tag": os.environ.get("BUILD_TAG", ""),
            "created_at": now(), "res_url": res_url,
            "needs_build": build, "requires_approval": requires,
            "approval_submitters": cfg.get("approval_submitters", ""),
            "lock_name": "odm-res-" + target_key,
            "catalog_lock_name": "odm-catalog-" + digest(cfg["nexus"]["url"])[:24],
            "config_sha256": sha256_file(config_path), "automation_sha256": automation_digest(),
            "release_sha256": "", "release_path": release_path(cfg, version),
            "ruleapp_path": "/" + application["ruleapp_name"] + "/" + application["ruleapp_version"]}
    plan["plan_hash"] = plan_hash(plan)
    return cfg, plan


def load_plan(config_path: Path, path: Path, *, prepared: bool = False) -> tuple[dict, dict]:
    plan = read_json(path)
    if not isinstance(plan, dict) or plan.get("plan_hash") != plan_hash(plan):
        raise OdmError("The deployment plan has changed. Prepare it again.")
    if plan.get("config_sha256") != sha256_file(config_path) or plan.get("automation_sha256") != automation_digest():
        raise OdmError("The configuration or deployment script changed after planning.")
    cfg = resolve(load_config(config_path), os.environ.get("DEPLOYMENT_TRACK", ""),
                  os.environ.get("APP", ""), os.environ.get("JOB_NAME"))
    version, action, baseline = check_inputs(cfg)
    if plan.get("build_tag", "") != os.environ.get("BUILD_TAG", ""):
        raise OdmError("The deployment plan belongs to a different Jenkins run.")
    for field, wanted in (("app", cfg["app"]), ("track", cfg["track"]), ("jar_version", version),
                          ("action", action), ("baseline_id", baseline), ("job_name", os.environ["JOB_NAME"])):
        if plan.get(field) != wanted:
            raise OdmError("The selected deployment parameters changed after planning.")
    if prepared and plan.get("phase") != "prepared":
        raise OdmError("Prepare the selected release before deployment.")
    return cfg, plan


def release_path(cfg: dict, version: str) -> str:
    nx = cfg["nexus"]
    group = clean_setting(nx.get("group_id"), "The Nexus group")
    artifact = clean_setting(cfg["application"].get("artifact_id"), "The Nexus artifact")
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in (group, artifact)):
        raise OdmError("The Nexus artifact coordinates are invalid.")
    return "/".join((group.replace(".", "/"), artifact, version, f"{artifact}-{version}.zip"))


class Nexus:
    def __init__(self, cfg: dict):
        self.cfg, self.settings = cfg, cfg["nexus"]
        self.http = client(self.settings, cfg)

    def path(self, repo: str, path: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            raise OdmError("The Nexus repository name is invalid.")
        return "repository/" + repo + "/" + path

    def get(self, path: str, *, records: bool = False, missing: bool = False) -> bytes | None:
        repo = self.settings["records_repository" if records else "release_repository"]
        result = self.http.request("GET", self.path(repo, path), expected=(200, 404) if missing else (200,))
        return None if result.status == 404 else result.body

    def put(self, path: str, data: bytes, *, records: bool = False, mutable: bool = False) -> None:
        existing = self.get(path, records=records, missing=True)
        if existing is not None and not mutable:
            if sha256_bytes(existing) != sha256_bytes(data):
                raise OdmError("This release or deployment record already exists with different content.")
            return
        repo = self.settings["records_repository" if records else "release_repository"]
        try:
            self.http.request("PUT", self.path(repo, path), data=data,
                              headers={"Content-Type": "application/json" if path.endswith(".json") else "application/zip"},
                              expected=(200, 201, 204))
        except OdmError:
            # Reconcile a rejected or lost write response. Do not repeat the PUT.
            saved = self.get(path, records=records, missing=True)
            if saved is None or sha256_bytes(saved) != sha256_bytes(data):
                raise OdmError("Nexus did not confirm the upload. Check the stored release before trying again.")
        saved = self.get(path, records=records)
        if saved is None or sha256_bytes(saved) != sha256_bytes(data):
            raise OdmError("The Nexus download checksum does not match the uploaded content.")

    def records(self, track: str, app: str) -> list[dict]:
        prefix = f"receipts/{track}/{app}/"
        token = None
        seen: set[str] = set()
        records = []
        for _ in range(1000):
            params = {"repository": self.settings["records_repository"], "format": "raw"}
            if token:
                params["continuationToken"] = token
            page = self.http.request("GET", "service/rest/v1/search/assets", params=params,
                                     expected=(200,), max_bytes=8 * 1024 * 1024).json()
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise OdmError("Nexus returned an invalid deployment record list.")
            for item in page["items"]:
                path = item.get("path", "")
                if not isinstance(path, str) or not path.startswith(prefix) or not path.endswith(".json"):
                    continue
                data = self.get(path, records=True)
                try:
                    record = json.loads(data or b"")
                except (ValueError, UnicodeError) as exc:
                    raise OdmError("A stored deployment record is not valid JSON.") from exc
                if not isinstance(record, dict) or record.get("app") != app or record.get("track") != track:
                    raise OdmError("A deployment record does not match its application or track.")
                records.append(record)
            token = page.get("continuationToken")
            if not token:
                return records
            if not isinstance(token, str) or token in seen:
                raise OdmError("Nexus repeated a search page. Deployment stopped.")
            seen.add(token)
        raise OdmError("The deployment record search exceeded its page limit.")


def eligible(cfg: dict, plan: dict, nexus: Nexus) -> list[dict]:
    source = plan["track"] if plan["action"] == "ROLLBACK" else cfg.get("predecessor")
    if not source:
        return []
    source_cfg = resolve(cfg, source, plan["app"])
    target = source_cfg["res"]["url"].rstrip("/")
    revoked = cfg["nexus"].get("revoked_versions", {}).get(plan["app"], [])
    return [r for r in nexus.records(source, plan["app"])
            if r.get("status") == "succeeded" and r.get("res_url") == target
            and r.get("jar_version") not in revoked
            and valid_receipt(r, source_cfg)]


def valid_receipt(record: dict, source_cfg: dict) -> bool:
    """Reject malformed/imported markers that lack actual test evidence."""
    version = record.get("jar_version")
    if (type(record.get("schema_version")) is not int or record["schema_version"] != 1 or not isinstance(version, str)
            or not VERSION.fullmatch(version) or version.lower() in {"latest", "highest", "select"}
            or "snapshot" in version.lower() or record.get("app") != source_cfg["app"]
            or record.get("track") != source_cfg["track"]
            or record.get("release_path") != release_path(source_cfg, version)
            or not re.fullmatch(r"[a-f0-9]{32}", str(record.get("run_id", "")))):
        return False
    for field in ("release_sha256", "plan_hash", "automation_sha256", "ruleapp_content_sha256", "xom_content_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(record.get(field, ""))):
            return False
    tests = record.get("tests")
    urls = source_cfg["res"].get("execution_urls", [])
    if not isinstance(tests, list) or not urls:
        return False
    required = {(case["name"], case["version"], url) for case in source_cfg["application"]["rulesets"] for url in urls}
    passed = {(case.get("ruleset"), case.get("version"), case.get("execution_url"))
              for case in tests if isinstance(case, dict) and case.get("passed") is True}
    return bool(required) and required.issubset(passed)


def check_eligibility(cfg: dict, plan: dict, nexus: Nexus, package_hash: str) -> None:
    if plan["jar_version"] in cfg["nexus"].get("revoked_versions", {}).get(plan["app"], []):
        raise OdmError("This release has been withdrawn from deployment.")
    if plan["needs_build"]:
        return
    if not any(r.get("jar_version") == plan["jar_version"] and r["release_sha256"] == package_hash
               for r in eligible(cfg, plan, nexus)):
        if plan["action"] == "ROLLBACK":
            raise OdmError("This release has not passed testing on the selected target.")
        raise OdmError(f"This release has not passed {str(cfg.get('predecessor', '')).upper()} testing.")


def package_files(data: bytes, cfg: dict, plan: dict) -> tuple[dict, dict[str, bytes]]:
    files = inspect_zip(data)
    try:
        manifest = json.loads(files["manifest.json"])
    except (KeyError, ValueError, UnicodeError) as exc:
        raise OdmError("The release package has no valid manifest.") from exc
    if manifest.get("schema_version") != 1 or manifest.get("app") != plan["app"] or manifest.get("jar_version") != plan["jar_version"]:
        raise OdmError("The Nexus release does not match the selected app and version.")
    for name in ("ruleapp.jar", "xom.zip", "source.zip"):
        if name not in files or manifest.get("files", {}).get(name) != sha256_bytes(files[name]):
            raise OdmError("The release package is incomplete or its checksum does not match.")
    check_jar(files["ruleapp.jar"], cfg["application"])
    inspect_zip(files["xom.zip"])
    inspect_zip(files["source.zip"])
    app = cfg["application"]
    expected_identity = {k: app[k] for k in ("ruleapp_name", "ruleapp_version", "xom_name", "library_name", "library_version")}
    if manifest.get("target") != expected_identity:
        raise OdmError("The release runtime paths do not match the selected application.")
    expected_rulesets = {(r["name"], r["version"]) for r in app["rulesets"]}
    actual_rulesets = {(r["name"], r["version"]) for r in manifest.get("tests", [])}
    if expected_rulesets != actual_rulesets or len(manifest["tests"]) != len(expected_rulesets):
        raise OdmError("The release does not contain tests for all expected rulesets.")
    for case in manifest["tests"]:
        for key, expectation in (("request", False), ("expected", True)):
            filename = case[key]
            if filename not in files or manifest["files"].get(filename) != sha256_bytes(files[filename]):
                raise OdmError("A release test file is missing or its checksum does not match.")
            fixture(files[filename], expectation)
    return manifest, files


@contextmanager
def git_auth(settings: dict):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_SSL_NO_VERIFY", None)
    overrides = [("credential.helper", ""), ("http.sslVerify", "true"), ("http.followRedirects", "false"),
                 ("core.autocrlf", "false"), ("core.safecrlf", "false")]
    env["GIT_CONFIG_COUNT"] = str(len(overrides))
    for index, (key, value) in enumerate(overrides):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    user, password = settings.get("username_env"), settings.get("password_env")
    if not user and not password:
        yield env
        return
    if not user or not password or not os.environ.get(user) or not os.environ.get(password):
        raise OdmError("Git credentials are missing from Jenkins.")
    with tempfile.TemporaryDirectory(prefix="odm-git-") as tmp:
        helper = Path(tmp) / "askpass.py"
        helper.write_text("import os,sys\nprint(os.environ[os.environ['ODM_GIT_USER_ENV'] if 'username' in sys.argv[1].lower() else os.environ['ODM_GIT_PASS_ENV']])\n", encoding="utf-8")
        launcher = Path(tmp) / ("askpass.cmd" if os.name == "nt" else "askpass.sh")
        if os.name == "nt":
            launcher.write_text(f'@echo off\n"{sys.executable}" "{helper}" %*\n', encoding="utf-8")
        else:
            import shlex
            launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper))} "$@"\n', encoding="utf-8")
            launcher.chmod(0o700)
        env.update(GIT_ASKPASS=str(launcher), ODM_GIT_USER_ENV=user, ODM_GIT_PASS_ENV=password)
        yield env


def save_baseline(cfg: dict, plan: dict, source: bytes) -> dict:
    settings = cfg.get("release_git", cfg.get("sync", {}).get("git", {}))
    url = clean_setting(settings.get("url"), "The baseline Git repository")
    if urlsplit(url).username or urlsplit(url).password or url.startswith("-"):
        raise OdmError("The Git URL must not contain credentials or command options.")
    branch = settings.get("branch", "main")
    if (not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
            or ".." in branch or "//" in branch or branch.endswith(("/", ".lock"))):
        raise OdmError("The baseline Git branch is invalid.")
    tag = f"{plan['app']}/{plan['jar_version']}"
    with git_auth(settings) as env, tempfile.TemporaryDirectory(prefix="odm-baseline-") as tmp:
        folder = Path(tmp) / "repo"
        clone_hooks = Path(tmp) / "disabled-hooks"
        clone_hooks.mkdir()
        run_git(["-c", f"core.hooksPath={clone_hooks}", "clone", "--no-checkout", "--branch", branch, "--single-branch", "--", url, str(folder)], Path(tmp), env=env)
        info = folder / ".git" / "info" / "attributes"
        info.parent.mkdir(exist_ok=True)
        info.write_text("* -text -filter -ident -working-tree-encoding -eol\n", encoding="utf-8")
        hooks = folder / ".git" / "odm-disabled-hooks"
        hooks.mkdir()
        run_git(["config", "--local", "core.hooksPath", str(hooks)], folder, env=env)
        run_git(["config", "--local", "commit.gpgsign", "false"], folder, env=env)
        run_git(["checkout", "--quiet", "--force", branch], folder, env=env)
        # Never retag or replace an existing release source.
        existing = run_git(["tag", "--list", tag], folder, env=env)
        if existing.strip():
            raise OdmError("The baseline Git tag already exists. Check the existing release before trying again.")
        destination = folder / "releases" / plan["app"] / plan["jar_version"]
        for candidate in (folder / "releases", folder / "releases" / plan["app"], destination):
            if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
                raise OdmError("The baseline repository contains an unsafe release path.")
        destination.mkdir(parents=True)
        (destination / "source.zip").write_bytes(source)
        atomic_json(destination / "source.json", {"app": plan["app"], "baseline_id": plan["baseline_id"],
                    "source_sha256": sha256_bytes(source), "source_content_sha256": canonical_zip_digest(source)})
        git_args = ["-c", "user.name=ODM Jenkins", "-c", "user.email=odm-jenkins@localhost"]
        run_git(["add", "--", str(destination.relative_to(folder)).replace("\\", "/")], folder, env=env)
        run_git(git_args + ["commit", "-m", f"Save {plan['app']} release {plan['jar_version']} baseline"], folder, env=env)
        commit = run_git(["rev-parse", "HEAD"], folder, env=env).strip()
        run_git(["tag", tag], folder, env=env)
        # Require atomic Git support so branch and source tag publish together.
        run_git(["push", "--atomic", "origin", f"HEAD:refs/heads/{branch}", f"refs/tags/{tag}"], folder, env=env)
        return {"commit": commit, "tag": tag}


def verify_baseline(cfg: dict, plan: dict, dc: HttpClient) -> dict:
    settings = cfg["decision_center"]
    spec = settings.get("operations", {}).get("baseline", {})
    result = operation(dc, settings, "baseline", variables(cfg, plan), read_only=True).json()
    app_source = cfg["application"]["decision_center"]
    for key, wanted in (("service_id", app_source["service_id"]), ("branch_id", app_source["branch_id"]),
                        ("baseline_id", plan["baseline_id"])):
        if str(pointer(result, spec[key + "_pointer"])) != str(wanted):
            raise OdmError("The selected baseline does not belong to this application's configured branch.")
    if spec.get("approved_pointer"):
        if canonical(pointer(result, spec["approved_pointer"])) != canonical(spec.get("approved_value", True)):
            raise OdmError("The selected baseline is not approved for deployment.")
    elif settings.get("baseline_selection_is_approval") is not True:
        raise OdmError("The baseline approval rule is not configured.")
    if settings.get("baseline_is_immutable") is not True:
        raise OdmError("The API contract does not confirm that the selected baseline is immutable.")
    return result


def build_release(cfg: dict, plan: dict, base: Path) -> bytes:
    settings = cfg["decision_center"]
    if settings.get("contracts_verified") is not True:
        raise OdmError("Decision Center API operations have not been verified for this installation.")
    dc = client(settings, cfg)
    for key in ("service_id", "branch_id", "deployment_id"):
        clean_setting(cfg["application"]["decision_center"].get(key), "The Decision Center source identifier")
    # A verified operation must visibly bind content to the selected immutable
    # baseline. A branch-head download must never masquerade as a release build.
    source_ops = ["export_baseline", "download_ruleapp"]
    output = settings.get("output", {})
    if not output.get("xom_member") and not output.get("xom_in_ruleapp_member"):
        source_ops.append("download_xom")
    for name in source_ops:
        spec = settings.get("operations", {}).get(name, {})
        selectors = str(spec.get("path", "")) + json.dumps(spec.get("params", {}))
        if "{baseline_id}" not in selectors:
            raise OdmError(f"The {name} operation is not bound to the selected baseline.")
    before = verify_baseline(cfg, plan, dc)
    values = variables(cfg, plan)
    source = operation(dc, settings, "export_baseline", values).body
    inspect_zip(source)
    if "build" in settings.get("operations", {}):
        spec = settings["operations"]["build"]
        if not spec.get("success_assertions"):
            raise OdmError("The Decision Center build completion check is missing.")
        operation(dc, settings, "build", values, b"")
    downloaded = operation(dc, settings, "download_ruleapp", values).body
    outer = inspect_zip(downloaded) if output.get("ruleapp_member") or output.get("xom_member") else {}
    if output.get("ruleapp_member"):
        try:
            jar = outer[output["ruleapp_member"]]
        except KeyError as exc:
            raise OdmError("The build output does not contain the configured RuleApp member.") from exc
    else:
        jar = downloaded
    check_jar(jar, cfg["application"])
    if output.get("xom_member"):
        try:
            xom = outer[output["xom_member"]]
        except KeyError as exc:
            raise OdmError("The build output does not contain the configured XOM member.") from exc
    elif output.get("xom_in_ruleapp_member"):
        try:
            xom = inspect_zip(jar)[output["xom_in_ruleapp_member"]]
        except KeyError as exc:
            raise OdmError("The RuleApp does not contain the configured XOM member.") from exc
    else:
        xom = operation(dc, settings, "download_xom", values).body
    inspect_zip(xom)
    after = verify_baseline(cfg, plan, dc)
    revision = settings["operations"]["baseline"].get("revision_pointer")
    if revision and pointer(before, revision) != pointer(after, revision):
        raise OdmError("The baseline changed while the release was being built.")
    files = {"ruleapp.jar": jar, "xom.zip": xom, "source.zip": source}
    tests = []
    for index, case in enumerate(cfg["application"]["rulesets"]):
        request, expected = (base / case["request_file"]).read_bytes(), (base / case["expected_file"]).read_bytes()
        fixture(request)
        fixture(expected, True)
        request_name, expected_name = f"tests/{index}-request.json", f"tests/{index}-expected.json"
        files[request_name], files[expected_name] = request, expected
        tests.append({"name": case["name"], "version": case["version"], "request": request_name,
                      "expected": expected_name, "description_path": clean_setting(case.get("description_path"), "The service description path")})
    source_git = save_baseline(cfg, plan, source)
    app = cfg["application"]
    manifest = {"schema_version": 1, "app": plan["app"], "jar_version": plan["jar_version"],
                "source": {**app["decision_center"], "baseline_id": plan["baseline_id"], **source_git},
                "target": {k: app[k] for k in ("ruleapp_name", "ruleapp_version", "xom_name", "library_name", "library_version")},
                "automation_sha256": plan["automation_sha256"], "created_at": now(), "tests": tests,
                "files": {name: sha256_bytes(data) for name, data in files.items()}}
    files["manifest.json"] = canonical(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])
    return buffer.getvalue()


def prepare(cfg: dict, plan: dict, plan_path: Path, base: Path) -> dict:
    nexus = Nexus(cfg)
    data = nexus.get(plan["release_path"], missing=True)
    if data is None:
        if not plan["needs_build"]:
            raise OdmError("The selected release was not found in Nexus.")
        data = build_release(cfg, plan, base)
        package_files(data, cfg, plan)
        nexus.put(plan["release_path"], data)
        data = nexus.get(plan["release_path"])
    assert data is not None
    manifest, _ = package_files(data, cfg, plan)
    if plan["needs_build"]:
        expected_source = {**cfg["application"]["decision_center"], "baseline_id": plan["baseline_id"]}
        if any(manifest.get("source", {}).get(key) != value for key, value in expected_source.items()):
            raise OdmError("This version already exists for a different source or baseline. Choose a new version.")
    package_hash = sha256_bytes(data)
    check_eligibility(cfg, plan, nexus, package_hash)
    private = plan_path.parent / "private" / plan["run_id"]
    private.mkdir(parents=True, exist_ok=True)
    (private / "release.zip").write_bytes(data)
    plan.update(phase="prepared", release_sha256=package_hash,
                package_file=str((private / "release.zip").resolve()))
    plan["plan_hash"] = plan_hash(plan)
    atomic_json(plan_path, plan)
    return plan


class RES:
    def __init__(self, cfg: dict, plan: dict):
        self.cfg, self.plan, self.settings = cfg, plan, cfg["res"]
        if self.settings.get("contracts_verified") is not True:
            raise OdmError("RES API operations have not been verified for this installation.")
        ops = self.settings.get("operations", {})
        if (ops.get("ruleapp_publish", {}).get("method") != "POST"
                or ops.get("ruleapp_publish", {}).get("params", {}).get("merging") != "REPLACE_MERGING_POLICY"
                or ops.get("library_update", {}).get("method") != "PUT"
                or ops.get("xom_publish", {}).get("method") != "POST"):
            raise OdmError("The RES write contracts do not match the reviewed replacement strategy.")
        self.http = client(self.settings, cfg)
        self.values = variables(cfg, plan)

    def op(self, name: str, data: bytes | None = None, **extra: str):
        try:
            return operation(self.http, self.settings, name, {**self.values, **extra}, data)
        except OdmError as exc:
            # A management write may still be running after the connection is
            # lost. Restoring immediately could race that unfinished write.
            if exc.ambiguous and name in {"library_update", "ruleapp_publish"}:
                exc.management_write_unknown = True
            raise

    def library_content(self) -> str:
        spec = self.settings["operations"]["library_detail"]
        value = pointer(self.op("library_detail").json(), spec["content_pointer"])
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            value = ",".join(value)
        if not isinstance(value, str) or not value.strip():
            raise OdmError("The existing XOM library has no valid resource reference.")
        return value.strip()

    def xom_versions(self) -> list[str]:
        spec = self.settings["operations"]["xom_versions"]
        items = pointer(self.op("xom_versions").json(), spec.get("versions_pointer", ""))
        if not isinstance(items, list) or len(items) > 10000:
            raise OdmError("RES returned an invalid XOM version list.")
        result = []
        for item in items:
            version = item.get(spec.get("version_key", "version")) if isinstance(item, dict) else item
            if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+", version):
                raise OdmError("RES returned an invalid XOM version.")
            result.append(version)
        return sorted(set(result), key=lambda v: tuple(map(int, v.split("."))))

    def xom(self, version: str) -> bytes:
        data = self.op("xom_archive", xom_version=version).body
        inspect_zip(data)
        return data

    def select_xom(self, data: bytes) -> str:
        wanted = canonical_zip_digest(data)
        for version in reversed(self.xom_versions()):
            if canonical_zip_digest(self.xom(version)) == wanted:
                return version
        spec = self.settings["operations"]["xom_publish"]
        if spec.get("preserves_existing") is not True:
            raise OdmError("The XOM upload contract does not preserve existing resource versions.")
        try:
            self.op("xom_publish", data)
        except OdmError as exc:
            if not exc.ambiguous:
                raise
            # A write may have completed. Inspect without issuing another POST.
        for version in reversed(self.xom_versions()):
            if canonical_zip_digest(self.xom(version)) == wanted:
                return version
        raise OdmError("RES did not confirm the new XOM resource. Check RES before trying again.")

    def verify_rules(self) -> None:
        app = self.cfg["application"]
        expected_uri = f"reslib://{app['library_name']}/{app['library_version']}"
        spec = self.settings["operations"]["ruleset_detail"]
        for case in app["rulesets"]:
            result = self.op("ruleset_detail", ruleset_name=case["name"], ruleset_version=case["version"]).json()
            actual = pointer(result, spec["managed_xom_pointer"])
            if actual != expected_uri and actual != [expected_uri]:
                raise OdmError("A ruleset does not reference the expected XOM library.")
            if spec.get("enabled_pointer") and canonical(pointer(result, spec["enabled_pointer"])) != canonical(spec.get("enabled_value", True)):
                raise OdmError("An expected ruleset is disabled.")

    def smoke(self, cases: list[dict], files: dict[str, bytes]) -> list[dict]:
        reports = []
        urls = self.settings.get("execution_urls", [])
        if not urls:
            raise OdmError("The rule execution URLs are not configured.")
        for url in urls:
            endpoint = client({**self.settings, "url": url}, self.cfg)
            for case in cases:
                values = {**self.values, "ruleset_name": case["name"], "ruleset_version": case["version"]}
                path = "/".join(quote(str(values[k]), safe="") for k in ("ruleapp_name", "ruleapp_version", "ruleset_name", "ruleset_version"))
                description = endpoint.request("GET", render(case["description_path"], values, True),
                                               expected=(200,), max_bytes=8 * 1024 * 1024).json()
                if not isinstance(description, dict) or not (description.get("openapi") or description.get("swagger")):
                    raise OdmError("The server did not return a valid service description.")
                request = fixture(files[case["request"]])
                expected = fixture(files[case["expected"]], True)
                response = endpoint.request("POST", path, json_body=request,
                                            expected=(200,), max_bytes=8 * 1024 * 1024).json()
                for assertion in expected["assertions"]:
                    if canonical(pointer(response, assertion["pointer"])) != canonical(assertion["equals"]):
                        raise OdmError("The deployed rules returned an unexpected business result.")
                reports.append({"ruleset": case["name"], "version": case["version"],
                                "execution_url": url, "passed": True})
        return reports


def fallback_tests(cfg: dict, base: Path) -> tuple[list[dict], dict[str, bytes]]:
    cases, files = [], {}
    for index, case in enumerate(cfg["application"]["rulesets"]):
        for key, suffix, expected in (("recovery_request_file", "request", False), ("recovery_expected_file", "expected", True)):
            name = f"recovery/{index}-{suffix}.json"
            filename = clean_setting(case.get(key), "The previous release verification file")
            data = (base / filename).read_bytes()
            fixture(data, expected)
            files[name] = data
        cases.append({"name": case["name"], "version": case["version"],
                      "request": f"recovery/{index}-request.json", "expected": f"recovery/{index}-expected.json",
                      "description_path": clean_setting(case.get("description_path"), "The service description path")})
    return cases, files


def backup(res: RES, nexus: Nexus, cfg: dict, plan: dict, base: Path, directory: Path) -> dict:
    jar = res.op("ruleapp_archive").body
    content_hash = check_jar(jar, cfg["application"])
    library = res.library_content()
    name = cfg["application"]["xom_name"]
    match = re.fullmatch(r"resuri://" + re.escape(name) + r"/(\d+\.\d+)", library)
    if not match:
        raise OdmError("This library has additional or unknown dependencies. Verify its recovery procedure first.")
    version = match[1]
    xom = res.xom(version)
    res.verify_rules()
    cases, files = None, None
    records = sorted(nexus.records(plan["track"], plan["app"]), key=lambda r: r.get("completed_at", ""), reverse=True)
    for record in records:
        if (record.get("status") == "succeeded" and record.get("res_url") == plan["res_url"]
                and record.get("ruleapp_content_sha256") == content_hash
                and record.get("library_content") == library
                and record.get("xom_content_sha256") == canonical_zip_digest(xom)):
            prior = nexus.get(record["release_path"])
            if prior is None or sha256_bytes(prior) != record["release_sha256"]:
                raise OdmError("The previous release package checksum does not match its deployment record.")
            old_plan = {**plan, "jar_version": record["jar_version"]}
            manifest, files = package_files(prior, cfg, old_plan)
            cases = manifest["tests"]
            break
    if cases is None:
        cases, files = fallback_tests(cfg, base)
    assert files is not None
    reports = res.smoke(cases, files)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ruleapp.jar").write_bytes(jar)
    (directory / "xom.zip").write_bytes(xom)
    for filename, content in files.items():
        target = directory / "verification" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    snapshot = {"library_content": library, "xom_version": version,
                "ruleapp_content_sha256": content_hash, "xom_content_sha256": canonical_zip_digest(xom),
                "tests": cases, "prechecks": reports, "jar": jar, "files": files,
                "verification_files": {name: sha256_bytes(content) for name, content in files.items()}}
    atomic_json(directory / "backup.json", {k: v for k, v in snapshot.items() if k not in {"jar", "files"}})
    return snapshot


def restore(res: RES, saved: dict) -> None:
    # The old XOM has never been deleted or overwritten by this workflow.
    if canonical_zip_digest(res.xom(saved["xom_version"])) != saved["xom_content_sha256"]:
        raise OdmError("The previous XOM resource changed. Recovery stopped.")
    res.op("library_update", saved["library_content"].encode())
    res.op("ruleapp_publish", saved["jar"])
    if res.library_content() != saved["library_content"]:
        raise OdmError("The previous library reference was not restored.")
    if canonical_zip_digest(res.op("ruleapp_archive").body) != saved["ruleapp_content_sha256"]:
        raise OdmError("The previous RuleApp content was not restored.")
    res.verify_rules()
    res.smoke(saved["tests"], saved["files"])


def recovery_package(directory: Path) -> bytes:
    """Make the verified backup durable before changing the active library."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise OdmError("The recovery folder contains an unsafe file link.")
            if path.is_file():
                info = zipfile.ZipInfo(path.relative_to(directory).as_posix(), (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    result = output.getvalue()
    inspect_zip(result)
    return result


def execute(cfg: dict, plan: dict, plan_path: Path, base: Path, result_path: Path) -> dict:
    if os.environ.get("ODM_TARGET_LOCK") != plan["lock_name"]:
        raise OdmError("The target deployment lock is not held by Jenkins.")
    if plan["requires_approval"] and (os.environ.get("ODM_APPROVED_PLAN_HASH") != plan["plan_hash"] or not os.environ.get("ODM_APPROVER")):
        raise OdmError("Production approval is missing or does not match this deployment plan.")
    package = Path(plan["package_file"]).read_bytes()
    if sha256_bytes(package) != plan["release_sha256"]:
        raise OdmError("The release checksum does not match. Deployment stopped.")
    manifest, files = package_files(package, cfg, plan)
    nexus = Nexus(cfg)
    # Recheck immutable bytes and predecessor evidence under the target lock.
    remote = nexus.get(plan["release_path"])
    if remote is None or sha256_bytes(remote) != plan["release_sha256"]:
        raise OdmError("The Nexus release changed after preparation. Deployment stopped.")
    check_eligibility(cfg, plan, nexus, plan["release_sha256"])
    inflight_path = f"inflight/{plan['track']}/{plan['app']}.json"
    existing = nexus.get(inflight_path, records=True, missing=True)
    if existing is not None:
        try:
            state = json.loads(existing)
        except (ValueError, UnicodeError) as exc:
            raise OdmError("The previous deployment state cannot be read. Check RES before continuing.") from exc
        if not isinstance(state, dict) or state.get("status") != "finished":
            raise OdmError("An earlier deployment has an unfinished result. Check RES and reconcile its state before continuing.")
    res = RES(cfg, plan)
    private = plan_path.parent / "private" / plan["run_id"] / "backup"
    saved = backup(res, nexus, cfg, plan, base, private)
    recovery = recovery_package(private)
    recovery_path = f"backups/{plan['track']}/{plan['app']}/{plan['run_id']}.zip"
    nexus.put(recovery_path, recovery, records=True)
    result = {"schema_version": 1, "app": plan["app"], "track": plan["track"],
              "jar_version": plan["jar_version"], "res_url": plan["res_url"],
              "release_path": plan["release_path"], "release_sha256": plan["release_sha256"],
              "plan_hash": plan["plan_hash"], "automation_sha256": plan["automation_sha256"],
              "run_id": plan["run_id"], "build_url": plan["build_url"],
              "action": plan["action"], "approver": os.environ.get("ODM_APPROVER", ""),
              "previous_ruleapp_content_sha256": saved["ruleapp_content_sha256"],
              "recovery_package_path": recovery_path, "recovery_package_sha256": sha256_bytes(recovery),
              "started_at": now(), "status": "started"}
    save_result(result_path, result, required=True)
    nexus.put(inflight_path, canonical({**result, "status": "started"}), records=True, mutable=True)
    runtime_mutated = False
    try:
        xom_version = res.select_xom(files["xom.zip"])
        library = f"resuri://{cfg['application']['xom_name']}/{xom_version}"
        # Set before sending the write: an absent response cannot prove that
        # the library remained unchanged.
        runtime_mutated = True
        res.op("library_update", library.encode())
        if res.library_content() != library:
            raise OdmError("The XOM library reference was not updated.")
        res.op("ruleapp_publish", files["ruleapp.jar"])
        if res.library_content() != library:
            raise OdmError("RuleApp publication changed the XOM library unexpectedly. Check the archive packaging and API contract.")
        if canonical_zip_digest(res.xom(saved["xom_version"])) != saved["xom_content_sha256"]:
            raise OdmError("RuleApp publication changed a previous XOM resource. Check the archive packaging and API contract.")
        deployed = res.op("ruleapp_archive").body
        if canonical_zip_digest(deployed) != canonical_zip_digest(files["ruleapp.jar"]):
            raise OdmError("The deployed RuleApp content does not match the selected release.")
        res.verify_rules()
        reports = res.smoke(manifest["tests"], files)
        result.update(status="succeeded", completed_at=now(), xom_version=xom_version, library_content=library,
                      xom_content_sha256=canonical_zip_digest(files["xom.zip"]),
                      ruleapp_content_sha256=canonical_zip_digest(deployed), tests=reports)
    except Exception as exc:
        original = str(exc) if isinstance(exc, OdmError) else "The deployment stopped unexpectedly."
        result.update(status="failed", completed_at=now(), error=original, recovery="started")
        save_result(result_path, result)
        try:
            if getattr(exc, "management_write_unknown", False):
                result["recovery"] = "inspection_required"
                message = "RES did not confirm the deployment. Check RES and reconcile its state before trying again."
            elif runtime_mutated:
                restore(res, saved)
                result["recovery"] = "succeeded"
                message = "Deployment failed. The previous release was restored and tested."
            else:
                result["recovery"] = "not_needed"
                message = "Deployment stopped before the active rules were changed. Check the deployment result."
        except Exception:
            result["recovery"] = "failed"
            message = "Deployment failed, and the previous release could not be restored. Check RES."
        save_result(result_path, result)
        try:
            nexus.put(f"receipts/{plan['track']}/{plan['app']}/{plan['jar_version']}/{plan['run_id']}.json", canonical(result), records=True)
        except OdmError:
            result["record_saved"] = False
            save_result(result_path, result)
        try:
            terminal = "finished" if result["recovery"] in {"succeeded", "not_needed"} else "needs_attention"
            nexus.put(inflight_path, canonical({**result, "status": terminal}), records=True, mutable=True)
        except OdmError:
            result["state_saved"] = False
            save_result(result_path, result)
        raise OdmError(message) from exc
    # A receipt failure must not trigger rollback of a deployment that passed.
    save_result(result_path, result)
    try:
        nexus.put(f"receipts/{plan['track']}/{plan['app']}/{plan['jar_version']}/{plan['run_id']}.json", canonical(result), records=True)
    except OdmError as exc:
        result.update(status="deployed_record_failed", record_saved=False)
        save_result(result_path, result)
        raise OdmError("Deployment passed, but its record could not be saved. Check RES before running again.") from exc
    result["record_saved"] = True
    save_result(result_path, result)
    try:
        nexus.put(inflight_path, canonical({**result, "status": "finished"}), records=True, mutable=True)
    except OdmError as exc:
        result.update(status="deployed_state_failed", state_saved=False)
        save_result(result_path, result)
        raise OdmError("Deployment passed, but its completion state could not be saved. Reconcile the result before running again.") from exc
    return result


def catalog(cfg: dict, plan: dict) -> dict:
    if os.environ.get("ODM_CATALOG_LOCK") != plan["catalog_lock_name"]:
        raise OdmError("The version catalog lock is not held by Jenkins.")
    nexus = Nexus(cfg)
    written = []
    # Publish only this job's target and direct successors for this APP.
    tracks = [name for name, item in cfg["tracks"].items() if name == plan["track"] or item.get("predecessor") == plan["track"]]
    for track in tracks:
        selected = resolve(cfg, track, plan["app"])
        for action, suffix in (("DEPLOY", ""), ("ROLLBACK", "-rollback")):
            selection = {**plan, "track": track, "action": action}
            records = eligible(selected, selection, nexus)
            versions: dict[str, str] = {}
            for record in records:
                version, checksum = record.get("jar_version", ""), record["release_sha256"]
                if not VERSION.fullmatch(version):
                    raise OdmError("A deployment record contains an invalid release version.")
                if version in versions and versions[version] != checksum:
                    raise OdmError("Deployment records disagree about a release checksum.")
                versions[version] = checksum
            value = {"schema_version": 1, "app": plan["app"], "track": track, "action": action,
                     "versions": [{"version": version, "sha256": versions[version]} for version in sorted(versions, reverse=True)]}
            path = f"catalogs/{track}/{plan['app']}{suffix}.json"
            nexus.put(path, canonical(value), records=True, mutable=True)
            written.append(path)
    return {"status": "succeeded", "catalogs": written}


def preflight(cfg: dict, plan: dict) -> dict:
    # Never call an export/download endpoint here: a GET can trigger a DC build.
    settings = cfg["res"]
    http = client(settings, cfg)
    values = variables(cfg, plan)
    for name in ("ruleapp_detail", "library_detail", "xom_versions"):
        operation(http, settings, name, values, read_only=True)
    if plan["needs_build"]:
        verify_baseline(cfg, plan, client(cfg["decision_center"], cfg))
    Nexus(cfg).http.request("GET", "service/rest/v1/search/assets",
                           params={"repository": cfg["nexus"]["release_repository"]}, expected=(200,))
    return {"status": "succeeded", "mode": "read_only", "app": plan["app"], "track": plan["track"],
            "note": "Read-only checks passed. Build, deployment and recovery still need DEV verification."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "preflight", "prepare", "execute", "catalog"))
    parser.add_argument("--config", type=Path, default=Path("config/odm.yaml"))
    parser.add_argument("--plan", type=Path, default=Path("output/deploy-plan.json"))
    parser.add_argument("--result", type=Path, default=Path("output/deploy-result.json"))
    args = parser.parse_args(argv)
    def interrupted(_number, _frame):
        raise OdmError("The deployment was interrupted.")
    # Jenkins normally sends SIGTERM before killing a timed-out process. Catch
    # that first signal so in-process recovery has a chance to run. Hard kills
    # and agent loss remain protected by the durable unfinished-run marker.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    try:
        base = args.config.resolve().parent.parent
        if args.command in {"plan", "preflight"}:
            cfg, plan = create_plan(args.config)
            if args.command == "plan":
                atomic_json(args.plan, plan)
                atomic_json(args.result, {"status": "planned", "run_id": plan["run_id"], "app": plan["app"],
                                          "track": plan["track"], "jar_version": plan["jar_version"]})
                print(f"Selected {plan['app']} {plan['jar_version']} for {plan['track']}: {plan['res_url']}")
            else:
                atomic_json(args.result, preflight(cfg, plan))
                print("Read-only API checks passed. No deployment was performed.")
            return 0
        cfg, plan = load_plan(args.config, args.plan, prepared=args.command in {"execute", "catalog"})
        if args.command == "prepare":
            plan = prepare(cfg, plan, args.plan, base)
            print(f"Prepared {plan['app']} {plan['jar_version']}. SHA-256: {plan['release_sha256']}")
        elif args.command == "execute":
            execute(cfg, plan, args.plan, base, args.result)
            print("Deployment passed and its result was recorded.")
        else:
            atomic_json(args.plan.parent / "catalog-result.json", catalog(cfg, plan))
            print("The eligible version lists were updated.")
        return 0
    except (OdmError, OSError, KeyError, TypeError, ValueError) as exc:
        message = str(exc) if isinstance(exc, OdmError) else "The deployment configuration or local files are incomplete. Check the setup guide."
        if args.command == "catalog":
            # The deployment already completed. A dropdown refresh failure
            # must not overwrite its successful result or erase its evidence.
            try:
                atomic_json(args.plan.parent / "catalog-result.json", {"status": "failed", "error": message})
            except (OSError, OdmError):
                pass
            print("The version list could not be refreshed. The deployment result is unchanged.", file=sys.stderr)
            return 1
        print(message, file=sys.stderr)
        # Preserve recovery details only for this exact run. Never archive a
        # success left by an earlier build when a new precondition check fails.
        current_run = locals().get("plan", {}).get("run_id")
        try:
            existing = read_json(args.result) if args.result.exists() else {}
        except OdmError:
            existing = {}
        preserve = (args.command == "execute" and current_run and existing.get("run_id") == current_run
                    and existing.get("status") in {"failed", "deployed_record_failed", "deployed_state_failed"})
        if not preserve:
            try:
                atomic_json(args.result, {"status": "failed", "run_id": current_run, "command": args.command, "error": message})
            except (OSError, OdmError):
                print("The result file could not be saved.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
