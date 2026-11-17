# -*- coding: utf-8 -*-
"""演奏谱：把一条轨渲染成「这种乐器的人会看的谱」。

- 吉他 / 贝斯 → 六线谱 / 四线谱（TAB，MusicXML 的 TAB 谱表 + verovio 渲染）
- 人声 → 简谱（数字谱，HTML 排版后截图，能挂歌词）
- 钢琴 / 鼓 / 其他乐器 → 五线谱（music21 出单轨 MusicXML）

每个乐器一个目录，互不掺和：parts/02-guitar/ 里只有六线谱。
"""

from __future__ import annotations

import html
import json
import os
import re
import unicodedata

# ---------------------------------------------------------------- 定弦

TUNINGS = {
    "guitar": [40, 45, 50, 55, 59, 64],            # E2 A2 D3 G3 B3 E4，低→高
    "ukulele": [67, 60, 64, 69],                   # G4 C4 E4 A4（重入弦，顺序见 names）
    "bass": [28, 33, 38, 43],                      # E1 A1 D2 G2
}

TUNING_NAMES = {
    "guitar": ["E2", "A2", "D3", "G3", "B3", "E4"],
    "ukulele": ["G4", "C4", "E4", "A4"],
    "bass": ["E1", "A1", "D2", "G2"],
}

MAX_FRET = 15


def tuning_for(instrument: str) -> tuple[list[int], list[str]]:
    inst = (instrument or "").lower()
    if inst in ("ukulele", "uke"):
        return TUNINGS["ukulele"], TUNING_NAMES["ukulele"]
    if inst in ("bass", "electric_bass"):
        return TUNINGS["bass"], TUNING_NAMES["bass"]
    return TUNINGS["guitar"], TUNING_NAMES["guitar"]


def fretted_instrument(instrument: str) -> bool:
    return (instrument or "").lower() in ("guitar", "bass", "ukulele", "uke", "electric_bass")


# ---------------------------------------------------------------- 品格指法

def _fret_candidates(pitch: int, strings: list[int]) -> list[tuple[int, int, float]]:
    """返回 (弦号 0=最低音弦, 品位, 代价)。代价小=更好按。"""
    out = []
    for idx, open_pitch in enumerate(strings):
        fret = pitch - open_pitch
        if fret < 0 or fret > MAX_FRET:
            continue
        # 空弦便宜，1–7 品最舒服，往高把位逐渐变贵
        if fret == 0:
            cost = 0.4
        elif fret <= 7:
            cost = 0.1 * fret
        elif fret <= 12:
            cost = 0.7 + 0.35 * (fret - 7)
        else:
            cost = 2.5 + 0.6 * (fret - 12)
        # 低音弦上弹高把位不合手；高音弦上弹低品也不爽
        cost += abs(idx - (len(strings) - 1 - fret / max(1.0, MAX_FRET) * len(strings))) * 0.05
        out.append((idx, int(fret), cost))
    return out


def assign_fingering(pitches: list[int], strings: list[int]) -> list[tuple[int, int] | None]:
    """给同一时刻的一组音分配 (弦号, 品位)，一弦一个音。分配不出来就返回 None。"""
    order = sorted(range(len(pitches)), key=lambda i: pitches[i])  # 低音先挑低弦
    used: set[int] = set()
    result: list[tuple[int, int] | None] = [None] * len(pitches)
    for i in order:
        cands = [c for c in _fret_candidates(pitches[i], strings) if c[0] not in used]
        if not cands:
            # 实在放不下：允许同弦（谱面会显示成和弦，先保证音不缺）
            cands = _fret_candidates(pitches[i], strings)
        if not cands:
            continue
        cands.sort(key=lambda c: (c[2], c[1]))
        idx, fret, _ = cands[0]
        used.add(idx)
        result[i] = (idx, fret)
    return result


# ---------------------------------------------------------------- 六线谱（自绘 HTML）

def tab_clusters(track: dict) -> list[dict]:
    """把一条吉他轨按起音聚簇，并标出指法、是否扫弦。"""
    instrument = track.get("instrument") or "guitar"
    strings, _ = tuning_for(instrument)
    clusters = _onset_clusters([n for n in track.get("notes") or [] if not n.get("drum")])
    out = []
    for i, cl in enumerate(clusters):
        pitches = [int(n["pitch"]) for n in cl["notes"]]
        fingers = assign_fingering(pitches, strings)
        placed = []
        for note, fing in zip(cl["notes"], fingers):
            if not fing:
                continue
            idx, fret = fing
            placed.append({"string": idx, "fret": fret, "pitch": int(note["pitch"]),
                           "velocity": float(note.get("velocity") or 0.7)})
        next_ql = clusters[i + 1]["ql"] if i + 1 < len(clusters) else cl["ql"] + 1.0
        dur = max(0.25, min(4.0, next_ql - cl["ql"]))
        placed.sort(key=lambda p: p["string"])
        strum = None
        if len(placed) >= 3:
            rows = [p["string"] for p in placed]
            if rows == sorted(rows) and rows[-1] - rows[0] >= 2:
                strum = "down"      # 从低音弦扫到高音弦 = 下扫
            elif rows == sorted(rows, reverse=True) and rows[0] - rows[-1] >= 2:
                strum = "up"
        out.append({"ql": cl["ql"], "dur": dur, "notes": placed, "strum": strum,
                    "cluster": len(placed)})
    return out


_ROOT_PC = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "Fb": 4, "E#": 5, "F": 5,
    "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11, "Cb": 11,
}

_CHORD_CACHE: dict[tuple[str, str], dict | None] = {}


def _parse_chord(name: str) -> tuple[int, list[int]] | None:
    text = str(name or "").strip()
    if not text:
        return None
    cut = 1
    if len(text) > 1 and text[1] in "#b":
        cut = 2
    root = text[:cut]
    if root not in _ROOT_PC:
        return None
    try:
        import theory
    except Exception:
        return None
    suffix = text[cut:]
    intervals = theory.CHORD_TEMPLATES.get(suffix)
    if intervals is None:
        lowered = suffix.replace("maj", "").replace("M", "")
        intervals = theory.CHORD_TEMPLATES.get(lowered) or theory.CHORD_TEMPLATES.get(suffix.lower())
    if intervals is None:
        return None
    return _ROOT_PC[root], list(intervals)


def chord_shape(name: str, instrument: str = "guitar", max_fret: int = 9) -> dict | None:
    """给和弦名找一个能按的指型：每根弦按第几品（None = 不弹）。"""
    key = (str(name or ""), instrument)
    if key in _CHORD_CACHE:
        return _CHORD_CACHE[key]
    parsed = _parse_chord(name)
    strings, _ = tuning_for(instrument)
    n = len(strings)
    result = None
    if parsed and n:
        root_pc, intervals = parsed
        tones = {(root_pc + iv) % 12 for iv in intervals}
        best: tuple[float, list] | None = None

        def walk(i: int, choice: list, lo: int, hi: int) -> None:
            nonlocal best
            if hi - lo > 4:
                return
            if i == n:
                sounding = [(k, f) for k, f in enumerate(choice) if f is not None]
                if len(sounding) < 3:
                    return
                fretted = [f for _, f in sounding if f > 0]
                if not fretted:
                    return
                used = {(strings[k] + f) % 12 for k, f in sounding}
                if root_pc not in used:
                    return
                span = max(fretted) - min(fretted)
                bass_pc = (strings[sounding[0][0]] + sounding[0][1]) % 12
                score = 0.0
                score += 3.0 if bass_pc == root_pc else 0.0
                score += 0.6 * len(used) + 0.35 * len(sounding)
                score -= 0.55 * span + 0.35 * max(0, max(fretted) - 5)
                gaps = sounding[-1][0] - sounding[0][0] + 1 - len(sounding)
                score -= 0.4 * gaps
                if 0 in [f for _, f in sounding]:
                    score += 0.3
                if best is None or score > best[0]:
                    best = (score, list(choice))
                return
            for fret in (None,) + tuple(range(0, max_fret + 1)):
                if fret is None:
                    choice.append(None)
                    walk(i + 1, choice, lo, hi)
                    choice.pop()
                    continue
                pc = (strings[i] + fret) % 12
                if pc not in tones:
                    continue
                if fret > 0:
                    nlo, nhi = (min(lo, fret), max(hi, fret)) if lo else (fret, fret)
                else:
                    nlo, nhi = lo, hi
                if nhi - nlo > 4:
                    continue
                choice.append(fret)
                walk(i + 1, choice, nlo, nhi)
                choice.pop()

        walk(0, [], 0, 0)
        if best:
            choice = best[1]
            frets = [f for f in choice if f is not None]
            base = min([f for f in frets if f > 0] or [1])
            result = {"name": str(name), "choice": choice, "base": base,
                      "strings": strings, "open_positions": [i for i, f in enumerate(choice) if f == 0],
                      "muted": [i for i, f in enumerate(choice) if f is None]}
    _CHORD_CACHE[key] = result
    return result


