import io
import re

path = r"D:\dsh 大肥鱼\plugins\dsh-plugin-score-smith\py\parts.py"
src = io.open(path, encoding="utf-8").read()

# ---------------------------------------------------------------- 1) 插辅助函数
helpers = '''
_DOTTED_BASE = (0.1875, 0.375, 0.75, 1.5, 3.0, 6.0)


def _dotted_underline(dur: float) -> str | None:
    """附点音符：时值是 0.75/1.5/3 这类"半拍+点"，返回它该带的下划线类。"""
    for base in _DOTTED_BASE:
        if abs(dur - base) < 0.03:
            if base >= 1.5:
                return ""
            return "u1" if base >= 0.5 else "u2"
    return None


def _measure_signature(notes_m: list[dict], meter: int) -> tuple:
    return tuple(
        (round(float(n["offsetQL"]) % meter, 3), round(float(n["durationQL"]), 3), int(n["pitch"]))
        for n in sorted(notes_m, key=lambda x: x["offsetQL"])
    )


def _section_marks(by_measure: dict, measures: int, meter: int, block: int = 4) -> tuple:
    """找出重复的 4 小节块，给段落打 A/B/A′ 标记并放反复记号。"""
    sig = {m: _measure_signature(by_measure.get(m, []), meter) for m in range(measures)}
    windows: dict[tuple, list[int]] = {}
    k = 0
    while k + block <= measures:
        w = tuple(sig[m] for m in range(k, k + block))
        if any(sig[m] for m in range(k, k + block)):
            windows.setdefault(w, []).append(k)
        k += block
    order = []
    for w in windows:
        if w not in order:
            order.append(w)
    letters = {w: (chr(ord("A") + i) if i < 8 else "?") for i, w in enumerate(order)}
    labels: dict[int, str] = {}
    rep_open: set[int] = set()
    rep_close: set[int] = set()
    for w, starts in windows.items():
        lab = letters.get(w, "")
        for i, s in enumerate(starts):
            if not lab:
                continue
            labels[s] = lab + ("′" * i)
        if len(starts) >= 2 and starts[0] + block <= measures:
            rep_open.add(starts[0])
            rep_close.add(starts[0] + block - 1)
    return labels, rep_open, rep_close


def _tie_pairs(notes_sorted: list[dict], tol: float = 0.08) -> list[tuple]:
    """延音线：同一音高、后一个音紧接在前一个音结束处（跨小节线也算）。"""
    pairs = []
    for a, b in zip(notes_sorted, notes_sorted[1:]):
        if int(a["pitch"]) != int(b["pitch"]):
            continue
        gap = float(b["offsetQL"]) - (float(a["offsetQL"]) + float(a["durationQL"]))
        if -1e-6 <= gap <= tol:
            pairs.append((a, b))
    return pairs


'''
anchor = 'def jianpu_html(model: dict, track: dict, lyrics: list[str] | None = None,'
assert anchor in src, "anchor for helpers not found"
src = src.replace(anchor, helpers + anchor, 1)

# ---------------------------------------------------------------- 2) 行渲染加标记/延音线
start_marker = "    rows = []\n    syl_idx = 0"
end_marker = "    key_txt = f\"{meta.get('keyTonic') or 'C'}"
i0 = src.index(start_marker)
i1 = src.index(end_marker)
assert i0 < i1

new_body = '''    labels, rep_open, rep_close = _section_marks(by_measure, measures, meter)
    notes_sorted = sorted(notes, key=lambda x: x["offsetQL"])
    ties = _tie_pairs(notes_sorted)

    gap = 14.0
    rows = []
    syl_idx = 0
    mpr = max(1, int(measures_per_row))
    mw = (1300.0 - gap * mpr) / mpr                       # 每小节多宽

    def note_x(n: dict, row_start: int) -> float:
        m = int(float(n["offsetQL"]) // meter)
        frac = (float(n["offsetQL"]) - m * meter) / meter
        return (m - row_start) * (mw + gap) + frac * mw

    for start in range(0, measures, mpr):
        cells_per_measure = []
        row_notes = [n for n in notes_sorted
                     if start * meter <= float(n["offsetQL"]) < (start + mpr) * meter]
        for m in range(start, min(start + mpr, measures)):
            notes_m = sorted(by_measure.get(m, []), key=lambda x: x["offsetQL"])
            cells = []
            # 每一拍都落位：有音符就画音符，没音符补一个 0
            starts = [n["offsetQL"] for n in notes_m]
            for beat in range(meter):
                beat_start = m * meter + beat
                busy = any(beat_start - 1e-6 <= s < beat_start + 1 - 1e-6 for s in starts)
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
                dots_up = "·" * max(0, min(2, octv))
                dots_dn = "·" * max(0, min(2, -octv))
                deg = html.escape(n["degree"])
                dotted_cls = _dotted_underline(float(dur))
                under = ""
                dot = ""
                if dotted_cls is not None:
                    dot = '<span class="dt">·</span>'
                    if dotted_cls:
                        under = f'<span class="{dotted_cls}"></span>'
                elif dur < 0.5:
                    under = '<span class="u2"></span>'
                elif dur < 1:
                    under = '<span class="u1"></span>'
                elif dur >= 2:
                    under = '<span class="dash">' + "－" * (int(round(dur)) - 1) + "</span>"
                lyric = ""
                if sylls:
                    lyric = sylls[syl_idx] if syl_idx < len(sylls) else ""
                    syl_idx += 1
                cells.append(
                    f'<span class="note" data-o="{n["offsetQL"]:.4f}" data-d="{dur:.4f}" '
                    f'data-p="{int(n["pitch"])}" style="left:{x:.2f}%">'
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
                marks += '<span class="rep">‖:</span>'
            elif m in rep_close:
                marks += '<span class="rep">:‖</span>'
            cells_per_measure.append(
                f'<span class="{cls}" style="width:{mw:.0f}px">'
                f'<span class="mno">{m + 1}</span>{marks}'
                f'{"".join(cells)}</span>'
            )

        # 延音线 / 连音线：整行一个 SVG 覆盖层
        paths = []
        for a, b in ties:
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
        svg = ""
        if paths:
            svg = ('<svg class="sl" viewBox="0 0 1300 108" preserveAspectRatio="none">'
                   + "".join(paths) + "</svg>")
        rows.append(f'<div class="bar">{svg}{"".join(cells_per_measure)}</div>')

'''
src = src[:i0] + new_body + src[i1:]

# ---------------------------------------------------------------- 3) 谱头：调号/拍号/速度
old_sub_re = re.compile(r'<div class="sub">[^<\n]*</div>')
assert old_sub_re.search(src), "sub block not found"
new_sub = '''<div class="sub">共 {measures} 小节 · {lyric_note}</div>
  <div class="keysig"><span class="k">1 = {html.escape(str(meta.get('keyTonic') or 'C'))}</span>
   <span class="t">{meter}/4<small>（{meter_text}）</small></span>
   <span class="tempo">♩ = {int(round(tempo))}</span></div>'''
src = old_sub_re.sub(new_sub, src, count=1)

# meter_text 变量
old_key = "    key_txt = f\"{meta.get('keyTonic') or 'C'} {'大调' if (meta.get('keyMode') or 'major') == 'major' else '小调'}\""
new_key = old_key + "\n    meter_text = {2: '二拍子', 3: '三拍子', 4: '四拍子', 6: '六拍子'}.get(meter, '')"
assert old_key in src, "key_txt not found"
src = src.replace(old_key, new_key, 1)

io.open(path, "w", encoding="utf-8").write(src)
print("patched", len(src))
