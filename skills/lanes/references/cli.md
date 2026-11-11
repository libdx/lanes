# CLI and project contract

Install the CLI from the [Lanes repository](https://github.com/libdx/lanes) with `python3 -m pip install .` in a suitable Python environment. The installed command is `lanes`; the Python distribution is `agent-lanes`. Runtime dependencies are Python 3.9+ and Git on macOS or Linux. Windows users may run it entirely within WSL; Windows-host consumers are outside that coordination boundary.

Global arguments precede the subcommand:

```sh
lanes --repo /path/to/project --registry /local/shared/registry status
```

`--repo` defaults to the current directory and accepts any checkout in the project. `--registry` defaults to `LANES_REGISTRY`, otherwise `~/.local/state/lanes/`. All participating projects and terminals must use the same registry to coordinate shared resources. Treat local registry records as operational metadata, not project tickets.

## Setup and lane lifecycle

```sh
lanes init --base main
# Configure lanes.json, review its commands, and commit it on the agreed base.
lanes create api-change --scope 'Implement the API change' --branch ticket-12-api-change
lanes list
lanes run api-change test --timeout 30
lanes status api-change
# After integrating or creating an appropriate backup ref:
lanes remove api-change --preserved-ref main
```

`init` refuses to overwrite configuration. `create` defaults the branch name to the lane ID, honors `--base` and `--prefix`, and creates a new branch/worktree without switching the primary checkout. Lane IDs are 1–63 lowercase letters, digits, or hyphens, starting with a letter or digit. For existing ticket conventions, pass the full ticket branch name. Existing branches are not adopted. Configuration must be committed on the chosen base to appear in its worktrees.

`run` reads configuration from the lane's worktree. Relative resource paths resolve against the primary checkout so the same shared path has the same identity in every lane. Claims are acquired atomically, including the lane itself; two commands cannot run in the same lane concurrently, even with no resource claims. Conflicts fail immediately unless a finite timeout is given. No locks are held during bounded conflict waiting. Hooks and commands inherit the invoking environment; their stdout/stderr pass through. The CLI prints a run token first. Normal completion returns the command's exit code; coordination/configuration errors return 2.

`status` without a lane reports all runs in the registry, including other projects. The `state` field is durable metadata; `running` does not prove the PID is alive. `remove` requires a clean worktree (including ignored files), no registered run, and a ref containing its HEAD. It preserves branches and lane data. Confirm agents have stopped before removal; external editors are not tracked. A squash integration needs a separate backup ref containing the lane's actual commits.

Creation records `creating` before Git operations and `needs-inspection` if a failed operation leaves a directory. An abrupt creation/removal crash can leave an intermediate record: preserve files and refs and inspect Git/worktree state before further maintenance. V1 intentionally refuses automatic repair of ambiguous worktree operations. Never directly delete the worktree or registry as routine cleanup.

## Configuration

`lanes.json` is versioned, project-owned configuration. Named commands are argument arrays, never implicitly shell-expanded. JSON examples are in the repository's `examples/lanes.json`.

```json
{
  "version": 1,
  "base": "main",
  "prefix": null,
  "resources": {
    "database": {
      "kind": "storage",
      "provision": "fresh",
      "identity": "postgres:local/{project}/{lane}"
    }
  },
  "commands": {
    "test": {
      "argv": ["python3", "-m", "unittest", "discover"],
      "claims": {"database": "exclusive"},
      "env": {"APP_DATABASE": "{project}_{lane}"},
      "prepare": [],
      "stop": [],
      "verify_stopped": [],
      "detached": false
    }
  }
}
```

The above declares database isolation; projects must supply their own supported provisioning and lifecycle commands. Empty hooks do not create a database.

- `resources` maps local names to `kind` (`storage` or `resource`), explicit `identity`, and, for storage, `provision` (`fresh`, `copy`, or `shared`). Commands request `shared-read` or `exclusive` per resource name. Aliases within one command collapse to the stronger mode.
- A command requires `argv`; optional `claims` and `env` are objects, and `prepare`, `stop`, and `verify_stopped` are arrays of argument arrays.
- Preparation runs sequentially before the main command. Stop and verification hooks run even after partial preparation or startup failure. A failed stop/verification retains claims. A normal foreground command failure releases claims after confirmed shutdown.
- `detached: true` requires nonempty stop and verification hooks. The main command defines the operation's duration; a command that merely starts a daemon will proceed immediately to stop hooks. Put service startup in preparation and tests or a foreground session driver in the main command.
- Strings in identities, command/hook arguments, and command environment values expand `{lane}`, `{project}`, `{repo}`, `{worktree}`, and `{state}`. Other braces are literal. `{state}` is a lane-owned directory underneath the registry, created before execution. It is not deleted by lane removal.
- Commands also receive `LANES_ID`, `LANES_PROJECT`, `LANES_WORKTREE`, `LANES_STATE_DIR`, and `LANES_RUN_TOKEN`.

Use named commands for provisioning, snapshots, or deliberate storage cleanup under claims. Shared storage cleanup must be an explicit project operation, never a side effect of lane removal. Hooks run trusted project code with the caller's permissions: review configuration before executing an unfamiliar branch.

## Recovery

```sh
lanes status
# Inspect owner and recorded process groups; stop all consumers first.
lanes recover RUN_TOKEN --confirm-stopped --reason 'Verified the lane containers and local consumers stopped'
```

Tokens are scoped to the project selected by `--repo`. Reasons and run details are retained in the local registry's recovery audit. Automatic expiry is deliberately unsupported. A missing owner process alone is not sufficient evidence that its consumers stopped.
