# -*- coding: utf-8 -*-
"""乐理库：调式、音阶、和弦、乐器音域与音色、鼓组映射。

这里只放"死的知识"，不做信号处理，也不依赖 numpy 之外的库。
transcribe / quantize / build_score 都从这里取常量。
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------- 音名 / 音高

NOTE_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# MIDI 60 = C4（科学音高记号），music21 用的也是这个约定
A4_MIDI = 69
A4_FREQ = 440.0


def midi_to_name(pitch: int, flats: bool = False) -> str:
    names = NOTE_NAMES_FLAT if flats else NOTE_NAMES_SHARP
    return f"{names[int(pitch) % 12]}{int(pitch) // 12 - 1}"


def name_to_pitch_class(name: str) -> int:
    """C / C# / Db / Gb → 0..11。"""
    raw = str(name or "C").strip()
    letter = raw[0].upper()
    semis = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}.get(letter, 0)
    for ch in raw[1:]:
        if ch == "#":
            semis += 1
        elif ch in "bB":
            semis -= 1
        else:
            break
    return semis % 12


def name_to_midi(name: str) -> int:
    """支持 C4 / C#4 / Db4 / c#3。"""
    raw = name.strip()
    letter = raw[0].upper()
    idx = 1
    semis = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter]
    while idx < len(raw) and raw[idx] in "#b":
        semis += 1 if raw[idx] == "#" else -1
        idx += 1
    octave = int(raw[idx:])
    return (octave + 1) * 12 + semis


def midi_to_freq(pitch: float) -> float:
    return A4_FREQ * (2.0 ** ((float(pitch) - A4_MIDI) / 12.0))


def freq_to_midi(freq: float) -> float:
    if freq <= 0:
        return -1.0
    return A4_MIDI + 12.0 * math.log2(float(freq) / A4_FREQ)


# ---------------------------------------------------------------- 调性检测

# Krumhansl-Schmuckler 调性轮廓
KRUMHANSL_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KRUMHANSL_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

KEY_NAMES = {
    # 每个调性给出五线谱上该用的记号：True 表示升号调
    "C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "F#": 6, "C#": 7,
    "F": -1, "Bb": -2, "Eb": -3, "Ab": -4, "Db": -5, "Gb": -6, "Cb": -7,
}


def _corr(a, b) -> float:
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def detect_key(chroma) -> dict:
    """chroma: 长度 12 的色度向量（按 C=0 起）。

    返回 {"tonic": "C", "mode": "major"|"minor", "score": 0.0~1.0, "alts": [...]}
    """
    vals = [float(x) for x in chroma]
    total = sum(vals) or 1.0
    vals = [v / total for v in vals]
    best = None
    for tonic in range(12):
        for mode, profile in (("major", KRUMHANSL_MAJOR), ("minor", KRUMHANSL_MINOR)):
            rotated = [profile[(i - tonic) % 12] for i in range(12)]
            score = _corr(vals, rotated)
            if best is None or score > best["score"]:
                best = {
                    "tonic": NOTE_NAMES_FLAT[tonic],
                    "tonicPc": tonic,
                    "mode": mode,
                    "score": score,
                }
    # 用大调/小调两种拼写里更常见的那个
    if best["mode"] == "minor":
        best["relativeMajor"] = NOTE_NAMES_FLAT[(best["tonicPc"] + 3) % 12]
    else:
        best["relativeMinor"] = NOTE_NAMES_FLAT[(best["tonicPc"] + 9) % 12]
    return best


def chroma_from_notes(notes, drum_key: str = "drum") -> list[float]:
    """把音符列表（{"pitch", "durationQL"|"end-start"}）换成 12 维色度向量。

    按时长加权：长音比过门更有说服力。鼓（带 drum 字段）不参与。
    """
    hist = [0.0] * 12
    for note in notes or []:
        if drum_key and note.get(drum_key):
            continue
        pitch = note.get("pitch")
        if pitch is None or not (0 <= int(pitch) <= 127):
            continue
        weight = note.get("durationQL")
        if weight is None:
            weight = (float(note.get("end", 0.0)) - float(note.get("start", 0.0))) or 0.2
        weight = max(0.05, float(weight))
        hist[int(pitch) % 12] += weight
    return hist


