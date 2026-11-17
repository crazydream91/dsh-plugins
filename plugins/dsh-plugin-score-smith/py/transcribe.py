# -*- coding: utf-8 -*-
"""把一条 stem 音频变成音符序列。

三种识别器：
  * 复音（吉他 / 钢琴 / other）  → basic-pitch（ONNX 版 NMP 模型，音高 + 起止 + 力度）
  * 单音（人声 / 贝斯）          → librosa.pyin 基频轨迹，再做音符切分
  * 打击乐（鼓）                 → 起音检测 + 频段/衰减特征分类成 底鼓/军鼓/踩镲/通鼓/镲

输出统一是 note dict：{"start":秒, "end":秒, "pitch":MIDI, "velocity":0~1}
鼓的 note 额外带 "drum": "kick"/"snare"/...
"""

from __future__ import annotations

import logging
import math
import os
import warnings

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import numpy as np

SR_PITCH = 22050

_BP_MODEL = None


def _np(x):
    return float(x)


# ---------------------------------------------------------------- 读音频

def load_audio(path: str, sr: int | None = None, mono: bool = True):
    import librosa

    y, sr_out = librosa.load(path, sr=sr, mono=mono)
    return y.astype(np.float32), int(sr_out)


# ---------------------------------------------------------------- 全局分析

def analyze(path: str) -> dict:
    """速度、拍点、调性、拍号猜测。用整段混音做，比单条 stem 稳。"""
    import librosa

    y, sr = load_audio(path, sr=SR_PITCH, mono=True)
    duration = len(y) / sr

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=256)
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env, sr=sr, hop_length=256, trim=False
    )
    tempo = float(np.atleast_1d(tempo)[0])
    beat_frames = np.atleast_1d(beat_frames)

    # 速度倍频校正：记谱一般落在 50–170 之间比较好看
    while tempo > 0 and tempo < 55:
        tempo *= 2
    while tempo > 175:
        tempo /= 2
    # 人耳和谱面都吃整数速度：95.7 就该写 96
    rounded = round(tempo)
    if tempo > 0 and abs(tempo - rounded) <= max(0.4, tempo * 0.005):
        tempo = float(rounded)

    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=256)
    # beat_track 的 tempo 是它自己估的，经常差 1–2 BPM（100 报成 99）。
    # 中位间隔也救不回来（99.4 → 99），整首都跟不上；改成对「拍点编号 ↔ 时间」线性回归，
    # 斜率就是每拍秒数，48 拍平均下来误差能压到 0.1 BPM 以内。
    if len(beat_times) >= 8:
        idx = np.arange(len(beat_times), dtype=float)
        slope = float(np.polyfit(idx, beat_times, 1)[0])
        if slope > 0:
            refined = 60.0 / slope
            while refined < 55:
                refined *= 2
            while refined > 175:
                refined /= 2
            if abs(refined - tempo) / max(tempo, 1e-6) <= 0.12:
                r2 = round(refined)
                tempo = float(r2) if abs(refined - r2) <= max(0.3, refined * 0.004) else round(refined, 2)
    interval = 60.0 / tempo if tempo > 0 else 0.5

    try:
        onset_frames = librosa.onset.onset_detect(
            onset_envelope=onset_env, sr=sr, hop_length=256, units="frames",
            backtrack=False, delta=0.045, wait=3)
        first_onset = float(librosa.frames_to_time(onset_frames[:1], sr=sr,
                                                   hop_length=256)[0]) if len(onset_frames) else 0.0
    except Exception:
        first_onset = 0.0

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
    chroma_mean = chroma.mean(axis=1)

    # 拍点常常整条偏半个拍（librosa 贴着起音走），而且和声换和弦的位置只有对齐了
    # 才能正确切小节。所以把「整体平移量 + 拍号 + 小节相位」放在一起搜。
    grid = _search_grid(onset_env, chroma, beat_times, interval, sr=sr, hop=256)
    shift = float(grid["shift"])
    beat_times = beat_times + shift
    beat_times, prepended = _extend_beats(beat_times, interval, duration)

    strengths = onset_env[beat_frames] if len(beat_frames) else np.array([])
    meter_onset, phase_onset = _guess_meter(strengths)

    if grid["contrast"] is not None and grid["contrast"] >= 0.008:
        meter, phase = int(grid["meter"]), int(grid["phase"])
    else:
        meter, phase = meter_onset, phase_onset

    phase = (int(phase) + int(prepended)) % max(1, int(meter))

    return {
        "duration": round(float(duration), 3),
        "tempo": round(float(tempo), 2),
        "beatTimes": [round(float(t), 4) for t in beat_times[:8192]],
        "beatCount": int(len(beat_times)),
        "beatOffsetSec": round(float(shift), 4),
        "firstOnsetSec": round(float(first_onset), 4),
        "meter": meter,
        "downbeatPhase": int(phase),
        "meterEvidence": {
            "chroma": grid.get("scores"),
            "contrast": grid["contrast"],
            "onset": [meter_onset, phase_onset],
        },
        "chroma": [round(float(c), 6) for c in chroma_mean],
        "sr": sr,
    }


