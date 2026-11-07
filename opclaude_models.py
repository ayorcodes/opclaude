"""Builds the litellm proxy config from the user's ~/.opclaude/models.yaml.

config.yaml in the repo holds only the proxy settings (callbacks, master key,
the claude-auto sentinel). The model list lives in the user's models.yaml,
grouped by provider, and is expanded into litellm `model_list` entries here.
The result is written next to config.yaml (litellm resolves the
`litellm_hooks` callback relative to the config file's directory).

Usage:
  opclaude_models.py build            regenerate config.generated.yaml
  opclaude_models.py list [provider]  grouped, human-readable listing
  opclaude_models.py default          print the default model name
  opclaude_models.py add <provider> <model-id> [key=value ...]
  opclaude_models.py path             print the models.yaml path

build also writes ~/.codex/opclaude-catalog.json when ~/.codex exists.
"""
import json
import os
import re
import shutil
import sys

import yaml

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_CONFIG = os.path.join(REPO_DIR, "config.yaml")
GENERATED_CONFIG = os.path.join(REPO_DIR, "config.generated.yaml")
EXAMPLE = os.path.join(REPO_DIR, "models.example.yaml")
MODELS_FILE = os.path.expanduser(
    os.environ.get("OPCLAUDE_MODELS", "~/.opclaude/models.yaml")
)
ENV_FILE = os.path.expanduser("~/.config/opclaude/.env")

# litellm provider types that talk OpenAI chat-completions and need the flag
# to avoid being sent to /responses.
CHAT_COMPLETIONS_TYPES = ("openai", "github_copilot")


class ConfigError(Exception):
    pass


def ensure_models_file():
    if not os.path.exists(MODELS_FILE):
        os.makedirs(os.path.dirname(MODELS_FILE), exist_ok=True)
        shutil.copyfile(EXAMPLE, MODELS_FILE)
        print(f"Created {MODELS_FILE} from models.example.yaml", file=sys.stderr)


def load_models_file():
    ensure_models_file()
    with open(MODELS_FILE) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data.get("providers"), dict):
        raise ConfigError(f"{MODELS_FILE}: missing `providers:` mapping")
    return data


def parse_context(value):
    """'256K' -> (262144, '256K'); '1M' -> (1048576, '1000K'); int -> (n, 'nK')."""
    if value is None:
        return None, None
    if isinstance(value, int):
        return value, f"{value // 1024}K"
    m = re.fullmatch(r"\s*(\d+)\s*([KkMm])\s*", str(value))
    if not m:
        raise ConfigError(f"bad context value {value!r} (use e.g. 256K or 1M)")
    n, unit = int(m.group(1)), m.group(2).upper()
    if unit == "M":
        return n * 1024 * 1024, f"{n * 1000}K"
    return n * 1024, f"{n}K"


def short_name(model_id):
    name = model_id.rsplit("/", 1)[-1]
    name = re.sub(r"[:-]cloud$", "", name)
    return name.replace(":", "-")


def expand(data):
    """Yield (provider, entry) for every enabled model, entry being a
    litellm model_list item."""
    seen = {}
    for pname, prov in data["providers"].items():
        prov = prov or {}
        ptype = prov.get("type", "openai")
        prefix = prov.get("prefix", pname)
        for model_id, opts in (prov.get("models") or {}).items():
            opts = opts or {}
            if not opts.get("enabled", True):
                continue
            ctx_tokens, ctx_label = parse_context(opts.get("context"))
            name = "claude-" + (f"{prefix}-" if prefix else "") + opts.get("name", short_name(model_id))
            if ctx_label:
                name += f"[{ctx_label}]"
            if name in seen:
                raise ConfigError(
                    f"duplicate model name {name} ({seen[name]} and {pname}/{model_id}); "
                    "set `name:` on one of them"
                )
            seen[name] = f"{pname}/{model_id}"

            params = {"model": f"{ptype}/{model_id}"}
            if prov.get("api_base"):
                params["api_base"] = prov["api_base"]
            if prov.get("api_key_env"):
                params["api_key"] = f"os.environ/{prov['api_key_env']}"
            elif prov.get("api_key"):
                params["api_key"] = prov["api_key"]
            if opts.get("chat_completions", prov.get("chat_completions", ptype in CHAT_COMPLETIONS_TYPES)):
                params["use_chat_completions_api"] = True
            if prov.get("drop_params", True):
                params["drop_params"] = True
            params.update(prov.get("params") or {})
            params.update(opts.get("params") or {})

            entry = {"model_name": name, "litellm_params": params}
            info = {}
            if "reasoning" in opts:
                info["supports_reasoning"] = bool(opts["reasoning"])
            if ctx_tokens:
                info["context_window"] = ctx_tokens
            if info:
                entry["model_info"] = info
            yield pname, model_id, entry


