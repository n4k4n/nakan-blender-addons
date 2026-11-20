# Nakan Bone Tools

More of what I wanted from the Bone Collections panel: copy a collection with its bones, rename in sequence, prefixes and sides.

I love working from the Bone Collections panel; building complex bone chains there keeps me organized. But I felt it was missing things to make it fluid, like renaming from it or copying the bones assigned to a collection. These tools add that to the panel's right-click menu (**Properties > Armature**), and they work on the active collection.

## Features

- **Duplicate Collection + Bones**: copies the active collection, its sub-collections and every bone assigned to them, keeping the hierarchy.
- **Prefix Bones with Collection Name**, optionally using each sub-collection's own name.
- **Rename Bones in Sequence**: `CollectionName_001`, `_002`… ordered by hierarchy, by position along an axis, or by Outliner order, with a live preview.
- **Side Suffix**: `.L`, `.R`, or Automatic by position, so Symmetrize works right away.
- **Deform On / Off** for every bone in the collection.

## How to use

1. Select your armature and open **Properties > Armature (Object Data) > Bone Collections**.
2. Organize the bones in collections as usual: select bones and click Assign on a collection.
3. Click a collection to make it active, then right-click it to see the Nakan tools.
4. To build the other side of a limb, or an IK/FK copy, choose **Duplicate Collection + Bones**, name the new collection, add an optional suffix and confirm.
5. To name a chain, choose **Rename Bones in Sequence**, pick the order, start number, digits and separator, check the preview and confirm.
6. To prepare a rig for mirroring, choose **Side Suffix > Automatic (by position)**, then use Blender's **Armature > Symmetrize** in Edit Mode.
7. For control or helper collections, use **Deform Off** so their bones don't deform the mesh.

Requires Blender 4.1 or newer. Install `nakan_bone_tools.py`.
