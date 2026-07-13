---
name: character-creation
description: "Photoreal and stylized character creation: MetaHuman via MCP (verified), Blender custom characters, import/retarget/animate in UE 5.8."
tags: [game-development, characters, metahuman, blender, animation, unreal-engine]
platforms: [windows]
---
# Character Design & Creation

**QUALITY BAR (producer-set, mandatory)**: read
`C:\Users\dayvo\Documents\ue5-agent-studio\art-reference\CHARACTER-QUALITY-BAR.md` and its two
reference images before ANY character work. Track A = stylized-realistic hero (FF7R-class:
MetaHuman base + Blender hair cards + Subsurface Profile). Track B = anime/cel-shade
(Blender/VRoid→VRM4U + cel post-process + face-SDF shadows + outlines). Every review is a
side-by-side against the reference, graded criterion by criterion. Never mix the tracks.

**Track B status (2026-07-04)**: the cel-shade renderer is LIVE — `M_CelShade` post-process
material (banded toon + depth outlines) at /Game/AgentStudio/Materials, applied via the
unbound `CelShadeVolume` actor in L_Playground. Delete/disable that actor for realistic
rendering; it affects the whole screen. Authoring pattern: material graphs are scripted via
`MaterialEditingLibrary` + a `MaterialExpressionCustom` HLSL node through `ue_exec.py` (see
scratchpad build_celshade.py pattern — SceneTexture nodes must be present and wired for
SceneTextureLookup to compile). KNOWN GAP: layered 2.5D Blender imports (shadow-box style)
need their translucent materials remapped to unlit/masked with explicit sort order — they
import washed-out otherwise. `assemble_imported` now takes `scale=` for wrong-unit sources
(e.g. 0.37 to bring 4.5m down to 1.65m).

Two pipelines, both free: **MetaHuman** for photoreal humans (fastest, first-party, MCP-driven,
verified working) and **Blender** for stylized/custom/creatures (see `blender-3d-modeling`).

## Pipeline A — MetaHuman via MCP (photoreal humans)
Toolset `metahuman_toolset.metahuman.MetaHumanToolset` — full loop VERIFIED on this machine:

1. `create {"asset_path": "/Game/<Project>/Characters/MH_Name"}` → returns character ref.
2. `begin_edit {"object_path": "/Game/.../MH_Name.MH_Name"}` → returns a session ref. FIRST.
3. Edit with the session (unset fields = "don't change"):
   - `set_body_shape` — body_shape: `masculine_feminine` (0=masc, 1=fem), `fat`, `muscularity`
     (all 0..1), `height_cm`. Epic's own calibration examples: athletic woman ≈ {0.80, 0.25,
     0.70, 168}; muscular man ≈ {0.20, 0.25, 0.80, 178}; stocky middle-aged man ≈ {0.20, 0.70,
     0.50, 175}. Keep proportions realistic; avoid fat<0.25 AND muscularity<0.25.
   - `set_skin_tone` — {lightness 0=light..1=dark, redness 0=cool..1=red}: pale {0.10,0.45},
     tan golden {0.60,0.65}, olive {0.50,0.30}, dark neutral {0.85,0.45}.
   - `set_eye_color` — {temperature 0=cool..1=warm, brightness}: light blue {0.1,0.9},
     dark brown {0.9,0.1}, amber {0.8,0.7}.
4. `end_edit {"session": <ref>}` — LAST, commits.
5. **SAVE THE ASSET** (verified the hard way — MetaHuman characters are NOT auto-saved and
   die with the editor session): via ue_exec run
   `unreal.EditorAssetLibrary.save_directory('/Game/<Proj>/Characters', only_if_is_dirty=False, recursive=True)`.
6. **Visual check** (verified working flow): CaptureAssetImage does NOT support MetaHuman
   assets and `add_to_scene_from_asset` won't place them. Instead: while an edit session is
   OPEN, run `unreal.get_editor_subsystem(unreal.MetaHumanCharacterEditorSubsystem)
   .spawn_meta_human_actor(char, keep_transient=False)` — a preview actor appears at world
   origin (it ignores relocation and despawns at end_edit). Find it with scene_report
   (class MetaHumanDefaultEditorPipelineActor), frame with CaptureViewport, judge vs the
   quality bar, then end_edit. Face arrives texture-synthesized; body is clay preview until
   a full `build_meta_human` assembly. Disable any cel-shade volume first for photoreal review.

Face detail sculpting isn't in the MCP toolset yet — do it via editor Python (`ue_exec.py`)
against the MetaHumanCharacter API. Never claim a finished character without a viewed
screenshot.

