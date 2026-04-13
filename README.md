# IFC to gbXML Converter

A Python-based command-line tool that converts IFC building models to the gbXML file format for whole-building energy analysis. It bridges the gap between Building Information Modeling (BIM) design tools and Building Performance Simulation (BPS) software by translating the open exchange format IFC into a validated gbXML file.

This project originated as the graduation research of M.G. (Maarten) Visschers at the Eindhoven University of Technology, in collaboration with Arcadis Nederland. The accompanying thesis — *BIM based whole-building energy analysis towards an improved interoperability* — is included in the repository.

## Supported standards

| Standard | Version | Reference |
|----------|---------|-----------|
| IFC | IFC2X3_TC1 | [buildingSMART schema](http://www.buildingsmart-tech.org/ifc/IFC2x3/TC1/html/index.htm) |
| gbXML | 6.01 | [gbXML schema](http://www.gbxml.org/Schema_Current_GreenBuildingXML_gbXML) |

## Quick start

### 1. Install dependencies

The converter requires **IfcOpenShell** (for reading IFC files) and **PythonOCC / OpenCASCADE** (for converting implicit geometry to explicit coordinates). The recommended installation method is conda:

```bash
conda install -c conda-forge ifcopenshell pythonocc-core
```

Standard-library modules used: `argparse`, `datetime`, `xml.dom.minidom`, `pathlib`.

### 2. Run the converter

```bash
python IFC_gbXML_Convert.py <input.ifc>
```

The output gbXML file is written to the `output/` folder (created automatically) and named after the input file so you can always trace which IFC produced which gbXML:

```
input:   MyBuilding.ifc
output:  output/MyBuilding_gbXML.xml
```

### 3. CLI options

```
usage: IFC_gbXML_Convert.py [-h] [-o OUTPUT_DIR] ifc_file

positional arguments:
  ifc_file                    Path to the input .ifc file

options:
  -h, --help                  Show this help message and exit
  -o, --output-dir OUTPUT_DIR Directory for output (default: ./output)
```

### Examples

```bash
# Convert a single file — result in output/model_gbXML.xml
python IFC_gbXML_Convert.py model.ifc

# Use a test case from the repo
python IFC_gbXML_Convert.py "Test cases/Pilot project 1/Pilot project 1.ifc"

# Specify a custom output directory
python IFC_gbXML_Convert.py model.ifc -o results/
```

## IFC input requirements

The converter relies on **2nd level space boundaries** (`IfcRelSpaceBoundary`) to build explicit geometry for the gbXML schema. If your IFC file does not contain these, the conversion will fail or produce incomplete results.

**How to export a compatible IFC file from Autodesk Revit:**

1. Go to **File > Export > IFC**.
2. In the export dialog, select the **IFC2x3 Coordination View 2.0** MVD (or a setup that targets IFC2X3_TC1).
3. Make sure **Export base quantities** is checked.
4. Under **Space boundaries**, select **2nd level** (this is critical).
5. Ensure rooms/spaces are properly defined and bounded in the Revit model.

## IFC-to-gbXML entity mapping

The table below shows which IFC entities are read and what gbXML elements they produce.

| IFC entity | gbXML element | Notes |
|---|---|---|
| `IfcSite` | `Campus`, `Location` | Longitude, latitude, elevation, postal address |
| `IfcBuilding` | `Building` | Building type set to "Unknown" by default |
| `IfcBuildingStorey` | `BuildingStorey` | Storey name and elevation level |
| `IfcSpace` | `Space` | Area, volume, storey reference |
| `IfcRelSpaceBoundary` | `SpaceBoundary`, `Surface` | Core geometric conversion; determines surface types |
| `IfcWall` / `IfcWallStandardCase` | `Surface` (ExteriorWall / InteriorWall) | Internal vs. external based on `InternalOrExternalBoundary` |
| `IfcSlab` | `Surface` (InteriorFloor) | Only floor slabs are processed |
| `IfcCovering` | `Surface` (Ceiling) | Ceiling elements |
| `IfcRoof` | `Surface` (Roof) | Roof elements |
| `IfcWindow` | `Opening`, `WindowType` | Includes U-value, solar heat gain coefficient, visible transmittance |
| `IfcRelAssociatesMaterial` | `Construction` | U-value, absorptance, layer set reference |
| `IfcMaterialLayerSetUsage` | `Layer` | One layer per unique material association |
| `IfcMaterialLayer` / `IfcMaterial` | `Material` | Name, thickness, R-value (from analytical property sets) |
| `IfcApplication`, `IfcPerson` | `DocumentHistory` | Program info, author, creation timestamp |

## Geometry conversion

IFC typically stores geometry in an **implicit** representation (extrusions, swept solids, CSG), whereas gbXML requires **explicit** planar geometry (`PolyLoop` with `CartesianPoint` coordinates). The converter handles this translation using OpenCASCADE (via PythonOCC):

1. The `IfcCurveBoundedPlane` attached to each `IfcRelSpaceBoundary` is passed to `ifcopenshell.geom.create_shape()`.
2. OpenCASCADE tessellates the resulting shape into faces, wires, edges and vertices.
3. The vertex coordinates are extracted and written as gbXML `CartesianPoint` elements.
4. A Z-offset is applied to account for storey-level placement by reading the `IfcCartesianPoint` of the related `IfcBuildingStorey`.

## Test cases

Three pilot projects of increasing complexity are included in `Test cases/`. Each folder contains the source Revit file (`.rvt`), the exported IFC file (`.ifc`), and a reference gbXML output (`New_Exported_gbXML.xml`) for comparison.

| Test case | Description |
|-----------|-------------|
| Pilot project 1 | Simple multi-storey building with basic walls, floors, and windows |
| Pilot project 2 | Additional building elements and material variations |
| Pilot project 3 | More complex geometry with a larger set of spaces and openings |

You can verify your installation by converting a test case and comparing the result with the included reference output:

```bash
python IFC_gbXML_Convert.py "Test cases/Pilot project 1/Pilot project 1.ifc"
# compare output/Pilot project 1_gbXML.xml with "Test cases/Pilot project 1/New_Exported_gbXML.xml"
```

## Validating the output

The generated gbXML files can be validated and visualised in several ways:

- **gbXML schema validation** — use the official XSD from [gbxml.org](http://www.gbxml.org/Schema_Current_GreenBuildingXML_gbXML) with any XML validator.
- **Spider gbXML Viewer** — a browser-based 3D viewer at [spider.gbxml.org](https://spider.gbxml.org/) that lets you visually inspect the geometry.
- **DesignBuilder** — import the gbXML and run a whole-building energy simulation directly.

## Known limitations

- **Proof of concept** — this tool demonstrates feasibility; it is not a production-ready converter.
- **No GUI** — interaction is command-line only.
- **IFC schema** — only IFC2X3_TC1 is supported. IFC4 files are not handled.
- **Complex geometry** — buildings with curved walls, non-orthogonal shapes, or intricate facade details may produce incomplete or incorrect geometry.
- **Thermal properties** — extraction of R-values and U-values depends on how the original model was authored in Revit. Missing or inconsistently placed analytical property sets will result in missing thermal data in the output.
- **IfcDoor** — door elements are not converted to gbXML openings.
- **IfcSlab subtypes** — only floor slabs are processed; roof slabs are handled via `IfcRoof`.
- **Multi-building sites** — the script assumes a single building per IFC file.

## Project structure

```
IFC-to-gbXML-converter/
  IFC_gbXML_Convert.py           Main converter script (CLI)
  README.md                      This file
  LICENSE                        GNU GPL v3
  pyproject.toml                 Linter / type-checker configuration
  Visschers_Thesis_2016July8.pdf Original thesis (Dutch summary included)
  Visschers_Thesis_2016July8_English.pdf  Thesis with Dutch summary translated to English
  Test cases/
    Pilot project 1/             Simple test model (.rvt, .ifc, reference .xml)
    Pilot project 2/             Medium-complexity test model
    Pilot project 3/             Higher-complexity test model
  output/                        Created automatically; converter writes results here
```

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

## References

- Visschers, M.G. (2016). *BIM based whole-building energy analysis towards an improved interoperability — A conversion from the IFC file format to a validated gbXML file format*. Master thesis, Eindhoven University of Technology.
- [buildingSMART IFC2x3 TC1 specification](http://www.buildingsmart-tech.org/ifc/IFC2x3/TC1/html/index.htm)
- [gbXML.org — Green Building XML schema](http://www.gbxml.org/)
- [IfcOpenShell](http://ifcopenshell.org/) — open-source IFC toolkit
- [PythonOCC](http://www.pythonocc.org/) — Python wrapper for OpenCASCADE geometry kernel

## Contact

For questions or issues, contact maartenvisschers@hotmail.com or file an issue in the repository.
