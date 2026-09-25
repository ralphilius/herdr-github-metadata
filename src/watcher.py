#!/usr/bin/env python3
"""github-metadata plugin: report the PR each agent is working on.

Resolution per agent pane (pure logic, no LLM):
  1. the agent's own session transcript: the latest worktree it used -> that
     branch's open PR; only when no worktree appears in the tail, the latest
     pull/<n> or `gh pr <verb> <n>` reference
  2. otherwise the pane checkout branch's open PR
  3. otherwise — only when a single agent works in that repo — the repo's most
     recent open PR

The result is reported as the pane token `pr` ("#<n>") and refreshed every 2s
while the Herdr server runs.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict

INTERVAL_SECONDS = 2.0
PR_TTL_SECONDS = 120
PR_LOOKUPS_PER_RUN = 3
SESSION_TAIL_BYTES = 400_000
TOKEN_TTL_MS = 10_000
SOURCE = "github-metadata"

WT_NAME_RE = re.compile(r"\.worktrees/([A-Za-z0-9_.-]+)")
PR_URL_RE = re.compile(r"pull/(\d+)")
PR_CLI_RE = re.compile(r"gh pr (?:view|checks|review|diff|comment|merge|close|reopen|edit|ready|lock|unlock)\s+(\d+)")

_running = True


def _stop(_signum, _frame):
    global _running
    _running = False


def already_running():
    """Guard against duplicate watchers (startup hook + manual action)."""
    try:
        with open(PID_PATH) as handle:
            pid = int(handle.read().strip())
        os.kill(pid, 0)
        return pid != os.getpid()
    except Exception:
        return False


def herdr_bin():
    env_bin = os.environ.get("HERDR_BIN_PATH") or ""
    if os.access(env_bin, os.X_OK):
        return env_bin
    found = shutil.which("herdr")
    if found:
        return found
    return os.path.expanduser("~/.local/bin/herdr")


BIN = herdr_bin()
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "state"
)
os.makedirs(STATE_DIR, exist_ok=True)
CACHE_PATH = os.path.join(STATE_DIR, "pr-cache.json")
PID_PATH = os.path.join(STATE_DIR, "watcher.pid")


def log(message):
    print(message, flush=True)


def run(args, timeout=5, cwd=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def repo_info(cwd):
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        common = run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"], timeout=3).stdout.strip()
        branch = run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], timeout=3).stdout.strip()
    except Exception:
        return None
    if not common:
        return None
    root = os.path.dirname(common)
    return common, root, (branch if branch and branch != "HEAD" else None)


def session_refs(session_path):
    try:
        with open(session_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - SESSION_TAIL_BYTES))
            text = handle.read().decode("utf-8", "replace")
    except Exception:
        return []
    refs = []
    for match in WT_NAME_RE.finditer(text):
        refs.append((match.start(), "wt", match.group(1)))
    for match in PR_URL_RE.finditer(text):
        refs.append((match.start(), "pr", int(match.group(1))))
    for match in PR_CLI_RE.finditer(text):
        refs.append((match.start(), "pr", int(match.group(1))))
    return refs


def worktree_path(root, name):
    try:
        proc = run(["git", "-C", root, "worktree", "list", "--porcelain"], timeout=3)
        for line in proc.stdout.splitlines():
            if line.startswith("worktree ") and os.path.basename(line[9:].strip()) == name:
                return line[9:].strip()
    except Exception:
        pass
    return None


def branch_of(directory):
    try:
        branch = run(["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"], timeout=3).stdout.strip()
    except Exception:
        return None
    return branch if branch and branch != "HEAD" else None


class Resolver:
    def __init__(self, gh):
        self.gh = gh
        self.now = time.time()
        self.budget = PR_LOOKUPS_PER_RUN
        try:
            self.cache = json.load(open(CACHE_PATH))
        except Exception:
            self.cache = {}

    def save(self):
        try:
            with open(CACHE_PATH, "w") as handle:
                json.dump(self.cache, handle)
        except Exception:
            pass

    def _gh_number(self, repo_dir, extra):
        if not self.gh:
            return None
        proc = run([self.gh, "pr", "list", "--state", "open", "--limit", "1", "--json", "number"] + extra,
                   timeout=6, cwd=repo_dir)
        if proc.returncode != 0:
            return None
        items = json.loads(proc.stdout)
        return items[0].get("number") if items else None

    def lookup(self, key, repo_dir, extra):
        entry = self.cache.get(key)
        if entry is not None and self.now - entry.get("at", 0) <= PR_TTL_SECONDS:
            return entry.get("pr")
        if not self.gh or self.budget <= 0:
            return entry.get("pr") if entry else None
        self.budget -= 1
        number = entry.get("pr") if entry else None
        try:
            found = self._gh_number(repo_dir, extra)
            if found is not None:
                number = found
        except Exception:
            pass
        self.cache[key] = {"pr": number, "at": self.now}
        return number

    def pr_open(self, common, repo_dir, number):
        key = common + "\x00#" + str(number)
        entry = self.cache.get(key)
        if entry is not None and self.now - entry.get("at", 0) <= PR_TTL_SECONDS:
            return entry.get("open")
        if not self.gh or self.budget <= 0:
            return entry.get("open") if entry else None
        self.budget -= 1
        state = None
        try:
            proc = run([self.gh, "pr", "view", str(number), "--json", "state", "--jq", ".state"],
                       timeout=6, cwd=repo_dir)
            if proc.returncode == 0:
                state = proc.stdout.strip() == "OPEN"
        except Exception:
            state = entry.get("open") if entry else None
        self.cache[key] = {"open": state, "at": self.now}
        return state


def report(pane_id, pr):
    args = [BIN, "pane", "report-metadata", pane_id,
            "--source", SOURCE, "--ttl-ms", str(TOKEN_TTL_MS)]
    args += ["--token", f"pr={pr}"] if pr else ["--clear-token", "pr"]
    try:
        run(args, timeout=5)
    except Exception:
        pass


def resolve_agent(agent, panes, agents_per_repo, resolver):
    """Return "#<n>" for this agent's PR, or None. `resolved` reports whether a
    definitive answer was reached (so no weaker fallback should apply)."""
    pane = panes.get(agent.get("pane_id"), {})
    cwd = (
        agent.get("foreground_cwd")
        or agent.get("cwd")
        or pane.get("foreground_cwd")
        or pane.get("cwd")
    )
    info = repo_info(cwd)
    if not info:
        return None, True
    common, root, branch = info

    session = agent.get("agent_session") or {}
    session_path = session.get("value") if session.get("kind") == "path" else None
    if session_path and os.path.isfile(session_path):
        refs = session_refs(session_path)
        worktrees = [ref for ref in refs if ref[1] == "wt"]
        pr_refs = [ref for ref in refs if ref[1] == "pr"]
        if worktrees:
            kind, value = "wt", max(worktrees, key=lambda ref: ref[0])[2]
        elif pr_refs:
            kind, value = "pr", max(pr_refs, key=lambda ref: ref[0])[2]
        else:
            kind, value = None, None
        if kind == "pr":
            state = resolver.pr_open(common, root, value)
            if state is True:
                return f"#{value}", True
            if state is False:
                return None, True
        elif kind == "wt":
            wt = worktree_path(root, value)
            if wt:
                wt_branch = branch_of(wt)
                if wt_branch:
                    number = resolver.lookup(common + "\x00" + wt_branch, wt, ["--head", wt_branch])
                    return (f"#{number}" if number else None), True

    if branch:
        number = resolver.lookup(common + "\x00" + branch, cwd, ["--head", branch])
        if number:
            return f"#{number}", True
        if agents_per_repo[common] == 1:
            number = resolver.lookup(common + "\x00*", cwd, [])
            if number:
                return f"#{number}", True
    return None, True


def tick(snap, last_report, resolver):
    panes = {pane["pane_id"]: pane for pane in snap.get("panes", [])}
    agents = [agent for agent in snap.get("agents", []) if agent.get("pane_id")]
    agents_per_repo = defaultdict(int)
    for agent in agents:
        pane = panes.get(agent.get("pane_id"), {})
        cwd = agent.get("foreground_cwd") or agent.get("cwd") or pane.get("foreground_cwd") or pane.get("cwd")
        info = repo_info(cwd)
        if info:
            agents_per_repo[info[0]] += 1

    seen = set()
    for agent in agents:
        pane_id = agent["pane_id"]
        seen.add(pane_id)
        pr, _ = resolve_agent(agent, panes, agents_per_repo, resolver)
        if last_report.get(pane_id) != pr:
            last_report[pane_id] = pr
            log(f"{pane_id} -> {pr or '(none)'}")
        report(pane_id, pr)

    for pane_id in list(last_report):
        if pane_id not in seen:
            last_report.pop(pane_id, None)


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if already_running():
        log("github-metadata watcher already running; exiting")
        return 0
    try:
        with open(PID_PATH, "w") as handle:
            handle.write(str(os.getpid()))
    except Exception:
        pass
    gh = shutil.which("gh")
    log(f"github-metadata plugin watching (herdr={BIN}, gh={gh or 'missing'})")
    last_report = {}
    failures = 0
    while _running:
        started = time.time()
        try:
            out = run([BIN, "api", "snapshot"], timeout=5).stdout
            snap = json.loads(out)["result"]["snapshot"]
            failures = 0
        except Exception as err:
            failures += 1
            if failures >= 15:
                log(f"snapshot failed {failures} times in a row, exiting: {err}")
                try:
                    os.unlink(PID_PATH)
                except Exception:
                    pass
                return 1
            time.sleep(INTERVAL_SECONDS)
            continue
        resolver = Resolver(gh)
        tick(snap, last_report, resolver)
        resolver.save()
        time.sleep(max(0.2, INTERVAL_SECONDS - (time.time() - started)))
    log("github-metadata plugin stopping")
    try:
        os.unlink(PID_PATH)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