## READ FIRST for any character card: docs/METAHUMAN-SCRIPTED-PIPELINE.md (2026-07-07)
The complete VERIFIED editor-Python chain (produced Hana, the R6 capstone base):
duplicate a PRESET (never sculpt from template) → `try_add_object_to_edit` →
`request_auto_rigging` (~16 s; preset duplicates come UNRIGGED) → `request_texture_sources`
(~25 s; retry one transient 'Server Error'; **SAVE IMMEDIATELY** — an editor crash cost us
the first download) → `can_build_meta_human` gate → `build_meta_human(CINEMATIC)` with NO
absolute_build_path/name_override (they made the build fail SILENTLY) and NO preview actor
alive (that combination crashed the editor once) → save_dirty_packages.
Hard-won facts: **Aoi is the MALE Japanese preset; Aera is the young East-Asian female** —
judge presets by spawning a lineup, never by name. Preview actors are transient and their
grooms are mis-bound on duplicates (hair floats at the jaw) — don't debug it, the full
build regenerates bindings. Hair/outfit swaps ONLY stick via
`get_preview_collection(char)` → `try_add_item_from_wardrobe_item(slot, WI)` →
`default_instance.set_single_slot_selection` → `on_edit_preview_collection` → rebuild;
the same calls on `internal_collection` no-op silently. Build errors NEVER raise into
Python — grep `Saved/Logs/AgentStudioLab.log` for LogMetaHumanCharacterEditor after any
"successful" call that produced no assets. Portrait rig + capture harness to reuse:
`test-results/capstone_hana/{portrait_scene,capture_hana,lineup}.py` (rect-light candela
ranges and manual-exposure pairings are in the doc; 14 cd reads BLACK).

### Grooms (hair) + wardrobe via editor Python (VERIFIED WORKING E2E)
Requires MetaHuman Creator Core Data (libraries mount at `/MetaHumanCharacter/Optional/`):
`Grooms/Bindings/{Hair,Eyebrows,Eyelashes,Beards,...}/WI_*` (95 groom wardrobe items) and
`Clothing/WI_DefaultGarment`. Slots: Hair, Eyebrows, Beard, Mustache, Eyelashes, Peachfuzz,
Outfits, Top Garment, Bottom Garment, SkeletalMesh, Character.

```python
sub = unreal.get_editor_subsystem(unreal.MetaHumanCharacterEditorSubsystem)
sub.try_add_object_to_edit(char)
col = sub.get_preview_collection(char)
wi = unreal.load_asset(".../WI_Hair_M_Layered.WI_Hair_M_Layered")
key = col.try_add_item_from_wardrobe_item("Hair", wi)          # returns item key
col.get_editor_property("default_instance").set_single_slot_selection("Hair", key)
sub.on_edit_preview_collection(char)   # CRITICAL — without this the build ignores everything
# then: can_build → build_meta_human → save_directory → respawn
```
The propagate call is the one everyone misses: the API doc says any preview-collection edit
must be followed by `on_edit_preview_collection(character)` or it never reaches the character
asset — build then silently produces the bald/underwear version. Verify on the respawned
actor: `GroomComponent.groom_asset` names should match your picks.

### Full build with real skin (all verified the hard way)
Prerequisites, or you get grey-checkerboard skin:
- **MetaHuman Creator Core Data** installed (Epic launcher → Library → engine slot ▼ → Options).
  Verify on disk: `Engine/Plugins/MetaHuman/MetaHumanCharacter/Content/Optional/TextureSynthesis`
  exists (~5.75 GB total with BodyTextures/Grooms/Clothing/Presets). The launcher's Apply
  silently fails (LS-0019-IS-0001) while ANY UnrealEditor instance runs — close editors first,
  and check the folder afterwards; never trust the dialog.
- **SM6 shader model.** MetaHuman's texture bake (TextureGraph + M_skin_unified_UI) fails with
  shader-compiler "Internal Error" on SM5 → bake silently outputs a grey checker into T_Head_BC.
  DefaultEngine.ini section `[/Script/WindowsTargetPlatform.WindowsTargetSettings]`
  (module is WindowsTargetPlatform — the wrong section name is silently ignored):
  `DefaultGraphicsRHI=DefaultGraphicsRHI_DX12`, then the ARRAY needs -/+ syntax (a bare `=`
  is silently ignored for config arrays): `-D3D12TargetedShaderFormats=PCD3D_SM5`,
  `-D3D12TargetedShaderFormats=PCD3D_SM6`, `+D3D12TargetedShaderFormats=PCD3D_SM6`.
  Verify in the boot log: "Using Highest Feature Level of D3D12: SM6".
  If the ini doesn't take (observed), launch with the `-sm6` command-line flag — that is the
  reliable fix. First boot after the switch = full shader recompile (10–30 min).

Build order via `unreal.MetaHumanCharacterEditorSubsystem` (ue_exec):
1. `try_add_object_to_edit(char)` (NOT add_object_to_edit).
2. `commit_skin_settings(char, char.get_editor_property('skin_settings'))` — BUT it skips
   synthesis "if not needed"; if the character carries stale placeholder textures, nudge a
   param first (skin.u ± 0.05) → commit → restore → commit.
