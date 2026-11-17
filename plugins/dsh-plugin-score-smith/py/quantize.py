# -*- coding: utf-8 -*-
"""把「秒」量化到「拍」，再排进小节。

输出统一用 music21 的 offset / quarterLength 语义：
  offsetQL   从第一小节第一拍算起的四分音符数
  durationQL 时值（四分音符为单位）

量化网格默认 1/16（0.25 个四分音符），可以调成 1/8、1/32，
也能按三连音网格排（grid="1/12"）。
"""

from __future__ import annotations

import bisect
import math

import numpy as np

GRID_NAMES = {
    "1/4": 1.0,
    "1/8": 0.5,
    "1/8t": 1.0 / 3.0,
    "1/16": 0.25,
    "1/16t": 1.0 / 6.0,
    "1/32": 0.125,
}


def grid_value(name: str) -> float:
    return GRID_NAMES.get(str(name), 0.25)


class BeatMapper:
    """秒 → 拍号（四分音符数）。有拍点就用拍点插值，没有就用恒定速度。"""

    def __init__(self, beat_times, tempo: float, origin: float = 0.0, meter: int = 4):
        self.tempo = float(tempo or 120.0)
        self.period = 60.0 / self.tempo
        self.meter = int(meter or 4)
        beats = sorted(float(b) for b in (beat_times or []))
        if beats:
            origin = float(origin) if origin is not None else beats[0]
            # 把拍点换算成"从 origin 起的第几个拍"。
            # 以前用 round((t-origin)/period)：速度只要差 1%，越往后错越多，
            # 最后相邻两拍会被算成同一个位置、直接吞掉一拍；现在按相邻间隔累积，
            # 只在间隔接近两拍（漏拍）时才跨一格。
            self.times = []
            self.positions = []
            first = round((beats[0] - origin) / self.period)
            for t in beats:
                if not self.times:
                    pos = float(first)
                else:
                    gap = t - self.times[-1]
                    step = max(1, int(round(gap / self.period)))
                    pos = self.positions[-1] + step
                self.times.append(float(t))
                self.positions.append(float(pos))
            # 拍点太少或者步子乱，退回恒定速度
            if len(self.times) < 4 or self._too_irregular():
                self.times = []
                self.positions = []
            # 速度稳定但拍点本身在抖（±40 ms 很常见）：用理想等距网格，
            # 不然 0.25 拍网格上的音会被抖到隔壁十六分音符去（1.2 s 变成 1.05 s）。
            elif len(self.times) >= 8:
                idx = np.arange(len(self.times), dtype=float)
                arr = np.asarray(self.times)
                slope, intercept = np.polyfit(idx, arr, 1)
                resid = arr - (slope * idx + intercept)
                if (slope > 0 and float(np.std(resid)) <= 0.06
                        and abs(float(slope) - self.period) / self.period < 0.15):
                    self.period = float(slope)
                    self.times = []
                    self.positions = []
            self.origin = origin
        else:
            self.origin = float(origin or 0.0)
            self.times = []
            self.positions = []

    def _too_irregular(self) -> bool:
        gaps = np.diff(self.times)
        if len(gaps) == 0:
            return True
        med = float(np.median(gaps)) or self.period
        # 拍点间隔偏离中位数太多（说明漏拍/多拍），不要用
        return bool(np.mean(np.abs(gaps - med) > 0.35 * med) > 0.25)

    def beat_at(self, t: float) -> float:
        if not self.times:
            return (float(t) - self.origin) / self.period
        if t <= self.times[0]:
            return self.positions[0] + (t - self.times[0]) / self.period
        if t >= self.times[-1]:
            return self.positions[-1] + (t - self.times[-1]) / self.period
        i = bisect.bisect_right(self.times, t) - 1
        t0, t1 = self.times[i], self.times[i + 1]
        p0, p1 = self.positions[i], self.positions[i + 1]
        if t1 <= t0:
            return p0
        return p0 + (t - t0) / (t1 - t0) * (p1 - p0)


