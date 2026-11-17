#!/usr/bin/env python3
"""把一份制谱师工程发布到 GitHub Pages。

把曲谱工程的产物（预览图、MusicXML、MIDI、分谱分享页）复制到
docs/scores/<slug>/，并生成一个自包含的浏览页，提交后即可用网址分享。

用法:
    python tools/publish_score.py --project "D:\\dsh 大肥鱼\\scores\\shengxia7" --slug shengxia7
    python tools/publish_score.py --project ... --slug ... --no-index   # 只复制不重排首页
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCORES_OUT = ROOT / "docs" / "scores"

COPY_ONE = ["preview.png", "score.musicxml", "score.mid", "score.json"]
COPY_GLOB = ["parts/*/*.html", "parts/*/*.png", "parts/*/*.mp3"]


def load_meta(project: Path) -> dict:
    meta = {"title": project.name, "createdAt": "", "tempo": "", "meter": "", "key": ""}
    sj = project / "score.json"
    if sj.is_file():
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
            meta["title"] = data.get("title") or meta["title"]
            meta["createdAt"] = data.get("createdAt") or ""
            info = data.get("meta") or {}
            meta["tempo"] = info.get("tempo") or ""
            meta["meter"] = info.get("meter") or ""
            meta["key"] = info.get("key") or ""
            tracks = data.get("tracks") or []
            meta["tracks"] = [t.get("label") or t.get("id") or "?" for t in tracks]
        except Exception as exc:  # 容错：坏文件不应该阻断发布
            print(f"[warn] 解析 score.json 失败: {exc}", file=sys.stderr)
    mf = project / "manifest.json"
    if mf.is_file():
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            for k in ("title", "createdAt", "tempo", "meter", "key"):
                if not meta.get(k) and data.get(k):
                    meta[k] = data[k]
        except Exception:
            pass
    return meta


def copy_assets(project: Path, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in COPY_ONE:
        src = project / name
        if src.is_file():
            shutil.copy2(src, dest / name)
            copied.append(name)
    for pattern in COPY_GLOB:
        for src in project.glob(pattern):
            rel = src.relative_to(project)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied.append(str(rel).replace("\\", "/"))
    return copied


def png_data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def build_page(dest: Path, meta: dict, files: list[str]) -> None:
    preview = dest / "preview.png"
    img_tag = (
        f'<img class="sheet" src="{png_data_uri(preview)}" alt="{html.escape(meta["title"])} 曲谱预览">'
        if preview.is_file()
        else '<p class="warn">（这份工程没有 preview.png）</p>'
    )

    downloads = []
    for name in ("score.musicxml", "score.mid", "score.json"):
        if (dest / name).is_file():
            label = {"score.musicxml": "MusicXML", "score.mid": "MIDI", "score.json": "工程 JSON"}[name]
            downloads.append(f'<a class="dl" href="{name}">{label}</a>')

    parts = []
    for f in files:
        if f.startswith("parts/") and f.endswith(".html") and "share" in f:
            parts.append(f'<li><a href="{f}">自包含分谱页 · {html.escape(Path(f).parent.name)}</a></li>')

    bits = []
    if meta.get("tempo"):
        bits.append(f'{meta["tempo"]} BPM')
    for k in ("meter", "key"):
        if meta.get(k):
            bits.append(str(meta[k]))
    meta_bits = " · ".join(bits)
    tracks = "、".join(meta.get("tracks") or [])

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(meta["title"])} · 曲谱</title>
<style>
  :root {{ --ink:#0f172a; --grey:#5b6473; --line:#e3e7ee; --blue:#2563eb; --bg:#f7f8fb; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif; line-height:1.6; }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 64px; }}
  a.back {{ font-size:13px; color:var(--blue); text-decoration:none; }}
  h1 {{ font-size:26px; margin:12px 0 6px; }}
  .meta {{ color:var(--grey); font-size:14px; margin-bottom:18px; }}
  .meta b {{ color:#33404f; font-weight:600; }}
  .sheet {{ width:100%; height:auto; background:#fff; border:1px solid var(--line);
            border-radius:12px; box-shadow:0 2px 10px rgba(15,23,42,.06); }}
  .dl, .parts a {{ display:inline-block; margin:6px 10px 0 0; font-size:14px; color:var(--blue);
                   border:1px solid var(--line); background:#fff; border-radius:8px; padding:6px 12px;
                   text-decoration:none; }}
  .dl:hover, .parts a:hover {{ border-color:var(--blue); }}
  h2 {{ font-size:16px; margin:26px 0 8px; }}
  ul {{ margin:0; padding-left:18px; }}
  .warn {{ color:#b45309; }}
  footer {{ margin-top:32px; font-size:13px; color:var(--grey); }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="../../">← 返回插件首页</a>
  <h1>{html.escape(meta["title"])}</h1>
  <p class="meta">{html.escape(meta_bits)}{"　·　" if meta_bits else ""}音符轨：{html.escape(tracks or "—")}<br>
    用 <b>制谱师 dsh-plugin-score-smith</b> 生成{("　·　" + html.escape(meta["createdAt"])) if meta.get("createdAt") else ""}</p>
  {img_tag}
  <h2>下载</h2>
  <p>{"".join(downloads) or '<span class="warn">无</span>'}</p>
  {('<h2>分谱</h2><ul class="parts">' + "".join(parts) + "</ul>") if parts else ""}
  <footer>本页自包含（图片内嵌），离线也能看；源自 <a href="https://github.com/crazydream91/dsh-plugins">dsh-plugins</a>。</footer>
</div>
</body>
</html>
"""
    dest.joinpath("index.html").write_text(page, encoding="utf-8")
    dest.joinpath("meta.json").write_text(
        json.dumps({**meta, "files": files}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="制谱师工程目录（含 score.json）")
    ap.add_argument("--slug", required=True, help="发布到 docs/scores/<slug>/")
    ap.add_argument("--no-index", action="store_true", help="不重新生成首页")
    args = ap.parse_args()

    project = Path(args.project)
    if not project.is_dir():
        raise SystemExit(f"工程目录不存在: {project}")

    dest = SCORES_OUT / args.slug
    if dest.exists():
        shutil.rmtree(dest)

    meta = load_meta(project)
    files = copy_assets(project, dest)
    build_page(dest, meta, files)
    print(f"已发布 {meta['title']} → {dest}（{len(files)} 个资源）")

    if not args.no_index:
        subprocess.run([sys.executable, str(ROOT / "tools" / "gen_docs.py")], check=True)


if __name__ == "__main__":
    main()