3. `request_texture_sources(char, unreal.MetaHumanCharacterTextureRequestParams())` — ASYNC
   (despite the blocking=True default); with Optional Content it's LOCAL and takes seconds
   (2k face+body). POLL `char.get_editor_property('has_high_resolution_textures')` until True
   before building. commit_skin_settings DISCARDS high-res textures, so this must come after
   commits, before build.
4. `can_build_meta_human(char, True)` — if False the reason is in the log.
5. `build_meta_human` (CINEMATIC) → `save_directory` IMMEDIATELY (outputs transient).
6. Destroy + respawn any placed actor from the rebuilt BP (a rebuild under a placed actor
   leaves dead `TRASH_` components with forced_lod 4/8 = faceted grey head). On the fresh
   actor pin `forced_lod_model=1` per SkeletalMeshComponent and `LODSync.forced_lod=0`.
7. MetaHuman BP forward axis is **+Y** (spawn yaw 90 to face a camera at -X).

Verification: NEVER AssetExportTask a transient synthesized texture — it asserts
(EditorFactories.cpp:4610) and kills the editor. Export the baked
`.../Unpacked/<Name>/Face/Baked/T_Head_BC` after build and check it isn't a grey checker
(placeholder samples ≈ RGB 94-95 uniform). If the editor crashes, move `Saved/Autosaves`
aside before relaunch or a "Restore Packages" modal blocks boot and remote exec.

## Pipeline B — Blender customs (stylized / creatures / enemies / 2D)
Full workflow in `blender-3d-modeling`. Character-specific: sculpt/model → retopo (quads,
deform loops at joints) → UV → texture (PBR) → rig (Rigify or UE-compatible skeleton) →
export FBX (or use existing UE quick-export presets) → UE import via
`editor_toolset.toolsets.asset.AssetTools` / Interchange → verify skeleton with
SkeletalMeshTools (bone hierarchy, sockets).
Existing character work to build on: `C:\Users\dayvo\Documents\blender-agent\` — asuna/kairi/
kurumi busts and 2.5D packages (.blend + .glb). Reuse these as base meshes and style refs.

### Final-quality stills — Movie Render Queue (verified recipe)
Viewport captures show hair-strand dither (no AA convergence). For beauty/marketing shots use
MRQ: enable `MovieRenderPipeline` plugin in .uproject (restart required). Then via ue_exec:
spawn a `CineCameraActor`, create a LevelSequence **while the target level is loaded**, bind
with `add_possessable(cam)`, add `MovieSceneCameraCutTrack` + section range 0-1 +
`set_camera_binding_id`, and VERIFY with `locate_bound_objects` — a binding created in another
level silently renders the PIE-pawn view instead. (`delete_asset` can silently fail/return
False — prefer a fresh asset name over deleting.) Job setup: `MoviePipelineQueueSubsystem` →
`allocate_new_job` → config `MoviePipelineOutputSetting` (resolution/dir/name),
`ImageSequenceOutput_PNG`, `DeferredPassBase`, `AntiAliasingSetting` (spatial_sample_count 48,
temporal 1, override on, AAM_NONE, warmups 48/90) →
`render_queue_with_executor(MoviePipelinePIEExecutor)`, poll `is_rendering()` (~30-45s per 4K
frame). Camera: `set_editor_property('current_focal_length', mm)` (no setter method exists),
focus_method DO_NOT_OVERRIDE to disable DOF. Framing rule (≈14mm sensor height): distance_cm ≈
subject_height_cm × focal_mm / 14 — portrait 65mm@3.7m, fullbody 35mm@5.2m.

## Animation (free)
- MetaHumans ship rig-ready; UE IK-Retargeter maps any humanoid anim between skeletons.
- Free anim sources: Mixamo (download FBX; never scrape — user account), UE marketplace/Fab
  free packs, MetaHuman Animator for performance capture later.
- In-engine: Sequencer suite via MCP (7 toolsets — sequences, keyframing, Control Rig,
  FBX import/export). `SequencerImportExportTools.import fbx onto bindings` for retargeted
  clips; `ControlRigTools`/`SequencerControlRigTools` for hand-keyed adjustments.
- Gameplay side: AnimBP via BlueprintTools graph DSL; state machines small and testable.

## Enemies & creatures
Humanoid enemies: MetaHuman base + Blender wardrobe/armor kitbash is fastest to photoreal.
Non-humanoid: Blender sculpt or local text-to-3D draft (TripoSR/Hunyuan3D-2mini — see
`blender-3d-modeling`) → Blender cleanup → rig → import. Enemy VARIETY comes cheap via
material/tint/scale/attachment permutations of one base — build permutation tables, not
one-off models.
