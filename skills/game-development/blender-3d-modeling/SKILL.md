---
name: blender-3d-modeling
description: "Blender mastery for game assets: headless bpy automation, 3D/2D character modeling, retopo/UV/bake, local text-to-3D drafts, export to UE 5.8."
tags: [game-development, blender, 3d-modeling, assets, characters, bpy]
platforms: [windows]
---
# Blender 3D/2D Modeling for Games

Blender is our modeling/retopo/UV/bake/rig tool — open source, fully scriptable. Agents drive
it HEADLESS via Python; the render is the verification.

## READ FIRST: docs/BLENDER-MASTERY.md (the studio's SOTA doctrine, 2026-07-07)
Research-grounded five pillars — (1) curated library over raw bpy: prefer the DATA API
(`bpy.data`/`bmesh`/`obj.modifiers.new`); `bpy.ops` fails poll() headless — wrap unavoidable
ops in `bpy.context.temp_override()` inside tools/blender_lib/, and promote any function you
use twice INTO blender_lib; (2) AAA surfaces = CC0 photo-scan base + procedural wear overlay
(`tools/blender_lib/asset_fetch.py` fetches ambientCG/Poly Haven sets by API — no login, no
EULA; `pbr_kit.make_scan_material(set_dir, wear_kind=...)` composites them); (3) verify with
eyes AND math via blender_gate; on critic fail, fix the NAMED worst flaw with a LOCALIZED
edit — never regenerate from scratch; (4) decompose into parts/modules and derive from
licensed sources; (5) every gate-passing technique becomes a recipe in
tools/blender_lib/recipes/ with a "when to use" line. Craft numbers (texel density 5.12/10.24
px/cm, FWN mid-poly, Nanite policy, Manny-skeleton rigging path, Non-Color bake contract)
are in the doctrine doc — cite the pillar when you file or fix a defect.

## Driving Blender
- Headless: `blender --background --python script.py -- <args>` (add `--factory-startup` for
  reproducibility). Locate the install via
  `Get-ItemProperty HKLM:\SOFTWARE\Classes\blendfile\shell\open\command` or common paths;
  if missing, download from blender.org (free).
- Everything is `bpy`. Structure scripts as: build scene → save .blend → render turntable
  PNGs → export .fbx/.glb. ALWAYS render 3-4 angle previews and LOOK at them before calling
  a model done — same act→capture→decode→correct discipline as UE.