def key_fit(hist, tonic: int, mode: str) -> float:
    """notes 直方图与某个调的贴合度。

    = 调内音占比 − 调外音惩罚 + 主/三/五音的覆盖 + 主音出现比重。
    """
    total = sum(hist) or 1.0
    scale = SCALES.get(mode) or SCALES["major"]
    inside = sum(hist[(tonic + step) % 12] for step in scale) / total
    outside = max(0.0, 1.0 - inside)
    third = 3 if mode == "minor" else 4
    anchor_pcs = [(tonic + step) % 12 for step in (0, third, 7)]
    coverage = sum(1.0 for pc in anchor_pcs if hist[pc] > 0.0) / 3.0
    tonic_weight = min(1.0, hist[tonic % 12] / total * 3.0)
    return float(inside * 1.0 - outside * 1.2 + coverage * 0.35 + tonic_weight * 0.25)


def detect_key_from_notes(notes_by_track, fallback: dict | None = None,
                          prefer_sharp: bool = False) -> dict:
    """用识别出来的音符反推调性（比 Chroma 更靠谱，因为音符已经落到半音上了）。

    notes_by_track: [[note, ...], ...] 多个轨的音符；鼓轨会被自动忽略。
    评分里带「调号越简单越好」和「大调略优先」的先验，避免 D 大调被认成 Gb 小调。
    """
    merged: list[dict] = []
    for track in notes_by_track or []:
        merged.extend(track or [])
    hist = chroma_from_notes(merged)
    total = sum(hist)
    if total <= 0:
        return fallback or {"tonic": "C", "tonicPc": 0, "mode": "major", "score": 0.0}

    scored = []
    for tonic in range(12):
        for mode in ("major", "minor"):
            fit = key_fit(hist, tonic, mode)
            fitted = [hist[(tonic + step) % 12] for step in SCALES[mode]]
            profile = KRUMHANSL_MAJOR if mode == "major" else KRUMHANSL_MINOR
            correlation = _corr(fitted, [profile[s] for s in SCALES[mode]])
            fifths = (tonic * 7) % 12
            fifths = fifths if fifths <= 6 else fifths - 12
            if mode == "minor":
                fifths = (fifths + 3) % 12
                fifths = fifths if fifths <= 6 else fifths - 12
            score = fit + correlation * 0.2
            score -= abs(fifths) * 0.02          # 调号简单优先
            if mode == "major":
                score += 0.04                     # 大调略常见
            scored.append({
                "tonicPc": tonic,
                "mode": mode,
                "fit": round(float(fit), 4),
                "correlation": round(float(correlation), 4),
                "score": round(float(score), 4),
            })

    scored.sort(key=lambda item: item["score"], reverse=True)
    best = dict(scored[0])

    # 关系大小调打平时，谱面写大调
    relative = scored[0]["tonicPc"] if scored[0]["mode"] == "major" else (scored[0]["tonicPc"] + 3) % 12
    for item in scored[1:]:
        if item["mode"] != "major":
            continue
        if item["tonicPc"] == relative and best["score"] - item["score"] < 0.06:
            best = dict(item)
            break

    names = NOTE_NAMES_SHARP if (prefer_sharp or _need_sharps(best["tonicPc"], best["mode"])) else NOTE_NAMES_FLAT
    best["tonic"] = names[best["tonicPc"]]
    if best["mode"] == "minor":
        best["relativeMajor"] = NOTE_NAMES_FLAT[(best["tonicPc"] + 3) % 12]
    else:
        best["relativeMinor"] = NOTE_NAMES_FLAT[(best["tonicPc"] + 9) % 12]
    best["source"] = "notes"
    if fallback:
        best["chromaGuess"] = f"{fallback.get('tonic')} {fallback.get('mode')}"
    return best


def _need_sharps(tonic: int, mode: str) -> bool:
    """升号调（G D A E B F# C#）念起来更常见的几个。"""
    major_sharps = {7, 2, 9, 4, 11, 6, 1}
    return mode == "major" and tonic in major_sharps


