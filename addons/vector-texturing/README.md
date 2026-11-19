# Nakan Vector Texturing

Illustrator-style curves and text projected on a mesh and rasterized into its UV texture as you work, with no seams across UV islands. They stay editable until you Bake them into final pixels. Everything lives in **Sidebar (N) > Nakan Vector Texturing**.

- **Layers**: curves and text objects are your layers, with groups, opacity, masks (inside / outside, like track mattes) and a Live mode.
- **Fill and Stroke**: solid or gradient fill and stroke, width and caps. Shapes can be projected flat or follow the surface.
- **Trim Path**: After Effects-style Start, End and Offset. Keyframe them to draw a line on; it plays in the viewport and in final renders. Trim paths together or one by one.
- **Dashes, Taper and Wave** for the stroke.
- **Modifiers**: Blur, and Roughen Edges, which breaks up the outline with noise for a hand-drawn look (inside / outside reach, size, detail, a color ramp, Evolution and Boil).
- **Animation**: keyframe almost any setting and the texture redraws on every frame, in the viewport and in renders.
- Works alongside Ucupaint.

**Important:** give Vector Texturing its own image (for example a separate Ucupaint layer). Every update clears that image and redraws only the vector shapes.

If an older Nakan Texturing Tools that still includes Vector Layers is enabled, update it first. Files made with it keep working.

Requires Blender 4.2 or newer. Install the `.zip` from Releases.
