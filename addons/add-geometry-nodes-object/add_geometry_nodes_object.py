bl_info = {
    "name": "Add Geometry Nodes Object",
    "author": "Nakan",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Add > Geometry Nodes",
    "description": "Adds an empty object with a Geometry Nodes modifier and a new node group",
    "category": "Add Mesh",
}

import bpy
from bpy.types import Operator


class OBJECT_OT_add_geometry_nodes(Operator):
    """Add an empty object with a new Geometry Nodes modifier"""
    bl_idname = "object.add_geometry_nodes_object"
    bl_label = "Geometry Nodes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Empty mesh (no vertices): Geometry Nodes generates everything
        mesh = bpy.data.meshes.new(name="GeometryNodes")
        obj = bpy.data.objects.new("GeometryNodes", mesh)

        # Link it to the active collection
        context.collection.objects.link(obj)

        # Deselect everything, then select the new object and make it active
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj

        # Place it at the 3D cursor (default Add behavior)
        obj.location = context.scene.cursor.location

        # Add the Geometry Nodes modifier
        modifier = obj.modifiers.new(name="GeometryNodes", type='NODES')

        # Create a new node group
        node_group = bpy.data.node_groups.new(
            name="GeometryNodes",
            type='GeometryNodeTree'
        )

        # Create the Input/Output sockets (Blender 4.x API)
        node_group.interface.new_socket(
            name="Geometry",
            in_out='INPUT',
            socket_type='NodeSocketGeometry'
        )
        node_group.interface.new_socket(
            name="Geometry",
            in_out='OUTPUT',
            socket_type='NodeSocketGeometry'
        )

        # Add the Group Input and Group Output nodes
        input_node = node_group.nodes.new('NodeGroupInput')
        input_node.location = (-200, 0)

        output_node = node_group.nodes.new('NodeGroupOutput')
        output_node.location = (200, 0)

        # Connect input straight to output
        node_group.links.new(input_node.outputs[0], output_node.inputs[0])

        # Assign the node group to the modifier
        modifier.node_group = node_group

        return {'FINISHED'}


def add_menu_entry(self, context):
    self.layout.separator()
    self.layout.operator(
        OBJECT_OT_add_geometry_nodes.bl_idname,
        text="Geometry Nodes",
        icon='GEOMETRY_NODES'
    )


def register():
    bpy.utils.register_class(OBJECT_OT_add_geometry_nodes)
    bpy.types.VIEW3D_MT_add.append(add_menu_entry)


def unregister():
    bpy.types.VIEW3D_MT_add.remove(add_menu_entry)
    bpy.utils.unregister_class(OBJECT_OT_add_geometry_nodes)


if __name__ == "__main__":
    register()
