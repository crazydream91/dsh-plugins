"""小晏的声音 —— 定版入口（2026-10-30 选定 ① 版声线；11-05 换参考修停顿）。

默认参数已经固化成"冰雪 + 慈爱、咬字轻"的听感，直接：

    python ayan.py "别怕，风雪再大，也在我怀里。"
    python ayan.py "第一句。第二句。" --out tts/out/demo.wav --stitch

参考音频默认用 ref3/stitch-02.wav（7.53 s 的紧凑拼接），不再用 ref2/clip-06.wav ——
后者 8 秒里夹着 1 秒多的大停顿，模型会照着学，每句都被塞进一秒多的空白。

默认还会走一遍节奏修正（rhythm.py）：掐首尾静音，并把句内超过 --pause-cap 的空白
压到 --pause-target。想要原样输出就加 --no-rhythm。
"""
import argparse
import difflib
import random
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
os.environ.setdefault("HF_HOME", str(ROOT / "hf"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import _boot  # noqa: F401,E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from f5_tts.api import F5TTS  # noqa: E402

from master import build_filter  # noqa: E402
import rhythm  # noqa: E402

VOCOS = ROOT / "hf/models--charactr--vocos-mel-24khz/snapshots/0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21"
# 微调之后的权重；想退回官方底模就把 --ckpt 指到 pretrained_model_1250000.safetensors
CKPT = ROOT / "tts/venv/Lib/ckpts/ayan/model_last.pt"
# 参考音频换成了「紧凑拼接版」：7.53 秒、只有 3 处 ≤230ms 的小停顿。
# 旧参考 ref2/clip-06.wav 8 秒里夹着 1.1/1.2/1.7 秒的大停顿，模型照着学，
# 每句中间都被塞进一秒多的空白 —— 这就是「停顿异常」的病根。
REF = ROOT / "ref3/stitch-02.wav"
TRANSCRIPTS = [ROOT / "ref2/transcripts.json", ROOT / "ref3/transcripts.json"]
FF = ROOT / "bin/ffmpeg.exe"

# ① 版参数（用户 2026-10-30 选定）
NFE, CFG, SPEED = 64, 1.6, 0.90
SOFT, AIR = 1.35, 1.15


def ref_text(path: Path) -> str:
    for f in TRANSCRIPTS:
        try:
            hit = json.loads(f.read_text(encoding="utf-8")).get(path.name, "")
        except Exception:
            continue
        if hit:
            return hit
    return ""


def split_clauses(text: str):
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？!?；;\n":
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out or [text]


def norm_text(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum())


_ASR = None


def transcribe(path: Path) -> str:
    """本地 whisper 听回来，用来核对有没有多字少字。"""
    res = asr_pipe()(str(path), generate_kwargs={"language": "chinese", "task": "transcribe"},
                     return_timestamps=False)
    return res["text"].strip()


def transcribe_words(path: Path):
    """逐词带时间戳：用来掐掉句首多出来的那半个字、以及对齐标点。

    注意必须给 chunk_length_s：transformers 的 pipeline 只有在分块推理时才会
    真的吐出逐词时间戳，否则整句只会返回一个 chunk，时间戳是假的（2026-11-07 踩过）。
    """
    res = asr_pipe()(str(path), generate_kwargs={"language": "chinese", "task": "transcribe"},
                     return_timestamps="word", chunk_length_s=30, stride_length_s=5)
    return [(c["text"].strip(), c.get("timestamp")) for c in res.get("chunks", [])]


def trim_leading(wav, sr, words, want: str):
    """句首多了字就把它连同前面的时间一起切掉。

    F5 偶尔会在开头吐半个字（"人今天风挺大的…"）。whisper 的逐词时间戳能告诉我们
    多出来的那个字落在哪一小段，把它切掉比换种子重来稳得多。
    """
    if not words:
        return wav, ""
    texts = [w[0] for w in words]
    joined = norm_text("".join(texts))
    if joined == want or not joined:
        return wav, joined

    best_k, best_hit = 0, -1.0
    for k in range(len(words)):
        cand = norm_text("".join(texts[k:]))
        if not cand:
            continue
        hit = 1.0 if cand == want else difflib.SequenceMatcher(None, cand, want).ratio()
        if hit > best_hit + 1e-9:
            best_hit, best_k = hit, k
        if hit >= 1.0:
            break
    if best_k == 0 or best_hit <= difflib.SequenceMatcher(None, joined, want).ratio():
        return wav, joined

    ts = words[best_k][1]
    start = ts[0] if isinstance(ts, (list, tuple)) and ts[0] is not None else None
    if start is None:
        return wav, joined
    cut = max(0, int((start - 0.03) * sr))
    if cut <= 0 or cut >= len(wav):
        return wav, joined
    print(f"[trim] 切掉句首 {best_k} 个词（{''.join(texts[:best_k])}），-{cut / sr:.2f}s", flush=True)
    return wav[cut:], joined


_ASR = None


def asr_pipe():
    global _ASR
    if _ASR is None:
        import torch
        from transformers import pipeline
        _ASR = pipeline("automatic-speech-recognition",
                        model=str(ROOT / "models/whisper-large-v3-turbo"),
                        device=0 if torch.cuda.is_available() else -1)
    return _ASR


def ref_of(a):
    return Path(a.ref) if Path(a.ref).is_absolute() else ROOT / a.ref


def ref_txt_of(a):
    return a.text_ref or ref_text(ref_of(a))


def synth_clause(f5, ref: Path, ref_txt: str, text: str, a):
    """合成一句；听回来自查，多字少字就换种子重来；句首多的字直接掐掉。"""
    want = norm_text(text)

    def render(seed, tmp):
        wav, sr, _ = f5.infer(ref_file=str(ref), ref_text=ref_txt, gen_text=text,
                              nfe_step=a.nfe, cfg_strength=a.cfg, speed=a.speed,
                              remove_silence=True, seed=seed, file_wave=str(tmp),
                              show_info=print if a.verify > 0 and seed is None else (lambda *x, **y: None))
        return np.asarray(wav, dtype=np.float32), sr

    if a.verify <= 0:
        tmp = ROOT / "tts/out/_check.wav"
        wav, sr = render(a.seed, tmp)
        if a.fix_lead:
            wav, _ = trim_leading(wav, sr, transcribe_words(tmp), want)
        tmp.unlink(missing_ok=True)
        return wav, sr

    tmp = ROOT / "tts/out/_check.wav"
    best, best_score = None, -1.0
    for k in range(a.verify):
        seed = None if a.seed is None else a.seed + k * 7
        wav, sr = render(seed, tmp)
        if a.fix_lead:
            wav, got = trim_leading(wav, sr, transcribe_words(tmp), want)
        else:
            got = norm_text(transcribe(tmp))
        hit = 1.0 if got == want else difflib.SequenceMatcher(None, got, want).ratio()
        print(f"[check {k + 1}/{a.verify}] {got}  (一致度 {hit:.0%})", flush=True)
        if hit > best_score:
            best_score, best = hit, (wav, sr)
        if hit >= 1.0:
            break
    tmp.unlink(missing_ok=True)
    return best


def apply_mood(a):
    """把 --mood 折算成停顿倍数、语速、抖动，挂回同一份参数上。"""
    m = rhythm.mood_conf(getattr(a, "mood", "") or "calm")
    a.punct_scale = getattr(a, "punct_scale", 1.0) * m["scale"]
    a.speed = getattr(a, "speed", SPEED) * m["speed"]
    a.jitter = m["jitter"]
    a.comma_extra = m["comma_extra"]
    a.sentence_mul = m["sentence"]
    return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--out", default="tts/out/ayan.wav")
    ap.add_argument("--batch", default="",
                    help="文本文件批量合成：一行一句，也可写「名字|句子」；--out 当输出目录用")
    ap.add_argument("--ref", default=str(REF))
    ap.add_argument("--text_ref", default="")
    ap.add_argument("--ckpt", default=str(CKPT))
    ap.add_argument("--no-ema", dest="use_ema", action="store_false",
                    help="用训练出来的原始权重（model_state_dict）而不是 EMA 权重——继续微调后 EMA 还没跟上时用这个")
    ap.add_argument("--nfe", type=int, default=NFE)
    ap.add_argument("--cfg", type=float, default=CFG)
    ap.add_argument("--speed", type=float, default=SPEED)
    ap.add_argument("--soft", type=float, default=SOFT)
    ap.add_argument("--air", type=float, default=AIR)
    ap.add_argument("--stitch", action="store_true", help="多句时逐句合成再拼接")
    ap.add_argument("--clause", action="store_true", default=False,
                    help="按小句（逗号/分号/句号）分别合成，再用标点规定的停顿拼起来——"
                         "停顿是句间的，不从字里抠；短句单独念容易念错时慎用")
    ap.add_argument("--seg-gate", type=float, default=0.85,
                    help="朗读谱模式下，某段回听一致度低于这个值就往后并一段重念（默认 0.85）")
    ap.add_argument("--segplan", default="",
                    help="朗读谱 JSON（segplan.py 产）：把谱里的断点写成标点再整句合成，停顿只在已有静音里微调（不切字）")
    ap.add_argument("--seglocal", action="store_true", default=False,
                    help="不联网，用本地断句器（seglocal.py，从 Kimi 的谱里学出来的）现场出一份谱")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--verify", type=int, default=0,
                    help="用本地 whisper 听回来核对，多字少字就换种子重试，最多 N 次")
    ap.add_argument("--raw", action="store_true", help="不做后期，只要干声")
    ap.add_argument("--rhythm", action="store_true", default=True,
                    help="掐掉首尾静音，并把句内超过 --pause-cap 的空白压到 --pause-target（默认开）")
    ap.add_argument("--no-rhythm", dest="rhythm", action="store_false")
    ap.add_argument("--punct-pause", dest="punct_pause", action="store_true", default=True,
                    help="按标点在音频里补停顿长度（默认开；逗号 0.30–0.40s，分号 0.38–0.50s，句末 0.60–0.85s）")
    ap.add_argument("--no-punct-pause", dest="punct_pause", action="store_false")
    ap.add_argument("--punct-scale", type=float, default=1.0,
                    help="标点停顿整体倍数（1.0 定版；觉得急就 1.25–1.5，觉得拖就 0.8）")
    ap.add_argument("--mood", default="calm",
                    choices=list(rhythm.MOODS) + [""],
                    help="情绪档：calm 平静 / gentle 温柔 / excited 兴奋 / sad 低落 / stern 严肃 / coy 撒娇；决定停顿长短与语速")
    ap.add_argument("--pause-plan", default="",
                    help="每个标点停多少毫秒，按标点顺序逗号分隔，例如 300,420,680")
    ap.add_argument("--pause-force", action="store_true", default=False,
                    help="标点处找不到安静落点时也硬插（可能劈字，插完 ASR 复核不行会自动退回）")
    ap.add_argument("--pause-retry", type=int, default=0,
                    help="有标点因为找不到安静落点而没补上时，换种子重合成最多 N 次，取停顿覆盖最好的一遍")
    ap.add_argument("--pause-cap", type=float, default=0.50,
                    help="句内停顿超过这个长度就压（秒）")
    ap.add_argument("--pause-target", type=float, default=0.24,
                    help="压到多长（秒），留一点换气感")
    ap.add_argument("--fix-lead", action="store_true", default=True,
                    help="句首多字就按逐词时间戳掐掉（默认开）")
    ap.add_argument("--no-fix-lead", dest="fix_lead", action="store_false")
    a = ap.parse_args()
    if getattr(a, "segplan", ""):
        sp = Path(a.segplan) if Path(a.segplan).is_absolute() else ROOT / a.segplan
        a.segplan = json.loads(sp.read_text(encoding="utf-8"))
    elif getattr(a, "seglocal", False):
        import seglocal as _sl
        _w = _sl.load_w()
        a.segplan = _sl.plan_of(a.text, _sl.decode(a.text, _w))
        print("[seglocal] " + " | ".join(f"{x['text']}({x['pause_ms']})" for x in a.segplan["segments"]),
              flush=True)
    if getattr(a, "pause_plan", ""):
        a.pause_plan = [float(x) / 1000.0 for x in a.pause_plan.replace("，", ",").split(",") if x.strip()]
    apply_mood(a)

    out = Path(a.out) if Path(a.out).is_absolute() else ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    ref = Path(a.ref) if Path(a.ref).is_absolute() else ROOT / a.ref
    ref_txt = a.text_ref or ref_text(ref)

    t0 = time.time()
    f5 = F5TTS(device="cuda", hf_cache_dir=str(ROOT / "hf"),
               ckpt_file=a.ckpt, vocoder_local_path=str(VOCOS), use_ema=a.use_ema)
    print(f"[load] {time.time() - t0:.1f}s  ref={ref.name}", flush=True)

    if a.batch:
        bpath = Path(a.batch) if Path(a.batch).is_absolute() else ROOT / a.batch
        lines = [ln.strip() for ln in bpath.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        sub = Path(a.out)
        outdir = sub if sub.is_absolute() else ROOT / sub
        outdir.mkdir(parents=True, exist_ok=True)
        for i, ln in enumerate(lines, 1):
            if "|" in ln:
                name, _, txt = ln.partition("|")
                name, txt = name.strip(), txt.strip()
            else:
                name, txt = f"line{i:02d}", ln
            synthesize(f5, a, txt, outdir / f"{name}.wav", name=name)
        return 0

    synthesize(f5, a, a.text, out)


def synth_seg(f5, ref: Path, ref_txt: str, text: str, a):
    """念一段并回听，返回 (wav, sr, 一致度)。"""
    want = norm_text(text)
    tmp = ROOT / "tts/out/_segcheck.wav"
    best, best_score = None, -1.0
    n = max(1, int(getattr(a, "verify", 0) or 1))
    for k in range(n):
        seed = None if a.seed is None else a.seed + k * 7
        wav, sr, _ = f5.infer(ref_file=str(ref), ref_text=ref_txt, gen_text=text,
                              nfe_step=a.nfe, cfg_strength=a.cfg, speed=a.speed,
                              remove_silence=True, seed=seed, file_wave=str(tmp),
                              show_info=lambda *x, **y: None)
        if a.fix_lead:
            wav, got = trim_leading(wav, sr, transcribe_words(tmp), want)
        else:
            got = norm_text(transcribe(tmp))
        hit = 1.0 if got == want else difflib.SequenceMatcher(None, got, want).ratio()
        print(f"[seg-check {k + 1}/{n}] {got}  (一致度 {hit:.0%})", flush=True)
        if best is None or hit > best_score:
            best, best_score = (np.asarray(wav, dtype=np.float32), sr), hit
        if hit >= 1.0:
            break
    tmp.unlink(missing_ok=True)
    return best[0], best[1], best_score


SUBJ_START = ("我", "你", "他", "她", "它", "咱", "您", "这", "那", "但", "所以", "而且", "然后", "另外")


def _pick_mark(seg_text: str, next_text: str) -> str:
    """断点处用哪个标点：换主语/另起一句用句号，接着往下说用逗号。"""
    cur = seg_text.rstrip()
    if cur and cur[-1] in "。！？；，、：…—":
        return ""
    nxt = next_text.lstrip()
    if nxt.startswith(SUBJ_START):
        return "。"
    return "，"


def _build_punct_text(segs) -> str:
    out = []
    for i, sg in enumerate(segs):
        body = str(sg.get("text", ""))
        out.append(body)
        if i < len(segs) - 1:
            try:
                mark = _pick_mark(body, str(segs[i + 1].get("text", "")))
            except Exception:
                mark = ""
            if mark:
                out.append(mark)
    return "".join(out)


def _fit_pause(audio, sr, want_ms, punct_scale):
    """把整段音频里「已有的静音」调成谱里要的长度（只动静音，不动字）。

    返回 (新音频, 实际动了几处)。做法：找最长的几段静音，按其位置从后往前改，
    因为改长度会让后面的索引失效。
    """
    want = float(np.clip(want_ms / 1000.0 * punct_scale, 0.08, 1.60))
    runs = rhythm.silence_runs(audio, sr)
    if not runs:
        return audio, 0
    # 只看内部静音（首尾不算），按长度从大到小，取与「要调的停顿」数量相当的几处
    inner = [(s, e) for s, e in runs if s > 0.05 and e < len(audio) / sr - 0.05]
    if not inner:
        return audio, 0
    inner.sort(key=lambda r: (r[1] - r[0]), reverse=True)
    target = int(want * sr)
    changed = 0
    for s, e in sorted(inner[:3], key=lambda r: r[0], reverse=True):
        i0, i1 = int(s * sr), int(e * sr)
        cur = i1 - i0
        if cur <= 0:
            continue
        if abs(cur - target) < int(0.03 * sr):
            continue
        # 只在静音内部增删，留 15ms 余量靠住有声段
        keep = 15
        pad_l = min(keep, cur // 4)
        pad_r = min(keep, cur // 4)
        mid = audio[i0 + pad_l:i1 - pad_r]
        if mid.size == 0:
            continue
        new_mid = np.zeros(target - pad_l - pad_r, dtype=np.float32)
        audio = np.concatenate([audio[:i0 + pad_l], new_mid, audio[i1 - pad_r:]])
        changed += 1
    return audio, changed


def render_segplan_take(f5, a, text, seed, tag=""):
    """朗读谱：把谱里的断点写成标点，让模型自己停（不切字）。"""
    plan = getattr(a, "segplan", None) or {}
    segs = plan.get("segments") or []
    if not segs:
        raise SystemExit("--segplan 里没有 segments")
    punct_text = _build_punct_text(segs)
    print(f"{tag}[谱→文本] {punct_text}", flush=True)
    wav, sr, hit = synth_seg(f5, ref_of(a), ref_txt_of(a), punct_text, a)
    audio = rhythm.trim_edges(np.asarray(wav, dtype=np.float32), sr, keep_ms=40)
    peak = float(np.abs(audio).max()) or 1.0
    audio = audio / peak * 0.89

    # 每处断点的目标停顿：取谱里的值，最后一段为 0
    wants = [int(sg.get("pause_ms") or 0) for sg in segs[:-1]]
    # 模型自己停得太长的地方先压回来，再按谱调
    audio = rhythm.compress_pauses(audio, sr, cap=max(a.pause_cap, 0.60), target=0.30)
    runs = rhythm.silence_runs(audio, sr)
    inner = [(s_, e_) for s_, e_ in runs if s_ > 0.05 and e_ < len(audio) / sr - 0.05]
    print(f"{tag}[模型] 自己停出 {len(inner)} 处内部停顿："
          f"{[round((b - aa) * 1000) for aa, b in inner]}ms", flush=True)
    audio, changed = _fit_pause(audio, sr, float(np.mean(wants)) if wants else 0.0, a.punct_scale)
    out = audio
    stats = {"marks": len(wants), "added": changed, "skipped": [], "enough": [],
             "expected": len(wants), "pauses": len(inner), "short": 0}
    print(f"{tag}[plan-done] {len(out) / sr:.2f}s  谱断点 {len(wants)} 处  "
          f"模型自己停 {len(inner)} 处  微调 {changed} 处  整句咬字 {hit:.0%}", flush=True)
    return {"audio": out, "sr": sr, "stats": stats, "added": wants, "hit": hit}


def render_clause_take(f5, a, text, seed, tag=""):
    """按小句合成再拼：每句都是完整的句子，停顿是句与句之间的，不是切字切出来的。"""
    pieces = rhythm.split_text(text)
    rng = random.Random(seed if seed is not None else 0)
    chunks, sr = [], None
    for i, (body, mark) in enumerate(pieces, 1):
        wav, sr = synth_clause(f5, ref_of(a), ref_txt_of(a), body, a)
        wav = np.asarray(wav, dtype=np.float32)
        wav = rhythm.trim_edges(wav, sr, keep_ms=40)
        peak = float(np.abs(wav).max()) or 1.0
        chunks.append(wav / peak * 0.89)
        print(f"{tag}[clause {i}/{len(pieces)}] {len(wav) / sr:.2f}s  {body}", flush=True)
        if i < len(pieces):
            gap = rhythm.pause_for(mark or "，", rng) * a.punct_scale * a.sentence_mul
            gap = float(np.clip(gap, 0.12, 1.20))
            chunks.append(np.zeros(int(gap * sr), dtype=np.float32))
    audio = np.concatenate(chunks)
    n = max(0, len(pieces) - 1)
    stats = {"marks": n, "added": n, "skipped": [], "enough": [],
             "expected": n, "pauses": n, "short": 0}
    return {"audio": audio, "sr": sr, "stats": stats, "added": [], "hit": 1.0}


def render_take(f5, a, text, seed, tag=""):
    """合成一遍并做节奏处理，返回这一遍的结果（不写最终文件）。"""
    a.seed = seed
    if getattr(a, "segplan", None):
        return render_segplan_take(f5, a, text, seed, tag)
    if getattr(a, "clause", False):
        return render_clause_take(f5, a, text, seed, tag)
    clauses = split_clauses(text) if a.stitch else [text]
    join_rng = random.Random(seed if seed is not None else 0)
    chunks = []
    for i, c in enumerate(clauses, 1):
        wav, sr = synth_clause(f5, ref_of(a), ref_txt_of(a), c, a)
        chunks.append(np.asarray(wav, dtype=np.float32))
        print(f"{tag}[synth {i}/{len(clauses)}] {len(wav) / sr:.2f}s  {c}", flush=True)
        if i < len(clauses):
            lo, hi = rhythm.SENTENCE_GAP
            lo, hi = lo * a.sentence_mul, hi * a.sentence_mul
            chunks.append(np.zeros(int(join_rng.uniform(lo, hi) * sr), dtype=np.float32))

    audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    stats = {"marks": 0, "added": 0, "skipped": [], "enough": []}
    added, hit = [], 1.0
    if a.rhythm:
        before = len(audio) / sr
        audio = rhythm.trim_edges(np.asarray(audio, dtype=np.float32), sr)
        # 先压掉模型自己吐出来的过长空白，再按标点补回「人味」的停顿
        audio = rhythm.compress_pauses(audio, sr, cap=a.pause_cap, target=a.pause_target)
        if a.punct_pause:
            tmp_in = ROOT / "tts/out/_rhythm_in.wav"
            sf.write(str(tmp_in), audio, sr)
            words = transcribe_words(tmp_in)
            hit = difflib.SequenceMatcher(
                None, norm_text("".join(w[0] for w in words)), norm_text(text)).ratio()
            cand, added, stats = rhythm.insert_punct_pauses(
                audio, sr, text, words, random.Random(seed if seed is not None else 0),
                scale=a.punct_scale, jitter=a.jitter, comma_extra=a.comma_extra,
                force=getattr(a, "pause_force", False),
                plan=getattr(a, "pause_plan", None))
            if added:
                # 插完再听一遍：插胶水把字劈坏了就整句回退
                tmp_out = ROOT / "tts/out/_rhythm_out.wav"
                sf.write(str(tmp_out), cand, sr)
                after_hit = difflib.SequenceMatcher(
                    None, norm_text(transcribe(tmp_out)), norm_text(text)).ratio()
                tmp_out.unlink(missing_ok=True)
                if after_hit + 0.02 < hit:
                    print(f"{tag}[pause] 插完反而糊了（{hit:.0%}→{after_hit:.0%}），这次不插", flush=True)
                    added = []
                    stats = dict(stats, added=0)
                else:
                    audio = cand
                    hit = after_hit
                    detail = "、".join(f"{d * 1000:.0f}ms" for _, d in added)
                    print(f"{tag}[pause] 标点处补了 {len(added)} 处：{detail}", flush=True)
            tmp_in.unlink(missing_ok=True)
        # 覆盖率不看 whisper 的时间戳（它给的是整句一个块，位置只能按字数估），
        # 直接数音频里真实存在的内部空白：比「该有几处停顿」少就说明被念过去了。
        marks = [ch for ch in text if ch in rhythm.PUNCT_CHARS]
        internal = len(marks) - (1 if text.strip()[-1:] in tuple(rhythm.PUNCT_CHARS) else 0)
        dur = len(audio) / sr
        found = [e - s for s, e in rhythm.silence_runs(audio, sr)
                 if e - s >= 0.12 and s > 0.05 and e < dur - 0.05]
        stats["expected"] = max(0, internal)
        stats["pauses"] = len(found)
        stats["short"] = max(0, internal - len(found))
        if stats["short"]:
            print(f"{tag}[pause] 标点应停 {internal} 处，实际只有 {len(found)} 处", flush=True)
        print(f"{tag}[rhythm] {before:.2f}s → {len(audio) / sr:.2f}s", flush=True)
    return {"audio": audio, "sr": sr, "stats": stats, "added": added, "hit": hit}


def synthesize(f5, a, text, out, name=""):
    tag = f"[{name}] " if name else ""
    t1 = time.time()
    base_seed = a.seed
    retries = max(0, int(getattr(a, "pause_retry", 0) or 0))
    best = render_take(f5, a, text, base_seed, tag)

    def score(t):
        # 先看「缺了几处停顿」（越少越好），再比补了几处，最后比咬字
        return (-t["stats"].get("short", 0), t["stats"]["added"], t["hit"])

    for k in range(1, retries + 1):
        if best["stats"].get("short", 0) <= 0:
            break
        seed = (base_seed or 0) + 977 * k
        print(f"{tag}[retry {k}/{retries}] 还差 {best['stats'].get('short', 0)} 处标点停顿，"
              f"换种子重来（seed={seed}）", flush=True)
        t = render_take(f5, a, text, seed, tag)
        if score(t) > score(best):
            best = t
    a.seed = base_seed

    audio, sr = best["audio"], best["sr"]
    if best["stats"].get("short", 0) > 0:
        print(f"{tag}[pause] 最终仍差 {best['stats']['short']} 处标点停顿", flush=True)
    peak = float(np.abs(audio).max()) or 1.0
    audio = audio / peak * 0.89
    dry = ROOT / "tts/out/_dry.wav"
    sf.write(str(dry), audio, sr)

    if a.raw:
        dry.replace(out)
    else:
        af = build_filter(a.air, a.soft) + ",loudnorm=I=-19:TP=-4:LRA=9"
        subprocess.run([str(FF), "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(dry), "-af", af, "-ac", "1", "-ar", "24000", str(out)],
                       check=True)
        dry.unlink(missing_ok=True)

    print(f"{tag}[done] {out}  {len(audio) / sr:.2f}s(干声)  用时 {time.time() - t1:.1f}s", flush=True)
    return out



if __name__ == "__main__":
    sys.exit(main())
