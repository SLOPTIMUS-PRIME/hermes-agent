#!/usr/bin/env python3
"""Real-Git integration tests for maintainer.py (TDD).

Every test builds disposable throwaway repositories (a bare "official" remote,
a bare "fork" remote, a seed worktree) and invokes the actual CLI
(`python3 maintainer.py --config ...`) as a subprocess. Git output is never
fabricated; the adapter scripts here are small local stand-ins for the
parent-owned repair/test adapters and honour the same JSON stdin/stdout
contract (repair: JSON on stdin -> {status, reason, edits} on stdout;
tests: candidate path as argv, exit code decides).

Run with either interpreter from the repo root:
    /usr/bin/python3 -m unittest discover -s tests -v
    python3 -m unittest discover -s tests -v
"""
import fcntl
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAINTAINER = ROOT / "maintainer.py"
GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_CONFIG_NOSYSTEM="1")

BASE_LIB = "value = 1\n"
UP_LIB = "value = 2\n"
MERGED_LIB = "value = 2  # seed-patch: conservative\n"
CURATOR = "CURATOR_MODE = 'conservative'\n"
CI_YML = "name: ci\njobs: {}\n"


def sh(argv, cwd=None, env=None, check=True, timeout=180):
    r = subprocess.run([str(a) for a in argv], cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, env=env or GIT_ENV, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"command failed ({r.returncode}): {argv}\n{r.stderr}")
    return r


def git(*args, cwd=None, check=True):
    return sh(["git", *args], cwd=cwd, check=check).stdout.strip()


REPAIR_ADAPTER = textwrap.dedent("""
    import json, pathlib, sys
    resp_path, log_path = sys.argv[1], sys.argv[2]
    raw = sys.stdin.buffer.read()
    entry = {"stdin_bytes": len(raw)}
    try:
        entry["stdin"] = json.loads(raw)
    except Exception as exc:
        entry["stdin_error"] = repr(exc)
    with open(log_path, "a") as fh:
        fh.write(json.dumps(entry) + "\\n")
    script = json.loads(pathlib.Path(resp_path).read_text())
    if isinstance(script, list):
        resp = script.pop(0) if script else {"status": "blocked",
                                             "reason": "scripted: no responses left",
                                             "edits": []}
        pathlib.Path(resp_path).write_text(json.dumps(script))
    else:
        resp = script
    if resp == "MALFORMED":
        sys.stdout.write("I have no idea what you mean\\n")
        sys.exit(0)
    if isinstance(resp, str) and resp.startswith("SLEEP:"):
        import time
        time.sleep(float(resp.split(":", 1)[1]))
        sys.exit(0)
    if resp == "CRASH":
        sys.stderr.write("boom\\n")
        sys.exit(3)
    sys.stdout.write(json.dumps(resp) + "\\n")
""")

TEST_ADAPTER_DEFAULT = textwrap.dedent("""
    import pathlib, sys
    cand = pathlib.Path(sys.argv[1])
    with open(sys.argv[2], "a") as fh:
        fh.write("invoked\\n")
    text = (cand / "src/lib.py").read_text()
    ok = "<<<<<<<" not in text and ">>>>>>>" not in text
    print("sandbox-tests:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
""")

TEST_ADAPTER_NEEDS_ADAPTED = textwrap.dedent("""
    import pathlib, sys
    cand = pathlib.Path(sys.argv[1])
    with open(sys.argv[2], "a") as fh:
        fh.write("invoked\\n")
    text = (cand / "src/lib.py").read_text()
    ok = "# adapted" in text and "<<<<<<<" not in text
    print("sandbox-tests:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
""")

TEST_ADAPTER_PUSHES_COMPETING = textwrap.dedent("""
    import os, pathlib, subprocess, sys, tempfile
    cand = pathlib.Path(sys.argv[1])
    with open(sys.argv[2], "a") as fh:
        fh.write("invoked\\n")
    fork, branch = os.environ["COMPETING_FORK"], os.environ["COMPETING_BRANCH"]
    tmp = tempfile.mkdtemp(prefix="competing-")
    subprocess.run(["git", "clone", "-q", fork, tmp], check=True)
    (pathlib.Path(tmp) / "competing.txt").write_text("concurrent work\\n")
    def git(*a):
        subprocess.run(["git", "-C", tmp, *a], check=True, capture_output=True)
    git("add", "-A")
    git("-c", "user.name=c", "-c", "user.email=c@c", "commit", "-qm", "competing")
    git("push", "-q", "origin", "HEAD:refs/heads/" + branch)
    print("competing commit pushed to fork; candidate tests PASS")
    sys.exit(0)
""")


