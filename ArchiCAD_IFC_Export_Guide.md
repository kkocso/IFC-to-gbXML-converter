# ArchiCAD IFC Export Settings for gbXML Conversion

This guide explains how to configure ArchiCAD's IFC export to produce an IFC file that works with the IFC-to-gbXML converter. It is based on an analysis of the file `2423_Sárfimizdó 7.2_26_04_10.ifc`, exported from Graphisoft ArchiCAD 26.

---

## What's in your current export

Your IFC file contains the basic building geometry — 175 walls, 116 slabs, 49 windows, 5 storeys, and 1,809 property values. That's a good start. However, several settings that are critical for energy simulation were turned off during export.

The table below shows every export option found in the file header, its current value, and the value required for a successful gbXML conversion.

| Export option | Current | Required | Why it matters |
|---|---|---|---|
| **IFC Space boundaries** | Off | **2nd level** | The converter uses `IfcRelSpaceBoundary` to generate all gbXML geometry. Without this, the output is essentially empty. This is the single most important setting. |
| **IFC Base Quantities** | Off | **On** | Provides area and volume values for spaces (`IfcElementQuantity`). Without it, gbXML `<Area>` and `<Volume>` elements will be missing. |
| **Space containment** | Off | **On** | Links building elements to the spaces they bound. Needed for the converter to associate walls, floors and ceilings with their adjacent spaces. |
| **Element Properties** | Off | **On** | Exports element-level IFC property sets. The converter reads U-values, thermal transmittance, absorptance, and heat transfer coefficients from these. |
| **Building Material Properties** | Off | **On** | Exports `IfcMaterialLayerSetUsage` with layer thicknesses and R-values. Currently your file has 0 material layer entities — the converter cannot produce Construction, Layer or Material elements without this. |
| **Element Parameters** | Off | **On** | Exports ArchiCAD element parameters as IFC properties. Some thermal and dimensional values are stored here. |
| **Component Parameters** | Off | **On** | Exports composite/component-level parameters. Needed for multi-layer wall and slab constructions. |
| Elements to export | Selected elements only | **Entire Model** (recommended) | Ensure no rooms or enclosing elements are accidentally excluded. |
| Geometry in Collision Detection only | On | **Off** | With this on, elements that don't participate in collision detection (e.g. some ceilings, coverings) may be excluded. |
| Split complex elements | Off | Off | Fine as-is. |
| Material Preservation | Explode all | Explode all | Fine as-is. |
| Convert Grid elements | On | On | Fine as-is. |
| Door Window Parameters | On | On | Fine as-is. |
| Element Classifications | On | On | Fine as-is. |
| Partial Structure Display | Entire Model | Entire Model | Fine as-is. |
| IFC Domain | All | All | Fine as-is. |
| Structural Function | All Elements | All Elements | Fine as-is. |

---

## Step-by-step: setting up the IFC Translator in ArchiCAD 26

### 1. Open the IFC Translator settings

Go to **File > Interoperability > IFC > IFC Translators...**

Duplicate one of the built-in translators (e.g. "General Translator") and name the copy something like **"gbXML Energy Export"** so you can reuse it.

### 2. Set the IFC schema version

In the translator settings, under **Format**:

- Set IFC version to **IFC 2x3**
- Set Model View Definition to **Coordination View 2.0** (this is the `CoordinationView_V2.0` your file already uses — keep it)

### 3. Configure Geometry Conversion

Under **Geometry Conversion**:

- **Export geometries that Participate in Collision Detection only**: set to **Off**
  - This ensures ceilings, coverings, and other non-structural elements are included
- **Split complex elements**: leave **Off**
- **Material Preservation**: leave **Explode all**
- **Curtain Wall / Railing / Stair export mode**: leave as **Single Element**

### 4. Configure Space and Boundary settings (critical)

Under **Space and Zone settings** (this is the most important section):

- **IFC Space boundaries**: set to **2nd level**
  - This generates `IfcRelSpaceBoundary` entities — the converter cannot produce any gbXML surfaces without them
  - "2nd level" means each side of a wall gets its own boundary, which is what energy simulation needs
- **Space containment**: set to **On**
  - Links elements to the spaces they enclose

