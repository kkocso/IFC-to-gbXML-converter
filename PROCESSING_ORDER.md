# IFC → gbXML Processing-Order Analysis

This document walks through the actual order in which `IFC_gbXML_Convert.py`
turns an IFC2X3 file into a gbXML 6.01 document. It focuses on the questions
asked by the user — zones, room/wall sizing, neighbour-wall reconciliation,
combined rooms, filtering of redundant walls, and interior vs. exterior
classification of openings — and flags places where the order is fragile or
plain wrong, with a suggested optimal order at the end.

All line numbers refer to `IFC_gbXML_Convert.py` at the current revision of
`claude/document-ifc-converter-order-KFopF`.

---

## 1. Top-level processing order (what runs, in what sequence)

`convert()` is one long top-down pass. It does **not** build any intermediate
data model; XML nodes are emitted as IFC entities are visited. The phases are:

| # | Phase | Driving IFC type | Lines |
|---|-------|------------------|-------|
| 1 | Open file, detect length unit (`MILLI`, `CENTI`, …) | `IfcUnitAssignment` | 229–251 |
| 2 | Create `<gbXML>` root and ID dictionary | — | 253–267 |
| 3 | Emit `<Campus>` / `<Location>` (lat/lon/elevation/CADModelAzimuth) | `IfcSite`, `IfcGeometricRepresentationContext.TrueNorth` | 270–333 |
| 4 | Collect addresses, write `<ZipcodeOrPostalCode>` and `<StreetAddress>` | `IfcPostalAddress` (from site, building, standalone) | 336–415 |
| 5 | Emit `<Building>` (one only — last wins) | `IfcBuilding` | 383–395 |
| 6 | Emit `<BuildingStorey>` for each storey | `IfcBuildingStorey` | 418–436 |
| 7 | Emit `<Space>` per room: area, height-fields, volume, name, description, then **first pass** of space boundaries | `IfcSpace` + its `BoundedBy` | 438–598 |
| 8 | Emit `<Surface>` (walls/slabs/coverings/roofs) and `<Opening>` (windows) — **second pass** over all `IfcRelSpaceBoundary` | `IfcRelSpaceBoundary` | 600–811 |
| 9 | Emit `<WindowType>` per `IfcWindow`, including U-value / SHGC / VLT | `IfcWindow` + its property sets | 813–857 |
| 10 | Emit `<Construction>` per material association — **third pass** over boundaries | `IfcRelSpaceBoundary` | 859–921 |
| 11 | Emit `<Layer>` per building element | `IfcBuildingElement` | 923–953 |
| 12 | Emit `<Material>` per `IfcMaterialLayer` (thickness, R-value) | `IfcBuildingElement` (again) | 955–1046 |
| 13 | `<DocumentHistory>` (program / person / date) | `IfcApplication`, `IfcPerson` | 1048–1084 |
| 14 | Pretty-print and write XML | — | 1086–1091 |

The same `IfcRelSpaceBoundary` collection is iterated **three times** (steps
7, 8, 10) and the same `IfcBuildingElement` collection twice (steps 11, 12).

---

## 2. Zones — how are they "calculated"?

**They are not.** The gbXML schema has a top-level `<Zone>` element that is
used to group thermally similar `<Space>`s (and is what most simulation
engines actually run on); this script never creates one. There is no use of
`IfcZone` (the IFC analogue) and no aggregation step that walks
`IfcRelAssignsToGroup` to discover zone membership.

Instead, every `IfcSpace` becomes a `<Space>` directly under `<Building>`
(lines 449–455). Each `<Space>` only carries `buildingStoreyIdRef`; it has no
`zoneIdRef`. As a result:

- Multi-room thermal zones cannot be expressed.
- The downstream simulator (e.g. Winwatt) is forced to treat each room as
  its own zone or to re-zone manually.
- `IfcZone` groupings authored in ArchiCAD are silently dropped.

> **Strange/missing behaviour #1 — no zone support at all.** This is the
> single biggest functional gap. See §8 for a suggested fix.

## 3. How a "zone" (here: a space) affects wall sizing

Because there are no real zones, sizing is per-space. Two pieces of data are
captured per `IfcSpace` and later reused on its bounding walls:

1. **Room height** — pulled from `BaseQuantities` / `ArchiCADQuantities` in
   the order
   `Height → FinishCeilingHeight → ClearHeight → FinishFloorHeight`
   (lines 498–519). The first `Height` quantity is stored in the
   `space_heights` dict keyed by space `GlobalId` (line 516) and is also
   written as the `<Space height="…">` attribute and a `<Height>` child.

2. **Floor area / volume** — area uses
   `GrossFloorArea → NetFloorArea → GrossCeilingArea → pset['Area']`
   (lines 478–491); volume uses `GrossVolume → NetVolume → pset['Volume']`
   (lines 522–535).

