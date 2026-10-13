# catalog-manager

**Token budget scanner for MCP tool catalogs.** Dumb math, honest numbers, zero dependencies.

Every tool an MCP server exposes costs tokens on **every single call**: its name,
description, and JSON-schema input definition are injected into the model's context
before the user's message is even read. Catalogs grow silently — a 50-tool server can
burn ~17,000 tokens per request, a 100-tool one ~40,000 — and nobody notices because
the cost hides inside "model latency".

catalog-manager measures that cost statically:

```
$ catalog-manager ./mcp-catalog/

catalog-manager — 46 tools, ~3,744 est. tokens per call
flagged: 12 (26%)   top-10 tools hold 41% of the budget

 tokens  tool
    337  issue_write                                  heavy-tool
    188  add_issue_comment                            long-description
    156  sub_issue_write                              -
    140  create_or_update_file                        -
    131  push_files                                   -
    ...

bloat by type:
  long-description         8  e.g. add_issue_comment, list_repository_collaborators
  heavy-tool               1  e.g. issue_write
```

(That output is real: it is [github/github-mcp-server](https://github.com/github/github-mcp-server)'s
own tool definitions, scanned from source.)

## Why

Three failure modes show up again and again in production agent deployments:

1. **Cost before work** — tool schemas are re-sent on every call. A bloated catalog
   is a tax on every single request, even when the agent uses one tool.
2. **Hallucinated tool calls** — when dozens of similarly-named tools compete in
   context, models pick the wrong one more often.
3. **Slow first token** — the model must chew through the whole catalog before it
   can start answering. 40k tokens of schemas is real seconds.

None of these show up in a dashboard. They all show up in a static scan.

## What it checks

| finding | meaning |
|---|---|
| `verbose-description` | description > 400 chars — instruction payload territory |
| `long-description` | description > 120 chars — worth tightening |
| `long-name` | tool name > 48 chars |
| `deep-nesting` | schema nesting > 5 levels |
| `large-enum` | > 12 enum entries anywhere in the schema |
| `unbounded-input` | `additionalProperties: true` — the agent can pass anything |
| `heavy-tool` | tool alone costs > 300 est. tokens per call |

## The JSON contract

`--json` emits a **versioned contract** (`contract_version: 1`). v1.1's planned
enforcement (`--budget`, `--fail-on-exceed`) will consume exactly this structure —
so adopting the report today means zero migration when enforcement lands.

```json
{
 "contract_version": 1,
 "scanner": "catalog-manager",
 "summary": {
  "tools_scanned": 46,
  "total_est_tokens_per_call": 3744,
  "flagged_tools": 12,
  "flag_rate": 0.261,
  "top10_share": 0.41
 },
 "findings_by_code": { "long-description": {"count": 8, "tools": ["..."]} },
 "tools": [
  {"name": "issue_write",
   "tokens": {"name": 1, "description": 48, "schema": 288, "total": 337,
              "estimated": true},
   "findings": [{"code": "heavy-tool", "detail": "~337 est. tokens per call (> 300)"}],
   "source": "/path/tools.json:tools-list"}
 ]
}
```

## Accepted input formats

- Exported tool lists: `{"tools": [{"name", "description", "inputSchema"}]}`
- MCP client configs: `{"mcpServers": {"name": {..., "tools": [...]}}}`
- Single tool manifests: `{"name", "description", "inputSchema"}`
- Any directory — every `*.json` is tried, broken files are skipped silently

## Honest limitations

- **Token counts are estimates.** The estimator is deterministic stdlib math
  (±10% of BPE-family tokenizers), not a provider tokenizer — every count is
  labeled `estimated: true` in the contract. For invoices, use your provider's
  numbers; for finding *which* tools are the problem, ratios are enough.
- **Static scan of what's on disk.** It reads manifests and configs. It does not
  call servers, does not enumerate live `tools/list`, and does not judge whether
  a tool is *useful* — a 400-token tool you call hourly may be fine. It tells you
  where the tokens are; you decide what's worth it.
- **Read-only.** It changes nothing, phones nothing, and has zero dependencies
  beyond Python 3.9+.

## Roadmap

- **v1.1** — enforcement on the same JSON contract: `--budget N` (fail when the
  catalog exceeds N est. tokens), `--fail-on-exceed code` (CI gate), per-tool budgets.
- **v2** — MCP server interface (the scanner as a tool an agent can call), only if
  demand shows up.

## License

MIT — see [LICENSE](LICENSE).
