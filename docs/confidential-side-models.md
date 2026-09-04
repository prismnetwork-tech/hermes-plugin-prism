# Confidential side models

Hermes runs small models behind the main one. Title generation reads the opening
of every session you start. Memory query rewriting reads the message you just
sent and turns it into a retrieval question. They are quiet, frequent, and by
default they go to a commercial API under an account in your name, on the same
key as everything else.

This plugin gives them somewhere else to go: a GPU enclave on Prism Network,
paid per generation from a wallet, with no account anywhere. The prompt is
encrypted to a key the enclave's hardware attestation commits to, so it is
readable only inside the enclave. Every generation returns a receipt.

Those two tasks are what fits the enclave today. Context compression does not,
for reasons worked out below.

## Install

The provider is a second plugin in this repository. Clone the repository once
and symlink the `inference` directory beside it, because Hermes discovers
plugins one directory deep under `~/.hermes/plugins/`:

```bash
git clone https://github.com/prismnetwork-tech/hermes-plugin-prism ~/.hermes/plugins/prism
ln -s ~/.hermes/plugins/prism/inference ~/.hermes/plugins/prism-inference
```

That is the whole install. A plugin manifest declaring `kind: model-provider`
is picked up by Hermes' provider discovery on its own, so `prism` appears in
`hermes model`, in `/model`, and in every auxiliary-task picker without an
enable step. Run `hermes plugins enable prism-inference` anyway if you want it
listed in `hermes plugins list` alongside the terminal backend.

The two plugins are independent. Install the provider on its own if you do not
want the GPU terminal backend; nothing here reads `terminal.prism`.

Requires hermes-agent v0.21.0 or later.

## Start the gateway

Hermes talks to Prism through a local OpenAI-compatible gateway. It holds the
wallet, quotes and pays for each generation, checks the attestation, and hands
back an ordinary chat completion.

```bash
export PRISM_AGENT_KEY=0x...      # the wallet that pays, funded with USDG and gas
npx -y prism-hermes
```

It listens on `127.0.0.1:8787` and prints its wallet address, its ceilings, and
whether attestation is being enforced. Leave it running. Hermes calls it the
next time it names a session or rewrites a memory query.

Without `PRISM_AGENT_KEY` it still answers `/healthz` and `/v1/models`, and
refuses generations with a 503 that says why. That is a useful state to install
in: you can see the catalog and the prices before any money is involved.

Fund the wallet with USDG for generations and a little ETH for gas on Robinhood
Chain (id 4663). The gateway's own ceilings are `PRISM_HERMES_MAX_USDG`
(0.05 per generation) and `PRISM_HERMES_DAILY_USDG` (1.00 per rolling day), both
enforced before anything is signed. The real ceiling is the wallet balance, so
fund it with what you are willing to lose.

## Point the side models at it

Hermes needs a placeholder credential to build a client. Prism authorises a
request by paying for it, so there is no provider key to hold. Put the
documented placeholder in `~/.hermes/.env`:

```
PRISM_INFERENCE_API_KEY=unused
```

Then route the two tasks that fit, in `~/.hermes/config.yaml`:

```yaml
auxiliary:
  title_generation:
    provider: prism
    model: openai/gpt-oss-20b
  memory_query_rewrite:
    provider: prism
    model: openai/gpt-oss-20b
```

or from the shell:

```bash
hermes config set auxiliary.title_generation.provider prism
hermes config set auxiliary.title_generation.model openai/gpt-oss-20b
hermes config set auxiliary.memory_query_rewrite.provider prism
hermes config set auxiliary.memory_query_rewrite.model openai/gpt-oss-20b
```

That is the whole supported routing. Do not add `auxiliary.compression` to it;
the next section is why.

Leave `model` out and Hermes uses the provider's default, `openai/gpt-oss-20b`,
the cheapest model in the catalog. On the latency-critical tasks Hermes asks the
profile instead, and the profile asks the running gateway which confidential
model is cheapest right now, so a retired model id is not a 404 you pay for.
Anything you do not set keeps using whatever it uses today.

`hermes model` lists Prism as a provider like any other, but the main agent is
not one of the things it fits. The gateway advertises a 4,096-token context
window and Hermes requires 64,000 for the model it runs the session on, so it
refuses to start: the session is rejected as it is created, before a prompt is
sent, with an error naming the model and both numbers. The 1024-token output cap
and the absence of tool calling would rule it out anyway.

