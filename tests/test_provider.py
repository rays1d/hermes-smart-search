"""Result-mapping tests for the smart-search web provider.

No network and no keys: the CLI boundary (``provider._run``) is stubbed, so these
run in CI and on a bare checkout.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import provider as p  # noqa: E402

_REAL_RUN = p._run


def _stub(outputs: dict):
    """Patch ``provider._run`` with canned JSON per verb; return (patch, calls)."""
    calls = []

    def fake_run(args, *, timeout):
        calls.append(args)
        return 0, json.dumps(outputs[args[0]]), ""

    return fake_run, calls


class _ProviderTest(unittest.TestCase):
    def setUp(self):
        self.prov = p.SmartSearchProvider()
        self.addCleanup(setattr, p, "_run", _REAL_RUN)


class SearchMappingTests(_ProviderTest):
    def test_sources_merge_and_dedupe(self):
        fake, calls = _stub({
            "search": {
                "ok": True,
                "content": "",
                "primary_sources": [{"url": "https://a.example", "title": "A", "description": "alpha", "provider": "exa"}],
                "extra_sources": [
                    {"url": "https://b.example", "title": "B", "description": "beta", "provider": "tavily", "published_date": "2026-11-01"},
                    {"url": "https://a.example", "title": "dup", "provider": "tavily"},
                ],
            }
        })
        p._run = fake
        out = self.prov.search("q", 5)
        self.assertTrue(out["success"])
        web = out["data"]["web"]
        self.assertEqual([r["url"] for r in web], ["https://a.example", "https://b.example"])
        self.assertEqual(web[0]["position"], 1)
        self.assertIn("[exa]", web[0]["description"])
        self.assertIn("2026-11-01", web[1]["description"])
        self.assertEqual(web[0]["title"], "A")
        self.assertEqual(calls, [["search", "q"]])

    def test_limit_is_respected(self):
        fake, _ = _stub({"search": {"ok": True, "sources": [{"url": f"https://x{i}.example"} for i in range(10)]}})
        p._run = fake
        out = self.prov.search("q", 3)
        self.assertEqual(len(out["data"]["web"]), 3)

    def test_content_only_answer_falls_back_to_single_hit(self):
        fake, _ = _stub({"search": {"ok": True, "content": "answer body", "sources": []}})
        p._run = fake
        out = self.prov.search("q", 5)
        self.assertEqual(out["data"]["web"][0]["description"], "answer body")

    def test_cli_error_becomes_failure(self):
        fake, _ = _stub({"search": {"ok": False, "error_type": "config_error", "error": "No enabled configured channels"}})
        p._run = fake
        out = self.prov.search("q", 5)
        self.assertFalse(out["success"])
        self.assertIn("config_error", out["error"])

    def test_no_json_is_reported(self):
        p._run = lambda args, *, timeout: (1, "", "boom")
        out = self.prov.search("q", 1)
        self.assertFalse(out["success"])
        self.assertIn("boom", out["error"])


class FetchMappingTests(_ProviderTest):
    def test_markdown_title_and_body(self):
        fake, calls = _stub({"fetch": {"ok": True, "provider": "tavily", "content": "# Hello Page\n\nbody"}})
        p._run = fake
        docs = self.prov.extract(["https://b.example/y"])
        doc = docs[0]
        self.assertEqual(doc["title"], "Hello Page")
        self.assertEqual(doc["content"], "# Hello Page\n\nbody")
        self.assertEqual(doc["metadata"]["provider"], "tavily")
        self.assertEqual(calls, [["fetch", "https://b.example/y"]])

    def test_frontmatter_title(self):
        fake, _ = _stub({"fetch": {"ok": True, "content": "---\ntitle: From FM\n---\n\nbody"}})
        p._run = fake
        docs = self.prov.extract(["https://c.example"])
        self.assertEqual(docs[0]["title"], "From FM")

    def test_fetch_failure_is_per_url(self):
        fake, _ = _stub({"fetch": {"ok": False, "error_type": "network_error", "error": "all fetch providers failed"}})
        p._run = fake
        docs = self.prov.extract(["https://d.example"])
        self.assertIn("error", docs[0])
        self.assertIn("network_error", docs[0]["error"])


if __name__ == "__main__":
    unittest.main()
