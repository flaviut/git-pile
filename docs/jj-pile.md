# jj-pile

`jj-pile` publishes individual Jujutsu changes as GitHub PRs. It keeps
git-pile's small, individually reviewable changes and integrated testing,
using explicit PR dependencies independently of jj's local graph. The existing
Git commands remain available separately.

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
Jujutsu, and `gh`. Publishing uses `jj duplicate --onto`.
Authenticate with `gh auth login`.
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

The upstream remote defaults to `origin`. When a `mine` remote exists, review
bookmarks are pushed there and fork-qualified PRs are opened against `origin`,
matching `git submitpr`. `--remote NAME` explicitly uses one remote for both
roles. The default PR base comes from the upstream repository's default branch.
`submit --base release` selects a different fetched base branch.
`--repo OWNER/REPO` overrides upstream GitHub repository detection from the
remote URL. GitLab is not implemented.

## Independent reviews, integrated testing

Local ancestry does not declare a PR dependency. Without `--onto`, `submit`
applies only the selected change's diff onto the fetched default branch (or
`--base`). You can keep a linear local stack and submit its changes in any order:

```sh
# Local stack: main -> A -> B -> C
jj pile submit -r <C>
jj pile submit -r <A>
jj pile submit -r <B>
```

Each PR contains one change. Publishing creates a separate commit and leaves
your source changes, their parents, and the working copy intact. If the diff
conflicts with the selected base, publication stops; resolve the source change
or declare the required dependency with `--onto`. A clean application does not
guarantee semantic independence: build and test against the intended base.

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
Declare actual PR dependencies with `--onto`.

## Dependent reviews

```sh
jj new <A>
# Implement a change that depends on A.
jj describe -m 'Build on feature A'
jj pile submit --onto <A>
```

`--onto` explicitly declares a dependency. A must have an open PR and a fetched
review branch. The selected change's diff is applied onto A's published head;
unpublished local edits to A are not included. A need not be the selected
change's local parent. For example, with local `main -> A -> B`, submit B
independently, then `jj pile submit -r <A> --onto <B>` to publish `main -> B -> A`.

After editing a dependency, update PRs in dependency order so each dependent
PR uses the updated published base. `update` reads its existing base from
GitHub and reapplies the source diff onto the fetched head of that branch.

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
wrappers. A review identifies a source jj change. Its full change ID determines
the stable bookmark `pile/<change-id>`, so changing the description does not lose
the PR. The bookmark points to a separate publishing commit; editing the
source changes the PR only when you run `update`. The bookmark is an ordinary
jj bookmark and a normal Git branch on GitHub. Keep editing and selecting the
original source change, not the generated publishing commit.

## Refresh and finish

```sh
jj git fetch --remote origin
jj pile update -r <A>
```

For a stack whose parent was squash-merged, first ensure GitHub has retargeted
the child PR to `main` (or change its base using GitHub/`gh`). Fetch and run
`jj pile update -r <child>`. You can rebase your local stack separately when
convenient. `update` reads the PR's current base from GitHub.
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

Every command accepts `--remote` and `--repo`. An explicit `--remote` also
disables automatic use of `mine`. To list all local branches
of work, use `jj pile status -r 'trunk()..visible_heads()'`. This broad revset
also includes generated publishing commits; select source revisions to query
their PRs.

The tool requires a nonempty, described, conflict-free change with one parent.
It duplicates that change onto the fetched PR base and rejects a conflicted or
empty result before pushing. Only the selected diff is applied; local ancestors
are never implicitly included. Merge changes remain for local integration.

Pushes use jj's remote-state checks and private-change rules. Commands do not
fetch automatically. If someone changes a review branch remotely, inspect it
with `jj git fetch`. A local `pile-published/<remote>/<full-change-id>` bookmark
records the last successful publication, so even a fetched remote auto-fix
blocks an update until you acknowledge it. These bookkeeping bookmarks are
not pushed by jj-pile; avoid including them in a manual `jj git push --all`.

After inspecting the remote diff and incorporating the desired edits into the
source change, acknowledge the fetched head by setting both local bookmarks:

```sh
jj bookmark set pile/<full-change-id> pile-published/origin/<full-change-id> \
  -r 'pile/<full-change-id>@origin' --allow-backwards
jj pile update -r <source-change-id>
```

Replace `origin` with the push remote, such as `mine`, when applicable. This is
an explicit acknowledgment that the next update may replace that remote head.
Do not acknowledge edits you have not reviewed. The same procedure can restore
bookkeeping after an interrupted push or when adopting a review in another
clone. Existing reviews from older jj-pile versions are adopted automatically
when their heads still identify the original source change.

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
