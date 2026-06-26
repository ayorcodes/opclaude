"""Shared task-classification logic for oc.

Used by both bin/oc-classify (the launcher's per-session classifier, which
adds an LLM fallback) and litellm_hooks.py (the per-turn router that rewrites
the model on every request in an auto-routed session). Keeping the heuristics
here means both paths agree on what "trivial" vs "critical" means.

Heuristics only -- no network calls -- so this is safe to run on the hot path
of every proxied request without adding latency.
"""
import os
import re

CRITICAL_RE = re.compile(
    r"architecture|concurren(cy|t)|race condition|security|vulnerabilit|"
    r"distributed|migrat(e|ion)|deadlock|memory leak|design (a |the )?system|"
    r"breaking change|root cause",
    re.IGNORECASE,
)
TRIVIAL_RE = re.compile(
    r"rename|typo|format(ting)?|lint|comment|docstring|translate|"
    r"whitespace|indent",
    re.IGNORECASE,
)
MODERATE_RE = re.compile(
    r"refactor|test|boilerplate|scaffold|add (a |an )?endpoint|implement",
    re.IGNORECASE,
)

TASK_TYPE_RE = [
    ("rename", re.compile(r"rename", re.IGNORECASE)),
    ("typo", re.compile(r"typo", re.IGNORECASE)),
    ("format", re.compile(r"format(ting)?|lint|whitespace|indent", re.IGNORECASE)),
    ("test", re.compile(r"\btest", re.IGNORECASE)),
    ("refactor", re.compile(r"refactor", re.IGNORECASE)),
    ("implement", re.compile(r"implement|build (the|this|it)", re.IGNORECASE)),
]


def detect_task_type(task):
    for name, pattern in TASK_TYPE_RE:
        if pattern.search(task):
            return name
    return "general"


def heuristic_tier(task):
    """Return (tier, reason) or (None, None) when heuristics are ambiguous."""
    is_critical = bool(CRITICAL_RE.search(task))
    is_trivial = bool(TRIVIAL_RE.search(task))
    is_moderate = bool(MODERATE_RE.search(task))

    if is_critical:
        return "critical", "matched a critical-risk keyword"
    if is_trivial and not is_moderate:
        return "trivial", "matched a trivial-task keyword"
    if is_moderate and not is_trivial:
        return "moderate", "matched a moderate-task keyword"
    return None, None


def pick_cheap_model(tier, task_type, router_config):
    """Map a tier+task_type to a cheap opencode-Zen model.

    'critical' has no real-Claude option here (the proxy can't reach Pro), so
    a critical-looking turn inside an auto-routed cheap session gets the
    strongest configured cheap model instead.
    """
    models = router_config.get("models", {})
    if tier == "critical":
        bucket = models.get("critical_cheap", {})
    else:
        bucket = models.get(tier, {})
    return (
        bucket.get(task_type)
        or bucket.get("default")
        or "claude-deepseek-v4-pro"
    )


def _parse_shallow_yaml(text):
    """Minimal parser for router.yaml's fixed nested-dict shape.

    Avoids a PyYAML dependency for a structure this simple:
        top:
          mid:
            leaf: value
    """
    root = {}
    stack = [(-1, root)]
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, value = line.strip().partition(":")
        value = value.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if value == "":
            child = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            if value.lower() in ("true", "false"):
                value = value.lower() == "true"
            elif value.startswith(("'", '"')) and value.endswith(("'", '"')):
                value = value[1:-1]
            parent[key] = value
    return root


def default_config():
    return {
        "models": {
            "trivial": {"default": "claude-deepseek-v4-flash"},
            "moderate": {"default": "claude-deepseek-v4-pro"},
            "critical_cheap": {"default": "claude-deepseek-v4-pro"},
        },
        "classifier": {"model": "claude-kimi-2.7"},
        "critical": {"default": "sonnet", "high_effort": "opus"},
        "per_turn": {"enabled": True},
        "logging": {"full_text": False},
    }


def load_router_config(path=None):
    if path is None:
        path = os.path.expanduser("~/.config/opclaude/router.yaml")
    default = default_config()
    if not path or not os.path.isfile(path):
        return default
    try:
        with open(path) as f:
            data = _parse_shallow_yaml(f.read())
        for key, value in default.items():
            data.setdefault(key, value)
        return data
    except Exception:
        return default


def last_user_text(messages):
    """Extract the latest user turn's text from an Anthropic/OpenAI message list."""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return " ".join(parts)
    return ""
