"""Load declarative topic policies shared by planning, generation and review."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import lru_cache, wraps
import json
from pathlib import Path
import re


TOPIC_RULES_FILE = Path(__file__).with_name("topic_rules.json")
_active_topic = ContextVar("topic_rules_topic", default="")
_STRING_LISTS = {"aliases", "must_include", "forbidden", "any", "unless_any",
                 "category_any", "patterns_all", "reason_markers", "protected_markers",
                 "learner_category_markers", "required_any", "forbidden_any",
                 "address_customer_categories"}
_RULE_LISTS = {"coverage", "contract_categories", "plan_rejections", "category_instructions",
               "intent_rules", "content_rejections"}


def _validate(value, location):
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    for key, field in value.items():
        path = f"{location}.{key}"
        if key in _STRING_LISTS:
            if not isinstance(field, list) or any(not isinstance(s, str) or not s.strip() for s in field):
                raise ValueError(f"{path} must be an array of nonempty strings")
            if key == "patterns_all":
                for pattern in field:
                    re.compile(pattern)
        elif key == "all":
            if (not isinstance(field, list) or any(not isinstance(group, list) or not group
                    or any(not isinstance(s, str) or not s.strip() for s in group) for group in field)):
                raise ValueError(f"{path} must be an array of nonempty string arrays")
        elif key in _RULE_LISTS:
            if not isinstance(field, list):
                raise ValueError(f"{path} must be an array of rule objects")
            for index, rule in enumerate(field):
                _validate(rule, f"{path}[{index}]")
                if not rule.get("message") and key not in {"category_instructions", "intent_rules"}:
                    raise ValueError(f"{path}[{index}] requires a message")
                if key in {"category_instructions", "intent_rules"}:
                    required = "instruction" if key == "category_instructions" else "purpose"
                    if not rule.get(required):
                        raise ValueError(f"{path}[{index}] requires {required}")
        elif key in {"instructions", "counterpart_validation", "review_protection", "candidate_roles"}:
            _validate(field, path)
        elif key == "counterpart_share":
            if type(field) not in (int, float) or not 0 <= field <= 1:
                raise ValueError(f"{path} must be between 0 and 1")
        elif key in {"unless_pattern"}:
            if not isinstance(field, str):
                raise ValueError(f"{path} must be a regex string")
            re.compile(field)
        elif key in {"message", "label", "source", "role_type", "instruction", "purpose",
                     "contract", "plan_review", "card_roles", "counterpart_candidates",
                     "card_review", "generation_review"}:
            if not isinstance(field, str) or not field.strip():
                raise ValueError(f"{path} must be a nonempty string")
            if key == "source" and field not in {"deck", "learner"}:
                raise ValueError(f"{path} must be deck or learner")
            if key == "role_type" and field not in {"learner_line", "counterpart_line"}:
                raise ValueError(f"{path} must be learner_line or counterpart_line")
        else:
            raise ValueError(f"Unknown topic rule field: {path}")


@lru_cache(maxsize=8)
def _load(path, modified, size):
    try:
        with open(path, encoding="utf-8") as source:
            rules = json.load(source)
        if not isinstance(rules, dict):
            raise ValueError("root must be an object keyed by topic name")
        for name, value in rules.items():
            if not name.strip():
                raise ValueError("topic names must not be blank")
            _validate(value, name)
        return rules
    except (OSError, ValueError, re.error) as exc:
        raise ValueError(f"Invalid topic rules in {path}: {exc}") from exc


def load_topic_rules():
    path = Path(TOPIC_RULES_FILE)
    try:
        status = path.stat()
    except OSError as exc:
        raise ValueError(f"Cannot load topic rules from {path}: {exc}") from exc
    return deepcopy(_load(str(path.resolve()), status.st_mtime_ns, status.st_size))


def _title(text):
    return " ".join(re.sub(r"[_-]+", " ", text.splitlines()[0] if text else "").casefold().split())


def get_topic_rules(topic):
    title = _title(topic)
    matches = []
    for name, rules in load_topic_rules().items():
        if name.startswith("_"):
            continue
        for alias in [name, *rules.get("aliases", [])]:
            marker = _title(alias)
            matched = (re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", title)
                       if marker.isascii() else marker in title)
            if matched:
                matches.append((marker == title, len(marker), name, rules))
    return max(matches, key=lambda entry: entry[:3])[3] if matches else {}


def matches_rule(text, rule):
    text = text.casefold()
    return (not rule.get("any") or any(s.casefold() in text for s in rule["any"])) and all(
        any(s.casefold() in text for s in group) for group in rule.get("all", [])
    ) and not any(s.casefold() in text for s in rule.get("unless_any", [])) and all(
        re.search(pattern, text) for pattern in rule.get("patterns_all", [])
    ) and not (rule.get("unless_pattern") and re.search(rule["unless_pattern"], text))


def topic_rule_text(topic, phase=""):
    rules = get_topic_rules(topic)
    if not rules:
        return ""
    lines = ["特定主題規則（仍須遵守使用者明確指定的內容焦點與邊界）："]
    if rules.get("must_include"):
        lines.append("必須涵蓋：\n" + "\n".join("- " + rule for rule in rules["must_include"]))
    if rules.get("forbidden"):
        lines.append("禁止使用／收錄：\n" + "\n".join("- " + rule for rule in rules["forbidden"]))
    instruction = rules.get("instructions", {}).get(phase, "")
    if instruction:
        lines.append(instruction)
    return "\n".join(lines)


def topic_messages(topic, prompt, *, system=""):
    rule_text = topic_rule_text(topic)
    instruction = "\n".join(part for part in (system, rule_text) if part)
    messages = [{"role": "system", "content": instruction}] if instruction else []
    return messages + [{"role": "user", "content": prompt}]


def default_content_issues(text):
    return [rule["message"] for rule in load_topic_rules().get("_defaults", {}).get("content_rejections", [])
            if matches_rule(text, rule)]


def default_rule_text(phase):
    return load_topic_rules().get("_defaults", {}).get("instructions", {}).get(phase, "")


def configured_communication_intent(text):
    for rules in load_topic_rules().values():
        for rule in rules.get("intent_rules", []):
            if matches_rule(text, rule):
                return rule["purpose"]
    return ""


def current_topic():
    return _active_topic.get()


@contextmanager
def topic_rule_scope(topic):
    token = _active_topic.set(topic)
    try:
        yield
    finally:
        _active_topic.reset(token)


def with_topic_rules(function):
    """Scope the existing topic/plan entry points without changing retry state."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        first = args[0] if args else kwargs.get("topic", kwargs.get("plan", ""))
        topic = first.get("topic", "") if isinstance(first, dict) else first
        get_topic_rules(topic)
        with topic_rule_scope(topic):
            return function(*args, **kwargs)
    return wrapped
