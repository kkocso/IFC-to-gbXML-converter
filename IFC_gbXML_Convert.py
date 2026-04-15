#!/usr/bin/env python3
"""
IFC to gbXML Converter
======================
Converts an IFC2X3_TC1 file to a validated gbXML 6.01 XML file.

Usage:
    python IFC_gbXML_Convert.py <input.ifc>
    python IFC_gbXML_Convert.py path/to/model.ifc
    python IFC_gbXML_Convert.py "Test cases/Pilot project 1/Pilot project 1.ifc"

Output:
    output/<input_stem>_gbXML.xml

Requirements:
    - ifcopenshell  (conda install -c conda-forge ifcopenshell)
    - The IFC file must contain 2nd level space boundaries (IfcRelSpaceBoundary).
    - pythonocc-core is NO LONGER required.

Author: Maarten Visschers (original), refactored for CLI usage.
"""

import argparse
import datetime
import math
import os
import sys
import time
from pathlib import Path
from xml.dom import minidom


# Module-level reference (populated by _init_geometry)
ifcopenshell = None


def _init_geometry():
    """Import ifcopenshell (no OCC required)."""
    global ifcopenshell
    import ifcopenshell as _ifc
    ifcopenshell = _ifc


# ---------------------------------------------------------------------------
# Geometry helpers – convert IfcCurveBoundedPlane to 3D vertices directly,
# without pythonocc-core / OpenCASCADE.
# ---------------------------------------------------------------------------

def _normalize(v):
    length = math.sqrt(sum(c * c for c in v))
    if length == 0:
        return v
    return tuple(c / length for c in v)


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _transform_2d_to_3d(points_2d, placement):
    """
    Transform a list of 2D local (u, v) coordinates into 3D world coordinates
    using an IfcAxis2Placement3D.
    """
    origin = tuple(float(c) for c in placement.Location.Coordinates)

    if placement.Axis:
        z_axis = _normalize(tuple(float(c) for c in placement.Axis.DirectionRatios))
    else:
        z_axis = (0.0, 0.0, 1.0)

    if placement.RefDirection:
        x_axis = _normalize(tuple(float(c) for c in placement.RefDirection.DirectionRatios))
    else:
        x_axis = (1.0, 0.0, 0.0)

    y_axis = _normalize(_cross(z_axis, x_axis))

    vertices = []
    for coords in points_2d:
        u = float(coords[0])
        v = float(coords[1])
        x3d = origin[0] + u * x_axis[0] + v * y_axis[0]
        y3d = origin[1] + u * x_axis[1] + v * y_axis[1]
        z3d = origin[2] + u * x_axis[2] + v * y_axis[2]
        vertices.append((x3d, y3d, z3d))
    return vertices


def _curve_to_2d_points(curve):
    """Extract 2D point coordinates from IfcPolyline or IfcCompositeCurve."""
    if curve.is_a('IfcPolyline'):
        return [pt.Coordinates for pt in curve.Points]
    if curve.is_a('IfcCompositeCurve'):
        pts = []
        for seg in (curve.Segments or []):
            parent = seg.ParentCurve
            if parent.is_a('IfcPolyline'):
                # Skip last point to avoid duplicating the junction vertex
                pts.extend(pt.Coordinates for pt in parent.Points[:-1])
            elif parent.is_a('IfcTrimmedCurve'):
                # Approximate: just take start/end trim points
                for trim in list(parent.Trim1 or []) + list(parent.Trim2 or []):
                    if hasattr(trim, 'Coordinates'):
                        pts.append(trim.Coordinates)
        return pts
    return []


def get_boundary_vertices(geom):
    """
    Return a list of (x, y, z) tuples for the outer boundary of an
    IfcCurveBoundedPlane, computed via pure Python coordinate transform.
    Returns [] if the geometry type is unsupported or data is missing.
    """
    if geom is None:
        return []
    if not geom.is_a('IfcCurveBoundedPlane'):
        return []
    basis = geom.BasisSurface
    if basis is None or not basis.is_a('IfcPlane'):
        return []
    placement = basis.Position
    if placement is None:
        return []
    outer = geom.OuterBoundary
    if outer is None:
        return []

    points_2d = _curve_to_2d_points(outer)
    if not points_2d:
        return []

    return _transform_2d_to_3d(points_2d, placement)


def _compute_surface_normal(vertices):
    """
    Compute unit normal vector from a polygon's vertices using Newell's method.
    Returns (nx, ny, nz).
    """
    n = [0.0, 0.0, 0.0]
    count = len(vertices)
    for i in range(count):
        v1 = vertices[i]
        v2 = vertices[(i + 1) % count]
        n[0] += (v1[1] - v2[1]) * (v1[2] + v2[2])
        n[1] += (v1[2] - v2[2]) * (v1[0] + v2[0])
        n[2] += (v1[0] - v2[0]) * (v1[1] + v2[1])
    length = math.sqrt(sum(c * c for c in n))
    if length < 1e-12:
        return (0.0, 0.0, 1.0)
    return tuple(c / length for c in n)


def _normal_to_azimuth_tilt(normal):
    """
    Convert a surface outward normal vector to gbXML Azimuth and Tilt angles.
    Azimuth: degrees clockwise from North (Y+ axis), range [0, 360).
    Tilt:    degrees from upward vertical; 0=ceiling, 90=wall, 180=floor.
    """
    nx, ny, nz = normal
    # Tilt from upward vertical (Z+)
    tilt = math.degrees(math.acos(max(-1.0, min(1.0, nz))))
    # Azimuth only meaningful for non-horizontal surfaces
    horiz_len = math.sqrt(nx * nx + ny * ny)
    if horiz_len < 1e-6:
        azimuth = 0.0  # floor or ceiling: azimuth undefined
    else:
        # atan2(nx, ny): angle from Y+(North) clockwise to horizontal projection
        azimuth = math.degrees(math.atan2(nx, ny)) % 360.0
    return round(azimuth, 2), round(tilt, 2)


