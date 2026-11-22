# Paintover PSD

Build the scene in Blender, paint it in Photoshop or Affinity. One click renders a layered PSD with every object already selectable.

Painting over a 3D layout is a great way to make illustrations and static backgrounds: Blender gives you perspective, proportions and light, and the painting happens in a 2D app. What slowed me down was the handoff. Cryptomatte is the best way to get object masks out of a render, but Photoshop and Affinity can't read it, and the magic wand leaves jagged edges. So I made this add-on to render straight to a PSD with everything I need to start painting.

## Features

- **One PSD, ready to paint**: the render, the flat colors and the shadows on their own layers.
- **A mask per object and per material**, with clean antialiased edges, organized in groups. Each mask has its own color: show the group and click an object with the Move tool (Auto-Select) to jump to its layer, or Ctrl/Cmd + click the layer thumbnail to load it as a selection.
- **Shadows as a layer**: black with transparency, so you can recolor, soften or repaint them.
  - **Shadow Catcher** (Cycles): shadows on the floor or walls you mark, with the catcher invisible in the render. Perfect for objects on a transparent background.
  - **Everything**: shadows on every surface. Renders twice, with and without shadows, and also gives you the render without shadows as a layer.
- **Export Last Render**: turn the render already in the render window into a PSD, without rendering again.
- **Convert EXR**: turn a multilayer EXR with Cryptomatte, from Blender or another app, into a PSD.
- Your passes and render settings are put back as they were after rendering.

## How to use

1. Set up your scene and camera as usual. Cycles or EEVEE both work.
2. Go to **Properties > Output > Paintover PSD**.
3. Choose the layers you want: **Render**, **Flat Colors**, **Object Masks** and **Material Masks**.
4. Choose the **Shadows**:
   - **Auto** uses the Shadow Catcher when the scene has one (in Cycles), and Everything otherwise.
   - To use a Shadow Catcher, select the floor (or a wall) and click **Selected as Shadow Catcher**. Turn on **Film > Transparent** in the render settings if you want only the objects and their shadows.
   - **Everything** renders twice, so it takes twice as long.
5. Pick the **Folder** (// is the folder of the .blend) and, if you want, a **Name**. Empty uses the .blend name and the frame number.
6. Click **Render Paintover PSD** (also in the **Render** menu). Blender renders the current frame and saves the PSD.
7. Open the PSD in Photoshop, Affinity Photo or any app that reads layered PSDs:
   - **Render** is the final image. **Flat Colors** (hidden) has the base colors without light.
   - **Shadows** is a black layer with transparency. It is hidden when the render already includes the shadows (Everything mode), and visible when they come from a Shadow Catcher.
   - In **Object Masks** and **Material Masks**, Ctrl/Cmd + click a layer thumbnail to select that object, then paint on a new layer.
8. Already rendered with F12? Click **Export Last Render**. Masks, flat colors and catcher shadows only show up if those passes were on when you rendered.
9. Have an EXR from elsewhere? Click **Convert EXR...** and pick the file. The PSD is saved next to it. The EXR needs Cryptomatte and the ZIP (or no) compression.

Use **Skip** to leave masks out by name, separated by commas, like `Light, Camera*, *_helper`.

Requires Blender 4.2 or newer. Install the `.zip` from Releases.
