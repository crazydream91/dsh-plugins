# -*- coding: utf-8 -*-
"""结构化曲谱模型 ↔ music21 ↔ MusicXML / MIDI。

score.json（我们的可编辑真身）结构：

{
  "version": 1,
  "title": "鲜花",
  "meta": {"tempo": 92, "meter": 4, "keyTonic": "G", "keyMode": "major",
           "durationSec": 231.4, "createdAt": "...", "source": "xxx.mp3"},
  "tracks": [
    {"id": "vocals", "instrument": "vocals", "label": "人声", "prominent": true,
     "chords": {"0": "G", "1": "Em"},
     "notes": [{"offsetQL": 0, "durationQL": 1.0, "pitch": 62, "velocity": 0.8}]}
  ]
}

改这个 JSON 就等于改谱子：移调、删音、改时值、加轨都在这里做，
再由 build_model() 重新生成 MusicXML / MIDI / 预览图。
"""

from __future__ import annotations

import json
import os
import time

from theory import DRUM_MAP, instrument_spec, key_signature_name, midi_to_name

CLEF_FACTORY = {}


def _clef(name: str):
    from music21 import clef as m21clef

    table = {
        "treble": m21clef.TrebleClef,
        "treble8vb": m21clef.Treble8vbClef,
        "bass": m21clef.BassClef,
        "percussion": m21clef.PercussionClef,
        "grand": m21clef.TrebleClef,
    }
    return table.get(name, m21clef.TrebleClef)()


def _key(tonic: str, mode: str):
    from music21 import key as m21key

    try:
        return m21key.Key(key_signature_name(tonic, mode))
    except Exception:
        return m21key.Key("C")


def _instrument(name: str):
    from music21 import instrument as m21inst

    table = {
        "vocals": m21inst.Vocalist,
        "guitar": m21inst.AcousticGuitar,
        "piano": m21inst.Piano,
        "bass": m21inst.ElectricBass,
        "sax": m21inst.AltoSaxophone,
        "drums": m21inst.Percussion,
        "other": m21inst.Instrument,
    }
    return table.get(name, m21inst.Instrument)()


def _unpitched(drum: str, quarter_length: float):
    from music21 import note as m21note

    spec = DRUM_MAP.get(drum) or DRUM_MAP["snare"]
    n = m21note.Unpitched()
    n.displayStep = spec["step"]
    n.displayOctave = spec["octave"]
    n.notehead = spec["notehead"]
    n.duration.quarterLength = quarter_length
    return n


def _safe_quarter_length(quarter_length: float) -> float:
    """把识别出来的奇怪时值（0.5833 这类）吸附到 music21 能导出的时值上。

    三连音的 1/3、2/3 保留（导出时挂 tuplet 记号），其余按 1/16 吸附。
    """
    third = 1.0 / 3.0
    for cand in (third, 2 * third):
        if abs(quarter_length - cand) < 0.03:
            return cand
    return max(0.0625, round(float(quarter_length) / 0.0625) * 0.0625)


def _pitched(pitch: int, quarter_length: float, velocity: float):
    from music21 import duration
    from music21 import note as m21note

    quarter_length = _safe_quarter_length(quarter_length)
    n = m21note.Note(int(pitch))
    # 三连音：music21 不接受「1/3 拍」这种无法用音符类型表达的时值，
    # 必须写成普通八分/四分音符再挂 3:2 连音记号，导出器才会生成 tuplet。
    if abs(quarter_length - 1.0 / 3.0) < 0.02:
        n.duration = duration.Duration(type="eighth")
        n.duration.appendTuplet(duration.Tuplet(3, 2))
    elif abs(quarter_length - 2.0 / 3.0) < 0.02:
        n.duration = duration.Duration(type="quarter")
        n.duration.appendTuplet(duration.Tuplet(3, 2))
    else:
        n.duration.quarterLength = quarter_length
    n.volume.velocity = int(max(1, min(127, round(velocity * 127))))
    return n


def _mxml_round(value: float, grid: float = 0.0625) -> float:
    """MusicXML 里不能出现「没有音符类型能表示」的时值/位置（如 2.2 拍、1/3 拍），
    统一吸附到 1/16 网格；三连音在简谱和 score.json 里仍然保留 1/3。"""
    return max(grid, round(float(value) / grid) * grid)


def _group_chords(notes: list[dict]) -> list[dict]:
    """把同一起点的多个音合成和弦事件。"""
    from collections import OrderedDict

    buckets: "OrderedDict[float, list[dict]]" = OrderedDict()
    for note in sorted(notes, key=lambda n: (n["offsetQL"], n["pitch"])):
        key = round(float(note["offsetQL"]), 6)
        buckets.setdefault(key, []).append(note)
    return [{"offsetQL": offset, "notes": group} for offset, group in buckets.items()]


