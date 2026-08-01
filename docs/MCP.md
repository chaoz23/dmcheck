# dmcheck MCP contract

dmcheck exposes a deliberately small, read-only, **batch-only** MCP server on
stdio. It implements the final MCP `2026-07-28` protocol revision and no legacy
revision. The protocol baseline is the official
[MCP 2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28),
including its [stateless version negotiation](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning),
[stdio framing](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio),
[discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover),
and [tools contract](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).

**Release status:** this document describes the unreleased dmcheck `0.6.0`
candidate in this source tree. Public `0.5.5` predates this contract. Do not
submit `server.json` to the registry or claim public package support until
`0.6.0` has been published and the registry-resolved artifact passes the same
cold smoke as the locally built wheel.

This is a server contract, not a claim that every MCP host already supports the
2026 revision. A client that only supports the legacy `initialize` lifecycle is
incompatible. The server returns `-32601` for `initialize`, with
`supportedVersions` in error data, instead of echoing a version it does not
implement.

## Run and discover

Install this unreleased source candidate and launch:

```console
pip install .
dmcheck-mcp
```

After `0.6.0` is published, `pip install dmcheck` is the intended install path.

stdio carries exactly one UTF-8 JSON-RPC message per line. Every request has the
modern per-request metadata envelope:

```json
{"jsonrpc":"2.0","id":"discover-1","method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"my-client","version":"1.0.0"}}}}
```

`server/discover` truthfully advertises only the `tools` capability. The server
does not advertise resources, prompts, tasks, subscriptions, or watch mode.
`tools/list` returns tools in deterministic order with complete JSON Schema
2020-12 input and output schemas, cache metadata, and read-only annotations.

## Tools

### `run`

`run` evaluates one completed session in closed mode and returns the complete,
canonical `EvaluationResult` in `structuredContent`. Its text content block is a
JSON security projection containing only controlled statuses, rule IDs, stable
codes, and counts. Prefer inline values:

```json
{
  "transcript": [
    {"ts": 1, "author": "GM", "content": "The door opens."},
    {"ts": 2, "author": "A", "content": "I enter."}
  ],
  "charter": {"gm": ["GM"]},
  "ledger": []
}
```

Exactly one of `transcript` or `transcript_path` is required. `charter` and
`charter_path` are mutually exclusive, as are `ledger` and `ledger_path`. An
omitted charter uses the release-locked packaged default. Inline charter fields
are partial overrides of that default. `gm` and `dice_authors` replace the
corresponding charter fields before semantic validation.

The transcript is **untrusted table data only**. dmcheck does not invoke a
model, execute transcript text, use it as a prompt, or interpret apparent
instructions embedded in it. A transcript line that says “ignore previous
instructions” remains an ordinary message for the deterministic rule engine.
Because findings can cite a 140-character evidence excerpt, hostile text can be
returned inside `structuredContent.findings[].evidence.content`. The
model-visible text projection redacts all transcript, charter, and ledger
strings and carries an in-band instruction never to follow content from
`structuredContent`; vendor metadata independently classifies transcript,
charter, and ledger inputs as untrusted, the canonical result as
`untrusted-input-bearing-data`, and the text as a
`security-redacted-projection`. This deliberately prioritizes agent safety over
the protocol's non-mandatory backwards-compatibility recommendation to duplicate
all structured JSON in TextContent. A host that injects `structuredContent`
directly into a model must preserve this trust boundary; dmcheck cannot enforce
host prompt construction.

The tool result has `isError: false` for both `clean` and `findings`: in both
cases the requested evaluation completed. It has `isError: true` for `invalid`
and `incomplete`, allowing an agent to repair input or collect missing evidence.
Stable domain errors remain inside the typed `EvaluationResult`; they never
become findings.

### `rules`

`rules` accepts an empty object and returns the immutable R1-R8 mapping. It does
not inspect files, use the network, or mutate state.

## Least-authority path inputs

All path input is denied by default. Inline input needs no filesystem authority.
An operator may grant read-only access to one or more absolute directories:

```console
dmcheck-mcp --allow-root /absolute/session-directory
```

