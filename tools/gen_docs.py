#!/usr/bin/env python3
"""扫描 plugins/*/package.json 与 docs/scores/*/meta.json，生成 docs/index.html。

用法:
    python tools/gen_docs.py

输出:
    docs/index.html   —— 自包含单文件，带内联样式，无外部依赖
"""
from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
SCORES = ROOT / "docs" / "scores"
OUT = ROOT / "docs" / "index.html"

# 手工维护的一句话介绍：包名 -> (中文名, 说明, 标签)
BLURB = {
    "dsh-plugin-score-smith": (
        "制谱师",
        "听一段音频，分离出各件乐器，识别音高与节奏，自动排出可编辑的曲谱：五线谱、简谱、六线谱、鼓谱都能出。",
        ["音频分轨", "音高识别", "MusicXML", "verovio 排版"],
    ),
    "dsh-plugin-stems": (
        "音轨分离",
        "把一首歌拆成人声、鼓、贝斯、吉他、钢琴和其它六轨，也可以按需混回去。",
        ["demucs", "六轨分离", "混音"],
    ),
    "dsh-plugin-hello": (
        "最小插件模板",
        "一个可运行的 DSH 插件样板：注册工具、返回结构化输出，照着改就能写自己的。",
        ["模板", "插件开发"],
    ),
}

EXTRA = [
    (
        "QQ 桥接",
        "qq-bridge",
        "让 DSH 住进 QQ：私聊、群聊、语音、图片、视频抽帧、掉线补回、日程提醒。",
        ["NapCat", "OneBot", "主动开口"],
    ),
    (
        "语音克隆",
        "voice",
        "本地 F5-TTS 微调自己的音色，批量合成台词，接回聊天软件当语音消息发。",
        ["F5-TTS", "whisper", "听写"],
    ),
    (
        "本地出图",
        "gen",
        "SDXL 本地文生图，含显存不足的分块放大与离线加载处理。",
        ["diffusers", "SDXL", "ESRGAN"],
    ),
]


def read_plugins() -> list[dict]:
    items = []
    if not PLUGINS.is_dir():
        return items
    for pkg_path in sorted(PLUGINS.glob("*/package.json")):
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = pkg.get("name") or pkg_path.parent.name
        cn, desc, tags = BLURB.get(
            name, (pkg_path.parent.name, pkg.get("description") or "（暂无说明）", [])
        )
        items.append(
            {
                "name": name,
                "folder": pkg_path.parent.name,
                "cn": cn,
                "desc": desc,
                "tags": tags,
                "version": pkg.get("version", "—"),
            }
        )
    return items


def read_scores() -> list[dict]:
    """每份已发布的曲谱：docs/scores/<slug>/meta.json。"""
    out = []
    if not SCORES.is_dir():
        return out
    for meta_path in sorted(SCORES.glob("*/meta.json")):
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(
            {
                "slug": meta_path.parent.name,
                "title": m.get("title") or meta_path.parent.name,
                "createdAt": m.get("createdAt") or "",
                "tracks": m.get("tracks") or [],
            }
        )
    return out


def card(title: str, sub: str, desc: str, tags: list[str], right: str, link: str) -> str:
    tag_html = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)
    return f"""      <article class="card">
        <div class="row">
          <h3>{html.escape(title)}</h3>
          <span class="ver">{html.escape(right)}</span>
        </div>
        <p class="sub">{html.escape(sub)}</p>
        <p class="desc">{html.escape(desc)}</p>
        <div class="tags">{tag_html}</div>
        <a class="link" href="{html.escape(link)}">查看源码 →</a>
      </article>"""


def score_card(s: dict) -> str:
    tracks = "、".join(s["tracks"]) or "—"
    return f"""      <article class="card score">
        <h3>{html.escape(s["title"])}</h3>
        <p class="sub">{html.escape(s["slug"])}</p>
        <p class="desc">音符轨：{html.escape(tracks)}</p>
        <a class="link" href="scores/{html.escape(s["slug"])}/">打开曲谱 →</a>
      </article>"""


