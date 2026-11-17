#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""制谱师内核：音频 → 分轨 → 逐轨识音 → 量化 → 曲谱。

stdout 每行一个 JSON 事件（Node 侧按行解析）：
  {"type":"phase","pct":12.5,"stage":"separate","msg":"..."}
  {"type":"log","msg":"..."}
  {"type":"result","ok":true,...}
  {"type":"error","ok":false,"message":"...","traceback":"..."}

子命令：
  transcribe  完整流水线
  build       从已有 score.json 重新生成 MusicXML / MIDI / 预览
  render      只重排预览图
  selftest    自检
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def phase(pct: float, stage: str, msg: str) -> None:
    emit({"type": "phase", "pct": round(float(pct), 1), "stage": stage, "msg": msg})


def log(msg: str) -> None:
    emit({"type": "log", "msg": msg})


# ---------------------------------------------------------------- 分轨

DEFAULT_MODEL = "htdemucs_6s"


def separate(input_path: str, out_dir: str, separator: dict) -> dict:
    """调用 stems 插件里的 demucs worker（它在有 torch 的环境里跑）。"""
    worker = separator.get("worker")
    python = separator.get("python")
    repo = separator.get("repoDir") or ""
    if not worker or not os.path.isfile(worker):
        raise RuntimeError(f"找不到分轨内核: {worker}")
    if not python or not os.path.isfile(python):
        raise RuntimeError(f"找不到分轨用的 Python: {python}")

    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        python, worker,
        "--input", input_path,
        "--out-dir", out_dir,
        "--model", separator.get("model") or DEFAULT_MODEL,
        "--device", separator.get("device") or "auto",
        "--shifts", str(separator.get("shifts") or 1),
        "--format", "wav",
    ]
    if repo:
        cmd += ["--repo-dir", repo]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    stems = []
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "progress":
                pct = 5.0 + float(event.get("pct") or 0) * 0.38
                phase(pct, "separate", "分轨中")
            elif kind == "phase":
                pct = 5.0 + float(event.get("pct") or 0) * 0.38
                phase(pct, "separate", event.get("msg") or "分轨中")
            elif kind == "result":
                stems = event.get("stems") or []
            elif kind == "error":
                raise RuntimeError(event.get("message") or "分轨失败")
    finally:
        if proc.stdout:
            proc.stdout.close()
        code = proc.wait()
    if code != 0 and not stems:
        raise RuntimeError(f"分轨进程退出码 {code}")
    return {"stems": stems, "dir": out_dir}


# ---------------------------------------------------------------- 主流程

DEFAULT_MAP = {
    "vocals": "vocals",
    "guitar": "guitar",
    "piano": "piano",
    "bass": "bass",
    "drums": "drums",
    "other": "other",
}


