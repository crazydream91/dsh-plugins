"""评估简谱音高准确度：把识别出的音符和 pyin 给出的主旋律 f0 对照。

用法:
    python test/eval_vocals.py <项目目录> [stem 名，默认 vocals]

输出:
    - 音符总数、与 pyin 主音高一致的比例（±0.6 半音）
    - 无声帧占比过高的"可疑音符"数量（可能是非人声/噪声）
    - 若干条最离谱的错音（时间、识别值、pyin 值）
"""
import json
import os
import sys

import numpy as np
import librosa


def main() -> int:
    project = sys.argv[1]
    stem = sys.argv[2] if len(sys.argv) > 2 else "vocals"
    wav = os.path.join(project, "stems", stem + ".wav")
    with open(os.path.join(project, "score.json"), encoding="utf-8") as fh:
        model = json.load(fh)
    track = next((t for t in model["tracks"] if t["instrument"] == stem or t["id"] == stem), model["tracks"][0])
    notes = track["notes"]
    meta = model["meta"]
    tempo = float(meta["tempo"])
    sec_per_ql = 60.0 / tempo

    y, sr = librosa.load(wav, sr=22050, mono=True)
    f0, voiced, prob = librosa.pyin(
        y, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C6"),
        sr=sr, frame_length=2048, hop_length=256,
    )
    times = librosa.times_like(f0, sr=sr, hop_length=256)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=256)[0]
    midi = np.where(np.isfinite(f0), librosa.hz_to_midi(np.nan_to_num(f0)), np.nan)

    exact = 0
    near = 0
    octave_err = 0
    unvoiced = 0
    head_ok = 0
    worst = []
    for n in notes:
        t0 = float(n["offsetQL"]) * sec_per_ql
        t1 = t0 + float(n["durationQL"]) * sec_per_ql
        sel = (times >= t0) & (times < max(t1, t0 + 0.03))
        vals = midi[sel]
        vals = vals[np.isfinite(vals)]
        head_sel = (times >= t0 - 0.02) & (times < t0 + 0.12)
        head_vals = midi[head_sel]
        head_vals = head_vals[np.isfinite(head_vals)]
        if len(vals) < 2:
            unvoiced += 1
            worst.append((round(t0, 2), n["pitch"], None, "unvoiced"))
            continue
        med = float(np.median(vals))
        d = med - n["pitch"]
        if abs(d) < 0.6:
            exact += 1
        elif abs(abs(d) - 12) < 0.8:
            octave_err += 1
            worst.append((round(t0, 2), n["pitch"], round(med, 1), "octave"))
        else:
            worst.append((round(t0, 2), n["pitch"], round(med, 1), "off"))
        if abs(d) < 1.2:
            near += 1
        if len(head_vals) >= 1 and abs(float(np.median(head_vals)) - n["pitch"]) < 0.6:
            head_ok += 1

    total = len(notes)
    print(f"stem={stem} notes={total}")
    print(f"match(±0.6半音)={exact} ({exact / total * 100:.1f}%)")
    print(f"near(±1.2)={near} ({near / total * 100:.1f}%)")
    print(f"octave_error={octave_err} ({octave_err / total * 100:.1f}%)")
    print(f"unvoiced={unvoiced} ({unvoiced / total * 100:.1f}%)")
    print(f"head_match(起音处±0.6)={head_ok} ({head_ok / total * 100:.1f}%)")
    worst.sort(key=lambda w: (w[3] == "off", w[1]))
    print("worst 25:")
    for t, p, med, kind in worst[:25]:
        print(f"  t={t:<7} note={p:<4} pyin={med} {kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
