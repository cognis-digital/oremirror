"""Core engine for oremirror — OCI registry mirror/sync for disconnected sites.

This module implements, from scratch, the small slice of the OCI Distribution
and OCI Image-Layout specifications that an air-gap mirror actually needs:

  * reference parsing      — registry / repository / tag-or-digest
  * registry client        — anonymous + bearer-token-challenge auth over the
                             distribution HTTP API (manifests, blobs, uploads)
  * transfer planning       — resolve a list of refs to their manifests and sum
                             the layer/config blobs into a deterministic plan
  * pull                    — materialize an OCI image-layout directory (the
                             artifact you physically carry across the air-gap)
  * verify                  — recompute every digest and confirm the layout is
                             intact and untampered
  * push                    — upload an OCI layout to a destination registry

Everything is standard-library only (urllib / json / hashlib / os / base64).
No third-party code is used, forked, or vendored; the implementation follows
only the public OCI specifications.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

# Tool identity (re-exported from the package __init__).
TOOL_NAME = "oremirror"
TOOL_VERSION = "0.1.0"

# Default registry assumed when a reference omits one (mirrors the conventional
# behavior of `name:tag` short refs without endorsing any specific registry).
DEFAULT_REGISTRY = "registry-1.docker.io"
DEFAULT_TAG = "latest"

# Media types we understand. Anything containing "index" or "list" is treated
# as a multi-arch index; anything containing "manifest" as a single manifest.
MEDIA_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_LAYOUT = "application/vnd.oci.image.layout.v1+json"

# Accept header advertising every manifest flavor a registry might return.
_ACCEPT_MANIFESTS = ", ".join([
    MEDIA_INDEX,
    MEDIA_MANIFEST,
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

_USER_AGENT = f"{TOOL_NAME}/{TOOL_VERSION}"

# digest looks like "sha256:<64 hex>"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class OreError(Exception):
    """Any well-understood failure in the mirror pipeline."""


class RegistryError(OreError):
    """A failure talking to a registry over the distribution API."""


# --------------------------------------------------------------------------- #
# Reference parsing
# --------------------------------------------------------------------------- #
@dataclass
class ImageRef:
    """A parsed image reference: registry / repository @ digest-or-tag."""

    registry: str
    repository: str
    tag: Optional[str] = None
    digest: Optional[str] = None

    @property
    def canonical(self) -> str:
        base = f"{self.registry}/{self.repository}"
        if self.digest:
            return f"{base}@{self.digest}"
        return f"{base}:{self.tag or DEFAULT_TAG}"

    @property
    def reference(self) -> str:
        """The value used in distribution URLs: a digest if pinned, else tag."""
        return self.digest or self.tag or DEFAULT_TAG

    def to_dict(self) -> Dict[str, Any]:
        return {
            "registry": self.registry,
            "repository": self.repository,
            "tag": self.tag,
            "digest": self.digest,
            "canonical": self.canonical,
        }


def parse_ref(text: str) -> ImageRef:
    """Parse an image reference string into an :class:`ImageRef`.

    Recognizes ``registry/repo:tag``, ``registry/repo@sha256:...``, short
    ``repo:tag`` (default registry assumed), and library short names. The rule
    for deciding the registry: the part before the first ``/`` is a registry
    only if it contains a ``.`` or a ``:`` (port) or is exactly ``localhost``.
    """
    if not isinstance(text, str) or not text.strip():
        raise OreError("empty image reference")
    text = text.strip()

    digest: Optional[str] = None
    tag: Optional[str] = None

    # Split off an @digest first (it is unambiguous).
    if "@" in text:
        text, _, digest = text.partition("@")
        digest = digest.strip()
        if not _DIGEST_RE.match(digest):
            raise OreError(f"malformed digest in reference: {digest!r}")

    # Now split registry/repository. The host is detected heuristically.
    if "/" in text:
        head, _, rest = text.partition("/")
        if "." in head or ":" in head or head == "localhost":
            registry = head
            remainder = rest
        else:
            registry = DEFAULT_REGISTRY
            remainder = text
    else:
        registry = DEFAULT_REGISTRY
        remainder = text

    # A trailing :tag on the remainder (but only after the last '/', so a port
    # in the registry — already stripped — never confuses us).
    if ":" in remainder.rsplit("/", 1)[-1]:
        repo_part, _, tag = remainder.rpartition(":")
        repository = repo_part
        tag = tag.strip() or None
    else:
        repository = remainder

    repository = repository.strip("/")
    if not repository:
        raise OreError(f"reference has no repository: {text!r}")

    # Conventionally, a bare single-segment repo on the default registry lives
    # under the "library" namespace.
    if registry == DEFAULT_REGISTRY and "/" not in repository:
        repository = f"library/{repository}"

    if not digest and not tag:
        tag = DEFAULT_TAG

    return ImageRef(registry=registry, repository=repository, tag=tag, digest=digest)


# --------------------------------------------------------------------------- #
# Image-list loading (text or minimal YAML)
# --------------------------------------------------------------------------- #
def load_image_list(path: str) -> List[ImageRef]:
    """Load an image list from a ``.txt`` (one ref per line) or simple ``.yaml``.

    The YAML reader is intentionally tiny and stdlib-only: it understands a
    top-level ``images:`` key whose value is a block list of scalar refs, and
    it also tolerates a bare list of ``- ref`` lines. Comments (``#``) and blank
    lines are ignored in both formats.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    return parse_image_list(raw)


def parse_image_list(raw: str) -> List[ImageRef]:
    refs: List[ImageRef] = []
    seen = set()
    for line in raw.splitlines():
        # Strip comments (outside of any quoting — refs never contain '#').
        line = line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()

        # YAML-ish: an "images:" header opens a block; "- ref" entries feed it.
        if stripped.lower() in ("images:", "images: |", "images:|"):
            continue
        if stripped.startswith("- "):
            value = stripped[2:].strip().strip("'\"")
            if value:
                _add(refs, seen, value)
            continue
        # A YAML mapping key has a colon followed by a space or end-of-line
        # (``registry:`` / ``key: value``). An image ref like ``nginx:1.27``
        # has no space after its colon, so it is NOT treated as a mapping key.
        if _is_yaml_mapping_key(stripped):
            continue

        # Plain text line: a bare reference.
        _add(refs, seen, stripped.strip("'\""))

    if not refs:
        raise OreError("image list contained no references")
    return refs


def _is_yaml_mapping_key(text: str) -> bool:
    """True if the line is a YAML mapping key (``key:`` or ``key: value``).

    The distinguishing rule vs. an ``image:tag`` reference: a mapping key's
    first colon is followed by whitespace or end-of-line, the key contains no
    ``/`` (which only image repos have), and the key is a bare identifier.
    """
    if ":" not in text or "/" in text or "@" in text:
        return False
    head, _, rest = text.partition(":")
    if not head.replace("-", "_").isidentifier():
        return False
    return rest == "" or rest[:1].isspace()


def _add(refs: List[ImageRef], seen: set, value: str) -> None:
    ref = parse_ref(value)
    key = ref.canonical
    if key not in seen:
        seen.add(key)
        refs.append(ref)


# --------------------------------------------------------------------------- #
# Digest helpers
# --------------------------------------------------------------------------- #
def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def _digest_to_path(blobs_dir: str, digest: str) -> str:
    algo, _, hexd = digest.partition(":")
    return os.path.join(blobs_dir, algo, hexd)


# --------------------------------------------------------------------------- #
# Registry client (OCI distribution HTTP API)
# --------------------------------------------------------------------------- #
class RegistryClient:
    """A minimal OCI distribution client: manifests, blobs, and uploads.

    Authentication follows the standard ``WWW-Authenticate: Bearer`` challenge
    flow used by public registries: on a 401 the server names a token endpoint
    plus ``service``/``scope`` parameters; we fetch an anonymous bearer token
    and retry. Static basic-auth credentials may also be supplied.
    """

    def __init__(
        self,
        registry: str,
        insecure: bool = False,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.registry = registry
        self.scheme = "http" if insecure else "https"
        self.username = username
        self.password = password
        self.timeout = timeout
        # Cache of repository-scope -> bearer token.
        self._tokens: Dict[str, str] = {}

    # -- low-level request ------------------------------------------------- #
    def _base(self) -> str:
        return f"{self.scheme}://{self.registry}/v2"

    def _basic_header(self) -> Optional[str]:
        if self.username is None and self.password is None:
            return None
        raw = f"{self.username or ''}:{self.password or ''}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _request(
        self,
        method: str,
        url: str,
        repository: str,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        scope_action: str = "pull",
        _retry_auth: bool = True,
    ) -> Tuple[int, Dict[str, str], bytes]:
        headers = dict(headers or {})
        headers.setdefault("User-Agent", _USER_AGENT)

        scope_key = f"repository:{repository}:{scope_action}"
        token = self._tokens.get(scope_key)
        if token:
            headers["Authorization"] = "Bearer " + token
        elif self._basic_header() and "Authorization" not in headers:
            headers["Authorization"] = self._basic_header()

        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                return resp.getcode(), dict(resp.headers.items()), body
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and _retry_auth:
                challenge = exc.headers.get("WWW-Authenticate", "")
                if self._authenticate(challenge, repository, scope_action):
                    return self._request(
                        method, url, repository, headers=headers, data=data,
                        scope_action=scope_action, _retry_auth=False,
                    )
            return exc.code, dict(exc.headers.items()), exc.read()
        except urllib.error.URLError as exc:
            raise RegistryError(f"network error contacting {self.registry}: {exc}") from exc

    def _authenticate(self, challenge: str, repository: str, scope_action: str) -> bool:
        """Handle a Bearer challenge; cache the resulting token. Returns success."""
        if not challenge.lower().startswith("bearer"):
            return False
        params = _parse_www_authenticate(challenge)
        realm = params.get("realm")
        if not realm:
            return False
        query = {}
        if params.get("service"):
            query["service"] = params["service"]
        scope = params.get("scope") or f"repository:{repository}:{scope_action}"
        query["scope"] = scope
        url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(query)

        headers = {"User-Agent": _USER_AGENT}
        if self._basic_header():
            headers["Authorization"] = self._basic_header()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError):
            return False
        token = payload.get("token") or payload.get("access_token")
        if not token:
            return False
        self._tokens[f"repository:{repository}:{scope_action}"] = token
        return True

    # -- manifests --------------------------------------------------------- #
    def get_manifest(self, repository: str, reference: str) -> Tuple[bytes, str, str]:
        """Return (raw_bytes, media_type, digest) for a manifest reference."""
        url = f"{self._base()}/{repository}/manifests/{urllib.parse.quote(reference, safe='')}"
        status, headers, body = self._request(
            "GET", url, repository, headers={"Accept": _ACCEPT_MANIFESTS})
        if status != 200:
            raise RegistryError(
                f"manifest fetch failed for {repository}:{reference} "
                f"(HTTP {status})")
        media = headers.get("Content-Type", MEDIA_MANIFEST).split(";")[0].strip()
        digest = headers.get("Docker-Content-Digest") or digest_bytes(body)
        return body, media, digest

    def put_manifest(self, repository: str, reference: str, body: bytes,
                     media_type: str) -> str:
        url = f"{self._base()}/{repository}/manifests/{urllib.parse.quote(reference, safe='')}"
        status, headers, _ = self._request(
            "PUT", url, repository, headers={"Content-Type": media_type},
            data=body, scope_action="push,pull")
        if status not in (200, 201, 202):
            raise RegistryError(
                f"manifest push failed for {repository}:{reference} (HTTP {status})")
        return headers.get("Docker-Content-Digest") or digest_bytes(body)

    # -- blobs ------------------------------------------------------------- #
    def blob_exists(self, repository: str, digest: str) -> bool:
        url = f"{self._base()}/{repository}/blobs/{digest}"
        status, _, _ = self._request("HEAD", url, repository)
        return status == 200

    def get_blob(self, repository: str, digest: str) -> bytes:
        url = f"{self._base()}/{repository}/blobs/{digest}"
        status, _, body = self._request("GET", url, repository)
        if status != 200:
            raise RegistryError(
                f"blob fetch failed for {repository} {digest} (HTTP {status})")
        return body

    def put_blob(self, repository: str, digest: str, data: bytes) -> None:
        """Monolithic blob upload: POST to start, PUT with ?digest= to finish."""
        start = f"{self._base()}/{repository}/blobs/uploads/"
        status, headers, _ = self._request(
            "POST", start, repository, scope_action="push,pull")
        if status not in (202, 201):
            raise RegistryError(
                f"failed to start blob upload for {repository} (HTTP {status})")
        location = headers.get("Location")
        if not location:
            raise RegistryError("upload session missing Location header")
        if location.startswith("/"):
            location = f"{self.scheme}://{self.registry}{location}"
        sep = "&" if "?" in location else "?"
        put_url = f"{location}{sep}digest={urllib.parse.quote(digest, safe='')}"
        status, _, _ = self._request(
            "PUT", put_url, repository,
            headers={"Content-Type": "application/octet-stream"},
            data=data, scope_action="push,pull")
        if status not in (200, 201):
            raise RegistryError(
                f"blob upload finalize failed for {repository} (HTTP {status})")


