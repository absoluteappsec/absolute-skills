#!/usr/bin/env python3
"""Structural validator for the absolute-appmap plugin.

Usage: python scripts/validate.py [PLUGIN_ROOT]   (default: this script's plugin)

Checks (codereview's, plus appmap-specific ones):
- .claude-plugin/plugin.json is valid JSON with the required keys; its version matches
  lib/appmap/__init__.py.
- Every skills/*/SKILL.md has frontmatter with name + description; name == directory;
  description <= 1024 chars, starts with "Use when" / "Use during", and is a plain YAML
  scalar (no ": " or " #", which strict YAML parsers in other harnesses reject).
- SKILL.md body order: ## Purpose -> ## Step N ... -> ## Completion Criteria -> ## Reference Files.
- Referenced references/*.md files exist (relative to the skill directory, "../<skill>/" allowed).
- Every `appmap <subcommand>` written as code (inline or fenced) in the agent-facing
  markdown (skills, commands, AGENTS.md, README.md) is a registered CLI subcommand
  (introspected from appmap.cli.build_parser()), and its --flags, query recipes and choice
  values exist.
- Every scripts/<file> mentioned exists.
- No agent-facing markdown mentions `mcp__` (no MCP dependency).
- skills/query-app-map/references/schema.md is byte-identical to lib/appmap/resources/SCHEMA.md.
- commands/*.md have a description and pass $ARGUMENTS.
- If the enclosing absolute-skills marketplace.json exists, it lists this plugin by path.
Exits non-zero on any failure. Needs only the standard library.
"""
import argparse
import glob
import json
import os
import re
import shlex
import sys

if sys.version_info < (3, 11):
    sys.exit(f"validate.py needs Python 3.11+ (this is {sys.version.split()[0]}); "
             "run it with the plugin's venv or python3.11+")

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else \
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
errors = []


def rel(path):
    return os.path.relpath(path, ROOT)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# -- 1. plugin.json ---------------------------------------------------------------
manifest = os.path.join(ROOT, ".claude-plugin", "plugin.json")
plugin_name = None
try:
    m = json.loads(read(manifest))
    for key in ("name", "version", "description", "author", "homepage", "license", "keywords"):
        if key not in m:
            errors.append(f"plugin.json missing key: {key}")
    plugin_name = m.get("name")
    init_py = os.path.join(ROOT, "lib", "appmap", "__init__.py")
    vm = re.search(r'^__version__ = "(.*)"', read(init_py), re.M) if os.path.exists(init_py) else None
    if vm and m.get("version") != vm.group(1):
        errors.append(f"plugin.json version {m.get('version')!r} != lib/appmap version {vm.group(1)!r}")
except Exception as e:
    errors.append(f"plugin.json invalid: {e}")

# -- 2. the CLI surface -----------------------------------------------------------
sys.path.insert(0, os.path.join(ROOT, "lib"))
try:
    from appmap.cli import build_parser  # stdlib-only import
    PARSER = build_parser()
except Exception as e:  # pragma: no cover - reported, not raised
    PARSER = None
    errors.append(f"cannot import appmap.cli.build_parser: {e}")


def subparsers(parser):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}


SUBCOMMANDS = subparsers(PARSER) if PARSER else {}
_NO_VALUE = (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._CountAction,
             argparse._HelpAction, argparse._VersionAction, argparse._StoreConstAction)


def option(parser, flag):
    for a in parser._actions:
        if flag in a.option_strings:
            return a
    return None


def positionals(parser):
    return [a for a in parser._actions
            if not a.option_strings and not isinstance(a, argparse._SubParsersAction)]


def placeholder(tok):
    return (not tok or tok[0] in "<[{$\"'(" or "..." in tok or "…" in tok
            or tok.upper() == tok or tok in ("R", "ID"))


