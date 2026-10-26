from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from sync import sync
from odm_common import OdmError, Response, resolve, sha256_bytes


GIT = os.environ.get("GIT_EXECUTABLE") or shutil.which("git")


def zip_bytes(files, date=(2026, 10, 25, 0, 0, 0)):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            info = zipfile.ZipInfo(name, date_time=date)
            archive.writestr(info, body)
    return output.getvalue()


def configuration(remote="https://git.example.invalid/rules.git"):
    return {
        "schema_version": 1,
        "http": {"timeout_seconds": 10},
        "apps": {
            "ESERV": {"decision_center": {"service_id": "service-e", "branch_id": "branch-e"}},
            "WACON": {"decision_center": {"service_id": "service-w", "branch_id": "branch-w"}},
        },
        "tracks": {
            "dev": {
                "sync_jobs": ["odm-sync-dev"], "deployment_jobs": ["odm-deploy-dev"],
                "sync_apps": ["ESERV", "WACON"],
                "sync": {
                    "target_root": "rules/dev", "max_push_attempts": 3,
                    "git": {"url": remote, "branch": "main", "author_name": "ODM Test",
                            "author_email": "odm-test@example.invalid", "allow_local_for_tests": True},
                },
                "decision_center": {
                    "url": "https://dc.example.invalid/decisioncenter-api/v1",
                    "contracts_verified": True,
                    "operations": {
                        "branch_revision": {
                            "method": "GET", "path": "sources/{service_id}/{branch_id}",
                            "revision_pointer": "/revision", "service_id_pointer": "/serviceId",
                            "branch_id_pointer": "/id",
                        },
                        "export_branch": {"method": "GET", "path": "exports/{service_id}",
                                          "params": {"selectedBranch": "{branch_id}"}},
                    },
                },
            },
        },
    }


