# Nakan Texturing Tools

Painting tools for the artist: editable round brushes, color variation per stroke, seamless blur and normals from your painting.

These are the painting tools I find essential: Photoshop-style round brushes you can edit, color variation on every stroke for graphic effects, filters like blur, turning a painting into normals. All so you can paint with quality in Blender without the tool taking over. It's a tool for an artist, not an add-on that tries to make the art for you. Everything lives in **Sidebar (N) > Nakan Texturing Tools**, in the 3D View and the Image Editor.

## Features

- **Brush**: elliptical tips with roundness and hardness, automatic spacing, and Align View to Surface (Alt+V). You can restrict painting to the region of the face you clicked.
- **Color**: random variation per stroke (hue, saturation, value), and dual painting, which paints a primary and a secondary color on two images at once. Palettes with groups, middle color, swap and import/export.
- **Filters**: blur and distort directly on the mesh surface with no seams, a 2D blur for the whole texture in UV space (with undo), and normals generated from your colors.

## How to use

1. Select a mesh with a UV map and an image texture, and switch to Texture Paint mode.
2. Open **Sidebar (N) > Nakan Texturing Tools**.
3. **Brush**: click *Apply elliptical tip* and adjust roundness and hardness. *Back to round tip* returns to Blender's default.
4. Before detailed work, press **Alt+V** to align the view to the surface under your mouse. In *View and surface* you can also limit painting to the area of the face you clicked (Angle limit).
5. **Color variation**: turn on *Variation per stroke* and set how much hue, saturation and value can change.
6. **Dual painting**: turn it on and choose image 1 and image 2 (two different images, for example two Ucupaint layers) and their colors. Each stroke paints both. Shift+X picks color 1 and Alt+Shift+X picks color 2.
7. **Filters**: in *Image filters*, use Blur or Distort on the surface, or *Blur whole texture (UV)* for the whole image (*Undo Blur* reverts it).
8. **Normals from colors**: generate a normal map from your painting to give it relief.

Vector curves and text painted into the texture are a separate add-on: [Vector Texturing](../vector-texturing).

Requires Blender 4.2 or newer. Install the `.zip` from Releases.
