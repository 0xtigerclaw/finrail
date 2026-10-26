"""SCHEMA — what a capability actually accepts, derived from the capability itself.

Native and honest: the parameter list comes from `inspect` on the method that will
really be called, so `-h2` cannot advertise a parameter the code ignores. (The old
system asked an external binary to `describe` a command; the two drifted, and the CLI
cheerfully accepted parameters that were silently dropped.)

Three rules enforced here:
  * an unknown parameter RAISES (never silently ignored)
  * a value is coerced to the annotated type, or raises
  * the rendered signature is exactly the accepted set
"""
import inspect
import json

from ..plugin import Plugin
from .errors import ContractViolation

TASK_PARAM = "task"


def target(cap: Plugin):
    """The method whose signature defines this capability's parameters."""
    if cap.read_only:
        return cap.run
    if cap.kind == "planned":
        return cap.plan
    return None                       # judged: free text, validated by validate()


def _annotation_kind(ann) -> str:
    if ann is inspect.Parameter.empty:
        return "str"
    if ann in (int,):
        return "int"
    if ann in (bool,):
        return "bool"
    if ann in (list,):
        return "list"
    if ann in (dict,):
        return "dict"
    return "str"


#: JSON Schema types, in the vocabulary `-h2` and the router speak.
_JSON_KIND = {"boolean": "bool", "integer": "int", "number": "float",
              "array": "list", "object": "dict", "string": "str"}


def _extra(cap: Plugin) -> list:
    """Parameters a judged capability declares through `takes`, typed from its own schema.

    `takes` is a list of names and nothing else, so every one of them was reported as a
    string. `finrail -h2 tasks.add` therefore advertised `now=<str>` while validation
    required a boolean — the help said one thing, the check demanded another, and the
    person who typed exactly what the help showed got "field 'now' should be boolean, got
    str". Two sources describing the same parameter will disagree eventually; this makes
    the schema the only one.
    """
    import json as _json
    types = {}
    raw = getattr(cap, "schema", "") or ""
    try:
        spec = _json.loads(raw) if isinstance(raw, str) else dict(raw)
        for name, meta in (spec.get("properties") or {}).items():
            if isinstance(meta, dict) and meta.get("type"):
                types[name] = _JSON_KIND.get(meta["type"], "str")
    except (ValueError, TypeError):
        pass
    return [{"name": name, "kind": types.get(name, "str"),
             "required": False, "default": None}
            for name in (cap.takes or ())]


def params(cap: Plugin) -> list:
    """[{name, kind, required, default}] — the accepted parameters."""
    fn = target(cap)
    if fn is None:
        # A judged capability is normally told what to do in prose, so free text is
        # required — UNLESS it declares `takes`, in which case a caller can say the same
        # thing in a parameter. `music.suggest from=<playlist url>` is a whole request,
        # and marking `task` required forced the caller to repeat it as a sentence.
        # `_judge` makes the same allowance and refuses when neither is given.
        return [{"name": TASK_PARAM, "kind": "str",
                 "required": not (getattr(cap, "takes", ()) or ()),
                 "default": None,
                 "doc": "free text describing what you want"}] + _extra(cap)
    out = _extra(cap)
    for name, p in inspect.signature(fn).parameters.items():
        if name in ("self", "kwargs", "kw") or p.kind in (
                inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        out.append({"name": name, "kind": _annotation_kind(p.annotation),
                    "required": p.default is inspect.Parameter.empty,
                    "default": None if p.default is inspect.Parameter.empty else p.default})
    return out


def accepts(cap: Plugin, name: str) -> bool:
    """Does this capability declare a parameter by this name?

    Judged capabilities take free text by definition, but they are not the only ones: a
    capability can have a deterministic plan and still take prose as its input. Asking
    what it DECLARES, rather than inferring from its kind, is what keeps that from being
    a special case someone has to remember.
    """
    if any(p["name"] == name for p in params(cap)):
        return True
    return cap.kind == "judged" and name == TASK_PARAM


def accepts_extras(cap: Plugin) -> bool:
    """True when the capability declares **kwargs and so opts into unknown params.
    Auditable: new code should not, precisely so unknown params are caught."""
    fn = target(cap)
    if fn is None:
        return False
    return any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in inspect.signature(fn).parameters.values())