def scale_tones(tonic: str, mode: str) -> set[int]:
    """某个调的音级集合（pitch class）。"""
    root = name_to_pitch_class(tonic)
    return {(root + step) % 12 for step in (SCALES.get(mode) or SCALES["major"])}


# ---------------------------------------------------------------- 音阶 / 和弦

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "harmonicMinor": [0, 2, 3, 5, 7, 8, 11],
    "melodicMinor": [0, 2, 3, 5, 7, 9, 11],
    "majorPentatonic": [0, 2, 4, 7, 9],
    "minorPentatonic": [0, 3, 5, 7, 10],
    "blues": [0, 3, 5, 6, 7, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "wholeTone": [0, 2, 4, 6, 8, 10],
}

CHORD_TEMPLATES = {
    "": [0, 4, 7],
    "m": [0, 3, 7],
    "dim": [0, 3, 6],
    "aug": [0, 4, 8],
    "sus2": [0, 2, 7],
    "sus4": [0, 5, 7],
    "6": [0, 4, 7, 9],
    "m6": [0, 3, 7, 9],
    "maj7": [0, 4, 7, 11],
    "7": [0, 4, 7, 10],
    "m7": [0, 3, 7, 10],
    "m7b5": [0, 3, 6, 10],
    "dim7": [0, 3, 6, 9],
    "add9": [0, 4, 7, 14],
    "9": [0, 4, 7, 10, 14],
    "maj9": [0, 4, 7, 11, 14],
    "m9": [0, 3, 7, 10, 14],
}


CHORD_PRIOR = {
    "": 0.06,
    "m": 0.05,
    "7": 0.0,
    "m7": -0.01,
    "sus4": -0.05,
    "sus2": -0.06,
    "maj7": -0.05,
    "6": -0.07,
    "m6": -0.08,
    "dim": -0.05,
    "m7b5": -0.07,
    "dim7": -0.12,
    "aug": -0.10,
    "add9": -0.11,
    "9": -0.12,
    "maj9": -0.15,
    "m9": -0.14,
}


def _extra_tones(intervals, root: int) -> tuple[int, float]:
    """三和弦之外还多按了几个音，以及这些音的惩罚。七音轻罚、六度/九度重罚。"""
    count = 0
    weight = 0.0
    for iv in intervals:
        pc = iv % 12
        if pc in (0, 3, 4, 7):
            continue
        count += 1
        weight += 0.02 if pc in (10, 11) else 0.055
    return count, weight


def chord_candidates(chroma, bass_pc: int | None = None, bass_weight: float = 0.1) -> list:
    """按色度向量给和弦打分（相对最像的那个 = 1.0）。

    关键：延伸和弦（6/9/maj7…）音多，如果只按「和弦内音量和 − 和弦外能量」打分，
    它们几乎永远压过三和弦。所以这里加了根音支撑、三音必备、以及多余音惩罚。
    """
    vals = [float(x) for x in chroma]
    total = sum(vals) or 1.0
    vals = [v / total for v in vals]
    peak = max(vals) or 1.0
    scored = []
    for root in range(12):
        for suffix, intervals in CHORD_TEMPLATES.items():
            inside = [(root + iv) % 12 for iv in intervals]
            score = sum(vals[i] for i in inside)
            outside = sum(v for i, v in enumerate(vals) if i not in inside)
            score -= 0.22 * outside
            # 根音得真的在响，否则容易把「A 大三和弦」认成「C6」
            root_rel = vals[root] / peak
            if root_rel < 0.55:
                score -= (0.55 - root_rel) * 0.45
            # 三音（或 sus 的二/四度）必须有能量
            color = next((iv for iv in intervals if (iv % 12) in (3, 4, 2, 5)), 4)
            color_pc = (root + color) % 12
            color_rel = vals[color_pc] / peak
            if color_rel < 0.3:
                score -= (0.3 - color_rel) * 0.4
            count, extra_penalty = _extra_tones(intervals, root)
            score -= extra_penalty
            score += CHORD_PRIOR.get(suffix, 0.0)
            if bass_pc is not None and (bass_pc % 12) == root:
                score += bass_weight
            scored.append({
                "name": f"{NOTE_NAMES_FLAT[root]}{suffix}",
                "root": NOTE_NAMES_FLAT[root],
                "rootPc": root,
                "suffix": suffix,
                "extra": count,
                "score": score,
            })
    scored.sort(key=lambda item: -item["score"])
    top = scored[0]["score"] or 1.0
    for item in scored:
        item["score"] = item["score"] / top
    return scored


