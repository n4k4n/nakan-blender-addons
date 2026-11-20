# Nakan Extras for PenForge

What I missed in PenForge, coming from Affinity Designer: live shapes, align and distribute, drawing projected on objects, and Blender's own shortcuts working on the construction grid.

**This is not PenForge.** It's a small add-on that plugs into [PenForge](https://4754272285663.gumroad.com/l/penforge) by Enrico Biancardi, which is sold separately. Buy and install PenForge first; these extras add panels to its tab.

I bought PenForge and liked its pen a lot, but coming from Affinity Designer I kept missing things: align and distribute, primitive shapes I could keep editing, Blender's usual shortcuts working without leaving the construction grid, projecting curves onto surfaces. So I made them as extras on top of it.

## Features

- **Live Shapes**: rectangles with roundness per corner, ellipses that turn into drops, polygons that turn into stars, all editable until you click Convert to Curves.
- **Align & Distribute** on the construction plane axes.
- **Vector Mode**: G, R and S move, rotate and scale on the construction plane, Shift+D duplicates on it, and P opens the pen.
- **Draw on objects**: what you draw is projected onto a surface; Wrap Selected, Re-project, and Apply Projection to make it permanent.
- **Contour and Split** for curves.
- **Target collection**: everything made in Vector Mode goes to one collection.

## How to use

1. Install and enable PenForge.
2. Install this add-on's `.zip` with **Edit > Preferences > Add-ons > Install from Disk** and enable it. Its panels appear in the PenForge tab of the Sidebar (N).
3. Turn on **Vector Mode** in its panel header. G, R, S, Shift+D and P now work on the construction plane.
4. Add shapes from the **Primitive** panel and change their size, corner roundness or points at any time. Click **Convert to Curves** when you want to edit their points with the pen, use them in PenForge's booleans or in the Shape Builder.
5. Select several objects and use **Align & Distribute** to line them up on the CPlane axes.
6. To draw on an object, set **Draw on** to **Object** in the Vector Mode panel and pick the surface. Use **Wrap Selected** for curves you already have, and **Apply Projection** to keep the projected shape for good. Texts become meshes when applied.
7. Turn Vector Mode off to go back to Blender's normal transforms.

Requires Blender 4.2 or newer and PenForge 1.2 or newer.