def check_invocation(where, sub, rest):
    """Check the flags / positionals that follow `appmap <sub>` in one code segment."""
    parser = SUBCOMMANDS[sub]
    try:
        toks = shlex.split(rest, comments=False, posix=True)
    except ValueError:
        toks = rest.split()
    pos_actions = positionals(parser)
    recipes = subparsers(parser)
    i = 0
    while i < len(toks):
        raw = toks[i]
        tok = raw.strip("[]()`,;.")
        if raw.startswith("[") and not tok.startswith("-"):
            i += 1  # optional placeholder such as [options]
            continue
        if tok in ("|", "&&", "||", ";", ">", "2>&1") or tok.startswith(("#", "→")):
            break
        if tok.startswith("--"):
            flag, _, val = tok.partition("=")
            act = option(parser, flag)
            if act is None:
                errors.append(f"{where}: `appmap {sub}` has no option {flag}")
            elif not isinstance(act, _NO_VALUE):
                if not val and i + 1 < len(toks):
                    i += 1
                    val = toks[i].strip("[]()`,;.")
                if act.choices and val and not placeholder(val):
                    for v in val.split("|"):
                        if v not in act.choices:
                            errors.append(f"{where}: `appmap {sub} {flag}` has no value {v!r}")
        elif not placeholder(tok) and not tok.startswith("-"):
            if recipes and parser is SUBCOMMANDS[sub]:
                if tok not in recipes:
                    errors.append(f"{where}: `appmap {sub}` has no recipe {tok!r}")
                    break
                parser = recipes[tok]
                pos_actions = positionals(parser)
            elif pos_actions:
                act = pos_actions.pop(0)
                if act.choices:
                    for v in tok.split("|"):
                        if v not in act.choices:
                            errors.append(f"{where}: `appmap {sub}` has no action {v!r}")
            else:
                break  # prose or another command after the invocation
        i += 1


MENTION = re.compile(r"(?:(?<=scripts/)|(?<![\w./-]))appmap[ \t]+([a-z][a-z-]*)")
# Shell variables holding the wrapper path (``A=.../scripts/appmap``) are invocations too: ``$A crawl``.
WRAPPER_VAR = re.compile(r"\b([A-Za-z_]\w*)=[\"']?\S*scripts/appmap\b")


def mention_re(text):
    names = sorted(set(WRAPPER_VAR.findall(text)))
    if not names:
        return MENTION
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(MENTION.pattern[:-len(r"appmap[ \t]+([a-z][a-z-]*)")]
                      + r"(?:appmap|\$\{?(?:" + alt + r")\}?\"?)[ \t]+([a-z][a-z-]*)")


FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
INLINE = re.compile(r"`([^`\n]+)`")


def code_segments(text):
    """Code pieces: fenced lines (with \\ continuations joined) and inline code spans.
    Prose ("an appmap run") is not a command, so it is not checked."""
    segs = []
    prose = FENCE.sub(lambda mm: segs.extend(
        mm.group(1).replace("\\\n", " ").splitlines()) or "\n", text)
    return segs + INLINE.findall(prose)


def check_mentions(path):
    where = rel(path)
    text = read(path)
    mention = mention_re(text)
    for seg in code_segments(text):
        seg = re.sub(r"\s#\s.*$", "", seg)  # shell comment
        matches = list(mention.finditer(seg))
        for n, mm in enumerate(matches):
            sub = mm.group(1)
            if sub not in SUBCOMMANDS:
                if SUBCOMMANDS:
                    errors.append(f"{where}: `appmap {sub}` is not a CLI subcommand "
                                  f"(have: {', '.join(SUBCOMMANDS)})")
                continue
            end = matches[n + 1].start() if n + 1 < len(matches) else len(seg)
            check_invocation(where, sub, seg[mm.end():end])


# -- 3. SKILL.md files ------------------------------------------------------------
def parse_frontmatter(text):
    mm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not mm:
        return None
    fm = {}
    for line in mm.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


SECTIONS = re.compile(r"^## (Purpose|Step \d+[^\n]*|Completion Criteria|Reference Files)\s*$", re.M)
REF = re.compile(r"(?<![\w/-])((?:\.\./[\w-]+/)?references/[\w./-]+\.md)")

