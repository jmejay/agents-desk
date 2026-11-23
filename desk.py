#!/usr/bin/env python3
"""Desk: every Claude Code or Codex session stays on the desk until it is closed.

Hooks append events to sessions.jsonl; desk.md is rebuilt from it.

  desk                      rebuild the desk and reveal desk.md in Finder
  desk show                 print the desk to the terminal
  desk close <id> [note]    close a session (id prefix is enough)
  desk reopen <id>          reopen a closed or removed session
  desk rm <id>...           drop sessions from the desk for good (resuming won't bring them back)
  desk sweep [--days N]     add sessions the hooks missed; close ones whose PR merged
  desk hook [codex]         hook entry point (reads the hook JSON on stdin; Claude unless "codex")
"""
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get("DESK_DIR") or HOME / ".desk")
LEDGER = ROOT / "sessions.jsonl"
DESK_MD = ROOT / "desk.md"
PROJECTS = HOME / ".claude" / "projects"
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
CODEX_INTERACTIVE = {"cli", "vscode"}  # not exec (headless) or subagents
TAIL_BYTES = 512 * 1024
SNIPPET = 240
TRUNK = {"main", "master", "dev", "preprod", "HEAD", ""}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def append(event):
    event.setdefault("ts", now())
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


class locked:
    """Serialise ledger writes and renders across concurrent hooks. Gives up waiting after a
    couple of seconds (Codex allows SessionEnd hooks 3s) rather than stall a session."""

    def __enter__(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.f = open(ROOT / ".lock", "w")
        for _ in range(40):
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.f.close()  # releases the lock


def read_ledger():
    if not LEDGER.exists():
        return []
    out = []
    with open(LEDGER) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


# ---------- transcript reading ----------

def _records(path, tail=True):
    try:
        with open(path, "rb") as f:
            if tail:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - TAIL_BYTES))
                if size > TAIL_BYTES:
                    f.readline()  # drop the partial first line
            data = f.read()
    except OSError:
        return []
    out = []
    for line in data.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _text_of(msg):
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_human_prompt(rec):
    if rec.get("type") != "user" or rec.get("isMeta"):
        return False
    t = _text_of(rec.get("message")).strip()
    return bool(t) and not t.startswith("<")


def read_transcript(path):
    """Title, last assistant text, cwd, branch, number of human prompts, when the last turn was prompted."""
    info = {"title": None, "last": None, "cwd": None, "branch": None, "prompts": 0, "prompt_ts": None}
    if not path or not Path(path).exists():
        return info
    recs = _records(path)
    first_prompt = None
    for r in recs:
        t = r.get("type")
        if t == "custom-title" and r.get("customTitle"):
            info["custom"] = r["customTitle"]
        elif t == "ai-title" and r.get("aiTitle"):
            info["title"] = r["aiTitle"]
        elif t == "assistant":
            txt = _text_of(r.get("message")).strip()
            if txt:
                info["last"] = txt
        elif t == "user" and not r.get("isMeta") and _text_of(r.get("message")).strip():
            info["prompt_ts"] = r.get("timestamp")  # anything that starts a turn: prompt, slash command, notification
            if _is_human_prompt(r):
                info["prompts"] += 1
                first_prompt = first_prompt or _text_of(r.get("message")).strip()
        if r.get("cwd"):
            info["cwd"] = r["cwd"]
        if r.get("gitBranch"):
            info["branch"] = r["gitBranch"]
    custom = info.pop("custom", None)  # a /rename beats the AI title
    if custom:
        info["title"] = custom
    if not info["title"] and first_prompt:
        info["title"] = one_line(first_prompt, 80)
    return info


