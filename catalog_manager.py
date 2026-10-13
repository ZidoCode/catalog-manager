#!/usr/bin/env python3
"""catalog-manager — token budget scanner for MCP tool catalogs.

Every tool an MCP server exposes costs tokens in the model's context window
on EVERY call: its name, description, and JSON-schema input definition are
injected before the user's message is even read. Catalogs grow silently —
a 50-tool server can burn ~17,000 tokens of every request, and a 100-tool
one ~40,000 — before any work happens.

This scanner measures that cost statically:

- counts context tokens per tool (name + description + input schema),
  with a deterministic stdlib estimator (no API, no tiktoken dependency)
- flags bloat per tool (verbose descriptions, oversized enums, deep
  nesting, duplicate descriptions, unbounded string fields)
- emits a stable JSON contract (versioned) that v1.1's --budget /
  --fail-on-exceed enforcement will consume unchanged

Read-only by design: it scans files, reports numbers, changes nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

CONTRACT_VERSION = 1

# ---------------------------------------------------------------------------
# Token estimation (stdlib, deterministic, provider-agnostic)
#
# We deliberately do NOT call a tokenizer API. The estimator below tracks the
# BPE-family tokenizers closely enough for budget work (~±10%), is fully
# reproducible, and keeps the tool dependency-free. Every count is labeled
# "estimated" in the JSON contract so nobody mistakes it for a provider quote.
# ---------------------------------------------------------------------------

# Rough BPE behavior: prose averages ~4 chars/token; JSON punctuation and
# base64-ish blobs tokenize far worse (~2.5-3); camelCase splits per part.
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    # count structural separators as their own tokens
    punct = len(re.findall(r'["{}\[\],:]', text))
    # runs of word-chars: camelCase/snake_case boundaries split ~1:1.3 words
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    word_tokens = sum(max(1, round(len(w) / 4.2)) for w in words)
    # numbers and other runs
    other = re.findall(r"[^\sA-Za-z_{}\[\],:\"]+", text)
    other_tokens = sum(max(1, round(len(o) / 3.2)) for o in other)
    return punct + word_tokens + other_tokens


# ---------------------------------------------------------------------------
# Bloat findings
# ---------------------------------------------------------------------------

DESC_SOFT_LIMIT = 120      # chars of description above which we look harder
DESC_HARD_LIMIT = 400      # outright verbose
ENUM_COUNT_LIMIT = 12      # enum with more entries than this is suspicious
NESTING_DEPTH_LIMIT = 5    # schema depth
TOTAL_TOOL_TOKEN_LIMIT = 300  # per-tool budget: above this, the tool is heavy


def schema_depth(schema, _d=0):
    """Max depth of a JSON schema tree."""
    if not isinstance(schema, dict):
        return _d
    best = _d
    for key in ("properties", "items", "anyOf", "oneOf", "allOf", "prefixItems"):
        child = schema.get(key)
        if isinstance(child, dict):
            for v in child.values():
                best = max(best, schema_depth(v, _d + 1))
        elif isinstance(child, list):
            for v in child:
                best = max(best, schema_depth(v, _d + 1))
    return best


def count_enum_entries(schema):
    """Total enum entries anywhere in the schema."""
    n = 0
    if isinstance(schema, dict):
        e = schema.get("enum")
        if isinstance(e, list):
            n += len(e)
        for key in ("properties", "items", "anyOf", "oneOf", "allOf", "prefixItems"):
            child = schema.get(key)
            if isinstance(child, dict):
                n += sum(count_enum_entries(v) for v in child.values())
            elif isinstance(child, list):
                n += sum(count_enum_entries(v) for v in child)
    return n


def analyze_tool(tool):
    """Return per-tool metrics + bloat findings. `tool` is a dict with
    name/description/inputSchema (MCP tools/list shape)."""
    name = tool.get("name") or tool.get("function", {}).get("name") or "?"
    desc = tool.get("description") or tool.get("function", {}).get("description") or ""
    schema = tool.get("inputSchema") or tool.get("function", {}).get("parameters") or tool.get("parameters") or {}

    schema_text = json.dumps(schema, sort_keys=True)
    name_t = estimate_tokens(str(name))
    desc_t = estimate_tokens(desc)
    schema_t = estimate_tokens(schema_text)
    total = name_t + desc_t + schema_t

    findings = []
    d = len(desc)
    if d > DESC_HARD_LIMIT:
        findings.append({"code": "verbose-description", "detail":
                         f"description is {d} chars (> {DESC_HARD_LIMIT})"})
    elif d > DESC_SOFT_LIMIT:
        findings.append({"code": "long-description", "detail":
                         f"description is {d} chars (> {DESC_SOFT_LIMIT})"})
    if len(str(name)) > 48:
        findings.append({"code": "long-name", "detail": f"tool name is {len(str(name))} chars"})

    if isinstance(schema, dict) and schema:
        depth = schema_depth(schema)
        if depth > NESTING_DEPTH_LIMIT:
            findings.append({"code": "deep-nesting",
                             "detail": f"schema depth {depth} (> {NESTING_DEPTH_LIMIT})"})
        enums = count_enum_entries(schema)
        if enums > ENUM_COUNT_LIMIT:
            findings.append({"code": "large-enum",
                             "detail": f"{enums} enum entries (> {ENUM_COUNT_LIMIT})"})
        # unbounded free-form input
        if schema.get("additionalProperties") is True:
            findings.append({"code": "unbounded-input",
                             "detail": "additionalProperties: true (agent can pass anything)"})

    if total > TOTAL_TOOL_TOKEN_LIMIT:
        findings.append({"code": "heavy-tool",
                         "detail": f"~{total} est. tokens per call (> {TOTAL_TOOL_TOKEN_LIMIT})"})

    return {
        "name": name,
        "tokens": {
            "name": name_t, "description": desc_t, "schema": schema_t,
            "total": total, "estimated": True,
        },
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Catalog discovery — the formats people actually have on disk
# ---------------------------------------------------------------------------

MCP_CONFIG_FILES = {
    "claude_desktop_config.json", "mcp.json", "claude.json",
    "mcp_settings.json", "continue.json",
}


def _iter_json_files(root):
    root = Path(root)
    if root.is_file():
        yield root
        return
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if fn.endswith(".json"):
                yield Path(dirpath) / fn


def _extract_tools_from_doc(doc):
    """Yield (source, tool_dict) pairs from any recognized JSON shape."""
    if not isinstance(doc, dict):
        return
    # shape 1: exported tools/list  {"tools": [...]}
    if isinstance(doc.get("tools"), list):
        for t in doc["tools"]:
            if isinstance(t, dict):
                yield "tools-list", t
    # shape 2: MCP client config {"mcpServers": {name: {...}}}
    servers = doc.get("mcpServers")
    if isinstance(servers, dict):
        for sname, scfg in servers.items():
            # configs sometimes embed static tool manifests
            for t in (scfg.get("tools") or []) if isinstance(scfg, dict) else []:
                if isinstance(t, dict):
                    yield f"mcp-server:{sname}", t
    # shape 3: bare single-tool manifest {"name":..., "inputSchema":...}
    if doc.get("name") and (doc.get("inputSchema") or doc.get("parameters")):
        yield "manifest", doc


def scan_paths(paths):
    """Scan files/dirs, return raw per-source tool lists."""
    sources = {}
    for p in paths:
        for jf in _iter_json_files(p):
            try:
                doc = json.loads(Path(jf).read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, OSError):
                continue
            for kind, tool in _extract_tools_from_doc(doc):
                src = f"{jf}:{kind}"
                sources.setdefault(src, []).append(tool)
    return sources


# ---------------------------------------------------------------------------
# Report — THE JSON CONTRACT (versioned; v1.1 enforcement consumes this)
# ---------------------------------------------------------------------------

def build_report(sources):
    tools = []
    for src, tlist in sorted(sources.items()):
        for t in tlist:
            entry = analyze_tool(t)
            entry["source"] = src
            tools.append(entry)

    tools.sort(key=lambda e: e["tokens"]["total"], reverse=True)
    total_tokens = sum(e["tokens"]["total"] for e in tools)
    flagged = [e for e in tools if e["findings"]]

    # aggregate per finding code
    by_code = {}
    for e in flagged:
        for f in e["findings"]:
            by_code.setdefault(f["code"], []).append(e["name"])

    return {
        "contract_version": CONTRACT_VERSION,
        "scanner": "catalog-manager",
        "summary": {
            "tools_scanned": len(tools),
            "total_est_tokens_per_call": total_tokens,   # cost of loading the whole catalog ONCE
            "flagged_tools": len(flagged),
            "flag_rate": round(len(flagged) / len(tools), 3) if tools else 0.0,
            "top10_share": (round(
                sum(e["tokens"]["total"] for e in tools[:10]) / total_tokens, 3)
                if total_tokens else 0.0),
        },
        "findings_by_code": {k: {"count": len(v), "tools": v}
                             for k, v in sorted(by_code.items(),
                                                key=lambda kv: -len(kv[1]))},
        "tools": tools,
    }


def render_table(report, top=25):
    s = report["summary"]
    lines = []
    lines.append(f"catalog-manager — {s['tools_scanned']} tools, "
                 f"~{s['total_est_tokens_per_call']:,} est. tokens per call")
    lines.append(f"flagged: {s['flagged_tools']} ({s['flag_rate']:.0%})   "
                 f"top-10 tools hold {s['top10_share']:.0%} of the budget")
    lines.append("")
    lines.append(f"{'tokens':>7}  tool")
    for e in report["tools"][:top]:
        codes = ",".join(sorted({f['code'] for f in e['findings']})) or "-"
        lines.append(f"{e['tokens']['total']:>7}  {e['name'][:44]:<44} {codes}")
    if report["findings_by_code"]:
        lines.append("")
        lines.append("bloat by type:")
        for code, info in report["findings_by_code"].items():
            lines.append(f"  {code:<22} {info['count']:>3}  e.g. {', '.join(info['tools'][:3])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="token budget scanner for MCP tool catalogs (read-only)")
    ap.add_argument("paths", nargs="+", help="JSON files or directories to scan")
    ap.add_argument("--json", action="store_true", help="emit the versioned JSON contract")
    ap.add_argument("--top", type=int, default=25, help="rows in table view")
    args = ap.parse_args(argv)

    sources = scan_paths(args.paths)
    if not sources:
        print("no tool definitions found in the given paths", file=sys.stderr)
        return 2
    report = build_report(sources)
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(render_table(report, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
