---
name: prism-receipts
description: Cite and verify a Prism GPU run from its public receipt.
license: Apache-2.0
version: 0.1.0
author: Prism Network <opensource@prismnetwork.tech>
metadata:
  hermes:
    tags:
      - receipts
      - evidence
      - citation
      - verification
      - prism
      - robinhood-chain
    category: compute
    related_skills:
      - prism-compute
      - prism-cuda-repro
---

# Citing a Prism run

Every finalized lease publishes a receipt. The receipt commits to a hash that a
settlement transaction on Robinhood Chain carries on-chain, so a reader who
trusts neither you nor Prism can still check that the run was paid for, how long
it ran, and what it cost.

What a receipt establishes: a lease existed, it settled for this amount, and for
a repro run the published hashes match the report the executor signed. What it
does not establish: that the computation was faithful. Say both when you cite
one.

## When to Use

- You just settled a lease and someone else has to be able to check the result.
- You are writing a claim into an issue, a pull request, or a research note and
  the number came off a rented GPU.
- Somebody cited a Prism receipt at you and you want to know whether it holds.
- You need to prove a run was refunded and never charged.

## Where receipts live

| Purpose | Where |
| --- | --- |
| Human page | `https://prismnetwork.tech/proof` |
| Machine index | `https://api.prismnetwork.tech/proof/index.json` |
| One receipt | `https://api.prismnetwork.tech/proof/receipts/<receipt_id>.json` |
| Query by id, tx or spec hash | `prism_gpu_receipts` on `https://prismnetwork.tech/api/mcp` |
| Settlement transaction | `https://robinhoodchain.blockscout.com/tx/<hash>` |

The index carries `total`, `page_size`, `pages` and `first_page`. When `total`
exceeds the window the index lists, walk `first_page` and follow each page's
`next` until it is `null`. Pages are immutable and newest first, so a walk stays
on one set even if a newer index is published while you read.

The feed is pseudonymous by design. It publishes no wallet address, no precise
geography, no full image reference outside a repro receipt, no files and no
terminal output. Do not promise a reader more than that.

## Three outcomes, and only one of them is clean

Read `outcome` and `failure_class` together. `outcome` alone will let you cite a
run that was cut short as if it had finished.

| `outcome` | `failure_class` | What happened |
| --- | --- | --- |
| `finalized` | `null` | The lease ran its course and settled. Cite it plainly. |
| `finalized` | `"interrupted"` | The machine stopped answering before the window ended. A partial charge stands. |
| `refunded` | `"provisioning_timeout"` | The machine never booted. Zero charge, full deposit back. |

An interrupted receipt is a valid receipt. It reconciles, its hash checks, and
the settlement transaction is on chain. It still evidences a run that stopped
early, and it can never evidence one that finished.

`credited_seconds` on that receipt counts the seconds the lease was held and
**not** charged for. It measures the gap. The metered figure stays
`runtime_seconds`. On the one interrupted receipt in the feed today, lease 143:
`runtime_seconds` 192, billed 42,624 base units at 222 per second,
`credited_seconds` 152.

The rule: a citation of an interrupted run names the interruption in the same
sentence as the claim, or it is not made.

## The proof capsule

This is the shape to write when you hand a run to someone else. It is a
projection of the public receipt: every field is copied from the receipt or
derived from it, so the capsule carries no authority of its own.

```json
{
  "image_digest": "sha256:2e6d1873c8abd20d50dd311ac76324ef432c0a0396bd71b201b34c633e005930",
  "gpu_model": "NVIDIA RTX A6000",
  "trust_class": "open",
  "started_at": null,
  "ended_at": null,
  "charged_seconds": 23,
  "credited_seconds": null,
  "failure_class": null,
  "charged_base_units": 5106,
  "stdout_sha256": "736c5b64ce0336f00e2fbf99a8ef7122c9d3f0b29bb6a5ec5a1de673078152a1",
  "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "artifact_hashes": [],
  "receipt_id": "25dd3d12-bf11-843b-8770-3b5ba725cc97",
  "settlement_tx": "0x200bf5bca1fb873df4ff04fb302ec94680327370a12a11d8d3a696df61960d56",
  "proof_url": "https://api.prismnetwork.tech/proof/receipts/25dd3d12-bf11-843b-8770-3b5ba725cc97.json"
}
```

