"""dmcheck MCP server (stdio, stdlib-only). Tools: run, rules."""
import json
import sys

from . import RULES, check, load_charter, load_ledger, load_transcript

TOOLS = [
    {"name": "run",
     "description": "Referee a tabletop session transcript against a table charter. "
                    "Returns conduct findings (unanswered players, unconsumed rolls, "
                    "missing cues, spoiler leaks, dead air...), each citing the charter "
                    "rule violated. False accusations are treated as the worst bug: "
                    "ambiguity produces silence, not findings.",
     "inputSchema": {"type": "object", "properties": {
         "transcript_path": {"type": "string"},
         "charter_path": {"type": "string"},
         "ledger_path": {"type": "string"},
         "gm": {"type": "array", "items": {"type": "string"},
                "description": "GM author name(s); overrides charter.gm"}},
         "required": ["transcript_path"]}},
    {"name": "rules", "description": "The rule set with one-line definitions.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _call(name, args):
    if name == "rules":
        return RULES
    ch = load_charter(args.get("charter_path"))
    if args.get("gm"):
        ch["gm"] = args["gm"]
    t = load_transcript(args["transcript_path"])
    led = load_ledger(args.get("ledger_path"))
    findings, code = check(t, ch, led)
    return {"exit_code": code, "messages": len(t), "findings": findings}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method = req.get("id"), req.get("method")
        resp = {"jsonrpc": "2.0", "id": rid}
        try:
            if method == "initialize":
                resp["result"] = {
                    "protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "dmcheck", "version": "0.2.0"}}
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                resp["result"] = {"tools": TOOLS}
            elif method == "tools/call":
                p = req.get("params", {})
                out = _call(p.get("name"), p.get("arguments") or {})
                resp["result"] = {"content": [{"type": "text", "text": json.dumps(out, indent=1)}],
                                  "structuredContent": out if isinstance(out, dict) else None}
            else:
                if rid is None:
                    continue
                resp["error"] = {"code": -32601, "message": f"method not found: {method}"}
        except Exception as e:  # noqa: BLE001
            resp["error"] = {"code": -32000, "message": str(e)}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