### Why those two and not the others

The gateway accepts a 32 KiB request body, returns at most 1024 tokens, and
advertises a 4,096-token context window on `/v1/models`. Sealing the prompt to
the enclave roughly doubles the envelope, so budget around 16 KiB of plaintext.
Each side task either fits that or it does not, and the sizes are fixed in
Hermes:

| Task | Prompt Hermes sends | Output it asks for | Fits |
| --- | --- | --- | --- |
| `title_generation` | Up to 1,000 chars of the opening message | 64 tokens | Yes, with room to spare |
| `memory_query_rewrite` | Up to 4,000 chars plus a short instruction | 96 tokens | Yes |
| `delegate_task` | As large as the subtask makes it | Whatever the child agent needs | No, and the tools rule it out first |
| `compression` | Up to 160,000 chars in one request | Up to ~14,000 tokens | No |

The first two are the two routed in the config above, and neither carries a
context floor Hermes could object to. That is the whole supported list.

**Delegation is refused.** `delegate_task` spawns a full child agent with its own
toolset, and Prism's inference endpoint accepts a model, messages, and an output
cap. Nothing else. A request carrying `tools`, `tool_choice`, `response_format`,
`logprobs`, or `n` above 1 comes back as a 400 naming the field it refused.
Subagents belong on a provider that does tool calling.

Two more tasks come up, and neither routes here either.

**Session search has no model to route.** It stopped using an auxiliary model in
hermes-agent, and the `auxiliary.session_search` config block went with it.
There is nothing to point at Prism.

**Memory flush is the memory provider's own call.** Extraction at a session
boundary runs inside whichever memory provider you configured. The auxiliary
router never sees it, so there is no `auxiliary.*` key that moves it here. Check
that provider's own model setting.

### Context compression, and why it is not offered

Compression is the side task that reads the most, so it is the one people ask
for first. It does not work on this gateway today, and two separate limits stop
it.

Hermes sends one request per compaction. There is no chunking: the multi-call
digest loop was removed because it made up to 28 extra calls and pushed a
compaction to several minutes. What replaced it is a single prompt bounded at
160,000 characters, about 40,000 tokens, roughly ten times the plaintext this
endpoint accepts in a whole request body. The reply is a
narrative summary plus a detailed session log, budgeted to a 10,000-token
ceiling and about 4,000 more, against a 1024-token cap here. Hermes also sends
no output cap on the wire for that call by design, so there is no knob to turn
it down with.

Hermes never gets that far, because the window stops it first. For a custom
endpoint it takes the context window from that endpoint's own `/v1/models`, and
the gateway advertises 4,096 tokens for every confidential model and 8,192 for
the open tier. The floor for a compression model is 64,000.

Where that check lands matters, because it is not the startup check the main
model gets. Hermes probes the compression model the first time a session needs
to compact, so a session with `auxiliary.compression` on Prism opens normally,
runs while the transcript stays under the compaction trigger, and then fails on
the compaction itself with an error naming the model, its 4,096-token window and
the 64,000 floor. The misconfiguration surfaces long after you wrote it.

Overriding the detected value with `auxiliary.compression.context_length` above
the floor clears that check and buys nothing: the prompt is still several times
the body this endpoint accepts, so the compaction fails on the size limit
instead.

This becomes viable when the gateway advertises at least 64,000 tokens and the
endpoint takes a prompt of that size and returns a summary of several thousand
tokens. Until then, leave compression on whatever provider you use for the main
model.

## What the models cost

Prices are quoted per request and you pay for the output cap, not for the tokens
produced. Asking for fewer tokens costs less. This is the confidential tier as
Prism published it on 2026-09-04, cheapest first; `GET /v1/models` is free and
returns the current figures.

| Model | Most one generation can cost |
| --- | --- |
| `openai/gpt-oss-20b` | 0.011024 USDG |
| `deepseek/deepseek-v4-flash` | 0.012048 USDG |
| `openai/gpt-oss-120b` | 0.013072 USDG |
| `z-ai/glm-5.3-flash` | 0.013072 USDG |
| `phala/gemma-4-26b-a4b-uncensored` | 0.014096 USDG |
| `phala/qwen3.6-35b-a3b-uncensored` | 0.018192 USDG |
| `meta-llama/llama-3.3-70b-instruct` | 0.020240 USDG |
| `qwen/qwen3.8-27b` | 0.025360 USDG |
| `z-ai/glm-5.2` | 0.025360 USDG |

