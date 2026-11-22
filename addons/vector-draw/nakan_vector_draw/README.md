# Nakan Vector Draw

Vector drawing in Blender: an Illustrator-style pen, live shapes, Shape Builder, Repeat, Align & Distribute and snapping, all on Bezier curves.

## Credits and license

Nakan Vector Draw started as a fork of **PenForge 1.2.1 by Enrico Biancardi**, released under the GPL-3.0-or-later. It is now developed independently by Nakan and keeps the same license (see LICENSE).

Changes from PenForge:
- Removed: Solid (Extrude / Thickness), Quad Fill / Quad Remesh, mesh Booleans and construction lines. The add-on is about drawing, not modeling.
- Added: Live Shapes (editable rectangles, ellipses, polygons and stars), Repeat (Radial, Grid, Mirror), Align & Distribute, Vector Mode, Contour, Split, Text, Draw on Object (projection).
- New sidebar tab, panel layout and operator names (`nkdraw.*`).

Stroke outlines use Clipper2 through the pyclipr wheels (Boost Software License 1.0).
