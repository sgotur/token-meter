#!/usr/bin/env python3
"""token_meter.py - Claude Code hook: token use and estimated cost, per turn and per session.

Install (merges into ~/.claude/settings.json, keeps a .bak backup):
    python3 token_meter.py --install              # prints after every finished response (Stop)
    python3 token_meter.py --install --per-tool   # also prints a one-line update after every tool call
    python3 token_meter.py --install --statusline # also adds a permanent status bar at the bottom
    python3 token_meter.py --install --statusline-only   # status bar only; silent Stop hook feeds it
    python3 token_meter.py --uninstall

Status bar: add --force to replace a statusLine you already have. The bar shows model, context
use, current/last turn tokens and cost, and session tokens and cost; a second line carries a tip
and the cost-optimization link (TOKEN_METER_STATUS_LINES=1 keeps it to one line).

Claude Code runs it as a hook and feeds a JSON payload on stdin (session_id, transcript_path, ...).
Token counts are read from the session transcript (message.usage on each assistant message), so
they match what the API reported. Costs are ESTIMATES at API list prices; on a Pro/Max plan you are
not billed per token, so read the dollar figure as "API-equivalent".

Environment variables (all optional):
    TOKEN_METER_MODE         message (default, shown by Claude Code) | tty (write to /dev/tty)
    TOKEN_METER_BUDGET       session budget in USD; a tip appears once it is passed
    TOKEN_METER_CONTEXT_WARN prompt-size tokens that trigger the /compact tip (default 150000)
    TOKEN_METER_COSTS_URL    link to cost-optimization guidance
    TOKEN_METER_PROMPT_URL   link to prompting guidance
    TOKEN_METER_HOME         state directory (default ~/.claude/token-meter)
Price overrides: put {"sonnet-5-5": [input, cache_write_5m, cache_write_1h, cache_read, output]}
(USD per million tokens) in <state dir>/pricing.json.
"""
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("TOKEN_METER_HOME") or (Path.home() / ".claude" / "token-meter"))
COSTS_URL = os.environ.get("TOKEN_METER_COSTS_URL", "https://docs.anthropic.com/en/docs/claude-code/costs")
PROMPT_URL = os.environ.get(
    "TOKEN_METER_PROMPT_URL",
    "https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/overview",
)

# USD per million tokens: (input, cache write 5m, cache write 1h, cache read, output)
# Source: Anthropic pricing page (platform.claude.com/docs/about-claude/pricing).
PRICES = {
    "fable-5-1": (10, 12.5, 20, 0.25, 50),
    "mythos-5-1": (10, 12.5, 20, 0.25, 50),
    "fable-5": (10, 12.5, 20, 1, 50),
    "mythos-5": (10, 12.5, 20, 1, 50),
    "opus-5-5": (4, 5, 8, 0.20, 20),
    "opus-5": (5, 6.25, 10, 0.50, 25),
    "opus-4-8": (5, 6.25, 10, 0.50, 25),
    "opus-4-7": (5, 6.25, 10, 0.50, 25),
    "opus-4-6": (5, 6.25, 10, 0.50, 25),
    "opus-4-5": (5, 6.25, 10, 0.50, 25),
    "opus-4-1": (15, 18.75, 30, 1.50, 75),
    "opus-4": (15, 18.75, 30, 1.50, 75),
    "sonnet-5-5": (2, 2.5, 4, 0.10, 10),
    "sonnet-5": (2, 2.5, 4, 0.20, 10),
    "sonnet-4-6": (3, 3.75, 6, 0.30, 15),
    "sonnet-4-5": (3, 3.75, 6, 0.30, 15),
    "sonnet-4": (3, 3.75, 6, 0.30, 15),
    "haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
    "haiku-4-5": (1, 1.25, 2, 0.10, 5),
    "haiku-3-5": (0.80, 1, 1.60, 0.08, 4),
}
HAIKU_55_LONG = (0.50, 0.625, 1, 0.05, 2.50)  # Haiku 5.5 prompts over 100K tokens
FALLBACK_SLUG = "sonnet-5-5"