All nine run on Intel TDX with an NVIDIA GPU. The gateway also serves an open
tier at a lower price, where the host operator can read anything the workload
touches. This provider does not list those models. Picking Prism for a side
model is picking confidentiality, and a cheaper row with none of it does not
belong in the same menu.

## What "attestation verified before the prompt is sent" means

Two separate things happen, in this order.

Before the prompt leaves your machine, the gateway fetches the enclave's
attestation quote, checks the code behind it against the workload the Prism SDK
pins, and encrypts the prompt to the key set that quote commits to. A relay
offering a different key set does not get the prompt. This is the part that
protects the data, and it is finished before anything is transmitted.

After the answer arrives, the receipt is verified against the quote. Verification
is retried a few times, because the evidence comes from third parties whose
outages should not condemn a generation you have already paid for. If the verdict
is still anything other than `verified`, the gateway returns a 502 and withholds
the answer. The call was paid for and the ledger records it.

So a withheld generation costs money. That is the trade attestation asks you to
make: evidence about a specific machine can be unavailable when you need it, and
requiring it means accepting a paid 502 rather than an unverified answer. Set
`PRISM_HERMES_REQUIRE_ATTESTATION=0` to accept unverified output instead, and
understand that you are then trusting the relay.

Attestation says which code served the request on which hardware. It does not
audit Prism's billing, and it does not make a small model behave like a large
one.

## Checking it

`hermes doctor` runs the provider's own connectivity probe once
`PRISM_INFERENCE_API_KEY` is set: a green row means the gateway answered
`/v1/models` on 8787.

For the full picture the plugin ships four checks, which you can run directly:

```bash
python3 ~/.hermes/plugins/prism-inference/gateway.py
```

```
✓ Prism inference gateway    (serving http://127.0.0.1:8787)
✓ Prism wallet               (paying from 0xEcaa…1039)
✓ Prism attestation          (quote verified and the prompt sealed to the enclave before it is sent)
✓ Prism daily budget         (0.9448 USDG left of today's ceiling)
```

Those four rows are what `ProviderProfile.doctor_checks()` returns. Hermes calls
that hook for terminal backends today and not yet for model providers, which is
why the command above exists; when it does, the same rows appear in
`hermes doctor` with no change here.

`GET /v1/receipts` on the gateway shows what has been spent, on what, and
against which receipt and transaction.

## What is not offered

- **No token-by-token streaming.** `stream: true` is accepted and the wire format
  is what an OpenAI client expects, but Prism returns a whole generation at once,
  so the gateway replays it as a single content chunk followed by `[DONE]`.
- **No tools, no structured output.** `tools`, `functions`, `tool_choice`,
  `function_call`, `response_format` and `logprobs` are refused with a 400. This
  is what rules out delegation and the main tool-calling loop.
- **No vision.** The enclave serves text. Leave `auxiliary.vision` where it is.
- **No long output.** 1024 tokens, and you pay for the cap.
- **No long input.** The endpoint accepts a 32,768-byte request body, and
  sealing roughly doubles the envelope, so budget around 16 KiB of plaintext.
  That is a byte budget, so the token equivalent moves with the text. Treat
  4,000 tokens as the working figure.
- **No context compression, and no main agent.** The gateway advertises a
  4,096-token context window and Hermes requires 64,000 for both of those roles.
  A main model on it is rejected as the session is created; a compression model
  on it survives until the first compaction and fails there. The compression
  prompt is several times larger than the body this endpoint takes as well. See
  the section above.
- **No attested or confidential rented workspace.** Confidential *inference*
  through a Phala TEE is this product. It has no shell. The GPU leases the
  terminal backend rents are trust class `open`, where the host operator can read
  anything the workload touches, and no higher class is on offer today.
- **No account, and no support queue behind one.** The gateway pays a wallet and
  keeps a local ledger. If a generation is withheld, the receipt and transaction
  id in the response headers are what you have.

## Licence

Apache-2.0. Built by Prism Network. Not affiliated with Nous Research.