class Lab:
    """Disposable repo landscape: official.git, fork.git, seed worktree, graph."""

    def __init__(self, root: pathlib.Path, *, official_variant="newfile",
                 fork_extra=False, with_symlink=False, pad_lib=False):
        self.root = root
        self.official = root / "official.git"
        self.fork = root / "fork.git"
        self.graph = root / "graph"
        self.seed = root / "seed"
        self.state = root / "state"
        self.adapters = root / "adapters"
        for d in (self.official, self.fork, self.adapters):
            d.mkdir(parents=True)
        git("init", "-q", "--bare", self.official)
        git("init", "-q", "--bare", self.fork)
        # bare HEAD must point at the deployment branch so clones check it out
        git("symbolic-ref", "HEAD", "refs/heads/nick-patches", cwd=self.fork)
        git("init", "-q", "-b", "main", self.graph)
        git("config", "user.name", "t", cwd=self.graph)
        git("config", "user.email", "t@t", cwd=self.graph)

        # A: common base on main.
        lib_b = BASE_LIB + (("pad " * 20 + "\n") * 90 if pad_lib else "")
        self._write({"src/lib.py": lib_b, "README.md": "base readme\n",
                     "agent/curator.py": CURATOR,
                     "tests/test_lib.py": "def test_base():\n    assert True\n",
                     ".github/workflows/ci.yml": CI_YML})
        self._commit("base A")
        self.A = self.rev("main")
        # B: unpublished local seed commit (rewrites lib line 1, optionally symlink).
        git("checkout", "-q", "-b", "seedwork", "main", cwd=self.graph)
        self._write({"src/lib.py": lib_b.replace("value = 1",
                                                 "value = 1  # seed-patch: conservative")})
        if with_symlink:
            self._write({"extras/target.txt": "original\n"})
        self._commit("seed patch B")
        if with_symlink:
            os.symlink("target.txt", self.graph / "extras/alias.txt")
            git("add", "extras/alias.txt", cwd=self.graph)
            git("-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", "seed symlink", cwd=self.graph)
        self.B = self.rev("seedwork")
        # C: upstream commit on top of A.
        git("checkout", "-q", "-b", "official", "main", cwd=self.graph)
        if official_variant == "lib":
            self._write({"src/lib.py": UP_LIB})
        elif official_variant == "readme":
            self._write({"README.md": "upstream readme\n"})
        else:
            self._write({"upstream/new_api.py": "NEW = True\n"})
        self._commit("official C")
        self.C = self.rev("official")
        # D: fork-only published commit on top of A.  E: competing commit.
        git("checkout", "-q", "-b", "forkpub", "main", cwd=self.graph)
        self._write({"README.md": "fork readme\n"})
        self._commit("fork tweak D")
        self.D = self.rev("forkpub")
        git("checkout", "-q", "-b", "competing", "main", cwd=self.graph)
        self._write({"competing.txt": "concurrent\n"})
        self._commit("competing E")
        self.E = self.rev("competing")
        git("checkout", "-q", "main", cwd=self.graph)

        git("clone", "-q", str(self.graph), str(self.seed))
        self.seed_at(self.B)
        self.push_official(self.C)
        self.push_fork(self.D if fork_extra else self.A)
        self._cfg_n = 0
        self._repair_argv = None
        self._test_argv = None

    # -- graph helpers ----------------------------------------------------
    def rev(self, ref):
        return git("rev-parse", ref, cwd=self.graph)

    def _write(self, files):
        for rel, content in files.items():
            p = self.graph / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

    def _commit(self, msg):
        git("add", "-A", cwd=self.graph)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg, cwd=self.graph)

    def edit(self, path, content):
        return {"status": "resolved", "reason": "scripted fix",
                "edits": [{"path": path, "content": content}]}

    def seed_at(self, sha):
        git("checkout", "-q", "-B", "main", sha, cwd=self.seed)

    def push_official(self, sha, branch="main"):
        git("push", "-q", "-f", str(self.official), f"{sha}:refs/heads/{branch}",
            cwd=self.graph)

    def push_fork(self, sha, branch="nick-patches"):
        if sha is None:
            # bare repos refuse deleting the HEAD branch via receive-pack;
            # manipulate the fixture ref directly
            git("--git-dir", str(self.fork), "update-ref", "-d",
                f"refs/heads/{branch}", cwd=self.root)
            return
        git("push", "-q", str(self.fork), f"{sha}:refs/heads/{branch}", cwd=self.graph)

    def push_official_only_commit(self):
        """Commit existing only in official.git on side branch 'secret'
        (graph branch deleted afterwards, seed cloned before it existed)."""
        git("checkout", "-q", "-b", "secret", "main", cwd=self.graph)
        self._write({"upstream/secret_feature.py": "SECRET = 1\n"})
        self._commit("secret S")
        sha = self.rev("secret")
        self.push_official(sha, branch="secret")
        git("checkout", "-q", "main", cwd=self.graph)
        git("branch", "-D", "secret", cwd=self.graph)
        return sha

    def lsremote_fork(self, branch="nick-patches"):
        out = git("ls-remote", str(self.fork), f"refs/heads/{branch}",
                  cwd=self.root, check=False)
        return out.split("\t")[0] if out else None

    # -- adapters / config --------------------------------------------------
    def make_script(self, name, body):
        p = self.adapters / name
        p.write_text(textwrap.dedent(body))
        return p

    def repair(self, script, name="repair_adapter.py"):
        adapter = self.make_script(name, REPAIR_ADAPTER)
        resp = self.adapters / (name + ".responses.json")
        resp.write_text(json.dumps(script))
        self.repair_log = self.adapters / (name + ".log")
        self._repair_argv = [sys.executable, str(adapter), str(resp), str(self.repair_log)]
        return self._repair_argv

    def test_adapter(self, body=TEST_ADAPTER_DEFAULT):
        script = self.make_script("test_adapter.py", body)
        self.test_log = self.adapters / "test.log"
        self._test_argv = [sys.executable, str(script), "{candidate}", str(self.test_log)]
        return self._test_argv

    def config(self, **over):
        if self._repair_argv is None:
            self.repair([{"status": "blocked", "reason": "default blocked", "edits": []}])
        if self._test_argv is None:
            self.test_adapter()
        cfg = {
            "official_url": str(self.official),
            "official_branch": "main",
            "fork_url": str(self.fork),
            "deployment_branch": "nick-patches",
            "seed_repo": str(self.seed),
            "seed_sha": self.B,
            "state_dir": str(self.state),
            "repair_command": self._repair_argv,
            "test_command": self._test_argv,
            "allowed_edit_paths": ["src/lib.py", "agent/curator.py",
                                   ".github/workflows/ci.yml",
                                   "extras/alias.txt", "extras/target.txt"],
            "patch_intents": {"src/lib.py":
                              "keep the seed conservative marker and adopt the upstream value"},
            "max_repairs": 3,
            "command_timeout": 20,
            "test_timeout": 30,
            "max_input_bytes": 750000,
            "max_edit_bytes": 300000,
        }
        for k, v in over.items():
            if v is None:
                cfg.pop(k, None)
            else:
                cfg[k] = v
        self._cfg_n += 1
        path = self.root / f"config-{self._cfg_n}.json"
        path.write_text(json.dumps(cfg, indent=2))
        return path

    # -- observations ---------------------------------------------------------
    def run(self, cfg, publish=False, env_extra=None, timeout=180):
        cmd = [sys.executable, str(MAINTAINER), "--config", str(cfg)]
        if publish:
            cmd.append("--publish")
        env = dict(GIT_ENV)
        env.update(env_extra or {})
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)

    def latest(self):
        return json.loads((self.state / "latest.json").read_text())

    def repair_calls(self):
        log = getattr(self, "repair_log", None)
        if not log or not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def test_calls(self):
        log = getattr(self, "test_log", None)
        if not log or not log.exists():
            return []
        return log.read_text().splitlines()

    def candidate_text(self, rel):
        return (self.state / "candidate" / rel).read_text()

    def candidate_dirty(self):
        return git("status", "--porcelain", cwd=self.state / "candidate", check=False)

    def candidate_head(self):
        return git("rev-parse", "HEAD", cwd=self.state / "candidate")

    def is_ancestor(self, old, new):
        return subprocess.run(["git", "merge-base", "--is-ancestor", old, new],
                              cwd=self.state / "candidate", env=GIT_ENV).returncode == 0


class MaintainerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmps = []
        self.lab = self.new_lab()

    def tearDown(self):
        for tmp in self._tmps:
            tmp.cleanup()

    def new_lab(self, **kw):
        tmp = tempfile.TemporaryDirectory(prefix="fork-maint-test-")
        self._tmps.append(tmp)
        return Lab(pathlib.Path(tmp.name), **kw)

    def edit(self, path, content):
        return {"status": "resolved", "reason": "scripted fix",
                "edits": [{"path": path, "content": content}]}

    def assertNoConflictMarkers(self, text):
        for i, line in enumerate(text.splitlines(), 1):
            for marker in ("<<<<<<<", ">>>>>>>", "=======", "|||||||"):
                self.assertFalse(line.startswith(marker),
                                 f"marker {marker!r} at line {i}: {line!r}")


class TestConfigAndLock(MaintainerTestBase):
    def test_missing_config_key_is_usage_error(self):
        r = self.lab.run(self.lab.config(official_url=None))
        self.assertEqual(r.returncode, 2)
        self.assertIn("official_url", r.stderr + r.stdout)
        self.assertEqual(json.loads(r.stdout)["status"], "config-error")

    def test_bad_types_rejected(self):
        for over in ({"repair_command": "not-a-list"}, {"max_repairs": 0},
                     {"command_timeout": -1}, {"allowed_edit_paths": []},
                     {"test_command": None}):
            with self.subTest(over=over):
                r = self.lab.run(self.lab.config(**over))
                self.assertEqual(r.returncode, 2, r.stderr)

    def test_cli_requires_config_flag(self):
        r = sh([sys.executable, MAINTAINER], check=False)
        self.assertEqual(r.returncode, 2)

    def test_lock_busy_exits_promptly(self):
        cfg = self.lab.config()
        self.lab.state.mkdir(parents=True, exist_ok=True)
        with open(self.lab.state / "lock", "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            t0 = time.time()
            r = self.lab.run(cfg)
            self.assertLess(time.time() - t0, 30)
        self.assertEqual(r.returncode, 3)
        self.assertIn("lock", (r.stderr + r.stdout).lower())

    def test_unknown_config_keys_tolerated(self):
        cfg = self.lab.config(deployment_hold="/tmp/hold.json",
                              installed_repo="/tmp/installed",
                              hermes_bin="/tmp/hermes")
        r = self.lab.run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestCleanFlows(MaintainerTestBase):
    def test_clean_merge_prepare_only_never_pushes(self):
        self.lab.seed_at(self.lab.A)
        cfg = self.lab.config(seed_sha=self.lab.A)
        r = self.lab.run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "prepared")
        latest = self.lab.latest()
        self.assertEqual(latest["status"], "prepared")
        self.assertEqual(latest["official"]["sha"], self.lab.C)
        self.assertEqual(latest["fork"]["published_before"], self.lab.A)
        self.assertEqual(latest["base"], self.lab.A)
        self.assertEqual(latest["candidate"], self.lab.candidate_head())
        self.assertEqual(latest["checks"]["tests"]["status"], "pass")
        self.assertEqual(latest["publish"]["status"], "skipped-prepare-only")
        self.assertEqual(self.lab.lsremote_fork(), self.lab.A)  # untouched
        self.assertEqual(self.lab.candidate_text("upstream/new_api.py"), "NEW = True\n")
        self.assertFalse(self.lab.candidate_dirty())

    def test_publish_fast_forward_updates_fork(self):
        self.lab.seed_at(self.lab.A)
        cfg = self.lab.config(seed_sha=self.lab.A)
        r = self.lab.run(cfg, publish=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        latest = self.lab.latest()
        self.assertEqual(latest["status"], "published")
        self.assertEqual(latest["publish"]["status"], "published")
        self.assertEqual(latest["publish"]["expected_old"], self.lab.A)
        self.assertEqual(self.lab.lsremote_fork(), latest["candidate"])
        self.assertTrue(self.lab.is_ancestor(self.lab.A, latest["candidate"]))

    def test_noop_update_still_validates_on_first_use_and_caches(self):
        self.lab.seed_at(self.lab.A)
        self.lab.push_official(self.lab.A)  # official == published: nothing to do
        cfg = self.lab.config(seed_sha=self.lab.A)
        r = self.lab.run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lab.latest()["status"], "no-op")
        self.assertEqual(len(self.lab.test_calls()), 1)  # validated on first use
        r = self.lab.run(cfg, publish=True)              # cached validation now
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lab.latest()["publish"]["status"], "skipped-no-op")
        self.assertEqual(len(self.lab.test_calls()), 1)  # not re-run
        self.assertEqual(self.lab.lsremote_fork(), self.lab.A)

    def test_unpublished_seed_commits_are_kept_and_published(self):
        cfg = self.lab.config()  # seed B ahead of published A; official adds new file
        r = self.lab.run(cfg, publish=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        latest = self.lab.latest()
        self.assertEqual(latest["base"], self.lab.B)
        self.assertTrue(latest["lineage"]["seed_integrated"])
        self.assertIn("# seed-patch: conservative", self.lab.candidate_text("src/lib.py"))
        self.assertEqual(self.lab.lsremote_fork(), latest["candidate"])
        self.assertTrue(self.lab.is_ancestor(self.lab.B, latest["candidate"]))

    def test_diverged_seed_and_fork_lineages_both_integrated(self):
        self.lab = self.new_lab(fork_extra=True)  # published = A+D, seed = A+B
        cfg = self.lab.config()
        r = self.lab.run(cfg, publish=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        latest = self.lab.latest()
        self.assertEqual(latest["base"], self.lab.B)
        self.assertTrue(latest["lineage"]["published_integrated"])
        self.assertIn("# seed-patch: conservative", self.lab.candidate_text("src/lib.py"))
        self.assertIn("fork readme", self.lab.candidate_text("README.md"))
        self.assertIn("NEW = True", self.lab.candidate_text("upstream/new_api.py"))
        self.assertEqual(self.lab.lsremote_fork(), latest["candidate"])
        for sha in (self.lab.B, self.lab.D, self.lab.C):
            self.assertTrue(self.lab.is_ancestor(sha, latest["candidate"]),
                            f"{sha} lineage dropped")

    def test_latest_json_schema_is_complete(self):
        self.lab.seed_at(self.lab.A)
        cfg = self.lab.config(seed_sha=self.lab.A)
        self.lab.run(cfg)
        latest = self.lab.latest()
        for key in ("schema", "started_utc", "finished_utc", "status", "reason",
                    "official", "fork", "seed", "base", "candidate", "repairs",
                    "checks", "publish", "lineage"):
            self.assertIn(key, latest)
        self.assertEqual(latest["official"]["sha"], self.lab.C)
        self.assertIn("attempts", latest["repairs"])
        self.assertEqual(latest["seed"]["sha"], self.lab.A)

    def test_official_side_branches_are_not_fetched(self):
        self.lab.seed_at(self.lab.A)
        secret = self.lab.push_official_only_commit()
        cfg = self.lab.config(seed_sha=self.lab.A)
        r = self.lab.run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        refs = git("for-each-ref", "--format=%(refname)",
                   cwd=self.lab.state / "candidate")
        self.assertNotIn("secret", refs)
        probe = subprocess.run(["git", "rev-parse", "--verify", "-q", secret + "^{commit}"],
                               cwd=self.lab.state / "candidate", env=GIT_ENV,
                               capture_output=True)
        self.assertNotEqual(probe.returncode, 0, "secret branch objects were fetched")


class TestConflictRepair(MaintainerTestBase):
    def setUp(self):
        # Conflict scenario: seed B and official C both rewrite src/lib.py.
        self._tmps = []
        self.lab = self.new_lab(official_variant="lib")

    def test_conflict_resolved_via_repair_adapter_and_published(self):
        # Seed B and official C both rewrite src/lib.py line 1 -> real conflict.
        self.lab.repair([self.edit("src/lib.py", MERGED_LIB)])
        r = self.lab.run(self.lab.config(), publish=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        latest = self.lab.latest()
        self.assertEqual(latest["status"], "published")
        self.assertEqual(latest["repairs"]["attempts"], 1)
        self.assertEqual(self.lab.candidate_text("src/lib.py"), MERGED_LIB)
        self.assertNoConflictMarkers(self.lab.candidate_text("src/lib.py"))
        self.assertEqual(self.lab.lsremote_fork(), latest["candidate"])
        call = self.lab.repair_calls()[0]["stdin"]
        self.assertEqual(call["patch_intents"]["src/lib.py"],
                         "keep the seed conservative marker and adopt the upstream value")
        conflict = call["conflicts"][0]
        self.assertEqual(conflict["path"], "src/lib.py")
        self.assertIn("<<<<<<<", conflict["merged"])
        self.assertIn("value = 1  # seed-patch", conflict["ours"])
        self.assertIn("value = 2", conflict["theirs"])
        self.assertIn("value = 1\n", conflict["base"])
        self.assertEqual(call["context"]["deployment_branch"], "nick-patches")

    def test_blocked_repair_stops_run_and_leaves_recoverable_state(self):
        self.lab.repair([{"status": "blocked", "reason": "intent conflicts with upstream API",
                          "edits": []}])
        r = self.lab.run(self.lab.config())
        self.assertEqual(r.returncode, 1)
        latest = self.lab.latest()
        self.assertEqual(latest["status"], "blocked")
        self.assertEqual(latest["repairs"]["last"]["reason"],
                         "intent conflicts with upstream API")
        self.assertEqual(self.lab.lsremote_fork(), self.lab.A)  # nothing pushed
        self.assertTrue((self.lab.state / "candidate" / ".git" / "MERGE_HEAD").exists())

    def test_recovery_after_blocked_run_and_marker_cleanup(self):
        self.lab.repair([{"status": "blocked", "reason": "no", "edits": []}])
        self.assertEqual(self.lab.run(self.lab.config()).returncode, 1)
        # Rerun with a working adapter: orchestrator must recover its own
        # recorded mid-merge state instead of refusing.
        self.lab.repair([self.edit("src/lib.py", MERGED_LIB)], name="repair2.py")
        cfg = self.lab.config()
        r = self.lab.run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lab.latest()["status"], "prepared")
        self.assertEqual(self.lab.candidate_text("src/lib.py"), MERGED_LIB)
        self.assertFalse((self.lab.state / "recovery.json").exists())
        self.assertFalse(self.lab.candidate_dirty())

    def test_malformed_repair_output_is_unknown_result(self):
        self.lab.repair(["MALFORMED"])
        r = self.lab.run(self.lab.config())
        self.assertEqual(r.returncode, 1)
        latest = self.lab.latest()
        self.assertEqual(latest["status"], "failed")
        self.assertIn("repair", latest["reason"].lower())
        self.assertEqual(self.lab.lsremote_fork(), self.lab.A)

    def test_repair_crash_exit_is_unknown_result(self):
        self.lab.repair(["CRASH"])
        r = self.lab.run(self.lab.config())
        self.assertEqual(r.returncode, 1)
        self.assertIn("repair", self.lab.latest()["reason"].lower())

    def test_repair_timeout_is_bounded(self):
        self.lab.repair(["SLEEP:6"])
        t0 = time.time()
        r = self.lab.run(self.lab.config(command_timeout=2))
        self.assertLess(time.time() - t0, 90)
        self.assertEqual(r.returncode, 1)
        self.assertIn("timed out", json.dumps(self.lab.latest()).lower())

    def test_allowlist_and_unsafe_edits_rejected_then_repaired(self):
        good = self.edit("src/lib.py", MERGED_LIB)
        script = [
            {"status": "resolved", "reason": "sneaky",
             "edits": [{"path": "tests/test_lib.py", "content": "hacked\n"}]},
            {"status": "resolved", "reason": "escape",
             "edits": [{"path": "../outside.txt", "content": "nope\n"}]},
            {"status": "resolved", "reason": "binary",
             "edits": [{"path": "src/lib.py", "content": "bad\x00nul\n"}]},
            {"status": "resolved", "reason": "dup",
             "edits": [{"path": "src/lib.py", "content": "a\n"},
                       {"path": "src/lib.py", "content": "b\n"}]},
            good,
        ]
        self.lab.repair(script)
        r = self.lab.run(self.lab.config(max_repairs=5))
        self.assertEqual(r.returncode, 0, r.stderr)
        latest = self.lab.latest()
        self.assertEqual(latest["repairs"]["attempts"], 5)
        diag = json.dumps(latest["repairs"])
        self.assertIn("tests/test_lib.py", diag)
        self.assertIn("../outside.txt", diag)
        self.assertIn("duplicate", diag.lower())
        self.assertIn("nul", diag.lower())
        self.assertEqual(self.lab.candidate_text("src/lib.py"), MERGED_LIB)

    def test_conflict_markers_in_edit_content_rejected(self):
        bad = self.edit("src/lib.py",
                        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> upstream\n")
        self.lab.repair([bad, self.edit("src/lib.py", MERGED_LIB)])
        r = self.lab.run(self.lab.config(max_repairs=2))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("conflict marker", json.dumps(self.lab.latest()["repairs"]).lower())

    def test_workflows_denied_by_default_even_when_allowlisted(self):
        self.lab.repair([
            self.edit(".github/workflows/ci.yml", "name: pwned\n"),
            self.edit("src/lib.py", MERGED_LIB),
        ])
        r = self.lab.run(self.lab.config(max_repairs=2))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lab.candidate_text(".github/workflows/ci.yml"), CI_YML)
        self.assertIn(".github", json.dumps(self.lab.latest()["repairs"]))

    def test_symlink_target_rejected_regular_file_accepted(self):
        self.lab = self.new_lab(with_symlink=True)
        # No conflict: official adds a new file. Tests demand an adaptation so the
        # repair loop runs; attempt 1 writes through a symlink (rejected),
        # attempt 2 edits the regular target plus the source file (accepted).
        self.lab.test_adapter(TEST_ADAPTER_NEEDS_ADAPTED)
        self.lab.repair([
            self.edit("extras/alias.txt", "write through symlink\n"),
            {"status": "resolved", "reason": "proper fix",
             "edits": [{"path": "src/lib.py", "content": BASE_LIB + "# adapted\n"},
                       {"path": "extras/target.txt", "content": "rewritten\n"}]},
        ])
        r = self.lab.run(self.lab.config(
            allowed_edit_paths=["src/lib.py", "extras/alias.txt", "extras/target.txt"]))
        self.assertEqual(r.returncode, 0, r.stderr)
        cand = self.lab.state / "candidate"
        self.assertTrue(os.path.islink(cand / "extras/alias.txt"))
        self.assertEqual((cand / "extras/target.txt").read_text(), "rewritten\n")
        self.assertEqual((cand / "src/lib.py").read_text(), BASE_LIB + "# adapted\n")
        self.assertIn("symlink", json.dumps(self.lab.latest()["repairs"]).lower())

    def test_repair_budget_exhausted_fails_without_publish(self):
        bad = {"status": "resolved", "reason": "sneaky",
               "edits": [{"path": "tests/test_lib.py", "content": "hacked\n"}]}
        self.lab.repair([bad, bad])
        r = self.lab.run(self.lab.config(max_repairs=2))
        self.assertEqual(r.returncode, 1)
        latest = self.lab.latest()
        self.assertEqual(latest["status"], "failed")
        self.assertEqual(latest["repairs"]["attempts"], 2)
        self.assertIn("budget", latest["reason"].lower())
        self.assertEqual(self.lab.lsremote_fork(), self.lab.A)


class TestTestPhase(MaintainerTestBase):
    def test_failing_tests_trigger_repair_then_pass(self):
        self.lab.seed_at(self.lab.A)
        self.lab.test_adapter(TEST_ADAPTER_NEEDS_ADAPTED)
        self.lab.repair([self.edit("src/lib.py", BASE_LIB + "# adapted\n")])
        cfg = self.lab.config(seed_sha=self.lab.A)
        r = self.lab.run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        latest = self.lab.latest()
        self.assertEqual(latest["status"], "prepared")
        self.assertEqual(latest["repairs"]["attempts"], 1)
        self.assertEqual(len(self.lab.test_calls()), 2)
        self.assertIn("sandbox-tests", json.dumps(latest["repairs"]))
        self.assertIn("# adapted", self.lab.candidate_text("src/lib.py"))
        self.assertFalse(self.lab.candidate_dirty())

    def test_test_failure_blocked_repair_never_publishes(self):
        self.lab.seed_at(self.lab.A)
        self.lab.test_adapter(TEST_ADAPTER_NEEDS_ADAPTED)
        self.lab.repair([])  # adapter blocks when invoked
        cfg = self.lab.config(seed_sha=self.lab.A)
        r = self.lab.run(cfg)
        self.assertEqual(r.returncode, 1)
        latest = self.lab.latest()
        self.assertIn(latest["status"], ("failed", "blocked"))
        self.assertIn("test", json.dumps(latest["checks"]).lower())
        self.assertEqual(self.lab.lsremote_fork(), self.lab.A)

    def test_input_truncated_to_max_input_bytes(self):
        lab = self.new_lab(pad_lib=True, official_variant="lib")
        self.lab = lab
        lab.repair([lab.edit("src/lib.py", MERGED_LIB)])
        cfg = lab.config(max_input_bytes=4000)
        r = lab.run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = lab.repair_calls()
        self.assertEqual(len(calls), 1)  # invoked, not aborted
        self.assertLessEqual(calls[0]["stdin_bytes"], 4000)
        self.assertIn("truncated", json.dumps(calls[0]["stdin"]["conflicts"]).lower())

    def test_competing_remote_push_is_never_overwritten(self):
        self.lab.seed_at(self.lab.A)
        cfg = self.lab.config(seed_sha=self.lab.A,
                              test_command=self.lab.test_adapter(TEST_ADAPTER_PUSHES_COMPETING))
        r = self.lab.run(cfg, publish=True, env_extra={
            "COMPETING_FORK": str(self.lab.fork), "COMPETING_BRANCH": "nick-patches"})
        self.assertEqual(r.returncode, 1)
        latest = self.lab.latest()
        self.assertEqual(latest["publish"]["status"], "remote-moved")
        self.assertNotEqual(latest["publish"]["remote_after"], latest["candidate"])
        # the concurrent commit (adapter's own) is intact on the fork
        self.assertEqual(latest["publish"]["remote_after"], self.lab.lsremote_fork())
        self.assertNotEqual(latest["publish"]["remote_after"],
                            latest["fork"]["published_before"])


class TestPublishSafety(MaintainerTestBase):
    def test_lost_push_response_reconciled_by_ls_remote(self):
        real_git = shutil.which("git")
        wrapper_dir = self.lab.root / "bin"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/bash\n"
            'if [[ " $* " == *" push "* ]]; then\n'
            f'  "{real_git}" "$@" >/dev/null 2>&1\n'
            '  echo "push: connection reset, response lost" >&2\n'
            '  exit 1\n'
            'fi\n'
            f'exec "{real_git}" "$@"\n')
        wrapper.chmod(0o755)
        self.lab.seed_at(self.lab.A)
        cfg = self.lab.config(seed_sha=self.lab.A)
        r = self.lab.run(cfg, publish=True,
                         env_extra={"PATH": f"{wrapper_dir}:{os.environ['PATH']}"})
        self.assertEqual(r.returncode, 0, r.stderr)
        latest = self.lab.latest()
        self.assertEqual(latest["publish"]["status"], "published")
        self.assertEqual(latest["publish"]["method"], "reconciled")
        self.assertEqual(self.lab.lsremote_fork(), latest["candidate"])

    def test_push_failure_with_unchanged_remote_reports_failure(self):
        real_git = shutil.which("git")
        wrapper_dir = self.lab.root / "bin"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/bash\n"
            'if [[ " $* " == *" push "* ]]; then\n'
            '  echo "push: network unreachable" >&2\n'
            '  exit 1\n'
            'fi\n'
            f'exec "{real_git}" "$@"\n')
        wrapper.chmod(0o755)
        self.lab.seed_at(self.lab.A)
        cfg = self.lab.config(seed_sha=self.lab.A)
        r = self.lab.run(cfg, publish=True,
                         env_extra={"PATH": f"{wrapper_dir}:{os.environ['PATH']}"})
        self.assertEqual(r.returncode, 1)
        latest = self.lab.latest()
        self.assertEqual(latest["publish"]["status"], "failed")
        self.assertEqual(self.lab.lsremote_fork(), self.lab.A)

    def test_publish_creates_missing_branch(self):
        self.lab.push_fork(None)  # fork has no deployment branch yet
        cfg = self.lab.config()
        r = self.lab.run(cfg, publish=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        latest = self.lab.latest()
        self.assertEqual(latest["status"], "published")
        self.assertIsNone(latest["publish"]["expected_old"])
        self.assertEqual(self.lab.lsremote_fork(), latest["candidate"])


class TestDirtyStateProtection(MaintainerTestBase):
    def setUp(self):
        # Blocked mid-merge states require a real conflict to produce.
        self._tmps = []
        self.lab = self.new_lab(official_variant="lib")

    def test_dirty_candidate_with_unknown_file_is_refused_not_wiped(self):
        self.lab.seed_at(self.lab.A)
        cfg = self.lab.config(seed_sha=self.lab.A)
        self.assertEqual(self.lab.run(cfg).returncode, 0)
        stray = self.lab.state / "candidate" / "operator-notes.txt"
        stray.write_text("precious unknown work\n")
        r = self.lab.run(cfg)
        self.assertEqual(r.returncode, 4)
        self.assertEqual(self.lab.latest()["status"], "refused-dirty")
        self.assertTrue(stray.exists(), "unknown work was wiped")
        self.assertEqual(stray.read_text(), "precious unknown work\n")

    def test_dirty_state_with_no_recovery_marker_is_refused(self):
        self.lab.repair([{"status": "blocked", "reason": "no", "edits": []}])
        self.assertEqual(self.lab.run(self.lab.config()).returncode, 1)
        (self.lab.state / "recovery.json").unlink()  # marker lost: unknown state
        self.lab.repair([self.edit("src/lib.py", MERGED_LIB)], name="repair3.py")
        r = self.lab.run(self.lab.config())
        self.assertEqual(r.returncode, 4)
        self.assertTrue((self.lab.state / "candidate" / ".git" / "MERGE_HEAD").exists())

    def test_seed_missing_when_needed_fails_clearly(self):
        self.lab.push_fork(None)  # no published branch -> seed is required
        cfg = self.lab.config(seed_repo=str(self.lab.root / "no-such-seed"))
        r = self.lab.run(cfg)
        self.assertEqual(r.returncode, 1)
        self.assertIn("seed", self.lab.latest()["reason"].lower())

    def test_seed_unreachable_sha_fails_when_needed(self):
        self.lab.push_fork(None)
        cfg = self.lab.config(seed_sha="0" * 40)
        r = self.lab.run(cfg)
        self.assertEqual(r.returncode, 1)
        self.assertIn("seed", self.lab.latest()["reason"].lower())


if __name__ == "__main__":
    unittest.main()
