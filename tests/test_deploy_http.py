"""End-to-end transport tests against a simulated contract, not IBM servers."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from deployment import deploy
from odm_common import OdmError, atomic_json, read_json, sha256_bytes


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for name, data in files.items():
            output.writestr(name, data)
    return buffer.getvalue()


def ruleapp(rule):
    return archive({"META-INF/archive.xml": '<ruleapp name="ESERVRuleApp" version="1.0"><ruleset name="Decision" version="1.0" /></ruleapp>',
                    "rules/decision.bin": rule})


class Simulation:
    def __init__(self):
        self.old_jar, self.new_jar = ruleapp("old"), ruleapp("new")
        self.old_xom, self.new_xom = archive({"Main.class": b"old-class"}), archive({"Main.class": b"new-class"})
        self.source = archive({"rules/decision.xml": "<rule>new</rule>"})
        self.targets = {track: {"jar": self.old_jar, "library": "resuri://ESERV_XOM.zip/1.0",
                               "xoms": {"1.0": self.old_xom}} for track in ("dev", "int", "prod")}
        self.store, self.calls = {}, []
        self.fail_new_test = False
        self.fail_receipt = False

    def handle(self, method, path, query, body):
        self.calls.append((method, path))
        if path.startswith("/nexus/repository/"):
            key = path[len("/nexus/repository/"):]
            if method == "GET":
                return (200, self.store[key]) if key in self.store else (404, b"missing")
            if method == "PUT":
                if self.fail_receipt and "/receipts/" in path:
                    return 503, b"simulated receipt failure"
                self.store[key] = body
                return 201, b""
        if path == "/nexus/service/rest/v1/search/assets":
            repo = query["repository"][0]
            return 200, {"items": [{"path": key[len(repo) + 1:]} for key in self.store if key.startswith(repo + "/")],
                         "continuationToken": None}
        if path == "/dc/baselines/b1":
            return 200, {"id": "b1", "service": "s1", "branch": "main", "approved": True, "revision": "r1"}
        if path in {"/dc/export", "/dc/ruleapp", "/dc/xom"}:
            if query.get("baseline") != ["b1"]:
                return 400, b"baseline missing"
            return 200, {"/dc/export": self.source, "/dc/ruleapp": self.new_jar, "/dc/xom": self.new_xom}[path]
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "res":
            state = self.targets[parts[1]]
            resource = parts[2]
            if resource == "ruleapp":
                if method == "POST":
                    state["jar"] = body
                    return 200, {"ok": True}
                return 200, {"name": "ESERVRuleApp"}
            if resource == "archive":
                return 200, state["jar"]
            if resource == "library":
                if method == "PUT":
                    state["library"] = body.decode()
                return 200, {"content": state["library"]}
            if resource == "ruleset":
                return 200, {"managedXom": "reslib://ESERV_library/1.0", "enabled": True}
            if resource == "xoms":
                if method == "POST":
                    version = "1." + str(len(state["xoms"]))
                    state["xoms"][version] = body
                    return 201, {"version": version}
                return 200, [{"version": v} for v in state["xoms"]]
            if resource == "xom":
                return (200, state["xoms"][parts[3]]) if parts[3] in state["xoms"] else (404, b"missing")
        if len(parts) >= 3 and parts[0] == "runtime":
            state = self.targets[parts[1]]
            if method == "GET":
                return 200, {"openapi": "3.0.1"}
            decision = "DENY" if state["jar"] == self.old_jar else ("BROKEN" if self.fail_new_test else "ALLOW")
            return 200, {"decision": decision}
        return 404, b"unconfigured fake endpoint"


@contextmanager
def simulated_server(simulation):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.serve()

        def do_POST(self):
            self.serve()

        def do_PUT(self):
            self.serve()

        def serve(self):
            url = urlsplit(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            status, response = simulation.handle(self.command, url.path, parse_qs(url.query), body)
            if not isinstance(response, bytes):
                response = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def configuration(url, base):
    fixtures = base / "fixtures"
    fixtures.mkdir()
    for name, value in {"request.json": {"case": "approved synthetic input"},
                        "expected.json": {"assertions": [{"pointer": "/decision", "equals": "ALLOW"}]},
                        "recovery-request.json": {"case": "approved synthetic input"},
                        "recovery-expected.json": {"assertions": [{"pointer": "/decision", "equals": "DENY"}]}}.items():
        atomic_json(fixtures / name, value)
    checks = [{"role": role, "xpath": xpath, "attribute": attribute, "equals": expected}
              for role, xpath, attribute, expected in (
                  ("ruleapp_name", ".", "name", "{ruleapp_name}"),
                  ("ruleapp_version", ".", "version", "{ruleapp_version}"),
                  ("ruleset_name", "ruleset", "name", "Decision"),
                  ("ruleset_version", "ruleset", "version", "1.0"))]
    app = {"artifact_id": "eserv-release", "ruleapp_name": "ESERVRuleApp", "ruleapp_version": "1.0",
           "xom_name": "ESERV_XOM.zip", "library_name": "ESERV_library", "library_version": "1.0",
           "archive_identity": {"member": "META-INF/archive.xml", "checks": checks},
           "decision_center": {"service_id": "s1", "branch_id": "main", "deployment_id": "d1"},
           "rulesets": [{"name": "Decision", "version": "1.0", "description_path": "{ruleapp_name}/{ruleapp_version}/{ruleset_name}/{ruleset_version}/openapi.json",
                         "request_file": "fixtures/request.json", "expected_file": "fixtures/expected.json",
                         "recovery_request_file": "fixtures/recovery-request.json", "recovery_expected_file": "fixtures/recovery-expected.json"}]}
    def op(method, path, **extra):
        return {"method": method, "path": path, "expected": [200, 201] if method in {"POST", "PUT"} else [200], **extra}
    tracks = {}
    for track, previous in (("dev", None), ("int", "dev"), ("prod", "int")):
        tracks[track] = {"build_release": track == "dev", "predecessor": previous,
            "require_approval": track == "prod", "approval_submitters": "release-team",
            "deployment_jobs": ["deploy-" + track], "sync_jobs": [],
            "res": {"url": url + "/res/" + track, "execution_urls": [url + "/runtime/" + track],
                    "contracts_verified": True, "operations": {
                        "ruleapp_detail": op("GET", "ruleapp"), "ruleapp_archive": op("GET", "archive"),
                        "library_detail": op("GET", "library", content_pointer="/content"),
                        "library_update": op("PUT", "library", content_type="text/plain"),
                        "ruleset_detail": op("GET", "ruleset", managed_xom_pointer="/managedXom", enabled_pointer="/enabled"),
                        "xom_versions": op("GET", "xoms", versions_pointer=""),
                        "xom_archive": op("GET", "xom/{xom_version}"),
                        "xom_publish": op("POST", "xoms", preserves_existing=True),
                        "ruleapp_publish": op("POST", "ruleapp", params={"merging": "REPLACE_MERGING_POLICY"})}},
            "decision_center": {"url": url + "/dc", "contracts_verified": True, "baseline_is_immutable": True,
                "operations": {"baseline": op("GET", "baselines/{baseline_id}", baseline_id_pointer="/id", service_id_pointer="/service", branch_id_pointer="/branch", approved_pointer="/approved", revision_pointer="/revision"),
                               "export_baseline": op("GET", "export", params={"baseline": "{baseline_id}"}),
                               "download_ruleapp": op("GET", "ruleapp", params={"baseline": "{baseline_id}"}),
                               "download_xom": op("GET", "xom", params={"baseline": "{baseline_id}"})}}}
    return {"schema_version": 1, "http": {"allow_http": True, "timeout_seconds": 2},
            "apps": {"ESERV": app}, "tracks": tracks,
            "nexus": {"url": url + "/nexus", "release_repository": "releases", "records_repository": "records", "group_id": "org.test.odm"}}


class DeploymentHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="odm-http-test-", dir=Path(__file__).resolve().parent)
        self.base = Path(self.temporary.name)
        self.simulation = Simulation()
        self.server = simulated_server(self.simulation)
        self.url = self.server.__enter__()
        self.config = configuration(self.url, self.base)
        (self.base / "config").mkdir()
        self.config_path = self.base / "config" / "odm.yaml"
        atomic_json(self.config_path, self.config)
        self.environment = patch.dict(os.environ, {"APP": "ESERV", "ACTION": "DEPLOY", "JAR_VERSION": "2026.10.25.1", "BUILD_TAG": "test-run", "BUILD_URL": "", "BASELINE_ID": "b1"})
        self.environment.start()
        self.source_git = patch.object(deploy, "save_baseline", return_value={"commit": "source-commit", "tag": "ESERV/2026.10.25.1"})
        self.source_git.start()

    def tearDown(self):
        self.source_git.stop()
        self.environment.stop()
        self.server.__exit__(None, None, None)
        self.temporary.cleanup()

    def prepare(self, track):
        os.environ.update(DEPLOYMENT_TRACK=track, JOB_NAME="deploy-" + track, BASELINE_ID="b1" if track == "dev" else "")
        cfg, plan = deploy.create_plan(self.config_path)
        path = self.base / "output" / (track + "-plan.json")
        atomic_json(path, plan)
        plan = deploy.prepare(cfg, plan, path, self.base)
        os.environ["ODM_TARGET_LOCK"] = plan["lock_name"]
        return cfg, plan, path

    def execute(self, track):
        cfg, plan, path = self.prepare(track)
        if plan["requires_approval"]:
            os.environ.update(ODM_APPROVED_PLAN_HASH=plan["plan_hash"], ODM_APPROVER="approved-test-user")
        result = deploy.execute(cfg, plan, path, self.base, self.base / "output" / (track + "-result.json"))
        return cfg, plan, result

    def test_build_once_then_promote_same_bytes_to_three_distinct_targets(self):
        dev_cfg, dev_plan, dev_result = self.execute("dev")
        source_calls = [call for call in self.simulation.calls if call[1].startswith("/dc/")]
        _, int_plan, _ = self.execute("int")
        _, prod_plan, _ = self.execute("prod")
        self.assertEqual(dev_plan["release_sha256"], int_plan["release_sha256"])
        self.assertEqual(dev_plan["release_sha256"], prod_plan["release_sha256"])
        self.assertEqual(source_calls, [call for call in self.simulation.calls if call[1].startswith("/dc/")])
        for state in self.simulation.targets.values():
            self.assertEqual(state["jar"], self.simulation.new_jar)
            self.assertEqual(state["library"], "resuri://ESERV_XOM.zip/1.1")
            self.assertEqual(state["xoms"]["1.0"], self.simulation.old_xom)
        os.environ["ODM_CATALOG_LOCK"] = prod_plan["catalog_lock_name"]
        deploy.catalog(deploy.resolve(self.config, "prod", "ESERV"), prod_plan)
        value = json.loads(self.simulation.store["records/catalogs/prod/ESERV.json"])
        self.assertEqual(value["versions"], [{"version": "2026.10.25.1", "sha256": dev_plan["release_sha256"]}])

    def test_failed_business_test_restores_jar_library_and_old_xom(self):
        cfg, plan, path = self.prepare("dev")
        self.simulation.fail_new_test = True
        result_path = self.base / "output" / "failed.json"
        with self.assertRaisesRegex(OdmError, "previous release was restored"):
            deploy.execute(cfg, plan, path, self.base, result_path)
        self.assertEqual(self.simulation.targets["dev"]["jar"], self.simulation.old_jar)
        self.assertEqual(self.simulation.targets["dev"]["library"], "resuri://ESERV_XOM.zip/1.0")
        self.assertEqual(read_json(result_path)["recovery"], "succeeded")
        marker = json.loads(self.simulation.store["records/inflight/dev/ESERV.json"])
        self.assertEqual(marker["status"], "finished")
        self.assertFalse(deploy.eligible(deploy.resolve(self.config, "int", "ESERV"), {**plan, "track": "int"}, deploy.Nexus(cfg)))

    def test_receipt_failure_preserves_successful_deployment_and_blocks_retry(self):
        cfg, plan, path = self.prepare("dev")
        self.simulation.fail_receipt = True
        result_path = self.base / "output" / "record-failed.json"
        with self.assertRaisesRegex(OdmError, "Deployment passed, but its record"):
            deploy.execute(cfg, plan, path, self.base, result_path)
        self.assertEqual(self.simulation.targets["dev"]["jar"], self.simulation.new_jar)
        self.assertEqual(read_json(result_path)["status"], "deployed_record_failed")
        writes = len([call for call in self.simulation.calls if call[0] != "GET" and call[1].startswith("/res/")])
        with self.assertRaisesRegex(OdmError, "unfinished result"):
            deploy.execute(cfg, plan, path, self.base, result_path)
        self.assertEqual(writes, len([call for call in self.simulation.calls if call[0] != "GET" and call[1].startswith("/res/")]))

    def test_uploaded_but_unverified_release_cannot_promote(self):
        self.prepare("dev")
        with self.assertRaisesRegex(OdmError, "has not passed DEV"):
            self.prepare("int")
        self.assertEqual(self.simulation.targets["int"]["jar"], self.simulation.old_jar)

    def test_production_rejects_an_unapproved_plan_before_res_writes(self):
        self.execute("dev")
        self.execute("int")
        cfg, plan, path = self.prepare("prod")
        os.environ.pop("ODM_APPROVED_PLAN_HASH", None)
        os.environ.pop("ODM_APPROVER", None)
        with self.assertRaisesRegex(OdmError, "approval is missing"):
            deploy.execute(cfg, plan, path, self.base, self.base / "output" / "prod-result.json")
        self.assertEqual(self.simulation.targets["prod"]["jar"], self.simulation.old_jar)

    def test_preflight_uses_only_read_operations(self):
        os.environ.update(DEPLOYMENT_TRACK="dev", JOB_NAME="deploy-dev", BASELINE_ID="b1")
        cfg, plan = deploy.create_plan(self.config_path)
        self.assertEqual(deploy.preflight(cfg, plan)["mode"], "read_only")
        self.assertTrue(all(method == "GET" for method, _ in self.simulation.calls))
        self.assertFalse(any(path in {"/dc/export", "/dc/ruleapp", "/dc/xom"} for _, path in self.simulation.calls))

    def test_mistyped_configuration_flags_cannot_skip_approval_or_change_build_mode(self):
        os.environ.update(DEPLOYMENT_TRACK="dev", JOB_NAME="deploy-dev", BASELINE_ID="b1")
        for field in ("require_approval", "build_release"):
            with self.subTest(field=field):
                original = self.config["tracks"]["dev"][field]
                self.config["tracks"]["dev"][field] = "false"
                atomic_json(self.config_path, self.config)
                with self.assertRaisesRegex(OdmError, "must be true or false"):
                    deploy.create_plan(self.config_path)
                self.config["tracks"]["dev"][field] = original
        self.assertFalse(self.simulation.calls)


if __name__ == "__main__":
    unittest.main()
