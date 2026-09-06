# Community plugin index entry

`hermes plugins search` reads a single JSON catalog. An entry there is what
turns `hermes plugins install prism-gpu` into a working command and what makes
this plugin answer a search for "gpu". The entry below is ready to submit.

## Where it goes

- Repository: `NousResearch/hermes-plugin-index`
- File: `index.json`
- Insert as one object in the top-level `plugins` array
- Review covers the entry's metadata only, not the code

The canonical index URL compiled into Hermes is
`https://raw.githubusercontent.com/NousResearch/hermes-plugin-index/main/index.json`.
As of 2026-09-06 both that URL and the repository return 404, so there is
nowhere to file the pull request yet. Every Hermes install therefore falls back
to the five-entry seed catalog bundled inside the CLI, which is why
`hermes plugins search prism` reports `index source: seed`. That is a dead
upstream, not a rejection.

## The entry

```json
{
  "name": "prism-gpu",
  "description": "Terminal backend that runs the agent's shell commands on an NVIDIA GPU rented by the second from the agent's own wallet, under two spend caps the model cannot raise.",
  "author": "Prism Network",
  "tags": ["gpu", "nvidia", "cuda", "terminal", "compute", "usdg", "x402"],
  "repo": "prismnetwork-tech/hermes-plugin-prism",
  "ref": "e305f2af7947d90517113257342828a9baa67438",
  "homepage": "https://prismnetwork.tech",
  "capabilities": ["terminal"],
  "api_version": 1,
  "added_at": "2026-09-06"
}
```

`ref` pins the 0.3.0 release commit. `name` is the bare handle the index
resolves, and it is deliberately not `prism`: search scores a term found in the
name above one found in the tags, so the handle carries the hardware.

## Until the index exists

`plugins.index_url` overrides the catalog location, and a self-hosted file is a
first-class index:

```bash
hermes config set plugins.index_url <url of an index.json>
```

Explicit identifiers never touch the index. This keeps working regardless:

```bash
hermes plugins install prismnetwork-tech/hermes-plugin-prism --enable
```
</content>
</invoke>