def load_prices():
    prices = dict(PRICES)
    try:
        extra = json.loads((STATE_DIR / "pricing.json").read_text())
        for k, v in extra.items():
            if isinstance(v, list) and len(v) == 5:
                prices[k] = tuple(float(x) for x in v)
    except (OSError, ValueError):
        pass
    return prices


def normalize_model(model):
    m = (model or "").lower().strip()
    m = re.sub(r"\[.*?\]", "", m)          # e.g. "[1m]" suffix
    m = re.sub(r"^claude-", "", m)
    m = re.sub(r"-latest$", "", m)
    m = re.sub(r"-\d{8}$", "", m)          # date suffix
    return m


def price_for(model, prices):
    """Return (price tuple, slug, known)."""
    m = normalize_model(model)
    if m in prices:
        return prices[m], m, True
    best = None
    for slug in prices:
        if m.startswith(slug + "-") and (best is None or len(slug) > len(best)):
            best = slug
    if best:
        return prices[best], best, True
    return prices[FALLBACK_SLUG], FALLBACK_SLUG, False


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def read_messages(path):
    """Unique assistant messages with usage. A message streamed over several transcript lines
    shares one id, so keep the field-wise maximum instead of double counting."""
    msgs, order = {}, []
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            if obj.get("type") != "assistant" and msg.get("role") != "assistant":
                continue
            model = msg.get("model") or ""
            if model.startswith("<"):       # "<synthetic>" placeholder messages
                continue
            cw_total = _int(usage.get("cache_creation_input_tokens"))
            cc = usage.get("cache_creation")
            if isinstance(cc, dict) and (cc.get("ephemeral_5m_input_tokens") is not None
                                         or cc.get("ephemeral_1h_input_tokens") is not None):
                cw5 = _int(cc.get("ephemeral_5m_input_tokens"))
                cw1 = _int(cc.get("ephemeral_1h_input_tokens"))
            else:
                cw5, cw1 = cw_total, 0
            rec = {
                "model": model,
                "in": _int(usage.get("input_tokens")),
                "out": _int(usage.get("output_tokens")),
                "cr": _int(usage.get("cache_read_input_tokens")),
                "cw5": cw5,
                "cw1": cw1,
            }
            key = msg.get("id") or obj.get("uuid") or "line%d" % i
            if key in msgs:
                prev = msgs[key]
                for k in ("in", "out", "cr", "cw5", "cw1"):
                    prev[k] = max(prev[k], rec[k])
                if rec["model"]:
                    prev["model"] = rec["model"]
            else:
                msgs[key] = rec
                order.append(key)
    return [msgs[k] for k in order]


def message_cost(rec, prices):
    p, slug, known = price_for(rec["model"], prices)
    prompt_len = rec["in"] + rec["cr"] + rec["cw5"] + rec["cw1"]
    if slug == "haiku-5-5" and prompt_len > 100_000:
        p = HAIKU_55_LONG
    return (rec["in"] * p[0] + rec["cw5"] * p[1] + rec["cw1"] * p[2]
            + rec["cr"] * p[3] + rec["out"] * p[4]) / 1_000_000, known


def totals_of(msgs, prices):
    t = {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0, "calls": 0, "approx": False}
    for r in msgs:
        c, known = message_cost(r, prices)
        t["in"] += r["in"]
        t["out"] += r["out"]
        t["cr"] += r["cr"]
        t["cw"] += r["cw5"] + r["cw1"]
        t["cost"] += c
        t["calls"] += 1
        if not known:
            t["approx"] = True
    return t


def diff(now, base):
    if not base or any(now[k] < base.get(k, 0) for k in ("in", "out", "cr", "cw", "calls")):
        base = {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0, "calls": 0}  # new/compacted transcript
    d = {k: now[k] - base.get(k, 0) for k in ("in", "out", "cr", "cw", "cost", "calls")}
    d["approx"] = now["approx"]
    return d


def fmt(n):
    return "{:,}".format(int(n))


def money(x, approx=False):
    s = "${:,.4f}".format(x) if x < 1 else "${:,.2f}".format(x)
    return ("~" + s) if approx else s


