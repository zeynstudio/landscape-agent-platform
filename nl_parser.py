# -*- coding: utf-8 -*-
"""语义层：自然语言 → 结构化景观设计参数（spec）。

两级解析，对外只暴露 parse()：
1. Claude API（环境变量 ANTHROPIC_API_KEY 存在时优先；任何失败自动回落）
2. 规则解析（离线、确定性，覆盖工作营 Prompt 01–12 的句式与中文口语）

spec 结构（所有长度单位：米）：
{
  "site":   {"shape": "rect|triangle|lshape|polygon|circle",
             "width": 60.0, "depth": 40.0, "sides": 6},
  "roads":  [{"kind": "loop|cross|spine", "width": 4.0, "inset": 3.0}],
  "plaza":  {"present": true, "shape": "circle|square",
             "position": "center|entrance", "radius": 8.0},
  "water":  {"present": false, "position": "ne", "radius": 6.0},
  "trees":  {"count": 30},
  "terrain": {"amplitude": 0.0},
  "paving": {"type": "default"},
  "green_ratio": null
}
"""
import copy
import json
import os
import re
import urllib.request

DEFAULT_SPEC = {
    "site": {"shape": "rect", "width": 60.0, "depth": 40.0, "sides": 6},
    "roads": [{"kind": "loop", "width": 4.0, "inset": 3.0},
              {"kind": "cross", "width": 3.0, "inset": 0.0}],
    "plaza": {"present": True, "shape": "circle", "position": "center", "radius": 8.0},
    "water": {"present": False, "position": "ne", "radius": 6.0},
    "trees": {"count": 30, "species": "mixed"},
    "terrain": {"amplitude": 0.0},
    "paving": {"type": "default"},
    "pavilion": {"present": False, "kind": "hex", "count": 1},
    "furniture": {"benches": True, "lamps": True,
                  "bins": False, "toilet": False, "playground": False},
    "green_ratio": None,
    "style": None,
}

_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9}
NUM = r"(\d+(?:\.\d+)?|[一二两三四五六七八九十]{1,3})"


def _cn_num(s):
    """'23' / '二十三' / '十' / '三十' → int；失败返回 None"""
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _CN.get(a, 1) if a else 1
        ones = _CN.get(b, 0) if b else 0
        return float(tens * 10 + ones)
    return float(_CN[s]) if s in _CN else None


def _num(pattern, text, default=None, flags=re.I):
    m = re.search(pattern, text, flags)
    if not m:
        return default
    v = _cn_num(m.group(1))
    return v if v is not None else default


# ---------------------------------------------------------------- 规则解析

# 否定语义：否定词出现在关键词前 4 字内 → 该要素视为"明确不要"
_NEG = r"(?:没有|无|不要|不带|无需|不用|别|去掉|取消|排除)"

# 风格预设：识别"某某风格"后按风格惯例**只填空缺字段**。
# 两条铁律：显式说过的字段不动（在 audit 里）；显式否定的不复活（在 blocked 里）。
# 风格填充不进 audit——coverage 只衡量"听懂用户原话"的程度，风格知识不算用户说的。
_STYLE_PRESETS = [
    ("mad", r"马岩松|\bmad\b|山水城市",
     {"shape": "irregular", "terrain": 1.2,
      "roads": [{"kind": "loop", "width": 4.0, "inset": 4.0}],
      "water": {"present": True, "position": "center", "radius": 7.0}}),
    ("zaha", r"扎哈|zaha",
     {"shape": "irregular", "terrain": 0.6,
      "roads": [{"kind": "loop", "width": 3.5, "inset": 2.0}]}),
    ("minimal", r"极简|minimal|留白",
     {"trees": 12, "terrain": 0.0}),
]

