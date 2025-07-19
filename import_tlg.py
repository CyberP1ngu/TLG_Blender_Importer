bl_info = {
    "name": "The Last Guardian Importer",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "File > Import > The Last Guardian (.bod)",
    "description": "Imports models, skeletons, skinning, and materials from The Last Guardian (.bod files)",
    "warning": "Requires setting the GNF Converter path in Add-on Preferences.",
    "doc_url": "",
    "category": "Import-Export",
}

import bpy
import struct
import os
import glob
import traceback
import subprocess
import math
from mathutils import Matrix, Quaternion, Vector
from bpy.props import (
    StringProperty,
    FloatProperty,
    CollectionProperty,
)
from bpy_extras.io_utils import (
    ImportHelper,
)


# --- Addon Preferences for Converter Path ---
class TLGAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    dds_converter_path: StringProperty(
        name="GNF to DDS Converter .exe",
        description="Path to your '__From_GNF_To_DDS_DXT5__GFDLibrary_.exe' tool",
        subtype='FILE_PATH',
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="The Last Guardian Importer Settings")
        layout.prop(self, "dds_converter_path")


class DataStringRef:
    def __init__(self): self.type, self.name = "", ""


class GeometryBuffer:
    def __init__(
            self): self.type, self.name, self.verts, self.elems = "GeometryBuffer", "", DataStringRef(), DataStringRef()


class Bone:
    def __init__(
            self): self.type, self.name, self.assetName, self.parent, self.rootPosition, self.rootRotation = "Bone", "", "", DataStringRef(), [
                                                                                                                                                  0.0] * 3, [
        1.0, 0.0, 0.0, 0.0]


class Mesh:
    def __init__(self): self.type, self.name, self.extensions = "Mesh", "", []


class RenderExt:
    def __init__(
            self): self.type, self.name, self.baseVertexIndex, self.numVerts, self.baseElemIndex, self.numElems, self.batches = "RenderExt", "", 0, 0, 0, 0, []


class SkinCluster:
    def __init__(self): self.type, self.name, self.boneNames, self.bindPoseMatrices = "SkinCluster", "", [], []


class Skeleton:
    def __init__(self): self.type, self.name, self.bones = "Skeleton", "", []


class SceneRoot:
    def __init__(self): self.type, self.name, self.children, self.geometryBuffer = "SceneRoot", "", [], DataStringRef()


class MaterialDefinition:
    def __init__(
            self): self.type, self.name, self.albedo, self.normal, self.emissive, self.specular = "MaterialDefinition", "", DataStringRef(), DataStringRef(), DataStringRef(), DataStringRef()

class RenderBatch:
    def __init__(self):
        self.type = "RenderBatch"
        self.name = ""
        self.materialDefinition = DataStringRef()
        self.start = 0
        self.numTris = 0


class Texture:
    def __init__(self): self.type, self.name = "Texture", ""


# --- Main Importer Logic ---