def pick_tip(turn, session, last_prompt_len):
    warn = _int(os.environ.get("TOKEN_METER_CONTEXT_WARN")) or 150_000
    try:
        budget = float(os.environ.get("TOKEN_METER_BUDGET", ""))
    except ValueError:
        budget = 0.0
    prompt = turn["in"] + turn["cr"] + turn["cw"]
    if budget and session["cost"] >= budget:
        return "Session spend passed your %s budget: consider /clear, a cheaper /model, or lower /effort." % money(budget)
    if last_prompt_len >= warn:
        return "Context is ~%s tokens and re-sent every call: run /compact <focus>, or /clear between tasks." % fmt(last_prompt_len)
    if prompt > 20_000 and turn["cr"] / max(prompt, 1) < 0.5:
        return "Low cache hits this turn: keep CLAUDE.md and early context stable so the prefix can be cached."
    if turn["out"] > 8_000:
        return "Long outputs cost the most per token: ask for diffs or concise answers."
    return "Name exact files and give a test command in your prompt to cut exploratory reads."


def render_full(model, turn, session, tip):
    return "\n".join([
        "[token-meter] %s | %d model call%s this turn" % (model or "model", turn["calls"], "" if turn["calls"] == 1 else "s"),
        "  This turn  in %s | out %s | cache read %s | cache write %s | est. cost %s" % (
            fmt(turn["in"]), fmt(turn["out"]), fmt(turn["cr"]), fmt(turn["cw"]), money(turn["cost"], turn["approx"])),
        "  Session    in %s | out %s | cache read %s | cache write %s | est. cost %s (%d call%s)" % (
            fmt(session["in"]), fmt(session["out"]), fmt(session["cr"]), fmt(session["cw"]),
            money(session["cost"], session["approx"]), session["calls"],
            "" if session["calls"] == 1 else "s"),
        "  Tip: " + tip,
        "  Optimize: %s" % COSTS_URL,
        "  Prompting: %s" % PROMPT_URL,
    ])


def render_brief(delta, session):
    return "[token-meter] +in %s out %s cache-read %s | +%s | session %s" % (
        fmt(delta["in"]), fmt(delta["out"]), fmt(delta["cr"]),
        money(delta["cost"], delta["approx"]), money(session["cost"], session["approx"]))


def emit(text):
    mode = os.environ.get("TOKEN_METER_MODE", "message").lower()
    if mode == "tty":
        try:
            with open("/dev/tty", "w") as tty:
                tty.write(text + "\n")
            return
        except OSError:
            pass
    sys.stdout.write(json.dumps({"systemMessage": text}) + "\n")


def prune_state(max_age_days=30):
    cutoff = time.time() - max_age_days * 86400
    try:
        for p in STATE_DIR.glob("*.json"):
            if p.name != "pricing.json" and p.stat().st_mtime < cutoff:
                p.unlink()
    except OSError:
        pass


def run_hook():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        return 0
    event = payload.get("hook_event_name") or "Stop"
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(payload.get("session_id") or "unknown"))

    prices = load_prices()
    msgs = read_messages(path)
    if not msgs:
        return 0
    session = totals_of(msgs, prices)
    last = msgs[-1]
    last_prompt_len = last["in"] + last["cr"] + last["cw5"] + last["cw1"]

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / (sid + ".json")
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}

    if event == "PostToolUse":
        delta = diff(session, state.get("last"))
        state["last"] = session
        if delta["calls"] > 0:
            emit(render_brief(delta, session))
    else:
        turn = diff(session, state.get("turn"))
        state["turn"] = session
        state["last"] = session
        if turn["calls"] > 0:
            state["last_turn"] = turn
            if os.environ.get("TOKEN_METER_QUIET") != "1":
                emit(render_full(last["model"], turn, session, pick_tip(turn, session, last_prompt_len)))

    tmp = state_file.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state))
        tmp.replace(state_file)
    except OSError:
        pass
    prune_state()
    return 0


def fmt_k(n):
    n = int(n)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1e6)
    if n >= 10_000:
        return "%dk" % round(n / 1e3)
    if n >= 1_000:
        return "%.1fk" % (n / 1e3)
    return str(n)