When the wall surface is later emitted in phase 8 it picks up its `Height`
from `space_heights[element.RelatingSpace.GlobalId]` (lines 717–727):

```python
is_vertical = abs(tilt - 90.0) < 45.0
if is_vertical:
    space_gid = element.RelatingSpace.GlobalId
    if space_gid in space_heights:
        surf_h = space_heights[space_gid]
    else:
        zs = [v[2] for v in scaled_vertices]
        surf_h = round(max(zs) - min(zs), 4) if zs else 0.0
```

So the **wall's `Height` is the room's height**, not the wall's own vertical
extent — except as a fallback when the room has no `Height` quantity. The
wall's `Width` is the horizontal length computed by
`_compute_surface_width()` (lines 179–195), projecting all vertices onto
`cross(normal, Z_up)` and taking max-min.

> **Strange/fragile behaviour #2 — `is_vertical` threshold is 45°.** A
> sloped roof at any tilt between 45° and 135° is classed as a wall and
> handed the room height. A 60°-pitched roof would therefore receive a
> `Height` equal to the room height instead of its actual rafter length. A
> tighter threshold (e.g. ±15°) would be safer.

## 4. Are neighbour walls checked to be the same type?

There is no explicit "do my two sides agree?" check, but there is a
deduplication pass that has the same effect for one specific case
(interior vs. exterior).

The mechanism (lines 607–658):

```python
element_to_surface = {}     # IfcWall.GlobalId → <Surface> XML node

for element in boundaries:                       # IfcRelSpaceBoundary
    ...
    if is_wall_or_slab:
        elem_gid = element.RelatedBuildingElement.GlobalId
        if elem_gid in element_to_surface:
            surface = element_to_surface[elem_gid]
            # Fix 6: upgrade InteriorWall → ExteriorWall …
            if (element.RelatedBuildingElement.is_a('IfcWall')
                    and element.InternalOrExternalBoundary == 'EXTERNAL'
                    and surface.getAttribute('surfaceType') == 'InteriorWall'):
                surface.setAttribute('surfaceType', 'ExteriorWall')
            …add AdjacentSpaceId if missing…
            continue
```

So:

- The **first** `IfcRelSpaceBoundary` that references a given `IfcWall`
  creates the `<Surface>` and stamps it `InteriorWall` or `ExteriorWall`
  using that boundary's `InternalOrExternalBoundary` flag (lines 679–682).
- Every **subsequent** boundary referring to the same wall:
  - adds an `<AdjacentSpaceId>` if it points to a different space
    (lines 649–657);
  - and if the new boundary says `EXTERNAL` while the surface was tagged
    `InteriorWall`, it is *upgraded* to `ExteriorWall` (the "Fix 6" comment).

What this does **not** do:

- It never downgrades `ExteriorWall` → `InteriorWall`. That's intentional and
  correct (a wall with one external side is exterior).
- It does not verify that both sides agree on the *construction*
  (`HasAssociations[0]`) — only the type assigned by the first boundary
  encountered is kept.
- It does not verify that the two `<AdjacentSpaceId>` belong to the same
  storey, or that the two boundaries' polygons actually coincide.

> **Strange/fragile behaviour #3 — first-encounter wins for everything
> except the interior/exterior flag.** Construction, name, geometry,
> rectangular geometry (Azimuth, Tilt, Height, Width) are all taken from
> the boundary that happens to come first in the IFC file's
> `IfcRelSpaceBoundary` ordering. If side A's boundary is missing
> `ConnectionGeometry` but side B's is present, side A wins anyway and the
> wall is dropped (line 619 returns `continue` and never registers it, so
> side B never reaches the dedup branch and we get *two* surfaces). A
> "best of both sides" merge would be more robust — see §8.

## 5. Combined kitchen + living room (no wall between two zones)

In IFC the canonical way to mark "two spaces but no physical separator" is a
`IfcRelSpaceBoundary` whose `PhysicalOrVirtualBoundary == 'VIRTUAL'` and
whose `RelatedBuildingElement` is either an `IfcVirtualElement` or `None`.

The script **discards every virtual boundary**, twice:

```python
# In the IfcSpace loop, line 562
if element.PhysicalOrVirtualBoundary == 'VIRTUAL':
    continue
```

```python
# In the IfcRelSpaceBoundary loop, line 617–618
if element.PhysicalOrVirtualBoundary == 'VIRTUAL':
    continue
```

Consequences:

- A combined kitchen + living room modelled as two `IfcSpace`s with a
  virtual boundary between them is exported as **two completely
  disconnected `<Space>` elements**. The opening between them is invisible
  to the simulator — heat will not flow between the two rooms.
- There is also no zone-grouping fallback (see §2), so the two rooms
  cannot even be merged into one thermal zone in the output.
