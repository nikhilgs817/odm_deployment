from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch
import zipfile

from odm_common import (
    HttpClient, OdmError, Response, atomic_json, canonical_zip_digest,
    inspect_zip, load_config, read_json, resolve, safe_extract_zip,
    sha256_bytes, sha256_file,
)


def make_zip(items, date=(2020, 1, 1, 0, 0, 0)):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in items:
            item = zipfile.ZipInfo(name, date)
            # ZipInfo normalizes Windows separators when constructed. Restore
            # the raw input so the unsafe-name test exercises a hostile ZIP.
            item.filename = name
            archive.writestr(item, content)
    return output.getvalue()


@contextmanager
def server():
    class Handler(BaseHTTPRequestHandler):
        calls = {}
        last_authorization = None
        last_path = None

        def log_message(self, format, *args):
            pass

        def handle_request(self):
            route = self.path.split("?", 1)[0]
            type(self).calls[route] = type(self).calls.get(route, 0) + 1
            type(self).last_authorization = self.headers.get("Authorization")
            type(self).last_path = self.path
            count = type(self).calls[route]
            if route.endswith("/lost"):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if route.endswith("/retry") and count < 3:
                status, body = 503, b"sensitive-error-content"
            elif route.endswith("/error"):
                status, body = 403, b"sensitive-error-content"
            elif route.endswith("/missing"):
                status, body = 404, b"missing"
            elif route.endswith("/logical"):
                status, body = 202, b'{"error":"not deployed"}'
            elif route.endswith("/redirect"):
                status, body = 302, b""
            elif route.endswith("/large"):
                status, body = 200, b"0123456789"
            else:
                status, body = 200, b'{"ok":true}'
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "http://127.0.0.1:1/leak")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = handle_request
        do_POST = handle_request
        do_PUT = handle_request
        do_HEAD = handle_request

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    instance.daemon_threads = True
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}/api", Handler
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


