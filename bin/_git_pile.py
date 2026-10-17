"""Shared implementation of git-pile's command-line tools."""

import argparse
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from _git_pile_git import ask, branch_name, exists, fail, git, output, run, worktree_path


@contextmanager
def checkout(branch):
    path = worktree_path()
    try:
        if path.is_dir():
            git("switch", "--quiet", branch, cwd=path)
        else:
            git("worktree", "add", "--quiet", "--force", str(path), branch)
        yield path
    finally:
        if path.is_dir():
            git("switch", "--detach", "--quiet", cwd=path, check=False)


@dataclass
class Submission:
    path: Path
    commit: str | None = None
    created: bool = False


@contextmanager
def submission_checkout(branch, remote):
    submission = Submission(worktree_path())
    try:
        with checkout(branch) as path:
            submission.path = path
            try:
                yield submission
            finally:
                if not submission.created:
                    git("cherry-pick", "--abort", cwd=path, silent=True, check=False)
                    if submission.commit:
                        result = git(
                            "push",
                            "--quiet",
                            "--no-verify",
                            f"--force-with-lease=refs/heads/{branch}:{submission.commit}",
                            remote,
                            f":refs/heads/{branch}",
                            cwd=path,
                            check=False,
                        )
                        if result.returncode:
                            print(
                                f"warning: could not clean up '{remote}/{branch}'; "
                                "the remote branch may still exist",
                                file=sys.stderr,
                            )
    finally:
        if not submission.created:
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
        try:
            git("pull", cwd=path)
        except subprocess.CalledProcessError:
            # Pull can rebase or merge; abort before checkout detaches the worktree.
            for operation in ("rebase", "merge"):
                git(operation, "--abort", cwd=path, silent=True, check=False)
            fail(f"upstream pull failed; reconcile '{branch}' and retry the PR update")


def native_open(url, path):
    for command in ("open", "xdg-open"):
        if shutil.which(command):
            run(command, url, cwd=path)
            return
    print(f"PR is created at {url}")


class GitLabPage(HTMLParser):
    gitlab = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property") == "og:site_name":
            self.gitlab = attrs.get("content") == "GitLab"


def gitlab_repository():
    origin = output("remote", "get-url", "origin")
    match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(?!//)(.+)", origin)
    if match:
        origin = f"https://{match[1]}/{match[2]}"
    parsed = urlsplit(origin)
    if parsed.scheme == "ssh":
        origin = f"https://{parsed.hostname}{parsed.path}"
        parsed = urlsplit(origin)
    if parsed.hostname == "gitlab.com":
        return True
    if parsed.hostname == "github.com" or parsed.scheme not in ("http", "https"):
        return False
    try:
        with urlopen(origin.removesuffix(".git"), timeout=10) as response:
            page = GitLabPage()
            page.feed(response.read(1024 * 1024).decode("utf-8", errors="replace"))
            return page.gitlab
    except (URLError, OSError, ValueError):
        fail("Cannot detect hosting service for origin")


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
    gitlab = gitlab_repository()
    cli = "glab" if gitlab else "gh"
    if not shutil.which(cli):
        fail(f"missing {cli}, install it before submitting")
    if gitlab and options.merge == "--rebase":
        fail("GitLab rebase merging must be configured in the project's merge settings")
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
    fork = (
        git("remote", "get-url", "mine", silent=True, check=False).returncode == 0
    )
    remote = "mine" if fork else "origin"
    with submission_checkout(branch, remote) as submission:
        path = submission.path
        cherry_pick(path, commit)
        git(
            "push",
            "--no-verify",
            "--quiet",
            "--set-upstream",
            f"--force-with-lease=refs/heads/{branch}:",
            remote,
            f"HEAD:refs/heads/{branch}",
            cwd=path,
        )
        submission.commit = output("rev-parse", "HEAD", cwd=path)
        if gitlab:
            create_args = [
                "--yes",
                "--title",
                output("show", "-s", "--format=%s", "HEAD", cwd=path),
                "--description",
                output("show", "-s", "--format=%b", "HEAD", cwd=path),
                "--repo",
                output("remote", "get-url", "origin", cwd=path),
                "--source-branch",
                branch,
                "--target-branch",
                remote_branch,
            ]
            if fork:
                create_args += ["--head", output("remote", "get-url", "mine", cwd=path)]
            if options.merge:
                create_args += ["--auto-merge"]
                if options.merge == "--squash":
                    create_args += ["--squash-before-merge=true"]
            result = run(
                "glab",
                "mr",
                "create",
                *create_args,
                *pr_args,
                cwd=path,
                capture=True,
                check=False,
            )
            if result.returncode:
                fail("failed to create merge request")
            submission.created = True
            print(result.stdout, end="")
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
            # gh's defaults look for the base on origin, not the fork.
            body_args = [
                "--title",
                output("show", "-s", "--format=%s", "HEAD", cwd=path),
                "--body",
                output("show", "-s", "--format=%b", "HEAD", cwd=path),
                "--repo",
                output("remote", "get-url", "origin", cwd=path),
            ]
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
            fail("failed to create PR")
        submission.created = True
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
    from _git_pile_update import UpdateGuard, UpdateMode, start_update

    parser = argparse.ArgumentParser(prog="git updatepr")
    parser.add_argument("target")
    parser.add_argument("--squash", action="store_true")
    parser.add_argument("--force", action="store_true")
    options = parser.parse_args(args)
    mode = UpdateMode.SQUASH if options.squash else UpdateMode.APPEND
    guard = UpdateGuard.FORCE if options.force else UpdateGuard.CHECK
    start_update(options.target, mode, guard)


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
    if gitlab_repository():
        run(
            "glab",
            "mr",
            "view",
            branch,
            "--web",
            "--repo",
            output("remote", "get-url", "origin"),
        )
        return
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
    upstream = output(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", branch + "@{upstream}"
    )
    remote, remote_branch = upstream.split("/", 1)
    commit = output("rev-parse", branch)
    git(
        "push",
        "--quiet",
        f"--force-with-lease=refs/heads/{remote_branch}:{commit}",
        remote,
        f":refs/heads/{remote_branch}",
    )
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
