---
name: prism-cuda-repro
description: Run and cite the audited Prism CUDA reproduction end to end.
license: Apache-2.0
version: 0.1.0
author: Prism Network <opensource@prismnetwork.tech>
metadata:
  hermes:
    tags:
      - gpu
      - cuda
      - reproducibility
      - evidence
      - prism
      - mcp
    category: compute
    related_skills:
      - prism-compute
      - prism-receipts
---

# The Prism CUDA repro rail

A repro run binds a digest-pinned image and a fixed command to a signed executor
report and a public settlement receipt, every link by hash. It is the only Prism
path where a third party can check the result against the payment that produced
it.

Follow the sequence exactly. It is the sequence the live server supports, and
the numbers in "What to expect" come from a settled mainnet run.

## When to use

- A CUDA result has to be reproducible by a third party.
- You want a receipt to attach to an issue, a report, or a claim.
- You are validating that the network still runs the audited workload.

Do not use this to run arbitrary work. For that, read `prism-compute` and pick
a purchase path.

## The MCP surface

Six read-only tools, on a hosted Streamable HTTP endpoint that needs no wallet
and no key:

    https://prismnetwork.tech/api/mcp

| Tool | Does |
| --- | --- |
| `prism_gpu_capacity` | Live classes and starting hourly rates. Optional `min_vram_gib`. |
| `prism_prepare_gpu_repro` | Binds image + command into a short-lived signed intent with a cost ceiling. |
| `prism_gpu_repro_status` | State and bounded result for one repro, by token. |
| `prism_gpu_repro_evidence` | The signed executor report for one repro, by token. |
| `prism_verify_gpu_repro` | The token, spec, command, signature, result and settlement checks. |
| `prism_gpu_receipts` | Public settlement receipts, filterable by `repro_spec_hash`. |

Every one of them is annotated `readOnlyHint: true`, `destructiveHint: false`.
**None of them can spend.** If a tool named `prism_prepare_gpu_repro` appears to
have created a lease, stop: the answer carries `lease_created: false` and
anything else means you are not talking to this server.

The six tools are absent from the `@prismnetwork/mcp` npm package. That package
is the wallet-side client for leases, inference and the vault, and it holds a
different set.

## The pinned spec

Do not paraphrase either value. The spec hash commits to both, and a changed
byte produces a different hash and a receipt nobody can match.

Image:

    registry.prismnetwork.tech/prism-cuda-vectoradd:vast-base-20260826@sha256:2e6d1873c8abd20d50dd311ac76324ef432c0a0396bd71b201b34c633e005930

Command:

    output=$(/usr/local/bin/prism-vectoradd 2>&1) || { code=$?; printf '%s\n' "$output"; exit "$code"; }; printf '%s\n' "$output"; case "$output" in *"Test PASSED"*) ;; *) exit 1 ;; esac

The command asserts the success marker itself, so a container that exits zero
without printing `Test PASSED` still fails. Assert the marker again on your side
when you read the result.

Other parameters: `duration_minutes: 30`, `min_vram_gib: 44` (45,056 MiB),
`expected_exit_code: 0`. `duration_minutes` accepts only 30, 60, 120 or 360.

The resulting spec hash for this exact spec is:

    77a4f656283268ae37b735b784eb6a0746cc14451b490eb86a14b60707a3dc75

If `prism_prepare_gpu_repro` returns a different `spec_hash`, you changed
something. Fix it before spending.

## Procedure

**1. Check capacity.**

    prism_gpu_capacity {"min_vram_gib": 44}

Require a row with `managedRepro: true` and `available >= 1`. If there is none,
stop and report "no capacity". Nothing has been spent and nothing needs undoing.

**2. Prepare the intent.**

    prism_prepare_gpu_repro {
      "image": "<the pinned image above>",
      "command": "<the pinned command above>",
      "duration_minutes": 30,
      "min_vram_gib": 44,
      "expected_exit_code": 0
    }

Keep the whole answer. `repro_token` is a 43-character base64url read
capability; it is the only handle to this run and it appears in no URL. Treat it
as a secret.

The intent lives 30 minutes. `issued_at` and `expires_at` are encoded in the
`approval_url` fragment if you need the exact deadline.

**3. Review the quote against policy. This is the decision point.**

Check all of these before asking anyone to fund anything:

- `lease_created` is `false`.
- `intent_version` is `prism.gpu-repro.intent.v2` and `estimated_executor` is
  `managed`.
- `spec_hash` matches the value above.
- `maximum_escrow` is at or below the operator's per-lease cap, and at or below
  0.5 USDG (500,000 base units), which is the audited ceiling for this rail.
- `maximum_escrow_usdg` renders the same number as `maximum_escrow`.
- `settlement` says Robinhood Chain, chain 4663, asset USDG, 6 decimals,
  contract `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168`.
- `approval_url` has origin `https://prismnetwork.tech`, path `/compute`, no
  query string, and exactly one `repro` parameter in the fragment.
- The rolling daily budget can absorb the deposit. Read `~/.prism/spend.json` or
  `prism_budget`.

The prepare answer is the whole of the evidence at this stage. Do not try to
confirm it with `prism_gpu_repro_status`: a repro has no readable state until
the operator's approval creates the lease, so a freshly prepared token answers
`not found` there, and so do the evidence and verify tools. That answer is
normal and means nothing has been funded yet. The three token tools start
answering after step 4.

