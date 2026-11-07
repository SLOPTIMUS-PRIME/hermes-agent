#!/usr/bin/env python3
"""Central LLM-assisted fork maintenance orchestrator (stdlib only).

Owns the candidate-checkout lifecycle for maintaining a patched fork of an
upstream project.  Builds a candidate merge of upstream work on top of the
fork's published deployment branch plus any unpublished local seed commits,
delegates conflict resolution / repair edits to an external no-tools Hermes
repair adapter, validates the result in the parent-owned sandbox test
adapter, and — only with --publish — publishes the tested exact commit to the
fork with an atomic compare-and-swap push.

    CLI:  python3 maintainer.py --config config.json [--publish]

    Without --publish: build, repair and test only; nothing is pushed.
    With    --publish: additionally CAS-publish the tested candidate.

Exit codes:
    0  success (status published / prepared / no-op)
    1  operational failure (status failed / blocked)
    2  config or CLI usage error
    3  lock busy (another run holds the state lock)
    4  safety refusal (dirty/unknown candidate checkout state)

CONFIG SCHEMA (config.json)
---------------------------
Required keys:
    official_url        str  upstream Git URL (fetched read-only, explicit ref only)
    official_branch     str  upstream branch name
    fork_url            str  fork Git URL (origin; publication target)
    deployment_branch   str  branch published on the fork (e.g. nick-patches)
    seed_repo           str  path of the installed live checkout (READ ONLY:
                             object source for unpublished local commits;
                             this program never writes there)
    seed_sha            str  full SHA of the installed-but-unpublished lineage
    state_dir           str  working state directory (created on demand)
    repair_command      [str] argv of the repair adapter; accepts one JSON
                             document on stdin, returns one JSON object on
                             stdout (schema below).  Run with cwd=state_dir.
    test_command        [str] argv of the sandbox test adapter; every element
                             containing "{candidate}" has it replaced by the
                             absolute candidate checkout path.  cwd=candidate.
                             Exit code 0 = pass.
    allowed_edit_paths  [str] exact relative file paths the repair adapter may
                             replace.  Anything else is rejected.
    patch_intents       {str: str} verbatim intent descriptions forwarded to
                             the repair adapter keyed by path.
    command_timeout     num  per-command timeout (seconds) for git, repair and
                             optional check commands
    test_timeout        num  timeout for the sandbox test command
    max_input_bytes     int  hard cap on the JSON stdin sent to the repair
                             adapter (conflict fields are truncated to fit)
    max_edit_bytes      int  cap per repair edit and per response in total

Optional keys (defaults in parentheses):
    max_repairs         int (3)   total repair-adapter invocations allowed per
                                  run across all merges and test failures
    check_command       [str]     optional extra dependency/runtime check;
                                  same {candidate} expansion and timeout as
                                  test_command; failure feeds the repair loop
    forbidden_edit_paths [str] ([".github"])
                                  deny list applied after the allowlist;
                                  prefix match on path or path+"/" so CI
                                  workflows can never be edited even if a
                                  future allowlist entry includes them
    output_capture_bytes int (20000)
                                  tail size of child stdout/stderr kept in
                                  memory, diagnostics and latest.json
    publish_attempts    int (2)   push attempts before reporting failure when
                                  the remote is unchanged between attempts

Unknown keys are ignored (e.g. deployment_hold / installed_repo /
hermes_bin belong to the parent-owned deployment tooling).

REPAIR ADAPTER PROTOCOL
-----------------------
stdin (one JSON object):
    {"schema": 1,
     "context": {official_url, official_branch, fork_url, deployment_branch,
                 base, official_sha, published_before, attempt, attempts_left},
     "patch_intents": {...verbatim from config...},
     "conflicts": [{"path", "base", "ours", "theirs", "merged"}],   # when mid-merge
     "diagnostics": [{"source": "reject"|"test"|"runtime"|"merge", "detail": str}]}
Conflict fields are UTF-8 text truncated to fit max_input_bytes; truncated
fields end with a "[truncated N bytes]" marker.

stdout (one JSON object):
    {"status": "resolved"|"blocked",
     "reason": str,                       # required for blocked
     "edits": [{"path": str, "content": str}]}   # complete replacement files;
                                                 # must be empty when blocked
The adapter must not execute anything, must not be given shell access and its
edits are applied only after validation: allowlisted exact paths, not on the
forbidden list, no symlink traversal, no NUL/binary, no conflict markers, no
duplicates, within size caps, target must be an existing regular file or a
new file whose parent chain contains no symlinks.

SAFETY MODEL
------------
- One flock()-locked state_dir; a concurrent run exits 3 immediately.
- The candidate checkout lives at state_dir/candidate.  A dirty or mid-merge
  checkout is refused (exit 4) unless it exactly matches this program's own
  recorded recovery state (state_dir/recovery.json), in which case it is
  rolled back before work resumes.  Unknown work is never wiped.
- Only explicit refs are fetched (never --all/-t of everything): the fork's
  deployment branch, the official branch, and the seed repo's heads.
- Seed lineage: if the published branch is behind the seed, the candidate is
  built from the seed; if they diverged, both lineages are merged explicitly;
  neither is ever silently dropped.
- Publication is compare-and-swap: git push --force-with-lease=REF:expected_old
  (plain push for branch creation), where expected_old is the fork ref read at
  the start of the run and must be an ancestor of the candidate.  Every push
  outcome is reconciled with a git ls-remote readback, so a lost push response
  is still detected; a concurrently moved remote is reported, never overwritten.
- state_dir/latest.json is written atomically (tmp file + fsync + rename) on
  every completed run with the exact input SHAs, candidate SHA, repair/test/
  publish status and failure reason.
- This program never touches the live seed checkout, production services,
  remote settings or anything outside state_dir and the local git object
  store; it never reads secrets, never runs candidate code itself (the
  parent-owned sandbox adapter does) and never publishes unvalidated state.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import posixpath
import re
import selectors
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_LOCKED = 3
EXIT_REFUSED = 4

DEFAULT_MAX_REPAIRS = 3
DEFAULT_FORBIDDEN_EDIT_PATHS = [".github"]
DEFAULT_OUTPUT_CAPTURE_BYTES = 20000
DEFAULT_PUBLISH_ATTEMPTS = 2

REQUIRED_STR_KEYS = ("official_url", "official_branch", "fork_url",
                     "deployment_branch", "seed_repo", "seed_sha", "state_dir")
REQUIRED_ARGV_KEYS = ("repair_command", "test_command")
REQUIRED_INT_KEYS = ("command_timeout", "test_timeout", "max_input_bytes", "max_edit_bytes")

CONFLICT_MARKER_RE = re.compile(rb"(?m)^(<<<<<<<( .*)?|=======|>>>>>>>|\|\|\|\|\|\|\|( .*)?)")
MAX_EVENTS = 200
EVENT_DETAIL_CAP = 400


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MaintainerAbort(Exception):
    """Raised for any planned abort; carries final status and reason."""

    def __init__(self, status: str, reason: str, exit_code: int = EXIT_FAILED):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.exit_code = exit_code


class Result:
    def __init__(self, rc, out, err, timed_out, argv, duration):
        self.rc = rc
        self.out = out
        self.err = err
        self.timed_out = timed_out
        self.argv = argv
        self.duration = duration

    @property
    def text(self):
        return (self.out + "\n" + self.err).strip()


def run_cmd(argv, cwd=None, timeout=30.0, stdin_text=None, capture_bytes=20000,
            env_extra=None):
    """Run argv with bounded streaming capture and a process-group deadline."""
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "/bin/true")
    env["LC_ALL"] = "C"
    if env_extra: env.update(env_extra)
    started = time.monotonic()
    proc = subprocess.Popen([str(a) for a in argv], cwd=str(cwd) if cwd else None,
        env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    incoming = memoryview(stdin_text.encode("utf-8")) if stdin_text is not None else None
    sent = 0
    seen = 0
    timed_out = False
    overflow = False
    output_limit = max(4_194_304, capture_bytes * 2)
    with selectors.DefaultSelector() as selector:
        for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if proc.stdin is not None:
            os.set_blocking(proc.stdin.fileno(), False)
            if incoming:
                selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
            else: proc.stdin.close()
        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(remaining, 0.25)):
                stream, name = key.fileobj, key.data
                if name == "stdin":
                    try: sent += os.write(stream.fileno(), incoming[sent:sent+65536])
                    except BrokenPipeError: sent = len(incoming)
                    if sent >= len(incoming):
                        selector.unregister(stream); stream.close()
                    continue
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    selector.unregister(stream); stream.close(); continue
                seen += len(chunk)
                buffers[name].extend(chunk)
                if len(buffers[name]) > capture_bytes:
                    del buffers[name][:-capture_bytes]
                if seen > output_limit:
                    overflow = True
                    break
            if overflow: break
    if timed_out or overflow:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    proc.wait(timeout=5)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None and not stream.closed: stream.close()
    out = buffers["stdout"].decode("utf-8", "replace")
    err = buffers["stderr"].decode("utf-8", "replace")
    if overflow: err += "\nCommand output exceeded the hard byte limit"
    return Result(proc.returncode if not overflow else -9, out, err, timed_out,
                  [str(a) for a in argv], time.monotonic() - started)


class Git:
    """Thin git runner bound to the candidate checkout."""

    def __init__(self, cfg, workdir, events):
        self.cfg = cfg
        self.cwd = workdir
        self.events = events

    def run(self, *args, timeout=None, check=True, env_extra=None, capture_bytes=None):
        r = run_cmd(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args], cwd=self.cwd,
                    timeout=timeout or self.cfg["command_timeout"],
                    capture_bytes=capture_bytes or self.cfg["output_capture_bytes"],
                    env_extra=env_extra)
        if len(self.events) < MAX_EVENTS:
            self.events.append(f"{utcnow()} git rc={r.rc} {' '.join(r.argv[1:6])}...")
        if check and r.rc != 0:
            raise MaintainerAbort("failed",
                                  f"git {' '.join(args[:3])} failed rc={r.rc}: {r.text[:400]}")
        return r

    def rev(self, ref, check=True):
        r = self.run("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
        if r.rc != 0:
            if check:
                raise MaintainerAbort("failed", f"cannot resolve ref {ref}: {r.text[:200]}")
            return None
        return r.out.strip() or None

    def is_ancestor(self, older, newer):
        return self.run("merge-base", "--is-ancestor", older, newer, check=False).rc == 0

    def file_bytes(self, rev_and_path):
        size = self.run("cat-file", "-s", rev_and_path, check=False)
        if size.rc or not size.out.strip().isdigit(): return None
        limit = max(self.cfg["max_input_bytes"], 1_048_576)
        if int(size.out.strip()) > limit:
            raise MaintainerAbort("blocked", "Conflict source exceeds bounded input size")
        r = self.run("cat-file", "blob", rev_and_path, check=False, capture_bytes=limit)
        # run_cmd decoded to str with replacements; re-encode losslessly enough
        return r.out.encode("utf-8", "surrogateescape") if r.rc == 0 else None


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def normalize_rel_path(p: str):
    """Return normalized relative path or None if unusable."""
    if not isinstance(p, str) or not p.strip():
        return None
    if p != p.strip(): return None
    if "\\" in p or "\x00" in p or p.startswith("/") or p.startswith("~"):
        return None
    norm = posixpath.normpath(p)
    if norm != p or norm in ("..", ".") or norm.startswith("../") or norm.startswith("/"):
        return None
    return norm


def load_config(path: str):
    """Parse and validate config; returns (cfg, error_message)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        return None, f"cannot read config {path}: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"config {path} is not valid JSON: {exc}"
    if not isinstance(raw, dict):
        return None, "config root must be a JSON object"

    missing = [k for k in REQUIRED_STR_KEYS if not isinstance(raw.get(k), str) or not raw[k]]
    missing += [k for k in REQUIRED_ARGV_KEYS if not is_argv(raw.get(k))]
    missing += [k for k in REQUIRED_INT_KEYS if not isinstance(raw.get(k), (int, float))
                or raw[k] <= 0]
    if not isinstance(raw.get("allowed_edit_paths"), list) or not raw["allowed_edit_paths"]:
        missing.append("allowed_edit_paths (non-empty list)")
    if not isinstance(raw.get("patch_intents"), dict) or \
            not all(isinstance(k, str) and isinstance(v, str)
                    for k, v in raw["patch_intents"].items()):
        missing.append("patch_intents (object of str->str)")
    if missing:
        return None, "invalid config; bad/missing keys: " + ", ".join(sorted(set(missing)))

    cfg = dict(raw)
    cfg["max_repairs"] = raw.get("max_repairs", DEFAULT_MAX_REPAIRS)
    if not isinstance(cfg["max_repairs"], int) or cfg["max_repairs"] < 1:
        return None, "invalid config: max_repairs must be a positive integer"
    if raw.get("check_command") is not None and not is_argv(raw["check_command"]):
        return None, "invalid config: check_command must be a non-empty argv list"
    cfg["check_command"] = raw.get("check_command")
    cfg["forbidden_edit_paths"] = raw.get("forbidden_edit_paths", DEFAULT_FORBIDDEN_EDIT_PATHS)
    if not isinstance(cfg["forbidden_edit_paths"], list) or \
            not all(isinstance(x, str) and x for x in cfg["forbidden_edit_paths"]):
        return None, "invalid config: forbidden_edit_paths must be a list of strings"
    cfg["output_capture_bytes"] = raw.get("output_capture_bytes", DEFAULT_OUTPUT_CAPTURE_BYTES)
    if not isinstance(cfg["output_capture_bytes"], int) or cfg["output_capture_bytes"] < 256:
        return None, "invalid config: output_capture_bytes must be an integer >= 256"
    cfg["publish_attempts"] = raw.get("publish_attempts", DEFAULT_PUBLISH_ATTEMPTS)
    if not isinstance(cfg["publish_attempts"], int) or cfg["publish_attempts"] < 1:
        return None, "invalid config: publish_attempts must be a positive integer"

    allowed = set()
    for p in raw["allowed_edit_paths"]:
        norm = normalize_rel_path(p)
        if norm is None:
            return None, f"invalid config: allowed_edit_paths entry unusable: {p!r}"
        allowed.add(norm)
    cfg["allowed"] = allowed
    cfg["forbidden"] = [posixpath.normpath(f) for f in cfg["forbidden_edit_paths"]]
    cfg["state_dir"] = os.path.abspath(os.path.expanduser(cfg["state_dir"]))
    return cfg, None