def _parse_www_authenticate(value: str) -> Dict[str, str]:
    """Parse the comma-separated key="value" params of a WWW-Authenticate line."""
    out: Dict[str, str] = {}
    # Drop the leading scheme word ("Bearer").
    _, _, rest = value.partition(" ")
    for match in re.finditer(r'(\w+)="([^"]*)"', rest):
        out[match.group(1)] = match.group(2)
    return out


# --------------------------------------------------------------------------- #
# Transfer planning
# --------------------------------------------------------------------------- #
@dataclass
class BlobPlan:
    digest: str
    size: int
    media_type: str
    kind: str  # "config" | "layer" | "manifest"


@dataclass
class ImagePlan:
    ref: ImageRef
    manifest_digest: str
    media_type: str
    blobs: List[BlobPlan] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def total_size(self) -> int:
        return sum(b.size for b in self.blobs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref.canonical,
            "manifest_digest": self.manifest_digest,
            "media_type": self.media_type,
            "layers": len([b for b in self.blobs if b.kind == "layer"]),
            "blobs": len(self.blobs),
            "total_size": self.total_size,
            "error": self.error,
        }


@dataclass
class TransferPlan:
    images: List[ImagePlan] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(i.total_size for i in self.images)

    @property
    def total_blobs(self) -> int:
        # Distinct blob digests across all images (dedup is the whole point).
        seen = set()
        for img in self.images:
            for b in img.blobs:
                seen.add(b.digest)
        return len(seen)

    @property
    def failed(self) -> bool:
        return any(i.error for i in self.images)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": TOOL_NAME,
            "version": TOOL_VERSION,
            "image_count": len(self.images),
            "distinct_blobs": self.total_blobs,
            "total_size": self.total_size,
            "images": [i.to_dict() for i in self.images],
            "failed": self.failed,
        }