class HttpTests(unittest.TestCase):
    def test_tls_and_credential_url_rules(self):
        for value in ["http://example.com", "https://user:secret@example.com", "https://example.com?q=x", "https://example.com:bad"]:
            with self.subTest(value=value), self.assertRaises(OdmError):
                HttpClient(value)
        HttpClient("https://example.com/api")

    def test_reads_retry_but_mutations_never_retry(self):
        with server() as (url, handler), patch("odm_common.time.sleep"):
            client = HttpClient(url, allow_http=True)
            self.assertEqual(client.request("GET", "retry").json(), {"ok": True})
            self.assertEqual(handler.calls["/api/retry"], 3)
            handler.calls.clear()
            with self.assertRaises(OdmError) as failure:
                client.request("POST", "retry", data=b"jar")
            self.assertEqual(handler.calls["/api/retry"], 1)
            self.assertEqual(failure.exception.status, 503)
            self.assertTrue(failure.exception.ambiguous)

    def test_lost_write_response_has_ambiguous_result(self):
        with server() as (url, handler):
            client = HttpClient(url, allow_http=True)
            with self.assertRaises(OdmError) as failure:
                client.request("PUT", "lost", data=b"jar")
            self.assertTrue(failure.exception.ambiguous)
            self.assertEqual(handler.calls["/api/lost"], 1)

    def test_build_triggering_get_can_disable_read_retries(self):
        with server() as (url, handler), patch("odm_common.time.sleep"):
            with self.assertRaises(OdmError) as failure:
                HttpClient(url, allow_http=True).request("GET", "retry", retry=False)
            self.assertEqual(failure.exception.status, 503)
            self.assertEqual(handler.calls["/api/retry"], 1)

    def test_redirect_does_not_forward_credentials(self):
        with server() as (url, handler), patch.dict(os.environ, {"TEST_USER": "u", "TEST_PASS": "secret"}):
            client = HttpClient(url, "TEST_USER", "TEST_PASS", allow_http=True)
            with self.assertRaises(OdmError) as failure:
                client.request("GET", "redirect")
            self.assertEqual(failure.exception.status, 302)
            self.assertEqual(handler.calls, {"/api/redirect": 1})

    def test_response_limit_and_operation_status_are_enforced(self):
        with server() as (url, _):
            client = HttpClient(url, allow_http=True)
            with self.assertRaisesRegex(OdmError, "larger"):
                client.request("GET", "large", max_bytes=4)
            with self.assertRaises(OdmError) as failure:
                client.request("POST", "logical", data=b"jar")
            self.assertEqual(failure.exception.status, 202)
            result = client.request("GET", "missing", expected=(200, 404))
            self.assertEqual(result.status, 404)

    def test_errors_do_not_include_response_data_or_secrets(self):
        with server() as (url, _), patch.dict(os.environ, {"TEST_USER": "u", "TEST_PASS": "secret"}):
            client = HttpClient(url, "TEST_USER", "TEST_PASS", allow_http=True)
            with self.assertRaises(OdmError) as failure:
                client.request("GET", "error")
            message = str(failure.exception)
            self.assertNotIn("secret", message)
            self.assertNotIn("sensitive-error-content", message)

    def test_url_paths_cannot_escape_base_or_change_origin(self):
        client = HttpClient("https://example.com/api")
        for path in ["../admin", "%2e%2e/admin", "https://other.example/api/x", "https://example.com/admin", "//other.example/x"]:
            with self.subTest(path=path), self.assertRaises(OdmError):
                client.request("GET", path)

    def test_query_encoding_and_basic_auth_from_environment(self):
        with server() as (url, handler), patch.dict(os.environ, {"TEST_USER": "user", "TEST_PASS": "secret"}):
            result = HttpClient(url, "TEST_USER", "TEST_PASS", allow_http=True).request(
                "GET", "/ok", params={"id": "A&B C"}
            )
            self.assertTrue(result.json()["ok"])
            self.assertEqual(handler.last_path, "/api/ok?id=A%26B+C")
            self.assertEqual(handler.last_authorization, "Basic dXNlcjpzZWNyZXQ=")

    def test_invalid_json_response_and_missing_credentials(self):
        with self.assertRaisesRegex(OdmError, "JSON"):
            Response(200, b"not json", {}).json()
        with patch.dict(os.environ, {}, clear=True):
            client = HttpClient("https://example.com", "MISSING_USER", "MISSING_PASS")
            with self.assertRaisesRegex(OdmError, "missing"):
                client.request("GET", "resource")


class ArchiveTests(unittest.TestCase):
    def test_safe_contents_and_metadata_independent_digest(self):
        first = make_zip([("rules/a.xml", b"a \n"), ("rules/b.xml", b"b")])
        second = make_zip([("rules/b.xml", b"b"), ("rules/a.xml", b"a \n")], (2025, 1, 2, 3, 4, 6))
        self.assertEqual(canonical_zip_digest(first), canonical_zip_digest(second))
        changed = make_zip([("rules/a.xml", b"a\n"), ("rules/b.xml", b"b")])
        self.assertNotEqual(canonical_zip_digest(first), canonical_zip_digest(changed))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            root = Path(temporary) / "export"
            safe_extract_zip(first, root)
            self.assertEqual((root / "rules" / "a.xml").read_bytes(), b"a \n")
            with self.assertRaisesRegex(OdmError, "empty destination"):
                safe_extract_zip(first, root)

    def test_unsafe_and_ambiguous_names_are_rejected(self):
        for name in [".", "../outside", "/absolute", "C:/drive", "folder\\evil", "a/../evil", "a//b", "a/./b", "a/b:stream", "NUL.txt", "a/file.", "file?.xml", "file\n.xml"]:
            with self.subTest(name=name), self.assertRaises(OdmError):
                inspect_zip(make_zip([(name, b"x")]))

    def test_duplicate_and_conflicting_paths_are_rejected(self):
        for items in [[("A.txt", b"a"), ("a.txt", b"b")], [("A", b"a"), ("a/b", b"b")]]:
            with self.assertRaises(OdmError):
                inspect_zip(make_zip(items))

    def test_symlinks_limits_and_empty_archives_are_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../outside")
        with self.assertRaisesRegex(OdmError, "link"):
            inspect_zip(output.getvalue())
        data = make_zip([("one", b"123"), ("two", b"456")])
        for limits in [{"max_files": 1}, {"max_file_bytes": 2}, {"max_total_bytes": 5}]:
            with self.assertRaises(OdmError):
                inspect_zip(data, **limits)
        with self.assertRaises(OdmError):
            inspect_zip(make_zip([]))
        with self.assertRaises(OdmError):
            inspect_zip(b"an HTML login page")


class ConfigurationTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("yaml"), "Install requirements for YAML parser tests.")
    def test_yaml_merges_allow_inherited_override_but_not_explicit_duplicates(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text("schema_version: 1\nsettings: &common\n  url: https://dev.test\n  timeout: 30\ntrack:\n  <<: *common\n  url: https://prod.test\n", encoding="utf-8")
            loaded = load_config(path)
            self.assertEqual(loaded["track"], {"url": "https://prod.test", "timeout": 30})
            path.write_text("schema_version: 1\ntrack:\n  url: https://dev.test\n  url: https://prod.test\n", encoding="utf-8")
            with self.assertRaisesRegex(OdmError, "repeated"):
                load_config(path)

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "Install requirements for YAML parser tests.")
    def test_shipped_yaml_template_loads_with_aliases(self):
        path = Path(__file__).resolve().parents[1] / "config" / "odm.yaml"
        loaded = load_config(path)
        self.assertEqual(loaded["schema_version"], 1)
        self.assertEqual(set(loaded["apps"]), {"ESERV", "WACON"})
        self.assertEqual(loaded["tracks"]["int"]["res"]["operations"], loaded["tracks"]["dev"]["res"]["operations"])

    def test_json_config_schema_and_duplicates(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text('{"schema_version":1,"tracks":{}}', encoding="utf-8")
            self.assertEqual(load_config(path)["schema_version"], 1)
            path.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
            with self.assertRaisesRegex(OdmError, "repeated"):
                load_config(path)
            path.write_text('{"schema_version":true}', encoding="utf-8")
            with self.assertRaisesRegex(OdmError, "schema version"):
                load_config(path)

    def test_resolution_is_track_and_app_specific(self):
        config = {
            "schema_version": 1,
            "apps": {"ESERV": {"ruleapp_name": "eserv", "nested": {"a": 1}}},
            "tracks": {
                "dev": {"res": {"url": "https://dev.example/res"}, "deployment_jobs": ["deploy-dev"], "sync_jobs": ["sync-dev"], "apps": {"ESERV": {"nested": {"b": 2}}}},
                "prod": {"res": {"url": "<PROD_URL>"}},
            },
        }
        result = resolve(config, "dev", "ESERV", "deploy-dev")
        self.assertEqual(result["application"]["nested"], {"a": 1, "b": 2})
        self.assertIn("prod", result["tracks"])
        result["application"]["nested"]["a"] = 99
        self.assertEqual(config["apps"]["ESERV"]["nested"]["a"], 1)
        resolve(config, "dev", "ESERV", "sync-dev")
        for track, app, job in [("missing", "ESERV", None), ("dev", "WACON", None), ("dev", "ESERV", "deploy-prod")]:
            with self.assertRaises(OdmError):
                resolve(config, track, app, job)

    def test_atomic_results_and_file_checksums(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            path = Path(temporary) / "results" / "plan.json"
            atomic_json(path, {"app": "ESERV", "version": "2026.10.25.1"})
            self.assertEqual(read_json(path)["app"], "ESERV")
            self.assertEqual(sha256_file(path), sha256_bytes(path.read_bytes()))
            self.assertEqual(list(path.parent.glob(".odm-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
