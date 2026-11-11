# Lanes

Coordinate parallel agent work with isolated Git worktrees and explicit claims on shared resources.

A **lane** owns one work scope, branch, worktree, runtime configuration, and resource claims. Agents can change without changing the lane. Worktrees isolate code; claims coordinate databases, datasets, fixed ports, browser profiles, and expensive verification.

Lanes includes an agent skill and a small CLI for macOS and Linux, using Python 3.9+ and Git with no third-party runtime dependencies. Claims are advisory and local to one host/user account. Participating clients must use the same registry and resource identities.

## Install

```sh
git clone https://github.com/libdx/lanes.git
cd lanes
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
```

Install the skill separately by copying `skills/lanes` into your agent's skills directory. For Codex, the usual location is `~/.codex/skills/lanes` (or `$CODEX_HOME/skills/lanes` when configured). Review an existing installation before replacing it. The repository does not install the skill globally or modify another project's instructions automatically.

Invoke it with: **“Use $lanes to organize parallel work with isolated worktrees and coordinated resource claims.”** The full [skill](skills/lanes/SKILL.md) also supports automatic discovery.

## Start a lane

In your own Git project, on its agreed integration branch:

```sh
lanes init --base main
# Edit lanes.json to define named commands, resources, and project hooks.
# Commit lanes.json to the base so new worktrees contain it.
lanes create api-change --scope 'Implement the API change' --branch ticket-12-api-change
lanes list
lanes run api-change test
lanes status
```

The repository's [example configuration](examples/lanes.json) includes a runnable `demo` command using lane-owned scratch storage. Copy it to your project's `lanes.json` and commit it before creating a lane; then run `lanes run api-change demo`. Its `inspect-dataset` command expects an existing shared reference directory. The `test` command assumes a project with Python unittest tests.

By default, worktrees live at `<primary-checkout-parent>/.worktrees/<project-key>/<lane-id>`. This keeps them outside ordinary repository searches, build contexts, and watchers. Override the prefix through configuration or `create --prefix`. Nested worktrees remain supported, with separate Git, Docker, watcher, and packaging exclusions. [Layout and migration considerations](skills/lanes/references/worktrees.md).

## Coordinate storage and services

Describe storage provisioning independently from access:

| Provisioning | Meaning |
| --- | --- |
| `fresh` | Initialize a lane-owned instance through project hooks |
| `copy` | Restore a supported consistent snapshot into a lane-owned instance |
| `shared` | Use one existing store across lanes |

Commands acquire **shared-read** or **exclusive** claims. Shared readers may coexist; exclusive claims conflict with every overlapping claim. All claims for a command are acquired atomically. Filesystem claims resolve symlinks and include conflicts between parent and descendant paths. Other resources use explicit namespaced identities shared across projects.

Use lane-specific ports, service names, endpoints, and writable storage where practical. Storage declarations do not provision anything automatically: project hooks use the appropriate database, filesystem, or container tooling. Never generically copy a running database's files. [Resource model and runtime lifetime](skills/lanes/references/resources.md).

## Failure and cleanup

Claims stay reserved after runner crashes or uncertain shutdown. They do not expire automatically. Inspect and stop all consumers before explicit recovery:

```sh
lanes status
lanes recover RUN_TOKEN --confirm-stopped --reason 'Inspected and stopped all consumers'
```

Foreground commands must remain in their process group. Detached services need explicit stop and shutdown-verification hooks, with claims held throughout their lifetime. Recovery is an operator attestation, not an automatic process killer or distributed fence.

After workers stop and commits are integrated or preserved:

```sh
lanes remove api-change --preserved-ref main
```

Removal refuses active runs, retained claims, dirty/untracked/ignored files, and commits absent from the preservation ref. It keeps the branch and lane-owned data and never removes shared storage. Squash integration requires a backup ref retaining the lane's actual commits.

See the [CLI/configuration contract](skills/lanes/references/cli.md) for hooks, environment expansion, registry selection, timeouts, and recovery. Global flags such as `--repo` and `--registry` precede the subcommand. The registry defaults to `~/.local/state/lanes/`, overridable with `LANES_REGISTRY`.

## Boundaries

V1 does not provide distributed locking, native Windows process supervision, automatic leases, generic storage drivers, automatic port allocation, or protection from applications that bypass the protocol. Keep its SQLite registry on a local filesystem. Windows users may run Lanes within WSL, with every coordinated consumer inside that boundary.

The skill generalizes coordinator/worker ownership, dependency ordering, handoffs, input routing, and integration without requiring a specific branch or task system. It does not grant permission to spawn agents, push, publish, or deploy. Existing repositories and worktrees are not migrated automatically.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 -m lanes --help
```

CI exercises Python 3.9 and 3.13 on macOS and Linux. Tests use temporary Git repositories and local storage. [MIT license](LICENSE), copyright 2026 libdx.