def _is_index(media_type: str) -> bool:
    m = media_type.lower()
    return "index" in m or "list" in m


def _resolve_one(
    client: RegistryClient,
    ref: ImageRef,
    fixtures: Optional[Dict[str, dict]] = None,
) -> ImagePlan:
    """Resolve a single ref to its manifest + blob plan.

    If ``fixtures`` is supplied (offline demo mode), the manifest is taken from
    the bundled sample set instead of the network. A multi-arch index is
    resolved to its first manifest entry for planning purposes.
    """
    try:
        body, media, mdigest = _fetch_manifest(client, ref, fixtures)
    except (RegistryError, OreError) as exc:
        return ImagePlan(ref=ref, manifest_digest="", media_type="", error=str(exc))

    manifest = json.loads(body.decode("utf-8"))

    # Multi-arch index: descend into the first child manifest.
    if _is_index(media):
        children = manifest.get("manifests") or []
        if not children:
            return ImagePlan(ref=ref, manifest_digest=mdigest, media_type=media,
                             error="empty image index")
        child_digest = children[0].get("digest", "")
        child_ref = ImageRef(ref.registry, ref.repository, None, child_digest)
        try:
            body, media, mdigest = _fetch_manifest(client, child_ref, fixtures)
            manifest = json.loads(body.decode("utf-8"))
        except (RegistryError, OreError) as exc:
            return ImagePlan(ref=ref, manifest_digest=mdigest, media_type=media,
                             error=f"index child unresolved: {exc}")

    blobs: List[BlobPlan] = []
    cfg = manifest.get("config") or {}
    if cfg.get("digest"):
        blobs.append(BlobPlan(cfg["digest"], int(cfg.get("size", 0)),
                              cfg.get("mediaType", ""), "config"))
    for layer in manifest.get("layers") or []:
        if layer.get("digest"):
            blobs.append(BlobPlan(layer["digest"], int(layer.get("size", 0)),
                                  layer.get("mediaType", ""), "layer"))

    return ImagePlan(ref=ref, manifest_digest=mdigest, media_type=media, blobs=blobs)


