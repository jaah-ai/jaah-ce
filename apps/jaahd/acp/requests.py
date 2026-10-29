"""Agent -> client requests: permission (answered by policy) and elicitation (answered by a human) — §3.3.

Permission: every launch is full-access, so `session/request_permission` is answered at once by policy —
the broadest allow the agent offered (`allow_always` over `allow_once`), never parked on a human — and the
choice is still recorded as an `approval_request` event so the console shows what was allowed and by whom.

A QUESTION THAT ARRIVES AS A PERMISSION REQUEST is the exception: an agent with no elicitation asks its question
tool's question through `session/request_permission`, its options the answers (`AcpDialect.question_call_prefix`
names the tool-call ids it gives such a request). Answered by policy, the first option would be picked for the
operator. It becomes a one-question `RuntimeRequest` instead (`request_from_question`), and the option the human
named is selected (`question_response`); anything else cancels it.

Elicitation: `elicitation/create` is a real human question. It becomes a `RuntimeRequest` — the one wire
shape the daemon's request lane accepts (`runtime.request_open`), validated here so host and daemon agree on
it — and the human's decision maps back to an ACP `{action, content}` response.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

DECISIONS = ("accept", "accept_for_session", "accept_always", "decline", "cancel")
ACP_PERMISSION_DECISIONS = {"allow_once": "accept", "allow_always": "accept_always", "reject_once": "decline",
                            "reject_always": "decline", "cancelled": "cancel"}
ELICITATION_DECISIONS = {"accept": "accept", "decline": "decline", "cancel": "cancel"}
DECIDED_BY_POLICY = "policy:full-access"

MAX_QUESTIONS = 4
HEADER_MAX = 12
META_KEY = "jaah.ai"


def choose_permission(options: list[dict]) -> dict | None:
    """The allow_always option, else allow_once, else None (nothing to allow — answer cancelled)."""
    by_kind: dict[str, dict] = {}
    for opt in options or ():
        if isinstance(opt, dict) and isinstance(opt.get("optionId"), str):
            by_kind.setdefault(opt.get("kind", ""), opt)
    return by_kind.get("allow_always") or by_kind.get("allow_once")


def permission_response(option: dict | None) -> dict:
    if option is None:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}


def _command(raw_input: object) -> str | None:
    if not isinstance(raw_input, dict):
        return None
    cmd = raw_input.get("command")
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    return cmd if isinstance(cmd, str) else None


def approval_data(params: dict, option: dict | None) -> dict:
    tc = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
    meta = (tc.get("_meta") or {}).get(META_KEY) if isinstance(tc.get("_meta"), dict) else None
    name = meta.get("tool_name") if isinstance(meta, dict) and meta.get("tool_name") else tc.get("title")
    locations = tc.get("locations") if isinstance(tc.get("locations"), list) else []
    return {
        "tool_call": {
            "name": name,
            "kind": tc.get("kind"),
            "paths": [loc["path"] for loc in locations if isinstance(loc, dict) and loc.get("path")],
            "command": _command(tc.get("rawInput")),
        },
        "options": [{"id": o.get("optionId"), "name": o.get("name"), "kind": o.get("kind")}
                    for o in params.get("options") or () if isinstance(o, dict)],
        "decision": ACP_PERMISSION_DECISIONS.get(option.get("kind", ""), "cancel") if option else "cancel",
        "decided_by": DECIDED_BY_POLICY,
    }


@dataclass(frozen=True)
class RuntimeRequest:
    id: str
    type: str
    questions: tuple[dict, ...]

    def to_wire(self) -> dict:
        return {"id": self.id, "type": self.type, "questions": [dict(q) for q in self.questions]}


def _nonempty_str(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")
    return value


def _question(q: object, n: int) -> dict:
    if not isinstance(q, dict):
        raise ValueError(f"question {n} must be an object")
    header = q.get("header", "")
    if not isinstance(header, str):
        raise ValueError(f"question {n} header must be a string")
    options = q.get("options", [])
    if not isinstance(options, list):
        raise ValueError(f"question {n} options must be a list")
    opts = []
    for i, o in enumerate(options):
        if not isinstance(o, dict):
            raise ValueError(f"question {n} option {i} must be an object")
        desc = o.get("description", "")
        if not isinstance(desc, str):
            raise ValueError(f"question {n} option {i} description must be a string")
        opts.append({"label": _nonempty_str(o.get("label"), f"question {n} option {i} label"), "description": desc})
    multi = q.get("multi_select", False)
    if not isinstance(multi, bool):
        raise ValueError(f"question {n} multi_select must be a boolean")
    return {"id": _nonempty_str(q.get("id"), f"question {n} id"), "header": header,
            "question": _nonempty_str(q.get("question"), f"question {n} question"),
            "options": opts, "multi_select": multi}


def validate_request(obj: object) -> RuntimeRequest:
    if not isinstance(obj, dict):
        raise ValueError("request must be an object")
    rid = _nonempty_str(obj.get("id"), "request id")
    if obj.get("type") != "user_input":
        raise ValueError("request type must be 'user_input'")
    questions = obj.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"request needs 1..{MAX_QUESTIONS} questions")
    parsed = tuple(_question(q, n) for n, q in enumerate(questions))
    if len({q["id"] for q in parsed}) != len(parsed):
        raise ValueError("question ids must be unique")
    return RuntimeRequest(id=rid, type="user_input", questions=parsed)


def _enum_options(prop: dict) -> tuple[list[dict], bool]:
    """Choices from a property schema: `enum`, `oneOf[{const,title}]`, or an array's `items` of either."""
    multi = prop.get("type") == "array"
    src = prop.get("items") if multi and isinstance(prop.get("items"), dict) else prop
    if isinstance(src.get("enum"), list):
        return [{"label": str(v), "description": ""} for v in src["enum"]], multi
    variants = src.get("oneOf") or src.get("anyOf")
    if isinstance(variants, list):
        opts = [{"label": str(v.get("title") or v.get("const")), "description": str(v.get("description") or "")}
                for v in variants if isinstance(v, dict) and "const" in v]
        return opts, multi
    return [], multi


