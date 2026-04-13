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
    - pythonocc-core (conda install -c conda-forge pythonocc-core)
    - The IFC file must contain 2nd level space boundaries (IfcRelSpaceBoundary).

Author: Maarten Visschers (original), refactored for CLI usage.
"""

import argparse
import datetime
import os
import sys
import time
from pathlib import Path
from xml.dom import minidom


def _load_geometry_libs():
    """Lazy-load heavy geometry dependencies so --help works without them."""
    import ifcopenshell.geom
    import OCC.Core.BRep
    import OCC.Core.BRepTools
    import OCC.Core.ProjLib
    import OCC.Core.TopAbs
    import OCC.Core.TopExp
    import OCC.Core.TopoDS
    return ifcopenshell, OCC


# Module-level references (populated by convert())
ifcopenshell = None
OCC = None

# ---------------------------------------------------------------------------
# Geometry helpers – convert implicit IFC geometry to explicit coordinates
# ---------------------------------------------------------------------------
FACE = WIRE = EDGE = VERTEX = None
_TOPO_CAST = {}


def _init_geometry():
    """Initialise geometry constants after imports are loaded."""
    global FACE, WIRE, EDGE, VERTEX, _TOPO_CAST, ifcopenshell, OCC
    ifcopenshell, OCC = _load_geometry_libs()
    FACE = OCC.Core.TopAbs.TopAbs_FACE
    WIRE = OCC.Core.TopAbs.TopAbs_WIRE
    EDGE = OCC.Core.TopAbs.TopAbs_EDGE
    VERTEX = OCC.Core.TopAbs.TopAbs_VERTEX
    _TOPO_CAST.update({
        FACE: OCC.Core.TopoDS.topods_Face,
        WIRE: OCC.Core.TopoDS.topods_Wire,
        EDGE: OCC.Core.TopoDS.topods_Edge,
        VERTEX: OCC.Core.TopoDS.topods_Vertex,
    })


def sub(shape, ty):
    """Iterate over sub-shapes of a given topology type."""
    cast = _TOPO_CAST[ty]
    exp = OCC.Core.TopExp.TopExp_Explorer(shape, ty)
    while exp.More():
        yield cast(exp.Current())
        exp.Next()


def ring(wire, face):
    """Return a list of (x, y, z) tuples for vertices along a wire."""
    def vertices():
        exp = OCC.Core.BRepTools.BRepTools_WireExplorer(wire, face)
        while exp.More():
            yield exp.CurrentVertex()
            exp.Next()
        yield exp.CurrentVertex()

    return [
        (p.X(), p.Y(), p.Z())
        for p in map(OCC.Core.BRep.BRep_Tool.Pnt, vertices())
    ]


def get_vertices(shape):
    """Extract the first set of face vertices from a shape."""
    for face in sub(shape, FACE):
        for idx, wire in enumerate(sub(face, WIRE)):
            verts = ring(wire, face)
            if idx > 0:
                verts.reverse()
            return verts
    return []


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

    # Load heavy geometry libraries on first use
    _init_geometry()

    # IfcOpenShell + OpenCASCADE settings
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_PYTHON_OPENCASCADE, True)

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
    for element in site:
        campus = root.createElement('Campus')
        campus.setAttribute('id', fix_xml_cmps(element.GlobalId))
        gbxml.appendChild(campus)
        dict_id[fix_xml_cmps(element.GlobalId)] = campus

        location = root.createElement('Location')
        campus.appendChild(location)

        longitude = root.createElement('Longitude')
        longitude.appendChild(root.createTextNode(str(element.RefLongitude[0])))
        location.appendChild(longitude)

        latitude = root.createElement('Latitude')
        latitude.appendChild(root.createTextNode(str(element.RefLatitude[0])))
        location.appendChild(latitude)

        elevation = root.createElement('Elevation')
        elevation.appendChild(root.createTextNode(str(element.RefElevation)))
        location.appendChild(elevation)

    address = ifc_file.by_type('IfcPostalAddress')
    for element in address:
        zipcode = root.createElement('ZipcodeOrPostalCode')
        zipcode.appendChild(root.createTextNode(element.PostalCode))
        location.appendChild(zipcode)

        name = root.createElement('Name')
        name.appendChild(root.createTextNode(element.Region + ', ' + element.Country))
        location.appendChild(name)

    # -- Building (IfcBuilding) ----------------------------------------------
    buildings = ifc_file.by_type('IfcBuilding')
    for element in buildings:
        building = root.createElement('Building')
        building.setAttribute('id', fix_xml_bldng(element.GlobalId))
        building.setAttribute('buildingType', 'Unknown')
        campus.appendChild(building)
        dict_id[fix_xml_bldng(element.GlobalId)] = building

    for element in address:
        streetAddress = root.createElement('StreetAddress')
        streetAddress.appendChild(root.createTextNode(element.Region + ', ' + element.Country))
        building.appendChild(streetAddress)

    # -- BuildingStorey (IfcBuildingStorey) -----------------------------------
    storeys = ifc_file.by_type('IfcBuildingStorey')
    storey_name = 1
    for element in storeys:
        buildingStorey = root.createElement('BuildingStorey')
        buildingStorey.setAttribute('id', fix_xml_stry(element.GlobalId))
        building.appendChild(buildingStorey)
        dict_id[fix_xml_stry(element.GlobalId)] = buildingStorey

        name = root.createElement('Name')
        name.appendChild(root.createTextNode('Storey_%d' % storey_name))
        storey_name += 1
        buildingStorey.appendChild(name)

        level = root.createElement('Level')
        level.appendChild(root.createTextNode(str(element.Elevation)))
        buildingStorey.appendChild(level)

    # -- Space (IfcSpace) ----------------------------------------------------
    spaces = ifc_file.by_type('IfcSpace')
    space_name = 1
    for s in spaces:
        space = root.createElement('Space')
        space.setAttribute('id', fix_xml_spc(s.GlobalId))
        building.appendChild(space)
        dict_id[fix_xml_spc(s.GlobalId)] = space
        space.setAttribute('buildingStoreyIdRef', fix_xml_stry(s.Decomposes[0].RelatingObject.GlobalId))

        area = root.createElement('Area')
        volume = root.createElement('Volume')

        for r in s.IsDefinedBy:
            if r.is_a('IfcRelDefinesByProperties'):
                if r.RelatingPropertyDefinition.is_a('IfcPropertySet'):
                    for p in r.RelatingPropertyDefinition.HasProperties:
                        if p.Name == 'Area':
                            area.appendChild(root.createTextNode(str(p.NominalValue.wrappedValue)))
                            space.appendChild(area)
                        if p.Name == 'Volume':
                            volume.appendChild(root.createTextNode(str(p.NominalValue.wrappedValue)))
                            space.appendChild(volume)

        name = root.createElement('Name')
        name.appendChild(root.createTextNode('Space_%d' % space_name))
        space_name += 1
        space.appendChild(name)

        # -- SpaceBoundary ---------------------------------------------------
        for element in s.BoundedBy:
            if element.RelatedBuildingElement is None:
                continue

            boundaryGeom = element.ConnectionGeometry.SurfaceOnRelatingElement
            if boundaryGeom.is_a('IfcCurveBoundedPlane') and boundaryGeom.InnerBoundaries is None:
                boundaryGeom.InnerBoundaries = ()

            space_boundary_shape = ifcopenshell.geom.create_shape(settings, boundaryGeom)

            if (element.RelatedBuildingElement.is_a('IfcCovering')
                    or element.RelatedBuildingElement.is_a('IfcSlab')
                    or element.RelatedBuildingElement.is_a('IfcWall')
                    or element.RelatedBuildingElement.is_a('IfcRoof')):

                spaceBoundary = root.createElement('SpaceBoundary')
                spaceBoundary.setAttribute('isSecondLevelBoundary', 'true')
                spaceBoundary.setAttribute('surfaceIdRef', fix_xml_id(element.GlobalId))
                space.appendChild(spaceBoundary)

                planarGeometry = root.createElement('PlanarGeometry')
                spaceBoundary.appendChild(planarGeometry)

                new_z = element.RelatingSpace.ObjectPlacement.PlacementRelTo.RelativePlacement.Location.Coordinates[2]
                polyLoop = root.createElement('PolyLoop')

                for v in get_vertices(space_boundary_shape):
                    x, y, z = v
                    z += new_z
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
    for element in boundaries:
        if element.RelatedBuildingElement is None:
            continue
        if element.ConnectionGeometry.SurfaceOnRelatingElement is None:
            continue

        surfaceGeom = element.ConnectionGeometry.SurfaceOnRelatingElement
        if surfaceGeom.is_a('IfcCurveBoundedPlane') and surfaceGeom.InnerBoundaries is None:
            surfaceGeom.InnerBoundaries = ()

        space_boundary_shape = ifcopenshell.geom.create_shape(settings, surfaceGeom)

        if (element.RelatedBuildingElement.is_a('IfcCovering')
                or element.RelatedBuildingElement.is_a('IfcSlab')
                or element.RelatedBuildingElement.is_a('IfcWall')
                or element.RelatedBuildingElement.is_a('IfcRoof')):

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

            new_z = element.RelatingSpace.ObjectPlacement.PlacementRelTo.RelativePlacement.Location.Coordinates[2]
            polyLoop = root.createElement('PolyLoop')

            for v in get_vertices(space_boundary_shape):
                x, y, z = v
                z += new_z
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

        if element.RelatedBuildingElement.is_a('IfcWindow'):
            opening = root.createElement('Opening')
            opening.setAttribute('windowTypeIdRef', fix_xml_id(element.RelatedBuildingElement.GlobalId))
            opening.setAttribute('openingType', 'OperableWindow')
            opening.setAttribute('id', 'Opening%d' % opening_id)
            opening_id += 1

            planarGeometry = root.createElement('PlanarGeometry')
            opening.appendChild(planarGeometry)

            new_z = element.RelatingSpace.ObjectPlacement.PlacementRelTo.RelativePlacement.Location.Coordinates[2]
            polyLoop = root.createElement('PolyLoop')

            for v in get_vertices(space_boundary_shape):
                x, y, z = v
                z += new_z
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
                if r.RelatingType.is_a('IfcWindowStyle'):
                    for p in r.RelatingType.HasPropertySets:
                        if p.Name == 'Analytical Properties(Type)':
                            for t in p.HasProperties:
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

        constructions = element.RelatedBuildingElement.HasAssociations[0].GlobalId
        if constructions in listCon:
            continue
        listCon.append(constructions)

        construction = root.createElement('Construction')
        construction.setAttribute('id', fix_xml_cons(element.RelatedBuildingElement.HasAssociations[0].GlobalId))
        dict_id[fix_xml_cons(element.RelatedBuildingElement.HasAssociations[0].GlobalId)] = construction

        analyticValue = element.RelatedBuildingElement.IsDefinedBy
        u_value = root.createElement('U-value')
        for r in analyticValue:
            if r.is_a('IfcRelDefinesByProperties'):
                if r.RelatingPropertyDefinition.is_a('IfcPropertySet'):
                    for p in r.RelatingPropertyDefinition.HasProperties:
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
                    for p in r.RelatingPropertyDefinition.HasProperties:
                        if p.Name == 'Absorptance':
                            absorptance.setAttribute('unit', 'Fraction')
                            absorptance.setAttribute('type', 'ExtIR')
                            absorptance.appendChild(root.createTextNode(str(p.NominalValue.wrappedValue)))
                            construction.appendChild(absorptance)

        layerId = fix_xml_layer(element.RelatedBuildingElement.HasAssociations[0].GlobalId)
        layer_id = root.createElement('LayerId')
        layer_id.setAttribute('layerIdRef', layerId)
        construction.appendChild(layer_id)

        name = root.createElement('Name')
        name.appendChild(root.createTextNode(
            element.RelatedBuildingElement.HasAssociations[0].RelatingMaterial.ForLayerSet.LayerSetName))
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

        layerId = fix_xml_layer(element.HasAssociations[0].GlobalId)
        layer = root.createElement('Layer')
        layer.setAttribute('id', layerId)
        dict_id[layerId] = layer

        if not element.HasAssociations[0].RelatingMaterial.is_a('IfcMaterialLayerSetUsage'):
            continue

        materials = element.HasAssociations[0].RelatingMaterial.ForLayerSet.MaterialLayers
        for l in materials:
            material_id = root.createElement('MaterialId')
            material_id.setAttribute('materialIdRef', 'mat_%d' % l.Material.id())
            layer.appendChild(material_id)
            dict_id['mat_%d' % l.Material.id()] = layer
            gbxml.appendChild(layer)

    # -- Material (IfcBuildingElement -> IfcMaterialLayer) --------------------
    listMat = []
    for element in buildingElements:
        if not (element.is_a('IfcWall') or element.is_a('IfcSlab')
                or element.is_a('IfcCovering') or element.is_a('IfcRoof')):
            continue
        if element.IsDecomposedBy:
            continue
        if not element.HasAssociations[0].RelatingMaterial.is_a('IfcMaterialLayerSetUsage'):
            continue

        materials = element.HasAssociations[0].RelatingMaterial.ForLayerSet.MaterialLayers
        for l in materials:
            item = l.Material.id()
            if item in listMat:
                continue
            listMat.append(item)

            material = root.createElement('Material')
            material.setAttribute('id', 'mat_%d' % l.Material.id())
            dict_id['mat_%d' % l.Material.id()] = material

            name = root.createElement('Name')
            name.appendChild(root.createTextNode(l.Material.Name))
            material.appendChild(name)

            thickness = root.createElement('Thickness')
            thickness.setAttribute('unit', 'Meters')
            valueT = l.LayerThickness
            thickness.appendChild(root.createTextNode(str(valueT)))
            material.appendChild(thickness)

            rValue = root.createElement('R-value')
            rValue.setAttribute('unit', 'SquareMeterKPerW')

            # Direct material properties (Pset_MaterialEnergy)
            for material_property in l.Material.HasProperties:
                if material_property.Name == 'Pset_MaterialEnergy':
                    for pset in material_property.Properties:
                        if pset.Name == 'ThermalConductivityTemperatureDerivative':
                            rValue.appendChild(root.createTextNode(str(pset.NominalValue.wrappedValue)))
                            material.appendChild(rValue)
                            gbxml.appendChild(material)

            # Analytical properties via type or property sets
            for r in element.IsDefinedBy:
                if r.is_a('IfcRelDefinesByType') and r.RelatingType.is_a('IfcWallType'):
                    for p in r.RelatingType.HasPropertySets:
                        if p.Name == 'Analytical Properties(Type)':
                            for t in p.HasProperties:
                                if t.Name == 'Heat Transfer Coefficient (U)':
                                    valueR = valueT / t.NominalValue.wrappedValue
                                    rValue.appendChild(root.createTextNode(str(valueR)))
                                    material.appendChild(rValue)
                                    gbxml.appendChild(material)

                if r.is_a('IfcRelDefinesByProperties'):
                    if r.RelatingPropertyDefinition.is_a('IfcPropertySet'):
                        for p in r.RelatingPropertyDefinition.HasProperties:
                            if p.Name == 'Heat Transfer Coefficient (U)':
                                valueR = valueT / p.NominalValue.wrappedValue
                                rValue.setAttribute('unit', 'SquareMeterKPerW')
                                rValue.appendChild(root.createTextNode(str(valueR)))
                                material.appendChild(rValue)
                                gbxml.appendChild(material)

                if element.is_a('IfcCovering') and r.is_a('IfcRelDefinesByProperties'):
                    if r.RelatingType.is_a('IfcPropertySet'):
                        for p in r.RelatingType.HasPropertySets:
                            if p.Name == 'Analytical Properties(Type)':
                                for t in p.HasProperties:
                                    if t.Name == 'Heat Transfer Coefficient (U)':
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