def _fill_measures(part, events, meter: int, drums: bool = False):
    """把事件塞进 part，并把整小节的空拍补成休止符。"""
    from music21 import chord as m21chord

    for event in events:
        group = event["notes"]
        if drums:
            for note in group:
                el = _unpitched(note.get("drum") or "snare", max(0.0625, note["durationQL"]))
                part.insert(event["offsetQL"], el)
            continue
        if len(group) == 1:
            note = group[0]
            el = _pitched(note["pitch"], max(0.0625, note["durationQL"]), note.get("velocity", 0.7))
        else:
            el = m21chord.Chord(
                [int(n["pitch"]) for n in group],
                quarterLength=_safe_quarter_length(max(0.0625, max(n["durationQL"] for n in group))),
            )
            el.volume.velocity = int(max(1, min(127, round(max(n.get("velocity", 0.7) for n in group) * 127))))
        part.insert(event["offsetQL"], el)


def build_m21(model: dict):
    """score.json → music21 Score。"""
    from music21 import layout, metadata, stream
    from music21 import meter as m21meter
    from music21 import tempo as m21tempo

    meta = model.get("meta", {})
    meter_count = int(meta.get("meter") or 4)
    tempo_bpm = float(meta.get("tempo") or 100)
    tonic = meta.get("keyTonic") or "C"
    mode = meta.get("keyMode") or "major"

    score = stream.Score()
    md = metadata.Metadata()
    md.title = model.get("title") or "未命名曲谱"
    md.composer = "制谱师 · DSH"
    score.insert(0, md)

    staff_groups = []
    tracks = model.get("tracks", [])
    # 全曲和弦只挂一次（挂在第一行谱上），否则每行谱都印一遍
    song_chords = model.get("chords") or {}
    if not song_chords:
        song_chords = next(
            (t.get("chords") for t in tracks
             if t.get("chords") and (t.get("kind") or "") != "drums"),
            {},
        )
    chords_placed = False
    for index, track in enumerate(tracks):
        instrument = track.get("instrument") or "other"
        spec = instrument_spec(instrument)
        notes = track.get("notes", [])
        if not notes:
            continue
        is_drums = spec["kind"] == "drums"
        give_chords = False if chords_placed else bool(song_chords) and not is_drums

        # 钢琴拆成高音/低音两个谱表
        if spec["clef"] == "grand" and not is_drums:
            upper = [n for n in notes if int(n["pitch"]) >= 60]
            lower = [n for n in notes if int(n["pitch"]) < 60]
            pair = []
            for suffix, subset, clef_name in (("右手", upper, "treble"), ("左手", lower, "bass")):
                if not subset:
                    continue
                part = _make_part(track, instrument, spec, clef_name, subset, meter_count,
                                  tempo_bpm, tonic, mode, first=(index == 0 and not pair),
                                  label_suffix=suffix, drums=False,
                                  chords=song_chords if (give_chords and not pair) else None)
                pair.append(part)
                score.insert(0, part)
            if give_chords and pair:
                chords_placed = True
            if len(pair) == 2:
                staff_groups.append(layout.StaffGroup(pair, symbol="brace", barTogether=True))
            continue

        part = _make_part(track, instrument, spec, spec["clef"], notes, meter_count,
                          tempo_bpm, tonic, mode, first=(index == 0), drums=is_drums,
                          chords=song_chords if give_chords else None)
        if give_chords:
            chords_placed = True
        score.insert(0, part)

    for group in staff_groups:
        score.insert(0, group)
    return score


def _make_part(track, instrument, spec, clef_name, notes, meter_count, tempo_bpm,
               tonic, mode, first=False, label_suffix="", drums=False, chords=None):
    from music21 import dynamics, stream
    from music21 import meter as m21meter
    from music21 import tempo as m21tempo
    from music21 import harmony

    part = stream.Part()
    label = track.get("label") or spec["label"]
    if label_suffix:
        label = f"{label}（{label_suffix}）"
    part.partName = label
    part.insert(0, _instrument(instrument))
    part.insert(0, _clef(clef_name))
    if first:
        part.insert(0, m21tempo.MetronomeMark(number=tempo_bpm))
    part.insert(0, _key(tonic, mode))
    part.insert(0, m21meter.TimeSignature(f"{meter_count}/4"))

    events = _group_chords([
        dict(n, offsetQL=_mxml_round(n["offsetQL"]), durationQL=_mxml_round(n["durationQL"]))
        for n in notes
    ])
    _fill_measures(part, events, meter_count, drums=drums)

    # 和弦记号：挂在每小节第一拍上方（由 build_m21 统一传进来，只挂一次）
    if chords and not drums:
        for measure_str, name in chords.items():
            try:
                offset = float(int(measure_str) * meter_count)
            except (TypeError, ValueError):
                continue
            try:
                part.insert(offset, harmony.ChordSymbol(name))
            except Exception:
                continue

    try:
        part.makeNotation(inPlace=True)
    except Exception:
        try:
            part.makeMeasures(inPlace=True)
            part.makeTies(inPlace=True)
            part.makeBeams(inPlace=True)
        except Exception:
            pass

    if first:
        try:
            part.insert(0, dynamics.Dynamic("mf"))
        except Exception:
            pass
    return part


