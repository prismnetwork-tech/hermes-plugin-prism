# Changelog

## 0.3.0 — 2026-09-06

### Skills renamed

The three bundled skills now carry the hardware in the name, because the hub
ranks a search on the name before the tags:

| Was | Now |
| --- | --- |
| `prism-compute` | `prism-gpu-compute` |
| `prism-cuda-repro` | `prism-gpu-cuda-repro` |
| `prism-receipts` | `prism-gpu-receipts` |

The old identifiers are retired. Anything that cited
`prismnetwork-tech/hermes-plugin-prism/prism-compute` needs the new name, and
receipts published before this release name the old paths inside their signed
artifact hashes; those capsules stand as they were signed.

Each description now leads with the hardware and the work, so that a search for
a GPU or for CUDA can reach the skill once the hub re-indexes. Until it does,
the exact identifier still resolves:
`hermes skills install prismnetwork-tech/hermes-plugin-prism/prism-gpu-cuda-repro`.

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
</content>
</invoke>
