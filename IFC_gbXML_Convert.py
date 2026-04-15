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

        # Volume (GrossVolume preferred, fallback NetVolume)
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

        # Build display name: room number + function (for <Name> child element)
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

        # Description = function name
        if room_function:
            desc = root.createElement('Description')
            desc.appendChild(root.createTextNode(room_function))
            space.appendChild(desc)

        # Height fields – tag names matched to Winwatt's expected gbXML import format.
        # Winwatt uses <Height> for room height and <CeilingHeight> for false ceiling.
        # When <Height> is present, Winwatt calculates Volume = Area × Height automatically.
        # IFC BaseQuantities mapping:
        #   Height              → <Height>         (Belmagasság)
        #   FinishCeilingHeight → <CeilingHeight>  (Álmennyezetmagasság)
        #   ClearHeight         → <ClearHeight>    (Szabad belmagasság, extra info)
        #   FinishFloorHeight   → <FinishFloorHeight> (Padlóburkolat szintje, extra info)
        height_fields = [
            ('Height',              'Height',            'Meters'),
            ('FinishCeilingHeight', 'CeilingHeight',     'Meters'),
            ('ClearHeight',         'ClearHeight',       'Meters'),
            ('FinishFloorHeight',   'FinishFloorHeight', 'Meters'),
        ]
        for bq_key, xml_tag, unit in height_fields:
            if bq_key not in bq:
                continue
            try:
                h_val = bq[bq_key].LengthValue
            except AttributeError:
                continue
            if h_val is None:
                continue
            h_el = root.createElement(xml_tag)
            h_el.setAttribute('unit', unit)
            h_el.appendChild(root.createTextNode(str(round(float(h_val), 4))))
            space.appendChild(h_el)

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
                    for c in (x, y, z):
                        coord = root.createElement('Coordinate')
                        coord.appendChild(root.createTextNode(str(c)))
                        point.appendChild(coord)
                    polyLoop.appendChild(point)

                planarGeometry.appendChild(polyLoop)

    # -- Surface (IfcRelSpaceBoundary) ---------------------------------------
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

        vertices = get_boundary_vertices(surfaceGeom)
        if not vertices:
            print(f"[WARN] Skipping boundary {element.GlobalId}: no vertices extracted")
            continue

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

            planarGeometry = root.createElement('PlanarGeometry')
            surface.appendChild(planarGeometry)

            polyLoop = root.createElement('PolyLoop')
            for v in vertices:
                x, y, z = v
                point = root.createElement('CartesianPoint')
                for c in (x, y, z):
                    coord = root.createElement('Coordinate')
                    coord.appendChild(root.createTextNode(str(c)))
                    point.appendChild(coord)
                polyLoop.appendChild(point)
            planarGeometry.appendChild(polyLoop)

            objectId = root.createElement('CADObjectId')
            objectId.appendChild(root.createTextNode(fix_xml_name(element.GlobalId)))
            surface.appendChild(objectId)

            campus.appendChild(surface)

        if is_window and surface is not None:
            opening = root.createElement('Opening')
            opening.setAttribute('windowTypeIdRef', fix_xml_id(element.RelatedBuildingElement.GlobalId))
            opening.setAttribute('openingType', 'OperableWindow')
            opening.setAttribute('id', 'Opening%d' % opening_id)
            opening_id += 1

            planarGeometry = root.createElement('PlanarGeometry')
            opening.appendChild(planarGeometry)

            polyLoop = root.createElement('PolyLoop')
            for v in vertices:
                x, y, z = v
                point = root.createElement('CartesianPoint')
                for c in (x, y, z):
                    coord = root.createElement('Coordinate')
                    coord.appendChild(root.createTextNode(str(c)))
                    point.appendChild(coord)
                polyLoop.appendChild(point)
            planarGeometry.appendChild(polyLoop)

            name = root.createElement('Name')
            name.appendChild(root.createTextNode(fix_xml_name(element.RelatedBuildingElement.Name)))
            opening.appendChild(name)

            objectId = root.createElement('CADObjectId')
            objectId.appendChild(root.createTextNode(fix_xml_name(element.RelatedBuildingElement.Name)))
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
