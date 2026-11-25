# -*- coding: utf-8 -*-
"""景观生成平台 · 本地服务器（Python 标准库零依赖）

  python app.py  →  http://localhost:8400

路由：
  GET  /                     平台聊天界面（static/index.html）
  GET  /static/*             平台静态资源
  GET  /viewer/*             FluxPlot 查看器（复用 CraftBot可视化工具，禁缓存）
  POST /api/generate         {"prompt": "..."} → 解析→生成→注册→返回查看器链接
  GET  /api/models           已生成模型列表
  GET  /api/health           健康检查（含 3DM 导出可用性）
  GET  /api/export?id=&fmt=  导出模型：fmt = obj / dae（SketchUp 导入用）/ 3dm
"""
import datetime
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import analytics
import exporter
import generator
import nl_parser as parser   # 旧名 parser.py 与 Python≤3.9 内置模块撞名，勿改回

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_viewer():
    """Optional FluxPlot viewer (CraftBot fork). Not bundled: point us at a copy
    with the CRAFTBOT_VIEWER env var, or drop it in vendor/craftbot/viewer."""
    candidates = []
    env = os.environ.get("CRAFTBOT_VIEWER")
    if env:
        candidates.append(env)
    candidates.append(os.path.join(BASE_DIR, "vendor", "craftbot", "viewer"))
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return ""


VIEWER_DIR = _resolve_viewer()

# Models live with the platform, so it runs with or without the viewer.
MODELS_DIR = os.path.join(BASE_DIR, "models")
os.makedirs(MODELS_DIR, exist_ok=True)
INDEX_JSON = os.path.join(MODELS_DIR, "index.json")
if not os.path.exists(INDEX_JSON):
    with open(INDEX_JSON, "w", encoding="utf-8") as _f:
        json.dump({"experiments": []}, _f)

PORT = int(os.environ.get("PLATFORM_PORT", "8400"))

_lock = threading.Lock()
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".wasm": "application/wasm", ".glb": "model/gltf-binary"}


def _next_gen_id():
    ids = [d for d in os.listdir(MODELS_DIR)
           if re.fullmatch(r"GEN\d{3}", d or "", re.I)]
    n = max([int(d[3:]) for d in ids], default=0) + 1
    return "GEN%03d" % n


def _register(mid, fname, title, elements, nbytes):
    with open(INDEX_JSON, "r", encoding="utf-8") as f:
        idx = json.load(f)
    idx["experiments"] = [e for e in idx.get("experiments", [])
                          if e.get("id") != mid]
    idx["experiments"].append({
        "id": mid, "title": title,
        "runs": [{"agent": "Landscape Platform",
                  "versions": [{"v": "v01", "file": fname,
                                "elements": elements, "bytes": nbytes,
                                "built": datetime.date.today().isoformat()}]}]})
    tmp = INDEX_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False)
    os.replace(tmp, INDEX_JSON)          # 原子替换，防并发写坏


def _log_generation(record):
    """学习数据落盘：每条 prompt+spec+结果追加到 logs/generations.jsonl。
    对应论文 §6：课程档案作为后训练数据的雏形。"""
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "generations.jsonl"), "a",
              encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def do_generate(prompt, fine=False):
    with _lock:                          # id 分配 + 注册必须串行
        spec, engine = parser.parse(prompt)
        if fine:                         # 前端"精细"开关优先于关键词
            spec["resolution"] = 0.25
        try:                             # Ask 模式：解释永不应拖垮生成
            explain = parser.explain_spec(spec)
        except Exception:
            explain = []
        model, stats = generator.generate(spec)
        mid = _next_gen_id()
        mdir = os.path.join(MODELS_DIR, mid)
        os.makedirs(mdir, exist_ok=True)
        fname = "%s/%s_v01.json" % (mid, mid.lower())
        payload = json.dumps(model, ensure_ascii=False, separators=(",", ":"))
        fpath = os.path.join(mdir, "%s_v01.json" % mid.lower())
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(payload)
        title = "%s · %s" % (mid, prompt[:24])
        _register(mid, fname, title, stats["elements"],
                  len(payload.encode("utf-8")))
    _log_generation({"id": mid, "ts": datetime.datetime.now().isoformat(),
                     "engine": engine, "prompt": prompt, "spec": spec,
                     "explain": explain, "stats": stats, "file": fname})
    return {"ok": True, "id": mid, "engine": engine, "spec": spec, "stats": stats,
            "explain": explain, "file": fname,
            "viewer_url": ("/viewer/?model=models/%s&style=flux" % fname) if VIEWER_DIR else None}


