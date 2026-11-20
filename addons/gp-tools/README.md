# Nakan GP Tools

Small things that make drawing in Grease Pencil feel closer to Photoshop: flip the canvas, mirror strokes, select by layer.

When I animate frame by frame in Grease Pencil, I miss basic things I'm used to in Photoshop: flipping the canvas to check the drawing, mirroring, selecting by layer. So I made these. They live in **3D View > Sidebar (N) > Nakan GP**.

## How to use

1. Select your Grease Pencil object and open **Sidebar (N) > Nakan GP**.
2. **Flip the canvas**: in the Canvas panel, click **Flip Canvas** to see the drawing mirrored and check its proportions. Click again to flip back.
3. **Mirror while drawing**: turn on the **Layer Mirror** panel with the checkbox in its header, tick the layers under *Mirrored layers*, choose the axis, and choose the Center: leave it empty for the object origin, or click the Empty button to create one at the 3D cursor. Draw in Draw Mode and each new stroke is mirrored live, eraser included.
4. **Select by layer**: in Edit Mode, select a few points and right-click. **Reveal in Layer** makes their layer active and **Select Whole Layer** selects every point of it. **Select All Vertices of Layer** is in the Layers panel menu.
5. Bonus: right-click any number field in Blender and choose **Flip** to multiply it by -1.

Requires Blender 4.3 or newer. Install `nakan_gp_tools.py`.