def _search_grid(onset_env, chroma, beat_times, interval: float, sr: int, hop: int,
                 shifts=(-0.5, 0.0, 0.5, -0.25, 0.25), meters=(4, 2, 3)):
    """联合搜索 (拍点平移, 拍号, 小节相位)。

    判据是「换和弦的位置是不是落在小节第一拍上」——比只看强弱拍可靠得多，
    顺便把拍点偏半个拍的问题一起修掉。
    """
    import librosa

    out = {"shift": 0.0, "meter": meters[0], "phase": 0, "contrast": None, "scores": None}
    if chroma is None or beat_times is None or len(beat_times) < 8:
        return out

    duration = chroma.shape[1] * 512 / sr
    env = np.asarray(onset_env, dtype=float) if onset_env is not None else None
    if env is not None and env.size:
        env = (env - env.mean()) / (env.std() + 1e-9)

    best = None
    for frac in shifts:
        shift = frac * interval
        times = np.asarray(beat_times, dtype=float) + shift
        times = times[(times >= 0.0) & (times < duration)]
        if len(times) < 8:
            continue
        try:
            frames = librosa.time_to_frames(times, sr=sr, hop_length=512)
            beat_chroma = librosa.util.sync(chroma, frames, aggregate=np.median)
        except Exception:
            continue
        if beat_chroma.shape[1] < 8:
            continue
        norms = np.linalg.norm(beat_chroma, axis=0, keepdims=True) + 1e-9
        beat_chroma = beat_chroma / norms
        nbeats = beat_chroma.shape[1]
        delta = np.zeros(nbeats)
        for i in range(1, nbeats):
            delta[i] = 1.0 - float(beat_chroma[:, i] @ beat_chroma[:, i - 1])

        onset_bonus = 0.0
        if env is not None and env.size:
            idx = ((times * sr / hop).astype(int))
            idx = idx[(idx >= 0) & (idx < len(env))]
            if idx.size:
                onset_bonus = float(env[idx].mean()) * 0.01

        scores = {}
        for meter in meters:
            best_score, best_ph = -9e9, 0
            for phase in range(meter):
                idx_all = np.arange(1, nbeats)
                mask = (idx_all - phase) % meter == 0
                if mask.sum() < 2 or (~mask).sum() < 2:
                    continue
                d = delta[1:]
                contrast = float(d[mask].mean() - d[~mask].mean())
                if meter == 4:
                    contrast += 0.004
                if contrast > best_score:
                    best_score, best_ph = contrast, phase
            if best_score > -8e9:
                scores[meter] = (round(float(best_score), 4), best_ph)

        if not scores:
            continue
        meter = max(scores, key=lambda m: scores[m][0])
        contrast, phase = scores[meter]
        # 2/4 与 4/4 的「小节第一拍」判定天然重叠（4 拍的 mask 是 2 拍 mask 的子集），
        # 单看换和弦位置会一律偏向 2/4。补一个重音周期判据：第 1 拍明显比第 3 拍重 → 4 拍子。
        if 2 in scores and 4 in scores and meter == 2:
            best4 = scores[4][0]
            if contrast - best4 < 0.012:
                idx_all = np.arange(1, nbeats)
                ph4 = scores[4][1]
                m0 = (idx_all - ph4) % 4 == 0
                m2 = (idx_all - ph4) % 4 == 2
                a0 = float(delta[1:][m0].mean()) if m0.sum() else 0.0
                a2 = float(delta[1:][m2].mean()) if m2.sum() else 0.0
                if a2 <= 0 or a0 / max(a2, 1e-6) > 1.25:
                    meter = 4
                    contrast, phase = scores[4]
        total = contrast + onset_bonus - abs(frac) * 0.004
        if best is None or total > best[0]:
            best = (total, {
                "shift": float(shift),
                "meter": int(meter),
                "phase": int(phase),
                "contrast": round(float(contrast), 4),
                "scores": {m: scores[m][0] for m in scores},
            })
    if best is not None:
        out = best[1]
    return out


def _extend_beats(beat_times, interval: float, duration: float):
    """把拍点向前补到 0 之前、向后补到曲尾，返回 (新拍点, 前面补了几拍)。"""
    import numpy as _np

    times = [float(t) for t in _np.atleast_1d(beat_times)] if len(beat_times) else []
    if not times or interval <= 0:
        return _np.array(times), 0
    prepended = 0
    while times[0] - interval > -interval * 0.5:
        times.insert(0, times[0] - interval)
        prepended += 1
        if prepended > 4096:
            break
    appended = 0
    while times[-1] + interval < duration + interval * 0.5:
        times.append(times[-1] + interval)
        appended += 1
        if appended > 4096:
            break
    return _np.array(times, dtype=float), prepended


