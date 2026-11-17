"""指弹独奏改编：把整首歌编进一把吉他。

思路（和弦-旋律编配 chord melody）：
- 旋律（人声/主奏）放高音弦，能听出来是"歌"；
- 低音落在强拍（第 1、3 拍）上，用和弦根音，撑住和声；
- 弱拍（第 2、4 拍）填和弦内音，做出分解和弦的密度；
- 一个起音一个音，六根弦不许重复占用，超出来的音丢掉（宁可少，不要乱）。

输出仍然是一条普通 track（{id, instrument, label, notes}），所以后面的
tab_html / assign_fingering / 六线谱渲染全都直接复用。
"""

from __future__ import annotations

import math

import parts

# 音区 → 目标弦（strings 数组是低→高：0=低音 E，5=高音 E）
def _target_string(pitch: int) -> float:
    if pitch >= 76:
        return 5.0
    if pitch >= 71:
        return 4.6
    if pitch >= 67:
        return 4.0
    if pitch >= 62:
        return 3.2
    if pitch >= 57:
        return 2.4
    if pitch >= 52:
        return 1.6
    if pitch >= 45:
        return 0.8
    return 0.0


def _fret_cost(fret: int) -> float:
    if fret <= 0:
        return 0.45
    if fret <= 5:
        return 0.08 * fret
    if fret <= 9:
        return 0.4 + 0.16 * (fret - 5)
    if fret <= 12:
        return 1.05 + 0.25 * (fret - 9)
    return 1.8 + 0.45 * (fret - 12)


def place_voices(pitches: list[int], strings: list[int], max_fret: int = 14) -> list[dict]:
    """给一组同时响的音找弦位：高音优先、按音区挑弦，一弦一个音。"""
    taken: set[int] = set()
    placed: list[dict] = []
    for pitch in sorted(pitches, reverse=True):
        best = None
        for si in range(len(strings)):
            if si in taken:
                continue
            fret = pitch - strings[si]
            if fret < 0 or fret > max_fret:
                continue
            cost = _fret_cost(fret) + 0.45 * abs(si - _target_string(pitch))
            if best is None or cost < best[0]:
                best = (cost, si, fret)
        if best is None:
            continue
        _, si, fret = best
        taken.add(si)
        placed.append({"string": si, "fret": fret, "pitch": int(pitch)})
    return placed


def _chord_tones(name: str) -> list[int]:
    parsed = parts._parse_chord(name)
    if not parsed:
        return []
    root_pc, intervals = parsed
    return [(root_pc + iv) % 12 for iv in intervals]


def _pitch_for_pc(pc: int, low: int, high: int) -> int | None:
    """把音级放进指定音区里最合适的八度。"""
    pitch = low + ((pc - low) % 12)
    if pitch <= high:
        return pitch
    return None