- Air-flow / inter-zone-airflow gbXML elements (`<AirLoop>`,
  `<InteriorWall>` with `surfaceType="Air"`) are never emitted.

> **Strange/serious behaviour #4 — virtual boundaries silently dropped.** The
> only signal IFC gives us about combined rooms is exactly the one we
> throw away. The correct behaviour is to emit an `<InteriorWall>` with
> `surfaceType="Air"` (gbXML 6.01 supports it) for each virtual
> boundary, with the two spaces as `<AdjacentSpaceId>`. A weaker but still
> useful fix is to group spaces connected by virtual boundaries into one
> `<Zone>` and emit it.

## 6. Order in which redundant wall objects are filtered out

The filter chain in the `IfcRelSpaceBoundary` pass (lines 613–664) runs in
this order — every check is a `continue` (no recovery):

1. **`RelatedBuildingElement is None`** — boundary points to nothing
   (line 614).
2. **`PhysicalOrVirtualBoundary == 'VIRTUAL'`** — virtual surface
   (line 617). *See §5.*
3. **`ConnectionGeometry is None`** — boundary has no geometry container
   (line 619).
4. **`SurfaceOnRelatingElement is None`** — geometry container has no
   actual surface (line 621).
5. **Type filter** — keep only `IfcCovering`, `IfcSlab`, `IfcWall`,
   `IfcRoof` (the "wall-or-slab" group) or `IfcWindow`. Everything else,
   including `IfcDoor`, `IfcCurtainWall`, `IfcColumn`, `IfcBeam`,
   `IfcPlate`, `IfcMember`, is dropped (lines 626–635).
6. **Element-level deduplication** — for the wall-or-slab group, if this
   element's `GlobalId` is already in `element_to_surface`, only update
   adjacency/type and `continue` (lines 638–658).
7. **Vertex extraction failure** — if `get_boundary_vertices()` returns
   `[]` (unsupported curve, broken `IfcCurveBoundedPlane`, etc.), warn and
   skip (lines 661–664).
8. **Window-only deduplication** — `window_to_opening` short-circuits a
   second `<Opening>` for the same `IfcWindow` (lines 760–762).

> **Strange/fragile behaviour #5 — `IfcDoor` is not in the type filter at
> all.** Doors are dropped at step 5, which means an exterior door is
> never emitted as an `<Opening>` and the wall it sits in has no door
> hole. This is wrong for both energy and daylight calculations.

> **Strange/fragile behaviour #6 — vertex extraction failures are dropped
> after deduplication.** Step 7 happens *after* step 6, so if the first
> boundary for a wall has unsupported geometry, the wall is dropped
> entirely; later boundaries for the same wall never get the chance to
> create the surface because the dedup table has not been populated. A
> fix is to attempt vertex extraction *before* registering the surface
> and only register on success.

## 7. How is a window/door classified as interior or exterior?

### 7.1 Walls

Per boundary, by reading `IfcRelSpaceBoundary.InternalOrExternalBoundary`:

```python
# lines 679–682
if … is_a('IfcWall') and element.InternalOrExternalBoundary == 'EXTERNAL':
    surface.setAttribute('surfaceType', 'ExteriorWall')
if … is_a('IfcWall') and element.InternalOrExternalBoundary == 'INTERNAL':
    surface.setAttribute('surfaceType', 'InteriorWall')
```

The "Fix 6" upgrade described in §4 then guarantees that any wall seen as
EXTERNAL on at least one side becomes `ExteriorWall`.

### 7.2 Windows

Windows do **not** decide their own interior/exterior status. The window
`<Opening>` is appended to whatever wall surface the script is *currently
holding in the `surface` local variable* at the moment it processes the
window's boundary (line 811: `surface.appendChild(opening)`).

The relevant code (lines 612, 758–811):

```python
surface = None  # keep reference for Window openings appended below
for element in boundaries:
    …
    if is_wall_or_slab:
        surface = root.createElement('Surface')   # overwrites `surface`
        …
        element_to_surface[…] = surface

    if is_window and surface is not None:
        …
        surface.appendChild(opening)
```

This works **only if** `IfcRelSpaceBoundary` for the window appears
*after* the boundary for its host wall in the IFC file's iteration order
**and** no other wall boundary comes between them. The script never reads
`IfcRelFillsElement` / `IfcRelVoidsElement`, which is the
correct way to tie a window to its host wall.

Practical consequences:

- If a window's boundary precedes its wall's boundary, the window is
  silently dropped (the `surface is not None` guard).
- If two walls' boundaries straddle the window, the window is glued to
  the **wrong** wall — and inherits that wall's interior/exterior status.
- A window therefore ends up "interior" or "exterior" purely by ordering
  accident.

Doors are not classified at all (see §6).