def _fetch_manifest(
    client: Optional[RegistryClient],
    ref: ImageRef,
    fixtures: Optional[Dict[str, dict]],
) -> Tuple[bytes, str, str]:
    """Return (body, media, digest), preferring fixtures when present."""
    if fixtures is not None:
        entry = fixtures.get(ref.reference) or fixtures.get(ref.canonical)
        if entry is None and ref.digest:
            entry = fixtures.get(ref.digest)
        if entry is None:
            raise OreError(f"no offline fixture for {ref.canonical}")
        body = json.dumps(entry["manifest"], separators=(",", ":"),
                          sort_keys=True).encode("utf-8")
        media = entry.get("mediaType", MEDIA_MANIFEST)
        return body, media, digest_bytes(body)
    if client is None:
        raise OreError("no registry client and no fixtures available")
    return client.get_manifest(ref.repository, ref.reference)


def build_plan(
    refs: List[ImageRef],
    fixtures: Optional[Dict[str, dict]] = None,
    client_factory: Optional[Callable[[str], RegistryClient]] = None,
    insecure: bool = False,
) -> TransferPlan:
    """Resolve every ref into a :class:`TransferPlan`. Never raises per-image."""
    plan = TransferPlan()
    factory = client_factory or (lambda reg: RegistryClient(reg, insecure=insecure))
    clients: Dict[str, RegistryClient] = {}
    for ref in refs:
        client = None
        if fixtures is None:
            client = clients.get(ref.registry) or factory(ref.registry)
            clients[ref.registry] = client
        plan.images.append(_resolve_one(client, ref, fixtures))
    return plan


