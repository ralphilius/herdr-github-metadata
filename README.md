# herdr-github-metadata

A [Herdr](https://herdr.dev) plugin that shows GitHub metadata for each agent in
the sidebar — currently the pull request it is working on.

- Each agent pane gets a `pr` token (`#123`) resolved from its own session
  record, git worktrees, and `gh`.
- Pure logic, no LLM: no model calls, no prompts — just a few cached `gh`
  lookups.
- When no open PR is found, the token simply disappears — never invented.

Resolution order per agent:

1. The latest worktree used in the agent's session transcript → that branch's
   open PR (`pull/<n>` / `gh pr view <n>` mentions are only used when no
   worktree appears in the transcript tail, so reading other PRs cannot
   misattribute one).
2. Otherwise the pane's checkout branch's open PR.
3. Otherwise — only when a single agent works in that repository — the
   repository's most recent open PR.

The transcript source uses the session path reported by the Herdr integration
(omp today; claude reports a transcript path too), so other harnesses fall back
to git-only resolution rather than breaking.

## Requirements

- Herdr `>= 0.9.0`
- Python 3 (stdlib only)
- `gh`, authenticated — used for PR lookups; without it the plugin stays quiet

## Install

From GitHub:

```sh
herdr plugin install ralphilius/herdr-github-metadata
```

Or link a local checkout while developing:

```sh
git clone https://github.com/ralphilius/herdr-github-metadata
herdr plugin link herdr-github-metadata
```

## Setup

Add the token to your agent sidebar rows (any position):

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "agent", "$pr", "workspace", "tab"],
  ["terminal_title_stripped"],
]
```

## How it runs

The plugin starts a watcher alongside the Herdr server (startup hook) and
reports the `pr` pane token every 2 seconds. Start it without restarting the
server:

```sh
herdr plugin action invoke start --plugin herdr.github-metadata
```

State and the PR cache live under
`~/.local/state/herdr/plugins/herdr.github-metadata`. The watcher is
pid-guarded, exits once the server is gone, and re-arms on the next server
start.

## Notes

- A PR opened or closed takes up to ~2 minutes to show (lookup cache TTL).
- Repos with several agents only show numbers that are unambiguous; no
  repo-wide guessing when multiple agents share one repository.
- `gh` must be on the Herdr server's `PATH`.