def build():
    data = load_models_file()
    with open(BASE_CONFIG) as f:
        base = yaml.safe_load(f)
    entries = [e for _, _, e in expand(data)]
    base["model_list"] = entries + (base.get("model_list") or [])
    tmp = GENERATED_CONFIG + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"# GENERATED from {MODELS_FILE} + config.yaml — do not edit.\n")
        yaml.safe_dump(base, f, sort_keys=False)
    os.replace(tmp, GENERATED_CONFIG)
    write_codex_catalog(data)
    return len(entries)


# Codex (`model_catalog_json` in ~/.codex/config.toml) needs a full catalog
# entry per model: base instructions, tool settings, and so on. We don't
# author those. We clone one entry from an existing catalog and override only
# the per-model fields (the same fields that differ between entries in a
# cliproxy catalog).
CODEX_DIR = os.path.expanduser("~/.codex")
CODEX_CATALOG = os.path.join(CODEX_DIR, "opclaude-catalog.json")
CODEX_TEMPLATE = os.path.expanduser("~/.opclaude/codex-template.json")
CODEX_TEMPLATE_SOURCE = os.path.join(CODEX_DIR, "cliproxy-catalog.json")
CODEX_DEFAULT_CONTEXT = 200000


def codex_template():
    if not os.path.exists(CODEX_TEMPLATE):
        if not os.path.exists(CODEX_TEMPLATE_SOURCE):
            return None
        with open(CODEX_TEMPLATE_SOURCE) as f:
            entry = json.load(f)["models"][0]
        with open(CODEX_TEMPLATE, "w") as f:
            json.dump(entry, f, indent=2)
    with open(CODEX_TEMPLATE) as f:
        return json.load(f)


def write_codex_catalog(data):
    if not os.path.isdir(CODEX_DIR):
        return
    template = codex_template()
    if template is None:
        print(
            f"Skipped {CODEX_CATALOG}: no template (put one catalog entry in {CODEX_TEMPLATE})",
            file=sys.stderr,
        )
        return
    models = []
    for priority, (pname, model_id, e) in enumerate(expand(data)):
        entry = json.loads(json.dumps(template))
        ctx = e.get("model_info", {}).get("context_window", CODEX_DEFAULT_CONTEXT)
        entry.update(
            slug=e["model_name"],
            display_name=e["model_name"].removeprefix("claude-"),
            description=f"{pname} · {model_id}",
            priority=priority,
            context_window=ctx,
            max_context_window=ctx,
            input_modalities=["text"],
            additional_speed_tiers=[],
            service_tiers=[],
        )
        models.append(entry)
    tmp = CODEX_CATALOG + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"models": models}, f, indent=2)
    os.replace(tmp, CODEX_CATALOG)
    print(f"Generated {CODEX_CATALOG} ({len(models)} models)", file=sys.stderr)


def resolve_default(data):
    default = data.get("default")
    names = {e["model_name"] for _, _, e in expand(data)}
    if default in names:
        return default
    # Allow `<provider>/<model-id>` as a shorthand.
    for pname, model_id, e in expand(data):
        if default == f"{pname}/{model_id}":
            return e["model_name"]
    raise ConfigError(f"default {default!r} is not an enabled model in {MODELS_FILE}")


def read_env_keys():
    keys = set()
    try:
        with open(ENV_FILE) as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and v and not k.startswith("#"):
                    keys.add(k)
    except FileNotFoundError:
        pass
    return keys | {k for k, v in os.environ.items() if v}