def cmd_transcribe(args) -> int:
    import build_score
    import quantize as qz
    import render as rnd
    import theory
    import transcribe as tr

    started = time.time()
    work_dir = os.path.abspath(args.work_dir)
    os.makedirs(work_dir, exist_ok=True)
    input_path = os.path.abspath(args.input)
    if not os.path.isfile(input_path):
        emit({"type": "error", "ok": False, "message": f"音频不存在: {input_path}"})
        return 2
    title = args.title or os.path.splitext(os.path.basename(input_path))[0]

    # 1) 分轨
    stems_dir = args.stems_dir
    if stems_dir and os.path.isdir(stems_dir):
        phase(40.0, "separate", f"复用已有分轨 {stems_dir}")
    elif args.no_separate:
        stems_dir = None
    else:
        phase(2.0, "separate", "开始分轨")
        sep_conf = json.loads(args.separator) if args.separator else {}
        info = separate(input_path, os.path.join(work_dir, "stems"), sep_conf)
        stems_dir = info["dir"]
        phase(43.0, "separate", "分轨完成")

    # 2) 全局分析
    phase(45.0, "analyze", "分析速度 / 调性 / 拍号")
    analysis = tr.analyze(input_path)
    tempo = float(args.tempo or analysis["tempo"])
    meter = int(args.meter or analysis["meter"])
    key_info = theory.detect_key(analysis["chroma"])
    key_tonic = args.key.split(":")[0] if args.key else key_info["tonic"]
    key_mode = (args.key.split(":")[1] if args.key and ":" in args.key else key_info["mode"])
    phase(50.0, "analyze", f"{tempo:g} BPM · {meter}/4 · {key_tonic} {key_mode}")

    grid = qz.grid_value(args.grid)
    duration = float(analysis.get("duration") or 0.0)

    # 3) 逐轨识别。先只出「原始音符（秒）」，节奏原点等看到所有声部的第一颗音再定：
    #    低频声部（贝斯 C2）的第一个起音经常检不出来，拿起音当锚会把整首往前/往后挪一拍。
    stem_files = _collect_stems(stems_dir, input_path, args.only)
    instrument_map = dict(DEFAULT_MAP)
    if args.map:
        for pair in args.map.split(","):
            if "=" in pair:
                stem, inst = pair.split("=", 1)
                instrument_map[stem.strip()] = inst.strip()

    max_seconds = float(args.max_seconds or 0)
    pending = []
    total = max(1, len(stem_files))
    for index, (stem_name, stem_path) in enumerate(stem_files):
        instrument = instrument_map.get(stem_name)
        if not instrument:
            continue
        spec = theory.instrument_spec(instrument)
        base_pct = 52.0 + 24.0 * index / total
        phase(base_pct, "transcribe", f"识别 {spec['label']}（{stem_name}）")
        try:
            audio_path = stem_path
            if max_seconds:
                audio_path = _trim(stem_path, os.path.join(work_dir, f"trim_{stem_name}.wav"), max_seconds)
            raw_notes = tr.transcribe_stem(audio_path, instrument)
        except Exception as exc:  # noqa: BLE001
            log(f"{stem_name} 识别失败，跳过：{exc}")
            continue
        pending.append((stem_name, stem_path, instrument, spec, raw_notes))

    if not pending:
        emit({"type": "error", "ok": False, "message": "没有识别出任何音符"})
        return 3

    # 3.5) 节奏原点：所有声部第一颗音里最早那个之前最近的拍点。
    starts = [float(n["start"]) for item in pending for n in item[4]]
    anchor = min(starts) if starts else float(analysis.get("firstOnsetSec") or 0.0)
    origin = args.origin if args.origin is not None else _origin(analysis, meter, anchor)
    mapper = qz.BeatMapper(analysis["beatTimes"], tempo, origin=origin, meter=meter)
    log(f"节奏原点 {origin:.3f}s（第一个音 {anchor:.3f}s）")

    # 3.6) 量化 / 合并 / 补连音
    tracks = []
    grids_used: list[float] = []
    for index, (stem_name, stem_path, instrument, spec, raw_notes) in enumerate(pending):
        stem_grid = _grid_for_stem(args.grid, raw_notes, tempo)
        grids_used.append(stem_grid)
        notes = qz.quantize_notes(
            raw_notes, mapper, grid=stem_grid,
            meter=meter,
            mono=(spec["kind"] == "mono"), min_dur=stem_grid / 2 if spec["kind"] != "drums" else stem_grid / 4,
            drums=(spec["kind"] == "drums"),
        )
        if spec["kind"] != "drums":
            notes = qz.tidy_retriggers(notes, grid=stem_grid)
            notes = qz.legato(notes, max_gap=stem_grid * 2.0)
            if spec["kind"] == "poly":
                notes = qz.unify_chord_durations(notes, grid=stem_grid)
        if not notes:
            log(f"{stem_name} 没识别到音符")
            continue
        if len(notes) < args.min_notes and duration >= 20:
            log(f"{stem_name} 只识别到 {len(notes)} 个音，整条略过（谱面上留空行反而碍事）")
            continue
        track = {
            "id": stem_name,
            "instrument": instrument,
            "label": spec["label"],
            "kind": spec["kind"],
            "notes": notes,
        }
        if spec["kind"] != "drums":
            chords = qz.estimate_chords(notes, meter=meter)
            if chords:
                track["chords"] = chords
        tracks.append(track)
        phase(52.0 + 30.0 * (index + 1) / max(1, len(pending)), "transcribe",
              f"{spec['label']} {len(notes)} 个音")

    if not tracks:
        emit({"type": "error", "ok": False, "message": "没有识别出任何音符"})
        return 3

    # 3.5) 用识别出来的音符再定一次调：Chroma 会被鼓和混音带偏，音符本身更准
    if not args.key:
        all_notes = [n for t in tracks for n in t["notes"]]
        note_key = theory.detect_key_from_notes([t["notes"] for t in tracks], fallback=key_info)
        chroma_fit = theory.key_fit(theory.chroma_from_notes(all_notes),
                                    theory.name_to_pitch_class(key_tonic), key_mode)
        if note_key.get("fit", 0) > chroma_fit + 0.02:
            log(f"调性按音符修正：{key_tonic} {key_mode} → {note_key['tonic']} {note_key['mode']}"
                f"（贴合度 {chroma_fit:.2f} → {note_key.get('fit', 0):.2f}）")
            key_tonic = note_key["tonic"]
            key_mode = note_key["mode"]
            key_info = {**key_info, **note_key, "score": note_key.get("fit", 0)}

    # 3.6) 调外音吸附：短促的调外音基本都是识别误差，吸回音阶（长音保留，可能是色彩音）
    snapped_total = 0
    for track in tracks:
        if track.get("kind") == "drums":
            continue
        fixed_notes, fixed_n = theory.snap_notes_to_scale(track["notes"], key_tonic, key_mode)
        if fixed_n:
            track["notes"] = fixed_notes
            snapped_total += fixed_n
    if snapped_total:
        log(f"调外音吸附：{snapped_total} 个短音拉回音阶（{key_tonic} {key_mode}）")

    # 4) 组装曲谱
    phase(84.0, "build", "排小节、连音、加谱号")
    song_chords = qz.estimate_song_chords(
        {t["id"]: t["notes"] for t in tracks if t.get("kind") != "drums"}, meter=meter
    )
    if not song_chords:
        song_chords = qz.merge_chord_tracks([t.get("chords") or {} for t in tracks])
    for track in tracks:
        if song_chords:
            track["chords"] = song_chords

    model = {
        "version": 1,
        "title": title,
        "createdAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "chords": {str(k): v for k, v in (song_chords or {}).items()},
        "meta": {
            "tempo": round(tempo, 2),
            "meter": meter,
            "keyTonic": key_tonic,
            "keyMode": key_mode,
            "keyConfidence": round(float(key_info.get("score") or 0), 3),
            "durationSec": analysis["duration"],
            "grid": _finest_grid(grids_used, args.grid),
            "originSec": round(float(getattr(mapper, "origin", 0.0) or 0.0), 4),
            "source": input_path,
        },
        "tracks": tracks,
    }
    out = build_score.write_outputs(model, work_dir, base_name="score")
    phase(90.0, "build", "已生成 MusicXML / MIDI")

    # 5) 预览
    preview = None
    try:
        phase(92.0, "render", "排版预览")
        summary = build_score.summary(model)
        subtitle = f"{summary['tempo']:g} BPM · {meter}/4 · {summary['key']} · {summary['measures']} 小节"
        preview = rnd.render_preview(out["musicxml"], work_dir, title, subtitle,
                                     resource_path=args.verovio_resources,
                                     browser=args.browser, model=model)
        phase(98.0, "render", "预览完成")
    except Exception as exc:  # noqa: BLE001
        log(f"预览渲染失败（不影响曲谱）：{exc}")

    manifest = {
        "title": title,
        "workDir": work_dir,
        "createdAt": model["createdAt"],
        "meta": model["meta"],
        "summary": build_score.summary(model),
        "files": {
            "musicxml": out["musicxml"],
            "midi": out["midi"],
            "json": out["json"],
            "previewPng": (preview or {}).get("png"),
            "previewPagePngs": (preview or {}).get("pagePngs") or [],
            "previewHtml": (preview or {}).get("html"),
        },
        "elapsedSec": round(time.time() - started, 2),
    }
    with open(os.path.join(work_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    emit({"type": "result", "ok": True, **manifest})
    return 0


def _origin(analysis: dict, meter: int, anchor: float | None = None):
    """节奏原点：第一个音之前最近的那个拍点。

    拍号相位在纯人声/纯贝斯上经常猜错（没有鼓，重音本来就虚），
    以前拿「第一个强拍」当原点，会把整首谱往后推 1–2 秒，量化看着正常、
    回放和评测全错。origin 只服从「音符要贴着音频」，相位只用来数小节。
    """
    beats = analysis.get("beatTimes") or []
    if not beats:
        return 0.0
    if anchor is None:
        anchor = float(analysis.get("firstOnsetSec") or 0.0)
    anchor = float(anchor)
    idx = bisect.bisect_right([float(b) for b in beats], anchor + 1e-6) - 1
    if idx < 0:
        return max(0.0, anchor)  # 第一个音比第一个拍点还早：以它为原点，别丢音
    origin = float(beats[idx])
    # 拍点搜索会把第一个拍点挪到 0 之前（负时间），原点必须落在音频范围内，
    # 否则整条谱被推后几十毫秒，评测/回放都对不上。
    return origin if origin >= 0.0 else max(0.0, anchor)


def _grid_for_stem(grid_arg: str, raw_notes: list[dict], tempo: float = 120.0) -> float:
    """`--grid auto` 时按「相邻起音间隔」决定网格：
    说唱这种密集咬字（中位间隔 < 0.42 拍）走 1/32，慢歌走 1/16。"""
    import quantize as qz

    if str(grid_arg).lower() != "auto":
        return qz.grid_value(grid_arg)
    starts = sorted(float(n.get("start", n.get("offsetQL", 0.0))) for n in raw_notes)
    gaps = [b - a for a, b in zip(starts, starts[1:]) if 0.02 < (b - a) < 2.0]
    if len(gaps) < 8:
        return qz.grid_value("1/16")
    gaps.sort()
    median = gaps[len(gaps) // 2]
    beats = median * max(1.0, float(tempo)) / 60.0
    return qz.grid_value("1/32") if beats < 0.42 else qz.grid_value("1/16")


def _finest_grid(grids: list[float], fallback: str) -> str:
    """meta 里记最细的那档网格（1/32 比 1/16 细）。"""
    import quantize as qz

    if not grids:
        return "auto" if str(fallback).lower() == "auto" else str(fallback)
    best = min(grids)
    for name, value in getattr(qz, "GRID_NAMES", {}).items():
        if abs(float(value) - float(best)) < 1e-6:
            return name
    return "1/16"


def _collect_stems(stems_dir: str | None, input_path: str, only: str = "") -> list[tuple[str, str]]:
    wanted = {s.strip() for s in (only or "").split(",") if s.strip()}
    found = []
    if stems_dir and os.path.isdir(stems_dir):
        for name in ("vocals", "drums", "bass", "guitar", "piano", "other"):
            path = os.path.join(stems_dir, f"{name}.wav")
            if os.path.isfile(path) and (not wanted or name in wanted):
                found.append((name, path))
    if not found:
        # 没有分轨时，直接把整段当人声/其他尝试一次
        found.append(("other", input_path))
    return found


def _trim(src: str, dst: str, seconds: float) -> str:
    import librosa
    import soundfile as sf

    y, sr = librosa.load(src, sr=None, mono=False, offset=0.0, duration=seconds)
    sf.write(dst, y.T if getattr(y, "ndim", 1) > 1 else y, sr)
    return dst


def cmd_build(args) -> int:
    import build_score
    import render as rnd

    project = os.path.abspath(args.project)
    json_path = os.path.join(project, "score.json") if os.path.isdir(project) else project
    model = build_score.load_model(json_path)
    work_dir = os.path.dirname(json_path)
    out = build_score.write_outputs(model, work_dir, base_name="score")
    phase(60.0, "build", "已重生成 MusicXML")
    preview = None
    if not args.no_preview:
        summary = build_score.summary(model)
        subtitle = f"{summary['tempo']:g} BPM · {summary['meter']}/4 · {summary['key']} · {summary['measures']} 小节"
        try:
            preview = rnd.render_preview(out["musicxml"], work_dir, model.get("title") or "曲谱",
                                         subtitle, resource_path=args.verovio_resources,
                                         browser=args.browser, model=model)
        except Exception as exc:  # noqa: BLE001
            log(f"预览失败：{exc}")
    emit({
        "type": "result", "ok": True,
        "musicxml": out["musicxml"], "midi": out["midi"], "json": out["json"],
        "previewPng": (preview or {}).get("png"),
        "previewPagePngs": (preview or {}).get("pagePngs") or [],
        "previewHtml": (preview or {}).get("html"),
        "summary": build_score.summary(model),
    })
    return 0


def _apply_ops(model: dict, ops: list[dict]) -> list[str]:
    """按 op 列表改 score.json。返回做过的事，用来回话。"""
    import build_score

    done = []
    for op in ops or []:
        kind = str(op.get("op") or "").strip()
        track_id = str(op.get("trackId") or "").strip()
        if kind == "transpose":
            semis = int(op.get("semitones") or 0)
            if not semis:
                continue
            if track_id:
                for track in model.get("tracks", []):
                    if track.get("id") != track_id:
                        continue
                    spec = build_score.instrument_spec(track.get("instrument") or "other")
                    if spec["kind"] == "drums":
                        continue
                    for note in track.get("notes", []):
                        note["pitch"] = int(note["pitch"]) + semis
            else:
                build_score.transpose_model(model, semis)
            done.append(f"整体移调 {semis:+d} 个半音" if not track_id else f"{track_id} 移调 {semis:+d} 个半音")
        elif kind == "deleteMeasures":
            start = int(op.get("from") or 0)
            end = op.get("to")
            end = int(end) if end is not None else start
            build_score.delete_range(model, track_id, start, end)
            done.append(f"删掉第 {start + 1}–{end + 1} 小节的音符" + (f"（{track_id}）" if track_id else ""))
        elif kind == "setMeta":
            name = str(op.get("key") or "")
            value = op.get("value")
            if not name:
                # 也接受 {"op":"setMeta","tempo":100} 这种扁平写法
                for field in ("tempo", "meter", "keyTonic", "keyMode", "grid"):
                    if field in op:
                        name, value = field, op[field]
                        break
            if name in ("tempo", "meter", "keyTonic", "keyMode", "grid"):
                if name == "tempo":
                    value = float(value)
                elif name == "meter":
                    value = int(value)
                model.setdefault("meta", {})[name] = value
                done.append(f"{name} = {value}")
        elif kind == "renameTitle":
            model["title"] = str(op.get("value") or op.get("title") or model.get("title") or "曲谱")
            done.append(f"曲名改成《{model['title']}》")
        elif kind == "renameTrack":
            for track in model.get("tracks", []):
                if track.get("id") == track_id:
                    track["label"] = str(op.get("label") or op.get("value") or track.get("label"))
                    done.append(f"{track_id} 改名 {track['label']}")
        elif kind == "removeTrack":
            before = len(model.get("tracks", []))
            model["tracks"] = [t for t in model.get("tracks", []) if t.get("id") != track_id]
            if len(model["tracks"]) < before:
                done.append(f"删掉整轨 {track_id}")
        elif kind == "setChords":
            chords = {str(k): str(v) for k, v in (op.get("chords") or {}).items()}
            if op.get("mode") == "replace":
                model["chords"] = dict(chords)
            else:
                merged = dict(model.get("chords") or {})
                merged.update(chords)
                model["chords"] = merged
            for track in model.get("tracks", []):
                if track_id and track.get("id") != track_id:
                    continue
                track["chords"] = dict(model["chords"])
            done.append(f"和弦记号更新（{len(chords)} 处）")
        elif kind == "nudgeNote":
            # 手动挪音符：pitch 绝对值，或按半音偏移
            for track in model.get("tracks", []):
                if track.get("id") != track_id:
                    continue
                for note in track.get("notes", []):
                    if abs(float(note["offsetQL"]) - float(op.get("offsetQL") or -1)) < 1e-6:
                        if op.get("pitch") is not None:
                            note["pitch"] = int(op["pitch"])
                        if op.get("durationQL") is not None:
                            note["durationQL"] = float(op["durationQL"])
                        done.append(f"改了 {track_id} 在 {note['offsetQL']} 处的音")
                        break
        elif kind in ("addNote", "deleteNote", "setNote", "clearNotes"):
            import theory

            track = next((t for t in model.get("tracks", []) if t.get("id") == track_id), None)
            if track is None:
                continue
            if kind == "clearNotes":
                track["notes"] = []
                done.append(f"清空 {track_id} 的音符")
                continue
            meter = int(model.get("meta", {}).get("meter") or 4)
            measure = op.get("measure")
            beat = op.get("beat")
            offset = op.get("offsetQL")
            if offset is None:
                offset = (float(beat) - 1.0) if beat is not None else 0.0
            target = ((int(measure) - 1) * meter if measure is not None else 0.0) + float(offset)
            place = f"第 {int(measure)} 小节" if measure is not None else f"{target:g} 拍处"

            def _pitch_value(value):
                if isinstance(value, (int, float)):
                    return int(value)
                return theory.name_to_midi(str(value))

            if kind == "deleteNote":
                tol = float(op.get("toleranceQL") or 0.26)
                want = op.get("pitch")
                want_pitch = _pitch_value(want) if want is not None else None
                keep, hit = [], 0
                for note in track.get("notes", []):
                    same_time = abs(float(note["offsetQL"]) - target) <= tol
                    same_pitch = want_pitch is None or int(note["pitch"]) == want_pitch
                    if same_time and same_pitch:
                        hit += 1
                        continue
                    keep.append(note)
                if hit:
                    track["notes"] = keep
                    done.append(f"{track_id} {place}删掉 {hit} 个音")
                continue

            if kind == "addNote":
                want = op.get("pitch")
                if want is None:
                    continue
                pitch = _pitch_value(want)
                dur = float(op.get("durationQL") or 1.0)
                note = {
                    "offsetQL": round(target, 4),
                    "durationQL": dur,
                    "pitch": pitch,
                    "velocity": float(op.get("velocity") or 0.8),
                }
                if op.get("drum"):
                    note["drum"] = str(op["drum"])
                track.setdefault("notes", []).append(note)
                track["notes"].sort(key=lambda n: (float(n["offsetQL"]), int(n["pitch"])))
                done.append(f"{track_id} {place}加了 {theory.midi_to_name(pitch)}（{dur:g} 拍）")
                continue

            # setNote：就地改音高 / 时长 / 力度
            tol = float(op.get("toleranceQL") or 0.26)
            changed = 0
            for note in track.get("notes", []):
                if abs(float(note["offsetQL"]) - target) > tol:
                    continue
                if op.get("pitch") is not None:
                    note["pitch"] = _pitch_value(op["pitch"])
                if op.get("durationQL") is not None:
                    note["durationQL"] = float(op["durationQL"])
                if op.get("velocity") is not None:
                    note["velocity"] = float(op["velocity"])
                changed += 1
            if changed:
                done.append(f"{track_id} {place}改了 {changed} 个音")
    return done


def cmd_edit(args) -> int:
    import build_score
    import render as rnd

    project = os.path.abspath(args.project)
    json_path = os.path.join(project, "score.json") if os.path.isdir(project) else project
    model = build_score.load_model(json_path)
    ops = json.loads(args.ops) if args.ops else []
    done = _apply_ops(model, ops)
    build_score.save_model(model, json_path)

    out = build_score.write_outputs(model, os.path.dirname(json_path), base_name="score")
    phase(70.0, "build", "已重新生成 MusicXML")
    summary = build_score.summary(model)
    preview = None
    if not args.no_preview:
        subtitle = f"{summary['tempo']:g} BPM · {summary['meter']}/4 · {summary['key']} · {summary['measures']} 小节"
        try:
            preview = rnd.render_preview(out["musicxml"], os.path.dirname(json_path),
                                         model.get("title") or "曲谱", subtitle,
                                         resource_path=args.verovio_resources, browser=args.browser,
                                         model=model)
        except Exception as exc:  # noqa: BLE001
            log(f"预览失败：{exc}")
    emit({
        "type": "result", "ok": True,
        "changed": done,
        "musicxml": out["musicxml"], "midi": out["midi"], "json": out["json"],
        "previewPng": (preview or {}).get("png"),
        "previewPagePngs": (preview or {}).get("pagePngs") or [],
        "previewHtml": (preview or {}).get("html"),
        "summary": summary,
    })
    return 0


def cmd_selftest(args) -> int:
    result = {"ok": True, "python": sys.version.split()[0], "checks": {}}
    libs = ["numpy", "librosa", "soundfile", "music21", "verovio", "onnxruntime",
            "basic_pitch", "pretty_midi"]
    for name in libs:
        try:
            module = __import__(name)
            version = getattr(module, "__version__", "")
            result["checks"][name] = f"ok {version}".strip()
        except Exception as exc:  # noqa: BLE001
            result["checks"][name] = f"FAIL {exc}"
            result["ok"] = False
    try:
        import render as rnd
        result["checks"]["browser"] = rnd._find_browser() or "未找到"
    except Exception as exc:  # noqa: BLE001
        result["checks"]["browser"] = f"FAIL {exc}"
    try:
        import verovio
        tk = verovio.toolkit()
        ok = bool(tk.loadData("X:1\nT:t\nM:4/4\nL:1/4\nK:C\nCDEF|"))
        result["checks"]["verovio_render"] = "ok" if ok else "FAIL 字体/资源路径有问题"
        if not ok:
            result["ok"] = False
    except Exception as exc:  # noqa: BLE001
        result["checks"]["verovio_render"] = f"FAIL {exc}"
        result["ok"] = False
    emit({"type": "result", **result})
    return 0


def cmd_parts(args) -> int:
    """演奏谱：按乐器分别出谱（人声简谱 / 吉他六线谱 / 钢琴五线谱 / ……），各存各的。"""
    import build_score
    import parts as partsmod

    project = os.path.abspath(args.project)
    json_path = os.path.join(project, "score.json") if os.path.isdir(project) else project
    model = build_score.load_model(json_path)
    work_dir = os.path.dirname(json_path)

    lyrics = None
    if args.lyrics:
        path = os.path.abspath(args.lyrics)
        with open(path, "r", encoding="utf-8-sig") as fh:
            lyrics = [line.strip() for line in fh if line.strip()]
        log(f"歌词读进来 {len(lyrics)} 行：{os.path.basename(path)}")

    only = [s.strip() for s in (args.only or "").split(",") if s.strip()] or None
    phase(30.0, "parts", "按乐器分别出谱")
    res = partsmod.render_parts(model, work_dir, lyrics=lyrics, only=only,
                                browser=args.browser or None)

    files = {"partsIndex": res["index"], "partsDir": res["dir"]}
    listing = []
    for entry in res["parts"]:
        png = entry["files"].get("png")
        files[entry["trackId"]] = png
        listing.append({
            "trackId": entry["trackId"], "label": entry["label"], "kind": entry["kind"],
            "dir": entry["dir"], "png": png,
            "html": entry["files"].get("html"),
            "musicxml": entry["files"].get("musicxml"),
        })
        phase(30.0 + 60.0 * (len(listing) / max(1, len(res["parts"]))), "parts",
              f"{entry['label']} → {entry['kind']}")
    emit({
        "type": "result", "ok": True,
        "partsDir": res["dir"], "partsIndex": res["index"],
        "parts": listing, "files": files,
        "summary": build_score.summary(model),
    })
    return 0


def cmd_arrange(args) -> int:
    """指弹改编：把整首歌编进一把吉他（和弦-旋律编配），出六线谱。"""
    import arrange as arr
    import build_score
    import parts as partsmod

    project = os.path.abspath(args.project)
    json_path = os.path.join(project, "score.json") if os.path.isdir(project) else project
    model = build_score.load_model(json_path)
    work_dir = os.path.dirname(json_path)

    tracks = model.get("tracks") or []
    melody = None
    if args.melody:
        melody = next((t for t in tracks if t.get("id") == args.melody), None)
        if melody is None:
            log(f"没找到轨道 {args.melody}，改用自动挑旋律")
    if melody is None:
        melody = arr.pick_melody_track(model)
    if melody is None:
        raise RuntimeError("没有可用的旋律轨（所有轨都是鼓或太短）")
    log(f"旋律取自：{melody.get('label') or melody.get('id')}")

    phase(20.0, "arrange", "把旋律、低音、和弦内音编进一把吉他")
    solo = arr.arrange_solo(model, melody_track=melody, grid=args.grid,
                            instrument=args.instrument,
                            bass_octave=args.bass_octave,
                            melody_shift=args.shift)

    kept = [t for t in tracks if t.get("id") != solo["id"]]
    kept.append(solo)
    model["tracks"] = kept
    build_score.save_model(model, json_path)
    log(f"指弹轨生成：{len(solo['notes'])} 个音")

    phase(60.0, "arrange", "出六线谱")
    res = partsmod.render_parts(model, work_dir, only=[solo["id"]],
                                browser=args.browser or None)
    entry = res["parts"][0] if res["parts"] else {}
    emit({
        "type": "result", "ok": True,
        "track": {"id": solo["id"], "label": solo["label"], "notes": len(solo["notes"])},
        "png": entry.get("files", {}).get("png"),
        "html": entry.get("files", {}).get("html"),
        "musicxml": entry.get("files", {}).get("musicxml"),
        "partsDir": res["dir"], "partsIndex": res["index"],
        "summary": build_score.summary(model),
    })
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="score_worker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    tp = sub.add_parser("transcribe")
    tp.add_argument("--input", required=True)
    tp.add_argument("--work-dir", required=True)
    tp.add_argument("--title", default="")
    tp.add_argument("--stems-dir", default="")
    tp.add_argument("--no-separate", action="store_true")
    tp.add_argument("--separator", default="", help="JSON: {python,worker,repoDir,model,device,shifts}")
    tp.add_argument("--tempo", type=float, default=0.0)
    tp.add_argument("--meter", type=int, default=0)
    tp.add_argument("--key", default="", help="例如 G:major 或 e:minor")
    tp.add_argument("--grid", default="auto", help="1/4 1/8 1/16 1/32，或 auto（按起音密度自动选）")
    tp.add_argument("--origin", type=float, default=None)
    tp.add_argument("--map", default="", help="stem=instrument 逗号分隔")
    tp.add_argument("--only", default="", help="只处理这些 stem")
    tp.add_argument("--max-seconds", type=float, default=0.0)
    tp.add_argument("--min-notes", type=int, default=5,
                    help="少于这么多音的整轨略过（时长 < 20s 时不生效）")
    tp.add_argument("--verovio-resources", default="")
    tp.add_argument("--browser", default="")
    tp.add_argument("--no-preview", action="store_true",
                    help="跳过 PNG/HTML 预览（评测/批量时快很多）")

    bp = sub.add_parser("build")
    bp.add_argument("--project", required=True)
    bp.add_argument("--no-preview", action="store_true")
    bp.add_argument("--verovio-resources", default="")
    bp.add_argument("--browser", default="")

    ep = sub.add_parser("edit")
    ep.add_argument("--project", required=True)
    ep.add_argument("--ops", required=True, help="JSON 数组，每个元素一个编辑动作")
    ep.add_argument("--no-preview", action="store_true")
    ep.add_argument("--verovio-resources", default="")
    ep.add_argument("--browser", default="")

    pp = sub.add_parser("parts")
    pp.add_argument("--project", required=True)
    pp.add_argument("--lyrics", default="", help="歌词文件（UTF-8，一行一句）")
    pp.add_argument("--only", default="", help="只出这些轨，逗号分隔")
    pp.add_argument("--browser", default="")

    sub.add_parser("selftest")

    args = ap.parse_args()
    try:
        if args.cmd == "transcribe":
            return cmd_transcribe(args)
        if args.cmd == "build":
            return cmd_build(args)
        if args.cmd == "edit":
            return cmd_edit(args)
        if args.cmd == "parts":
            return cmd_parts(args)
        if args.cmd == "selftest":
            return cmd_selftest(args)
        return 1
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "ok": False, "message": str(exc),
              "traceback": traceback.format_exc()})
        return 5


if __name__ == "__main__":
    sys.exit(main())
