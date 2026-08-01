"""dmcheck MCP server (stdio, stdlib-only). Tools: run, rules."""
import json
import sys

from . import RULES, evaluate_paths
from .core import invalid_result
from .validation import InputValidationError, issue, parse_json_value

TOOLS = [
    {"name": "run",
     "description": "Referee a tabletop session transcript against a table charter. "
                    "Returns a typed clean/findings/invalid/incomplete result. Conduct "
                    "findings include unanswered players, unconsumed rolls, "
                    "missing cues, spoiler leaks, and dead air; each cites the charter "
                    "rule violated. False accusations are treated as the worst bug: "
                    "ambiguity produces silence, while unusable evidence fails closed. "
                    "Evaluates conduct and communication only; never decides whether "
                    "an action is legal.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {
         "transcript_path": {"type": "string"},
         "charter_path": {"type": "string"},
         "ledger_path": {"type": "string"},
         "gm": {"type": "array", "items": {"type": "string"},
                "description": "GM author name(s); overrides charter.gm"},
         "dice_authors": {"type": "array", "items": {"type": "string"},
                          "description": "Dice author name(s); overrides "
                                         "charter.dice_authors"}},
         "required": ["transcript_path"]}},
    {"name": "rules", "description": "The rule set with one-line definitions.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {}}},
]


def _call(name, args):
    if name == "rules":
        if args not in (None, {}):
            return invalid_result([
                issue("mcp.arguments_type", "/arguments",
                      "rules arguments must be an empty object")
            ]).to_dict()
        return RULES
    if name != "run":
        return invalid_result([
            issue("mcp.unknown_tool", "/name", "unknown tool %r" % name)
        ]).to_dict()
    if not isinstance(args, dict):
        return invalid_result([
            issue("mcp.arguments_type", "/arguments",
                  "tool arguments must be an object")
        ]).to_dict()
    unknown = set(args) - {"transcript_path", "charter_path", "ledger_path",
                           "gm", "dice_authors"}
    if unknown:
        key = sorted(unknown, key=str)[0]
        return invalid_result([
            issue("mcp.unknown_argument", "/" + str(key),
                  "unknown run argument %r" % key)
        ]).to_dict()
    path = args.get("transcript_path")
    if not isinstance(path, str) or not path.strip():
        return invalid_result([
            issue("input.path_required", "/transcript_path",
                  "transcript_path must be a nonempty string")
        ]).to_dict()
    for key in ("charter_path", "ledger_path"):
        value = args.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return invalid_result([
                issue("input.path_type", "/" + key,
                      "%s must be a nonempty string" % key)
            ]).to_dict()
    result = evaluate_paths(
        path, charter_path=args.get("charter_path"),
        ledger_path=args.get("ledger_path"), gm=args.get("gm"),
        dice_authors=args.get("dice_authors"),
        mode="closed")
    return result.to_dict()


def main():
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    for line in stream:
        if isinstance(line, bytes):
            try:
                line = line.decode("utf-8")
            except UnicodeDecodeError:
                resp = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700,
                                  "message": "request must be valid UTF-8"}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
                continue
        line = line.strip()
        if not line:
            continue
        try:
            req = parse_json_value(line, "/request")
        except InputValidationError:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(req, dict):
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "invalid request"}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
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
                out = _call(p.get("name"), p.get("arguments", {}))
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
