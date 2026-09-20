# jj-pile

`jj-pile` publishes individual Jujutsu changes as GitHub PRs. It keeps
git-pile's small, individually reviewable changes and integrated testing,
using jj's graph as the source of truth. The existing Git commands remain
available separately.

## Install

```sh
nix profile install .#jj-pile
```

With Home Manager, import `homeManagerModules.default` from this flake and
enable the module. It installs `jj-pile`, enables Jujutsu, and configures the
`jj pile` alias automatically:

```nix
programs.jj-pile.enable = true;
```

Or add this repository's `bin` to `PATH` and install Python 3.10+, Git,
Jujutsu, and `gh`. Tested with jj 0.36.0 and 0.41.0. Authenticate with `gh auth login`.
Both colocated and non-colocated jj Git repositories work.

If you are not using the Home Manager module, invoke it as `jj pile` by adding
this to your jj configuration:

```toml
[aliases]
pile = ["util", "exec", "--", "jj-pile"]
```

Jujutsu's dynamic shell completions include configured aliases such as `pile`;
its standard, statically generated completions do not.

Examples below use that alias; `jj-pile` accepts the same arguments.

## Start with one change

```sh
jj git clone git@github.com:example/project.git
cd project
# Edit files. jj snapshots the working copy automatically.
jj describe -m 'Add a feature'
jj pile submit
jj new
```

Submission uses `@` by default. If `@` is the empty, undescribed working-copy
change created by `jj new` or `jj commit`, submission uses its parent. Pass
`-r @` to select the working-copy change explicitly. Other commands do not
fall back, and revsets matching multiple changes are rejected.

The remote defaults to `origin`. `--remote NAME` selects another remote;
the default PR base comes from that repository's GitHub default branch.
`submit --base release` selects a different fetched base branch.
`--repo OWNER/REPO` overrides GitHub repository detection from the remote
URL. The Git remote and GitHub repository must refer to the same repository;
cross-repository fork PRs and GitLab are not implemented.

## Independent reviews, integrated testing

Create independent changes as siblings off the fetched trunk. Combine them
in a merge working copy to build and test everything together:

```sh
jj new main@origin
# Implement feature A.
jj describe -m 'Feature A'
jj log # note A's change ID
jj pile submit

jj new main@origin
# Implement feature B.
jj describe -m 'Feature B'
jj log # note B's change ID
jj pile submit

jj new <A> <B>
# Build and test A + B together here.
```

Replace `<A>` and `<B>` with actual change IDs. The merge working copy is
for local integration; publish the individual changes with `-r <A>` or
`-r <B>`. Publishing an ancestor does not switch your working copy.
If two changes fundamentally depend on each other, stack them instead.

## Dependent reviews

```sh
jj new <A>
# Implement a change that depends on A.
jj describe -m 'Build on feature A'
jj pile submit --onto <A>
```

`--onto` requires A to have an open PR and its current version to be pushed.
The child PR targets A's review bookmark. Publishing to trunk would include
A as well, so the command refuses that accidental multi-change PR.

After editing a parent, jj rebases descendants automatically. Update PRs in
parent-to-child order so each child's published base matches its local parent.

## Incorporate review feedback

Use jj's existing editing operations, then publish the rewritten change:

```sh
jj edit <A>
# Edit files.
jj pile update
```

Or stay in the integration workspace and move feedback into the relevant
change with `jj squash --into <A>` or `jj absorb`. Inspect `jj diff -r <A>`
and run `jj pile update -r <A>`. When `jj absorb` edits several changes,
update each affected PR. Normal jj conflict resolution applies.

There are deliberately no separate headpr, replacepr, absorb, or rebasepr
wrappers. A review identifies a jj change; editing that change updates its
bookmark automatically. Its full change ID determines the stable bookmark
`pile/<change-id>`, so changing the description does not lose the PR.
The bookmark is an ordinary jj bookmark and a normal Git branch on GitHub.

## Refresh and finish

```sh
jj git fetch --remote origin
jj rebase -s <A> -d main@origin
jj pile update -r <A>
```

For a stack whose parent was squash-merged, first ensure GitHub has retargeted
the child PR to `main` (or change its base using GitHub/`gh`). Fetch, rebase
the remaining child and its descendants with `jj rebase -s <child> -d main@origin`,
then update the child PR. `update` reads the PR's current base from GitHub.
After verifying the merged result, use native `jj abandon` for obsolete
local changes and native bookmark commands for cleanup. There is no automatic
merge, abandonment, or remote branch deletion.

## Commands and recovery

| Command | Behavior |
| --- | --- |
| `jj pile submit [-r REV] [--base BRANCH \| --onto REV] [--draft]` | Push one change and create a PR from its description. |
| `jj pile update [-r REV]` | Push a rewritten change to its existing open PR. |
| `jj pile open [-r REV]` | Open that change's PR in the browser, including closed PRs. |
| `jj pile status [-r REVSET]` | Show PR state and URLs; defaults to `trunk()..@`. |

Running `jj pile` without a subcommand is shorthand for `jj pile status`.

Every command accepts `--remote` and `--repo`. To list all local branches
of work, use `jj pile status -r 'trunk()..visible_heads()'`.

The tool requires a nonempty, described, conflict-free change with one parent.
It checks that the fetched base-to-change range contains exactly one commit
before pushing. It does not cherry-pick or project a linear pile onto separate
review branches. Reshape an existing linear pile using native `jj rebase`,
or submit it as an explicit stack.

Pushes use jj's remote-state checks and private-change rules. Commands do not
fetch automatically. If someone changes a review branch remotely, inspect it
with `jj git fetch` and resolve any bookmark conflict before retrying. Neither
remote edits nor conflicts are automatically overwritten.

If pushing succeeds but PR creation fails, the bookmark remains available.
Retry `submit` to finish creating the PR. If the PR already exists, `submit`
prints its URL without republishing; use `update` to publish edits. A closed
or merged PR is not reused for a new submission. Updates preserve the PR's
existing title and body, including edits made on GitHub.

Local operations remain visible in `jj op log`. Use jj's operation recovery
for local mistakes; it does not undo GitHub PR creation or remote pushes.
No second workspace, private state database, or commit-message trailers are
created.

## Development

```sh
python3 -m unittest discover -s tests -v
nix build .#jj-pile
```

Integration tests use real jj repositories and local bare Git remotes with a
fake `gh`, so they do not contact GitHub. The jj tests skip when jj is absent;
the `jj-pile` Nix package supplies jj and runs both command suites.

The design follows jj's native [bookmark semantics](https://docs.jj-vcs.dev/latest/bookmarks/)
and [external command aliases](https://docs.jj-vcs.dev/latest/cli-reference/#jj-util-exec).