### Which fields exist today

| Capsule field | Source in the public receipt | Available |
| --- | --- | --- |
| `receipt_id` | `receipt_id` | Always |
| `gpu_model` | `gpu_model` | Always |
| `charged_base_units` | `charged_base_units` | Always |
| `settlement_tx` | `transaction_hash` | Always |
| `proof_url` | Derived from `receipt_id` | Always |
| `charged_seconds` | `runtime_seconds` | Always |
| `failure_class` | `failure_class` | Always, and `null` on a clean run |
| `credited_seconds` | `credited_seconds` | Interrupted runs only; `null` otherwise |
| `trust_class` | `trust_class` | Present on current receipts; omitted on receipts minted before the field existed |
| `image_digest` | `repro.image_digest` | Repro runs only |
| `stdout_sha256` | `repro.stdout_hash` | Repro runs only |
| `stderr_sha256` | `repro.stderr_hash` | Repro runs only |
| `started_at` | Not published | `null` today |
| `ended_at` | Not published | `null` today |
| `artifact_hashes` | Not published | `[]` today |

`started_at` and `ended_at` exist inside the executor's signed report, which is
capability-scoped to whoever holds the repro token. They are deliberately absent
from the public feed. Emit `null`. Do not derive them from the settlement block
time.

`artifact_hashes` is reserved for output files committed alongside the streams.
Nothing writes it yet. Emit `[]` and do not describe it as a feature.

Both, along with the `attestation` block that the receipt format already
reserves (`kind`, `verdict_digest`, `verifier_version`), arrive with the attested
and confidential trust classes. Those classes are not served today, so no current
receipt carries an attestation, and a capsule that claims one is wrong.

An empty stream hashes to `e3b0c442…b855`, the SHA-256 of zero bytes. On the
reference run that is exactly what `stderr_sha256` is, which is a fast way to
tell "nothing was written" from "we did not record it".

## Attaching a capsule

**To a GitHub issue or pull request comment.** Lead with the claim, then the
evidence, then the limit. Keep it short enough to read without expanding.

```markdown
Reproduced on a rented NVIDIA RTX A6000 (trust class `open`), 23 s of GPU time,
settled for 0.005106 USDG on Robinhood Chain.

- Image: `sha256:2e6d1873c8abd20d50dd311ac76324ef432c0a0396bd71b201b34c633e005930`
- Receipt: https://api.prismnetwork.tech/proof/receipts/25dd3d12-bf11-843b-8770-3b5ba725cc97.json
- Settlement: https://robinhoodchain.blockscout.com/tx/0x200bf5bca1fb873df4ff04fb302ec94680327370a12a11d8d3a696df61960d56
- stdout `736c5b64…52a1`, stderr empty

The receipt shows the lease was paid for and how long it ran. It does not prove
the computation was faithful.
```

Attach the full capsule JSON in a collapsed block when the thread is technical.
Never paste the `repro_token`: it is a read capability over the run's private
evidence, and a comment is forever.

When the receipt carries `failure_class: "interrupted"`, the first line changes
and the claim goes with it:

```markdown
Ran on a rented NVIDIA RTX 5880 Ada (trust class `open`) and was cut short: the
machine stopped answering after 192 s of a 900 s window. Billed 0.042624 USDG,
with 152 s held and not charged. The measurement is partial.
```

Do not lead with the number and add the interruption underneath. A reader who
stops at the first line has to have been told.

**To a research note.** Same three parts, plus the spec hash so a reader can pull
every run against the same spec with
`prism_gpu_receipts {"repro_spec_hash": "..."}`. Record the capsule beside the
result it supports. A bibliography at the end separates the claim from its
evidence.

## Verifying a receipt someone else cited

Run the bundled checker:

    python3 ${HERMES_SKILL_DIR}/scripts/verify_receipt.py <receipt_id_or_url>

It fetches the artifact, recomputes `receipt_hash` from the canonical payload,
reconciles the amounts, and prints the settlement transaction to check on-chain.
Standard library only, no wallet, no key.

Its exit code is the answer, so branch on it rather than on the output:

| Code | Meaning |
| --- | --- |
| 0 | Verified, and the run completed cleanly. Cite it. |
| 1 | A check failed. This is not evidence. |
| 2 | Wrong arguments. |
| 3 | Verified, and `failure_class` is set. Cite it only with the failure named. |

