---
name: music-and-audio
description: "Music and audio for games with open-source tools: local music generation, MetaSounds procedural audio, adaptive music systems in UE 5.8."
tags: [game-development, music, audio, metasounds, sound-design, unreal-engine]
platforms: [windows]
---
# Music & Audio Production (open-source stack)

## Suno mastery — v5.5 (researched + verified current 2026-07-05)
Model facts: v5.5 (released 2026-03-26) — richer arrangements, sharper vocals, strongest
fine-grained timbre control yet. Pro plan: commercial rights, ~2500 credits/mo. Generation
runs through the producer's authenticated browser session (no official API; third-party
wrappers break commercial licensing — NEVER use them).
**Prompt structure that wins:** [Genre/Subgenre], [Tempo/BPM], [Key instruments],
[Vocal style or 'no vocals'], [Production/Mood], [Modifiers]. Concrete SCENES beat emotion
words ~5x — 'rain on slate, distant bronze bell, bowed metal' not 'sad atmosphere'.
v5.5 resolves detail as fine as 'lo-fi tape hiss' or '1980s DX7 electric piano'.
**Always negative-prompt:** 1-3 'no X' constraints (no drums, no vocals, no heroic fanfare)
tighten results dramatically — unconstrained prompts give the model too much latitude.
**Structure/meta tags:** [Energy: High], [Build-Up], [Drop] placed right before the section
that needs the turn; one mood + one energy direction + 1-3 sound cues before the first lyric
line. Use the ///*****/// token to stop Suno singing your metadata (lyric bleed).
**#1 failure mode:** contradictory Style vs Lyrics fields (style says 'minimal', lyrics
demand 'full orchestra'). Check they point the same direction before generating.
**Instrumentals:** lyrics field = [Instrumental]; structure tags still steer arrangement.
**Studio 1.2 tools for post:** Remove FX, Warp Markers, time signatures, Alternates —
useful before export, but our mastering truth is still local (ffmpeg/rikku's lane).
**Custom Models (Pro: up to 3):** once the slice's best cues are picked, train a custom
model on them — every future Vesper Hollow cue then inherits the established sound. File
this as a card when the score stabilizes; it is the consistency cheat code.
**My Taste / Magic Wand:** personalization drift — do NOT rely on it for studio work; our
prompts must be explicit and reproducible.

## Game music craft (what makes a score read AAA)
1. **One motif, many states.** A single memorable motif that transforms (explore: implied;
   tension: fragmented; combat: driven; finale: silenced) beats ten unrelated themes. The
   Vesper Hollow bell motif families (D minor nave / E Phrygian gallery / C minor furnace)
   are the working example — keep any new cue inside a family.
2. **Compose for the crossfade.** Explore/tension/combat variants of an area MUST share
   tempo, key, and motif so MetaSounds can blend them mid-bar on CombatState/Intensity.
   Generate variants as one family, never as unrelated songs.
3. **Loop discipline:** author intro + loop body separately; cut loop points at zero
   crossings on the bar; a loop the player notices is a failed loop. Long ambience beds
   (60-90s) hide repetition better than short musical loops.
4. **Silence is a weapon.** The Last Bell hard-cut to near-silence is the loudest moment
   in the slice BECAUSE everything else is restrained. Do not score every second; duck
   music for stingers and reveals.
5. **Loudness law:** music masters to -16 LUFS integrated (cue-list standard), dialogue
   normalized consistently, SFX peaks readable above the bed. Verify with ffmpeg loudnorm
   print_format=summary — numbers, not ears alone.
6. **Diegetic anchors:** the bells exist IN the world — when the score's bell and the
   world's bell agree (same pitch family), the whole mix feels intentional.

## VO lane — ElevenLabs (APPROVED PAID, wired 2026-07-05)
`python tools/vo_gen.py` in ue5-agent-studio: `voices --search x` (28 library voices +
instant cloning, 10 slots), `gen --text --voice --out`, `batch --manifest
docs/audio/vo-manifest.json --outdir <Content path>`. Key auto-read from repo .env.
Output = mono 44.1k WAV normalized to -16 LUFS (matches cue-list standard). Starter tier:
40k chars/month — the slice script fits ~8x over, but don't burn budget on bulk retries;
iterate voice_settings (stability/style) on ONE line before batch runs. Manifest schema in
the tool docstring. Voice casting = kurumi's call vs the character's written voice.

## Composition & generation (no paid tools)
- **Local generation**: Meta **MusicGen / AudioCraft** (open weights; musicgen-small/medium run
  on the RTX 2070 SUPER 8GB — batch, GPU shared with the editor) for instrumental cues;
  **Stable Audio Open** for SFX/ambient textures/one-shots. Generate 4-8 variants per cue,
  pick by listening review; regenerate rather than salvage.
- **DAW-grade assembly**: **LMMS** or **Ardour** (both FOSS) scripted/manual for arranging
  stems, loop-point surgery, mixing. **FFmpeg** for batch convert/normalize/trim
  (UE wants 48kHz WAV; loudness: music ≈ -16 LUFS, SFX peaks ≤ -3dBFS, then real balance
  happens in-engine via submixes).
- Composition brief per cue (from story/creative director): emotion, tempo/energy, instrumen-
  tation palette, length, loop vs stinger. Keep a music bible (`docs/audio/music-bible.md`):
  motifs per character/faction, palette per region — REUSE motifs; coherence beats variety.

## SFX
Layered design: impact = transient + body + tail (3 sources). Sources: generate (Stable Audio
Open), record-style synthesis in MetaSounds, or freesound.org CC0 ONLY (verify license per
file, log attribution in `docs/audio/licenses.md`).

## In-engine (UE 5.8, all free/built-in)
- Import 48kHz WAV via AssetTools; SoundClasses + SoundMixes / **submix** hierarchy
  (Master → Music/SFX/VO/Ambience) with a ducking chain (VO ducks music -6dB).
- **MetaSounds** for procedural/systemic audio: footsteps (surface-switched), weapons
  (round-robin + pitch jitter ±3%), ambience beds (layered loops + random one-shots).
- **Adaptive music**: quartz-clock-synced MetaSound or level-BP driven layer system —
  exploration/tension/combat stems that crossfade on gameplay params (expose combat state
  from C++/GAS via gameplay tags; see `gameplay-programming`). Write stems as separate
  aligned WAVs (same tempo/key/length) at generation time — design for adaptivity up front.
- Spatial: attenuation presets per category (footstep 15m, gunshot 300m+occlusion), reverb
  volumes per interior. Audio components/params scriptable via ObjectTools/BlueprintTools MCP.

## Verification
Audio has no screenshots — verify by: PIE run + LogsToolset (missing-asset/cook errors),
loudness meters on rendered mixes (ffmpeg loudnorm print), and an explicit LISTENING pass
of every cue at 1x in context (play the level section it scores). A cue nobody listened to
in context is not done.
