"""Real jj repositories and local pushes, with only GitHub mocked."""

import json
import shutil
import subprocess
import sys
import unittest

import test_commands

BIN = test_commands.BIN


@unittest.skipUnless(shutil.which("jj"), "jj is required for jj-pile integration tests")
class JjPile(unittest.TestCase):
    command = test_commands.Commands.command
    commit = test_commands.Commands.commit

    def setUp(self):
        test_commands.Commands.setUp(self)
        self.env = {key: value for key, value in self.env.items() if not key.startswith("JJ_")}
        self.env["JJ_CONFIG"] = str(self.root / "jj.toml")
        (self.root / "jj.toml").write_text(
            '[user]\nname = "Test User"\nemail = "test@example.com"\n'
        )
        self.env["PR_STATE"] = str(self.root / "prs.json")
        (self.root / "prs.json").write_text("{}")
        # Explicit --repo allows local bare remotes and exercises non-colocated jj.
        (self.root / "bin" / "gh").write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['GH_LOG'], 'a') as f: f.write(json.dumps(args) + '\\n')\n"
            "path = Path(os.environ['PR_STATE'])\n"
            "prs = json.loads(path.read_text())\n"
            "def arg(name): return args[args.index(name) + 1]\n"
            "if args[:2] == ['repo', 'view']:\n"
            " assert args[2] == 'example/repo' and '--repo' not in args\n"
            " print(json.dumps({'defaultBranchRef': {'name': 'main'}}))\n"
            "elif args[:2] == ['pr', 'list']:\n"
            " print(json.dumps([prs[arg('--head')]] if arg('--head') in prs else []))\n"
            "elif args[:2] == ['pr', 'create']:\n"
            " if os.environ.get('GH_FAIL'): sys.exit(1)\n"
            " pr = {'url': 'https://github.com/example/repo/pull/' + str(len(prs)+1),"
            " 'state': 'OPEN', 'baseRefName': arg('--base')}\n"
            " prs[arg('--head')] = pr\n"
            " path.write_text(json.dumps(prs))\n"
            " print(pr['url'])\n"
        )
        self.repo = self.root / "jj workspace"
        self.repo.mkdir()
        self.jj("git", "clone", "--no-colocate", str(self.root / "origin"), ".")
        # The fixture's bare remote need not advertise main as its HEAD.
        self.jj("new", "main@origin")
        self.change("first", "first.txt")

    def invoke(self, *args, check=True):
        result = subprocess.run(args, cwd=self.repo, env=self.env, text=True, capture_output=True)
        if check and result.returncode:
            self.fail(f"{args}:\n{result.stdout}\n{result.stderr}")
        return result

    def jj(self, *args, **kwargs):
        return self.invoke("jj", "--no-pager", "--color=never", *args, **kwargs).stdout.strip()

    def pile(self, name, *args, **kwargs):
        return self.invoke(str(BIN / "jj-pile"), name, "--repo", "example/repo", *args, **kwargs)

    def change(self, title, file):
        (self.repo / file).write_text(title + "\n")
        self.jj("describe", "-m", title + "\n\nBody of " + title)
        return self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")

    def calls(self):
        return [json.loads(line) for line in (self.root / "gh.log").read_text().splitlines()]

    def test_submit_rewrite_update_and_open(self):
        change = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        before = self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        self.pile("submit", "--draft")
        self.assertEqual(before, self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id"))
        self.jj("describe", "-m", "renamed")
        (self.repo / "first.txt").write_text("review feedback\n")
        self.pile("update")
        self.assertEqual(self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id"),
                         self.jj("log", "--no-graph", "-r", f"pile/{change}@origin", "-T", "commit_id"))
        self.pile("open")
        self.assertIn("--web", self.calls()[-1])
        self.assertIn("open renamed", self.pile("status").stdout)
        create = next(c for c in self.calls() if c[:2] == ["pr", "create"])
        self.assertIn("--draft", create)
        self.assertEqual(create[create.index("--body") + 1], "Body of first")

    def test_retry_after_creation_failure(self):
        self.env["GH_FAIL"] = "1"
        self.assertNotEqual(self.pile("submit", check=False).returncode, 0)
        del self.env["GH_FAIL"]
        self.pile("submit")
        self.pile("submit")
        self.assertEqual(sum(c[:2] == ["pr", "create"] for c in self.calls()), 2)

    def test_stack_and_unsubmitted_ancestor_guard(self):
        parent = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        self.pile("submit")
        self.jj("new")
        self.change("second", "second.txt")
        result = self.pile("submit", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("other changes", result.stderr)
        self.pile("submit", "--onto", parent)
        self.jj("describe", "-r", parent, "-m", "parent edited")
        self.assertNotEqual(self.pile("update", check=False).returncode, 0)
        self.pile("update", "-r", parent)
        self.pile("update")

    def test_independent_changes_and_integration_workspace(self):
        first = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        self.jj("new", "main@origin")
        second = self.change("second", "second.txt")
        self.jj("new", first, second)
        before = self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        self.pile("submit", "-r", first)
        self.pile("submit", "-r", second)
        self.assertEqual(before, self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id"))
        self.assertTrue((self.repo / "first.txt").exists())
        self.assertTrue((self.repo / "second.txt").exists())
        self.assertNotEqual(self.pile("submit", check=False).returncode, 0)

    def test_invalid_revisions_and_empty_changes(self):
        self.assertNotEqual(self.pile("submit", "-r", "all()", check=False).returncode, 0)
        self.jj("new")
        self.assertNotEqual(self.pile("submit", check=False).returncode, 0)
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in self.calls()))

    def test_closed_pr_is_not_reused(self):
        self.pile("submit")
        path = self.root / "prs.json"
        prs = json.loads(path.read_text())
        next(iter(prs.values()))["state"] = "MERGED"
        path.write_text(json.dumps(prs))
        self.assertNotEqual(self.pile("submit", check=False).returncode, 0)
        self.assertNotEqual(self.pile("update", check=False).returncode, 0)

    def test_colocated_repo_and_alias(self):
        self.repo = self.root / "colocated"
        self.repo.mkdir()
        self.jj("git", "clone", "--colocate", str(self.root / "origin"), ".")
        self.jj("new", "main@origin")
        self.change("colocated", "feature.txt")
        self.jj("config", "set", "--repo", "aliases.pile", '["util", "exec", "--", "jj-pile"]')
        self.jj("pile", "submit", "--repo", "example/repo")
        self.assertTrue((self.repo / ".git").exists())

    def test_explicit_base_and_remote(self):
        self.invoke("git", "--git-dir", str(self.root / "origin"),
                    "update-ref", "refs/heads/release", "refs/heads/main")
        self.jj("git", "remote", "rename", "origin", "upstream")
        self.jj("git", "fetch", "--remote", "upstream")
        self.pile("submit", "--remote", "upstream", "--base", "release")
        create = next(c for c in self.calls() if c[:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "release")

    def test_conflicted_change_is_not_published(self):
        first = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        self.jj("new", "main@origin")
        self.change("conflicting", "first.txt")
        other = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        self.jj("rebase", "-r", first, "-d", other)
        result = self.pile("submit", "-r", first, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflict-free", result.stderr)

    def test_repository_detection_from_remote_urls(self):
        for url in ("git@github.com:example/repo.git",
                    "https://github.com/example/repo.git",
                    "ssh://git@github.com/example/repo.git"):
            with self.subTest(url=url):
                self.jj("git", "remote", "set-url", "origin", url)
                self.invoke(str(BIN / "jj-pile"), "status", "-r", "@")
                self.assertEqual(self.calls()[-1][-2:], ["--repo", "github.com/example/repo"])

    def test_unexpected_remote_edit_is_not_overwritten(self):
        self.pile("submit")
        branch = next(iter(json.loads((self.root / "prs.json").read_text())))
        # Simulate another client deleting the remote branch after our push.
        self.invoke("git", "--git-dir", str(self.root / "origin"), "update-ref", "-d", "refs/heads/" + branch)
        self.jj("describe", "-m", "local edit")
        self.assertNotEqual(self.pile("update", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
