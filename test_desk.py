import importlib.util
import io
import json
from pathlib import Path

import pytest

HERE = Path(__file__).parent


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_DIR", str(tmp_path / "desk"))
    spec = importlib.util.spec_from_file_location("desk", HERE / "desk.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_hook(desk, monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    desk.main(["desk", "hook"])


def test_session_end_leaves_no_exit_file(desk, monkeypatch, tmp_path):
    exit_file = tmp_path / "exit"
    exit_file.write_text("")
    monkeypatch.setenv("DESK_EXIT_FILE", str(exit_file))
    run_hook(desk, monkeypatch, {"hook_event_name": "SessionEnd", "session_id": "abc12345-x"})
    assert exit_file.read_text() == ""
    assert [e["ev"] for e in desk.read_ledger()] == ["end"]


@pytest.mark.parametrize("cmd", ["exit", "needs"])
def test_removed_commands_print_usage(desk, capsys, cmd):
    desk.main(["desk", cmd, "abc"])
    assert capsys.readouterr().out.strip() == desk.__doc__.strip()
    assert f"desk {cmd}" not in desk.__doc__


def test_show_prints_the_desk(desk, capsys, monkeypatch):
    opened = []
    monkeypatch.setattr(desk.subprocess, "run", lambda *a, **k: opened.append(a))
    desk.main(["desk", "show"])
    assert capsys.readouterr().out.startswith("# Desk")
    assert not opened  # terminal only; bare `desk` is the Finder one


def test_print_is_gone(desk, capsys):
    desk.main(["desk", "print"])
    assert capsys.readouterr().out.strip() == desk.__doc__.strip()


# ---------- Codex alongside Claude ----------

import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone


@pytest.fixture
def clock(desk, monkeypatch):
    """Each desk.now() call is one second after the last, so event order is unambiguous."""
    t = [datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).timestamp()]

    def tick():
        t[0] += 1
        return datetime.fromtimestamp(t[0], timezone.utc).isoformat(timespec="milliseconds")
    monkeypatch.setattr(desk, "now", tick)
    return t


@pytest.fixture
def codex_home(tmp_path, monkeypatch, desk):
    """A fake $CODEX_HOME holding Codex's thread table (state_5.sqlite) as 0.168 lays it out."""
    home = tmp_path / "codex"
    home.mkdir()
    db = sqlite3.connect(home / "state_5.sqlite")
    db.execute("create table threads (id text primary key, rollout_path text, updated_at integer,"
               " source text, cwd text, title text, name text, git_branch text, archived integer)")
    db.commit()
    db.close()
    monkeypatch.setattr(desk, "CODEX_HOME", home)
    return home


def add_thread(home, sid, source="cli", cwd="/repo", name=None, title="first prompt",
               branch="feat/x", archived=0, updated=None, rollout=None):
    db = sqlite3.connect(home / "state_5.sqlite")
    db.execute("insert into threads values (?,?,?,?,?,?,?,?,?)",
               (sid, rollout, int(updated or time.time()), source, cwd, title, name, branch, archived))
    db.commit()
    db.close()


def codex_hook(desk, monkeypatch, event, sid, **extra):
    payload = {"hook_event_name": event, "session_id": sid, "cwd": "/repo",
               "transcript_path": None, **extra}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    desk.main(["desk", "hook", "codex"])


def codex_turn(desk, monkeypatch, sid, reply, prompt="do it"):
    codex_hook(desk, monkeypatch, "UserPromptSubmit", sid, turn_id="t", prompt=prompt)
    codex_hook(desk, monkeypatch, "Stop", sid, turn_id="t", last_assistant_message=reply)


def desk_md(desk):
    desk.render()
    return desk.DESK_MD.read_text()


def test_codex_turn_puts_session_on_desk_with_codex_resume(desk, monkeypatch, codex_home, clock):
    sid = "01a1fab0-0000-0000-0000-000000000001"
    add_thread(codex_home, sid, source="vscode", name="Assess desk tool")
    codex_turn(desk, monkeypatch, sid, "Done.\n\nShall I commit?")
    md = desk_md(desk)
    assert "**Assess desk tool**" in md
    assert "codex" in md.split("Assess desk tool")[1].splitlines()[0]  # labelled on its line
    assert "> Shall I commit?" in md
    assert f"codex resume {sid}" in md
    assert "claude -r" not in md
    assert md.count(sid[:8]) == 1 + 1  # the id tag and the resume line: one entry


def test_codex_session_start_tells_the_agent_how_to_close(desk, monkeypatch, codex_home, capsys):
    codex_hook(desk, monkeypatch, "SessionStart", "01a1fab0-aaaa", source="startup")
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "desk.py close 01a1fab0" in ctx


def test_closing_inside_a_codex_turn_stays_closed(desk, monkeypatch, codex_home, clock):
    sid = "01a1aaaa-0000"
    add_thread(codex_home, sid)
    codex_hook(desk, monkeypatch, "UserPromptSubmit", sid, turn_id="t1", prompt="this is done, close it")
    desk.main(["desk", "close", sid[:8], "shipped"])  # the agent runs this during the turn
    codex_hook(desk, monkeypatch, "Stop", sid, turn_id="t1", last_assistant_message="Closed.")
    assert sid[:8] not in desk_md(desk)


