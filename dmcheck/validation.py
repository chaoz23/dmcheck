"""Shared, dependency-free input normalization for every dmcheck surface.

The public evaluators fail closed and return typed outcomes.  These helpers
raise :class:`InputValidationError` only at the parsing boundary so CLI, MCP,
watch, and direct API adapters can all turn the same stable issues into the
same result envelope.
"""

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from importlib import resources


CHARTER_SCHEMA_VERSION = "1.0"
CHARTER_SCHEMA_ID = "https://github.com/chaoz23/dmcheck/schemas/charter-1.0.json"
RULE_IDS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8")
DEFAULT_CHARTER_RESOURCE = "default_charter.json"
DEFAULT_CHARTER_RELEASES = {
    ("1.0", "1.0"):
        "sha256:b90d27a7624c345117a80028e4e07429e15e714698dbf3b1650a323b830c0c34",
    ("1.0", "1.1"):
        "sha256:e3164d80180e6a5f24c5d2552a4e7901de28706f20f5ae03199457cd45709938",
}

_STRING_LIST_FIELDS = ("gm", "players", "dice_authors", "ooc_markers",
                       "hidden_terms")
_INTEGER_THRESHOLDS = ("answer_within_messages", "roll_ack_within_messages",
                       "cue_within_gm_messages", "quiet_table_max_messages")
_DURATION_THRESHOLDS = ("dead_air_seconds",)
_TOP_LEVEL_KEYS = {
    "$schema", "schema_version", "charter_version", "charter_digest",
    "effective_date", "description", "gm", "players", "dice_authors",
    "ooc_markers", "hidden_terms", "thresholds", "rules_enabled", "seats",
    "question_requires_gm_address", "dead_air_requires_quiet_table",
    "seat_quiet_gm_beats",
}


def _pointer_part(value):
    return str(value).replace("~", "~0").replace("/", "~1")


@dataclass(frozen=True)
class ValidationIssue:
    """A stable machine-readable validation failure."""

    code: str
    pointer: str
    message: str

    def to_dict(self):
        return {"code": self.code, "pointer": self.pointer,
                "message": self.message}


class InputValidationError(ValueError):
    """Carries one or more validation issues without losing JSON pointers."""

    def __init__(self, issues):
        self.issues = tuple(issues)
        super().__init__("; ".join(issue.message for issue in self.issues))


def issue(code, pointer, message):
    return ValidationIssue(code, pointer, message)


