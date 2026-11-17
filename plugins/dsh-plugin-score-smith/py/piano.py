"""用 Windows 自带的 GM 音源（gm.dls）采样渲染钢琴声。

用户明确说过：不要自己用振荡器硬合成，要「借钢琴的音」。Windows 的
`C:\\Windows\\System32\\drivers\\gm.dls` 就是一个 3.4 MB 的 DLS 音色库，
里面的 Acoustic Grand Piano 是**真钢琴采样**（每几个半音一个样本，按音高变调播放）。

这个模块只做两件事：
1. `load_piano()` → 从 DLS 里抠出钢琴的采样与键位映射；
2. `render_notes()` → 把 score.json 的音符排成一段真实钢琴音频。

DLS 结构（RIFF）：
    RIFF DLS
      colh                     连接信息
      ptbl                     样本表（cue 点）
      wvpl  → wave*            波形池，每个 wave 里是 fmt + data（PCM16）
      lins  → ins *            乐器表
        insh                    乐器头
        rgn2 / rgn *            区域（键位范围）
          rgnh                  键位/力度范围
          wsmp                  采样参数（usUnityNote=录音音高、sFineTune、lAttenuation）
          wlnk                  指向 ptbl 的样本索引
"""

from __future__ import annotations

import os
import struct

import numpy as np

DEFAULT_DLS = r"C:\Windows\System32\drivers\gm.dls"


class DlsError(RuntimeError):
    pass


def _read_chunks(buf: bytes, start: int, end: int):
    """遍历 RIFF 子块，产出 (id, 内容起点, 内容长度)。"""
    pos = start
    while pos + 8 <= end:
        cid = buf[pos:pos + 4].decode("latin-1")
        size = struct.unpack_from("<I", buf, pos + 4)[0]
        body = pos + 8
        yield cid, body, size
        pos = body + size + (size & 1)


def _find(buf: bytes, start: int, end: int, cid: str):
    for i, body, size in _read_chunks(buf, start, end):
        if i == cid:
            return body, size
    return None, None


def _find_list(buf: bytes, start: int, end: int, form: str):
    """找 LIST 块：DLS 里乐器表和波形池都是 LIST <form> 的形式。"""
    for i, body, size in _read_chunks(buf, start, end):
        if i != "LIST" or size < 4:
            continue
        if buf[body:body + 4].decode("latin-1") == form:
            return body + 4, size - 4
    return None, None


def _parse_wave(buf: bytes, body: int, size: int):
    fmt = None
    data = None
    for cid, b, n in _read_chunks(buf, body, body + size):
        if cid == "fmt ":
            fmt = buf[b:b + n]
        elif cid == "data":
            data = buf[b:b + n]
    if fmt is None or data is None or len(fmt) < 16:
        return None
    tag, channels, rate, _avg, _align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if bits != 16 or tag not in (1, 0xFFFE):
        return None
    arr = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)
    return {"rate": rate, "pcm": arr}


def _parse_region(buf: bytes, body: int, size: int):
    key_lo, key_hi, vel_lo, vel_hi = 0, 127, 0, 127
    unity = 60
    fine = 0
    atten = 0
    wlink = None
    for cid, b, n in _read_chunks(buf, body, body + size):
        if cid == "rgnh" and n >= 12:
            key_lo, key_hi, vel_lo, vel_hi = struct.unpack_from("<HHHH", buf, b)
        elif cid == "wsmp" and n >= 20:
            # wsmp：cbSize(I) usUnityNote(H) sFineTune(h，有符号！) lAttenuation(i) fulOptions(I) cSampleLoops(I)
            # 之前把 sFineTune 当无符号读，-4 变成 65532 → 倍率 2^655 直接静音（76/79/84 全哑）。
            _cb, us_unity, s_fine, l_att, _opts, loops = struct.unpack_from("<IHhiII", buf, b)
            unity = us_unity
            fine = s_fine
            atten = l_att
        elif cid == "wlnk" and n >= 12:
            # DLS1：三个 ULONG，最后一个是 ptbl 里的样本序号
            _a, _b2, table_index = struct.unpack_from("<III", buf, b)
            wlink = table_index
    return {
        "key_lo": key_lo,
        "key_hi": key_hi,
        "vel_lo": vel_lo,
        "vel_hi": vel_hi,
        "unity": unity,
        "fine": fine,
        "atten": atten,
        "wave": wlink,
    }


