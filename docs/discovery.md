# Discovery: how someone finds this plugin

Two separate catalogs decide whether a Hermes user searching for "gpu" ever sees
this plugin, and neither of them is fed by pushing to this repository. This page
records what each one reads, what state we are in, and what is prepared.

Nothing here is required to install. Explicit identifiers bypass both catalogs
and work today:

```bash
hermes plugins install prismnetwork-tech/hermes-plugin-prism --enable
hermes skills install prismnetwork-tech/hermes-plugin-prism/prism-gpu-cuda-repro
```

## 1. The plugin index, behind `hermes plugins search`

`hermes plugins search` reads a single JSON catalog. Hermes compiles this URL in
as the default:

```
https://raw.githubusercontent.com/NousResearch/hermes-plugin-index/main/index.json
```

Checked on 2026-09-07, that URL and the `NousResearch/hermes-plugin-index`
repository both return 404, and the GitHub API reports the repository as Not
Found. Hermes falls back through remote index, then cached copy, then the
five-entry seed file bundled in the CLI, which is why
`hermes plugins search prism` reports `index source: seed`. There is nowhere to
file a pull request. The upstream is absent, not unwelcoming.

The entry is written and ready for the day the repository appears:

- Repository: `NousResearch/hermes-plugin-index`
- File: `index.json`
- Insert as one object in the top-level `plugins` array

```json
{
  "name": "prism-gpu",
  "description": "Terminal backend that runs the agent's shell commands on an NVIDIA GPU rented by the second from the agent's own wallet, under two spend caps the model cannot raise.",
  "author": "Prism Network",
  "tags": ["gpu", "nvidia", "cuda", "terminal", "compute", "usdg", "x402"],
  "repo": "prismnetwork-tech/hermes-plugin-prism",
  "homepage": "https://prismnetwork.tech",
  "capabilities": ["terminal"],
  "api_version": 1
}
```

`name` is the bare handle the index resolves. It is deliberately not `prism`:
that handle is already taken on the skills hub by an unrelated skill, and it
says nothing about the hardware. Add `ref` pinned to the `main` tip and
`added_at` on the day the entry is filed, rather than to a commit that will have
moved by then.

A self-hosted catalog is a first-class index in the meantime, and overrides the
default location:

```bash
hermes config set plugins.index_url <url of an index.json>
```

## 2. The skills hub, behind `hermes skills search`

`hermes skills search` queries several sources, and the community one is
skills.sh. skills.sh has no submission form, no registration endpoint, and no
pull request to open. Its public API is read-only, authenticated through Vercel
OIDC, and serves the catalog rather than accepting entries. The leaderboard the
catalog is drawn from is built from anonymous install telemetry reported by the
`skills` CLI when someone runs `npx skills add <owner/repo>`.

So the route onto skills.sh is installs, not a filing. The prerequisite is that
the repository resolves cleanly through that CLI, and it does. Checked on
2026-09-07:

```
$ npx skills add prismnetwork-tech/hermes-plugin-prism --list
Found 3 skills
  prism-gpu-compute       Rent an NVIDIA GPU by the second from an agent wallet.
  prism-gpu-cuda-repro    Run pinned CUDA on a rented NVIDIA GPU, cite the receipt.
  prism-gpu-receipts      Check and cite the receipt for a rented NVIDIA GPU run.
```

### Where we actually stand

skills.sh holds no entry for this repository. Its detail pages under
`skills.sh/prismnetwork-tech/hermes-plugin-prism/<skill>` render the same 404
body as a skill name invented on the spot, and `GET /api/search?q=prismnetwork`
returns zero results. This was equally true under the old skill names, so the
0.3.0 rename neither cost nor bought any measured discovery.

Hermes still resolves these skills, because it falls back to GitHub. Its own
resolution cache records the source:

```json
{"name": "prism-gpu-compute", "source": "github",
 "identifier": "prismnetwork-tech/hermes-plugin-prism/skills/prism-gpu-compute"}
```

The `Source: skills.sh` label and the detail-page link that `hermes skills
inspect` prints for these skills are built locally from the identifier. Neither
means the hub holds the entry.

### Open

Getting into the skills.sh catalog needs real installs through the `skills` CLI,
which is downstream of people knowing the plugin exists. Nothing in this
repository can force it. What this repository can do is already done: the skills
resolve through the CLI, they are named for the hardware, and their descriptions
lead with the work.
