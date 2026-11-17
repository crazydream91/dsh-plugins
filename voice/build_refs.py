"""从整段人声轨里挑"干净、有起伏"的短片段，作为新的参考音候选。

滑动窗口而不是按静音切：按静音切出来的段大多是 1-3 秒的碎句。
输出 voice/ref2/clip-XX.wav + manifest.json（时长、泄漏比、起伏度）
"""
import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).parent
FF = ROOT / "bin/ffmpeg.exe"
VOC = ROOT / "sep-long/htdemucs/long-44k/vocals.wav"
BG = ROOT / "sep-long/htdemucs/long-44k/no_vocals.wav"
OUT = ROOT / "ref2"
SR = 24000

WIN = 8.0          # 候选时长（秒）
HOP = 1.0
MIN_LEAK_DB = -12.0   # 背景比人声至少低 12 dB
MIN_FLAT = 0.22       # 帧响度变异系数，太低=念经
MAX_PICK = 10
MIN_GAP = 6.0         # 相邻候选至少隔 6 秒，避免全挑同一段


def rms(x):
    return float(np.sqrt(np.mean(x ** 2) + 1e-12))


def score(voc, bg):
    leak = 20 * np.log10((rms(bg) + 1e-9) / (rms(voc) + 1e-9))
    levels = np.array([rms(f) for f in np.array_split(voc, 10)])
    flat = float(np.std(levels) / (np.mean(levels) + 1e-9))
    return leak, flat


def main():
    OUT.mkdir(exist_ok=True)
    voc, sr = sf.read(str(VOC), always_2d=True)
    bg, _ = sf.read(str(BG), always_2d=True)
    v = voc.mean(axis=1)
    b = bg.mean(axis=1)
    total = len(v) / sr

    win = int(WIN * sr)
    hop = int(HOP * sr)
    cands = []
    for start in range(0, len(v) - win, hop):
        sv = v[start:start + win]
        sb = b[start:start + win]
        if rms(sv) < 0.008:          # 基本没人声
            continue
        leak, flat = score(sv, sb)
        if leak > MIN_LEAK_DB or flat < MIN_FLAT:
            continue
        cands.append(dict(start=start / sr, leak=round(leak, 1), flat=round(flat, 3)))

    cands.sort(key=lambda c: (c["leak"], -c["flat"]))
    picked = []
    for c in cands:
        if all(abs(c["start"] - p["start"]) >= MIN_GAP for p in picked):
            picked.append(c)
        if len(picked) >= MAX_PICK:
            break
    picked.sort(key=lambda c: c["start"])

    manifest = []
    for idx, c in enumerate(picked, 1):
        tmp = OUT / f"_tmp-{idx}.wav"
        subprocess.run([str(FF), "-y", "-ss", f"{c['start']:.2f}", "-t", f"{WIN:.2f}",
                        "-i", str(VOC), "-ac", "1", "-ar", str(SR), str(tmp)],
                       capture_output=True)
        data, sr2 = sf.read(str(tmp))
        peak = float(np.abs(data).max()) or 1.0
        data = data * (0.89 / peak)
        final = OUT / f"clip-{idx:02d}.wav"
        sf.write(str(final), data, sr2)
        tmp.unlink(missing_ok=True)
        manifest.append({"file": final.name, "start": round(c["start"], 2),
                         "dur": round(len(data) / sr2, 2), "leak_db": c["leak"], "flat": c["flat"]})

    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"picked {len(manifest)} clips from {total:.0f}s source")
    for m in manifest:
        print(f"{m['file']}  @{m['start']}s  {m['dur']}s  leak {m['leak_db']}dB  flat {m['flat']}")


if __name__ == "__main__":
    main()