BAR_STYLE = "\033[0;1;37;40m"   # bold white text on black background
BAR_RESET = "\033[0m"


def _c(code, text):
    """Colour a fragment, then return to the bar style (not a full reset) so the black
    background and bold white text continue after it."""
    return text if os.environ.get("NO_COLOR") else "\033[%sm%s%s" % (code, text, BAR_STYLE)


def bar_line(text):
    """Wrap a status line: black background, bold white text, background filled to the edge."""
    return BAR_STYLE + " " + text + " \033[K" + BAR_RESET


def _budget_color(cost):
    try:
        b = float(os.environ.get("TOKEN_METER_BUDGET", ""))
    except ValueError:
        return None
    if b <= 0:
        return None
    return "31" if cost >= b else ("33" if cost >= 0.8 * b else "32")


def _dict(v):
    return v if isinstance(v, dict) else {}


def run_statusline():
    """Permanent bar at the bottom of Claude Code (statusLine command). Reads the same transcript
    as the hook, cached by file size and mtime so frequent refreshes stay cheap."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    payload = _dict(payload)
    model = _dict(payload.get("model"))
    name = model.get("display_name") or normalize_model(model.get("id")) or "claude"
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(payload.get("session_id") or "unknown"))
    prices = load_prices()
    path = payload.get("transcript_path")

    session, last_prompt = None, 0
    cache_file = STATE_DIR / (sid + ".sl.json")
    if path and os.path.exists(path):
        try:
            st = os.stat(path)
            key = [st.st_size, st.st_mtime_ns]
            try:
                cache = json.loads(cache_file.read_text())
            except (OSError, ValueError):
                cache = {}
            if cache.get("key") == key and "session" in cache:
                session, last_prompt = cache["session"], cache.get("last_prompt", 0)
            else:
                msgs = read_messages(path)
                if msgs:
                    session = totals_of(msgs, prices)
                    m = msgs[-1]
                    last_prompt = m["in"] + m["cr"] + m["cw5"] + m["cw1"]
                    STATE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps({"key": key, "session": session, "last_prompt": last_prompt}))
        except OSError:
            pass
    if session is None:  # no transcript yet: fall back to what Claude Code passes in
        cw, cost = _dict(payload.get("context_window")), _dict(payload.get("cost"))
        try:
            usd = float(cost.get("total_cost_usd") or 0)
        except (TypeError, ValueError):
            usd = 0.0
        session = {"in": _int(cw.get("total_input_tokens")), "out": _int(cw.get("total_output_tokens")),
                   "cr": 0, "cw": 0, "cost": usd, "calls": 0, "approx": True}

    zero = {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0, "calls": 0, "approx": False}
    try:
        state = json.loads((STATE_DIR / (sid + ".json")).read_text())
    except (OSError, ValueError):
        state = {}
    live = diff(session, state.get("turn")) if state.get("turn") else zero
    if live["calls"] > 0:
        turn, label = live, "turn"
    else:
        turn, label = state.get("last_turn") or zero, "last"

    sep = " │ "
    parts = [name]
    cwin = _dict(payload.get("context_window"))
    pct = cwin.get("used_percentage")
    if pct is None and cwin.get("context_window_size") and last_prompt:
        pct = last_prompt * 100.0 / cwin["context_window_size"]
    try:
        if pct is not None:
            pct = float(pct)
            parts.append("ctx " + _c("31" if pct >= 80 else ("33" if pct >= 60 else "32"), "%d%%" % round(pct)))
        elif last_prompt:
            parts.append("ctx " + fmt_k(last_prompt))
    except (TypeError, ValueError):
        pass
    if session["calls"] or session["cost"]:
        if turn["calls"]:
            parts.append("%s in %s out %s %s" % (label, fmt_k(turn["in"]), fmt_k(turn["out"]),
                                                  money(turn["cost"], turn.get("approx", False))))
        sc = money(session["cost"], session["approx"])
        col = _budget_color(session["cost"])
        parts.append("session in %s out %s cache %s %s" % (
            fmt_k(session["in"]), fmt_k(session["out"]), fmt_k(session["cr"]),
            _c(col, sc) if col else sc))
    else:
        parts.append("no usage yet")
    print(bar_line(sep.join(parts)))

    if os.environ.get("TOKEN_METER_STATUS_LINES", "2") != "1":
        ref = turn if turn["calls"] else zero
        print(bar_line("tip: %s | %s" % (pick_tip(ref, session, last_prompt), COSTS_URL)))
    return 0


def _settings_path():
    return Path.home() / ".claude" / "settings.json"


def _load_settings():
    sp = _settings_path()
    if not sp.exists():
        return {}
    text = sp.read_text().strip()
    data = json.loads(text) if text else {}
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    shutil.copy2(sp, sp.with_name("settings.json.bak"))
    return data


def _is_ours(hook, cmd_tail="token_meter.py"):
    return isinstance(hook, dict) and cmd_tail in str(hook.get("command", ""))


def install(per_tool, statusline=False, only=False, force=False):
    dest_dir = Path.home() / ".claude" / "hooks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "token_meter.py"
    if Path(__file__).resolve() != dest.resolve():
        shutil.copy2(__file__, dest)
    dest.chmod(0o755)
    try:
        data = _load_settings()
    except ValueError as e:
        print("Not changed: could not read %s (%s). Fix the JSON and retry." % (_settings_path(), e))
        return 1
    cmd = 'python3 "%s"' % dest
    # --statusline-only still needs a silent Stop hook so the bar can show "last turn" figures
    events = [("Stop", None)] + ([("PostToolUse", "*")] if per_tool and not only else [])
    hook_cmd = cmd + (" --quiet" if only else "")
    hooks = data.setdefault("hooks", {})
    for event, matcher in events:
        groups = hooks.setdefault(event, [])
        if any(_is_ours(h) for g in groups if isinstance(g, dict) for h in g.get("hooks", [])):
            continue
        group = {"hooks": [{"type": "command", "command": hook_cmd, "timeout": 10}]}
        if matcher is not None:
            group["matcher"] = matcher
        groups.append(group)
    bar = False
    if statusline or only:
        current = data.get("statusLine")
        if isinstance(current, dict) and current.get("command") and not _is_ours(current) and not force:
            print("statusLine already set to: %s\n  left unchanged (re-run with --force to replace it)." % current.get("command"))
        else:
            data["statusLine"] = {"type": "command", "command": cmd + " --statusline"}
            bar = True
    _settings_path().parent.mkdir(parents=True, exist_ok=True)
    _settings_path().write_text(json.dumps(data, indent=2) + "\n")
    print("Installed script: %s" % dest)
    print("Updated %s (backup: settings.json.bak)" % _settings_path())
    what = [e + " hook" for e, _ in events] + (["status bar"] if bar else [])
    print("Enabled: " + (", ".join(what) or "nothing new") + ". Restart Claude Code to load it.")
    return 0


def uninstall():
    sp = _settings_path()
    if not sp.exists():
        print("Nothing to remove.")
        return 0
    try:
        data = _load_settings()
    except ValueError as e:
        print("Not changed: %s" % e)
        return 1
    hooks = data.get("hooks", {})
    for event in list(hooks):
        kept = []
        for g in hooks[event]:
            if isinstance(g, dict):
                g["hooks"] = [h for h in g.get("hooks", []) if not _is_ours(h)]
                if g["hooks"]:
                    kept.append(g)
            else:
                kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
    if _is_ours(data.get("statusLine")):
        del data["statusLine"]
    sp.write_text(json.dumps(data, indent=2) + "\n")
    print("Removed token-meter entries from %s (backup: settings.json.bak)." % sp)
    return 0


def main():
    args = sys.argv[1:]
    if "--quiet" in args:
        os.environ["TOKEN_METER_QUIET"] = "1"
    if "--install" in args:
        return install("--per-tool" in args, "--statusline" in args, "--statusline-only" in args, "--force" in args)
    if "--uninstall" in args:
        return uninstall()
    if sys.stdin.isatty():
        print(__doc__)
        return 0
    try:
        if "--statusline" in args:
            return run_statusline()
        return run_hook()
    except Exception:
        return 0  # a metering problem must never interrupt Claude Code


if __name__ == "__main__":
    sys.exit(main())
