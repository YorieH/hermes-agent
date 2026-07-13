---
name: ue58-unreal-mcp
description: "Masterfully drive Unreal Engine 5.8+ via the official MCP server: discovery flow, friction rules, all 53 toolsets, perception, screenshots."
tags: [game-development, unreal-engine, ue5, mcp, editor-automation]
platforms: [windows]
---
# Unreal Engine 5.8 MCP Mastery

UE 5.8 embeds an official MCP server in the editor (plugin `ModelContextProtocol`).
Everything here is VERIFIED working on this machine (2026-07-04). Read this whole
skill before touching the editor — it removes the failure modes that make agents flail.

## Connection
- Server: `http://127.0.0.1:8000/mcp`, MCP protocol `2025-06-18`.
- Lab project: `C:\Users\dayvo\Documents\ue5-agent-studio\AgentStudioLab\AgentStudioLab.uproject`
  (MCP auto-starts). Launch: `& "C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe" <uproject>`.
  Boot takes 2-4 min; the port accepts connections ~1 min AFTER "Starting MCP server" appears in
  the log (`<project>\Saved\Logs\*.log`). Retry with backoff.
- **Transport trap**: tool results arrive as a LATER unframed SSE write on the held-open POST
  socket. Standard HTTP clients see an empty body. USE the working client:
  `python C:\Users\dayvo\Documents\ue5-agent-studio\tools\mcp_client.py` (importable: `McpClient`).

## MANDATORY: editor lock + ensure-editor (five agents, ONE editor)
The whole studio shares a single editor process. Before ANY mutating editor work
(spawn/edit/delete assets or actors, editor Python, PIE, renders, imports):
1. `python C:\Users\dayvo\Documents\ue5-agent-studio\tools\ensure_editor.py` — probes MCP,
   launches the editor **with `-sm6`** if it's down (SM5 silently ruins MetaHuman texture
   bakes), waits for readiness. Exit 0 = MCP up.
2. `python C:\Users\dayvo\Documents\ue5-agent-studio\tools\editor_lock.py acquire --owner <your
   profile name> --wait 3600 --note "<what you're doing>"` — blocks until you hold the mutex.
   Exit 0 = yours. Default TTL 30 min: for longer jobs `renew --owner <you>` at least every
   20 min or another agent may take over mid-operation.
3. Work. Save levels/assets as you go (crashes lose unsaved work for everyone).
4. `python ...\editor_lock.py release --owner <you>` — ALWAYS, even on failure (use try/finally
   thinking). Holding the lock while idle blocks four teammates.
Read-only probes (status, screenshots of current state, log reads) may skip the lock; anything
that changes editor state may not. Never kill or restart the editor while another agent holds
the lock (`editor_lock.py status` tells you — it also shows the FIFO wait queue).

**Lock etiquette (added 2026-07-05 after a 4-agent, ~2.7-hour convoy):**
- **Acquire LATE, hold SHORT.** Do ALL non-editor work first (Blender, docs, audio, code,
  file prep, planning) and only then acquire for the editor-touching phase. Never acquire at
  card start "to have it ready" — that idles four teammates.
- **Release between phases.** If your card alternates editor and non-editor phases (import →
  Blender fix → re-import), release during the non-editor phase and re-acquire after. Waiters
  queue FIFO now, so you re-enter at the back — that's fair and intended.
- **Long editor surgery** (plugin enables, editor restarts, big migrations): announce it in a
  card comment with an ETA so waiters can re-plan, and renew the TTL honestly rather than
  setting a huge one up front.
- **Dead workers self-heal**: the lock cross-checks the holder's kanban run; if that run is no
  longer running, the lock reads stale and the next acquire takes over in seconds. Never
  force-release by hand unless `status` shows `stale: true` and takeover isn't happening.
- **While you wait, work.** The acquire call blocks, but if the queue is deep (`status`),
  reorder your own card: pull the non-editor steps forward instead of sitting idle.
- **Don't burn your runtime cap in a wait loop.** Worker runs are capped (~45 min) and a
  timeout counts toward the dispatcher's give-up limit. If you've been lock-waiting
  >15 min AND your run is past the 25-min mark, post a checkpoint comment and exit
  gracefully (`block --kind transient`) — the requeue is free, the timeout is not.

