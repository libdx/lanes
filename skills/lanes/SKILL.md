---
name: lanes
description: Coordinate parallel agent work using isolated Git worktrees, runtime resources, and storage claims. Use when splitting, supervising, integrating, or cleaning up concurrent work lanes.
---

# Lanes

A **lane** owns one work scope, branch, worktree, runtime configuration, and resource claims. Agents are assigned to lanes; replacing an agent does not replace the lane.

## Establish the batch

Read the project's agent instructions and task rules. Identify dependencies, overlapping files or models, acceptance criteria, and runtime/storage needs before delegating. Parallelize independent work; sequence dependent changes and substantial overlap. Do not spawn agents unless the current environment and user authorization allow delegation.

Keep a coordinator checkout for integration. Give every editing worker its own lane and absolute worktree path, branch, scope, expected artifact, acceptance criteria, and file-ownership boundaries. Workers stay inside their assigned worktrees and must not switch branches. Read-only analysis may share the coordinator checkout. Avoid ticket implementation in the coordinator checkout while editing workers are active.

Use project branch and ticket conventions and the agreed base; do not assume `main` or require a particular task system. Default to external worktrees at `<PREFIX>/.worktrees/<project-key>/<lane-id>`, with `PREFIX` equal to the primary checkout's parent. Honor sandbox and project constraints before choosing a path. Read [worktrees.md](references/worktrees.md) when selecting a layout or planning migration.

## Coordinate execution

Inventory shared resources before starting development or verification. A worktree isolates checked-out files, not ports, databases, volumes, browser profiles, or expensive build slots. Assign lane-specific service names, endpoints, ports, and writable storage where practical. Read [resources.md](references/resources.md) before running consumers of shared resources.

For every storage, distinguish provisioning (**fresh**, **copy**, **shared**) from access (**shared-read**, **exclusive**). Default shared writable storage to exclusive use. Hold every required claim before provisioning, starting consumers, or running verification, through confirmed shutdown. Do not treat read-only mounts, database transactions, or `git worktree lock` as lane coordination.

Use the installed `lanes` CLI and a common host-local registry when available; read [cli.md](references/cli.md) for setup and lifecycle. Do not imply enforcement when commands bypass the helper. If the CLI is unavailable, prepare the resource inventory and request its setup before concurrent shared-resource use; independent isolated work may continue. Do not substitute an informal check-then-write lock file.

## Supervise and integrate

Keep integration and shared task-state ownership with the coordinator. Workers must not merge into the integration branch, push, close tickets, or update shared dashboards unless explicitly delegated and authorized. This skill does not grant publication or deployment permission.

Workers continue safe independent work when input is missing. Route questions to the coordinator with lane/ticket, question, impact, affected scope, blocking status, and a recommended default when available. Never invent personal facts, evidence, or business decisions. The coordinator deduplicates questions, asks one decision at a time unless tightly coupled, routes answers back, and resumes affected work. Persist unresolved decisions in the project's work artifact rather than duplicating dashboards. Mark an entire lane blocked only when no meaningful in-scope work remains.

Require each handoff to identify commits, changed files, verification, unresolved decisions, and blockers. Review and integrate in dependency order, resolve conflicts, rerun proportionate checks, and synchronize task status under project rules. Push only within existing authorization.

Before cleanup, confirm no worker or consumer is using the lane, all claims are resolved, and useful commits and local artifacts are preserved. Remove worktrees through Git; never delete shared storage as incidental cleanup. Treat crash-retained claims as active until consumers have been inspected and stopped.