def captured(app="ESERV", body=None, revision="1", raw="archive-one"):
    files = body if body is not None else {"rules/eligibility.xml": b"<rule/>\r\n"}
    return {"app": app, "service_id": "service-e" if app == "ESERV" else "service-w",
            "branch_id": "branch-e" if app == "ESERV" else "branch-w", "source_revision": revision,
            "content_sha256": sync.content_digest(files), "archive_sha256": sha256_bytes(raw.encode()),
            "files": files}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.selected = resolve(configuration(), "dev", "ESERV")

    def response(self, revision=1, service="service-e", branch="branch-e"):
        return Response(200, json.dumps({"revision": revision, "serviceId": service, "id": branch}).encode(), {})

    def client(self, archive, revision_after=1):
        client = Mock()
        client.request.side_effect = [self.response(), Response(200, archive, {}), self.response(revision_after)]
        return client

    def test_captures_exact_files_and_verified_revision(self):
        archive = zip_bytes({"rules.xml": b"<a> \r\n</a>"})
        client = self.client(archive)
        with patch("sync.sync._client", return_value=client):
            result = sync.capture_source(self.selected)
        self.assertEqual(result["files"], {"rules.xml": b"<a> \r\n</a>"})
        self.assertEqual(result["source_revision"], "1")
        self.assertEqual(result["archive_sha256"], sha256_bytes(archive))
        self.assertEqual(client.request.call_args_list[1].kwargs["params"], {"selectedBranch": "branch-e"})

    def test_changed_revision_fails_without_publishing_capture(self):
        with patch("sync.sync._client", return_value=self.client(zip_bytes({"a.xml": b"one"}), 2)):
            with self.assertRaisesRegex(OdmError, "Rules changed during the export"):
                sync.capture_source(self.selected)

    def test_wrong_source_identity_is_rejected_before_export(self):
        client = Mock()
        client.request.return_value = self.response(service="other-service")
        with patch("sync.sync._client", return_value=client):
            with self.assertRaisesRegex(OdmError, "different service or branch"):
                sync.capture_source(self.selected)
        self.assertEqual(client.request.call_count, 1)

    def test_unverified_api_contract_cannot_make_requests(self):
        self.selected["decision_center"]["contracts_verified"] = False
        with patch("sync.sync._client") as client:
            with self.assertRaisesRegex(OdmError, "has not been verified"):
                sync.capture_source(self.selected)
        client.assert_not_called()

    def test_empty_archive_cannot_replace_git_tree(self):
        with patch("sync.sync._client", return_value=self.client(zip_bytes({}))):
            with self.assertRaises(OdmError):
                sync.capture_source(self.selected)

    def test_git_control_paths_in_export_are_rejected(self):
        with patch("sync.sync._client", return_value=self.client(zip_bytes({".git/config": b"unsafe"}))):
            with self.assertRaisesRegex(OdmError, "Git control"):
                sync.capture_source(self.selected)

    def test_endpoint_ids_are_encoded_without_changing_query_values(self):
        variables = {"service_id": "service/with space", "branch_id": "branch +/&"}
        path, params = sync._operation(self.selected["decision_center"], "export_branch", variables)
        self.assertEqual(path, "exports/service%2Fwith%20space")
        self.assertEqual(params, {"selectedBranch": "branch +/&"})

    def test_invalid_revision_pointer_does_not_fall_back_to_branch_head(self):
        self.selected["decision_center"]["operations"]["branch_revision"]["revision_pointer"] = "/missing"
        with patch("sync.sync._client", return_value=self.client(zip_bytes({"a.xml": b"one"}))):
            with self.assertRaisesRegex(OdmError, "source identity"):
                sync.capture_source(self.selected)

    def test_json_pointer_escape_and_array_indices(self):
        self.assertEqual(sync._json_pointer({"a/b": [{"~": 7}]}, "/a~1b/0/~0"), 7)
        with self.assertRaises(OdmError):
            sync._json_pointer([1, 2], "/-1")

    def test_content_hash_ignores_zip_timestamps_and_preserves_whitespace(self):
        one = zip_bytes({"a.xml": b"<x> </x>"})
        two = zip_bytes({"a.xml": b"<x> </x>"}, (2026, 10, 25, 1, 0, 0))
        self.assertNotEqual(sha256_bytes(one), sha256_bytes(two))
        self.assertEqual(sync.content_digest(sync.inspect_zip(one)), sync.content_digest(sync.inspect_zip(two)))
        self.assertNotEqual(sync.content_digest({"a.xml": b"<x> </x>"}), sync.content_digest({"a.xml": b"<x></x>"}))

    def test_plan_has_stable_branch_lock_and_no_network_calls(self):
        with patch("sync.sync.capture_source") as capture:
            first = sync.plan(configuration(), "dev", "ESERV", "odm-sync-dev")
            second = sync.plan(configuration(), "dev", "WACON", "odm-sync-dev")
        capture.assert_not_called()
        self.assertEqual(first["lock_name"], second["lock_name"])
        self.assertEqual(first["target_root"], "rules/dev")

    def test_plan_rejects_overlapping_capture_locations_on_shared_branch(self):
        cfg = configuration()
        cfg["tracks"]["int"] = deepcopy(cfg["tracks"]["dev"])
        cfg["tracks"]["int"]["sync_jobs"] = ["odm-sync-int"]
        with self.assertRaisesRegex(OdmError, "overlapping Git export folders"):
            sync.plan(cfg, "dev", "ESERV", "odm-sync-dev")

    def test_plan_rejects_ancestor_capture_that_would_erase_another_app(self):
        cfg = configuration()
        cfg["tracks"]["int"] = deepcopy(cfg["tracks"]["dev"])
        cfg["tracks"]["int"]["sync"]["target_root"] = "rules/dev/ESERV"
        cfg["tracks"]["int"]["sync_apps"] = ["WACON"]
        with self.assertRaisesRegex(OdmError, "overlapping Git export folders"):
            sync.plan(cfg, "dev", "ESERV", "odm-sync-dev")

    def test_separate_git_repositories_can_use_the_same_export_root(self):
        cfg = configuration()
        cfg["tracks"]["int"] = deepcopy(cfg["tracks"]["dev"])
        cfg["tracks"]["int"]["sync"]["git"]["url"] = "https://another.example.invalid/rules.git"
        self.assertEqual(sync.plan(cfg, "dev", "ESERV")["status"], "planned")

    def test_equivalent_https_url_spelling_uses_the_same_lock(self):
        one = configuration("https://git.example.invalid/rules.git")
        two = configuration("https://GIT.example.invalid:443/rules.git/")
        self.assertEqual(sync.plan(one, "dev")["lock_name"], sync.plan(two, "dev")["lock_name"])

    def test_deployment_job_cannot_run_sync(self):
        with patch("sync.sync.capture_source") as capture:
            with self.assertRaisesRegex(OdmError, "not allowed to sync"):
                sync.run_sync(configuration(), "dev", "ESERV", "odm-deploy-dev")
        capture.assert_not_called()

    def test_missing_shared_lock_stops_before_any_export(self):
        with patch.dict(os.environ, {"ODM_SYNC_LOCK": ""}), patch("sync.sync.capture_source") as capture:
            with self.assertRaisesRegex(OdmError, "lock is missing"):
                sync.run_sync(configuration(), "dev", "ESERV", "odm-sync-dev")
        capture.assert_not_called()

    def test_failure_result_contains_plain_message_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg, output = Path(temporary) / "config.json", Path(temporary) / "result.json"
            cfg.write_text(json.dumps(configuration()), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"DEPLOYMENT_TRACK": "missing", "APP": "", "JOB_NAME": ""}), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                result = sync.main(["plan", "--config", str(cfg), "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertEqual(json.loads(output.read_text())["status"], "failed")
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_askpass_files_contain_no_password(self):
        settings = {"username_env": "TEST_GIT_USER", "password_env": "TEST_GIT_PASS"}
        with patch.dict(os.environ, {"TEST_GIT_USER": "test-user", "TEST_GIT_PASS": "test-secret-never-written"}):
            with sync.git_environment(settings) as env:
                directory = Path(env["GIT_ASKPASS"]).parent
                files = "\n".join(item.read_text(encoding="utf-8") for item in directory.iterdir())
                self.assertNotIn("test-secret-never-written", files)
                self.assertEqual(env["ODM_SYNC_GIT_PASSWORD"], "test-secret-never-written")
                git_config = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
                              for i in range(int(env["GIT_CONFIG_COUNT"]))}
                self.assertEqual(git_config["http.followRedirects"], "false")
                self.assertEqual(git_config["http.sslVerify"], "true")
                self.assertEqual(env["GIT_SSL_NO_VERIFY"], "false")


@unittest.skipUnless(GIT, "Git is required for local sync integration tests")
class GitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="odm-sync-test-")
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.environment = patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_EXECUTABLE": GIT,
        })
        self.environment.start()
        self.git(["init", "--bare", str(self.remote)])
        self.config = configuration(str(self.remote))
        self.lock = patch.dict(os.environ, {"ODM_SYNC_LOCK": sync.plan(self.config, "dev")["lock_name"]})
        self.lock.start()

    def tearDown(self):
        self.lock.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def git(self, args, cwd=None, binary=False):
        result = subprocess.run([GIT, *args], cwd=cwd, env=os.environ, capture_output=True,
                                text=not binary, check=True)
        return result.stdout if binary else result.stdout.strip()

    def run_capture(self, capture, app="ESERV"):
        with patch("sync.sync.capture_source", return_value=capture):
            return sync.run_sync(self.config, "dev", app, "odm-sync-dev", git_executable=GIT)

    def show(self, path):
        return self.git(["--git-dir", str(self.remote), "show", "main:" + path], binary=True)

    def count(self):
        return int(self.git(["--git-dir", str(self.remote), "rev-list", "--count", "main"]))

    def external_commit(self, relative, body):
        clone = self.root / ("racer-" + str(len(list(self.root.glob("racer-*")))))
        self.git(["clone", "--branch", "main", str(self.remote), str(clone)])
        self.git(["config", "user.name", "Other Writer"], cwd=clone)
        self.git(["config", "user.email", "other@example.invalid"], cwd=clone)
        self.git(["config", "commit.gpgsign", "false"], cwd=clone)
        target = clone / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        self.git(["add", "-A"], cwd=clone)
        self.git(["commit", "-m", "Concurrent write"], cwd=clone)
        self.git(["push", "origin", "HEAD:refs/heads/main"], cwd=clone)

    def test_first_capture_pushes_and_second_identical_export_has_no_commit(self):
        first = self.run_capture(captured())
        second = self.run_capture(captured(revision="2", raw="different-zip-timestamp"))
        self.assertEqual(first["status"], "synced")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(self.count(), 1)
        metadata = json.loads(self.show(".odm-sync/dev/ESERV.json"))
        self.assertEqual(metadata["source_revision"], "1")

    def test_deleted_source_file_is_removed_from_export_only(self):
        self.run_capture(captured(body={"keep.xml": b"keep", "remove.xml": b"remove"}))
        result = self.run_capture(captured(body={"keep.xml": b"keep"}, revision="2"))
        self.assertEqual(result["status"], "synced")
        self.assertEqual(self.count(), 2)
        listing = self.git(["--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main"])
        self.assertNotIn("remove.xml", listing)
        self.assertIn("rules/dev/ESERV/keep.xml", listing)

    def test_source_mapping_change_with_same_files_updates_provenance(self):
        self.run_capture(captured())
        updated = captured(revision="2")
        updated["service_id"] = "new-service-e"
        self.config["apps"]["ESERV"]["decision_center"]["service_id"] = "new-service-e"
        result = self.run_capture(updated)
        self.assertEqual(result["status"], "synced")
        self.assertEqual(self.count(), 2)
        metadata = json.loads(self.show(".odm-sync/dev/ESERV.json"))
        self.assertEqual(metadata["service_id"], "new-service-e")

    def test_git_attributes_and_ignores_cannot_change_or_omit_export_bytes(self):
        files = {".gitattributes": b"*.txt text eol=lf\n", ".gitignore": b"ignored.txt\n",
                 "rule.txt": b"keep\r\nthese\r\nbytes\r\n", "ignored.txt": b"must be included"}
        self.run_capture(captured(body=files))
        self.assertEqual(self.show("rules/dev/ESERV/rule.txt"), files["rule.txt"])
        self.assertEqual(self.show("rules/dev/ESERV/ignored.txt"), files["ignored.txt"])
        self.assertEqual(self.run_capture(captured(body=files, raw="new-container"))["status"], "unchanged")

    def test_all_sources_are_captured_before_any_git_write(self):
        self.run_capture(captured())
        with patch("sync.sync.capture_source", side_effect=[captured(revision="2"), OdmError("WACON export failed.")]):
            with self.assertRaisesRegex(OdmError, "WACON export failed"):
                sync.run_sync(self.config, "dev", None, "odm-sync-dev", git_executable=GIT)
        self.assertEqual(self.count(), 1)

    def test_unrelated_remote_commit_is_preserved_during_retry(self):
        self.run_capture(captured(body={"rule.xml": b"before"}))
        outer = self
        original = sync.Git

        class RacingGit(original):
            raced = False

            def run(self, args, **kwargs):
                if args[0] == "push" and not self.raced:
                    self.raced = True
                    outer.external_commit("notes.txt", b"other writer")
                return super().run(args, **kwargs)

        with patch("sync.sync.Git", RacingGit):
            result = self.run_capture(captured(body={"rule.xml": b"after"}, revision="2"))
        self.assertEqual(result["status"], "synced")
        self.assertEqual(self.show("notes.txt"), b"other writer")
        self.assertEqual(self.show("rules/dev/ESERV/rule.xml"), b"after")
        self.assertEqual(self.count(), 3)

    def test_same_target_remote_change_is_not_overwritten(self):
        self.run_capture(captured(body={"rule.xml": b"before"}))
        outer = self
        original = sync.Git

        class RacingGit(original):
            raced = False

            def run(self, args, **kwargs):
                if args[0] == "push" and not self.raced:
                    self.raced = True
                    outer.external_commit("rules/dev/ESERV/rule.xml", b"concurrent source")
                return super().run(args, **kwargs)

        with patch("sync.sync.Git", RacingGit):
            with self.assertRaisesRegex(OdmError, "same rule export changed"):
                self.run_capture(captured(body={"rule.xml": b"our source"}, revision="2"))
        self.assertEqual(self.show("rules/dev/ESERV/rule.xml"), b"concurrent source")
        self.assertEqual(self.count(), 2)


if __name__ == "__main__":
    unittest.main()
