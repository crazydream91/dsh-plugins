# -*- coding: utf-8 -*-
"""生成一套「知道标准答案」的评测素材（test/eval_score.py 用）。

每个 case 目录里有：
  mix.wav + <stem>.wav   —— 直接喂给 score_worker 的 --stems-dir
  answer.json            —— 标准答案：每个 stem 的音符（起始秒/时值/音高/装饰音/连音标记）

case 重点覆盖用户这次提出的问题：
  voice_legato  人声：颤音、legato 滑音（圆滑线）、倚音（转音）、A 段原样反复
  piano_poly    真钢琴采样（gm.dls）的复音织体
  guitar_arp    吉他分解和弦（复音）
  bass_slide    贝斯滑音与八度跳
  drums         打击乐（沿用合成鼓）

用法（score-venv）：
    python test/gen_eval_cases.py [--out DIR] [--only voice_legato,...]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "py"))

import numpy as np
import soundfile as sf

SR = 44100


# ---------------------------------------------------------------- 基础工具
def hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12.0)


def t_axis(n: int) -> np.ndarray:
    return np.arange(n, dtype=np.float64) / SR


def env(n: int, attack: float, decay: float, sustain: float, release: float) -> np.ndarray:
    e = np.zeros(n, dtype=np.float32)
    a = min(n, max(1, int(attack * SR)))
    e[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32)
    r = min(n, max(1, int(release * SR)))
    body_end = max(a, n - r)
    if body_end > a:
        # 线性衰减到 sustain
        e[a:body_end] = np.linspace(1.0, sustain, body_end - a, dtype=np.float32)
    if r > 0 and n - r >= 0:
        e[n - r:] = np.linspace(e[n - r], 0.0, r, dtype=np.float32)
    return e


def place(buf: np.ndarray, start: float, sig: np.ndarray, gain: float = 1.0) -> None:
    i = int(round(start * SR))
    if i >= buf.size or sig.size == 0:
        return
    j = min(buf.size, i + sig.size)
    buf[i:j] += sig[: j - i] * gain


# ---------------------------------------------------------------- 人声
def voice_note(pitch: int, dur: float, prev_pitch: int | None = None,
               glide: float = 0.0, vibrato: float = 0.45,
               gain: float = 0.5, vowel: str = "a") -> np.ndarray:
    n = int(dur * SR)
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    t = t_axis(n)
    # 音高轨迹：起音从上一个音滑过来（legato/portamento）
    target = np.full(n, float(pitch))
    if prev_pitch is not None and glide > 0:
        g = min(n, int(glide * SR))
        # 对数频率上做线性插值
        frac = np.linspace(0.0, 1.0, g)
        start = np.full(g, float(prev_pitch))
        target[:g] = start + (pitch - start) * (frac ** 0.8)
    vib_depth = vibrato * (0.6 + 0.4 * np.sin(2 * np.pi * 0.7 * t))
    f0 = hz(target + vib_depth * np.sin(2 * np.pi * 5.4 * t))
    # 谐波 + 共振峰（/a/ 或 /e/ 的粗略包络）
    formants = {"a": [(700, 130), (1220, 160), (2600, 220)],
                "e": [(500, 120), (1900, 150), (2500, 220)]}[vowel]
    phase = np.cumsum(2 * np.pi * f0 / SR)
    hs = np.arange(1, 14, dtype=np.float64)[:, None]
    fh = hs * f0[None, :]
    w = np.zeros_like(fh)
    for fc, bw in formants:
        w += np.exp(-((fh - fc) ** 2) / (2 * bw ** 2))
    amps = (0.25 * w + 0.02) / (hs ** 0.65)
    sig = np.sum(amps * np.sin(hs * phase[None, :]), axis=0).astype(np.float32)
    # 气声
    sig += (0.006 * np.random.RandomState(3).randn(n)).astype(np.float32)
    sig *= env(n, 0.025, 0.2, 0.85, 0.06)
    return sig * gain


def render_voice(timeline: list[dict], total: float, out: np.ndarray) -> None:
    """timeline: [{start, dur, pitch, prev, glide, gain}]；grace 音小一点。"""
    for item in timeline:
        sig = voice_note(item["pitch"], item["dur"], item.get("prev"),
                         item.get("glide", 0.0), item.get("vibrato", 0.45),
                         item.get("gain", 0.5) * (0.35 if item.get("grace") else 1.0),
                         item.get("vowel", "a"))
        place(out, item["start"], sig)


# ---------------------------------------------------------------- 吉他 / 贝斯
def pluck(pitch: int, dur: float, gain: float = 0.3, noise: float = 0.02) -> np.ndarray:
    n = int(max(dur, 0.25) * SR)
    t = t_axis(n)
    f = hz(pitch)
    sig = (np.sin(2 * np.pi * f * t) * 0.55
           + np.sin(2 * np.pi * 2 * f * t) * 0.22
           + np.sin(2 * np.pi * 3 * f * t) * 0.10
           + np.sin(2 * np.pi * 4.02 * f * t) * 0.05)
    sig *= np.exp(-t * 2.2)
    sig += noise * np.random.RandomState(pitch % 97).randn(n) * np.exp(-t * 18)
    return (sig * env(n, 0.003, 0.5, 0.15, 0.08) * gain).astype(np.float32)


def bass_note(pitch: int, dur: float, prev: int | None = None, glide: float = 0.0,
              gain: float = 0.5) -> np.ndarray:
    n = int(dur * SR)
    t = t_axis(n)
    target = np.full(n, float(pitch))
    if prev is not None and glide > 0:
        g = min(n, int(glide * SR))
        target[:g] = np.linspace(float(prev), float(pitch), g)
    f = hz(target)
    phase = np.cumsum(2 * np.pi * f / SR)
    sig = (np.sin(phase) + 0.35 * np.sin(2 * phase) + 0.12 * np.sin(3 * phase))
    return (sig * env(n, 0.01, 0.2, 0.75, 0.05) * gain).astype(np.float32)


# ---------------------------------------------------------------- 鼓
def drum(kind: str, gain: float = 0.9) -> np.ndarray:
    if kind == "kick":
        dur, f0, f1 = 0.22, 120.0, 45.0
        n = int(dur * SR)
        t = t_axis(n)
        f = f0 * (f1 / f0) ** (t / dur)
        return (np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.002, 0.2, 0.0, 0.02)
                * gain * 1.6).astype(np.float32)
    if kind == "snare":
        n = int(0.18 * SR)
        t = t_axis(n)
        noise = np.random.RandomState(7).randn(n)
        sig = noise * 0.7 + np.sin(2 * np.pi * 190 * t) * 0.3
        return (sig * env(n, 0.001, 0.16, 0.0, 0.02) * gain * 0.8).astype(np.float32)
    n = int(0.06 * SR)
    noise = np.random.RandomState(11).randn(n)
    high = np.diff(np.concatenate([[0.0], noise]))
    return (high * env(n, 0.0005, 0.05, 0.0, 0.008) * gain * 0.5).astype(np.float32)


# ---------------------------------------------------------------- 案例：人声
def case_voice_legato() -> dict:
    tempo, meter, bars = 100.0, 4, 12
    beat = 60.0 / tempo
    total = bars * meter * beat
    mix = np.zeros(int(total * SR) + SR, dtype=np.float32)

    def at(bar: int, b: float) -> float:
        return (bar * meter + b) * beat

    # A 段（0-3 小节）：legato 级进 + 一个倚音 + 一个三连音
    mel = [
        (0, 0.0, 1.0, 69, None),      # A4
        (0, 1.0, 1.0, 71, "tie"),     # B4（与上一个 legato）
        (0, 2.0, 1.0, 72, "tie"),
        (0, 3.0, 1.0, 74, "tie"),
        (1, 0.0, 1.5, 76, "tie"),     # E5
        (1, 1.5, 0.5, 74, "tie"),
        (1, 2.0, 2.0, 72, "tie"),
        (2, 0.0, 0.2, 70, "grace"),   # 倚音 70（B♭4，调外、短）
        (2, 0.2, 1.0, 72, "tie"),     # C5
        (2, 1.2, 1.0, 74, "tie"),
        (2, 2.2, 0.333, 76, "tie"),   # 三连音
        (2, 2.533, 0.333, 77, "tie"),
        (2, 2.867, 0.333, 76, "tie"),
        (3, 3.2, 0.8, 74, "tie"),
    ]
    tl = []
    prev = None
    for bar, beat_in, dur_beats, pitch, flag in mel:
        start = at(bar, beat_in)
        dur = dur_beats * beat
        glide = 0.12 if (flag == "tie" and prev is not None) else 0.0
        tl.append({"start": start, "dur": dur, "pitch": pitch, "prev": prev,
                   "glide": glide, "grace": flag == "grace",
                   "gain": 0.5})
        prev = pitch
    a_section = [dict(x) for x in tl]

    # A 段原样反复（4-7 小节）
    rep = [dict(x, start=x["start"] + 4 * meter * beat) for x in a_section]
    # B 段（8-11 小节）：换一条旋律
    b_mel = [
        (8, 0.0, 1.0, 67, "tie"), (8, 1.0, 1.0, 72, "tie"),
        (8, 2.0, 2.0, 76, "tie"), (9, 0.0, 1.0, 74, "tie"),
        (9, 1.0, 1.0, 72, "tie"), (9, 2.0, 2.0, 71, "tie"),
        (10, 0.0, 1.0, 69, "tie"), (10, 1.0, 1.0, 71, "tie"),
        (10, 2.0, 2.0, 72, "tie"), (11, 0.0, 4.0, 69, None),
    ]
    b_tl = []
    prev = None
    for bar, beat_in, dur_beats, pitch, flag in b_mel:
        start = at(bar, beat_in)
        b_tl.append({"start": start, "dur": dur_beats * beat, "pitch": pitch,
                     "prev": prev, "glide": 0.12 if flag == "tie" else 0.0,
                     "gain": 0.5})
        prev = pitch

    all_tl = a_section + rep + b_tl
    render_voice(all_tl, total, mix)

    answer_notes = []
    for item in all_tl:
        answer_notes.append({
            "start": round(item["start"], 4),
            "dur": round(item["dur"], 4),
            "pitch": item["pitch"],
            "grace": bool(item.get("grace")),
            "slur_in": bool(item.get("glide")),
        })
    return {
        "tempo": tempo, "meter": meter, "bars": bars, "key": "C major",
        "duration": round(total, 3),
        "stems": {"vocals": {"audio": mix, "notes": answer_notes,
                             "repeat": {"blockBars": 4, "sections": ["A", "A", "B"]}}},
    }


# ---------------------------------------------------------------- 案例：真钢琴
def case_piano_poly(tmpdir: str) -> dict:
    import piano as piano_mod

    tempo, meter, bars = 96.0, 4, 8
    beat = 60.0 / tempo
    total = bars * meter * beat
    prog = [("C", [48, 52, 55, 60]), ("Am", [45, 48, 52, 57]),
            ("F", [41, 45, 48, 53]), ("G", [43, 47, 50, 55])]
    notes: list[dict] = []
    for bar in range(bars):
        chord = prog[bar % 4][1]
        # 左手：八分音符分解
        for k in range(8):
            pitch = chord[k % 4] + 12
            notes.append({"offsetQL": bar * 4 + k * 0.5, "durationQL": 0.5,
                          "pitch": pitch, "velocity": 0.55})
        # 右手：每小节两个和弦 + 一个旋律长音
        for k, off in enumerate((0.0, 2.0)):
            for p in [p + 24 for p in chord[1:]]:
                notes.append({"offsetQL": bar * 4 + off, "durationQL": 1.5,
                              "pitch": p, "velocity": 0.6})
        notes.append({"offsetQL": bar * 4 + 1.0, "durationQL": 2.0,
                      "pitch": chord[0] + 36, "velocity": 0.7})
    # 尾部收尾
    notes.append({"offsetQL": bars * 4 - 1.0, "durationQL": 1.5,
                  "pitch": 48 + 24, "velocity": 0.6})

    audio = piano_mod.render_notes(notes, tempo=tempo)
    answer_notes = [{"start": round(float(n["offsetQL"]) * beat, 4),
                     "dur": round(float(n["durationQL"]) * beat, 4),
                     "pitch": int(n["pitch"]), "grace": False, "slur_in": False}
                    for n in notes]
    return {"tempo": tempo, "meter": meter, "bars": bars, "key": "C major",
            "duration": round(total, 3),
            "stems": {"piano": {"audio": audio, "notes": answer_notes}}}


# ---------------------------------------------------------------- 案例：吉他
def case_guitar_arp() -> dict:
    tempo, meter, bars = 108.0, 4, 8
    beat = 60.0 / tempo
    total = bars * meter * beat
    mix = np.zeros(int(total * SR) + SR, dtype=np.float32)
    prog = [("C", [48, 52, 55, 60]), ("Am", [45, 48, 52, 57]),
            ("F", [41, 45, 48, 53]), ("G", [43, 47, 50, 55])]
    answer = []
    for bar in range(bars):
        chord = prog[bar % 4][1]
        # 分解和弦 + 第 3 拍扫弦
        for k in range(8):
            pitch = chord[k % 4] + 12
            start = (bar * 4 + k * 0.5) * beat
            place(mix, start, pluck(pitch, beat * 0.48, 0.24))
            answer.append({"start": round(start, 4), "dur": round(beat * 0.48, 4),
                           "pitch": pitch, "grace": False, "slur_in": False})
        # 扫弦（低八度，模拟拨到低音弦）
        for j, pitch in enumerate([chord[0] + 12, chord[1] + 12, chord[2] + 12]):
            start = (bar * 4 + 2.0) * beat + j * 0.012
            place(mix, start, pluck(pitch, beat * 0.9, 0.20))
            answer.append({"start": round(start, 4), "dur": round(beat * 0.9, 4),
                           "pitch": pitch, "grace": False, "slur_in": False})
    return {"tempo": tempo, "meter": meter, "bars": bars, "key": "C major",
            "duration": round(total, 3),
            "stems": {"guitar": {"audio": mix, "notes": answer}}}


# ---------------------------------------------------------------- 案例：贝斯
def case_bass_slide() -> dict:
    tempo, meter, bars = 104.0, 4, 8
    beat = 60.0 / tempo
    total = bars * meter * beat
    mix = np.zeros(int(total * SR) + SR, dtype=np.float32)
    roots = {"C": 36, "Am": 33, "F": 29, "G": 31}
    prog = ["C", "Am", "F", "G"] * 2
    answer = []
    prev = None
    for bar, name in enumerate(prog):
        root = roots[name]
        pattern = [(0.0, 1.0, root), (1.0, 0.75, root + 7), (1.75, 0.25, root + 12),
                   (2.0, 1.0, root + 12), (3.0, 1.0, root)]
        for beat_in, dur_beats, pitch in pattern:
            start = (bar * 4 + beat_in) * beat
            glide = 0.10 if (prev is not None and abs(pitch - prev) <= 7) else 0.0
            place(mix, start, bass_note(pitch, dur_beats * beat, prev, glide, 0.5))
            answer.append({"start": round(start, 4), "dur": round(dur_beats * beat, 4),
                           "pitch": pitch, "grace": False, "slur_in": bool(glide)})
            prev = pitch
    return {"tempo": tempo, "meter": meter, "bars": bars, "key": "C major",
            "duration": round(total, 3),
            "stems": {"bass": {"audio": mix, "notes": answer}}}


# ---------------------------------------------------------------- 案例：鼓
def case_drums() -> dict:
    tempo, meter, bars = 112.0, 4, 8
    beat = 60.0 / tempo
    total = bars * meter * beat
    mix = np.zeros(int(total * SR) + SR, dtype=np.float32)
    answer = []
    for bar in range(bars):
        for k in range(4):
            kind = "kick" if k in (0, 2) else "snare"
            start = (bar * 4 + k) * beat
            place(mix, start, drum(kind))
            answer.append({"start": round(start, 4), "dur": 0.1, "pitch": None,
                           "drum": kind, "grace": False, "slur_in": False})
            if k != 3:
                start2 = start + beat / 2
                place(mix, start2, drum("hat", 0.5))
                answer.append({"start": round(start2, 4), "dur": 0.06, "pitch": None,
                               "drum": "hat", "grace": False, "slur_in": False})
    return {"tempo": tempo, "meter": meter, "bars": bars, "key": "C major",
            "duration": round(total, 3),
            "stems": {"drums": {"audio": mix, "notes": answer}}}


CASES = {
    "voice_legato": case_voice_legato,
    "piano_poly": None,      # 需要 out dir（读 gm.dls）
    "guitar_arp": case_guitar_arp,
    "bass_slide": case_bass_slide,
    "drums": case_drums,
}


def write_case(out: str, name: str, spec: dict) -> None:
    d = os.path.join(out, name)
    os.makedirs(d, exist_ok=True)
    stems = spec["stems"]
    mix = np.zeros_like(next(iter(stems.values()))["audio"], dtype=np.float32)
    answer = {"tempo": spec["tempo"], "meter": spec["meter"], "bars": spec["bars"],
              "key": spec["key"], "duration": spec["duration"], "stems": {}}
    for stem, info in stems.items():
        audio = np.clip(info["audio"], -1.0, 1.0).astype(np.float32)
        sf.write(os.path.join(d, f"{stem}.wav"), audio, SR)
        mix[: audio.size] += audio[: mix.size]
        answer["stems"][stem] = {"notes": info["notes"]}
        if info.get("repeat"):
            answer["stems"][stem]["repeat"] = info["repeat"]
    peak = float(np.max(np.abs(mix))) or 1.0
    sf.write(os.path.join(d, "mix.wav"), (mix / peak * 0.9).astype(np.float32), SR)
    with open(os.path.join(d, "answer.json"), "w", encoding="utf-8") as fh:
        json.dump(answer, fh, ensure_ascii=False, indent=1)
    print(f"  {name}: {', '.join(answer['stems'])}  ({spec['duration']:.1f}s)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.environ.get("TEMP", "."), "score-eval-cases"))
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    wanted = [s.strip() for s in args.only.split(",") if s.strip()] or list(CASES)
    print("cases ->", args.out)
    for name in wanted:
        if name == "piano_poly":
            write_case(args.out, name, case_piano_poly(args.out))
        else:
            write_case(args.out, name, CASES[name]())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
