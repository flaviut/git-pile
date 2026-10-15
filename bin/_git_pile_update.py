"""Prepare off-branch, publish with a lease, then advance local refs."""

import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from enum import Enum
from pathlib import Path

from _git_pile_git import (
    ask,
    branch_busy,
    branch_name,
    check_idle,
    exists,
    fail,
    git,
    output,
    ref_commands,
    update_refs,
    worktree_path,
)


class UpdateMode(Enum):
    APPEND = "append"
    SQUASH = "squash"


class UpdateGuard(Enum):
    CHECK = "check"
    FORCE = "force"


def _ancestor(older, newer):
    result = git("merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode > 1:
        result.check_returncode()
    return result.returncode == 0


def _pile_target(target, branch, head):
    commit = output("rev-parse", "--verify", target + "^{commit}")
    if _ancestor(commit, head + "^"):
        return commit
    candidates = output("rev-list", "@{upstream}.." + head + "^").splitlines()
    matches = [ref for ref in candidates if branch_name(ref) == branch]
    if len(matches) != 1:
        fail("target must identify one earlier commit in the pile")
    return matches[0]


def start_update(target, mode, guard):
    source = output("rev-parse", "--show-toplevel")
    check_idle(source)
    source_ref = git("symbolic-ref", "--quiet", "HEAD", capture=True, check=False)
    if source_ref.returncode:
        fail("updatepr requires a checked-out pile branch")
    head = output("rev-parse", "HEAD")
    if exists(branch_name(head)) and guard is UpdateGuard.CHECK:
        fail("a PR exists for HEAD; pass --force to update another PR")
    branch = target if exists(target) else branch_name(target)
    if not exists(branch):
        fail(f"branch '{branch}' doesn't exist")
    upstream = output(
        "for-each-ref",
        "--format=%(upstream:remotename)%00%(upstream:remoteref)",
        "refs/heads/" + branch,
    ).split("\0")
    if len(upstream) != 2 or not all(upstream) or upstream[0] == ".":
        fail(f"'{branch}' must track a remote branch")
    plan = {
        "source": source,
        "source_ref": source_ref.stdout.strip(),
        "head": head,
        "branch": branch,
        "branch_head": output("rev-parse", "refs/heads/" + branch),
        "target": _pile_target(target, branch, head),
        "remote": upstream[0],
        "remote_ref": upstream[1],
    }
    _check_refs(plan)
    with _scratch() as root:
        prefix = "refs/git-pile/" + root.name
        try:
            pr = _prepare_pr(plan, mode, root / "pr", prefix)
            pile = _prepare_pile(plan, root / "pile")
        except subprocess.CalledProcessError:
            fail(
                "preparation failed; reconcile your pile or PR branch, then rerun git updatepr"
            )
        _check_refs(plan)
        _finish(plan, pr, pile, prefix)


@contextmanager
def _scratch():
    cache = worktree_path()
    parent = cache.parent / "updates" / cache.name
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="update-", dir=parent))
    try:
        yield root
    finally:
        for name in ("pr", "pile"):
            path = root / name
            if path.exists():
                git("worktree", "remove", "--force", str(path), check=False)
        git("update-ref", "-d", "refs/git-pile/" + root.name + "/upstream", check=False)
        if not any(root.iterdir()):
            root.rmdir()


def _rebase(path, *args):
    git(
        "-c",
        "rebase.updateRefs=false",
        "-c",
        "rebase.autoStash=false",
        "-c",
        "rebase.autoSquash=false",
        "rebase",
        *args,
        cwd=path,
        env={"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"},
    )