skills = sorted(glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md")))
if not skills:
    errors.append("no SKILL.md files found under skills/")
for path in skills:
    r = rel(path)
    text = read(path)
    fm = parse_frontmatter(text)
    if fm is None:
        errors.append(f"{r}: missing or malformed frontmatter")
        continue
    name, desc = fm.get("name", ""), fm.get("description", "")
    parent = os.path.basename(os.path.dirname(path))
    if not name:
        errors.append(f"{r}: frontmatter missing name")
    elif name != parent:
        errors.append(f"{r}: name '{name}' != directory '{parent}'")
    if not desc:
        errors.append(f"{r}: frontmatter missing description")
    else:
        if len(desc) > 1024:
            errors.append(f"{r}: description too long ({len(desc)} > 1024)")
        if not desc.strip("'\"").startswith(("Use when", "Use during")):
            errors.append(f"{r}: description must start with 'Use when' or 'Use during'")
        if desc[0] not in "'\"" and (": " in desc or " #" in desc):
            errors.append(f"{r}: unquoted description contains ': ' or ' #' (invalid plain YAML)")
    order = [s.split()[0] for s in SECTIONS.findall(text)]
    want_tail = ["Completion", "Reference"]
    if not order or order[0] != "Purpose" or "Step" not in order \
            or [s for s in order if s != "Step"][-2:] != want_tail \
            or order.index("Step") > order.index("Completion") \
            or max(i for i, s in enumerate(order) if s == "Step") > order.index("Completion"):
        errors.append(f"{r}: body must be ## Purpose -> ## Step N ... -> "
                      "## Completion Criteria -> ## Reference Files")
    skill_dir = os.path.dirname(path)
    for ref_path in [path] + glob.glob(os.path.join(skill_dir, "references", "*.md")):
        for ref in REF.findall(read(ref_path)):
            if not os.path.exists(os.path.normpath(os.path.join(skill_dir, ref))):
                errors.append(f"{rel(ref_path)}: referenced file not found: {ref}")

# -- 4. agent-facing markdown: CLI mentions, scripts, MCP ---------------------------
agent_md = sorted(set(glob.glob(os.path.join(ROOT, "skills", "**", "*.md"), recursive=True)
                      + glob.glob(os.path.join(ROOT, "commands", "*.md"))
                      + [p for p in (os.path.join(ROOT, "AGENTS.md"),
                                     os.path.join(ROOT, "README.md")) if os.path.exists(p)]))
SCRIPT = re.compile(r"(?<![\w/.-])scripts/([\w-]+(?:\.\w+)?)")
for path in agent_md:
    text = read(path)
    if PARSER:
        check_mentions(path)
    for s in set(SCRIPT.findall(text)):
        if not os.path.exists(os.path.join(ROOT, "scripts", s)):
            errors.append(f"{rel(path)}: scripts/{s} does not exist")
    if "mcp__" in text:
        errors.append(f"{rel(path)}: mentions an MCP tool (mcp__); skills must not depend on MCP")

# -- 5. schema reference copy --------------------------------------------------------
src = os.path.join(ROOT, "lib", "appmap", "resources", "SCHEMA.md")
copy = os.path.join(ROOT, "skills", "query-app-map", "references", "schema.md")
if not os.path.exists(copy):
    errors.append("skills/query-app-map/references/schema.md is missing")
elif not os.path.exists(src) or open(src, "rb").read() != open(copy, "rb").read():
    errors.append("skills/query-app-map/references/schema.md differs from "
                  "lib/appmap/resources/SCHEMA.md (copy it again)")

# -- 6. commands ------------------------------------------------------------------
commands = sorted(glob.glob(os.path.join(ROOT, "commands", "*.md")))
if not commands:
    errors.append("no commands found under commands/")
for path in commands:
    text = read(path)
    fm = parse_frontmatter(text)
    if not fm or not fm.get("description"):
        errors.append(f"{rel(path)}: frontmatter missing description")
    if "$ARGUMENTS" not in text:
        errors.append(f"{rel(path)}: does not pass $ARGUMENTS")

# -- 7. enclosing marketplace (only when this plugin sits in absolute-skills) ---------
market = os.path.join(ROOT, "..", "..", ".claude-plugin", "marketplace.json")
if os.path.exists(market) and plugin_name:
    try:
        mk = json.loads(read(market))
        entry = next((p for p in mk.get("plugins", []) if p.get("name") == plugin_name), None)
        if entry is None:
            errors.append(f"marketplace.json: does not list plugin {plugin_name!r}")
        else:
            target = os.path.normpath(os.path.join(os.path.dirname(market), "..",
                                                   entry.get("source", "")))
            if target != os.path.normpath(ROOT):
                errors.append(f"marketplace.json: source {entry.get('source')!r} does not "
                              "point at this plugin")
    except Exception as e:
        errors.append(f"marketplace.json invalid: {e}")

if errors:
    print("VALIDATION FAILED:")
    for e in errors:
        print("  - " + e)
    sys.exit(1)
print(f"OK: validated {len(skills)} skill(s) and plugin.json")
