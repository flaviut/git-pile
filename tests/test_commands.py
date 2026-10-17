"""Exercise the CLI against isolated repositories and a local bare remote."""

import hashlib
import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
spec = importlib.util.spec_from_file_location("pile", BIN / "_git_pile.py")
pile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pile)


class RepositoryTestCase(unittest.TestCase):
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
            "import json, os, sys, subprocess\n"
            "with open(os.environ['GH_LOG'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:3] == ['pr', 'create']:\n"
            "    if os.environ.get('GH_ADVANCE_REMOTE'): subprocess.run(['git', 'push', '--force', 'origin', 'main^:refs/heads/feature'], check=True)\n"
            "    if os.environ.get('GH_CHECK_DEFAULTS') and ('--title' not in sys.argv or '--body' not in sys.argv):\n"
            "        base = sys.argv[sys.argv.index('--base') + 1]\n"
            "        subprocess.run(['git', 'log', f'origin/{base}...HEAD'], check=True, stdout=subprocess.DEVNULL)\n"
            "    if os.environ.get('GH_FAIL'): sys.exit(1)\n"
            "    print('https://github.com/example/repo/pull/1')\n"
            "elif sys.argv[1:3] == ['pr', 'merge']:\n"
            "    if os.environ.get('GH_FAIL_MERGE'): sys.exit(1)\n"
            "elif sys.argv[1:3] == ['pr', 'list']:\n"
            "    if '--jq' in sys.argv: print('https://github.com/example/repo/pull/1')\n"
            "    elif '--head' in sys.argv: print('[{\"url\": \"https://github.com/example/repo/pull/1\"}]')\n"
            "    else: print('[]')\n"
        )
        gh.chmod(0o755)
        glab = fake_bin / "glab"
        glab.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['GLAB_LOG'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if os.environ.get('GLAB_FAIL'): sys.exit(1)\n"
            "print('https://gitlab.example.com/team/repo/-/merge_requests/1')\n"
        )
        glab.chmod(0o755)
        for opener in ("open", "xdg-open"):
            script = fake_bin / opener
            script.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "sys.exit(1 if os.environ.get('OPENER_FAIL') else 0)\n"
            )
            script.chmod(0o755)
        self.env = {
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("GIT_", "GH_", "GLAB_"))
            },
            "HOME": str(self.home),
            "PATH": os.pathsep.join((str(fake_bin), str(BIN), os.environ["PATH"])),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": "true",
            "GH_LOG": str(self.root / "gh.log"),
            "GLAB_LOG": str(self.root / "glab.log"),
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