def do_list_models():
    out = []
    if os.path.exists(INDEX_JSON):
        with open(INDEX_JSON, "r", encoding="utf-8") as f:
            idx = json.load(f)
        for e in idx.get("experiments", []):
            if re.fullmatch(r"GEN\d{3}", e.get("id", ""), re.I):
                v = e["runs"][0]["versions"][0]
                out.append({"id": e["id"], "title": e.get("title", e["id"]),
                            "file": v["file"], "elements": v["elements"],
                            "built": v.get("built", "")})
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "LandscapePlatform/0.1"

    def log_message(self, fmt, *args):
        pass

    # ---------------- helpers
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8",
              extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, root, rel):
        rel = urllib.parse.unquote(rel).lstrip("/")
        path = os.path.abspath(os.path.join(root, rel))
        if not path.startswith(os.path.abspath(root)):
            return self._send(403, b"forbidden")
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        if not os.path.isfile(path):
            return self._send(404, b"not found")
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    # ---------------- routes
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if path == "/":
            return self._file(os.path.join(BASE_DIR, "static"), "index.html")
        if path.startswith("/static/"):
            return self._file(os.path.join(BASE_DIR, "static"), path[8:])
        if path == "/viewer":
            return self._send(302, b"", extra={"Location": "/viewer/"})
        if path.startswith("/viewer/models/"):
            # models live with the platform, not inside the viewer bundle
            return self._file(MODELS_DIR, path[len("/viewer/models/"):])
        if path.startswith("/viewer/"):
            if not VIEWER_DIR:
                return self._send(404, "FluxPlot viewer not installed. Set CRAFTBOT_VIEWER "
                                       "or place it at vendor/craftbot/viewer.".encode("utf-8"))
            return self._file(VIEWER_DIR, path[8:])
        if path == "/api/health":
            return self._json({"ok": True, "viewer_dir": VIEWER_DIR or None,
                               "export3dm": exporter.has_3dm(),
                               "analytic_roads": generator.HAS_SHAPELY})
        if path == "/api/models":
            return self._json({"ok": True, "models": do_list_models()})
        if path == "/api/analytics":
            try:
                return self._json({"ok": True, "data": analytics.summarize()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/export":
            qs = urllib.parse.parse_qs(url.query)
            return self._export(qs.get("id", [""])[0].strip(),
                                qs.get("fmt", [""])[0].strip().lower())
        self._send(404, b"not found")

    def _export(self, mid, fmt):
        """导出已注册模型：obj/dae 内存生成；3dm 写 exports/ 后回传（需 rhino3dm）。"""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", mid or ""):
            return self._json({"ok": False, "error": "id 非法"}, 400)
        if fmt not in ("obj", "dae", "3dm"):
            return self._json({"ok": False, "error": "fmt 须为 obj/dae/3dm"}, 400)
        model = exporter.load_model(MODELS_DIR, mid)
        if model is None:
            return self._json({"ok": False, "error": "模型不存在"}, 404)
        fname = "%s.%s" % (mid.upper(), fmt)
        if fmt == "obj":
            data = exporter.to_obj(model)
        elif fmt == "dae":
            data = exporter.to_dae(model)
        else:
            out_dir = os.path.join(BASE_DIR, "exports")
            os.makedirs(out_dir, exist_ok=True)
            fpath = os.path.join(out_dir, fname)
            if not exporter.to_3dm(model, fpath):
                return self._json({"ok": False,
                                   "error": "当前 Python 无 rhino3dm，3DM 不可用"
                                            "（用 py 启动可恢复）"}, 501)
            with open(fpath, "rb") as f:
                data = f.read()
        self._send(200, data, "application/octet-stream",
                   {"Content-Disposition": "attachment; filename=%s" % fname})

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/api/generate":
            return self._send(404, b"not found")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n)
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except UnicodeDecodeError:  # 个别 Windows 客户端按 GBK 发请求体
                body = json.loads(raw.decode("gbk", "replace") or "{}")
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                return self._json({"ok": False, "error": "prompt 为空"}, 400)
            if len(prompt) > 500:
                return self._json({"ok": False, "error": "prompt 过长"}, 400)
            self._json(do_generate(prompt, fine=bool(body.get("fine"))))
        except Exception as e:  # 演示服务器：错误直接回给前端显示
            self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)


def main():
    os.makedirs(MODELS_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("景观生成平台已启动：  http://localhost:%d" % PORT)
    if VIEWER_DIR:
        print("查看器目录：        %s" % VIEWER_DIR)
    else:
        print("未找到 FluxPlot 查看器（设 CRAFTBOT_VIEWER 或放到 vendor/craftbot/viewer）；")
        print("生成与 OBJ/DAE/3DM 导出仍可用，/viewer/ 预览不可用。")
    print("Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