def _prepare_pr(plan, mode, path, prefix):
    git(
        "fetch",
        "--quiet",
        "--no-tags",
        plan["remote"],
        plan["remote_ref"] + ":" + prefix + "/upstream",
    )
    plan["remote_head"] = output("rev-parse", prefix + "/upstream")
    git("worktree", "add", "--quiet", "--detach", str(path), plan["branch_head"])
    if not _ancestor(plan["remote_head"], plan["branch_head"]):
        git("diff", plan["branch_head"] + "..." + plan["remote_head"])
        if not ask("Upstream changed. Rebase the prepared PR onto it? [y/N]: "):
            fail("upstream update declined")
        _rebase(path, plan["remote_head"])
    previous = output("rev-parse", "HEAD", cwd=path)
    git("cherry-pick", plan["head"], cwd=path)
    if mode is UpdateMode.SQUASH:
        git("reset", "--soft", previous, cwd=path)
        git(
            "commit",
            "--quiet",
            "--amend",
            "--no-edit",
            "--signoff",
            "--no-verify",
            cwd=path,
        )
    return output("rev-parse", "HEAD", cwd=path)


def _prepare_pile(plan, path):
    git("worktree", "add", "--quiet", "--detach", str(path), plan["head"])
    git(
        "commit",
        "--quiet",
        "--amend",
        "--signoff",
        "--no-verify",
        "-m",
        "fixup! " + plan["target"],
        cwd=path,
    )
    _rebase(path, "--interactive", "--autosquash", plan["target"] + "^")
    # Identical trees allow ref updates without touching the caller's files or index.
    if output("rev-parse", "HEAD^{tree}", cwd=path) != output(
        "rev-parse", plan["head"] + "^{tree}"
    ):
        fail("prepared pile changes the final tree; resolve in your pile and retry")
    return output("rev-parse", "HEAD", cwd=path)


def _check_refs(plan):
    check_idle(plan["source"])
    if output("symbolic-ref", "HEAD", cwd=plan["source"]) != plan["source_ref"]:
        fail("pile worktree changed branches")
    for ref, expected in (
        (plan["source_ref"], plan["head"]),
        ("refs/heads/" + plan["branch"], plan["branch_head"]),
    ):
        if output("rev-parse", ref) != expected:
            fail(f"'{ref}' moved; expected {expected}")
    if branch_busy(plan["branch"]):
        fail(
            f"'{plan['branch']}' is in use; finish its Git operation and detach that worktree"
        )


def _push(plan, pr):
    result = git(
        "push",
        "--quiet",
        f"--force-with-lease={plan['remote_ref']}:{plan['remote_head']}",
        plan["remote"],
        pr + ":" + plan["remote_ref"],
        cwd=plan["source"],
        check=False,
    )
    if result.returncode == 0:
        return True
    # A lost response can follow a successful push; verify before discarding candidates.
    refs = output("ls-remote", "--refs", plan["remote"], plan["remote_ref"]).split()
    return bool(refs) and refs[0] == pr


def _finish(plan, pr, pile, prefix):
    updates = [
        ("refs/heads/" + plan["branch"], pr, plan["branch_head"]),
        (plan["source_ref"], pile, plan["head"]),
    ]
    pins = [prefix + "/pr", prefix + "/pile"]
    update_refs([(pins[0], pr, None), (pins[1], pile, None)])
    keep = True
    message = "Publication outcome unknown; inspect the remote before local recovery."
    try:
        if not _push(plan, pr):
            keep = False
            fail("publication failed; local branches are unchanged")
        message = "PR published; local finalization failed."
        _check_refs(plan)
        update_refs(updates, cwd=plan["source"])
        keep = False
    finally:
        if keep:
            print(
                message
                + "\nPrepared commits: "
                + ", ".join(pins)
                + "\nAfter reconciling local refs, finalize with:\n"
                + "git update-ref --stdin <<'EOF'\n"
                + ref_commands(updates)
                + "EOF\n"
                + "Then remove the recovery refs:\n"
                + "\n".join("git update-ref -d " + shlex.quote(ref) for ref in pins),
                file=sys.stderr,
            )
        else:
            for ref in pins:
                git("update-ref", "-d", ref, check=False)