def _guess_meter_from_chroma(chroma, beat_times, sr: int, candidates=(4, 3)):
    """和声节奏定拍号：按小节长度分组后，同组各拍的色度相似度更高就说明分组对。"""
    import librosa

    if chroma is None or beat_times is None or len(beat_times) < 8:
        return candidates[0], 0, None
    try:
        beat_frames = librosa.time_to_frames(beat_times, sr=sr, hop_length=512)
        beat_chroma = librosa.util.sync(chroma, beat_frames, aggregate=np.median)
    except Exception:
        return candidates[0], 0, None
    if beat_chroma.shape[1] < 8:
        return candidates[0], 0, None

    # 每拍归一化
    norms = np.linalg.norm(beat_chroma, axis=0, keepdims=True) + 1e-9
    beat_chroma = beat_chroma / norms
    nbeats = beat_chroma.shape[1]

    # 和声变化量：换和弦的位置色度跳得最厉害，正常情况下都落在小节第一拍
    delta = np.zeros(nbeats)
    for i in range(1, nbeats):
        delta[i] = 1.0 - float(beat_chroma[:, i] @ beat_chroma[:, i - 1])

    scores: dict[int, float] = {}
    best_phase = {meter: 0 for meter in candidates}
    for meter in candidates:
        best_score = -9e9
        best_ph = 0
        for phase in range(meter):
            idx = np.arange(1, nbeats)
            d = delta[1:]
            mask = (idx - phase) % meter == 0
            if mask.sum() < 2 or (~mask).sum() < 2:
                continue
            contrast = float(d[mask].mean() - d[~mask].mean())
            if meter == 4:
                contrast += 0.004  # 4/4 略常见
            if contrast > best_score:
                best_score = contrast
                best_ph = phase
        if best_score <= -8e9:
            continue
        scores[meter] = round(float(best_score), 4)
        best_phase[meter] = best_ph
    if not scores:
        return candidates[0], 0, None
    best_meter = max(scores, key=lambda m: scores[m])
    return best_meter, best_phase[best_meter], scores


def _guess_meter(strengths: np.ndarray) -> tuple[int, int]:
    """返回 (拍号分子, 强拍相位)。样本太少就默认 4/4 且相位 0。"""
    if strengths is None or len(strengths) < 12:
        return 4, 0
    s = np.asarray(strengths, dtype=float)
    s = (s - s.mean()) / (s.std() + 1e-9)
    best = (4, 0, -9e9)
    for meter in (4, 3):
        for phase in range(meter):
            idx = np.arange(len(s))
            mask = (idx - phase) % meter == 0
            if mask.sum() < 2:
                continue
            contrast = float(s[mask].mean() - s[~mask].mean()) if (~mask).sum() else 0.0
            # 4/4 常见，给一点先验，别被噪声带偏
            if meter == 4:
                contrast += 0.12
            if contrast > best[2]:
                best = (meter, phase, contrast)
    return best[0], best[1]


# ---------------------------------------------------------------- 复音

def _get_model():
    global _BP_MODEL
    if _BP_MODEL is None:
        from basic_pitch.inference import Model
        from basic_pitch import ICASSP_2022_MODEL_PATH

        _BP_MODEL = Model(ICASSP_2022_MODEL_PATH)
    return _BP_MODEL


def transcribe_polyphonic(path: str, spec: dict, onset_threshold: float = 0.5,
                          frame_threshold: float = 0.3, min_note_sec: float = 0.06,
                          max_polyphony: int = 6) -> list[dict]:
    """basic-pitch 复音识别。

    分析频段比乐器音域上下各放宽一个八度：音域太窄会把高音/低音整条吃掉，
    识别完再用 fold_into_range 把音折回音域里（移八度，不丢音）。
    """
    import basic_pitch.inference as bpi

    lo, hi = spec.get("range", (21, 108))
    ana_lo = max(24, lo - 12)
    ana_hi = min(108, hi + 12)
    model = _get_model()
    _, _, notes = bpi.predict(
        path,
        model,
        onset_threshold=onset_threshold,
        frame_threshold=frame_threshold,
        minimum_note_length=max(30.0, min_note_sec * 1000.0),
        minimum_frequency=float(440.0 * 2 ** ((ana_lo - 69) / 12.0)),
        maximum_frequency=float(440.0 * 2 ** ((ana_hi - 69) / 12.0)),
        melodia_trick=True,
    )
    from theory import fold_into_range

    out = []
    for item in notes:
        start, end, pitch, amplitude = item[0], item[1], item[2], item[3]
        if end - start < min_note_sec:
            continue
        out.append({
            "start": round(float(start), 4),
            "end": round(float(end), 4),
            "pitch": int(fold_into_range(int(pitch), spec)),
            "velocity": round(float(min(1.0, max(0.1, amplitude))), 3),
        })
    out.sort(key=lambda n: (n["start"], n["pitch"]))
    out = _suppress_harmonics(out)
    return _limit_polyphony(out, max_polyphony)