## The discovery flow (why naive agents fail)
`tools/list` returns ONLY 3 meta-tools: `list_toolsets`, `describe_toolset`, `call_tool`.
The real ~53 toolsets hide behind them. ALWAYS:
1. `call_tool {"tool_name":"list_toolsets"}` once per session.
2. `describe_toolset {"toolset_name": X}` before first use of a toolset — get exact tool names
   AND schemas. Do not guess parameters.
3. `call_tool {"toolset_name": X, "tool_name": <SHORT name, no prefix>, "arguments": {...}}`.

## Friction rules (all verified — violating any = silent failure or error)
1. Params WITHOUT an explicit schema `default` are REQUIRED even if they look optional.
   e.g. `find_actors` needs `"tag": ""`, `"collision_channels": []`; `CaptureViewport` needs
   `"annotations": {}`.
2. Param names are inconsistent per toolset: `instance` (ObjectTools/MaterialInstanceTools),
   `actor` (PrimitiveTools), `values` not `properties`, `folder_path`+`asset_name` not path+name,
   MaterialInstance param key is `name` not `parameter_name`. When in doubt: describe_toolset.
3. Results: `content[0].text` = JSON `{"returnValue": <value>}`. Object refs are
   `{"refPath": "/Temp/....:PersistentLevel.Actor_0"}` — pass them back verbatim.
4. Images come as `returnValue.image.data` (base64 PNG), not MCP image content.
5. Property paths on set_properties use `Component.Property` (e.g.
   `"PointLightComponent.Intensity": 8000.0`).

## Toolset map (what to reach for)
- **SceneTools** (`editor_toolset.toolsets.scene.SceneTools`): add_to_scene_from_class/asset
  (snap_to_ground!), find_actors, remove_from_scene, trace_world, load_level, merge_actors.
- **PrimitiveTools**: add_cube/sphere/cylinder/cone on an actor — greyboxing.
- **EditorAppToolset** (`EditorToolset.EditorAppToolset`): CaptureViewport (pass
  `captureTransform` + `annotations:{}` → base64 PNG **with actor labels + world-coord grid
  overlay**), SetCameraTransform, GetVisibleActors, StartPIE/StopPIE, CaptureAssetImage.
- **AgentPerceptionToolset** (`agent_perception.toolset.AgentPerceptionToolset`, ours):
  scene_report (AABBs + computed overlaps + floating actors), check_enclosure (ray fan →
  openings/doorways/missing walls), measure_clearance (line-of-sight). **USE THESE FOR ALL
  SPATIAL DECISIONS — do not infer geometry from screenshots.** Verified: finds a doorway's
  exact angle and a wall's exact inner face distance.
- **AgentConstructionToolset** (`agent_construction.toolset.AgentConstructionToolset`, ours) —
  PREFER these composites over raw primitive calls:
  - `blockout_room(name, width, depth, height, wall_thickness, door_wall, door_width,
    door_height, origin_x/y/z, add_ceiling)` — full room (floor/walls/doorway/lintel) in ONE
    call, returns its own ray-verification (`sealed_correctly` must be true — if false, the
    geometry is wrong: fix before proceeding).
  - `add_three_point_lighting(target_x/y/z, distance, key_intensity, name_prefix)` — key/fill/rim rig.
  - `apply_material(actor_label, material_path, slot=-1)` — handles slot friction.
  - `lookup_unreal_api(symbol)` — CHECK ANY unreal.* API HERE BEFORE using it in editor
    Python. Returns real docs/members or "does not exist". Kills hallucinated APIs.
  - `save_level(asset_path)` — call after every build step; untitled levels need asset_path
    once (e.g. /Game/<Project>/Maps/L_Name). Unsaved work is lost on editor restart.
  - `create_review_stage(name, x, y, radius, key_intensity)` — clean art-review set: neutral
    stage + backdrop + calibrated 3-point rig + exposure-locked PP volume (bias 8.5, key 250 —
    sweep-calibrated) + hides editor sprites/volume lines. Returns fullbody/portrait/profile
    camera poses. USE THIS for every character/asset review shot — never review against level
    clutter or auto-exposure.
  - `inspect_materials(folder)` / `set_material_instance_param(path, name, r[,g,b,a])` —
    first stop when any import looks wrong (washed out, invisible, too shiny).
  - `connect_rooms(name, start/end x,y, width, height, floor_z)` — straight corridor between
    two doorways, returns `walkable` verification.
  - `import_asset(file_path, destination, enable_nanite)` — FBX/GLB/OBJ import, reports
    mesh sizes (sanity-check scale!) and enables Nanite.
  - `assemble_imported(folder_path, label_prefix, x, y, z, yaw, drop_name_contains,
    ground_to_z)` — reassembles a multi-part GLB import in-level (parts spawn at one origin;
    baked transforms line up), auto-drops showcase props by name, grounds to floor, returns
    real combined size. `remove_assembly(label_prefix)` undoes it. PROVEN: 226-part Blender
    character bust.
  - `tint_actor(actor_label, r, g, b)` — solid-color material instance, auto-created.