def pick_melody_track(model: dict) -> dict | None:
    tracks = [t for t in (model.get("tracks") or []) if str(t.get("instrument") or "") != "drums"]
    for track in tracks:
        if str(track.get("instrument") or "") == "vocals":
            return track
    best, best_med = None, -1.0
    for track in tracks:
        pitches = sorted(int(n["pitch"]) for n in track.get("notes") or [] if not n.get("drum"))
        if len(pitches) < 4:
            continue
        med = pitches[len(pitches) // 2]
        if med > best_med:
            best, best_med = track, med
    return best


def melody_line(track: dict | None, grid: float = 0.25) -> dict[float, int]:
    """单声部旋律：同一网格点上留最高音。"""
    if not track:
        return {}
    out: dict[float, int] = {}
    for note in track.get("notes") or []:
        if note.get("drum"):
            continue
        slot = round(float(note["offsetQL"]) / grid) * grid
        pitch = int(note["pitch"])
        if slot not in out or pitch > out[slot]:
            out[slot] = pitch
    return out


def arrange_solo(model: dict, melody_track: dict | None = None, grid: float = 0.5,
                 instrument: str = "guitar", bass_octave: int = 40,
                 melody_shift: int = 0) -> dict:
    """把整首编成一把吉他的指弹/弹唱伴奏谱，返回一条 track。"""
    meta = model.get("meta") or {}
    meter = int(meta.get("meter") or 4)
    chords = model.get("chords") or {}
    strings, _ = parts.tuning_for(instrument)
    mel = melody_line(melody_track or pick_melody_track(model), grid=grid / 2)

    total_ql = 0.0
    for track in model.get("tracks") or []:
        for note in track.get("notes") or []:
            total_ql = max(total_ql, float(note["offsetQL"]) + float(note["durationQL"]))
    measures = max(1, int(math.ceil(total_ql / meter)))

    voiced: dict[float, list[int]] = {}
    for mi in range(measures):
        tones = _chord_tones(str(chords.get(str(mi)) or ""))
        for beat in range(meter):
            for sub in range(int(round(1.0 / grid))):
                t = mi * meter + beat + sub * grid
                pit: list[int] = []
                melody_pitch = mel.get(round(t, 3))
                strong = (beat % 2 == 0) and sub == 0
                if tones:
                    # 低音：强拍上根音 / 五音交替
                    if strong:
                        root = _pitch_for_pc(tones[0], bass_octave, bass_octave + 12)
                        if root:
                            pit.append(root)
                    # 弱拍填和弦内音（三音、五音各一）
                    if not strong and sub == 0 and len(tones) >= 2:
                        for pc in tones[1:3]:
                            p = _pitch_for_pc(pc, bass_octave + 12, bass_octave + 26)
                            if p:
                                pit.append(p)
                if melody_pitch is not None:
                    pit.append(int(melody_pitch) + int(melody_shift))
                if not pit:
                    continue
                # 同一时刻同音只留一个（避免八度重复挤掉别的音）
                dedup: list[int] = []
                for p in sorted(set(pit), reverse=True):
                    if any(abs(p - q) % 12 == 0 and abs(p - q) > 0 and abs(p - q) < 12 for q in dedup):
                        continue
                    dedup.append(p)
                voiced[round(t, 3)] = dedup

    placed = place_voices_sequence(voiced, strings)
    notes = []
    keys = sorted(placed)
    for i, t in enumerate(keys):
        nxt = keys[i + 1] if i + 1 < len(keys) else t + grid
        dur = max(grid, min(2.0, nxt - t))
        for item in placed[t]:
            notes.append({
                "offsetQL": float(t),
                "durationQL": float(dur),
                "pitch": int(item["pitch"]),
                "velocity": 0.72 if t % 1 == 0 else 0.6,
                "string": item["string"],
                "fret": item["fret"],
            })
    return {
        "id": "guitar-solo",
        "instrument": instrument,
        "label": "吉他（指弹）",
        "kind": "pitched",
        "chords": dict(chords),
        "notes": notes,
    }


def place_voices_sequence(voiced: dict[float, list[int]], strings: list[int],
                          window: float = 1.5) -> dict[float, list[dict]]:
    """逐拍挑弦位，顺带约束"前后两拍别在同一根弦上跳来跳去"。"""
    out: dict[float, list[dict]] = {}
    last_fret: dict[int, int] = {}
    for t in sorted(voiced):
        taken: set[int] = set()
        placed: list[dict] = []
        for pitch in sorted(voiced[t], reverse=True):
            best = None
            for si in range(len(strings)):
                if si in taken:
                    continue
                fret = pitch - strings[si]
                if fret < 0 or fret > 14:
                    continue
                cost = _fret_cost(fret) + 0.45 * abs(si - _target_string(pitch))
                prev = last_fret.get(si)
                if prev is not None:
                    cost += 0.10 * abs(fret - prev)
                if best is None or cost < best[0]:
                    best = (cost, si, fret)
            if best is None:
                continue
            _, si, fret = best
            taken.add(si)
            placed.append({"string": si, "fret": fret, "pitch": int(pitch)})
        for item in placed:
            last_fret[item["string"]] = item["fret"]
        out[t] = placed
    return out
