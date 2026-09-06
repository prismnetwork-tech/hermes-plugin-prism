# Community plugin index entry

`hermes plugins search` reads a single JSON catalog. Only an entry in that
catalog can shorten the install to `hermes plugins install prism-gpu` or put
this plugin in front of someone searching for "gpu". The entry below is ready
to submit.

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
  "ref": "0da9167380d2c59c75a05c5e8fede61e755ebe3d",
  "homepage": "https://prismnetwork.tech",
  "capabilities": ["terminal"],
  "api_version": 1,
  "added_at": "2026-09-06"
}
```

`ref` pins a 0.3.0 commit so the entry names an exact tree rather than a moving
branch; refresh it to the `main` tip on the day the entry is filed. `name` is
the bare handle the index resolves, and it is deliberately not `prism`: that
handle is taken on the skills hub by an unrelated skill, and it says nothing
about the hardware.

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