CUSTOM_SUFFIX = "_custom"


def _companion_of(key: str, prop: object, props: dict) -> str:
    """The choice property a free-text companion field belongs to, else "": `_meta.<ns>.questionId` (a note
    beside a choice), or an elicitation bridge's unmarked `<choice>_custom` "Other" box beside a choice property
    that offers options (the shape an SDK adapter's question bridge builds; measured on a live adapter)."""
    meta = prop.get("_meta") if isinstance(prop, dict) else None
    for inner in meta.values() if isinstance(meta, dict) else ():
        if isinstance(inner, dict) and isinstance(inner.get("questionId"), str) and inner["questionId"] in props \
                and inner["questionId"] != key:
            return inner["questionId"]
    main = key[:-len(CUSTOM_SUFFIX)] if key.endswith(CUSTOM_SUFFIX) else ""
    if main in props and isinstance(props[main], dict) and _enum_options(props[main])[0]:
        return main
    return ""


def companions(schema: dict | None) -> dict[str, str]:
    """`{choice property: its OPTIONAL free-text companion}` — the companion is not asked on its own: it is
    the "Other" answer of its choice's round (a typed answer that is no option lands there)."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    found: dict[str, str] = {}
    for key, prop in props.items():
        main = _companion_of(key, prop, props)
        if main and key not in required and main not in found:
            found[main] = key
    return found


def _asked(schema: dict, props: dict) -> list:
    """The properties a human is asked: all but an optional companion of another asked property."""
    hidden = set(companions(schema).values())
    return [key for key in props if key not in hidden]


def request_from_elicitation(params: dict, request_id: str, *, question_preamble: str = "",
                             question_meta_key: str = "") -> RuntimeRequest:
    message = params.get("message") if isinstance(params.get("message"), str) else ""
    schema = params.get("requestedSchema") if isinstance(params.get("requestedSchema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    questions = []
    asked = _asked(schema, props)
    for key in asked:
        prop = props[key] if isinstance(props[key], dict) else {}
        title = prop.get("title") if isinstance(prop.get("title"), str) and prop.get("title") else ""
        desc = prop.get("description") if isinstance(prop.get("description"), str) else ""
        options, multi = _enum_options(prop)
        # A property the dialect marks as its question tool's is that shape even with a label over HEADER_MAX or a
        # question without "?".
        meta = prop.get("_meta")
        marked = bool(question_meta_key) and isinstance(meta, dict) and question_meta_key in meta
        if desc and title and (marked or title.rstrip().endswith("?") and len(desc) <= HEADER_MAX):
            # The question itself in `title`, its short label in `description`: the label is the header, the
            # question the text.
            # The adapter's stock preamble adds nothing to a question already supplied in the title.
            context = "" if str(message).strip() == question_preamble else message
            header, text = desc, "\n\n".join(p for p in (context, title) if p)
        else:
            header = title[:HEADER_MAX] if title else key[:HEADER_MAX]
            # The property's own name joins the message when it has no description, so two questions of one
            # elicitation never share a text (the answer is matched back to its question by that text). A lone
            # question is the message itself: its title is already the header.
            named = (title or key) if message and len(asked) > 1 else ""
            text = "\n\n".join(p for p in (message, desc or named) if p) or title or key
        questions.append({"id": key, "header": header, "question": text, "options": options, "multi_select": multi})
    if not questions:  # a bare message: one free-text question
        questions.append({"id": "answer", "header": "Question", "question": message or "Input requested",
                          "options": [], "multi_select": False})
    return validate_request({"id": request_id, "type": "user_input", "questions": questions})


def validate_decision(value: object) -> str:
    if not isinstance(value, str) or value not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    return value


_TRUE = ("true", "yes", "y", "1", "on")
_FALSE = ("false", "no", "n", "0", "off")
BARE_ANSWER_ID = "answer"


def _choice(src: dict, text: str) -> object:
    """A label the human picked -> the schema's own value (`enum` member or `oneOf` const); else the text."""
    if isinstance(src.get("enum"), list):
        pairs = [(str(v), v) for v in src["enum"]]
    else:
        variants = src.get("oneOf") or src.get("anyOf") or []
        pairs = []
        for v in variants if isinstance(variants, list) else ():
            if isinstance(v, dict) and "const" in v:
                pairs += [(str(v.get("title") or v["const"]), v["const"]), (str(v["const"]), v["const"])]
    for same in (lambda a, b: a == b, lambda a, b: a.casefold() == b.casefold(),
                 lambda a, b: question_key(a) == question_key(b) != ""):
        for label, value in pairs:
            if same(label, text):
                return value
    return text


