---
name: story-and-narrative
description: "Story writing and narrative direction for AAA games: story bible, GDD narrative spine, quest/dialogue structure, cinematics via Sequencer."
tags: [game-development, story, narrative, writing, quests, dialogue, cinematics]
platforms: [windows, linux, macos]
---
# Story Writing & Narrative Direction

Narrative is a DESIGN discipline: it must survive contact with gameplay. Everything ships as
versioned documents in the project repo (`docs/narrative/`) that other agents consume.

## The story bible (single source of truth)
`docs/narrative/story-bible.md`: premise (2 sentences), theme (what the game argues), world
rules (hard constraints — magic/tech/politics), timeline, factions, and per-character entries
(want vs need, flaw, arc, voice notes with 3 sample lines each). Every other agent reads this
before making anything character- or world-adjacent; art bible and story bible must agree.

## Structure for games
- Spine: three acts on the CRITICAL PATH only; side content hangs off it. For each beat:
  location, playable verb (what the PLAYER does — a beat with no gameplay verb is a cutscene
  smell), stakes change, and what the player learns.
- Player agency: branch where choice is meaningful, bottleneck where production reality
  demands it (hub-and-spoke beats full branching for scope).
- Environmental storytelling FIRST (it's cheap and AAA-signature): the level tells the story —
  brief yuna/rikku on set-dressing implications of every beat.
- Pacing: alternate tension/release; map beats to level flow diagrams with the level designer.

## Quests & dialogue
- Quest specs as data (`docs/narrative/quests/*.md`): giver, want/blocker, steps (verb + place
  + fail states), rewards, state flags. Writers write STATES, not scripts — gameplay owns flow.
- Dialogue: UE's Conversation Graph assets (`ConversationTools` MCP toolset inspects
  UConversationDatabase) or DataTables (`DataTableTools`) keyed by StringTables
  (`StringTableTools` — localization-ready from day 1). Barks/ambient lines in bulk tables:
  situation × faction × 3 variants.
- Voice: character voice consistency > cleverness. Read lines aloud mentally at 1.2x — cut 30%.

## Cinematics & story direction
Sequencer via MCP (SequencerTools + keyframing + ControlRig + custom bindings + import/export)
builds cutscenes agent-side: camera cuts, actor bindings, animation tracks. Direction rules:
cut late/leave early, one idea per shot, coverage (wide-medium-close), eyeline consistency;
capture frames via CaptureViewport at storyboard moments and REVIEW them vs the storyboard doc
before calling a scene done.

## Working with the studio
Narrative reviews every level greybox for story readability. Kurumi (director) owns
tone/canon vetoes. Nothing is "in the game" until it exists as an asset/level/sequence —
prose that can't be built gets rewritten to what CAN be.
