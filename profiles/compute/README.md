# Creating the Compute Bot

A Hermes profile whose terminal is a rented NVIDIA GPU. Other Bots hand it GPU
work and get back a result, a cost in USDG, and a public receipt. Its own spend
ceilings are lower than the plugin defaults, so a mistake costs cents.

Everything below is copied into a profile you own. Nothing here is applied
automatically.

## 1. Create the profile

```bash
hermes profile create compute \
    --description "Runs GPU work on a rented NVIDIA card and reports what it cost."
```

The profile gets its own Hermes home at `~/.hermes/profiles/compute/`, its own
`config.yaml`, `.env`, `SOUL.md`, memory and sessions, and a `compute` command
alias. The `--description` is what the Kanban orchestrator reads when it decides
where to route a card, so write it as a routing hint.

To start from your existing model and provider settings, add `--clone`. That
copies `config.yaml`, `.env`, `SOUL.md` and skills, and leaves memory and
sessions fresh.

Then configure models and keys:

```bash
compute setup
```

## 2. Install the plugin into this profile

```bash
pip install prismnetwork
git clone https://github.com/prismnetwork-tech/hermes-plugin-prism \
    ~/.hermes/profiles/compute/plugins/prism
hermes -p compute plugins enable prism
```

## 3. Give it a wallet

Create a key, fund it with USDG for leases and a little ETH for gas on Robinhood
Chain (id 4663), and put it in this profile's env file at
`~/.hermes/profiles/compute/.env`:

```
PRISM_AGENT_KEY=0x...
```

Give this Bot a wallet of its own with only what you are willing to lose on it.
The caps are enforced before money moves; the balance is what survives a bug in
everything above them.

## 4. Apply the config

Merge [`config.yaml`](config.yaml) into
`~/.hermes/profiles/compute/config.yaml`. It sets the terminal backend, tighter
caps than the plugin defaults, the three Prism skills, and the read-only MCP
surface. Or set the same keys one at a time:

```bash
compute config set terminal.backend prism
compute config set terminal.prism.max_usdg 0.5
compute config set terminal.prism.daily_budget_usdg 2
compute config set terminal.prism.min_vram_mib 45056
compute config set terminal.prism.lease_seconds 900
compute config set timeouts.tools.sequential_call 900
```

### Why the lease is 15 minutes

A lease deposits its whole window into escrow up front and is charged for the
seconds until the plugin releases it, so the window is the ceiling and the
deposit is what the caps see. At the rate live capacity has quoted since these
classes came online, 0.7992 USDG per hour or 222 base units per second, the
deposit is the window times the rate:

| Window | Deposit |
| --- | --- |
| 15 minutes | 0.1998 USDG |
| 30 minutes | 0.3996 USDG |
| 1 hour | 0.7992 USDG |

`SOUL.md` stops for your approval above a 0.25 USDG deposit. 15 minutes sits
under that, so routine work runs without interrupting you and anything longer
comes to you first. A 30-minute default would put every single lease above the
stop, which trains you to approve without reading.

Move the two together. Raising `lease_seconds` past 900 without raising the
threshold in `SOUL.md` means an approval prompt on every command; raising the
threshold without shortening the window means the stop never fires. Both numbers
stay under `max_usdg`, which is the ceiling the model cannot reach at all.

## 5. Give it the soul

```bash
cp ~/.hermes/profiles/compute/plugins/prism/profiles/compute/SOUL.md \
   ~/.hermes/profiles/compute/SOUL.md
```

[`SOUL.md`](SOUL.md) is where the refusals live: no raising a cap, no secrets on
an `open` machine, no claiming a capability that does not exist, and an approval
stop above a 0.25 USDG deposit. Edit the threshold to whatever you are willing
to have spent without being asked, and read the arithmetic above before you do.
`SOUL.md` changes take effect on a new session.

## 6. Check it

```bash
compute doctor
```

It reports the wallet address, the per-lease ceiling, what is left of today's
budget, the ledger path, the trust class, and whether a GPU is currently rented.
A red row here is a setup problem, not a capacity problem.

```bash
compute skills list | grep prism
compute mcp test prism-repro
```

## Handing it work

Assign a Kanban card to the profile, which is the routing the `--description`
above feeds:

```bash
hermes kanban create "Benchmark the fused kernel on an A6000" --assignee compute
```

In prose on a board, `@compute` routes the same way. The Bot claims the card,
picks a purchase path, runs it, and completes with the settled cost and the
receipt in its handoff, which the next card inherits through the parent link.

## What it will refuse

Worth knowing before you hand it something:

- A quote above `max_usdg`, or one that would take the rolling day past
  `daily_budget_usdg`. Nothing is charged and the refusal names both numbers.
- Anything involving a key, a customer record, or a credential. Every machine on
  offer today is trust class `open`.
- A request for a persistent workspace, an attested rented box, or a pinned
  training or serving image. None of those exist today and it says so rather
  than approximating one.
- Funding the audited repro rail without a human approval. The six repro tools
  are read-only and cannot spend; funding happens when the operator approves the
  quote in the Prism web application.

## Two profiles, two wallets

Never point two agent processes at one profile. Both write memory, and each
loads the other's writes at session start. If you want a second GPU Bot, create
a second profile with its own wallet.

The daily budget belongs to the *wallet*. The ledger at `~/.prism/spend.json` is
shared by every Prism client on the machine, so two profiles sharing one key
share one day.
