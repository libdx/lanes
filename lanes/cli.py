"""Small, durable, advisory coordinator. No daemon or network service required."""
import argparse
import contextlib
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid


class LaneError(Exception):
    pass


class Conflict(LaneError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise LaneError(result.stderr.strip() or "Git command failed")
    return result.stdout.strip()


def project(repo):
    root = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = root / common
    common = common.resolve()
    # Git lists the primary checkout first. Paths with whitespace are preserved.
    primary = git(root, "worktree", "list", "--porcelain", "-z").split("\0")[0]
    if not primary.startswith("worktree "):
        raise LaneError("A non-bare primary checkout is required")
    root = Path(primary[len("worktree "):]).resolve()
    slug = re.sub(r"[^a-z0-9-]+", "-", root.name.lower()).strip("-") or "project"
    key = slug[:40] + "-" + hashlib.sha256(str(common).encode()).hexdigest()[:12]
    return root, key


def checked_name(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise LaneError("Lane IDs use 1–63 lowercase letters, digits, and hyphens")
    return value


def load_config(root):
    try:
        config = json.loads((Path(root) / "lanes.json").read_text())
    except (OSError, ValueError) as exc:
        raise LaneError("Cannot read lanes.json: " + str(exc)) from exc
    if not isinstance(config, dict) or config.get("version") != 1:
        raise LaneError("lanes.json must be an object with version: 1")
    if not isinstance(config.get("base"), str) or not config["base"]:
        raise LaneError("A base branch is required")
    if config.get("prefix") is not None and not isinstance(config["prefix"], str):
        raise LaneError("prefix must be a path string")
    for key in ("resources", "commands"):
        if not isinstance(config.get(key, {}), dict):
            raise LaneError(key + " must be an object")
    return config


def argv(value):
    if (not isinstance(value, list) or not value
            or not all(isinstance(v, str) and v for v in value)):
        raise LaneError("Commands and hooks must be nonempty argument arrays")
    return value


def expand(value, variables):
    for name, replacement in variables.items():
        value = value.replace("{" + name + "}", str(replacement))
    return value


def identity(value, variables, root):
    if not isinstance(value, str) or not value:
        raise LaneError("Every resource needs an explicit identity")
    value = expand(value, variables)
    if value.startswith("path:"):
        path = Path(value[5:]).expanduser()
        if not path.is_absolute():
            path = root / path
        return "path:" + str(path.resolve())
    if ":" not in value or any(c.isspace() for c in value):
        raise LaneError("Non-path identities need a namespace, e.g. docker:engine/volume")
    return value


def overlaps(a, b):
    if a == b:
        return True
    if a.startswith("path:") and b.startswith("path:"):
        pa, pb = Path(a[5:]), Path(b[5:])
        return pa in pb.parents or pb in pa.parents
    return False


class Registry:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.directory / "registry.sqlite3"), timeout=10,
                                  isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS lanes (
          project TEXT NOT NULL, id TEXT NOT NULL, repo TEXT NOT NULL,
          worktree TEXT NOT NULL UNIQUE, branch TEXT NOT NULL, base TEXT NOT NULL,
          scope TEXT NOT NULL, state TEXT NOT NULL, created TEXT NOT NULL,
          PRIMARY KEY(project,id));
        CREATE TABLE IF NOT EXISTS runs (
          token TEXT PRIMARY KEY, project TEXT NOT NULL, lane TEXT NOT NULL,
          pid INTEGER NOT NULL, purpose TEXT NOT NULL, started TEXT NOT NULL,
          state TEXT NOT NULL, details TEXT NOT NULL,
          FOREIGN KEY(project,lane) REFERENCES lanes(project,id));
        CREATE TABLE IF NOT EXISTS claims (
          token TEXT NOT NULL REFERENCES runs(token) ON DELETE CASCADE,
          resource TEXT NOT NULL, mode TEXT NOT NULL,
          PRIMARY KEY(token,resource));
        """)

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def lane(self, project_key, lane_id):
        row = self.db.execute("SELECT * FROM lanes WHERE project=? AND id=?",
                              (project_key, lane_id)).fetchone()
        if row is None:
            raise LaneError("Unknown lane: " + lane_id)
        return dict(row)

    def acquire(self, project_key, lane_id, purpose, claims, details):
        token = uuid.uuid4().hex
        with self.transaction():
            if self.lane(project_key, lane_id)["state"] != "ready":
                raise Conflict("Lane is not ready")
            existing = self.db.execute("SELECT * FROM runs WHERE project=? AND lane=?",
                                       (project_key, lane_id)).fetchone()
            if existing:
                raise Conflict("Lane already has run " + existing["token"])
            rows = self.db.execute("""SELECT c.*,r.lane,r.project,r.pid,r.purpose,r.started
                FROM claims c JOIN runs r USING(token)""").fetchall()
            for resource, mode in claims.items():
                for row in rows:
                    if overlaps(resource, row["resource"]) and (
                            mode == "exclusive" or row["mode"] == "exclusive"):
                        raise Conflict("Resource {} conflicts with {} held by {}/{} "
                                       "(run {}, pid {}, purpose {}, since {})".format(
                                           resource, row["resource"], row["project"], row["lane"],
                                           row["token"], row["pid"], row["purpose"], row["started"]))
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                            (token, project_key, lane_id, os.getpid(), purpose, now(),
                             "running", json.dumps(details)))
            self.db.executemany("INSERT INTO claims VALUES(?,?,?)",
                                [(token, resource, mode) for resource, mode in claims.items()])
        return token

    def release(self, token):
        with self.transaction():
            self.db.execute("DELETE FROM runs WHERE token=?", (token,))

    def uncertain(self, token):
        self.db.execute("UPDATE runs SET state='needs-recovery' WHERE token=?", (token,))


def variables_for(lane, registry):
    return {"lane": lane["id"], "project": lane["project"], "repo": lane["repo"],
            "worktree": lane["worktree"],
            "state": str(registry.directory / "data" / lane["project"] / lane["id"])}


def command_spec(config, name, variables, root):
    spec = config.get("commands", {}).get(name)
    if not isinstance(spec, dict):
        raise LaneError("Unknown command: " + name)
    command = [expand(v, variables) for v in argv(spec.get("argv"))]
    hooks = {}
    for key in ("prepare", "stop", "verify_stopped"):
        values = spec.get(key, [])
        if not isinstance(values, list):
            raise LaneError(key + " must be an array of argument arrays")
        hooks[key] = [[expand(v, variables) for v in argv(hook)] for hook in values]
    if not isinstance(spec.get("detached", False), bool):
        raise LaneError("detached must be a boolean")
    if spec.get("detached") and (not hooks["stop"] or not hooks["verify_stopped"]):
        raise LaneError("Detached services require stop and verify_stopped hooks")
    requests = spec.get("claims", {})
    if not isinstance(requests, dict):
        raise LaneError("claims must map resource names to access modes")
    claims = {}
    for name, mode in requests.items():
        if mode not in ("shared-read", "exclusive"):
            raise LaneError("Access mode must be shared-read or exclusive")
        resource = config.get("resources", {}).get(name)
        if not isinstance(resource, dict) or resource.get("kind") not in ("storage", "resource"):
            raise LaneError("Resource needs kind storage or resource: " + name)
        if resource["kind"] == "storage" and resource.get("provision") not in (
                "fresh", "copy", "shared"):
            raise LaneError("Storage needs provision fresh, copy, or shared: " + name)
        resource_id = identity(resource.get("identity"), variables, root)
        # Multiple aliases in one command collapse to the stronger claim.
        claims[resource_id] = ("exclusive" if mode == "exclusive"
                               or claims.get(resource_id) == "exclusive" else "shared-read")
    env = spec.get("env", {})
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
            and isinstance(v, str) for k, v in env.items()):
        raise LaneError("env must map environment variable names to strings")
    return command, hooks, claims, {k: expand(v, variables) for k, v in env.items()}


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


class Runner:
    """Each subprocess has a private process group; detached consumers need hooks."""
    def __init__(self, cwd, env, record=None):
        self.cwd, self.env = cwd, env
        self.record = record
        self.active = None
        self.interrupted = False
        self.signal_received = None
        self.active_signal_time = None
        self.uncertain = False

    def handle_signal(self, signum, frame):
        self.interrupted = True
        self.signal_received = signum
        self.active_signal_time = time.monotonic()
        if self.active is not None:
            try:
                os.killpg(self.active.pid, signum)
            except ProcessLookupError:
                pass

    def call(self, command, cleanup=False):
        if self.interrupted and not cleanup:
            raise LaneError("Interrupted before command start")
        self.active_signal_time = None
        child = subprocess.Popen(command, cwd=self.cwd, env=self.env, start_new_session=True)
        self.active = child
        if self.record:
            self.record(child.pid, command)
        try:
            while True:
                try:
                    code = child.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if (self.active_signal_time is not None
                            and time.monotonic() - self.active_signal_time > 3):
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        self.uncertain = True
            # Surviving descendants must stop before this command's claims can be freed.
            if group_exists(child.pid):
                os.killpg(child.pid, signal.SIGTERM)
                deadline = time.monotonic() + 3
                while group_exists(child.pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if group_exists(child.pid):
                    os.killpg(child.pid, signal.SIGKILL)
                    self.uncertain = True
            return code
        finally:
            self.active = None


def run_lane(args, registry, project_key):
    lane = registry.lane(project_key, args.lane)
    root = Path(lane["worktree"])
    config = load_config(root)
    variables = variables_for(lane, registry)
    command, hooks, claims, extra_env = command_spec(config, args.command, variables, root)
    if not math.isfinite(args.timeout) or args.timeout < 0:
        raise LaneError("timeout must be finite and nonnegative")
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            token = registry.acquire(project_key, args.lane, args.command, claims,
                                     {"argv": command, "worktree": str(root), "hooks": hooks})
            break
        except Conflict:
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    print("Run " + token, flush=True)
    runner = None
    old_signals = {}
    clean = False
    try:
        Path(variables["state"]).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(extra_env)
        env.update({"LANES_ID": args.lane, "LANES_PROJECT": project_key,
                    "LANES_WORKTREE": str(root), "LANES_STATE_DIR": variables["state"],
                    "LANES_RUN_TOKEN": token})
        def record_child(pid, child_command):
            row = registry.db.execute("SELECT details FROM runs WHERE token=?", (token,)).fetchone()
            details = json.loads(row["details"])
            details.setdefault("processes", []).append({"pid": pid, "pgid": pid,
                                                         "argv": child_command, "started": now()})
            registry.db.execute("UPDATE runs SET details=? WHERE token=?",
                                (json.dumps(details), token))
        runner = Runner(root, env, record_child)
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_signals[signum] = signal.signal(signum, runner.handle_signal)
        code = 1
        failure = None
        try:
            for hook in hooks["prepare"]:
                if runner.call(hook):
                    raise LaneError("Preparation hook failed")
            code = runner.call(command)
        except (OSError, LaneError) as exc:
            failure = exc
        # Stop even after partial preparation or failed startup.
        stopped = True
        for hook in hooks["stop"]:
            try:
                if runner.call(hook, cleanup=True):
                    stopped = False
            except OSError:
                stopped = False
        for hook in hooks["verify_stopped"]:
            try:
                if runner.call(hook, cleanup=True):
                    stopped = False
            except OSError:
                stopped = False
        clean = stopped and not runner.uncertain
        if failure:
            raise LaneError(str(failure))
        if not clean:
            raise LaneError("Shutdown uncertain; claims retained. Inspect and recover " + token)
        return (128 + runner.signal_received if runner.interrupted
                else (code if code >= 0 else 128 - code))
    finally:
        for signum, previous in old_signals.items():
            signal.signal(signum, previous)
        if clean:
            registry.release(token)
        else:
            registry.uncertain(token)
            print("Claims retained for run " + token, file=sys.stderr)


def create_lane(args, registry, root, project_key):
    checked_name(args.lane)
    config = load_config(root)
    base = args.base or config["base"]
    base_commit = git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}")
    branch = args.branch or args.lane
    git(root, "check-ref-format", "--branch", branch)
    prefix = Path(args.prefix or config.get("prefix") or root.parent).expanduser()
    if not prefix.is_absolute():
        prefix = root / prefix
    worktree = prefix.resolve() / ".worktrees" / project_key / args.lane
    if worktree.exists():
        raise LaneError("Worktree path already exists: " + str(worktree))
    with registry.transaction():
        registry.db.execute("INSERT INTO lanes VALUES(?,?,?,?,?,?,?,?,?)",
                            (project_key, args.lane, str(root), str(worktree), branch,
                             base, args.scope, "creating", now()))
    try:
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git(root, "worktree", "add", "-b", branch, str(worktree), base_commit)
    except BaseException:
        # Preserve a record if creation progressed far enough to leave a checkout.
        if worktree.exists():
            registry.db.execute("UPDATE lanes SET state='needs-inspection' WHERE project=? AND id=?",
                                (project_key, args.lane))
        else:
            registry.db.execute("DELETE FROM lanes WHERE project=? AND id=?", (project_key, args.lane))
        raise
    registry.db.execute("UPDATE lanes SET state='ready' WHERE project=? AND id=?",
                        (project_key, args.lane))
    print(json.dumps(registry.lane(project_key, args.lane), indent=2))


def remove_lane(args, registry, project_key):
    with registry.transaction():
        lane = registry.lane(project_key, args.lane)
        if lane["state"] != "ready":
            raise LaneError("Lane needs inspection; removal refused")
        if registry.db.execute("SELECT 1 FROM runs WHERE project=? AND lane=?",
                               (project_key, args.lane)).fetchone():
            raise Conflict("Lane has a run or retained claims; inspect/recover it first")
        registry.db.execute("UPDATE lanes SET state='removing' WHERE project=? AND id=?",
                            (project_key, args.lane))
    try:
        worktree = Path(lane["worktree"])
        # Includes ignored files: do not silently discard local artifacts or secrets.
        if git(worktree, "status", "--porcelain", "--untracked-files=all", "--ignored"):
            raise LaneError("Worktree has modified, untracked, or ignored files; preserve them first")
        head = git(worktree, "rev-parse", "HEAD")
        if not args.preserved_ref:
            raise LaneError("Supply --preserved-ref naming an integration or backup ref")
        preserved = git(lane["repo"], "rev-parse", "--verify", "--end-of-options", args.preserved_ref + "^{commit}")
        git(lane["repo"], "merge-base", "--is-ancestor", head, preserved)
        git(lane["repo"], "worktree", "remove", lane["worktree"])
    except BaseException:
        registry.db.execute("UPDATE lanes SET state='ready' WHERE project=? AND id=?",
                            (project_key, args.lane))
        raise
    registry.db.execute("DELETE FROM lanes WHERE project=? AND id=?", (project_key, args.lane))
    print("Removed worktree; branch and lane data preserved")


def parser():
    p = argparse.ArgumentParser(description="Coordinate parallel work lanes and resource claims")
    p.add_argument("--repo", default=".", help="Any checkout of the target repository")
    p.add_argument("--registry", default=os.environ.get(
        "LANES_REGISTRY", "~/.local/state/lanes"), help="Shared host-local registry directory")
    sub = p.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", help="Create lanes.json in the primary checkout")
    init.add_argument("--base", default="main")
    init.add_argument("--prefix")
    create = sub.add_parser("create", help="Create a lane branch and worktree")
    create.add_argument("lane")
    create.add_argument("--scope", required=True)
    create.add_argument("--branch")
    create.add_argument("--base")
    create.add_argument("--prefix")
    sub.add_parser("list", help="List this project's lanes")
    status = sub.add_parser("status", help="Show all host claims or one lane")
    status.add_argument("lane", nargs="?")
    run = sub.add_parser("run", help="Run a configured command under resource claims")
    run.add_argument("lane")
    run.add_argument("command")
    run.add_argument("--timeout", type=float, default=0)
    recover = sub.add_parser("recover", help="Release a run after manually verifying shutdown")
    recover.add_argument("token")
    recover.add_argument("--confirm-stopped", action="store_true", required=True)
    recover.add_argument("--reason", required=True)
    remove = sub.add_parser("remove", help="Remove a clean, preserved, inactive worktree")
    remove.add_argument("lane")
    remove.add_argument("--preserved-ref", required=True)
    return p


def main(argv_args=None):
    args = parser().parse_args(argv_args)
    registry = None
    try:
        if sys.platform == "win32":
            raise LaneError("Native Windows is unsupported; use Linux inside WSL")
        root, project_key = project(args.repo)
        if args.action == "init":
            git(root, "rev-parse", "--verify", "--end-of-options", args.base + "^{commit}")
            with (root / "lanes.json").open("x") as file:
                json.dump({"version": 1, "base": args.base, "prefix": args.prefix,
                           "resources": {}, "commands": {}}, file, indent=2)
                file.write("\n")
            print("Created lanes.json; configure and commit it before creating lanes")
            return 0
        registry = Registry(args.registry)
        if args.action == "create":
            create_lane(args, registry, root, project_key)
        elif args.action == "list":
            print(json.dumps([dict(row) for row in registry.db.execute(
                "SELECT * FROM lanes WHERE project=? ORDER BY created", (project_key,))], indent=2))
        elif args.action == "status":
            output = {"registry": str(registry.directory)}
            if args.lane:
                output["lane"] = registry.lane(project_key, args.lane)
            query = "SELECT * FROM runs"
            params = ()
            if args.lane:
                query += " WHERE project=? AND lane=?"
                params = (project_key, args.lane)
            output["runs"] = []
            for row in registry.db.execute(query, params):
                item = dict(row)
                item["details"] = json.loads(item["details"])
                item["claims"] = [dict(claim) for claim in registry.db.execute(
                    "SELECT resource,mode FROM claims WHERE token=?", (row["token"],))]
                output["runs"].append(item)
            print(json.dumps(output, indent=2))
        elif args.action == "run":
            return run_lane(args, registry, project_key)
        elif args.action == "recover":
            if not args.reason.strip():
                raise LaneError("Recovery needs a nonempty inspection reason")
            with registry.transaction():
                row = registry.db.execute("SELECT * FROM runs WHERE token=? AND project=?",
                                          (args.token, project_key)).fetchone()
                if row is None:
                    raise LaneError("Unknown run in this project")
                # Reject known-live owners. A missing PID is not sufficient evidence;
                # the required operator attestation covers detached consumers.
                try:
                    os.kill(row["pid"], 0)
                except ProcessLookupError:
                    pass
                else:
                    raise LaneError("Runner PID is still present; stop and inspect it first")
                registry.db.execute("""CREATE TABLE IF NOT EXISTS recoveries (
                    token TEXT PRIMARY KEY, recovered TEXT, reason TEXT, details TEXT)""")
                registry.db.execute("INSERT INTO recoveries VALUES(?,?,?,?)",
                                    (args.token, now(), args.reason, json.dumps(dict(row))))
                registry.db.execute("DELETE FROM runs WHERE token=?", (args.token,))
            print("Recovered " + args.token)
        elif args.action == "remove":
            remove_lane(args, registry, project_key)
        return 0
    except (LaneError, OSError, sqlite3.Error, ValueError) as exc:
        print("lanes: " + str(exc), file=sys.stderr)
        return 2
    finally:
        if registry is not None:
            registry.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
