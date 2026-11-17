"""过滤 ASR 幻觉样本，生成 F5-TTS 训练用 CSV（audio_file|text，绝对路径）。"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
items = json.loads((ROOT / "ft/meta.json").read_text(encoding="utf-8"))

BAD = re.compile(r"中文字幕|字幕志愿者|点赞|订阅|转发|打赏|明镜|点点栏|谢谢观看|请不吝")
kept, seen = [], set()
for it in items:
    t = it["text"].strip()
    if len(t) < 8 or BAD.search(t):
        continue
    if len(set(t)) < len(t) * 0.4:      # 重复字太多 = 幻觉
        continue
    if t in seen:
        continue
    seen.add(t)
    kept.append({"file": (ROOT / "ft" / it["file"]).as_posix(), "text": t})

(ROOT / "ft/meta-clean.json").write_text(
    json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")

lines = ["audio_file|text"] + [f"{k['file']}|{k['text']}" for k in kept]
(ROOT / "ft/meta.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"kept {len(kept)} / {len(items)}")