HARMONIC_INTERVALS = (12, 19, 24, 28, 31, 36)


def _suppress_harmonics(notes: list[dict], amp_ratio: float = 0.72,
                        onset_tol: float = 0.09) -> list[dict]:
    """去掉"泛音冒充的音"：比它低 12/19/24… 半音、同时起音、却明显更弱的，一定是泛音。"""
    if not notes:
        return notes
    ordered = sorted(notes, key=lambda n: (n["start"], n["pitch"]))
    kept = []
    for index, note in enumerate(ordered):
        is_harmonic = False
        for other in ordered:
            if other is note:
                continue
            diff = note["pitch"] - other["pitch"]
            if diff not in HARMONIC_INTERVALS:
                continue
            if abs(other["start"] - note["start"]) > onset_tol:
                continue
            overlap = min(note["end"], other["end"]) - max(note["start"], other["start"])
            if overlap < 0.5 * min(note["end"] - note["start"], other["end"] - other["start"]):
                continue
            if other["velocity"] >= note["velocity"] / amp_ratio:
                is_harmonic = True
                break
        if not is_harmonic:
            kept.append(note)
    return kept


def _limit_polyphony(notes: list[dict], max_voices: int) -> list[dict]:
    """同一时刻最多留 max_voices 个音（留力度大的），避免识别出一坨噪音。"""
    if max_voices <= 0 or not notes:
        return notes
    events = []
    for idx, note in enumerate(notes):
        events.append((note["start"], 1, idx))
        events.append((note["end"], 0, idx))
    events.sort(key=lambda e: (e[0], e[1]))
    active = {}
    dropped = set()
    for _, kind, idx in events:
        if kind == 1:
            active[idx] = notes[idx]["velocity"]
            if len(active) > max_voices:
                weakest = min(active, key=lambda k: active[k])
                dropped.add(weakest)
                active.pop(weakest, None)
        else:
            active.pop(idx, None)
    return [n for i, n in enumerate(notes) if i not in dropped]


# ---------------------------------------------------------------- 单音

def stem_level_db(path: str) -> float:
    """这条 stem 整体有多响（dBFS 峰值）。太低就不用识别了。"""
    import soundfile as sf

    peak = 0.0
    try:
        with sf.SoundFile(path) as handle:
            while True:
                block = handle.read(1 << 18, dtype="float32", always_2d=True)
                if block.size == 0:
                    break
                peak = max(peak, float(np.max(np.abs(block))))
    except Exception:
        return -120.0
    return 20.0 * float(np.log10(peak + 1e-9))


