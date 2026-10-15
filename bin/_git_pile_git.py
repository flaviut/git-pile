"""Git execution and shared repository queries."""

import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path


def run(*args, cwd=None, capture=False, check=True, silent=False, env=None, input=None):
    if os.environ.get("GIT_PILE_VERBOSE"):
        print("+ " + shlex.join(str(arg) for arg in args), file=sys.stderr)
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        check=check,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL if silent else None,
        stderr=subprocess.DEVNULL if silent else None,
        env={**os.environ, **env} if env else None,
        input=input,
    )


def git(*args, **kwargs):
    return run("git", *args, **kwargs)


def output(*args, **kwargs):
    return git(*args, capture=True, **kwargs).stdout.rstrip("\n")


def fail(message):
    raise SystemExit("error: " + message)


def branch_name(ref):
    if ref.startswith("-"):
        fail(f"invalid ref starts with dash: {ref}")
    name = output("show", "--no-patch", "--no-show-signature", "--format=%f", ref)
    if not name:
        fail(f"no branch found for ref: {ref}")
    name = name.lower().lstrip(".")
    if name.endswith(".lock"):
        name = name[:-5] + "-lock"
    return os.environ.get("GIT_PILE_PREFIX", "") + name


def worktree_path():
    # -z avoids Git's quoting of paths containing special characters.
    first = output("worktree", "list", "--porcelain", "-z").split("\0", 1)[0]
    root = first.removeprefix("worktree ")
    digest = hashlib.md5(os.fsencode(root + "\n"), usedforsecurity=False).hexdigest()
    return Path.home() / ".cache" / "git-pile" / digest


def exists(branch):
    return (
        git(
            "show-ref", "--verify", "--quiet", "refs/heads/" + branch, check=False
        ).returncode
        == 0
    )


def branch_busy(branch):
    ref = "refs/heads/" + branch
    records = output("worktree", "list", "--porcelain", "-z").split("\0")
    if "branch " + ref in records:
        return True

    # Rebase detaches HEAD but retains ownership of the branch it will update.
    common = Path(output("rev-parse", "--path-format=absolute", "--git-common-dir"))
    for directory in (common, *common.glob("worktrees/*")):
        for operation in ("rebase-merge", "rebase-apply"):
            name = directory / operation / "head-name"
            if name.is_file() and name.read_text().strip() == ref:
                return True
    return False


def check_idle(path):
    directory = Path(
        output("rev-parse", "--path-format=absolute", "--git-dir", cwd=path)
    )
    operations = (
        "rebase-merge",
        "rebase-apply",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "sequencer",
    )
    if any((directory / operation).exists() for operation in operations):
        fail(f"finish the Git operation in {shlex.quote(str(path))} first")


def ref_commands(updates):
    commands = ["start"]
    for ref, new, old in updates:
        if old is None:
            commands.append(f"create {ref} {new}")
            continue
        commands.append(f"update {ref} {new} {old}")
    return "\n".join([*commands, "prepare", "commit", ""])


def update_refs(updates, cwd=None):
    git(
        "update-ref",
        "-m",
        "git-pile: update PR",
        "--stdin",
        input=ref_commands(updates),
        cwd=cwd,
        silent=True,
    )


def config(name):
    return output("config", "--default", "false", "--type=bool", name) == "true"


def ask(prompt):
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