def best_chord(chroma, min_score: float = 0.62) -> dict | None:
    cands = chord_candidates(chroma)
    if not cands:
        return None
    top = cands[0]
    if top["score"] < min_score or top["root"] == "N":
        return None
    return top


# ---------------------------------------------------------------- 乐器

# kind: mono 单音（人声/贝斯/萨克斯这类一条旋律线），poly 复音，drums 打击乐
INSTRUMENTS = {
    "vocals": {
        "label": "人声",
        "kind": "mono",
        "clef": "treble",
        "midiProgram": 53,
        "range": (48, 84),          # C3–C6
        "transpose": 0,
        "stem": "vocals",
        "color": "#c8542f",
    },
    "guitar": {
        "label": "吉他",
        "kind": "poly",
        "clef": "treble8vb",
        "midiProgram": 25,
        "range": (40, 88),          # E2–E6
        "transpose": 0,
        "stem": "guitar",
        "color": "#2f6fc8",
    },
    "piano": {
        "label": "钢琴",
        "kind": "poly",
        "clef": "grand",
        "midiProgram": 1,
        "range": (21, 108),
        "transpose": 0,
        "stem": "piano",
        "color": "#4a4a4a",
    },
    "bass": {
        "label": "贝斯",
        "kind": "mono",
        "clef": "bass",
        "midiProgram": 34,
        "range": (28, 60),          # E1–C4
        "transpose": 0,
        "stem": "bass",
        "color": "#7a4fc8",
    },
    "sax": {
        "label": "萨克斯",
        "kind": "mono",
        "clef": "treble",
        "midiProgram": 66,
        "range": (54, 88),
        "transpose": 0,
        "stem": "other",
        "color": "#c8a12f",
    },
    "other": {
        "label": "其他乐器",
        "kind": "poly",
        "clef": "treble",
        "midiProgram": 0,
        "range": (36, 96),
        "transpose": 0,
        "stem": "other",
        "color": "#2f9e8f",
    },
    "violin": {
        "label": "小提琴",
        "kind": "mono",
        "clef": "treble",
        "midiProgram": 40,
        "range": (55, 103),         # G3–G7
        "transpose": 0,
        "stem": "other",
        "color": "#b8552f",
    },
    "erhu": {
        "label": "二胡",
        "kind": "mono",
        "clef": "treble",
        "midiProgram": 110,
        "range": (55, 93),
        "transpose": 0,
        "stem": "other",
        "color": "#a03f3f",
    },
    "flute": {
        "label": "长笛",
        "kind": "mono",
        "clef": "treble",
        "midiProgram": 73,
        "range": (60, 96),
        "transpose": 0,
        "stem": "other",
        "color": "#3f8fa0",
    },
    "ukulele": {
        "label": "尤克里里",
        "kind": "poly",
        "clef": "treble8vb",
        "midiProgram": 24,
        "range": (60, 88),          # C4–E6
        "transpose": 0,
        "stem": "guitar",
        "color": "#3f9e6f",
    },
    "strings": {
        "label": "弦乐",
        "kind": "poly",
        "clef": "treble",
        "midiProgram": 48,
        "range": (36, 96),
        "transpose": 0,
        "stem": "other",
        "color": "#6f5f9e",
    },
    "guzheng": {
        "label": "古筝",
        "kind": "poly",
        "clef": "treble",
        "midiProgram": 107,
        "range": (48, 96),          # C3–C7
        "transpose": 0,
        "stem": "other",
        "color": "#c8a15f",
    },
    "drums": {
        "label": "架子鼓",
        "kind": "drums",
        "clef": "percussion",
        "midiProgram": 0,
        "range": (0, 127),
        "transpose": 0,
        "stem": "drums",
        "color": "#b03a3a",
    },
}