def chord_diagram_svg(name: str, instrument: str = "guitar") -> str:
    """和弦指法图（小图，竖着六根弦）。"""
    shape = chord_shape(name, instrument)
    if not shape:
        return ""
    strings = shape["strings"]
    n = len(strings)
    choice = shape["choice"]
    base = shape["base"]
    rows = 5
    w, h = 16 + n * 11, 30 + rows * 12
    pad_x, top = 8, 16
    parts = [f'<svg class="dg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">']
    # 六根弦（左=低音弦）
    for k in range(n):
        x = pad_x + k * 11
        parts.append(f'<line x1="{x}" y1="{top}" x2="{x}" y2="{top + rows * 12}" '
                     f'stroke="#333" stroke-width="1"/>')
    # 品格
    for r in range(rows + 1):
        y = top + r * 12
        thick = 2.4 if r == 0 and base <= 1 else 1.1
        parts.append(f'<line x1="{pad_x}" y1="{y}" x2="{pad_x + (n - 1) * 11}" y2="{y}" '
                     f'stroke="#333" stroke-width="{thick}"/>')
    # 空弦/闷音
    for k in range(n):
        x = pad_x + k * 11
        f = choice[k]
        if f is None:
            parts.append(f'<text x="{x}" y="{top - 5}" font-size="10" fill="#888" '
                         f'text-anchor="middle">×</text>')
        elif f == 0:
            parts.append(f'<circle cx="{x}" cy="{top - 8}" r="3" fill="none" stroke="#333" '
                         f'stroke-width="1.1"/>')
    # 按弦点
    for k in range(n):
        f = choice[k]
        if not f:
            continue
        rowidx = f - base
        if rowidx < 0 or rowidx >= rows:
            continue
        x = pad_x + k * 11
        y = top + rowidx * 12 + 6
        parts.append(f'<circle cx="{x}" cy="{y}" r="4.6" fill="#1a4b8c"/>')
    if base > 1:
        parts.append(f'<text x="{pad_x - 3}" y="{top + 9}" font-size="10" fill="#1a4b8c" '
                     f'text-anchor="end">{base}</text>')
    parts.append("</svg>")
    return "".join(parts)


