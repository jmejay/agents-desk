# desk

Every Claude Code or Codex session goes on the desk and stays there until you close it, so none slip through when many run at once. Both agents share one desk; each entry says which agent it belongs to and gives that agent's resume command.

## TL;DR

- **Start working.** Use `claude` or `codex` like you normally do. Every session is added to your desk for you.
- **Check your desk.** Your status line tells you how many sessions are waiting for an answer. Type `desk` to see the full list.
- **Go back to a session.** Each one on the list has a line you can copy and paste to open it again.
- **Answer from anywhere.** In any session, ask "what's on my desk?" and reply to the waiting questions there.
- **Finish.** Tell the session you're done, and it takes itself off the desk.
- **Change your mind.** Talk to a finished session again and it comes back.
- **Throw one away.** Type `desk rm` and the session's id.
- **Clean up.** Type `desk sweep`. It removes sessions whose work has been merged and adds any it missed.

Nothing is ever removed just because it's old. More detail on each step below.

## Journeys

**1.** Run `claude` or `codex` as usual. The session is on the desk after its first reply. Nothing to do.

**2. See what's waiting.** The status line shows `N items on your desk`: sessions waiting on an answer from you. Run `desk` to open the list in Finder (`desk show` for the terminal):

- **Needs you**: the session's last reply ended on a question.
- **Live**: the session hasn't ended (for Claude, still open in a terminal).
- **Exited**: closed the terminal, work not marked done.

Oldest first, because those are the ones slipping. An entry looks like this:

```
## Needs you: ended on a question (3)

- **Login refactor** `3f2a91c0` claude · ~/code/myapp @ feat/login · 2h ago · found by sweep
  > The tests pass. Shall I open the PR?
  `cd ~/code/myapp && claude -r 3f2a91c0-5b7e-4d21-9c3a-8e1f6b2d4a77`
```

Title, short id, agent, where it ran (directory @ branch), when it last did anything, the last paragraph of its last reply, and the line to resume it. "found by sweep" means the hooks missed the session and `desk sweep` added it later.

**3. Pick one up.** Each entry ends with a resume line for its agent. Paste it:

```
cd ~/code/myapp && claude -r <session-id>
cd ~/code/myapp && codex resume <session-id>
```

**4. Triage without resuming.** In any session, run `desk show` or ask "what's on my desk?" (the agent can read `~/.desk/desk.md`). You can answer several waiting questions from one place and only resume the sessions that need their own context.

**5. Finish.** Tell the session the work is done. Each session is told its desk id at start and to run `desk close <id> "<one-line outcome>"` when you say so. From another terminal: `desk close <id> "outcome"`. The outcome is kept in `sessions.jsonl` with the close.

**6. Changed your mind.** Prompt a closed session again and it comes back on its own. Or `desk reopen <id>`.

**7. Throw one away.** `desk rm <id>` drops it for good; resuming won't bring it back (only `desk reopen` will).

**8. Tidy up.** `desk sweep` closes exited sessions whose branch PR has merged, and picks up any Claude or Codex sessions the hooks missed in the last 14 days (`--days N` to change).

Nothing ever closes because it's old.

## Install

macOS only: bare `desk` reveals the list in Finder (`open -R`), and the lock uses `fcntl`. Needs Python 3; `gh` is optional (used by `sweep` to spot merged PRs).

```
git clone https://github.com/jmejay/agents-desk.git && cd agents-desk
mkdir -p ~/.desk
ln -s "$PWD/desk.py" ~/.desk/desk.py
ln -s "$PWD/desk.py" ~/.local/bin/desk
```

Then hook up whichever agents you use. Neither needs the other.

- **Claude Code**: merge `settings.hooks.json` into `~/.claude/settings.json` (user level, so every repo is covered), then restart Claude Code or open `/hooks` once.
- **Codex**: append `codex.hooks.toml` to `~/.codex/config.toml` (keep anything already there, including `notify`), then run `/hooks` in Codex once to review and trust them.

## How it works

Each agent's lifecycle hooks append to `~/.desk/sessions.jsonl`, the source of truth: SessionStart, Stop and SessionEnd for both, plus UserPromptSubmit for Codex (`desk hook codex`). After every event it rebuilds `~/.desk/desk.md` and `~/.desk/status` (`<needs you> <open>`, read by the status line). A lock file keeps concurrent hooks from either agent from clobbering each other; it gives up after two seconds rather than stall a session.

Claude's snippet and title come from its transcript. Codex's reply comes from the hook payload, and its title and source from Codex's thread table (`$CODEX_HOME/state_*.sqlite`, read-only). Codex calls its own storage internal, so if that table moves, Codex sessions still land on the desk but untitled, `codex exec` runs are no longer filtered out, and sweep finds no Codex sessions.

`DESK_DIR` points it elsewhere, for tests: `python3 -m pytest test_desk.py`.

## Known gaps

- "Needs you" just means the last paragraph ends in `?`. A session waiting on you without asking ("Steps 1 and 2 wait on your yes", "Tell me which to start", "I can file this if you want") lands under Live or Exited, so skim those snippets too.
- Sessions picked up by `sweep` show as exited.
- Cloud sessions, `claude -p` runs, `codex exec` runs and Codex subagents stay off the desk.
- Codex sessions found by sweep only have a snippet if their transcript file still exists (most older ones don't).
- For a Codex session started in VS Code, "Live" means it hasn't ended, not that a terminal is open; `codex resume` for those is untested.
