# -*- coding: utf-8 -*-
"""造一段"知道答案"的测试音乐：4 条 stem，用来验证识别准不准。

调：C 大调，速度 96 BPM，4/4，8 小节，和弦 C - Am - F - G（各两小节）。
输出：%TEMP%/score-synth/{mix,vocals,guitar,bass,drums}.wav + answer.json（标准答案）
"""

import json
import math
import os

import numpy as np
import soundfile as sf

SR = 44100
BPM = 96.0
BEAT = 60.0 / BPM
BAR = 4 * BEAT
BARS = 8
TOTAL = BAR * BARS
N = int(TOTAL * SR)
t = np.arange(N) / SR

OUT = os.path.join(os.environ.get("TEMP", "."), "score-synth")
os.makedirs(OUT, exist_ok=True)


def midi_hz(m):
    return 440.0 * 2 ** ((m - 69) / 12.0)


def env(n, attack=0.005, decay=0.4, sustain=0.0):
    a = int(attack * SR)
    e = np.ones(n)
    a = min(a, n)
    if a > 0:
        e[:a] = np.linspace(0, 1, a)
    rest = n - a
    if rest > 0:
        d = int(decay * SR)
        d = min(d, rest)
        e[a:a + d] = np.linspace(1, sustain if sustain else 0.0, d)
        if rest > d:
            e[a + d:] = sustain if sustain else 0.0
    return e


def place(buf, start, sig, gain=1.0):
    i = int(start * SR)
    j = min(len(buf), i + len(sig))
    if i >= len(buf) or j <= i:
        return
    buf[i:j] += sig[: j - i] * gain


def tone(midi, dur, kind="guitar", gain=0.5):
    n = int(dur * SR)
    tt = np.arange(n) / SR
    f = midi_hz(midi)
    if kind == "guitar":
        sig = (np.sin(2 * np.pi * f * tt) * 0.6
               + np.sin(2 * np.pi * f * 2 * tt) * 0.25
               + np.sin(2 * np.pi * f * 3 * tt) * 0.12
               + np.sin(2 * np.pi * f * 4 * tt) * 0.06)
        return sig * env(n, 0.004, 0.9, 0.0) * gain
    if kind == "bass":
        sig = np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(2 * np.pi * f * 2 * tt)
        return sig * env(n, 0.008, 0.5, 0.25) * gain
    # voice 更"正弦"一点
    sig = (np.sin(2 * np.pi * f * tt) * 0.8
           + np.sin(2 * np.pi * f * 2 * tt) * 0.2
           + np.sin(2 * np.pi * f * 3 * tt) * 0.08)
    vib = 1 + 0.004 * np.sin(2 * np.pi * 5.0 * tt)
    return sig * vib * env(n, 0.03, 0.2, 0.7) * gain


def drum(kind, gain=0.9):
    if kind == "kick":
        dur, f0, f1 = 0.22, 120.0, 45.0
        n = int(dur * SR)
        tt = np.arange(n) / SR
        f = f0 * (f1 / f0) ** (tt / dur)
        return np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.002, 0.2, 0.0) * gain * 1.6
    if kind == "snare":
        dur = 0.18
        n = int(dur * SR)
        noise = np.random.RandomState(7).randn(n)
        sig = noise * 0.7 + np.sin(2 * np.pi * 190 * np.arange(n) / SR) * 0.3
        return sig * env(n, 0.001, 0.16, 0.0) * gain * 0.8
    dur = 0.06
    n = int(dur * SR)
    noise = np.random.RandomState(11).randn(n)
    high = np.diff(np.concatenate([[0], noise]))  # 粗糙的高通
    return high * env(n, 0.0005, 0.05, 0.0) * gain * 0.5


# ---------------------------------------------------------------- 谱面（标准答案）
chords = ["C", "Am", "F", "G"]
guitar_notes = {  # 每小节和弦的分解和弦音
    "C": [48, 52, 55, 60], "Am": [45, 48, 52, 57], "F": [41, 45, 48, 53], "G": [43, 47, 50, 55],
}
bass_notes = {"C": 36, "Am": 33, "F": 29, "G": 31}
melody = [  # (小节, 拍, midi, 时值拍)
    (0, 0, 72, 1), (0, 1, 74, 1), (0, 2, 76, 1), (0, 3, 72, 1),
    (1, 0, 76, 1.5), (1, 2, 74, 1), (1, 3, 72, 1),
    (2, 0, 69, 1), (2, 1, 72, 1), (2, 2, 74, 2),
    (3, 0, 71, 1.5), (3, 2, 74, 1), (3, 3, 76, 1),
    (4, 0, 77, 1), (4, 1, 76, 1), (4, 2, 74, 1), (4, 3, 72, 1),
    (5, 0, 71, 1), (5, 1, 72, 1), (5, 2, 74, 2),
    (6, 0, 69, 1), (6, 1, 72, 1), (6, 2, 74, 1), (6, 3, 76, 1),
    (7, 0, 74, 2), (7, 2, 72, 2),
]

guitar = np.zeros(N)
bass = np.zeros(N)
vocals = np.zeros(N)
drums = np.zeros(N)

for bar in range(BARS):
    chord = chords[bar % 4]
    bar_t = bar * BAR
    # 吉他：八分音符扫分解和弦
    for beat in range(8):
        note = guitar_notes[chord][beat % 4] + 12
        place(guitar, bar_t + beat * BEAT / 2, tone(note, BEAT * 0.45, "guitar", 0.22))
        place(guitar, bar_t + beat * BEAT / 2, tone(note - 12, BEAT * 0.45, "guitar", 0.12))
    # 贝斯：每拍一个根音，第 3 拍抬八度
    root = bass_notes[chord]
    for beat in range(4):
        pitch = root + (12 if beat == 2 else 0)
        place(bass, bar_t + beat * BEAT, tone(pitch, BEAT * 0.9, "bass", 0.5))
    # 鼓：底鼓 1、3，军鼓 2、4，踩镲八分
    for beat in range(4):
        if beat in (0, 2):
            place(drums, bar_t + beat * BEAT, drum("kick"))
        else:
            place(drums, bar_t + beat * BEAT, drum("snare"))
        place(drums, bar_t + beat * BEAT + BEAT / 2, drum("hat", 0.5))

for bar, beat, pitch, dur_beats in melody:
    place(vocals, bar * BAR + beat * BEAT, tone(pitch, dur_beats * BEAT * 0.92, "voice", 0.55))

mix = vocals + guitar + bass + drums
peak = max(float(np.max(np.abs(x))) for x in (mix, vocals, guitar, bass, drums)) or 1.0
scale = 0.85 / peak
for label, buf in (("mix", mix), ("vocals", vocals), ("guitar", guitar), ("bass", bass), ("drums", drums)):
    sf.write(os.path.join(OUT, f"{label}.wav"), (buf * scale).astype(np.float32), SR)

answer = {
    "tempoBPM": BPM, "meter": 4, "key": "C major", "bars": BARS,
    "chords": [chords[b % 4] for b in range(BARS)],
    "melody": [{"bar": b, "beat": bt, "midi": m, "durationBeats": d} for b, bt, m, d in melody],
    "bass": [{"bar": b, "midi": bass_notes[chords[b % 4]]} for b in range(BARS)],
    "seconds": TOTAL,
}
with open(os.path.join(OUT, "answer.json"), "w", encoding="utf-8") as fh:
    json.dump(answer, fh, ensure_ascii=False, indent=2)

print("written", OUT, "seconds", round(TOTAL, 2))
