# Altus

A terminal coding and DevOps harness with bring-your-own-key support for eight
LLM providers — and, ahead of it, a workflow designer that turns the harness
into a software factory.

> **Status: Phase 2 complete.** Streaming chat across all eight providers;
> filesystem tools behind a diff-first approval gate; and four clouds ---
> **Kubernetes**, **AWS**, **Azure** and **Google Cloud** --- with reads that
> draw you a picture and changes gated on whatever preview that cloud actually
> offers. Next is the workflow designer.

## Install

Requires Python 3.14+, or [uv](https://docs.astral.sh/uv/), which fetches an
interpreter for you.

**Altus is not on PyPI**, so `uv tool install altus` will not work. Clone it:

```bash
git clone https://github.com/shubhambakshi374/altus
cd altus
uv sync
uv run altus              # the TUI
uv run altus tools list   # anything else
```

To get an `altus` command on your PATH instead of typing `uv run`:

```bash
uv tool install .                 # from a clone
uv tool install --editable .      # ...or track your edits live
uvx --from . altus --version        # ...or run it once, installing nothing
```

That installs a snapshot, so after changing the code either re-run it with
`--force` or use `--editable` from the start. `uv tool uninstall altus` removes
it. Installing straight from the remote works too, without cloning:

```bash
uv tool install git+https://github.com/shubhambakshi374/altus
```

## Quick start

Just start it. Altus runs with nothing configured and walks you through setup on
first launch:

```bash
altus                              # launch the TUI
```

The wizard asks which provider, takes your API key, **checks it against the
provider** before accepting it, and then lets you pick a default model from
what that key can actually reach. `/setup` reopens it any time, and `escape`
skips it --- the rest of Altus still works.

Prefer the shell?

```bash
altus config set-key anthropic     # stored in the OS keyring, never on disk
altus config doctor                # which providers can authenticate
```

Headless, for scripts and CI:

```bash
altus chat --once "explain this failing rollout" --model claude-sonnet-5
```

## The workspace

Every session has a **workspace**: a rooted filesystem context the model can
read. It defaults to the current directory.

```bash
altus tools list                                   # what the model can call, and where
altus chat --once "what does the retry logic do?"  # the model reads the repo to answer
altus --allow-path /etc/nginx                      # add a root
altus --no-tools                                   # plain chat
```

The workspace is also the Phase 2 primitive: a flow will construct one and
hand the same instance to every step, which is why it lives in
`altus/workspace.py` rather than inside the tools.

| Tool | What it does | |
|---|---|---|
| `read_file` | Line-numbered read with `offset`/`limit`. Refuses binaries. | read-only |
| `list_dir` | Directory contents, directories first. | read-only |
| `glob` | Find files by pattern, newest first. | read-only |
| `grep` | Regex content search. Uses `rg` when installed, else pure Python. | read-only |
| `write_file` | Create a file, or replace one wholesale. | **asks first** |
| `edit_file` | Replace an exact string. Refuses an ambiguous match. | **asks first** |
| `delete_path` | Delete a file, or a directory with `recursive`. | **asks first** |

`.gitignore` is honoured and `.git` is always skipped, so the model sees your
source rather than `node_modules`.

### Changes always ask first

Nothing is written before you say yes. Each change opens a prompt showing the
**actual unified diff** --- approving a change you cannot see is not consent ---
plus whether git could get the file back:

```
EDIT — approval required
src/altus/core/retry.py
tracked by git and unmodified — recoverable with git checkout

  @@ -12,7 +12,7 @@
  -    base: float = 0.5,
  +    base: float = 1.0,

              [ Reject (n) ]  [ Always allow edit_file (a) ]  [ Approve (y) ]
```

Reject is focused by default, so Enter takes the safe option. "Always allow"
is scoped to **one tool**, lives in memory for **one session**, is never
written to disk, and is shown in the status bar the whole time it is active.

`edit_file` requires `old_string` to match exactly once, so an ambiguous edit
is refused rather than guessed at. `write_file` is for new files and full
rewrites; the model is told to prefer `edit_file`, which keeps diffs small and
reviewable.

Headless runs cannot prompt, so `altus chat --once` **refuses changes** unless
you pass `--yes`:

```bash
altus chat --once "bump the version" --yes
```

Writes additionally refuse anything inside `.git`, and the same secret
denylist applies --- so the model cannot create a `.env` either. Files are
written atomically (temp file, then rename), so an interrupted write leaves
the original intact.

### What it will not read

Two rules, both enforced in `Workspace.resolve` before anything touches disk:

1. **Nothing outside the workspace.** Symlinks are resolved *before* the
   containment check, so a link inside the root pointing at `~/.ssh` does not
   escape it.
2. **No credential files, even inside the workspace** --- `.env*`, `*.pem`,
   `*.key`, `id_rsa*`, `.netrc`, `.npmrc`, `.aws/credentials` and similar.

The second rule exists because Altus ships file contents to external model
providers by design. "Model reads `.env`, quotes it back, key lands in a
provider's logs" is the most plausible way this tool leaks a credential.
Refusals are explicit, so the model reports them instead of retrying. Set
`deny_secrets = false` under `[workspace]` if you genuinely need it off.

```toml
[workspace]
extra_roots = ["/etc/nginx"]

[tools]
enabled = true
max_iterations = 25
max_file_bytes = 262144
git = true

[tools.shell]
# Empty by default, and an empty list means there is no shell tool at all ---
# not a shell tool that refuses. A binary missing from this list is refused
# outright rather than asked about; the gate underneath asks what *this*
# invocation will do, not whether you meant to allow curl.
allow = ["make", "pytest", "uv", "npm"]
timeout = 120
```

## Slash commands

Anything starting with `/` is a command, not a prompt. **Type `/` and the list
appears** --- filtered as you type, with `↑↓` to choose, `tab` to complete and
`esc` to dismiss. Once you are past the command name it becomes a usage hint
for the command you are writing.

| | |
|---|---|
| `/help` | List commands |
| `/setup [<provider>]` | Add a provider: key, health check, default model |
| `/provider` · `/providers` | Interactive picker. Unconfigured providers open setup |
| `/provider use <name>` | Switch directly |
| `/key <provider>` · `/key rm <provider>` | Store a key (masked, straight to the OS keyring) |
| `/model` · `/models` | Searchable picker — unlisted ids are looked up live |
| `/model <id>` | Set one directly |
| `/login` · `/login <cloud>` | Cloud auth status, or sign in |
| `/kube` · `/kube use <ctx>` · `/kube add <path>` | Kubernetes contexts |
| `/aws` · `/aws region <name>` · `/aws profile <name>` | AWS identity, account and region |
| `/azure` · `/azure sub <id>` | Azure tenant, subscription and identity |
| `/gcp` · `/gcp project <id>` | GCP account, project and identity |
| `/mcp` · `/mcp check` | MCP servers, what each covers, and drift against the manifest |
| `/workflow` · `/workflow <name>` | Open the workflow designer |
| `/workflow new <what it should do>` | Describe one; the model drafts it, you approve the file |
| `/workflow templates` · `/workflow new --from <t> [<name>]` | Start from one Altus ships |
| `/workflow list` · `show` · `validate` · `path <name>` | The same workflows from the keyboard |
| `/workflow run <name> [k=v ...]` | Run one, step by step, on a screen |
| `/workflow runs` · `/workflow runs <id>` | What has been run, and what happened |
| `/dashboard [aws \| azure \| gcp \| k8s] [<scope>]` | Several read-only views on one screen |
| `/graphics [auto \| image \| cells \| off]` | How visuals are drawn, and why |
| `/tools` | Tools, installed integrations, standing approvals |
| `/new` | Start a fresh session |

`altus login` and `altus kube list|use|add` do the same from the shell.

## AWS

431 services and 19,189 operations, so there is no tool per operation. There is
one generic call, and the SDK's own service models are what make it usable:

```
> what's running in eu-west-1, and what is it costing us
```

`aws_inventory` for EC2, RDS and Lambda in one table; `aws_topology` for the
VPC tree with security groups drawn across it; `aws_cost` for spend by service
over time. `aws_explain` hands the model an operation's exact parameter
contract, which is why it reaches for the SDK rather than guessing CLI flags.

**Reads run freely. Everything else asks.** And the prompt is honest about what
it was able to check, which is where AWS differs from Kubernetes:

| | |
|---|---|
| EC2 | Dry-run against AWS — 819 operations support it |
| Everything else | An IAM permission check, plus the resource's current state |

AWS offers no preview for 95.7% of its operations, so the prompt says *"not
dry-run: AWS cannot preview this operation"* rather than implying a check that
never happened.

Operations are classified on four levels, and 6.2% are **privileged** —
rewriting IAM, destroying something that holds data, opening a resource to the
network. Those demand the target typed out and can never be granted standing
approval. Ordinary changes take a keypress; a challenge that fires on
everything just trains you to type through it.

```toml
[cloud.aws]
allow_writes        = true
allow_iam_writes    = true   # checked at the gate, not by withholding a tool
allow_delete        = true
allow_cost_explorer = true   # Cost Explorer bills per request
```

`aws_cost` costs money — roughly $0.01 a call — so it says so in its own
description, and it is left off the dashboard, which refreshes on a keypress.

## Azure

Azure Resource Manager is already a generic API — every management operation is
an HTTP verb on a resource path — so Altus talks to it directly rather than
through two hundred `azure-mgmt-*` packages:

```
> what's in this subscription, what does it cost, and what's exposed to the internet
```

`azure_inventory` and `azure_topology` are each a single Resource Graph query
rather than a walk of every service; `azure_query` hands you raw KQL across the
whole subscription. `azure_cost` charts spend by service — Cost Management is
**free**, unlike AWS Cost Explorer, so it sits on the dashboard. `azure_quotas`
plots real current usage against each ceiling, which the AWS version could not.

`api-version` is never guessed. It is mandatory, differs per resource type, and
`azure_explain` resolves it for you along with every RBAC operation the type
defines — a call pinned to a wrong version fails in a way that looks like the
resource is gone.

**Reads run freely. Everything else asks**, and the prompt names which check
actually ran:

| | |
|---|---|
| `azure_write` | **What-If** — a real server-side, property-level diff |
| `azure_delete` | No preview exists. A resource-lock check, plus RBAC |
| `azure_action` | No preview exists. RBAC |

What-If is the closest any cloud gets to `kubectl diff`, so an Azure write is
gated more like a Kubernetes one than an AWS one. A `CanNotDelete` lock refuses
a delete outright, before you are asked — AWS has no equivalent check at all.

Classification parses rather than guesses: Azure states its verb in a closed
set of four, and `action` is the interesting one because it covers both `start`
and `listKeys`. Scope counts too — the same delete removes one diagnostic
setting at a resource and every one of them a subscription up.

```toml
[cloud.azure]
allow_writes      = true
allow_rbac_writes = true   # roles, policy, and locks — checked at the gate
allow_delete      = true
allow_cli         = true   # the `az` fallback
```

One credential commonly sees many subscriptions. Altus acts in exactly one, so
the target named in a prompt is the one that gets touched — switch it with
`/azure sub <id>`, and widen a Resource Graph query explicitly when you mean to.

## Google Cloud

Google ships its API contracts **on disk** — 600 discovery documents inside the
client library, covering 335 APIs — so Altus resolves a method's exact
parameters, its HTTP verb and whether it supports a dry run without a single
network call:

```
> what's running in this project, what can reach it, and what are we near the limit on
```

`gcp_assets` searches every resource at once through Cloud Asset Inventory;
`gcp_inventory` and `gcp_topology` build on it and fall back to listing services
one by one when that API is off — saying which, because the two do not see the
same things. `gcp_quotas` plots real usage against each ceiling.

Classification **parses** rather than guesses. Each method's document states its
HTTP verb, and of 10,987 GET methods exactly five have a write-shaped name —
listed by hand, because five is small enough to be exact. `setIamPolicy` leads
the precedence on every service: it is how a bucket becomes world-readable and
how anyone grants themselves owner.

**Reads run freely. Everything else asks** — and here the prompt has the least
to offer of the four clouds, so it says so:

| | |
|---|---|
| `validateOnly` | Only 1.9% of methods support it — measured, not estimated |
| deletion protection | Read from the resource. A refusal, not a warning |
| liens | Block deleting a project. Also a refusal |
| `testIamPermissions` | "May I" — the most available of the four preflights |

A method with no dry run produces *"no preview exists: this method cannot be
validated without running it, and 98% of Google's methods cannot."* Never a
claim that something was checked when it was not.

```toml
[cloud.gcp]
allow_writes         = true
allow_iam_writes     = true   # setIamPolicy, service-account keys, KMS
allow_delete         = true
allow_cli            = true   # the `gcloud` fallback
billing_export_table = ""     # see below
```

**GCP has no cost API.** Cloud Billing exposes account metadata and SKU pricing
and not a cent of actual spend — that lives only in a BigQuery export you
configure yourself. Point `billing_export_table` at it and `gcp_cost` charts it;
leave it empty and the tool says exactly that and lists your budgets instead. It
never returns a number it did not get.

## MCP — the rest of the toolchain

The four clouds cover infrastructure. The systems around it — the ticket that
explains a deploy, the dashboard that showed it failing, the warehouse the data
landed in — have proprietary APIs with no corpus to sweep and vendor-maintained
MCP servers already written. Altus ships nine of them kitted out, and a door for
anything else. You bring credentials, not config files.

| Server | Covers |
|---|---|
| `github` | Repositories, issues, pull requests, Actions, code scanning |
| `atlassian` | Jira, Confluence, **Bitbucket Cloud**, JSM, Compass — one endpoint |
| `grafana` | Dashboards, Prometheus, Loki, Pyroscope, incidents, on-call |
| `datadog` | Metrics, logs, traces, monitors, incidents, security signals |
| `newrelic` | Entities, NRQL, alerts, errors, deployment impact |
| `crowdstrike` | Detections, vulnerabilities, hosts, threat intel, containers, RTR |
| `servicenow` | Incidents, changes, problems, the CMDB, knowledge |
| `snowflake` | Cortex Analyst and Search, and whatever SQL the server object allows |
| `databricks` | Genie spaces, Vector Search indexes, Unity Catalog functions |

Bitbucket is not a separate entry because it is not a separate server:
Atlassian's hosted server carries it alongside Jira. That server is **Cloud
only** — Jira Data Center cannot connect to it at all.

**Four tools, however many servers connect.** Those nine publish well over six
hundred tools between them, which is far more schema than everything else Altus
registers put together, so none of it sits in the prompt:

| | |
|---|---|
| `mcp_servers` | What is reachable, what each covers, and where the manifest has drifted |
| `mcp_tools` | Search the live inventory. The only way the model learns a tool exists |
| `mcp_call` | Reads only — refuses anything else before contacting the server |
| `mcp_do` | Everything that changes something, through the gate |

### This is the one surface that cannot be measured offline

botocore, the ARM provider manifests and the 600 GCP discovery documents all
ship on disk, so those classifiers read a corpus. An MCP server's tool list
lives behind an authenticated connection to a product that ships on its own
schedule. Altus classifies against a **manifest** instead — 642 tool names as
shipped — and it says where each one came from, because the three sources are
not equally good:

| `source` | Meaning | Servers |
|---|---|---|
| `derived` | Generated from an upstream machine-readable artifact at a pinned ref | github, crowdstrike |
| `documented` | Parsed from the vendor's published tool table | grafana, datadog, newrelic |
| `curated` | Written by hand, or structurally impossible to list | atlassian, snowflake, databricks, servicenow |

GitHub is `derived` because its server checks a JSON snapshot of every tool into
its own repository — annotations, descriptions and input schemas, generated from
the source by its own test suite. `scripts/refresh_manifests.py` reads that at a
pinned release and rewrites the manifest; a scheduled CI job re-runs it and
reports drift. The hand-written version of that file, built from the published
README, was **40 tools short of 122** — one GitHub tool in three demanded a typed
challenge to file an issue. That is the whole argument for deriving it.

The generator decides only whether a tool reads or writes. Anything that needed
judgement lives in an `[overrides]` table it never touches, and a refresh fails
rather than dropping an override whose tool the vendor renamed.

Two rules make the remaining drift loud rather than silent:

**A tool absent from the manifest is privileged.** A name Altus has never seen
is, almost by definition, a vendor release. `/mcp check` lists them.

**A server's own annotations may raise a tool's sensitivity and never lower
it.** MCP lets a server declare `readOnlyHint` and `destructiveHint`, and the
specification says a client must not rely on those from a server it does not
trust. Datadog is the worked example: it labels `execute_code` and
`datadog_remote_action_restricted_shell_run_command` read-only, on the correct
grounds that both respect the caller's permissions. Both also take code from
the model and run it somewhere else. Altus calls them privileged.

Two servers let the customer name their own tools, so a name table is
impossible. Databricks is classified from the **endpoint** instead — a
`vector-search/` URL exposes one index query per index and can do nothing else,
while a `functions/` URL runs arbitrary UDFs. Snowflake's names fail closed, and
`/mcp` says why rather than pretending the list is complete.

### The preview, across all five surfaces

| | Preview |
|---|---|
| Kubernetes | `dryRun=All` on every mutation — the server's own verdict |
| Azure | What-If: a real property-level diff |
| AWS | EC2 `DryRun`, 4.3% of operations |
| GCP | `validateOnly`, 1.9% of methods |
| **MCP** | **None. No such mechanism exists in the protocol** |

So every `mcp_do` prompt contains the words *"no preview exists"*. Where the
manifest names a read that fetches current state, the prompt shows that as the
"before"; where it does not, it says the server declared nothing. Nothing else
is claimed.

```toml
[mcp]
enabled      = true
servers      = []     # empty = whichever have credentials
allow_writes = true   # also passes the servers' own read-only switches
max_rows     = 200    # query results are capped and redacted

[mcp.databricks]
url   = "https://acme.cloud.databricks.com/api/2.0/mcp/{scope}"
scope = "genie/01ef"
```

Six of the nine are hosted; Grafana and CrowdStrike run as local subprocesses,
and Altus never installs either. Credentials come from the environment first and the OS
keyring second, never from `config.toml`; OAuth tokens go to the keyring too. A
stdio server is handed `PATH` and its own credentials and nothing else —
`os.environ` would give a third-party binary every other credential on the
machine.

**Listing a server's tools needs working credentials.** Every hosted endpoint
tested rejects the request before `initialize`, so there is no way to enumerate
one — or to diff it against the manifest — without an account.

**Snowflake and Databricks answer questions with rows**, and every row reaches
your model provider. Results are redacted and capped at `max_rows` on the way,
and `mcp_servers` says so out loud rather than burying it here.

### And the rest

A curated catalogue can never be finished — Darktrace has no official MCP
server today — and the alternative to a door is that somebody forks Altus to
add one. So there is a door, and it does not pretend:

```toml
[mcp.custom.darktrace]
url     = "https://mcp.internal/darktrace"
env     = ["DARKTRACE_TOKEN"]
summary = "Darktrace threat detection"
```

```
  darktrace   ready  (custom, unclassified)
    Darktrace threat detection
    no manifest --- this server is configured under [mcp.custom] and Altus
    ships no manifest for it, so nothing is known about what its tools do
    every tool needs a typed confirmation unless the server says otherwise
```

A custom server carries an empty manifest and the strictest fail-closed setting
there is. Its own annotations still apply **as a ceiling**, so a tool it
declares read-only classifies as a read — a custom server can lower its tools'
sensitivity and can never raise its own trust.

That is a real cost, and it is the point: the nine shipped servers were
classified by a human against a source, and a tenth that nobody classified
should not be able to sit in the list looking the same.

## Workflows

A workflow is several steps, in an order, with one blast radius --- the
composable half of the software factory. `/workflow` opens the designer;
`/workflow new "deploy the API to staging"` describes one in words and lets the
model draft it.

`/workflow run` runs one, a step at a time, on a screen that shows each one
resolving.

Three kinds of step, because three is what Altus can do today:

| | |
|---|---|
| `tool` | Calls one registered tool with arguments |
| `agent` | Gives the model a prompt, and optionally a narrowed tool set |
| `approval` | Stops, so a human decides whether the rest runs |

Steps depend on each other through `needs`, not through their order in the
file, because the engine will want a DAG and retrofitting dependency edges
later would mean rewriting every workflow anyone had written.

```toml
name = "deploy-api"
description = "ship the API to staging"

[[steps]]
id = "inventory"
kind = "tool"
tool = "k8s_topology"
args = { namespace = "staging" }

[[steps]]
id = "plan"
kind = "agent"
needs = ["inventory"]
prompt = "which deployments changed since the last release?"
tools = ["k8s_get", "k8s_events"]

[[steps]]
id = "approve"
kind = "approval"
needs = ["plan"]
message = "ship to staging?"
```

One file each, under `<config>/workflows`, meant to be diffed and committed
next to the code it operates on.

### Three to start from

```
/workflow templates
  jira-bug             11 steps   Take a Jira bug through to a merged pull request and transition it
  servicenow-change     8 steps   Raise a change, deploy behind its approval, and close it with the outcome
  vuln-fix             13 steps   Check for vulnerabilities, fix one, raise a PR, and see it merged

/workflow new --from vuln-fix api-vulns
```

Copied as text, comments and all — the prose explaining *why* a step is there
is the part a reader needs most, and a round trip through the parser would drop
every line of it. Each one is checked against a real tool registry in CI: a
template that does not validate is worse than no template, because it teaches
the format wrong and fails at the moment somebody trusted it.

`vuln-fix` asks **two** vulnerability sources, and the split is the honest
answer to a join that does not exist. GitHub's own scanning answers *what is
wrong in this repository* — it knows the code and the dependency graph and
needs no mapping. CrowdStrike answers *what is exploitable in what we are
running*, which is a different question about a different artifact: Falcon's
data is host- and image-centric and contains no repository identifier anywhere.
A workflow claiming to find "the repo's vulnerabilities in CrowdStrike" would
be inventing that join.

### Workflows take inputs

```toml
[inputs.repo]
description = "owner/name"
default     = "@git.origin"   # this checkout's own remote

[inputs.image]
description = "container image to check in Falcon"
required    = true
```

`/workflow run vuln-fix image=acme/api:1.2`, and anything required and missing
is asked for **before the run gate** — an approval prompt showing
`${inputs.repo}` where the target should be is approving nothing. An input
nobody declared is refused rather than ignored, because a misspelt `repo=` that
quietly does nothing runs against whatever the default was.

`@git.origin` is the only dynamic default there is. Each one is something that
can resolve differently on two machines, which is exactly what stops a workflow
file being portable.

### Steps that wait

"Check CI has passed" and "check the PR is merged" are steps that poll.

```toml
[steps.wait]
until    = "conclusion"   # this key must appear, non-empty, in the output
interval = 30
timeout  = 1800
```

Two forms and no operators: `until` names a JSON key that must appear with a
non-empty value, or `contains` is a literal substring. The moment it grows `!=`
it is an expression language, and a workflow whose shape depends on run-time
values can no longer have its blast radius worked out before it runs. Anything
needing a *decision* is an agent step, where the gate is already watching.

**Only a read may wait**, refused by the validator rather than warned about:
waiting means calling the same thing repeatedly, and nothing about "check until
it is done" implies anybody wanted a mutation repeated. **A wait is not a
retry** either — a step that failed did not produce an answer the condition
could be true of.

### Steps pass text, and only to steps they depend on

`${inventory}` anywhere in a later step's arguments, prompt or message is
replaced with what the step called `inventory` produced. That is the whole
mechanism — one substitution, no expressions.

An expression language was the alternative and it is the wrong trade. Every one
of them grows conditionals, and a workflow whose *shape* depends on values
computed at run time cannot have its blast radius worked out before it runs —
which is the property this whole design is built on. Anything cleverer is an
`agent` step's job, where the gate is already watching.

**`${x}` is fatal unless `x` is in the transitive closure of `needs`.** Order in
the file guarantees nothing, because the engine runs the graph; a reference to a
merely-earlier step would work right up until somebody reordered two
independent steps.

### What happens when step 3 of 6 fails

The run stops, and everything after it is **skipped by name** — a step that
never ran did not fail, and a trail that cannot say what did *not* happen is not
a trail.

```
  ✓ listing            list_dir                     1 entries in .  0.01s
  ✓ readit             read_file                    read 1 line of notes.md  0.0s
  ✓ copy               write_file                   created copy.md  0.0s
  ✗ boom               read_file                    not found  0.0s
  ~ never              list_dir                     the run stopped at boom

Stopped by a failure — 4 steps ran, 1 skipped, 0.02s
  record: ~/.local/share/altus/runs/r_1a0a915324eb.jsonl
```

`on_error = "continue"` opts out per step, for the ones the rest genuinely does
not depend on. A step that *needed* the failed one is still skipped: `continue`
means the run survives, not that the failure did not matter.

### Two gates, not one

Every mutation inside a run still asks for itself, exactly as it does in chat —
the engine calls the same tools through the same registry. What running adds is
**one question before anything starts**, because agreeing to six actions
individually, in sequence, with no sight of the whole is how consent gets worn
down. That question goes through the ordinary approval machinery, so a workflow
whose blast radius is privileged demands the typed challenge without the run
screen knowing the rule. Declining at either level stops the run.

### One at a time, unless the workflow says otherwise

Steps run in dependency order, ties broken by the file's order, and one at a
time by default — a sequential run is the one whose record reads like what
happened. `parallel = 3` at the top of the file lets a *wave* — the steps with
no path between them — run together:

```toml
name = "sweep"
parallel = 3
```

Opt-in rather than automatic, because the author is the one who knows whether
two steps with no dependency edge are really independent: `needs` does not say
that both of them write the same file. Every workflow written before this
existed keeps its meaning exactly.

Two things do not change when steps run together. **Approval prompts stay
strictly one at a time** — concurrency here is for waiting on somebody else's
API, never for asking a person two questions at once. And a stopping failure
lets what is already in flight finish, then skips everything that had not
begun, by name: killing a sibling mid-mutation to honour "the run stops here"
is worse than letting it land. The record says which steps shared a wave, so
"these happened in this order" and "these happened at the same time" stay
different facts.

### Every run leaves a record

One JSONL file per run under `<data>/runs`, written as it happens rather than
at the end — the runs you most want a record of are the ones that were
interrupted, and a record assembled at the end is exactly the record those runs
never get. `/workflow runs` lists them; `/workflow runs <id>` reads one back.

A run with no closing line reads as `interrupted`, never as completed.

### Running one without the TUI

`altus workflow` is the same engine with nothing rendering it, which is what a
cron entry, a CI job and the trigger supervisor all need:

```
altus workflow list | show <name> | validate <name>
altus workflow run <name> [key=value ...] [--unattended | --yes]
altus workflow runs [--parked] · altus workflow resume <run id>
altus workflow serve
```

Exit codes are the interface, because the caller is usually a script: **0**
completed, **1** a step failed, **2** refused — invalid or declined — and **3**
**parked**. The last one is the point: "somebody has to look at this" and "this
is broken" call for different things, and a CI job should be able to tell them
apart without reading the log.

### Nobody there? The run parks

The whole safety model here is a human at a gate, and an unattended run is by
definition one with nobody at it. So it does not guess. `--unattended` installs
a policy that *raises* rather than answering: the step stops, the record keeps
the question the gate would have asked, and the run ends `parked`.

```
running vuln-fix: 6 steps, mutate
  ✓ alerts             12 open advisories
  ✓ plan               answered
  ⏸ branch             needs approval to switch to fix/CVE-2026-1
parked: 2 ran, 0 skipped — branch: needs approval to switch to
```

Parked is not denied. A denial is an answer somebody gave; this is nobody
having been asked, and a record that conflates the two records a decision that
was never made.

`altus workflow runs --parked` lists what is waiting, and `altus workflow
resume <id>` — or `/workflow resume <id>` in the TUI — picks it up on the same
screen, through the same gates. Three things make that safe rather than
convenient:

- **The steps that already finished are replayed from the record, never re-run.**
  A resume that re-ran a commit to get back to where it stopped would be the
  safety mechanism causing the damage.
- **The workflow is fingerprinted.** Edit the file and the resume is refused:
  the steps already done were planned from a different file, and finishing the
  new one would be approving something nobody looked at.
- **The inputs are replayed too**, so `@git.origin` resolved in one checkout
  does not quietly re-resolve in whatever checkout happens to be current when
  somebody gets round to approving.

### Something other than a person can start one

```toml
[[triggers]]
kind  = "schedule"
every = "1d"

[[triggers]]
kind  = "watch"
every = "15m"
tool  = "mcp_call"
args  = { server = "crowdstrike", tool = "falcon_search_detections" }
into  = "found"     # the answer arrives as ${inputs.found}
```

Two kinds, declared in the workflow file so the thing that starts a run and the
run it starts are one artefact you can read in one sitting. A `schedule` fires
on an interval; a `watch` polls a read and fires when the answer **changes**,
handing that answer to the run so it can act on what changed rather than going
to look again. The tool a watch polls must be a read — it is called forever,
and nothing about "tell me when this changes" implies anybody wanted a write
repeated.

`altus workflow serve` watches them. It is a foreground supervisor, not a
daemon: nothing is installed into launchd or systemd, so the decision about
what runs unattended on this machine stays yours to make explicitly. Runs it
starts are unattended by construction, which means they park.

**First sight is not an event.** A watch that has never looked records what it
sees and fires nothing; a schedule that has never fired records the time and
waits out its interval. Otherwise restarting the supervisor — after a deploy,
after a crash — would be a way to run everything at once, turning an
operational hiccup into a fleet of unattended runs. The state is on disk for
the same reason.

There is deliberately **no webhook kind**. A listening socket is a separate
surface with its own authentication story, and adding one here would smuggle it
in behind a workflow feature.

### The blast radius is a floor, and says when it is one

A workflow's level is the strictest of its steps. This is the first place in
Altus that rule applies to an action **nobody has taken yet**, so three things
stop a printed level buying false confidence:

- `aws_write` reaches 19,189 operations and the same `k8s_apply` writes a
  ConfigMap and a ClusterRoleBinding --- for those the arguments decide, and
  anything computed now is a floor;
- an `agent` step with no `tools` listed may reach any tool in the session, so
  it is by construction as dangerous as the worst one. Naming tools narrows it,
  and the designer says so until you do;
- a step naming a tool this machine lacks has no level at all, and is reported
  separately rather than failing closed to privileged --- crying wolf over a
  typo is how a warning stops being read.

Any of those and the radius renders `(at least)`, with the step ids behind it.

```
deploy-api  —  ship the API to staging
   1  inventory        tool      k8s_topology                             read
   2  plan             agent     which deployments changed since the las… read          after inventory
   3  approve          approval  ship to staging?                         read          after plan
   4  apply            tool      k8s_apply                                mutate        after approve

blast radius: mutate (at least)
  steps plan, apply dispatch — what they actually do is decided by their
  arguments, so this could be stricter when they run
```

### Letting the model write one

`workflow_save` is the only tool in Altus that writes outside the workspace,
and the only one whose *contents* are the dangerous part. So the approval
prompt carries the whole rendered file, and the request takes the workflow's
own rolled-up level as its sensitivity: a drafted workflow containing a
privileged step demands the typed challenge, exactly as calling that step
directly would. Otherwise the file would be a route around the gate --- queue
the privileged call today, approve a harmless-looking save, run it later.

It is registered only while you are drafting, and takes itself back out once a
workflow is saved.

```toml
[workflow]
enabled               = true
dir                   = ""     # empty = <config>/workflows
allow_model_authoring = true   # false: workflows come only from humans
```

An unrunnable draft is refused rather than saved with warnings, and you are
not asked about it --- the model can fix a dangling `needs` without you.

## Kubernetes

Read-only in this release. Ask a question, get a chart:

```
> what is running in the qam namespace and how is it sized?

◆ Deployment/api  3/3 ready
└─ ◇ ReplicaSet/api-6f4
   ├─ ● Pod/api-6f4-2xk  Running 1/1
   │  ├─ · → mounts PersistentVolumeClaim/data
   │  └─ · ← selects by Service/api
   └─ ● Pod/api-6f4-9dm  CrashLoopBackOff
◈ Service/api  ClusterIP
├─ · → selects 2 Pods
└─ · ← routes-to by Ingress/public
```

### Changing things

`k8s_apply`, `k8s_delete`, `k8s_scale` and `k8s_rollout` each run a
**server-side dry run first**, and the approval prompt shows what the API
server says will happen --- not what the model claims will happen. A manifest
the server rejects never reaches you: the error comes back as a schema
correction instead.

Every prompt names the blast radius (`cluster AKS_QAM · namespace shop`), and
a **protected** context demands you type its name rather than press a key:

```
DELETE — approval required
Deployment/web
cluster AKS_EU_PROD · namespace payments
⚠ PROTECTED ENVIRONMENT — type  AKS_EU_PROD  to confirm
✓ server-side dry run succeeded: the delete is permitted
nothing here recreates it; deletion is permanent
```

There is no "always allow" on a protected target. Set `mode = "deny"` to
refuse outright instead.

### Knowing the schema before writing it

`k8s_explain` reads **your cluster's own OpenAPI**, the same source
`kubectl explain` uses:

```
> k8s_explain kind=Certificate field=spec

Certificate.spec  (cert-manager.io/v1)
  REQUIRED: issuerRef, secretName
  * secretName    string    Name of the Secret resource to store the certificate in
    commonName    string    Requested common name X509 certificate subject attribute
```

That is authoritative for your cluster at your version and covers custom
resources for free --- a static reference could not know your cluster serves
107 non-core API groups. `apiVersion` is resolved from discovery too, so
`Certificate` finds `cert-manager.io/v1` without anyone hardcoding it.

`k8s_topology` follows `ownerReferences` for the tree and infers the rest ---
Service selectors, Ingress backends, volume mounts, ConfigMap references, HPA
targets --- so it is a graph, not just a listing. `k8s_usage` charts requests
against limits against live usage; `k8s_top` and `k8s_storage` do the same for
node/pod metrics and volumes. Press Enter on any chart to expand it full-screen.

**The model never sees the chart.** Tools return a compact text summary for the
LLM and the visual separately, so a whole-cluster topology costs almost no
context. That is what makes this affordable rather than a novelty.

### What it will not pretend to know

- **No metrics-server, no live usage.** `k8s_top` names the missing component
  and how to install it. Requests, limits and counts still work.
- **Volume fill level is not available.** metrics-server exposes no volume
  statistics --- that needs Prometheus. `k8s_storage` charts provisioned size
  relative to the largest claim and says so. A bound PVC has requested ==
  capacity, so charting one against the other would show every volume at 100%
  and read as "full".

## Clouds

Optional extras, so you only carry what you use:

```bash
uv tool install '.[k8s]'    # from a clone; or aws, azure, gcp, all
uv sync --extra k8s         # ...or just for a dev checkout
```

Once Altus is on PyPI these become `altus[k8s]`. Until then the hints Altus prints
use the forms above, because `uv tool install 'altus[k8s]'` would simply fail.

Uninstalled integrations show up in `/tools` with the command to add them,
rather than silently not being there.

`/login azure` uses a **device-code flow through `azure-identity`** and works
with no `az` installed. `/login gcp` needs either `gcloud` or a service-account
key in `GOOGLE_APPLICATION_CREDENTIALS` — Google has no device-code flow that
works without a registered client, so there is no way around that.

### Two things it does not do

**It does not modify `~/.kube/config` by default.** `/kube use` records the
context in Altus's own config, because changing your global context as a side
effect of a chat message would silently retarget every other terminal you have
open. If you want kubectl-like behaviour:

```toml
[cloud]
kube_context_scope = "global"   # default "altus"
```

or per invocation: `/kube use <ctx> --global` (and `--local` to override the
other way). The global write sets exactly one key and preserves the rest of the
file, atomically.

**It redacts secrets before the model sees them.** Tool results are transmitted
to whichever LLM provider is active, so an unredacted Kubernetes Secret would
put base64 credentials in a third party's logs with no undo. Secret payloads,
credential-shaped keys, and `{name: API_TOKEN, value: …}` pairs are replaced
with a visible marker. `[cloud] secret_redaction = false` opts out.

### Protected environments

```toml
[cloud.protected]
patterns = ["*prod*", "*production*"]   # matched case-insensitively
accounts = ["123456789012"]
mode = "confirm"                        # or "deny"
```

Matching is case-insensitive on purpose: real clusters are as likely to be
called `AKS_EU_PROD` as `prod-eu`, and a rule that misses on case is worse
than no rule. `confirm` will require typing the target's name rather than
pressing a key.

## Providers

| Provider | Credential | Notes |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | extended thinking |
| `openai` | `OPENAI_API_KEY` | |
| `openrouter` | `OPENROUTER_API_KEY` | routes to many upstream models |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-reasoner` streams reasoning |
| `azure_foundry` | `AZURE_OPENAI_API_KEY` | needs `base_url`; model = deployment name |
| `gemini` | `GEMINI_API_KEY` | |
| `mistral` | `MISTRAL_API_KEY` | |
| `bedrock` | **AWS credential chain** | `AWS_PROFILE`, instance/IRSA roles — no API key |
| `local` | **none** | Anything OpenAI-compatible you run yourself — see below |

Credentials resolve in this order: `WAI_<PROVIDER>_API_KEY`, then the native
environment variable above, then the OS keyring. Environment wins so CI and
headless runs work without a keyring backend.

Bedrock is deliberately different: it authenticates through the standard boto3
chain, because requiring an API key would break the way a DevOps tool is
normally deployed.

## Local and self-hosted models

Ollama or LM Studio on your laptop, vLLM or llama.cpp on your own
infrastructure, MoE models included. `/setup local` finds servers running on
this machine without being told a port:

```
2 of 4 — which server?
  Ollama · http://127.0.0.1:11434/v1 · 3 models
  …or a URL, e.g. https://vllm.internal:8000
```

An endpoint counts only when `GET /v1/models` returns a real model list. A port
being open proves nothing — on macOS, port 5000 answers 403 and is AirPlay.
Only loopback is probed; remote endpoints are entered by URL.

**Profiles are how endpoints are named**, so one person can hold several:

```toml
[profiles.laptop]
provider = "local"
model = "qwen3:30b"
base_url = "http://127.0.0.1:11434/v1"

[profiles.cluster]
provider = "local"
model = "Qwen/Qwen3-235B-A22B"
base_url = "https://vllm.internal:8000/v1"
api_key_env = "VLLM_TOKEN"     # most local servers need no key at all
```

`/profile` lists them, `/profile use cluster` switches endpoint and model
together. Nothing local ever touches your OS keyring.

### Tool support is detected, not assumed

Altus's filesystem and Kubernetes tools need a model that can call tools, and
plenty of good local models cannot. Ollama reports this, so Altus reads it:

```
llama3.2:latest  (3.2B, Q4_K_M)              tools
tinyllama:latest (1.1B, Q4_0)                no tools
```

Choose a model without tool support and Altus declares no tools and says so —
offering them produces hallucinated call syntax or a hard error, which is far
more confusing than being told. vLLM and llama.cpp report nothing, so those
are assumed capable; override with `supports_tools = false` on the profile.

A large MoE loads before it emits its first token, so a cold start can take a
minute and Ollama re-loads after its idle timeout. Timeouts are generous and
the status line says the model is loading rather than looking hung.

## Configuration

`altus config path` prints the location (`~/.config/altus/config.toml` on Linux,
`~/Library/Application Support/altus/config.toml` on macOS). It never contains
secrets.

```toml
default_profile = "sonnet"

[profiles.sonnet]
provider = "anthropic"
model = "claude-sonnet-5"
max_tokens = 8192

[profiles.ops]
provider = "bedrock"
model = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
system = "You are a careful SRE. Explain before you act."

[providers.bedrock]
region = "eu-west-1"

[providers.azure_foundry]
base_url = "https://my-resource.openai.azure.com"
api_version = "2024-10-21"
```

Sessions are appended as JSONL under the platform data directory as each turn
completes, so an interrupted run never loses history.

## Keys

| | |
|---|---|
| `Enter` | send |
| `Ctrl+J` | newline |
| `Ctrl+P` | switch model |
| `Ctrl+N` | new session |
| `y` / `n` / `a` | at an approval prompt: approve, reject, always allow that tool |
| `Ctrl+C` | cancel the stream |
| `Ctrl+Q` | quit |

## Architecture

```
altus/core        normalized types, the event unions, retries
altus/providers   one adapter per provider, all folding onto that union
altus/config      configuration and credential resolution
altus/storage     JSONL session persistence
altus/workspace   the rooted filesystem context, and its containment rules
altus/tools       the tools, one package per surface (git, fs, and the clouds)
altus/cloud       Kubernetes, AWS, Azure and GCP: auth, classifiers, targets
altus/mcp         the nine shipped MCP servers, their manifests, and the custom door
altus/workflow    what a workflow is, how it runs, and its blast radius
altus/render      visuals, independent of the terminal drawing them
altus/runner      one inference call
altus/agent       the loop: inference, tool execution, repeat
altus/tui         the Textual front end
```

**Everything above `altus/tui` must stay importable without `textual`.** That
was a promise for five phases and `altus.workflow` is the first thing to
collect on it: a workflow is edited in a screen but defined, validated and
weighed with no terminal attached, which is what lets the engine drive it
headlessly. `tests/test_layering.py` enforces the rule — if it fails, move the
offending code into `altus.tui` rather than deleting the test.

Every adapter normalizes its provider's stream onto one event union
(`altus/core/events.py`), which already defines tool-call and reasoning events
even though Phase 1 emits only text. That is deliberate: it keeps the agent
loop from being a breaking change.

## Development

```bash
git clone https://github.com/shubhambakshi374/altus && cd altus
uv sync --all-groups        # also pulls every cloud extra: the dev group depends on altus[all]
uv run altus                  # the TUI, straight from the checkout
uvx --from . altus --version  # or run it once in a throwaway env, installing nothing
```

### The check suite

CI runs exactly these four, in this order. Run all of them before pushing ---
`ruff check` passing does **not** mean `ruff format --check` will:

```bash
uv run ruff check .
uv run ruff format .        # CI runs `--check`; run it without to fix in place
uv run mypy
uv run pytest
```

| | |
|---|---|
| `uv run pytest` | Unit tests. No network, no cluster, no API keys. |
| `uv run pytest -m live` | Hits real provider APIs and clusters. **Costs money.** Skips anything without credentials. |
| `uv run pytest tests/test_layering.py` | The architectural guard --- see below. |
| `uv run pytest tests/test_snapshots.py --snapshot-update` | Refresh TUI snapshots after a deliberate layout change or a Textual upgrade. |

`tests/test_layering.py` asserts that `core`, `providers`, `config`,
`storage`, `tools`, `cloud`, `runner.py`, `workspace.py` and `agent.py` never
import `textual`. If it fails, move the offending code into `altus.tui` rather
than deleting the test --- the workflow engine drives all of that headlessly.

### Debugging the TUI

`print` goes nowhere useful in a full-screen app. Use two terminals:

```bash
uv run textual console                          # terminal 1: log sink
uv run textual run --dev altus.tui.app:WaiApp     # terminal 2: the app, with live CSS reload
```

`self.log(...)` inside a widget then shows up in the console.

### Building a distributable

```bash
uv build                          # -> dist/altus-<version>-py3-none-any.whl and .tar.gz
uv tool install --force dist/*.whl
altus --version
uv tool uninstall altus
```

That produces a wheel anyone can install with `uv tool install` or `pipx`.
There is **no publishing set up** --- no PyPI release workflow, deliberately.
Distribution today is `git clone` or `uv tool install git+<url>`.

There is also no standalone single-file binary. `providers/registry.py` and
the cloud integrations import lazily via `importlib`, which PyInstaller's
static analysis cannot see, so a naive freeze would build cleanly and then
find zero providers at runtime. Doing it properly needs explicit
`hiddenimports` and a per-platform signing story.

### Working on one cloud only

The dev group installs every extra. To reproduce what a user with a partial
install sees --- and check the graceful-degradation path --- skip it:

```bash
uv sync --extra k8s --no-dev      # kubernetes only; GCP and Azure absent
uv run --no-dev altus tools list    # missing integrations show their install hint
```

### Layout

```
src/altus/       the package (see Architecture above)
tests/         mirrors it; tests/__snapshots__ holds the TUI SVGs
.github/       CI only
```

## Roadmap

- **Phase 1 — skeleton and chat.** ✅ Eight providers, streaming TUI, sessions, BYOK config.
- **Phase 1.5 — the workspace and read-only tools.** ✅ Agent loop, `read_file`/`list_dir`/`glob`/`grep`, sandboxed.
- **Phase 1.75 — writes behind an approval gate.** ✅ `write_file`/`edit_file`/`delete_path`, diff-first prompts.
- **Phase 2a — DevOps foundations.** ✅ Cloud auth, contexts, redaction, protected environments, slash commands.
- **Phase 2b — Kubernetes.** ✅ Topology, usage, storage, metrics, schema lookup, and changes behind a dry-run gate.
- **Local models.** ✅ Ollama, LM Studio, vLLM, llama.cpp — discovered, capability-checked, no key.
- **Phase 2c — AWS.** ✅ Inventory, VPC topology, cost, quotas, and any operation behind a gate that says what it could check.
- **Phase 2d — Azure.** ✅ Resource Graph inventory and topology, cost, quotas, and changes behind a gate that runs a real What-If diff where one exists.
- **Phase 2e — Google Cloud.** ✅ Asset-inventory search, VPC topology, quotas, and changes behind a gate that is honest about having almost nothing to preview.
- **Phase 2f — MCP.** ✅ Seven vendor servers kitted out, classified against a curated manifest that fails closed, with a gate honest about having no preview at all.
- **Phase 3a — the workflow designer.** ✅ Compose multi-step workflows in a screen, in conversation or in a file, each with one honest blast radius. Headless, which is the promise the layering guard has been keeping since Phase 1.
- **Phase 3b — the engine.** ✅ Run them: `${step}` substitution between steps, a failure that stops the run and names what it skipped, two gates, and a JSONL record written as it happens.
- **Phase 3c — the factory floor.** ✅ CrowdStrike and ServiceNow, a `[mcp.custom]` door for the rest, local git, steps that wait, workflow inputs, and three templates that run end to end.
- **Phase 3+ — shell, concurrency and triggers.** ✅ An allowlisted `shell_run` behind the same gate, `parallel` across independent branches, a headless `altus workflow` surface, and runs that **park** at the gate when nobody is watching rather than guessing.
- **Next —** per-item trigger fan-out, and whatever the first real factory floor asks for.

## License

Dual licensed under either of

- MIT ([LICENSE-MIT](LICENSE-MIT))
- Apache License 2.0 ([LICENSE-APACHE](LICENSE-APACHE))

at your option. Unless you state otherwise, any contribution you intentionally
submit for inclusion in this work shall be dual licensed as above, without any
additional terms or conditions.
