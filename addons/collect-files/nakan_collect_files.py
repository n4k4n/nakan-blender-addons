bl_info = {
    "name": "Nakan Collect Files",
    "author": "Nakan Art",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "File > External Data > Nakan Collect Files",
    "description": "Gathers everything the file needs: linked libraries become local, "
                   "textures, UDIMs, Geometry Nodes bakes and IES/OSL go inside the .blend; VDB, "
                   "Alembic/USD, videos, image sequences and fluid/ocean/physics caches go to "
                   "a <name>_files folder next to it. Unpack does the reverse.",
    "category": "Import-Export",
}

import os
import sys
import shutil
import bpy
from bpy.props import StringProperty, BoolProperty


# --------------------------------------------------------------------------
# collecting external dependencies
# --------------------------------------------------------------------------

# (collection in bpy.data, friendly label, can be packed by Pack Resources)
COLLECTIONS = (
    ("images", "image", True),
    ("sounds", "sound", True),
    ("fonts", "font", True),
    ("volumes", "volume", False),
    ("cache_files", "cache (Alembic/USD)", False),
    ("movieclips", "movie clip", False),
)

# collections swept by Make Local, in the order they usually depend on each other
LOCALIZE = (
    "objects", "meshes", "curves", "hair_curves", "pointclouds", "volumes",
    "metaballs", "lattices", "armatures", "cameras", "lights", "speakers",
    "materials", "node_groups", "images", "textures", "fonts", "sounds",
    "collections", "worlds", "actions", "particles", "grease_pencils",
    "linestyles", "palettes", "brushes", "texts",
)

# shading modes that wake up a render engine in the viewport
LIVE_SHADING = {'RENDERED', 'MATERIAL'}


def _is_real_file_user(db, kind):
    """Skip datablocks that don't point to a file on disk."""
    if kind == "image":
        if db.type != 'IMAGE' or db.source in {'GENERATED', 'VIEWER'}:
            return False
    if kind == "font" and db.filepath in {"<builtin>", ""}:
        return False
    return bool(db.filepath)


def collect_external():
    """List every datablock with an external file that is not packed yet."""
    out = []
    for attr, kind, packable in COLLECTIONS:
        col = getattr(bpy.data, attr, None)
        if col is None:
            continue
        for db in col:
            if getattr(db, "packed_file", None):
                continue
            if not _is_real_file_user(db, kind):
                continue
            out.append({
                "db": db,
                "kind": kind,
                "packable": packable,
                "raw": db.filepath,
                "lib": getattr(db, "library", None),
                "abs": os.path.normpath(
                    bpy.path.abspath(db.filepath, library=getattr(db, "library", None))
                ),
            })
    return out


def real_libraries():
    """Real libraries (ignores internal markers like <startup.blend>)."""
    return [l for l in bpy.data.libraries if not l.filepath.startswith("<")]


def libs_still_used():
    """Libraries that still provide some data (empty ones vanish on reopen)."""
    usadas = set()
    for attr in LOCALIZE:
        col = getattr(bpy.data, attr, None)
        if col is None:
            continue
        for db in col:
            lib = getattr(db, "library", None)
            if lib is not None:
                usadas.add(lib.filepath)
    return [l for l in real_libraries() if l.filepath in usadas]


# --------------------------------------------------------------------------
# broken collection references
# --------------------------------------------------------------------------
#
# A Collection Info node with Collection = None, whose input does NOT come from above
# and whose output is connected, produces a NULL collection instance. Anything that walks
# those duplis -- Cycles syncing the viewport, or Blender drawing the .blend
# thumbnail on save -- dereferences that pointer and segfaults inside
# BKE_collection_object_cache_get.
#
# We only touch Collection Info. An empty Object Info is common by design in packs
# like Botaniq (the value comes from above on every use) and does not cause this crash --
# touching it would break those assets.

def node_group_users():
    """Map: node group name -> who uses it (another node group or an object)."""
    users = {}
    for ng in bpy.data.node_groups:
        for n in ng.nodes:
            if n.type == 'GROUP' and n.node_tree:
                users.setdefault(n.node_tree.name, set()).add(("NG", ng.name))
    for ob in bpy.data.objects:
        for m in ob.modifiers:
            if m.type == 'NODES' and m.node_group:
                users.setdefault(m.node_group.name, set()).add(
                    ("OBJ", "%s [mod '%s']" % (ob.name, m.name)))
    return users


def objects_using(ng_name, users, visto=None, prof=0):
    """Walk up the chain of node groups until real objects are found."""
    if visto is None:
        visto = set()
    if ng_name in visto or prof > 15:
        return set()
    visto.add(ng_name)
    out = set()
    for kind, who in users.get(ng_name, ()):
        if kind == "OBJ":
            out.add(who)
        else:
            out |= objects_using(who, users, visto, prof + 1)
    return out


def null_collection_nodes():
    """Collection Info nodes that really evaluate with a NULL collection."""
    out = []
    seen = set()

    def walk(ng):
        if ng is None or ng.name in seen:
            return
        seen.add(ng.name)
        for n in ng.nodes:
            if n.bl_idname == 'GeometryNodeCollectionInfo':
                try:
                    sock = n.inputs['Collection']
                    vazio = sock.default_value is None
                    de_cima = sock.is_linked
                except Exception:
                    continue
                if vazio and not de_cima and not n.mute:
                    if any(o.is_linked for o in n.outputs):
                        out.append((ng, n))
            if n.type == 'GROUP':
                walk(n.node_tree)

    for ng in bpy.data.node_groups:
        if getattr(ng, "type", "") == 'GEOMETRY':
            walk(ng)
    return out


# --------------------------------------------------------------------------
# viewport: leave Rendered before touching the data
# --------------------------------------------------------------------------
#
# With the viewport in Rendered, any data change tags the depsgraph and
# forces the engine (Cycles/EEVEE) to resync the scene right away. In scenes with
# nested collection instances this has crashed Blender 5.2 with a segfault
# inside BKE_collection_object_cache_get. So: drop to Solid, let
# Blender redraw (releasing the engine), and only then touch the data.

def live_viewports():
    """Every 3D viewport in Rendered/Material, with its original mode."""
    out = []
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for sp in area.spaces:
                if sp.type != 'VIEW_3D':
                    continue
                try:
                    if sp.shading.type in LIVE_SHADING:
                        out.append((sp, sp.shading.type))
                except Exception:
                    pass
    return out


def set_shading(pairs, mode=None):
    """mode=None restores each viewport to the mode it had."""
    n = 0
    for sp, original in pairs:
        try:
            sp.shading.type = original if mode is None else mode
            n += 1
        except Exception:
            pass
    return n


# --------------------------------------------------------------------------
# VDB (volumes)
# --------------------------------------------------------------------------
#
# Blender lets you pack VDB, but the volume loader only reads from a FILE on
# disk -- never from inside the .blend (tested in 5.2: the packed data is
# intact, but the volume opens empty with "<file>.vdb not found"). So VDB
# always travels in a folder next to the .blend, and the path is rewritten to it.

import re
import glob


def _sequence_files(path):
    """Every file of a sequence (frame number at the end of the name)."""
    pasta, base = os.path.split(path)
    m = re.search(r"(\d+)(?!.*\d)", base)
    if not m:
        return [path] if os.path.exists(path) else []
    padrao = base[:m.start()] + "[0-9]" * len(m.group(1)) + base[m.end():]
    return sorted(glob.glob(os.path.join(glob.escape(pasta), padrao)))


def relocate_volumes(dest, pasta_nome, L):
    """Copy/extract each VDB to dest/pasta_nome and repoint the volume.
    Returns the set of volumes handled."""
    tratados = set()
    feitos = {}  # source -> final name (volumes that point to the same file)
    pasta = os.path.join(dest, pasta_nome)
    total = 0
    for v in bpy.data.volumes:
        if v.library is not None:
            L.append("   VDB '%s' is still linked: it can't be repointed." % v.name)
            continue
        if not v.filepath:
            continue
        src = os.path.normpath(bpy.path.abspath(v.filepath))
        try:
            os.makedirs(pasta, exist_ok=True)
            if v.is_sequence:
                arqs = _sequence_files(src)
                if not arqs:
                    L.append("   VDB '%s': sequence NOT found at %s" % (v.name, src))
                    continue
                for f in arqs:
                    alvo = os.path.join(pasta, os.path.basename(f))
                    if not os.path.exists(alvo):
                        shutil.copy2(f, alvo)
                    total += os.path.getsize(alvo)
                nome = os.path.basename(src)
                L.append("   VDB '%s': sequence of %d files copied" % (v.name, len(arqs)))
            else:
                if src in feitos:
                    nome = feitos[src]
                else:
                    nome = os.path.basename(src) or (v.name + ".vdb")
                    alvo = os.path.join(pasta, nome)
                    if os.path.exists(src):
                        shutil.copy2(src, alvo)
                        origem = "copied from disk"
                    elif v.packed_file:
                        with open(alvo, "wb") as fh:
                            fh.write(v.packed_file.data)
                        origem = "extracted from inside the .blend"
                    else:
                        L.append("   VDB '%s': NOT found and not packed (%s)" % (v.name, src))
                        continue
                    feitos[src] = nome
                    total += os.path.getsize(alvo)
                    L.append("   VDB '%s': %s" % (v.name, origem))
            v.filepath = "//" + pasta_nome + "/" + nome
            tratados.add(v.name)
        except Exception as err:
            L.append("   VDB '%s': ERROR %s" % (v.name, err))
    if tratados:
        L.append("VDB: %d volume(s) pointing to the folder '%s' (%.1f MB)."
                 % (len(tratados), pasta_nome, total / 1048576.0))
        L.append("   Blender can't read VDB from inside the .blend: take the folder with it.")
    return tratados