def is_argv(v):
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x for x in v)


# --------------------------------------------------------------------------
# state helpers
# --------------------------------------------------------------------------

class State:
    def __init__(self, cfg):
        self.dir = cfg["state_dir"]
        self.candidate = os.path.join(self.dir, "candidate")
        self.lock = os.path.join(self.dir, "lock")
        self.latest = os.path.join(self.dir, "latest.json")
        self.recovery = os.path.join(self.dir, "recovery.json")
        self.marker = os.path.join(self.dir, "candidate.json")
        os.makedirs(self.dir, exist_ok=True)

    def write_json_atomic(self, path, doc):
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dfd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def read_json(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def write_recovery(self, phase, head, base):
        self.write_json_atomic(self.recovery, {
            "schema": 1, "phase": phase, "head": head, "base": base,
            "time": utcnow(),
        })

    def clear_recovery(self):
        try:
            os.unlink(self.recovery)
        except FileNotFoundError:
            pass


def acquire_lock(state) -> int:
    """flock the state lock exclusively and non-blocking; returns fd."""
    fd = os.open(state.lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise MaintainerAbort("lock-busy",
                              "another maintenance run holds the state lock",
                              exit_code=EXIT_LOCKED)
    return fd


def ensure_candidate_repo(cfg, state, git: Git):
    """Create or verify the isolated candidate checkout (never the seed)."""
    git_dir = os.path.join(state.candidate, ".git")
    if os.path.exists(git_dir):
        marker = state.read_json(state.marker)
        expected = {"fork_url": cfg["fork_url"], "official_url": cfg["official_url"],
                    "deployment_branch": cfg["deployment_branch"]}
        if not marker or any(marker.get(k) != v for k, v in expected.items()):
            raise MaintainerAbort(
                "failed",
                "candidate checkout exists but was not created by this "
                "maintainer for this config (unknown git state); refusing. "
                f"Remove {state.candidate} manually if it is safe to do so.",
                exit_code=EXIT_REFUSED)
        return
    if os.path.exists(state.candidate):
        raise MaintainerAbort("failed",
                              f"{state.candidate} exists but is not a git checkout",
                              exit_code=EXIT_REFUSED)
    git = Git(cfg, state.dir, [])  # parent dir first: candidate does not exist yet
    run_cmd(["git", "init", "-q", "-b", "candidate", state.candidate],
            cwd=state.dir, timeout=cfg["command_timeout"],
            capture_bytes=cfg["output_capture_bytes"])
    git_cand = Git(cfg, state.candidate, [])
    for args in (("config", "user.name", "Fork Maintainer (local)"),
                 ("config", "user.email", "fork-maintainer@localhost"),
                 ("config", "commit.gpgsign", "false"),
                 ("config", "gc.auto", "0"),
                 ("config", "advice.detachedHead", "false"),
                 ("remote", "add", "origin", cfg["fork_url"]),
                 ("remote", "add", "upstream", cfg["official_url"])):
        git_cand.run(*args)
    state.write_json_atomic(state.marker, {
        "schema": 1, "fork_url": cfg["fork_url"],
        "official_url": cfg["official_url"],
        "deployment_branch": cfg["deployment_branch"],
        "created_utc": utcnow(),
    })


def porcelain_entries(git: Git):
    r = git.run("status", "--porcelain=v1", "-z", check=False)
    entries = []
    for chunk in r.out.split("\0"):
        if not chunk:
            continue
        code = chunk[:2]
        path = chunk[3:]
        entries.append((code, path))
    return entries, r.rc


def merge_in_progress(state) -> bool:
    git_dir = os.path.join(state.candidate, ".git")
    return any(os.path.exists(os.path.join(git_dir, f))
               for f in ("MERGE_HEAD", "rebase-merge", "rebase-apply"))


def recovery_snapshot(state, git):
    """Fingerprint dirty bytes and index so post-failure manual edits are never discarded."""
    entries, rc = porcelain_entries(git)
    if rc: return None
    files = []
    total = 0
    try:
        for code, path in sorted(entries):
            if normalize_rel_path(path) != path: return None
            target = os.path.join(state.candidate, path)
            if os.path.islink(target): digest = "link:" + os.readlink(target)
            elif os.path.isfile(target):
                total += os.path.getsize(target)
                if total > 268_435_456: return None
                h = hashlib.sha256()
                with open(target, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1_048_576), b""): h.update(chunk)
                digest = h.hexdigest()
            elif not os.path.lexists(target): digest = "missing"
            else: return None
            files.append([code, path, digest])
        with open(os.path.join(state.candidate, ".git", "index"), "rb") as handle:
            index = hashlib.sha256(handle.read()).hexdigest()
        return {"files": files, "index": index}
    except OSError: return None


def recover_or_refuse(cfg, state, git: Git):
    """Refuse dirty/unknown candidate state; roll back only our own recorded state."""
    entries, rc = porcelain_entries(git)
    if rc != 0:
        raise MaintainerAbort("failed", "git status failed in candidate checkout")
    dirty = bool(entries)
    midmerge = merge_in_progress(state)
    if not dirty and not midmerge:
        if state.read_json(state.recovery) is not None:
            state.clear_recovery()  # stale marker from a completed run
        return

    marker = state.read_json(state.recovery)
    if not marker:
        detail = "; ".join(f"{c} {p}" for c, p in entries[:10]) or "mid-merge"
        raise MaintainerAbort(
            "refused-dirty",
            f"candidate checkout is dirty or mid-merge with no recovery "
            f"marker; refusing to touch unknown work ({detail})",
            exit_code=EXIT_REFUSED)
    head = git.rev("HEAD", check=False)
    if not head or marker.get("head") != head:
        raise MaintainerAbort("refused-dirty",
                              "recovery marker does not match candidate HEAD; "
                              "refusing to touch unknown work",
                              exit_code=EXIT_REFUSED)
    snapshot = marker.get("snapshot")
    if not snapshot or snapshot != recovery_snapshot(state, git):
        raise MaintainerAbort("refused-dirty",
                              "candidate bytes/index differ from the owned failure snapshot; preserving unknown work",
                              exit_code=EXIT_REFUSED)
    if midmerge:
        git.run("merge", "--abort")
    git.run("reset", "--hard", "HEAD", check=False)
    for code, p in entries:
        if code.startswith("?"):
            target = os.path.join(state.candidate, p)
            if os.path.isfile(target) or os.path.islink(target):
                os.unlink(target)
    entries2, _ = porcelain_entries(git)
    if entries2 or merge_in_progress(state):
        raise MaintainerAbort("refused-dirty",
                              "candidate still dirty after recovery attempt; refusing",
                              exit_code=EXIT_REFUSED)


# --------------------------------------------------------------------------
# fetch + base decision
# --------------------------------------------------------------------------

def lsremote_sha(cfg, url, branch):
    r = run_cmd(["git", "ls-remote", url, f"refs/heads/{branch}"],
                cwd=cfg["state_dir"], timeout=cfg["command_timeout"],
                capture_bytes=cfg["output_capture_bytes"])
    if r.rc != 0:
        raise MaintainerAbort("failed", "Remote branch lookup failed; publication state is unknown")
    line = r.out.strip().splitlines()[0] if r.out.strip() else ""
    return (line.split("\t")[0] or None), r


def fetch_refs(cfg, state, git: Git, events):
    """Fetch only explicit refs; returns dict of resolved shas."""
    published_before, _ = lsremote_sha(cfg, cfg["fork_url"], cfg["deployment_branch"])
    official_sha = None
    if published_before:
        git.run("fetch", "--no-tags", cfg["fork_url"],
                f"+refs/heads/{cfg['deployment_branch']}:refs/maint/fork/published")
        fetched = git.rev(f"refs/maint/fork/published", check=False)
        if fetched != published_before:
            raise MaintainerAbort(
                "failed",
                "fork deployment branch moved between ls-remote and fetch "
                f"({published_before} -> {fetched}); refusing to run against a "
                "moving remote", )
    else:
        # Branch absent: nothing to fetch; creation path will CAS-create it.
        pass

    git.run("fetch", "--no-tags", cfg["official_url"],
            f"+refs/heads/{cfg['official_branch']}:refs/maint/official/{cfg['official_branch']}")
    official_sha = git.rev(f"refs/maint/official/{cfg['official_branch']}")

    seed_available = True
    fr = git.run("fetch", "--no-tags", cfg["seed_repo"],
                 "+refs/heads/*:refs/maint/seed/heads/*", check=False)
    if fr.rc != 0:
        seed_available = False
    seed_ok = False
    if seed_available:
        probe = git.run("cat-file", "-e", f"{cfg['seed_sha']}^{{commit}}", check=False)
        seed_ok = probe.rc == 0
    events.append(f"{utcnow()} fetched official={official_sha} published={published_before} "
                  f"seed_available={seed_available and seed_ok}")
    return {"published_before": published_before, "official_sha": official_sha,
            "seed_available": seed_available and seed_ok}


def decide_base(cfg, git: Git, shas):
    """Apply the seed/published lineage rules; returns (base, merge_targets)."""
    published = shas["published_before"]
    seed = cfg["seed_sha"]
    if not shas["seed_available"]:
        if published is None:
            raise MaintainerAbort(
                "failed",
                f"no published branch on fork and seed_repo {cfg['seed_repo']} does "
                f"not provide seed_sha {seed}; cannot bootstrap")
        base = published
        merge_targets = []
    elif published is None:
        base = seed
        merge_targets = []
        probe = git.run("cat-file", "-e", f"{seed}^{{commit}}", check=False)
        if probe.rc != 0:
            raise MaintainerAbort("failed",
                                  f"seed_sha {seed} not present after seed fetch")
    else:
        seed_is_ancestor = git.is_ancestor(seed, published)
        published_is_ancestor = git.is_ancestor(published, seed)
        if seed_is_ancestor and not published_is_ancestor:
            base = published            # seed already integrated upstream-side
        elif published_is_ancestor and not seed_is_ancestor:
            base = seed                 # published behind seed: build from seed
        elif seed == published:
            base = published
        else:
            base = seed                 # diverged: integrate published explicitly
        merge_targets = []
        if base == seed and published not in (None, seed) and \
                not git.is_ancestor(published, seed):
            merge_targets.append(("fork-published", published))
    if shas["official_sha"] and (base is None or not git.is_ancestor(shas["official_sha"], base)):
        merge_targets.append(("official", shas["official_sha"]))
    seed_integrated = (base == cfg["seed_sha"])
    published_integrated = (published is None) or (base == published)
    for lbl, sha in merge_targets:
        if lbl == "fork-published":
            published_integrated = True
    lineage = {"seed_integrated": seed_integrated,
               "published_integrated": published_integrated}
    return base, merge_targets, lineage


# --------------------------------------------------------------------------
# conflict collection / repair protocol
# --------------------------------------------------------------------------

def collect_conflicts(cfg, state, git: Git):
    r = git.run("ls-files", "-u", "-z", check=False)
    stages = {}
    for chunk in r.out.split("\0"):
        if not chunk:
            continue
        meta, path = chunk.split("\t", 1)
        _mode, sha, stage = meta.split()
        stages.setdefault(path, {})[int(stage)] = sha
    conflicts = []
    field_cap_per_file = max(256, (int(cfg["max_input_bytes"]) - 4096) //
                             (4 * max(1, len(stages))))
    for path in sorted(stages):
        blob = stages[path]

        def blob_text(stage_sha):
            if stage_sha is None:
                return ""
            raw = git.file_bytes(f"{stage_sha}")
            return decode_for_json(raw or b"")

        wt = os.path.join(state.candidate, path)
        if os.path.islink(wt) or not os.path.isfile(wt):
            merged_raw = b""
        else:
            with open(wt, "rb") as fh:
                merged_raw = fh.read(max(cfg["max_input_bytes"], 1_048_576) + 1)
            if len(merged_raw) > max(cfg["max_input_bytes"], 1_048_576):
                raise MaintainerAbort("blocked", "Merged conflict source exceeds bounded input size")
        conflicts.append({
            "path": path,
            "base": truncate_text(blob_text(blob.get(1)), field_cap_per_file),
            "ours": truncate_text(blob_text(blob.get(2)), field_cap_per_file),
            "theirs": truncate_text(blob_text(blob.get(3)), field_cap_per_file),
            "merged": truncate_text(decode_for_json(merged_raw), field_cap_per_file),
        })
    return conflicts


def decode_for_json(raw: bytes) -> str:
    if b"\x00" in raw:
        return "[binary or NUL content, %d bytes]\n" % len(raw) + \
            raw.replace(b"\x00", b"\xe2\x90\x80").decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def truncate_text(text: str, cap: int) -> str:
    raw = text.encode("utf-8", "replace")
    if len(raw) <= cap:
        return text
    keep = max(cap - 40, 0)
    return raw[:keep].decode("utf-8", "ignore") + \
        f"\n[truncated {len(raw) - keep} bytes to fit max_input_bytes]"


def fit_repair_payload(payload, limit):
    """Size the actual JSON bytes, including escaping; never shorten patch intents."""
    fitted = json.loads(json.dumps(payload))
    for _ in range(256):
        if len(json.dumps(fitted).encode("utf-8")) <= limit: return fitted
        choices = []
        for record in fitted.get("conflicts", []):
            for key in ("base", "ours", "theirs", "merged"):
                value = record.get(key, "")
                if len(value.encode("utf-8")) > 128:
                    choices.append((len(json.dumps(value)), record, key))
        for record in fitted.get("diagnostics", []):
            value = record.get("detail", "")
            if len(value.encode("utf-8")) > 128:
                choices.append((len(json.dumps(value)), record, "detail"))
        if not choices: break
        _, record, key = max(choices, key=lambda item: item[0])
        record[key] = truncate_text(record[key], max(128, len(record[key].encode("utf-8")) // 2))
    raise MaintainerAbort("blocked", "Patch intents/context do not fit the bounded repair input")


def build_repair_payload(cfg, conflicts, diagnostics, attempt, shas, base):
    return {
        "schema": 1,
        "context": {
            "official_url": cfg["official_url"],
            "official_branch": cfg["official_branch"],
            "fork_url": cfg["fork_url"],
            "deployment_branch": cfg["deployment_branch"],
            "base": base,
            "official_sha": shas.get("official_sha"),
            "published_before": shas.get("published_before"),
            "attempt": attempt,
            "attempts_left": max(0, cfg["max_repairs"] - attempt),
        },
        "patch_intents": cfg["patch_intents"],
        "conflicts": conflicts,
        "diagnostics": diagnostics[-20:],
    }


def parse_repair_response(r: Result):
    """Return the response dict, or None when unknown/unusable."""
    if r.timed_out:
        return None
    obj = None
    text = r.out.strip()
    if text:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if lines:
                try:
                    obj = json.loads(lines[-1])
                except json.JSONDecodeError:
                    obj = None
    if not isinstance(obj, dict):
        return None
    if obj.get("status") not in ("resolved", "blocked"):
        return None
    if not isinstance(obj.get("reason"), str):
        return None
    edits = obj.get("edits")
    if not isinstance(edits, list):
        return None
    for e in edits:
        if not isinstance(e, dict) or not isinstance(e.get("path"), str) or \
                not isinstance(e.get("content"), str):
            return None
    if r.rc != 0 and obj["status"] == "resolved":
        return None  # resolved over a failing exit code is not trustworthy
    return obj


def validate_edits(cfg, state, response):
    """Validate resolved edits; returns (norm_paths_contents, rejection_reasons)."""
    rejections = []
    accepted = []
    seen = set()
    total = 0
    forbidden = cfg["forbidden"]
    for e in response.get("edits", []):
        path_raw, content = e["path"], e["content"]
        norm = normalize_rel_path(path_raw)
        if norm is None:
            rejections.append(f"unusable path {path_raw!r}")
            continue
        if norm in seen:
            rejections.append(f"duplicate path in response: {norm}")
            continue
        seen.add(norm)
        if norm not in cfg["allowed"]:
            rejections.append(f"path not allowlisted: {norm}")
            continue
        if any(norm == f or norm.startswith(f + "/") for f in forbidden):
            rejections.append(f"path is on the forbidden list: {norm}")
            continue
        if "\x00" in content:
            rejections.append(f"NUL byte in content for {norm}")
            continue
        try:
            blob = content.encode("utf-8")
        except UnicodeEncodeError:
            rejections.append(f"content not UTF-8 encodable for {norm}")
            continue
        if len(blob) > cfg["max_edit_bytes"]:
            rejections.append(f"edit for {norm} exceeds max_edit_bytes "
                              f"({len(blob)} > {cfg['max_edit_bytes']})")
            continue
        total += len(blob)
        if total > cfg["max_edit_bytes"]:
            rejections.append(f"combined edits exceed max_edit_bytes "
                              f"({total} > {cfg['max_edit_bytes']})")
            continue
        if CONFLICT_MARKER_RE.search(blob):
            rejections.append(f"conflict marker present in content for {norm}")
            continue
        target = os.path.join(state.candidate, norm)
        # walk parent chain: no symlinks, no non-directories
        parent = state.candidate
        chain_ok = True
        for part in norm.split("/")[:-1]:
            parent = os.path.join(parent, part)
            if os.path.islink(parent) or (os.path.exists(parent) and not os.path.isdir(parent)):
                rejections.append(f"symlink or non-directory in parent chain of {norm}")
                chain_ok = False
                break
        if not chain_ok:
            continue
        if os.path.islink(target):
            rejections.append(f"refusing to write through symlink: {norm}")
            continue
        if os.path.exists(target) and not os.path.isfile(target):
            rejections.append(f"target exists and is not a regular file: {norm}")
            continue
        if not os.path.isdir(os.path.dirname(target)):
            rejections.append(f"parent directory does not exist for new file: {norm}")
            continue
        accepted.append((norm, blob))
    return accepted, rejections


def apply_edits(cfg, state, git: Git, accepted):
    for norm, blob in accepted:
        target = os.path.join(state.candidate, norm)
        with open(target, "wb") as fh:
            fh.write(blob)
        with open(target, "rb") as fh:
            if fh.read() != blob:
                raise MaintainerAbort("failed", f"apply verification failed for {norm}")
        git.run("add", "--", norm)


# --------------------------------------------------------------------------
# main pipeline
# --------------------------------------------------------------------------

class Run:
    def __init__(self, cfg, publish):
        self.cfg = cfg
        self.publish = publish
        self.state = State(cfg)
        self.events = []
        self.git = Git(cfg, self.state.candidate, self.events)
        self.attempts = 0
        self.repair_results = []
        self.diagnostics = []
        self.checks = {"runtime": None, "tests": None}
        self.merges = []
        self.shas = {}
        self.base = None
        self.lineage = {"seed_integrated": False, "published_integrated": False}

    # -- repair invocation -------------------------------------------------
    def repair(self, conflicts):
        cfg = self.cfg
        if self.attempts >= cfg["max_repairs"]:
            last = self.repair_results[-1] if self.repair_results else {}
            raise MaintainerAbort(
                "failed",
                f"repair budget exhausted after {self.attempts} attempts "
                f"(max_repairs={cfg['max_repairs']}); last problem: "
                f"{json.dumps(last.get('reason') or last.get('rejections') or 'unknown')[:300]}")
        self.attempts += 1
        if not conflicts:
            # Failed tests still require actual source context, not just a traceback.
            for path in sorted(cfg["allowed"]):
                if any(path == p or path.startswith(p + "/") for p in cfg["forbidden"]): continue
                target = os.path.join(self.state.candidate, path)
                if os.path.islink(target) or not os.path.isfile(target): continue
                with open(target, "rb") as source: raw_source = source.read(cfg["max_input_bytes"] + 1)
                if len(raw_source) > cfg["max_input_bytes"]: continue
                source_text = raw_source.decode("utf-8", "strict")
                conflicts.append({"path": path, "base": "", "ours": source_text,
                                  "theirs": "", "merged": source_text,
                                  "kind": "current-source-for-test-repair"})
        payload = build_repair_payload(cfg, conflicts, self.diagnostics,
                                       self.attempts, self.shas, self.base)
        payload = fit_repair_payload(payload, cfg["max_input_bytes"])
        raw = json.dumps(payload)
        if len(raw.encode("utf-8")) > cfg["max_input_bytes"]:
            raise MaintainerAbort("failed",
                                  "repair input exceeds max_input_bytes even after "
                                  "field truncation; refusing to send")
        prompted_by = list(payload["diagnostics"])
        r = run_cmd(cfg["repair_command"], cwd=self.state.dir,
                    timeout=cfg["command_timeout"], stdin_text=raw,
                    capture_bytes=max(cfg["output_capture_bytes"], cfg["max_edit_bytes"] * 6 + 4096))
        if r.timed_out:
            self.repair_results.append({"attempt": self.attempts, "status": "timeout",
                                        "prompted_by": prompted_by,
                                        "reason": f"repair command timed out after "
                                                  f"{cfg['command_timeout']}s"})
            raise MaintainerAbort("failed",
                                  f"repair command timed out after {cfg['command_timeout']}s")
        response = parse_repair_response(r)
        if response is None:
            self.repair_results.append({
                "attempt": self.attempts, "status": "unknown",
                "prompted_by": prompted_by,
                "reason": f"repair adapter returned unknown/unusable result "
                          f"(rc={r.rc}, output: {r.text[:300]!r})"})
            raise MaintainerAbort(
                "failed",
                f"repair adapter returned unknown/unusable result (rc={r.rc}); "
                f"output tail: {r.text[:300]!r}")
        if response["status"] == "blocked":
            reason = response.get("reason") or "repair adapter blocked without reason"
            self.repair_results.append({"attempt": self.attempts, "status": "blocked",
                                        "prompted_by": prompted_by,
                                        "reason": reason})
            raise MaintainerAbort("blocked", reason)
        accepted, rejections = validate_edits(cfg, self.state, response)
        result = {"attempt": self.attempts, "status": "resolved",
                  "reason": response.get("reason", ""),
                  "prompted_by": prompted_by,
                  "rejections": rejections,
                  "applied_paths": [p for p, _ in accepted]}
        self.repair_results.append(result)
        if rejections:
            self.diagnostics.append({"source": "reject",
                                     "detail": "; ".join(rejections)[:800]})
            return False
        if not accepted:
            self.diagnostics.append({"source": "reject",
                                     "detail": "resolved response contained no edits"})
            return False
        apply_edits(cfg, self.state, self.git, accepted)
        return True

    # -- merge ---------------------------------------------------------------
    def do_merge(self, label, target):
        cfg, git, state = self.cfg, self.git, self.state
        msg = f"Merge {label} {target[:12]} into {cfg['deployment_branch']} (maintainer)"
        r = git.run("merge", "--no-ff", "--no-commit", "-m", msg, target, check=False)
        first = True
        while True:
            merge_head = os.path.exists(os.path.join(state.candidate, ".git", "MERGE_HEAD"))
            conflicts = collect_conflicts(cfg, state, git)
            if r.rc == 0 and not merge_head:
                self.merges.append({"label": label, "sha": target, "result": "up-to-date"})
                return
            if r.rc == 0 and merge_head and not conflicts:
                self.commit_index(msg, merge=True)
                self.merges.append({"label": label, "sha": target, "result": "merged"})
                return
            if conflicts:
                if first and r.rc not in (0, 1):
                    raise MaintainerAbort(
                        "failed", f"git merge failed rc={r.rc}: {r.text[:300]}")
                first = False
                applied = self.repair(conflicts)
                if applied:
                    remaining = collect_conflicts(cfg, state, git)
                    if not remaining:
                        self.commit_index(msg, merge=True)
                        self.merges.append({"label": label, "sha": target,
                                            "result": "merged+repaired"})
                        return
                    self.diagnostics.append({
                        "source": "merge",
                        "detail": "still unmerged after edits: "
                                  + ", ".join(c["path"] for c in remaining)})
                # rejected or partial edits: loop; the merge stays in progress
                # and repair runs again with accumulated diagnostics
                continue
            raise MaintainerAbort("failed",
                                  f"git merge {label} {target[:12]} failed rc={r.rc}: "
                                  f"{r.text[:300]}")

    def commit_index(self, msg, merge):
        git = self.git
        entries, _ = porcelain_entries(git)
        if entries:
            r = git.run("commit", "-m", msg, check=False)
            if r.rc != 0 and merge:
                r = git.run("commit", "--allow-empty", "-m", msg, check=False)
            if r.rc != 0:
                raise MaintainerAbort("failed",
                                      f"git commit failed rc={r.rc}: {r.text[:300]}")
        elif merge:
            # complete the merge even when the resolved tree matches HEAD
            r = git.run("commit", "-m", msg, check=False)
            if r.rc != 0:
                r2 = git.run("commit", "--allow-empty", "-m", msg, check=False)
                if r2.rc != 0:
                    raise MaintainerAbort("failed",
                                          f"git commit failed rc={r2.rc}: {r2.text[:300]}")

    # -- validation ------------------------------------------------------------
    def expand(self, argv):
        return [a.replace("{candidate}", self.state.candidate) for a in argv]

    def run_checks(self):
        cfg = self.cfg
        if cfg["check_command"]:
            r = run_cmd(self.expand(cfg["check_command"]), cwd=self.state.candidate,
                        timeout=cfg["command_timeout"],
                        capture_bytes=cfg["output_capture_bytes"])
            self.checks["runtime"] = {
                "status": "pass" if (r.rc == 0 and not r.timed_out) else "fail",
                "exit_code": r.rc, "timed_out": r.timed_out, "tail": r.text[-1000:]}
            if r.rc != 0 or r.timed_out:
                self.diagnostics.append({
                    "source": "runtime",
                    "detail": ("runtime check timed out" if r.timed_out
                               else f"runtime check failed rc={r.rc}") +
                              ": " + r.text[-1500:]})
            return
        self.checks["runtime"] = None

    def run_tests(self):
        cfg = self.cfg
        r = run_cmd(self.expand(cfg["test_command"]), cwd=self.state.candidate,
                    timeout=cfg["test_timeout"],
                    capture_bytes=cfg["output_capture_bytes"])
        ok = r.rc == 0 and not r.timed_out
        self.checks["tests"] = {"status": "pass" if ok else "fail",
                                "exit_code": r.rc, "timed_out": r.timed_out,
                                "tail": r.text[-2000:]}
        if not ok:
            self.diagnostics.append({
                "source": "test",
                "detail": ("test command timed out" if r.timed_out
                           else f"test command failed rc={r.rc}") + ": " + r.text[-1500:]})
        return ok

    def validate_loop(self):
        """Run runtime check + tests; loop repairs until pass or budget out."""
        cfg, git, state = self.cfg, self.git, self.state
        self.state.write_recovery("test", git.rev("HEAD"), self.base)
        while True:
            self.run_checks()
            ok = self.run_tests()
            runtime_ok = not self.checks["runtime"] or \
                self.checks["runtime"]["status"] == "pass"
            if ok and runtime_ok:
                return
            applied = self.repair([])  # no conflicts; diagnostics carry the failure
            if applied:
                self.commit_index(
                    f"Apply maintainer-validated repair edits (attempt {self.attempts})",
                    merge=False)

    # -- gates + publish ---------------------------------------------------------
    def conflict_marker_scan(self):
        """Scan every file this run changed (base..HEAD) for conflict markers.

        Binary files and links are skipped; files are read up to 1 MiB.
        """
        r = self.git.run("diff", "--name-only", "--diff-filter=ACMR",
                         f"{self.base}..HEAD", check=False)
        for p in r.out.splitlines():
            p = p.strip()
            if not p:
                continue
            fp = os.path.join(self.state.candidate, p)
            if os.path.islink(fp) or not os.path.isfile(fp):
                continue
            with open(fp, "rb") as fh:
                blob = fh.read(1000000)
            if b"\x00" in blob:
                continue
            for line in blob.splitlines():
                if line.startswith(b"<<<<<<<") or line.startswith(b">>>>>>>") or \
                        line in (b"=======", b"|||||||"):
                    raise MaintainerAbort(
                        "failed",
                        f"refusing to publish: conflict marker detected in {p}")

    def final_gates(self):
        git, state = self.git, self.state
        entries, _ = porcelain_entries(git)
        if entries:
            raise MaintainerAbort("failed",
                                  "refusing to publish: candidate has unstaged or "
                                  f"untracked content: {[e for e in entries[:8]]}")
        if merge_in_progress(state):
            raise MaintainerAbort("failed",
                                  "refusing to publish: merge still in progress")
        if collect_conflicts(self.cfg, state, git):
            raise MaintainerAbort("failed",
                                  "refusing to publish: unmerged index entries remain")
        self.conflict_marker_scan()
        head = git.rev("HEAD", check=False)
        if head != self.candidate_sha:
            raise MaintainerAbort("failed",
                                  "refusing to publish: HEAD moved after validation")
        expected_old = self.shas["published_before"]
        if expected_old and not git.is_ancestor(expected_old, head):
            raise MaintainerAbort("failed",
                                  "refusing to publish: expected_old "
                                  f"{expected_old[:12]} is not an ancestor of candidate")

    def publish_candidate(self):
        cfg, git = self.cfg, self.git
        branch = cfg["deployment_branch"]
        candidate = self.candidate_sha
        expected_old = self.shas["published_before"]
        refspec = f"{candidate}:refs/heads/{branch}"
        attempts_left = cfg["publish_attempts"]
        reason = None
        while attempts_left > 0:
            attempts_left -= 1
            argv = ["push", "--porcelain"]
            argv.append(f"--force-with-lease=refs/heads/{branch}:{expected_old or ''}")
            argv += [cfg["fork_url"], refspec]
            r = git.run(*argv, check=False)
            remote, rr = lsremote_sha(cfg, cfg["fork_url"], branch)
            self.shas["remote_after"] = remote
            if remote == candidate:
                return {"requested": True, "status": "published",
                        "expected_old": expected_old, "remote_after": remote,
                        "method": "push" if r.rc == 0 else "reconciled",
                        "reason": ""}
            if r.rc == 0:
                reason = f"push reported success but remote is {remote!r}"
            elif remote in (expected_old, None):
                reason = f"push rejected rc={r.rc}: {r.text[:300]}"
                if attempts_left:
                    self.events.append(f"{utcnow()} push retry: {reason[:200]}")
                    continue
            else:
                return {"requested": True, "status": "remote-moved",
                        "expected_old": expected_old, "remote_after": remote,
                        "method": None,
                        "reason": f"fork deployment branch moved concurrently "
                                  f"(now {remote}); refusing to overwrite; "
                                  f"push output: {r.text[:200]}"}
        return {"requested": True, "status": "failed", "expected_old": expected_old,
                "remote_after": self.shas.get("remote_after"), "method": None,
                "reason": reason or "push failed"}

    # -- cache for no-op validation ---------------------------------------------
    def cached_validation(self):
        if not self.cfg.get("reuse_validation", True): return False
        prev = self.state.read_json(self.state.latest)
        if not prev:
            return False
        if prev.get("status") not in ("prepared", "no-op", "published"):
            return False
        if not prev.get("checks", {}).get("tests") or \
                prev["checks"]["tests"].get("status") != "pass":
            return False
        same = (prev.get("official", {}).get("sha") == self.shas["official_sha"] and
                prev.get("fork", {}).get("published_before") == self.shas["published_before"] and
                prev.get("seed", {}).get("sha") == self.cfg["seed_sha"] and
                prev.get("base") == self.base and
                prev.get("candidate") == self.candidate_sha)
        return same

    # -- orchestration ---------------------------------------------------------
    def execute(self):
        cfg, state, git = self.cfg, self.state, self.git
        ensure_candidate_repo(cfg, state, git)
        recover_or_refuse(cfg, state, git)
        self.shas = fetch_refs(cfg, state, git, self.events)
        self.base, merge_targets, self.lineage = decide_base(cfg, git, self.shas)
        state.write_recovery("merge", self.base, self.base)
        git.run("checkout", "-q", "-B", "candidate", self.base)
        for label, sha in merge_targets:
            self.do_merge(label, sha)
        self.candidate_sha = git.rev("HEAD", check=False)
        if self.candidate_sha == self.shas["published_before"] and \
                not merge_targets and self.cached_validation():
            self.checks = {"runtime": None,
                           "tests": {"status": "pass", "cached": True,
                                     "tail": "no-op update: reusing previous "
                                             "passing validation for identical "
                                             "input SHAs"}}
            status = "no-op"
        else:
            self.validate_loop()
            self.candidate_sha = git.rev("HEAD", check=False)
            published_before = self.shas["published_before"]
            status = "no-op" if self.candidate_sha == published_before else "prepared"
        self.final_gates()
        publish_doc = {"requested": self.publish, "status": "skipped-prepare-only",
                       "expected_old": self.shas["published_before"],
                       "remote_after": self.shas["published_before"],
                       "method": None, "reason": "prepare-only run (no --publish)"}
        if self.publish:
            if self.candidate_sha == self.shas["published_before"]:
                publish_doc = {"requested": True, "status": "skipped-no-op",
                               "expected_old": self.shas["published_before"],
                               "remote_after": self.shas["published_before"],
                               "method": None,
                               "reason": "candidate equals published branch"}
            else:
                publish_doc = self.publish_candidate()
        state.clear_recovery()
        if publish_doc["status"] in ("failed", "remote-moved"):
            return ("failed", f"publication did not complete: {publish_doc['reason']}",
                    publish_doc, EXIT_FAILED)
        status = ("published" if publish_doc["status"] == "published"
                  else "no-op" if status == "no-op" else status)
        return status, "", publish_doc, EXIT_OK


def build_latest(cfg, runobj, status, reason, publish_doc, exit_code):
    shas = runobj.shas
    return {
        "schema": 1,
        "started_utc": runobj.started_utc,
        "finished_utc": utcnow(),
        "status": status,
        "reason": reason,
        "exit_code": exit_code,
        "input": {
            "official_url": cfg["official_url"],
            "official_branch": cfg["official_branch"],
            "fork_url": cfg["fork_url"],
            "deployment_branch": cfg["deployment_branch"],
            "seed_repo": cfg["seed_repo"],
            "seed_sha": cfg["seed_sha"],
            "publish_requested": runobj.publish,
        },
        "official": {"url": cfg["official_url"], "branch": cfg["official_branch"],
                     "sha": shas.get("official_sha")},
        "fork": {"url": cfg["fork_url"], "branch": cfg["deployment_branch"],
                 "published_before": shas.get("published_before"),
                 "published_after": shas.get("remote_after",
                                             shas.get("published_before"))},
        "seed": {"repo": cfg["seed_repo"], "sha": cfg["seed_sha"],
                 "available": shas.get("seed_available", False),
                 "used_as_base": runobj.base == cfg["seed_sha"]},
        "base": runobj.base,
        "candidate": getattr(runobj, "candidate_sha", None),
        "merges": runobj.merges,
        "lineage": runobj.lineage,
        "repairs": {"max": cfg["max_repairs"], "attempts": runobj.attempts,
                    "results": runobj.repair_results[-20:],
                    "last": runobj.repair_results[-1] if runobj.repair_results else None},
        "checks": runobj.checks,
        "publish": publish_doc,
        "candidate_checkout": runobj.state.candidate,
        "events": runobj.events[-MAX_EVENTS:],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Central fork maintenance orchestrator: build, repair, test "
                    "and (with --publish) CAS-publish the fork deployment branch. "
                    "See module docstring for the full config/protocol contract.",
        epilog="Exit codes: 0 success, 1 failed/blocked, 2 config error, "
               "3 lock busy, 4 safety refusal.")
    ap.add_argument("--config", required=True, help="path to config.json")
    ap.add_argument("--publish", action="store_true",
                    help="publish the validated candidate (CAS push); "
                         "default is prepare-only")
    args = ap.parse_args(argv)

    cfg, err = load_config(args.config)
    if cfg is None:
        print(json.dumps({"status": "config-error", "exit_code": EXIT_CONFIG,
                          "reason": err}))
        print(err, file=sys.stderr)
        return EXIT_CONFIG

    runobj = Run(cfg, args.publish)
    runobj.started_utc = utcnow()
    latest_doc = None
    exit_code = EXIT_FAILED
    status, reason = "failed", ""
    lock_fd = None
    try:
        lock_fd = acquire_lock(runobj.state)
        status, reason, publish_doc, exit_code = runobj.execute()
    except MaintainerAbort as abort:
        status, reason, exit_code = abort.status, abort.reason, abort.exit_code
        if status == "lock-busy":
            print(json.dumps({"status": "lock-busy", "exit_code": EXIT_LOCKED,
                              "reason": reason}))
            print(reason, file=sys.stderr)
            return EXIT_LOCKED
        publish_doc = getattr(runobj, "_last_publish_doc", None) or {
            "requested": args.publish, "status": "not-attempted",
            "expected_old": runobj.shas.get("published_before"),
            "remote_after": None, "method": None, "reason": reason}
    except Exception as exc:  # unexpected: record everything we know
        status = "failed"
        reason = f"internal error: {type(exc).__name__}: {exc}"
        publish_doc = {"requested": args.publish, "status": "not-attempted",
                       "expected_old": runobj.shas.get("published_before"),
                       "remote_after": None, "method": None, "reason": reason}
        import traceback
        runobj.events.append(traceback.format_exc()[-1000:])

    if exit_code == EXIT_FAILED and lock_fd is not None:
        marker = runobj.state.read_json(runobj.state.recovery)
        if marker:
            snapshot = recovery_snapshot(runobj.state, runobj.git)
            if snapshot is not None:
                marker.update(snapshot=snapshot, head=runobj.git.rev("HEAD", check=False))
                runobj.state.write_json_atomic(runobj.state.recovery, marker)
    latest_doc = build_latest(cfg, runobj, status, reason, publish_doc, exit_code)
    try:
        runobj.state.write_json_atomic(runobj.state.latest, latest_doc)
    except OSError as exc:
        reason += f" (failed to write latest.json: {exc})"
        latest_doc["reason"] = reason
        exit_code = EXIT_FAILED
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    print(json.dumps({"status": status, "exit_code": exit_code, "reason": reason,
                      "candidate": latest_doc.get("candidate"),
                      "base": latest_doc.get("base"),
                      "publish": publish_doc.get("status"),
                      "repairs": runobj.attempts,
                      "tests": (runobj.checks.get("tests") or {}).get("status"),
                      "latest_json": runobj.state.latest}))
    if exit_code != EXIT_OK:
        print(reason, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