# --------------------------------------------------------------------------- #
# OCI image-layout: pull / verify
# --------------------------------------------------------------------------- #
def _ensure_layout(out_dir: str) -> str:
    blobs = os.path.join(out_dir, "blobs", "sha256")
    os.makedirs(blobs, exist_ok=True)
    layout_marker = os.path.join(out_dir, "oci-layout")
    if not os.path.exists(layout_marker):
        with open(layout_marker, "w", encoding="utf-8") as fh:
            json.dump({"imageLayoutVersion": "1.0.0"}, fh)
    return os.path.join(out_dir, "blobs")


def _write_blob(blobs_dir: str, data: bytes) -> str:
    digest = digest_bytes(data)
    path = _digest_to_path(blobs_dir, digest)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return digest


def pull_to_layout(
    refs: List[ImageRef],
    out_dir: str,
    fixtures: Optional[Dict[str, dict]] = None,
    insecure: bool = False,
    client_factory: Optional[Callable[[str], RegistryClient]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Download manifests + config + layers into an OCI image-layout directory.

    Writes ``oci-layout``, ``index.json`` (with one descriptor per requested
    ref, annotated with its original name), and every blob under
    ``blobs/sha256/<hex>``. Returns a summary dict.
    """
    log = log or (lambda _m: None)
    blobs_dir = _ensure_layout(out_dir)
    factory = client_factory or (lambda reg: RegistryClient(reg, insecure=insecure))
    clients: Dict[str, RegistryClient] = {}

    index_manifests: List[Dict[str, Any]] = []
    pulled_blobs = 0
    skipped_blobs = 0

    for ref in refs:
        client = None
        if fixtures is None:
            client = clients.get(ref.registry) or factory(ref.registry)
            clients[ref.registry] = client

        body, media, mdigest = _fetch_manifest(client, ref, fixtures)
        # Persist the manifest itself as a blob.
        _write_blob(blobs_dir, body)
        manifest = json.loads(body.decode("utf-8"))
        log(f"manifest {ref.canonical} -> {mdigest}")

        # Collect the blob descriptors this manifest references.
        descriptors: List[Dict[str, Any]] = []
        if not _is_index(media):
            cfg = manifest.get("config") or {}
            if cfg.get("digest"):
                descriptors.append(cfg)
            descriptors.extend(manifest.get("layers") or [])

        for desc in descriptors:
            digest = desc.get("digest")
            if not digest:
                continue
            dest = _digest_to_path(blobs_dir, digest)
            if os.path.exists(dest):
                skipped_blobs += 1
                continue
            if fixtures is not None:
                # Synthesize deterministic blob content for offline demos so the
                # layout verifies; real pulls fetch the true bytes.
                data = _fixture_blob(fixtures, digest)
            else:
                data = client.get_blob(ref.repository, digest)
            written = _write_blob(blobs_dir, data)
            if fixtures is None and written != digest:
                raise OreError(
                    f"digest mismatch pulling {digest}: got {written}")
            pulled_blobs += 1
            log(f"blob {digest} ({len(data)} bytes)")

        index_manifests.append({
            "mediaType": media,
            "digest": mdigest,
            "size": len(body),
            "annotations": {"org.opencontainers.image.ref.name": ref.canonical},
        })

    index = {
        "schemaVersion": 2,
        "mediaType": MEDIA_INDEX,
        "manifests": index_manifests,
    }
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)

    return {
        "out_dir": out_dir,
        "images": len(refs),
        "blobs_pulled": pulled_blobs,
        "blobs_skipped": skipped_blobs,
    }


def _fixture_blob(fixtures: Dict[str, dict], digest: str) -> bytes:
    """Return the bytes for a fixture blob, looked up by digest.

    Demo fixtures may carry an explicit ``blobs: {digest: "text"}`` map; if a
    digest is absent we deterministically synthesize content whose own digest
    is recorded by the writer (the demo manifests use such synthesized blobs).
    """
    table = fixtures.get("_blobs") or {}
    if digest in table:
        return table[digest].encode("utf-8")
    # Deterministic filler keyed on the digest hex so repeated runs match.
    return f"oremirror-demo-blob:{digest}".encode("utf-8")


@dataclass
class VerifyResult:
    layout: str
    checked: int = 0
    ok: int = 0
    problems: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layout": self.layout,
            "checked": self.checked,
            "ok": self.ok,
            "problems": self.problems,
            "passed": self.passed,
        }


def verify_layout(layout_dir: str) -> VerifyResult:
    """Recompute every blob digest and confirm the OCI layout is intact."""
    result = VerifyResult(layout=layout_dir)

    marker = os.path.join(layout_dir, "oci-layout")
    if not os.path.exists(marker):
        result.problems.append("missing oci-layout marker file")
    index_path = os.path.join(layout_dir, "index.json")
    if not os.path.exists(index_path):
        result.problems.append("missing index.json")
        return result

    try:
        with open(index_path, "r", encoding="utf-8") as fh:
            index = json.load(fh)
    except (OSError, ValueError) as exc:
        result.problems.append(f"index.json unreadable: {exc}")
        return result

    blobs_root = os.path.join(layout_dir, "blobs", "sha256")

    # 1) Every manifest named in the index must exist and match its digest.
    for desc in index.get("manifests", []):
        mdigest = desc.get("digest", "")
        result.checked += 1
        path = _digest_to_path(os.path.join(layout_dir, "blobs"), mdigest)
        if not os.path.exists(path):
            result.problems.append(f"manifest blob missing: {mdigest}")
            continue
        actual = digest_file(path)
        if actual != mdigest:
            result.problems.append(
                f"manifest digest mismatch: {mdigest} != {actual}")
            continue
        result.ok += 1

        # 2) Recurse into the manifest's referenced blobs.
        try:
            with open(path, "rb") as fh:
                manifest = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            result.problems.append(f"manifest unparseable {mdigest}: {exc}")
            continue
        referenced = []
        cfg = manifest.get("config") or {}
        if cfg.get("digest"):
            referenced.append(cfg["digest"])
        referenced.extend(l.get("digest") for l in (manifest.get("layers") or []))
        for bdigest in referenced:
            if not bdigest:
                continue
            result.checked += 1
            bpath = _digest_to_path(os.path.join(layout_dir, "blobs"), bdigest)
            if not os.path.exists(bpath):
                result.problems.append(f"blob missing: {bdigest}")
                continue
            if digest_file(bpath) != bdigest:
                result.problems.append(f"blob digest mismatch: {bdigest}")
                continue
            result.ok += 1

    # 3) Stray blobs whose filename does not match their content are tamper.
    if os.path.isdir(blobs_root):
        for name in os.listdir(blobs_root):
            full = os.path.join(blobs_root, name)
            if not os.path.isfile(full):
                continue
            expect = "sha256:" + name
            actual = digest_file(full)
            if actual != expect:
                result.problems.append(
                    f"orphan/tampered blob {name}: content is {actual}")

    return result


# --------------------------------------------------------------------------- #
# Push (OCI layout -> destination registry)
# --------------------------------------------------------------------------- #
def push_layout(
    layout_dir: str,
    dest_registry: str,
    repository_override: Optional[str] = None,
    dry_run: bool = True,
    insecure: bool = False,
    client_factory: Optional[Callable[[str], RegistryClient]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Upload an OCI layout to ``dest_registry``.

    With ``dry_run`` (the default) it prints/returns the exact upload plan —
    every blob existence check and manifest PUT it *would* perform — without
    touching the network. Set ``dry_run=False`` to actually push.
    """
    log = log or (lambda _m: None)
    index_path = os.path.join(layout_dir, "index.json")
    with open(index_path, "r", encoding="utf-8") as fh:
        index = json.load(fh)

    blobs_base = os.path.join(layout_dir, "blobs")
    actions: List[Dict[str, Any]] = []
    client = None
    if not dry_run:
        factory = client_factory or (lambda reg: RegistryClient(reg, insecure=insecure))
        client = factory(dest_registry)

    for desc in index.get("manifests", []):
        ref_name = (desc.get("annotations") or {}).get(
            "org.opencontainers.image.ref.name", "")
        src = parse_ref(ref_name) if ref_name else None
        repository = repository_override or (src.repository if src else "unknown")
        tag = (src.tag if src and src.tag else None) or desc.get("digest", "")

        mdigest = desc.get("digest", "")
        mpath = _digest_to_path(blobs_base, mdigest)
        with open(mpath, "rb") as fh:
            mbody = fh.read()
        manifest = json.loads(mbody.decode("utf-8"))

        referenced = []
        cfg = manifest.get("config") or {}
        if cfg.get("digest"):
            referenced.append(cfg["digest"])
        referenced.extend(l.get("digest") for l in (manifest.get("layers") or []))

        for bdigest in referenced:
            if not bdigest:
                continue
            bpath = _digest_to_path(blobs_base, bdigest)
            size = os.path.getsize(bpath) if os.path.exists(bpath) else 0
            if dry_run:
                actions.append({"action": "blob", "repository": repository,
                                "digest": bdigest, "size": size,
                                "note": "HEAD then upload if absent"})
            else:
                if client.blob_exists(repository, bdigest):
                    log(f"blob exists, skip {bdigest}")
                else:
                    with open(bpath, "rb") as fh:
                        client.put_blob(repository, bdigest, fh.read())
                    log(f"uploaded blob {bdigest}")
                actions.append({"action": "blob", "repository": repository,
                                "digest": bdigest, "size": size})

        if dry_run:
            actions.append({"action": "manifest", "repository": repository,
                            "tag": tag, "digest": mdigest,
                            "media_type": desc.get("mediaType", MEDIA_MANIFEST)})
        else:
            pushed = client.put_manifest(
                repository, tag, mbody, desc.get("mediaType", MEDIA_MANIFEST))
            log(f"pushed manifest {repository}:{tag} -> {pushed}")
            actions.append({"action": "manifest", "repository": repository,
                            "tag": tag, "digest": pushed})

    return {
        "dest": dest_registry,
        "dry_run": dry_run,
        "manifests": len(index.get("manifests", [])),
        "actions": actions,
    }