Present the ceiling, the network, the duration, the image digest and the node to
the operator in those terms. Do not describe a ceiling as a price: the deposit
is what leaves the wallet, the charge is what it settles at.

**4. Get it funded.** Funding happens in the Prism web application, and nowhere
in this skill. The operator opens `approval_url`, reviews the quote, and
approves it. That approval sets an exact USDG allowance and calls `createLease`
on the escrow at `0xfD4228eEEfC49e4b76A0CD40af9fdd546220B2FD` for the quote you
reviewed.

There is no second path, and none of the six tools can create one. If the
operator has not approved, the run does not happen, and that is a normal outcome
to report.

**5. Wait for settlement.**

    prism_gpu_repro_status {"repro_token": "<token>"}

This is the first call that answers. Poll on an interval measured in seconds and
log each status change once. Terminal states other than `settled` are failures:
stop and report the status verbatim. When `result` appears, assert
`exit_code == 0`, `truncated == false`, and that `stdout` contains
`Test PASSED`.

A continued `not found` after the operator says they approved means the approval
never reached the escrow. Nothing has been charged. Ask them to open
`approval_url` again, or re-prepare if the intent has expired.

**6. Collect the evidence.**

    prism_gpu_repro_evidence {"repro_token": "<token>"}

Check the report before you believe it:

- `evidence.report.executor` is `managed` and `report.signer` is the escrow's
  current on-chain `gateway()`. Read `gateway()` yourself; do not take the
  report's word for its own signer.
- `report.outcome` is `completed` and `report.error` is `null`.
- `report.gpu_vram_mib` is at or above 45,056.
- `report.transport_host_key_sha256` is a well-formed digest.
- `report.signature` recovers to `report.signer`.

**7. Verify.**

    prism_verify_gpu_repro {"repro_token": "<token>"}

All eight of these must be `true`: `token_bound`, `spec_hash_valid`,
`command_bound`, `report_signature_valid`, `report_bound`,
`receipt_hash_valid`, `receipt_bound`, `expected_exit_code`.

`executor_identity_valid` is `null` on the managed executor by design. That is
not a pass. It means the gateway's identity has to be resolved on-chain
separately, which step 6 does by reading `gateway()`.

**8. Find the public receipt.**

    prism_gpu_receipts {"limit": 100, "repro_spec_hash": "<spec_hash>"}

The feed keeps every run against this spec, so the spec hash alone hands you
other people's runs: three receipts already carry `77a4f656…`. Match on
`repro.token_hash` *and* `chain_lease_id`. Exactly one row should match. More
than one means something is wrong; report it and cite nothing.

**9. Cite it.** Hand off to `prism-receipts` for the capsule shape and the
citation format.

## What to expect

From the settled reference run, chain lease 10:

| | |
| --- | --- |
| Class | RTX A6000, trust class `open` |
| VRAM floor | 45,056 MiB (44 GiB) |
| Deposit | 399,600 base units (0.3996 USDG), a 30-minute window |
| Runtime | 23 seconds |
| Charged | 5,106 base units (0.005106 USDG) |
| Refunded | 394,494 base units |
| Exit code | 0, `Test PASSED` in stdout |
| Receipt | `25dd3d12-bf11-843b-8770-3b5ba725cc97` |
| Settlement | `0x200bf5bca1fb873df4ff04fb302ec94680327370a12a11d8d3a696df61960d56` |

Wall-clock from funding to a settled receipt is dominated by provisioning; the
23 seconds of compute are a rounding error against it. Budget minutes.

## Failure modes

| Symptom | Meaning | Do |
| --- | --- | --- |
| `prism_gpu_capacity` returns nothing at 44 GiB | No supply right now | Report and stop. Nothing spent. |
| `spec_hash` differs from the value above | You edited the image or command | Fix the spec. Do not fund. |
| `maximum_escrow` above the cap | Rate moved, or the window is too long | Refuse. Report both numbers. |
| Status answers `not found` | Normal before approval; after it, the approval never landed | Before step 4, keep waiting. After it, ask for the approval again. Nothing spent either way. |
| Quote expires before funding | Review took too long, past the 30-minute window | Re-prepare from step 2. Nothing spent. |
| Status reaches a terminal failure | The run did not complete | Report the status. Settlement decides the charge. |
| Receipt shows `provisioning_timeout` | The machine never booted | Full refund, nothing owed. Re-quote. |
| Receipt shows `failure_class: "interrupted"` | The machine stopped answering mid-run | Partial charge stands, `credited_seconds` were not billed. Re-run, and never cite it as a clean reproduction. |
| `executor_identity_valid: null` | Expected for `managed` | Resolve `gateway()` on-chain instead. |
| Two receipts match the token hash | Something is wrong upstream | Cite nothing. Escalate. |

## After the run, write this down

Put these in memory so the next run is cheaper and quieter:

- Which class actually served it (`gpu_model` from the receipt) and its VRAM.
- The rate you were quoted and the settled charge, both in base units.
- The image digest and the `spec_hash`, so a later run can be compared without
  re-deriving them.
- The `chain_lease_id`, `receipt_id` and settlement transaction hash.
- Whether provisioning succeeded first time, and on which node reliability
  figure. A class in the low 70s will fail sometimes, so plan a retry.

Next run: prepare directly at the known floor and quote the operator a real
number from the last settled charge.
