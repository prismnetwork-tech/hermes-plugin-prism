# hermes-plugin-prism

A Hermes Agent terminal backend that runs the agent's shell commands on a
dedicated NVIDIA GPU, rented by the second from the agent's own wallet on
[Prism Network](https://prismnetwork.tech) and paid for on-chain in USDG.

The agent gets a real GPU box for the session, with CUDA and root. You get two
spending caps it cannot raise, and a ledger of what it spent.

```
hermes config set terminal.backend prism
```

## Install

```bash
hermes plugins install prismnetwork-tech/hermes-plugin-prism --enable
hermes config set terminal.backend prism
hermes config set timeouts.tools.sequential_call 900
```

Hermes never installs a plugin's Python dependencies, and this one needs the
`prismnetwork` SDK, version 0.4.0 or later and below 0.5.0. The install command
prints that requirement and the line that satisfies it; `hermes doctor` names it
again if you skip the step.

The timeout line matters. Hermes abandons a tool call after 420 seconds by
default, and the first command of a session rents the GPU and waits for it to
come up, which the escrow allows ten minutes for. Under the default a lease
could be funded with nothing left waiting for it, so the backend refuses to
rent until the ceiling is 900 or higher (`0` removes it).

Then give it a wallet. Create a key, fund it with USDG for leases and a little
ETH for gas on Robinhood Chain (id 4663), and save it as `PRISM_AGENT_KEY` in
the `.env` file of your Hermes home:

```
PRISM_AGENT_KEY=0x...
```

That key stays on your machine. It is stripped from every command the model
runs and is never copied to the rented GPU, which
`tests/test_prism_terminal_live.py` asserts against a real lease.

`hermes doctor` reports the wallet, the caps, what is left of today's budget,
the trust class, and whether a GPU is currently rented.

Requires hermes-agent v0.20.6 (tag `v2026.8.27`) or later, the release that made
terminal backends pluggable. To run from source instead, put the repository at
`plugins/prism` under your Hermes home and run `hermes plugins enable prism`.

It settles on mainnet today. Lease 1230, on 2026-09-04, rented an RTX 6000 Ada
with 49,140 MiB, deposited 0.133200 USDG for the window, and was released after
28 seconds of access: 0.006216 USDG charged, 0.126984 returned. Receipt
`8f3e0c1d-391c-8510-9f77-ebc574905ffe` is listed on
[prismnetwork.tech/proof](https://prismnetwork.tech/proof), settled by
transaction `0x1de4eba627f8ffc6c0ce3f628b648c5f13d9f2815843d1fc6979a9b731309d88`
on Robinhood Chain.

## Configuration

All keys live under `terminal.prism` in `~/.hermes/config.yaml`.

| Key | Default | What it does |
| --- | --- | --- |
| `max_usdg` | `1` | Most a single lease may cost. |
| `daily_budget_usdg` | `5` | Most this wallet may spend in a rolling 24 hours. `0` removes the ceiling. |
| `min_vram_mib` | `16000` | Smallest GPU worth renting for this agent. |
| `trust_class` | `open` | Every machine on the network is `open` today. Leave it. |
| `lease_seconds` | `3600` | Length of the paid window, 300 or more. Priced up front, see below. |
| `image` | Prism's pinned default | Digest-pinned container image to run. |
| `sync_credentials` | `false` | Whether Hermes credential files are copied to the rented machine. |
| `idle_release_seconds` | `300` | Seconds without a command before the plugin gives the GPU back. `0` holds the lease until the session ends. |
| `ledger_path` | `~/.prism/spend.json` | File the day's spend is counted in. |

```bash
hermes config set terminal.prism.max_usdg 1
hermes config set terminal.prism.daily_budget_usdg 5
hermes config set terminal.prism.min_vram_mib 40000
```

`max_usdg`, `daily_budget_usdg` and `ledger_path` are the same three settings as
the SDK's `PRISM_MAX_USDG`, `PRISM_DAILY_BUDGET_USDG` and `PRISM_LEDGER_PATH`.
A value set here wins over an exported one. Pin `ledger_path` too: it names the
file today's spend is counted in, and an environment that moves it hands back a
budget that has already been spent.

## What it costs

No GPU is rented until the model's first command: starting Hermes, running
`hermes doctor` or `hermes status`, and a conversation that never reaches a tool
call all cost nothing.

A GPU is rented on the first command of a session and kept for the rest of it.
The lease survives between turns, so a conversation that runs twenty commands
pays for one machine. It is released when the session ends, or earlier by the
plugin's own idle monitor: five minutes without a command and the GPU goes back.

Releasing is what stops the meter. Live capacity quotes 0.7992 USDG per hour,
which is 222 base units per second. The whole window is deposited into escrow
when the lease opens; settlement charges the seconds between the machine
opening and the release and returns the rest. A lease that is never released
bills until its window ends, so the deposit is what your caps see and the
release is what decides the charge:

| Window | Deposit | Charged |
| --- | --- | --- |
| 15 minutes | 0.1998 USDG | Seconds until release, at most the window |
| 1 hour (the default) | 0.7992 USDG | Seconds until release, at most the window |

The gap between the two is usually large. A settled 30-minute lease that ran a
23-second CUDA job deposited 0.3996 USDG, charged 0.005106, and returned the
rest. Receipt `25dd3d12-bf11-843b-8770-3b5ba725cc97` on
[prismnetwork.tech/proof](https://prismnetwork.tech/proof) is that run. Budget
against the deposit and report the charge.

Rates move with supply. `hermes doctor` and the site carry the current number.

Two caps bound all of it, and the model can reach neither. Both are enforced
before any money moves:

- **Per lease.** A quote above `max_usdg` is refused, unfunded.
- **Per day.** Every lease is written to `~/.prism/spend.json` before it is
  funded. When the next lease would take the rolling 24-hour total past
  `daily_budget_usdg`, it is refused and nothing is charged. One wallet has one
  daily ceiling no matter which client is holding it, so the Prism MCP server
  and this backend draw down the same budget.

A lease that fails before its funding transaction is broadcast gives the
reservation back and costs nothing. Once the transaction is on the wire the
spend is counted against the day and the ledger entry records the hash. That
includes a lease whose receipt never arrives inside the wait: the transaction
usually lands, and forgiving it would let one wallet fund escrow after escrow
while the day's total read zero. The error names the transaction so you can
check it on Robinhood Chain. A lease the network refunds is credited back to the
day's budget once the refund is seen on chain: a machine that never hands over
access has its deposit returned by the escrow, and the ledger entry drops to
zero when the control plane reports the refund, keeping the transaction on file.

Hermes runs a failed command again up to three times before it gives the error
to the model, so a lease that fails with money already on the wire blocks the
next one for two minutes. Each retry is handed the transaction the first attempt
sent, so one command produces one lease.

A release the network refuses is written into the transcript and the capsule
(`release: failed`), because the machine then bills until its window ends. A
Hermes process that is killed outright never reaches its release; the lease
settles at the end of its window and the receipt shows the full charge.

The caps are re-read from config before every lease, so lowering
`daily_budget_usdg` mid-session applies to the next one. If
`~/.hermes/config.yaml` cannot be read or does not parse, the backend refuses to
rent anything and names the file to repair. Falling back to Hermes' built-in
defaults would spend at ceilings you never chose.

## The rented machine is public

Every GPU on the network is trust class `open`, which means the machine's
operator can read anything the workload touches. That is fine for builds, tests
and training runs on public data. It rules out keys, customer records, and any
credential you cannot rotate. There is no attested or confidential rented
workspace on Prism today, and this plugin will not pretend otherwise.

Hermes credential files stay off the rented machine unless you set
`sync_credentials: true`. Skills and cache files are always synced; they are what
the agent's own tools need to work.

Confidential *inference* is a different product: prompts sealed to a hardware
enclave, with no shell. [`docs/confidential-side-models.md`](docs/confidential-side-models.md)
covers it.

## The workspace is ephemeral

The rented machine's disk lives and dies with the lease. There is no resumable
volume and nothing is copied back on teardown. Work that must survive the
session belongs in a repository, an object store, or a file the agent writes
home over the network before the lease ends.

A background thread in the plugin watches for idleness and releases the lease
after five minutes with no command, so an agent that goes quiet stops paying for
the machine. The next command rents a fresh one, against the same caps, and the
model is told its disk went with the old machine. Change the window with
`terminal.prism.idle_release_seconds`, or set it to `0` to hold the lease for
the whole session.

Sessions share one rented machine by default. Set
`terminal.container_persistent: false` to give each session a lease of its own.

## File transfer

The SDK exposes commands over the lease's SSH channel and no separate file
transfer, so files are inlined: base64 on stdin into `base64 -d` on the far
side. That is fine for skills, cache entries and small artifacts. Anything over
4 MiB is skipped with a warning, so one large file cannot stall the session.
Move large payloads by fetching them on the box.

## Capacity

When nothing on the network matches the request, the command fails with a plain
"no GPU is available right now, retry" and nothing is charged. It never blocks
waiting for supply. Lower `min_vram_mib` or `trust_class` if it persists.

## Skills

Three skills ship in `skills/`. They carry judgment and procedure: what to buy,
in what order, and how to hand the result to someone who does not trust you.

| Skill | Answers |
| --- | --- |
| `prism-gpu-compute` | Should this run here, in Docker, or on a rented GPU? One-shot command or a lease? Which class, what will it cost, what do the caps mean, and what to do when there is no capacity. `references/costs.md` and `references/gpu-classes.md` carry the numbers. |
| `prism-gpu-cuda-repro` | The audited CUDA reproduction as a procedure: pin the image and command, prepare, review the quote, get it funded, wait, collect the signed evidence, verify, cite the receipt. |
| `prism-gpu-receipts` | Turning a settled lease into a citation, and checking one somebody else cited. Defines the proof capsule and ships `scripts/verify_receipt.py`. |

The verifier recomputes a published receipt hash with nothing but the standard
library, and its exit code is the answer: `0` verified and clean, `1` a check
failed, `3` verified but the run carries a `failure_class` and must not be cited
as a clean result.

```bash
python3 skills/prism-gpu-receipts/scripts/verify_receipt.py --self-test
python3 skills/prism-gpu-receipts/scripts/verify_receipt.py 25dd3d12-bf11-843b-8770-3b5ba725cc97
```

Installing the plugin puts the directory on disk but does not load the skills.
Take one from the hub, backend or no backend:

```bash
hermes skills install prismnetwork-tech/hermes-plugin-prism/prism-gpu-cuda-repro
```

Or point a profile at the directory the plugin already ships:

```bash
hermes config set skills.external_dirs '["plugins/prism/skills"]'
```

The path resolves against that profile's Hermes home, the skills load read-only,
and skill creation still writes to `~/.hermes/skills/`.

The skills were published as `prism-compute`, `prism-cuda-repro` and
`prism-receipts` before 0.3.0. Those identifiers are retired; the ones above are
the current names.

Lint them the way Hermes does, and check that Hermes really finds them:

```bash
~/.hermes/hermes-agent/venv/bin/python -m tools.skill_linter skills/
python -m pytest tests/test_skills_and_profile.py
```

The test copies the plugin into a throwaway Hermes home, loads it through the
real plugin manager, and asserts the skills are discovered, the SKILL.md files
render, `mcp.json` parses under the Agent Plugins v1 validator, and the Compute
Bot fragment does not loosen the plugin's ceilings. No wallet and no network.

### The MCP surface

`mcp.json` at the repo root declares one server: the Prism repro endpoint at
`https://prismnetwork.tech/api/mcp`. It publishes exactly six tools, all
annotated read-only, none of which can spend or sign:
`prism_gpu_capacity`, `prism_prepare_gpu_repro`, `prism_gpu_repro_status`,
`prism_gpu_repro_evidence`, `prism_verify_gpu_repro`, `prism_gpu_receipts`.
No wallet and no key. The filtering is the server's, so there is nothing to
allowlist.

Hermes does not load that file for this plugin, so treat it as a declaration you
can copy. Tool filtering lives in `config.yaml`, which supports it:

```bash
hermes mcp add prism-repro --url https://prismnetwork.tech/api/mcp
```

then set `mcp_servers.prism-repro.tools.include` to the six names, matched
exactly or by glob. This matters for the wallet-side `@prismnetwork/mcp` npm
server, a different set of tools that includes leasing, inference and the vault.
Adding that one without an include list gives the model everything in it.
`profiles/compute/config.yaml` has both entries written out.

## Compute Bot

A ready-made profile template lives in `profiles/compute/`: a Bot whose terminal
is a rented GPU, with ceilings below the plugin defaults, the three skills
enabled, and the read-only MCP surface wired up. Other Bots hand it GPU work by
assigning a Kanban card to `compute`, and it reports back what ran, what it cost
in USDG, and the receipt.

```bash
hermes profile create compute \
    --description "Runs GPU work on a rented NVIDIA card and reports what it cost."
```

`profiles/compute/README.md` walks the rest: installing the plugin into the
profile, giving it a wallet of its own, merging `config.yaml`, and copying
`SOUL.md`. The soul is where the refusals are written down. It will not raise a
cap or split a job to slip under one. It will not put a credential on a machine
whose operator can read it. It stops for approval above a 0.25 USDG deposit,
which is why the profile's default lease is 15 minutes and 0.1998 USDG. And it
says plainly when something is not offered: persistent disks, attested rented
workspaces, pinned training images.

## Tests

The suite needs `pytest` and the `prismnetwork` SDK in the environment running
Hermes; an editable install of a local SDK checkout works if you are changing
both at once.

```bash
python -m pytest tests/test_prism_environment.py            # unit, no wallet, no network
PRISM_LIVE_SPEND=1 python -m pytest tests/test_prism_terminal_live.py -m integration
```

The unit suite mocks everything at the `PrismAgent` boundary. The integration
suite rents a real GPU and spends real USDG, which is why it takes two opt-ins.
Both need a hermes-agent checkout on the path; set `HERMES_AGENT_REPO` if yours
is not at `~/.hermes/hermes-agent`.

## License

Apache-2.0. Built by Prism Network. Not affiliated with Nous Research.