def main() -> None:
    plugins = read_plugins()
    scores = read_scores()

    cards = [
        card(p["cn"], p["name"], p["desc"], p["tags"], f"v{p['version']}", f"../plugins/{p['folder']}/")
        for p in plugins
    ]
    cards += [
        card(cn, folder, desc, tags, "脚本集", f"../{folder}/") for cn, folder, desc, tags in EXTRA
    ]

    score_section = ""
    if scores:
        score_section = f"""    <h2 class="gap">曲谱（制谱师生成）</h2>
    <div class="grid">
{chr(10).join(score_card(s) for s in scores)}
    </div>
"""

    today = date.today().isoformat()
    score_meta = f" · 曲谱 {len(scores)} 份" if scores else ""

    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dsh-plugins · DSH 插件合集</title>
<style>
  :root {{
    --ink:#0f172a; --grey:#5b6473; --line:#e3e7ee; --blue:#2563eb;
    --bg:#f7f8fb; --card:#ffffff; --teal:#0f9e8e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font-family: "PingFang SC","Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif;
    line-height:1.6;
  }}
  header {{
    background: linear-gradient(135deg,#0f172a 0%,#1e3a5f 60%,#2563eb 100%);
    color:#fff; padding:56px 24px 44px;
  }}
  .wrap {{ max-width: 980px; margin:0 auto; }}
  header h1 {{ margin:0 0 8px; font-size:32px; letter-spacing:.5px; }}
  header p {{ margin:0; color:#c7d2e5; font-size:15px; }}
  .meta {{ margin-top:18px; font-size:13px; color:#9fb0cc; }}
  main {{ padding:36px 24px 64px; }}
  h2 {{ font-size:20px; margin:0 0 18px; }}
  h2.gap {{ margin-top:40px; }}
  .grid {{ display:grid; gap:18px; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); }}
  .card {{
    background:var(--card); border:1px solid var(--line); border-radius:14px;
    padding:20px 20px 16px; display:flex; flex-direction:column;
    box-shadow:0 1px 2px rgba(15,23,42,.04);
    transition:transform .15s ease, box-shadow .15s ease;
  }}
  .card:hover {{ transform:translateY(-2px); box-shadow:0 8px 24px rgba(15,23,42,.08); }}
  .row {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; }}
  .card h3 {{ margin:0; font-size:17px; }}
  .ver {{ font-size:12px; color:var(--teal); background:#e7f6f4; border-radius:999px; padding:2px 10px; white-space:nowrap; }}
  .sub {{ margin:4px 0 8px; font-size:12.5px; color:var(--grey); font-family:ui-monospace,Consolas,monospace; }}
  .desc {{ margin:0 0 12px; font-size:14px; color:#33404f; flex:1; }}
  .tags {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; }}
  .tag {{ font-size:12px; color:var(--blue); background:#eef3ff; border-radius:6px; padding:2px 8px; }}
  .link {{ font-size:13.5px; color:var(--blue); text-decoration:none; font-weight:500; }}
  .link:hover {{ text-decoration:underline; }}
  .card.score h3 {{ margin-bottom:4px; }}
  footer {{ border-top:1px solid var(--line); padding:22px 24px 40px; color:var(--grey); font-size:13px; }}
  code {{ background:#eef1f6; padding:1px 6px; border-radius:5px; font-size:12.5px; }}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>dsh-plugins</h1>
    <p>给 DeepSeek Harness 加能力的一组自建插件与工具链。全部本地运行，不依赖云端服务。</p>
    <div class="meta">更新于 {today} · 插件 {len(plugins)} 个 · 脚本集 {len(EXTRA)} 套{score_meta}</div>
  </div>
</header>
<main>
  <div class="wrap">
    <h2>插件与工具链</h2>
    <div class="grid">
{chr(10).join(cards)}
    </div>
{score_section}  </div>
</main>
<footer>
  <div class="wrap">
    版本以各插件 <code>package.json</code> 为准，变更记录见
    <a href="../CHANGELOG.md">CHANGELOG</a>。License: Apache-2.0。
  </div>
</footer>
</body>
</html>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    print(f"已生成 {OUT}（插件 {len(plugins)} 个，曲谱 {len(scores)} 份）")


if __name__ == "__main__":
    main()
