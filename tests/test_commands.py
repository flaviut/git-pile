"""Exercise the CLI against isolated repositories and local bare remotes."""

import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
pile = importlib.import_module("_git_pile")


class Commands(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="git-pile-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        home = self.root / "home"
        home.mkdir()
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.env = {
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("GIT_", "GH_", "GLAB_"))
            },
            "HOME": str(home),
            "PATH": os.pathsep.join((str(self.fake_bin), str(BIN), os.environ["PATH"])),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": "true",
            "FORGE_LOG": str(self.root / "forge.log"),
        }
        self.git("init", "--bare", "-b", "main", self.root / "origin")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "pull.rebase", "true")
        self.commit("base", "base.txt", "base\n")
        self.git("remote", "add", "origin", self.root / "origin")
        self.git("push", "-u", "origin", "main")

        self.stub(
            "gh",
            """\
import json, os, subprocess, sys

args = sys.argv[1:]
with open(os.environ['FORGE_LOG'], 'a') as log:
    print(json.dumps(args), file=log)

if args[:2] == ['pr', 'create']:
    target = args[args.index('--repo') + 1] if '--repo' in args else 'origin'
    base = args[args.index('--base') + 1]
    refs = subprocess.check_output(
        ['git', 'ls-remote', '--heads', target, f'refs/heads/{base}'], text=True
    )
    if not refs:
        sys.exit('base branch missing from target repository')
    if not subprocess.check_output(['git', 'rev-list', f'{refs.split()[0]}..HEAD']):
        sys.exit('no commits relative to target base')
    if os.environ.get('GH_FAIL'):
        sys.exit(1)
    print('https://github.com/example/repo/pull/1')
elif args[:2] == ['pr', 'list']:
    print('https://github.com/example/repo/pull/1')
""",
        )
        for opener in ("open", "xdg-open"):
            self.stub(opener, "pass\n")

    def run_cli(self, *args, check=True, input=None):
        result = subprocess.run(
            args,
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            input=input,
            check=False,
        )
        if check and result.returncode:
            self.fail(f"{args}:\n{result.stdout}\n{result.stderr}")
        return result

    def git(self, *args):
        return self.run_cli("git", *args).stdout.rstrip("\n")

    def commit(self, subject, file="feature.txt", text="feature\n"):
        (self.repo / file).write_text(text)
        self.git("add", "--", file)
        self.git("commit", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def stub(self, name, body):
        script = self.fake_bin / name
        script.write_text(f"#!{sys.executable}\n" + body)
        script.chmod(0o755)

    def calls(self):
        log = self.root / "forge.log"
        return [json.loads(line) for line in log.read_text().splitlines()]

    def hook(self, path, body):
        path.write_text("#!/bin/sh\nset -e\n" + body)
        path.chmod(0o755)

    def test_submit_uses_configured_base_and_template(self):
        (self.repo / ".github").mkdir()
        self.commit(
            "Add template", ".github/pull_request_template.md", "Template text\n"
        )
        self.git("push", "origin", "main", "main^:refs/heads/release")
        self.git("config", "pile.prBase", "origin/release")
        self.commit("Feature")

        self.assertIn("Feature", self.git("missingprs"))
        self.git("submitpr", "--draft", "--merge-squash")
        create, merge = self.calls()
        self.assertEqual(create[create.index("--base") + 1], "release")
        self.assertIn("Template text", create)
        self.assertIn("--draft", create)
        self.assertIn("--squash", merge)
        self.assertEqual(self.git("missingprs"), "")
        self.assertEqual(
            self.git("rev-parse", "feature^"), self.git("rev-parse", "origin/release")
        )
        self.git("openpr")
        self.assertEqual(self.calls()[-1][:2], ["pr", "view"])
        path = Path(self.git("pileworktreepath"))
        self.assertTrue(path.is_dir())
        self.git("pilereset")
        self.assertFalse(path.exists())

    def test_gitlab_submission_and_open(self):
        self.stub(
            "glab",
            """\
import json, os, sys

with open(os.environ['FORGE_LOG'], 'a') as log:
    print(json.dumps(sys.argv[1:]), file=log)
print('https://gitlab.com/team/repo/-/merge_requests/1')
""",
        )
        origin = "https://gitlab.com/team/repo.git"
        self.git("remote", "set-url", "--push", "origin", self.root / "origin")
        self.git("remote", "set-url", "origin", origin)
        self.commit("Feature\n\nMerge request description")

        self.git("submitpr", "--merge-squash")
        self.git("openpr")

        create, view = self.calls()
        self.assertEqual(create[:2], ["mr", "create"])
        self.assertIn("Merge request description", create)
        self.assertIn("--squash-before-merge=true", create)
        self.assertEqual(view, ["mr", "view", "feature", "--web", "--repo", origin])

    def test_unpublished_fork_base_creates_cumulative_draft(self):
        fork = self.root / "fork"
        self.git("init", "--bare", fork)
        self.git("remote", "add", "mine", fork)
        self.git("push", "origin", "main:trunk")
        self.git(
            "--git-dir",
            self.root / "origin",
            "symbolic-ref",
            "HEAD",
            "refs/heads/trunk",
        )
        first = self.commit("First")
        self.git("submitpr")
        head = self.commit("Second", "second.txt")

        self.git("submitpr", "--onto", first)

        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(
            self.git("rev-parse", "mine/second^"), self.git("rev-parse", "mine/first")
        )
        create = self.calls()[-1]
        self.assertEqual(create[create.index("--repo") + 1], str(self.root / "origin"))
        self.assertEqual(create[create.index("--base") + 1], "trunk")
        self.assertIn("--draft", create)
        self.assertIn("Includes dependency `first`", create[create.index("--body") + 1])

    def test_stacked_submission_targets_existing_pr_branch(self):
        first = self.commit("First")
        self.git("submitpr")
        self.commit("Second", "second.txt")

        self.git("submitpr", "--onto", first)

        self.assertEqual(
            self.git("rev-parse", "origin/second^"),
            self.git("rev-parse", "origin/first"),
        )
        create = self.calls()[-1]
        self.assertEqual(create[create.index("--base") + 1], "first")
        self.assertNotIn("--draft", create)

    def test_failed_create_cleans_branches_and_allows_retry(self):
        self.commit("Feature")
        self.env["GH_FAIL"] = "1"

        result = self.run_cli("git", "submitpr", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to create PR", result.stderr)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/feature"), "")
        self.assertEqual(self.git("branch", "--list", "feature"), "")
        del self.env["GH_FAIL"]
        self.git("submitpr")

    def test_squash_updates_pr_without_touching_pending_edits(self):
        original = self.commit("Feature")
        self.git("submitpr")
        self.commit("Later", "later.txt")
        self.commit("Review", text="updated\n")
        (self.repo / "later.txt").write_text("staged\n")
        self.git("add", "later.txt")
        (self.repo / "later.txt").write_text("unstaged\n")
        cached, unstaged = self.git("diff", "--cached"), self.git("diff")

        self.git("updatepr", original, "--squash")

        self.assertEqual(
            self.git("log", "--format=%s", "origin/main..HEAD"), "Later\nFeature"
        )
        self.assertEqual(self.git("show", "origin/feature:feature.txt"), "updated")
        self.assertEqual(self.git("diff", "--cached"), cached)
        self.assertEqual(self.git("diff"), unstaged)

    def test_update_does_not_overwrite_concurrent_push(self):
        original = self.commit("Feature")
        self.git("submitpr")
        branch = self.git("rev-parse", "feature")
        update = self.commit("Review", text="updated\n")
        base = self.git("rev-parse", "origin/main")
        self.hook(
            self.repo / ".git/hooks/pre-push",
            f"git --git-dir='{self.root / 'origin'}' update-ref refs/heads/feature {base}\n"
            "git fetch --quiet origin feature:refs/remotes/origin/feature\n",
        )

        result = self.run_cli("git", "updatepr", original, "--squash", check=False)

        self.assertIn("publication failed", result.stderr)
        self.assertEqual(
            self.git("rev-parse", "HEAD", "feature"), f"{update}\n{branch}"
        )
        self.assertEqual(
            self.git("ls-remote", "origin", "refs/heads/feature").split()[0], base
        )

    def test_update_can_recover_after_local_transaction_failure(self):
        original = self.commit("Feature")
        self.git("submitpr")
        branch = self.git("rev-parse", "feature")
        update = self.commit("Review", text="updated\n")
        hook = self.repo / ".git/hooks/reference-transaction"
        self.hook(
            hook,
            """\
if [ "$1" = prepared ] && grep -q ' refs/heads/main$'; then
    exit 1
fi
""",
        )

        result = self.run_cli("git", "updatepr", original, "--squash", check=False)

        self.assertIn("PR published; local finalization failed", result.stderr)
        self.assertEqual(
            self.git("rev-parse", "HEAD", "feature"), f"{update}\n{branch}"
        )
        hook.unlink()
        commands = result.stderr.split("git update-ref --stdin <<'EOF'\n", 1)[1]
        self.run_cli(
            "git", "update-ref", "--stdin", input=commands.split("\nEOF", 1)[0] + "\n"
        )
        self.assertEqual(self.git("show", "HEAD:feature.txt"), "updated")
        self.assertEqual(
            self.git("rev-parse", "feature"), self.git("rev-parse", "origin/feature")
        )

    def test_unknown_push_preserves_prepared_update(self):
        original = self.commit("Feature")
        self.git("submitpr")
        update = self.commit("Review", text="updated\n")
        self.hook(
            self.repo / ".git/hooks/pre-push",
            "git config remote.origin.uploadpack false\nexit 1\n",
        )

        result = self.run_cli("git", "updatepr", original, "--squash", check=False)

        self.assertIn("Publication outcome unknown", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), update)
        refs = self.git(
            "for-each-ref", "--format=%(refname)", "refs/git-pile/"
        ).splitlines()
        prepared = next(ref for ref in refs if ref.endswith("/pr"))
        self.assertEqual(self.git("show", prepared + ":feature.txt"), "updated")

    def test_absorb_rolls_back_rejected_push(self):
        original = self.commit("Feature")
        self.git("submitpr")
        (self.repo / "feature.txt").write_text("staged\n")
        self.git("add", "feature.txt")
        (self.repo / "feature.txt").write_text("unstaged\n")
        cached, unstaged = self.git("diff", "--cached"), self.git("diff")
        hook = self.root / "origin/hooks/pre-receive"
        self.hook(hook, "exit 1\n")

        result = self.run_cli("git", "absorb", "--squash", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), original)
        self.assertEqual(self.git("diff", "--cached"), cached)
        self.assertEqual(self.git("diff"), unstaged)
        hook.unlink()
        self.git("absorb", "--squash")
        self.assertEqual(self.git("show", "origin/feature:feature.txt"), "staged")

    def test_absorb_without_pr_fixes_earlier_commit(self):
        self.commit("Feature")
        self.commit("Later", "later.txt", "later\n")
        (self.repo / "feature.txt").write_text("absorbed update\n")
        self.git("add", "feature.txt")
        (self.repo / "later.txt").write_text("unstaged update\n")

        self.git("absorb")

        self.assertEqual(self.git("show", "HEAD^:feature.txt"), "absorbed update")
        self.assertEqual(self.git("show", "HEAD:later.txt"), "later")
        self.assertEqual((self.repo / "later.txt").read_text(), "unstaged update\n")
        self.assertEqual(self.git("diff", "--cached"), "")

    def test_replace_rebase_and_cleanup(self):
        self.commit("Feature")
        self.git("submitpr")
        (self.repo / "feature.txt").write_text("replacement\n")
        self.git("commit", "-a", "--amend", "--no-edit")
        self.git("replacepr")
        self.git("switch", "-c", "upstream", "origin/main")
        upstream = self.commit("Upstream", "upstream.txt")
        self.git("push", "origin", "HEAD:main")
        self.git("switch", "main")
        self.git("rebasepr", "HEAD", "--force")

        self.assertEqual(self.git("rev-parse", "origin/feature^"), upstream)
        self.assertEqual(self.git("show", "origin/feature:feature.txt"), "replacement")
        self.git("pilecleanupremotebranch", "HEAD")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/feature"), "")
        self.assertEqual(self.git("branch", "--list", "feature"), "")


class Helpers(unittest.TestCase):
    def test_self_hosted_gitlab_detection(self):
        with (
            patch.object(
                pile,
                "output",
                return_value="ssh://git@forge.example:2222/team/repo.git",
            ),
            patch.object(pile, "urlopen") as request,
        ):
            request.return_value.__enter__.return_value = io.BytesIO(
                b'<meta property="og:site_name" content="GitLab">'
            )
            self.assertTrue(pile.gitlab_repository())
            self.assertEqual(
                request.call_args.args[0], "https://forge.example/team/repo"
            )


if __name__ == "__main__":
    unittest.main()