> **Strange/serious behaviour #7 — window→wall association by iteration
> order.** Replace `surface` with an explicit lookup:
> `host_wall_gid = ifc_window.FillsVoids[0].RelatingOpeningElement.VoidsElements[0].RelatingBuildingElement.GlobalId`
> then `surface = element_to_surface[host_wall_gid]`. This decouples the
> result from IFC ordering and makes it correct for both windows and doors.

## 8. Other order-dependent oddities

| # | Where | Issue |
|---|-------|-------|
| 8.1 | Lines 270–334 | If the file has more than one `IfcSite` or more than one `IfcBuilding`, only the **last** survives because `campus`, `location`, `building` are scalar locals overwritten in the loop. |
| 8.2 | Lines 859–921 | The `Construction` pass iterates `boundaries` again and uses `HasAssociations[0]` only — multi-association elements lose all but the first material set. It also re-reads U-value the same way the `Surface` pass already implicitly did, instead of caching. |
| 8.3 | Lines 923–953 | `Layer` is appended to `gbxml` *inside* the `for layer_item …` loop, so a layer with N materials is appended N times to `<gbXML>`. The schema validator accepts it but the file gets bigger. |
| 8.4 | Lines 955–1046 | `Material` is also appended once per matching property hit; for a wall with several `IfcRelDefinesByProperties` you can see the same `<Material>` emitted twice or three times. |
| 8.5 | Line 388 | `buildingType="Unknown"` is hardcoded — `IfcBuilding.OccupancyType` and the `Pset_BuildingCommon.OccupancyType` property are never read. |
| 8.6 | Lines 1068–1077 | `created` is built inside one `for` loop and used inside another. If `IfcPerson` is empty but `IfcApplication` is not, `created` is undefined when the second loop runs (`NameError`). |
| 8.7 | Line 716 | `is_vertical = abs(tilt - 90.0) < 45.0` — see §3. |
| 8.8 | Line 457 | `s.Decomposes[0].RelatingObject.GlobalId` — assumes the decomposing object is a storey. If a space is decomposed by another space (mezzanine), the storey ref points to a non-storey. |

## 9. Suggested optimal processing order

A cleaner pipeline that fixes the issues above without changing the public
API:

1. **Open file & detect units** (unchanged).
2. **First pass — index everything into Python data structures, no XML yet.**
   - Build `space_heights`, `space_areas`, `space_volumes`,
     `storey_of_space`, address records, true-north angle.
   - Walk `IfcRelSpaceBoundary` once and populate:
     - `wall_records[ifc_wall.GlobalId] = { sides: [boundary,…],
       construction, geometry, has_external_side }`
     - `virtual_pairs = [(spaceA, spaceB), …]` from VIRTUAL boundaries.
     - `window_to_host = { ifc_window.GlobalId: host_wall.GlobalId }` via
       `IfcRelFillsElement` / `VoidsElements` (not iteration order).
     - `door_to_host`  same trick for `IfcDoor`.
   - Walk `IfcZone` and `IfcRelAssignsToGroup` to build
     `zone_of_space`.
3. **Second pass — emit XML in schema-friendly top-down order:**
   1. `<Campus>` / `<Location>` / `<Building>` / `<BuildingStorey>`.
   2. `<Zone>` per IfcZone or per virtual-boundary group (so that
      kitchen + living room ends up in one zone).
   3. `<Space>` per IfcSpace, referencing `zoneIdRef` if any.
   4. `<Surface>` per wall record (one surface per IfcWall, with both
      `<AdjacentSpaceId>` already known) — this removes the
      first-encounter-wins fragility entirely.
   5. `<Opening>` per window/door, attached to the host surface looked up
      from `window_to_host` / `door_to_host`.
   6. For each `virtual_pair`, emit an `<InteriorWall>` with
      `surfaceType="Air"` and the two spaces as `<AdjacentSpaceId>` so
      energy flows between them.
   7. `<WindowType>`, `<Construction>`, `<Layer>`, `<Material>`
      from the indexed data — guaranteed unique because the index
      already deduplicated.
   8. `<DocumentHistory>`.
4. **Validation pass.** Before writing, sanity-check:
   - every `<Surface>` has 1 or 2 `<AdjacentSpaceId>`;
   - every `<Opening>` is inside a `<Surface>`;
   - every `spaceIdRef` resolves;
   - no duplicate `id`.
5. **Write XML** (unchanged).

The key wins of this order:

- One iteration of `IfcRelSpaceBoundary` instead of three.
- Window/door host lookup is data-driven, not iteration-driven (fixes §7).
- Combined-room information is preserved (fixes §5).
- Wall type/construction is decided after seeing **both** sides (fixes §4).
- Virtual-element dedup prevents the duplicated `<Layer>` / `<Material>`
  emissions (fixes §8.3, §8.4).
- A real `<Zone>` element appears for every `IfcZone` (fixes §2).
