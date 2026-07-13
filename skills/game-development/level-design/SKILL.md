---
name: level-design
description: "AAA level design in UE 5.8: greybox → verify spatially → PCG population → Fab/Megascans photoreal dressing → lighting → QA loop."
tags: [game-development, level-design, unreal-engine, pcg, environment-art]
platforms: [windows]
---
# Level Design & Environment Art (UE 5.8)

Prereq: `ue58-unreal-mcp` skill. Levels are built in verifiable stages — never jump to
set dressing before the greybox is proven correct.

## Stage 1 — Design on paper first
Write the layout as data before touching the editor: rooms/zones with dimensions (cm),
connections (doorways/corridors with widths), flow (player path, sightlines, encounter beats).
UE units: 1uu = 1cm. Human metrics: player capsule ~180cm tall; doorway ≥ 120w × 210h;
corridor ≥ 250w; ceiling ≥ 300. Design the spatial graph, then place it.

## Stage 2 — Greybox (verified pattern)
- One empty parent actor per room/zone (`SceneTools.add_to_scene_from_class`
  `/Script/Engine.Actor`), then `PrimitiveTools.add_cube` per surface (floor/walls/lintels)
  with `local_transform` offsets. Walls-with-openings = segments + lintel, not boolean cuts.
- Reference: working script `C:\Users\dayvo\Documents\ue5-agent-studio\tests\e2e_greybox.py`
  (builds an 8×8m room with doorway; every placement verified pixel-correct).
- **Verify with AgentPerception after EVERY room**: `check_enclosure` from room center (openings
  must match designed doorways EXACTLY — extra openings = wall gaps, missing = blocked door);
  `scene_report` (unintended overlaps, floating geometry); `measure_clearance` along the player
  path (must be clear start→end through doors).
- Then PIE-test: `StartPIE`, walk the path, `StopPIE`. Check LogsToolset for errors.

## Stage 3 — Populate procedurally (PCG)
`PCGToolset` builds/modifies PCG graphs — use for scatter (rocks/vegetation/debris/props),
building interiors, city blocks. Deterministic spatial logic beats hand-placing 500 props, and
it is the documented working approach for agent city-building (shape → districts → blocks →
buildings). Keep PCG rules in version control; regenerate, don't hand-fix.

## Stage 4 — Photoreal dressing (free content only)
- **Quixel Megascans + Fab plugins are installed** (free with UE). Photoreal = Megascans
  surfaces/meshes + Nanite + Lumen, not generated textures.
- Search assets semantically in-project: `SemanticSearchToolset`.
- Replace greybox surfaces: import asset → `add_to_scene_from_asset` (use `snap_to_ground`) →
  re-verify with scene_report (dressing must not break clearances).
- Materials: MaterialTools/MaterialInstanceTools; landscape + water via engine tools.

## Stage 5 — Lighting
Spawn via add_to_scene_from_class: DirectionalLight + SkyLight + SkyAtmosphere +
ExponentialHeightFog for exteriors; Point/Rect/Spot for interiors. Set properties via
ObjectTools.set_properties (`"PointLightComponent.Intensity": 8000.0` style). Lumen GI is on
in the lab project. Then capture eye-level screenshots and judge: readable silhouettes, no
blown highlights, shadows ground objects, lighting directs the eye along the intended path.

## What makes it read PROFESSIONAL (environment-art craft)
The difference between "dressed greybox" and "shipped AAA level" is craft, not asset count:
1. **Cluster, never scatter.** Props live in narrative clusters (2-3 sizes per cluster:
   large anchor + medium + small — the "composition triangle"), pushed to edges and corners,
   combat lanes clean. Even distribution is the #1 amateur tell.
2. **Kill the wall-floor junction.** Bare 90° wall-floor seams scream greybox. Trim pieces,
   debris lines, dirt decals, or a skirting mesh along every visible junction.
3. **Decals are the cheapest realism multiplier.** Leaks under pipes, grime radiating from
   drains, puddles in floor dips, soot above flames, wear down door centerlines. Motivated
   placement only — every stain tells you what happened there.
4. **Vertex-paint blending.** One tiling material per big surface = obvious. Blend 2-3
   Megascans surfaces (wet/dry stone, mossy/clean) with vertex paint or masks; break tiling
   with macro-variation noise.
5. **Light the path, not the room.** Motivate every light (window, candle, furnace); key the
   route and goals bright, let corners fall dark; rim/silhouette separation at the gameplay
   camera; volumetrics (god rays, fog banks) for depth layering. Lock exposure — auto-exposure
   fights art direction.
6. **Value structure per shot.** Squint test: each vista needs readable dark/mid/light zoning
   with ONE brightest focal point on the intended path (our art bible: amber/brass focal
   against blue-slate field). If everything is mid-value, it's mush.
7. **Scale witnesses.** Human-scale references everywhere (benches, tags, tools, steps) —
   they make spaces feel real and huge things feel huge.
**Verify like a pro:** score golden-path shots at the PLAYER camera with art_critic (scene
bar) — not drone angles nobody will see. Fix the named worst flaw, re-score. Composition
first, materials second, micro-detail last.

## QA loop (every stage)
3-angle capture (top-down / eye-level / player-eye) + scene_report + LogsToolset errors.
Definition of done for a level: player path walkable in PIE, zero unintended
overlaps/floating/openings, 3-angle screenshots pass art review (studio lead judges vs the
art bible), no log errors, and `stat unit` in PIE within frame budget.
