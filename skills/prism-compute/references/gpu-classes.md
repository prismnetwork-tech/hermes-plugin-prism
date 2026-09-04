# What is actually on offer

Two questions, two sources, and they answer different things.

- *What can I rent right now?* `prism_gpu_capacity` (no arguments, no wallet, no
  reservation) returns the live offer list.
- *What has the network actually run?* The public receipt feed at
  `https://api.prismnetwork.tech/proof/index.json`, one row per settled lease.

Quote the first when you are planning a purchase. Quote the second when someone
asks what Prism has done. Never quote one as the other.

## What is on offer right now

Two classes rotate through the live list: RTX A6000 and RTX 6000 Ada. Both
report 45 to 48 GiB of VRAM, CUDA 12 and trust class `open`, and both quote from
0.7992 USDG per hour. A single reading usually shows only one of them, split
across rows by VRAM.

A live reading on 2026-09-04:

| Model | VRAM | CUDA | Available | Managed repro | Starting rate | Best reliability |
| --- | --- | --- | --- | --- | --- | --- |
| RTX A6000 | 48 GiB | 12 | 2 | yes | 0.7992 USDG/hr | 71.4% |
| RTX A6000 | 45 GiB | 12 | 1 | yes | 0.7992 USDG/hr | 76.6% |

Three machines that day, all of one class. Supply of two or three at a time is
normal, and "no capacity, retry" is a normal answer with no fault behind it.

Every machine on offer is trust class `open`. Nothing on the live list is
attested or confidential.

`deviceRepro: false` across the list means a repro runs under the *managed*
executor: the report is signed by Prism's escrow gateway in place of an enrolled
node device key. Both are checkable signatures and neither proves faithful
computation. See `prism-receipts`.

`bestReliabilityPercent` is the supplier's historical completion record for that
class. In the 70s, expect a provisioning failure sometimes. It refunds in full,
so the cost is time.

## What the network has served

Six classes appear in the published feed. Counts are the 256 receipts in the
index read on 2026-09-04:

| Model | Settled leases | On offer today |
| --- | --- | --- |
| RTX A6000 | 92 | yes |
| RTX 6000 Ada | 63 | yes |
| RTX 5880 Ada | 52 | no |
| L40S | 31 | no |
| H100 PCIe | 15 | no |
| A40 | 3 | no |

The four classes with no live offer are supply that has come and gone. An H100
PCIe lease has settled on this network, and you still cannot rent one today. Say
it that way when it comes up. Promising a class off the feed is how a plan dies
at the funding step.

Of those 256 rows, 199 settled `finalized` and 57 settled `refunded`, every
refund carrying `failure_class: "provisioning_timeout"` and a zero charge. One
finalized row carries `failure_class: "interrupted"`. `prism-receipts` covers
what that means for a citation.

Every settled lease in the feed metered at 222 base units per second (0.7992
USDG per hour) except one RTX 6000 Ada lease at 177 (0.6372 per hour). Rates are
the supplier's, so read the live quote rather than assuming 222.

## Matching a floor to the list

Two settings express the same floor in different units:

- `terminal.prism.min_vram_mib` in Hermes config, default `16000`.
- `min_vram_gib` on `prism_gpu_capacity` and `prism_prepare_gpu_repro`.

The floor is a filter, not a request. Set it above 45 and the 45 GiB row stops
matching, which cuts a third of live supply for no gain. The CUDA repro spec
uses 45,056 MiB (44 GiB), which matches both rows.

Raise the floor only when the job genuinely will not fit. Lower it when the
answer is "no capacity" and the job is small.

## VRAM arithmetic

The numbers below are arithmetic on parameter counts. None of them is measured
from a Prism run. Use them to rule things out, then measure.

Weights alone, at 2 bytes per parameter for fp16 or bf16:

| Model size | fp16 weights | 8-bit | 4-bit |
| --- | --- | --- | --- |
| 3B | 6 GB | 3 GB | 1.5 GB |
| 7B | 14 GB | 7 GB | 3.5 GB |
| 8B | 16 GB | 8 GB | 4 GB |
| 13B | 26 GB | 13 GB | 6.5 GB |
| 34B | 68 GB | 34 GB | 17 GB |
| 70B | 140 GB | 70 GB | 35 GB |

Against roughly 46 GiB of usable VRAM on either class on offer:

| Job | Fits | Note |
| --- | --- | --- |
| Inference, 7B/8B fp16 | Comfortably | Room for a long KV cache. |
| Inference, 13B fp16 | Yes | Watch batch size and context length. |
| Inference, 34B 8-bit | Tight | Little room for cache. Prefer 4-bit. |
| Inference, 70B 4-bit | Very tight | 35 GB of weights leaves ~10 GiB. Assume it needs tuning. |
| Inference, 70B fp16 | No | Needs multi-GPU. Not on offer. |
| LoRA fine-tune, 7B fp16 base | Yes | Base weights plus adapters, gradients on the adapters only. |
| QLoRA, 13B | Yes | 4-bit base leaves plenty of activation headroom. |
| Full fine-tune, 7B, Adam | No | ~16 bytes per parameter with optimizer state is ~112 GB. |
| CUDA kernel work, custom ops | Yes | This is what these classes are best at. |

Add activations and the KV cache on top of weights. A long context or a large
batch can double the requirement, so leave headroom.

## What is not on offer

Say so plainly when asked. Do not quote a roadmap:

- No H100, A100 or multi-GPU node in the live class list. This describes what you
  can buy today. The feed shows H100 PCIe leases have already settled, so the
  history is a separate question with a different answer. Read
  `prism_gpu_capacity` before repeating either half.
- No attested or confidential rented workspace. Confidential *inference* through
  a Phala TEE exists and is a separate product with no shell.
- No persistent disk across leases. The rented disk dies with the lease and
  nothing is copied back.
- No pinned LoRA or vLLM serving image. The lease image defaults to the upstream
  Ollama image at a pinned digest, which Prism does not build. Bring a
  digest-pinned image of your own for anything else.