def _note_pitch_from_segment(filled, vprob, loud, s: int, e: int):
    """一段音内部的音高：取稳定帧的加权众数，而不是切分点那一帧。

    以前直接 `round(filled[s])`，s 正好落在换音的那几帧上（滑音/颤音起点），
    所以整首谱会有大量 ±1 半音的错音——这是用户说「音高很多是错的」的主因。
    """
    vals = []
    # 段尾常被下一个音的起音污染（短倚音尤其明显：0.12 s 的音后面跟着大跳，
    # 尾段 30 ms 就够把众数抬半个音）。两端各收 1~2 帧再统计。
    if e - s >= 6:
        s += 1
        e -= 2
    # 更短的音（≤0.22 s，约 19 帧）再狠一点收尾：倚音/转音的尾段基本是
    # 下一颗音的滑入，留 3/4 就够取到本音的音高。
    if e - s <= 19:
        cut = max(2, (e - s) // 4)
        if e - s - cut >= 2:
            e -= cut
    for i in range(max(0, s), min(len(filled), e)):
        if not np.isfinite(filled[i]) or not loud[i]:
            continue
        w = float(vprob[i]) if i < len(vprob) and np.isfinite(vprob[i]) else 0.5
        vals.append((float(filled[i]), w))
    if len(vals) < 2:
        return None
    ps = np.array([v[0] for v in vals])
    ws = np.array([v[1] for v in vals])
    med = float(np.median(ps))
    keep = np.abs(ps - med) <= 0.7  # 抖掉的正是滑音/颤音两侧的怪值
    if keep.sum() >= 2:
        ps, ws = ps[keep], ws[keep]
    bins = np.round(ps).astype(int)
    lo, hi = int(bins.min()), int(bins.max())
    counts = np.zeros(hi - lo + 1)
    for b, w in zip(bins, ws):
        counts[b - lo] += w
    total = float(counts.sum()) or 1.0
    if counts.max() / total < 0.34:  # 势均力敌就退回中位数，别硬选
        return int(round(med)), med, float(ws.mean())
    return int(lo + int(np.argmax(counts))), med, float(ws.mean())


def _fold_octave_outliers(notes: list[dict], window_sec: float = 6.0,
                          limit: float = 9.0, short_sec: float = 0.30) -> list[dict]:
    """单音轨道里偶尔整条被 pyin 判成高低八度，用邻域中位数拉回来。"""
    if len(notes) < 4:
        return notes
    starts = np.array([n["start"] for n in notes], float)
    pitches = np.array([n["pitch"] for n in notes], float)
    for i, note in enumerate(notes):
        near = np.abs(starts - starts[i]) <= window_sec
        near[i] = False
        if near.sum() < 3:
            continue
        ref = float(np.median(pitches[near]))
        diff = note["pitch"] - ref
        if abs(diff) < limit:
            continue
        if (note["end"] - note["start"]) > short_sec and abs(diff) < 14:
            continue  # 长音可能是真高音，别乱动
        shift = -12 if diff > 0 else 12
        candidate = note["pitch"] + shift
        if abs(candidate - ref) < abs(diff):
            note["pitch"] = int(candidate)
    return notes


def transcribe_monophonic(path: str, spec: dict, min_note_sec: float = 0.09,
                          silence_db: float | None = None,
                          hysteresis_frames: int = 3,
                          min_voicing: float = 0.15) -> list[dict]:
    """pyin 基频轨迹 → 音符。适合人声、贝斯、萨克斯这类单线条乐器。

    silence_db 给 None 时按本轨自己的响度定门限——不同 stem 的绝对电平差很多，
    固定 -38dB 会把安静的贝斯轨整条吃掉。
    """
    import librosa

    y, sr = load_audio(path, sr=SR_PITCH, mono=True)
    if len(y) == 0:
        return []
    peak_db = 20.0 * float(np.log10(float(np.max(np.abs(y))) + 1e-9))
    if peak_db < -48.0:
        return []  # 这一轨基本没声音

    lo, hi = spec.get("range", (21, 108))
    fmin = float(440.0 * 2 ** ((lo - 69) / 12.0))
    fmax = float(min(440.0 * 2 ** ((hi - 69) / 12.0), sr / 2.2))

    hop = 256
    frame_length = 2048 if fmin >= 60 else 4096  # 低频要更长的窗才看得出周期
    f0, voiced, vprob = librosa.pyin(
        y, fmin=fmin, fmax=fmax, sr=sr, frame_length=frame_length, hop_length=hop,
        fill_na=np.nan, center=True,
    )
    times = librosa.times_like(f0, sr=sr, hop_length=hop)
    if vprob is None or len(vprob) != len(f0):
        vprob = np.where(np.isfinite(f0), 1.0, 0.0)
    # pyin 的 voiced_prob 没标定过（实测人声轨 voiced 帧中位数才 0.15），
    # 直接用绝对值当门限会把高音/气声全砍掉，这里换成相对置信度。
    vbase = float(np.nanpercentile(vprob[np.isfinite(vprob)], 90)) if np.isfinite(vprob).any() else 1.0
    vrel = vprob / max(vbase, 1e-6)

    rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=hop)[0]
    rms = np.pad(rms, (0, max(0, len(f0) - len(rms))))[: len(f0)]
    db = 20.0 * np.log10(np.maximum(rms, 1e-9))

    if silence_db is None:
        # 相对门限：比这条轨自己的中位音量低 22dB 就算静音，同时不高于 -42
        loud_ref = float(np.percentile(db, 80))
        gate = max(-52.0, min(-30.0, loud_ref - 22.0))
    else:
        gate = float(silence_db)
    loud = db > gate

    midi = np.full(len(f0), np.nan, dtype=float)
    ok = np.isfinite(f0) & (f0 > 0)
    midi[ok] = 69.0 + 12.0 * np.log2(f0[ok] / 440.0)

    # 中值滤波压掉颤音抖动
    filled = _nan_median(midi, 5)

    segments: list[tuple[int, int, int]] = []
    cur_start = None
    cur_pitch = None
    pending_pitch = None
    pending_count = 0
    for i in range(len(filled)):
        voiced_now = (np.isfinite(filled[i]) and loud[i]
                      and (not np.isfinite(vrel[i]) or vrel[i] >= min_voicing))
        if not voiced_now:
            if cur_start is not None:
                segments.append((cur_start, i, int(cur_pitch)))
                cur_start = None
                cur_pitch = None
            pending_pitch, pending_count = None, 0
            continue
        p = int(round(filled[i]))
        if cur_start is None:
            cur_start, cur_pitch = i, p
            pending_pitch, pending_count = None, 0
            continue
        if p == cur_pitch:
            pending_pitch, pending_count = None, 0
            continue
        # 换音要连续几帧才算数，免得颤音/滑音把音切碎
        if p == pending_pitch:
            pending_count += 1
        else:
            pending_pitch, pending_count = p, 1
        if pending_count >= hysteresis_frames:
            segments.append((cur_start, i - pending_count + 1, int(cur_pitch)))
            cur_start, cur_pitch = i - pending_count + 1, p
            pending_pitch, pending_count = None, 0

    if cur_start is not None:
        segments.append((cur_start, len(filled) - 1, int(cur_pitch)))

    notes: list[dict] = []
    for s, e, _prov in segments:
        refined = _note_pitch_from_segment(filled, vrel, loud, s, e)
        if refined is None:
            continue
        pitch, _med, voicing = refined
        note = _mk_note(times, s, e, pitch, db)
        note["pitch"] = int(pitch)
        note["voicing"] = round(float(voicing), 3)
        notes.append(note)

    out = []
    for note in notes:
        if note["end"] - note["start"] < min_note_sec:
            continue
        if note.get("voicing", 1.0) < min_voicing:
            continue
        if not (lo - 2 <= note["pitch"] <= hi + 2):
            continue
        out.append(note)
    out = _merge_same_pitch(out)
    out = _fold_octave_outliers(out)
    # 同一个音高连敲/连唱几次，pyin 只会给一条长音，得靠起音切开
    onsets = _onset_times(y, sr, hop)
    out = _split_at_onsets(out, onsets, min_note_sec, y=y, sr=sr, hop=hop)
    # 换音处的起点：pyin 要等滑音走到位才认新音，会把起音往后拖 50–150 ms。
    # 用起音包络把起点拉回真正的 attack，顺带标连音（slur）与倚音（grace）。
    out = _snap_starts_to_onsets(out, onsets)
    out = _mark_articulation(out, onsets)
    out = _absorb_passing(out)
    out = [n for n in out if n["end"] - n["start"] >= 0.04]
    return out


