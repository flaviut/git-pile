"""Shared implementation of git-pile's command-line tools."""

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode


def run(*args, cwd=None, capture=False, check=True, silent=False, env=None):
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


@contextmanager
def checkout(branch, *, submitting=False):
    path = worktree_path()
    success = False
    try:
        if path.is_dir():
            git("switch", "--quiet", branch, cwd=path)
        else:
            git("worktree", "add", "--quiet", "--force", str(path), branch)
        yield path
        success = True
    finally:
        if path.is_dir():
            if submitting:
                git("cherry-pick", "--abort", cwd=path, silent=True, check=False)
            git("switch", "--detach", "--quiet", cwd=path, check=False)
        if submitting and not success:
            git("branch", "-D", branch, check=False)


def cherry_pick(path, ref):
    if git("cherry-pick", ref, cwd=path, check=False).returncode == 0:
        return
    try:
        git("mergetool", cwd=path)
        git("-c", "core.editor=true", "cherry-pick", "--continue", cwd=path)
    except BaseException:
        git("cherry-pick", "--abort", cwd=path, check=False)
        raise


def fetch_updates(path, branch):
    upstream = output(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", cwd=path
    )
    remote, remote_branch = upstream.split("/", 1)
    git("fetch", "--quiet", remote, remote_branch, cwd=path)
    if git("diff", "--quiet", "HEAD...@{upstream}", cwd=path, check=False).returncode:
        git("diff", "HEAD...@{upstream}", cwd=path)
        if not ask(
            "warning: upstream has new commits, would you like to pull those (or abort)? (y/n) "
        ):
            fail(f"not updating PR, checkout '{branch}', pull and try again")
        git("pull", cwd=path)


def native_open(url, path):
    for command in ("open", "xdg-open"):
        if shutil.which(command):
            run(command, url, cwd=path)
            return
    print(f"PR is created at {url}")


def submitpr(args):
    parser = argparse.ArgumentParser(prog="git submitpr", allow_abbrev=False)
    # Only consume a leading commit, leaving gh's option values untouched.
    commit_arg = args.pop(0) if args and not args[0].startswith("-") else "HEAD"
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--onto")
    group.add_argument("--base")
    merge = parser.add_mutually_exclusive_group()
    for flag in ("merge", "merge-squash", "merge-rebase"):
        merge.add_argument(
            "--" + flag,
            action="store_const",
            const="--" + flag.removeprefix("merge-"),
            dest="merge",
        )
    options, pr_args = parser.parse_known_args(args)
    gitlab = config("pile.gitlabModeEnabled")
    gitlab_url = None
    if gitlab:
        origin = output("remote", "get-url", "origin").removesuffix(".git")
        match = re.fullmatch(r"git@(?:git\.)?([^:]+):(.+)", origin)
        if match:
            origin = f"https://{match[1]}/{match[2]}"
        elif not origin.startswith("https://"):
            fail("Cannot derive GitLab merge request URL from origin")
        gitlab_url = origin + "/-/merge_requests/new"
    elif not shutil.which("gh"):
        fail("missing gh, install here: https://cli.github.com")
    commit = output("rev-parse", commit_arg)
    branch = branch_name(commit)
    upstream = "@{upstream}"
    if options.onto:
        base = branch_name(options.onto)
        if not exists(base):
            for remote in ("mine", "origin"):
                git("fetch", "--quiet", remote, base, silent=True, check=False)
                if (
                    git(
                        "show-ref",
                        "--verify",
                        "--quiet",
                        f"refs/remotes/{remote}/{base}",
                        check=False,
                    ).returncode
                    == 0
                ):
                    if not ask(
                        f"warning: --onto ref '{options.onto}' resolves to missing local branch '{base}'. Create it tracking '{remote}/{base}'? [y/N]: "
                    ):
                        fail(
                            f"create the branch with: git branch --track '{base}' '{remote}/{base}'"
                        )
                    git("branch", "--track", base, f"{remote}/{base}")
                    break
            if not exists(base):
                fail(
                    f"branch '{base}' does not exist locally, maybe you haven't created a PR for that commit?"
                )
        upstream = base + "@{upstream}"
    elif options.base:
        upstream = options.base + "@{upstream}"
    if exists(branch):
        fail(
            f"branch named '{branch}' already exists, maybe you've already created the PR for this commit?"
        )
    remote_branch = output(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", upstream
    ).split("/", 1)[1]
    git("branch", "--no-track", branch, upstream)
    with checkout(branch, submitting=True) as path:
        cherry_pick(path, commit)
        fork = (
            git(
                "remote", "get-url", "mine", cwd=path, silent=True, check=False
            ).returncode
            == 0
        )
        git(
            "push",
            "--no-verify",
            "--quiet",
            "--set-upstream",
            "mine" if fork else "origin",
            branch,
            cwd=path,
        )
        if gitlab_url:
            native_open(
                gitlab_url
                + "?"
                + urlencode(
                    {
                        "merge_request[source_branch]": branch,
                        "merge_request[target_branch]": remote_branch,
                    }
                ),
                path,
            )
            return
        body_args = ["--fill"]
        template = path / ".github" / "pull_request_template.md"
        if (
            not fork
            and os.environ.get("GIT_PILE_USE_PR_TEMPLATE")
            and template.is_file()
        ):
            subject = output("show", "-s", "--format=%s", "HEAD", cwd=path)
            body = output("show", "-s", "--format=%b", "HEAD", cwd=path)
            body_args = [
                "--title",
                subject,
                "--body",
                (body + "\n\n" if body else "") + template.read_text().rstrip("\n"),
            ]
        if fork:
            body_args += ["--repo", output("remote", "get-url", "origin", cwd=path)]
        result = run(
            "gh",
            "pr",
            "create",
            *body_args,
            "--base",
            remote_branch,
            *pr_args,
            cwd=path,
            capture=True,
            check=False,
        )
        if result.returncode:
            if config("pile.cleanupRemoteOnSubmitFailure"):
                cleanup_remote_branch(branch)
            fail("failed to create PR")
        url = result.stdout.strip()
        if options.merge:
            result = run(
                "gh", "pr", "merge", url, "--auto", options.merge, cwd=path, check=False
            )
            if result.returncode:
                native_open(url, path)
                fail(f"failed to auto-merge PR with {options.merge}")
        native_open(url, path)


