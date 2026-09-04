# What Prism compute costs

Every figure here is either a live quote or a settled receipt. Prices move with
supply, so re-read capacity before quoting a number to the operator.

## The unit

USDG on Robinhood Chain has 6 decimals. Receipts and quotes are in base units.

    1 USDG = 1,000,000 base units

## Observed lease rate

Live capacity quotes `0.7992` USDG per hour for both classes on offer, RTX A6000
and RTX 6000 Ada. Every settled lease in the public feed metered at that rate
except one RTX 6000 Ada lease at 177 base units per second. The rate is the
supplier's, so read the live quote rather than assuming the table:

| Window | Base units | USDG |
| --- | --- | --- |
| 1 second | 222 | 0.000222 |
| 1 minute | 13,320 | 0.01332 |
| 10 minutes | 133,200 | 0.1332 |
| 30 minutes | 399,600 | 0.3996 |
| 1 hour | 799,200 | 0.7992 |

The lease deposits the *whole window* into escrow up front. Settlement charges
the seconds until the lease is released and returns the rest; a lease nobody
releases is charged for the window. Budget against the deposit.

## The settled reference run

Chain lease 10 on the RTX A6000 class, trust class `open`, the CUDA vector-add
repro image:

| Field | Value |
| --- | --- |
| `receipt_id` | `25dd3d12-bf11-843b-8770-3b5ba725cc97` |
| `runtime_seconds` | 23 |
| `charged_base_units` | 5,106 (0.005106 USDG) |
| `refunded_base_units` | 394,494 (0.394494 USDG) |
| Deposit | 399,600 (0.3996 USDG), a 30-minute window |
| `transaction_hash` | `0x200bf5bca1fb873df4ff04fb302ec94680327370a12a11d8d3a696df61960d56` |

23 seconds of work against a 30-minute booking cost half a cent. The deposit is
what the cap sees; the charge is what the wallet loses.

Four later leases on the same class ran the full 600 seconds and charged 133,200
base units each, which is the same 222 per second.

## Refunds and interrupted runs

Lease 15 on the same node never provisioned. It settled `outcome: "refunded"`,
`failure_class: "provisioning_timeout"`, `charged_base_units: 0`, with the full
133,200 deposit returned. A machine that does not boot costs nothing, and the
refund is published in the same public feed as a successful run. 57 of the 256
published receipts are refunds of exactly this shape.

A machine that boots and then stops answering settles differently. Lease 143 on
an RTX 5880 Ada ran against a 900-second window, was billed for 192 seconds
(42,624 base units), and carries `outcome: "finalized"` with
`failure_class: "interrupted"` and `credited_seconds: 152`. Those 152 seconds
are time the lease was held and *not* charged, because the machine had already
gone quiet. The run is settled and the receipt verifies. It is still a run that
was cut short, and citing it without saying so is a false claim. See
`prism-receipts`.

## The one-shot endpoint

`POST https://api.prismnetwork.tech/x402/run` charges one flat price per
command, whatever the runtime:

| Rail | Price | Base units |
| --- | --- | --- |
| USDC on Base (`eip155:8453`) | 0.03 | 30,000 |
| USDG on Robinhood Chain (`eip155:4663`) | 0.03 | 30,000 |

The `402` challenge carries the binding price and the EIP-712 domain for the
request in hand. Treat these numbers as a prior and let the challenge set the
price.

The charge is taken when the job reaches a terminal `completed` state, which
means the command ran on the box. A job that never gets that far releases the
authorisation or refunds it. The `charged` field on the finished job record is
what actually happened, so read it rather than inferring from the exit code.

Break-even against a lease is around 135 seconds of A6000 time. Below that the
one shot costs more per second and is still the right choice, because a lease
also charges you for provisioning and for the window you did not use.

## Managed inference

Priced per model, as a base charge plus a per-token charge, capped per
generation. The open models today:

| Model | Base | Per token | Cap per generation |
| --- | --- | --- | --- |
| `llama3.2:3b` | 3,000 | 3 | 6,072 (0.006072 USDG) |
| `llama3.1:8b` | 6,000 | 6 | 12,144 (0.012144 USDG) |

The confidential tier relays to a Phala TEE and is priced on its own table, with
per-generation caps from 11,024 to 25,360 base units depending on the model.
`GET /inference/v1/models` is free and carries both tables. Read it and name a
model from the list.

Payment is consumed only when a response is served. A `503 warming_up` means a
GPU is being leased and models are being pulled, nothing was charged, and the
*same* payment header should be sent again. Signing a second authorisation after
a `503` risks paying twice for one answer.

## Caps, in numbers

Defaults are 1 USDG per lease and 5 USDG per rolling 24 hours.

- A 30-minute lease deposits 0.3996, so twelve fit in a day's budget by deposit
  even though twelve runs as short as the reference one would settle under 0.07
  in total.
- A 1-hour lease deposits 0.7992, inside the per-lease cap with little room. A
  2-hour window at this rate would deposit 1.5984 and be refused.
- 166 one-shot `/x402/run` calls fit in a 5 USDG day.

Check what is left before planning: `prism_budget` in the npm MCP server, or
`hermes doctor`, which prints the per-lease ceiling and the remaining day
alongside the ledger path.

## Where the money goes

A settled receipt reconciles exactly:

    charged_base_units + refunded_base_units == deposit
    provider_paid_base_units + protocol fee == charged_base_units

On lease 10 the provider was paid 4,596 of the 5,106 charged. On the 600-second
leases, 119,880 of 133,200. If a receipt does not reconcile, do not cite it.