def list_models(only=None):
    data = load_models_file()
    try:
        default = resolve_default(data)
    except ConfigError:
        default = None
    env_keys = read_env_keys()
    rows = list(expand(data))
    width = max((len(e["model_name"]) for _, _, e in rows), default=20) + 2
    tty = sys.stdout.isatty()
    bold = (lambda s: f"\033[1m{s}\033[0m") if tty else (lambda s: s)
    dim = (lambda s: f"\033[2m{s}\033[0m") if tty else (lambda s: s)

    print(dim(f"from {MODELS_FILE}"))
    for pname, prov in data["providers"].items():
        if only and pname != only:
            continue
        prov = prov or {}
        prov_rows = [(m, e) for p, m, e in rows if p == pname]
        disabled = sum(
            1 for o in (prov.get("models") or {}).values() if (o or {}).get("enabled") is False
        )
        if not prov_rows and not disabled:
            continue
        key = prov.get("api_key_env")
        status = ""
        if key:
            status = f"{key} set" if key in env_keys else f"{key} NOT SET — opclaude set-key {pname}"
        print()
        print(bold(pname) + (f"  {dim(status)}" if status else ""))
        for model_id, e in prov_rows:
            name = e["model_name"]
            tags = []
            if e.get("model_info", {}).get("supports_reasoning"):
                tags.append("reasoning")
            if name == default:
                tags.append("default")
            tag = f"  ({', '.join(tags)})" if tags else ""
            print(f"  {name:<{width}}{dim(model_id)}{tag}")
        if disabled:
            print(dim(f"  + {disabled} disabled"))
    print()
    print(dim("opclaude --model <name>   ·   opclaude models add <provider> <id>   ·   opclaude models edit"))


def add_model(provider, model_id, kv):
    """Insert `model_id:` under providers.<provider>.models, keeping the
    file's comments and layout intact (plain text insert, then re-validate)."""
    data = load_models_file()
    if provider not in data["providers"]:
        raise ConfigError(
            f"unknown provider {provider!r}; known: {', '.join(data['providers'])}. "
            "Add new providers with `opclaude models edit`."
        )
    if model_id in ((data["providers"][provider] or {}).get("models") or {}):
        raise ConfigError(f"{provider}/{model_id} is already listed")

    opts = {}
    for item in kv:
        k, sep, v = item.partition("=")
        if not sep:
            raise ConfigError(f"expected key=value, got {item!r}")
        opts[k] = yaml.safe_load(v)
    line_value = ""
    if opts:
        line_value = " " + yaml.safe_dump(opts, default_flow_style=True).strip()

    with open(MODELS_FILE) as f:
        lines = f.readlines()
    prov_re = re.compile(rf"^(\s+){re.escape(provider)}:\s*(#.*)?$")
    i = next((i for i, l in enumerate(lines) if prov_re.match(l)), None)
    if i is None:
        raise ConfigError(f"couldn't locate `{provider}:` in {MODELS_FILE}; edit it by hand")
    prov_indent = len(prov_re.match(lines[i]).group(1))
    j = i + 1
    while j < len(lines):
        l = lines[j]
        if l.strip() and not l.lstrip().startswith("#"):
            indent = len(l) - len(l.lstrip())
            if indent <= prov_indent:
                break
            if l.strip().startswith("models:"):
                break
        j += 1
    if j >= len(lines) or not lines[j].strip().startswith("models:"):
        raise ConfigError(f"`{provider}` has no `models:` block; edit {MODELS_FILE} by hand")
    models_indent = len(lines[j]) - len(lines[j].lstrip())
    # Append after the last model line of this block.
    k = j + 1
    item_indent = None
    last = j
    while k < len(lines):
        l = lines[k]
        if l.strip() and not l.lstrip().startswith("#"):
            indent = len(l) - len(l.lstrip())
            if indent <= models_indent:
                break
            item_indent = item_indent or indent
            last = k
        k += 1
    item_indent = item_indent or models_indent + 2
    new_line = " " * item_indent + f"{model_id}:{line_value}\n"
    backup = "".join(lines)
    lines.insert(last + 1, new_line)
    with open(MODELS_FILE, "w") as f:
        f.writelines(lines)
    try:
        data = load_models_file()
        name = next(e["model_name"] for p, m, e in expand(data) if p == provider and m == model_id)
    except Exception:
        with open(MODELS_FILE, "w") as f:
            f.write(backup)
        raise
    print(f"Added {name}")


def main(argv):
    cmd = argv[0] if argv else "list"
    try:
        if cmd == "build":
            n = build()
            print(f"Generated {GENERATED_CONFIG} ({n} models)", file=sys.stderr)
        elif cmd == "list":
            list_models(argv[1] if len(argv) > 1 else None)
        elif cmd == "default":
            print(resolve_default(load_models_file()))
        elif cmd == "add":
            if len(argv) < 3:
                raise ConfigError("usage: opclaude models add <provider> <model-id> [key=value ...]")
            add_model(argv[1], argv[2], argv[3:])
        elif cmd == "path":
            ensure_models_file()
            print(MODELS_FILE)
        else:
            raise ConfigError(f"unknown command {cmd!r}")
    except (ConfigError, yaml.YAMLError) as e:
        print(f"opclaude: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