def load_dls(path: str = DEFAULT_DLS):
    if not os.path.exists(path):
        raise DlsError(f"找不到 DLS 音色库：{path}")
    buf = open(path, "rb").read()
    if buf[:4] != b"RIFF":
        raise DlsError("不是 RIFF 文件")
    form = buf[8:12]
    if form not in (b"DLS ", b"ds64"):
        raise DlsError(f"不是 DLS 文件（form={form!r}）")

    # 样本池
    wvpl_body, wvpl_size = _find_list(buf, 12, len(buf), "wvpl")
    waves = []
    if wvpl_body is not None:
        for cid, b, n in _read_chunks(buf, wvpl_body, wvpl_body + wvpl_size):
            if cid == "wave":                      # 少数库直接放 wave 块
                w = _parse_wave(buf, b, n)
            elif cid == "LIST" and n >= 4 and buf[b:b + 4] in (b"wave", b"WAVE"):
                w = _parse_wave(buf, b + 4, n - 4)   # gm.dls 实际是 LIST 'wave'
            else:
                continue
            if w:
                waves.append(w)

    # 乐器：lins 里的每个 LIST 是一个 instrument（form 'ins '），
    # instrument 里的每个 LIST 是一个 region（form 'rgn ' 或 'rgn2'）
    lins_body, lins_size = _find_list(buf, 12, len(buf), "lins")
    instruments = []
    if lins_body is not None:
        for cid, b, n in _read_chunks(buf, lins_body, lins_body + lins_size):
            if cid != "LIST" or n < 4:
                continue
            if buf[b:b + 4].decode("latin-1", "replace") != "ins ":
                continue
            regions = []
            # ins → LIST 'lrgn' → LIST 'rgn ' / 'rgn2'
            for cid2, b2, n2 in _read_chunks(buf, b + 4, b + n):
                if cid2 != "LIST" or n2 < 4:
                    continue
                if buf[b2:b2 + 4].decode("latin-1", "replace") != "lrgn":
                    continue
                for cid3, b3, n3 in _read_chunks(buf, b2 + 4, b2 + n2):
                    if cid3 != "LIST" or n3 < 4:
                        continue
                    if buf[b3:b3 + 4].decode("latin-1", "replace").startswith("rgn"):
                        regions.append(_parse_region(buf, b3 + 4, n3 - 4))
            if regions:
                instruments.append(regions)
    return {"waves": waves, "instruments": instruments}


class Piano:
    """把 DLS 里的钢琴做成一个能按音高触发的采样器。"""

    def __init__(self, path: str = DEFAULT_DLS, instrument_index: int = 0):
        bank = load_dls(path)
        waves = bank["waves"]
        insts = bank["instruments"]
        if not insts:
            raise DlsError("DLS 里没有乐器")
        inst = insts[min(instrument_index, len(insts) - 1)]
        regions = []
        for r in inst:
            idx = r.get("wave")
            if idx is None or idx >= len(waves):
                continue
            w = waves[idx]
            if w["pcm"].size < 64:
                continue
            regions.append({**r, "pcm": w["pcm"], "rate": w["rate"]})
        if not regions:
            raise DlsError("没有可用的钢琴采样")
        self.regions = regions
        self.path = path

    def _pick(self, pitch: int):
        best = None
        for r in self.regions:
            if r["key_lo"] <= pitch <= r["key_hi"] and r["vel_lo"] <= 100 <= r["vel_hi"]:
                return r
        for r in self.regions:  # 力度不匹配就退到只比键位
            if r["key_lo"] <= pitch <= r["key_hi"]:
                best = r
                break
        if best is not None:
            return best
        return min(self.regions, key=lambda r: abs((r["key_lo"] + r["key_hi"]) / 2 - pitch))

    def sample_for(self, pitch: int):
        """返回 (波形, 播放倍率)。倍率 = 实际音高 / 录音音高。"""
        r = self._pick(pitch)
        semis = (pitch - r["unity"]) + (r["fine"] / 100.0)
        return r["pcm"], float(2.0 ** (semis / 12.0)), r


