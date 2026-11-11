# Worktree layout

Prefer `<PREFIX>/.worktrees/<project-key>/<lane-id>`. The primary checkout's parent is the default prefix. CLI overrides take precedence over `lanes.json`; relative prefixes resolve against the primary checkout. A project key combines the repository name with a hash of its canonical Git common directory, separating clones with identical names. Linked worktrees resolve to the same project.

External worktrees stay outside searches, root-scoped watchers, package discovery, Docker contexts rooted at the checkout, and bind mounts of that checkout. They share Git objects without copying the whole repository history. Access may require a wider sandbox; choose an authorized prefix rather than assuming the parent is writable.

Nested worktrees are a supported layout: set the prefix to the primary checkout. Use this when a sandbox or tooling requires it. Explicitly exclude `.worktrees/` from Git, each applicable Docker build context, watchers, archive/package tools, and recursive test discovery. Hidden directories are not universally excluded. `.gitignore` does not configure Docker, and `.dockerignore` does not exclude files from bind mounts. Broader build contexts can include external worktrees too; inspect the actual context boundary.

The default checkout stays on the project's integration branch while editing workers run. Linked worktrees have separate indexes and checked-out files but share branches, refs, objects, and administrative state. Serialize integration, shared task updates, and branch maintenance through the coordinator. `git worktree lock` protects worktree administration; it is not a consumer mutex.

## Existing worktrees

Do not move active workers. Inventory with `git worktree list --porcelain`, preserve commits and local artifacts, stop consumers, and check project-specific submodule restrictions. Use `git worktree move` for supported linked worktrees, then update lane records and local paths only after validation. Lanes v1 has no import/move command: migrate unmanaged worktrees separately and create new managed lanes afterward. Moving the primary checkout changes its path-derived project identity; keep the old registry available until all old lanes and claims are closed.

Adoption by an existing repository is optional: reference the installed skill while preserving project-specific branch and task conventions. Do not migrate existing worktrees or change project policies merely as a side effect of installing the skill.

Sources: [Git worktrees](https://git-scm.com/docs/git-worktree), [Docker build contexts](https://docs.docker.com/build/concepts/context/).
