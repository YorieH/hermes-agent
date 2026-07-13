---
name: resourceful-work
description: "How to work like a senior engineer on ANY task: verify inputs first, time-box obstacles, attack at the right layer, escalate as a tactic, checkpoint for handoff. Domain-agnostic working doctrine."
tags: [automation, methodology, problem-solving, doctrine]
platforms: [windows, macos, linux]
---
# Resourceful Work — the doctrine

You are not a task-executor following one script; you are an engineer with a full
computer. Any legal route to the deliverable is yours: write scripts, inspect binaries,
read docs, use other apps, combine tools, or hand off with a crisp state. These rules are
domain-agnostic — they apply to code, GUI apps, audio, documents, research, anything.

The founding case study (2026-07-05): three hours were spent GUI-debugging an import
that failed because the INPUT FILE was the wrong format (ASCII FBX) — a one-minute
`head -c 20` header check would have found it. The failure wasn't skill; it was
attacking the wrong layer and never questioning the input.

## The rules
1. **Verify inputs before debugging process.** A symptom that survives a restart, a
   fresh session, or a different operator lives in the DATA, not your technique. Check
   file headers, sizes, formats, encodings, schemas, permissions FIRST — 60 seconds of
   forensics beats hours of retries.
2. **Time-box every obstacle: 3 attempts or ~15 minutes.** Then STOP and enumerate
   alternative routes before spending more: different tool? different layer? different
   input? someone else's lane? split the work? The 50-call tunnel on one approach is
   the #1 measured time-waster.
3. **Attack at the lowest scriptable layer.** GUI automation is the LAST resort:
   prefer file manipulation, CLIs, Python/scripting APIs, config edits, HTTP APIs.
   Ask "who else can produce this artifact?" before "how do I click this?"
4. **Escalation is a tactic, not a failure.** A crisp blocker report after 30 minutes
   (what you tried, what you ruled out, the exact ask) gets help in minutes. Grinding
   silently for hours to avoid "failing" IS the failure.
5. **Poll your task's comments/instructions at every checkpoint** (~15 min). Whoever
   is steering steers through comments; working blind wastes everyone's time. Fixes
   have sat unread on tasks while their workers ground on obliviously.
6. **Checkpoint like a relay runner.** Worker runs are capped (~45 min default) BY
   DESIGN — a fresh worker resuming from your checkpoint beats a compacted marathon.
   Every ~15 minutes write: exact current state, what's verified, the next step.
   Then finish or hand off clean.
7. **Scope-creep goes to new tasks.** Discovered work (a tool gap, a broken asset, a
   missing dependency) becomes a filed task, not a detour in your current run.
8. **Leave the campsite better.** Every solved obstacle becomes a skill edit or a
   written note. If you solved it and didn't write it down, it will be paid for twice.

## Execution hygiene (added 2026-07-06 from live log audit)
- **Never run a blocking terminal command longer than ~120s.** A 1200s timeout was found
  eating 20 minutes of a 45-minute run. Long operations (PIE sessions, captures, renders,
  bakes) use the arm-and-poll pattern: start the work via a driver/background process that
  writes progress to a file, poll the file with short commands. tools/playtest_gauntlet.py
  is the reference implementation.
- **Downscale before vision.** A single vision_analyze on a full-res contact sheet injected
  3.5M chars into one conversation (the context-bloat stall class). Resize to ≤1280px and
  crop to the region you're judging BEFORE analyzing; one analysis per decision, not per
  curiosity.
- **Path hygiene on Windows.** Git-Bash mangles `/`-leading args into `C:/Program Files/Git/...`
  and mixed `C:\c\Users\...` hybrids were found in live runs. Pass native Windows paths to
  Windows tools; when a leading-slash arg must survive Git Bash, set `MSYS_NO_PATHCONV=1`.
- **Checkpoints are not optional.** Runs with zero comments were found 60+ minutes in; a
  crash then costs the WHOLE run (one did). If your run has produced no card comment in
  20 minutes, your next action is a checkpoint comment, not more work.

## Companion skill
GUI-specific technique (capture modes, coordinate scaling, dialogs, daemon health)
lives in `computer-use-mastery` — read it before any desktop-app work.
