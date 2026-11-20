# Nakan Vector Texturing

Like illustrating in Illustrator directly on the mesh: vector curves and text projected into the texture, with pixel-style filters on top.

It's like illustrating in Adobe Illustrator directly on the mesh, already seeing the final result projected onto it. You only need to place a curve roughly where you want it and the add-on projects it into a texture, with no seams across UV islands. You get what vector software has, like fill, stroke, caps and dashes, and filters you'd expect from pixel software, like blur and roughen edges. A small graphic suite that works through Blender's native curves, so I can create right on top of the model without going to another program. Everything lives in **Sidebar (N) > Nakan Vector Texturing**.

## Features

- **Layers**: curves and text objects are your layers, with groups, opacity, masks (inside / outside, like track mattes) and a Live mode.
- **Fill and Stroke**: solid or gradient fill and stroke, width and caps. Shapes can be projected flat or follow the surface.
- **Trim Path**: After Effects-style Start, End and Offset. Keyframe them to draw a line on; it plays in the viewport and in final renders.
- **Dashes, Taper and Wave** for the stroke.
- **Modifiers**: Blur, and Roughen Edges, which breaks up the outline with noise for a hand-drawn look (inside / outside reach, size, detail, a color ramp, Evolution and Boil).
- **Animation**: keyframe almost any setting and the texture redraws on every frame, in the viewport and in renders.
- Works alongside Ucupaint.

**Important:** give Vector Texturing its own image (for example a separate Ucupaint layer). Every update clears that image and redraws only the vector shapes.

## How to use

1. Select a mesh with a UV map and open **Sidebar (N) > Nakan Vector Texturing**.
2. In *Vector Layers*, click **+** (New Vector Set). It creates a collection for this mesh's layers.
3. Click **New Image** and pick a size. It's a transparent image only for the vector shapes.
4. Use that image in the mesh's material: an Image Texture node with it, mixed over your base color using its alpha, or a separate Ucupaint layer. The add-on draws into the image; you decide how the material uses it.
5. Click **Draw Curve** and draw over the mesh, or **Add Text** and type. You can also move any existing curve or text object into the set's collection. Meshes are ignored: convert them with Object > Convert > Curve first.
6. Keep **Live** on and the texture redraws as you edit. Set Fill and Stroke, colors, width and caps in the *Layer* panel.
7. Add Trim Path, Dashes, Taper or Wave to the stroke, and Blur or Roughen Edges from *Add Modifier*.
8. Animate by keyframing almost any setting.
9. When you're happy, click **Bake** to turn the result into final pixels.

If an older Nakan Texturing Tools that still includes Vector Layers is enabled, update it first. Files made with it keep working.

Requires Blender 4.2 or newer. Install the `.zip` from Releases.