def _resample(pcm: np.ndarray, ratio: float, out_len: int) -> np.ndarray:
    """按倍率画出 out_len 个点（线性插值），ratio>1 表示音更高、放得更快。"""
    if out_len <= 0:
        return np.zeros(0, dtype=np.float32)
    idx = np.arange(out_len, dtype=np.float64) * ratio
    idx = idx[idx < pcm.size - 1]
    if idx.size == 0:
        return np.zeros(0, dtype=np.float32)
    i0 = idx.astype(np.int64)
    frac = (idx - i0).astype(np.float32)
    out = pcm[i0] * (1.0 - frac) + pcm[i0 + 1] * frac
    return out


def _envelope(n: int, sr: int, attack: float = 0.006, release: float = 0.10) -> np.ndarray:
    """钢琴包络：极短起音 + 指数衰减（采样本身已经带衰减，这里只修尾巴和收尾）。"""
    env = np.ones(n, dtype=np.float32)
    a = max(1, int(attack * sr))
    if a < n:
        env[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32)
    rel = max(1, int(release * sr))
    if rel < n:
        env[-rel:] *= np.linspace(1.0, 0.0, rel, dtype=np.float32) ** 1.5
    return env


def render_notes(notes: list[dict], tempo: float, sr: int = 44100,
                 gain: float = 0.32, piano: Piano | None = None,
                 max_seconds: float | None = None) -> np.ndarray:
    """把音符渲染成钢琴音频（单声道 float32）。notes 用 score.json 的字段。"""
    piano = piano or Piano()
    sec_per_ql = 60.0 / float(tempo)
    if not notes:
        return np.zeros(int(sr * 0.5), dtype=np.float32)

    end_ql = max(float(n["offsetQL"]) + float(n["durationQL"]) for n in notes)
    total = end_ql * sec_per_ql + 2.0
    if max_seconds:
        total = min(total, float(max_seconds))
    out = np.zeros(int(total * sr) + 1, dtype=np.float32)

    for n in notes:
        pitch = int(n["pitch"])
        start = float(n["offsetQL"]) * sec_per_ql
        dur = max(0.05, float(n["durationQL"]) * sec_per_ql)
        vel = float(n.get("velocity") or 0.8)
        pcm, ratio, _r = piano.sample_for(pitch)
        # 采样率也要算进去：gm.dls 是 22050，输出是 44100，
        # 只按音高倍率播放会整条高一个八度（之前评测里钢琴全判成 +12 半音就是这个）。
        ratio = float(ratio) * float(_r.get("rate") or sr) / float(sr)
        want = int((dur + 0.35) * sr)              # 尾巴留一点自然衰减
        seg = _resample(pcm, ratio, want)
        if seg.size == 0:
            continue
        seg = seg * _envelope(seg.size, sr) * (gain * (0.45 + 0.55 * min(1.0, vel)))
        i = int(start * sr)
        j = min(out.size, i + seg.size)
        if i >= out.size:
            continue
        out[i:j] += seg[: j - i]

    peak = float(np.max(np.abs(out))) or 1.0
    if peak > 0.95:
        out *= 0.95 / peak
    return out


def write_audio(samples: np.ndarray, path: str, sr: int = 44100):
    import soundfile as sf

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    sf.write(path, np.clip(samples, -1.0, 1.0), sr)
    return path


def self_test():
    p = Piano()
    info = {
        "dls": p.path,
        "regions": len(p.regions),
        "keys": [min(r["key_lo"] for r in p.regions), max(r["key_hi"] for r in p.regions)],
        "rates": sorted({r["rate"] for r in p.regions}),
        "samples": [int(r["pcm"].size) for r in p.regions][:6],
    }
    notes = [{"offsetQL": i, "durationQL": 1, "pitch": 60 + i, "velocity": 0.8} for i in range(8)]
    audio = render_notes(notes, tempo=120, piano=p)
    info["peak"] = float(np.max(np.abs(audio)))
    info["seconds"] = round(audio.size / 44100.0, 2)
    return info


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), ensure_ascii=False, indent=2))