# --------------------------------------------------------------------------
# what Blender doesn't pack: goes to <name>_files/ next to the .blend
# --------------------------------------------------------------------------
#
# Alembic/USD, videos, image sequences, fluid/ocean caches and VDB
# are only read from DISK. They travel in a <name>_files/ folder next to the .blend,
# with relative paths. Geometry Nodes bakes and IES/OSL go inside the .blend.
# Physics caches on disk (cloth, particles...) go to blendcache_<name>/,
# which is where Blender always looks for them.

def _copiar(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
        shutil.copy2(src, dst)
    return os.path.getsize(dst)


def _copiar_arvore(src_dir, dst_dir):
    n = tot = 0
    for raiz, _d, arqs in os.walk(src_dir):
        rel = os.path.relpath(raiz, src_dir)
        for a in arqs:
            if a in (".DS_Store", "Thumbs.db", "desktop.ini"):
                continue
            tot += _copiar(os.path.join(raiz, a), os.path.normpath(os.path.join(dst_dir, rel, a)))
            n += 1
    return n, tot


def _udim_arquivo(padrao, numero):
    u, v = (numero - 1001) % 10 + 1, (numero - 1001) // 10 + 1
    return padrao.replace("<UDIM>", str(numero)).replace("<UVTILE>", "u%d_v%d" % (u, v))


class _Lado:
    """Copy to <blend_dir>/<stem>_files/<sub>/ without name collisions."""

    def __init__(self, blend_dir, stem):
        self.blend_dir = blend_dir
        self.pasta = stem + "_files"
        self.usados = {}
        self.n = 0
        self.bytes = 0

    def destino(self, sub, nome, origem, eh_pasta=False):
        if eh_pasta:
            base, ext = _nome_seguro(os.path.splitext(nome)[0] if "." in nome else nome), ""
        else:
            base, ext = os.path.splitext(_nome_seguro(nome))
        k = 1
        while True:
            arq = base + ("_%d" % k if k > 1 else "") + ext
            absd = os.path.join(self.blend_dir, self.pasta, sub, arq)
            dono = self.usados.get(absd)
            if dono is None and os.path.exists(absd) and os.path.isfile(absd) and os.path.isfile(origem):
                dono = origem if _hash_arquivo(absd) == _hash_arquivo(origem) else "other"
            if dono is None or dono == origem:
                self.usados[absd] = origem
                return absd, "//" + "/".join((self.pasta, sub, arq))
            k += 1

    def arquivo(self, sub, origem):
        absd, rel = self.destino(sub, os.path.basename(origem), os.path.normpath(origem))
        self.bytes += _copiar(origem, absd)
        self.n += 1
        return rel

    def pasta_inteira(self, sub, nome, origem_dir):
        absd, rel = self.destino(sub, nome, os.path.normpath(origem_dir) + os.sep, True)
        n, b = _copiar_arvore(origem_dir, absd)
        self.n += n
        self.bytes += b
        return rel

    def sequencia(self, sub, nome, primeiro):
        arqs = _sequence_files(primeiro)
        if not arqs:
            return None
        absd, rel = self.destino(sub, nome, os.path.normpath(primeiro) + "|seq", True)
        for f in arqs:
            self.bytes += _copiar(f, os.path.join(absd, os.path.basename(f)))
            self.n += 1
        return rel + "/" + os.path.basename(primeiro)

    def varios(self, sub, nome, arquivos):
        absd, rel = self.destino(sub, nome, "|".join(sorted(arquivos)), True)
        for f in arquivos:
            self.bytes += _copiar(f, os.path.join(absd, os.path.basename(f)))
            self.n += 1
        return rel


def _point_caches():
    for ob in bpy.data.objects:
        if ob.library is not None:
            continue
        for md in ob.modifiers:
            pc = getattr(md, "point_cache", None)
            if pc is not None:
                yield ob.name, pc
            if md.type == 'DYNAMIC_PAINT' and getattr(md, "canvas_settings", None):
                for s in md.canvas_settings.canvas_surfaces:
                    yield ob.name, s.point_cache
        for ps in ob.particle_systems:
            yield ob.name, ps.point_cache
    for sc in bpy.data.scenes:
        rb = sc.rigidbody_world
        if rb is not None and rb.point_cache is not None:
            yield sc.name, rb.point_cache


def _arvores_de_nodes():
    for attr in ("materials", "worlds", "lights", "node_groups", "linestyles"):
        for idb in getattr(bpy.data, attr, []):
            if idb.library is not None:
                continue
            nt = idb if attr == "node_groups" else getattr(idb, "node_tree", None)
            if nt is not None:
                yield nt


def _strips(sc):
    se = sc.sequence_editor
    if se is None:
        return []
    return list(se.strips_all if hasattr(se, "strips_all") else se.sequences_all)


def coletar_ao_lado(blend_dir, stem, L):
    """Bring along everything Blender doesn't pack. Call BEFORE saving the
    new .blend (// paths still resolve from the original file)."""
    lado = _Lado(blend_dir, stem)
    feitos, problemas = [], []
    origem_dir = os.path.dirname(bpy.data.filepath)
    origem_stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    ab = lambda p: os.path.normpath(bpy.path.abspath(p))

    # 1. Geometry Nodes bakes on disk -> packed inside the .blend
    mods_empacotados = []
    for o in list(bpy.data.objects):
        if o.library is not None:
            continue
        for md in o.modifiers:
            if md.type != 'NODES':
                continue
            for bk in md.bakes:
                if bk.use_custom_path and bk.directory:
                    pasta = ab(bk.directory)
                elif md.bake_directory:
                    pasta = os.path.join(ab(md.bake_directory), str(bk.bake_id))
                else:
                    continue
                if not os.path.isdir(os.path.join(pasta, "meta")):
                    continue
                try:
                    with bpy.context.temp_override(object=o, active_object=o):
                        r = bpy.ops.object.geometry_node_bake_pack_single(
                            session_uid=o.session_uid, modifier_name=md.name, bake_id=bk.bake_id)
                    if 'FINISHED' in r:
                        mods_empacotados.append((o, md))
                        feitos.append("GN bake '%s / %s': packed into the .blend" % (o.name, md.name))
                        continue
                except Exception as err:
                    problemas.append("GN bake '%s / %s': could not pack (%s), copying the folder"
                                     % (o.name, md.name, err))
                try:
                    rel = lado.pasta_inteira("bakes", "%s_%s_%d" % (o.name, md.name, bk.bake_id), pasta)
                    bk.use_custom_path = True
                    bk.directory = rel
                    feitos.append("GN bake '%s / %s': folder copied" % (o.name, md.name))
                except Exception as err:
                    problemas.append("bake GN '%s / %s': %s" % (o.name, md.name, err))

    # the bake directory now points to the new folder (in case of a rebake to disk)
    for o, md in mods_empacotados:
        md.bake_directory = "//%s/bakes/%s" % (lado.pasta, _nome_seguro("%s_%s" % (o.name, md.name)))

    # 2. images Pack can't reach (sequence, video, unpacked UDIM)
    for im in bpy.data.images:
        if im.library is not None or im.packed_file is not None or len(im.packed_files):
            continue
        if im.type != 'IMAGE' or im.source in {'GENERATED', 'VIEWER'} or not im.filepath:
            continue
        src = ab(im.filepath)
        if im.source in {'FILE', 'TILED'}:
            try:
                im.pack()  # pack whatever can be packed
            except Exception:
                pass
            if im.packed_file is not None or len(im.packed_files):
                continue
        try:
            if im.source == 'SEQUENCE':
                rel = lado.sequencia("sequences", im.name, src)
            elif im.source == 'TILED':
                tiles = [_udim_arquivo(src, t.number) for t in im.tiles]
                tiles = [t for t in tiles if os.path.exists(t)]
                rel = None
                if tiles:
                    d = lado.varios("textures", os.path.splitext(im.name)[0] + "_udim", tiles)
                    rel = d + "/" + os.path.basename(src)
            elif os.path.exists(src):
                rel = lado.arquivo("movies" if im.source == 'MOVIE' else "textures", src)
            else:
                rel = None
            if rel is None:
                problemas.append("image '%s': file not found (%s)" % (im.name, src))
                continue
            im.filepath = rel
            feitos.append("image '%s' (%s)" % (im.name, im.source.lower()))
        except Exception as err:
            problemas.append("image '%s': %s" % (im.name, err))

    # 3. Alembic / USD
    for cf in bpy.data.cache_files:
        if cf.library is not None or not cf.filepath:
            continue
        src = ab(cf.filepath)
        try:
            if cf.is_sequence:
                rel = lado.sequencia("cache", cf.name, src)
            else:
                rel = lado.arquivo("cache", src) if os.path.exists(src) else None
            if rel is None:
                problemas.append("cache '%s': file not found (%s)" % (cf.name, src))
                continue
            cf.filepath = rel
            feitos.append("cache Alembic/USD '%s'" % cf.name)
        except Exception as err:
            problemas.append("cache '%s': %s" % (cf.name, err))

    # 4. movie clips (tracking / compositor)
    for mc in bpy.data.movieclips:
        if mc.library is not None or not mc.filepath:
            continue
        src = ab(mc.filepath)
        try:
            if mc.source == 'SEQUENCE':
                rel = lado.sequencia("sequences", mc.name, src)
            else:
                rel = lado.arquivo("movies", src) if os.path.exists(src) else None
            if rel is None:
                problemas.append("movie clip '%s': file not found (%s)" % (mc.name, src))
                continue
            mc.filepath = rel
            feitos.append("movie clip '%s'" % mc.name)
        except Exception as err:
            problemas.append("movie clip '%s': %s" % (mc.name, err))

    # 5. Video Sequencer: movies and image sequences
    for sc in bpy.data.scenes:
        for s in _strips(sc):
            try:
                if s.type == 'MOVIE' and s.filepath:
                    src = ab(s.filepath)
                    if not os.path.exists(src):
                        problemas.append("VSE '%s': video not found (%s)" % (s.name, src))
                        continue
                    s.filepath = lado.arquivo("movies", src)
                    feitos.append("VSE video '%s'" % s.name)
                elif s.type == 'IMAGE' and s.directory:
                    d = ab(s.directory)
                    arqs = [os.path.join(d, e.filename) for e in s.elements]
                    falt = [a for a in arqs if not os.path.exists(a)]
                    if falt:
                        problemas.append("VSE '%s': %d image(s) not found" % (s.name, len(falt)))
                        continue
                    s.directory = lado.varios("sequences", "vse_" + s.name, arqs) + "/"
                    feitos.append("VSE images '%s' (%d)" % (s.name, len(arqs)))
            except Exception as err:
                problemas.append("VSE '%s': %s" % (s.name, err))

    # 6. external IES and OSL scripts -> text inside the .blend
    for nt in _arvores_de_nodes():
        for n in nt.nodes:
            if n.bl_idname not in ("ShaderNodeTexIES", "ShaderNodeScript"):
                continue
            if n.mode != 'EXTERNAL' or not n.filepath:
                continue
            src = ab(n.filepath)
            try:
                with open(src, "r", encoding="utf-8", errors="replace") as fh:
                    conteudo = fh.read()
                t = bpy.data.texts.new(os.path.basename(src))
                t.write(conteudo)
                n.mode = 'INTERNAL'
                if n.bl_idname == "ShaderNodeTexIES":
                    n.ies = t
                else:
                    n.script = t
                if (n.ies if n.bl_idname == "ShaderNodeTexIES" else n.script) is None:
                    raise RuntimeError("Blender did not accept the embedded text")
                feitos.append("%s '%s': embedded as text" % ("IES" if "IES" in n.bl_idname else "OSL", n.name))
            except Exception as err:
                problemas.append("%s '%s': %s" % (n.bl_idname, n.name, err))

    # 7. fluid (Mantaflow) and ocean: whole cache folder
    for o in bpy.data.objects:
        if o.library is not None:
            continue
        for md in o.modifiers:
            try:
                if md.type == 'FLUID' and md.fluid_type == 'DOMAIN':
                    d = md.domain_settings
                    src = ab(d.cache_directory) if d.cache_directory else ""
                    if not src or not os.path.isdir(src) or not os.listdir(src):
                        continue
                    d.cache_directory = lado.pasta_inteira("fluid", o.name, src)
                    feitos.append("fluid cache '%s'" % o.name)
                elif md.type == 'OCEAN' and md.filepath:
                    src = ab(md.filepath)
                    if not os.path.isdir(src) or not os.listdir(src):
                        continue
                    md.filepath = lado.pasta_inteira("ocean", o.name, src)
                    feitos.append("ocean cache '%s'" % o.name)
            except Exception as err:
                problemas.append("cache of '%s': %s" % (o.name, err))

    # 8. physics caches (cloth, soft body, particles, rigid body, dynamic paint)
    em_disco = [(dono, pc) for dono, pc in _point_caches() if pc.use_disk_cache or pc.use_external]
    if em_disco:
        velha = os.path.join(origem_dir, "blendcache_" + origem_stem)
        nova = os.path.join(blend_dir, "blendcache_" + stem)
        if any(pc.use_disk_cache and not pc.use_external for _d, pc in em_disco) and os.path.isdir(velha):
            n = 0
            for a in os.listdir(velha):
                f = os.path.join(velha, a)
                if os.path.isfile(f) and a.endswith(".bphys"):
                    lado.bytes += _copiar(f, os.path.join(nova, a))
                    lado.n += 1
                    n += 1
            if n:
                feitos.append("physics caches: %d file(s) -> blendcache_%s" % (n, stem))
        for dono, pc in em_disco:
            if pc.use_external and pc.filepath:
                src = ab(pc.filepath)
                if os.path.isdir(src):
                    pc.filepath = lado.pasta_inteira("ptcache", "%s_%s" % (dono, pc.name or "cache"), src)
                    feitos.append("external cache '%s'" % dono)
                else:
                    problemas.append("external cache of '%s' not found (%s)" % (dono, src))

    # 9. VDB
    if bpy.data.volumes:
        lado.n += len(relocate_volumes(blend_dir, stem + "_files/vdb", L))

    if feitos:
        L.append("Brought along (%d file(s), %.1f MB in '%s'):"
                 % (lado.n, lado.bytes / 1048576.0, lado.pasta))
        for x in feitos[:40]:
            L.append("   - " + x)
        if len(feitos) > 40:
            L.append("   ... and %d more" % (len(feitos) - 40))
    if problemas:
        L.append("WARNING, could not bring along:")
        for x in problemas[:40]:
            L.append("   ! " + x)
    L.append("")
    return lado, feitos, problemas


def _existe_externo(e):
    db = e["db"]
    if e["kind"] == "image" and db.source == 'TILED':
        return any(os.path.exists(_udim_arquivo(e["abs"], t.number)) for t in db.tiles)
    if e["kind"] in ("cache (Alembic/USD)", "movie clip"):
        return True  # coletar_ao_lado reports it if missing
    if e["kind"] == "image" and db.source in {'SEQUENCE', 'MOVIE'}:
        return True
    return os.path.exists(e["abs"])


def _tratado_ao_lado(e):
    if e["kind"] in ("volume", "cache (Alembic/USD)", "movie clip"):
        return True
    return e["kind"] == "image" and e["db"].source in {'SEQUENCE', 'MOVIE', 'TILED'}


# --------------------------------------------------------------------------
# make local
# --------------------------------------------------------------------------

def make_all_local(max_passes=6):
    """Make every linked datablock local.

    Runs in passes because making a datablock local often exposes
    dependencies that were still linked.
    """
    total = 0
    falhas = 0
    for _ in range(max_passes):
        n = 0
        for attr in LOCALIZE:
            col = getattr(bpy.data, attr, None)
            if col is None:
                continue
            for db in list(col):
                if getattr(db, "library", None) is None:
                    continue
                try:
                    db.make_local()
                    n += 1
                except Exception:
                    falhas += 1
        total += n
        if n == 0:
            break
    return total, falhas


# --------------------------------------------------------------------------
# resolving paths after moving the .blend
# --------------------------------------------------------------------------

def lib_dir_after(lib, blend_dir):
    """Folder where library 'lib' will resolve, with the .blend saved in blend_dir.

    Libraries can be chained (one linked from inside another),
    hence the recursion through lib.parent.
    """
    if lib is None:
        return blend_dir
    base = lib_dir_after(getattr(lib, "parent", None), blend_dir)
    p = lib.filepath
    if p.startswith("//"):
        return os.path.dirname(os.path.normpath(os.path.join(base, p[2:])))
    return os.path.dirname(os.path.normpath(p))


def target_after(entry, blend_dir):
    """Where the external file will point, with the .blend saved in blend_dir.

    Returns None if the path is absolute (not portable).
    """
    raw = entry["raw"]
    if not raw.startswith("//"):
        return None
    base = lib_dir_after(entry["lib"], blend_dir)
    return os.path.normpath(os.path.join(base, raw[2:]))


def nested_dir(dest, levels):
    """DEST/blend  |  DEST/_lvl2/blend  |  DEST/_lvl2/_lvl3/blend ..."""
    parts = ["_lvl%d" % i for i in range(2, levels + 1)] + ["blend"]
    return os.path.join(dest, *parts)


def choose_depth(entries, dest, max_levels=8):
    """Find the minimum depth that keeps ALL files inside dest.

    A path like //../../../x goes up three levels; if the .blend sits too
    shallow, the target falls outside the archive folder.
    """
    dest_n = os.path.normpath(dest) + os.sep
    for levels in range(1, max_levels + 1):
        bdir = nested_dir(dest, levels)
        ok = True
        for e in entries:
            t = target_after(e, bdir)
            if t is None:
                continue  # absolute: handled separately
            if not (os.path.normpath(t) + os.sep).startswith(dest_n):
                ok = False
                break
        if ok:
            return levels
    return None


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def show_report(lines, name="NAKAN COLLECT"):
    txt = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    txt.clear()
    txt.write("\n".join(lines))
    try:
        for area in bpy.context.screen.areas:
            if area.type == 'TEXT_EDITOR':
                area.spaces.active.text = txt
                return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# the actual work (module-level function: outlives the operator)
# --------------------------------------------------------------------------

LAST_REPORT = []


def do_collect(o, pairs):
    """o = options dict. pairs = viewports that were switched to Solid."""
    L = ["=== NAKAN COLLECT FILES ===",
         "source : %s" % bpy.data.filepath,
         "target : %s" % o["dest"],
         "mode   : %s" % ("SINGLE FILE (make local)" if o["make_local"]
                          else "PORTABLE FOLDER (keeps the links)"),
         ""]

    if pairs:
        L.append("Viewport: %d viewport(s) taken out of Rendered/Material mode "
                 "before touching the data." % len(pairs))
        L.append("")

    def finish(ok_msg=None):
        if pairs:
            if o["restore_shading"]:
                n = set_shading(pairs)  # back to the original mode
                L.append("")
                L.append("Viewport: %d restored to the original mode." % n)
            else:
                L.append("")
                L.append("Viewport left in SOLID on purpose.")
                L.append("Switch back to Rendered yourself whenever you want -- "
                         "that way the resync happens when YOU say so.")
        if ok_msg:
            L.append("")
            L.append(ok_msg)
        global LAST_REPORT
        LAST_REPORT = list(L)
        show_report(L)
        return None  # ends the timer

    # 0. check before touching anything -- no point making local with missing files
    antes = collect_external()
    faltando = [e for e in antes if not _existe_externo(e)]
    if faltando:
        L.append("ABORTED: %d file(s) not found on disk." % len(faltando))
        L.append("Collecting now would freeze their absence.")
        L.append("")
        for e in faltando[:40]:
            L.append("   %s  ->  %s" % (e["db"].name, e["abs"]))
        return finish()

    # 0.5 broken collection references ---------------------------------
    # Before anything that makes Blender re-evaluate the scene.
    if o["repair_null_collections"]:
        ruins = null_collection_nodes()
        if ruins:
            users = node_group_users()
            L.append("FIX: %d 'Collection Info' node(s) with a NULL collection." % len(ruins))
            L.append("   They produce no geometry at all, but crash Blender")
            L.append("   when something walks the instances (viewport in Rendered,")
            L.append("   .blend thumbnail on save).")
            L.append("   They were MUTED -- reversible, and the null pointer goes away.")
            for ng, n in ruins:
                L.append("")
                L.append("   - node '%s' in node group '%s'" % (n.name, ng.name))
                for who in sorted(objects_using(ng.name, users))[:15]:
                    L.append("        used by: %s" % who)
                try:
                    n.mute = True
                except Exception as err:
                    L.append("        COULD NOT MUTE: %s" % err)
            L.append("")
        else:
            L.append("Collection references: none broken.")
            L.append("")

    # 1. make local ----------------------------------------------------
    if o["make_local"]:
        n_libs = len(real_libraries())
        n, falhas = make_all_local()
        restantes = libs_still_used()
        L.append("Make Local: %d datablocks made local" % n)
        if falhas:
            L.append("   (%d could not be made local)" % falhas)
        L.append("   libraries before: %d  |  still with linked data: %d"
                 % (n_libs, len(restantes)))
        for l in restantes[:20]:
            L.append("      remaining: %s" % l.filepath)
        L.append("")

    # 2. pack ----------------------------------------------------------
    if o["pack_resources"]:
        # always now (even with autopack): after this the paths may
        # change, and autopack on save would read from the wrong place.
        try:
            bpy.ops.file.pack_all()
            L.append("Pack Resources: ok")
        except Exception as err:
            L.append("Pack Resources: FAILED (%s)" % err)

    if o["pack_libraries"] and libs_still_used():
        op = getattr(bpy.ops.file, "pack_libraries", None)
        if op is None:
            L.append("Pack Linked Libraries: operator missing in this version")
        else:
            try:
                op()
                L.append("Pack Linked Libraries: ok")
            except Exception as err:
                L.append("Pack Linked Libraries: FAILED (%s)" % err)
    L.append("")

    # 3. what still has to travel along ------------------------------
    # (VDB, Alembic, videos, sequences and caches: coletar_ao_lado, below)
    entries = [e for e in collect_external() if not _tratado_ao_lado(e)]
    # what Pack Resources handles on its own doesn't need copying
    precisa = [e for e in entries
               if (e["lib"] is not None) or (not e["packable"])]
    absolutos = [e for e in precisa if not e["raw"].startswith("//")]
    portateis = [e for e in precisa if e["raw"].startswith("//")]

    # 4. where the .blend will live -----------------------------------
    dest = o["dest"]
    if portateis:
        levels = choose_depth(portateis, dest) or 1
        blend_dir = nested_dir(dest, levels)
        if levels > 1:
            L.append("Depth %d: some path goes up several levels, "
                     "so the .blend was nested for everything to fit in the folder." % levels)
    else:
        # nothing to copy: the .blend is self-sufficient, no subfolder needed
        blend_dir = dest
    os.makedirs(blend_dir, exist_ok=True)

    # 4.5 what Blender doesn't pack: <name>_files/ next to the .blend
    stem_novo = os.path.splitext(o["name"])[0]
    lado, _feitos, _probs = coletar_ao_lado(blend_dir, stem_novo, L)

    # 5. copy what's left ---------------------------------------------
    copiados = 0
    total = 0
    for e in portateis:
        tgt = target_after(e, blend_dir)
        if tgt is None:
            continue
        os.makedirs(os.path.dirname(tgt), exist_ok=True)
        if not os.path.exists(tgt):
            shutil.copy2(e["abs"], tgt)
            copiados += 1
        total += os.path.getsize(tgt)

    if portateis:
        L.append("files copied alongside: %d  (%.1f MB)"
                 % (copiados, total / 1048576.0))
        L.append("   (take the whole FOLDER, not just the .blend)")
    else:
        if lado.n:
            L.append("take the folder '%s' along with the .blend." % lado.pasta)
        else:
            L.append("nothing had to be copied: the .blend is self-contained.")

    if absolutos:
        L.append("")
        L.append("WARNING: %d file(s) with an ABSOLUTE path were not copied."
                 % len(absolutos))
        L.append("Run File > External Data > Make Paths Relative and collect again.")
        for e in absolutos[:20]:
            L.append("   %s  ->  %s" % (e["db"].name, e["raw"]))
    L.append("")

    # 6. save ----------------------------------------------------------
    destino_blend = os.path.join(blend_dir, o["name"])

    # SAFETY LOCK: never save with a live NULL collection.
    # Blender draws the scene off-screen for the thumbnail, walks the
    # instances and segfaults. Better to refuse than to crash Blender.
    sobrou = null_collection_nodes()
    if sobrou and o["save"]:
        L.append("!!! SAVE CANCELLED !!!")
        L.append("There are still %d 'Collection Info' node(s) with a NULL collection:" % len(sobrou))
        for ng, n in sobrou:
            L.append("   - node '%s' in node group '%s'" % (n.name, ng.name))
        L.append("")
        L.append("Saving now would crash Blender while generating the thumbnail.")
        L.append("Turn on 'Fix broken collections' and run it again,")
        L.append("or mute those nodes by hand before saving.")
        return finish()

    if o["save"]:
        # Blender draws the scene off-screen to generate the .blend thumbnail.
        # That drawing walks the collection instances -- and in a scene with a
        # broken collection reference it segfaults inside
        # BKE_collection_object_cache_get. Turn the thumbnail off only during the save.
        prefs = None
        thumb_antes = None
        try:
            prefs = bpy.context.preferences.filepaths
            # Blender 4.2+ / 5.x: file_preview_type ('NONE','AUTO','SCREENSHOT','CAMERA').
            # Older versions used the boolean use_save_preview_images.
            if hasattr(prefs, "file_preview_type"):
                thumb_antes = ("file_preview_type", prefs.file_preview_type)
                prefs.file_preview_type = 'NONE'
                agora = prefs.file_preview_type
            elif hasattr(prefs, "use_save_preview_images"):
                thumb_antes = ("use_save_preview_images", prefs.use_save_preview_images)
                prefs.use_save_preview_images = False
                agora = 'NONE' if not prefs.use_save_preview_images else 'ON'
            else:
                thumb_antes = None
                agora = "?"
                L.append(".blend thumbnail: this Blender version doesn't expose the "
                         "preference. Could not turn it off.")
            if thumb_antes is not None:
                L.append(".blend thumbnail: %s was %r, now %r during save."
                         % (thumb_antes[0], thumb_antes[1], agora))
                if agora != 'NONE':
                    L.append("   WARNING: it did NOT turn off. Turn it off by hand in Preferences "
                             "> Save & Load > Blend File Preview = None.")
        except Exception as err:
            prefs = None
            thumb_antes = None
            L.append(".blend thumbnail: could not change the preference (%s)." % err)
        try:
            # relative_remap=False is essential: with True Blender rewrites
            # the paths so they keep pointing to the original location,
            # undoing the collect.
            bpy.ops.wm.save_as_mainfile(filepath=destino_blend,
                                        relative_remap=False,
                                        compress=False)
            try:
                mb = os.path.getsize(destino_blend) / 1048576.0
                L.append("saved to: %s  (%.0f MB)" % (destino_blend, mb))
            except OSError:
                L.append("saved to: %s" % destino_blend)
        except Exception as err:
            L.append("SAVE FAILED: %s" % err)
            L.append("Save manually to:")
            L.append("   %s" % destino_blend)
            L.append("   and UNCHECK 'Remap Relative' in Save As.")
        finally:
            if prefs is not None and thumb_antes is not None:
                try:
                    setattr(prefs, thumb_antes[0], thumb_antes[1])
                except Exception:
                    pass
    else:
        L.append("Ready. Now save manually to:")
        L.append("   %s" % destino_blend)
        L.append("   and UNCHECK 'Remap Relative' in Save As.")

    return finish(">>> Close and reopen the file, then run "
                  "File > External Data > Check External Dependencies")


# --------------------------------------------------------------------------
# operator: check dependencies
# --------------------------------------------------------------------------

class NAKANCOLLECT_OT_check(bpy.types.Operator):
    bl_idname = "nakancollect.check"
    bl_label = "Check External Dependencies"
    bl_description = ("Lists what is still outside the .blend: unpacked "
                      "libraries, external textures and missing files")

    def execute(self, context):
        entries = collect_external()
        L = ["=== NAKAN COLLECT - check ===",
             "blend: %s" % (bpy.data.filepath or "(not saved)"),
             "autopack: %s" % ("ON" if bpy.data.use_autopack else "off"),
             ""]

        vp = live_viewports()
        if vp:
            L.append("WARNING: %d viewport(s) in Rendered/Material." % len(vp))
            L.append("   Collect Files takes them out of Rendered by itself before touching anything.")
            L.append("")

        ruins = null_collection_nodes()
        if ruins:
            users = node_group_users()
            L.append("DANGER: %d 'Collection Info' node(s) with a NULL collection." % len(ruins))
            L.append("   This makes Blender segfault when saving the .blend thumbnail")
            L.append("   or with the viewport in Rendered.")
            for ng, n in ruins:
                L.append("   - node '%s' in '%s'" % (n.name, ng.name))
                for who in sorted(objects_using(ng.name, users))[:15]:
                    L.append("        used by: %s" % who)
            L.append("   Collect Files mutes these nodes by itself (reversible).")
            L.append("")
        else:
            L.append("Collection references: none broken.")
            L.append("")

        libs = real_libraries()
        npk = sum(1 for l in libs if getattr(l, "packed_file", None))
        L.append("linked libraries: %d  |  packed: %d" % (len(libs), npk))
        for l in libs:
            if not getattr(l, "packed_file", None):
                L.append("   NOT PACKED: %s" % l.filepath)
        L.append("")

        faltando = [e for e in entries if not os.path.exists(e["abs"])]
        absolutos = [e for e in entries if not e["raw"].startswith("//")]
        de_lib = [e for e in entries if e["lib"] is not None]

        vdb_ruins = []
        for v in bpy.data.volumes:
            if not v.filepath:
                continue
            ap = bpy.path.abspath(v.filepath, library=v.library)
            if v.is_sequence:
                ok = bool(_sequence_files(os.path.normpath(ap)))
            else:
                ok = os.path.exists(ap)
            if not ok:
                vdb_ruins.append((v, ap))
        if vdb_ruins:
            L.append("VDB THAT WILL OPEN EMPTY: %d" % len(vdb_ruins))
            L.append("   (Blender only reads VDB from disk -- being packed is not enough)")
            for v, ap in vdb_ruins:
                L.append("   - '%s'%s  ->  %s" % (v.name, " [packed]" if v.packed_file else "", ap))
            L.append("")
        nao_packable = [e for e in entries if not e["packable"]]

        L.append("external files not packed: %d" % len(entries))
        L.append("   of these, from libraries (Pack Resources does NOT reach them): %d" % len(de_lib))
        L.append("   of these, with an ABSOLUTE path (not portable): %d" % len(absolutos))
        L.append("   of these, a type Blender never packs: %d" % len(nao_packable))
        L.append("   of these, MISSING on disk: %d" % len(faltando))
        L.append("")

        if faltando:
            L.append("--- MISSING (fix before collecting) ---")
            for e in faltando[:40]:
                L.append("   %s  ->  %s" % (e["db"].name, e["abs"]))
            L.append("")

        if absolutos:
            L.append("--- ABSOLUTE PATH (use File > External Data > Make Paths Relative) ---")
            for e in absolutos[:40]:
                L.append("   %s  ->  %s" % (e["db"].name, e["raw"]))
            L.append("")

        por_dono = {}
        for e in entries:
            k = e["lib"].filepath if e["lib"] else "<main file>"
            por_dono[k] = por_dono.get(k, 0) + 1
        if por_dono:
            L.append("--- external files by owner ---")
            for k in sorted(por_dono):
                L.append("   %4d  %s" % (por_dono[k], k))

        if not entries and not libs:
            L.append("")
            L.append(">>> SINGLE FILE: nothing linked, nothing external.")
        elif not entries and npk == len(libs):
            L.append("")
            L.append(">>> SELF-CONTAINED: libraries packed, nothing external missing.")

        show_report(L)
        self.report({'INFO'}, "Report in the Text Editor: NAKAN COLLECT")
        return {'FINISHED'}


# --------------------------------------------------------------------------
# operator: collect
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# collect in the BACKGROUND
# --------------------------------------------------------------------------
#
# The collect (make local + pack + save) runs in a SECOND Blender, with no UI,
# opened from the file saved on disk. The open session is never changed.
#
# Why: doing Make Local inside the open session leaves it with
# stale references to the collections that used to be linked. The next viewport
# redraw walks those instances and Blender segfaults in
# BKE_collection_object_cache_get -- even with the viewport in Solid. The saved
# file is perfect; what breaks is the session. In the background there is no
# viewport, no thumbnail and no session to break.

import subprocess
import tempfile

WORKER_FLAG = "--nakan-collect-worker"
_JOB = {}


def _ctx_override():
    """Explicit window context. Timers run WITHOUT a window in the context, and then
    the status bar, reports and dialogs fail silently."""
    wm = bpy.context.window_manager
    win = wm.windows[0]
    area = max(win.screen.areas, key=lambda a: a.width * a.height)
    d = dict(window=win, screen=win.screen, area=area)
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    if region is not None:
        d["region"] = region
    return d


def _status(text):
    """Text in the status bar (None clears it)."""
    try:
        with bpy.context.temp_override(**_ctx_override()):
            bpy.context.workspace.status_text_set(text)
    except Exception as err:
        print("status failed:", err)


def _notify(msg, kind='INFO'):
    """Standard Blender message (flashes in the bar and stays in the Info log)."""
    try:
        with bpy.context.temp_override(**_ctx_override()):
            bpy.ops.nakancollect.notify(message=msg, kind=kind)
    except Exception:
        print(msg)


LAST_RESULT = {}


def _popup(title, linhas, icon, pasta=None):
    """Result dialog that only closes when you click OK."""
    try:
        with bpy.context.temp_override(**_ctx_override()):
            bpy.ops.nakancollect.result('INVOKE_DEFAULT', titulo=title,
                                        texto="\n".join(linhas), icone=icon,
                                        pasta=pasta or "")
    except Exception as err:
        print("dialog failed:", err)


def _clear_status_later():
    if _JOB.get("proc") is None:
        _status(None)
    return None


class NAKANCOLLECT_OT_result(bpy.types.Operator):
    bl_idname = "nakancollect.result"
    bl_label = "Nakan Collect"
    bl_options = {'INTERNAL'}
    titulo: StringProperty()
    texto: StringProperty()
    icone: StringProperty(default='INFO')
    pasta: StringProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(
            self, width=560, title=self.titulo, confirm_text="OK")

    def draw(self, context):
        col = self.layout.column()
        primeira = True
        for ln in self.texto.split("\n"):
            if primeira:
                col.label(text=ln, icon=self.icone)
                primeira = False
            else:
                col.label(text=ln)
        if self.pasta:
            col.separator()
            op = col.operator("wm.path_open", text="Open folder", icon='FILE_FOLDER')
            op.filepath = self.pasta
        col.separator()
        col.label(text="Full report: Text Editor > NAKAN COLLECT")

    def execute(self, context):
        return {'FINISHED'}


class NAKANCOLLECT_OT_notify(bpy.types.Operator):
    bl_idname = "nakancollect.notify"
    bl_label = "Nakan Collect Notify"
    bl_options = {'INTERNAL'}
    message: StringProperty()
    kind: StringProperty(default='INFO')

    def execute(self, context):
        self.report({self.kind}, self.message)
        return {'FINISHED'}


def _poll_job():
    import time
    proc = _JOB.get("proc")
    if proc is None:
        return None
    if proc.poll() is None:
        seg = int(time.time() - _JOB.get("t0", time.time()))
        _status("Nakan Collect: building %s in the background... %ds  "
                "(you can keep working)" % (_JOB.get("name", ""), seg))
        return 1.0  # check again in 1s

    seg = int(time.time() - _JOB.get("t0", time.time()))
    log_path = _JOB.get("log")
    linhas = []
    try:
        with open(log_path, "r", errors="replace") as fh:
            for ln in fh:
                if ln.startswith("[NCF] "):
                    linhas.append(ln[6:].rstrip("\n"))
    except Exception as err:
        linhas.append("Could not read the worker log: %s" % err)
    rc = proc.returncode

    salvo = next((ln for ln in linhas if ln.startswith("saved to:")), None)
    problema = next((ln for ln in linhas
                     if ln.startswith("ABORTED") or ln.startswith("!!!")
                     or ln.startswith("SAVE FAILED") or ln.startswith("ERROR")), None)

    if rc != 0:
        linhas.append("")
        linhas.append("!!! The background Blender exited with code %s." % rc)
        linhas.append("Full log: %s" % log_path)
    linhas.append("")
    linhas.append("Your open session was NOT changed -- it is still on the original file.")
    show_report(linhas)
    bpy.app.timers.register(_clear_status_later, first_interval=900.0)

    pasta = _JOB.get("dest")
    hora = time.strftime("%H:%M")
    if rc == 0 and salvo:
        caminho = salvo[len("saved to:"):].strip()
        LAST_RESULT.clear()
        LAST_RESULT.update(ok=True, hora=hora, texto=os.path.basename(caminho.split("  (")[0]))
        _status("Nakan Collect: DONE at %s  ->  %s" % (hora, caminho))
        ao_lado = next((ln for ln in linhas if ln.startswith("take the folder")
                        or ln.startswith("files copied alongside")), None)
        n_prob = sum(1 for ln in linhas if ln.startswith("   ! "))
        msg = [
            "File created in %ds:" % seg,
            caminho,
            ("Take the folder next to the .blend with it (VDB, caches, videos...)." if ao_lado
             else "Everything is inside the .blend.") + " Your session was not changed.",
        ]
        if n_prob:
            msg.append("WARNING: %d item(s) were not brought along -- see the report." % n_prob)
        _popup("Nakan Collect: DONE", msg, 'ERROR' if n_prob else 'CHECKMARK', pasta)
        _notify("Nakan Collect: done -> %s" % caminho, 'INFO')
    else:
        motivo = problema or ("the background Blender exited with code %s" % rc)
        LAST_RESULT.clear()
        LAST_RESULT.update(ok=False, hora=hora, texto=motivo[:60])
        _status("Nakan Collect: DID NOT FINISH at %s  -  %s" % (hora, motivo))
        _popup("Nakan Collect: DID NOT FINISH", [
            motivo,
            "Nothing was saved. Your session was not changed.",
        ], 'ERROR', None)
        _notify("Nakan Collect: failed - %s" % motivo, 'ERROR')

    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()
    except Exception:
        pass
    _JOB.clear()
    return None


class NAKANCOLLECT_OT_collect(bpy.types.Operator):
    bl_idname = "nakancollect.collect"
    bl_label = "Nakan Collect Files"
    bl_description = ("Creates a self-contained .blend in a new folder: local libraries, "
                      "everything packed. Runs in a separate Blender, in the background -- "
                      "the open session is not changed")
    bl_options = {'REGISTER'}

    directory: StringProperty(subtype='DIR_PATH')
    filename: StringProperty(name=".blend name", default="")

    make_local: BoolProperty(
        name="Make Local (single file)", default=True,
        description="Makes linked libraries local so their textures "
                    "can be packed. Only affects the NEW file: the original and the "
                    "open session keep their links working")
    repair_null_collections: BoolProperty(
        name="Fix broken collections (recommended)", default=True,
        description="Mutes, in the new file, the Collection Info nodes that evaluate with a "
                    "NULL collection (they cause a segfault when drawing the scene)")
    do_pack_resources: BoolProperty(
        name="Pack Resources", default=True,
        description="Packs images/sounds/fonts inside the .blend")
    do_pack_libraries: BoolProperty(
        name="Pack Linked Libraries", default=True,
        description="Packs the libraries that remain linked")

    def invoke(self, context, event):
        if not bpy.data.filepath:
            self.report({'ERROR'}, "Save the file once before collecting")
            return {'CANCELLED'}
        if _JOB.get("proc") is not None:
            self.report({'WARNING'}, "A collect is already running")
            return {'CANCELLED'}
        if not self.filename:
            base = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
            self.filename = base + "_collected.blend"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        lay = self.layout
        lay.prop(self, "filename")
        lay.separator()
        box = lay.box()
        box.label(text="Runs in a separate Blender.", icon='INFO')
        box.label(text="Your open session is not changed.")
        if bpy.data.is_dirty:
            box.label(text="UNSAVED changes are left out!", icon='ERROR')
            box.label(text="Cancel and save first (Ctrl+S).")
        lay.separator()
        lay.prop(self, "make_local")
        lay.prop(self, "repair_null_collections")
        lay.prop(self, "do_pack_resources")
        lay.prop(self, "do_pack_libraries")

    def execute(self, context):
        if _JOB.get("proc") is not None:
            self.report({'WARNING'}, "A collect is already running")
            return {'CANCELLED'}
        src = bpy.data.filepath
        if not src or not os.path.exists(src):
            self.report({'ERROR'}, "Save the file before collecting")
            return {'CANCELLED'}
        dest = bpy.path.abspath(self.directory)
        if not dest or not os.path.isdir(dest):
            self.report({'ERROR'}, "Choose a valid target folder")
            return {'CANCELLED'}
        name = self.filename or os.path.basename(src)
        if not name.lower().endswith(".blend"):
            name += ".blend"
        if os.path.normpath(os.path.join(dest, name)) == os.path.normpath(src):
            self.report({'ERROR'}, "The target can't be the original file itself")
            return {'CANCELLED'}

        args = [
            bpy.app.binary_path, "-b", "--factory-startup", src,
            "-P", os.path.abspath(__file__), "--", WORKER_FLAG,
            "--dest", dest, "--name", name,
            "--make-local", "1" if self.make_local else "0",
            "--repair", "1" if self.repair_null_collections else "0",
            "--pack-res", "1" if self.do_pack_resources else "0",
            "--pack-lib", "1" if self.do_pack_libraries else "0",
        ]
        fd, log_path = tempfile.mkstemp(prefix="nakan_collect_", suffix=".log")
        os.close(fd)
        try:
            fh = open(log_path, "w")
            proc = subprocess.Popen(args, stdout=fh, stderr=subprocess.STDOUT)
        except Exception as err:
            self.report({'ERROR'}, "Could not start the background Blender: %s" % err)
            return {'CANCELLED'}
        import time
        _JOB.update(proc=proc, log=log_path, t0=time.time(), dest=dest, name=name)
        _status("Nakan Collect: building %s in the background..." % name)
        aviso = ["=== NAKAN COLLECT FILES ===",
                 "Running in the background from:",
                 "   %s" % src,
                 "Target: %s" % os.path.join(dest, name),
                 ""]
        if bpy.data.is_dirty:
            aviso.append("WARNING: the session had UNSAVED changes.")
            aviso.append("The collect uses the file saved on disk -- those changes were left out.")
            aviso.append("")
        aviso.append("You can keep working. The report shows up here when it finishes.")
        show_report(aviso)
        bpy.app.timers.register(_poll_job, first_interval=1.0)
        self.report({'INFO'}, "Nakan Collect running in the background...")
        return {'FINISHED'}


# --------------------------------------------------------------------------
# UNPACK: takes everything out of the .blend into a folder next to it
# --------------------------------------------------------------------------
#
# Unlike the native Unpack Resources:
#  - everything goes to ONE folder (<name>_files/) at the same level as the .blend;
#  - names never collide: two different images with the same file name
#    (common with Botaniq/BlenderKit/Ucupaint) get _2, _3... The native one would make
#    the second reuse the first one's file -- wrong texture, no warning;
#  - identical content becomes a single file (deduplicated by hash);
#  - each file is checked on disk BEFORE releasing the data from inside the .blend.

import hashlib

EXT_IMAGEM = {
    'PNG': '.png', 'JPEG': '.jpg', 'JPEG2000': '.jp2', 'TARGA': '.tga',
    'TARGA_RAW': '.tga', 'BMP': '.bmp', 'OPEN_EXR': '.exr',
    'OPEN_EXR_MULTILAYER': '.exr', 'HDR': '.hdr', 'TIFF': '.tif',
    'WEBP': '.webp', 'DPX': '.dpx', 'CINEON': '.cin', 'IRIS': '.rgb',
}
UNPACK_GRUPOS = (
    ("images", "textures", 'IMAGE'),
    ("sounds", "sounds", 'SOUND'),
    ("fonts", "fonts", 'FONT'),
    ("volumes", "vdb", 'VOLUME'),
)


def _nome_seguro(nome):
    nome = re.sub(r'[\\/:*?"<>|]', "_", nome or "").strip()
    return nome or "sem_nome"


def _hash_arquivo(caminho):
    h = hashlib.sha1()
    with open(caminho, "rb") as fh:
        for bloco in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


# Blender's internal ID code (MAKE_ID2) -- the unpack_item operator wants an integer
_ID_CODE = {'VOLUME': ord('V') | (ord('O') << 8)}


def _soltar(db, id_type, rel):
    """Release the packed data WITHOUT Blender writing anything anywhere.

    Image/sound/font: unpack(method='REMOVE') only discards the packed data.
    (The other modes use the path stored INSIDE the package -- the original --
    and recreate files there, even in old folders. Tested: never use them.)
    Volumes have no unpack(); the operator with USE_ORIGINAL only reuses the
    file we already wrote, because the filepath already points to it.
    """
    if hasattr(db, "packed_files"):
        for pf in db.packed_files:
            pf.filepath = "//" + rel
    if hasattr(db, "unpack"):
        db.unpack(method='REMOVE')
        if hasattr(db, "reload"):
            db.reload()
    else:
        alvo = bpy.path.abspath(db.filepath)
        if not os.path.exists(alvo):
            raise IOError("file %s does not exist; not releasing the packed data" % alvo)
        bpy.ops.file.unpack_item(method='USE_ORIGINAL', id_name=db.name,
                                 id_type=_ID_CODE[id_type])


def _dentro(caminho, pasta):
    caminho = os.path.normcase(os.path.realpath(caminho))
    pasta = os.path.normcase(os.path.realpath(pasta))
    return caminho == pasta or caminho.startswith(pasta + os.sep)


def _limpa_vdb_antigo(blend_dir, stem, vdb_dir, L):
    """Delete the <stem>_vdb folder left by Collect, with safeguards:
    - only the folder with that exact name, next to the .blend (never another folder of yours);
    - no datablock in this .blend points inside it;
    - every file in it has an IDENTICAL copy (sha1) in <stem>_files/vdb.
    If any file has no copy, nothing is deleted."""
    velha = os.path.join(blend_dir, stem + "_vdb")
    if not os.path.isdir(velha) or os.path.islink(velha):
        return
    for attr in ("volumes", "images", "sounds", "fonts", "movieclips", "cache_files", "libraries"):
        for db in getattr(bpy.data, attr, []):
            fp = getattr(db, "filepath", "")
            if fp and not fp.startswith("<") and _dentro(bpy.path.abspath(fp), velha):
                L.append("Folder %s_vdb kept: '%s' still uses it." % (stem, db.name))
                return
    copias = set()
    if os.path.isdir(vdb_dir):
        for raiz, _d, arqs in os.walk(vdb_dir):
            for a in arqs:
                copias.add(_hash_arquivo(os.path.join(raiz, a)))
    arquivos = []
    for raiz, _d, arqs in os.walk(velha):
        for a in arqs:
            f = os.path.join(raiz, a)
            if a in (".DS_Store", "Thumbs.db", "desktop.ini"):
                arquivos.append(f)
                continue
            if _hash_arquivo(f) not in copias:
                L.append("Folder %s_vdb kept: '%s' has no copy in %s_files/vdb."
                         % (stem, a, stem))
                return
            arquivos.append(f)
    try:
        for f in arquivos:
            os.remove(f)
        for raiz, dirs, _a in sorted(os.walk(velha), key=lambda t: -len(t[0])):
            os.rmdir(raiz)
        L.append("Old folder %s_vdb deleted (the VDBs are already in %s_files/vdb)."
                 % (stem, stem))
    except OSError as err:
        L.append("Could not delete %s_vdb: %s -- delete it yourself." % (stem, err))


def _unpack_udim(im, blend_dir, pasta_nome):
    """Write each packed UDIM tile to textures/<name>_udim/ and release the packed data."""
    base = os.path.basename(bpy.path.abspath(im.filepath)) if im.filepath else im.name
    b, ext = os.path.splitext(base)
    if not ext:
        ext = EXT_IMAGEM.get(getattr(im, "file_format", ""), ".png")
    b = _nome_seguro(re.sub(r"[._]?(<UDIM>|<UVTILE>|\d{4})$", "", b) or im.name)
    k = 1
    while True:
        sub = "%s_udim%s" % (b, "_%d" % k if k > 1 else "")
        absdir = os.path.join(blend_dir, pasta_nome, "textures", sub)
        if not os.path.exists(absdir):
            break
        k += 1
    os.makedirs(absdir)
    n = tot = 0
    for pf in im.packed_files:
        p = os.path.join(absdir, "%s.%d%s" % (b, pf.tile_number, ext))
        data = pf.packed_file.data
        with open(p, "wb") as fh:
            fh.write(data)
        if os.path.getsize(p) != len(data):
            raise IOError("tile %d written with the wrong size" % pf.tile_number)
        pf.filepath = "//" + os.path.relpath(p, blend_dir).replace(os.sep, "/")
        n += 1
        tot += len(data)
    im.filepath = "//" + os.path.relpath(os.path.join(absdir, b + ".<UDIM>" + ext),
                                          blend_dir).replace(os.sep, "/")
    im.unpack(method='REMOVE')
    im.reload()
    if len(im.packed_files):
        raise IOError("UDIM is still packed")
    return n, tot


def _unpack_bakes(blend_dir, stem, pasta_nome, L, falhas):
    """Packed Geometry Nodes bakes -> <stem>_files/bakes/.

    Blender's operator always writes to //blendcache_<stem>/<obj>_<mod>/<id>,
    ignoring the path we ask for. So: if that folder already exists (a bake
    of yours), it is set aside and put back at the end; what Blender wrote is
    moved to <stem>_files/bakes/."""
    n = 0
    raiz = os.path.join(blend_dir, "blendcache_" + stem)
    raiz_existia = os.path.exists(raiz)
    ja_existiam = set(d for d, _s, _a in os.walk(raiz)) if raiz_existia else set()
    for o in list(bpy.data.objects):
        if o.library is not None:
            continue
        for md in o.modifiers:
            if md.type != 'NODES':
                continue
            for bk in md.bakes:
                alvo = bk.bake_target if bk.bake_target != 'INHERIT' else md.bake_target
                if alvo != 'PACKED':
                    continue
                esperado = os.path.join(raiz, "%s_%s" % (o.name, md.name), str(bk.bake_id))
                guardado = None
                try:
                    if os.path.exists(esperado):
                        guardado = esperado + ".nakan_guardado"
                        os.rename(esperado, guardado)
                    with bpy.context.temp_override(object=o, active_object=o):
                        r = bpy.ops.object.geometry_node_bake_unpack_single(
                            session_uid=o.session_uid, modifier_name=md.name,
                            bake_id=bk.bake_id, method='WRITE_LOCAL')
                    if 'FINISHED' not in r:
                        continue
                    escrito = os.path.normpath(bpy.path.abspath(bk.directory)) if bk.directory else esperado
                    if not os.path.isdir(os.path.join(escrito, "meta")):
                        falhas.append("bake '%s / %s': Blender wrote nothing" % (o.name, md.name))
                        continue
                    base = _nome_seguro("%s_%s_%d" % (o.name, md.name, bk.bake_id))
                    destino = os.path.join(blend_dir, pasta_nome, "bakes", base)
                    k = 2
                    while os.path.exists(destino):
                        destino = os.path.join(blend_dir, pasta_nome, "bakes", "%s_%d" % (base, k))
                        k += 1
                    os.makedirs(os.path.dirname(destino), exist_ok=True)
                    shutil.move(escrito, destino)
                    bk.use_custom_path = True
                    bk.directory = "//" + os.path.relpath(destino, blend_dir).replace(os.sep, "/")
                    bk.bake_target = 'DISK'
                    n += 1
                except Exception as err:
                    falhas.append("bake '%s / %s': %s" % (o.name, md.name, err))
                finally:
                    if guardado:
                        if not os.path.exists(esperado):
                            os.rename(guardado, esperado)
                        else:
                            falhas.append("bake '%s / %s': your old folder was left at %s"
                                          % (o.name, md.name, guardado))
    if os.path.isdir(raiz):
        # only remove EMPTY folders that Blender just created
        for d, _s, _a in sorted(os.walk(raiz), key=lambda t: -len(t[0])):
            try:
                if d not in ja_existiam and not os.listdir(d):
                    os.rmdir(d)
            except OSError:
                pass
    if n:
        L.append("Geometry Nodes bakes: %d -> %s/bakes" % (n, pasta_nome))
    return n


def do_unpack(salvar=True, apagar_vdb_antigo=True):
    blend = bpy.data.filepath
    blend_dir = os.path.dirname(blend)
    stem = os.path.splitext(os.path.basename(blend))[0]
    pasta_nome = stem + "_files"
    L = ["=== NAKAN UNPACK FILES ===",
         "blend : %s" % blend,
         "folder: %s" % os.path.join(blend_dir, pasta_nome),
         ""]
    try:
        antes_mb = os.path.getsize(blend) / 1048576.0
    except OSError:
        antes_mb = None

    por_hash = {}     # content -> relative path already written
    usados = {}       # relative path (lowercase) -> content
    escritos = reaproveitados = soltos = 0
    total = 0
    pulados = []
    falhas = []

    for attr, sub, id_type in UNPACK_GRUPOS:
        col = getattr(bpy.data, attr, None)
        if col is None:
            continue
        for db in list(col):
            if db.library is not None:
                continue
            pf = getattr(db, "packed_file", None)
            if pf is None:
                continue
            if attr == "fonts" and db.filepath == "<builtin>":
                continue
            try:
                if attr == "images" and db.source == 'TILED' and not db.is_multiview:
                    nt_, tb_ = _unpack_udim(db, blend_dir, pasta_nome)
                    escritos += nt_
                    total += tb_
                    soltos += 1
                    continue
                if attr == "images":
                    if len(db.packed_files) > 1:
                        pulados.append("%s (UDIM/multiview: several files)" % db.name)
                        continue
                    if db.is_dirty:
                        db.pack()  # bring the in-memory painting inside before extracting
                        pf = db.packed_file
                data = pf.data
                h = hashlib.sha1(data).hexdigest()

                if h in por_hash:
                    rel = por_hash[h]
                    reaproveitados += 1
                else:
                    base = os.path.basename(bpy.path.abspath(db.filepath)) if db.filepath else ""
                    base = _nome_seguro(base) if base else ""
                    nome, ext = os.path.splitext(base)
                    if not ext:
                        if attr == "images":
                            ext = EXT_IMAGEM.get(getattr(db, "file_format", ""), ".png")
                        else:
                            ext = {"sounds": ".wav", "fonts": ".ttf", "volumes": ".vdb"}[attr]
                        nome = nome or _nome_seguro(db.name)
                    k = 1
                    while True:
                        arq = nome + (("_%d" % k) if k > 1 else "") + ext
                        rel = "%s/%s/%s" % (pasta_nome, sub, arq)
                        absp = os.path.join(blend_dir, pasta_nome, sub, arq)
                        dono = usados.get(rel.lower())
                        if dono is None and os.path.exists(absp):
                            dono = _hash_arquivo(absp)   # left over from a previous run
                            usados[rel.lower()] = dono
                        if dono is None or dono == h:
                            break
                        k += 1
                    os.makedirs(os.path.dirname(absp), exist_ok=True)
                    if not os.path.exists(absp):
                        with open(absp, "wb") as fh:
                            fh.write(data)
                    if os.path.getsize(absp) != len(data):
                        falhas.append("%s: file written with the wrong size" % db.name)
                        continue
                    usados[rel.lower()] = h
                    por_hash[h] = rel
                    escritos += 1
                    total += len(data)

                db.filepath = "//" + rel
                _soltar(db, id_type, rel)
                if getattr(db, "packed_file", None) is None:
                    soltos += 1
                else:
                    falhas.append("%s: still packed" % db.name)
            except Exception as err:
                falhas.append("%s: %s" % (db.name, err))

    bakes_soltos = _unpack_bakes(blend_dir, stem, pasta_nome, L, falhas)

    # VDBs already OUTSIDE the .blend (e.g. the <name>_vdb folder from Collect):
    # copied to <name>_files/vdb and repointed. Copy, don't move -- the old folder
    # is only listed in the report so you can delete it whenever you want.
    vdb_dir = os.path.join(blend_dir, pasta_nome, "vdb")
    vdb_movidos = 0
    pastas_antigas = set()
    for v in bpy.data.volumes:
        if v.library is not None or v.packed_file is not None or not v.filepath:
            continue
        src = os.path.normpath(bpy.path.abspath(v.filepath))
        if os.path.normpath(os.path.dirname(src)).startswith(os.path.normpath(vdb_dir)):
            continue  # already in place
        arqs = _sequence_files(src) if v.is_sequence else ([src] if os.path.exists(src) else [])
        if not arqs:
            falhas.append("VDB %s: file not found (%s)" % (v.name, src))
            continue
        try:
            destino = vdb_dir
            # if a file with the same name and different content already exists,
            # use a subfolder named after the volume (sequences can't be renamed)
            for f in arqs:
                alvo = os.path.join(destino, os.path.basename(f))
                if os.path.exists(alvo) and _hash_arquivo(alvo) != _hash_arquivo(f):
                    destino = os.path.join(vdb_dir, _nome_seguro(v.name))
                    break
            os.makedirs(destino, exist_ok=True)
            for f in arqs:
                alvo = os.path.join(destino, os.path.basename(f))
                if not os.path.exists(alvo):
                    shutil.copy2(f, alvo)
                    total += os.path.getsize(alvo)
                    escritos += 1
                if os.path.getsize(alvo) != os.path.getsize(f):
                    raise IOError("copy of %s has the wrong size" % os.path.basename(f))
            rel_dir = os.path.relpath(destino, blend_dir).replace(os.sep, "/")
            v.filepath = "//" + rel_dir + "/" + os.path.basename(src)
            vdb_movidos += 1
            pastas_antigas.add(os.path.dirname(src))
        except Exception as err:
            falhas.append("VDB %s: %s" % (v.name, err))

    L.append("files written           : %d  (%.1f MB)" % (escritos, total / 1048576.0))
    velha_vdb = os.path.join(blend_dir, stem + "_vdb")
    outras = sorted(pa for pa in pastas_antigas if not _dentro(pa, velha_vdb))
    if vdb_movidos:
        L.append("VDBs brought into %s/vdb: %d" % (pasta_nome, vdb_movidos))
    if outras:
        L.append("   VDB folders this .blend no longer uses (copied, originals untouched):")
        for pa in outras:
            L.append("      %s" % pa)
    L.append("repeated content (stored once): %d" % reaproveitados)
    L.append("datablocks unpacked: %d" % soltos)
    if pulados:
        L.append("")
        L.append("Left packed (Blender stores several files per image):")
        for x in pulados[:30]:
            L.append("   - " + x)
    if falhas:
        L.append("")
        L.append("FAILURES:")
        for x in falhas[:30]:
            L.append("   - " + x)
    L.append("")

    salvo = False
    if salvar and (soltos or vdb_movidos or bakes_soltos) and not falhas:
        prefs = bpy.context.preferences.filepaths
        antes_prev = getattr(prefs, "file_preview_type", None)
        try:
            if antes_prev is not None:
                prefs.file_preview_type = 'NONE'
            bpy.ops.wm.save_mainfile(compress=False)
            salvo = True
        except Exception as err:
            L.append("SAVE FAILED: %s -- save it yourself (Ctrl+S)." % err)
        finally:
            if antes_prev is not None:
                prefs.file_preview_type = antes_prev
    if salvo:
        depois_mb = os.path.getsize(blend) / 1048576.0
        if antes_mb is not None:
            L.append("Saved. .blend: %.0f MB  ->  %.0f MB" % (antes_mb, depois_mb))
        else:
            L.append("Saved. .blend is now %.0f MB" % depois_mb)
        L.append("The previous version (with everything inside) was kept as %s.blend1" % stem)
    elif salvar and falhas:
        L.append("NOT SAVED because of the failures above. Nothing was written to the .blend.")
    elif not salvar:
        L.append("Now save (Ctrl+S) so the .blend gets lighter.")
    # only delete if nothing points to the old folder and everything has an identical copy
    if apagar_vdb_antigo and not falhas and (salvo or not (vdb_movidos or bakes_soltos)):
        _limpa_vdb_antigo(blend_dir, stem, vdb_dir, L)
    L.append("")
    L.append("Always keep the folder '%s' together with the .blend." % pasta_nome)
    return L, salvo, escritos, total, falhas, os.path.join(blend_dir, pasta_nome)


class NAKANCOLLECT_OT_unpack(bpy.types.Operator):
    bl_idname = "nakancollect.unpack"
    bl_label = "Nakan Unpack Files"
    bl_description = ("Moves textures, sounds, fonts and VDBs out of the .blend into a "
                      "<name>_files folder next to it, with no name collisions, and saves a lighter .blend")
    bl_options = {'REGISTER'}

    salvar: BoolProperty(name="Save the .blend at the end", default=True,
                         description="Saves the lighter .blend. The previous version is kept as .blend1")
    apagar_vdb: BoolProperty(name="Delete the old <name>_vdb folder", default=True,
                             description="After copying, deletes the _vdb folder from Collect. "
                                         "Only deletes it if every file has an identical copy in "
                                         "<name>_files/vdb and nothing else uses the folder")

    def invoke(self, context, event):
        if not bpy.data.filepath:
            self.report({'ERROR'}, "Save the file once first")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
        n = sum(1 for attr, _s, _t in UNPACK_GRUPOS
                for db in getattr(bpy.data, attr, [])
                if getattr(db, "packed_file", None) is not None and db.library is None)
        vdb_dir = os.path.join(os.path.dirname(bpy.data.filepath), stem + "_files", "vdb")
        n += sum(1 for o in bpy.data.objects if o.library is None for md in o.modifiers
                 if md.type == 'NODES' for bk in md.bakes
                 if (bk.bake_target if bk.bake_target != 'INHERIT' else md.bake_target) == 'PACKED')
        n += sum(1 for v in bpy.data.volumes
                 if v.library is None and v.packed_file is None and v.filepath
                 and not _dentro(bpy.path.abspath(v.filepath), vdb_dir))
        col = self.layout.column()
        col.label(text="%d packed items will go to:" % n, icon='FILE_FOLDER')
        col.label(text="    %s_files/  (next to the .blend)" % stem)
        col.separator()
        col.prop(self, "salvar")
        if os.path.isdir(os.path.join(os.path.dirname(bpy.data.filepath), stem + "_vdb")):
            col.prop(self, "apagar_vdb")

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({'ERROR'}, "Save the file once first")
            return {'CANCELLED'}
        L, salvo, n, total, falhas, pasta = do_unpack(self.salvar, self.apagar_vdb)
        show_report(L)
        if falhas:
            _popup("Nakan Unpack: FINISHED WITH FAILURES",
                   ["%d failure(s) - nothing was saved." % len(falhas), falhas[0][:90]],
                   'ERROR', None)
            self.report({'ERROR'}, "Nakan Unpack: %d failure(s)" % len(falhas))
        else:
            resumo = [ln for ln in L if ln.startswith("Saved.")] or ["Done. Save with Ctrl+S."]
            resumo += [ln for ln in L if "_vdb" in ln and ("deleted" in ln or "kept" in ln)]
            _popup("Nakan Unpack: DONE",
                   ["%d files (%.0f MB) in:" % (n, total / 1048576.0), pasta] + resumo,
                   'CHECKMARK', pasta)
            self.report({'INFO'}, "Nakan Unpack: done")
        return {'FINISHED'}


# --------------------------------------------------------------------------
# menu
# --------------------------------------------------------------------------

def menu_func(self, context):
    lay = self.layout
    lay.separator()
    lay.operator(NAKANCOLLECT_OT_collect.bl_idname, icon='PACKAGE')
    lay.operator(NAKANCOLLECT_OT_unpack.bl_idname, icon='UGLYPACKAGE')
    lay.operator(NAKANCOLLECT_OT_check.bl_idname, icon='INFO')
    if _JOB.get("proc") is not None:
        lay.label(text="Collecting in the background...", icon='SORTTIME')
    elif LAST_RESULT:
        lay.label(text="Last collect %s: %s  %s" % (
            LAST_RESULT["hora"], "OK" if LAST_RESULT["ok"] else "FAILED",
            LAST_RESULT["texto"]),
            icon='CHECKMARK' if LAST_RESULT["ok"] else 'ERROR')


CLASSES = (NAKANCOLLECT_OT_notify, NAKANCOLLECT_OT_result, NAKANCOLLECT_OT_check, NAKANCOLLECT_OT_collect, NAKANCOLLECT_OT_unpack)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_external_data.append(menu_func)


def unregister():
    bpy.types.TOPBAR_MT_file_external_data.remove(menu_func)
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


# --------------------------------------------------------------------------
# worker entry point (Blender -b ... -P this_file -- --nakan-collect-worker ...)
# --------------------------------------------------------------------------

def _worker_main(argv):
    def arg(nome, padrao=None):
        if nome in argv:
            i = argv.index(nome)
            if i + 1 < len(argv):
                return argv[i + 1]
        return padrao

    opts = {
        "dest": arg("--dest"),
        "name": arg("--name"),
        "make_local": arg("--make-local", "1") == "1",
        "repair_null_collections": arg("--repair", "1") == "1",
        "pack_resources": arg("--pack-res", "1") == "1",
        "pack_libraries": arg("--pack-lib", "1") == "1",
        "save": True,
        "restore_shading": False,
    }
    rc = 0
    try:
        do_collect(opts, [])
    except Exception:
        import traceback
        LAST_REPORT.append("ERROR IN WORKER:")
        LAST_REPORT.extend(traceback.format_exc().splitlines())
        rc = 2
    for ln in LAST_REPORT:
        print("[NCF] " + ln)
    if any("SAVED" in ln.upper() or ln.startswith("saved to:") for ln in LAST_REPORT):
        pass
    elif rc == 0:
        rc = 1
    sys.stdout.flush()
    os._exit(rc)


if __name__ == "__main__":
    import sys
    _argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if WORKER_FLAG in _argv:
        _worker_main(_argv)
    else:
        register()