def _valid_unicode(value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@lru_cache(maxsize=1)
def _packaged_default_document():
    try:
        text = resources.files("dmcheck").joinpath(
            DEFAULT_CHARTER_RESOURCE).read_text(encoding="utf-8")
        value = parse_json_value(text, "/charter")
    except (AttributeError, OSError, UnicodeError,
            InputValidationError) as exc:
        raise InputValidationError([
            issue("charter.default_unavailable", "/charter",
                  "the packaged default charter is missing or unreadable")
        ]) from exc
    if not isinstance(value, dict):
        raise InputValidationError([
            issue("charter.default_invalid", "/charter",
                  "the packaged default charter must be a JSON object")
        ])
    actual_digest = canonical_charter_digest(value)
    declared_digest = value.get("charter_digest")
    release_key = (value.get("schema_version"), value.get("charter_version"))
    locked_digest = DEFAULT_CHARTER_RELEASES.get(release_key)
    if declared_digest != actual_digest:
        raise InputValidationError([
            issue("charter.default_digest_mismatch", "/charter_digest",
                  "the packaged default charter digest is invalid")
        ])
    if locked_digest != actual_digest:
        raise InputValidationError([
            issue("charter.default_migration_required", "/schema_version",
                  "a packaged default change requires a new schema or charter version")
        ])
    return value


def canonical_charter_digest(charter):
    """Digest the effective charter, excluding its digest and note-only keys."""
    payload = {key: value for key, value in charter.items()
               if key != "charter_digest" and not key.startswith("_")}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _string_list(value, field, problems, pointer=None):
    pointer = pointer or "/" + _pointer_part(field)
    if not isinstance(value, list):
        problems.append(issue("charter.type", pointer,
                              "%s must be an array of strings" % field))
        return []
    out = []
    seen = set()
    for index, item in enumerate(value):
        item_pointer = "%s/%d" % (pointer, index)
        if not isinstance(item, str):
            problems.append(issue("charter.type", item_pointer,
                                  "%s entries must be strings" % field))
            continue
        if not item.strip():
            problems.append(issue("charter.empty_string", item_pointer,
                                  "%s entries must be nonempty" % field))
            continue
        if not _valid_unicode(item):
            problems.append(issue("charter.unicode", item_pointer,
                                  "%s entries must contain valid Unicode" % field))
            continue
        folded = item.casefold()
        if folded in seen:
            problems.append(issue("charter.duplicate", item_pointer,
                                  "%s contains a duplicate value" % field))
            continue
        seen.add(folded)
        out.append(item)
    return out


def _positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _finite_nonnegative_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and value >= 0
    except (OverflowError, ValueError):
        return False


def normalize_charter(raw, require_gm=False, verify_digest=True):
    """Return the one effective charter shape or raise typed issues.

    Missing optional values inherit from the packaged default.  The packaged
    resource is always the base, including in a source checkout, so execution
    context cannot select a second default.
    """
    if not isinstance(raw, dict):
        raise InputValidationError([
            issue("charter.type", "/charter", "charter must be a JSON object")
        ])

    problems = []
    supplied = copy.deepcopy(raw)
    base = copy.deepcopy(_packaged_default_document())
    base.pop("charter_digest", None)
    charter = base

    for key, value in supplied.items():
        if key == "thresholds":
            continue
        if not isinstance(key, str):
            problems.append(issue("charter.key_type",
                                  "/" + _pointer_part(key),
                                  "charter field names must be strings"))
            continue
        if key not in _TOP_LEVEL_KEYS and not key.startswith("_"):
            problems.append(issue("charter.unknown_key",
                                  "/" + _pointer_part(key),
                                  "unknown charter field %r" % key))
            continue
        charter[key] = value

    default_thresholds = base.get("thresholds", {})
    supplied_thresholds = supplied.get("thresholds", {})
    if not isinstance(supplied_thresholds, dict):
        problems.append(issue("charter.type", "/thresholds",
                              "thresholds must be a JSON object"))
        supplied_thresholds = {}
    thresholds = copy.deepcopy(default_thresholds)
    for key, value in supplied_thresholds.items():
        if key not in _INTEGER_THRESHOLDS + _DURATION_THRESHOLDS:
            problems.append(issue("charter.unknown_threshold",
                                  "/thresholds/" + _pointer_part(key),
                                  "unknown threshold %r" % key))
            continue
        thresholds[key] = value
    charter["thresholds"] = thresholds

    if charter.get("$schema") != CHARTER_SCHEMA_ID:
        problems.append(issue("charter.schema", "/$schema",
                              "$schema must identify %s" % CHARTER_SCHEMA_ID))
    if charter.get("schema_version") != CHARTER_SCHEMA_VERSION:
        problems.append(issue(
            "charter.schema_version", "/schema_version",
            "schema_version must be %s" % CHARTER_SCHEMA_VERSION))
    version = charter.get("charter_version")
    if (not isinstance(version, str) or not version.strip()
            or not _valid_unicode(version)):
        problems.append(issue("charter.version", "/charter_version",
                              "charter_version must be a nonempty string"))
    description = charter.get("description")
    if (not isinstance(description, str) or not description.strip()
            or not _valid_unicode(description)):
        problems.append(issue("charter.type", "/description",
                              "description must be a nonempty string"))
    effective_date = charter.get("effective_date")
    if "effective_date" in supplied:
        if not isinstance(effective_date, str):
            problems.append(issue("charter.type", "/effective_date",
                                  "effective_date must be an ISO date string"))
        else:
            try:
                date.fromisoformat(effective_date)
            except ValueError:
                problems.append(issue("charter.date", "/effective_date",
                                      "effective_date must be an ISO date"))

    for field in _STRING_LIST_FIELDS:
        charter[field] = _string_list(charter.get(field), field, problems)
    if require_gm and not charter["gm"]:
        problems.append(issue("charter.gm.empty", "/gm",
                              "at least one GM author is required"))

    gm_names = {name.casefold() for name in charter["gm"]}
    for index, name in enumerate(charter["dice_authors"]):
        if name.casefold() in gm_names:
            problems.append(issue(
                "charter.author_collision", "/dice_authors/%d" % index,
                "a dice author cannot also be a GM author"))

    for name in _INTEGER_THRESHOLDS:
        value = thresholds.get(name)
        if not _positive_integer(value):
            problems.append(issue(
                "charter.integer_threshold", "/thresholds/" + name,
                "%s must be a positive integer; booleans are not integers"
                % name))
    for name in _DURATION_THRESHOLDS:
        value = thresholds.get(name)
        if not _finite_nonnegative_number(value):
            problems.append(issue(
                "charter.duration_threshold", "/thresholds/" + name,
                "%s must be a finite nonnegative duration" % name))

    for field in ("question_requires_gm_address",
                  "dead_air_requires_quiet_table"):
        if not isinstance(charter.get(field), bool):
            problems.append(issue("charter.boolean", "/" + field,
                                  "%s must be a boolean" % field))
    if not _positive_integer(charter.get("seat_quiet_gm_beats")):
        problems.append(issue(
            "charter.integer_threshold", "/seat_quiet_gm_beats",
            "seat_quiet_gm_beats must be a positive integer"))

    rules = charter.get("rules_enabled")
    if not isinstance(rules, list):
        problems.append(issue("charter.type", "/rules_enabled",
                              "rules_enabled must be an array of rule IDs"))
        rules = []
    normalized_rules = []
    seen_rules = set()
    for index, rule in enumerate(rules):
        pointer = "/rules_enabled/%d" % index
        if not isinstance(rule, str):
            problems.append(issue("charter.type", pointer,
                                  "rule IDs must be strings"))
        elif rule not in RULE_IDS:
            problems.append(issue("charter.unknown_rule", pointer,
                                  "unknown rule ID %r" % rule))
        elif rule in seen_rules:
            problems.append(issue("charter.duplicate", pointer,
                                  "rules_enabled contains a duplicate rule"))
        else:
            seen_rules.add(rule)
            normalized_rules.append(rule)
    charter["rules_enabled"] = normalized_rules

    seats = charter.get("seats")
    normalized_seats = {}
    identity_owner = {}
    if not isinstance(seats, dict):
        problems.append(issue("charter.type", "/seats",
                              "seats must be a JSON object"))
        seats = {}
    for seat_name, seat_config in seats.items():
        seat_pointer = "/seats/" + _pointer_part(seat_name)
        if (not isinstance(seat_name, str) or not seat_name.strip()
                or not _valid_unicode(seat_name)):
            problems.append(issue("charter.seat_name", seat_pointer,
                                  "seat names must be nonempty strings"))
            continue
        if not isinstance(seat_config, dict):
            problems.append(issue("charter.type", seat_pointer,
                                  "seat configuration must be an object"))
            continue
        unknown = set(seat_config) - {"cue_requires_mention", "mention", "aliases"}
        for key in sorted(unknown, key=str):
            problems.append(issue(
                "charter.unknown_seat_key", seat_pointer + "/" + _pointer_part(key),
                "unknown seat field %r" % key))
        cue_required = seat_config.get("cue_requires_mention", False)
        if not isinstance(cue_required, bool):
            problems.append(issue(
                "charter.boolean", seat_pointer + "/cue_requires_mention",
                "cue_requires_mention must be a boolean"))
            cue_required = False
        mention_supplied = "mention" in seat_config
        mention = seat_config.get("mention")
        if mention_supplied and (not isinstance(mention, str)
                                 or not mention.strip()
                                 or not _valid_unicode(mention)):
            problems.append(issue("charter.empty_string", seat_pointer + "/mention",
                                  "mention must be a nonempty string"))
            mention = None
        if cue_required and mention is None:
            problems.append(issue(
                "charter.mention_required", seat_pointer + "/mention",
                "mention is required when cue_requires_mention is true"))
        aliases = _string_list(seat_config.get("aliases", []), "aliases",
                               problems, seat_pointer + "/aliases")
        normalized_seats[seat_name] = {
            "cue_requires_mention": cue_required,
            "aliases": aliases,
        }
        if mention is not None:
            normalized_seats[seat_name]["mention"] = mention

        for alias_index, identity in enumerate([seat_name] + aliases):
            folded = identity.casefold()
            pointer = (seat_pointer if alias_index == 0 else
                       seat_pointer + "/aliases/%d" % (alias_index - 1))
            previous = identity_owner.get(folded)
            if previous is not None:
                problems.append(issue(
                    "charter.alias_collision", pointer,
                    "%r is already an identity for seat %r" %
                    (identity, previous)))
            else:
                identity_owner[folded] = seat_name
    charter["seats"] = normalized_seats

    digest_supplied = "charter_digest" in supplied
    provided_digest = supplied.get("charter_digest")
    if digest_supplied and not isinstance(provided_digest, str):
        problems.append(issue("charter.digest", "/charter_digest",
                              "charter_digest must be a sha256 string"))

    if problems:
        raise InputValidationError(problems)
    digest = canonical_charter_digest(charter)
    if verify_digest and provided_digest is not None and provided_digest != digest:
        raise InputValidationError([
            issue("charter.digest_mismatch", "/charter_digest",
                  "charter_digest does not match the effective charter")
        ])
    charter["charter_digest"] = digest
    return charter


def apply_charter_overrides(charter, gm=None, dice_authors=None):
    """Apply CLI/MCP overrides without carrying a stale effective digest."""
    value = copy.deepcopy(charter)
    value.pop("charter_digest", None)
    if gm is not None:
        value["gm"] = gm
    if dice_authors is not None:
        value["dice_authors"] = dice_authors
    return value


def _timestamp(value, pointer, problems):
    if value is None:
        return None
    if isinstance(value, bool):
        problems.append(issue("timestamp.type", pointer,
                              "timestamp must be a finite nonnegative epoch number "
                              "or timezone-aware ISO-8601 string"))
        return None
    if isinstance(value, (int, float)):
        try:
            finite = math.isfinite(float(value))
        except (OverflowError, ValueError):
            finite = False
        if not finite or value < 0:
            problems.append(issue("timestamp.range", pointer,
                                  "numeric timestamp must be finite and nonnegative"))
            return None
        return float(value)
    if not isinstance(value, str):
        problems.append(issue("timestamp.type", pointer,
                              "timestamp must be a number or ISO-8601 string"))
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        problems.append(issue("timestamp.format", pointer,
                              "timestamp must be valid ISO-8601"))
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        problems.append(issue("timestamp.timezone", pointer,
                              "ISO-8601 timestamp must include a timezone"))
        return None
    try:
        epoch = parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        problems.append(issue("timestamp.range", pointer,
                              "timestamp is outside the supported epoch range"))
        return None
    if not math.isfinite(epoch) or epoch < 0:
        problems.append(issue("timestamp.range", pointer,
                              "timestamp must identify a nonnegative epoch instant"))
        return None
    return epoch


def normalize_timestamp(value, pointer="/now"):
    problems = []
    parsed = _timestamp(value, pointer, problems)
    if problems:
        raise InputValidationError(problems)
    return parsed


def _message_author(value, pointer, problems):
    if isinstance(value, dict):
        value = value.get("username")
        pointer += "/username"
    if not isinstance(value, str):
        problems.append(issue("transcript.author_type", pointer,
                              "author must be a string or {username: string}"))
        return None
    if not value.strip():
        problems.append(issue("transcript.author_empty", pointer,
                              "author must be nonempty"))
        return None
    if not _valid_unicode(value):
        problems.append(issue("transcript.author_unicode", pointer,
                              "author must contain valid Unicode"))
        return None
    return value


def normalize_transcript(raw):
    if not isinstance(raw, list):
        raise InputValidationError([
            issue("transcript.type", "/transcript",
                  "transcript must be an array of message objects")
        ])
    problems = []
    rows = []
    for index, message in enumerate(raw):
        pointer = "/transcript/%d" % index
        if not isinstance(message, dict):
            problems.append(issue("transcript.message_type", pointer,
                                  "each transcript message must be an object"))
            continue
        author = _message_author(message.get("author"), pointer + "/author",
                                 problems)
        if "content" not in message:
            problems.append(issue("transcript.content_missing", pointer + "/content",
                                  "content is required"))
            content = None
        else:
            content = message.get("content")
            if not isinstance(content, str):
                problems.append(issue("transcript.content_type", pointer + "/content",
                                      "content must be a string"))
                content = None
            elif not _valid_unicode(content):
                problems.append(issue("transcript.content_unicode",
                                      pointer + "/content",
                                      "content must contain valid Unicode"))
                content = None

        has_ts = "ts" in message
        has_timestamp = "timestamp" in message
        ts_value = message.get("ts") if has_ts else message.get("timestamp")
        ts = _timestamp(ts_value, pointer + ("/ts" if has_ts else "/timestamp"),
                        problems)
        if has_ts and has_timestamp:
            other = _timestamp(message.get("timestamp"), pointer + "/timestamp",
                               problems)
            if ts != other:
                problems.append(issue("timestamp.conflict", pointer,
                                      "ts and timestamp identify different instants"))
        if author is not None and content is not None:
            rows.append({"ts": ts, "author": author, "content": content})

    if problems:
        raise InputValidationError(problems)

    timestamps = [row["ts"] for row in rows]
    present = [value for value in timestamps if value is not None]
    if len(present) > 1:
        ascending = all(a <= b for a, b in zip(present, present[1:]))
        descending = all(a >= b for a, b in zip(present, present[1:]))
        if len(present) == len(timestamps) and descending and not ascending:
            rows.reverse()
        elif not ascending:
            raise InputValidationError([
                issue("transcript.timestamp_order", "/transcript",
                      "timestamps must be chronological; a fully timestamped "
                      "transcript may instead be reverse chronological")
            ])
    for index, row in enumerate(rows):
        row["i"] = index
    return rows


def normalize_ledger(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InputValidationError([
            issue("ledger.type", "/ledger",
                  "ledger must be an array of event objects")
        ])
    problems = []
    rows = []
    for index, event in enumerate(raw):
        pointer = "/ledger/%d" % index
        if not isinstance(event, dict):
            problems.append(issue("ledger.event_type", pointer,
                                  "each ledger event must be an object"))
            continue
        kind = event.get("type")
        if not isinstance(kind, str) or kind not in ("turn", "act", "event"):
            problems.append(issue("ledger.kind", pointer + "/type",
                                  "type must be turn, act, or event"))
        actor = event.get("actor")
        if actor is not None and (not isinstance(actor, str) or not actor.strip()
                                  or not _valid_unicode(actor)):
            problems.append(issue("ledger.actor", pointer + "/actor",
                                  "actor must be a nonempty string when present"))
            actor = None
        text = event.get("text")
        if text is not None and (not isinstance(text, str)
                                 or not _valid_unicode(text)):
            problems.append(issue("ledger.text", pointer + "/text",
                                  "text must be a string when present"))
            text = None
        ts = _timestamp(event.get("ts"), pointer + "/ts", problems)
        normalized = {"ts": ts, "type": kind}
        if actor is not None:
            normalized["actor"] = actor
        if text is not None:
            normalized["text"] = text
        rows.append(normalized)
    if problems:
        raise InputValidationError(problems)
    return rows


def normalize_beats(raw):
    if not isinstance(raw, list):
        raise InputValidationError([
            issue("craft.type", "/beats", "craft beats must be an array")
        ])
    problems = []
    beats = []
    for index, beat in enumerate(raw):
        if not isinstance(beat, str):
            problems.append(issue("craft.beat_type", "/beats/%d" % index,
                                  "each craft beat must be a string"))
        elif not _valid_unicode(beat):
            problems.append(issue("craft.beat_unicode", "/beats/%d" % index,
                                  "craft beats must contain valid Unicode"))
        elif beat.strip():
            beats.append(beat)
    if problems:
        raise InputValidationError(problems)
    return beats


def _read_utf8(path, kind):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise InputValidationError([
            issue("input.utf8", "/" + kind,
                  "%s must be valid UTF-8" % kind)
        ]) from exc
    except (OSError, TypeError) as exc:
        raise InputValidationError([
            issue("input.unreadable", "/" + kind,
                  "%s could not be read: %s" % (kind, exc))
        ]) from exc


def _reject_json_constant(value):
    raise ValueError("non-finite JSON number %s" % value)


MAX_JSON_NUMBER_CHARACTERS = 128


def _bounded_json_int(value):
    if len(value) > MAX_JSON_NUMBER_CHARACTERS:
        raise ValueError("JSON integer exceeds numeric token limit")
    return int(value)


def _bounded_json_float(value):
    if len(value) > MAX_JSON_NUMBER_CHARACTERS:
        raise ValueError("JSON number exceeds numeric token limit")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number is not finite")
    return parsed


def _json_nesting_exceeds(text, maximum=256):
    """Apply one deterministic nesting limit across Python JSON runtimes."""
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > maximum:
                return True
        elif character in "]}":
            depth = max(0, depth - 1)
    return False


def parse_json_value(text, pointer):
    """Parse strict JSON and translate parser limits into a typed issue."""
    if _json_nesting_exceeds(text):
        raise InputValidationError([
            issue("input.invalid_json", pointer,
                  "JSON exceeds the maximum nesting depth of 256")
        ])
    try:
        return json.loads(
            text, parse_constant=_reject_json_constant,
            parse_int=_bounded_json_int, parse_float=_bounded_json_float)
    except json.JSONDecodeError as exc:
        raise InputValidationError([
            issue("input.invalid_json", pointer,
                  "invalid JSON at line %d column %d" %
                  (exc.lineno, exc.colno))
        ]) from exc
    except (RecursionError, ValueError) as exc:
        raise InputValidationError([
            issue("input.invalid_json", pointer,
                  "JSON exceeds parser limits or contains a non-finite number")
        ]) from exc


def _jsonl_values(text, kind):
    values = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        values.append(parse_json_value(
            line, "/%s/line/%d" % (kind, line_number)))
    return values


def load_charter(path=None, gm=None, dice_authors=None):
    """Load and validate the effective charter, including transport overrides.

    Overrides are applied to the parsed document before semantic validation so
    an explicitly supplied value replaces (rather than merely follows) the
    corresponding stored field.  The packaged default is still release-locked
    before any override is applied.
    """
    if path is None:
        raw = copy.deepcopy(_packaged_default_document())
    else:
        raw = parse_json_value(_read_utf8(path, "charter"), "/charter")
    if isinstance(raw, dict) and (gm is not None or dice_authors is not None):
        raw = apply_charter_overrides(raw, gm=gm,
                                      dice_authors=dice_authors)
    return normalize_charter(raw, require_gm=False)


def load_transcript(path):
    text = _read_utf8(path, "transcript")
    stripped = text.strip()
    raw = (parse_json_value(stripped, "/transcript") if stripped.startswith("[")
           else _jsonl_values(text, "transcript"))
    return normalize_transcript(raw)


def load_ledger(path):
    if path is None:
        return []
    text = _read_utf8(path, "ledger")
    stripped = text.strip()
    raw = (parse_json_value(stripped, "/ledger") if stripped.startswith("[")
           else _jsonl_values(text, "ledger"))
    return normalize_ledger(raw)


def load_craft_input(path):
    text = _read_utf8(path, "craft")
    stripped = text.strip()
    if not stripped:
        return []
    return (parse_json_value(stripped, "/craft") if stripped.startswith("[")
            else _jsonl_values(text, "craft"))