# 风格外置：styles.json 存在时以文件为准（可编辑/可分享的 context profile，
# 班级之间传递 = "every cohort leaves the agent better" 的最小实现）。
# mtime 变化即热重载；文件损坏自动回落内置预设。
_STYLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "styles.json")
_STYLES_CACHE = {"mtime": None, "presets": None}


def _style_presets():
    try:
        mtime = os.path.getmtime(_STYLES_FILE)
    except OSError:
        mtime = None
    if mtime != _STYLES_CACHE["mtime"]:
        presets = list(_STYLE_PRESETS)
        if mtime is not None:
            try:
                with open(_STYLES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                custom = [(k, d["match"], d.get("fill", {}))
                          for k, d in data.items()
                          if isinstance(d, dict) and d.get("match")]
                if custom:
                    presets = custom
            except (ValueError, OSError):
                pass
        _STYLES_CACHE["mtime"] = mtime
        _STYLES_CACHE["presets"] = presets
    return _STYLES_CACHE["presets"]


def _negated(low, kw):
    return bool(re.search(_NEG + r"[^，。,；;！!]{0,4}?(?:" + kw + r")", low))


def _assign_sizes(t, low):
    """'半径X/直径X' 的尺寸归属：优先按前方最近归属词（'广场半径8米'），
    其次按后方紧跟的归属词（'直径10米的水池'）。"""
    owners = (("plaza", ["广场", "plaza"]),
              ("water", ["水体", "水池", "湖", "池塘", "pond", "water", "lake"]),
              ("site", ["场地", "公园", "边形", "地块", "site", "park"]))
    sizes = {}
    for m in re.finditer(r"(半径|直径)\s*" + NUM, t):
        v = _cn_num(m.group(2))
        if v is None:
            continue
        pre = t[:m.start()].lower()
        best, best_d = None, 99
        for kind, words in owners:
            for w in words:
                p = pre.rfind(w)
                if p >= 0 and len(pre) - p < best_d:
                    best, best_d = kind, len(pre) - p
        if best and best not in sizes:
            sizes[best] = v / 2.0 if m.group(1) == "直径" else v
    # 后向归属：归属词紧跟尺寸之后（允许"的"连接），仅作补充不覆盖前向结果
    for m in re.finditer(r"(半径|直径)\s*" + NUM + r"\s*(?:米|m)?\s*的?\s*"
                         r"(广场|水体|水池|湖|池塘|plaza|pond|water|lake)", low):
        v = _cn_num(m.group(2))
        if v is None:
            continue
        kind = "plaza" if m.group(3) in ("广场", "plaza") else "water"
        if kind not in sizes:
            sizes[kind] = v / 2.0 if m.group(1) == "直径" else v
    return sizes


def parse_rules(prompt):
    spec = copy.deepcopy(DEFAULT_SPEC)
    t = prompt.strip()
    low = t.lower()
    sizes = _assign_sizes(t, low)
    audit = set()          # 命中的语义字段（解析审计/低置信提示用）
    blocked = set()        # 显式否定的字段（风格预设不得复活）

    # ---- 场地形状（先剔除修饰广场的"圆形/方形广场"，避免误判场地形状）
    site_text = re.sub(r"(圆形|方形|圆)\s*广场", "", low)
    if re.search(r"三角|triangle", site_text):
        spec["site"]["shape"] = "triangle"
        audit.add("site.shape")
    elif re.search(r"l\s*形|l[- ]?shape", site_text):
        spec["site"]["shape"] = "lshape"
        audit.add("site.shape")
    elif re.search(r"圆形|圆地|circle|circular|round", site_text):
        spec["site"]["shape"] = "circle"
        audit.add("site.shape")
    elif re.search(r"六边|hexagon", site_text):
        spec["site"]["shape"], spec["site"]["sides"] = "polygon", 6
        audit.add("site.shape")
    elif re.search(r"八边|octagon", site_text):
        spec["site"]["shape"], spec["site"]["sides"] = "polygon", 8
        audit.add("site.shape")
    elif re.search(r"五边|pentagon", site_text):
        spec["site"]["shape"], spec["site"]["sides"] = "polygon", 5
        audit.add("site.shape")
    elif re.search(r"异形|不规则|自由形态|有机形态|曲边|irregular|organic|free[- ]?form", site_text):
        # 异形 ≠ 正多边形：生成径向抖动的不规则边界（见 generator.site_polygon）
        spec["site"]["shape"] = "irregular"
        audit.add("site.shape")
    elif re.search(r"多边|polygon", site_text):
        spec["site"]["shape"], spec["site"]["sides"] = "polygon", 6
        audit.add("site.shape")

    # ---- 场地尺寸：60x40 / 60×40 / 60米乘40米 / 长60宽40 / 60m by 40m
    m = re.search(NUM + r"\s*(?:米|m)?\s*[x×*乘]\s*" + NUM + r"\s*(?:米|m)?", low)
    if m:
        spec["site"]["width"] = _cn_num(m.group(1))
        spec["site"]["depth"] = _cn_num(m.group(2))
        audit.add("site.size")
    else:
        w = _num(r"长\s*" + NUM, t)
        d = _num(r"宽\s*" + NUM, t)
        if w:
            spec["site"]["width"] = w
            audit.add("site.size")
        if d:
            spec["site"]["depth"] = d
            audit.add("site.size")
    # 圆形/多边形场地：归属尺寸（_assign_sizes 已统一折算为半径）→ 外接直径
    if spec["site"]["shape"] in ("circle", "polygon") and "site" in sizes:
        spec["site"]["width"] = spec["site"]["depth"] = sizes["site"] * 2
        audit.add("site.size")

    # ---- 道路（宽度按路型分别识别：环路宽X / 十字路宽X / 通用 路宽X；否定词可整体关闭）
    roads = []
    if _negated(low, r"道路|公路|路网|路|road|path"):
        blocked.add("roads")
    else:
        road_w = (_num(r"(?<!环)(?<!十字)路宽\s*" + NUM, t)
                  or _num(r"road\s*width\s*" + NUM, low)
                  or _num(NUM + r"\s*(?:米|m)\s*宽?的?(?:道|路)", t))
        loop_w = _num(r"环路宽\s*" + NUM, t) or _num(r"loop[^0-9]{0,8}" + NUM, low)
        cross_w = _num(r"十字路?宽\s*" + NUM, t) or _num(r"cross[^0-9]{0,8}" + NUM, low)
        if re.search(r"环路|环形|环道|loop|ring", low) and not _negated(low, r"环路|环形|环道|loop|ring"):
            roads.append({"kind": "loop", "width": loop_w or road_w or 4.0, "inset": 3.0})
        if re.search(r"十字|cross|两条路", low) and not _negated(low, r"十字|cross"):
            roads.append({"kind": "cross", "width": cross_w or road_w or 3.0, "inset": 0.0})
        if re.search(r"主路|轴线|spine|main\s*road", low):
            roads.append({"kind": "spine", "width": road_w or 5.0, "inset": 0.0})
        # 小径/栈道：蜿蜒的漫游路径（曲线走 generator 的种子化正弦中线）
        if re.search(r"小径|步道|园路|游步道|曲径|栈道|path|trail|boardwalk", low) \
                and not _negated(low, r"小径|步道|园路|游步道|曲径|栈道|path|trail|boardwalk"):
            pw = (_num(r"(?:小径|步道|园路|栈道)宽\s*" + NUM, t)
                  or _num(r"path[^0-9]{0,8}" + NUM, low))
            wood = bool(re.search(r"木栈道|木质栈道|木步道|boardwalk", low))
            roads.append({"kind": "path", "width": pw or (2.4 if wood else 2.0),
                          "inset": 0.0, "material": "wood" if wood else "asphalt"})
        if not roads and re.search(r"道路|road|path|路", low):
            roads = copy.deepcopy(DEFAULT_SPEC["roads"])
            if road_w:
                for r in roads:
                    r["width"] = road_w
    # 铺装材质分流到道路（透水砖/砾石/沥青；木栈道已在 path 里处理）
    rmat = None
    if re.search(r"透水砖|砖铺|砖路|brick", low):
        rmat = "brick"
    elif re.search(r"砾石|碎石|gravel", low):
        rmat = "gravel"
    if rmat:
        for r in roads:
            r.setdefault("material", rmat)
            if r.get("material") == "asphalt":
                r["material"] = rmat
    spec["roads"] = roads
    if roads:
        audit.add("roads")

    # ---- 广场
    if _negated(low, r"广场|plaza"):
        blocked.add("plaza")
    has_plaza = bool(re.search(r"广场|plaza", low)) and "plaza" not in blocked
    spec["plaza"]["present"] = has_plaza
    if has_plaza:
        audit.add("plaza")
        if re.search(r"方形广场|square\s*plaza", low):
            spec["plaza"]["shape"] = "square"
        if re.search(r"入口|门口|entrance|entry", low):
            spec["plaza"]["position"] = "entrance"
        if "plaza" in sizes:
            spec["plaza"]["radius"] = sizes["plaza"]
        else:
            r = _num(r"radius\s*(?:of\s*)?" + NUM, low)   # 英文无主半径默认给广场
            if r:
                spec["plaza"]["radius"] = r

    # ---- 水体（含方位词；否定词关闭）
    if _negated(low, r"水体|水池|湖|池塘|pond|water|lake"):
        blocked.add("water")
    elif re.search(r"水体|水池|湖|池塘|pond|water|lake", low):
        spec["water"]["present"] = True
        audit.add("water")
        if "water" in sizes:
            spec["water"]["radius"] = sizes["water"]
        for word, pos in (("东北", "ne"), ("西北", "nw"), ("东南", "se"), ("西南", "sw"),
                          ("中央", "center"), ("中心", "center"),
                          ("北", "n"), ("南", "s"), ("东", "e"), ("西", "w")):
            if re.search(word + r"(?:角|侧|边|部)?[^，。,；;]{0,4}(?:水体|水池|湖|池塘)", t):
                spec["water"]["position"] = pos
                break

    # ---- 树木（"不要树"→0）
    c = (_num(NUM + r"\s*(?:棵|株)", t) or _num(NUM + r"\s*trees?", low)
         or _num(r"trees?\s*" + NUM, low))
    if c:
        spec["trees"]["count"] = int(c)
        audit.add("trees")
    if _negated(low, r"树|trees?"):
        spec["trees"]["count"] = 0
        blocked.add("trees")
    # 树种：不点名则 mixed 混植（阔叶/针叶/伞冠按比例随机）
    for pat, sp in ((r"松树|柏树|针叶|conifer|pine|cypress", "conifer"),
                    (r"棕榈|palm", "palm"),
                    (r"梧桐|樟树|伞形|umbrella", "umbrella"),
                    (r"樱花|银杏|阔叶|乔木|broadleaf", "broadleaf")):
        if re.search(pat, low) and not _negated(low, pat):
            spec["trees"]["species"] = sp
            audit.add("trees.species")
            break

    # ---- 亭子/廊架等构筑（否定关闭）
    if _negated(low, r"亭|廊架|花架|藤架|膜结构|构筑|pavilion|pergola"):
        blocked.add("pavilion")
        spec["pavilion"]["present"] = False
    else:
        m = re.search(r"六角亭|四角亭|方亭|廊架|花架|藤架|张拉膜|膜结构|"
                      r"凉亭|亭子|亭|pavilion|pergola", low)
        if m:
            kind = {"廊架": "pergola", "花架": "pergola", "藤架": "pergola",
                    "pergola": "pergola", "张拉膜": "canopy", "膜结构": "canopy",
                    "四角亭": "square", "方亭": "square"}.get(m.group(0), "hex")
            spec["pavilion"].update(present=True, kind=kind)
            audit.add("pavilion")
            npv = _num(NUM + r"\s*(?:个|座)亭", t)
            if npv:
                spec["pavilion"]["count"] = int(npv)

    # ---- 城市家具（座椅/路灯默认标配；垃圾桶/厕所/游乐设施点名才放）
    furn = spec["furniture"]
    if _negated(low, r"家具|城市家具|设施"):
        furn.update(benches=False, lamps=False, bins=False,
                    toilet=False, playground=False)
        blocked.add("furniture")
    else:
        if _negated(low, r"座椅|长椅|坐凳|bench"):
            furn["benches"] = False
            blocked.add("furniture")
        if _negated(low, r"路灯|照明|灯"):
            furn["lamps"] = False
            blocked.add("furniture")
        if re.search(r"垃圾桶|果皮箱|bins?", low) \
                and not _negated(low, r"垃圾桶|果皮箱|bins?"):
            furn["bins"] = True
            audit.add("furniture")
        if re.search(r"厕所|卫生间|公厕|toilet|wc", low) \
                and not _negated(low, r"厕所|卫生间|公厕|toilet|wc"):
            furn["toilet"] = True
            audit.add("furniture")
        if re.search(r"滑梯|儿童|游乐|playground|slide", low) \
                and not _negated(low, r"滑梯|儿童|游乐|playground|slide"):
            furn["playground"] = True
            audit.add("furniture")
        if re.search(r"座椅|长椅|坐凳|bench", low) \
                or re.search(r"路灯|照明|lamps?", low):
            audit.add("furniture")

    # ---- 地形（"平坦/不要地形"保持 0）
    if _negated(low, r"地形|起伏|微地形|terrain") or re.search(r"平坦|flat", low):
        blocked.add("terrain")
    elif re.search(r"地形|起伏|微地形|terrain|slope|mound", low):
        spec["terrain"]["amplitude"] = 1.0
        audit.add("terrain")
        h = _num(r"高差\s*" + NUM, t) or _num(NUM + r"\s*(?:米|m)\s*高差", t)
        if h:
            spec["terrain"]["amplitude"] = float(h)

    # ---- 绿地率（信息项）
    g = _num(r"绿地率?\s*" + NUM + r"\s*%", t) or _num(NUM + r"\s*%\s*绿", t)
    if g:
        spec["green_ratio"] = g / 100.0
        audit.add("green")

    # ---- 铺装（信息项）
    m = re.search(r"(塑胶|橡胶|砾石|碎石|透水砖|花岗岩|木质|石板|混凝土|沥青)\s*(?:铺装)?"
                  r"|(塑胶|橡胶)场地", t)
    if m:
        spec["paving"]["type"] = m.group(1) or m.group(2)
        audit.add("paving")

    # ---- 精细模式（分辨率语义，不占 audit 字段）
    if re.search(r"精细|高精度|高清|fine|high[\s-]?res", low):
        spec["resolution"] = 0.25

    # ---- 风格预设：只填空缺（audit 里没有的字段），显式否定的不复活（blocked）
    for skey, pat, fill in _style_presets():
        if not re.search(pat, low):
            continue
        spec["style"] = skey
        if "shape" in fill and "site.shape" not in audit:
            spec["site"]["shape"] = fill["shape"]
        if ("roads" in fill and "roads" not in audit
                and "roads" not in blocked and not spec["roads"]):
            spec["roads"] = copy.deepcopy(fill["roads"])
        if ("water" in fill and "water" not in audit
                and "water" not in blocked and not spec["water"]["present"]):
            spec["water"].update(fill["water"])
        if ("terrain" in fill and "terrain" not in audit
                and "terrain" not in blocked):
            spec["terrain"]["amplitude"] = fill["terrain"]
        if ("trees" in fill and "trees" not in audit
                and "trees" not in blocked):
            spec["trees"]["count"] = fill["trees"]
        break

    # ---- 解析审计：命中的字段与覆盖率（前端低置信提示 / 研究留痕）
    # negated 让"显式否定"也可解释（Ask 模式要区分"没有"与"你没说"）
    FIELDS = ("site.shape", "site.size", "roads", "plaza", "water",
              "trees", "terrain", "green", "paving")
    spec["audit"] = {"matched": sorted(audit), "negated": sorted(blocked),
                     "coverage": round(len(audit & set(FIELDS)) / len(FIELDS), 2)}
    return spec


# ---------------------------------------------------------------- Claude API

_SCHEMA_HINT = """把用户的景观设计需求解析为严格 JSON（只输出 JSON，不要任何解释）：
{"site":{"shape":"rect|triangle|lshape|polygon|circle","width":60,"depth":40,"sides":6},
 "roads":[{"kind":"loop|cross|spine|path","width":4,"inset":3,"material":"asphalt|brick|gravel|wood"}],
 "plaza":{"present":true,"shape":"circle|square","position":"center|entrance","radius":8},
 "water":{"present":false,"position":"ne","radius":6},
 "trees":{"count":30,"species":"mixed|broadleaf|conifer|palm|umbrella"},
 "terrain":{"amplitude":0},"paving":{"type":"default"},
 "pavilion":{"present":false,"kind":"hex|square|pergola|canopy","count":1},
 "furniture":{"benches":true,"lamps":true,"bins":false,"toilet":false,"playground":false},
 "green_ratio":null,"style":null}
单位：米。未提及的字段用以上默认值；没提广场 present=false，没提道路 roads=[]。
树种：松/柏/针叶→conifer，棕榈→palm，梧桐/樟→umbrella，樱/银杏/阔叶→broadleaf，未提→mixed。
构筑：亭→hex，四角亭→square，廊架/花架→pergola，张拉膜→canopy；没提 present=false。
path=蜿蜒小径/木栈道（栈道 material=wood）。家具：提了才置 true，否定词置 false。
style 仅当用户点名风格时填：马岩松/MAD/山水城市→"mad"，扎哈→"zaha"，极简/留白→"minimal"。"""


def _normalize(spec):
    out = copy.deepcopy(DEFAULT_SPEC)
    try:
        for k in ("site", "plaza", "water", "trees", "terrain", "paving",
                  "pavilion", "furniture"):
            if isinstance(spec.get(k), dict):
                out[k].update(spec[k])
        if isinstance(spec.get("roads"), list):
            out["roads"] = [r for r in spec["roads"] if isinstance(r, dict) and r.get("kind")]
        if spec.get("green_ratio") is not None:
            out["green_ratio"] = float(spec["green_ratio"])
        if isinstance(spec.get("style"), str) and spec["style"] in ("mad", "zaha", "minimal"):
            out["style"] = spec["style"]
        out["site"]["width"] = float(out["site"]["width"])
        out["site"]["depth"] = float(out["site"]["depth"])
        out["trees"]["count"] = int(out["trees"]["count"])
        if out["trees"].get("species") not in ("mixed", "broadleaf", "conifer",
                                               "palm", "umbrella"):
            out["trees"]["species"] = "mixed"
        if out["pavilion"].get("kind") not in ("hex", "square", "pergola", "canopy"):
            out["pavilion"]["kind"] = "hex"
        out["pavilion"]["count"] = max(1, min(3, int(out["pavilion"].get("count", 1))))
    except (TypeError, ValueError, KeyError):
        return None
    return out


def parse_llm(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 700,
        "system": _SCHEMA_HINT,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in payload.get("content", []))
    m = re.search(r"\{.*\}", text, re.S)
    return _normalize(json.loads(m.group(0))) if m else None


# ---------------------------------------------------------------- 入口

def parse(prompt, use_llm=True):
    """自然语言 → spec。LLM 失败/无 key 时回落规则解析。返回 (spec, engine)。"""
    spec, engine = None, "rules"
    if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            spec = parse_llm(prompt)
            engine = "claude" if spec else "rules"
        except Exception:
            spec, engine = None, "rules"
    if spec is None:
        spec = parse_rules(prompt)
    spec["prompt"] = prompt
    return spec, engine


# ---------------------------------------------------------------- Ask 模式（解释）

_SHAPE_CN = {"rect": "矩形", "triangle": "三角形", "lshape": "L形",
            "circle": "圆形", "irregular": "异形有机曲线边界"}
_KIND_CN = {"loop": "环路", "cross": "十字路", "spine": "主路/轴线"}
_POS_CN = {"center": "中央", "entrance": "入口",
           "ne": "东北角", "nw": "西北角", "se": "东南角", "sw": "西南角",
           "n": "北侧", "s": "南侧", "e": "东侧", "w": "西侧"}
_STYLE_CN = {"mad": "马岩松/山水城市", "zaha": "扎哈", "minimal": "极简/留白"}


def explain_spec(spec):
    """把 spec 讲成人话（Ask 模式，纯规则模板，不需要 LLM）。
    每条标注来源：【你说的】原话命中 /【风格补】风格预设填的空 /
    【默认】兜底值 /【你排除的】显式否定——让机器的推理可读（黑箱批判力）。
    返回字符串列表，一行一条。"""
    audit = spec.get("audit") or {}
    matched = set(audit.get("matched", []))
    negated = set(audit.get("negated", []))
    style = spec.get("style")
    dflt = DEFAULT_SPEC

    def tag(field, current, default):
        if field in matched:
            return "【你说的】"
        if field in negated:
            return "【你排除的】"
        if style and current != default:
            return "【%s风格补的】" % _STYLE_CN.get(style, style)
        return "【默认】"

    L = []
    site = spec["site"]
    if site["shape"] == "polygon":
        shape_cn = "%d边形" % site.get("sides", 6)
    else:
        shape_cn = _SHAPE_CN.get(site["shape"], site["shape"])
    size_note = "" if "site.size" in matched else "（尺寸未指定，按默认 60×40）"
    if (site["width"], site["depth"]) != (dflt["site"]["width"], dflt["site"]["depth"]):
        size_note = ""
    L.append("场地：%s，%g×%g米 %s%s"
             % (shape_cn, site["width"], site["depth"],
                tag("site.shape", site["shape"], dflt["site"]["shape"]), size_note))

    if spec["roads"]:
        parts = ["%s宽%g米" % (_KIND_CN.get(r["kind"], r["kind"]), r["width"])
                 for r in spec["roads"]]
        L.append("道路：%s %s" % (" ＋ ".join(parts),
                                  tag("roads", spec["roads"], dflt["roads"])))
    else:
        L.append("道路：无 %s" % ("【你排除的】" if "roads" in negated else "【未提及】"))

    pl = spec["plaza"]
    if pl["present"]:
        L.append("广场：%s%s，半径%g米 %s"
                 % (_POS_CN.get(pl["position"], pl["position"]),
                    "圆形" if pl["shape"] == "circle" else "方形", pl["radius"],
                    tag("plaza", True, dflt["plaza"]["present"])))
    else:
        L.append("广场：无 %s" % ("【你排除的】" if "plaza" in negated else "【未提及】"))

    wtr = spec["water"]
    if wtr["present"]:
        L.append("水体：%s，半径%g米 %s"
                 % (_POS_CN.get(wtr["position"], wtr["position"]), wtr["radius"],
                    tag("water", True, dflt["water"]["present"])))
    else:
        L.append("水体：无 %s" % ("【你排除的】" if "water" in negated else "【未提及】"))

    nt = spec["trees"]["count"]
    if "trees" in negated or nt == 0:
        L.append("树木：0 棵 %s" % ("【你排除的】" if "trees" in negated else ""))
    else:
        note = tag("trees", nt, dflt["trees"]["count"])
        if "trees" not in matched and "trees" not in negated \
                and not style and nt == dflt["trees"]["count"]:
            note = "【默认 30 棵】"
        L.append("树木：%d 棵（绿地内随机布点，最小间距 3.6 米）%s" % (nt, note))
    sp = spec["trees"].get("species", "mixed")
    _SP_CN = {"conifer": "针叶（松/柏）", "palm": "棕榈",
              "umbrella": "伞冠（梧桐/樟）", "broadleaf": "阔叶"}
    if sp != "mixed":
        L.append("树种：%s %s" % (_SP_CN.get(sp, sp),
                                  tag("trees.species", sp, "mixed")))

    pav = spec.get("pavilion") or {}
    _PAV_CN = {"hex": "六角亭（重檐攒尖）", "square": "四方亭（四坡顶）",
               "pergola": "廊架（柱网+密檩）", "canopy": "现代平顶亭"}
    if pav.get("present"):
        L.append("构筑：%s ×%d，落在绿地 %s"
                 % (_PAV_CN.get(pav.get("kind"), pav.get("kind")),
                    int(pav.get("count", 1)),
                    tag("pavilion", True, dflt["pavilion"]["present"])))
    elif "pavilion" in negated:
        L.append("构筑：无 【你排除的】")

    furn = spec.get("furniture") or {}
    on = [k for k in ("benches", "lamps", "bins", "toilet", "playground")
          if furn.get(k)]
    _FURN_CN = {"benches": "座椅", "lamps": "路灯", "bins": "垃圾桶",
                "toilet": "公厕", "playground": "儿童滑梯"}
    if "furniture" in negated and not on:
        L.append("家具：无 【你排除的】")
    elif on:
        ftag = "【你说的】" if "furniture" in matched else "【默认标配：座椅+路灯】"
        L.append("家具：%s %s" % ("、".join(_FURN_CN[k] for k in on), ftag))

    amp = spec["terrain"]["amplitude"]
    if amp and amp > 0:
        L.append("地形：有起伏，最大高差 %g 米 %s"
                 % (amp, tag("terrain", amp, dflt["terrain"]["amplitude"])))
    else:
        L.append("地形：平整 %s" % ("【你排除的】" if "terrain" in negated
                                    else "【默认】"))

    pv = spec["paving"]["type"]
    if pv and pv != "default":
        L.append("铺装：%s【你说的】（信息项，暂不入几何）" % pv)

    if style:
        filled = []
        if "site.shape" not in matched and site["shape"] != dflt["site"]["shape"]:
            filled.append("异形边界")
        if "roads" not in matched and spec["roads"] and spec["roads"] != dflt["roads"]:
            filled.append("环路")
        if "water" not in matched and spec["water"]["present"]:
            filled.append("水景")
        if "terrain" not in matched and amp != dflt["terrain"]["amplitude"]:
            filled.append("地形起伏")
        if "trees" not in matched and nt != dflt["trees"]["count"]:
            filled.append("树量 %d 棵" % nt)
        L.insert(0, "风格：识别为「%s」→ 按风格惯例补齐：%s"
                 % (_STYLE_CN.get(style, style),
                    "、".join(filled) if filled else "无（你都已说明白）"))

    cov = audit.get("coverage")
    if cov is not None:
        L.append("—— 语义覆盖率 %d%%（你的原话被接住的比例）" % round(cov * 100))
        if cov < 0.35:
            L.append("提示：很多描述没被接住，结果偏默认方案。试试带数字的句式："
                     "「60×40米、环路宽4米、中央广场半径8米、30棵树」；"
                     "或配置 ANTHROPIC_API_KEY 让 Claude 来解析自由语言。")
    return L
