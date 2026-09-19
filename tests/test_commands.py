"""Exercise the CLI against isolated repositories and a local bare remote."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "bin"
spec = importlib.util.spec_from_file_location("pile", BIN / "_git_pile.py")
pile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pile)


class Commands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="git-pile-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        # No network requests or browser launches are made by these tests.
        gh = fake_bin / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['GH_LOG'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:3] == ['pr', 'create']:\n"
            "    if os.environ.get('GH_FAIL'): sys.exit(1)\n"
            "    print('https://github.com/example/repo/pull/1')\n"
            "elif sys.argv[1:3] == ['pr', 'list']:\n"
            "    if '--jq' in sys.argv: print('https://github.com/example/repo/pull/1')\n"
            "    elif '--head' in sys.argv: print('[{\"url\": \"https://github.com/example/repo/pull/1\"}]')\n"
            "    else: print('[]')\n"
        )
        gh.chmod(0o755)
        for opener in ("open", "xdg-open"):
            script = fake_bin / opener
            script.write_text(f"#!{sys.executable}\n")
            script.chmod(0o755)
        self.env = {
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("GIT_", "GH_"))
            },
            "HOME": str(self.home),
            "PATH": os.pathsep.join((str(fake_bin), str(BIN), os.environ["PATH"])),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": "true",
            "GH_LOG": str(self.root / "gh.log"),
        }
        self.command("init", "--bare", str(self.root / "origin"))
        self.command("init", "-b", "main")
        self.command("config", "user.name", "Test User")
        self.command("config", "user.email", "test@example.com")
        self.command("config", "pull.rebase", "true")
        self.commit("base", "base.txt", "base\n")
        self.command("remote", "add", "origin", str(self.root / "origin"))
        self.command("push", "-u", "origin", "main")

    def command(self, *args, check=True, input=None):
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env,
            text=True,
            input=input,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            self.fail(f"git {args}:\n{result.stdout}\n{result.stderr}")
        return result

    def commit(self, subject, filename="feature.txt", contents="feature\n"):
        (self.repo / filename).write_text(contents)
        self.command("add", "--", filename)
        self.command("commit", "-m", subject)
        return self.command("rev-parse", "HEAD").stdout.strip()

    def test_branch_names(self):
        self.commit("...Mixed Case.lock")
        self.assertEqual(
            self.command("pilebranchname", "HEAD").stdout.strip(), "mixed-case-lock"
        )
        self.env["GIT_PILE_PREFIX"] = "person/"
        self.assertEqual(
            self.command("pilebranchname", "HEAD").stdout.strip(),
            "person/mixed-case-lock",
        )
        self.assertNotEqual(
            self.command("pilebranchname", "--bad", check=False).returncode, 0
        )

    def test_worktree_path_is_shared_and_handles_spaces(self):
        digest = hashlib.md5((str(self.repo) + "\n").encode()).hexdigest()
        expected = self.home / ".cache" / "git-pile" / digest
        self.assertEqual(self.command("pileworktreepath").stdout.strip(), str(expected))
        other = self.root / "other worktree"
        self.command("worktree", "add", "--detach", str(other))
        self.assertEqual(
            self.command("-C", str(other), "pileworktreepath").stdout.strip(),
            str(expected),
        )

    def test_submit_template_open_missing_and_reset(self):
        (self.repo / ".github").mkdir()
        (self.repo / ".github" / "pull_request_template.md").write_text(
            "Template text\n"
        )
        self.command("add", ".github")
        self.commit("Feature")
        self.assertIn("Feature", self.command("missingprs").stdout)
        self.env["GIT_PILE_USE_PR_TEMPLATE"] = "1"
        self.command("submitpr", "--draft", "--merge-squash")
        log = (self.root / "gh.log").read_text()
        self.assertIn("Template text", log)
        self.assertIn('"--draft"', log)
        self.assertIn('"--squash"', log)
        self.assertEqual(self.command("missingprs").stdout, "")
        self.command("openpr")
        path = Path(self.command("pileworktreepath").stdout.strip())
        self.assertTrue(path.is_dir())
        self.assertNotEqual(
            self.command(
                "-C", str(path), "symbolic-ref", "HEAD", check=False
            ).returncode,
            0,
        )
        self.command("pilereset")
        self.assertFalse(path.exists())

    def test_fork_submission(self):
        self.command("init", "--bare", str(self.root / "fork"))
        self.command("remote", "add", "mine", str(self.root / "fork"))
        self.commit("Feature")
        self.command("submitpr", "--title", "A custom title", "--merge-rebase")
        calls = [
            json.loads(line) for line in (self.root / "gh.log").read_text().splitlines()
        ]
        self.assertIn("--repo", calls[0])
        self.assertIn("A custom title", calls[0])
        self.assertIn("--rebase", calls[1])
        self.assertEqual(
            self.command(
                "rev-parse", "--abbrev-ref", "feature@{upstream}"
            ).stdout.strip(),
            "mine/feature",
        )

    def test_update_and_squash(self):
        original = self.commit("Feature")
        self.command("submitpr")
        self.commit("Review feedback", contents="feature\nreview\n")
        self.command("updatepr", original, "--squash")
        self.assertEqual(
            self.command("rev-list", "--count", "origin/main..feature").stdout.strip(),
            "1",
        )
        self.assertEqual(
            self.command("rev-list", "--count", "origin/main..HEAD").stdout.strip(), "1"
        )
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "feature\nreview\n"
        )

    def test_headpr_and_absorb(self):
        self.commit("Feature")
        self.command("submitpr")
        (self.repo / "feature.txt").write_text("head update\n")
        self.command("headpr", "-a", "-m", "Update")
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "head update\n"
        )
        (self.repo / "feature.txt").write_text("absorbed update\n")
        self.command("absorb", "--squash")
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "absorbed update\n"
        )

    def test_replace_rebase_and_cleanup(self):
        self.commit("Feature")
        self.command("submitpr")
        (self.repo / "feature.txt").write_text("replacement\n")
        self.command("commit", "-a", "--amend", "--no-edit")
        self.command("replacepr")
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "replacement\n"
        )
        self.command("rebasepr", "HEAD", "--force")
        self.command("pilecleanupremotebranch", "HEAD")
        self.assertNotEqual(
            self.command(
                "show-ref", "--verify", "refs/heads/feature", check=False
            ).returncode,
            0,
        )
        self.assertEqual(
            self.command("ls-remote", "origin", "refs/heads/feature").stdout, ""
        )

    def test_onto_and_base(self):
        first = self.commit("First")
        self.command("submitpr")
        self.commit("Second", "second.txt", "second\n")
        self.command("submitpr", "--onto", first)
        self.assertEqual(
            self.command("rev-list", "--count", "origin/main..second").stdout.strip(),
            "2",
        )
        self.assertIn('"--base", "first"', (self.root / "gh.log").read_text())
        self.commit("Third", "third.txt", "third\n")
        self.command("submitpr", "--base", "first")
        self.assertEqual(
            self.command("rev-list", "--count", "origin/main..third").stdout.strip(),
            "2",
        )

    def test_failed_create_cleans_worktree(self):
        self.commit("Feature")
        self.env["GH_FAIL"] = "1"
        self.command("config", "pile.cleanupRemoteOnSubmitFailure", "true")
        result = self.command("submitpr", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to create PR", result.stderr)
        self.assertEqual(
            self.command("ls-remote", "origin", "refs/heads/feature").stdout, ""
        )
        self.assertNotEqual(
            self.command(
                "show-ref", "--verify", "refs/heads/feature", check=False
            ).returncode,
            0,
        )

    def test_onto_recreates_missing_branch(self):
        first = self.commit("First")
        self.command("submitpr")
        self.command("branch", "-D", "first")
        self.commit("Second", "second.txt", "second\n")
        self.command("submitpr", "--onto", first, input="y\n")
        self.assertEqual(
            self.command(
                "rev-parse", "--abbrev-ref", "first@{upstream}"
            ).stdout.strip(),
            "origin/first",
        )

    def test_conflict_aborts_and_removes_submission_branch(self):
        self.commit("Dependency", "base.txt", "changed\n")
        self.commit("Conflicting", "base.txt", "depends on changed\n")
        self.command("config", "merge.tool", "test-fail")
        self.command("config", "mergetool.test-fail.cmd", "false")
        self.command("config", "mergetool.test-fail.trustExitCode", "true")
        self.command("config", "mergetool.prompt", "false")
        self.assertNotEqual(
            self.command("submitpr", input="n\n", check=False).returncode, 0
        )
        self.assertNotEqual(
            self.command(
                "show-ref", "--verify", "refs/heads/conflicting", check=False
            ).returncode,
            0,
        )
        path = Path(self.command("pileworktreepath").stdout.strip())
        self.assertEqual(
            self.command(
                "-C", str(path), "diff", "--name-only", "--diff-filter=U"
            ).stdout,
            "",
        )

    def test_invalid_options_do_not_create_branches(self):
        self.commit("Feature")
        result = self.command(
            "submitpr", "--base", "main", "--onto", "HEAD", check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(
            self.command(
                "show-ref", "--verify", "refs/heads/feature", check=False
            ).returncode,
            0,
        )


class Helpers(unittest.TestCase):
    def test_gitlab_submission_url(self):
        for origin in (
            "git@git.gitlab.example.com:team/repo.git",
            "https://gitlab.example.com/team/repo.git",
        ):
            with (
                self.subTest(origin=origin),
                patch.object(pile, "config", return_value=True),
                patch.object(
                    pile, "output", side_effect=[origin, "sha", "origin/main"]
                ),
                patch.object(pile, "branch_name", return_value="person/feature"),
                patch.object(pile, "exists", return_value=False),
                patch.object(pile, "git"),
                patch.object(pile, "checkout") as checkout,
                patch.object(pile, "cherry_pick"),
                patch.object(pile, "native_open") as opener,
            ):
                checkout.return_value.__enter__.return_value = Path("/unused")
                pile.submitpr([])
                self.assertEqual(
                    opener.call_args.args[0],
                    "https://gitlab.example.com/team/repo/-/merge_requests/new?"
                    "merge_request%5Bsource_branch%5D=person%2Ffeature&"
                    "merge_request%5Btarget_branch%5D=main",
                )

    def test_prompt_eof_declines(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(pile.ask("Continue?"))

    def test_prompt_retries(self):
        with patch("builtins.input", side_effect=["invalid", "Y"]):
            self.assertTrue(pile.ask("Continue?"))


if __name__ == "__main__":
    unittest.main()