# 鼓组：把 GM 打击乐键映射到五线谱上的显示位置 + 音符头
DRUM_MAP = {
    "kick":  {"label": "底鼓", "midi": 36, "step": "F", "octave": 4, "notehead": "normal", "y": 0},
    "snare": {"label": "军鼓", "midi": 38, "step": "C", "octave": 5, "notehead": "normal", "y": 1},
    "hat":   {"label": "踩镲", "midi": 42, "step": "G", "octave": 5, "notehead": "x", "y": 2},
    "openhat": {"label": "开镲", "midi": 46, "step": "G", "octave": 5, "notehead": "circle-x", "y": 2},
    "ride":  {"label": "叮叮镲", "midi": 51, "step": "F", "octave": 5, "notehead": "x", "y": 3},
    "crash": {"label": "吊镲", "midi": 49, "step": "A", "octave": 5, "notehead": "x", "y": 3},
    "tom1":  {"label": "高音通鼓", "midi": 48, "step": "E", "octave": 5, "notehead": "normal", "y": 4},
    "tom2":  {"label": "中音通鼓", "midi": 45, "step": "D", "octave": 5, "notehead": "normal", "y": 5},
    "tom3":  {"label": "落地通鼓", "midi": 41, "step": "A", "octave": 4, "notehead": "normal", "y": 6},
}

DRUM_ORDER = ["crash", "ride", "openhat", "hat", "tom1", "tom2", "tom3", "snare", "kick"]


def instrument_spec(name: str) -> dict:
    return INSTRUMENTS.get(name) or INSTRUMENTS["other"]


def in_range(pitch: int, spec: dict) -> bool:
    lo, hi = spec.get("range", (0, 127))
    return lo <= pitch <= hi


def fold_into_range(pitch: int, spec: dict) -> int:
    """把越界的音高折回音域（按八度平移），保持音级不变。"""
    lo, hi = spec.get("range", (0, 127))
    p = int(pitch)
    while p < lo:
        p += 12
    while p > hi:
        p -= 12
    return p


# ---------------------------------------------------------------- 记谱辅助

DURATIONS = [
    (4.0, "whole"),
    (3.0, "half"),
    (2.0, "half"),
    (1.5, "quarter"),
    (1.0, "quarter"),
    (0.75, "eighth"),
    (0.5, "eighth"),
    (0.375, "16th"),
    (0.25, "16th"),
]


def duration_type(quarter_length: float) -> str:
    for value, name in DURATIONS:
        if abs(quarter_length - value) < 1e-6:
            return name
    return "16th"


def key_signature_name(tonic: str, mode: str) -> str:
    """music21 key.Key 用的字符串，例如 'G' / 'e'。"""
    tonic = tonic.replace("♯", "#").replace("♭", "b")
    if mode == "minor":
        return tonic.lower()
    return tonic


def snap_notes_to_scale(notes: list[dict], tonic: str, mode: str,
                        short_ql: float = 0.75, max_shift: int = 1) -> tuple[list[dict], int]:
    """把「调外音」里明显是识别误差的吸回音阶。

    只动短音（时值 <= short_ql 拍）——长音更可能是刻意的色彩音，不动。
    离最近调内音超过 max_shift 半音也不动（那可能是转调/离调）。
    返回 (新音符表, 改动个数)。
    """
    tones = scale_tones(tonic, mode)
    if not tones or not notes:
        return notes, 0
    out: list[dict] = []
    fixed = 0
    for note in notes:
        pitch = int(note.get("pitch", 0))
        dur = float(note.get("durationQL") or 0.0)
        if note.get("drum") or dur > short_ql or (pitch % 12) in tones:
            out.append(note)
            continue
        best = None
        for delta in range(-max_shift, max_shift + 1):
            if delta == 0:
                continue
            cand = pitch + delta
            if (cand % 12) in tones and 12 <= cand <= 120:
                if best is None or abs(delta) < abs(best[0]):
                    best = (delta, cand)
        if best is None:
            out.append(note)
            continue
        item = dict(note)
        item["pitch"] = best[1]
        item["snapped"] = best[0]
        out.append(item)
        fixed += 1
    return out, fixed
