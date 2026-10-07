# 🎈 fly-tui

A high-performance, developer-centric Terminal UI for managing [Fly.io](https://fly.io) machines.

Built with Python and [Textual](https://textual.textualize.io/), `fly-tui` provides an instant, real-time dashboard for your Fly.io infrastructure without leaving your terminal.

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![PyPI](https://img.shields.io/badge/pypi-v0.1.0-orange)

## Contents

- [Features](#-features)
- [Installation](#-installation)
- [Running it](#-running-it)
- [The two views, and when each opens](#-the-two-views-and-when-each-opens)
- [Command-line flags](#-command-line-flags)
- [Key bindings](#%EF%B8%8F-key-bindings)
- [The multi-account view](#-the-multi-account-view)
- [Filtering](#-filtering)
- [Accounts config (`accounts.toml`)](#-accounts-config-accountstoml)
- [Safety and read-only accounts](#-safety-and-read-only-accounts)
- [The inspect panel](#-the-inspect-panel)
- [Mock mode (`--mock`)](#-mock-mode---mock)
- [Performance](#-performance)
- [Limitations](#-limitations)
- [Running the tests](#-running-the-tests)

## ✨ Features

- **🚀 Instant Insights:** Zero-config dashboard that auto-detects your Fly app.
- **📡 Real-time Monitoring:** Configurable refresh intervals with live status indicators (●).
- **📝 Live Logs:** Stream machine logs with full ANSI color support and mouse-drag selection.
- **⚡ Quick Controls:** Start, stop, and restart machines with lightning-fast keybindings.
- **🐚 SSH Integration:** Drop into an interactive SSH console instantly.
- **⚖️ Elastic Scaling:** Scale machine counts and VM sizes via intuitive modal dialogs.
- **🎯 Cursor Stability:** Intelligent data diffing ensures your selection never flickers during refreshes.
- **🗂 Multiple Accounts:** One view of every machine across several Fly accounts, orgs and apps, with a global filter, a started/stopped toggle and read-only accounts.
- **🔍 Inspect:** Read-only view of an app's deployed config, a machine's env, its secret names and digests, and a diff against its staging twin. Secret values are never shown.

## 📦 Installation

Install as a global tool using [uv](https://github.com/astral-sh/uv):

```bash
uv tool install fly-tui
```

Or install from PyPI:

```bash
pip install fly-tui
```

Or run from a clone:

```bash
git clone https://github.com/dvf/fly-tui && cd fly-tui
uv run ftui --mock
```

Requirements:

- Python 3.12+.
- [`flyctl`](https://fly.io/docs/flyctl/install/) (`fly`) on your `PATH` for the
  single-app view, for every action (start, stop, restart, scale, logs, SSH) and
  for the inspect panel's Config and Secrets tabs. The multi-account view reads
  orgs, apps and machines over HTTP, so it can list your fleet without flyctl
  (though the default `fly` token source asks flyctl for your login token).
  `--mock` needs neither flyctl nor a Fly account.

## 🛠 Running it

```bash
ftui                      # in a directory with fly.toml: that app (single-app view)
ftui                      # anywhere else: every app your fly login can see
ftui --all                # multi-account view of everything, ignoring ./fly.toml
ftui --app 'shop-*'       # multi-account view, filtered to matching apps
ftui --mock               # simulated data, no Fly account needed
```

Quit with `Ctrl+Q`.

## 🧭 The two views, and when each opens

**Single-app view.** The original `ftui`: the machines of the app in the current
directory's `fly.toml`, managed with your local `flyctl` login. It is unchanged.
It opens when all of these hold:

- there is a `fly.toml` in the current directory;
- there is no `~/.config/ftui/accounts.toml`;
- none of `--accounts`, `--app`, `--org`, `--account`, `--all` is given.

**Multi-account view.** A sidebar of accounts → orgs → apps next to one table of
every machine. It opens when any of these is true:

- `~/.config/ftui/accounts.toml` exists, or you pass `--accounts <file>`;
- there is no `fly.toml` in the current directory;
- you pass `--app`, `--org`, `--account` or `--all`.

`fly.toml` is never required. With no accounts config the multi-account view
uses your local flyctl login (one implicit account called `local`) and shows
every org and app that login can see. With an accounts config and a local
`fly.toml`, the view opens with that app as its starting filter
(`app:<name>`), which you can clear; `--all` skips that.

## 🚩 Command-line flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--mock` | off | Simulated data, no Fly account or flyctl needed ([Mock mode](#-mock-mode---mock)). |
| `--refresh <seconds>` | `5` | How often machines are re-fetched (both views). |
| `--accounts <file>` | `~/.config/ftui/accounts.toml` | Accounts file; opens the multi-account view. A missing file given here is an error. |
| `--app <glob>` | | Multi-account view, starting filtered to matching apps (`app:<glob>`). |
| `--org <glob>` | | Multi-account view, starting filtered to matching orgs (`org:<glob>`). |
| `--account <glob>` | | Multi-account view, starting filtered to matching accounts (`acct:<glob>`). |
| `--all` | off | Multi-account view of everything; ignores `./fly.toml`. |
| `--apps-refresh <seconds>` | `60` | Multi-account view: how often orgs and apps are re-listed. Never faster than `--refresh`. |
| `--help` | | Show the flags. |

`--app`, `--org` and `--account` combine, and only set the starting text of the
filter bar: `ftui --account acme --app 'shop-*'` opens with
`acct:acme app:shop-*`.

## ⌨️ Key bindings

Every screen also has Textual's `Ctrl+Q` (quit) and `Ctrl+P` (command palette),
and `Tab` / `Shift+Tab` to move focus.

### Single-app view

| Key | Action |
|-----|--------|
| `r` | Refresh now |
| `l` | Logs for the selected machine |
| `s` | Scale the app (count and/or VM size) |
| `h` | SSH console into the selected machine (`fly ssh console -s -m ID`) |
| `Ctrl+S` | Start the selected machine |
| `Ctrl+X` | Stop the selected machine |
| `Ctrl+R` | Restart the selected machine |
| `c` | Inspect: config, env, secrets, diff ([below](#-the-inspect-panel)) |
| `↑` / `↓` | Move the cursor |

In the single-app view, start, stop, restart and scale run straight away, as
they always have.

### Multi-account view

All the single-app keys, plus `/` and `f`:

| Key | Action |
|-----|--------|
| `r` | Refresh machines now |
| `/` | Focus the filter bar |
| `f` | Cycle all → started only → stopped only → all |
| `l` | Logs for the selected machine |
| `s` | Scale the selected machine's app (then confirm) |
| `h` | SSH console into the selected machine (`fly ssh console -a APP --machine ID`) |
| `Ctrl+S` | Start the selected machine (then confirm) |
| `Ctrl+X` | Stop the selected machine (then confirm) |
| `Ctrl+R` | Restart the selected machine (then confirm) |
| `c` | Inspect the selected machine's app |
| `Tab` / `Shift+Tab` | Move between the sidebar, the filter bar and the table |

In the filter bar, `Enter` or `Esc` returns to the table (the filter stays).

In the sidebar (Textual's tree keys):

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move |
| `Enter` (or click) | Show only this account, org or app in the table |
| `Space` | Expand / collapse the node |
| `Shift+Space` | Expand / collapse everything |
| `Shift+←` / `Shift+→` | Parent / parent's next sibling |
| `Shift+↑` / `Shift+↓` | Previous / next sibling |

### Log screen

| Key | Action |
|-----|--------|
| `q` | Back to the machine list (stops streaming) |
| `c` | Clear the log |
| `Ctrl+C` | Copy the whole log to the clipboard (`pbcopy`, macOS) |

Logs stream from `fly logs --instance ID` (single-app) or
`fly logs -a APP --machine ID` as the machine's account (multi-account).

### Scale dialog

| Key | Action |
|-----|--------|
| `Enter` | Apply (single-app) / continue to the confirmation (multi-account) |
| `Esc` | Cancel |

Leave a field empty to leave it unchanged: count only, VM size only, or both.

### Confirmation dialog (multi-account view)

| Key | Action |
|-----|--------|
| `y` | Confirm |
| `n` / `Esc` | Cancel |

### Inspect panel

| Key | Action |
|-----|--------|
| `1` / `2` / `3` / `4` | Config / Env / Secrets / Diff tab (or click, or arrows on the tab bar) |
| `a` | Pick the app to diff against |
| `Esc` / `q` | Close the panel |

### Diff app picker (`a` in the inspect panel)

| Key | Action |
|-----|--------|
| type | Filter the list of apps |
| `↑` / `↓` | Move through the list while typing |
| `Enter` (or click) | Diff against the highlighted app |
| `Esc` | Cancel |

## 🗂 The multi-account view

The **header** shows `shown/total machines`, the app and account counts, the
`f` mode when it isn't "all", and the refresh interval. The **subtitle** shows
the sidebar selection (`all`, or e.g. `acme / acme-prod / shop-api`).

The **sidebar** is a tree of account → org → app. Each node shows its machine
count and one coloured dot per machine state (or a count per state once there
are more than 8). Markers:

| Marker | Meaning |
|--------|---------|
| `✖ error` | The account or app failed to load. The rest still load. |
| `⚠` | The app runs fewer started machines than its `min_machines_running`. |
| `ro` | The account is `read_only`. |
| **bold** | The node has no machines. |

Select a node (`Enter` or click) to show just its machines. `All` shows
everything.

The **table** has one row per machine: Account, Org, App, Machine (id and
name), State, Checks (`passing/total`), Region, Size, Image and Updated. The
cursor stays on the same machine across refreshes.

## 🔎 Filtering

Press `/` and type free text and `key:value` tokens. Everything updates as you type.

| Token | Matches |
|-------|---------|
| `app:` | App name |
| `org:` | Org slug |
| `acct:` | Account name (`account:` also works) |
| `state:` | Machine state: `started`, `stopped`, `suspended`, ... |
| `region:` | Region code |
| free text | Anywhere in the row: account, org, app, machine id and name, state, region, size, image, process group |

Rules:

- Tokens match the whole value as a case-insensitive glob (`app:shop-*`, `region:?ra`).
- Free text is a case-insensitive substring; several words must all match.
- The same key twice means OR (`region:fra region:ams`); different keys mean AND.
- A leading `-` negates a token (`-state:started`, `-app:*-staging`).
- Quotes group words (`"shop api"`). A key with no value yet (`state:`) is ignored.

Examples:

```
state:stopped region:fra        stopped machines in Frankfurt
acct:acme -app:*-staging        acme's apps, without the staging ones
shop region:fra region:ams      "shop" anywhere, in fra or ams
org:globex state:started        started machines in the globex org
-state:started                  everything not running
```

### The filter is global

The filter and `f` narrow the sidebar as well as the table:

- The sidebar shows only the accounts, orgs and apps with at least one matching
  machine.
- Terms about names (`app:`, `org:`, `acct:`, and free text that matches an
  account, org or app name) also keep apps that match by name but have no
  machines, or failed to load, so `app:shop-cron` still finds an app with 0
  machines. Filters with `state:` or `region:` are about machines and keep only
  apps with matching machines; so does `f`.
- While filtering, counts become `matched/total` (e.g. `shop-api 2/3 ●●`, with
  a dot for each matching machine), and the header shows
  `shown/total machines, matched/total apps, matched/total accounts`.
- The sidebar selection is kept if it still matches. If it doesn't, the first
  matching node at the same level (app, org or account) is selected, preferring
  one with matching machines, or `All` if none. Your own selection comes back
  once it matches again.
- Clearing the filter (and `f` back to "all") restores everything.

### The `f` cycle

`f` steps through **all → started → stopped → all**, and combines with the
filter bar (AND). The header shows `[started]` or `[stopped]` while it is on.
Other states (`suspended`, `starting`, ...) show only in "all"; use
`state:suspended` for those.

## 🔐 Accounts config (`accounts.toml`)

`~/.config/ftui/accounts.toml` (or `--accounts <file>`) holds one `[[account]]`
table per account. Without it, the multi-account view uses one implicit account,
`local`, with your flyctl login.

```toml
[[account]]
name = "acme"                 # label shown in the UI
token = "fly"                 # the local flyctl login
orgs = ["acme-prod"]          # optional: only these orgs (default: all the token sees)

[[account]]
name = "globex"
token = "env:FLY_TOKEN_GLOBEX"
read_only = true              # no start/stop/restart/scale/ssh

[[account]]
name = "initech"
token = "op://Work/Fly Initech/token"

[[account]]
name = "personal"
token = "keychain:fly-personal"
exclude_apps = ["*-scratch"]
```

### Fields

| Field | Required | Default | Meaning |
|-------|----------|---------|---------|
| `name` | yes | | Label in the UI and for `acct:` filters. Must be unique. |
| `token` | no | `fly` | Where to read the token from ([below](#token-sources)). |
| `orgs` | no | every org | Org slugs to show. Also enables the per-org fallback when GraphQL is refused. |
| `read_only` | no | `false` | `true` refuses start, stop, restart, scale and SSH. Logs and inspect still work. |
| `apps` | no | every app | Shell-style patterns; only matching app names are shown. |
| `exclude_apps` | no | none | Shell-style patterns; matching app names are hidden. Applied after `apps`. |

`orgs`, `apps` and `exclude_apps` take a list or a single string. The file is
checked at startup: a missing `name`, a duplicate name, invalid TOML or no
`[[account]]` at all stops `ftui` with an error.

### Token sources

Tokens are never written in the file, only where to read them from. A raw token
in `token` is refused.

| `token` | Read from |
|---------|-----------|
| `fly` (default) | The local flyctl login (`fly auth token`) |
| `env:NAME` | The environment variable `NAME` |
| `op://vault/item/field` | 1Password CLI (`op read`) |
| `keychain:service` | macOS Keychain (`security find-generic-password -s service -w`) |

Each token is read once at startup, in parallel. If one can't be read (tool
missing, variable unset, empty result), that account shows `✖` with the error
and the others still load. Tokens stay in memory and are never shown in the UI,
errors or logs. Actions pass them to flyctl as `FLY_API_TOKEN`; the `fly`
source uses flyctl's own login instead.

### `apps` and `exclude_apps` patterns

Patterns are case-sensitive `fnmatch` globs: `*` (anything), `?` (one
character), `[abc]` (one of). An app is shown if it matches any `apps` pattern
(or `apps` is empty) and no `exclude_apps` pattern.

```toml
apps = ["shop-*", "billing"]          # only these
exclude_apps = ["*-scratch", "tmp-?"] # never these
```

They also let one org show as two groups in the sidebar. Here the `shop-*` apps
get their own group, and everything else in the org stays under `acme`:

```toml
[[account]]
name = "acme-shop"
orgs = ["acme"]
apps = ["shop-*"]

[[account]]
name = "acme"
orgs = ["acme"]
exclude_apps = ["shop-*"]
```

Both use the same token source, so the same token is read twice.

### `read_only`

A read-only account can be browsed, filtered, logged and inspected, but start,
stop, restart, scale and SSH are refused with a warning before any dialog opens.
The refusal is also enforced in the flyctl wrapper, so no mutating command can
run as a read-only account. Read-only accounts show `ro` in the sidebar.

## 🛡 Safety and read-only accounts

In the multi-account view:

- Start, stop, restart and scale ask for confirmation (`y` / `n`) in a dialog
  naming the account, org, app and (except for scale) machine. Scale shows what
  will change (`count -> 3, vm -> shared-cpu-2x`).
- Accounts with `read_only = true` refuse start, stop, restart, scale and SSH
  without opening a dialog. Logs and the inspect panel still work.
- Every flyctl command names its app (`-a APP`) and runs with the machine's own
  account token (`FLY_API_TOKEN`), so it can't land on the wrong app or
  account, whatever directory `ftui` was started in.

The single-app view behaves as it always has: actions run on the app in
`fly.toml` with your flyctl login, without confirmation.

## 🔍 The inspect panel

Press `c` on a machine, in either view, to open a read-only panel for its app.
It changes nothing, so it works on read-only accounts.

| Tab | Shows | Source |
|-----|-------|--------|
| 1 Config | The deployed app config, as TOML | `fly config show --toml -a APP` (older flyctl: JSON, converted) |
| 2 Env | The selected machine's `config.env`, sorted, with its process group | The machine data already fetched |
| 3 Secrets | Name, digest, status (Deployed / Staged / Partial), created | `fly secrets list --json -a APP` |
| 4 Diff | Env and secrets against a second app | The same, for both apps |

**Diff default sibling.** The Diff tab starts with the app's twin: the app whose
name adds or removes a `-staging` suffix. `shop-api` ↔ `shop-api-staging`;
`acme-prod` or `acme-production` → `acme-staging`; `acme-staging` → `acme`,
`acme-production` or `acme-prod`, the first that exists. An app in the same
account is preferred. With no twin, the tab says so; press `a` to pick any app
(in the multi-account view, any app in any account; in the single-app view, the
apps your login can see).

The diff lists:

- secrets with the same digest on both apps, labelled **same value on both**.
  Matching digests mean matching values, so this flags, for example,
  production running with a staging key;
- secret names that exist on one side only;
- secrets on both sides with different digests (*different value*);
- env keys whose values differ, or that exist on one side only.

Env keys with the same value on both are counted in the summary, not listed.
The other app's env comes from one of its machines, preferring the same
process group.

**Secret values are never shown.** Only names, digests and status are read;
any other field in flyctl's output is dropped before it reaches the UI. There
is no `printenv`, SSH or secret-value path.

**flyctl dependency.** Config and Secrets (and so the secrets half of Diff) run
`flyctl`, as the app's account in the multi-account view. Env works without it.
In the single-app view, the other app's env for Diff comes from
`fly machines list --json`. Each tab fails on its own: a missing flyctl, no
permission for secrets, or an unknown app shows an error in that tab while the
others still load.

## 🧪 Mock mode (`--mock`)

`--mock` runs on simulated data, offline, with no Fly account or flyctl.

- **In a directory with a `fly.toml`** (and no accounts config or multi-account
  flags): the single-app view on a simulated app, `mock-app-production`, with
  two machines and a `mock-app-staging` twin for Diff.
- **Anywhere else, or with `--all` / `--app` / `--org` / `--account`**: the
  multi-account view on a simulated fleet of 3 accounts (`acme`, `globex`,
  `initech`), 4 orgs, 7 apps and 10 machines. `globex` is read-only, one app
  has no machines and always fails to load (to show `✖`), two apps are below
  `min_machines_running` (to show `⚠`), and one app's secrets are "not
  authorized" (to show a tab error). Start, stop and restart change the
  simulated fleet; SSH is disabled. The accounts file is not read.

The mock sits at the HTTP transport, so mock mode runs the same fetching,
parsing and error handling as the real thing. Logs print simulated lines. The
inspect panel has fake config, env and secrets (with shared digests, to show
"same value on both").

```bash
ftui --mock --all
```

## ⚡ Performance

- Orgs and apps come from one GraphQL query per account (`organizations { apps }`),
  re-run every `--apps-refresh` seconds. flyctl would need `fly orgs list` plus
  one `fly apps list` per org, each a subprocess. If GraphQL is refused (e.g. an
  org-scoped token) and the account lists `orgs`, it falls back to the Machines
  API per org.
- Machines come from the Machines API (`GET /v1/apps/{app}/machines`), fetched
  in parallel 8 at a time, every `--refresh` seconds. Each app has its own
  10-second timeout, so a slow or broken app marks only itself.
- Accounts load in parallel and independently; one bad token or account never
  blocks the rest.
- The screen shows at once and fills in as data arrives. Refreshes update
  table cells in place when the rows haven't changed, so nothing flickers, and
  the sidebar is only rebuilt when its shape changes.
- Filtering is local: typing in the filter bar, `f` and sidebar selection never
  call the API.

## 🚧 Limitations

- The 1Password source is tested only with a mocked `op`, not against a real vault.
- The Keychain source is macOS-only; log copying uses `pbcopy` (macOS).
- Health checks are often empty in the Machines API response, so the Checks
  column is frequently blank.
- The GraphQL query lists at most 500 apps per org.
- The `f` cycle covers started and stopped only; use `state:` for other states.
- Actions, logs, SSH and inspect's Config and Secrets tabs need flyctl.
- Env diffs compare one machine per app (the same process group where
  possible), not every machine.
- Scale in the multi-account view applies to the whole app, like `fly scale`.

## ✅ Running the tests

```bash
uv sync
uv run pytest -q
```

The suite covers the config parser, the filter, the fleet data layer against a
mock HTTP transport, the inspector, and Textual pilot tests of both views, the
global filter and the inspect panel. It needs no Fly account, flyctl or network.

## 🤝 Contributing

Contributions are welcome! This project is built for the community. Feel free to open issues or PRs on [GitHub](https://github.com/dvf/fly-tui).

## 📄 License

MIT © [Fly.io Community](LICENSE)
