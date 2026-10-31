# Visio output

Created 2026-10-31 · Status: implemented. The converter's samples open in Microsoft Visio (checked by Jon); a check of the direct writer's sample in Visio is pending

## Requirement

The project requirements were updated to emit Microsoft Visio files in
addition to draw.io. Production is moving to Visio as its standard, so:

- the Visio file must not depend on draw.io: it is written directly, not
  converted from the draw.io file;
- both formats are kept for now, and draw.io may be dropped later, so
  dropping it must not touch the Visio side.

Every run writes `output/topology.vsdx` and `output/topology.drawio`, and the
playbook publishes both.

## Approach: one diagram model, one writer per format

| Option | For | Against |
| --- | --- | --- |
| Convert the finished draw.io XML to Visio (built first, see [History](#history)) | One drawing path, little new code | Visio depends on draw.io: dropping draw.io means rewriting the Visio side, and every draw.io-ism (HTML labels, style strings, action links) has to be reverse-engineered |
| A second renderer per format | Each format uses its native features | Every feature (IPsec, VLANs, zones, roles, later VRFs) draws itself twice, and the two outputs drift apart |
| **A format-neutral diagram model with a writer per format** (chosen) | Features draw once. Each writer is independent; either can be deleted alone | The model has to cover what both formats need |

```
facts JSON -> links -> pathfinder -> renderer.build() --+
                                     features.run_features() --> diagram.Diagram
                                                                   |-> vsdx.write()   topology.vsdx
                                                                   '-> drawio.write() topology.drawio
```

- `scripts/diagram.py` is the model: `Diagram`, `Page`, `Layer`, `Shape`
  (box, ellipse or text, optionally with an `Icon`), `Line` (source,
  target, waypoints, and exit/entry fractions on each node) and
  `LineLabel` (a label at a point along a line). Every item can carry a
  tooltip, a link (`page:<id>`, `page:<id>#highlight=<tags>`, or a web
  address), tags and free data. It also holds the geometry both writers
  need (`Page.route`, `along`, `curve`, `clip`).
- Styles are plain dicts with a fixed set of keys (`fill`, `stroke`,
  `dashed`, `rounded`, `font_size`, `label_bg`, `curved`, `end_arrow`,
  `jumps` and so on, listed in `diagram.py`). An unknown key raises, so a
  typo fails the feature instead of being dropped silently by one writer.
  `config.py` and every feature's `SETTINGS` now hold style dicts instead of
  draw.io style strings.
- `renderer.build()` returns the model; features draw into it through
  `layer.edge(...)` and `layer.text(...)`, which build model items.
- `scripts/vsdx.py` and `scripts/drawio.py` each write the model. Neither
  imports the other, and `tests/test_vsdx.py` checks that the Visio writer
  never loads the draw.io one.
- `config.OUTPUT_FORMATS` (default `["vsdx", "drawio"]`) and
  `main.py --formats=vsdx,drawio` pick the files to write. A format that
  isn't picked has any older file of its removed from the output folder.

A `.vsdx` file is an Open Packaging Conventions zip of XML parts, so
`zipfile` and `xml.etree` are enough. No library and no Visio install is
needed.

## Visio mapping

| Model | Visio |
| --- | --- |
| Page | Page with the same name (duplicates get ` (2)`), sized to its drawing plus a 0.5 in margin |
| Layer | Row in the page's `Layer` section: same name, `Visible`, `Lock`. Base items are on `Background` |
| Shape | 2-D shape: `PinX`/`PinY` at the centre, `Width`/`Height`, `Angle` from `rotation`, `FillForegnd` + `FillPattern`, `LineColor` + `LinePattern` (2 for dashed), `LineWeight` |
| `rounded` | Four `ArcTo` rows in the geometry, radius `arc`% of the shorter side (the `Rounding` cell isn't used, because LibreOffice ignores it) |
| `kind: ellipse` | `Ellipse` geometry row |
| `kind: text` (labels, badges) | No fill or outline unless the style has one; `label_bg` becomes the fill and `label_border` or `stroke` the outline |
| Text | `Character` row for colour, bold and size; `Paragraph` row for alignment |
| Icon (a PNG, or an SVG that only wraps one PNG) | `Foreign` shape with the PNG in `visio/media/`, letterboxed to the picture's aspect ratio. `label_below` puts the text block below the picture. Each distinct picture is stored once |
| Other icon | Plain box with the label |
| Line | 2-D shape whose geometry is a polyline from `Page.route()`: the exit point on the source, the waypoints, the entry point. Without an exit/entry fraction the end is clipped to the shape's border. `curved` is sampled into a smooth polyline (quadratic segments through the waypoint midpoints, the same curve draw.io draws) |
| `start_arrow` / `end_arrow` | `BeginArrow` / `EndArrow`: `classic` (the default end) → 5, `block` → 4, `open` → 1, `oval` → 10, `diamond` → 22, `none` → 0 |
| LineLabel | Shape at fraction `(at + 1) / 2` along the line, plus the offset. A label without a size is sized from its text |
| Tooltip | `Comment` cell (Visio's ScreenTip) |
| `page:<id>` link | `Hyperlink` row with `SubAddress` = the target page's name. Highlighting has no Visio equivalent, so `#highlight=` only opens the page |
| Web link | `Hyperlink` row with that `Address` |
| Tags, data | `Property` (Shape Data) rows, row names sanitised |

Coordinates: 96 model units are one inch, and the y axis is flipped because
Visio's points up.

Lines are 2-D shapes rather than 1-D connectors. A 1-D shape's geometry is
recalculated by Visio from its begin and end points, which would lose the
routed waypoints. Gluing lines to shapes (Visio's `Connects`) is left out
for the same reason: a glued connector reroutes itself when opened.

The draw.io writer maps the same model to cells, styles, `<object>`
wrappers for tooltips/links/data, and action links that open a page and
highlight tagged cells. Its output is cell-for-cell the same as before the
refactor (same ids, parents, labels, tooltips and geometry).

## Visio's schema rules

Visio validates each part against its schema; LibreOffice and draw.io don't.
The writer follows the schema's order inside every shape: `Cell` elements,
then `Trigger`, `Section` (and inside a section, its `Cell`s before its
`Row`s), `Text`, `Data1`–`Data3`, `ForeignData`, then `Shapes`. The package
also has `visio/windows.xml` (a drawing window on the first page), with its
relationship and content type. `tests/test_vsdx.py` checks these rules for
every kind of shape the writer makes and on a full `main.py` run.

## Checking it

LibreOffice reads `.vsdx` with its own independent importer, so each sample
was converted to PDF with `soffice --headless --convert-to pdf` and compared
with draw.io's own viewer rendering the draw.io file:

| draw.io (viewer) | Visio file (LibreOffice) |
| --- | --- |
| ![](visio-l2-drawio.png) | ![](visio-l2-libreoffice.png) |

With device icons from `docs/Prod_Library.xml` (the two Catalyst switches
are SVG-wrapped PNGs, the ASR1004 a plain PNG):

![](visio-icons-libreoffice.png)

The comparison found and fixed: missing fills (`FillPattern` must be set),
rounded corners (LibreOffice ignores `Rounding`, so arcs are drawn), the
arrowhead index for the default arrow, and the default font size (12 px).

Jon opened the converter's fixed samples (see [History](#history)) in
Microsoft Visio, and they open and display correctly. The direct writer
writes the same parts in the same order, and its sample is waiting for the
same check.

## Failure handling

If one writer raises, `main.py` deletes any older copy of its file so a
stale one is never published, prints the error, still writes the other
formats and `template.drawio`, and exits with status 3. The playbook accepts
status 3 for the build, publishes the files that exist, and fails the job
in its last play, after the read-only audit.

## History

The first version (earlier the same day) converted the finished draw.io XML
to Visio. Its samples opened in LibreOffice and draw.io but not in Visio
("some parts are missing or invalid"): a tooltip cell was written after the
text sections, `ForeignData` came before `Text`, and there was no
`visio/windows.xml`. Fixing those made the converter's files open in Visio.
Production then asked for the Visio output not to depend on draw.io at all,
so the converter was replaced by the model and two writers described above.

## Open points

- **Positions still come from draw.io.** `template.drawio` holds node
  positions and is edited in draw.io. If draw.io is dropped, positions need
  another home: reading them from a `template.vsdx` arranged in Visio, or a
  plain positions file.
- **Icon library format.** `docs/Prod_Library.xml` is a draw.io library
  file. The code only reads the pictures from it, so it could become a
  folder of PNGs, or a Visio stencil the team prefers, without touching the
  writers.
- **HTML tables.** The VRF design draws each routing table as one HTML-table
  shape in draw.io. The model has no tables, so the VRF work will draw one
  shape per row (or per cell), which both writers handle.