Code 3 covers both an interrupted run and a refunded one. Both are real
receipts, and neither is evidence that work finished.

`--self-test` runs the checks against two receipts pinned in the script, one
clean and one interrupted, and needs no network.

By hand, in order:

1. **Fetch the artifact** at `.../proof/receipts/<receipt_id>.json`. If it is not
   in the feed, the citation is unverifiable. Stop there.
2. **Check the outcome and the failure class together.** `finalized` with
   `failure_class: null` is a clean run. `finalized` with
   `failure_class: "interrupted"` ran and was cut short. `refunded` with a
   `failure_class` never ran, and the deposit came back. `disputed` receipts are
   never published as final proof.
3. **Reconcile the amounts.** `charged_base_units + refunded_base_units` must
   equal the deposit, and `provider_paid_base_units` must not exceed
   `charged_base_units`. A receipt that does not reconcile is not evidence.
4. **Recompute `receipt_hash`.** SHA-256 over the canonical payload. Three rules
   decide whether you reproduce it, and all three are easy to get wrong:
   - Field order is declaration order, never sorted:
     `receipt_id, lease_id, node_id_hash, gpu_model, runtime_seconds,
     charged_base_units, refunded_base_units, provider_paid_base_units,
     failure_class, outcome, trust_class, attestation, credited_seconds, repro`
   - Separators are compact. No space after `:` or `,`.
   - Absent optional fields are omitted, never `null`. `failure_class` is the
     exception and serializes as `null` when absent.

   `receipt_hash`, `transaction_hash`, `escrow_address` and `chain_lease_id` are
   excluded from the hash. Inside `repro` the order is
   `executor, token_hash, spec_hash, image_digest, command_hash, result_hash,
   stdout_hash, stderr_hash, report_hash, exit_code, expected_exit_code,
   succeeded, truncated`.
5. **Match it on-chain.** Load `transaction_hash` on Robinhood Chain (id 4663)
   and confirm the escrow at `escrow_address` emitted one `LeaseFinalized` whose
   `leaseId` equals `chain_lease_id`, whose `charged`, `providerPaid` and
   `refunded` equal the receipt's, and whose `receiptHash` equals the hash you
   just recomputed. `fee + providerPaid` must equal `charged`.
6. **Check the escrow is the one you expect.** Escrow counters restart after a
   deployment, so `chain_lease_id` alone is not globally unique. The identity is
   the pair `(escrow_address, chain_lease_id)`. The current escrow is
   `0xfd4228eeefc49e4b76a0cd40af9fdd546220b2fd`.

A refund carries `reasonHash` in place of a receipt hash, so its off-chain hash
is self-consistency evidence and never a value the chain committed to. Match a
refund on escrow, chain lease id, transaction outcome and refunded amount.

## What a verified receipt does and does not say

Says:

- A lease with this identity was funded and settled on chain 4663.
- It ran for this many seconds and charged this much.
- Whether it finished or was cut short, in `failure_class`.
- The renter was promised this `trust_class` when the quote was issued.
- For a repro run, the published hashes match a report signed by an enrolled node
  device key (`executor: "node"`) or by Prism's escrow gateway
  (`executor: "managed"`).

Does not say:

- That the computation was faithful. A signature establishes who asserted the
  result, and says nothing about whether the hardware computed it correctly.
- That the hardware was attested. No receipt on the current open class carries
  an attestation verdict.
- Who ran it. The feed is pseudonymous and publishes no wallet address.

## Pitfalls

- `lease_id` in a receipt is the escrow's on-chain lease id. The HTTP API keeps a
  separate counter for the same lease, and the two do not agree.
- `credited_seconds` counts seconds that were *never* charged. Reporting it as
  the metered time understates a run and makes the arithmetic stop reconciling:
  `charged_base_units` divides by `runtime_seconds` to give the rate.
- A receipt with a non-null `failure_class` still verifies. Verification and
  cleanliness are separate questions and the checker answers both.
- A spec hash matches every run of that spec, including other people's. Match on
  `repro.token_hash` together with `chain_lease_id` when you mean one run.
- Sorting keys before hashing produces a different digest and a false negative.
- A `200` from the feed on a URL you built by hand is not proof the receipt is
  the one being cited. Compare `receipt_id` in the body.