def _scalar(prop: dict, text: str) -> object:
    kind = prop.get("type")
    stripped = text.strip()
    try:
        if kind == "integer":
            return int(stripped)
        if kind == "number":
            try:
                return int(stripped)
            except ValueError:
                number = float(stripped)
                return number if math.isfinite(number) else text  # NaN/inf are not JSON
    except ValueError:
        return text
    if kind == "boolean":
        if stripped.lower() in _TRUE:
            return True
        if stripped.lower() in _FALSE:
            return False
        return text
    return _choice(prop, text)


def coerce_answer(prop: dict, answer: object) -> object:
    """A human's answer text -> the value the property's schema asks for: an enum label -> its value, an
    array -> its items (labels split on ", " or newlines), number/integer/boolean parsed; else the text."""
    if not isinstance(prop, dict):
        prop = {}
    if prop.get("type") == "array":
        items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
        if isinstance(answer, list):
            parts = [str(a) for a in answer]
        else:
            parts = [p.strip() for line in str(answer).splitlines() for p in line.split(", ")]
        return [_scalar(items, p) for p in parts if p]
    if not isinstance(answer, str):
        return answer
    return _scalar(prop, answer)


def _picked(prop: dict, answer: object) -> bool:
    """Is `answer` one of the choice property's own options (each item, for an array)?"""
    src = prop.get("items") if prop.get("type") == "array" and isinstance(prop.get("items"), dict) else prop
    values = list(src["enum"]) if isinstance(src.get("enum"), list) else [
        v["const"] for v in (src.get("oneOf") or src.get("anyOf") or []) if isinstance(v, dict) and "const" in v]
    value = coerce_answer(prop, answer)
    items = value if isinstance(value, list) else [value]
    return bool(items) and all(item in values for item in items)


