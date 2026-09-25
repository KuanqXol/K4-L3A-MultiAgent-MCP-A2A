# L3A Architecture Record

## 1. System overview

```text
inputs/<case_id>.json
        │
        ▼
Coordinator ── discover names + input schemas ──► MCP Evidence Gateway
        │                                           │
        ├── Order/item agent ◄───────────────────────┤
        ├── Payment/refund agent ◄───────────────────┤
        ├── Shipment agent ◄─────────────────────────┤
        └── Policy agent ◄───────────────────────────┘
                    │
                    ▼
                 Verifier ──► outputs/<case_id>.json
                    │
                    └────────► traces/trace.jsonl
```

The coordinator treats the customer request as a claim, not as ground truth. It
discovers MCP tools and their advertised input schemas, executes bounded specialist
lookups, normalizes authoritative evidence, and sends only relevant evidence to the
verifier. The implementation recognizes semantic identifier and amount aliases; it
does not depend on Kaggle table names, row order, or sample values.

## 2. Agent ownership

| Actor | Input | Responsibility | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case envelope, discovered schemas | Extract candidate identifiers, route bounded work, assemble output | Specialist assignments; final output |
| Order/item | Order/item identifiers | Retrieve order state, item availability, totals, sellers | Normalized order/item evidence to verifier |
| Payment | Order/payment identifiers | Reconcile captures, split payments, duplicates, refunds | Payment/refund evidence and totals to verifier |
| Shipment | Order/shipment identifiers | Compare handoff and delivery milestones with promises | Delay attribution to verifier |
| Policy | Policy version and provisional issue | Retrieve applicable policy without inventing entitlements | Policy evidence to verifier |
| Verifier | Specialist handoffs | Check scope, evidence linkage, totals, actions, confidence, schema invariants | Verified decision to coordinator |

Agents only receive tools whose discovered name or schema identifies their domain.
Seller and policy calls are dependent calls: they run after upstream evidence has
provided a seller identifier or a provisional issue.

## 3. A2A protocol

The observable envelope is the public trace event: `case_id` is the correlation key;
`actor` owns the current step; `target` is the recipient; `decision_code` is a stable
machine-readable outcome; and `evidence_refs` contains only MCP-issued references.

The sequence is `case_received` (CLI), `task_assigned`, zero or more
`tool_result_consumed`, specialist `handoff`, `policy_decided`,
`verification_completed`, then `case_finalized` (CLI). A task has one handoff and is
never returned to the sender, so the graph is acyclic. Calls are sequential per case
to keep trace ordering deterministic. A transient timeout/connection failure receives
at most one idempotent retry; other failures are not retried.

Only observable event codes, counts, domains, and references are traced. Prompts,
customer secrets, raw evidence, and private reasoning are never written to the trace.

## 4. Evidence lifecycle

1. `EvidenceGateway` validates every response against
   `mcp-evidence-response-v1.schema.json` before returning it.
2. The workflow stores the immutable `evidence_ref`, returned domain, tool name, and
   data in case-local memory. References are deduplicated and never shared across
   calls to `solve_case`.
3. Each consumed result immediately emits `tool_result_consumed` with the original
   reference and tool name.
4. The classifier derives claims only from evidence data. Output references are
   filtered to domains that support the selected issue and capped by the contract.
5. The verifier handoff and `verification_completed` link the evidence used for the
   final decision. No reference is synthesized or transformed.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/connection | Once | Continue with other authoritative domains; lower result to insufficient evidence if necessary | No consumed event for a failed call |
| Not found/tool error | No | Preserve known identifiers; do not infer missing data | Assignment remains observable; no fake evidence |
| Source conflict | No | Leave source unselected and require investigation | Output `AUTHORITATIVE_SOURCES_CONFLICT` |
| Invalid MCP envelope | No | Gateway rejects it; exclude result | No `tool_result_consumed` |
| Missing required tool argument | No call | Skip inapplicable tool based on advertised schema | No assignment or evidence event |

Failures never turn customer text into facts. A case without usable authoritative
evidence is classified `insufficient_evidence`, with zero recommended refund.

## 6. Verification invariants

Before returning a result, the workflow enforces these construction invariants:

- output `case_id` equals input `case_id` and all evidence was fetched with that key;
- every output reference came from a validated response in the current invocation;
- identifiers are unique, non-empty, bounded, and originate in input or evidence;
- refund lines sum to `recommended_refund_brl`, use BRL, and never go below zero;
- `no_action` has zero refund and `NO_ACTION`; actionable issues have an action;
- seller responsibility uses an evidence-derived seller ID when available;
- cause ranks are contiguous and confidence stays in `[0, 1]`;
- trace evidence is a subset of consumed references and schema limits are respected;
- the CLI performs final public-schema validation before writing atomically.

## 7. Reproducibility

- Runtime: Python 3.11+, package constraints in `pyproject.toml`.
- Algorithm: deterministic rules; no model call, random seed, clock-based decision,
  or dependency on input file ordering. Trace IDs and timestamps are intentionally
  non-deterministic identifiers only.
- Concurrency: one case and one MCP call at a time; transient retry limit is one.
- Resource bounds: at most one call per applicable discovered tool in each phase,
  20 identifiers per entity type, 30 output references, and public schema caps.
- Commands: `python -m pip install -e ".[dev]"`, `pytest -q`, `ruff check .`,
  `day09 validate-inputs`, `day09 run`, `day09 validate`, and
  `day09 package --output dist/submission.zip`.
- Configuration comes from `.env`; API keys are never logged or packaged.
