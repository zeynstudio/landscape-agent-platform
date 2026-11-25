# -*- coding: utf-8 -*-
"""模型导出：craftbot-model JSON → OBJ / DAE / 3DM。

- OBJ/DAE：纯标准库手写，任何 Python 可用
- 3DM：依赖 rhino3dm（py 3.7 环境已装；PY312 没有 → has_3dm()=False，前端置灰）
- SKP 是 SketchUp 闭源格式无法直写 → 用 DAE（SketchUp 原生导入，File→Import）

坐标：保持平台米制 Z-up；DAE 显式声明 <unit meter="1"> + Z_UP。
所有网格面均为三角形或四边形（generator 保证）；3DM 写出时对 >4 顶点的面
防御性扇形三角化。
"""
import json
import os
import xml.sax.saxutils as saxutils


def has_3dm():
    try:
        import rhino3dm  # noqa: F401
        return True
    except ImportError:
        return False


def load_model(models_dir, model_id):
    """按 id 从模型库读 model dict；找不到返回 None。
    index.json 的 file 字段形如 'GEN011/gen011_v01.json'——相对 models/ 目录。"""
    idx_path = os.path.join(models_dir, "index.json")
    if not os.path.isfile(idx_path):
        return None
    with open(idx_path, "r", encoding="utf-8") as f:
        idx = json.load(f)
    for e in idx.get("experiments", []):
        if e.get("id", "").upper() == model_id.upper():
            rel = e["runs"][0]["versions"][0]["file"]
            if rel.replace("\\", "/").lower().startswith("models/"):
                rel = rel[7:]          # 兼容带 models/ 前缀的旧条目（REC 系列）
            path = os.path.normpath(os.path.join(models_dir, rel))
            if not path.startswith(os.path.abspath(models_dir)):
                return None
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return None


# ---------------------------------------------------------------- OBJ

def to_obj(model):
    """OBJ 文本：每网格一个 g 组，面支持多边形（四边形原样输出）。"""
    out = ["# Landscape Agent Platform · OBJ export",
           "# units: meters, Z-up"]
    off = 0
    for m in model.get("meshes", []):
        name = (m.get("name") or "mesh").replace(" ", "_")
        out.append("g %s" % name)
        v = m["verts"]
        for i in range(0, len(v), 3):
            out.append("v %.5f %.5f %.5f" % (v[i], v[i + 1], v[i + 2]))
        for face in m["faces"]:
            out.append("f " + " ".join(str(k + 1 + off) for k in face))
        off += len(v) // 3
    return ("\n".join(out) + "\n").encode("utf-8")


# ---------------------------------------------------------------- DAE (COLLADA 1.4.1)

def to_dae(model):
    geoms, nodes = [], []
    for gi, m in enumerate(model.get("meshes", [])):
        gid = "geom%d" % gi
        name = saxutils.escape(m.get("name") or gid)
        v = m["verts"]
        pos = " ".join("%.5f" % x for x in v)
        nv = len(v) // 3
        faces = m["faces"]
        vcount = " ".join(str(len(f)) for f in faces)
        pidx = " ".join(str(k) for f in faces for k in f)
        geoms.append(
            '<geometry id="%s" name="%s"><mesh>'
            '<source id="%s-pos"><float_array id="%s-pos-arr" count="%d">%s</float_array>'
            '<technique_common><accessor source="#%s-pos-arr" count="%d" stride="3">'
            '<param name="X" type="float"/><param name="Y" type="float"/>'
            '<param name="Z" type="float"/></accessor></technique_common></source>'
            '<vertices id="%s-vtx"><input semantic="POSITION" source="#%s-pos"/></vertices>'
            '<polylist count="%d">'
            '<input semantic="VERTEX" source="#%s-vtx" offset="0"/>'
            '<vcount>%s</vcount><p>%s</p></polylist>'
            '</mesh></geometry>'
            % (gid, name, gid, gid, len(v), pos, gid, nv,
               gid, gid, len(faces), gid, vcount, pidx))
        nodes.append('<node id="node%d" name="%s">'
                     '<instance_geometry url="#%s"/></node>' % (gi, name, gid))
    doc = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">'
        '<asset><unit meter="1" name="meter"/><up_axis>Z_UP</up_axis></asset>'
        '<library_geometries>%s</library_geometries>'
        '<library_visual_scenes><visual_scene id="scene" name="LandscapePlatform">'
        '%s</visual_scene></library_visual_scenes>'
        '<scene><instance_visual_scene url="#scene"/></scene></COLLADA>'
        % ("".join(geoms), "".join(nodes)))
    return doc.encode("utf-8")


# ---------------------------------------------------------------- 3DM

def to_3dm(model, path):
    """写 Rhino .3dm（v7）。网格按名称落到同名图层。无 rhino3dm 返回 False。

    rhino3dm 版本差异：老版（py3.7 现装）Vertices/Faces/Layers 是属性，
    新版是方法——统一用 _acc 适配。"""
    try:
        import rhino3dm
    except ImportError:
        return False

    def _acc(x):
        return x() if callable(x) else x

    f = rhino3dm.File3dm()
    f.Settings.ModelUnitSystem = rhino3dm.UnitSystem.Meters
    layer_idx = {}
    for m in model.get("meshes", []):
        mesh = rhino3dm.Mesh()
        verts, faces = _acc(mesh.Vertices), _acc(mesh.Faces)
        v = m["verts"]
        for i in range(0, len(v), 3):
            verts.Add(v[i], v[i + 1], v[i + 2])
        for face in m["faces"]:
            if len(face) == 3:
                faces.AddFace(face[0], face[1], face[2])
            elif len(face) == 4:
                faces.AddFace(face[0], face[1], face[2], face[3])
            else:   # 防御：>4 顶点扇形三角化
                for k in range(1, len(face) - 1):
                    faces.AddFace(face[0], face[k], face[k + 1])
        _acc(mesh.Normals).ComputeNormals()
        mesh.Compact()
        name = m.get("name") or "mesh"
        if name not in layer_idx:
            ly = rhino3dm.Layer()
            ly.Name = name
            layer_idx[name] = _acc(f.Layers).Add(ly)
        attr = rhino3dm.ObjectAttributes()
        attr.Name = name
        attr.LayerIndex = layer_idx[name]
        _acc(f.Objects).AddMesh(mesh, attr)
    return f.Write(path, 7)