def _absorb_passing(notes: list[dict], max_dur: float = 0.11,
                    step: int = 4) -> list[dict]:
    """吃掉「转音中途」的过渡音。

    人声从 A 滑到 B 时 pyin 会切出一小段中间音高（70/71 这种），时值只有 80 ms，
    既不是倚音也不是真音符，留在谱上就是一堆碎音。判据：很短 + 夹在前后两个音之间
    （音高各差不超过 4 个半音）→ 并进前一个音，并把后一个音标成 slur（滑音接入）。
    """
    if len(notes) < 2:
        return notes
    out: list[dict] = []
    i = 0
    pending_slur = False
    while i < len(notes):
        n = notes[i]
        dur = float(n["end"]) - float(n["start"])
        prev = out[-1] if out else None
        nxt = notes[i + 1] if i + 1 < len(notes) else None
        if (dur < max_dur and prev is not None and nxt is not None
                and abs(int(n["pitch"]) - int(prev["pitch"])) <= step
                and abs(int(nxt["pitch"]) - int(n["pitch"])) <= step):
            prev["end"] = max(float(prev["end"]), float(n["end"]))
            pending_slur = True
            i += 1
            continue
        if pending_slur:
            n = dict(n)
            n["slur_in"] = True
            pending_slur = False
        out.append(n)
        i += 1
    return out


def _snap_starts_to_onsets(notes: list[dict], onsets, back: float = 0.34,
                           ahead: float = 0.02) -> list[dict]:
    """把每个音的起点拉到最近的起音峰上。

    pyin 是「等滑音走到新音高才确认换音」，所以带转音/滑音的句子起音会偏晚
    50–150 ms，量化到网格时容易整条挪到下一格。用能量起音峰把起点拉回来。
    """
    if not notes or not onsets:
        return notes
    peaks = sorted(float(t) for t in onsets)
    for note in notes:
        start = float(note["start"])
        best = None
        for t in peaks:
            if t < start - back:
                continue
            if t > start + ahead:
                break
            if best is None or abs(t - start) < abs(best - start):
                best = t
        if best is not None:
            note["start"] = round(best, 4)
    notes.sort(key=lambda n: (n["start"], n["pitch"]))
    for a, b in zip(notes, notes[1:]):
        if b["start"] <= a["start"]:
            b["start"] = round(a["start"] + 0.04, 4)
        if b["start"] < a["end"]:
            a["end"] = round(b["start"], 4)
    return [n for n in notes if n["end"] - n["start"] >= 0.04]


def _mark_articulation(notes: list[dict], onsets, tol: float = 0.09,
                       grace_max: float = 0.18) -> list[dict]:
    """标连音（slur_in）与倚音（grace），给简谱/MusicXML 画连音线和装饰音用。

    连音线的判据不是「有没有起音峰」——换音高本身就会让 spectral flux 冒一个峰，
    按起音判会把连音全判成新起音。这里只看「中间有没有断」：两个音几乎贴着
    （gap ≤ 0.05 s）就算连音，中间断开（换气/断奏）就不算。
    """
    if not notes:
        return notes
    for cur, nxt in zip(notes, notes[1:]):
        gap = float(nxt["start"]) - float(cur["end"])
        if gap <= 0.05 and int(nxt["pitch"]) != int(cur["pitch"]):
            nxt["slur_in"] = True
    for prev, cur, nxt in zip(notes, notes[1:], notes[2:]):
        dur = float(cur["end"]) - float(cur["start"])
        gap = float(nxt["start"]) - float(cur["end"])
        if (dur <= grace_max and gap <= 0.03
                and float(nxt["end"]) - float(nxt["start"]) >= 0.2
                and int(cur["pitch"]) != int(prev["pitch"])
                and abs(int(cur["pitch"]) - int(prev["pitch"])) <= 2):
            cur["grace"] = True
    return notes


