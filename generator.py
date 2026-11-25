# -*- coding: utf-8 -*-
"""执行层：设计参数 spec → craftbot-model JSON（FluxPlot 查看器原生格式）。

v0.2 渲染管线：
  · 广场/水体用解析网格（48 边形真圆/精确方形），不再由 0.5m 栅格拼边
  · 其余类别走 0.5m 栅格，角点类别不一致的边界格自动细分到 0.25m
  · 绿地在广场/水体/道路边缘"出血"垫底，高类别薄板 z 更高直接盖住
  · 地形高度是位置的连续函数（角点属 lawn 才起伏）→ 路缘缓坡无硬崖
  · 平面元素一律闭合薄板 + 侧壁按边注册（共享边不画）→ 水密、体积恒正

格式对齐 REC01–04：
  collections: ["", "绿地", "广场", "道路", "TREE", "水体", "其它地面"]
  layer 索引（viewer/js/model-data.js LAYERS）:
    site=9, terrain=10, water=11, circulation=12, planting=13
"""
import hashlib
import math
import random

RES = 0.5            # 栅格分辨率（米）；边界格自动细分到 RES/2
COLLECTIONS = ["", "绿地", "广场", "道路", "TREE", "水体", "其它地面",
               "TREE·CONIFER", "TREE·PALM", "TREE·UMBRELLA",
               "家具", "亭", "小品", "道路·砖", "道路·砾", "道路·木"]
# 与 viewer/js/model-data.js 的 LAYERS 完全一致（元素的 layer 字段索引此表）
LAYERS = ["frame", "cladding ext", "cladding int", "interior", "roof",
          "floors", "foundations", "fixtures", "other",
          "site", "terrain", "water", "circulation", "planting",
          "structures", "furniture"]
(C_GREEN, C_PLAZA, C_ROAD, C_TREE, C_WATER, C_GROUND, C_CONIFER, C_PALM,
 C_UMBRELLA, C_FURN, C_PAV, C_MISC, C_ROAD_BRICK, C_ROAD_GRAVEL,
 C_ROAD_WOOD) = range(1, 16)
(L_SITE, L_TERRAIN, L_WATER, L_CIRC, L_PLANT,
 L_STRUCT, L_FURN) = 9, 10, 11, 12, 13, 14, 15
# 道路材质 → collection（铺装分色，viewer FLUX 按 collection 正则上色）
ROAD_MAT_COLL = {"asphalt": C_ROAD, "brick": C_ROAD_BRICK,
                 "gravel": C_ROAD_GRAVEL, "wood": C_ROAD_WOOD}

Z = {"site": 0.0, "lawn": 0.015, "water": 0.018, "road": 0.02, "plaza": 0.023}


# ---------------------------------------------------------------- 场地边界

def site_polygon(site):
    """返回 [(x,y), …] 逆时针多边形，中心在原点。"""
    w, d = site["width"] / 2.0, site["depth"] / 2.0
    shape = site["shape"]
    if shape == "triangle":
        return [(-w, -d), (w, -d), (0, d)]
    if shape == "lshape":
        return [(-w, -d), (w, -d), (w, 0), (0, 0), (0, d), (-w, d)]
    if shape == "circle":
        # 96 边形逼近（分类走 _make_cls_fn 的解析快速路径，不随边数变慢；
        # 96 边弦高差仅 ~1.6cm@R30，基底边缘视觉光滑）
        r = min(w, d)
        return [(r * math.cos(a), r * math.sin(a))
                for a in [2 * math.pi * i / 96 for i in range(96)]]
    if shape == "irregular":
        # 异形场地：prompt 种子化的径向抖动多边形。顶点按角序生成（角抖动 < 半步距，
        # 不打乱顺序）→ 关于原点的星形简单多边形，_in_poly/_edge_dist/_earcut 全安全；
        # 同一 prompt 形状可复现，不同 prompt 形状不同
        seed = int(hashlib.md5((site.get("prompt", "") + "|irregular")
                               .encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed)
        n = rng.choice((7, 8, 9))
        r = min(w, d)
        base = rng.uniform(0.0, 2.0 * math.pi)
        pts = []
        for i in range(n):
            a = base + 2.0 * math.pi * i / n + rng.uniform(-0.2, 0.2)
            rr = r * rng.uniform(0.62, 1.0)
            pts.append((rr * math.cos(a), rr * math.sin(a)))
        # Chaikin 角切细分 ×2：折线 → 平滑有机曲线边界（"马岩松式"流动感）。
        # 角切只削角不跨边，星形简单性与绕向保持，_earcut/_edge_dist 安全
        for _ in range(2):
            nxt = []
            m = len(pts)
            for i in range(m):
                p, q = pts[i], pts[(i + 1) % m]
                nxt.append((0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]))
                nxt.append((0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]))
            pts = nxt
        return pts
    if shape == "polygon":
        n = int(site.get("sides", 6))
        r = min(w, d)
        return [(r * math.cos(a), r * math.sin(a))
                for a in [2 * math.pi * i / n + math.pi / n for i in range(n)]]
    return [(-w, -d), (w, -d), (w, d), (-w, d)]            # rect