def quantize_notes(notes: list[dict], mapper: BeatMapper, grid: float = 0.25,
                   min_dur: float | None = None, meter: int = 4,
                   mono: bool = False, keep_last: bool = True,
                   drums: bool = False) -> list[dict]:
    """音符秒值 → offsetQL / durationQL，并做去重、合并、超界裁剪。"""
    min_dur = min_dur if min_dur is not None else grid
    # 三连音：1/16、1/32 网格都表示不了 1/3 拍，硬量化会把 76-77-76 并成两个音。
    # 单声部（人声/独奏）先找「三个相邻音把一拍三等分」的组，直接落到 1/3 网格上。
    triplets = _triplet_map(notes, mapper) if (mono and not drums) else {}
    out = []
    for note in notes:
        start_q = mapper.beat_at(note["start"])
        end_q = mapper.beat_at(note["end"])
        # 拍点没覆盖到的开头（前奏抢拍）不要整条丢掉，压到 0 更合理
        start_q = max(0.0, _snap(start_q, grid))
        end_q = max(start_q, _snap(end_q, grid))
        dur = end_q - start_q
        if dur < min_dur:
            dur = min_dur
        triplet = triplets.get(id(note))
        if triplet:
            start_q, dur = triplet[0], triplet[1]
        out.append({
            "offsetQL": round(start_q, 6),
            "durationQL": round(dur, 6),
            "pitch": int(note.get("pitch", 0)),
            "velocity": float(note.get("velocity", 0.7)),
            "drum": note.get("drum"),
            "rawStart": note.get("start"),
            "retrigger": bool(note.get("retrigger")),
            "grace": bool(note.get("grace")),
            "slur_in": bool(note.get("slur_in")),
            "triplet": bool(triplet),
            "tgroup": int(triplet[2]) if triplet else 0,
        })

    out.sort(key=lambda n: (n["offsetQL"], n["pitch"]))
    if drums:
        # 鼓没有音高（pitch 都是 0），同音高合并会把踩镲并成一条长音，只能按「同一位置同一件鼓」去重
        out = _dedupe_drum_hits(out)
    else:
        out = _merge_same_pitch(out)
        if mono:
            out = _flatten_mono(out)
    out = _clip_overlaps(out, mono=mono)
    if not keep_last:
        out = [n for n in out if n["durationQL"] >= grid]
    return out


def _snap(value: float, grid: float) -> float:
    return round(value / grid) * grid


def _triplet_map(notes: list[dict], mapper: BeatMapper) -> dict[int, tuple[float, float, int]]:
    """找三连音组：三个相邻单音把一拍（或其一部分）平分成三等分。

    返回 {id(原音符): (落地 offsetQL, durationQL, 组号)}。
    判据用「拍位坐标」而不是秒，速度不同也能用；容差 0.085 拍 ≈ 一个 32 分音符的一半。
    """
    tol = 0.07
    third = 1.0 / 3.0
    ordered = sorted(notes, key=lambda n: (float(n.get("start") or 0.0),
                                           int(n.get("pitch") or 0)))
    mapped: dict[int, tuple[float, float, int]] = {}
    used: set[int] = set()
    group = 0
    i = 0
    while i + 2 < len(ordered):
        trio = ordered[i:i + 3]
        if any(id(x) in used for x in trio):
            i += 1
            continue
        try:
            q = [mapper.beat_at(float(x["start"])) for x in trio]
            raw_dur = [float(x["end"]) - float(x["start"]) for x in trio]
        except (KeyError, TypeError, ValueError):
            i += 1
            continue
        # 三个音必须挤在一个拍位区间里，而且间隔真的接近 1/3 拍
        span = q[2] - q[0]
        d1, d2 = q[1] - q[0], q[2] - q[1]
        if not (0.45 <= span <= 0.90 and q[0] < q[1] < q[2]):
            i += 1
            continue
        if abs(d1 - third) > 0.10 or abs(d2 - third) > 0.10:
            i += 1
            continue
        # 首尾都要落在 1/3 网格上，中间那个大概在中间就行（换音检测常晚一二十毫秒）
        base = round(q[0] * 3.0) / 3.0
        if abs(q[0] - base) > tol or abs(q[2] - (base + third * 2)) > tol:
            i += 1
            continue
        if min(raw_dur) < 0.045:
            i += 1
            continue
        group += 1
        # 保留三连音真实的起点（不强吸到 1/3 网格）：起点落在 2.2 拍这种位置的
        # 三连音在流行歌里很常见，硬挪到 2.333 会让整组晚 80 ms。
        for k, item in enumerate(trio):
            mapped[id(item)] = (q[0] + third * k, third, group)
        used.update(id(x) for x in trio)
        i += 3
    return mapped


