# Nakan Vector Draw

Vector drawing in Blender the way I'm used to from Illustrator and Affinity Designer: a pen tool, shapes that stay editable, Shape Builder, Repeat, align and distribute, and snapping, all on regular Bezier curves.

I draw a lot of flat and stylized work in Blender, and I wanted to stop jumping to another program for the vector part. This is the tool I put together for that. It started as a fork of PenForge by Enrico Biancardi (see Credits below). I removed what was about modeling, kept the drawing part and added the things I kept missing. From here on I'll keep developing it on its own.

## Features

- **Pen**: click for corners, drag for smooth points, Alt to convert or break handles, Ctrl to select points directly. Type lengths and angles while drawing (`3.5`, `<45`, `3.5<45`, `x,y`).
- **Rectangle, Circle and Polygon** drawn corner to corner, Shift for square, Alt from the centre.
- **Live Shapes**: rectangles with roundness per corner, ellipses that turn into drops, polygons that turn into stars. Editable until you click Convert to Curves.
- **Snap**: anchors, midpoints, centres, intersections, perpendicular, on curve and grid, plus smart guides that line the cursor up with points you hovered.
- **Repeat** (like Illustrator's Object > Repeat): Radial, Grid and Mirror copies that stay live. Edit the original and every copy follows. Expand turns them into separate curves.
- **Shape Builder**: drag across overlapping shapes to merge regions, Alt+drag to delete them.
- **Align & Distribute** on the drawing plane axes.
- **Move, Copy and Rotate** from a base point to a snapped destination.
- **Contour and Split** for curves.
- **Mirror while drawing** across the plane's X or Y axis.
- **Draw on objects**: what you draw is projected onto a surface. Apply Projection keeps the projected shape for good.
- **Text** that stays editable on the drawing plane.
- **Vector Mode**: G, R and S move, rotate and scale on the drawing plane, Shift+D duplicates on it, P opens the pen.
- **Drawing plane and grid**: draw on Top, Front, Right, the 3D cursor, the view, or pick a face.

## How to use

1. Download the `.zip` from [Releases](../../../../releases) and install it with **Edit > Preferences > Get Extensions > Install from Disk** (or drag it into Blender). If you have Nakan Vector Tools or Nakan Extras for PenForge, disable them first.
2. Open the **Nakan Vector Draw** tab in the Sidebar (N) and turn on **Vector Mode** at the top.
3. Click **Pen** (or Ctrl+Shift+P). Click to place corners, drag to make smooth points, click the first point to close the shape. Enter finishes, Esc leaves.
4. While drawing, **D** turns snapping on and off and **S** opens the snap menu (click a name to use it once). The **Snap**, **Grid** and **Guides** buttons in the panel do the same from outside the pen.
5. Use **Rect**, **Circle** or **Polygon** for shapes. Change their size, roundness or points later in the **Primitive** panel. Click **Convert to Curves** when you want to edit their points with the pen.
6. To make copies, select a shape and click **Radial**, **Grid** or **Mirror** in the **Repeat** panel. Change the number of copies, the angle, the spacing or the axis there. Edit the original and the copies follow. Click **Expand** when you want them as separate curves.
7. Select overlapping closed shapes and click **Shape Builder**. Drag across regions to merge them, Alt+drag to delete them, Enter to finish.
8. Select several objects and use **Align & Distribute** to line them up.
9. To draw on an object, set **Draw on** to **Object** in the Vector Mode panel and pick the surface. Click **Apply Projection** when it looks right.
10. Turn Vector Mode off to go back to Blender's normal transforms.

Requires Blender 4.2 or newer.

## Credits

Nakan Vector Draw started as a fork of **PenForge 1.2.1 by Enrico Biancardi**, released under the GPL-3.0-or-later, and keeps the same license. The pen, the snaps, the drawing plane, the Shape Builder and Move/Copy/Rotate come from PenForge. If you also want its modeling tools (extrude, thickness, quad meshes, booleans), [PenForge](https://4754272285663.gumroad.com/l/penforge) is sold by its author.

Removed from PenForge: Solid (Extrude / Thickness), Quad Fill and Remesh, mesh Booleans, construction lines. Added: Live Shapes, Repeat, Align & Distribute, Vector Mode, Contour, Split, Text, Draw on objects, and a new panel layout.

Stroke outlines use Clipper2 through the pyclipr wheels (Boost Software License 1.0).
