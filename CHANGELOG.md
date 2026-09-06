# Changelog

## 0.3.0 — 2026-09-07

### Skills renamed

The three bundled skills now carry the hardware in the name. A skill called
`prism` already exists on the hub and has nothing to do with GPUs, so the old
names competed with it while saying nothing about the work:

| Was | Now |
| --- | --- |
| `prism-compute` | `prism-gpu-compute` |
| `prism-cuda-repro` | `prism-gpu-cuda-repro` |
| `prism-receipts` | `prism-gpu-receipts` |

The old identifiers are retired, so anything that cited
`prismnetwork-tech/hermes-plugin-prism/prism-compute` needs the new name.
Published receipts are untouched by this. A receipt hashes lease and run fields
only, never a skill name or a repository path, so no receipt in the public feed
changes meaning because a directory moved.

The rename does not improve discovery today, and it is worth being exact about
that. skills.sh holds no entry for this repository under the old names or the
new ones, so a search for "gpu" or for "cuda" does not reach these skills and
did not reach them before. What still works, and always has, is the explicit
identifier, which Hermes resolves straight from GitHub:

```bash
hermes skills install prismnetwork-tech/hermes-plugin-prism/prism-gpu-cuda-repro
```

The names are now the ones worth being indexed under when that happens.
`docs/discovery.md` records how both catalogs are populated, what is filed, and
what is still open.

### One-line install

`hermes plugins install prismnetwork-tech/hermes-plugin-prism --enable` now
carries the whole install. The README leads with it, along with the terminal
backend and the 900-second tool ceiling the first command of a session needs.

### A quieter plugin scan

Hermes scans a plugin before it installs it. This release removes the reasons a
reviewer had to stop and read a line twice, without dropping anything the docs
said: install steps now go through `hermes plugins install` rather than a bare
clone, the SDK requirement is stated as a version range instead of a shell
command, the wallet key's location is described rather than pasted as a path
into the Hermes secrets file, and the inference gateway's loopback address lives
in one constant that the tests assert against.

The wallet key documentation now says plainly what the live suite has always
asserted: the key stays on the operator's machine, is stripped from every
command the model runs, and is never copied to the rented GPU.

## 0.2.0 and earlier

No changelog was kept. The commit history is the record.