# The CP's joiner between an answer and the operator's own note (`routes/tasks/qa_render.py::_COMMENT_JOINER`).
COMMENT_JOINER = " — operator comment: "


def _choice_and_other(prop: dict, answer: object) -> tuple[object, str]:
    """A choice's answer split into `(the options it picked, or None; the typed rest for its "Other" box)`. An
    operator's note after a pick is the rest (the agent reads it as the pick's notes); an array keeps its ticks and
    joins what is no option into the rest."""
    head, _, note = answer.partition(COMMENT_JOINER) if isinstance(answer, str) else (answer, "", "")
    if _picked(prop, head):
        return coerce_answer(prop, head), note
    if prop.get("type") == "array":
        picks = [p for p in coerce_answer(prop, head) if _picked(prop, [p])]
        if picks:
            typed = [str(p) for p in coerce_answer(prop, head) if p not in picks]
            return picks, COMMENT_JOINER.join(t for t in (", ".join(typed), note) if t)
    return None, answer if isinstance(answer, str) else str(answer)


def elicitation_content(schema: dict | None, answers: dict | None) -> dict:
    """Answers keyed by question id -> ACP `content` keyed by property name, each value of the property's
    type. Questions ARE the schema's properties (`request_from_elicitation`); a bare message has the one
    question `answer`. A choice with a free-text companion ("Other") gets what its answer picked; the typed
    rest goes to the companion (`_choice_and_other`). Keys that name no question are dropped."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    props = props if isinstance(props, dict) and props else {BARE_ANSWER_ID: {"type": "string"}}
    other = companions(schema)
    content: dict = {}
    for qid, value in (answers or {}).items():
        if qid not in props:
            continue
        if qid not in other:
            content[qid] = coerce_answer(props[qid], value)
            continue
        picked, typed = _choice_and_other(props[qid], value)
        if picked is not None:
            content[qid] = picked
        if typed:
            content[other[qid]] = coerce_answer(props[other[qid]], typed)
    return content


def question_key(text: object) -> str:
    """A question's text reduced to its letters and digits, casefolded: how a delivered answer names the round it
    answers once the control plane has flattened and defanged it (`routes/tasks/qa_render.py::_neutralize_batch_delimiters`
    swaps punctuation, `_flatten` folds the line breaks)."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text if isinstance(text, str) else "")).casefold()


# One round of the control plane's batch hand-off, `N) <question> → <resolution>`
# (`routes/tasks/answers.py::_compose_batch_answer`); the question half carries no `→` (it is defanged to `-`).
_BATCH_ROUND = re.compile(r"^\d+\) (.*?) → (.*)$")


def batch_rounds(text: str) -> list[tuple[str, str]]:
    """`[(question, resolution), ...]` from a batch hand-off; a line that starts no round (or numbers one out of
    the batch's ascending order) continues the last resolution — only an operator's typed half holds a line
    break. [] when the text is no batch."""
    rounds: list[list[str]] = []
    last = 0
    for line in text.splitlines():
        match = _BATCH_ROUND.match(line)
        number = int(line.split(")", 1)[0]) if match else 0
        if match and number > last:
            rounds.append([match.group(1), match.group(2)])
            last = number
        elif rounds:
            rounds[-1][1] += "\n" + line
    return [(q, a) for q, a in rounds]


