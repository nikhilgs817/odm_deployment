"""Small, shared I/O and safety helpers for the two ODM entry points.

Workflow decisions belong in deploy.py and sync.py. This module does not
choose a release, deploy an application, or change a Jenkins job.
"""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)
import zipfile


class OdmError(Exception):
    """A short operator-facing failure, without credentials or response data."""

    def __init__(self, message: str, *, status: int | None = None,
                 ambiguous: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


def _unique_mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise OdmError("Configuration field names must be text.")
        if key in result:
            raise OdmError("The configuration contains a repeated field.")
        result[key] = value
    return result


def load_config(path: str | Path) -> dict[str, Any]:
    """Read JSON-compatible YAML or ordinary YAML, rejecting duplicate keys."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise OdmError("The configuration file could not be read.") from exc
    if len(text) > 4 * 1024 * 1024:
        raise OdmError("The configuration file is too large.")
    try:
        value = json.loads(text, object_pairs_hook=_unique_mapping)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:
            raise OdmError("PyYAML is missing. Install the project requirements first.") from exc

        class UniqueLoader(yaml.SafeLoader):
            pass

        def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[str, Any]:
            # A YAML merge intentionally permits a local field to override an
            # inherited value. Reject duplicate *explicit* keys first, then let
            # SafeLoader apply standard merge precedence.
            explicit: set[str] = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = loader.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise OdmError("Configuration field names must be text.")
                if key in explicit:
                    raise OdmError("The configuration contains a repeated field.")
                explicit.add(key)
            loader.flatten_mapping(node)
            result: dict[str, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise OdmError("Configuration field names must be text.")
                result[key] = loader.construct_object(value_node, deep=deep)
            return result

        UniqueLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
        )
        try:
            value = yaml.load(text, Loader=UniqueLoader)
        except OdmError:
            raise
        except yaml.YAMLError as exc:
            raise OdmError("The configuration file is not valid YAML.") from exc
    if not isinstance(value, dict):
        raise OdmError("The configuration must contain named fields.")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise OdmError("The configuration schema version must be 1.")
    return value


def _merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(left))
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve(config: Mapping[str, Any], track: str, app: str | None = None,
            job_name: str | None = None) -> dict[str, Any]:
    """Resolve a configured track and optional app without making any requests.

    Root service and track configuration is retained; track fields override defaults.
    The selected app is returned under ``application``. ``apps`` retains the
    complete configured app mapping for sync. If job_name is supplied, it must
    match an explicitly configured job for this track.
    """
    tracks = config.get("tracks")
    if not isinstance(tracks, dict) or track not in tracks:
        raise OdmError("The deployment track is not configured.")
    selected = tracks[track]
    if not isinstance(selected, dict):
        raise OdmError("The deployment track configuration is invalid.")
    if job_name is not None:
        deployment_jobs = selected.get("deployment_jobs", [])
        sync_jobs = selected.get("sync_jobs", [])
        if not isinstance(deployment_jobs, list) or not isinstance(sync_jobs, list):
            raise OdmError("The Jenkins job configuration is invalid.")
        if job_name not in deployment_jobs + sync_jobs:
            raise OdmError("This Jenkins job is not allowed to use the selected track.")
    root = {key: value for key, value in config.items()
            if key != "defaults"}
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        raise OdmError("The default configuration is invalid.")
    result = _merge(root, defaults)
    result = _merge(result, {key: value for key, value in selected.items()
                             if key != "apps"})
    result["track"] = track
    apps = config.get("apps", {})
    if not isinstance(apps, dict):
        raise OdmError("The application configuration is invalid.")
    overrides = selected.get("apps", {})
    if not isinstance(overrides, dict):
        raise OdmError("The track application configuration is invalid.")
    if not all(isinstance(item, dict) for item in apps.values()):
        raise OdmError("The application configuration is invalid.")
    if not all(isinstance(item, dict) for item in overrides.values()):
        raise OdmError("The track application configuration is invalid.")
    result["apps"] = {name: _merge(item, overrides.get(name, {}))
                      for name, item in apps.items()}
    if app is not None:
        if app not in result["apps"]:
            raise OdmError("The application is not configured.")
        result["app"] = app
        result["application"] = result["apps"][app]
    return result


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OdmError("The server returned an invalid JSON response.") from exc


def _origin(url: str) -> tuple[str, str | None, int]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.hostname, parsed.port or (
        443 if parsed.scheme.lower() == "https" else 80
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int,
                         msg: str, headers: Any, newurl: str) -> None:
        # Reject all redirects: a login page or alternate artifact host must
        # never receive credentials or be mistaken for a successful archive.
        return None


class HttpClient:
    """Verified TLS, bounded responses, and retries only for read operations."""

    def __init__(self, base_url: str, username_env: str | None = None,
                 password_env: str | None = None, token_env: str | None = None,
                 timeout: float = 30, ca_bundle: str | None = None,
                 allow_http: bool = False, retries: int = 2) -> None:
        if type(allow_http) is not bool:
            raise OdmError("The allow_http setting must be true or false.")
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ({"https", "http"} if allow_http else {"https"})
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment
                or any(ord(char) <= 32 for char in base_url)):
            raise OdmError("The server URL must use HTTPS and contain no credentials or query.")
        try:
            parsed.port
        except ValueError as exc:
            raise OdmError("The server URL contains an invalid port.") from exc
        if timeout <= 0 or timeout > 3600 or retries < 0 or retries > 5:
            raise OdmError("The HTTP timeout or retry setting is invalid.")
        if bool(username_env) != bool(password_env) or (token_env and username_env):
            raise OdmError("The credential configuration is invalid.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.username_env = username_env
        self.password_env = password_env
        self.token_env = token_env
        try:
            context = ssl.create_default_context(cafile=ca_bundle)
        except (OSError, ssl.SSLError) as exc:
            raise OdmError("The trusted certificate file could not be loaded.") from exc
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))

    def _url(self, path: str, params: Mapping[str, Any] | None) -> str:
        parsed = urlsplit(path)
        if parsed.query or parsed.fragment or any(ord(char) <= 32 for char in path):
            raise OdmError("Pass request query values separately from the API path.")
        decoded = unquote(parsed.path)
        if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
            raise OdmError("The API path is invalid.")
        if parsed.scheme or parsed.netloc:
            if _origin(path) != _origin(self.base_url):
                raise OdmError("The request points to a different server.")
            base_path = urlsplit(self.base_url).path.rstrip("/")
            if parsed.path != base_path and not parsed.path.startswith(base_path + "/"):
                raise OdmError("The request is outside the configured API path.")
            url = path
        else:
            url = self.base_url + "/" + path.lstrip("/")
        if params:
            url += "?" + urlencode(params, doseq=True)
        return url

    def _authorization(self) -> str | None:
        if self.token_env:
            value = os.environ.get(self.token_env, "")
            if not value:
                raise OdmError("The server token is missing from Jenkins credentials.")
            if "\r" in value or "\n" in value:
                raise OdmError("The server token contains invalid characters.")
            return "Bearer " + value
        if self.username_env:
            username = os.environ.get(self.username_env, "")
            password = os.environ.get(self.password_env or "", "")
            if not username or not password:
                raise OdmError("The server username or password is missing from Jenkins credentials.")
            return "Basic " + base64.b64encode(
                (username + ":" + password).encode("utf-8")
            ).decode("ascii")
        return None

    def request(self, method: str, path: str, *,
                params: Mapping[str, Any] | None = None,
                data: bytes | None = None, json_body: Any = None,
                headers: Mapping[str, str] | None = None,
                expected: Sequence[int] = (200,),
                retry: bool = True,
                max_bytes: int = 256 * 1024 * 1024) -> Response:
        method = method.upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"}:
            raise OdmError("The HTTP request method is not supported.")
        if max_bytes <= 0:
            raise OdmError("The response size limit is invalid.")
        if data is not None and json_body is not None:
            raise OdmError("The request cannot contain two different bodies.")
        if data is not None and not isinstance(data, bytes):
            raise OdmError("The request body must contain bytes.")
        values = {"Accept": "application/json"}
        values.update(headers or {})
        if any(key.lower() in {"authorization", "proxy-authorization", "host"}
               for key in (headers or {})):
            raise OdmError("Authentication headers must come from Jenkins credentials.")
        authorization = self._authorization()
        if authorization:
            values["Authorization"] = authorization
        if json_body is not None:
            try:
                data = json.dumps(json_body, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise OdmError("The JSON request body is invalid.") from exc
            values["Content-Type"] = "application/json"
        url = self._url(path, params)
        attempts = self.retries + 1 if retry and method in {"GET", "HEAD"} else 1
        for attempt in range(attempts):
            request = Request(url, data=data, headers=values, method=method)
            try:
                with self.opener.open(request, timeout=self.timeout) as result:
                    status = result.status
                    response_headers = dict(result.headers.items())
                    if status not in expected:
                        raise OdmError(
                            f"The server returned an unexpected result for {method} (HTTP {status}).",
                            status=status,
                        )
                    size = result.headers.get("Content-Length")
                    try:
                        if size is not None and int(size) > max_bytes:
                            raise OdmError("The server response is larger than the allowed limit.")
                    except ValueError as exc:
                        raise OdmError("The server returned an invalid response size.") from exc
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = result.read(min(1024 * 1024, max_bytes - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise OdmError("The server response is larger than the allowed limit.")
                        chunks.append(chunk)
                    return Response(status, b"".join(chunks), response_headers)
            except HTTPError as exc:
                status = exc.code
                if status in expected:
                    # Callers may intentionally use 404/409 to reconcile state.
                    with exc:
                        body = exc.read(max_bytes + 1)
                        if len(body) > max_bytes:
                            raise OdmError("The server response is larger than the allowed limit.")
                        return Response(status, body, dict(exc.headers.items()))
                if status in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    exc.close()
                    time.sleep(min(2 ** attempt, 4))
                    continue
                exc.close()
                raise OdmError(
                    f"The server rejected the {method} request (HTTP {status}).",
                    status=status,
                    ambiguous=method not in {"GET", "HEAD"} and (status >= 500 or status == 408),
                ) from exc
            except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError, HTTPException) as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                if method not in {"GET", "HEAD"}:
                    raise OdmError(
                        f"The server did not confirm the {method} request. Check its state before trying again.",
                        ambiguous=True,
                    ) from exc
                raise OdmError(f"The {method} request could not reach the server.") from exc
        raise OdmError("The HTTP request did not complete.")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise OdmError("The file could not be read for checksum verification.") from exc
    return digest.hexdigest()


def atomic_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    temporary: str | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".odm-",
                                         suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except (OSError, TypeError, ValueError) as exc:
        raise OdmError("The result file could not be saved.") from exc
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink(missing_ok=True)


def read_json(path: str | Path, max_bytes: int = 4 * 1024 * 1024) -> Any:
    try:
        with Path(path).open("rb") as source:
            data = source.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise OdmError("The JSON file is larger than the allowed limit.")
        return json.loads(data.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OdmError("The JSON file could not be read.") from exc


_RESERVED_WINDOWS = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def _archive_name(name: str) -> str:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise OdmError("The ZIP contains an unsafe file path.")
    path = PurePosixPath(name)
    if not path.parts or any(part in {".", ".."} or ":" in part
           or any(char in '<>"|?*' or ord(char) < 32 for char in part)
           or part.rstrip(" .") != part
           or _RESERVED_WINDOWS.match(part) for part in path.parts):
        raise OdmError("The ZIP contains an unsafe file path.")
    # PurePosixPath normalizes repeated slashes and '.', so also reject them.
    if str(path) != name.rstrip("/"):
        raise OdmError("The ZIP contains an ambiguous file path.")
    return str(path)


def inspect_zip(data: bytes | str | Path, *, max_files: int = 10000,
                max_total_bytes: int = 512 * 1024 * 1024,
                max_file_bytes: int = 256 * 1024 * 1024) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    names: set[str] = set()
    total = 0
    try:
        source = io.BytesIO(data) if isinstance(data, bytes) else data
        with zipfile.ZipFile(source) as archive:
            if len(archive.infolist()) > max_files:
                raise OdmError("The ZIP contains too many files.")
            for info in archive.infolist():
                # ZipInfo normalizes separators and truncates NUL names on
                # some platforms. Validate the original central-directory name.
                name = _archive_name(info.orig_filename)
                key = name.casefold()
                if key in names:
                    raise OdmError("The ZIP contains repeated file paths.")
                names.add(key)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise OdmError("The ZIP contains a link or unsupported file type.")
                if info.flag_bits & 0x1:
                    raise OdmError("Encrypted ZIP files are not supported.")
                if info.is_dir():
                    continue
                if info.file_size > max_file_bytes or total + info.file_size > max_total_bytes:
                    raise OdmError("The ZIP is larger than the allowed limit.")
                with archive.open(info) as entry:
                    content = entry.read(max_file_bytes + 1)
                if len(content) != info.file_size or len(content) > max_file_bytes:
                    raise OdmError("The ZIP contains an invalid file size.")
                total += len(content)
                if total > max_total_bytes:
                    raise OdmError("The ZIP is larger than the allowed limit.")
                files[name] = content
        if not files:
            raise OdmError("The ZIP does not contain any files.")
        all_names = {name.casefold() for name in files}
        for name in files:
            parent = PurePosixPath(name).parent
            while str(parent) != ".":
                if str(parent).casefold() in all_names:
                    raise OdmError("The ZIP contains conflicting file paths.")
                parent = parent.parent
        return files
    except OdmError:
        raise
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise OdmError("The downloaded file is not a readable ZIP archive.") from exc


def safe_extract_zip(data: bytes | str | Path, destination: str | Path,
                     **limits: Any) -> dict[str, bytes]:
    files = inspect_zip(data, **limits)
    root = Path(destination)
    try:
        if root.is_symlink() or (root.exists() and any(root.iterdir())):
            raise OdmError("ZIP extraction requires an empty destination folder.")
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
        for name, content in files.items():
            target = root.joinpath(*PurePosixPath(name).parts)
            if not target.resolve().is_relative_to(resolved):
                raise OdmError("The ZIP contains an unsafe file path.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    except OSError as exc:
        raise OdmError("The ZIP could not be extracted.") from exc
    return files


def canonical_zip_digest(data: bytes | str | Path, **limits: Any) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(inspect_zip(data, **limits).items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def run_git(args: Sequence[str], cwd: str | Path, *,
            env: Mapping[str, str] | None = None, timeout: float = 120) -> str:
    """Run an argument-vector Git command; never put secrets in arguments."""
    try:
        result = subprocess.run(["git", *args], cwd=cwd,
                                env=dict(os.environ, **dict(env or {})),
                                text=True, encoding="utf-8", errors="replace",
                                capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OdmError("Git did not complete. Check the repository connection.") from exc
    if result.returncode:
        raise OdmError("Git failed. Check the repository access and branch state.")
    return result.stdout
