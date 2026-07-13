---
name: gameplay-programming
description: "AAA gameplay programming in UE 5.8: C++-first architecture, Blueprint text DSL, GAS, Enhanced Input, automation testing, PIE verification."
tags: [game-development, gameplay, unreal-engine, cpp, blueprints, gas, testing]
platforms: [windows]
---
# Gameplay & Systems Programming (UE 5.8)

## FASTEST vertical slice: graft Epic's template games (VERIFIED E2E)
The engine install ships complete playable games as free content — use them as the gameplay
foundation instead of building from zero. `<UE>/Templates/TemplateResources/` has shared packs
(Characters=Manny/Quinn+anims+ABPs, Input=Enhanced Input set, LevelPrototyping) and
**variant packs: Variant_Combat (melee + StateTree AI enemies + spawners + checkpoints +
damage), Variant_Platforming, Variant_SideScrolling**. Graft procedure (all verified):
1. Copy `TemplateResources/High/<Pack>/Content/*` → `Content/<PackName>/` (CREATE the dest
   dir first — PowerShell Copy-Item into a missing dir silently flattens one folder level).
2. TP template content: `Templates/TP_ThirdPersonBP/Content/ThirdPerson` → `Content/`.
3. World-Partition maps keep actors in `Content/__ExternalActors__/<map path>/` — copy from
   `Templates/TP_ThirdPerson/Content/__ExternalActors__/` (correctly pathed) or the level
   loads with 0 actors. A loaded umap file is locked — switch levels before overwriting.
4. Enable the `GameplayStateTree` plugin (+restart) or the combat AI's ST asset won't load.
5. Renderer ini or EVERYTHING renders blown-white ("Cached lighting ... PreExposure" hint):
   `r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange=true`, plus
   `r.AllowStaticLighting=False`, `r.Lumen.TraceMeshSDFs=0`.
6. MetaHuman as the player: child BP of the template character, swap CDO mesh to the MH body
   (UE5-skeleton compatible — mannequin ABPs drive it, no retarget), attach Face/Outfit/groom
   components via SubobjectDataSubsystem (body torso is CULLED under the outfit — the outfit
   component is mandatory), and wire `SetLeaderPoseComponent` for Face+Outfit **in the
   construction script** via the BP DSL (template-property leader refs serialize to the CDO
   and silently break at runtime). Char mesh getter node: `Variables|Character|GetMesh`.
7. PIE proof shots: CaptureViewport only sees the editor viewport (black during PIE) — use
   `execute_console_command(get_game_world(), 'HighResShot 1920x1080')` →
   `Saved/Screenshots/`. Runtime probe: `GameplayStatics.get_player_pawn` (there is no
   `PlayerController.get_pawn` in Python).
8. Perf gate (tools/perf_test.py budgets thread times): mid-GPU fix that keeps the visual
   bar = TSR upscale `r.ScreenPercentage=66` + Lumen probe/reflection downsample + VSM LOD
   bias + `r.HairStrands.LODMode=1` in `[SystemSettings]` (verified 20.7→15.4ms GPU).

## Architecture rule: C++ for logic, Blueprints for tuning
Gameplay systems, state, algorithms → **C++** (text = reviewable, diffable, testable — agent
strength). Blueprints ONLY as thin designer-tuning layers over C++ base classes. Research
verdict: agents fail at node-graph programming but excel at C++; UE 5.8's Blueprint text DSL
covers the thin BP layer.

## C++ workflow
- Toolchain: VS Build Tools 2022 is installed. Add C++ to a project: create
  `Source/<Module>/` with `.Target.cs`/`.Build.cs`/module files, regenerate project files
  (`UnrealBuildTool -projectfiles`), build Development Editor, or start from a C++ template.
- **Live Coding** (Ctrl+Alt+F11 / LiveCodingToolset MCP) hot-compiles C++ into the running
  editor — the fast iteration loop. Full rebuild only for header/class-layout changes.
- Compile errors: read them from LogsToolset or the build output — never claim a system works
  without a clean compile AND a PIE run.
- Core stack for an action game: `ACharacter` + **Enhanced Input** (mapping contexts) +
  **GAS** (GameplayAbilities: abilities/attributes/effects/cues — the AAA-standard ability
  framework; GASToolsets MCP inspects runtime state) + **StateTree** or Behavior Trees for AI
  (both have MCP toolsets) + `AGameMode`/`AGameState`/`APlayerState` for rules/score/net.
- Multiplayer-aware from day one: mark replicated state, server-authoritative mutations.

## Blueprint layer via MCP
`BlueprintTools`: `create` (parent = your C++ class) → `get_graph_dsl_docs` (READ FIRST) →
`write_graph_dsl` → `compile_blueprint` → fix reported errors → verify. Variables/functions/
event dispatchers all manageable; `read_graph_dsl` to review existing graphs. UI: UMGToolSet
(follow its strict list_properties→get→set workflow), MVVM for data binding.

## Testing (the credibility layer)
- **AutomationTestToolset** via MCP: `DiscoverTests` once → `ListTests` → `RunTests` →
  `GetTestResults`. Write C++ `IMPLEMENT_SIMPLE_AUTOMATION_TEST` unit tests for every system;
  functional tests (`AFunctionalTest`) for gameplay flows.
- PIE smoke-test every feature: `StartPIE` → exercise (SlateInspectorToolset can drive input;
  gameplay debugger + GASToolsets inspect ability state) → `StopPIE` → LogsToolset for
  errors/ensures/warnings.
- Performance: `stat unit`, `stat game` via console (SearchCVars/EditorAppToolset); frame
  budget is a hard constraint — profile before and after each system lands.
- Definition of done: compiles clean, unit+functional tests green, PIE run clean logs,
  frame budget held, and the feature demonstrated in a captured PIE session.

## Music/audio hooks
MetaSounds (free, built-in) for procedural audio; expose gameplay params (intensity, combat
state) to MetaSound graphs; see `music-and-audio`.