- **PIE smoke test** (harness): `python C:\Users\dayvo\Documents\ue5-agent-studio\tools\
  smoke_test.py --seconds 10 [--level /Game/...]` — starts PIE, collects log errors, stops.
  Run it before calling any level "done". Exit 0 = clean.
- **Playtest gauntlet (GATE REQUIREMENT, 2026-07-06)**: `python ...\tools\playtest_gauntlet.py
  --expected-pawn BP_MH_Player_C [--level /Game/...]` — plays as "player zero" in PIE:
  asserts pawn spawns <6s, MOVE_WALKING, ≥150cm forward + ≥100cm strafe under injected
  input, enemy reacts when approached, player-camera screenshots not black (luminance),
  log free of spawn/BP-runtime/nav errors, pawn class matches. Exit 0 = playable. Acquires
  the editor lock itself (pass --no-lock if you already hold it). Attach the results JSON
  to your card. Static companion: `tools\bp_audit.py --bp <BP path> [--player]
  [--deny-pattern X]` — lints scale outliers / cosmetic-component collision /
  dont-spawn-if-colliding / foreign assets; no lock needed, seconds to run.
- **Spawn/route spatial checks**: use capsule tests at character size (r=36, hh=91):
  `capsule_overlap_actors` for "can a character exist here", capsule sweep for "can it
  walk from A to B". Line traces slip through thin geometry and miss oversized collision
  hulls — both Gate 4 spawn-trap bugs were invisible to line traces.
- **Python toolset quirk**: str-default params still become REQUIRED in the MCP schema —
  pass `""` explicitly rather than omitting.
- **BlueprintTools**: `read_graph_dsl` / `write_graph_dsl` — Blueprint graphs as TEXT. Call
  `get_graph_dsl_docs` first for syntax. compile_blueprint after writing. Never build graphs
  node-by-node.
- **MaterialTools / MaterialInstanceTools / TextureTools**; **MetaHumanToolset** (see
  character-creation skill); **PCGToolset/PCGSpatialToolset**; **NiagaraToolsets** (5);
  **GASToolsets** (3); Sequencer suite (7 toolsets) + ControlRigTools for animation;
  **AutomationTestToolset** (Discover→List→Run→GetResults); **LogsToolset** (read output log —
  check it after every risky operation); **SemanticSearchToolset** (vector+BM25 asset search);
  **SlateInspectorToolset** (Playwright-style editor UI automation); **ProgrammaticToolset**
  (batch tool calls in sandboxed Python); **UMGToolSet** (UI; follow its list_properties →
  get → set workflow strictly).

## The core loop (mandatory discipline)
ACT (tool calls) → CAPTURE (CaptureViewport 3 angles: top-down pitch -90 for layout, eye-level
for look, player-eye z≈160 for feel) → **DECODE (AgentPerception scene_report/check_enclosure —
structured facts, not vibes)** → CORRECT. Never claim success without a DECODE step that
confirms it. Agents that skip verification report success while wrong — this is the #1
documented agent failure in game dev.

## Python in the editor
Remote Execution is enabled in the lab project. Run arbitrary editor Python:
`python C:\Users\dayvo\Documents\ue5-agent-studio\tools\ue_exec.py "<code>"` (or `--file`,
`--reload <toolset_pkg>` to hot-reload custom toolsets). Full `unreal` module = anything the
MCP lacks. Add new MCP tools by writing a toolset (pattern:
`AgentStudioLab\Content\Python\agent_perception\toolset.py`, register in
`Content\Python\init_unreal.py` via `unreal.ToolsetRegistry.register_toolset_class`).
