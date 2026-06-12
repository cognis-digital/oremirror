# Demo 01 — Mirroring two images across the air-gap (offline)

This scenario runs the full `oremirror` pipeline against bundled **offline
fixtures** (`fixtures.json`), so it works with zero network egress. The image
list (`images.txt` / `images.yaml`) names two images — `alpine:v3.20` and
`nginx:1.27` — that a disconnected site wants mirrored into its internal
registry.

## Run it

```bash
# 1. Plan: resolve refs to manifests and size the transfer.
python -m oremirror plan demos/01-basic/images.txt --fixtures demos/01-basic/fixtures.json

# 2. Pull: materialize the OCI image-layout you carry across the air-gap.
python -m oremirror pull demos/01-basic/images.txt --fixtures demos/01-basic/fixtures.json -o /tmp/carry

# 3. Verify: recompute every digest — confirm the carried artifact is intact.
python -m oremirror verify /tmp/carry

# 4. Push (dry-run): print the exact upload plan against the destination.
python -m oremirror push /tmp/carry --dest registry.internal:5000 --dry-run
```

## What it shows

| Step    | What happens                                                        |
|---------|---------------------------------------------------------------------|
| plan    | 2 images resolve to their manifests; 7 distinct blobs are summed.   |
| pull    | Manifests + config + layer blobs land under `blobs/sha256/`, plus an `index.json` annotated with each original ref. |
| verify  | Every blob is re-hashed; the layout reports **PASS**. Append a byte to any blob and it flips to **FAIL** (tamper detection). |
| push    | The dry-run lists each blob existence-check and manifest PUT it would issue — no network touched. Drop `--dry-run` (with egress) to actually upload. |

The fixtures are self-consistent: the manifest digests match the synthesized
blob bytes, so `verify` genuinely recomputes and confirms sha256 integrity
end-to-end without contacting a real registry.

## Real (online) use

With egress to the source registry, omit `--fixtures` and `oremirror` speaks
the live OCI distribution API (anonymous + bearer-token-challenge auth):

```bash
python -m oremirror pull images.txt -o ./carry
# physically move ./carry to the disconnected side, then:
python -m oremirror push ./carry --dest registry.internal:5000
```