def apply_answer(questions: list[dict], got: dict, question: str,
                 text: str) -> tuple[bool | None, list[tuple[str, str]]]:
    """Fill `got` (question id -> answer text) from ONE delivered answer: `(decided, rest)` — whether the request
    is now decided (None when it had no unanswered question to fill), and the batch rounds it had no question
    for, as `(question, answer)` pairs the caller hands the agent otherwise.

    With a `question` (one round answered): its text names the question, else it fills the first unanswered one,
    and the request is decided once every question is answered. With none, `text` is the batch hand-off the
    control plane sends when the gate clears with several rounds answered, and the request is decided at once —
    the gate cleared, so nothing more comes for it. Each round first fills the question it names (a second round
    naming an answered one is a duplicate, dropped); the rounds naming none then fill the questions left, in
    order; a text that is no batch fills the first."""
    def unanswered() -> list[dict]:
        return [q for q in questions if q.get("id") not in got]

    def named_by(named: str, pool: list[dict]) -> dict | None:
        key = question_key(named)
        return next((q for q in pool if key and question_key(q.get("question")) == key), None)

    if question:
        target = named_by(question, unanswered()) or next(iter(unanswered()), None)
        if target is None:
            return None, []
        got[target["id"]] = text
        return not unanswered(), []
    if not unanswered():
        return None, []
    unnamed: list[tuple[str, str]] = []
    for named, answer in batch_rounds(text) or [("", text)]:
        target = named_by(named, unanswered())
        if target is not None:
            got[target["id"]] = answer
        elif named_by(named, questions) is None:
            unnamed.append((named, answer))
    rest: list[tuple[str, str]] = []
    for named, answer in unnamed:
        target = next(iter(unanswered()), None)
        if target is not None:
            got[target["id"]] = answer
        else:
            rest.append((named, answer))
    return True, rest


def elicitation_response(decision: str, answers: dict | None, schema: dict | None = None) -> dict:
    """`answers` as they came, or — given the elicitation's `requestedSchema` — converted to its types."""
    decision = validate_decision(decision)
    action = "accept" if decision.startswith("accept") else decision
    if action == "accept":
        content = elicitation_content(schema, answers) if schema is not None else dict(answers or {})
        return {"action": "accept", "content": content}
    return {"action": action}


QUESTION_HEADER = "Question"


def is_agent_question(params: dict, prefix: str) -> bool:
    """Is this `session/request_permission` an agent's question rather than a permission? Its tool-call id starts
    with the dialect's `prefix`, and it offers no `reject_*` option: a yes/no prompt the agent raises through the
    same channel (a workspace-trust prompt) offers one, and stays a policy answer."""
    tc = params.get("toolCall")
    call_id = tc.get("toolCallId") if isinstance(tc, dict) else None
    if not (prefix and isinstance(call_id, str) and call_id.startswith(prefix)):
        return False
    options = [o for o in params.get("options") or () if isinstance(o, dict)]
    return bool(options) and not any(str(o.get("kind", "")).startswith("reject") for o in options)


def request_from_question(params: dict, request_id: str) -> RuntimeRequest:
    """The question as ONE operator round: its title the question, each option's name a choice."""
    tc = params.get("toolCall")
    title = tc.get("title") if isinstance(tc, dict) else None
    options = []
    for o in params.get("options") or ():
        if isinstance(o, dict):
            name = o.get("name")
            options.append({"label": name if isinstance(name, str) and name.strip() else str(o.get("optionId") or ""),
                            "description": ""})
    return validate_request({"id": request_id, "type": "user_input", "questions": [{
        "id": BARE_ANSWER_ID, "header": QUESTION_HEADER, "question": title or "Input requested",
        "options": options, "multi_select": False}]})


def question_response(decision: str, answers: dict | None, options: list) -> dict:
    """The ACP permission outcome for a question round: the option whose name (or id) the human's answer names —
    exactly, then ignoring case, then by its letters and digits — else `cancelled` (a declined round, or a typed
    answer that is no option: the agent's single-choice question cannot carry it)."""
    decision = validate_decision(decision)
    answer = (answers or {}).get(BARE_ANSWER_ID) if decision.startswith("accept") else None
    if isinstance(answer, str):
        head = answer.partition(COMMENT_JOINER)[0].strip()
        offered = [o for o in options if isinstance(o, dict) and isinstance(o.get("optionId"), str)]
        for same in (lambda a, b: a == b, lambda a, b: a.casefold() == b.casefold(),
                     lambda a, b: question_key(a) == question_key(b) != ""):
            for option in offered:
                if any(isinstance(label, str) and same(label.strip(), head)
                       for label in (option.get("name"), option["optionId"])):
                    return permission_response(option)
    return permission_response(None)
