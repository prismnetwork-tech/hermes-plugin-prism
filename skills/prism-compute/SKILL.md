---
name: prism-compute
description: Choose where a job runs and rent a GPU on Prism Network.
license: Apache-2.0
version: 0.1.0
author: Prism Network <opensource@prismnetwork.tech>
metadata:
  hermes:
    tags:
      - gpu
      - compute
      - cuda
      - prism
      - usdg
      - x402
    category: compute
    related_skills:
      - prism-cuda-repro
      - prism-receipts
---

# Renting a GPU on Prism

Prism rents dedicated NVIDIA GPUs by the second. Payment settles on-chain in
USDG on Robinhood Chain (id 4663), or in USDC on Base for the one-shot HTTP
endpoint. There is no account and no API key. Paying for a request is what
authorises it, and metering runs against seconds actually served.

A short lease costs cents. Work down the decision table anyway: a lease books
its whole window as a deposit against the day's budget, and provisioning adds
minutes before the first command runs.

## When to use

- A job needs CUDA and this machine has no NVIDIA GPU.
- A job needs more VRAM than the local card has.
- A result has to be citable by someone who does not trust you: a settled lease
  publishes a public receipt. See `prism-receipts`.
- A CUDA reproduction has to run against a pinned image. Use `prism-cuda-repro`,
  the audited path, which removes this skill's judgment calls.

## Where should this run

Work down the table. Stop at the first row that fits.

| Situation | Where it runs | Why |
| --- | --- | --- |
| No GPU needed, no untrusted code | This machine | Free and immediate. |
| No GPU needed, untrusted or messy dependencies | Local Docker | Isolation without renting. |
| GPU needed, local NVIDIA card has enough VRAM | This machine | Free. Check with `nvidia-smi`. |
| GPU needed, no local card, one bounded command | Prism `/x402/run` | Flat price whatever the runtime. |
| GPU needed, multi-step or interactive | Prism lease | You keep the machine and pay per second. |
| One LLM generation, no local model, no API key | Prism managed inference | Fractions of a cent, and no GPU to size. |

A single `nvidia-smi` or a five-second sanity check does not repay a lease.
Neither does a job you have not yet run once on CPU at reduced size.

## Two purchase paths

**One shot.** `POST https://api.prismnetwork.tech/x402/run` with
`{"command": "..."}`. It runs asynchronously, so the answer to the POST is a
handle rather than a result. Three steps.

*Read the price.* An unpaid POST comes back `402 Payment Required` with an
`accepts` array holding both rails: 30,000 base units (0.03) of USDC on Base
(`eip155:8453`) or of USDG on Robinhood Chain (`eip155:4663`), each with the
EIP-712 domain to sign an EIP-3009 authorisation against. The 402 is
authoritative for the request in hand. Reading it is free and runs nothing.

*Submit.* Retry the same body with the payment header. An accepted request
answers `202` with the handle:

```json
{
  "job_id": "3f2a1c88-0f1e-4c1a-9f0b-7c2d5e6a1b34",
  "status": "queued",
  "token": "b7c1e0a2-9d3f-4a55-8e21-0c6f4b9d2a17",
  "poll": "/jobs/3f2a1c88-0f1e-4c1a-9f0b-7c2d5e6a1b34"
}
```

Keep `token`. It is the only handle to this job's output and it is not repeated
anywhere.

*Poll.* `poll` is relative to the endpoint's base, so the full URL is
`https://api.prismnetwork.tech/x402/jobs/<job_id>`. Authorise it with
`Authorization: Bearer <token>`, or `?token=<token>` when a header is already
taken. `status` walks `queued` to `running` to one of two terminal values:

| Terminal status | Carries | Means |
| --- | --- | --- |
| `completed` | `exit_code`, `stdout`, `stderr`, `lease_id`, `charged` | The command ran on the GPU. Read `exit_code` yourself. |
| `failed` | `error`, sometimes `detail` | The job never ran. Nothing is owed. |

A wrong token answers `401 invalid_job_token`. An unknown or expired id answers
`404 job_not_found`; finished jobs are dropped an hour after they end, so
collect the output rather than planning to come back for it.

`charged` on the finished record is what actually happened to the money.
`charged: false` with `"not charged: the authorization was never broadcast"`
means you can spend the same authorisation on a retry. `charged: null` with
`settlement: {"error": "settlement_unconfirmed"}` means the broadcast may have
landed: do not sign a second authorisation, and report the job id to the
operator.

Nothing survives between calls: no files and no running processes. A command
that expects output from an earlier call will not find it.

**A lease.** You hold the machine for a fixed window and pay for the seconds
until it is released, the window being the most you can be charged. This is what
`terminal.backend: prism` does inside Hermes: a GPU is rented on the first
command of a session and released on cleanup or after five idle minutes, and the
release is what stops the meter. Use it when steps depend on each other, or when
the work needs a shell that outlives one command.

The lease image defaults to the upstream Ollama image at a pinned digest,
`docker.io/ollama/ollama@sha256:a61a8fd3…`. Prism does not build it and it
carries no training or serving stack beyond Ollama. Set `terminal.prism.image`
to a digest-pinned image of your own when the job needs one.

Choose the one shot when the whole job is one command whose exit code is the
answer. Choose a lease the moment a second command needs the first one's output.

## Reading capacity and price before you commit

Both are free and neither reserves anything.

- `prism_gpu_capacity` on `https://prismnetwork.tech/api/mcp` returns the live
  class list with a starting hourly rate and a reliability figure per class.
  Optional `min_vram_gib` filters it.
- `prism_list_gpus` and `prism_price_index` in the `@prismnetwork/mcp` npm server
  do the same from a wallet-holding client.