def tab_html(model: dict, track: dict, measures_per_row: int = 2,
             melody: dict | None = None, lyrics: list[str] | None = None) -> str:
    meta = model.get("meta") or {}
    meter = int(meta.get("meter") or 4)
    tempo = int(round(float(meta.get("tempo") or 100)))
    instrument = track.get("instrument") or "guitar"
    strings, names = tuning_for(instrument)
    nlines = len(strings)
    label = str(track.get("label") or "吉他")
    title = str(model.get("title") or "")
    chords = model.get("chords") or {}

    clusters = tab_clusters(track)
    total_ql = max((c["ql"] + c["dur"] for c in clusters), default=meter)
    measure_count = max(1, int((total_ql + meter - 1) // meter))

    by_measure: dict[int, list[dict]] = {}
    for c in clusters:
        by_measure.setdefault(int(c["ql"] // meter), []).append(c)

    # 顶部：和弦指法图；底部：简谱 + 歌词（有旋律轨时）
    mel_notes = jianpu_notes(melody, model) if melody else []
    mel_by_measure: dict[int, list[dict]] = {}
    for n in mel_notes:
        mel_by_measure.setdefault(int(n["offsetQL"] // meter), []).append(n)
    sylls: list[str] = []
    for line in lyrics or []:
        line = str(line).strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) <= 1:
            parts = [c for c in line if not c.isspace()]
        sylls.extend(parts)
    syl_i = 0

    top_chord = 2
    bar_top = 96
    arr_top = bar_top + nlines * 17 + 4
    mel_top = arr_top + 22
    lyr_top = mel_top + 17
    row_h = (lyr_top + 22) if melody else (arr_top + 24)

    rows_html = []
    m = 0
    while m < measure_count:
        cells = []
        for k in range(measures_per_row):
            mi = m + k
            if mi >= measure_count:
                break
            chord = chords.get(str(mi))
            notes_html = []
            for c in by_measure.get(mi, []):
                x = 3.0 + (c["ql"] - mi * meter) / meter * 94.0
                mark = ""
                if c["dur"] < 0.75:
                    mark = '<i class="u"></i>'
                elif c["dur"] < 1.5:
                    mark = '<i class="u long"></i>'
                arrows = ""
                if c["strum"] == "down":
                    arrows = '<b class="ar dn">↓</b>'
                elif c["strum"] == "up":
                    arrows = '<b class="ar up">↑</b>'
                for n in c["notes"]:
                    # 最低音弦画在最下面一行：k = nlines-1-string
                    top = bar_top + (nlines - 1 - n["string"]) * 17 + 8.5
                    notes_html.append(
                        f'<span class="f" style="left:{x:.2f}%;top:{top:.1f}px">{n["fret"]}{mark}</span>'
                    )
                notes_html.append(
                    f'<span class="pos" style="left:{x:.2f}%;top:{arr_top}px">{arrows}</span>'
                )
            # 简谱旋律 + 歌词
            if melody:
                for n in sorted(mel_by_measure.get(mi, []), key=lambda v: v["offsetQL"]):
                    x = 3.0 + (n["offsetQL"] - mi * meter) / meter * 94.0
                    up = '<i class="oc up"></i>' * max(0, n["octave"])
                    dn = '<i class="oc dn"></i>' * max(0, -n["octave"])
                    mark = ""
                    if n["durationQL"] < 0.6:
                        mark = '<i class="u"></i>'
                    elif n["durationQL"] < 1.2:
                        mark = '<i class="u long"></i>'
                    dash = ""
                    if n["durationQL"] >= 1.6:
                        dash = '<em class="dash">－</em>'
                    syl = ""
                    if syl_i < len(sylls):
                        syl = sylls[syl_i]
                        syl_i += 1
                    notes_html.append(
                        f'<span class="jp" style="left:{x:.2f}%;top:{mel_top}px">'
                        f'{up}<b>{html.escape(str(n["degree"]))}</b>{dn}{dash}{mark}'
                        f'<em class="lyr">{html.escape(syl)}</em></span>'
                    )
            diagram = chord_diagram_svg(str(chord), instrument) if chord else ""
            chord_html = f'<span class="ch">{html.escape(str(chord))}</span>' if chord else ""
            cells.append(
                f'<div class="m">{diagram}{chord_html}'
                f'<div class="bar">{"".join(f"<i></i>" for _ in range(nlines))}</div>'
                f'<span class="mno">{mi + 1}</span>{"".join(notes_html)}</div>'
            )
        rows_html.append(f'<div class="row">{"".join(cells)}</div>')
        m += measures_per_row

    tuning_txt = " ".join(names)
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{html.escape(title)} · {html.escape(label)} 六线谱</title>
<style>
 body {{ margin:0; padding:26px 30px 40px; background:#fff; color:#111;
        font-family:"Segoe UI","Microsoft YaHei",sans-serif; }}
 h1 {{ font-size:24px; margin:0 0 4px; }}
 .sub {{ color:#666; font-size:13px; margin-bottom:16px; }}
 .row {{ display:flex; gap:26px; margin-bottom:26px; }}
 .m {{ position:relative; flex:1; height:{row_h}px; }}
 .bar {{ position:absolute; left:0; right:0; top:{bar_top}px; height:{nlines*17}px;
        display:flex; flex-direction:column; }}
 .bar i {{ flex:1; border-bottom:1.4px solid #2b2b2b; }}
 .bar i:first-child {{ border-top:1.4px solid #2b2b2b; }}
 .mno {{ position:absolute; right:-2px; top:{top_chord}px; font-size:11px; color:#999; }}
 .ch {{ position:absolute; left:78px; top:{top_chord + 4}px; font-size:15px; font-weight:600;
       color:#1a4b8c; }}
 .dg {{ position:absolute; left:0; top:{top_chord}px; }}
 .f {{ position:absolute; transform:translate(-50%,-50%); background:#fff; padding:0 4px;
      font-size:14px; line-height:1; font-variant-numeric:tabular-nums; z-index:2; }}
 .f .u {{ position:absolute; left:50%; transform:translateX(-50%); width:16px; bottom:-7px;
          border-bottom:1.6px solid #333; }}
 .f .u.long {{ width:26px; }}
 .pos {{ position:absolute; transform:translate(-50%,0); z-index:2; }}
 .ar {{ position:absolute; font-size:13px; color:#1a4b8c; }}
 .jp {{ position:absolute; transform:translate(-50%,0); font-size:15px; line-height:1;
       z-index:2; white-space:nowrap; }}
 .jp b {{ font-weight:500; }}
 .jp .oc {{ position:absolute; left:50%; transform:translateX(-50%); width:4px; height:4px;
           border-radius:50%; background:#222; }}
 .jp .oc.up {{ top:-7px; }}
 .jp .oc.dn {{ bottom:9px; }}
 .jp .u {{ position:absolute; left:50%; transform:translateX(-50%); width:15px; bottom:-5px;
          border-bottom:1.5px solid #333; }}
 .jp .u.long {{ width:24px; }}
 .jp .dash {{ position:absolute; left:14px; font-style:normal; color:#333; }}
 .jp .lyr {{ position:absolute; left:50%; transform:translateX(-50%); top:19px;
            font-size:12px; font-style:normal; color:#444; white-space:nowrap; }}
</style></head><body>
 <h1>{html.escape(title)} · {html.escape(label)}（六线谱）</h1>
 <div class="sub">{tempo} BPM · {meter}/4 · 定弦 {tuning_txt} · 共 {measure_count} 小节
 · 数字=品位，↓=下扫 ↑=上扫{'　·　下方简谱为旋律' if melody else ''}</div>
 {''.join(rows_html)}
</body></html>"""

_XML_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 3.1 Partwise//EN" "http://www.musicxml.org/dtds/partwise.dtd">
<score-partwise version="3.1">
  <work><work-title>{title}</work-title></work>
  <identification><encoding><software>score-smith parts</software></encoding></identification>
  <part-list>
    <score-part id="P1">
      <part-name>{name}</part-name>
      <score-instrument id="P1-I1"><instrument-name>{name}</instrument-name></score-instrument>
    </score-part>
  </part-list>
  <part id="P1">
"""

_XML_TAIL = """  </part>
</score-partwise>
"""

_PITCH_STEPS = [("C", 0), ("C", 1), ("D", 0), ("D", 1), ("E", 0), ("F", 0),
                ("F", 1), ("G", 0), ("G", 1), ("A", 0), ("A", 1), ("B", 0)]


def _pitch_xml(midi: int) -> str:
    step, alter = _PITCH_STEPS[midi % 12]
    octave = midi // 12 - 1
    alter_xml = ""
    if alter == 1:
        alter_xml = "<alter>1</alter>"
    elif alter == -1:
        alter_xml = "<alter>-1</alter>"
    return f"<pitch><step>{step}</step>{alter_xml}<octave>{octave}</octave></pitch>"


_FIFTHS_MAJOR = {"C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "F#": 6, "C#": 7,
                 "F": -1, "BB": -2, "EB": -3, "AB": -4, "DB": -5, "GB": -6, "CB": -7}
_FIFTHS_MINOR = {"A": 0, "E": 1, "B": 2, "F#": 3, "C#": 4, "G#": 5, "D#": 6, "A#": 7,
                 "D": -1, "G": -2, "C": -3, "F": -4, "BB": -5, "EB": -6, "AB": -7}


def fifths_for(tonic: str, mode: str) -> int:
    name = (tonic or "C").strip().upper()
    table = _FIFTHS_MINOR if (mode or "major").lower().startswith("min") else _FIFTHS_MAJOR
    return table.get(name, 0)


def _key_xml(tonic: str, mode: str) -> str:
    return f"<key><fifths>{int(fifths_for(tonic, mode))}</fifths></key>"


def _divisions(ql: float) -> int:
    """把四分音符长度换成 divisions=16 下的整数时值。"""
    ticks = int(round(ql * 16))
    return max(1, ticks)


def _onset_clusters(notes: list[dict]) -> list[dict]:
    """按起音聚簇：同一时刻的音合成一个和弦/扫弦。"""
    buckets: dict[int, list[dict]] = {}
    for n in notes:
        key = int(round(float(n["offsetQL"]) * 16))
        buckets.setdefault(key, []).append(n)
    clusters = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda n: n["pitch"])
        clusters.append({"tick": key, "ql": key / 16.0, "notes": group})
    return clusters


def build_tab_xml(model: dict, track: dict) -> str:
    """把一条轨写成 TAB 谱 MusicXML（六线/四线）。"""
    meta = model.get("meta") or {}
    tempo = float(meta.get("tempo") or 100.0)
    meter = int(meta.get("meter") or 4)
    instrument = track.get("instrument") or "guitar"
    strings, names = tuning_for(instrument)
    nlines = len(strings)
    title = html.escape(f"{model.get('title') or ''} · {track.get('label') or ''}")

    clusters = _onset_clusters([n for n in track.get("notes") or [] if not n.get("drum")])
    # 起音簇之间互相截断，谱面才干净（同一弦不能同时两个音）
    for i, cl in enumerate(clusters):
        next_tick = clusters[i + 1]["tick"] if i + 1 < len(clusters) else cl["tick"] + 16
        cl["dur_ticks"] = max(1, min(next_tick - cl["tick"], 64))

    total_ticks = (clusters[-1]["tick"] + clusters[-1]["dur_ticks"]) if clusters else 16 * meter
    measures = max(1, int((total_ticks + 16 * meter - 1) // (16 * meter)))

    out = [_XML_HEAD.format(title=title, name=html.escape(str(track.get("label") or instrument)))]
    by_measure: dict[int, list[dict]] = {}
    for cl in clusters:
        m = int(cl["tick"] // (16 * meter))
        by_measure.setdefault(m, []).append(cl)

    for m in range(measures):
        out.append(f'    <measure number="{m + 1}">')
        if m == 0:
            out.append("      <attributes>")
            out.append("        <divisions>16</divisions>")
            out.append("        " + _key_xml(str(meta.get("keyTonic") or "C"), str(meta.get("keyMode") or "major")))
            out.append(f"        <time><beats>{meter}</beats><beat-type>4</beat-type></time>")
            out.append("        <clef><sign>TAB</sign><line>5</line></clef>")
            out.append(f"        <staff-details><staff-lines>{nlines}</staff-lines>")
            for line, tuning_name in enumerate(names, start=1):
                step = tuning_name[0]
                octave = tuning_name[-1]
                out.append(
                    "          <staff-tuning line=\"%d\"><tuning-step>%s</tuning-step><tuning-octave>%s</tuning-octave></staff-tuning>"
                    % (line, step, octave)
                )
            out.append("        </staff-details>")
            out.append("      </attributes>")
            out.append("      <direction placement=\"above\"><direction-type><metronome><beat-unit>quarter</beat-unit>"
                       f"<per-minute>{int(round(tempo))}</per-minute></metronome></direction-type></direction>")
        cursor = m * 16 * meter
        for cl in by_measure.get(m, []):
            start = cl["tick"] - cursor
            if start > 0:
                gap = min(start, 16 * meter - (cursor - m * 16 * meter))
                if gap > 0:
                    out.append(f"      <forward><duration>{int(gap)}</duration></forward>")
                    cursor += gap
            fingers = assign_fingering([n["pitch"] for n in cl["notes"]], strings)
            placed = [(n, f) for n, f in zip(cl["notes"], fingers) if f]
            if not placed:
                continue
            dur = int(cl["dur_ticks"])
            remaining = 16 * meter - (cursor - m * 16 * meter)
            if remaining <= 0:
                break
            dur = max(1, min(dur, remaining))
            for i, (note, (idx, fret)) in enumerate(placed):
                chord = "<chord/>" if i > 0 else ""
                out.append(
                    "      <note>%s%s<duration>%d</duration><voice>1</voice><type>%s</type>"
                    "<notations><technical><string>%d</string><fret>%d</fret></technical></notations></note>"
                    % (chord, _pitch_xml(int(note["pitch"])), dur, _type_for_ticks(dur, False),
                       idx + 1, fret)
                )
            cursor += dur
        # 补齐小节剩余
        used = cursor - m * 16 * meter
        if used < 16 * meter:
            out.append(f"      <forward><duration>{int(16 * meter - used)}</duration></forward>")
        out.append("    </measure>")
    out.append(_XML_TAIL)
    return "".join(out)


def _type_for_ticks(ticks: int, dotted: bool) -> str:
    table = {16: "quarter", 8: "eighth", 4: "16th", 32: "half", 24: "quarter", 12: "eighth",
             6: "16th", 48: "half", 64: "whole", 2: "32nd", 1: "32nd"}
    return table.get(int(ticks), "quarter")


# ---------------------------------------------------------------- 简谱

_DEGREE_FOR_PC = {0: "1", 2: "2", 4: "3", 5: "4", 7: "5", 9: "6", 11: "7"}
_SCALE_MAJOR = [0, 2, 4, 5, 7, 9, 11]
_SCALE_MINOR = [0, 2, 3, 5, 7, 8, 10]
_SEMI_NAMES = ["1", "#1", "2", "#2", "3", "4", "#4", "5", "#5", "6", "#6", "7"]


def _degree(pitch: int, tonic_pc: int, mode: str = "major") -> tuple[str, int]:
    """返回 (音级记号, 八度偏移)。"""
    rel = (pitch - tonic_pc) % 12
    scale = _SCALE_MINOR if mode == "minor" else _SCALE_MAJOR
    if rel in scale:
        idx = scale.index(rel)
        deg = str(idx + 1)
        octave = (pitch - tonic_pc) // 12
        # 主音落在哪个八度：以 tonic 的首个八度为 0，往上 +1
        tonic_midi = tonic_pc + 12 * ((pitch - tonic_pc) // 12)
        octave = 0
        base = pitch - (pitch - tonic_pc) % 12  # 该八度内主音
        while base + (tonic_pc - (base % 12)) > pitch:
            base -= 12
        octave = (pitch - tonic_midi_baseline(pitch, tonic_pc)) // 12
        return deg, octave
    return _SEMI_NAMES[rel], 0


def tonic_midi_baseline(pitch: int, tonic_pc: int) -> int:
    """比 pitch 低（或等）的、音级等于主音的那个音。"""
    return pitch - ((pitch - tonic_pc) % 12)


def jianpu_notes(track: dict, model: dict) -> list[dict]:
    meta = model.get("meta") or {}
    tonic = str(meta.get("keyTonic") or "C")
    mode = str(meta.get("keyMode") or "major")
    try:
        import theory

        tonic_pc = theory.name_to_pitch_class(tonic)
    except Exception:
        tonic_pc = 0
    meter = int(meta.get("meter") or 4)
    notes = sorted([n for n in track.get("notes") or [] if not n.get("drum")],
                   key=lambda n: (float(n["offsetQL"]), int(n["pitch"])))
    # 单声部：同一时刻只留最高音（简谱是给人唱的）
    merged: list[dict] = []
    for n in notes:
        if merged and abs(float(merged[-1]["offsetQL"]) - float(n["offsetQL"])) < 1e-6:
            if int(n["pitch"]) > int(merged[-1]["pitch"]):
                merged[-1] = n
            continue
        merged.append(n)

    scale = _SCALE_MINOR if mode == "minor" else _SCALE_MAJOR
    out = []
    for n in merged:
        pitch = int(n["pitch"])
        rel = (pitch - tonic_pc) % 12
        base = pitch - rel          # 这个音所在八度的主音
        if rel in scale:
            deg = str(scale.index(rel) + 1)
        else:
            deg = "?"
            for back in range(1, 7):          # 往下找最近的调内音，加 #
                if (rel - back) % 12 in scale:
                    deg = "#" + str(scale.index((rel - back) % 12) + 1)
                    break
            else:
                for fwd in range(1, 7):       # 再找不到就往上找，加 b
                    if (rel + fwd) % 12 in scale:
                        deg = "b" + str(scale.index((rel + fwd) % 12) + 1)
                        break
        out.append({
            "offsetQL": float(n["offsetQL"]),
            "durationQL": float(n["durationQL"]),
            "pitch": pitch,
            "degree": deg,
            "base": base,
            "octave": 0,
            "grace": bool(n.get("grace")),
            "slur_in": bool(n.get("slur_in")),
            "triplet": bool(n.get("triplet")),
            "tgroup": int(n.get("tgroup") or 0),
        })
    # 八度点：以整条轨的中位主音八度为 0，往上记点、往下记点
    if out:
        bases = sorted(item["base"] for item in out)
        ref = bases[len(bases) // 2]
        for item in out:
            item["octave"] = int(round((item["base"] - ref) / 12))
    return out


_JP_CSS = """
 body { margin: 0; padding: 0 0 60px; background: #fff; color: #111;
        font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }
 .wrap { padding: 22px 34px 0; }
 h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: .5px; }
 .sub { color: #666; font-size: 13px; margin-bottom: 14px; }
 .bar { display: flex; align-items: flex-start; padding: 4px 0;
        border-bottom: 1px solid #e6e6e6; }
 .m { position: relative; height: 84px;
      border-left: 1px solid #d6d6d6; margin-left: 2px; }
 .m:first-child { border-left: none; }
 .mno { position: absolute; left: 3px; top: 0; color: #aaa; font-size: 10px; }
 .note { position: absolute; top: 14px; transform: translateX(-50%);
         display: inline-flex; flex-direction: column; align-items: center;
         line-height: 1.1; border-radius: 4px; padding: 0 2px; }
 .up, .dn { font-size: 11px; color: #333; height: 13px; }
 .dg { font-size: 25px; font-weight: 600; position: relative; padding: 0 2px; }
 .u1 { position: absolute; left: 1px; right: 1px; bottom: -3px; border-bottom: 2px solid #111; }
 .u2 { position: absolute; left: 1px; right: 1px; bottom: -7px; border-bottom: 2px solid #111; }
 .u3 { position: absolute; left: 1px; right: 1px; bottom: -11px; border-bottom: 2px solid #111; }
 .dash { font-size: 20px; letter-spacing: 2px; margin-left: 2px; }
 .ly { font-size: 13px; color: #c0392b; min-height: 18px; margin-top: 3px; max-width: 100%; }
 .rest { color: #888; }
 .gap { margin: 6px 2px; padding: 4px 6px; font-size: 13px; color: #888;
        border-left: 3px solid #e2e2e2; }
 .rest .dg { font-size: 21px; font-weight: 400; }
 .note.on { background: #fff3cd; box-shadow: 0 0 0 2px #f6c96b inset; }
 .note.on .dg { color: #b03a2e; }
 #pl { position: sticky; top: 0; z-index: 9; display: flex; flex-wrap: wrap;
       align-items: center; gap: 12px; padding: 10px 34px;
       background: rgba(255,255,255,.96); border-bottom: 1px solid #e2e2e2;
       font-size: 13px; }
 #pl button { font: inherit; padding: 6px 14px; border-radius: 8px; cursor: pointer;
              border: 1px solid #d0d0d0; background: #fafafa; }
 #pl button.primary { background: #1f6f45; border-color: #1f6f45; color: #fff; }
 #pl input[type=range] { width: 96px; vertical-align: middle; }
 #seek { width: 100%; margin: 0; }
 #pos { font-variant-numeric: tabular-nums; color: #444; min-width: 108px; }
 #warn { color: #b03a2e; font-size: 12px; }
 /* --- 简谱谱面加强：谱头（调号/拍号）、段落标记、反复记号、延音线 --- */
 .m { height: 108px; padding-top: 20px; }
 .m.rep-open { border-left: 3px double #111; }
 .m.rep-close::after { content: ""; position: absolute; right: -2px; top: 24px;
                       bottom: 10px; width: 2px; border-left: 1px solid #111;
                       border-right: 1px solid #111; }
 .seclab { position: absolute; left: 4px; top: 0; font-size: 12px; font-weight: 700;
           color: #1f6f45; letter-spacing: .5px; }
 .rep { position: absolute; top: 0; font-size: 13px; font-weight: 700; color: #b8860b; }
 .rep.openr { left: -3px; }
 .rep.closer { right: -3px; }
 .dt { font-size: 22px; font-weight: 700; margin-left: 1px; }
 .keysig { display: flex; align-items: baseline; gap: 22px; margin: 4px 0 6px;
           padding: 6px 2px 4px; border-bottom: 2px solid #111; }
 .keysig .k { font-size: 22px; font-weight: 700; letter-spacing: 1px; }
 .keysig .t { font-size: 22px; font-weight: 700; }
 .keysig .t small { font-size: 13px; font-weight: 400; color: #666; }
 .keysig .tempo { font-size: 14px; color: #444; }
 .bar { position: relative; }
 .sl { position: absolute; left: 0; top: 0; width: 100%; height: 108px; pointer-events: none; }
 /* 倚音（转音的写法）：小一号，斜线划掉时值，占位仍按它自己的起音走 */
 .note.grace { width: 26px; transform: translateX(-88%) translateY(-1px); }
 .note.grace .dg { font-size: 16px; font-weight: 500; color: #555; }
 .note.grace .dg::after { content: ""; position: absolute; left: -2px; right: -2px;
                          top: 52%; border-top: 1.3px solid #666; transform: rotate(-16deg); }
 .note.grace .ly { min-height: 0; }
 #pAudio { display: none; }
"""

_JP_JS = r"""
(function () {
  const NOTES = @@NOTES@@;
  const TEMPO = @@TEMPO@@;
  const TOTAL = @@TOTAL@@;
  const BEAT = @@BEAT@@;
  const secPerQL = 60 / TEMPO;

  const $ = (id) => document.getElementById(id);
  const state = {
    ctx: null, master: null, playing: false, fromQL: 0, t0: 0,
    speed: 1, vol: 0.85, raf: 0, clickOn: false, follow: true
  };

  function unlockAudio() {
    if (state.ctx) return state.ctx;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) { $('warn').textContent = '这个浏览器不支持 Web Audio'; return null; }
    state.ctx = new AC();
    state.master = state.ctx.createGain();
    state.master.gain.value = state.vol;
    state.master.connect(state.ctx.destination);
    return state.ctx;
  }

  function voice(ctx, dest, freq, t, dur, gain) {
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = 'triangle';
    osc.frequency.value = freq;
    const a = Math.max(0.008, Math.min(0.03, dur * 0.25));
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(Math.max(0.02, gain), t + a);
    g.gain.exponentialRampToValueAtTime(0.0001, t + Math.max(a + 0.05, dur * 0.92));
    osc.connect(g); g.connect(dest);
    osc.start(t); osc.stop(t + Math.max(a + 0.08, dur));
    return osc;
  }

  function click(ctx, dest, t) {
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = 'square';
    osc.frequency.value = 1600;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.12, t + 0.004);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.06);
    osc.connect(g); g.connect(dest);
    osc.start(t); osc.stop(t + 0.08);
  }

  function scheduleAll(ctx, dest, fromQL, untilQL, atTime, speed) {
    const sec = secPerQL / speed;
    for (const n of NOTES) {
      if (n.o < fromQL - 1e-6 || n.o >= untilQL) continue;
      const t = atTime + (n.o - fromQL) * sec;
      const dur = Math.max(0.08, n.d * sec);
      voice(ctx, dest, 440 * Math.pow(2, (n.p - 69) / 12), t, dur, 0.20);
    }
    if (state.clickOn) {
      for (let b = Math.ceil(fromQL / BEAT) * BEAT; b < untilQL; b += BEAT) {
        click(ctx, dest, atTime + (b - fromQL) * sec);
      }
    }
  }

  function currentQL() {
    if (!state.playing || !state.ctx) return state.fromQL;
    return state.fromQL + (state.ctx.currentTime - state.t0) * state.speed / secPerQL;
  }

  function stopNodes() {
    // 直接调度出去的音符无法可靠枚举，靠断开主增益来静音
    if (state.master && state.ctx) {
      state.master.gain.cancelScheduledValues(state.ctx.currentTime);
      state.master.gain.setValueAtTime(0.0001, state.ctx.currentTime);
      state.master.gain.setValueAtTime(state.vol, state.ctx.currentTime + 0.05);
    }
  }

  function play(fromQL) {
    const ctx = unlockAudio();
    if (!ctx) return;
    if (ctx.state === 'suspended') ctx.resume();
    state.fromQL = Math.max(0, Math.min(TOTAL - 0.01, fromQL));
    state.t0 = ctx.currentTime + 0.06;
    scheduleAll(ctx, state.master, state.fromQL, TOTAL, state.t0, state.speed);
    state.playing = true;
    $('pPlay').textContent = '⏸ 暂停';
    tick();
  }

  function pause() {
    state.fromQL = currentQL();
    state.playing = false;
    // 停止在途音符：重建主增益节点，旧的自动丢弃
    try {
      state.master.disconnect();
    } catch (e) {}
    if (state.ctx) {
      state.master = state.ctx.createGain();
      state.master.gain.value = state.vol;
      state.master.connect(state.ctx.destination);
    }
    $('pPlay').textContent = '▶ 播放';
    cancelAnimationFrame(state.raf);
  }

  function stopReset() {
    pause();
    state.fromQL = 0;
    paint(0);
    $('seek').value = '0';
    $('pos').textContent = '0:00 / ' + fmt(TOTAL * secPerQL);
  }

  const noteEls = Array.from(document.querySelectorAll('.note[data-o]'))
    .map((el) => ({ el, o: parseFloat(el.dataset.o) }))
    .sort((a, b) => a.o - b.o);

  function paint(ql) {
    let cur = null;
    for (const item of noteEls) {
      if (item.o <= ql + 1e-6) cur = item; else break;
    }
    for (const item of noteEls) item.el.classList.toggle('on', item === cur);
    if (cur && state.follow) {
      const r = cur.el.getBoundingClientRect();
      if (r.top < 120 || r.bottom > window.innerHeight - 60) {
        cur.el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }
  }

  function fmt(sec) {
    sec = Math.max(0, sec);
    const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return m + ':' + String(s).padStart(2, '0');
  }

  function tick() {
    if (!state.playing) return;
    const ql = currentQL();
    paint(ql);
    $('pos').textContent = fmt(ql * secPerQL) + ' / ' + fmt(TOTAL * secPerQL);
    $('seek').value = String(Math.min(1000, Math.round(ql / TOTAL * 1000)));
    if (ql >= TOTAL - 0.02) { stopReset(); return; }
    state.raf = requestAnimationFrame(tick);
  }

  $('pPlay').addEventListener('click', () => { state.playing ? pause() : play(state.fromQL); });
  $('pStop').addEventListener('click', stopReset);
  $('pSpeed').addEventListener('input', (e) => {
    const wasPlaying = state.playing;
    const ql = currentQL();
    state.speed = parseFloat(e.target.value) || 1;
    $('pSpeedV').textContent = state.speed.toFixed(2) + '×';
    if (wasPlaying) { pause(); play(ql); }
  });
  $('pVol').addEventListener('input', (e) => {
    state.vol = parseFloat(e.target.value);
    if (state.master && !state.playing) state.master.gain.value = state.vol;
    else if (state.master) state.master.gain.setTargetAtTime(state.vol, state.ctx.currentTime, 0.02);
  });
  $('pClick').addEventListener('change', (e) => {
    state.clickOn = e.target.checked;
    if (state.playing) { const ql = currentQL(); pause(); play(ql); }
  });
  $('pFollow').addEventListener('change', (e) => { state.follow = e.target.checked; });
  $('seek').addEventListener('change', (e) => {
    const ql = parseFloat(e.target.value) / 1000 * TOTAL;
    const wasPlaying = state.playing;
    pause(); paint(ql);
    if (wasPlaying) play(ql); else { state.fromQL = ql; $('pos').textContent = fmt(ql * secPerQL) + ' / ' + fmt(TOTAL * secPerQL); }
  });
  $('pTest').addEventListener('click', () => {
    const ctx = unlockAudio();
    if (!ctx) return;
    if (ctx.state === 'suspended') ctx.resume();
    const t = ctx.currentTime + 0.05;
    voice(ctx, state.master, 440, t, 0.25, 0.25);
    voice(ctx, state.master, 660, t + 0.22, 0.30, 0.22);
    $('warn').textContent = '刚才那两声是测试音，有声音就说明能放';
    setTimeout(() => { $('warn').textContent = ''; }, 3000);
  });
  document.addEventListener('keydown', (e) => {
    if (e.code === 'Space') { e.preventDefault(); state.playing ? pause() : play(state.fromQL); }
  });

  window.__jpProbe = async function (seconds) {
    const oc = new OfflineAudioContext(2, Math.floor(44100 * seconds), 44100);
    const g = oc.createGain();
    g.gain.value = 1;
    g.connect(oc.destination);
    // 只调度这段窗口里的音：把整首（含 200 秒后的音）都排进去，headless 里会卡住不出结果
    const start = NOTES.length ? NOTES[0].o : 0;
    const untilQL = Math.min(TOTAL, start + seconds / secPerQL + 1);
    const old = state.clickOn;
    state.clickOn = false;
    scheduleAll(oc, g, start, untilQL, 0, 1);
    state.clickOn = old;
    const buf = await oc.startRendering();
    const d = buf.getChannelData(0);
    let peak = 0, sum = 0;
    for (let i = 0; i < d.length; i++) {
      const v = Math.abs(d[i]);
      if (v > peak) peak = v;
      sum += v * v;
    }
    return { peak: peak, rms: Math.sqrt(sum / d.length), notes: NOTES.length };
  };

  paint(0);
  $('pos').textContent = '0:00 / ' + fmt(TOTAL * secPerQL);
})();
"""

_JP_AUDIO_JS = r"""
(function () {
  // 音频模式：真钢琴采样渲染成 play.mp3，<audio> 当时间源——所以进度条天然能拖
  const TEMPO = @@TEMPO@@;
  const TOTAL = @@TOTAL@@;
  const secPerQL = 60 / TEMPO;
  const $ = (id) => document.getElementById(id);
  const A = $('pAudio');
  const state = { speed: 1, vol: 0.85, follow: true, raf: 0 };

  function dur() {
    return (isFinite(A.duration) && A.duration > 0.5) ? A.duration : TOTAL * secPerQL;
  }

  const noteEls = Array.from(document.querySelectorAll('.note[data-o]'))
    .map((el) => ({ el, o: parseFloat(el.dataset.o) }))
    .sort((a, b) => a.o - b.o);

  function fmt(sec) {
    sec = Math.max(0, sec || 0);
    const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return m + ':' + String(s).padStart(2, '0');
  }

  function paint(ql) {
    let cur = null;
    for (const item of noteEls) {
      if (item.o <= ql + 1e-6) cur = item; else break;
    }
    for (const item of noteEls) item.el.classList.toggle('on', item === cur);
    if (cur && state.follow) {
      const r = cur.el.getBoundingClientRect();
      if (r.top < 130 || r.bottom > window.innerHeight - 80) {
        cur.el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }
  }

  function showPos(t) {
    $('pos').textContent = fmt(t) + ' / ' + fmt(dur());
    $('seek').value = String(Math.min(1000, Math.round(t / dur() * 1000)));
  }

  function tick() {
    if (A.paused) return;
    paint(A.currentTime / secPerQL);
    showPos(A.currentTime);
    state.raf = requestAnimationFrame(tick);
  }

  function play() {
    A.playbackRate = state.speed;
    A.volume = state.vol;
    const p = A.play();
    if (p && p.catch) p.catch((e) => { $('warn').textContent = '播放失败：' + e.message; });
    $('pPlay').textContent = '⏸ 暂停';
    tick();
  }

  function pause() {
    A.pause();
    $('pPlay').textContent = '▶ 播放';
    cancelAnimationFrame(state.raf);
  }

  function seekSec(t) {
    t = Math.max(0, Math.min(dur() - 0.05, t));
    try { A.currentTime = t; } catch (e) {}
    paint(t / secPerQL);
    showPos(t);
  }

  $('pPlay').addEventListener('click', () => { A.paused ? play() : pause(); });
  $('pStop').addEventListener('click', () => { pause(); seekSec(0); });

  $('pSpeed').addEventListener('input', (e) => {
    state.speed = parseFloat(e.target.value) || 1;
    A.playbackRate = state.speed;
    $('pSpeedV').textContent = state.speed.toFixed(2) + '×';
  });

  $('pVol').addEventListener('input', (e) => {
    state.vol = parseFloat(e.target.value);
    A.volume = state.vol;
  });

  $('pFollow').addEventListener('change', (e) => { state.follow = e.target.checked; });
  $('pClick').addEventListener('change', () => {
    $('warn').textContent = '音频模式没有节拍器（要节拍器用合成音模式）';
    setTimeout(() => { $('warn').textContent = ''; }, 2600);
  });

  // input + change 都绑：拖到一半也能立刻生效，不会像只绑 change 那样"拖不动"
  const onSeek = (e) => seekSec(parseFloat(e.target.value) / 1000 * dur());
  $('seek').addEventListener('input', onSeek);
  $('seek').addEventListener('change', onSeek);

  $('pTest').addEventListener('click', () => {
    const back = A.currentTime;
    seekSec(0);
    play();
    setTimeout(() => { pause(); seekSec(back); $('warn').textContent = '刚才那段是钢琴音源'; }, 1500);
    setTimeout(() => { $('warn').textContent = ''; }, 4200);
  });

  document.addEventListener('keydown', (e) => {
    if (e.code === 'Space') { e.preventDefault(); A.paused ? play() : pause(); }
  });

  A.addEventListener('ended', () => { pause(); });
  A.addEventListener('loadedmetadata', () => showPos(A.currentTime));

  showPos(0);
  window.__jpProbe = async () => ({ mode: 'audio', duration: dur() });
})();
"""



_DOTTED_BASE = (0.1875, 0.375, 0.75, 1.5, 3.0, 6.0)


def _underline(n: int) -> str:
    """简谱的减时线：八分 1 条、十六分 2 条、三十二分 3 条（每条各自一层，叠起来才是真记法）。"""
    return "".join(f'<span class="u{i}"></span>' for i in range(1, max(0, n) + 1))


def _dotted_underline(dur: float) -> str | None:
    """附点音符：时值是 0.75/1.5/3 这类"半拍+点"，返回它该带的减时线。"""
    for base in _DOTTED_BASE:
        if abs(dur - base) < 0.03:
            if base >= 1.5:
                return ""
            return _underline(1) if base >= 0.5 else _underline(2)
    return None


def _measure_signature(notes_m: list[dict], meter: int) -> tuple:
    return tuple(
        (round(float(n["offsetQL"]) % meter, 3), round(float(n["durationQL"]), 3), int(n["pitch"]))
        for n in sorted(notes_m, key=lambda x: x["offsetQL"])
    )


def _measure_similarity(notes_a: list[dict], notes_b: list[dict]) -> float:
    """两小节像不像：按音高分布（音级直方图）算余弦相似度，不纠结节奏细节。"""
    import numpy as np

    a = np.zeros(12)
    b = np.zeros(12)
    for n in notes_a:
        a[int(n["pitch"]) % 12] += float(n["durationQL"])
    for n in notes_b:
        b[int(n["pitch"]) % 12] += float(n["durationQL"])
    if a.sum() == 0 or b.sum() == 0:
        return 1.0 if a.sum() == b.sum() else 0.0
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def _section_marks(by_measure: dict, measures: int, meter: int, block: int = 4,
                   threshold: float = 0.80) -> tuple:
    """找出重复的 4 小节块，给段落打 A/B 标记并放反复记号。

    以前要「每个音完全一致」才算重复——实录/识别出来的一首歌几乎不可能做到，
    于是永远只标一个 A。改成小节级和声轮廓的相似度（≥0.80 且每小节都不低于 0.55），
    实录的 A-A-B 也能认出来。
    """
    if measures < block:
        return {}, set(), set()
    windows = [m for m in range(0, measures - block + 1, block)
               if any(by_measure.get(t) for t in range(m, m + block))]
    if not windows:
        return {}, set(), set()

    def sim(a: int, b: int) -> float:
        hits = [_measure_similarity(by_measure.get(a + t, []), by_measure.get(b + t, []))
                for t in range(block)]
        return sum(hits) / block if hits else 0.0

    templates: list[tuple[str, int]] = []
    labels: dict[int, str] = {}
    rep_open: set[int] = set()
    rep_close: set[int] = set()
    for s in windows:
        best_sim, best = 0.0, None
        for label, start in templates:
            v = sim(s, start)
            if v > best_sim:
                best_sim, best = v, (label, start)
        if best is not None and best_sim >= threshold:
            labels[s] = best[0]
            if best[1] not in rep_open and best[1] + block <= measures:
                rep_open.add(best[1])
                rep_close.add(best[1] + block - 1)
        else:
            label = chr(ord("A") + len(templates)) if len(templates) < 8 else "?"
            labels[s] = label
            templates.append((label, s))
    return labels, rep_open, rep_close


def _tie_pairs(notes_sorted: list[dict], tol: float = 0.08, meter: int = 4) -> list[tuple]:
    """延音线：同一音高、后一个音紧接在前一个音结束处。

    只有「真的延续同一个音」才画线——要么跨小节线，要么前一个音本身够长（≥1.5 拍）；
    否则同音反复（一句一个音节）会被误画成延音线。
    """
    pairs = []
    for a, b in zip(notes_sorted, notes_sorted[1:]):
        if int(a["pitch"]) != int(b["pitch"]):
            continue
        gap = float(b["offsetQL"]) - (float(a["offsetQL"]) + float(a["durationQL"]))
        if not (-1e-6 <= gap <= tol):
            continue
        cross_bar = int(float(b["offsetQL"]) // meter) != int(float(a["offsetQL"]) // meter)
        if cross_bar or float(a["durationQL"]) >= 1.5:
            pairs.append((a, b))
    return pairs


def jianpu_html(model: dict, track: dict, lyrics: list[str] | None = None,
                measures_per_row: int = 2, player: bool = True,
                audio: str | None = None) -> str:
    """简易数字谱：HTML 排版（下面交给无头浏览器截图）。"""
    meta = model.get("meta") or {}
    meter = int(meta.get("meter") or 4)
    tempo = float(meta.get("tempo") or 100)
    title = str(model.get("title") or "")
    label = str(track.get("label") or "人声")
    notes = jianpu_notes(track, model)
    total_ql = max((n["offsetQL"] + n["durationQL"] for n in notes), default=meter)
    measures = max(1, int((total_ql + meter - 1) // meter))

    # 歌词：按需切成每个音一个音节（中文逐字，英文按空白）
    sylls: list[str] = []
    if lyrics:
        for line in lyrics:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) <= 1:
                parts = [c for c in line if not c.isspace()]
            sylls.extend(parts)

    by_measure: dict[int, list[dict]] = {}
    for n in notes:
        m = int(n["offsetQL"] // meter)
        by_measure.setdefault(m, []).append(n)

    labels, rep_open, rep_close = _section_marks(by_measure, measures, meter)
    notes_sorted = sorted(notes, key=lambda x: x["offsetQL"])
    ties = _tie_pairs(notes_sorted, meter=meter)
    # 连音线（圆滑线）：不同音高之间的 slur。_tie_pairs 只管同音高的延音线，
    # 这里把识别阶段标出的 slur_in 接上「前一个音」，不同音高就画一条弧。
    slurs = []
    for prev, cur in zip(notes_sorted, notes_sorted[1:]):
        if cur.get("slur_in") and int(cur["pitch"]) != int(prev["pitch"]):
            slurs.append((prev, cur))
    slur_keys = {(round(float(a["offsetQL"]), 4), round(float(b["offsetQL"]), 4))
                 for a, b in slurs}
    tie_pairs = [pair for pair in ties
                 if (round(float(pair[0]["offsetQL"]), 4), round(float(pair[1]["offsetQL"]), 4))
                 not in slur_keys]

    gap = 14.0
    rows = []
    syl_idx = 0
    mpr = max(1, int(measures_per_row))
    mw = (1300.0 - gap * mpr) / mpr                       # 每小节多宽

    def note_x(n: dict, row_start: int) -> float:
        m = int(float(n["offsetQL"]) // meter)
        frac = (float(n["offsetQL"]) - m * meter) / meter
        return (m - row_start) * (mw + gap) + frac * mw

    gap_from = None
    gap_to = None
    for start in range(0, measures, mpr):
        row_end = min(start + mpr, measures)
        row_empty = not any(by_measure.get(m) for m in range(start, row_end))
        if row_empty and start >= mpr and row_end <= measures - mpr:
            if gap_from is None:
                gap_from = start
            gap_to = row_end
            continue
        if gap_from is not None:
            rows.append(f'<div class="gap">第 {gap_from + 1}–{gap_to} 小节：休止</div>')
            gap_from = None
        cells_per_measure = []
        row_notes = [n for n in notes_sorted
                     if start * meter <= float(n["offsetQL"]) < (start + mpr) * meter]
        for m in range(start, min(start + mpr, measures)):
            notes_m = sorted(by_measure.get(m, []), key=lambda x: x["offsetQL"])
            cells = []
            # 每一拍都落位：有音符就画音符，没音符补一个 0
            # 「有音符」看的是这一拍是否被音符盖住，而不是只看起音点——
            # 否则跨拍的长音下面会再叠一个 0 休止。
            for beat in range(meter):
                beat_start = m * meter + beat
                busy = any(
                    float(n["offsetQL"]) < beat_start + 1 - 1e-6
                    and float(n["offsetQL"]) + float(n["durationQL"]) > beat_start + 0.35
                    for n in notes_m
                )
                if busy:
                    continue
                x = beat / meter * 100.0
                cells.append(
                    f'<span class="note rest" style="left:{x:.2f}%">'
                    f'<span class="up"></span><span class="dg">0</span>'
                    f'<span class="dn"></span><span class="ly"></span></span>'
                )
            for n in notes_m:
                x = (n["offsetQL"] - m * meter) / meter * 100.0
                dur = n["durationQL"]
                octv = n["octave"]
                is_grace = bool(n.get("grace"))
                dots_up = "·" * max(0, min(2, octv))
                dots_dn = "·" * max(0, min(2, -octv))
                deg = html.escape(n["degree"])
                dotted_cls = None if is_grace else _dotted_underline(float(dur))
                under = ""
                dot = ""
                if dotted_cls is not None:
                    dot = '<span class="dt">·</span>'
                    under = dotted_cls
                elif is_grace:
                    under = ""
                elif dur < 0.25:
                    under = _underline(3)
                elif dur < 0.5:
                    under = _underline(2)
                elif dur < 1:
                    under = _underline(1)
                elif dur >= 2:
                    under = '<span class="dash">' + "－" * (int(round(dur)) - 1) + "</span>"
                lyric = ""
                if sylls and not is_grace:
                    lyric = sylls[syl_idx] if syl_idx < len(sylls) else ""
                    syl_idx += 1
                cls_note = "note grace" if is_grace else "note"
                cells.append(
                    f'<span class="{cls_note}" data-o="{n["offsetQL"]:.4f}" data-d="{dur:.4f}" '
                    f'data-p="{int(n["pitch"])}"'
                    + (' data-g="1"' if is_grace else '')
                    + f' style="left:{x:.2f}%">'
                    f'<span class="up">{dots_up}</span>'
                    f'<span class="dg">{deg}{dot}{under}</span>'
                    f'<span class="dn">{dots_dn}</span>'
                    f'<span class="ly">{html.escape(lyric)}</span>'
                    f"</span>"
                )
            cls = "m"
            if m in rep_open:
                cls += " rep-open"
            if m in rep_close:
                cls += " rep-close"
            marks = ""
            if m in labels:
                marks += f'<span class="seclab">{html.escape(labels[m])}</span>'
            if m in rep_open:
                marks += '<span class="rep openr">‖:</span>'
            elif m in rep_close:
                marks += '<span class="rep closer">:‖</span>'
            cells_per_measure.append(
                f'<span class="{cls}" style="width:{mw:.0f}px">'
                f'<span class="mno">{m + 1}</span>{marks}'
                f'{"".join(cells)}</span>'
            )

        # 延音线 / 连音线：整行一个 SVG 覆盖层
        paths = []
        for a, b in tie_pairs:
            if not (start * meter <= float(a["offsetQL"]) < (start + mpr) * meter):
                continue
            if not (start * meter <= float(b["offsetQL"]) < (start + mpr) * meter):
                continue
            x1 = note_x(a, start)
            x2 = note_x(b, start)
            if x2 - x1 < 4:
                x2 = x1 + 12
            cx = (x1 + x2) / 2.0
            top = 30.0
            lift = min(26.0, 8.0 + (x2 - x1) * 0.18)
            paths.append(
                f'<path d="M{x1:.1f},{top:.1f} Q{cx:.1f},{top - lift:.1f} {x2:.1f},{top:.1f}" '
                f'fill="none" stroke="#333" stroke-width="1.6"/>'
            )
        # 连音线（slur）：弧线比延音线浅一点、细一点，一眼能分出来
        for a, b in slurs:
            if not (start * meter <= float(a["offsetQL"]) < (start + mpr) * meter):
                continue
            if not (start * meter <= float(b["offsetQL"]) < (start + mpr) * meter):
                continue
            x1 = note_x(a, start)
            x2 = note_x(b, start)
            if x2 - x1 < 4:
                x2 = x1 + 12
            cx = (x1 + x2) / 2.0
            top = 40.0
            lift = min(20.0, 6.0 + (x2 - x1) * 0.12)
            paths.append(
                f'<path d="M{x1:.1f},{top:.1f} Q{cx:.1f},{top - lift:.1f} {x2:.1f},{top:.1f}" '
                f'fill="none" stroke="#555" stroke-width="1.2" stroke-linecap="round"/>'
            )
        # 三连音：三个音上方画方括号 + 「3」（简谱惯例）
        trip: dict[int, list[dict]] = {}
        for n in notes_sorted:
            if n.get("triplet") and n.get("tgroup"):
                trip.setdefault(int(n["tgroup"]), []).append(n)
        for group_notes in trip.values():
            group_notes.sort(key=lambda x: float(x["offsetQL"]))
            inside = [n for n in group_notes
                      if start * meter <= float(n["offsetQL"]) < (start + mpr) * meter]
            if len(inside) < 2:
                continue
            x1 = note_x(inside[0], start)
            x2 = note_x(inside[-1], start)
            if x2 - x1 < 10:
                x2 = x1 + 16
            top = 13.0
            paths.append(
                f'<path d="M{x1:.1f},{top + 7:.1f} L{x1:.1f},{top:.1f} '
                f'L{x2:.1f},{top:.1f} L{x2:.1f},{top + 7:.1f}" '
                f'fill="none" stroke="#666" stroke-width="1"/>'
            )
            paths.append(
                f'<text x="{(x1 + x2) / 2:.1f}" y="{top - 2:.1f}" font-size="12" '
                f'fill="#666" text-anchor="middle">3</text>'
            )
        svg = ""
        if paths:
            svg = ('<svg class="sl" viewBox="0 0 1300 108" preserveAspectRatio="none">'
                   + "".join(paths) + "</svg>")
        rows.append(f'<div class="bar">{svg}{"".join(cells_per_measure)}</div>')
    if gap_from is not None:
        rows.append(f'<div class=gap>第 {gap_from + 1}–{gap_to} 小节：休止</div>')

    key_txt = f"{meta.get('keyTonic') or 'C'} {'大调' if (meta.get('keyMode') or 'major') == 'major' else '小调'}"
    meter_text = {2: '二拍子', 3: '三拍子', 4: '四拍子', 6: '六拍子'}.get(meter, '')
    total_ql_all = max(float(total_ql), float(meter))
    toolbar = ""
    script = ""
    if player:
        toolbar = (
            '<div id="pl">'
            '<button id="pPlay" class="primary">▶ 播放</button>'
            '<button id="pStop">⏹ 停止</button>'
            '<button id="pTest">🔊 试音</button>'
            '<span id="pos">0:00 / 0:00</span>'
            '<span>速度 <input id="pSpeed" type="range" min="0.5" max="1.5" '
            'step="0.05" value="1"> <b id="pSpeedV">1.00×</b></span>'
            '<span>音量 <input id="pVol" type="range" min="0" max="1" '
            'step="0.05" value="0.85"></span>'
            '<label><input id="pClick" type="checkbox"> 节拍器</label>'
            '<label><input id="pFollow" type="checkbox" checked> 跟着滚</label>'
            '<span id="warn"></span>'
            '<input id="seek" type="range" min="0" max="1000" value="0">'
            '</div>'
        )
        payload = [
            {"o": round(float(n["offsetQL"]), 4),
             "d": round(float(n["durationQL"]), 4),
             "p": int(n["pitch"])}
            for n in sorted(notes, key=lambda x: x["offsetQL"])
        ]
        if audio:
            js = (_JP_AUDIO_JS.replace("@@TEMPO@@", str(float(tempo)))
                             .replace("@@TOTAL@@", str(round(total_ql_all, 4))))
            toolbar = toolbar.replace(
                '<span id="warn"></span>',
                '<span id="warn"></span>'
                f'<audio id="pAudio" src="{html.escape(audio)}" preload="auto"></audio>',
                1,
            )
        else:
            js = (_JP_JS.replace("@@NOTES@@", json.dumps(payload))
                        .replace("@@TEMPO@@", str(float(tempo)))
                        .replace("@@TOTAL@@", str(round(total_ql_all, 4)))
                        .replace("@@BEAT@@", "1"))
        script = "<script>" + js + "</script>"

    lyric_note = ('· 歌词 ' + str(len(sylls)) + ' 字') if sylls else '· 无歌词（可用 --lyrics 传歌词文件）'
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{html.escape(title)} · {html.escape(label)} 简谱</title>
<style>{_JP_CSS}</style></head><body>
{toolbar}
<div class="wrap">
 <h1>{html.escape(title)} · {html.escape(label)}（简谱）</h1>
 <div class="sub">共 {measures} 小节 · {lyric_note}</div>
  <div class="keysig"><span class="k">1 = {html.escape(str(meta.get('keyTonic') or 'C'))}</span>
   <span class="t">{meter}/4<small>（{meter_text}）</small></span>
   <span class="tempo">♩ = {int(round(tempo))}</span></div>
 {''.join(rows)}
</div>
{script}
</body></html>"""


# ---------------------------------------------------------------- 五线谱（单轨）

def staff_musicxml(model: dict, track: dict, out_path: str) -> str:
    from build_score import build_m21

    sub = {
        "version": 1,
        "title": f"{model.get('title') or ''} · {track.get('label') or ''}",
        "meta": dict(model.get("meta") or {}),
        "tracks": [dict(track)],
        "chords": {},
    }
    score = build_m21(sub)
    # 单乐器分谱里不需要「钢琴（右手）」这种长名字，页边放不下会被截成省略号
    short = str(track.get("label") or track.get("id") or "")
    for part in getattr(score, "parts", []):
        try:
            part.partName = short
            part.partAbbreviation = short[:4]
        except Exception:
            pass
    written = score.write("musicxml", fp=out_path)
    return written or out_path


# ---------------------------------------------------------------- 出谱总调度

def _slug(text: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(text or "part")).strip("-")
    return text or "part"


def _crop_white(path: str, pad: int = 18) -> None:
    try:
        from PIL import Image
    except Exception:
        return
    try:
        im = Image.open(path).convert("RGB")
        w, h = im.size
        px = im.load()
        step = max(1, w // 400)
        top, bottom = 0, h - 1
        for y in range(h):
            row_white = True
            for x in range(0, w, step):
                r, g, b = px[x, y]
                if r < 248 or g < 248 or b < 248:
                    row_white = False
                    break
            if not row_white:
                top = max(0, y - pad)
                break
        for y in range(h - 1, -1, -1):
            row_white = True
            for x in range(0, w, step):
                r, g, b = px[x, y]
                if r < 248 or g < 248 or b < 248:
                    row_white = False
                    break
            if not row_white:
                bottom = min(h - 1, y + pad)
                break
        if bottom - top > 40 and (top > 0 or bottom < h - 1):
            im.crop((0, top, w, bottom + 1)).save(path)
    except Exception:
        return


_FFMPEG = r"D:\dsh 大肥鱼\voice\bin\ffmpeg.exe"


def _write_share_html(html_path: str, audio_path: str, out_path: str) -> str:
    """把播放页做成「单文件」：音频 base64 内联进去，发给别人双击就能放，不需要服务器。"""
    import base64
    import mimetypes

    page = open(html_path, encoding="utf-8").read()
    mime = mimetypes.guess_type(audio_path)[0] or "audio/mpeg"
    data = base64.b64encode(open(audio_path, "rb").read()).decode("ascii")
    name = os.path.basename(audio_path)
    if f'src="{name}"' not in page:
        return out_path
    page = page.replace(f'src="{name}"', f'src="data:{mime};base64,{data}"')
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out_path


def _render_play_audio(model: dict, track: dict, out_dir: str, name: str = "play") -> str | None:
    """把这条轨的音符用 gm.dls 的钢琴采样渲染成音频（有 ffmpeg 就压成 mp3）。"""
    import shutil
    import subprocess

    import piano

    notes = track.get("notes") or []
    if not notes:
        return None
    tempo = float((model.get("meta") or {}).get("tempo") or 120.0)
    pcm = piano.render_notes(notes, tempo, gain=0.32)
    wav_path = os.path.join(out_dir, name + ".wav")
    piano.write_audio(pcm, wav_path)
    ff = _FFMPEG if os.path.exists(_FFMPEG) else shutil.which("ffmpeg")
    if not ff:
        return name + ".wav"
    mp3_path = os.path.join(out_dir, name + ".mp3")
    try:
        subprocess.run(
            [ff, "-y", "-loglevel", "error", "-i", wav_path,
             "-codec:a", "libmp3lame", "-b:a", "128k", mp3_path],
            check=True, capture_output=True,
        )
        os.remove(wav_path)
        return name + ".mp3"
    except Exception:
        return name + ".wav"


def kind_for(track: dict) -> str:
    inst = str(track.get("instrument") or "").lower()
    if inst == "vocals":
        return "jianpu"
    if fretted_instrument(inst):
        return "tab"
    return "staff"


def render_parts(model: dict, project_dir: str, lyrics: list[str] | None = None,
                 only: list[str] | None = None, browser: str | None = None) -> dict:
    """按乐器分别出谱，一个乐器一个目录。"""
    import render as rnd

    parts_root = os.path.join(project_dir, "parts")
    # parts 是派生文件，每次重新出，别留旧乐器的残骸
    if os.path.isdir(parts_root):
        import shutil

        shutil.rmtree(parts_root, ignore_errors=True)
    os.makedirs(parts_root, exist_ok=True)
    meta = model.get("meta") or {}
    tempo = int(round(float(meta.get("tempo") or 100)))
    meter = int(meta.get("meter") or 4)
    key = f"{meta.get('keyTonic') or 'C'} {meta.get('keyMode') or 'major'}"

    entries = []
    idx = 0
    # 弹唱/指弹谱要配一条旋律：优先人声，其次音域最高的非鼓轨
    melody_track = None
    for track in model.get("tracks") or []:
        if str(track.get("instrument") or "") == "vocals":
            melody_track = track
            break
    if melody_track is None:
        best_med = -1.0
        for track in model.get("tracks") or []:
            if str(track.get("instrument") or "") == "drums":
                continue
            pitches = sorted(int(n["pitch"]) for n in track.get("notes") or [] if not n.get("drum"))
            if len(pitches) < 3:
                continue
            med = pitches[len(pitches) // 2]
            if med > best_med:
                best_med, melody_track = med, track
    for track in model.get("tracks") or []:
        tid = str(track.get("id") or "")
        if only and tid not in only:
            continue
        notes = [n for n in track.get("notes") or [] if not n.get("drum")]
        if track.get("instrument") != "drums" and len(notes) < 3:
            continue
        if track.get("instrument") == "drums" and not track.get("notes"):
            continue
        idx += 1
        kind = kind_for(track)
        label = str(track.get("label") or tid)
        dname = f"{idx:02d}-{_slug(tid)}"
        pdir = os.path.join(parts_root, dname)
        os.makedirs(pdir, exist_ok=True)
        subtitle = f"{tempo} BPM · {meter}/4 · {key}"

        entry = {"trackId": tid, "label": label, "kind": kind, "dir": pdir, "files": {}}
        if kind == "jianpu":
            # 真钢琴音源：用 gm.dls 的采样渲染成 play.mp3，播放器拿 <audio> 当时间源
            audio_name = None
            try:
                audio_name = _render_play_audio(model, track, pdir)
            except Exception as exc:  # 出不了音频就退回合成音，别让整份谱挂掉
                print(f"[parts] 音频渲染失败，退回合成音：{exc}")
            page_html = jianpu_html(model, track, lyrics, audio=audio_name)
            html_path = os.path.join(pdir, "jianpu.html")
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(page_html)
            rows = page_html.count('class="bar"')
            height = 210 + rows * 62
            png_path = os.path.join(pdir, "jianpu.png")
            ok = rnd.html_to_png(page_html, png_path, width=1600, height=min(30000, height),
                                 browser=browser, timeout=180)
            if ok:
                _crop_white(png_path)
            entry["files"] = {"png": png_path, "html": html_path}
            if audio_name:
                entry["files"]["audio"] = os.path.join(pdir, audio_name)
                # 分享用单文件版：音频用 base64 内联，单一个 .html 发出去就能放
                try:
                    share_path = os.path.join(pdir, "jianpu-share.html")
                    _write_share_html(html_path, os.path.join(pdir, audio_name), share_path)
                    entry["files"]["share"] = share_path
                except Exception as exc:  # noqa: BLE001
                    print(f"[parts] 分享单文件生成失败：{exc}")
        elif kind == "tab":
            mel = melody_track if (melody_track and melody_track.get("id") != tid) else None
            page_html = tab_html(model, track, melody=mel, lyrics=lyrics)
            html_path = os.path.join(pdir, "tab.html")
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(page_html)
            rows = page_html.count('class="row"')
            height = 150 + rows * (280 if mel else 150)
            png_path = os.path.join(pdir, "tab.png")
            ok = rnd.html_to_png(page_html, png_path, width=1600, height=min(30000, height),
                                 browser=browser, timeout=180)
            if ok:
                _crop_white(png_path)
            xml_path = os.path.join(pdir, f"{_slug(tid)}-tab.musicxml")
            try:
                with open(xml_path, "w", encoding="utf-8") as fh:
                    fh.write(build_tab_xml(model, track))
            except Exception:
                xml_path = None
            entry["files"] = {"png": png_path, "html": html_path, "musicxml": xml_path}
        else:
            xml_path = os.path.join(pdir, f"{_slug(tid)}.musicxml")
            staff_musicxml(model, track, xml_path)
            svgs = rnd.musicxml_to_svgs(xml_path)
            page_html, pw, ph = rnd.build_html(svgs, f"{model.get('title') or ''} · {label}", subtitle)
            html_path = os.path.join(pdir, "part.html")
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(page_html)
            png_path = os.path.join(pdir, "part.png")
            ok = rnd.html_to_png(page_html, png_path, width=int(pw), height=min(30000, int(ph) + 40),
                                 browser=browser, timeout=180)
            if ok:
                _crop_white(png_path)
            entry["files"] = {"png": png_path, "html": html_path, "musicxml": xml_path,
                              "pages": len(svgs)}
        entries.append(entry)

    index_html = _parts_index(model, entries)
    index_path = os.path.join(parts_root, "index.html")
    with open(index_path, "w", encoding="utf-8") as fh:
        fh.write(index_html)
    return {"dir": parts_root, "index": index_path, "parts": entries}


def _parts_index(model: dict, entries: list[dict]) -> str:
    meta = model.get("meta") or {}
    cards = []
    for e in entries:
        png = e["files"].get("png")
        rel = os.path.basename(e["dir"]) + "/" + os.path.basename(png) if png else ""
        kind_name = {"jianpu": "简谱", "tab": "六线谱", "staff": "五线谱"}[e["kind"]]
        html_file = e["files"].get("html")
        play_link = ""
        if html_file:
            play_rel = os.path.basename(e["dir"]) + "/" + os.path.basename(html_file)
            play_link = (f' <a class="play" href="{play_rel}">'
                         f'{e["label"]} · 可播放版</a>')
        cards.append(
            f'<section><h2>{html.escape(e["label"])} <small>{kind_name}</small>{play_link}</h2>'
            f'<img src="{rel}" alt="{html.escape(e["label"])}">'
            f'<p class="meta">{html.escape(os.path.basename(e["dir"]))}</p></section>'
        )
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{html.escape(str(model.get('title') or ''))} · 演奏谱</title>
<style>
body{{margin:0;padding:28px 34px;background:#faf9f7;color:#17161a;
     font-family:"Segoe UI","Microsoft YaHei",sans-serif}}
h1{{font-size:26px;margin:0 0 6px}} .sub{{color:#6b6873;font-size:13px;margin-bottom:22px}}
section{{background:#fff;border:1px solid #e6e3dc;border-radius:14px;padding:18px 20px;margin-bottom:22px;
        box-shadow:0 6px 18px rgba(40,30,20,.05)}}
h2{{font-size:17px;margin:0 0 12px}} h2 small{{color:#a08b6a;font-weight:600;font-size:12px;
   border:1px solid #e3d6bd;border-radius:20px;padding:1px 8px;margin-left:6px}}
img{{width:100%;height:auto;border:1px solid #f0eee8;border-radius:8px;background:#fff}}
.play{{font-size:12px;color:#1f6f45;border:1px solid #bcd9c8;background:#eef7f1;
       border-radius:20px;padding:2px 10px;margin-left:8px;text-decoration:none}}
.meta{{color:#9b98a3;font-size:12px;margin:8px 0 0}}
</style></head><body>
<h1>{html.escape(str(model.get('title') or ''))} · 分乐器演奏谱</h1>
<div class="sub">{int(round(float(meta.get('tempo') or 100)))} BPM · {meta.get('meter') or 4}/4 ·
 {html.escape(str(meta.get('keyTonic') or 'C'))} {html.escape(str(meta.get('keyMode') or 'major'))} ·
 共 {len(entries)} 份谱</div>
{''.join(cards)}
</body></html>"""