def _dedupe_drum_hits(notes: list[dict]) -> list[dict]:
    """同一个网格位置上同一件鼓只留一下。"""
    seen = set()
    out = []
    for note in notes:
        key = (round(note["offsetQL"], 6), str(note.get("drum") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(note)
    return out


def _merge_same_pitch(notes: list[dict]) -> list[dict]:
    """同音高且首尾紧挨着的相邻音合成一个（识别器常见的碎片）。

    被起音切开的重复音（retrigger）不合并，否则连敲的音会被并成一条长音。
    """
    out: list[dict] = []
    for note in notes:
        if out and out[-1]["pitch"] == note["pitch"]:
            prev = out[-1]
            if prev.get("retrigger") or note.get("retrigger"):
                out.append(dict(note))
                continue
            prev_end = prev["offsetQL"] + prev["durationQL"]
            if abs(note["offsetQL"] - prev_end) <= 1e-6:
                prev["durationQL"] = round(note["offsetQL"] + note["durationQL"] - prev["offsetQL"], 6)
                continue
            if note["offsetQL"] < prev_end - 1e-6 and note["offsetQL"] >= prev["offsetQL"]:
                # 重叠：短的丢弃，长的保留
                if note["durationQL"] <= prev["durationQL"]:
                    continue
                prev["durationQL"] = round(note["offsetQL"] + note["durationQL"] - prev["offsetQL"], 6)
                continue
        out.append(dict(note))
    return out


def _flatten_mono(notes: list[dict]) -> list[dict]:
    """单音乐器：同一时间只能响一个音，重叠时截短前一个。"""
    notes = sorted(notes, key=lambda n: (n["offsetQL"], -n["velocity"]))
    out: list[dict] = []
    for note in notes:
        if out:
            prev = out[-1]
            prev_end = prev["offsetQL"] + prev["durationQL"]
            if note["offsetQL"] < prev_end - 1e-6:
                prev["durationQL"] = round(max(0.0, note["offsetQL"] - prev["offsetQL"]), 6)
                if prev["durationQL"] <= 0:
                    out.pop()
        out.append(dict(note))
    return [n for n in out if n["durationQL"] > 0]


def _clip_overlaps(notes: list[dict], mono: bool = False) -> list[dict]:
    out = []
    for note in notes:
        if note["durationQL"] <= 0:
            continue
        out.append(note)
    return out


def legato(notes: list[dict], max_gap: float = 0.5) -> list[dict]:
    """连音化：同一条旋律线上，前一个音拖到后一个音起音为止（缺口小于 max_gap 拍）。

    识别的音尾普遍偏短（发音衰减、换音检测滞后），不补齐的话谱面全是碎八分休止。
    """
    ordered = sorted(notes, key=lambda n: (n["offsetQL"], n["pitch"]))
    for index in range(len(ordered) - 1):
        cur = ordered[index]
        nxt = ordered[index + 1]
        if cur["pitch"] == nxt["pitch"] and cur.get("retrigger") and nxt.get("retrigger"):
            continue
        end = cur["offsetQL"] + cur["durationQL"]
        gap = nxt["offsetQL"] - end
        if 0.0 <= gap <= max_gap and cur["pitch"] != nxt["pitch"]:
            cur["durationQL"] = round(nxt["offsetQL"] - cur["offsetQL"], 6)
    return ordered


def tidy_retriggers(notes: list[dict], grid: float = 0.25) -> list[dict]:
    """重复音的分界点如果落在奇怪的 1/16 上（起音检测晚了一点点），挪到最近的 1/8。"""
    ordered = sorted(notes, key=lambda n: (n["offsetQL"], n["pitch"]))
    coarse = grid * 2.0
    for index in range(len(ordered) - 1):
        cur, nxt = ordered[index], ordered[index + 1]
        if cur["pitch"] != nxt["pitch"]:
            continue
        boundary = nxt["offsetQL"]
        if abs((cur["offsetQL"] + cur["durationQL"]) - boundary) > 1e-6:
            continue
        snapped = round(boundary / coarse) * coarse
        if abs(snapped - boundary) < 1e-6 or abs(snapped - boundary) > grid + 1e-6:
            continue
        if snapped <= cur["offsetQL"] or snapped >= nxt["offsetQL"] + nxt["durationQL"]:
            continue
        cur["durationQL"] = round(snapped - cur["offsetQL"], 6)
        nxt["durationQL"] = round(nxt["offsetQL"] + nxt["durationQL"] - snapped, 6)
        nxt["offsetQL"] = round(snapped, 6)
    return ordered


def unify_chord_durations(notes: list[dict], grid: float = 0.25) -> list[dict]:
    """同一拍上一起按下去的几个音，时值取齐（识别器对每个泛音给的尾巴长短不一样）。

    只在彼此时长差不到一倍时才取齐，免得把真的分开的音也拉平。
    """
    groups: dict[float, list[dict]] = {}
    for note in notes:
        key = round(float(note["offsetQL"]) / grid) * grid
        groups.setdefault(round(key, 6), []).append(note)
    for group in groups.values():
        if len(group) < 2:
            continue
        durs = [float(n["durationQL"]) for n in group]
        longest = max(durs)
        if longest <= 0 or min(durs) < longest / 2.0:
            continue
        for note in group:
            note["durationQL"] = round(longest, 6)
    return notes


def group_by_measure(notes: list[dict], meter: int = 4) -> dict:
    """按小节分组，顺便算每小节的和弦候选用的音高集合。"""
    measures: dict[int, list[dict]] = {}
    for note in notes:
        measure_index = int(note["offsetQL"] // meter)
        measures.setdefault(measure_index, []).append(note)
    return measures


def estimate_chords(notes: list[dict], meter: int = 4, min_score: float = 0.55) -> dict:
    """每小节从本轨音符量出和弦（按发音时长加权，低音区加倍看重）。"""
    from theory import chord_candidates

    measures = group_by_measure(notes, meter)
    result = {}
    for index, group in measures.items():
        chroma = [0.0] * 12
        low = [0.0] * 12
        for note in group:
            weight = max(0.25, note["durationQL"]) * (0.5 + note["velocity"])
            chroma[note["pitch"] % 12] += weight
            if note["pitch"] <= 55:  # G3 以下当作低音线索
                low[note["pitch"] % 12] += weight * (1.0 + (55 - note["pitch"]) * 0.05)
        if sum(chroma) <= 0:
            continue
        # 把低音的能量补进整体色度，避免和弦根音被高音区的泛音带走
        if sum(low) > 0:
            scale = sum(chroma) / sum(low) * 0.35
            chroma = [c + l * scale for c, l in zip(chroma, low)]
        bass_pc = max(range(12), key=lambda pc: low[pc]) if sum(low) > 0 else None
        cands = chord_candidates(chroma, bass_pc=bass_pc)
        if cands and cands[0]["score"] >= min_score:
            result[index] = cands[0]["name"]
    return result


def estimate_song_chords(track_notes: dict, meter: int = 4, min_score: float = 0.5) -> dict:
    """全曲和弦：把所有有音高的轨混在一起按小节量，而不是各轨量完再投票。

    低音区（贝斯）给更高权重当根音线索，人声/其他轨权重低一点，免得旋律把和弦带偏。
    """
    from theory import chord_candidates

    weights = {"bass": 1.25, "guitar": 1.0, "piano": 1.0, "sax": 1.0,
               "strings": 0.8, "other": 0.7, "vocals": 0.75}
    measures: dict[int, tuple[list[float], list[float]]] = {}
    for track_id, notes in track_notes.items():
        if not notes:
            continue
        weight = weights.get(track_id, 0.8)
        for note in notes:
            index = int(note["offsetQL"] // meter)
            chroma, low = measures.setdefault(index, ([0.0] * 12, [0.0] * 12))
            duration = max(0.25, min(float(note["durationQL"]), float(meter)))
            energy = duration * (0.5 + note["velocity"]) * weight
            chroma[note["pitch"] % 12] += energy
            if note["pitch"] <= 55:
                low[note["pitch"] % 12] += energy * (1.0 + (55 - note["pitch"]) * 0.05)

    result = {}
    for index, (chroma, low) in sorted(measures.items()):
        total = sum(chroma)
        if total <= 0:
            continue
        if sum(low) > 0:
            scale = total / sum(low) * 0.35
            chroma = [c + l * scale for c, l in zip(chroma, low)]
        bass_pc = max(range(12), key=lambda pc: low[pc]) if sum(low) > 0 else None
        cands = chord_candidates(chroma, bass_pc=bass_pc)
        if cands and cands[0]["score"] >= min_score:
            result[index] = cands[0]["name"]
    return result


MAJOR_LIKE = {"", "7", "maj7", "6", "add9", "9", "maj9"}
MINOR_LIKE = {"m", "m7", "m6", "m9", "m7b5", "dim", "dim7"}
ROOT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


def _chord_identity(name: str):
    """把 'Cmaj9' 拆成 ((根音音级, 大/小/挂留), 'Cmaj9')，用来投票。"""
    root = None
    for candidate in sorted(ROOT_NAMES, key=len, reverse=True):
        if name.startswith(candidate):
            root = candidate
            break
    if root is None:
        return None
    suffix = name[len(root):]
    if suffix in MINOR_LIKE:
        family = "m"
    elif suffix in MAJOR_LIKE:
        family = "M"
    else:
        family = "s"
    root_pc = ROOT_NAMES.index(root)
    return (root_pc, family), name


def merge_chord_tracks(tracks: list[dict]) -> dict:
    """多轨和弦合并：先按「根音 + 大小调性」投票，再在同一格里选最长出现的那个写法。"""
    votes: dict[int, dict] = {}
    for track in tracks:
        for measure, name in track.items():
            ident = _chord_identity(name)
            if ident is None:
                continue
            key, full = ident
            bucket = votes.setdefault(measure, {})
            cell = bucket.setdefault(key, {"count": 0, "names": {}})
            cell["count"] += 1
            cell["names"][full] = cell["names"].get(full, 0) + 1
    result = {}
    for measure, bucket in votes.items():
        key, cell = max(bucket.items(), key=lambda kv: (kv[1]["count"], max(kv[1]["names"].values())))
        name = max(cell["names"].items(), key=lambda kv: kv[1])[0]
        result[measure] = name
    return result
