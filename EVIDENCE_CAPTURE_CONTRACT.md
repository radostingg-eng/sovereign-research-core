# Consequential evidence capture

Schema-v4 cycles at or after `2026-09-19T04:00:00Z`, or cycles declaring
`evidence_coverage_schema_version: 1`, use `evidence_calls`.

Each row wraps an unchanged v4 call:

```json
{
  "producer": "portfolio",
  "projection": {
    "extractor": "json_pointer_v1",
    "bindings": [
      {
        "source_path": "/net_liquidation_value",
        "target_path": "/snapshot/net_liquidation_value"
      }
    ]
  },
  "call": {
    "tool_call_id": "stable-invocation-id",
    "kind": "connector_lookup",
    "tool": "Interactive Brokers (IBKR)",
    "call": {"action": "get_portfolio", "arguments": {}},
    "result": {"net_liquidation_value": 0},
    "provenance": {
      "result_origin": "connector_response",
      "observed_at": "2026-09-19T04:00:00Z",
      "source_refs": [],
      "capture": {
        "schema_version": 1,
        "representation": "canonical_response",
        "redactions": []
      },
      "web_sources": []
    }
  }
}
```

Producers are `portfolio`, `saved_instructions`, `account_orders`,
`account_trades`, and `market_sessions`. Capture empty connector results; do
not infer emptiness from an omitted call.

Connector responses bind raw result values to the accepted cycle through
`json_pointer_v1`. Values must match canonically and cannot be redacted.
Every present copy of order instructions must match the same captured result.

Host summaries use `projection: null`, retain a stable source locator, and are
explicitly not raw connector capture. Each market row cites its session-source
calls through `evidence_tool_call_ids`.

Repeated `tool_call_id` values are references to one invocation and therefore
must carry an identical nested call envelope. Different invocations use
different IDs.

Large connector responses use a single-level result reference:

```json
{"$artifact":{"schema_version":1,"sha256":"<64hex>","byte_length":123,
"media_type":"application/json"}}
```

Store the exact canonical JSON bytes at
`tool_artifacts/sha256/<first-two-hex>/<sha256>.json` in the same private
staging push. Missing, mutable, noncanonical, recursive, symlinked, legacy, or
oversized references fail before a receipt. The journal retains the reference,
not the response body.
