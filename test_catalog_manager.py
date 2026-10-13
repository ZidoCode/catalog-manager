#!/usr/bin/env python3
"""Tests for catalog_manager. Run: python3 test_catalog_manager.py"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog_manager as cm


def tool(name, desc="Do a thing.", schema=None):
    t = {"name": name, "description": desc}
    if schema is not None:
        t["inputSchema"] = schema
    return t


class TestTokenEstimator(unittest.TestCase):
    def test_deterministic(self):
        a = cm.estimate_tokens("list_files reads the directory")
        b = cm.estimate_tokens("list_files reads the directory")
        self.assertEqual(a, b)
        self.assertGreater(a, 0)

    def test_zero(self):
        self.assertEqual(cm.estimate_tokens(""), 0)

    def test_monotonic(self):
        short = cm.estimate_tokens("get user")
        long = cm.estimate_tokens(
            "get_user_retrieves_the_complete_user_profile_record_including_all_fields")
        self.assertGreater(long, short)

    def test_json_punctuation_counted(self):
        # JSON schemas tokenize worse than prose — punctuation is real cost
        bare = cm.estimate_tokens("abcdef")
        jsoned = cm.estimate_tokens('{"a":"b","c":"d"}')
        self.assertGreater(jsoned, bare)


class TestAnalyzeTool(unittest.TestCase):
    def test_basic_tool_no_findings(self):
        r = cm.analyze_tool(tool("get_time", "Returns current UTC time.",
                                 {"type": "object",
                                  "properties": {"tz": {"type": "string"}}}))
        self.assertEqual(r["tokens"]["total"],
                         r["tokens"]["name"] + r["tokens"]["description"] + r["tokens"]["schema"])
        self.assertEqual(r["findings"], [])

    def test_verbose_description_flagged(self):
        r = cm.analyze_tool(tool("x", "word " * 200))
        codes = {f["code"] for f in r["findings"]}
        self.assertIn("verbose-description", codes)

    def test_large_enum_flagged(self):
        r = cm.analyze_tool(tool("x", "pick", {"type": "object", "properties": {
            "region": {"type": "string", "enum": [f"region-{i:03d}" for i in range(40)]}}}))
        codes = {f["code"] for f in r["findings"]}
        self.assertIn("large-enum", codes)

    def test_deep_nesting_flagged(self):
        deep = {"type": "object"}
        node = deep
        for _ in range(7):
            node["properties"] = {"child": {"type": "object"}}
            node = node["properties"]["child"]
        r = cm.analyze_tool(tool("x", "d", deep))
        codes = {f["code"] for f in r["findings"]}
        self.assertIn("deep-nesting", codes)

    def test_unbounded_input_flagged(self):
        r = cm.analyze_tool(tool("x", "u", {"type": "object", "additionalProperties": True}))
        codes = {f["code"] for f in r["findings"]}
        self.assertIn("unbounded-input", codes)

    def test_heavy_tool_flagged(self):
        r = cm.analyze_tool(tool("x", "word " * 150,
                                 {"type": "object", "properties": {
                                     f"field_{i}": {"type": "string",
                                                    "description": "a" * 60}
                                     for i in range(20)}}))
        codes = {f["code"] for f in r["findings"]}
        self.assertIn("heavy-tool", codes)

    def test_function_call_shape_supported(self):
        # OpenAI function-calling shape
        r = cm.analyze_tool({"type": "function", "function": {
            "name": "f1", "description": "hello",
            "parameters": {"type": "object", "properties": {}}}})
        self.assertEqual(r["name"], "f1")
        self.assertGreater(r["tokens"]["total"], 0)


class TestScanPaths(unittest.TestCase):
    def test_tools_list_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tools.json")
            json.dump({"tools": [tool("a"), tool("b")]}, open(p, "w"))
            srcs = cm.scan_paths([d])
            names = [t["name"] for tl in srcs.values() for t in tl]
            self.assertEqual(sorted(names), ["a", "b"])

    def test_mcp_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mcp.json")
            json.dump({"mcpServers": {"github": {"command": "npx",
                                                 "tools": [tool("gh_pr")]}}},
                      open(p, "w"))
            srcs = cm.scan_paths([d])
            names = [t["name"] for tl in srcs.values() for t in tl]
            self.assertEqual(names, ["gh_pr"])

    def test_skips_invalid_json_silently(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "broken.json"), "w").write("{nope")
            open(os.path.join(d, "good.json"), "w").write(
                json.dumps({"tools": [tool("k")]}))
            srcs = cm.scan_paths([d])
            all_names = [t["name"] for tl in srcs.values() for t in tl]
            self.assertEqual(all_names, ["k"])

    def test_skips_node_modules(self):
        with tempfile.TemporaryDirectory() as d:
            nm = os.path.join(d, "node_modules", "pkg")
            os.makedirs(nm)
            json.dump({"tools": [tool("junk")]}, open(os.path.join(nm, "t.json"), "w"))
            self.assertEqual(cm.scan_paths([d]), {})


class TestReportContract(unittest.TestCase):
    def test_contract_fields_present(self):
        rep = cm.build_report({"src": [tool("a"), tool("bb", "word " * 150)]})
        self.assertEqual(rep["contract_version"], 1)
        self.assertEqual(rep["scanner"], "catalog-manager")
        for k in ("tools_scanned", "total_est_tokens_per_call", "flagged_tools",
                  "flag_rate", "top10_share"):
            self.assertIn(k, rep["summary"])
        self.assertIn("findings_by_code", rep)
        # contract stability: every tool entry carries these keys
        for e in rep["tools"]:
            for k in ("name", "tokens", "findings", "source"):
                self.assertIn(k, e)
            self.assertTrue(e["tokens"]["estimated"])

    def test_sorted_desc_by_tokens(self):
        rep = cm.build_report({"src": [tool("small", "hi"),
                                       tool("big", "word " * 150)]})
        totals = [e["tokens"]["total"] for e in rep["tools"]]
        self.assertEqual(totals, sorted(totals, reverse=True))
        self.assertEqual(rep["tools"][0]["name"], "big")

    def test_findings_by_code_counts(self):
        rep = cm.build_report({"src": [tool("a", "word " * 150),
                                       tool("b", "word " * 160)]})
        info = rep["findings_by_code"].get("verbose-description")
        self.assertEqual(info["count"], 2)
        self.assertEqual(sorted(info["tools"]), ["a", "b"])

    def test_empty_catalog_handles(self):
        rep = cm.build_report({})
        self.assertEqual(rep["summary"]["tools_scanned"], 0)
        self.assertEqual(rep["summary"]["top10_share"], 0.0)


class TestTable(unittest.TestCase):
    def test_renders_without_crash(self):
        rep = cm.build_report({"src": [tool("a"), tool("b", "word " * 150)]})
        out = cm.render_table(rep)
        self.assertIn("catalog-manager", out)
        self.assertIn("verbose-description", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