def updatepr(args):
    parser = argparse.ArgumentParser(prog="git updatepr")
    parser.add_argument("target")
    parser.add_argument("--squash", action="store_true")
    parser.add_argument("--force", action="store_true")
    options = parser.parse_args(args)
    os.environ["GIT_SEQUENCE_EDITOR"] = "true"
    if git("show", branch_name("HEAD"), silent=True, check=False).returncode == 0:
        if not options.force:
            fail(
                "a pull request exists for HEAD already, are you updating a PR onto another PR? Pass --force to ignore this check"
            )
        print(
            "warning: a pull request exists for HEAD already, ignoring this check because --force was passed",
            file=sys.stderr,
        )
    new_commit = output("rev-parse", "HEAD")
    if exists(options.target):
        branch = options.target
        commit = output("rev-parse", "--verify", branch)
    else:
        commit = output("rev-parse", "--verify", options.target)
        branch = branch_name(commit)
        if not exists(branch):
            fail(f"branch '{branch}' doesn't exist")
    with checkout(branch) as path:
        fetch_updates(path, branch)
        cherry_pick(path, new_commit)
        if options.squash:
            git(
                "commit",
                "--quiet",
                "--signoff",
                "--no-verify",
                "--amend",
                "--fixup=HEAD~",
                cwd=path,
            )
            git(
                "rebase", "--quiet", "--interactive", "--autosquash", "HEAD~2", cwd=path
            )
        push_args = ["--force-with-lease"] if options.squash else []
        if git("push", *push_args, "--quiet", cwd=path, check=False).returncode:
            print(f"warning: failed to push '{branch}'", file=sys.stderr)
        git(
            "rebase",
            "--quiet",
            "--interactive",
            "--autostash",
            "--exec",
            "git commit --signoff --no-verify --amend --fixup " + shlex.quote(commit),
            new_commit + "^",
        )
        git(
            "rebase",
            "--quiet",
            "--interactive",
            "--autostash",
            "--autosquash",
            commit + "^",
        )


