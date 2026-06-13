# oremirror — OCI registry mirror/sync for disconnected environments

> Part of the **[Cognis Neural Suite](https://github.com/cognis-digital)** by [Cognis Digital](https://cognis.digital)
> Cognis Open Collaboration License (COCL) v1.0 · domain: `ops`

[![CI](https://github.com/cognis-digital/oremirror/actions/workflows/ci.yml/badge.svg)](https://github.com/cognis-digital/oremirror/actions)
[![License: COCL 1.0](https://img.shields.io/badge/License-COCL%201.0-2b6cb0.svg)](LICENSE)
[![Suite](https://img.shields.io/badge/Cognis-Neural%20Suite-6b46c1.svg)](https://github.com/cognis-digital)

**Plan, pull, verify, and push OCI images across the air-gap — with digest-pinned, integrity-verified transfers.**

`oremirror` is a single-purpose, dependency-free tool for moving container
images into disconnected or egress-limited sites. It speaks the OCI Distribution
HTTP API to resolve and download images into a portable **OCI image-layout**
directory (the artifact you physically carry across the gap), re-hashes every
blob to prove the carry is untampered, and replays the layout into a destination
registry on the far side.

Standard library only — `urllib`, `json`, `hashlib`, `os`. No pip dependencies,
no daemon, no external CLIs to shell out to.

## Why

Air-gapped and egress-limited environments (regulated networks, secure
enclaves, edge/disconnected Kubernetes) cannot pull images on demand. Teams
need a way to (1) enumerate exactly which images and blobs they must carry,
(2) move them as a verifiable bundle, and (3) load them into an internal
registry — without trusting that nothing was corrupted or swapped in transit.
`oremirror` does the four steps with content-addressed integrity at every hop.

<!-- cognis:domains:start -->
## Domains

**Primary domain:** AI & ML  ·  **JTF MERIDIAN division:** ATHENA-PRIME · SAGE

**Topics:** `cognis` `ai` `llm` `machine-learning`

Part of the **Cognis Neural Suite** — 300+ source-available tools organized across 12 domains under the JTF MERIDIAN command structure. See the [suite on GitHub](https://github.com/cognis-digital) and [jtf-meridian](https://github.com/cognis-digital/jtf-meridian) for how the pieces fit together.
<!-- cognis:domains:end -->

## Install

```bash
pip install -e ".[dev]"   # from this repo
# or just run it in place — there are no dependencies:
python -m oremirror --help
```

## Quick start

```bash
oremirror --version

# Resolve a list of images to a transfer plan (size, layers, distinct blobs).
oremirror plan images.txt
oremirror plan images.txt --format json

# Pull into a portable OCI image-layout directory (the air-gap artifact).
oremirror pull images.txt -o ./carry

# Recompute every digest and confirm the layout is intact.
oremirror verify ./carry

# Replay the layout into the internal registry (dry-run prints the plan first).
oremirror push ./carry --dest registry.internal:5000 --dry-run
oremirror push ./carry --dest registry.internal:5000

# Expose the safe, read-only slice (plan + verify) over MCP stdio.
oremirror mcp
```

### Offline demo (no network required)

```bash
oremirror plan   demos/01-basic/images.txt --fixtures demos/01-basic/fixtures.json
oremirror pull   demos/01-basic/images.txt --fixtures demos/01-basic/fixtures.json -o ./carry
oremirror verify ./carry
oremirror push   ./carry --dest registry.internal:5000 --dry-run
```

See [`demos/01-basic/SCENARIO.md`](demos/01-basic/SCENARIO.md) for the walkthrough.

## Image lists

Plain text (one ref per line, `#` comments) or a small YAML `images:` block:

```text
alpine:v3.20
nginx:1.27
ghcr.io/org/app@sha256:<64-hex>     # digest-pinned for reproducibility
myreg.local:5000/team/svc:v2
```

```yaml
images:
  - alpine:v3.20
  - nginx:1.27
```

## What each command does

- **plan** — resolves every ref to its manifest via the distribution API
  (anonymous + bearer-token-challenge auth for public registries), descends
  multi-arch indexes, sums config + layer blobs, and deduplicates shared blobs
  across images into one transfer figure. Table or JSON.
- **pull** — writes a spec-compliant OCI image-layout: `oci-layout`,
  `index.json` (one descriptor per requested ref, annotated with its original
  name), and every blob under `blobs/sha256/<hex>`. Already-present blobs are
  skipped, and each downloaded blob's digest is checked against its descriptor.
- **verify** — recomputes the sha256 of every manifest and blob the layout
  references, plus a sweep for orphan/tampered files whose content no longer
  matches their digest filename. Exits non-zero on any mismatch.
- **push** — for each manifest: `HEAD`s each blob, monolithically uploads the
  missing ones (`POST` upload session → `PUT ?digest=`), then `PUT`s the
  manifest. `--dry-run` prints the exact action list without any network I/O.

## Design improvements

- **Digest-pinned + integrity-verified transfers** — content addressing is
  enforced end to end; `verify` is a true tamper check, not a file-count.
- **MCP server** — `oremirror mcp` exposes the non-destructive `plan` and
  `verify` tools over stdio JSON-RPC for Cognis.Studio / Claude Desktop / Cursor.
- **Opt-in AI expansion (`--ai`, default OFF)** — with a *local* OpenAI-compatible
  endpoint configured via `COGNIS_AI_*`, `--ai --describe "<stack>"` proposes a
  concrete image list for a human to review. Off by default, never auto-mirrors,
  degrades to a no-op when no backend is configured.

## How it fits the Cognis Neural Suite

`oremirror` is one tool in the [Cognis Neural Suite](https://github.com/cognis-digital).
Every tool ships an MCP server so [Cognis.Studio](https://cognis.studio) agents
can call them as scoped capabilities.

**Sibling tools in `ops`:** [`admitd`](https://github.com/cognis-digital/admitd), [`airlock`](https://github.com/cognis-digital/airlock), [`k8scost`](https://github.com/cognis-digital/k8scost), [`otelbox`](https://github.com/cognis-digital/otelbox), [`statuskit`](https://github.com/cognis-digital/statuskit)

## Originality

100% original, clean-room implementation written against the public OCI
Distribution and OCI Image-Layout specifications. No third-party registry,
mirror, or transfer tool is forked, vendored, or wrapped, and no third-party
names, logos, or branding are used.

## License

Source-available under the **Cognis Open Collaboration License (COCL) v1.0** —
free for personal, internal-evaluation, research, and educational use;
**commercial / production use requires a license** (licensing@cognis.digital).
See [LICENSE](LICENSE).

## About

**[Cognis Digital](https://cognis.digital)** — Wyoming, USA · *Making Tomorrow Better Today: Advanced Cybersecurity, AI Innovation, and Blockchain Expertise.*