def coerce_value(kind: str, value):
    if isinstance(value, (list, dict)) or value is None:
        return value
    s = str(value)
    try:
        if kind == "int":
            return int(s)
        if kind == "bool":
            if isinstance(value, bool):
                return value
            low = s.strip().lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off"):
                return False
            raise ValueError(f"'{s}' is not a boolean")
        if kind == "list":
            s = s.strip()
            if s.startswith("["):
                return json.loads(s)
            return [x.strip() for x in s.split(",") if x.strip()]
        if kind == "dict":
            return json.loads(s)
    except (ValueError, json.JSONDecodeError) as e:
        raise ContractViolation(f"expected {kind}: {e}")
    return s


def apply_aliases(cap: Plugin, kwargs: dict) -> dict:
    """Translate accepted older spellings to the canonical name."""
    table = cap.aliases or {}
    if not table:
        return dict(kwargs)
    out = {}
    for k, v in kwargs.items():
        real = table.get(k, k)
        if real in out and out[real] != v:
            raise ContractViolation(
                f"{cap.name}: '{k}' and '{real}' are the same parameter, given twice "
                f"with different values")
        out[real] = v
    return out


def coerce(cap: Plugin, kwargs: dict) -> dict:
    """Type the raw k=v strings a CLI produces, by the capability's own signature."""
    kwargs = apply_aliases(cap, kwargs)
    kinds = {p["name"]: p["kind"] for p in params(cap)}
    out = {}
    for k, v in kwargs.items():
        if k in kinds:
            try:
                out[k] = coerce_value(kinds[k], v)
            except ContractViolation as e:
                raise ContractViolation(f"{cap.name}: parameter '{k}': {e}")
        else:
            out[k] = v
    return out


def validate_params(cap: Plugin, payload: dict) -> dict:
    """Unknown parameter → raise. This is the check that stops a caller (human or model)
    believing a flag took effect when nothing read it."""
    payload = apply_aliases(cap, payload)
    known = {p["name"] for p in params(cap)}
    if accepts_extras(cap):
        return payload
    unknown = [k for k in payload if k not in known]
    if unknown:
        raise ContractViolation(
            f"{cap.name}: unknown parameter(s) {', '.join(sorted(unknown))}. "
            f"Accepts: {', '.join(sorted(known)) or '(none)'}")
    missing = [p["name"] for p in params(cap) if p["required"] and p["name"] not in payload]
    if missing:
        raise ContractViolation(
            f"{cap.name}: missing required parameter(s) {', '.join(missing)}")
    return payload


def signature(cap: Plugin) -> str:
    """One honest usage line."""
    bits = []
    for p in params(cap):
        token = f"{p['name']}=<{p['kind']}>"
        bits.append(token if p["required"] else f"[{token}]")
    verb = "do" if cap.kind != "read" else "get"
    return f"finrail {verb} {cap.name} " + " ".join(bits)


def describe(cap: Plugin) -> dict:
    """Everything knowable about a capability — used by `-h2`, the router prompt and
    `finrail rules`. One source, so none of them can disagree."""
    d = cap.rules()
    d["params"] = params(cap)
    d["signature"] = signature(cap)
    d["accepts_extras"] = accepts_extras(cap)
    d["aliases"] = dict(cap.aliases or {})
    if cap.kind == "judged" and cap.schema:
        try:
            d["schema"] = json.loads(cap.schema) if isinstance(cap.schema, str) else cap.schema
        except json.JSONDecodeError:
            d["schema"] = {}
    return d