def _onset_times(y, sr: int, hop: int) -> list:
    import librosa

    try:
        frames = librosa.onset.onset_detect(
            y=y, sr=sr, hop_length=hop, backtrack=False, units="frames",
            delta=0.045, wait=3,
        )
    except Exception:
        return []
    return [float(t) for t in librosa.frames_to_time(frames, sr=sr, hop_length=hop)]


def _split_at_onsets(notes: list[dict], onsets, min_sec: float = 0.08, y=None,
                     sr: int = 22050, hop: int = 256, jump: float = 1.3) -> list[dict]:
    """长音内部如果有「真的重新起音」，就切成多个同音高的音（重复音）。

    只按 onset_detect 切会把颤音/揉弦也切开，所以还要看能量有没有跳一下。
    """
    if not notes:
        return notes
    times = sorted(float(t) for t in onsets) if onsets else []
    if not times:
        return notes

    env = None
    if y is not None and len(y):
        import librosa

        env = librosa.feature.rms(y=y, frame_length=1024, hop_length=hop)[0]

    def real_attack(t: float) -> bool:
        if env is None:
            return True
        fps = sr / hop
        centre = int(t * fps)
        before = env[max(0, centre - int(0.10 * fps)): max(1, centre - int(0.02 * fps))]
        after = env[centre: centre + int(0.08 * fps)]
        if before.size == 0 or after.size == 0:
            return True
        base = float(np.mean(before)) + 1e-9
        return float(np.max(after)) / base >= jump

    guard = max(0.05, min_sec * 1.2)
    out: list[dict] = []
    for note in notes:
        start, end = note["start"], note["end"]
        cuts = [t for t in times if start + guard < t < end - guard and real_attack(t)]
        if not cuts:
            out.append(dict(note))
            continue
        points = [start] + cuts + [end]
        for a, b in zip(points, points[1:]):
            if b - a < min_sec:
                continue
            piece = dict(note)
            piece["start"], piece["end"] = round(a, 4), round(b, 4)
            # 标记是被起音切开的重复音，后面量化时别再merge回一条长音
            piece["retrigger"] = True
            out.append(piece)
    return out


def _merge_same_pitch(notes: list[dict], gap_sec: float = 0.06) -> list[dict]:
    """相邻同音高、间隔很小的音合成一个（连音/换气造成的碎音）。"""
    out: list[dict] = []
    for note in notes:
        if out and out[-1]["pitch"] == note["pitch"] and note["start"] - out[-1]["end"] <= gap_sec:
            out[-1]["end"] = note["end"]
            out[-1]["velocity"] = max(out[-1]["velocity"], note["velocity"])
        else:
            out.append(dict(note))
    return out


def _nan_median(values: np.ndarray, width: int) -> np.ndarray:
    if len(values) == 0:
        return values
    half = width // 2
    out = values.copy()
    for i in range(len(values)):
        window = values[max(0, i - half): i + half + 1]
        window = window[np.isfinite(window)]
        if len(window):
            out[i] = float(np.median(window))
    return out


def _mk_note(times, start_idx, end_idx, pitch, db) -> dict:
    start = float(times[start_idx])
    end = float(times[min(end_idx, len(times) - 1)])
    if end <= start:
        end = start + 0.05
    level = float(np.max(db[start_idx: max(start_idx + 1, end_idx)]))
    vel = float(np.clip((level + 50.0) / 45.0, 0.1, 1.0))
    return {
        "start": round(start, 4),
        "end": round(end, 4),
        "pitch": int(pitch),
        "velocity": round(vel, 3),
    }


# ---------------------------------------------------------------- 打击乐