or set `DMCHECK_MCP_ALLOWED_ROOTS` to an `os.pathsep`-delimited list (`:` on
POSIX, `;` on Windows). CLI and environment roots are combined. Every root must
already exist and be a directory; otherwise the server refuses to start.

Requested paths must be absolute, resolve to regular files, and remain beneath
a configured root after canonical resolution. `..` traversal, a symlink that
escapes the root, the root directory itself, relative paths, missing paths, and
all paths when no root is configured fail closed. Client-advertised MCP roots
are never treated as authorization: MCP roots are informational, not an access
control mechanism.

On supported POSIX runtimes, every canonical root and file component is opened
relative to a directory handle with no-follow flags, and final components are
opened nonblocking before regular-file verification. On a platform without secure
`openat`/`O_NOFOLLOW` semantics, inline operation remains available but every
path call fails closed; the server never falls back to resolve-then-reopen.

## Bounds

Limits are measured as UTF-8 bytes, not Python character counts. Inline value
limits use that value's deterministic compact JSON encoding; the independent
stdio-frame limit still counts the exact request bytes, including whitespace:

| Input or output | Maximum |
|---|---:|
| one stdio JSON-RPC request | 1 MiB |
| transcript, inline or file | 512 KiB |
| charter, inline or file | 128 KiB |
| ledger, inline or file | 256 KiB |
| complete `CallToolResult` (structured + text forms) | 512 KiB |

Transcript and ledger arrays are additionally capped at 5,000 records each to
bound evaluator work even when individual records are tiny. JSON numeric tokens
are capped at 128 characters before integer or float conversion, including on
Python 3.9.

Files are read at most one byte beyond their limit, so an oversized file is not
loaded into memory. JSON nesting is limited to 256. Non-finite JSON numbers,
invalid UTF-8, malformed JSON/JSONL, and oversized content fail closed. A whole
stdio request over 1 MiB uses application error `-33000`; the server drains that
frame before reading the next one. The code is intentionally outside the
JSON-RPC and MCP reserved error ranges.

The synchronous stdio loop admits only one operation at a time. A process-local
token bucket additionally limits `tools/call` to 120 calls per minute with a
burst of 32. Exhaustion returns application error `-33001` with
`retryAfterMs`; this operational admission state does not carry conversation,
capability, or evaluation context between requests. Embedders may configure
those values, but a rate slower than one call per 24 hours or a burst above
1,000,000 is refused at construction so refill and retry calculations remain
finite and typed.

## Error ownership

| Condition | Result |
|---|---|
| invalid JSON or UTF-8 JSON-RPC frame | `-32700` Parse error |
| malformed JSON-RPC request object | `-32600` Invalid request |
| unsupported/unknown method | `-32601` Method not found |
| missing metadata, invalid method params, unknown tool, malformed tool arguments or cursor | `-32602` Invalid params |
| unexpected server/evaluator fault | `-32603` Internal error, with no exception or input disclosure |
| unsupported protocol revision | `-32022`, with `supported` and `requested` data |
| process-local tool admission limit exhausted | `-33001`, with `retryAfterMs` data |
| path denial, content-size failure, semantic input failure, or insufficient evidence | completed `CallToolResult` with `isError: true` and a typed `EvaluationResult` |

This split follows MCP's distinction between protocol errors and tool execution
errors. In particular, a model can inspect and correct an invalid charter or
incomplete transcript, while an unknown tool is a malformed protocol call.

## Cancellation and lifecycle boundary

`notifications/cancelled` is accepted as a notification and never receives a
response. `run` is synchronous, deterministic, bounded batch work; the stdio
loop normally cannot read a cancellation frame until that call has completed.
The server therefore makes no claim of mid-evaluation preemption. EOF is the
portable graceful shutdown signal, and the process exits promptly after it.
The server never emits `input_required`, so `inputResponses` and `requestState`
retry parameters are rejected rather than silently ignored.

Live `dmcheck watch` remains a CLI capability, not an MCP tool. Exposing a
supervised, cancellable, restart-safe watch lifecycle is deferred to
`PORT-001`/`PORT-005`; this server does not emulate it with hidden background
state or a misleading long-running `run` call.