def one_line(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def last_paragraph(text):
    paras = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    return paras[-1] if paras else ""


def asks_question(text):
    para = last_paragraph(text)
    for line in para.splitlines():
        if re.sub(r"[\s*_`)\]>]+$", "", line).endswith("?"):
            return True
    return False


def git_branch(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "branch", "--show-current"],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or None
    except Exception:
        return None


def codex_threads(where="1", args=()):
    """Rows from Codex's thread table (state_N.sqlite, read-only). [] if Codex isn't there or the
    schema moved: Codex's storage is internal and may change under us."""
    dbs = sorted(CODEX_HOME.glob("state_*.sqlite"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    if not dbs:
        return []
    try:
        db = sqlite3.connect(f"file:{dbs[-1]}?mode=ro", uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(
                "select id, rollout_path, updated_at, source, cwd, title, name, git_branch, archived"
                f" from threads where {where}", args)]
        finally:
            db.close()
    except sqlite3.Error:
        return []


def codex_last_reply(path):
    """The final reply of the last completed turn in a Codex rollout file, if the file still exists."""
    last = None
    for r in _records(path) if path else []:
        p = r.get("payload")
        if r.get("type") == "event_msg" and isinstance(p, dict) and p.get("type") == "task_complete":
            last = p.get("last_agent_message") or last
    return last


def codex_title(row):
    return row.get("name") or (one_line(row.get("title"), 80) if row.get("title") else None)


# ---------- hook ----------

def print_start_context(sid):
    ctx = (f"This session is tracked on the desk (~/.desk/desk.md; \"desk\" is this session tracker, not email) as {sid[:8]}. "
           f"When the user says this work is finished, run `~/.desk/desk.py close {sid[:8]} \"<one-line outcome>\"`.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))


def codex_hook(payload, ev, sid, cwd, tp):
    """Codex sends the same four lifecycle events. Its transcript format is unstable, so the reply
    comes from the payload and the title and source from Codex's thread table."""
    rows = codex_threads("id = ?", (sid,))
    row = rows[0] if rows else {}
    if row and row.get("source") not in CODEX_INTERACTIVE:
        return  # `codex exec` or a subagent: like `claude -p`, not desk work
    base = {"sid": sid, "agent": "codex"}
    if ev == "SessionStart":
        append({**base, "ev": "start", "cwd": cwd, "branch": git_branch(cwd),
                "transcript": tp, "source": payload.get("source")})
        render()
        print_start_context(sid)
    elif ev == "UserPromptSubmit":
        # Codex transcripts give no reliable prompt time, so record it here; it decides whether
        # the stop that follows a close is the same turn (stay closed) or a new one (reopen)
        append({**base, "ev": "prompt", "turn": payload.get("turn_id")})
    elif ev == "Stop":
        last = payload.get("last_assistant_message") or ""
        append({**base, "ev": "stop", "cwd": cwd, "branch": row.get("git_branch") or git_branch(cwd),
                "transcript": tp, "title": codex_title(row),
                "last": one_line(last_paragraph(last), SNIPPET), "question": asks_question(last)})
        render()
    elif ev == "SessionEnd":
        append({**base, "ev": "end", "reason": payload.get("reason")})
        render()


def hook(agent="claude"):
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    ev = payload.get("hook_event_name")
    sid = payload.get("session_id")
    if not sid:
        return
    cwd = payload.get("cwd") or os.getcwd()
    tp = payload.get("transcript_path")
    if agent == "codex":
        return codex_hook(payload, ev, sid, cwd, tp)
    if ev == "SessionStart":
        append({"ev": "start", "sid": sid, "cwd": cwd, "branch": git_branch(cwd),
                "transcript": tp, "source": payload.get("source")})
        render()
        print_start_context(sid)
    elif ev == "Stop":
        info = read_transcript(tp)
        last = payload.get("last_assistant_message") or info["last"] or ""
        append({"ev": "stop", "sid": sid, "cwd": cwd, "branch": info["branch"] or git_branch(cwd),
                "transcript": tp, "title": info["title"], "prompt_ts": info["prompt_ts"],
                "last": one_line(last_paragraph(last), SNIPPET), "question": asks_question(last)})
        render()
    elif ev == "SessionEnd":
        append({"ev": "end", "sid": sid, "reason": payload.get("reason")})
        render()


# ---------- state ----------

def _dt(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def prompted_before(prompt_ts, closed_ts):
    """True when the turn was prompted before the close: the close ran inside that same turn."""
    try:
        return bool(prompt_ts and closed_ts) and _dt(prompt_ts) <= _dt(closed_ts)
    except ValueError:
        return False


def fold(events):
    s = {}
    for e in events:
        sid = e.get("sid")
        if not sid:
            continue
        x = s.setdefault(sid, {"sid": sid, "first": e["ts"], "last_ts": e["ts"], "turns": 0,
                               "live": False, "closed": False, "removed": False, "found": False})
        kind = e.get("ev")
        for k in ("agent", "cwd", "branch", "transcript", "title"):
            if e.get(k):
                x[k] = e[k]
        if kind in ("start", "stop", "found"):
            x["last_ts"] = e.get("activity", e["ts"])
        if kind == "start":
            x["live"] = True
        elif kind == "prompt":
            x["prompt_ts"] = e["ts"]
        elif kind == "stop":
            x["live"] = True
            x["turns"] += 1
            x["last"], x["question"] = e.get("last"), e.get("question", False)
            if not prompted_before(e.get("prompt_ts") or x.get("prompt_ts"), x.get("closed_ts")):
                x["closed"] = False  # a new prompt after the close: back on the list
        elif kind == "end":
            x["live"] = False
        elif kind == "found":
            x["found"] = True
            x["turns"] = max(x["turns"], e.get("turns", 1))
            x["last"], x["question"] = e.get("last"), e.get("question", False)
        elif kind == "close":
            x["closed"], x["note"], x["closed_ts"] = True, e.get("note"), e["ts"]
        elif kind == "rm":
            x["removed"] = True  # sticky: later turns do not undo it, only reopen
        elif kind == "reopen":
            x["closed"] = x["removed"] = False
    return s


def ago(ts):
    secs = time.time() - datetime.fromisoformat(ts).timestamp()
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit} ago"
    return "just now"


def entry(x):
    where = (x.get("cwd") or "?").replace(str(HOME), "~")
    br = x.get("branch")
    tag = " · found by sweep" if x["found"] else ""
    agent = x.get("agent", "claude")
    out = [f"- **{x.get('title') or '(untitled)'}** `{x['sid'][:8]}` {agent} · {where}"
           f"{' @ ' + br if br else ''} · {ago(x['last_ts'])}{tag}"]
    if x.get("last"):
        out.append(f"  > {x['last']}")
    resume = f"codex resume {x['sid']}" if agent == "codex" else f"claude -r {x['sid']}"
    return out + [f"  `cd {where} && {resume}`", ""]


def render():
    s = fold(read_ledger())
    open_ = [x for x in s.values() if not x["closed"] and not x["removed"] and x["turns"] > 0]
    groups = [
        ("Needs you: ended on a question", [x for x in open_ if x.get("question")]),
        ("Live in a terminal", [x for x in open_ if not x.get("question") and x["live"]]),
        ("Exited: resume to continue", [x for x in open_ if not x.get("question") and not x["live"]]),
    ]
    lines = [f"# Desk", "",
             f"{len(open_)} open · rebuilt {datetime.now().strftime('%a %d %b %H:%M')} · "
             f"close with `desk close <id> \"outcome\"`", ""]
    for name, rows in groups:
        if not rows:
            continue
        lines += [f"## {name} ({len(rows)})", ""]
        for x in sorted(rows, key=lambda r: r["last_ts"]):  # oldest first: those are the ones slipping
            lines += entry(x)
    if not open_:
        lines.append("Nothing open.")
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / f"desk.md.{os.getpid()}.tmp"  # per process, so concurrent renders can't collide
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, DESK_MD)
    # one line for the status line: "<needs you> <open>"
    tmp = ROOT / f"status.{os.getpid()}.tmp"
    tmp.write_text(f"{len(groups[0][1])} {len(open_)}\n")
    os.replace(tmp, ROOT / "status")
    return open_


# ---------- commands ----------

def resolve(prefix):
    ids = [sid for sid in fold(read_ledger()) if sid.startswith(prefix)]
    if len(ids) != 1:
        sys.exit(f"{'no' if not ids else 'ambiguous'} session matching {prefix!r}")
    return ids[0]


def sweep(days=14):
    s = fold(read_ledger())
    cutoff = time.time() - days * 86400
    added = closed = 0
    for path in PROJECTS.glob("*/*.jsonl"):
        sid = path.stem
        mtime = path.stat().st_mtime
        if sid in s or mtime < cutoff:
            continue
        info = read_transcript(str(path))
        if info["prompts"] == 0 or not info["last"]:
            continue  # empty, or headless with no human prompt in the tail
        append({"ev": "found", "sid": sid, "cwd": info["cwd"], "branch": info["branch"],
                "transcript": str(path), "title": info["title"], "turns": info["prompts"],
                "last": one_line(last_paragraph(info["last"]), SNIPPET),
                "question": asks_question(info["last"]),
                "activity": datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds")})
        added += 1
    for row in codex_threads("archived = 0 and updated_at >= ?", (int(cutoff),)):
        if row["id"] in s or row["source"] not in CODEX_INTERACTIVE:
            continue
        last = codex_last_reply(row["rollout_path"]) or ""
        append({"ev": "found", "sid": row["id"], "agent": "codex", "cwd": row["cwd"],
                "branch": row["git_branch"], "transcript": row["rollout_path"], "title": codex_title(row),
                "turns": 1, "last": one_line(last_paragraph(last), SNIPPET) or None,
                "question": asks_question(last),
                "activity": datetime.fromtimestamp(row["updated_at"], timezone.utc).isoformat(timespec="seconds")})
        added += 1
    # close open, exited sessions whose branch has a merged PR
    for x in fold(read_ledger()).values():
        br, cwd = x.get("branch") or "", x.get("cwd")
        if x["closed"] or x["removed"] or x["live"] or br in TRUNK or not cwd or not Path(cwd).is_dir():
            continue
        try:
            r = subprocess.run(["gh", "pr", "list", "--head", br, "--state", "merged",
                                "--json", "url", "-L", "1"], cwd=cwd,
                               capture_output=True, text=True, timeout=15)
            prs = json.loads(r.stdout or "[]")
        except Exception:
            continue
        if prs:
            append({"ev": "close", "sid": x["sid"], "note": f"PR merged: {prs[0]['url']}"})
            closed += 1
    render()
    print(f"sweep: {added} untracked session(s) added, {closed} closed on merged PRs")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "finder"
    if cmd == "hook":
        try:
            with locked():
                hook("codex" if argv[2:3] == ["codex"] else "claude")
        except Exception as e:  # a hook must never break a session
            print(f"desk: {e}", file=sys.stderr)
        return
    if cmd == "finder":
        render()
        subprocess.run(["open", "-R", str(DESK_MD)], check=False)
    elif cmd == "show":
        render()
        print(DESK_MD.read_text(), end="")
    elif cmd == "close" and len(argv) > 2:
        sid = resolve(argv[2])
        with locked():
            append({"ev": "close", "sid": sid, "note": " ".join(argv[3:]) or None})
            render()
        print(f"closed {sid[:8]}")
    elif cmd == "reopen" and len(argv) > 2:
        sid = resolve(argv[2])
        with locked():
            append({"ev": "reopen", "sid": sid})
            render()
        print(f"reopened {sid[:8]}")
    elif cmd == "rm" and len(argv) > 2:
        sids = [resolve(p) for p in argv[2:]]
        with locked():
            for sid in sids:
                append({"ev": "rm", "sid": sid})
            render()
        print("removed " + " ".join(sid[:8] for sid in sids))
    elif cmd == "sweep":
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 14
        sweep(days)
    else:
        print(__doc__.strip())


if __name__ == "__main__":
    main(sys.argv)
