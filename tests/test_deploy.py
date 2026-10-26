"""Workflow tests. Servers are replaced with stateful in-memory adapters."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from deployment import deploy
from odm_common import OdmError, Response, atomic_json, canonical_zip_digest, read_json, resolve, sha256_bytes


def zipped(files):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)
    return result.getvalue()


def application(name):
    return {
        "artifact_id": name.lower() + "-release",
        "ruleapp_name": name + "App", "ruleapp_version": "1.0",
        "xom_name": name + "XOM.zip", "xom_version": "1.0",
        "library_name": name + "Library", "library_version": "1.0",
        "archive_identity": {"member": "META-INF/archive.xml", "checks": [
            {"role": "ruleapp_name", "xpath": ".", "attribute": "name", "equals": "{ruleapp_name}"},
            {"role": "ruleapp_version", "xpath": ".", "attribute": "version", "equals": "{ruleapp_version}"},
            {"role": "ruleset_name", "xpath": "ruleset", "attribute": "name", "equals": "Decision"},
            {"role": "ruleset_version", "xpath": "ruleset", "attribute": "version", "equals": "1.0"},
        ]},
        "decision_center": {"service_id": name + "-service", "branch_id": name + "-main", "deployment_id": name + "-deployment"},
        "rulesets": [{"name": "Decision", "version": "1.0", "description_path": "descriptions/Decision.json",
                      "request_file": "fixtures/request.json", "expected_file": "fixtures/expected.json",
                      "recovery_request_file": "fixtures/recovery-request.json", "recovery_expected_file": "fixtures/recovery-expected.json"}],
    }


def configuration():
    return {
        "schema_version": 1, "http": {"allow_http": False},
        "nexus": {"url": "https://nexus.test", "release_repository": "odm-releases",
                  "records_repository": "odm-deployments", "group_id": "org.test.odm"},
        "apps": {name: application(name) for name in ("ESERV", "WACON")},
        "tracks": {name: {
            "build_release": name == "dev", "predecessor": predecessor,
            "require_approval": name == "prod", "approval_submitters": "release-approvers",
            "deployment_jobs": ["odm-deploy-" + name], "sync_jobs": ["odm-sync-" + name],
            "res": {"url": "https://" + name + ".test/res/apiauth/v1", "contracts_verified": True,
                    "execution_urls": ["https://" + name + ".test/DecisionService/rest/v1"]},
            "decision_center": {"url": "https://dc.test/decisioncenter-api/v1", "contracts_verified": True,
                                "baseline_is_immutable": True, "operations": {}},
        } for name, predecessor in (("dev", None), ("int", "dev"), ("prod", "int"))},
    }


def jar_bytes(app, content=b"new rules"):
    return zipped({"META-INF/archive.xml": (
        f'<ruleapp name="{app["ruleapp_name"]}" version="1.0">'
        '<ruleset name="Decision" version="1.0"/></ruleapp>'
    ).encode(), "rules/decision.bin": content})


def release_bytes(cfg, version="2026.10.25.1", *, app_name="ESERV", jar=None):
    app = cfg["application"]
    files = {
        "ruleapp.jar": jar or jar_bytes(app),
        "xom.zip": zipped({"model/Request.class": b"new model"}),
        "source.zip": zipped({"rules/decision.xml": b"<rule>new</rule>"}),
        "tests/request.json": b'{"amount":12}',
        "tests/expected.json": b'{"assertions":[{"pointer":"/decision","equals":"approved"}]}',
    }
    manifest = {
        "schema_version": 1, "app": app_name, "jar_version": version,
        "source": {**app["decision_center"], "baseline_id": "approved-baseline"},
        "target": {key: app[key] for key in ("ruleapp_name", "ruleapp_version", "xom_name", "library_name", "library_version")},
        "tests": [{"name": "Decision", "version": "1.0", "description_path": "descriptions/Decision.json",
                   "request": "tests/request.json", "expected": "tests/expected.json"}],
        "files": {name: sha256_bytes(data) for name, data in files.items()},
    }
    files["manifest.json"] = deploy.canonical(manifest)
    return zipped(files)


def successful_receipt(cfg, plan, package, source):
    source_cfg = resolve(cfg, source, plan["app"])
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        ruleapp, xom = archive.read("ruleapp.jar"), archive.read("xom.zip")
    return {
        "schema_version": 1, "app": plan["app"], "track": source,
        "status": "succeeded", "jar_version": plan["jar_version"],
        "release_sha256": sha256_bytes(package),
        "release_path": deploy.release_path(source_cfg, plan["jar_version"]),
        "res_url": source_cfg["res"]["url"], "run_id": "d" * 32,
        "plan_hash": plan["plan_hash"], "automation_sha256": plan["automation_sha256"],
        "ruleapp_content_sha256": canonical_zip_digest(ruleapp),
        "xom_content_sha256": canonical_zip_digest(xom),
        "tests": [{"ruleset": case["name"], "version": case["version"], "execution_url": url, "passed": True}
                  for case in source_cfg["application"]["rulesets"] for url in source_cfg["res"]["execution_urls"]],
    }


class FakeNexus:
    def __init__(self, package, path):
        self.objects = {(False, path): package}
        self.history = []
        self.records_by_source = {}
        self.fail_receipts = False

    def get(self, path, *, records=False, missing=False):
        self.history.append(("get", records, path))
        return self.objects.get((records, path))

    def put(self, path, data, *, records=False, mutable=False):
        self.history.append(("put", records, path))
        if self.fail_receipts and path.startswith("receipts/"):
            raise OdmError("The test receipt write failed.")
        self.objects[(records, path)] = data

    def records(self, track, app):
        return copy.deepcopy(self.records_by_source.get((track, app), []))


class FakeRES:
    def __init__(self, cfg):
        self.app = cfg["application"]
        self.execution_urls = cfg["res"]["execution_urls"]
        self.jar = jar_bytes(self.app, b"previous working rules")
        self.old_jar = self.jar
        self.xoms = {"1.0": zipped({"model/Request.class": b"previous working model"})}
        self.library = f"resuri://{self.app['xom_name']}/1.0"
        self.old_library = self.library
        self.fail_new_smoke = False
        self.history = []

    def op(self, name, data=None, **extra):
        self.history.append((name, data))
        if name == "ruleapp_archive":
            return Response(200, self.jar, {})
        if name == "library_update":
            self.library = data.decode()
        elif name == "ruleapp_publish":
            self.jar = data
        else:
            raise AssertionError("Unexpected test API operation: " + name)
        return Response(200, b"{}", {})

    def library_content(self):
        return self.library

    def xom(self, version):
        return self.xoms[version]

    def select_xom(self, data):
        self.history.append(("xom_publish", data))
        self.xoms["1.1"] = data
        return "1.1"

    def verify_rules(self):
        self.history.append(("verify_rules", self.library))

    def smoke(self, cases, files):
        self.history.append(("smoke", self.jar))
        if self.fail_new_smoke and self.jar != self.old_jar:
            raise OdmError("The deployed rules returned an unexpected business result.")
        return [{"ruleset": "Decision", "version": "1.0", "execution_url": url, "passed": True} for url in self.execution_urls]


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.base = Path(self.temporary.name)
        self.config_path = self.base / "config" / "odm.yaml"
        self.config_path.parent.mkdir()
        self.root = configuration()
        atomic_json(self.config_path, self.root)
        self.plan_path = self.base / "output" / "deploy-plan.json"
        self.result_path = self.base / "output" / "deploy-result.json"
        fixtures = self.base / "fixtures"
        fixtures.mkdir()
        for name in ("request", "recovery-request"):
            (fixtures / (name + ".json")).write_bytes(b'{"amount":12}')
        for name in ("expected", "recovery-expected"):
            (fixtures / (name + ".json")).write_bytes(b'{"assertions":[{"pointer":"/decision","equals":"approved"}]}')
        self.environment = patch.dict(os.environ, {
            "DEPLOYMENT_TRACK": "int", "APP": "ESERV", "JOB_NAME": "odm-deploy-int",
            "ACTION": "DEPLOY", "JAR_VERSION": "2026.10.25.1", "BASELINE_ID": "",
            "BUILD_TAG": "jenkins-odm-deploy-int-31", "BUILD_URL": "https://jenkins.test/job/31/",
            "ODM_TARGET_LOCK": "", "ODM_CATALOG_LOCK": "", "ODM_APPROVED_PLAN_HASH": "", "ODM_APPROVER": "",
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def planned(self):
        return deploy.create_plan(self.config_path)

    def prepared(self):
        cfg, plan = self.planned()
        package = release_bytes(cfg)
        package_file = self.base / "release.zip"
        package_file.write_bytes(package)
        plan.update(phase="prepared", release_sha256=sha256_bytes(package), package_file=str(package_file))
        plan["plan_hash"] = deploy.plan_hash(plan)
        atomic_json(self.plan_path, plan)
        nexus = FakeNexus(package, plan["release_path"])
        source = cfg.get("predecessor")
        if source:
            nexus.records_by_source[(source, "ESERV")] = [successful_receipt(cfg, plan, package, source)]
        os.environ["ODM_TARGET_LOCK"] = plan["lock_name"]
        return cfg, plan, package, nexus

    def test_user_must_select_an_explicit_version(self):
        for value in ("", "Select a version", "select", "latest", "highest", "1.0-SNAPSHOT", "../other"):
            with self.subTest(value=value), patch.dict(os.environ, {"JAR_VERSION": value}), self.assertRaises(OdmError):
                self.planned()
        self.assertEqual(self.planned()[1]["jar_version"], "2026.10.25.1")

    def test_job_and_track_binding_and_higher_track_baseline_rejection(self):
        for changes in ({"JOB_NAME": "odm-deploy-prod"}, {"JOB_NAME": "odm-sync-int"}, {"BASELINE_ID": "branch-head"}):
            with self.subTest(changes=changes), patch.dict(os.environ, changes), self.assertRaises(OdmError):
                self.planned()

    def test_first_track_requires_baseline_and_uses_separate_res_url(self):
        with patch.dict(os.environ, {"DEPLOYMENT_TRACK": "dev", "JOB_NAME": "odm-deploy-dev"}):
            with self.assertRaisesRegex(OdmError, "baseline"):
                self.planned()
            with patch.dict(os.environ, {"BASELINE_ID": "approved-baseline"}):
                _, plan = self.planned()
                self.assertTrue(plan["needs_build"])
                self.assertEqual(plan["res_url"], self.root["tracks"]["dev"]["res"]["url"])

    def test_plan_is_bound_to_selected_parameters_and_jenkins_run(self):
        _, plan = self.planned()
        atomic_json(self.plan_path, plan)
        for changes in ({"JAR_VERSION": "2026.10.25.2"}, {"BUILD_TAG": "another-run"}):
            with self.subTest(changes=changes), patch.dict(os.environ, changes), self.assertRaises(OdmError):
                deploy.load_plan(self.config_path, self.plan_path)

    def test_higher_track_prepares_exact_nexus_bytes_without_building(self):
        cfg, plan, package, nexus = self.prepared()
        plan.update(phase="planned", release_sha256="")
        with patch.object(deploy, "Nexus", return_value=nexus), patch.object(deploy, "build_release") as build:
            result = deploy.prepare(cfg, plan, self.plan_path, self.base)
        build.assert_not_called()
        self.assertEqual(Path(result["package_file"]).read_bytes(), package)
        self.assertEqual(result["release_sha256"], sha256_bytes(package))
        self.assertFalse(any(item[0] == "put" for item in nexus.history))

    def test_missing_predecessor_evidence_stops_before_preparing_or_mutating(self):
        cfg, plan, _, nexus = self.prepared()
        nexus.records_by_source.clear()
        with patch.object(deploy, "Nexus", return_value=nexus), patch.object(deploy, "RES") as res:
            with self.assertRaisesRegex(OdmError, "DEV testing"):
                deploy.execute(cfg, plan, self.plan_path, self.base, self.result_path)
        res.assert_not_called()
        self.assertFalse(any(item[0] == "put" for item in nexus.history))

    def test_release_manifest_app_and_inner_artifact_checksums_are_enforced(self):
        cfg, plan = self.planned()
        with self.assertRaisesRegex(OdmError, "selected app"):
            deploy.package_files(release_bytes(cfg, app_name="WACON"), cfg, plan)
        data = release_bytes(cfg)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files["ruleapp.jar"] = jar_bytes(cfg["application"], b"changed after manifest")
        with self.assertRaisesRegex(OdmError, "checksum"):
            deploy.package_files(zipped(files), cfg, plan)

    def test_wrong_ruleapp_inside_valid_package_is_rejected(self):
        cfg, plan = self.planned()
        wrong = jar_bytes(self.root["apps"]["WACON"])
        with self.assertRaisesRegex(OdmError, "configured application"):
            deploy.package_files(release_bytes(cfg, jar=wrong), cfg, plan)

    def test_production_approval_must_match_prepared_plan(self):
        with patch.dict(os.environ, {"DEPLOYMENT_TRACK": "prod", "JOB_NAME": "odm-deploy-prod"}):
            cfg, plan, _, _ = self.prepared()
            with patch.dict(os.environ, {"ODM_APPROVED_PLAN_HASH": "wrong", "ODM_APPROVER": "release-user"}), patch.object(deploy, "Nexus") as nexus:
                with self.assertRaisesRegex(OdmError, "approval"):
                    deploy.execute(cfg, plan, self.plan_path, self.base, self.result_path)
            nexus.assert_not_called()

    def test_modified_prepared_package_stops_before_server_access(self):
        cfg, plan, _, _ = self.prepared()
        Path(plan["package_file"]).write_bytes(b"different bytes")
        with patch.object(deploy, "Nexus") as nexus, self.assertRaisesRegex(OdmError, "checksum"):
            deploy.execute(cfg, plan, self.plan_path, self.base, self.result_path)
        nexus.assert_not_called()

    def test_failed_business_test_restores_jar_library_and_old_xom(self):
        cfg, plan, _, nexus = self.prepared()
        res = FakeRES(cfg)
        old_xom = res.xom("1.0")
        res.fail_new_smoke = True
        with patch.object(deploy, "Nexus", return_value=nexus), patch.object(deploy, "RES", return_value=res):
            with self.assertRaisesRegex(OdmError, "restored and tested"):
                deploy.execute(cfg, plan, self.plan_path, self.base, self.result_path)
        self.assertEqual(res.jar, res.old_jar)
        self.assertEqual(res.library, res.old_library)
        self.assertEqual(res.xom("1.0"), old_xom)
        self.assertGreaterEqual(sum(name == "smoke" and content == res.old_jar for name, content in res.history), 2)
        result = read_json(self.result_path)
        self.assertEqual((result["status"], result["recovery"]), ("failed", "succeeded"))
        state = json.loads(nexus.objects[(True, "inflight/int/ESERV.json")])
        self.assertEqual(state["status"], "finished")

    def test_successful_deployment_receipt_failure_does_not_restore_old_release(self):
        cfg, plan, _, nexus = self.prepared()
        nexus.fail_receipts = True
        res = FakeRES(cfg)
        with patch.object(deploy, "Nexus", return_value=nexus), patch.object(deploy, "RES", return_value=res):
            with self.assertRaisesRegex(OdmError, "record could not be saved"):
                deploy.execute(cfg, plan, self.plan_path, self.base, self.result_path)
        self.assertNotEqual(res.jar, res.old_jar)
        self.assertEqual(res.library, "resuri://ESERVXOM.zip/1.1")
        publishes = [data for name, data in res.history if name == "ruleapp_publish"]
        self.assertEqual(len(publishes), 1)
        self.assertEqual(read_json(self.result_path)["status"], "deployed_record_failed")
        self.assertEqual(json.loads(nexus.objects[(True, "inflight/int/ESERV.json")])["status"], "started")

    def test_previous_unfinished_run_blocks_before_res_changes(self):
        cfg, plan, _, nexus = self.prepared()
        nexus.objects[(True, "inflight/int/ESERV.json")] = b'{"status":"started","run_id":"older-run"}'
        with patch.object(deploy, "Nexus", return_value=nexus), patch.object(deploy, "RES") as res:
            with self.assertRaisesRegex(OdmError, "unfinished"):
                deploy.execute(cfg, plan, self.plan_path, self.base, self.result_path)
        res.assert_not_called()
        self.assertFalse(any(item[0] == "put" for item in nexus.history))

    def test_success_records_exact_digest_and_complete_state(self):
        cfg, plan, package, nexus = self.prepared()
        res = FakeRES(cfg)
        with patch.object(deploy, "Nexus", return_value=nexus), patch.object(deploy, "RES", return_value=res):
            result = deploy.execute(cfg, plan, self.plan_path, self.base, self.result_path)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["release_sha256"], sha256_bytes(package))
        self.assertTrue(result["record_saved"])
        self.assertEqual(result["library_content"], res.library)
        self.assertEqual(result["ruleapp_content_sha256"], canonical_zip_digest(res.jar))
        self.assertTrue((self.plan_path.parent / "private" / plan["run_id"] / "backup" / "xom.zip").is_file())

    def test_conflicting_deployment_digests_cannot_enter_version_catalog(self):
        cfg, plan, _, nexus = self.prepared()
        first = nexus.records_by_source[("dev", "ESERV")][0]
        nexus.records_by_source[("dev", "ESERV")].append({**first, "release_sha256": "f" * 64})
        with patch.dict(os.environ, {"ODM_CATALOG_LOCK": plan["catalog_lock_name"]}), patch.object(deploy, "Nexus", return_value=nexus):
            with self.assertRaisesRegex(OdmError, "disagree"):
                deploy.catalog(cfg, plan)
        self.assertFalse(any(item[0] == "put" for item in nexus.history))

    def test_build_download_must_bind_selected_baseline(self):
        with patch.dict(os.environ, {"DEPLOYMENT_TRACK": "dev", "JOB_NAME": "odm-deploy-dev", "BASELINE_ID": "approved-baseline"}):
            cfg, plan = self.planned()
        cfg["decision_center"]["operations"] = {
            "export_baseline": {"method": "GET", "path": "exports/{baseline_id}"},
            "download_ruleapp": {"method": "GET", "path": "downloads/{branch_id}"},
            "download_xom": {"method": "GET", "path": "xom/{baseline_id}"},
        }
        with patch.object(deploy, "client") as client:
            with self.assertRaisesRegex(OdmError, "download_ruleapp.*selected baseline"):
                deploy.build_release(cfg, plan, self.base)
        client.return_value.request.assert_not_called()

    def test_failed_new_run_cannot_reuse_old_success_result(self):
        cfg, plan = self.planned()
        atomic_json(self.result_path, {"status": "succeeded", "run_id": "an-older-run"})
        with patch.object(deploy, "load_plan", return_value=(cfg, plan)), patch.object(deploy, "execute", side_effect=OdmError("The target lock is missing.")), patch.object(deploy.signal, "signal"):
            status = deploy.main(["execute", "--config", str(self.config_path), "--plan", str(self.plan_path), "--result", str(self.result_path)])
        result = read_json(self.result_path)
        self.assertEqual(status, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["run_id"], plan["run_id"])

    def test_recovery_runs_even_when_failure_result_cannot_be_saved(self):
        cfg, plan, _, nexus = self.prepared()
        res = FakeRES(cfg)
        res.fail_new_smoke = True
        original_save = deploy.atomic_json
        failed_writes = []

        def save_with_failure(path, value):
            if Path(path) == self.result_path and value.get("status") == "failed" and value.get("recovery") == "started":
                failed_writes.append(True)
                raise OdmError("The result disk is full.")
            return original_save(path, value)

        with patch.object(deploy, "Nexus", return_value=nexus), patch.object(deploy, "RES", return_value=res), patch.object(deploy, "atomic_json", side_effect=save_with_failure):
            with self.assertRaisesRegex(OdmError, "restored and tested"):
                deploy.execute(cfg, plan, self.plan_path, self.base, self.result_path)
        self.assertEqual(failed_writes, [True])
        self.assertEqual(res.jar, res.old_jar)
        self.assertEqual(res.library, res.old_library)
        self.assertEqual(read_json(self.result_path)["recovery"], "succeeded")

    def test_catalog_failure_does_not_overwrite_successful_deployment_result(self):
        cfg, plan = self.planned()
        completed = {"status": "succeeded", "run_id": plan["run_id"], "app": "ESERV", "release_sha256": "a" * 64}
        atomic_json(self.result_path, completed)
        with patch.object(deploy, "load_plan", return_value=(cfg, plan)), patch.object(deploy, "catalog", side_effect=OdmError("Nexus catalog write failed.")), patch.object(deploy.signal, "signal"):
            status = deploy.main(["catalog", "--config", str(self.config_path), "--plan", str(self.plan_path), "--result", str(self.result_path)])
        self.assertEqual(status, 1)
        self.assertEqual(read_json(self.result_path), completed)
        self.assertEqual(read_json(self.plan_path.parent / "catalog-result.json")["status"], "failed")

    def test_ambiguous_management_write_requires_inspection_without_racing_restore(self):
        cfg, plan, _, nexus = self.prepared()
        res = FakeRES(cfg)
        original_op = res.op

        def lost_write_reply(name, data=None, **extra):
            response = original_op(name, data, **extra)
            if name == "ruleapp_publish":
                unknown = OdmError("RES did not confirm the management write.", ambiguous=True)
                unknown.management_write_unknown = True
                raise unknown
            return response

        with patch.object(deploy, "Nexus", return_value=nexus), patch.object(deploy, "RES", return_value=res), patch.object(res, "op", side_effect=lost_write_reply):
            with self.assertRaisesRegex(OdmError, "reconcile"):
                deploy.execute(cfg, plan, self.plan_path, self.base, self.result_path)
        self.assertNotEqual(res.jar, res.old_jar)
        self.assertNotEqual(res.library, res.old_library)
        self.assertEqual(sum(name == "ruleapp_publish" for name, _ in res.history), 1)
        self.assertEqual(read_json(self.result_path)["recovery"], "inspection_required")
        self.assertEqual(json.loads(nexus.objects[(True, "inflight/int/ESERV.json")])["status"], "needs_attention")

    def test_existing_dev_release_must_match_full_source_not_only_baseline_string(self):
        with patch.dict(os.environ, {"DEPLOYMENT_TRACK": "dev", "JOB_NAME": "odm-deploy-dev", "BASELINE_ID": "approved-baseline"}):
            cfg, plan = self.planned()
        package = release_bytes(cfg)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(files["manifest.json"])
        manifest["source"]["deployment_id"] = "different-deployment-configuration"
        files["manifest.json"] = deploy.canonical(manifest)
        nexus = FakeNexus(zipped(files), plan["release_path"])
        with patch.object(deploy, "Nexus", return_value=nexus), patch.object(deploy, "build_release") as build:
            with self.assertRaisesRegex(OdmError, "different source or baseline"):
                deploy.prepare(cfg, plan, self.plan_path, self.base)
        build.assert_not_called()
        self.assertFalse(any(item[0] == "put" for item in nexus.history))

    def test_malformed_or_untested_markers_cannot_authorize_promotion(self):
        cfg, plan, _, nexus = self.prepared()
        valid = nexus.records_by_source[("dev", "ESERV")][0]
        bad_tests = copy.deepcopy(valid["tests"])
        bad_tests[0]["passed"] = False
        wrong_runtime_tests = copy.deepcopy(valid["tests"])
        wrong_runtime_tests[0]["execution_url"] = "https://another-runtime.test"
        for changes in ({"schema_version": 0}, {"run_id": "missing-run"}, {"tests": []},
                        {"tests": bad_tests}, {"tests": wrong_runtime_tests},
                        {"release_path": "other-app/another-release.zip"}, {"plan_hash": "missing"}):
            with self.subTest(changes=changes):
                nexus.records_by_source[("dev", "ESERV")] = [{**valid, **changes}]
                self.assertEqual(deploy.eligible(cfg, plan, nexus), [])
                with self.assertRaisesRegex(OdmError, "DEV testing"):
                    deploy.check_eligibility(cfg, plan, nexus, plan["release_sha256"])

    @unittest.skipUnless(shutil.which("git"), "Git is required for baseline provenance integration.")
    def test_baseline_source_and_tag_are_published_together_and_never_retagged(self):
        def git(*args, cwd=None):
            result = subprocess.run(["git", *args], cwd=cwd or self.base, capture_output=True, check=True)
            return result.stdout

        remote = self.base / "source.git"
        seed = self.base / "seed"
        git("init", "--bare", str(remote))
        git("clone", str(remote), str(seed))
        (seed / "README.md").write_text("Baseline audit repository\n", encoding="utf-8")
        git("add", "README.md", cwd=seed)
        git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Initialize", cwd=seed)
        git("branch", "-M", "main", cwd=seed)
        git("push", "origin", "main", cwd=seed)
        with patch.dict(os.environ, {"DEPLOYMENT_TRACK": "dev", "JOB_NAME": "odm-deploy-dev", "BASELINE_ID": "approved-baseline"}):
            cfg, plan = self.planned()
        cfg["release_git"] = {"url": remote.as_uri(), "branch": "main"}
        source = zipped({"rules/approved.xml": b"<rule>approved immutable source</rule>"})
        original_temporary_directory = tempfile.TemporaryDirectory

        def workspace_temporary(**kwargs):
            return original_temporary_directory(dir=self.base, **kwargs)

        with patch.object(deploy.tempfile, "TemporaryDirectory", side_effect=workspace_temporary):
            saved = deploy.save_baseline(cfg, plan, source)
            tag = "ESERV/2026.10.25.1"
            self.assertEqual(saved["tag"], tag)
            commit = git("--git-dir", str(remote), "rev-parse", "refs/heads/main").decode().strip()
            self.assertEqual(saved["commit"], commit)
            self.assertEqual(git("--git-dir", str(remote), "rev-parse", "refs/tags/" + tag).decode().strip(), commit)
            self.assertEqual(git("--git-dir", str(remote), "show", tag + ":releases/ESERV/2026.10.25.1/source.zip"), source)
            with self.assertRaisesRegex(OdmError, "tag already exists"):
                deploy.save_baseline(cfg, plan, source)
            self.assertEqual(git("--git-dir", str(remote), "rev-parse", "refs/tags/" + tag).decode().strip(), commit)


if __name__ == "__main__":
    unittest.main()
