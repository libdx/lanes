# Resource coordination

A **resource** is something whose use can conflict across lanes. A **storage** is a resource that retains data: a directory, database, Docker volume, object-store prefix, or snapshot. Resource identity describes the underlying thing, not its local nickname.

## Provisioning and access are separate

| Provisioning | Meaning | Typical use |
| --- | --- | --- |
| `fresh` | Project hooks initialize a new lane-owned instance | Empty test database or scratch volume |
| `copy` | Project hooks restore a consistent copy into a lane-owned target | Database dump/restore or supported snapshot |
| `shared` | Lanes reference the same existing instance | Large dataset or expensive database |

Provisioning declarations describe intent; the CLI does not copy, initialize, or delete storage automatically. Use project-supported commands in `prepare` hooks or named provisioning commands. Repeated preparation must be safe or explicitly fail rather than erase existing work. Protect both a snapshot source and its destination. Never copy a running database's raw files unless the engine explicitly supports that procedure.

| Claim | Compatible with |
| --- | --- |
| `shared-read` | Other `shared-read` claims |
| `exclusive` | No overlapping claims |

Fresh/copied stores should use lane-specific identities, endpoints, paths, or namespaces. Shared writable stores default to exclusive claims. A read-only mount does not prevent a writer in another lane from changing its view. Shared-read requires a workload that does not mutate the resource, including incidental writes. Database transactions and isolated schemas do not automatically make migrations, extensions, resets, or global settings safe to run concurrently.

## Identity and conflict boundaries

- Filesystem identities use `path:/absolute/path` or a path relative to the primary checkout. The CLI expands `~`, resolves symlinks and `..`, and conflicts parent and descendant paths. Use a single canonical spelling on case-insensitive filesystems; hard-link aliases and filesystem remounts are not detected.
- Other identities use an explicit namespace, such as `postgres:local-cluster/app`, `docker:desktop/reference-data`, or `port:localhost/5432`. All projects using one underlying object must use the same identity. Include server/engine/cluster identity where relevant. Do not put credentials in identities or command arguments.
- Overlap for non-filesystem identities is exact matching. Use one cluster-level identity for operations that affect the whole cluster. The CLI cannot discover that differently named database endpoints or volumes are aliases.
- Register non-storage bottlenecks as `kind: resource`: GPU slots, fixed ports, browser profiles, or resource-heavy PDF verification. Capacity counting is outside v1; use one exclusive identity to serialize a bottleneck.

Lanes claims are advisory. All participants must use the same registry, resource identities, and access contract. They do not prevent writes by unrelated applications or users. Keep the SQLite registry on a local filesystem, not NFS or cloud sync; multi-host and multi-user fencing are outside v1. Never delete or reset a registry to clear a conflict.

## Runtime lifetime

Use lane-specific ports, temp/output directories, service names, and Compose project names. Project hooks choose and configure ports; Lanes does not allocate sockets. A unique Compose project name isolates default names, but explicit volume names, external volumes/networks, host ports, bind mounts, and `container_name` can still collide.

A foreground command must not daemonize or leave independent consumers. The runner supervises its process group and attempts to stop surviving descendants. Programs that escape the process group, Docker containers, remote jobs, and other detached consumers require `detached: true`, stop hooks, and verification hooks. Hooks must verify the actual external consumers and complete before claims release. Declare all resources used by the command and its hooks; do not recursively run the same lane inside a hook.

For Compose, a project may use preparation to start dependencies, run tests as its main command, stop the project's services afterward, and verify they stopped. Pass a lane-specific project name to every lifecycle command. Avoid `down --volumes` for shared stores. Verification must fail when consumers are still alive; a successful `docker` startup/stop request alone is not proof of shutdown.

## Crashes and recovery

Claims survive runner death, hook exceptions, and uncertain shutdown. Status exposes the owner PID, process-group history, arguments, purpose, and acquisition time. A PID can be reused: process IDs and timestamps are evidence for inspection, never a lease or automatic expiry mechanism.

Inspect and stop all consumers, including detached services, before `recover`. Recovery requires a reason and explicit confirmation, refuses a currently present owner PID, and records an audit entry. A live unrelated process reusing the PID also causes a conservative refusal; wait until that PID is absent rather than editing the registry. Recovery does not stop processes or repair data. It is an operator attestation, not fencing.

Sources: [Compose project names](https://docs.docker.com/compose/how-tos/project-name/), [Compose volumes](https://docs.docker.com/reference/compose-file/volumes/).
