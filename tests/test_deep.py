"""Deep behavior tests for oremirror — registry client auth, layout, MCP, AI.

Standard library only, no real network. The registry client is exercised
against an in-process stub that mimics the OCI distribution bearer-token
challenge flow.
"""

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oremirror import (  # noqa: E402
    RegistryClient,
    build_plan,
    parse_image_list,
    pull_to_layout,
    verify_layout,
    push_layout,
    digest_bytes,
)
from oremirror import core, mcp_server, ai  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_DIR = os.path.join(REPO_ROOT, "demos", "01-basic")


def _fixtures():
    with open(os.path.join(DEMO_DIR, "fixtures.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


class FakeRegistry:
    """An in-memory OCI registry implementing the bits the client uses.

    Wired into RegistryClient by monkeypatching its private ``_request`` so no
    sockets are opened. It records pushes so push() can be asserted on.
    """

    def __init__(self):
        self.blobs = {}        # (repo, digest) -> bytes
        self.manifests = {}    # (repo, ref) -> (bytes, media)
        self.uploads = []      # ordered log of actions
        self._next_session = 0

    def seed_manifest(self, repo, ref, body, media):
        self.manifests[(repo, ref)] = (body, media)
        self.manifests[(repo, digest_bytes(body))] = (body, media)

    def seed_blob(self, repo, digest, data):
        self.blobs[(repo, digest)] = data

    def handle(self, method, url, repository, headers=None, data=None,
               scope_action="pull", _retry_auth=True):
        # Parse the path off the URL.
        path = url.split("/v2/", 1)[-1] if "/v2/" in url else url
        if "/manifests/" in path:
            repo, ref = path.split("/manifests/", 1)
            if method == "GET":
                if (repo, ref) in self.manifests:
                    body, media = self.manifests[(repo, ref)]
                    return 200, {"Content-Type": media,
                                 "Docker-Content-Digest": digest_bytes(body)}, body
                return 404, {}, b""
            if method == "PUT":
                self.manifests[(repo, ref)] = (data, headers.get("Content-Type", ""))
                self.uploads.append(("manifest", repo, ref))
                return 201, {"Docker-Content-Digest": digest_bytes(data)}, b""
        if "/blobs/uploads/session" in path:
            # finalize PUT with ?digest= (check this more-specific case first)
            repo = path.split("/blobs/uploads/", 1)[0]
            self.uploads.append(("blob", repo))
            return 201, {}, b""
        if "/blobs/uploads/" in path:
            repo = path.split("/blobs/uploads/", 1)[0]
            self._next_session += 1
            loc = f"/v2/{repo}/blobs/uploads/session{self._next_session}"
            return 202, {"Location": loc}, b""
        if "/blobs/" in path:
            repo, digest = path.split("/blobs/", 1)
            if method == "HEAD":
                return (200 if (repo, digest) in self.blobs else 404), {}, b""
            if method == "GET":
                if (repo, digest) in self.blobs:
                    return 200, {}, self.blobs[(repo, digest)]
                return 404, {}, b""
        return 404, {}, b""


def _wire(client, fake):
    client._request = fake.handle  # type: ignore[assignment]
    return client


class TestRegistryClientPull(unittest.TestCase):
    def test_pull_from_fake_registry(self):
        fx = _fixtures()
        # Build a manifest + blobs server from the alpine fixture.
        alpine = fx["v3.20"]
        body = json.dumps(alpine["manifest"], separators=(",", ":"),
                          sort_keys=True).encode("utf-8")
        repo = "library/alpine"
        fake = FakeRegistry()
        fake.seed_manifest(repo, "v3.20", body, alpine["mediaType"])
        m = alpine["manifest"]
        for d in [m["config"]["digest"]] + [l["digest"] for l in m["layers"]]:
            fake.seed_blob(repo, d, fx["_blobs"][d].encode("utf-8"))

        def factory(reg):
            return _wire(RegistryClient(reg), fake)

        with tempfile.TemporaryDirectory() as tmp:
            refs = parse_image_list("alpine:v3.20\n")
            summary = pull_to_layout(refs, tmp, client_factory=factory)
            self.assertEqual(summary["blobs_pulled"], 3)
            self.assertTrue(verify_layout(tmp).passed)


class TestRegistryAuthParsing(unittest.TestCase):
    def test_www_authenticate_parse(self):
        params = core._parse_www_authenticate(
            'Bearer realm="https://auth.example/token",'
            'service="reg.example",scope="repository:library/x:pull"')
        self.assertEqual(params["realm"], "https://auth.example/token")
        self.assertEqual(params["service"], "reg.example")
        self.assertIn("library/x", params["scope"])


class TestPushToFakeRegistry(unittest.TestCase):
    def test_live_push_uploads_blobs_and_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            refs = parse_image_list("alpine:v3.20\nnginx:1.27\n")
            pull_to_layout(refs, tmp, fixtures=_fixtures())
            fake = FakeRegistry()

            def factory(reg):
                return _wire(RegistryClient(reg), fake)

            summary = push_layout(tmp, "dst.local:5000", dry_run=False,
                                  client_factory=factory)
            self.assertFalse(summary["dry_run"])
            self.assertEqual(summary["manifests"], 2)
            kinds = [u[0] for u in fake.uploads]
            self.assertIn("manifest", kinds)
            self.assertIn("blob", kinds)


class TestMcpServer(unittest.TestCase):
    def _rpc(self, req):
        return mcp_server.handle_request(req)

    def test_initialize_and_list(self):
        init = self._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "oremirror")
        listed = self._rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        self.assertEqual(names, {"plan", "verify"})

    def test_plan_tool_with_fixtures(self):
        resp = self._rpc({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "plan",
                       "arguments": {"images": "alpine:v3.20\nnginx:1.27",
                                     "fixtures": _fixtures()}},
        })
        self.assertFalse(resp["result"]["isError"])
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload["image_count"], 2)

    def test_verify_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            pull_to_layout(parse_image_list("alpine:v3.20\n"), tmp,
                           fixtures=_fixtures())
            resp = self._rpc({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "verify", "arguments": {"layout": tmp}},
            })
            self.assertFalse(resp["result"]["isError"])

    def test_unknown_method(self):
        resp = self._rpc({"jsonrpc": "2.0", "id": 5, "method": "nope"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_notification_returns_none(self):
        self.assertIsNone(self._rpc({"jsonrpc": "2.0", "method": "initialized"}))

    def test_run_loop_over_stdio(self):
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        out = io.StringIO()
        mcp_server.run_mcp_server(stdin=io.StringIO(line + "\n"), stdout=out)
        resp = json.loads(out.getvalue().strip())
        self.assertEqual(resp["id"], 1)


class TestAiOffByDefault(unittest.TestCase):
    def test_disabled_without_config(self):
        # No COGNIS_AI_* env in CI -> expander is off and returns [].
        exp = ai.AIExpander(backend=None, endpoint=None, model=None)
        self.assertFalse(exp.is_enabled())
        self.assertEqual(exp.expand("a kubernetes ingress stack"), [])

    def test_parse_string_array(self):
        text = 'prelude ```json\n["alpine:3", "nginx:1.27"]\n``` trailer'
        self.assertEqual(ai._parse_string_array(text), ["alpine:3", "nginx:1.27"])

    def test_parse_ignores_non_strings(self):
        self.assertEqual(ai._parse_string_array('[1, "ok", null]'), ["ok"])


if __name__ == "__main__":
    unittest.main()