For this to work, **your ArchiCAD model must have Zones defined**:

- In the ArchiCAD model, place **Zones** (the zone tool) in every room/space
- Each zone must be properly enclosed by walls and slabs
- Zone stamps should be visible and correctly placed
- Use **Design > Update Zones** to recalculate zone geometry before export

### 5. Configure Properties and Quantities

Under **Properties** / **Data Conversion**:

- **Properties To Export**: set to **All properties**
- **Element Properties**: set to **On**
- **Building Material Properties**: set to **On**
- **Element Parameters**: set to **On**
- **Component Parameters**: set to **On**
- **IFC Base Quantities**: set to **On**
- **Door Window Parameters**: leave **On**
- **Element Classifications**: leave **On**

### 6. Configure the elements to export

Under **Elements to Export**:

- Use **Entire Model** rather than "Selected elements only" — this avoids accidentally leaving out enclosing elements or zones

### 7. Set up site and address data in ArchiCAD

The converter reads location data from the IFC file. In ArchiCAD:

- Go to **Options > Project Preferences > Project Location**
- Fill in latitude, longitude, elevation, and altitude (these map to `IfcSite`)
- Go to **File > Info > Project Info** and fill in the address fields (these map to `IfcPostalAddress` — currently missing from your file)

### 8. Set up thermal properties on materials

For the converter to include U-values, R-values, and thermal conductivity in the gbXML output, these need to be defined in ArchiCAD:

- Open **Options > Element Attributes > Building Materials**
- For each building material, fill in the **Thermal Conductivity** value
- For composite walls/slabs, ensure each layer has correct thickness and material assigned
- Use the **Energy Evaluation** feature in ArchiCAD to verify thermal properties are computed

Alternatively, set analytical properties on elements using ArchiCAD's **Property Manager** (Manage > Properties) to add custom properties like "ThermalTransmittance" or "Heat Transfer Coefficient (U)" to walls, slabs and windows.

### 9. Export

1. Go to **File > Interoperability > IFC > Save as IFC...**
2. Select your **"gbXML Energy Export"** translator
3. Choose **IFC** as the file format (not IFC XML)
4. Save the file

---

## Verify the export before running the converter

After exporting, you can do a quick check by opening the `.ifc` file in a text editor and searching for:

| Search term | Expected | What it means |
|---|---|---|
| `IFCRELSPACEBOUNDARY` | Hundreds of entries | Space boundaries are present — the converter can generate geometry |
| `IFCSPACE(` | One per room/zone | Zones were exported as IFC spaces |
| `IFCMATERIALLAYERSET(` | One per wall/slab type | Material layers are included |
| `IFCELEMENTQUANTITY` | Many entries | Base quantities (area, volume) are included |
| `ThermalTransmittance` | One per element with U-value | Thermal properties are present |

If `IFCRELSPACEBOUNDARY` returns zero results, go back and check that Zones are defined in the model and "IFC Space boundaries" is set to "2nd level".

---

## Summary of required changes

Your current IFC header includes this line:

```
'Option [IFC Space boundaries: Off]'
```

After applying the settings above, it should read:

```
'Option [IFC Space boundaries: 2ndLevel]'
```

And these options should all change from `Off` to `On`:

```
'Option [Space containment: On]'
'Option [Element Properties: On]'
'Option [Building Material Properties: On]'
'Option [Element Parameters: On]'
'Option [Component Parameters: On]'
'Option [IFC Base Quantities: On]'
'Option [Export geometries that Participates in Collision Detection only: Off]'
```

---

## Troubleshooting

**"The converter ran but the gbXML file is mostly empty"**
The IFC file is missing space boundaries. Re-export with "IFC Space boundaries: 2nd level".

**"No IfcSpace entities in the file"**
ArchiCAD zones are not defined or not being exported. Place zones in every room and set "Space containment: On".

**"No thermal properties in the gbXML"**
Building material thermal data is missing. Set "Building Material Properties: On" and "Element Properties: On", and ensure thermal conductivity values are filled in on ArchiCAD building materials.

**"Geometry looks wrong or incomplete"**
Check that zones are fully enclosed by walls and slabs. Open zones may not get correct space boundaries. Run "Design > Update Zones" before exporting.