def transcribe_drums(path: str, delta: float = 0.035) -> list[dict]:
    """起音检测 + 频段特征打分，分成 底鼓/军鼓/踩镲/开镲/通鼓/吊镲。"""
    import librosa

    y, sr = load_audio(path, sr=22050, mono=True)
    hop = 256
    # 前面垫一点静音：onset_strength 靠帧间差分，第一拍会被漏掉
    pad = int(0.08 * sr)
    padded = np.concatenate([np.zeros(pad, dtype=y.dtype), y])
    onset_env = librosa.onset.onset_strength(y=padded, sr=sr, hop_length=hop)
    frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, hop_length=hop, units="frames",
        delta=delta, wait=4,
    )
    pad_frames = pad / hop
    S = np.abs(librosa.stft(padded, n_fft=2048, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)

    def band_energy(spectrum, lo, hi):
        mask = (freqs >= lo) & (freqs < hi)
        if not mask.any():
            return 0.0
        return float(spectrum[mask].sum())

    notes = []
    for frame in frames:
        frame = int(frame)
        t = float(librosa.frames_to_time(frame, sr=sr, hop_length=hop)) - pad / sr
        if t < -0.02:
            continue
        t = max(0.0, t)
        spec = S[:, max(0, frame): frame + 3].max(axis=1)
        total = float(spec.sum()) + 1e-9
        low = band_energy(spec, 25, 110)
        lowmid = band_energy(spec, 110, 300)
        mid = band_energy(spec, 300, 1200)
        highmid = band_energy(spec, 1200, 4500)
        high = band_energy(spec, 4500, 11000)
        centroid = float((freqs * spec).sum() / total)

        # 衰减长度：往后 0.5s 内能量掉到峰值的 10% 需要多久
        decay = _decay_time(S, freqs, frame, sr, hop)

        # 一个时间点上可以同时响好几件鼓（底鼓+踩镲是常态），所以是多标签而不是选一个
        kinds = []
        wide = (mid + highmid) / total
        if low / total > 0.34:
            kinds.append("kick")
        if wide > 0.30 and low / total < 0.6 and centroid > 700:
            kinds.append("snare")
        # 踩镲和军鼓都是"宽带噪音"，光看高频占比会把军鼓也判成镲。
        # 实测：闭镲 hi/(hm+hi)≈0.82、衰减 0.05s；军鼓 ≈0.64、衰减 0.15s。
        # 所以要求高频比很高 + 衰减短，才算镲；军鼓的噪音中心更低、拖尾更长。
        band_top = high / (high + highmid + 1e-9)
        if band_top > 0.70 and centroid > 3000 and high / total > 0.25 and wide > 0.20:
            kinds.append("openhat" if decay > 0.12 else "hat")
        if high / total > 0.2 and decay > 0.42 and centroid > 3000 and "hat" in kinds:
            kinds.append("crash")
        if not kinds:
            if lowmid / total > 0.34 and 150 < centroid < 900:
                kinds.append("tom3" if centroid < 320 else ("tom2" if centroid < 520 else "tom1"))
            elif wide > 0.25:
                kinds.append("snare")
            elif low / total > 0.2:
                kinds.append("kick")
            else:
                kinds.append("hat")
        kinds = kinds[:3]

        level = float(onset_env[min(frame, len(onset_env) - 1)]) if len(onset_env) else 1.0
        vel = float(np.clip(level / 4.0, 0.25, 1.0))
        for kind in kinds:
            notes.append({
                "start": round(t, 4),
                "end": round(t + (0.02 if kind in ("hat", "snare", "kick") else 0.1), 4),
                "pitch": 0,
                "drum": kind,
                "velocity": round(vel, 3),
                "decay": round(decay, 3),
                "centroid": round(centroid, 1),
            })

    # 去掉过于密集的重复起音（同一类 25ms 内只留一个）
    notes.sort(key=lambda n: n["start"])
    cleaned = []
    for note in notes:
        if cleaned and note["drum"] == cleaned[-1]["drum"] and note["start"] - cleaned[-1]["start"] < 0.025:
            continue
        cleaned.append(note)
    return cleaned


def _decay_time(S, freqs, frame, sr, hop) -> float:
    seg = S[:, frame: min(S.shape[1], frame + int(0.8 * sr / hop))]
    if seg.shape[1] < 3:
        return 0.0
    energy = seg.sum(axis=0)
    peak = float(energy.max()) + 1e-9
    thresh = peak * 0.12
    below = np.where(energy < thresh)[0]
    if len(below) == 0:
        return float(seg.shape[1] * hop / sr)
    return float(below[0] * hop / sr)


# ---------------------------------------------------------------- 入口

def transcribe_stem(path: str, instrument: str) -> list[dict]:
    from theory import instrument_spec

    spec = instrument_spec(instrument)
    kind = spec["kind"]
    if stem_level_db(path) < -48.0:
        return []
    if kind == "drums":
        return transcribe_drums(path)
    if kind == "mono":
        return transcribe_monophonic(path, spec)
    # basic-pitch 的灵敏度可以用环境变量临时调，方便离线评测找最优阈值
    onset_th = float(os.environ.get("SCORE_POLY_ONSET", "0.5"))
    frame_th = float(os.environ.get("SCORE_POLY_FRAME", "0.3"))
    max_voices = int(os.environ.get("SCORE_POLY_MAX", "6"))
    return transcribe_polyphonic(path, spec, onset_threshold=onset_th,
                                 frame_threshold=frame_th, max_polyphony=max_voices)
