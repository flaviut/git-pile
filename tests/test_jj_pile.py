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
        self.env = {
            key: value for key, value in self.env.items() if not key.startswith("JJ_")
        }
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
        result = subprocess.run(
            args,
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            self.fail(f"{args}:\n{result.stdout}\n{result.stderr}")
        return result

    def jj(self, *args, **kwargs):
        return self.invoke(
            "jj", "--no-pager", "--color=never", *args, **kwargs
        ).stdout.strip()

    def pile(self, name, *args, **kwargs):
        return self.invoke(
            str(BIN / "jj-pile"), name, "--repo", "example/repo", *args, **kwargs
        )

    def change(self, title, file):
        (self.repo / file).write_text(title + "\n")
        self.jj("describe", "-m", title + "\n\nBody of " + title)
        return self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")

    def calls(self):
        return [
            json.loads(line) for line in (self.root / "gh.log").read_text().splitlines()
        ]

    def test_submit_rewrite_update_and_open(self):
        change = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        before = self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        self.pile("submit", "--draft")
        self.assertEqual(
            before, self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        )
        self.jj("describe", "-m", "renamed")
        (self.repo / "first.txt").write_text("review feedback\n")
        self.pile("update")
        self.assertEqual(
            self.jj("diff", "--from", "@", "--to", f"pile/{change}@origin"), ""
        )
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

    def revision(self, rev="@", template="commit_id"):
        return self.jj("log", "--no-graph", "-r", rev, "-T", template)

    def published(self, change):
        return f"pile/{change}@origin"

    def test_linear_changes_publish_independently_in_reverse_order(self):
        first = self.revision(template="change_id")
        self.jj("new")
        second = self.change("second", "second.txt")
        before = self.revision()
        self.pile("submit")
        self.pile("submit", "-r", first)
        self.assertEqual(before, self.revision())
        self.assertEqual(
            self.revision(f"parents({self.published(second)})"),
            self.revision("main@origin"),
        )
        self.assertNotIn(
            "first.txt", self.jj("file", "list", "-r", self.published(second))
        )
        self.assertIn(
            "second.txt", self.jj("file", "list", "-r", self.published(second))
        )
        (self.repo / "second.txt").write_text("updated second\n")
        before = self.revision()
        self.pile("update")
        self.assertEqual(before, self.revision())
        self.assertEqual(
            self.jj("file", "show", "-r", self.published(second), "second.txt"),
            "updated second",
        )
        self.assertNotIn(
            "first.txt", self.jj("file", "list", "-r", self.published(second))
        )

    def test_explicit_dependency_can_reverse_local_order(self):
        first = self.revision(template="change_id")
        self.jj("new")
        second = self.change("second", "second.txt")
        before = self.revision()
        self.pile("submit")
        self.pile("submit", "-r", first, "--onto", second)
        self.assertEqual(before, self.revision())
        self.assertEqual(
            self.revision(f"parents({self.published(first)})"),
            self.revision(self.published(second)),
        )
        self.assertIn(
            "second.txt", self.jj("file", "list", "-r", self.published(first))
        )
        self.jj("describe", "-r", second, "-m", "second edited")
        self.pile("update", "-r", second)
        self.pile("update", "-r", first)
        self.assertEqual(
            self.revision(f"parents({self.published(first)})"),
            self.revision(self.published(second)),
        )

    def test_dependency_uses_published_version(self):
        parent = self.revision(template="change_id")
        self.pile("submit")
        published_parent = self.revision(self.published(parent))
        self.jj("describe", "-m", "unpublished parent edit")
        self.jj("new")
        child = self.change("second", "second.txt")
        self.pile("submit", "--onto", parent)
        self.assertEqual(
            self.revision(f"parents({self.published(child)})"), published_parent
        )
        self.pile("update", "-r", parent)
        self.pile("update")
        self.assertEqual(
            self.revision(f"parents({self.published(child)})"),
            self.revision(self.published(parent)),
        )

    def test_missing_dependency_conflicts_without_changing_source(self):
        parent = self.revision(template="change_id")
        self.jj("new")
        child = self.change("dependent edit", "first.txt")
        before = self.revision()
        result = self.pile("submit", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts with the PR base", result.stderr)
        self.assertEqual(before, self.revision())
        self.assertEqual(
            self.jj("log", "--no-graph", "-r", "conflicts()", "-T", "commit_id"), ""
        )
        self.pile("submit", "-r", parent)
        self.pile("submit", "--onto", parent)
        self.assertEqual(
            self.jj("file", "show", "-r", self.published(child), "first.txt"),
            "dependent edit",
        )

    def test_private_source_is_not_published_through_duplicate(self):
        source = self.revision(template="change_id")
        self.jj("config", "set", "--repo", "git.private-commits", source)
        result = self.pile("submit", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private", result.stderr)
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in self.calls()))

    def test_independent_changes_and_integration_workspace(self):
        first = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        self.jj("new", "main@origin")
        second = self.change("second", "second.txt")
        self.jj("new", first, second)
        before = self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        self.pile("submit", "-r", first)
        self.pile("submit", "-r", second)
        self.assertEqual(
            before, self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        )
        self.assertTrue((self.repo / "first.txt").exists())
        self.assertTrue((self.repo / "second.txt").exists())
        self.assertNotEqual(self.pile("submit", check=False).returncode, 0)

    def test_submit_defaults_to_parent_of_empty_working_copy(self):
        change = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        self.jj("new")
        self.pile("submit", "--draft")
        create = next(c for c in self.calls() if c[:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--head") + 1], f"pile/{change}")

    def test_invalid_revisions_and_explicit_empty_changes(self):
        self.assertNotEqual(
            self.pile("submit", "-r", "all()", check=False).returncode, 0
        )
        self.jj("new")
        self.assertNotEqual(self.pile("submit", "-r", "@", check=False).returncode, 0)
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
        self.jj(
            "config",
            "set",
            "--repo",
            "aliases.pile",
            '["util", "exec", "--", "jj-pile"]',
        )
        self.jj("pile", "submit", "--repo", "example/repo")
        self.assertTrue((self.repo / ".git").exists())

    def test_bare_alias_defaults_to_status(self):
        self.jj(
            "config",
            "set",
            "--repo",
            "aliases.pile",
            '["util", "exec", "--", "jj-pile"]',
        )
        self.jj(
            "git", "remote", "set-url", "origin", "https://github.com/example/repo.git"
        )
        self.assertIn("unsubmitted first", self.jj("pile"))

    def test_help_uses_jj_alias_name(self):
        result = self.invoke(str(BIN / "jj-pile"), "--help")
        self.assertTrue(result.stdout.startswith("usage: jj pile "))

    def test_explicit_base_and_remote(self):
        self.invoke(
            "git",
            "--git-dir",
            str(self.root / "origin"),
            "update-ref",
            "refs/heads/release",
            "refs/heads/main",
        )
        self.jj("git", "remote", "rename", "origin", "upstream")
        self.jj("git", "fetch", "--remote", "upstream")
        self.pile("submit", "--remote", "upstream", "--base", "release")
        create = next(c for c in self.calls() if c[:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "release")

    def test_mine_is_preferred_for_pushes(self):
        mine = self.root / "mine"
        self.invoke("git", "init", "--bare", str(mine))
        self.jj("git", "remote", "add", "mine", str(mine))
        change = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        self.pile("submit")
        branch = "pile/" + change
        self.assertTrue(
            self.invoke(
                "git",
                "--git-dir",
                str(mine),
                "show-ref",
                "--verify",
                "refs/heads/" + branch,
                check=False,
            ).returncode
            == 0
        )
        self.assertNotEqual(
            self.invoke(
                "git",
                "--git-dir",
                str(self.root / "origin"),
                "show-ref",
                "--verify",
                "refs/heads/" + branch,
                check=False,
            ).returncode,
            0,
        )
        create = next(c for c in self.calls() if c[:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--head") + 1], branch)

    def test_fork_head_is_owner_qualified(self):
        self.jj(
            "git", "remote", "set-url", "origin", "https://github.com/upstream/repo.git"
        )
        self.jj("git", "remote", "add", "mine", "git@github.com:contributor/repo.git")
        self.invoke(str(BIN / "jj-pile"), "status", "-r", "@")
        lookup = self.calls()[-1]
        self.assertNotIn("contributor:", lookup[lookup.index("--head") + 1])
        branch = "pile/" + self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        # Exercise PR creation arguments without contacting the non-local fork.
        module = __import__("runpy").run_path(
            str(BIN / "jj-pile"), run_name="jj_pile_test"
        )
        github = object.__new__(module["GitHub"])
        github.head_owner = "contributor"
        self.assertEqual(github.head(branch), "contributor:" + branch)

    def test_conflicted_change_is_not_published(self):
        first = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        self.jj("new", "main@origin")
        self.change("conflicting", "first.txt")
        other = self.jj("log", "--no-graph", "-r", "@", "-T", "change_id")
        self.jj("rebase", "-r", first, "-d", other)
        result = self.pile("submit", "-r", first, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflict-free", result.stderr)

    def test_fetched_remote_fix_is_not_overwritten(self):
        source = self.revision(template="change_id")
        self.pile("submit")
        branch = "pile/" + source
        remote = str(self.root / "origin")
        head = self.revision(self.published(source))
        tree = self.invoke(
            "git", "--git-dir", remote, "rev-parse", head + "^{tree}"
        ).stdout.strip()
        fix = self.invoke(
            "git",
            "-c",
            "user.name=CI",
            "-c",
            "user.email=ci@example.com",
            "--git-dir",
            remote,
            "commit-tree",
            tree,
            "-p",
            head,
            "-m",
            "pre-commit.ci auto-fix",
        ).stdout.strip()
        self.invoke(
            "git", "--git-dir", remote, "update-ref", "refs/heads/" + branch, fix
        )
        self.jj("git", "fetch", "--remote", "origin")
        self.jj("describe", "-m", "local edit")
        result = self.pile("update", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inspect remote edits", result.stderr)
        self.assertEqual(
            self.invoke(
                "git", "--git-dir", remote, "rev-parse", "refs/heads/" + branch
            ).stdout.strip(),
            fix,
        )
        # Explicitly acknowledge the inspected remote head, then retry.
        self.jj(
            "bookmark",
            "set",
            branch,
            "pile-published/origin/" + source,
            "-r",
            self.published(source),
            "--allow-backwards",
        )
        self.pile("update")

    def test_update_uses_retargeted_base_after_merge(self):
        parent = self.revision(template="change_id")
        self.pile("submit")
        self.jj("new")
        child = self.change("second", "second.txt")
        self.pile("submit", "--onto", parent)
        self.invoke(
            "git",
            "--git-dir",
            str(self.root / "origin"),
            "update-ref",
            "refs/heads/main",
            self.revision(self.published(parent)),
        )
        self.jj("git", "fetch", "--remote", "origin")
        path = self.root / "prs.json"
        prs = json.loads(path.read_text())
        prs["pile/" + child]["baseRefName"] = "main"
        path.write_text(json.dumps(prs))
        before = self.revision()
        self.pile("update")
        self.assertEqual(before, self.revision())
        self.assertEqual(
            self.revision(f"parents({self.published(child)})"),
            self.revision("main@origin"),
        )

    def test_existing_direct_publication_can_be_updated(self):
        source = self.revision(template="change_id")
        branch = "pile/" + source
        self.jj("bookmark", "set", branch)
        self.jj("bookmark", "track", branch + "@origin")
        self.jj("git", "push", "--remote", "origin", "--bookmark", branch)
        (self.root / "prs.json").write_text(
            json.dumps(
                {
                    branch: {
                        "url": "https://github.com/example/repo/pull/1",
                        "state": "OPEN",
                        "baseRefName": "main",
                    }
                }
            )
        )
        self.jj("describe", "-m", "updated legacy change")
        before = self.revision()
        self.pile("update")
        self.assertEqual(before, self.revision())
        self.assertEqual(
            self.revision(self.published(source), "description").strip(),
            "updated legacy change",
        )

    def test_empty_projection_does_not_create_pr(self):
        first = self.revision(template="change_id")
        self.pile("submit")
        self.jj("new", "main@origin")
        self.change("same content", "first.txt")
        (self.repo / "first.txt").write_text("first\n")
        before = self.revision()
        result = self.pile("submit", "--onto", first, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("empty on that base", result.stderr)
        self.assertEqual(before, self.revision())
        self.assertEqual(sum(c[:2] == ["pr", "create"] for c in self.calls()), 1)

    def test_failed_update_preserves_previous_publication(self):
        first = self.revision(template="change_id")
        self.pile("submit")
        self.jj("new", "main@origin")
        second = self.change("second", "second.txt")
        self.pile("submit", "--onto", first)
        old = self.revision(self.published(second))
        self.jj("edit", first)
        (self.repo / "second.txt").write_text("conflicting addition\n")
        self.pile("update")
        result = self.pile("update", "-r", second, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts with the PR base", result.stderr)
        self.assertEqual(self.revision(self.published(second)), old)
        self.assertEqual(self.revision("pile/" + second), old)

    def test_repository_detection_from_remote_urls(self):
        for url in (
            "git@github.com:example/repo.git",
            "https://github.com/example/repo.git",
            "ssh://git@github.com/example/repo.git",
        ):
            with self.subTest(url=url):
                self.jj("git", "remote", "set-url", "origin", url)
                self.invoke(str(BIN / "jj-pile"), "status", "-r", "@")
                self.assertEqual(
                    self.calls()[-1][-2:], ["--repo", "github.com/example/repo"]
                )

    def test_unexpected_remote_edit_is_not_overwritten(self):
        self.pile("submit")
        branch = next(iter(json.loads((self.root / "prs.json").read_text())))
        # Simulate another client deleting the remote branch after our push.
        self.invoke(
            "git",
            "--git-dir",
            str(self.root / "origin"),
            "update-ref",
            "-d",
            "refs/heads/" + branch,
        )
        self.jj("describe", "-m", "local edit")
        self.assertNotEqual(self.pile("update", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
