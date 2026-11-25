# -*- coding: utf-8 -*-
"""平台自检工作流（selftest）——解析断言 + 生成体检 + API 冒烟 + 结果落档。

用法：
  py tools/selftest.py            # 全量（含 API 冒烟，需服务器在跑）
  py tools/selftest.py --no-api   # 只跑本地解析+生成

结果追加到 logs/devlog.md（机器生成表）并打印摘要。
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import generator                       # noqa: E402
import nl_parser as parser             # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVLOG = os.path.join(BASE, "logs", "devlog.md")
API = "http://localhost:%s" % os.environ.get("PLATFORM_PORT", "8400")

# ---------------------------------------------------------------- 用例库
# (名称, prompt, [ (检查点描述, spec -> bool) ], 期望备注)
CASES = [
    ("矩形全要素", "生成一个60×40米的矩形公园：中央圆形广场半径8米，环路宽4米加十字路，40棵树，东北角一个水池",
     [("矩形", lambda s: s["site"]["shape"] == "rect"),
      ("60x40", lambda s: s["site"]["width"] == 60 and s["site"]["depth"] == 40),
      ("广场r8", lambda s: s["plaza"]["present"] and s["plaza"]["radius"] == 8),
      ("环路+十字", lambda s: {r["kind"] for r in s["roads"]} == {"loop", "cross"}),
      ("环路宽4", lambda s: any(r["kind"] == "loop" and r["width"] == 4 for r in s["roads"])),
      ("树40", lambda s: s["trees"]["count"] == 40),
      ("水体ne", lambda s: s["water"]["present"] and s["water"]["position"] == "ne")], ""),
    ("六边形", "六边形口袋公园，直径40米，环路加中央圆形广场半径6米，20棵树，西南角一个水池",
     [("六边形", lambda s: s["site"]["shape"] == "polygon" and s["site"]["sides"] == 6),
      ("直径40", lambda s: s["site"]["width"] == 40),
      ("广场r6", lambda s: s["plaza"]["radius"] == 6),
      ("水体sw", lambda s: s["water"]["present"] and s["water"]["position"] == "sw")], ""),
    ("L形地形", "L形场地80乘60，环路宽4米，中央广场，35棵树，地形起伏高差1米",
     [("L形", lambda s: s["site"]["shape"] == "lshape"),
      ("高差1", lambda s: s["terrain"]["amplitude"] == 1.0),
      ("树35", lambda s: s["trees"]["count"] == 35)], ""),
    ("三角形", "三角形社区公园，50x50米，十字路宽3米，中央广场，25棵树",
     [("三角形", lambda s: s["site"]["shape"] == "triangle"),
      ("十字宽3", lambda s: any(r["kind"] == "cross" and r["width"] == 3 for r in s["roads"]))], ""),
    ("英文", "a 60x40m rectangular park: central plaza radius 8m, loop road 4m wide, 40 trees, a pond",
     [("矩形", lambda s: s["site"]["shape"] == "rect"),
      ("60x40", lambda s: s["site"]["width"] == 60 and s["site"]["depth"] == 40),
      ("广场r8", lambda s: s["plaza"]["radius"] == 8),
      ("loop", lambda s: any(r["kind"] == "loop" for r in s["roads"])),
      ("树40", lambda s: s["trees"]["count"] == 40),
      ("pond", lambda s: s["water"]["present"])], ""),
    ("入口方形广场", "80乘60米场地，南入口方形广场，环路宽5米，60棵树，地形起伏高差1.5米",
     [("方形广场", lambda s: s["plaza"]["shape"] == "square"),
      ("入口", lambda s: s["plaza"]["position"] == "entrance"),
      ("环路宽5", lambda s: any(r["kind"] == "loop" and r["width"] == 5 for r in s["roads"])),
      ("高差1.5", lambda s: s["terrain"]["amplitude"] == 1.5)], ""),
    ("中文数字", "六十乘四十米场地，二十棵树，中央广场",
     [("60x40", lambda s: s["site"]["width"] == 60 and s["site"]["depth"] == 40),
      ("树20", lambda s: s["trees"]["count"] == 20)], ""),
    ("水体直径", "60乘40米公园，东北角一个直径10米的水池，30棵树",
     [("水r5", lambda s: s["water"]["present"] and s["water"]["radius"] == 5)], ""),
    ("八边形", "八边形广场公园，直径50米，环路，15棵树",
     [("八边形", lambda s: s["site"]["shape"] == "polygon" and s["site"]["sides"] == 8)], ""),
    ("圆形场地", "圆形公园，直径60米，中央广场，环路，20棵树",
     [("圆形", lambda s: s["site"]["shape"] == "circle"),
      ("直径60", lambda s: s["site"]["width"] == 60)], ""),
    ("异形场地", "异形场地公园，40乘40米，环路，中央广场，20棵树",
     [("异形→irregular", lambda s: s["site"]["shape"] == "irregular")],
     "异形≠正多边形≠矩形"),
    ("不规则+否定", "不规则形状场地，30乘30米，不要路，15棵树",
     [("不规则→irregular", lambda s: s["site"]["shape"] == "irregular"),
      ("无路", lambda s: s["roads"] == [])], "否定组合"),
    ("马岩松风格", "我想要一个马岩松一样的异形公园，红色的塑胶场地有树有厕所有滑滑梯",
     [("风格识别", lambda s: s.get("style") == "mad"),
      ("异形", lambda s: s["site"]["shape"] == "irregular"),
      ("地形填充", lambda s: s["terrain"]["amplitude"] > 0),
      ("环路填充", lambda s: any(r["kind"] == "loop" for r in s["roads"])),
      ("水景填充", lambda s: s["water"]["present"]),
      ("塑胶", lambda s: s["paving"]["type"] == "塑胶")], "风格预设填空，用户原话"),
    ("风格让位否定", "马岩松风格公园，不要水体，不要地形",
     [("mad", lambda s: s.get("style") == "mad"),
      ("无水", lambda s: not s["water"]["present"]),
      ("无地形", lambda s: s["terrain"]["amplitude"] == 0)], "否定优先于风格"),
    ("精细模式", "60乘40米公园，环路，中央广场，20棵树，精细",
     [("resolution=0.25", lambda s: s.get("resolution") == 0.25)], "0.25m 栅格"),
    # ---- 否定/边界语义（易错点）----
    ("否定·没有广场", "60乘40米公园，环路宽4米，没有广场，30棵树",
     [("无广场", lambda s: not s["plaza"]["present"])], "否定词"),
    ("否定·不要路", "50乘40米场地，中央广场，不要道路，25棵树",
     [("无路", lambda s: s["roads"] == [])], "否定词"),
    ("否定·不要树", "50乘40米场地，中央广场加环路，不要树",
     [("树0", lambda s: s["trees"]["count"] == 0)], "否定词"),
    ("否定·不要水", "60乘40米公园，环路，中央广场，不要水池，20棵树",
     [("无水", lambda s: not s["water"]["present"])], "否定词"),
    ("超大树数", "60x40米公园，5000棵树",
     [("请求5000", lambda s: s["trees"]["count"] == 5000)], "树不足提示路径"),
    ("乱码", "asdfgh qwerty",
     [("返回默认spec", lambda s: s["site"]["shape"] == "rect")], "应低置信提示"),
    ("无尺寸默认", "带环路和中央广场的社区公园",
     [("默认60x40", lambda s: s["site"]["width"] == 60 and s["site"]["depth"] == 40)], ""),
    ("中英混排", "site是三角形，50x50米，central plaza加loop，trees要30棵",
     [("三角形", lambda s: s["site"]["shape"] == "triangle"),
      ("树30", lambda s: s["trees"]["count"] == 30)], ""),
    # ---- v0.9 构件库语义（树种/构筑/家具/小径材质）----
    ("树种·松树", "50×35米公园，种20棵松树，环路宽3米",
     [("针叶", lambda s: s["trees"].get("species") == "conifer"),
      ("树20", lambda s: s["trees"]["count"] == 20)], "v0.9 树种"),
    ("构筑·六角亭", "60×40米公园，中央广场，有一个六角亭，30棵树",
     [("有亭", lambda s: s["pavilion"]["present"]),
      ("六角", lambda s: s["pavilion"]["kind"] == "hex")], "v0.9 构筑"),
    ("构筑·廊架", "公园东侧一个廊架，环路，25棵树",
     [("廊架", lambda s: s["pavilion"]["present"]
       and s["pavilion"]["kind"] == "pergola")], "v0.9 构筑"),
    ("小径·木栈道", "50×35米公园，一条木栈道小径宽2.4米，环路宽3米，20棵树",
     [("path", lambda s: any(r["kind"] == "path" for r in s["roads"])),
      ("木", lambda s: any(r.get("material") == "wood" for r in s["roads"])),
      ("宽2.4", lambda s: any(r["kind"] == "path" and r["width"] == 2.4
                              for r in s["roads"]))], "v0.9 蜿蜒小径"),
    ("家具·厕所滑梯", "60×40米公园，中央广场，环路，有厕所有滑滑梯，30棵树",
     [("厕所", lambda s: s["furniture"]["toilet"]),
      ("滑梯", lambda s: s["furniture"]["playground"])], "v0.9 家具"),
    ("家具·不要座椅", "40×30米公园，环路，不要座椅，15棵树",
     [("无座椅", lambda s: not s["furniture"]["benches"])], "v0.9 否定"),
]


def _signed_vol(msh):
    v, F = msh["verts"], msh["faces"]
    vol = 0.0
    for tri in F:
        for i in range(1, len(tri) - 1):
            a, b, c = tri[0] * 3, tri[i] * 3, tri[i + 1] * 3
            vol += (v[a] * (v[b + 1] * v[c + 2] - v[b + 2] * v[c + 1])
                    + v[a + 1] * (v[b + 2] * v[c] - v[b] * v[c + 2])
                    + v[a + 2] * (v[b] * v[c + 1] - v[b + 1] * v[c])) / 6.0
    return vol


def run_battery():
    rows = []
    for name, prompt, checks, note in CASES:
        t0 = time.time()
        try:
            spec, engine = parser.parse(prompt, use_llm=False)
        except Exception as e:
            rows.append((name, "解析异常 %s" % e, "-", "-", "-", note))
            continue
        fails = [desc for desc, ok in checks if not _safe(ok, spec)]
        parse_ok = "✓" if not fails else "✗ " + ",".join(fails)
        # ---- 生成体检
        try:
            t1 = time.time()
            model, stats = generator.generate(spec)
            neg = [m["name"] for m in model["meshes"] if _signed_vol(m) <= 0]
            nbytes = len(json.dumps(model))
            gen_ok = ("✓ %.1fs/%.1fMB" % (time.time() - t1, nbytes / 1e6)
                      if not neg else "✗负体积:" + ",".join(neg))
            trees = "%d/%d" % (stats["trees"], stats["trees_requested"])
        except Exception as e:
            gen_ok, trees = "✗异常 %s" % e, "-"
        rows.append((name, parse_ok, gen_ok, trees,
                     "%.2fs" % (time.time() - t0), note))
    return rows


def _safe(fn, spec):
    try:
        return bool(fn(spec))
    except Exception:
        return False


def run_api():
    out = []
    try:
        h = json.loads(urllib.request.urlopen(API + "/api/health", timeout=3).read())
        out.append(("GET /api/health", "✓" if h.get("ok") else "✗"))
    except Exception as e:
        out.append(("GET /api/health", "✗ %s（跳过 API 冒烟）" % e))
        return out, False

    def post(body_obj, raw=None):
        data = raw if raw is not None else json.dumps(body_obj).encode("utf-8")
        req = urllib.request.Request(API + "/api/generate", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=120)
            return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))

    st, d = post({"prompt": ""})
    out.append(("POST 空prompt→400", "✓" if st == 400 and not d.get("ok") else "✗ %s" % st))
    st, d = post({"prompt": "x" * 600})
    out.append(("POST 600字→400", "✓" if st == 400 else "✗ %s" % st))
    st, d = post({"prompt": "冒烟测试：40乘30米矩形场地，中央广场，环路，10棵树"})
    ok = st == 200 and d.get("ok")
    out.append(("POST 正常生成", "✓ %s" % d.get("id") if ok else "✗ %s %s" % (st, d.get("error"))))
    # 导出冒烟：obj/dae 必通；3dm 视 rhino3dm 可用性；坏 id 应 404
    if ok:
        mid = d["id"]

        def get(path):
            try:
                r = urllib.request.urlopen(API + path, timeout=60)
                return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        st, body = get("/api/export?id=%s&fmt=obj" % mid)
        out.append(("GET export obj", "✓ %.1fKB" % (len(body) / 1e3)
                    if st == 200 and body.startswith(b"#") and b"\nv " in body
                    else "✗ %s" % st))
        st, body = get("/api/export?id=%s&fmt=dae" % mid)
        out.append(("GET export dae", "✓ %.1fKB" % (len(body) / 1e3)
                    if st == 200 and b"COLLADA" in body else "✗ %s" % st))
        st, body = get("/api/export?id=%s&fmt=3dm" % mid)
        if st == 501:
            out.append(("GET export 3dm", "· 无 rhino3dm 降级（501 符合预期）"))
        else:
            out.append(("GET export 3dm", "✓ %.1fKB" % (len(body) / 1e3)
                        if st == 200 and len(body) > 500 else "✗ %s" % st))
        st, _ = get("/api/export?id=NOPE999&fmt=obj")
        out.append(("GET export 坏id→404", "✓" if st == 404 else "✗ %s" % st))
    # 清理冒烟产物（不污染用户历史）
    if ok:
        try:
            mid = d["id"]
            mdir = os.path.join(os.path.dirname(generator.__file__), "..")
            vdir = os.path.join(os.environ.get("VIEWER_DIR") or _viewer_dir(), "models")
            import shutil
            shutil.rmtree(os.path.join(vdir, mid), ignore_errors=True)
            exp3dm = os.path.join(BASE, "exports", "%s.3dm" % mid)
            if os.path.isfile(exp3dm):
                os.remove(exp3dm)
            idx_p = os.path.join(vdir, "index.json")
            with open(idx_p, "r", encoding="utf-8") as f:
                idx = json.load(f)
            idx["experiments"] = [e for e in idx["experiments"] if e.get("id") != mid]
            with open(idx_p + ".tmp", "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False)
            os.replace(idx_p + ".tmp", idx_p)
            out.append(("清理冒烟产物", "✓"))
        except Exception as e:
            out.append(("清理冒烟产物", "✗ %s" % e))
    return out, True


def _viewer_dir():
    import app
    return app.VIEWER_DIR


def main():
    no_api = "--no-api" in sys.argv
    rows = run_battery()
    n_pass = sum(1 for r in rows if r[1] == "✓")
    api_rows, api_ran = run_api() if not no_api else ([], False)

    print("=" * 72)
    print("自检 %d 例：解析全过 %d/%d" % (len(rows), n_pass, len(rows)))
    for r in rows:
        print("  %-14s 解析:%s  生成:%s  树:%s  %s %s" % r)
    for name, res in api_rows:
        print("  [api] %-22s %s" % (name, res))

    os.makedirs(os.path.dirname(DEVLOG), exist_ok=True)
    if not os.path.exists(DEVLOG):
        with open(DEVLOG, "w", encoding="utf-8") as f:
            f.write("# 平台迭代日志 DEVLOG\n\n")
    with open(DEVLOG, "a", encoding="utf-8") as f:
        f.write("\n## %s · selftest 自动报告\n\n" % time.strftime("%Y-%m-%d %H:%M"))
        f.write("| 用例 | 解析 | 生成 | 树 | 总耗时 | 备注 |\n|---|---|---|---|---|---|\n")
        for r in rows:
            f.write("| %s | %s | %s | %s | %s | %s |\n" % r)
        if api_ran:
            f.write("\nAPI 冒烟：" + "；".join("%s %s" % (n, r) for n, r in api_rows) + "\n")
        f.write("\n解析通过率 **%d/%d**\n" % (n_pass, len(rows)))
    print("已追加到", DEVLOG)


if __name__ == "__main__":
    main()