def _compute_surface_width(vertices, normal):
    """
    Compute the wall width (horizontal extent) in the direction perpendicular
    to the surface normal and perpendicular to Z (i.e. along the wall length).
    For a vertical wall: returns the wall length in metres.
    For a horizontal surface: returns the extent in X.
    """
    nx, ny, nz = normal
    # Horizontal direction = cross(normal, Z_up) = (ny*1-nz*0, nz*0-nx*1, 0) = (ny, -nx, 0)
    hx, hy = ny, -nx
    h_len = math.sqrt(hx * hx + hy * hy)
    if h_len < 1e-9:
        hx, hy = 1.0, 0.0  # for floor/ceiling: project onto X
    else:
        hx, hy = hx / h_len, hy / h_len
    projections = [v[0] * hx + v[1] * hy for v in vertices]
    return round(max(projections) - min(projections), 4)


# ---------------------------------------------------------------------------
# XML ID sanitisation helpers
# ---------------------------------------------------------------------------
def _sanitise(prefix, raw):
    """Strip characters that are illegal in XML IDs."""
    return prefix + raw.replace('$', '').replace(':', '').replace(' ', '').replace('(', '').replace(')', '')


def fix_xml_cmps(a):   return _sanitise('campus', a)
def fix_xml_bldng(a):  return _sanitise('building', a)
def fix_xml_stry(a):   return _sanitise('storey', a)
def fix_xml_spc(a):    return _sanitise('space', a)
def fix_xml_id(a):     return _sanitise('id', a)
def fix_xml_name(a):   return _sanitise('object', a)
def fix_xml_cons(a):   return _sanitise('construct', a)
def fix_xml_layer(a):  return _sanitise('lyr', a)


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------
def convert(ifc_path: Path, output_path: Path) -> Path:
    """
    Read *ifc_path*, build a gbXML DOM, and write it to *output_path*.
    Returns the resolved output path.
    """

    # Import ifcopenshell (no OCC/pythonocc-core required)
    _init_geometry()

    print(f"[INFO] Opening IFC file: {ifc_path}")
    ifc_file = ifcopenshell.open(str(ifc_path))

    # Detect IFC project length unit and compute scale factor to convert to metres.
    # ArchiCAD typically exports in MILLIMETRE → scale = 0.001.
    # If the project uses METRE → scale = 1.0.
    _prefix_scales = {
        'EXA': 1e18, 'PETA': 1e15, 'TERA': 1e12, 'GIGA': 1e9, 'MEGA': 1e6,
        'KILO': 1e3, 'HECTO': 1e2, 'DECA': 1e1,
        None: 1.0,
        'DECI': 1e-1, 'CENTI': 1e-2, 'MILLI': 1e-3, 'MICRO': 1e-6,
        'NANO': 1e-9, 'PICO': 1e-12, 'FEMTO': 1e-15, 'ATTO': 1e-18,
    }
    length_scale = 1.0  # default: already in metres
    try:
        for unit_assign in ifc_file.by_type('IfcUnitAssignment'):
            for u in unit_assign.Units:
                if u.is_a('IfcSIUnit') and u.UnitType == 'LENGTHUNIT':
                    prefix = getattr(u, 'Prefix', None)
                    length_scale = _prefix_scales.get(prefix, 1.0)
                    break
    except Exception:
        pass
    print(f"[INFO] IFC length unit scale factor: {length_scale} (1.0 = metres, 0.001 = millimetres)")

    # Create the XML root
    root = minidom.Document()

    # -- gbXML root element --------------------------------------------------
    gbxml = root.createElement('gbXML')
    root.appendChild(gbxml)
    gbxml.setAttribute('xmlns', 'http://www.gbxml.org/schema')
    gbxml.setAttribute('temperatureUnit', 'C')
    gbxml.setAttribute('lengthUnit', 'Meters')
    gbxml.setAttribute('areaUnit', 'SquareMeters')
    gbxml.setAttribute('volumeUnit', 'CubicMeters')
    gbxml.setAttribute('useSIUnitsForResults', 'true')
    gbxml.setAttribute('version', '0.37')

    dict_id = {}

    # -- Campus (IfcSite) ----------------------------------------------------
    site = ifc_file.by_type('IfcSite')
    campus = None
    location = None
    for element in site:
        campus = root.createElement('Campus')
        campus.setAttribute('id', fix_xml_cmps(element.GlobalId))
        gbxml.appendChild(campus)
        dict_id[fix_xml_cmps(element.GlobalId)] = campus

        location = root.createElement('Location')
        campus.appendChild(location)

        def _dms_to_decimal(dms_tuple):
            """Convert IFC DMS tuple (deg, min, sec, microsec) to decimal degrees."""
            if not dms_tuple or len(dms_tuple) < 1:
                return 0.0
            d = float(dms_tuple[0])
            m = float(dms_tuple[1]) if len(dms_tuple) > 1 else 0.0
            s = float(dms_tuple[2]) if len(dms_tuple) > 2 else 0.0
            us = float(dms_tuple[3]) if len(dms_tuple) > 3 else 0.0
            sign = -1.0 if d < 0 else 1.0
            return sign * (abs(d) + m / 60.0 + s / 3600.0 + us / 3600000000.0)

        if element.RefLongitude:
            lon_dec = _dms_to_decimal(element.RefLongitude)
            longitude = root.createElement('Longitude')
            longitude.appendChild(root.createTextNode(str(round(lon_dec, 8))))
            location.appendChild(longitude)

        if element.RefLatitude:
            lat_dec = _dms_to_decimal(element.RefLatitude)
            latitude = root.createElement('Latitude')
            latitude.appendChild(root.createTextNode(str(round(lat_dec, 8))))
            location.appendChild(latitude)

        elevation = root.createElement('Elevation')
        elevation.appendChild(root.createTextNode(str(element.RefElevation or 0)))
        location.appendChild(elevation)

        # Site name → Location/Name
        site_name = (element.Name or '').strip()
        if site_name:
            loc_name = root.createElement('Name')
            loc_name.appendChild(root.createTextNode(site_name))
            location.appendChild(loc_name)

        # TrueNorth: angle from geographic North, read from IfcGeometricRepresentationContext
        # TrueNorth direction vector (2D) → angle in degrees (clockwise from North)
        try:
            for ctx in ifc_file.by_type('IfcGeometricRepresentationContext'):
                tn = getattr(ctx, 'TrueNorth', None)
                if tn is None:
                    continue
                dr = tn.DirectionRatios
                if len(dr) >= 2:
                    import math as _math
                    # atan2(x, y): angle from Y+ (North) clockwise
                    angle_deg = _math.degrees(_math.atan2(float(dr[0]), float(dr[1])))
                    true_north_el = root.createElement('CADModelAzimuth')
                    true_north_el.appendChild(root.createTextNode(str(round(angle_deg, 4))))
                    location.appendChild(true_north_el)
                    break
        except Exception:
            pass

    # Collect all postal addresses from IfcSite, IfcBuilding, or standalone
    def _collect_addresses(ifc_f):
        addrs = []
        # From IfcSite.SiteAddress
        for s in ifc_f.by_type('IfcSite'):
            if getattr(s, 'SiteAddress', None):
                addrs.append(s.SiteAddress)
        # From IfcBuilding.BuildingAddress
        for b in ifc_f.by_type('IfcBuilding'):
            if getattr(b, 'BuildingAddress', None):
                addrs.append(b.BuildingAddress)
        # Standalone IfcPostalAddress
        addrs.extend(ifc_f.by_type('IfcPostalAddress'))
        # Deduplicate by id
        seen = set()
        result = []
        for a in addrs:
            if id(a) not in seen:
                seen.add(id(a))
                result.append(a)
        return result

    address = _collect_addresses(ifc_file)

    for element in address:
        if location is not None and element.PostalCode:
            zipcode = root.createElement('ZipcodeOrPostalCode')
            zipcode.appendChild(root.createTextNode(element.PostalCode))
            location.appendChild(zipcode)

        # Build a descriptive name from available address fields
        addr_parts = []
        if getattr(element, 'AddressLines', None):
            addr_parts.extend([ln for ln in element.AddressLines if ln])
        if getattr(element, 'Town', None):
            addr_parts.append(element.Town)
        if getattr(element, 'Region', None):
            addr_parts.append(element.Region)
        if getattr(element, 'Country', None):
            addr_parts.append(element.Country)
        if location is not None and addr_parts:
            # Only add Name if not already present from site name
            if not location.getElementsByTagName('Name'):
                loc_name = root.createElement('Name')
                loc_name.appendChild(root.createTextNode(', '.join(addr_parts)))
                location.appendChild(loc_name)

    # -- Building (IfcBuilding) ----------------------------------------------
    building = None
    buildings = ifc_file.by_type('IfcBuilding')
    for element in buildings:
        building = root.createElement('Building')
        building.setAttribute('id', fix_xml_bldng(element.GlobalId))
        building.setAttribute('buildingType', 'Unknown')
        # Building name
        bldg_name = (element.Name or '').strip()
        if bldg_name:
            building.setAttribute('name', bldg_name)
        if campus is not None:
            campus.appendChild(building)
        dict_id[fix_xml_bldng(element.GlobalId)] = building

    if building is not None:
        for element in address:
            # Build full street address string
            parts = []
            if getattr(element, 'AddressLines', None):
                parts.extend([ln for ln in element.AddressLines if ln])
            if getattr(element, 'Town', None):
                parts.append(element.Town)
            if getattr(element, 'PostalCode', None):
                parts.append(element.PostalCode)
            if getattr(element, 'Region', None):
                parts.append(element.Region)
            if getattr(element, 'Country', None):
                parts.append(element.Country)
            if parts:
                streetAddress = root.createElement('StreetAddress')
                streetAddress.appendChild(root.createTextNode(', '.join(parts)))
                building.appendChild(streetAddress)
                break  # one address block is enough

    # -- BuildingStorey (IfcBuildingStorey) -----------------------------------
    storeys = ifc_file.by_type('IfcBuildingStorey')
    storey_idx = 1
    for element in storeys:
        buildingStorey = root.createElement('BuildingStorey')
        buildingStorey.setAttribute('id', fix_xml_stry(element.GlobalId))
        if building is not None:
            building.appendChild(buildingStorey)
        dict_id[fix_xml_stry(element.GlobalId)] = buildingStorey

        # Use the IFC Name (e.g. "Földszint", "Padlás") if available
        storey_display_name = (element.Name or '').strip() or ('Storey_%d' % storey_idx)
        storey_idx += 1
        name = root.createElement('Name')
        name.appendChild(root.createTextNode(storey_display_name))
        buildingStorey.appendChild(name)

        level = root.createElement('Level')
        level.appendChild(root.createTextNode(str(element.Elevation or 0)))
        buildingStorey.appendChild(level)

    # -- Space (IfcSpace) ----------------------------------------------------
    # space_heights: GlobalId → room height in metres (from BaseQuantities)
    # Used later to populate Surface/RectangularGeometry/Height
    space_heights = {}

    spaces = ifc_file.by_type('IfcSpace')
    space_idx = 1
    for s in spaces:
        room_number   = (s.Name or '').strip()
        room_function = (s.LongName or '').strip()

        space = root.createElement('Space')
        space.setAttribute('id', fix_xml_spc(s.GlobalId))
        # 'name' attribute: function name (LongName), fallback to room number
        space.setAttribute('name', room_function or room_number or ('Space_%d' % space_idx))
        if building is not None:
            building.appendChild(space)
        dict_id[fix_xml_spc(s.GlobalId)] = space
        if s.Decomposes:
            space.setAttribute('buildingStoreyIdRef', fix_xml_stry(s.Decomposes[0].RelatingObject.GlobalId))

        # Collect quantities from BaseQuantities (IfcElementQuantity)
        # and properties from IfcPropertySet
        bq = {}   # BaseQuantities: name → quantity object
        pset = {} # property sets: name → value
        for r in s.IsDefinedBy:
            if not r.is_a('IfcRelDefinesByProperties'):
                continue
            pdef = r.RelatingPropertyDefinition
            if pdef.is_a('IfcElementQuantity') and (pdef.Name or '') in ('BaseQuantities', 'ArchiCADQuantities'):
                for q in (pdef.Quantities or []):
                    bq[q.Name] = q
            elif pdef.is_a('IfcPropertySet'):
                for p in (pdef.HasProperties or []):
                    try:
                        pset[p.Name] = p.NominalValue.wrappedValue
                    except AttributeError:
                        pass

        # Area (GrossFloorArea preferred, fallback to NetFloorArea or pset)
        area_val = None
        for key in ('GrossFloorArea', 'NetFloorArea', 'GrossCeilingArea'):
            if key in bq:
                try:
                    area_val = bq[key].AreaValue
                    break
                except AttributeError:
                    pass
        if area_val is None:
            area_val = pset.get('Area')
        if area_val is not None:
            area_el = root.createElement('Area')
            area_el.appendChild(root.createTextNode(str(round(float(area_val), 4))))
            space.appendChild(area_el)

        # Height fields – written BEFORE Volume and Name so Winwatt's sequential
        # parser encounters them early.  Written in three ways for max compatibility:
        #   (a) 'height' XML attribute on <Space>
        #   (b) <Height> child element (Winwatt custom)
        #   (c) stored in space_heights dict → later used in Surface RectGeom
        height_fields = [
            ('Height',              'Height'),
            ('FinishCeilingHeight', 'CeilingHeight'),
            ('ClearHeight',         'ClearHeight'),
            ('FinishFloorHeight',   'FinishFloorHeight'),
        ]
        for bq_key, xml_tag in height_fields:
            if bq_key not in bq:
                continue
            try:
                h_val = bq[bq_key].LengthValue
            except AttributeError:
                continue
            if h_val is None:
                continue
            h_m = round(float(h_val) * length_scale, 4)
            if xml_tag == 'Height':
                space.setAttribute('height', str(h_m))   # (a)
                space_heights[s.GlobalId] = h_m           # (c)
            h_el = root.createElement(xml_tag)            # (b)
            h_el.appendChild(root.createTextNode(str(h_m)))
            space.appendChild(h_el)

        # Volume (written after Height so Winwatt reads Height first)
        vol_val = None
        for key in ('GrossVolume', 'NetVolume'):
            if key in bq:
                try:
                    vol_val = bq[key].VolumeValue
                    break
                except AttributeError:
                    pass
        if vol_val is None:
            vol_val = pset.get('Volume')
        if vol_val is not None:
            vol_el = root.createElement('Volume')
            vol_el.appendChild(root.createTextNode(str(round(float(vol_val), 4))))
            space.appendChild(vol_el)

        # Build display name
        if room_number and room_function:
            display_name = f'{room_number} {room_function}'
        elif room_function:
            display_name = room_function
        elif room_number:
            display_name = room_number
        else:
            display_name = 'Space_%d' % space_idx
        space_idx += 1

        name_el = root.createElement('Name')
        name_el.appendChild(root.createTextNode(display_name))
        space.appendChild(name_el)

        if room_function:
            desc = root.createElement('Description')
            desc.appendChild(root.createTextNode(room_function))
            space.appendChild(desc)

        # -- SpaceBoundary ---------------------------------------------------
        for element in s.BoundedBy:
            if element.RelatedBuildingElement is None:
                continue
            # Skip virtual boundaries – they have no physical surface geometry
            if element.PhysicalOrVirtualBoundary == 'VIRTUAL':
                continue

            boundaryGeom = element.ConnectionGeometry.SurfaceOnRelatingElement
            if boundaryGeom is None:
                continue

            if (element.RelatedBuildingElement.is_a('IfcCovering')
                    or element.RelatedBuildingElement.is_a('IfcSlab')
                    or element.RelatedBuildingElement.is_a('IfcWall')
                    or element.RelatedBuildingElement.is_a('IfcRoof')):

                vertices = get_boundary_vertices(boundaryGeom)
                if not vertices:
                    print(f"[WARN] Skipping SpaceBoundary {element.GlobalId}: no vertices extracted")
                    continue

                spaceBoundary = root.createElement('SpaceBoundary')
                spaceBoundary.setAttribute('isSecondLevelBoundary', 'true')
                spaceBoundary.setAttribute('surfaceIdRef', fix_xml_id(element.GlobalId))
                space.appendChild(spaceBoundary)

                planarGeometry = root.createElement('PlanarGeometry')
                spaceBoundary.appendChild(planarGeometry)

                polyLoop = root.createElement('PolyLoop')

                for v in vertices:
                    x, y, z = v
                    point = root.createElement('CartesianPoint')
                    for c in (x * length_scale, y * length_scale, z * length_scale):
                        coord = root.createElement('Coordinate')
                        coord.appendChild(root.createTextNode(str(round(c, 6))))
                        point.appendChild(coord)
                    polyLoop.appendChild(point)

                planarGeometry.appendChild(polyLoop)

    # -- Surface (IfcRelSpaceBoundary) ---------------------------------------
    # Deduplicate by building element GlobalId:
    #   Each physical element (IfcWall, IfcSlab…) must appear only ONCE as a
    #   <Surface>.  When the same element is encountered again (because it borders
    #   another space, or because a window splits the wall into fragments), we only
    #   add a new <AdjacentSpaceId> to the existing Surface instead of creating a
    #   duplicate.  This keeps the surface count in line with the actual floor plan.
    element_to_surface = {}   # element GlobalId → Surface XML node
    window_to_opening  = {}   # window GlobalId → Opening XML node (dedup)

    boundaries = ifc_file.by_type('IfcRelSpaceBoundary')
    opening_id = 1
    surface = None  # keep reference for Window openings appended below
    for element in boundaries:
        if element.RelatedBuildingElement is None:
            continue
        # Skip virtual boundaries – they carry no physical surface
        if element.PhysicalOrVirtualBoundary == 'VIRTUAL':
            continue
        if element.ConnectionGeometry is None:
            continue
        if element.ConnectionGeometry.SurfaceOnRelatingElement is None:
            continue

        surfaceGeom = element.ConnectionGeometry.SurfaceOnRelatingElement

        is_wall_or_slab = (
            element.RelatedBuildingElement.is_a('IfcCovering')
            or element.RelatedBuildingElement.is_a('IfcSlab')
            or element.RelatedBuildingElement.is_a('IfcWall')
            or element.RelatedBuildingElement.is_a('IfcRoof')
        )
        is_window = element.RelatedBuildingElement.is_a('IfcWindow')

        if not (is_wall_or_slab or is_window):
            continue

        # --- Deduplication for wall/slab/roof/covering elements ---------------
        if is_wall_or_slab:
            elem_gid = element.RelatedBuildingElement.GlobalId
            if elem_gid in element_to_surface:
                surface = element_to_surface[elem_gid]
                # Fix 6: upgrade InteriorWall → ExteriorWall if this boundary
                # is EXTERNAL (first encounter may have been from inside a room)
                if (element.RelatedBuildingElement.is_a('IfcWall')
                        and element.InternalOrExternalBoundary == 'EXTERNAL'
                        and surface.getAttribute('surfaceType') == 'InteriorWall'):
                    surface.setAttribute('surfaceType', 'ExteriorWall')
                # Register adjacent space if not already listed
                existing_refs = {
                    n.getAttribute('spaceIdRef')
                    for n in surface.getElementsByTagName('AdjacentSpaceId')
                }
                space_ref = fix_xml_spc(element.RelatingSpace.GlobalId)
                if space_ref not in existing_refs:
                    adj = root.createElement('AdjacentSpaceId')
                    adj.setAttribute('spaceIdRef', space_ref)
                    surface.appendChild(adj)
                continue  # no new Surface needed
        # ----------------------------------------------------------------------

        vertices = get_boundary_vertices(surfaceGeom)
        if not vertices:
            print(f"[WARN] Skipping boundary {element.GlobalId}: no vertices extracted")
            continue

        # Scale vertices to metres
        scaled_vertices = [(x * length_scale, y * length_scale, z * length_scale)
                           for (x, y, z) in vertices]

        if is_wall_or_slab:
            surface = root.createElement('Surface')
            surface.setAttribute('id', fix_xml_id(element.GlobalId))
            dict_id[fix_xml_id(element.GlobalId)] = surface

            if element.RelatedBuildingElement.is_a('IfcCovering'):
                surface.setAttribute('surfaceType', 'Ceiling')
            if element.RelatedBuildingElement.is_a('IfcSlab'):
                surface.setAttribute('surfaceType', 'InteriorFloor')
            if element.RelatedBuildingElement.is_a('IfcWall') and element.InternalOrExternalBoundary == 'EXTERNAL':
                surface.setAttribute('surfaceType', 'ExteriorWall')
            if element.RelatedBuildingElement.is_a('IfcWall') and element.InternalOrExternalBoundary == 'INTERNAL':
                surface.setAttribute('surfaceType', 'InteriorWall')
            if element.RelatedBuildingElement.is_a('IfcRoof'):
                surface.setAttribute('surfaceType', 'Roof')

            if element.RelatedBuildingElement.HasAssociations:
                surface.setAttribute('constructionIdRef',
                                     fix_xml_cons(element.RelatedBuildingElement.HasAssociations[0].GlobalId))

            name = root.createElement('Name')
            name.appendChild(root.createTextNode(fix_xml_name(element.GlobalId)))
            surface.appendChild(name)

            adjacentSpaceId = root.createElement('AdjacentSpaceId')
            adjacentSpaceId.setAttribute('spaceIdRef', fix_xml_spc(element.RelatingSpace.GlobalId))
            surface.appendChild(adjacentSpaceId)

            # RectangularGeometry: Azimuth + Tilt + Height + Width per surface.
            # Winwatt reads Azimuth/Tilt (confirmed working).
            # Height: only for vertical walls (tilt ≈ 90°) — Fix 4: ceilings/
            #         floors (tilt ≈ 0°/180°) must NOT get a room height value.
            # Width:  horizontal wall length, Fix 2: maps to Winwatt's x field.
            if len(scaled_vertices) >= 3:
                normal = _compute_surface_normal(scaled_vertices)
                azimuth, tilt = _normal_to_azimuth_tilt(normal)
                rectGeom = root.createElement('RectangularGeometry')

                az_el = root.createElement('Azimuth')
                az_el.appendChild(root.createTextNode(str(azimuth)))
                rectGeom.appendChild(az_el)

                tilt_el = root.createElement('Tilt')
                tilt_el.appendChild(root.createTextNode(str(tilt)))
                rectGeom.appendChild(tilt_el)

                is_vertical = abs(tilt - 90.0) < 45.0  # roughly a wall
                if is_vertical:
                    # Fix 4: Height only for vertical (wall) surfaces
                    space_gid = element.RelatingSpace.GlobalId
                    if space_gid in space_heights:
                        surf_h = space_heights[space_gid]
                    else:
                        zs = [v[2] for v in scaled_vertices]
                        surf_h = round(max(zs) - min(zs), 4) if zs else 0.0
                    h_el = root.createElement('Height')
                    h_el.appendChild(root.createTextNode(str(surf_h)))
                    rectGeom.appendChild(h_el)

                    # Fix 2: Width = horizontal wall length (Winwatt x field)
                    wall_w = _compute_surface_width(scaled_vertices, normal)
                    w_el = root.createElement('Width')
                    w_el.appendChild(root.createTextNode(str(wall_w)))
                    rectGeom.appendChild(w_el)

                surface.appendChild(rectGeom)

            planarGeometry = root.createElement('PlanarGeometry')
            surface.appendChild(planarGeometry)

            polyLoop = root.createElement('PolyLoop')
            for (x, y, z) in scaled_vertices:
                point = root.createElement('CartesianPoint')
                for c in (x, y, z):
                    coord = root.createElement('Coordinate')
                    coord.appendChild(root.createTextNode(str(round(c, 6))))
                    point.appendChild(coord)
                polyLoop.appendChild(point)
            planarGeometry.appendChild(polyLoop)

            objectId = root.createElement('CADObjectId')
            objectId.appendChild(root.createTextNode(fix_xml_name(element.GlobalId)))
            surface.appendChild(objectId)

            campus.appendChild(surface)
            # Register so duplicate boundaries don't create extra surfaces
            element_to_surface[element.RelatedBuildingElement.GlobalId] = surface

        if is_window and surface is not None:
            win_gid = element.RelatedBuildingElement.GlobalId
            # Fix 3: deduplicate windows – one Opening per IfcWindow element
            if win_gid in window_to_opening:
                continue

            opening = root.createElement('Opening')
            opening.setAttribute('windowTypeIdRef', fix_xml_id(win_gid))
            opening.setAttribute('openingType', 'OperableWindow')
            opening.setAttribute('id', 'Opening%d' % opening_id)
            opening_id += 1
            window_to_opening[win_gid] = opening

            # Fix 5: window dimensions from IFC OverallWidth / OverallHeight
            win_el = element.RelatedBuildingElement
            try:
                win_h = getattr(win_el, 'OverallHeight', None)
                win_w = getattr(win_el, 'OverallWidth', None)
                if win_h is not None and win_w is not None:
                    win_h_m = round(float(win_h) * length_scale, 4)
                    win_w_m = round(float(win_w) * length_scale, 4)
                    winRectGeom = root.createElement('RectangularGeometry')
                    wh = root.createElement('Height')
                    wh.appendChild(root.createTextNode(str(win_h_m)))
                    winRectGeom.appendChild(wh)
                    ww = root.createElement('Width')
                    ww.appendChild(root.createTextNode(str(win_w_m)))
                    winRectGeom.appendChild(ww)
                    opening.appendChild(winRectGeom)
            except Exception:
                pass

            planarGeometry = root.createElement('PlanarGeometry')
            opening.appendChild(planarGeometry)

            polyLoop = root.createElement('PolyLoop')
            for (x, y, z) in scaled_vertices:
                point = root.createElement('CartesianPoint')
                for c in (x, y, z):
                    coord = root.createElement('Coordinate')
                    coord.appendChild(root.createTextNode(str(round(c, 6))))
                    point.appendChild(coord)
                polyLoop.appendChild(point)
            planarGeometry.appendChild(polyLoop)

            name = root.createElement('Name')
            name.appendChild(root.createTextNode(fix_xml_name(win_el.Name or win_gid)))
            opening.appendChild(name)

            objectId = root.createElement('CADObjectId')
            objectId.appendChild(root.createTextNode(fix_xml_name(win_el.Name or win_gid)))
            opening.appendChild(objectId)

            surface.appendChild(opening)

    # -- WindowType (IfcWindow) ----------------------------------------------
    windows = ifc_file.by_type('IfcWindow')
    for element in windows:
        window = root.createElement('WindowType')
        window.setAttribute('id', fix_xml_id(element.GlobalId))
        gbxml.appendChild(window)
        dict_id[fix_xml_id(element.GlobalId)] = window

        name = root.createElement('Name')
        name.appendChild(root.createTextNode(fix_xml_name(element.Name)))
        window.appendChild(name)

        description = root.createElement('Description')
        description.appendChild(root.createTextNode(fix_xml_name(element.Name)))
        window.appendChild(description)

        analyticValue = element.IsDefinedBy
        u_value = root.createElement('U-value')
        for r in analyticValue:
            if r.is_a('IfcRelDefinesByProperties'):
                if r.RelatingPropertyDefinition.is_a('IfcPropertySet'):
                    for p in r.RelatingPropertyDefinition.HasProperties:
                        if p.Name == 'ThermalTransmittance':
                            u_value.setAttribute('unit', 'WPerSquareMeterK')
                            u_value.appendChild(root.createTextNode(str(p.NominalValue.wrappedValue)))
                            window.appendChild(u_value)

        solarHeat = root.createElement('SolarHeatGainCoeff')
        visualLight = root.createElement('Transmittance')
        for r in analyticValue:
            if r.is_a('IfcRelDefinesByType'):
                if r.RelatingType and r.RelatingType.is_a('IfcWindowStyle'):
                    if r.RelatingType.HasPropertySets:
                        for p in r.RelatingType.HasPropertySets:
                            if p.Name == 'Analytical Properties(Type)':
                                for t in (p.HasProperties or []):
                                    if t.Name == 'Solar Heat Gain Coefficient':
                                        solarHeat.setAttribute('unit', 'Fraction')
                                        solarHeat.appendChild(root.createTextNode(str(t.NominalValue.wrappedValue)))
                                        window.appendChild(solarHeat)
                                    if t.Name == 'Visual Light Transmittance':
                                        visualLight.setAttribute('unit', 'Fraction')
                                        visualLight.setAttribute('type', 'Visible')
                                        visualLight.appendChild(root.createTextNode(str(t.NominalValue.wrappedValue)))
                                        window.appendChild(visualLight)

    # -- Construction (IfcRelSpaceBoundary -> material associations) ----------
    listCon = []
    for element in boundaries:
        if element.RelatedBuildingElement is None:
            continue
        if not (element.RelatedBuildingElement.is_a('IfcCovering')
                or element.RelatedBuildingElement.is_a('IfcSlab')
                or element.RelatedBuildingElement.is_a('IfcWall')
                or element.RelatedBuildingElement.is_a('IfcRoof')):
            continue
        if not element.RelatedBuildingElement.HasAssociations:
            continue

        constructions = element.RelatedBuildingElement.HasAssociations[0].GlobalId
        if constructions in listCon:
            continue
        listCon.append(constructions)

        construction = root.createElement('Construction')
        construction.setAttribute('id', fix_xml_cons(element.RelatedBuildingElement.HasAssociations[0].GlobalId))
        dict_id[fix_xml_cons(element.RelatedBuildingElement.HasAssociations[0].GlobalId)] = construction

        analyticValue = element.RelatedBuildingElement.IsDefinedBy or []
        u_value = root.createElement('U-value')
        for r in analyticValue:
            if r.is_a('IfcRelDefinesByProperties'):
                if r.RelatingPropertyDefinition.is_a('IfcPropertySet'):
                    for p in (r.RelatingPropertyDefinition.HasProperties or []):
                        if element.RelatedBuildingElement.is_a('IfcWall') and p.Name == 'ThermalTransmittance':
                            u_value.setAttribute('unit', 'WPerSquareMeterK')
                            u_value.appendChild(root.createTextNode(str(p.NominalValue.wrappedValue)))
                            construction.appendChild(u_value)
                        if p.Name == 'Heat Transfer Coefficient (U)':
                            u_value.setAttribute('unit', 'WPerSquareMeterK')
                            u_value.appendChild(root.createTextNode(str(p.NominalValue.wrappedValue)))
                            construction.appendChild(u_value)

        absorptance = root.createElement('Absorptance')
        for r in analyticValue:
            if r.is_a('IfcRelDefinesByProperties'):
                if r.RelatingPropertyDefinition.is_a('IfcPropertySet'):
                    for p in (r.RelatingPropertyDefinition.HasProperties or []):
                        if p.Name == 'Absorptance':
                            absorptance.setAttribute('unit', 'Fraction')
                            absorptance.setAttribute('type', 'ExtIR')
                            absorptance.appendChild(root.createTextNode(str(p.NominalValue.wrappedValue)))
                            construction.appendChild(absorptance)

        layerId = fix_xml_layer(element.RelatedBuildingElement.HasAssociations[0].GlobalId)
        layer_id = root.createElement('LayerId')
        layer_id.setAttribute('layerIdRef', layerId)
        construction.appendChild(layer_id)

        # Try to get the layer set name from the material association
        try:
            layer_set_name = element.RelatedBuildingElement.HasAssociations[0].RelatingMaterial.ForLayerSet.LayerSetName
        except (AttributeError, TypeError):
            layer_set_name = 'Unknown'
        name = root.createElement('Name')
        name.appendChild(root.createTextNode(layer_set_name or 'Unknown'))
        construction.appendChild(name)

        gbxml.appendChild(construction)

    # -- Layer (IfcBuildingElement) -------------------------------------------
    buildingElements = ifc_file.by_type('IfcBuildingElement')
    for element in buildingElements:
        if not (element.is_a('IfcWall') or element.is_a('IfcCovering')
                or element.is_a('IfcSlab') or element.is_a('IfcRoof')):
            continue
        if element.IsDecomposedBy:
            continue
        if not element.HasAssociations:
            continue

        layerId = fix_xml_layer(element.HasAssociations[0].GlobalId)
        layer = root.createElement('Layer')
        layer.setAttribute('id', layerId)
        dict_id[layerId] = layer

        try:
            if not element.HasAssociations[0].RelatingMaterial.is_a('IfcMaterialLayerSetUsage'):
                continue
            materials = element.HasAssociations[0].RelatingMaterial.ForLayerSet.MaterialLayers
        except (AttributeError, TypeError):
            continue

        for layer_item in (materials or []):
            if layer_item.Material is None:
                continue
            material_id = root.createElement('MaterialId')
            material_id.setAttribute('materialIdRef', 'mat_%d' % layer_item.Material.id())
            layer.appendChild(material_id)
            dict_id['mat_%d' % layer_item.Material.id()] = layer
            gbxml.appendChild(layer)

    # -- Material (IfcBuildingElement -> IfcMaterialLayer) --------------------
    listMat = []
    for element in buildingElements:
        if not (element.is_a('IfcWall') or element.is_a('IfcSlab')
                or element.is_a('IfcCovering') or element.is_a('IfcRoof')):
            continue
        if element.IsDecomposedBy:
            continue
        if not element.HasAssociations:
            continue

        try:
            if not element.HasAssociations[0].RelatingMaterial.is_a('IfcMaterialLayerSetUsage'):
                continue
            materials = element.HasAssociations[0].RelatingMaterial.ForLayerSet.MaterialLayers
        except (AttributeError, TypeError):
            continue

        for layer_item in (materials or []):
            if layer_item.Material is None:
                continue
            item = layer_item.Material.id()
            if item in listMat:
                continue
            listMat.append(item)

            material = root.createElement('Material')
            material.setAttribute('id', 'mat_%d' % layer_item.Material.id())
            dict_id['mat_%d' % layer_item.Material.id()] = material

            name = root.createElement('Name')
            name.appendChild(root.createTextNode(layer_item.Material.Name or 'Unknown'))
            material.appendChild(name)

            thickness = root.createElement('Thickness')
            thickness.setAttribute('unit', 'Meters')
            valueT = layer_item.LayerThickness or 0
            thickness.appendChild(root.createTextNode(str(valueT)))
            material.appendChild(thickness)

            rValue = root.createElement('R-value')
            rValue.setAttribute('unit', 'SquareMeterKPerW')

            # Direct material properties (Pset_MaterialEnergy)
            for material_property in (getattr(layer_item.Material, 'HasProperties', None) or []):
                if material_property.Name == 'Pset_MaterialEnergy':
                    for pset in (material_property.Properties or []):
                        if pset.Name == 'ThermalConductivityTemperatureDerivative':
                            rValue.appendChild(root.createTextNode(str(pset.NominalValue.wrappedValue)))
                            material.appendChild(rValue)
                            gbxml.appendChild(material)

            # Analytical properties via type or property sets
            for r in (element.IsDefinedBy or []):
                if (r.is_a('IfcRelDefinesByType')
                        and r.RelatingType is not None
                        and r.RelatingType.is_a('IfcWallType')
                        and r.RelatingType.HasPropertySets):
                    for p in r.RelatingType.HasPropertySets:
                        if p.Name == 'Analytical Properties(Type)':
                            for t in (p.HasProperties or []):
                                if t.Name == 'Heat Transfer Coefficient (U)' and t.NominalValue and t.NominalValue.wrappedValue:
                                    valueR = valueT / t.NominalValue.wrappedValue
                                    rValue.appendChild(root.createTextNode(str(valueR)))
                                    material.appendChild(rValue)
                                    gbxml.appendChild(material)

                if r.is_a('IfcRelDefinesByProperties'):
                    if r.RelatingPropertyDefinition.is_a('IfcPropertySet'):
                        for p in (r.RelatingPropertyDefinition.HasProperties or []):
                            if p.Name == 'Heat Transfer Coefficient (U)' and p.NominalValue and p.NominalValue.wrappedValue:
                                valueR = valueT / p.NominalValue.wrappedValue
                                rValue.setAttribute('unit', 'SquareMeterKPerW')
                                rValue.appendChild(root.createTextNode(str(valueR)))
                                material.appendChild(rValue)
                                gbxml.appendChild(material)

                if (element.is_a('IfcCovering')
                        and r.is_a('IfcRelDefinesByProperties')
                        and hasattr(r, 'RelatingType')
                        and r.RelatingType is not None
                        and r.RelatingType.is_a('IfcPropertySet')
                        and r.RelatingType.HasPropertySets):
                    for p in r.RelatingType.HasPropertySets:
                        if p.Name == 'Analytical Properties(Type)':
                            for t in (p.HasProperties or []):
                                if t.Name == 'Heat Transfer Coefficient (U)' and t.NominalValue and t.NominalValue.wrappedValue:
                                    valueR = valueT / t.NominalValue.wrappedValue
                                    rValue.setAttribute('unit', 'SquareMeterKPerW')
                                    rValue.appendChild(root.createTextNode(str(valueR)))
                                    material.appendChild(rValue)
                                    gbxml.appendChild(material)

    # -- DocumentHistory -----------------------------------------------------
    programInfo = ifc_file.by_type('IfcApplication')
    docHistory = root.createElement('DocumentHistory')
    for element in programInfo:
        program = root.createElement('ProgramInfo')
        program.setAttribute('id', element.ApplicationIdentifier)
        docHistory.appendChild(program)

        company = root.createElement('CompanyName')
        company.appendChild(root.createTextNode(element.ApplicationDeveloper.Name))
        program.appendChild(company)

        product = root.createElement('ProductName')
        product.appendChild(root.createTextNode(element.ApplicationFullName))
        program.appendChild(product)

        version = root.createElement('Version')
        version.appendChild(root.createTextNode(element.Version))
        program.appendChild(version)

    personInfo = ifc_file.by_type('IfcPerson')
    for element in personInfo:
        created = root.createElement('CreatedBy')
        created.setAttribute('personId', element.GivenName)

    for element in programInfo:
        created.setAttribute('programId', element.ApplicationIdentifier)
        today = datetime.date.today()
        created.setAttribute('date', today.strftime('%Y-%m-%dT') + time.strftime('%H:%M:%S'))
        docHistory.appendChild(created)

    for element in personInfo:
        person = root.createElement('PersonInfo')
        person.setAttribute('id', element.GivenName)
        docHistory.appendChild(person)

    gbxml.appendChild(docHistory)

    # -- Write output --------------------------------------------------------
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        root.writexml(f, indent="  ", addindent="  ", newl='\n')

    print(f"[OK] gbXML written to: {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Convert an IFC file to gbXML format.",
        epilog="Example:  python IFC_gbXML_Convert.py model.ifc",
    )
    parser.add_argument(
        "ifc_file",
        type=str,
        help="Path to the input .ifc file",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=str,
        default="output",
        help="Directory for the output gbXML file (default: ./output)",
    )
    args = parser.parse_args()

    ifc_path = Path(args.ifc_file).resolve()
    if not ifc_path.exists():
        print(f"[ERROR] IFC file not found: {ifc_path}", file=sys.stderr)
        sys.exit(1)
    if not ifc_path.suffix.lower() == '.ifc':
        print(f"[WARNING] File does not have .ifc extension: {ifc_path}", file=sys.stderr)

    # Output: output/<input_stem>_gbXML.xml
    output_dir = Path(args.output_dir).resolve()
    output_filename = f"{ifc_path.stem}_gbXML.xml"
    output_path = output_dir / output_filename

    print(f"{'=' * 60}")
    print(f"  IFC to gbXML Converter")
    print(f"{'=' * 60}")
    print(f"  Input:  {ifc_path}")
    print(f"  Output: {output_path}")
    print(f"{'=' * 60}")

    convert(ifc_path, output_path)


if __name__ == "__main__":
    main()