- Existing work/reference: `C:\Users\dayvo\Documents\blender-agent\` (asuna/kairi/kurumi
  character busts, realistic + 2.5D variants, render setups). Study these before starting new
  characters — they encode the studio's character style.

## Character modeling (3D)
1. Reference sheets first (front/side orthos — 2D concept, below).
2. Blockout: primitives for proportions (real human 165-185cm; export scale: 1 Blender unit
   = 1m, UE expects cm — set unit scale 0.01 in scene or scale on FBX export).
3. Sculpt/refine → **retopo** for games: quads, edge loops at deformation points (shoulders,
   elbows, knees, jaw), target budgets — hero ~50-80k tris, NPC ~20-40k, background ~5-15k
   (Nanite relaxes this for static, NOT for skeletal meshes).
4. UV unwrap (islands by material region, minimal stretch) → bake high→low normal/AO →
   PBR textures (Principled BSDF; basecolor/normal/roughness/metallic maps).
5. Rig: Rigify (then convert) or build a UE-Mannequin-compatible skeleton for free retarget;
   weight paint, test poses in renders.
6. Export FBX: mesh+armature, apply transforms, tangent space, no leaf bones; import in UE
   and verify with SkeletalMeshTools (bones, sockets, materials).

## 2D characters & concept art
- Blender IS the 2D tool too: **Grease Pencil** for 2D character art/animatics; flat-shaded
  3D + toon shaders + Freestyle/line-art modifier for 2.5D (see blender-agent's
  `realistic_2p5d` work). Orthographic camera renders = sprite sheets (render per-frame per
  angle for 8-direction sprites).
- Concept passes: local Stable Diffusion-class models via ComfyUI (8GB VRAM: SDXL/quantized
  FLUX) for mood/refs; the modeled result is authoritative, not the concept.

## Props & environment assets
Same loop minus rigging. Modifier stacks (array/mirror/bevel/solidify) for kit pieces —
model FAMILIES (modular walls, crates, pipes) with consistent grid sizes (UE snap: 10/50/100cm)
so PCG and level design can assemble them.

## THE GATE (mandatory since 2026-07-07): tools/blender_gate.py
No asset or kit batch leaves the Blender lane without exit 0 from
`python tools/blender_gate.py --blend <file> [--prefix SM_Chapel] --bar scene [--budget-tris N]`.
It opens the .blend headless, runs mechanical checks (UVs present, materials assigned,
image textures resolve, object scales APPLIED, tri budget), renders a neutral-lit 4-angle
turntable + closeup (Eevee/Filmic; adds a 3-point rig if the scene has no lights), builds a
contact sheet, and scores it with the same calibrated art critic as the UE lane (>=3.5).
Exit 1 = mechanical fail (fix geometry/materials), exit 2 = critic fail (craft below bar).
Attach `blender_gate_verdict.json` + the contact sheet to your card — a completion claim
without them is invalid.

**QUALITY-DECISION RULES (2026-07-07, measured):** the VLM critic has ±0.3 noise on an
IDENTICAL image. Any pass/fail, route, or stop-rule decision MUST use `--runs 3` (median);
single runs are for iteration steering only (blender_gate itself now defaults to
median-of-3). Pick the RIGHT bar: `--bar kit` for modular architecture (the prop bar
punishes deliberately-neutral tiling modules for lacking hero wear — wrong rubric),
`prop` for isolated hero assets, `scene` only for lit assemblies/vignettes. A lighting or
render-fixture flaw is NOT an asset verdict: you're only measuring the asset once the
critic's worst-flaw class is something the fixture can't explain. KIT GOTCHA: gate renders
of modules that all sit at origin OVERLAP into a jumble the critic will trash — judge kits
mechanically per-module, artistically by their ASSEMBLY render. First live run caught both classic failure classes in a
PROFESSIONAL scene: 11 unapplied object scales (the 100x-import bug class) and unresolved
texture paths (magenta renders = the checkerboard-export bug class). The gate makes both
impossible to ship silently.

## Modular architecture kits (the cathedral workflow)
**CATHEDRAL KIT V1 EXISTS AND IS THE SPRINT 2 BASE (route verdict 2026-07-07, FINAL —
do not relitigate).** Read `docs/KIT-BIBLE-cathedral.md` FIRST: grid contract, pivots,
material sets, the proven Blender→UE chain, and the prioritized v2 iteration list
(kit-bar 2.6 in-engine → target 3.5). The whole pipeline is runnable:
`tools/build_modular_cathedral_kit.py` → gate → `tools/assemble_cathedral_bay_preview.py`
(LOOK at the assembly) → `tools/export_cathedral_kit_fbx.py` → `tools/import_cathedral_kit_ue.py`.
shape_kit now has correct-by-construction architecture primitives — USE THEM, don't
freehand: `pointed_arch_points` (rise MUST be clearly > span/2 or the arch reads Roman),
`extrude_polygon` (concave outlines + interior holes; floor-touching openings are traced
into the OUTER outline, never modeled as holes), `sweep_profile_x` (moldings),
`prism`/`octagon_profile` (piers), `box_project_uv` (headless-safe world-scale UVs, leaves
exactly ONE UVMap = UE UV0 per the t_e74beb4b rule), `merge_into` (headless join).
UE-side traps (all hit live): TextureSampleParameter2D nodes NEED default textures or the
master material silently compiles to checker; PolyHaven normals are GL → set
flip_green_channel on UE import; FBX wall fronts face -Y in UE; `unreal.Rotator` positional
args are (ROLL, PITCH, YAW) — always pass kwargs.

Architecture is a KIT problem, not a sculpture problem. Build order:
1. **Kit bible first** (one card, before any modeling): module list with exact dimensions on
   the UE grid (walls/pillars/arches/vaults in 100cm increments, floor tiles 200/400cm,
   doorways >=120cm clear), naming (`SM_<Kit>_<Module>_<Variant>`), pivot convention
   (back-bottom-left corner, Z-up, forward -Y), per-module tri budgets, and the material/
   atlas plan. Modules must tile: matching edge loops + vertex positions at seams.
2. **Trim sheets + tileable materials before modules.** 1-2 trim sheets (moldings, cornices,
   edge strips) + 3-4 tileables (stone, plaster, slate, wood) cover a whole kit. Author with
   tools/blender_lib/pbr_kit.py (see below), bake, then UV modules onto them. Unique-bake
   ONLY hero pieces (altar, bell, portal).
3. **Assemble a test bay INSIDE Blender** (array a wall run + pillars + vault into one bay)
   and run the gate on the ASSEMBLY — modules that pass individually can still fail as a
   family (scale drift, seam gaps, value mismatch).
4. **Derive, don't invent.** We hold a CC-BY professional church interior SOURCE .blend
   (external/fab_downloads/t_f7eac5b1/.../source/eglise-sketchfab5.blend + ~30 atlases,
   attribution already in docs/licenses.md — CC-BY 4.0 permits derivatives). Open it, study
   how a pro structures arches/atlases/proportions, and HARVEST: separate its meshes into
   modules, re-UV onto our trim/tileables where atlases don't fit, extend with new modules
   matched to its proportions. Deriving from a pro source beats blank-file modeling for
   both speed and score. Same license discipline for any other CC-BY/CC0 source.
5. **Export contract per batch:** apply ALL transforms (ctrl-A equivalent in bpy:
   `bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)`), binary FBX,
   1 Blender unit = 1m with scene unit scale 0.01 for UE cm, UCX_ collision meshes for
   anything the player touches, gate verdict attached, then import verify in UE (materials
   resolved, Nanite on, scale sanity vs the kit bible).

## Material craft library: tools/blender_lib/pbr_kit.py (import in your bpy scripts)
`pbr_kit.make_material(kind)` — procedural PBR implementing the playbook below: curvature
edge wear (Bevel-node trick), AO cavity dirt, noise roughness variation, micro-bump. Kinds:
wet_slate, civic_stone, tarnished_brass, soot_iron, charred_wood, aged_plaster, bronze_bell
(the Vesper Hollow palette). `pbr_kit.add_bevel_shading(obj)` kills razor edges.
`pbr_kit.bake_pbr(obj, mat, out_dir)` bakes to basecolor/roughness/normal for export.
Extend the palette in the file (it's a library, not a black box) — but NEVER ship a
single-value-roughness material; that's the #1 reason scripted assets read "programmer art".

## Local text-to-3D drafts (open source, fits 8GB VRAM) — OPERATIONAL
**Working pipeline (verified E2E on this machine):**
`python C:\Users\dayvo\Documents\ue5-agent-studio\tools\gen3d.py <image> --name SM_Thing
--tris 15000 --import-ue`
= TripoSR (GPU, ~4s after model load) → Blender headless cleanup (decimate, normals,
Y-up→Z-up axis fix already handled) → FBX → UE import with Nanite → size report.
Venv: ue5-agent-studio\gen3d\venv (torch cu121; torchmcubes is a PyMCubes shim — do NOT
try to pip-install real torchmcubes, it needs a CUDA toolkit that isn't installed).
Text-to-3D = make a concept image first (ComfyUI/SD), then gen3d it. Quality is DRAFT tier —
always judge the placed result via CaptureViewport; regenerate or hand-model if it reads
melty at gameplay distance. Hero assets: prefer Megascans/Fab (free, photoreal) or
hand-modeling. GPU is shared with the editor — batch generation, don't run live.

## The professional-quality playbook (why scripted assets read "programmer art" — and the fix)
The frontier art critic now judges everything; these are the craft rules that move scores.
Naively scripted geometry ALWAYS reads amateur for predictable, fixable reasons:
1. **Razor edges.** Nothing real has a perfectly sharp edge. Bevel/chamfer EVERYTHING
   (Bevel modifier 1-3mm + weighted normals) — edge highlights are what sell "solid object".
2. **Single-value roughness.** Uniform roughness = plastic toy. Realism is ~80% roughness
   VARIATION: layer noise-driven breakup + curvature-driven edge wear (Pointiness/bevel-AO
   into ColorRamp) + cavity dirt. All procedural, all scriptable, bake to maps on export.
3. **No wear logic.** Weathering goes where physics puts it: edge wear where hands/rain rub,
   dirt in crevices and floor junctions, patina on brass EXCEPT polished touch-points,
   water-darkening on lower edges. Arbitrary grunge reads as noise, motivated wear as history.
4. **Uniform repetition.** Arrays of identical elements scream procedural. Break with per-
   instance rotation/scale jitter (geometry nodes), asymmetric damage, one unique element
   per cluster.
5. **Detail density mismatch.** Real objects have 3 frequencies: silhouette forms, mid detail
   (seams/panels/stitches), micro (normal-map texture). Missing mids = "melty"; missing micro
   = "clay". Author all three deliberately.
**Agent-native routes to pro quality (in preference order):**
(a) **Kitbash photoscans** — Megascans meshes/surfaces as detail donors; pro quality is IN
    the scan, composition is your work. (b) **Geometry nodes** procedural detail — scriptable,
    deterministic, art-directable. (c) **Procedural PBR shaders** per rule 2, baked out.
(d) Hand-modeled forms + tileable detail normals. Humans stay in the MetaHuman lane;
    Blender characters = stylized/creatures/2.5D where sculpt-realism isn't the bar.
**Capture discipline:** the critic grades the IMAGE. Neutral A-pose, flat lighting, or blown
exposure fails a good asset. Pose it, light it (3-point + rim), expose it (no clipped whites),
frame it — then score, fix the named worst flaw, re-score. Two focused iterations beat ten
blind rebuilds.

## Bridge to UE
Import: `AssetTools` toolset or drop into `Content/` and let the editor scan. After import:
enable Nanite on static meshes (StaticMeshTools), check materials resolved, LODs/collision
present (`setup_collision`, generate LODs for skeletal), then place a test instance and
CaptureViewport to confirm scale/look in-world.