class TLGReader:
    def __init__(self, filepath, scale, context):
        self.filepath = filepath
        self.directory = os.path.dirname(filepath)
        self.scale = scale
        self.context = context
        self.file, self.data_strings, self.obj_arr = None, [], []
        self.object_map = {}
        self.texture_base_path = None
        self.material_definitions = {}
        self.loaded_files = set()
        self.base_game_dir = self.find_game_base_dir()
        self.correction_matrix = Matrix.Rotation(math.radians(90.0), 4, 'X')
        self.armature_object = None
        self.variant_material_map = {}



    def find_armature_in_scene(self):
        """Finds the most likely target armature in the scene."""
        # 1. Prioritize the armature created during this import session.
        if self.armature_object:
            return self.armature_object

        # 2. Check for a selected armature
        if self.context.active_object and self.context.active_object.type == 'ARMATURE':
            print("  - Found selected armature in scene.")
            return self.context.active_object

        # 3. Fallback to the first armature found in the scene.
        for obj in self.context.scene.objects:
            if obj.type == 'ARMATURE':
                print("  - Found first available armature in scene (fallback).")
                return obj

        return None

    def find_game_base_dir(self):
        """Find the base GAME directory from the file path"""
        parts = os.path.normpath(self.directory).split(os.sep)
        try:
            game_index = [p.upper() for p in parts].index('GAME')
            return os.sep.join(parts[:game_index + 1])
        except ValueError:
            print("  - WARNING: Could not find GAME directory in path")
            return None

    def read(self):
        try:
            self.parse_file(self.filepath)
            self.load_dependencies()
            self.build_variant_map()
            self.build_blender_scene()
        except Exception as e:
            print(f"An unexpected error occurred during import: {e}")
            traceback.print_exc()
            return {'CANCELLED'}
        print("--- Import Complete ---")
        return {'FINISHED'}

    def get_base_name(self, name):
        """Consistently strips variant suffixes to get a clean base name."""
        suffixes_to_strip = ["_fresnelShape", "_furShape", "_fresnel", "_fur"]
        for suffix in suffixes_to_strip:
            if suffix in name:
                return name.split(suffix)[0]
        return name

    def build_variant_map(self):
        """
        Parses mesh variants ('_fresnel', '_fur') and maps their material
        definitions to the corresponding base mesh name for easy lookup.
        """
        print("\n--- Building Variant Material Map ---")
        for obj in self.obj_arr:
            if isinstance(obj, Mesh) and obj.extensions:
                variant_type = None
                if "_fresnel" in obj.name:
                    variant_type = "fresnel"
                elif "_fur" in obj.name:
                    variant_type = "fur"

                if not variant_type:
                    continue

                base_name = self.get_base_name(obj.name)

                try:
                    ext_obj = self.object_map.get(obj.extensions[0].name)
                    batch_obj = self.object_map.get(ext_obj.batches[0].name)
                    mat_def = self.material_definitions.get(batch_obj.materialDefinition.name)

                    if mat_def:
                        if base_name not in self.variant_material_map:
                            self.variant_material_map[base_name] = {}
                        self.variant_material_map[base_name][variant_type] = mat_def
                        print(f"  - Mapped '{variant_type}' material '{mat_def.name}' to base mesh '{base_name}'")
                except (IndexError, KeyError, AttributeError):
                    print(f"  - WARNING: Could not trace material for variant '{obj.name}'")

    def parse_file(self, filepath):
        abs_path = os.path.abspath(filepath)
        if abs_path in self.loaded_files:
            print(f"  - Skipping already loaded file: {os.path.basename(filepath)}")
            return

        print(f"Parsing file: {os.path.basename(filepath)}")
        self.loaded_files.add(abs_path)

        try:
            with open(filepath, 'rb') as f:
                self.file = f
                header = self.read_long(7)
                if not header:
                    print("  - Failed to read header")
                    return

                _, _, data_offset, string_buffer_offset, _, _, data_count = header
                self.file.seek(string_buffer_offset)
                string_count = self.read_long()
                if string_count is None:
                    print("  - Failed to read string count")
                    return

                # Read data strings
                self.data_strings = []
                for i in range(string_count):
                    str_len = self.read_long()
                    if str_len is None:
                        print(f"  - Failed to read string length at index {i}")
                        continue
                    self.data_strings.append(self.read_fixed_string(str_len))

                # Parse objects
                self.file.seek(data_offset)
                for i in range(data_count):
                    self.parse_object_block()
        except Exception as e:
            print(f"  - ERROR parsing file {filepath}: {e}")

    def load_dependencies(self):
        print("\n--- Loading dependencies ---")
        # 1. Load files in the same directory
        same_dir_files = glob.glob(os.path.join(self.directory, "*.bod"))
        for file_path in same_dir_files:
            if os.path.abspath(file_path) not in self.loaded_files:
                self.parse_file(file_path)

        # 2. Load material files from the MATERIALS directory
        if self.base_game_dir:
            materials_dir = os.path.join(self.base_game_dir, "MATERIALS")
            if os.path.exists(materials_dir):
                print(f"  - Searching for materials in: {materials_dir}")
                material_files = glob.glob(os.path.join(materials_dir, "**", "*.bod"), recursive=True)

                for file_path in material_files:
                    if os.path.abspath(file_path) not in self.loaded_files:
                        self.parse_file(file_path)
            else:
                print(f"  - WARNING: Materials directory not found: {materials_dir}")
        else:
            print("  - WARNING: Base game directory not found, skipping MATERIALS search")

        # Cache MaterialDefinitions for faster access
        for obj in self.obj_arr:
            if isinstance(obj, MaterialDefinition):
                self.material_definitions[obj.name] = obj
                print(f"  - Cached MaterialDefinition: {obj.name}")

    def parse_object_block(self):
        try:
            obj_type_index = self.read_long()
            if obj_type_index is None: return
            obj_type_str = self.data_strings[obj_type_index]

            obj_name_index = self.read_long()
            if obj_name_index is None: return
            obj_name_str = self.data_strings[obj_name_index]

            _ = self.read_long()  # Unknown value

            obj = self.get_obj_struct(obj_type_str)
            obj.name = obj_name_str

            while True:
                data_string_index = self.read_long()
                if data_string_index == -1:
                    break

                prop_type = self.data_strings[data_string_index]
                prop_length = self.read_long()
                prop_end = self.file.tell() + prop_length

                try:
                    # Handle different property types
                    if prop_type in ["parent", "geometryBuffer", "verts", "elems",
                                     "albedo", "normal", "emissive", "materialDefinition",
                                     "specular"]:
                        ref = DataStringRef()
                        ref_type_index = self.read_long()
                        if ref_type_index is None: continue
                        ref.type = self.data_strings[ref_type_index]

                        ref_name_index = self.read_long()
                        if ref_name_index is None: continue
                        ref.name = self.data_strings[ref_name_index]
                        setattr(obj, prop_type, ref)

                    elif prop_type in ["children", "extensions", "batches"]:
                        count = self.read_long()
                        prop_list = []
                        for _ in range(count):
                            item = DataStringRef()

                            item_type_index = self.read_long()
                            if item_type_index is None: continue
                            item.type = self.data_strings[item_type_index]

                            item_name_index = self.read_long()
                            if item_name_index is None: continue
                            item.name = self.data_strings[item_name_index]

                            prop_list.append(item)
                        setattr(obj, prop_type, prop_list)

                    elif prop_type == "bones":
                        count = self.read_long()
                        bones = []
                        for _ in range(count):
                            self.read_long()  # Unknown value
                            bone_name_index = self.read_long()
                            if bone_name_index is None: continue
                            bones.append(self.data_strings[bone_name_index])
                        setattr(obj, prop_type, bones)

                    elif prop_type == "boneNames":
                        count = self.read_long()
                        names = []
                        for _ in range(count):
                            name_index = self.read_long()
                            if name_index is None: continue
                            names.append(self.data_strings[name_index])
                        setattr(obj, prop_type, names)

                    elif prop_type == "bindPoseMatrices":
                        count = self.read_long()
                        matrices = []
                        for _ in range(count):
                            matrices.append(self.read_float(16))
                        setattr(obj, prop_type, matrices)

                    elif prop_type in ["baseVertexIndex", "numVerts",
                                       "baseElemIndex", "numElems"]:
                        value = self.read_long()
                        if value is not None:
                            setattr(obj, prop_type, value)

                    elif prop_type == "assetName":
                        asset_name_index = self.read_long()
                        if asset_name_index is None: continue
                        setattr(obj, prop_type, self.data_strings[asset_name_index])

                    elif prop_type == "rootPosition":
                        position = self.read_float(3)
                        self.read_float()  # Skip w component
                        setattr(obj, "rootPosition", position)

                    elif prop_type == "rootRotation":
                        rotation = self.read_float(4)
                        setattr(obj, "rootRotation", rotation)

                    if prop_type == "start":
                        setattr(obj, prop_type, self.read_long())

                    elif prop_type == "numTris":
                        setattr(obj, prop_type, self.read_long())

                except Exception as e:
                    print(f"    - Error parsing property {prop_type}: {e}")
                    traceback.print_exc()

                self.file.seek(prop_end)

            self.obj_arr.append(obj)
            self.object_map[obj.name] = obj

        except Exception as e:
            print(f"  - ERROR parsing object block: {e}")
            traceback.print_exc()

            self.file.seek(prop_end)

        self.obj_arr.append(obj)
        self.object_map[obj.name] = obj

    def build_blender_scene(self):
        scene_root = next((o for o in self.obj_arr if isinstance(o, SceneRoot)), None)
        if not scene_root:
            print("  - ERROR: Could not find SceneRoot object.")
            return

        self.texture_base_path = self.find_texture_path()
        vert_buffer, uv_buffer, face_buffer = None, None, None

        if scene_root.geometryBuffer.name:
            gbuf = self.object_map.get(scene_root.geometryBuffer.name)
            if gbuf:
                vert_path = os.path.join(self.directory, gbuf.verts.name.split('/')[-1] + ".data")
                elem_path = os.path.join(self.directory, gbuf.elems.name.split('/')[-1] + ".data")
                geom_data = self.get_data_buffer(vert_path, "GEOMETRY")
                elem_data = self.get_data_buffer(elem_path, "ELEMS")
                if geom_data: vert_buffer, uv_buffer = geom_data.get("verts"), geom_data.get("uvs")
                if elem_data: face_buffer = elem_data.get("faces")

        # Process all children in a single pass
        for child_ref in scene_root.children:
            obj = self.object_map.get(child_ref.name)
            if not obj: continue

            if isinstance(obj, Skeleton):
                self.build_skeleton(obj)
            elif isinstance(obj, Mesh) and obj.extensions:
                if '_fresnel' in obj.name or '_fur' in obj.name:
                    continue  # Skip variants

                render_ext_obj = self.object_map.get(obj.extensions[0].name)
                if not render_ext_obj: continue

                if vert_buffer and face_buffer:
                    self.build_meshes(render_ext_obj, vert_buffer, face_buffer, uv_buffer)

    def setup_mesh_object(self, blender_obj, render_ext_obj):
        self.apply_skinning_data(blender_obj, render_ext_obj)
        self.apply_material_data(blender_obj, render_ext_obj)

    def build_meshes(self, render_ext_obj, global_vert_buffer, global_face_buffer, global_uv_buffer):
        start_v, num_verts = render_ext_obj.baseVertexIndex, render_ext_obj.numVerts
        if start_v + num_verts > len(global_vert_buffer) or num_verts == 0: return []
        mesh_face_start = render_ext_obj.baseElemIndex // 3
        num_faces = render_ext_obj.numElems // 3
        mesh_face_end = mesh_face_start + num_faces
        if mesh_face_end > len(global_face_buffer) or num_faces == 0: return []

        sub_verts = global_vert_buffer[start_v: start_v + num_verts]
        sub_faces = global_face_buffer[mesh_face_start:mesh_face_end]
        mesh_data = bpy.data.meshes.new(render_ext_obj.name)
        mesh_data.from_pydata(sub_verts, [], sub_faces)

        if global_uv_buffer and len(mesh_data.vertices) == len(sub_verts):
            sub_uvs = global_uv_buffer[start_v: start_v + num_verts]
            uv_layer = mesh_data.uv_layers.new(name="UVMap")
            for p in mesh_data.polygons:
                for l_idx in p.loop_indices:
                    l = mesh_data.loops[l_idx]
                    uv_layer.data[l_idx].uv = (sub_uvs[l.vertex_index][0], 1.0 - sub_uvs[l.vertex_index][1])

        mesh_data.update()
        mesh_data.validate()

        blender_obj = bpy.data.objects.new(render_ext_obj.name, mesh_data)
        blender_obj.matrix_world.identity()

        bpy.context.collection.objects.link(blender_obj)
        blender_obj.data.materials.clear()
        blender_obj.data.shade_smooth()

        armature = self.find_armature_in_scene()
        if armature:
            blender_obj.parent = armature

        for batch_ref in render_ext_obj.batches:
            batch = self.object_map.get(batch_ref.name)
            if not (batch and hasattr(batch, 'materialDefinition')): continue
            mat_def = self.material_definitions.get(batch.materialDefinition.name)
            if not mat_def: continue

            material = self.get_or_create_material(mat_def, render_ext_obj)

            blender_obj.data.materials.append(material)
            material_index = len(blender_obj.data.materials) - 1
            batch_poly_start = (batch.start // 3) - mesh_face_start
            batch_poly_end = batch_poly_start + batch.numTris
            for i in range(batch_poly_start, batch_poly_end):
                if i < len(mesh_data.polygons):
                    mesh_data.polygons[i].material_index = material_index

        self.apply_skinning_data(blender_obj, render_ext_obj)
        return [blender_obj]

    def get_or_create_material(self, mat_def, render_ext_obj):
        material = bpy.data.materials.get(mat_def.name)
        if material: return material

        print(f"--- Creating new Blender material: '{mat_def.name}' ---")
        material = bpy.data.materials.new(name=mat_def.name)
        material.use_nodes = True
        material.node_tree.nodes.clear()
        bsdf = material.node_tree.nodes.new('ShaderNodeBsdfPrincipled')
        output = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
        bsdf.location = (0, 0);
        output.location = (400, 0)
        material.node_tree.links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        bsdf.inputs['Roughness'].default_value = 0.85

        # Standard textures
        if hasattr(mat_def, 'albedo') and mat_def.albedo.name:
            self.create_texture_node(material, mat_def.albedo.name, bsdf.inputs['Base Color'], "Albedo", is_albedo=True)
        if hasattr(mat_def, 'normal') and mat_def.normal.name:
            self.create_texture_node(material, mat_def.normal.name, bsdf.inputs['Normal'], "Normal", is_normal_map=True)
        if hasattr(mat_def, 'emissive') and mat_def.emissive.name:
            self.create_texture_node(material, mat_def.emissive.name, bsdf.inputs['Emission Color'], "Emissive")

        # --- Variant & Special Texture Lookup ---
        specular_input_used = False

        # 1. Check for SP (Specular) texture on the main material first.
        if hasattr(mat_def, 'specular') and mat_def.specular.name:
            print(f"  - SUCCESS: Applying SP texture '{mat_def.specular.name}' to Specular.")
            self.create_texture_node(material, mat_def.specular.name, bsdf.inputs['Specular IOR Level'], "Specular")
            specular_input_used = True

        # Get variant info from the pre-built map
        base_name = self.get_base_name(render_ext_obj.name)
        variant_info = self.variant_material_map.get(base_name)

        if variant_info:
            print(f"  - Found variant info: {list(variant_info.keys())}")

            # 2. Fur (Sheen)
            fur_mat = variant_info.get("fur")
            if fur_mat and hasattr(fur_mat, 'albedo') and fur_mat.albedo.name:
                print(f"  - SUCCESS: Applying mapped Fur texture '{fur_mat.albedo.name}' to Sheen.")
                self.create_texture_node(material, fur_mat.albedo.name, bsdf.inputs['Sheen Tint'], "Sheen")
                bsdf.inputs['Sheen Weight'].default_value = 1.0

            # 3. Fresnel (Specular Tint) - only if SP was not used.
            if not specular_input_used:
                fresnel_mat = variant_info.get("fresnel")
                if fresnel_mat and hasattr(fresnel_mat, 'albedo') and fresnel_mat.albedo.name:
                    print(f"  - SUCCESS: Applying mapped Fresnel texture '{fresnel_mat.albedo.name}' to Specular Tint.")
                    self.create_texture_node(material, fresnel_mat.albedo.name, bsdf.inputs['Specular Tint'],
                                             "Specular Tint")
            elif "fresnel" in variant_info:
                print("  - INFO: Skipping Fresnel texture because an SP texture was already applied.")

        # Backlight Search
        print("\n--- Checking for Backlight Textures ---")
        mat_filename = mat_def.name.split('/')[-1]
        search_key = "_".join(mat_filename.split('_')[1:3])
        if self.texture_base_path and os.path.exists(self.texture_base_path):
            candidates = [f for f in os.listdir(self.texture_base_path) if
                          search_key.lower() in f.lower() and "backlightmap" in f.lower() and f.lower().endswith(
                              ".gnf")]
            if candidates:
                preferred = [c for c in candidates if "_bc7" not in c.lower()]
                chosen_file = preferred[0] if preferred else candidates[0]
                print(f"    - SUCCESS: Found Backlight Map -> '{chosen_file}'")
                self.create_texture_node(material, os.path.splitext(chosen_file)[0], bsdf.inputs['Subsurface Weight'],
                                         "Subsurface")
                bsdf.inputs['Subsurface Radius'].default_value = (11.0, 1.0, 1.0)

        return material

    def convert_gnf_to_dds(self, gnf_path):
        prefs = bpy.context.preferences.addons[__name__].preferences
        converter_exe = prefs.dds_converter_path
        if not (converter_exe and os.path.exists(converter_exe)):
            print(f"    - ERROR: GNF to DDS Converter path not set or invalid in Add-on Preferences: '{converter_exe}'")
            return None

        dds_path = os.path.splitext(gnf_path)[0] + '.dds'
        if os.path.exists(dds_path):
            return dds_path

        if not os.path.exists(gnf_path):
            print(f"    - ERROR: Source GNF file not found: {gnf_path}")
            return None

        print(f"    - Converting '{os.path.basename(gnf_path)}' to DDS...")
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            result = subprocess.run(
                [converter_exe, gnf_path],
                capture_output=True,
                text=True,
                startupinfo=startupinfo
            )

            if result.returncode == 0 and os.path.exists(dds_path):
                print("      -> Conversion successful.")
                return dds_path
            else:
                print(f"      -> ERROR: Conversion failed with return code {result.returncode}")
                print(f"      -> stdout: {result.stdout}")
                print(f"      -> stderr: {result.stderr}")
                return None
        except Exception as e:
            print(f"      -> GNF to DDS conversion failed: {e}")
            return None

    def convert_normal_map_alternative(self, gnf_path, png_path):
        """Alternative method to convert normal maps using Blender's built-in image handling"""
        try:
            # Try loading directly as image
            img = bpy.data.images.load(gnf_path, check_existing=True)
            img.filepath_raw = png_path
            img.save()
            print(f"      -> Saved alternative PNG: {png_path}")
            return png_path
        except:
            print("      -> Alternative conversion failed")
            return None

    def apply_skinning_data(self, blender_obj, render_ext_obj):
        skin_cluster = next((obj for obj in self.object_map.values() if
                             isinstance(obj, SkinCluster) and obj.name.endswith(render_ext_obj.name)), None)
        if not skin_cluster: return

        armature_obj = self.find_armature_in_scene()
        if not armature_obj:
            print(f"  - Armature not found for skinning mesh {blender_obj.name}, skipping.")
            return

        # Do not create a new modifier if one already exists
        if any(mod.type == 'ARMATURE' for mod in blender_obj.modifiers):
            # Ensure the existing modifier points to the correct armature
            for mod in blender_obj.modifiers:
                if mod.type == 'ARMATURE':
                    mod.object = armature_obj
        else:
            mod = blender_obj.modifiers.new(name="Armature", type='ARMATURE')
            mod.object = armature_obj

        if skin_cluster.bindPoseMatrices:
            bone_matrix_map = {name: Matrix(list(zip(*[iter(skin_cluster.bindPoseMatrices[i])] * 4))) for i, name in
                               enumerate(skin_cluster.boneNames)}
            bpy.context.view_layer.objects.active = armature_obj
            bpy.ops.object.mode_set(mode='POSE')
            arm_inv_world = armature_obj.matrix_world.inverted()
            for pose_bone in armature_obj.pose.bones:
                if pose_bone.name not in bone_matrix_map: continue
                inv_world_bind_matrix = bone_matrix_map[pose_bone.name]
                world_bind_matrix = inv_world_bind_matrix.inverted()
                if pose_bone.parent and pose_bone.parent.name in bone_matrix_map:
                    parent_world_bind_matrix = bone_matrix_map[pose_bone.parent.name].inverted()
                    pose_bone.matrix = parent_world_bind_matrix.inverted() @ world_bind_matrix
                else:
                    pose_bone.matrix = arm_inv_world @ world_bind_matrix
            bpy.ops.object.mode_set(mode='OBJECT')
            for pose_bone in armature_obj.pose.bones: pose_bone.matrix_basis.identity()

        for name in skin_cluster.boneNames:
            if name not in blender_obj.vertex_groups: blender_obj.vertex_groups.new(name=name)
        search_pattern = os.path.join(self.directory, f"*_{render_ext_obj.name}.weights")
        weights_filepaths = glob.glob(search_pattern)
        if weights_filepaths: self.parse_and_apply_weights(blender_obj, render_ext_obj, weights_filepaths[0],
                                                           skin_cluster.boneNames)

    def parse_and_apply_weights(self, blender_obj, render_ext_obj, weights_filepath, bone_names_map):
        with open(weights_filepath, 'rb') as f:
            f.seek(16)
            vert_data_struct = struct.Struct('<4I4f')
            for i in range(render_ext_obj.numVerts):
                if f.tell() + vert_data_struct.size > os.fstat(f.fileno()).st_size: break
                unpacked_data = vert_data_struct.unpack(f.read(vert_data_struct.size))
                indices, weights = unpacked_data[:4], unpacked_data[4:]
                for j in range(4):
                    if weights[j] > 1e-5 and indices[j] < len(bone_names_map):
                        vgroup = blender_obj.vertex_groups.get(bone_names_map[indices[j]])
                        if vgroup: vgroup.add([i], weights[j], 'ADD')

    def apply_material_data(self, blender_obj, render_ext_obj):
        print(f"\n--- Applying material for '{blender_obj.name}' ---")
        if not render_ext_obj.batches:
            print("  - INFO: No batches found on RenderExt. Skipping material assignment.")
            return

        print(
            f"  - Found {len(render_ext_obj.batches)} batch(es). Using the first one: '{render_ext_obj.batches[0].name}'")
        batch_ref = render_ext_obj.batches[0]
        batch = self.object_map.get(batch_ref.name)
        if not batch:
            print(f"  - ERROR: Could not find RenderBatch object '{batch_ref.name}' in the parsed object map.")
            return

        print(f"  - Found RenderBatch. Checking for its MaterialDefinition: '{batch.materialDefinition.name}'")
        mat_def = self.object_map.get(batch.materialDefinition.name)
        if not mat_def:
            print(
                f"  - ERROR: Could not find MaterialDefinition object '{batch.materialDefinition.name}' in the parsed object map.")
            return

        print(f"  - Found MaterialDefinition: '{mat_def.name}'")
        material = bpy.data.materials.get(mat_def.name)
        if not material:
            print(f"  - Creating new Blender material: '{mat_def.name}'")
            material = bpy.data.materials.new(name=mat_def.name)
            material.use_nodes = True
            bsdf = material.node_tree.nodes.get('Principled BSDF')
            if not bsdf:
                print("  - ERROR: Could not find Principled BSDF node in new material.")
                return

            self.create_texture_node(material, mat_def.albedo.name, bsdf.inputs['Base Color'], "Albedo")
            self.create_texture_node(material, mat_def.normal.name, bsdf.inputs['Normal'], "Normal", is_normal_map=True)
            self.create_texture_node(material, mat_def.emissive.name, bsdf.inputs['Emission Color'], "Emissive")

        if blender_obj.data.materials:
            blender_obj.data.materials[0] = material
        else:
            blender_obj.data.materials.append(material)
        print("--- Material setup complete. ---")

    def create_texture_node(self, material, tex_name, link_socket, tex_type, is_normal_map=False, is_albedo=False):
        if not tex_name or tex_name.lower() == "_black_texture": return None
        if not self.texture_base_path: return None

        base_tex_name = tex_name.split('/')[-1]
        gnf_path = os.path.join(self.texture_base_path, base_tex_name + ".GNF")

        dds_path = self.convert_gnf_to_dds(gnf_path)

        if not dds_path:
            print(f"    - Failed to find or convert texture. Skipping node creation for '{tex_name}'.")
            return None

        print(f"    - Creating node for '{os.path.basename(dds_path)}' ({tex_type})")
        tex_image_node = material.node_tree.nodes.new('ShaderNodeTexImage')
        tex_image_node.image = bpy.data.images.load(dds_path, check_existing=True)
        bsdf_node = link_socket.node
        tex_image_node.location = bsdf_node.location.x - 1200, bsdf_node.location.y

        if is_albedo:
            material.node_tree.links.new(tex_image_node.outputs['Color'], link_socket)
            material.node_tree.links.new(tex_image_node.outputs['Alpha'], bsdf_node.inputs['Alpha'])
            if hasattr(material, 'blend_method'): material.blend_method = 'HASHED'
            if hasattr(material, 'shadow_method'): material.shadow_method = 'HASHED'

        elif is_normal_map:
            tex_image_node.image.colorspace_settings.name = 'Non-Color'

            # Create all the required nodes
            sep_color_node = material.node_tree.nodes.new('ShaderNodeSeparateColor')

            # --- Remap R and G from [0, 1] to [-1, 1] for vector math ---
            map_r_node = material.node_tree.nodes.new('ShaderNodeMapRange')
            map_r_node.inputs['From Min'].default_value = 0.0
            map_r_node.inputs['From Max'].default_value = 1.0
            map_r_node.inputs['To Min'].default_value = -1.0
            map_r_node.inputs['To Max'].default_value = 1.0

            map_g_node = material.node_tree.nodes.new('ShaderNodeMapRange')
            map_g_node.inputs['From Min'].default_value = 0.0
            map_g_node.inputs['From Max'].default_value = 1.0
            map_g_node.inputs['To Min'].default_value = -1.0
            map_g_node.inputs['To Max'].default_value = 1.0

            # --- Math nodes to calculate Z = sqrt(1 - X^2 - Y^2) ---
            power_x_node = material.node_tree.nodes.new('ShaderNodeMath')
            power_y_node = material.node_tree.nodes.new('ShaderNodeMath')
            add_node = material.node_tree.nodes.new('ShaderNodeMath')
            subtract_node = material.node_tree.nodes.new('ShaderNodeMath')
            sqrt_node = material.node_tree.nodes.new('ShaderNodeMath')

            power_x_node.operation = 'POWER';
            power_x_node.inputs[1].default_value = 2.0
            power_y_node.operation = 'POWER';
            power_y_node.inputs[1].default_value = 2.0
            add_node.operation = 'ADD'
            subtract_node.operation = 'SUBTRACT';
            subtract_node.inputs[0].default_value = 1.0;
            subtract_node.use_clamp = True
            sqrt_node.operation = 'SQRT'

            # --- Node to recombine R, G, and new B into a final color ---
            comb_color_node = material.node_tree.nodes.new('ShaderNodeCombineColor')

            # Final Normal Map node for strength control
            normal_map_node = material.node_tree.nodes.new('ShaderNodeNormalMap')
            normal_map_node.inputs['Strength'].default_value = 0.0

            # Position nodes
            sep_color_node.location = tex_image_node.location + Vector((200, 0))
            map_r_node.location = sep_color_node.location + Vector((180, 80))
            map_g_node.location = sep_color_node.location + Vector((180, -80))
            power_x_node.location = map_r_node.location + Vector((180, 0))
            power_y_node.location = map_g_node.location + Vector((180, 0))
            add_node.location = power_x_node.location + Vector((180, -40))
            subtract_node.location = add_node.location + Vector((180, 0))
            sqrt_node.location = subtract_node.location + Vector((180, 0))
            comb_color_node.location = sqrt_node.location + Vector((200, 40))
            normal_map_node.location = comb_color_node.location + Vector((200, 0))

            # Link the node chain
            links = material.node_tree.links
            links.new(tex_image_node.outputs['Color'], sep_color_node.inputs['Color'])

            # Remap R and G to vector space [-1, 1]
            links.new(sep_color_node.outputs['Red'], map_r_node.inputs['Value'])
            links.new(sep_color_node.outputs['Green'], map_g_node.inputs['Value'])

            # Calculate X^2 and Y^2
            links.new(map_r_node.outputs['Result'], power_x_node.inputs[0])
            links.new(map_g_node.outputs['Result'], power_y_node.inputs[0])

            # Calculate X^2 + Y^2
            links.new(power_x_node.outputs['Value'], add_node.inputs[0])
            links.new(power_y_node.outputs['Value'], add_node.inputs[1])

            # Calculate 1 - (X^2 + Y^2)
            links.new(add_node.outputs['Value'], subtract_node.inputs[1])

            # Calculate Z = sqrt(...)
            links.new(subtract_node.outputs['Value'], sqrt_node.inputs[0])

            # Combine original R, G, and the reconstructed B (Z)
            links.new(sep_color_node.outputs['Red'], comb_color_node.inputs['Red'])
            links.new(sep_color_node.outputs['Green'], comb_color_node.inputs['Green'])
            links.new(sqrt_node.outputs['Value'], comb_color_node.inputs['Blue'])

            # Final connection to the shader
            links.new(comb_color_node.outputs['Color'], normal_map_node.inputs['Color'])
            links.new(normal_map_node.outputs['Normal'], link_socket)

        elif tex_type == "Subsurface":
            sep_node = material.node_tree.nodes.new('ShaderNodeSeparateColor')
            sep_node.location = bsdf_node.location.x - 150, bsdf_node.location.y - 200
            material.node_tree.links.new(tex_image_node.outputs['Color'], sep_node.inputs['Color'])
            material.node_tree.links.new(sep_node.outputs['Red'], link_socket)
        else:
            material.node_tree.links.new(tex_image_node.outputs['Color'], link_socket)

    def find_texture_path(self):
        print("--- Searching for texture directory ---")
        try:
            parts = os.path.normpath(self.directory).split(os.sep)
            game_index = [p.upper() for p in parts].index('GAME')
            game_dir = os.sep.join(parts[:game_index + 1])
            asset_parts = parts[game_index + 2:]

            # Handle path structures like ASSETS/CHARA/SKIN/BOYA -> TEXTURES/CHARA/BOYA
            if len(asset_parts) > 2 and asset_parts[1].upper() == 'SKIN':
                path = os.path.join(game_dir, 'TEXTURES', asset_parts[0], asset_parts[2])
                print(f"  - Found character texture path: {path}")
                return path
            else:  # Handle simpler paths like ASSETS/PROPS/BARREL -> TEXTURES/PROPS/BARREL
                path = os.path.join(game_dir, 'TEXTURES', *asset_parts)
                print(f"  - Found generic texture path: {path}")
                return path
        except:
            print("  - ERROR: Could not determine texture directory from model path.")
            return None

    def build_skeleton(self, skel_obj):
        if not skel_obj.bones: return
        if bpy.context.active_object and bpy.context.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
        arm_data = bpy.data.armatures.new(skel_obj.name)
        arm_obj = bpy.data.objects.new(skel_obj.name, arm_data)
        bpy.context.collection.objects.link(arm_obj)
        arm_obj.matrix_world = self.correction_matrix.copy()
        bpy.context.view_layer.objects.active = arm_obj
        bpy.ops.object.mode_set(mode='EDIT')
        created_bones = {}
        bone_data_map = {}

        for bone_ref in skel_obj.bones:
            bone_data = self.object_map.get(bone_ref)
            if bone_data and hasattr(bone_data, 'assetName') and bone_data.assetName:
                edit_bone = arm_data.edit_bones.new(bone_data.assetName)
                created_bones[bone_ref] = edit_bone
                bone_data_map[bone_ref] = bone_data

        for bone_ref, edit_bone in created_bones.items():
            bone_data = bone_data_map[bone_ref]
            parent_ref = bone_data.parent.name
            if parent_ref in created_bones and edit_bone != created_bones[parent_ref]:
                edit_bone.parent = created_bones[parent_ref]
            pos = Vector(bone_data.rootPosition) * self.scale
            q_xyzw = bone_data.rootRotation
            rot = Quaternion((q_xyzw[3], q_xyzw[0], q_xyzw[1], q_xyzw[2]))
            y_axis = rot @ Vector((0.0, 1.0, 0.0))
            z_axis = rot @ Vector((0.0, 0.0, 1.0))
            edit_bone.head = pos
            edit_bone.tail = edit_bone.head + y_axis * max(0.01 * self.scale, 0.01)
            edit_bone.align_roll(z_axis)

        bpy.ops.object.mode_set(mode='OBJECT')
        self.armature_object = arm_obj

    def get_data_buffer(self, path, buffer_type):
        if not os.path.exists(path):
            print(f"  - WARNING: Data buffer file not found: {path}")
            return None

        try:
            with open(path, 'rb') as f:
                header = f.read(16)
                if len(header) < 16:
                    print("  - WARNING: Incomplete header in data buffer")
                    return None

                cdat, _, _, stride, length = struct.unpack('<4shhii', header)
                if cdat != b'CDAT':
                    print("  - WARNING: Invalid CDAT header in data buffer")
                    return None

                if buffer_type == "GEOMETRY" and stride == 0x20:
                    verts, uvs = [], []

                    # Read vertex data
                    for _ in range(length // stride):
                        # Position (3 floats)
                        vx = struct.unpack('<f', f.read(4))[0]
                        vy = struct.unpack('<f', f.read(4))[0]
                        vz = struct.unpack('<f', f.read(4))[0]

                        # Skip normal and padding (4 bytes normal + 8 bytes padding)
                        f.read(12)

                        # UV coordinates (2 floats)
                        u = struct.unpack('<f', f.read(4))[0]
                        v = struct.unpack('<f', f.read(4))[0]

                        verts.append((vx * self.scale, vy * self.scale, vz * self.scale))
                        uvs.append((u, v))

                    return {"verts": verts, "uvs": uvs}

                elif buffer_type == "ELEMS" and stride == 0x02:
                    faces = []
                    # Calculate number of faces
                    num_faces = length // (stride * 3)

                    # Read triangle indices
                    for _ in range(num_faces):
                        fa = struct.unpack('<H', f.read(2))[0]
                        fb = struct.unpack('<H', f.read(2))[0]
                        fc = struct.unpack('<H', f.read(2))[0]
                        # Reverse winding order for Blender
                        faces.append((fa, fc, fb))

                    return {"faces": faces}

        except Exception as e:
            print(f"  - ERROR reading data buffer: {e}")
            traceback.print_exc()

        return None

    def read_long(self, count=1):
        try:
            fmt, size = f'<{count}i', 4 * count;
            data = self.file.read(size)
            if len(data) < size: return None if count == 1 else []
            res = struct.unpack(fmt, data);
            return res[0] if count == 1 else list(res)
        except:
            return None if count == 1 else []

    def read_float(self, count=1):
        try:
            fmt, size = f'<{count}f', 4 * count;
            data = self.file.read(size)
            if len(data) < size: return 0.0 if count == 1 else [0.0] * count
            res = struct.unpack(fmt, data);
            return res[0] if count == 1 else list(res)
        except:
            return 0.0 if count == 1 else [0.0] * count

    def read_fixed_string(self, length):
        if length <= 0: return ""
        return self.file.read(length).split(b'\x00', 1)[0].decode('utf-8', 'ignore')

    def get_obj_struct(self, obj_type):
        cls_map = {"SceneRoot": SceneRoot, "Skeleton": Skeleton, "Bone": Bone, "Mesh": Mesh, "RenderExt": RenderExt,
                   "SkinCluster": SkinCluster, "GeometryBuffer": GeometryBuffer,
                   "MaterialDefinition": MaterialDefinition,
                   "RenderBatch": RenderBatch, "Texture": Texture}
        return cls_map.get(obj_type, type(obj_type, (object,), {"name": "", "type": obj_type}))()


# --- Blender UI and Registration ---
class ImportTLGAnim(bpy.types.Operator, ImportHelper):
    """Import an animation from The Last Guardian (.DATA)"""
    bl_idname = "import_scene.tlg_anim"
    bl_label = "Import TLG Animation"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext: StringProperty(default="*.data", options={'HIDDEN'})
    filter_glob: StringProperty(default="*.data", options={'HIDDEN'})

    scale: FloatProperty(
        name="Scale",
        description="Global scale for the animation (should match model import scale)",
        default=1.0,
    )

    def execute(self, context):
        armature = self.find_armature(context)
        if not armature:
            self.report({'ERROR'}, "No armature selected or found. Please select the target armature.")
            return {'CANCELLED'}

        try:
            reader = TLGAnimReader(self.filepath, armature, context, self.scale)
            return reader.read()
        except Exception as e:
            self.report({'ERROR'}, f"Failed to import animation: {e}. See console for details.")
            traceback.print_exc()
            return {'CANCELLED'}

    def find_armature(self, context):
        if context.active_object and context.active_object.type == 'ARMATURE':
            return context.active_object
        for obj in context.scene.objects:
            if obj.type == 'ARMATURE':
                return obj
        return None


class TLGAnimReader:
    """
    Final version of the TLG .DATA animation parser.
    Correctly calculates pose transforms relative to the parent bone's space.
    """

    # World-space correction for the root bone
    ROOT_CORRECTION_QUAT = Quaternion((math.sqrt(0.5), -math.sqrt(0.5), 0, 0))

    def __init__(self, filepath, armature, context, scale):
        self.filepath = filepath
        self.armature = armature
        self.context = context
        self.global_scale = scale
        self.raw_data = None
        self.header = {}
        self.info_table = []
        self.animation_data_cache = {}

    def read(self):
        """Main entry point to read and apply the animation."""
        print("--- Starting Animation Import ---")
        with open(self.filepath, 'rb') as f:
            self.raw_data = f.read()

        self.read_header()
        self.read_info_table_and_cache_data()

        action = bpy.data.actions.new(name=os.path.splitext(os.path.basename(self.filepath))[0])
        if not self.armature.animation_data:
            self.armature.animation_data_create()
        self.armature.animation_data.action = action

        self.apply_animation_to_bones(action)

        print("\n--- Animation import finished. ---")
        return {'FINISHED'}

    def read_header(self):
        """Reads the 48-byte file header."""
        header_format = '<4s12xIfII'
        magic, frame_rate, _, info_table_entries, frame_count = struct.unpack(
            header_format, self.raw_data[:struct.calcsize(header_format)]
        )
        self.header = {
            'magic': magic.decode('ascii', 'ignore'),
            'info_table_entries': info_table_entries,
            'frame_count': frame_count,
            'frame_rate': frame_rate or 30
        }
        if self.header['magic'] != 'CDAT':
            raise ValueError(f"Invalid magic number: {self.header['magic']}.")
        print(
            f"Parsing Animation: {self.header['info_table_entries']} Tracks, "
            f"{self.header['frame_count']} Frames @ {self.header['frame_rate']}fps"
        )

    def read_info_table_and_cache_data(self):
        """Reads track metadata and caches the raw animation data for all tracks."""
        info_table_start = 0x30
        offset_fix = 16
        num_frames = self.header['frame_count']
        for i in range(self.header['info_table_entries']):
            entry_start = info_table_start + (i * 32)
            flag, ptr_trans, ptr_rot, ptr_scale, bone_name_ptr = struct.unpack('<IIIII', self.raw_data[
                                                                                         entry_start: entry_start + 20])
            bone_name_start = bone_name_ptr + offset_fix
            bone_name = self.raw_data[bone_name_start:].split(b'\0')[0].decode('ascii', 'ignore')
            self.info_table.append({'name': bone_name, 'flag': flag})
            trans_keys = num_frames if flag in [5, 4, 0] else 1
            rot_keys = num_frames if flag in [6, 4, 0] else 1
            scale_keys = num_frames if flag in [0, 3] else 1
            self.animation_data_cache[bone_name] = {
                'T': self._unpack_data(ptr_trans + offset_fix, trans_keys, 'vec3'),
                'R': self._unpack_data(ptr_rot + offset_fix, rot_keys, 'quat'),
                'S': self._unpack_data(ptr_scale + offset_fix, scale_keys, 'vec3')
            }

    def _unpack_data(self, start_offset, num_keys, data_type):
        """Unpacks a raw block of animation data."""
        values = []
        data_block = self.raw_data[start_offset: start_offset + (12 * num_keys)]
        for i in range(num_keys):
            chunk = data_block[i * 12: (i + 1) * 12]
            try:
                x, y, z = struct.unpack('<fff', chunk)
                if data_type == 'vec3':
                    values.append(Vector((x, y, z)))
                elif data_type == 'quat':
                    mag_sq = x * x + y * y + z * z
                    w = math.sqrt(1.0 - min(1.0, mag_sq)) if mag_sq <= 1.001 else 0.0
                    values.append(Quaternion((w, x, y, z)))
            except struct.error:
                if data_type == 'vec3':
                    values.append(Vector((0, 0, 0)))
                else:
                    values.append(Quaternion((1, 0, 0, 0)))
        return values

    def apply_animation_to_bones(self, action):
        """Iterates through frames and bones to calculate and apply local keyframes."""
        if self.armature.mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')

        num_frames = self.header['frame_count']

        fcurves = {}
        for track_info in self.info_table:
            bone_name = track_info['name']
            if self.armature.pose.bones.get(bone_name):
                fcurves[bone_name] = {
                    'location': [action.fcurves.new(data_path=f'pose.bones["{bone_name}"].location', index=i) for i in
                                 range(3)],
                    'rotation': [action.fcurves.new(data_path=f'pose.bones["{bone_name}"].rotation_quaternion', index=i)
                                 for i in range(4)],
                    'scale': [action.fcurves.new(data_path=f'pose.bones["{bone_name}"].scale', index=i) for i in
                              range(3)]
                }

        for frame_idx in range(num_frames):
            for track_info in self.info_table:
                bone_name = track_info['name']
                pose_bone = self.armature.pose.bones.get(bone_name)
                if not pose_bone:
                    continue

                track_data = self.animation_data_cache[bone_name]

                t_idx = frame_idx if len(track_data['T']) > 1 else 0
                r_idx = frame_idx if len(track_data['R']) > 1 else 0
                s_idx = frame_idx if len(track_data['S']) > 1 else 0

                loc_data = track_data['T'][t_idx] * self.global_scale
                rot_data = track_data['R'][r_idx]
                scl_data = track_data['S'][s_idx]

                if not pose_bone.parent:
                    mat_parent_rest_inv = pose_bone.bone.matrix_local.inverted()
                else:
                    mat_parent_rest_inv = pose_bone.parent.bone.matrix_local.inverted()

                mat_anim_local = Matrix.Translation(loc_data) @ rot_data.to_matrix().to_4x4()

                # Calculate the bone's true rest pose relative to its parent.

                mat_rest_local = mat_parent_rest_inv @ pose_bone.bone.matrix_local

                # Calculate the final pose matrix by "removing" the rest pose from the target animation pose.
                # This gives the delta transform that Blender needs for its pose bones.
                mat_pose_delta = mat_rest_local.inverted() @ mat_anim_local

                key_loc, key_rot, _ = mat_pose_delta.decompose()

                frame_float = float(frame_idx + 1)
                for i in range(3):
                    fcurves[bone_name]['location'][i].keyframe_points.insert(frame_float, key_loc[i])
                for i in range(4):
                    fcurves[bone_name]['rotation'][i].keyframe_points.insert(frame_float, key_rot[i])
                for i in range(3):
                    fcurves[bone_name]['scale'][i].keyframe_points.insert(frame_float, scl_data[i])

        for bone_curves in fcurves.values():
            for fcurve_list in bone_curves.values():
                for fcurve in fcurve_list:
                    fcurve.update()

        bpy.context.scene.frame_end = num_frames
        bpy.context.scene.render.fps = self.header['frame_rate']
        bpy.ops.object.mode_set(mode='OBJECT')


class ImportTLG(bpy.types.Operator, ImportHelper):
    """Import a model from The Last Guardian (.bod)"""
    bl_idname = "import_scene.tlg"
    bl_label = "Import TLG Model"
    bl_options = {'PRESET', 'UNDO'}
    filename_ext: StringProperty(default="*.bod", options={'HIDDEN'})
    filter_glob: StringProperty(default="*.bod", options={'HIDDEN'})
    scale: FloatProperty(name="Scale", default=1.0)
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    directory: StringProperty(options={'HIDDEN'})

    def execute(self, context):
        for path in [os.path.join(self.directory, f.name) for f in self.files] or [self.filepath]:
            if not os.path.exists(path):
                self.report({'ERROR'}, f"File not found: {path}")
                continue
            if TLGReader(path, self.scale, context).read() == {'CANCELLED'}:
                self.report({'ERROR'}, f"Failed to import {os.path.basename(path)}. Check Console for details.")
                return {'CANCELLED'}
        return {'FINISHED'}


def menu_func_import(self, context):
    self.layout.operator(ImportTLG.bl_idname, text="The Last Guardian (.bod)")
    self.layout.operator(ImportTLGAnim.bl_idname, text="The Last Guardian Animation (.data)")


classes_to_register = (
    ImportTLG,
    ImportTLGAnim,
    TLGAddonPreferences,
)


def register():
    for cls in classes_to_register: bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes_to_register): bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
