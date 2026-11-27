# Curve Sync

See your animation curves where you sync timing: the final NLA curve in the Graph Editor, and the curves of a scene right on its strip in the Video Sequencer.

When I edit in After Effects I'm used to lining things up by looking at the motion itself, not by scrubbing back and forth. In Blender two places made that hard. In the NLA, the Graph Editor only shows the curves of one action, never the final result of all the layers together. And in the Video Sequencer, a scene strip is just a colored block: nothing tells you where the camera move eases out or where the hit lands. So I made this add-on to draw those curves where I'm doing the syncing.

## Features

- **Final Curve (Graph Editor)**: draws the final value of each visible channel, with every NLA layer combined (blend modes, influence, repeat, transitions, tweak mode), over the curves you are editing. Line up the easing of one layer with the motion of the others without markers.
- **Sync Curves (Video Sequencer)**: mark any channels of a scene, like a camera move or a character jump, and see them drawn over that scene's strips in the Sequencer.
  - The curves follow the strip: moving, trimming, offset, reverse and speed changes.
  - A dot on each keyframe, so you can snap cuts and sounds to the exact frame.
  - It shows the final value, so NLA layers are included.
  - Works for scene strips inside meta strips too.
- Each curve keeps its channel color, and you can show, hide or recolor it.

## How to use

### Final Curve in the Graph Editor

1. Open the Graph Editor and the sidebar (**N**), tab **Curve Sync**.
2. **Final Curve** is on by default. The final value of each visible channel is drawn as a lighter line over the curves.
3. Use **Only Selected Channels** to reduce it to the channels you're working on.
4. When you tweak a strip in the NLA, **Tweak Stack** set to **Auto** finds out if you entered with Full Stack or Lower Stack.

### Sync Curves in the Video Sequencer

1. Go to the scene with the animation (not the edit scene).
2. In the Graph Editor (or the Dope Sheet), select the channels you want to see in the edit, like the camera's Z Location or a character's root bone.
3. Right-click the channels and choose **Mark as Sync Curves**, or click **Mark Selected** in the sidebar, tab **Curve Sync**. They show up in the **Sync Curves** list of that scene.
4. Go to your edit scene and select the scene strip (or a meta strip that has it).
5. In the Sequencer sidebar (**N**), tab **Curve Sync**, turn on **Show on '...'**. You can also right-click the strips and choose **Toggle Sync Curves**.
6. The marked curves are drawn over the strip, each one stretched to the strip height, with a dot on each keyframe.
7. In **Display** you can change the height of the curves, the dark backdrop behind them, line width and opacity, and turn the keyframe dots off.

Speed changes inside a strip (more than one retiming segment) aren't followed yet: the curves use the average speed, and the panel warns you.

If you used the older NLA Final Curve add-on, turn it off: Curve Sync replaces it.

Requires Blender 4.4 or newer. Install the `.zip` from Releases.