class Commands(RepositoryTestCase):
    def test_automatic_gitlab_submission_and_open(self):
        class GitLabHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'<meta content="GitLab" property="og:site_name">')

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), GitLabHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}/team/repo.git"
                self.command("remote", "set-url", "origin", origin)
                self.command(
                    "remote", "set-url", "--push", "origin", str(self.root / "origin")
                )
                self.commit("Feature\n\nMerge request description")
                self.command("submitpr", "HEAD", "--draft", "--merge-squash")
                self.command("openpr")
            finally:
                server.shutdown()
                thread.join()
        calls = [
            json.loads(line)
            for line in (self.root / "glab.log").read_text().splitlines()
        ]
        self.assertEqual(calls[0][:2], ["mr", "create"])
        for arg in (
            "--draft",
            "--yes",
            "--auto-merge",
            "--squash-before-merge=true",
            "Feature",
            "Merge request description",
            origin,
            "feature",
            "main",
        ):
            self.assertIn(arg, calls[0])
        self.assertEqual(calls[1], ["mr", "view", "feature", "--web", "--repo", origin])
        self.assertFalse((self.root / "gh.log").exists())
        self.assertEqual(
            self.command(
                "rev-parse", "--abbrev-ref", "feature@{upstream}"
            ).stdout.strip(),
            "origin/feature",
        )

    def test_gitlab_fork_and_failure(self):
        origin = "https://gitlab.com/team/repo.git"
        self.command("remote", "set-url", "origin", origin)
        self.command("init", "--bare", str(self.root / "fork"))
        self.command("remote", "add", "mine", str(self.root / "fork"))
        self.commit("Feature")
        self.command("submitpr", "--draft")
        call = json.loads((self.root / "glab.log").read_text().splitlines()[0])
        self.assertEqual(call[call.index("--head") + 1], str(self.root / "fork"))
        self.assertEqual(call[call.index("--repo") + 1], origin)
        self.commit("Second", "second.txt")
        self.env["GLAB_FAIL"] = "1"
        result = self.command("submitpr", check=False)
        self.assertIn("failed to create merge request", result.stderr)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.command("branch", "--list", "second").stdout)

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
        self.assertEqual(
            calls[0][calls[0].index("--repo") + 1], str(self.root / "origin")
        )
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

    def _check_scratch(self):
        worktrees = self.command("worktree", "list", "--porcelain", "-z").stdout
        self.assertEqual(
            sum(record.startswith("worktree ") for record in worktrees.split("\0")), 2
        )

    def _recover_refs(self, result):
        commands = result.stderr.split("git update-ref --stdin <<'EOF'\n", 1)[1]
        commands = commands.split("\nEOF", 1)[0] + "\n"
        self.command("update-ref", "--stdin", input=commands)

    def _hook(self, path, body):
        path.write_text("#!/bin/sh\nset -e\n" + body)
        path.chmod(0o755)

    def test_absorb_stale_pr_branch(self):
        original = self.commit("Feature")
        self.command("submitpr")
        self.command("checkout", "-b", "remote-update", "origin/main")
        self.commit("New upstream", "upstream.txt", "upstream\n")
        self.command("push", "origin", "HEAD:main")
        self.command("cherry-pick", original)
        self.command("push", "--force", "origin", "HEAD:feature")
        self.command("checkout", "main")
        self.command("rebase", "origin/main")
        (self.repo / "feature.txt").write_text("absorbed update\n")

        result = self.command("absorb", "--squash")
        self.assertNotIn("Upstream changed", result.stdout)
        self.assertNotIn("diff --git", result.stdout)
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "absorbed update\n"
        )
        self.assertEqual(
            self.command("rev-parse", "feature^").stdout,
            self.command("rev-parse", "origin/main").stdout,
        )
        self.assertEqual(self.command("status", "--porcelain").stdout, "")
        self._check_scratch()

    def test_absorb_sync_conflict(self):
        original = self.commit("Feature")
        self.command("submitpr")
        branch = self.command("rev-parse", "feature").stdout.strip()
        self.command("checkout", "-b", "remote-update", "feature")
        remote = self.commit("Remote update", contents="remote\n")
        self.command("push", "origin", "HEAD:feature")
        self.command("checkout", "main")
        (self.repo / "feature.txt").write_text("absorbed update\n")
        before = self.command("diff").stdout

        result = self.command("absorb", "--squash", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONFLICT", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.command("rev-parse", "HEAD").stdout.strip(), original)
        self.assertEqual(self.command("rev-parse", "feature").stdout.strip(), branch)
        self.assertEqual(
            self.command("rev-parse", "origin/feature").stdout.strip(), remote
        )
        self.assertEqual(self.command("diff").stdout, before)
        self.assertEqual(self.command("diff", "--cached").stdout, "")
        self._check_scratch()
        self.assertEqual(self.command("for-each-ref", "refs/git-pile/").stdout, "")

        self.command("push", "--force", "origin", "feature")
        self.command("absorb", "--squash")
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "absorbed update\n"
        )

    def test_absorb_rejected_push(self):
        original = self.commit("Feature")
        self.command("submitpr")
        branch = self.command("rev-parse", "feature").stdout.strip()
        (self.repo / "feature.txt").write_text("staged\n")
        self.command("add", "feature.txt")
        (self.repo / "feature.txt").write_text("unstaged\n")
        cached = self.command("diff", "--cached").stdout
        unstaged = self.command("diff").stdout
        hook = self.root / "origin" / "hooks" / "pre-receive"
        self._hook(hook, "exit 1\n")

        result = self.command("absorb", "--squash", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.command("rev-parse", "HEAD").stdout.strip(), original)
        self.assertEqual(self.command("diff", "--cached").stdout, cached)
        self.assertEqual(self.command("diff").stdout, unstaged)
        self.assertEqual(self.command("rev-parse", "feature").stdout.strip(), branch)
        self.assertEqual(self.command("for-each-ref", "refs/git-pile/").stdout, "")
        self._check_scratch()

    def test_absorb_commit_failure(self):
        original = self.commit("Feature")
        self.command("submitpr")
        (self.repo / "feature.txt").write_text("updated\n")
        before = self.command("diff").stdout
        self._hook(self.repo / ".git" / "hooks" / "pre-commit", "exit 1\n")

        result = self.command("absorb", "--squash", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.command("rev-parse", "HEAD").stdout.strip(), original)
        self.assertEqual(self.command("diff").stdout, before)
        self.assertEqual(self.command("diff", "--cached").stdout, "")

    def test_absorb_published_recovery(self):
        original = self.commit("Feature")
        self.command("submitpr")
        branch = self.command("rev-parse", "feature").stdout.strip()
        (self.repo / "feature.txt").write_text("updated\n")
        base = self.command("rev-parse", "origin/main").stdout.strip()
        self._hook(
            self.repo / ".git" / "hooks" / "pre-push",
            f"git update-ref refs/heads/feature {base}\n",
        )

        result = self.command("absorb", "--squash", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PR published; local finalization failed", result.stderr)
        self.assertNotEqual(self.command("rev-parse", "HEAD").stdout.strip(), original)
        self.assertEqual(self.command("show", "HEAD:feature.txt").stdout, "updated\n")
        self.assertEqual(
            len(self.command("for-each-ref", "refs/git-pile/").stdout.splitlines()), 2
        )
        self._check_scratch()
        self.command("update-ref", "refs/heads/feature", branch, base)
        self._recover_refs(result)
        self.assertEqual(self.command("show", "feature:feature.txt").stdout, "updated\n")

    def test_absorb_unknown_publication(self):
        original = self.commit("Feature")
        self.command("submitpr")
        (self.repo / "feature.txt").write_text("updated\n")
        self._hook(
            self.repo / ".git" / "hooks" / "pre-push",
            "git config remote.origin.uploadpack false\nexit 1\n",
        )

        result = self.command("absorb", "--squash", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Publication outcome unknown", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotEqual(self.command("rev-parse", "HEAD").stdout.strip(), original)
        self.assertEqual(self.command("show", "HEAD:feature.txt").stdout, "updated\n")
        self.assertEqual(
            len(self.command("for-each-ref", "refs/git-pile/").stdout.splitlines()), 2
        )
        self._check_scratch()

    def test_update_busy_branch(self):
        original = self.commit("Feature", "base.txt", "feature\n")
        self.command("submitpr")
        branch = self.command("rev-parse", "feature").stdout
        self.command("checkout", "-b", "remote-update", "origin/main")
        self.commit("Remote update", "base.txt", "remote\n")
        self.command("checkout", "main")
        update = self.commit("Review", "feature.txt", "updated\n")
        path = self.command("pileworktreepath").stdout.strip()
        self.command("-C", path, "switch", "feature")
        conflict = self.command("-C", path, "rebase", "remote-update", check=False)
        self.assertNotEqual(conflict.returncode, 0)
        status = self.command("-C", path, "status", "--porcelain").stdout

        result = self.command("updatepr", original, "--squash", check=False)
        self.assertIn("'feature' is in use", result.stderr)
        self.assertEqual(self.command("rev-parse", "feature").stdout, branch)
        self.assertEqual(self.command("rev-parse", "HEAD").stdout.strip(), update)
        self.assertEqual(
            self.command("-C", path, "status", "--porcelain").stdout, status
        )
        self._check_scratch()

    def test_update_rejected_push(self):
        original = self.commit("Feature")
        self.command("submitpr")
        branch = self.command("rev-parse", "feature").stdout.strip()
        update = self.commit("Review", contents="updated\n")
        hook = self.root / "origin" / "hooks" / "pre-receive"
        self._hook(hook, "exit 1\n")

        result = self.command("updatepr", original, "--squash", check=False)
        self.assertIn("publication failed", result.stderr)
        self.assertEqual(self.command("rev-parse", "HEAD").stdout.strip(), update)
        self.assertEqual(self.command("rev-parse", "feature").stdout.strip(), branch)
        self.assertEqual(
            self.command("rev-parse", "origin/feature").stdout.strip(), branch
        )
        self._check_scratch()
        self.assertEqual(self.command("for-each-ref", "refs/git-pile/").stdout, "")
        hook.unlink()
        self.command("updatepr", original, "--squash")
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "updated\n"
        )
        self.assertEqual(self.command("show", "HEAD:feature.txt").stdout, "updated\n")

    def test_update_remote_race(self):
        original = self.commit("Feature")
        self.command("submitpr")
        branch = self.command("rev-parse", "feature").stdout.strip()
        update = self.commit("Review", contents="updated\n")
        base = self.command("rev-parse", "origin/main").stdout.strip()
        hook = self.repo / ".git" / "hooks" / "pre-push"
        self._hook(
            hook,
            f"git --git-dir='{self.root / 'origin'}' update-ref refs/heads/feature {base}\n"
            "git fetch --quiet origin feature:refs/remotes/origin/feature\n",
        )

        result = self.command("updatepr", original, "--squash", check=False)
        self.assertIn("publication failed", result.stderr)
        self.assertEqual(self.command("rev-parse", "HEAD").stdout.strip(), update)
        self.assertEqual(self.command("rev-parse", "feature").stdout.strip(), branch)
        self.assertEqual(
            self.command("ls-remote", "origin", "refs/heads/feature").stdout.split()[0],
            base,
        )
        self._check_scratch()
        self.assertEqual(self.command("for-each-ref", "refs/git-pile/").stdout, "")

    def test_update_local_race(self):
        original = self.commit("Feature")
        self.command("submitpr")
        branch = self.command("rev-parse", "feature").stdout.strip()
        update = self.commit("Review", contents="updated\n")
        base = self.command("rev-parse", "origin/main").stdout.strip()
        hook = self.repo / ".git" / "hooks" / "pre-push"
        self._hook(hook, f"git update-ref refs/heads/feature {base}\n")

        result = self.command("updatepr", original, "--squash", check=False)
        self.assertIn("PR published; local finalization failed", result.stderr)
        self._check_scratch()
        self.assertEqual(self.command("rev-parse", "HEAD").stdout.strip(), update)
        self.assertEqual(self.command("rev-parse", "feature").stdout.strip(), base)
        published = self.command("ls-remote", "origin", "refs/heads/feature").stdout
        self.assertNotEqual(published.split()[0], branch)
        self.assertEqual(
            len(self.command("for-each-ref", "refs/git-pile/").stdout.splitlines()), 2
        )
        hook.unlink()
        self.command("update-ref", "refs/heads/feature", branch, base)
        self._recover_refs(result)
        self.assertEqual(
            self.command("ls-remote", "origin", "refs/heads/feature").stdout, published
        )
        self.assertEqual(
            self.command("rev-parse", "feature").stdout.strip(), published.split()[0]
        )

    def test_update_atomic_refs(self):
        original = self.commit("Feature")
        self.command("submitpr")
        branch = self.command("rev-parse", "feature").stdout.strip()
        update = self.commit("Review", contents="updated\n")
        hook = self.repo / ".git" / "hooks" / "reference-transaction"
        self._hook(
            hook,
            'if [ "$1" = prepared ]; then\n'
            "    while read old new ref; do\n"
            '        if [ "$ref" = refs/heads/main ]; then exit 1; fi\n'
            "    done\n"
            "fi\n",
        )
        result = self.command("updatepr", original, "--squash", check=False)
        self.assertIn("PR published; local finalization failed", result.stderr)
        self.assertEqual(self.command("rev-parse", "HEAD").stdout.strip(), update)
        self.assertEqual(self.command("rev-parse", "feature").stdout.strip(), branch)
        self._check_scratch()
        hook.unlink()
        self._recover_refs(result)
        self.assertEqual(self.command("show", "HEAD:feature.txt").stdout, "updated\n")
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "updated\n"
        )

    def test_update_apply_conflict(self):
        original = self.commit("Feature")
        self.command("submitpr")
        branch = self.command("rev-parse", "feature").stdout.strip()
        self.command("checkout", "-b", "remote-update", "feature")
        remote = self.commit("Remote update", contents="remote\n")
        self.command("push", "origin", "HEAD:feature")
        self.command("checkout", "main")
        self.commit("Review", contents="updated\n")
        result = self.command(
            "updatepr", original, "--squash", input="y\n", check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONFLICT", result.stdout)
        self.assertEqual(self.command("rev-parse", "feature").stdout.strip(), branch)
        self.assertEqual(
            self.command("rev-parse", "origin/feature").stdout.strip(), remote
        )
        self._check_scratch()
        self.assertEqual(self.command("for-each-ref", "refs/git-pile/").stdout, "")
        self.assertEqual(self.command("show", "HEAD:feature.txt").stdout, "updated\n")

    def test_update_pile_conflict(self):
        original = self.commit("Feature")
        self.command("submitpr")
        self.command("checkout", "feature")
        remote = self.commit("PR feedback", contents="later\n")
        self.command("push", "origin", "feature")
        self.command("checkout", "main")
        self.commit("Later", contents="later\n")
        update = self.commit("Review", contents="updated\n")
        result = self.command("updatepr", original, "--squash", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONFLICT", result.stdout)
        self.assertEqual(self.command("rev-parse", "HEAD").stdout.strip(), update)
        self.assertEqual(self.command("rev-parse", "feature").stdout.strip(), remote)
        self.assertEqual(
            self.command("rev-parse", "origin/feature").stdout.strip(), remote
        )
        self._check_scratch()
        self.assertEqual(self.command("for-each-ref", "refs/git-pile/").stdout, "")

    def test_update_preserves_files(self):
        original = self.commit("Feature")
        self.command("submitpr")
        self.command("branch", "sentinel", original)
        self.command("config", "rebase.updateRefs", "true")
        self.commit("Later", "later.txt", "later\n")
        self.commit("Review", contents="updated\n")
        (self.repo / "later.txt").write_text("staged\n")
        self.command("add", "later.txt")
        (self.repo / "later.txt").write_text("unstaged\n")
        (self.repo / "untracked.txt").write_text("untracked\n")
        cached = self.command("diff", "--cached").stdout
        unstaged = self.command("diff").stdout
        times = {path: path.stat().st_mtime_ns for path in self.repo.glob("*.txt")}
        self.command("updatepr", original, "--squash")
        self.assertEqual(self.command("diff", "--cached").stdout, cached)
        self.assertEqual(self.command("diff").stdout, unstaged)
        self.assertEqual({path: path.stat().st_mtime_ns for path in times}, times)
        self.assertEqual(self.command("rev-parse", "sentinel").stdout.strip(), original)
        self.assertEqual(
            self.command("log", "--format=%s", "origin/main..HEAD").stdout,
            "Later\nFeature\n",
        )

    def test_update_lost_push_receipt(self):
        original = self.commit("Feature")
        self.command("submitpr")
        self.commit("Review", contents="updated\n")
        hook = self.repo / ".git" / "hooks" / "pre-push"
        self._hook(
            hook,
            "read local_ref local_oid remote_ref remote_oid\n"
            'git -c core.hooksPath=/dev/null push --quiet "--force-with-lease=$remote_ref:$remote_oid" "$2" "$local_oid:$remote_ref"\n'
            "exit 1\n",
        )
        self.command("updatepr", original, "--squash")
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "updated\n"
        )
        self._check_scratch()
        self.assertEqual(
            self.command("ls-remote", "origin", "refs/heads/feature").stdout.split()[0],
            self.command("rev-parse", "feature").stdout.strip(),
        )

    def test_update_unknown_push(self):
        original = self.commit("Feature")
        self.command("submitpr")
        branch = self.command("rev-parse", "feature").stdout.strip()
        update = self.commit("Review", contents="updated\n")
        hook = self.repo / ".git" / "hooks" / "pre-push"
        self._hook(hook, "git config remote.origin.uploadpack false\nexit 1\n")

        result = self.command("updatepr", original, "--squash", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Publication outcome unknown", result.stderr)
        self.assertEqual(self.command("rev-parse", "feature").stdout.strip(), branch)
        self.assertEqual(self.command("rev-parse", "HEAD").stdout.strip(), update)
        refs = self.command(
            "for-each-ref", "--format=%(refname)", "refs/git-pile/"
        ).stdout.splitlines()
        self.assertEqual(len(refs), 2)
        prepared = next(ref for ref in refs if ref.endswith("/pr"))
        self.assertEqual(
            self.command("show", prepared + ":feature.txt").stdout, "updated\n"
        )
        self._check_scratch()

    def test_update_duplicate_titles(self):
        self.commit("Feature", "earlier.txt", "earlier\n")
        original = self.commit("Feature")
        self.command("submitpr")
        self.commit("Review", contents="updated\n")
        self.command("updatepr", original, "--squash")
        self.assertEqual(self.command("show", "HEAD:feature.txt").stdout, "updated\n")
        self.assertEqual(self.command("show", "HEAD^:earlier.txt").stdout, "earlier\n")
        self.assertNotEqual(
            self.command("show", "HEAD^:feature.txt", check=False).returncode, 0
        )

    def test_update_branch_target(self):
        self.commit("Feature")
        self.command("submitpr")
        self.commit("Review", contents="first update\n")
        self.command("updatepr", "feature")
        self.commit("More review", contents="second update\n")
        self.command("updatepr", "feature", "--squash")
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "second update\n"
        )
        self.assertEqual(
            self.command("log", "--format=%s", "origin/main..HEAD").stdout, "Feature\n"
        )

    def test_update_fork(self):
        self.command("init", "--bare", str(self.root / "fork"))
        self.command("remote", "add", "mine", str(self.root / "fork"))
        original = self.commit("Feature")
        self.command("submitpr")
        self.commit("Review", contents="updated\n")
        self.command("updatepr", original, "--squash")
        self.assertEqual(
            self.command("ls-remote", "mine", "refs/heads/feature").stdout.split()[0],
            self.command("rev-parse", "feature").stdout.strip(),
        )
        self.assertEqual(
            self.command("ls-remote", "origin", "refs/heads/feature").stdout, ""
        )

    def test_absorb_without_pr_amends_head(self):
        original = self.commit("Feature")
        parent = self.command("rev-parse", "HEAD^").stdout
        message = self.command("show", "-s", "--format=%B", "HEAD").stdout
        (self.repo / "feature.txt").write_text("absorbed update\n")
        result = self.command("absorb")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertNotEqual(self.command("rev-parse", "HEAD").stdout.strip(), original)
        self.assertEqual(self.command("rev-parse", "HEAD^").stdout, parent)
        self.assertEqual(
            self.command("show", "-s", "--format=%B", "HEAD").stdout, message
        )
        self.assertEqual(
            self.command("show", "HEAD:feature.txt").stdout, "absorbed update\n"
        )
        self.assertEqual(self.command("status", "--porcelain").stdout, "")
        self.assertEqual(self.command("branch", "--list", "feature").stdout, "")
        self.assertFalse((self.root / "gh.log").exists())

    def test_absorb_without_pr_squashes_into_head(self):
        self.commit("Feature")
        (self.repo / "feature.txt").write_text("absorbed update\n")
        self.command("absorb", "--squash")
        self.assertEqual(
            self.command("show", "HEAD:feature.txt").stdout, "absorbed update\n"
        )
        self.assertEqual(
            self.command("log", "--format=%s", "origin/main..HEAD").stdout, "Feature\n"
        )

    def test_absorb_without_pr_fixes_earlier_commit(self):
        self.commit("Feature")
        self.commit("Later", "later.txt", "later\n")
        (self.repo / "feature.txt").write_text("absorbed update\n")
        self.command("add", "feature.txt")
        (self.repo / "later.txt").write_text("unstaged update\n")
        self.command("absorb")
        self.assertEqual(
            self.command("show", "HEAD^:feature.txt").stdout, "absorbed update\n"
        )
        self.assertEqual(
            self.command("log", "--format=%s", "origin/main..HEAD").stdout,
            "Later\nFeature\n",
        )
        self.assertEqual(self.command("show", "HEAD:later.txt").stdout, "later\n")
        self.assertEqual((self.repo / "later.txt").read_text(), "unstaged update\n")
        self.assertEqual(self.command("diff", "--cached").stdout, "")

    def test_replace_rebase_and_cleanup(self):
        self.commit("Feature")
        self.command("submitpr")
        (self.repo / "feature.txt").write_text("replacement\n")
        self.command("commit", "-a", "--amend", "--no-edit")
        self.command("replacepr")
        self.assertEqual(
            self.command("show", "feature:feature.txt").stdout, "replacement\n"
        )
        self.command("checkout", "-b", "upstream", "origin/main")
        upstream = self.commit("Upstream", "upstream.txt", "upstream\n")
        self.command("push", "origin", "HEAD:main")
        self.command("checkout", "main")
        self.command("rebasepr", "HEAD", "--force")
        self.assertEqual(self.command("rev-parse", "feature^").stdout.strip(), upstream)
        self.assertEqual(
            self.command("rev-parse", "origin/feature").stdout,
            self.command("rev-parse", "feature").stdout,
        )
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

    def test_fork_onto_without_origin_ref(self):
        self.command("init", "--bare", str(self.root / "fork"))
        self.command("remote", "add", "mine", str(self.root / "fork"))
        first = self.commit("First")
        self.command("submitpr")
        self.commit("Second\n\nSecond body", "second.txt", "second\n")
        self.env["GH_CHECK_DEFAULTS"] = "1"

        self.command("submitpr", "HEAD", "--draft", "--onto", first)

        call = json.loads((self.root / "gh.log").read_text().splitlines()[-1])
        self.assertEqual(call[call.index("--repo") + 1], str(self.root / "fork"))
        self.assertEqual(call[call.index("--title") + 1], "Second")
        self.assertEqual(call[call.index("--body") + 1], "Second body")

        self.commit("Third", "third.txt", "third\n")
        self.command("submitpr", "--base", "first")
        call = json.loads((self.root / "gh.log").read_text().splitlines()[-1])
        self.assertEqual(call[call.index("--repo") + 1], str(self.root / "fork"))
        self.assertEqual(call[call.index("--base") + 1], "first")
        self.assertNotEqual(
            self.command("rev-parse", "--verify", "origin/first", check=False).returncode,
            0,
        )

    def test_failed_create_cleans_worktree(self):
        self.commit("Feature")
        self.env["GH_FAIL"] = "1"
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

    def test_failed_create_can_be_retried_after_amending(self):
        self.commit("Feature")
        self.env["GH_FAIL"] = "1"
        self.assertNotEqual(self.command("submitpr", check=False).returncode, 0)
        self.command("commit", "--amend", "-m", "Feature", "-m", "Updated body")
        del self.env["GH_FAIL"]
        self.command("submitpr")

    def test_failed_create_cleans_fork_for_both_hosts(self):
        self.command("init", "--bare", str(self.root / "fork"))
        self.command("remote", "add", "mine", str(self.root / "fork"))
        for host, failure in (("github.com", "GH_FAIL"), ("gitlab.com", "GLAB_FAIL")):
            with self.subTest(host=host):
                self.command(
                    "remote", "set-url", "origin", f"https://{host}/team/repo.git"
                )
                self.commit("Feature", contents=host)
                self.env[failure] = "1"
                self.assertNotEqual(
                    self.command("submitpr", check=False).returncode, 0
                )
                del self.env[failure]
                self.assertEqual(
                    self.command("ls-remote", "mine", "refs/heads/feature").stdout, ""
                )
                self.assertFalse(self.command("branch", "--list", "feature").stdout)

    def test_failed_push_preserves_existing_remote_branch(self):
        base = self.command("rev-parse", "HEAD").stdout.strip()
        self.command("push", "origin", "HEAD:feature")
        self.commit("Feature")
        self.assertNotEqual(self.command("submitpr", check=False).returncode, 0)
        self.assertEqual(
            self.command("ls-remote", "origin", "refs/heads/feature").stdout.split()[0],
            base,
        )
        self.assertFalse(self.command("branch", "--list", "feature").stdout)
        self.assertFalse((self.root / "gh.log").exists())

    def test_cleanup_preserves_remote_changed_during_creation(self):
        self.commit("Feature")
        self.env["GH_FAIL"] = "1"
        self.env["GH_ADVANCE_REMOTE"] = "1"
        result = self.command("submitpr", check=False)
        self.assertIn("failed to create PR", result.stderr)
        self.assertIn("could not clean up", result.stderr)
        self.assertEqual(
            self.command("ls-remote", "origin", "refs/heads/feature").stdout.split()[0],
            self.command("rev-parse", "main^").stdout.strip(),
        )

    def test_auto_merge_failure_preserves_created_pr_branch(self):
        self.commit("Feature")
        self.env["GH_FAIL_MERGE"] = "1"
        result = self.command("submitpr", "--merge-squash", check=False)
        self.assertIn("failed to auto-merge PR", result.stderr)
        self.assertTrue(self.command("branch", "--list", "feature").stdout)
        self.assertEqual(
            self.command("rev-parse", "feature").stdout,
            self.command("rev-parse", "origin/feature").stdout,
        )

    def test_browser_failure_preserves_created_pr_branch(self):
        self.commit("Feature")
        self.env["OPENER_FAIL"] = "1"
        self.assertNotEqual(self.command("submitpr", check=False).returncode, 0)
        self.assertTrue(self.command("branch", "--list", "feature").stdout)
        self.assertEqual(
            self.command("rev-parse", "feature").stdout,
            self.command("rev-parse", "origin/feature").stdout,
        )

    def test_manual_cleanup_uses_fork(self):
        self.command("init", "--bare", str(self.root / "fork"))
        self.command("remote", "add", "mine", str(self.root / "fork"))
        self.commit("Feature")
        self.command("submitpr")
        self.command("pilecleanupremotebranch", "HEAD")
        self.assertEqual(
            self.command("ls-remote", "mine", "refs/heads/feature").stdout, ""
        )
        self.assertFalse(self.command("branch", "--list", "feature").stdout)

    def test_manual_cleanup_preserves_changed_remote_and_local_branch(self):
        self.commit("Feature")
        self.command("submitpr")
        self.command("push", "--force", "origin", "main^:feature")
        self.assertNotEqual(
            self.command("pilecleanupremotebranch", "HEAD", check=False).returncode, 0
        )
        self.assertTrue(self.command("branch", "--list", "feature").stdout)
        self.assertTrue(
            self.command("ls-remote", "origin", "refs/heads/feature").stdout
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
    def test_gitlab_detection(self):
        for origin in (
            "git@git.gitlab.example.com:team/repo.git",
            "https://gitlab.example.com/team/repo.git",
            "ssh://git@invent.kde.org:2222/frameworks/baloo.git",
        ):
            with (
                self.subTest(origin=origin),
                patch.object(pile, "output", return_value=origin),
                patch.object(pile, "urlopen") as request,
            ):
                request.return_value.__enter__.return_value = io.BytesIO(
                    b'<meta content="GitLab" property="og:site_name">'
                )
                self.assertTrue(pile.gitlab_repository())
                self.assertTrue(request.call_args.args[0].startswith("https://"))

    def test_known_hosts_skip_detection_request(self):
        for origin, expected in (
            ("git@github.com:team/repo.git", False),
            ("https://gitlab.com/team/repo.git", True),
            ("/local/repo", False),
        ):
            with (
                self.subTest(origin=origin),
                patch.object(pile, "output", return_value=origin),
                patch.object(pile, "urlopen") as request,
            ):
                self.assertEqual(pile.gitlab_repository(), expected)
                request.assert_not_called()

    def test_detection_failure_happens_before_submission(self):
        with (
            patch.object(
                pile, "output", return_value="https://forge.example/team/repo.git"
            ),
            patch.object(pile, "urlopen", side_effect=OSError("offline")),
            patch.object(pile, "git") as git,
        ):
            with self.assertRaisesRegex(SystemExit, "Cannot detect hosting service"):
                pile.submitpr([])
            git.assert_not_called()

    def test_prompt_eof_declines(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(pile.ask("Continue?"))

    def test_prompt_retries(self):
        with patch("builtins.input", side_effect=["invalid", "Y"]):
            self.assertTrue(pile.ask("Continue?"))


if __name__ == "__main__":
    unittest.main()