def test_a_new_codex_prompt_after_close_reopens(desk, monkeypatch, codex_home, clock):
    sid = "01a1aaaa-0000"
    add_thread(codex_home, sid)
    codex_turn(desk, monkeypatch, sid, "Done.")
    desk.main(["desk", "close", sid[:8]])
    codex_turn(desk, monkeypatch, sid, "Picked it back up.")
    assert "Picked it back up." in desk_md(desk)


def test_rm_survives_new_codex_turns(desk, monkeypatch, codex_home, clock):
    sid = "01a1aaaa-0000"
    add_thread(codex_home, sid)
    codex_turn(desk, monkeypatch, sid, "Done.")
    desk.main(["desk", "rm", sid[:8]])
    codex_turn(desk, monkeypatch, sid, "Still here?")
    assert sid[:8] not in desk_md(desk)


@pytest.mark.parametrize("source", ["exec", '{"subagent":{"other":"guardian"}}'])
def test_headless_and_subagent_codex_threads_stay_off_the_desk(desk, monkeypatch, codex_home, clock, source):
    sid = "01a1bbbb-0000"
    add_thread(codex_home, sid, source=source)
    codex_turn(desk, monkeypatch, sid, "ok")
    assert sid[:8] not in desk_md(desk)


def test_claude_hook_is_unchanged(desk, monkeypatch, clock):
    sid = "c1a0dec0-0000-0000-0000-000000000002"
    run_hook(desk, monkeypatch, {"hook_event_name": "Stop", "session_id": sid, "cwd": "/repo",
                                 "last_assistant_message": "All done."})
    md = desk_md(desk)
    assert f"claude -r {sid}" in md
    assert "codex" not in md
    assert all(e.get("agent", "claude") == "claude" for e in desk.read_ledger())


def test_old_ledger_without_agent_renders_as_claude(desk):
    desk.ROOT.mkdir(parents=True)
    desk.LEDGER.write_text(json.dumps({"ev": "stop", "sid": "abcd1234-old", "ts": "2026-11-01T10:00:00+00:00",
                                       "cwd": "/repo", "last": "hi", "title": "Old one"}) + "\n")
    assert "claude -r abcd1234-old" in desk_md(desk)


def test_sweep_finds_interactive_codex_threads_once(desk, codex_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(desk, "PROJECTS", tmp_path / "no-claude-projects")
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("\n".join(json.dumps(r) for r in [
        {"type": "session_meta", "payload": {"id": "01a1cccc-0000"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "First."}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "commentary"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Merge it?"}},
        {"type": "mystery_record"},
    ]) + "\n")
    add_thread(codex_home, "01a1cccc-0000", source="cli", name="Found thread", rollout=str(rollout))
    add_thread(codex_home, "01a1dddd-0000", source="vscode", name="No transcript left",
               rollout=str(tmp_path / "gone.jsonl"))
    add_thread(codex_home, "01a1eeee-0000", source="exec", name="Headless")
    add_thread(codex_home, "01a1ffff-0000", source="cli", name="Archived", archived=1)
    add_thread(codex_home, "01a1abab-0000", source="cli", name="Ancient", updated=time.time() - 30 * 86400)
    desk.sweep()
    desk.sweep()
    md = desk.DESK_MD.read_text()
    assert md.count("Found thread") == 1 and "> Merge it?" in md
    assert "Needs you" in md.split("Found thread")[0]
    assert "No transcript left" in md
    for gone in ("Headless", "Archived", "Ancient"):
        assert gone not in md
    assert "codex resume 01a1cccc-0000" in md


def test_sweep_without_codex_installed_still_works(desk, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(desk, "PROJECTS", tmp_path / "none")
    monkeypatch.setattr(desk, "CODEX_HOME", tmp_path / "no-codex")
    desk.sweep()
    assert "0 untracked" in capsys.readouterr().out


def test_concurrent_hooks_keep_every_event_and_a_valid_desk(tmp_path):
    env = {"DESK_DIR": str(tmp_path / "desk"), "CODEX_HOME": str(tmp_path / "nocodex"), "PATH": "/usr/bin:/bin"}
    procs = []
    for i in range(16):
        agent = ["codex"] if i % 2 else []
        payload = json.dumps({"hook_event_name": "Stop", "session_id": f"sess{i:04d}-x", "cwd": str(tmp_path),
                              "last_assistant_message": f"reply {i}"})
        p = subprocess.Popen([sys.executable, str(HERE / "desk.py"), "hook", *agent], env=env,
                             stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p.stdin.write(payload)
        p.stdin.close()
        procs.append(p)
    errs = [p.stderr.read() for p in procs]
    [p.wait() for p in procs]
    assert not any(errs), errs
    lines = (tmp_path / "desk" / "sessions.jsonl").read_text().splitlines()
    assert len(lines) == 16 and all(json.loads(l) for l in lines)
    md = (tmp_path / "desk" / "desk.md").read_text()
    assert md.startswith("# Desk") and "16 open" in md
    assert (tmp_path / "desk" / "status").read_text() == "0 16\n"
    assert not list((tmp_path / "desk").glob("*.tmp"))