def _in_poly(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _edge_dist(x, y, poly):
    """点到多边形边界的最短距离（内部为正）。"""
    best = 1e9
    j = len(poly) - 1
    for i in range(len(poly)):
        x1, y1 = poly[j]
        x2, y2 = poly[i]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
        px, py = x1 + t * dx, y1 + t * dy
        best = min(best, math.hypot(x - px, y - py))
        j = i
    return best


# ---------------------------------------------------------------- 锚点与分类

def _centroid(poly):
    a = 0.0
    cx = cy = 0.0
    j = len(poly) - 1
    for i in range(len(poly)):
        x1, y1 = poly[j]
        x2, y2 = poly[i]
        cr = x1 * y2 - x2 * y1
        a += cr
        cx += (x1 + x2) * cr
        cy += (y1 + y2) * cr
        j = i
    a *= 0.5
    if abs(a) < 1e-9:
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        return sum(xs) / len(xs), sum(ys) / len(ys)
    return cx / (6 * a), cy / (6 * a)


def _anchors(spec, poly):
    # 质心而非 bbox 中心：L 形/三角形等异形场地 bbox 中心可能落在场外
    cx, cy = _centroid(poly)
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    pl = spec["plaza"]
    if pl["position"] == "entrance":          # 南入口内侧
        p_center = (cx, min(ys) + pl["radius"] + 3.0)
    else:
        p_center = (cx, cy)
    if not _in_poly(p_center[0], p_center[1], poly):
        p_center = (cx, cy)
    w = spec["water"]
    # 方位词 → 相对质心的偏移方向
    dirs = {"ne": (1, 1), "nw": (-1, 1), "se": (1, -1), "sw": (-1, -1),
            "n": (0, 1), "s": (0, -1), "e": (1, 0), "w": (-1, 0),
            "center": (0, 0)}
    dx, dy = dirs.get(w.get("position", "ne"), (1, 1))
    # 候选点必须离边界足够远（放得下水体圆）：不够就往质心逐步收，
    # 异形场地角隅可能紧贴边界（edge_dist≈0.5 → _fit_circle 会缩死）
    need = w["radius"] + 1.5
    for t in (0.22, 0.15, 0.09, 0.04, 0.0):
        w_center = (cx + dx * spec["site"]["width"] * t,
                    cy + dy * spec["site"]["depth"] * t)
        if _in_poly(w_center[0], w_center[1], poly) \
                and _edge_dist(w_center[0], w_center[1], poly) >= need:
            break
    if not _in_poly(w_center[0], w_center[1], poly):
        w_center = (cx, cy)
    return cx, cy, p_center, w_center


def _path_polyline(spec, poly):
    """小径/栈道的中线：横跨场地的种子化正弦曲线（东西向为主），
    采样成折线供分类与 shapely buffer 用。返回 [(x,y), …] 或 []。"""
    xs = [p[0] for p in poly]
    x0, x1 = min(xs), max(xs)
    seed = int(hashlib.md5((spec.get("prompt", "") + "|path")
                           .encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    amp = (x1 - x0) * rng.uniform(0.06, 0.10)               # 蜿蜒振幅（克制才像园路）
    freq = rng.uniform(1.2, 1.8) * 2 * math.pi / max(x1 - x0, 1.0)
    phase = rng.uniform(0, 2 * math.pi)
    pts = []
    n = max(16, int((x1 - x0) / 2.0))
    for i in range(n + 1):
        x = x0 + (x1 - x0) * i / n
        pts.append((x, amp * math.sin(x * freq + phase)))
    return pts


def _dist_to_polyline(x, y, pts):
    best = 1e9
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best


def _make_cls_fn(spec, poly, anchors):
    """单点分类函数 cls_at(x, y) -> lawn/road/plaza/water/None(场外)。
    优先级 water > plaza > road > lawn。高度/细分/发射全共用这一个函数，
    保证任意位置取值唯一 → 网格水密。"""
    cx, cy, p_center, w_center = anchors
    pl, wtr = spec["plaza"], spec["water"]
    # 小径中线（有 path 型道路时才有）
    path_pts = _path_polyline(spec, poly) \
        if any(r["kind"] == "path" for r in spec["roads"]) else []

    # 圆形场地快速路径：场内判定与到边界距离都有解析解，
    # 免去 48 边形 ray-cast + 逐边最近点（圆形场地生成耗时大头）
    if spec["site"]["shape"] == "circle":
        cr = min(spec["site"]["width"], spec["site"]["depth"]) / 2.0

        def inside(x, y, cr2=cr * cr):
            return x * x + y * y <= cr2

        def edge_d(x, y):
            return cr - math.hypot(x, y)
    else:
        def inside(x, y):
            return _in_poly(x, y, poly)

        def edge_d(x, y):
            return _edge_dist(x, y, poly)

    def cls_at(x, y):
        if not inside(x, y):
            return None
        if wtr["present"] and math.hypot(x - w_center[0], y - w_center[1]) <= wtr["radius"]:
            return "water"
        if pl["present"]:
            dx, dy = abs(x - p_center[0]), abs(y - p_center[1])
            if pl["shape"] == "square":
                if max(dx, dy) <= pl["radius"]:
                    return "plaza"
            elif math.hypot(dx, dy) <= pl["radius"]:
                return "plaza"
        for r in spec["roads"]:
            hw = r["width"] / 2.0
            if r["kind"] == "loop":
                if r["inset"] <= edge_d(x, y) <= r["inset"] + r["width"]:
                    return "road"
            elif r["kind"] == "cross":
                if abs(y - cy) <= hw or abs(x - cx) <= hw:
                    return "road"
            elif r["kind"] == "spine":
                if abs(y - cy) <= hw:
                    return "road"
            elif r["kind"] == "path":
                if _dist_to_polyline(x, y, path_pts) <= hw:
                    return "road"
        return "lawn"
    return cls_at


# ---------------------------------------------------------------- 三角化与基础网格

def _pt_in_tri(p, a, b, c):
    """点是否在三角形内（边界算在内）。"""
    d = [(b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]),
         (c[0] - b[0]) * (p[1] - b[1]) - (c[1] - b[1]) * (p[0] - b[0]),
         (a[0] - c[0]) * (p[1] - c[1]) - (a[1] - c[1]) * (p[0] - c[0])]
    return not ((d[0] < 0 or d[1] < 0 or d[2] < 0) and
                (d[0] > 0 or d[1] > 0 or d[2] > 0))


def _earcut(poly):
    """耳切法三角化简单多边形（凹多边形安全，三角形全部落在多边形内、
    朝向与原多边形一致）。扇形三角化对凹多边形会产生越界且朝向翻转的
    三角形（L 形缺口里的黑楔子），故场地基底必须用它。"""
    def area2(p):
        s = 0.0
        for i in range(len(p)):
            s += p[i - 1][0] * p[i][1] - p[i][0] * p[i - 1][1]
        return s
    ccw = area2(poly) > 0
    idx = list(range(len(poly)))
    tris = []
    guard = 0
    while len(idx) > 3 and guard < 4 * len(poly):
        guard += 1
        m = len(idx)
        clipped = False
        for k in range(m):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % m]
            a, b, c = poly[i0], poly[i1], poly[i2]
            cross = ((b[0] - a[0]) * (c[1] - a[1])
                     - (b[1] - a[1]) * (c[0] - a[0]))
            if abs(cross) < 1e-12 or (cross > 0) != ccw:
                continue                                # 凹角或退化 → 不是耳
            if any(j not in (i0, i1, i2) and _pt_in_tri(poly[j], a, b, c)
                   for j in idx):
                continue                                # 耳内有其他顶点
            tris.append((i0, i1, i2))
            del idx[k]
            clipped = True
            break
        if not clipped:
            # 兜底：退化输入退回扇形（宁可近似也不丢面）
            for k in range(1, len(idx) - 1):
                tris.append((idx[0], idx[k], idx[k + 1]))
            return tris
    if len(idx) == 3:
        tris.append((idx[0], idx[1], idx[2]))
    return tris


def _hexa_faces(mb, b):
    """标准六面体面表：8 顶点 = 底 0-3 逆时针 + 顶 4-7 逆时针，全部外向。"""
    mb.faces += [[b + 2, b + 1, b + 0], [b + 3, b + 2, b + 0],      # 底朝下
                 [b + 4, b + 5, b + 6], [b + 4, b + 6, b + 7]]      # 顶朝上
    for i in range(4):                                              # 四壁外向
        j = (i + 1) % 4
        mb.faces.append([b + i, b + j, b + 4 + j])
        mb.faces.append([b + i, b + 4 + j, b + 4 + i])


class MeshBuilder:
    def __init__(self):
        self.verts = []
        self.faces = []

    def poly(self, pts3):
        """任意平面 n 边形的扇形三角化（仅用于凸/小面片）。pts3 = [(x,y,z), …]"""
        b = len(self.verts) // 3
        for x, y, z in pts3:
            self.verts += [x, y, z]
        for i in range(1, len(pts3) - 1):
            self.faces.append([b, b + i, b + i + 1])

    def box(self, cx, cy, z0, z1, r, sides=6, apex=True):
        """正多边形截面柱（柱/干用）。apex=True 时带尖顶（柱顶小金字塔）；
        树冠等大半径用法必须 apex=False——否则尖顶高出 r×0.9，树冠变松果。"""
        b = len(self.verts) // 3
        for z in (z0, z1):
            for k in range(sides):
                a = 2 * math.pi * k / sides
                self.verts += [cx + r * math.cos(a), cy + r * math.sin(a), z]
        for k in range(sides):
            k2 = (k + 1) % sides
            self.faces.append([b + k, b + k2, b + sides + k2])
            self.faces.append([b + k, b + sides + k2, b + sides + k])
        for k in range(sides - 2):                          # 底面扇形（朝下）
            self.faces.append([b, b + sides - 1 - k, b + sides - 2 - k])
        if apex:
            top = len(self.verts) // 3
            self.verts += [cx, cy, z1 + r * 0.9]               # 尖顶
            for k in range(sides):
                self.faces.append([b + sides + k, b + sides + (k + 1) % sides, top])
        else:                                                # 平顶（朝上）
            for k in range(sides - 2):
                self.faces.append([b + sides, b + sides + 1 + k,
                                   b + sides + 2 + k])

    def cone(self, cx, cy, z0, z1, r0, r1, sides=12):
        """截头圆锥（r1≤0.05 → 尖锥）。水密：侧壁外向、底朝下、顶/尖朝上。"""
        b = len(self.verts) // 3
        for k in range(sides):                              # 底环
            a = 2 * math.pi * k / sides
            self.verts += [cx + r0 * math.cos(a), cy + r0 * math.sin(a), z0]
        for k in range(sides - 2):                          # 底面（朝下）
            self.faces.append([b, b + sides - 1 - k, b + sides - 2 - k])
        if r1 > 0.05:                                       # 截头：顶环 + 侧壁 + 顶面
            for k in range(sides):
                a = 2 * math.pi * k / sides
                self.verts += [cx + r1 * math.cos(a), cy + r1 * math.sin(a), z1]
            for k in range(sides):
                k2 = (k + 1) % sides
                self.faces.append([b + k, b + k2, b + sides + k2])
                self.faces.append([b + k, b + sides + k2, b + sides + k])
            for k in range(sides - 2):
                self.faces.append([b + sides, b + sides + 1 + k, b + sides + 2 + k])
        else:                                               # 尖锥：侧壁直接收成顶点
            apex = len(self.verts) // 3
            self.verts += [cx, cy, z1]
            for k in range(sides):
                self.faces.append([b + k, b + (k + 1) % sides, apex])

    def cuboid(self, cx, cy, z0, z1, hx, hy):
        """轴对齐长方体（hx/hy 为半宽）——亭/厕所/坐凳体块用。"""
        b = len(self.verts) // 3
        for z in (z0, z1):
            for px, py in ((cx - hx, cy - hy), (cx + hx, cy - hy),
                           (cx + hx, cy + hy), (cx - hx, cy + hy)):
                self.verts += [px, py, z]
        _hexa_faces(self, b)

    def beam(self, x0, y0, z0, x1, y1, z1, w, th=0.09):
        """任意方向、两端可不同高度的矩形梁（靠背/檩条/滑道/灯臂）。
        w = 梁宽，th = 竖直厚度。水密六面体。"""
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L * w / 2.0, dx / L * w / 2.0        # 左法线
        b = len(self.verts) // 3
        for dz in (-th, 0.0):
            for px, py, pz in ((x0 - nx, y0 - ny, z0), (x1 - nx, y1 - ny, z1),
                               (x1 + nx, y1 + ny, z1), (x0 + nx, y0 + ny, z0)):
                self.verts += [px, py, pz + dz]
        _hexa_faces(self, b)


# ---------------------------------------------------------------- 解析道路（可选：shapely）

try:
    from shapely.geometry import (Polygon as _ShpPolygon, box as _shp_box,
                                  LineString as _ShpLine)
    from shapely.ops import triangulate as _shp_triangulate
    try:                                   # shapely 2.x: BufferCapStyle 枚举
        from shapely import BufferCapStyle as _BCS
        _CAP_FLAT = _BCS.flat
    except ImportError:                    # shapely 1.8: cap_style 用 int（2=flat）
        _CAP_FLAT = 2
    HAS_SHAPELY = True
except ImportError:                      # 零依赖兜底：没装 shapely 走栅格道路
    HAS_SHAPELY = False


def _roads_analytic(spec, poly, anchors, road_meshes):
    """shapely 在场时的解析道路：环路=场地边界的双侧偏移环（圆角转角），
    十字/主路=矩形条 ∩ 场地，小径=种子化正弦中线 buffer ∩ 场地。
    road_meshes: {material: SlabMesh}——按铺装材质分网格（viewer 分色）。
    返回道路总面积；无 shapely 或无道路返回 None。
    三角化 = Delaunay + 覆盖过滤（带孔的环路环也安全），顶面统一 CCW。"""
    if not HAS_SHAPELY or not spec["roads"]:
        return None
    site = _ShpPolygon(poly)
    if not site.is_valid:
        return None
    cx, cy = anchors[0], anchors[1]
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    parts = []                             # (material, geometry)
    for r in spec["roads"]:
        hw = r["width"] / 2.0
        mat = r.get("material", "asphalt")
        if r["kind"] == "loop":
            outer = site.buffer(-r["inset"], resolution=16)
            inner = site.buffer(-(r["inset"] + r["width"]), resolution=16)
            g = outer.difference(inner)
        elif r["kind"] == "cross":
            bars = _shp_box(cx - hw, min(ys) - 1, cx + hw, max(ys) + 1).union(
                   _shp_box(min(xs) - 1, cy - hw, max(xs) + 1, cy + hw))
            g = site.intersection(bars)
        elif r["kind"] == "path":
            line = _ShpLine(_path_polyline(spec, poly))
            g = site.intersection(line.buffer(hw, resolution=8,
                                              cap_style=_CAP_FLAT))
        else:   # spine
            g = site.intersection(_shp_box(min(xs) - 1, cy - hw,
                                           max(xs) + 1, cy + hw))
        if not g.is_empty:
            parts.append((mat, g))
    if not parts:
        return None                        # 全部落空 → 让栅格兜底（防御）
    total = 0.0
    z = Z["road"]
    for mat, part in parts:
        mb = road_meshes.get(mat) or road_meshes["asphalt"]
        geoms = [part] if part.geom_type == "Polygon" else list(part.geoms)
        for g in geoms:
            if g.geom_type != "Polygon":
                continue
            total += g.area
            for tri in _shp_triangulate(g):
                if not g.covers(tri.representative_point()):
                    continue               # Delaunay 溢出凹角/孔洞的三角形
                pts = [(p[0], p[1]) for p in tri.exterior.coords[:3]]
                cross = ((pts[1][0] - pts[0][0]) * (pts[2][1] - pts[0][1])
                         - (pts[2][0] - pts[0][0]) * (pts[1][1] - pts[0][1]))
                if cross < 0:
                    pts.reverse()
                mb.tri(pts, [z, z, z])
    return total


# ---------------------------------------------------------------- 薄板

# 平面元素一律输出为"薄板"（顶面+底面+边界侧壁的闭合体）。
# 原因：viewer 的 parseModel 对负体积元素整体翻转三角形（修复内翻网格），
# 纯平面网格体积为浮点 epsilon，符号不定 → 同一批代码不同坐标结果随机。
# 闭合薄板体积确定性为正，永远不触发翻转。
SLAB_THICK = 0.04


class SlabMesh(MeshBuilder):
    """一批四边形 → 一个水密薄板网格。

    quad() 只画顶/底并登记四条边；finish() 给"只被一个 quad 拥有"的边
    （=区域边界）补侧壁。共享边不画 → 内部无多余面；侧壁绕向用水平法线
    (dy,-dx) 判定（与高度无关，陡坡不错向）。底面是顶面的 -t 等距平移，
    整个网格是高度场下的竖直柱体 → 散度体积 = 面积×t，恒正。"""

    def __init__(self):
        super().__init__()
        self._edges = {}   # key=(p1,p2) 排序后 -> [owner_cx, owner_cy, p1, p2, z1, z2]

    @staticmethod
    def _ekey(p1, p2):
        a = (round(p1[0], 6), round(p1[1], 6))
        b = (round(p2[0], 6), round(p2[1], 6))
        return (a, b) if a <= b else (b, a)

    def quad(self, pts2, zs):
        """pts2 = [(x,y)×4] 逆时针；zs = 各角点顶面高度。"""
        t = SLAB_THICK
        self.poly([(x, y, z) for (x, y), z in zip(pts2, zs)])            # 顶
        rev = list(zip(pts2, zs))[::-1]
        self.poly([(x, y, z - t) for (x, y), z in rev])                  # 底（反向）
        cx = sum(p[0] for p in pts2) / 4.0
        cy = sum(p[1] for p in pts2) / 4.0
        for i in range(4):
            p1, p2 = pts2[i], pts2[(i + 1) % 4]
            z1, z2 = zs[i], zs[(i + 1) % 4]
            k = self._ekey(p1, p2)
            if k in self._edges:
                self._edges[k] = None          # 被两个 quad 共享 → 非边界
            else:
                self._edges[k] = (cx, cy, p1, p2, z1, z2)

    def tri(self, pts2, zs):
        """三角形版本（pts2 逆时针 ×3）——解析道路的三角化输出走这里，
        边登记/侧壁逻辑与 quad 完全一致（共享边按坐标消去）。"""
        t = SLAB_THICK
        self.poly([(x, y, z) for (x, y), z in zip(pts2, zs)])
        rev = list(zip(pts2, zs))[::-1]
        self.poly([(x, y, z - t) for (x, y), z in rev])
        cx = sum(p[0] for p in pts2) / 3.0
        cy = sum(p[1] for p in pts2) / 3.0
        for i in range(3):
            p1, p2 = pts2[i], pts2[(i + 1) % 3]
            z1, z2 = zs[i], zs[(i + 1) % 3]
            k = self._ekey(p1, p2)
            if k in self._edges:
                self._edges[k] = None
            else:
                self._edges[k] = (cx, cy, p1, p2, z1, z2)

    def finish(self):
        t = SLAB_THICK
        for rec in self._edges.values():
            if rec is None:
                continue
            cx, cy, p1, p2, z1, z2 = rec
            quad = [(p1[0], p1[1], z1 - t), (p2[0], p2[1], z2 - t),
                    (p2[0], p2[1], z2), (p1[0], p1[1], z1)]
            ex, ey = (p1[0] + p2[0]) / 2.0 - cx, (p1[1] + p2[1]) / 2.0 - cy
            if (p2[1] - p1[1]) * ex - (p2[0] - p1[0]) * ey < 0:
                quad = [quad[0], quad[3], quad[2], quad[1]]
            self.poly(quad)


def _slab_polygon(mb, poly, ztop, thick=0.06):
    """整张多边形薄板（场地基底）。顶/底用耳切法，凹多边形不越界。"""
    zb = ztop - thick
    for i, j, k in _earcut(poly):                        # 顶（随多边形朝向，向上）
        b = len(mb.verts) // 3
        for p in (poly[i], poly[j], poly[k]):
            mb.verts += [p[0], p[1], ztop]
        mb.faces.append([b, b + 1, b + 2])
    for i, j, k in _earcut(poly):                        # 底（反序，向下）
        b = len(mb.verts) // 3
        for p in (poly[i], poly[k], poly[j]):
            mb.verts += [p[0], p[1], zb]
        mb.faces.append([b, b + 1, b + 2])
    cx, cy = _centroid(poly)
    j = len(poly) - 1
    for i in range(len(poly)):
        x1, y1 = poly[j]
        x2, y2 = poly[i]
        quad = [(x1, y1, zb), (x2, y2, zb), (x2, y2, ztop), (x1, y1, ztop)]
        ax, ay = x2 - x1, y2 - y1
        nx, ny = ay * (ztop - zb), -ax * (ztop - zb)
        ex, ey = (x1 + x2) / 2.0 - cx, (y1 + y2) / 2.0 - cy
        if nx * ex + ny * ey < 0:
            quad = [quad[0], quad[3], quad[2], quad[1]]
        mb.poly(quad)
        j = i


# ---------------------------------------------------------------- 地形与构件

def _terrain_z(spec, x, y, base):
    amp = spec["terrain"]["amplitude"]
    if not amp:
        return base
    return base + amp * (0.6 * math.sin(x * 0.12) * math.cos(y * 0.09)
                         + 0.4 * math.sin(x * 0.05 + 1.3) * math.sin(y * 0.07))


def _tree(mb, x, y, z, rng, species="broadleaf"):
    """五种参数化树型，尺寸带 ±15% 抖动（种子固定仍可复现）。
    树冠一律用锥体收顶（box 尖顶会把冠层戳成松果，勿用）。"""
    s = rng.uniform(0.85, 1.15)
    h = rng.uniform(0.9, 1.15)
    if species == "conifer":       # 针叶（松/柏）：矮干 + 三层叠锥收尖
        mb.box(x, y, z, z + 1.1 * h, 0.16 * s, sides=6, apex=False)
        mb.cone(x, y, z + 0.9 * h, z + 3.2 * h, 1.25 * s, 0.75 * s, sides=10)
        mb.cone(x, y, z + 2.5 * h, z + 4.4 * h, 0.85 * s, 0.42 * s, sides=10)
        mb.cone(x, y, z + 3.8 * h, z + 5.4 * h, 0.5 * s, 0.0, sides=10)
    elif species == "palm":        # 棕榈：高瘦干 + 七片下垂放射叶
        mb.box(x, y, z, z + 4.4 * h, 0.13 * s, sides=6, apex=False)
        zt = z + 4.4 * h
        for k in range(7):
            a = 2 * math.pi * k / 7 + rng.uniform(-0.2, 0.2)
            mb.beam(x, y, zt,
                    x + 1.9 * s * math.cos(a), y + 1.9 * s * math.sin(a),
                    zt - 0.6 * s, 0.26 * s, th=0.05)
        mb.box(x, y, zt - 0.15, zt + 0.35 * s, 0.28 * s, sides=6, apex=False)
    elif species == "umbrella":    # 伞冠（梧桐/樟）：高干 + 双锥扁伞盖
        mb.box(x, y, z, z + 3.4 * h, 0.2 * s, sides=8, apex=False)
        mb.cone(x, y, z + 3.3 * h, z + 4.0 * h, 0.6 * s, 1.85 * s, sides=12)
        mb.cone(x, y, z + 4.0 * h, z + 4.6 * h, 1.85 * s, 0.2 * s, sides=12)
    else:                          # broadleaf 阔叶：干 + 双层锥冠（圆润收顶）
        mb.box(x, y, z, z + 2.8 * h, 0.22 * s, sides=8, apex=False)
        mb.cone(x, y, z + 2.2 * h, z + 4.2 * h, 0.35 * s, 1.55 * s, sides=12)
        mb.cone(x, y, z + 3.8 * h, z + 5.2 * h, 1.55 * s, 0.28 * s, sides=12)


_SPECIES_MIX = (("broadleaf", 0.55), ("conifer", 0.23), ("umbrella", 0.22))


def _pick_species(rng, mode):
    if mode and mode != "mixed":
        return mode
    r = rng.random()
    acc = 0.0
    for sp, p in _SPECIES_MIX:
        acc += p
        if r < acc:
            return sp
    return "broadleaf"


def _pavilion(mb, x, y, z, kind, rng):
    """景观构筑四种：六角亭（重檐攒尖）/四方亭（四坡顶）/廊架（柱网+密檩）/
    现代平顶亭（四柱+悬浮薄板）。足尺可坐人尺度。"""
    s = rng.uniform(0.92, 1.08)
    if kind == "pergola":
        for sx in (-1.5, 1.5):
            for sy in (-1.2, 0.0, 1.2):          # 2×3 柱网（3.8×2.4m）
                mb.box(x + sx * s, y + sy * s, z, z + 2.4, 0.09, sides=4)
        for sy in (-1.2, 0.0, 1.2):              # 纵向主梁
            mb.beam(x - 1.9 * s, y + sy * s, z + 2.4,
                    x + 1.9 * s, y + sy * s, z + 2.4, 0.14, th=0.16)
        for k in range(9):                       # 横向密檩（出挑 0.4m）
            xx = x - 1.9 * s + k * 0.475 * s
            mb.beam(xx, y - 1.6 * s, z + 2.58, xx, y + 1.6 * s, z + 2.58,
                    0.09, th=0.1)
    elif kind == "square":
        for sx in (-1.5, 1.5):
            for sy in (-1.5, 1.5):
                mb.box(x + sx * s, y + sy * s, z, z + 2.6, 0.11, sides=4)
        mb.cone(x, y, z + 2.6, z + 4.1, 2.7 * s, 0.0, sides=4)
        mb.box(x, y, z + 4.1, z + 4.4, 0.1, sides=4)
    elif kind == "canopy":
        for sx in (-1.4, 1.4):
            for sy in (-1.4, 1.4):
                mb.box(x + sx * s, y + sy * s, z, z + 2.8, 0.08, sides=4)
        mb.beam(x - 1.9 * s, y - 1.9 * s, z + 2.86,
                x + 1.9 * s, y + 1.9 * s, z + 2.86, 3.9 * s, th=0.12)
    else:                                        # hex 六角亭：六柱 + 重檐攒尖
        for k in range(6):
            a = 2 * math.pi * k / 6 + math.pi / 6
            mb.box(x + 1.7 * s * math.cos(a), y + 1.7 * s * math.sin(a),
                   z, z + 2.5, 0.10, sides=4)
        mb.cone(x, y, z + 2.5, z + 3.25, 2.9 * s, 2.2 * s, sides=6)   # 下檐
        mb.cone(x, y, z + 3.2, z + 4.45, 1.9 * s, 0.0, sides=6)       # 上檐攒尖
        mb.box(x, y, z + 4.45, z + 4.8, 0.12, sides=6)                # 宝顶


def _bench(mb, x, y, z, yaw):
    """长椅：两足 + 1.7m 坐板 + 后倾靠背，面向 yaw（弧度）。"""
    dx, dy = math.cos(yaw), math.sin(yaw)
    px, py = -dy, dx                               # 坐板方向（垂直于面向）
    for sg in (-0.6, 0.6):
        mb.box(x + px * sg, y + py * sg, z, z + 0.42, 0.05, sides=4)
    mb.beam(x - px * 0.85, y - py * 0.85, z + 0.42,
            x + px * 0.85, y + py * 0.85, z + 0.42, 0.45, th=0.07)
    mb.beam(x - px * 0.85 - dx * 0.24, y - py * 0.85 - dy * 0.24, z + 0.5,
            x + px * 0.85 - dx * 0.24, y + py * 0.85 - dy * 0.24, z + 0.95,
            0.07, th=0.05)


def _lamp(mb, x, y, z, yaw):
    """庭院灯：3.4m 杆 + 0.7m 挑臂 + 灯头，臂朝道路一侧。"""
    mb.box(x, y, z, z + 3.4, 0.055, sides=6)
    ax, ay = x + math.cos(yaw) * 0.7, y + math.sin(yaw) * 0.7
    mb.beam(x, y, z + 3.28, ax, ay, z + 3.42, 0.07, th=0.06)
    mb.box(ax, ay, z + 3.24, z + 3.5, 0.14, sides=6)


def _bin(mb, x, y, z):
    mb.box(x, y, z, z + 0.62, 0.24, sides=8, apex=False)


def _toilet(mb, x, y, z):
    """公厕小品：4.4×3.2m 体块 + 挑檐 + 檐下标牌。"""
    mb.cuboid(x, y, z, z + 2.7, 2.2, 1.6)
    mb.cuboid(x, y, z + 2.7, z + 2.86, 2.35, 1.75)
    mb.cuboid(x, y - 1.79, z + 1.95, z + 2.35, 0.6, 0.05)


def _slide(mb, x, y, z, yaw):
    """儿童滑梯：1.2m 高台 + 倾斜滑道（带挡边）+ 后侧爬梯。"""
    dx, dy = math.cos(yaw), math.sin(yaw)
    mb.cuboid(x, y, z + 1.15, z + 1.3, 0.6, 0.6)             # 高台板
    for sx in (-0.5, 0.5):
        for sy in (-0.5, 0.5):
            mb.box(x + sx, y + sy, z, z + 1.15, 0.05, sides=4)
    mb.beam(x, y, z + 1.24, x + dx * 2.6, y + dy * 2.6, z + 0.12,
            0.56, th=0.06)                                  # 滑道
    for sg in (-0.27, 0.27):                                # 滑道挡边
        mb.beam(x - dy * sg, y + dx * sg, z + 1.36,
                x + dx * 2.6 - dy * sg, y + dy * 2.6 + dx * sg, z + 0.24,
                0.05, th=0.05)
    for sg in (-0.25, 0.25):                                # 爬梯侧梃
        mb.beam(x - dx * 1.05 - dy * sg, y - dy * 1.05 + dx * sg, z,
                x - dx * 0.45 - dy * sg, y - dy * 0.45 + dx * sg, z + 1.22,
                0.06, th=0.06)


# ---------------------------------------------------------------- 栅格发射

def _emit_grid(cls_fn, spec, x0, y0, nx, ny, builders, res=RES, max_depth=1,
               skip_road=False):
    """逐格分类发射。边界格（四角类别不一致）自动细分 max_depth 级
    （默认 1 级到 res/2；精细模式 2 级到 res/4）。
    plaza/water 栅格不发射（由解析网格覆盖），但有 lawn 角点时发平地绿地垫底。
    skip_road=True 时道路栅格不发射（解析道路已覆盖，避免双重几何）。

    返回各类发射面积（m²，按 quad 水平投影）统计。"""
    amp = spec["terrain"]["amplitude"]
    area = {"lawn": 0.0, "road": 0.0}

    def lawn_z(x, y):
        # 只有落在绿地的角点才带地形 → 路缘/广场边缘地形缓降到平
        if cls_fn(x, y) == "lawn":
            return _terrain_z(spec, x, y, Z["lawn"])
        return Z["lawn"]

    def emit_quad(cls, pts2):
        if cls == "lawn":
            builders["lawn"].quad(pts2, [lawn_z(px, py) for px, py in pts2])
            area["lawn"] += (pts2[1][0] - pts2[0][0]) * (pts2[3][1] - pts2[0][1])
        elif cls == "road" and not skip_road:
            builders["road"].quad(pts2, [Z["road"]] * 4)
            area["road"] += (pts2[1][0] - pts2[0][0]) * (pts2[3][1] - pts2[0][1])
        # plaza/water 的栅格不发射
        elif cls in ("plaza", "water"):
            pass

    def cell(gx0, gy0, size, depth):
        cxq, cyq = gx0 + size / 2.0, gy0 + size / 2.0
        c0 = cls_fn(cxq, cyq)
        pts2 = [(gx0, gy0), (gx0 + size, gy0),
                (gx0 + size, gy0 + size), (gx0, gy0 + size)]
        cc = [cls_fn(px, py) for px, py in pts2]
        # 分裂判据：角点类别不一致（含 None 场外），或中心类别不在角点集合里
        if depth < max_depth and (len(set(cc)) > 1 or c0 not in cc):
            h = size / 2.0
            cell(gx0, gy0, h, depth + 1)
            cell(gx0 + h, gy0, h, depth + 1)
            cell(gx0 + h, gy0 + h, h, depth + 1)
            cell(gx0, gy0 + h, h, depth + 1)
            return
        if c0 is None:
            return
        if c0 in ("plaza", "water"):
            # 出血垫底：有任何 lawn 角点 → 发一片平地绿地（被高 z 的解析薄板盖住）
            if "lawn" in cc:
                builders["lawn"].quad(pts2, [lawn_z(px, py) for px, py in pts2])
                area["lawn"] += size * size * 0.5      # 约半数露出，仅作统计
            return
        emit_quad(c0, pts2)

    for ix in range(nx):
        gx0 = x0 + ix * res
        for iy in range(ny):
            cell(gx0, y0 + iy * res, res, 0)
    return area


def _fit_circle(poly, center, r):
    """解析圆必须完整落在场地内，否则等比收缩（防越界悬出）。"""
    while r > 1.0:
        ok = True
        for i in range(16):
            a = 2 * math.pi * i / 16
            if not _in_poly(center[0] + r * math.cos(a),
                            center[1] + r * math.sin(a), poly):
                ok = False
                break
        if ok:
            return r
        r *= 0.85
    return r


# ---------------------------------------------------------------- 主入口

def generate(spec):
    """spec → (model_dict, stats)"""
    poly = site_polygon(spec["site"])
    anchors = _anchors(spec, poly)
    cx, cy, p_center, w_center = anchors
    cls_fn = _make_cls_fn(spec, poly, anchors)
    rng = random.Random(int(hashlib.md5(
        spec.get("prompt", "").encode("utf-8")).hexdigest()[:8], 16))

    builders = {"site": MeshBuilder()}
    lawn_mesh = SlabMesh()
    builders["lawn"] = lawn_mesh
    # 道路按铺装材质分网格（分色/导出分层）；grid 模式全部并入 asphalt
    road_meshes = {m: SlabMesh() for m in ROAD_MAT_COLL}
    builders["road"] = road_meshes["asphalt"]

    # 场地基底（整张多边形薄板，耳切法凹多边形安全）
    _slab_polygon(builders["site"], poly, Z["site"])

    # 分辨率：spec.resolution（默认 RES=0.5；精细模式 0.25 + 边界二次细分到 0.125）
    res = float(spec.get("resolution") or RES)
    max_depth = 2 if res <= 0.25 else 1

    # 解析道路（shapely 在场时自动启用，无则栅格兜底；按材质分流）
    analytic_road = _roads_analytic(spec, poly, anchors, road_meshes)

    # 栅格发射（lawn/road + 出血垫底；解析道路已接管时跳过道路格）
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    x0, y0 = min(xs), min(ys)
    nx = int((max(xs) - x0) / res) + 1
    ny = int((max(ys) - y0) / res) + 1
    area = _emit_grid(cls_fn, spec, x0, y0, nx, ny, builders, res, max_depth,
                      skip_road=analytic_road is not None)
    if analytic_road is not None:
        area["road"] = analytic_road
    lawn_mesh.finish()
    for mbm in road_meshes.values():
        mbm.finish()

    # 解析广场/水体（96 边形真圆薄板，z 高于垫底绿地）
    plaza_mb, water_mb = MeshBuilder(), MeshBuilder()
    pl, wtr = spec["plaza"], spec["water"]
    plaza_area = water_area = 0.0
    if pl["present"]:
        r = _fit_circle(poly, p_center, pl["radius"])
        if pl["shape"] == "square":
            pts = [(p_center[0] - r, p_center[1] - r), (p_center[0] + r, p_center[1] - r),
                   (p_center[0] + r, p_center[1] + r), (p_center[0] - r, p_center[1] + r)]
            plaza_area = 4 * r * r
        else:
            pts = [(p_center[0] + r * math.cos(2 * math.pi * i / 96),
                    p_center[1] + r * math.sin(2 * math.pi * i / 96))
                   for i in range(96)]
            plaza_area = math.pi * r * r
        _slab_polygon(plaza_mb, pts, Z["plaza"], SLAB_THICK)
    if wtr["present"]:
        r = _fit_circle(poly, w_center, wtr["radius"])
        pts = [(w_center[0] + r * math.cos(2 * math.pi * i / 96),
                w_center[1] + r * math.sin(2 * math.pi * i / 96))
               for i in range(96)]
        water_area = math.pi * r * r
        _slab_polygon(water_mb, pts, Z["water"], SLAB_THICK)

    meshes = []

    def emit(name, mb, collection, layer):
        if mb.faces:
            meshes.append({"name": name, "collection": collection,
                           "verts": [round(v, 4) for v in mb.verts],
                           "faces": mb.faces, "layer": layer})

    emit("Site B-001", builders["site"], C_GROUND, L_SITE)
    emit("Lawn L-001", builders["lawn"], C_GREEN, L_PLANT)
    _ROAD_NAME = {"asphalt": "Road R-001", "brick": "Road R-002 砖",
                  "gravel": "Road R-003 砾", "wood": "Boardwalk R-004 木"}
    for mat, mbm in road_meshes.items():
        emit(_ROAD_NAME.get(mat, "Road"), mbm, ROAD_MAT_COLL[mat], L_CIRC)
    emit("Plaza PL-001", plaza_mb, C_PLAZA, L_CIRC)
    emit("Water W-001", water_mb, C_WATER, L_WATER)

    # 树：lawn 格内随机选点（最小间距 3.2 m，种子=prompt 哈希，可复现）
    lawn_pts = []
    for ix in range(nx):
        for iy in range(ny):
            x, y = x0 + (ix + 0.5) * res, y0 + (iy + 0.5) * res
            if cls_fn(x, y) == "lawn":
                lawn_pts.append((x, y))
    rng.shuffle(lawn_pts)
    want = max(0, int(spec["trees"]["count"]))
    picked = []
    for x, y in lawn_pts:
        if len(picked) >= want:
            break
        if all((x - px) ** 2 + (y - py) ** 2 >= 3.6 ** 2 for px, py in picked):
            picked.append((x, y))
    sp_mode = spec["trees"].get("species", "mixed")
    _SP_COLL = {"broadleaf": C_TREE, "conifer": C_CONIFER,
                "palm": C_PALM, "umbrella": C_UMBRELLA}
    sp_count = {}
    for i, (x, y) in enumerate(picked, 1):
        sp = _pick_species(rng, sp_mode)
        sp_count[sp] = sp_count.get(sp, 0) + 1
        mb = MeshBuilder()
        _tree(mb, x, y, _terrain_z(spec, x, y, Z["lawn"]), rng, sp)
        meshes.append({"name": "Tree T-%03d %s" % (i, sp),
                       "collection": _SP_COLL[sp],
                       "verts": [round(v, 4) for v in mb.verts],
                       "faces": mb.faces, "layer": L_PLANT})

    # ------------------------------------------------ 构筑与家具布置
    # 选点规则：亭子/厕所/滑梯落在 lawn 且与树、广场、水体保持距离；
    # 座椅沿广场（或水体）边缘环布；路灯沿环路中线等距；垃圾桶挨着座椅。
    occupied = list(picked)                      # 已占用点（树）

    def free_spot(cands, clearance, edge_min, avoid_rings=()):
        for x, y in cands:
            if _edge_dist(x, y, poly) < edge_min:
                continue
            if any((x - px) ** 2 + (y - py) ** 2 < clearance ** 2
                   for px, py in occupied):
                continue
            if any(math.hypot(x - ax, y - ay) < ar for ax, ay, ar in avoid_rings):
                continue
            return (x, y)
        return None

    avoid = []
    if pl["present"]:
        avoid.append((p_center[0], p_center[1], pl["radius"] + 3.0))
    if wtr["present"]:
        avoid.append((w_center[0], w_center[1], wtr["radius"] + 3.0))

    pav = spec.get("pavilion") or {}
    n_pav = 0
    pav_spots = []
    if pav.get("present"):
        for k in range(min(int(pav.get("count", 1)), 3)):
            # 第二座起与已放亭子保持 12m 间距
            rings = avoid + [(sx, sy, 12.0) for sx, sy in pav_spots]
            spot = free_spot(lawn_pts, 3.5, 3.0, rings)
            if not spot:
                break
            pav_spots.append(spot)
            occupied.append(spot)
            mb = MeshBuilder()
            _pavilion(mb, spot[0], spot[1],
                      _terrain_z(spec, spot[0], spot[1], Z["lawn"]),
                      pav.get("kind", "hex"), rng)
            n_pav += 1
            meshes.append({"name": "Pavilion S-%03d" % n_pav,
                           "collection": C_PAV,
                           "verts": [round(v, 4) for v in mb.verts],
                           "faces": mb.faces, "layer": L_STRUCT})

    furn = spec.get("furniture") or {}
    furn_count = {"benches": 0, "lamps": 0, "bins": 0}

    def put_furn(name, builder_fn, coll=C_FURN, layer=L_FURN):
        mb = MeshBuilder()
        builder_fn(mb)
        meshes.append({"name": name, "collection": coll,
                       "verts": [round(v, 4) for v in mb.verts],
                       "faces": mb.faces, "layer": layer})

    # 座椅：广场（优先）或水体边缘环布，面向中心
    if furn.get("benches"):
        anchor, rr, zz = None, 0.0, Z["plaza"]
        if pl["present"]:
            anchor, rr = p_center, max(pl["radius"] - 0.9, 1.2)
        elif wtr["present"]:
            anchor, rr, zz = w_center, wtr["radius"] + 1.6, Z["lawn"]
        if anchor:
            n_b = max(3, min(6, int(2 * math.pi * rr / 6.0)))
            for k in range(n_b):
                a = 2 * math.pi * k / n_b
                bx, by = anchor[0] + rr * math.cos(a), anchor[1] + rr * math.sin(a)
                if not _in_poly(bx, by, poly) or cls_fn(bx, by) in ("water", None):
                    continue
                yaw = math.atan2(anchor[1] - by, anchor[0] - bx)
                z0 = zz if cls_fn(bx, by) == "plaza" \
                    else _terrain_z(spec, bx, by, Z["lawn"])
                put_furn("Bench F-%03d" % (furn_count["benches"] + 1),
                         lambda mb, x=bx, y=by, z=z0, w=yaw: _bench(mb, x, y, z, w))
                furn_count["benches"] += 1
                occupied.append((bx, by))

    # 路灯：沿环路中线每 ~45° 一盏（最多 8 盏），臂挑向道路；
    # 无环路时退到蜿蜒小径/主轴沿线（每 ~1/6 弧长一盏，立于路肩）
    loop = next((r for r in spec["roads"] if r["kind"] == "loop"), None)
    if furn.get("lamps") and loop:
        target = loop["inset"] + loop["width"] / 2.0
        max_r = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / 2.0
        for k in range(8):
            a = 2 * math.pi * k / 8 + math.pi / 8
            dx, dy = math.cos(a), math.sin(a)
            r_walk = 2.0
            spot = None
            while r_walk < max_r:                # 从质心向外走，找 edge_dist≈target
                px_, py_ = cx + dx * r_walk, cy + dy * r_walk
                if not _in_poly(px_, py_, poly):
                    break
                if abs(_edge_dist(px_, py_, poly) - target) <= 0.8:
                    spot = (px_, py_)
                    break
                r_walk += 0.5
            if spot and cls_fn(spot[0], spot[1]) == "road":
                yaw = math.atan2(cy - spot[1], cx - spot[0])
                put_furn("Lamp F-%03d" % (furn_count["lamps"] + 1),
                         lambda mb, x=spot[0], y=spot[1], w=yaw:
                         _lamp(mb, x, y, Z["road"], w))
                furn_count["lamps"] += 1
    elif furn.get("lamps"):
        path_r = next((r for r in spec["roads"] if r["kind"] == "path"), None)
        if path_r:
            pts = _path_polyline(spec, poly)
            hw = path_r["width"] / 2.0 + 0.7
            step = max(2, len(pts) // 6)
            for i in range(1, len(pts) - 1, step):
                if furn_count["lamps"] >= 6:
                    break
                x, y = pts[i]
                dx, dy = pts[i + 1][0] - x, pts[i + 1][1] - y
                L = math.hypot(dx, dy) or 1.0
                lx, ly = x - dy / L * hw, y + dx / L * hw     # 左路肩
                if _in_poly(lx, ly, poly) and cls_fn(lx, ly) == "lawn":
                    yaw = math.atan2(y - ly, x - lx)
                    put_furn("Lamp F-%03d" % (furn_count["lamps"] + 1),
                             lambda mb, x0=lx, y0=ly, w=yaw:
                             _lamp(mb, x0, y0,
                                   _terrain_z(spec, x0, y0, Z["lawn"]), w))
                    furn_count["lamps"] += 1

    # 垃圾桶：每隔一张座椅配一个（座椅旁 1.2m）
    if furn.get("bins"):
        bench_pts = [p for p in occupied
                     if cls_fn(p[0], p[1]) in ("plaza", "lawn")][::2]
        for bx, by in bench_pts[:3]:
            put_furn("Bin F-%03d" % (furn_count["bins"] + 1),
                     lambda mb, x=bx + 1.2, y=by:
                     _bin(mb, x, y, Z["plaza"]))
            furn_count["bins"] += 1

    # 公厕：南边缘内侧的 lawn 点（须越过环路带，隐蔽但可达）
    if furn.get("toilet"):
        loop_in = (loop["inset"] + loop["width"]) if loop else 0.0
        cands = [(cx + ax, min(ys) + d)
                 for d in (loop_in + 2.5, loop_in + 5.0, loop_in + 8.0)
                 for ax in (0, 6, -6, 12, -12)]
        spot = free_spot(cands, 4.0, 3.2)      # 不避让广场环：cls 已排除压广场
        if spot and cls_fn(spot[0], spot[1]) == "lawn":
            occupied.append(spot)
            put_furn("Toilet WC-001",
                     lambda mb: _toilet(mb, spot[0], spot[1], Z["lawn"]),
                     coll=C_MISC, layer=L_STRUCT)

    # 滑梯：广场侧后方的 lawn 上（面向广场，滑梯朝向 210° 象限）
    if furn.get("playground") and pl["present"]:
        a = math.radians(210)
        sx = p_center[0] + (pl["radius"] + 5.0) * math.cos(a)
        sy = p_center[1] + (pl["radius"] + 5.0) * math.sin(a)
        if _in_poly(sx, sy, poly) and cls_fn(sx, sy) == "lawn" \
                and _edge_dist(sx, sy, poly) >= 3.0:
            occupied.append((sx, sy))
            yaw = math.atan2(p_center[1] - sy, p_center[0] - sx)
            put_furn("Slide F-001",
                     lambda mb: _slide(mb, sx, sy,
                                       _terrain_z(spec, sx, sy, Z["lawn"]), yaw))

    model = {"format": "craftbot-model", "version": 1,
             "source": "platform/%s" % spec.get("prompt", "")[:60],
             "collections": COLLECTIONS, "layers": LAYERS,
             "boxes": [], "meshes": meshes}
    stats = {"elements": len(meshes), "trees": len(picked),
             "trees_requested": want, "species": sp_count,
             "pavilions": n_pav, "furniture": furn_count,
             "cells": nx * ny,
             "resolution": res,
             "road_mode": "analytic" if analytic_road is not None else "grid",
             "lawn_area": round(area["lawn"], 1), "road_area": round(area["road"], 1),
             "plaza_area": round(plaza_area, 1), "water_area": round(water_area, 1),
             "lawn_cells": int(area["lawn"] / (res * res)),
             "road_cells": int(area["road"] / (res * res)),
             "plaza_cells": int(plaza_area / (res * res)),
             "water_cells": int(water_area / (res * res))}
    return model, stats