def write_outputs(model: dict, out_dir: str, base_name: str = "score") -> dict:
    """生成 MusicXML / MIDI / musicxml 压缩包，返回文件清单。"""
    os.makedirs(out_dir, exist_ok=True)
    score = build_m21(model)
    musicxml_path = os.path.join(out_dir, f"{base_name}.musicxml")
    midi_path = os.path.join(out_dir, f"{base_name}.mid")
    json_path = os.path.join(out_dir, "score.json")

    score.write("musicxml", fp=musicxml_path)
    try:
        score.write("midi", fp=midi_path)
        midi_ok = True
    except Exception:
        midi_ok = False

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False, indent=2)

    files = [musicxml_path, json_path]
    if midi_ok:
        files.append(midi_path)
    return {
        "musicxml": musicxml_path,
        "midi": midi_path if midi_ok else None,
        "json": json_path,
        "files": files,
    }


def load_model(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_model(model: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 编辑操作

def transpose_model(model: dict, semitones: int) -> dict:
    from theory import NOTE_NAMES_FLAT

    for track in model.get("tracks", []):
        spec = instrument_spec(track.get("instrument") or "other")
        if spec["kind"] == "drums":
            continue
        for note in track.get("notes", []):
            note["pitch"] = int(note["pitch"]) + int(semitones)
    meta = model.setdefault("meta", {})
    tonic = meta.get("keyTonic") or "C"
    if tonic in NOTE_NAMES_FLAT:
        pc = NOTE_NAMES_FLAT.index(tonic)
        meta["keyTonic"] = NOTE_NAMES_FLAT[(pc + int(semitones)) % 12]
    # 和弦记号也跟着移
    for track in model.get("tracks", []):
        chords = track.get("chords") or {}
        for key, name in list(chords.items()):
            chords[key] = _transpose_chord_name(name, semitones)
    return model


def _transpose_chord_name(name: str, semitones: int) -> str:
    from theory import NOTE_NAMES_FLAT

    if not name:
        return name
    root = name[0]
    idx = 1
    while idx < len(name) and name[idx] in "#b":
        idx += 1
    root_full = name[:idx]
    suffix = name[idx:]
    if root_full not in NOTE_NAMES_FLAT:
        return name
    pc = NOTE_NAMES_FLAT.index(root_full)
    return NOTE_NAMES_FLAT[(pc + int(semitones)) % 12] + suffix


def delete_range(model: dict, track_id: str, from_measure: int, to_measure: int | None = None) -> dict:
    """删掉某轨第 from_measure 到 to_measure（0 起，含）之间的小节内容。"""
    meter = int(model.get("meta", {}).get("meter") or 4)
    lo = float(from_measure) * meter
    hi = float((to_measure if to_measure is not None else from_measure) + 1) * meter
    for track in model.get("tracks", []):
        if track_id and track.get("id") != track_id:
            continue
        track["notes"] = [
            n for n in track.get("notes", [])
            if not (lo <= float(n["offsetQL"]) < hi)
        ]
    return model


def set_meta(model: dict, key: str, value) -> dict:
    model.setdefault("meta", {})[key] = value
    return model


def summary(model: dict) -> dict:
    tracks = []
    for track in model.get("tracks", []):
        notes = track.get("notes", [])
        spec = instrument_spec(track.get("instrument") or "other")
        pitches = [int(n["pitch"]) for n in notes if spec["kind"] != "drums"]
        tracks.append({
            "id": track.get("id"),
            "label": track.get("label") or spec["label"],
            "instrument": track.get("instrument"),
            "notes": len(notes),
            "range": (midi_to_name(min(pitches)), midi_to_name(max(pitches))) if pitches else None,
        })
    meter = int(model.get("meta", {}).get("meter") or 4)
    max_offset = 0.0
    for track in model.get("tracks", []):
        for note in track.get("notes", []):
            max_offset = max(max_offset, float(note["offsetQL"]) + float(note["durationQL"]))
    return {
        "title": model.get("title"),
        "tempo": model.get("meta", {}).get("tempo"),
        "meter": meter,
        "key": f"{model.get('meta', {}).get('keyTonic')} {model.get('meta', {}).get('keyMode')}",
        "measures": int(max_offset // meter) + 1 if max_offset else 0,
        "tracks": tracks,
    }