def replacepr(args):
    parser = argparse.ArgumentParser(prog="git replacepr")
    parser.add_argument("commit", nargs="?", default="HEAD")
    options = parser.parse_args(args)
    os.environ["GIT_SEQUENCE_EDITOR"] = "true"
    commit = output("rev-parse", options.commit)
    branch = branch_name(options.commit)
    if not exists(branch):
        fail(f"branch '{branch}' doesn't exist")
    upstream = output(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    with checkout(branch) as path:
        fetch_updates(path, branch)
        base = output("merge-base", upstream, "HEAD", cwd=path)
        git("reset", "--hard", base, cwd=path)
        cherry_pick(path, commit)
        if git(
            "push", "--force-with-lease", "--quiet", cwd=path, check=False
        ).returncode:
            print(f"warning: failed to force push '{branch}'", file=sys.stderr)


def rebasepr(args):
    commit = "HEAD"
    rebase_args = []
    force = False
    found_commit = False
    for arg in args:
        if arg == "--force":
            force = True
        elif arg == "-i":
            rebase_args.append(arg)
        elif not found_commit:
            commit, found_commit = arg, True
        else:
            rebase_args.append(arg)
    branch = branch_name(commit)
    if not exists(branch):
        fail(f"branch '{branch}' doesn't exist")
    upstream = output(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    with checkout(branch) as path:
        if git("rebase", upstream, *rebase_args, cwd=path, check=False).returncode:
            try:
                while True:
                    git("mergetool", ".", cwd=path)
                    if (
                        git(
                            "rebase",
                            "--continue",
                            cwd=path,
                            env={"GIT_EDITOR": "true"},
                            check=False,
                        ).returncode
                        == 0
                    ):
                        break
                    if (
                        git(
                            "diff", "--quiet", "--diff-filter=U", cwd=path, check=False
                        ).returncode
                        == 0
                    ):
                        fail("failed to continue rebase")
            except BaseException:
                git("rebase", "--abort", cwd=path, check=False)
                raise
        if force:
            git("commit", "--amend", "--no-edit", cwd=path)
        git("push", "--force-with-lease", "--quiet", "--no-verify", cwd=path)


def headpr(args):
    squash = "--squash" in args
    commit_args = [arg for arg in args if arg != "--squash"]
    if squash:
        commit_args += ["-m", "ignore", "-s"]
    head = output("rev-parse", "HEAD")
    git("commit", *commit_args)
    git("updatepr", head, *(["--squash"] if squash else []))


def openpr(args):
    parser = argparse.ArgumentParser(prog="git openpr")
    parser.add_argument("commit", nargs="?", default="HEAD")
    branch = branch_name(parser.parse_args(args).commit)
    url = run(
        "gh",
        "pr",
        "list",
        "--head",
        branch,
        "--json",
        "url",
        "--jq",
        ".[0].url",
        "--limit",
        "1",
        capture=True,
    ).stdout.strip()
    if not url:
        fail(f"no PR found for branch '{branch}'")
    run("gh", "pr", "view", url, "--web")


def missingprs(args):
    for commit in output("rev-list", "@{upstream}..HEAD").splitlines():
        if not exists(branch_name(commit)):
            line = output(
                "-c",
                "color.ui=always",
                "--no-pager",
                "log",
                "--oneline",
                "-n",
                "1",
                commit,
            )
            if "LOCAL ONLY" not in line:
                print(line)


def cleanup_remote_branch(branch):
    if not exists(branch):
        branch = branch_name(branch)
    if not exists(branch):
        fail(f"branch named '{branch}' does not exist")
    path = worktree_path()
    git("push", "--quiet", "--delete", "origin", branch, cwd=path, check=False)
    git("switch", "--detach", "--quiet", cwd=path)
    git("branch", "-D", branch)


def pilecleanupremotebranch(args):
    parser = argparse.ArgumentParser(prog="git pilecleanupremotebranch")
    parser.add_argument("branch")
    cleanup_remote_branch(parser.parse_args(args).branch)


def pilereset(args):
    path = worktree_path()
    git("worktree", "remove", "--force", str(path), silent=True, check=False)
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def pilebranchname(args):
    parser = argparse.ArgumentParser(prog="git pilebranchname")
    parser.add_argument("ref")
    print(branch_name(parser.parse_args(args).ref))


def pileworktreepath(args):
    print(worktree_path())


def main(command):
    try:
        globals()[command](sys.argv[1:])
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
    except OSError as error:
        fail(str(error))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
