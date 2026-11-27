# Nakan Text Animator Tools

After Effects' text animators in Blender, plus what AE can't do: animated materials, extrude and bevel per character.

I missed After Effects' text animators in Blender. They open up a huge range of type animation, and here they go a bit further than in AE: since the text is 3D, you can also animate materials, extrude and bevel per character. The text stays a normal Text object, so you can press Tab and retype it at any moment; font, size, spacing, alignment, bold/italic and material slots stay in sync.

## Features

- **Animators**: Opacity, Position, Scale, Rotation, Tracking, Fill (Material), Extrude, Bevel and Floating. Stack as many as you want, each one keyframable.
- **Range Selector like AE**: Start, End and Offset, Falloff with an editable falloff curve, Ease High and Ease Low, Hard mode, Based On characters, words or lines, Amount, Invert, Reverse, and Randomize Order with a seed.
- **Anchor point** per character for Scale and Rotation, set as a percentage of each glyph's box.
- **Tracking anchor**: letters can spread from the left, the center or the right of each line (or follow the text's own alignment), and that side stays in place.
- **Per-character geometry**: fill, extrude and round bevel are built per character, so depth and bevel can be animated too.
- **Material fill**: blend selected characters into another material. Your original materials are never edited.
- **Floating**: characters wobble on their own with random position and rotation, no keyframes needed. In Orbit mode they move around their place within the range you set and always come back; in Drift mode they also keep travelling and spinning away. Correlation goes from every letter on its own to all of them moving together, and the Range Selector controls how strongly each one floats.

## How to use

1. Add a text with **Shift+A > Text**, press Tab and type it. Set font, size and alignment in **Properties > Text (Object Data)** as usual.
2. In the same tab, click **Make Animatable Text**.
3. Click **Add Animator** and choose the property: Opacity, Position, Scale, Rotation, Tracking, Fill (Material), Extrude, Bevel or Floating.
4. In the animator's properties, set the value the characters should have when they are selected, for example Opacity 0% or Position Y -0.5.
5. Animate the Range Selector: keyframe Start, End or Offset (for example End from 0% to 100%) and the effect sweeps through the text. Falloff softens the edge, and Based On switches between characters, words and lines. Ease Low and Ease High smooth each character's own move, so letters settle into place instead of stopping abruptly (Ease Low on the resting side, Ease High on the animator's side).
6. Stack more animators for combined effects, such as Position and Opacity together for a classic type-on.
7. To animate into another material, add a Fill (Material) animator and pick the material. For depth, use Extrude or Bevel animators.
8. For letters that float by themselves, add a Floating animator. Position and Rotation are the range (how far each character can go from its rest place), Speed is how many wobbles per second. Switch Mode to Drift to make them wander off: Drift Speed and Spin set how far and how much they travel per second, and Drift Start is the frame where it begins. Keyframe Amount or the range to calm the letters down or set them free.
9. Check Opacity and Fill in Material Preview or Rendered view; Solid view doesn't show them.
10. Retype the text whenever you want with Tab. If a file made with an older version shows "Made with an older version", click the Resync button next to Add Animator.

Requires Blender 5.0 or newer. Install `nakan_text_animator_tools.py`.