- `https://prismnetwork.tech` shows the same capacity for a human.

Read capacity first. Supply is two or three machines at a time, so a plan that
assumes a GPU is waiting will fail at the funding step.

## Which class fits

Two classes rotate through the live offer list, RTX A6000 and RTX 6000 Ada, both
at 45 to 48 GiB, CUDA 12, trust class `open`, quoting from 0.7992 USDG per hour.
A single reading often shows only one of them.

The public receipt feed carries four more classes that have settled leases and
are not on offer today: RTX 5880 Ada, L40S, H100 PCIe and A40. Someone who has
read `/proof` may ask for an H100. The answer is that the network has run them
and you cannot rent one right now. The counts, the offer list and the VRAM
arithmetic are in [gpu-classes.md](references/gpu-classes.md).

The short version for what you can buy: about 46 GiB of usable VRAM. That is
comfortable for a 7B or 13B model in fp16 and for CUDA kernel work, and it holds
most single-GPU fine-tunes with LoRA at modest sequence length. It is short of a
70B model in fp16.

At trust class `open` the host operator can read anything the workload touches.
Treat a rented box as a public machine. Keep private keys and customer records
off it, along with any credential you cannot rotate.

## The spend policy

Two ceilings bound every purchase, and the model can lower them but never raise
them.

| Setting | Default | Bounds |
| --- | --- | --- |
| `terminal.prism.max_usdg` / `PRISM_MAX_USDG` | 1 | Any single lease or generation. |
| `terminal.prism.daily_budget_usdg` / `PRISM_DAILY_BUDGET_USDG` | 5 | Everything in a rolling 24 hours. `0` removes the ceiling. |

The ledger is a single file, `~/.prism/spend.json`, shared by every Prism client
on the machine. The MCP server and this plugin draw down the same day. Spend is
written before the money moves, so a crash between funding and answering still
counts against the day.

If you pass `max_usdg` on a call, it is read as a request for a *lower* ceiling.
A value above the operator's is clamped back down. Ask the operator to raise the
config value; do not try to route around it.

The wallet balance bounds all of it, and no config value can raise that.

## What it actually costs

Full worked numbers in [costs.md](references/costs.md). The priors:

- Observed lease rate: 0.7992 USDG per hour, which is 222 base units per second.
- A settled 30-minute lease that ran for 23 seconds charged 5,106 base units
  (0.005106 USDG) and refunded the rest of the 0.3996 USDG deposit.
- A one-shot `/x402/run` is a flat 0.03 on either rail.
- Managed inference is priced per model. A `llama3.2:3b` generation caps at
  6,072 base units (0.006072 USDG) and a `llama3.1:8b` generation at 12,144
  (0.012144). `GET /inference/v1/models` carries the current table.

A 30-minute lease deposits about 0.4 USDG into escrow. That is inside the 1 USDG
per-lease default and consumes about 8% of a 5 USDG day even though the settled
charge is usually a fraction of it. Budget against the deposit, not against the
charge you expect.

## Failure modes

**No capacity.** The request fails immediately with a plain "no GPU is available
right now, retry" and nothing is charged. It never blocks waiting for supply.
Lower `min_vram_mib`, or wait and retry. Do not loop tightly; capacity changes on
the order of minutes to hours. Tell the operator when it persists.

**Slow boot.** A machine that never comes up settles as a refund with
`failure_class: "provisioning_timeout"` and the full deposit returned. The public
feed carries these, so you can confirm one happened. Nothing is owed. Re-quote
and try again, ideally against a different node.

**Cap refusal.** A quote above the per-lease cap, or one that would take the
rolling day past the daily cap, is refused before the escrow is funded. Nothing
is charged and the refusal names both ceilings. Report the numbers to the
operator and stop. Do not split one job into several smaller leases to slip under
a cap; the daily ledger totals them anyway.

**Command failed on a healthy GPU.** A lease still bills for the seconds it ran,
and `/x402/run` still bills its flat price once the job reaches `completed`.
Exit codes are yours to handle.

**Machine went quiet mid-lease.** The receipt settles `finalized` with
`failure_class: "interrupted"` and a `credited_seconds` count of the time you
held but were not charged for. The work is gone and the partial charge stands.
Re-run it, and if you cite the receipt, say it was interrupted. `prism-receipts`
has the citation rule.

## When NOT to use

- The job runs on CPU. Renting a GPU to run `pytest` wastes the operator's money.
- You have not run the job once at small scale locally. Debug on free hardware.
- The work touches secrets, customer records, or a key. Every machine on offer
  today is trust class `open` and the host operator can read it. Prism has no
  attested or confidential rented *workspace* today. Confidential *inference* is
  a separate product: prompts encrypted to a TEE, with no shell.
- The work needs a disk that survives the lease. That does not exist. The rented
  disk is destroyed with the lease and nothing is copied back. Push results to a
  repository or an object store before the window ends.
- You want a pinned LoRA or vLLM serving image. Prism publishes no `prism-train`
  or `prism-serve` image. The lease default is the upstream Ollama image; bring a
  digest-pinned image of your own, or use the CUDA repro image.
- The operator has not funded a wallet. Check first: `hermes doctor` reports the
  wallet, the caps, and what is left of today's budget.

## Verification

After any purchase:

1. Read the actual output as well as the status code. Exit zero means the process
   ended, and says nothing about whether it did the right thing.
2. For a lease, confirm the settled charge against the receipt feed, and read
   `failure_class` before calling the run clean. See `prism-receipts` for the
   fields and the citation format.
3. Report the charge to the operator in USDG with the lease id, so the next run
   can be quoted from a real number.
