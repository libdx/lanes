import concurrent.futures
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from lanes.cli import (Conflict, LaneError, Registry, command_spec, identity,
                       overlaps, project)

class LanesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lanes test ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.repo = self.home / "source repo"
        self.state = self.home / "registry"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.config = {"version": 1, "base": "main", "resources": {
            "data": {"kind": "storage", "provision": "shared",
                     "identity": "path:" + str(self.home / "data")}}, "commands": {
            "ok": {"argv": [sys.executable, "-c", "pass"], "claims": {"data": "exclusive"}},
            "fail": {"argv": [sys.executable, "-c", "raise SystemExit(7)"],
                     "claims": {"data": "exclusive"}},
            "sleep": {"argv": [sys.executable, "-c", "import time; time.sleep(60)"],
                      "claims": {"data": "exclusive"}},
            "bad-stop": {"argv": [sys.executable, "-c", "pass"],
                         "claims": {"data": "exclusive"},
                         "stop": [[sys.executable, "-c", "raise SystemExit(1)"]]}}}
        self.commit_config()
        self.root, self.key = project(self.repo)

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", "-C", str(cwd or self.repo), *args],
                                       text=True, stderr=subprocess.PIPE).strip()

    def commit_config(self):
        (self.repo / "lanes.json").write_text(json.dumps(self.config))
        self.git("add", ".")
        self.git("commit", "-m", "config")

    def cli(self, *args, ok=True, repo=None):
        result = subprocess.run(self.command(*args, repo=repo), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if ok and result.returncode:
            self.fail(result.stdout + result.stderr)
        return result

    def command(self, *args, repo=None):
        return [sys.executable, "-m", "lanes", "--repo", str(repo or self.repo),
                "--registry", str(self.state), *args]

    def lane(self, name="one"):
        return json.loads(self.cli("create", name, "--scope", "test " + name).stdout)

    def registry(self):
        registry = Registry(self.state)
        self.addCleanup(registry.db.close)
        return registry

    def test_worktree_external_and_primary_unchanged(self):
        lane = self.lane()
        self.assertEqual(Path(lane["worktree"]),
                         self.home / ".worktrees" / self.key / "one")
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual(project(lane["worktree"]), (self.root, self.key))
        self.assertEqual(json.loads(self.cli("list").stdout)[0]["scope"], "test one")

    def test_prefix_and_branch_override(self):
        custom = self.home / "custom prefix"
        lane = json.loads(self.cli("create", "fix", "--scope", "fix", "--prefix", str(custom),
                                   "--branch", "ticket-1-fix").stdout)
        self.assertEqual(lane["branch"], "ticket-1-fix")
        self.assertEqual(Path(lane["worktree"]).parents[2], custom)

    def test_atomic_claims_and_readers(self):
        for name in ("one", "two", "three"):
            self.lane(name)
        registry = self.registry()
        first = registry.acquire(self.key, "one", "read", {"db:shared": "shared-read"}, {})
        registry.acquire(self.key, "two", "read", {"db:shared": "shared-read"}, {})
        with self.assertRaises(Conflict):
            registry.acquire(self.key, "three", "write",
                             {"db:other": "exclusive", "db:shared": "exclusive"}, {})
        self.assertEqual(registry.db.execute("SELECT count(*) FROM claims WHERE resource='db:other'")
                         .fetchone()[0], 0)
        registry.release(first)
        with self.assertRaises(Conflict):
            registry.acquire(self.key, "three", "write", {"db:shared": "exclusive"}, {})

    def test_concurrent_exclusive_acquisition(self):
        self.lane("one")
        self.lane("two")
        def acquire(name):
            registry = Registry(self.state)
            try:
                return registry.acquire(self.key, name, "race", {"db:same": "exclusive"}, {})
            except Conflict:
                return None
            finally:
                registry.db.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(acquire, ["one", "two"]))
        self.assertEqual(sum(value is not None for value in outcomes), 1)

    def test_path_aliases_and_ancestor_conflicts(self):
        target = self.home / "data"
        target.mkdir()
        alias = self.home / "alias"
        alias.symlink_to(target, target_is_directory=True)
        a = identity("path:" + str(alias), {}, self.root)
        b = identity("path:" + str(target), {}, self.root)
        self.assertEqual(a, b)
        self.assertTrue(overlaps(a, b + "/nested"))
        self.assertFalse(overlaps(a, b + "-other"))
        self.lane("one")
        self.lane("two")
        registry = self.registry()
        registry.acquire(self.key, "one", "parent", {a: "exclusive"}, {})
        with self.assertRaises(Conflict):
            registry.acquire(self.key, "two", "child", {b + "/nested": "shared-read"}, {})

    def test_cross_project_conflict(self):
        self.lane()
        other = self.home / "other repo"
        self.git("clone", str(self.repo), str(other), cwd=self.home)
        lane = json.loads(self.cli("create", "two", "--scope", "other", repo=other).stdout)
        registry = self.registry()
        registry.acquire(self.key, "one", "first", {"docker:engine/shared": "exclusive"}, {})
        with self.assertRaises(Conflict):
            registry.acquire(lane["project"], "two", "second",
                             {"docker:engine/shared": "exclusive"}, {})

    def test_runs_release_after_success_and_failure(self):
        self.lane()
        self.cli("run", "one", "ok")
        self.assertEqual(self.cli("run", "one", "fail", ok=False).returncode, 7)
        self.assertEqual(json.loads(self.cli("status").stdout)["runs"], [])

    def test_stop_failure_retains_and_recovery_requires_evidence(self):
        self.lane()
        self.assertEqual(self.cli("run", "one", "bad-stop", ok=False).returncode, 2)
        run = json.loads(self.cli("status", "one").stdout)["runs"][0]
        self.assertEqual(run["state"], "needs-recovery")
        self.assertTrue(run["claims"])
        self.cli("recover", run["token"], "--confirm-stopped", "--reason", "Inspected all consumers")
        self.assertEqual(json.loads(self.cli("status").stdout)["runs"], [])
        self.assertEqual(self.registry().db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 1)

    def test_runner_sigkill_retains_claims(self):
        lane = self.lane()
        # Persist the child PID for test cleanup. SIGKILL cannot run cleanup handlers.
        config = json.loads((Path(lane["worktree"]) / "lanes.json").read_text())
        marker = self.home / "child-pid"
        config["commands"]["sleep"]["argv"] = [sys.executable, "-c",
            "import os,pathlib,time; pathlib.Path({!r}).write_text(str(os.getpid())); time.sleep(60)"
            .format(str(marker))]
        (Path(lane["worktree"]) / "lanes.json").write_text(json.dumps(config))
        process = subprocess.Popen(self.command("run", "one", "sleep"), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            child = int(marker.read_text())
            process.kill()
            process.wait(timeout=5)
            runs = json.loads(self.cli("status").stdout)["runs"]
            self.assertEqual(len(runs), 1)
            self.assertTrue(runs[0]["claims"])
            self.assertEqual(self.cli("run", "one", "ok", ok=False).returncode, 2)
        finally:
            process.kill() if process.poll() is None else None
            if marker.exists():
                try:
                    os.killpg(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=5)

    def test_conflict_timeout_and_live_recovery_refusal(self):
        self.lane("one")
        self.lane("two")
        registry = self.registry()
        resource = identity(self.config["resources"]["data"]["identity"], {}, self.root)
        token = registry.acquire(self.key, "one", "held", {resource: "exclusive"}, {})
        started = time.monotonic()
        result = self.cli("run", "two", "ok", "--timeout", "0.2", ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertIn("held", result.stderr)
        self.assertEqual(self.cli("recover", token, "--confirm-stopped", "--reason", "checked",
                                  ok=False).returncode, 2)

    def test_prepare_failure_still_stops(self):
        marker = self.home / "stopped"
        self.config["commands"]["prepare-fail"] = {
            "argv": [sys.executable, "-c", "raise AssertionError('must not run')"],
            "claims": {"data": "exclusive"},
            "prepare": [[sys.executable, "-c", "raise SystemExit(1)"]],
            "stop": [[sys.executable, "-c", "from pathlib import Path; Path({!r}).touch()"
                      .format(str(marker))]]}
        self.commit_config()
        self.lane()
        self.assertEqual(self.cli("run", "one", "prepare-fail", ok=False).returncode, 2)
        self.assertTrue(marker.exists())
        self.assertEqual(json.loads(self.cli("status").stdout)["runs"], [])

    def test_reject_detached_without_lifecycle_and_invalid_claims(self):
        self.config["commands"]["ok"]["detached"] = True
        with self.assertRaises(LaneError):
            command_spec(self.config, "ok", {}, self.root)
        self.config["commands"]["ok"]["detached"] = False
        self.config["commands"]["ok"]["claims"]["data"] = "write-ish"
        with self.assertRaises(LaneError):
            command_spec(self.config, "ok", {}, self.root)

    def test_remove_requires_preserved_clean_inactive_work(self):
        lane = self.lane()
        worktree = Path(lane["worktree"])
        (worktree / "new").write_text("valuable")
        self.assertEqual(self.cli("remove", "one", "--preserved-ref", "main", ok=False).returncode, 2)
        self.git("add", ".", cwd=worktree)
        self.git("commit", "-m", "valuable", cwd=worktree)
        self.assertEqual(self.cli("remove", "one", "--preserved-ref", "main", ok=False).returncode, 2)
        self.git("merge", "--ff-only", "one")
        registry = self.registry()
        token = registry.acquire(self.key, "one", "active", {}, {})
        self.assertEqual(self.cli("remove", "one", "--preserved-ref", "main", ok=False).returncode, 2)
        registry.release(token)
        self.cli("remove", "one", "--preserved-ref", "main")
        self.assertFalse(worktree.exists())
        self.assertEqual(self.git("rev-parse", "one"), self.git("rev-parse", "main"))

    def test_ignored_artifacts_preserved(self):
        (self.repo / ".gitignore").write_text("*.local\n")
        self.git("add", ".")
        self.git("commit", "-m", "ignore")
        lane = self.lane()
        (Path(lane["worktree"]) / "valuable.local").write_text("preserve me")
        self.assertEqual(self.cli("remove", "one", "--preserved-ref", "main", ok=False).returncode, 2)
        self.assertTrue(Path(lane["worktree"]).exists())

    def test_invalid_names_and_failed_creation(self):
        self.assertEqual(self.cli("create", "../escape", "--scope", "bad", ok=False).returncode, 2)
        self.assertEqual(self.cli("create", "one", "--scope", "bad", "--branch", "main",
                                  ok=False).returncode, 2)
        self.assertEqual(json.loads(self.cli("list").stdout), [])

    def test_detached_lifecycle_holds_claim_until_verified(self):
        marker = self.home / "service-marker"
        self.config["commands"]["lifecycle"] = {
            "detached": True,
            "claims": {"data": "exclusive"},
            "prepare": [[sys.executable, "-c", "from pathlib import Path; Path({!r}).touch()"
                         .format(str(marker))]],
            "argv": [sys.executable, "-c", "from pathlib import Path; assert Path({!r}).exists()"
                     .format(str(marker))],
            "stop": [[sys.executable, "-c", "from pathlib import Path; Path({!r}).unlink()"
                      .format(str(marker))]],
            "verify_stopped": [[sys.executable, "-c",
                                "from pathlib import Path; assert not Path({!r}).exists()"
                                .format(str(marker))]]}
        self.commit_config()
        self.lane()
        self.cli("run", "one", "lifecycle")
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads(self.cli("status").stdout)["runs"], [])

    def test_failed_verification_retains_claims(self):
        self.config["commands"]["ok"]["verify_stopped"] = [
            [sys.executable, "-c", "raise SystemExit(1)"]]
        self.commit_config()
        self.lane()
        self.assertEqual(self.cli("run", "one", "ok", ok=False).returncode, 2)
        self.assertTrue(json.loads(self.cli("status").stdout)["runs"][0]["claims"])

    def test_signal_stops_foreground_and_releases_claim(self):
        lane = self.lane()
        marker = self.home / "signal-child"
        config = json.loads((Path(lane["worktree"]) / "lanes.json").read_text())
        config["commands"]["sleep"]["argv"] = [sys.executable, "-c",
            "import os,pathlib,time; pathlib.Path({!r}).write_text(str(os.getpid())); time.sleep(60)"
            .format(str(marker))]
        (Path(lane["worktree"]) / "lanes.json").write_text(json.dumps(config))
        process = subprocess.Popen(self.command("run", "one", "sleep"), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=8)
            self.assertEqual(process.returncode, 143)
            self.assertEqual(json.loads(self.cli("status").stdout)["runs"], [])
        finally:
            if process.poll() is None:
                process.kill()
            if marker.exists():
                try:
                    os.killpg(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=5)

    def test_expansions_and_alias_strength(self):
        config = {"resources": {
            "a": {"kind": "resource", "identity": "port:{project}/{lane}"},
            "b": {"kind": "resource", "identity": "port:{project}/{lane}"}},
            "commands": {"check": {"argv": ["echo", "{state}"],
                                   "env": {"CHECK": "{lane}"},
                                   "claims": {"a": "shared-read", "b": "exclusive"}}}}
        command, hooks, claims, env = command_spec(
            config, "check", {"project": "p", "lane": "l", "state": "/state path"}, self.root)
        self.assertEqual(claims, {"port:p/l": "exclusive"})
        self.assertEqual(command, ["echo", "/state path"])
        self.assertEqual(env, {"CHECK": "l"})

    def test_copy_source_and_target_are_acquired_together(self):
        self.lane("one")
        self.lane("two")
        registry = self.registry()
        registry.acquire(self.key, "one", "restore", {"db:target": "exclusive"}, {})
        with self.assertRaises(Conflict):
            registry.acquire(self.key, "two", "snapshot",
                             {"db:source": "shared-read", "db:target": "exclusive"}, {})
        self.assertEqual(registry.db.execute("SELECT count(*) FROM claims WHERE resource='db:source'")
                         .fetchone()[0], 0)

    def test_command_inherits_resolved_registry_and_lane_environment(self):
        marker = self.home / "environment.json"
        self.config["commands"]["environment"] = {
            "argv": [sys.executable, "-c",
                     "import json,os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text("
                     "json.dumps({k:v for k,v in os.environ.items() if k.startswith('LANES_')}))",
                     str(marker)],
            "env": {"LANES_REGISTRY": "must-be-overridden"}}
        self.commit_config()
        lane = self.lane()
        self.cli("run", "one", "environment")
        env = json.loads(marker.read_text())
        self.assertEqual(env["LANES_REGISTRY"], str(self.state))
        self.assertEqual(env["LANES_ID"], "one")
        self.assertEqual(env["LANES_WORKTREE"], lane["worktree"])
        self.assertEqual(env["LANES_PROJECT"], self.key)
        self.assertTrue(env["LANES_RUN_TOKEN"])
        self.assertTrue(Path(env["LANES_STATE_DIR"]).is_dir())

    def test_init_does_not_overwrite(self):
        original = (self.repo / "lanes.json").read_text()
        self.assertEqual(self.cli("init", ok=False).returncode, 2)
        self.assertEqual((self.repo / "lanes.json").read_text(), original)


if __name__ == "__main__":
    unittest.main()
