# -*- coding: utf-8 -*-
"""给简谱播放器加「真钢琴音频」模式：有 play.mp3 就用 <audio>（音质真、能拖进度条）。"""
from __future__ import annotations

import io
import os

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parts.py")
src = io.open(P, encoding="utf-8").read()

# ---------- 1) CSS：隐藏 <audio> ----------
CSS_ANCHOR = ' .bar { position: relative; }'
if '#pAudio' not in src:
    src = src.replace(
        CSS_ANCHOR,
        ' #pAudio { display: none; }\n' + CSS_ANCHOR,
        1,
    )

# ---------- 2) 音频模式 JS ----------
AUDIO_JS = r'''
_JP_AUDIO_JS = r"""
(function () {
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
      if (r.top < 120 || r.bottom > window.innerHeight - 60) {
        cur.el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }
  }

  function tick() {
    if (A.paused) return;
    const t = A.currentTime;
    paint(t / secPerQL);
    $('pos').textContent = fmt(t) + ' / ' + fmt(dur());
    $('seek').value = String(Math.min(1000, Math.round(t / dur() * 1000)));
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
    $('pos').textContent = fmt(t) + ' / ' + fmt(dur());
    $('seek').value = String(Math.min(1000, Math.round(t / dur() * 1000)));
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
  $('pClick').addEventListener('change', () => { $('warn').textContent = '音频模式没有节拍器'; });

  // 进度条：input 事件连拖带点都生效（change 只在松手时触发，容易以为没反应）
  $('seek').addEventListener('input', (e) => seekSec(parseFloat(e.target.value) / 1000 * dur()));
  $('seek').addEventListener('change', (e) => seekSec(parseFloat(e.target.value) / 1000 * dur()));

  $('pTest').addEventListener('click', () => {
    const back = A.currentTime;
    seekSec(0);
    play();
    setTimeout(() => { pause(); seekSec(back); }, 1500);
  });

  document.addEventListener('keydown', (e) => {
    if (e.code === 'Space') { e.preventDefault(); A.paused ? play() : pause(); }
  });

  A.addEventListener('ended', () => { $('pPlay').textContent = '▶ 播放'; });
  A.addEventListener('loadedmetadata', () => {
    $('pos').textContent = '0:00 / ' + fmt(dur());
  });

  $('pos').textContent = '0:00 / ' + fmt(TOTAL * secPerQL);
  window.__jpProbe = async () => ({ mode: 'audio', duration: dur() });
})();
"""
'''

if "_JP_AUDIO_JS" not in src:
    anchor = '_JP_JS = r"""'
    idx = src.find(anchor)
    assert idx > 0, "找不到 _JP_JS"
    end = src.find('\n"""\n', idx)
    assert end > 0
    end += len('\n"""\n')
    src = src[:end] + AUDIO_JS + src[end:]

# ---------- 3) jianpu_html 支持 audio 参数 ----------
old_sig = 'def jianpu_html(model: dict, track: dict, lyrics=None, measures_per_row: int = 2, player: bool = True) -> str:'
new_sig = ('def jianpu_html(model: dict, track: dict, lyrics=None, measures_per_row: int = 2,\n'
           '                player: bool = True, audio: str | None = None) -> str:')
if old_sig in src:
    src = src.replace(old_sig, new_sig, 1)
else:
    assert new_sig in src, "jianpu_html 签名没匹配上"

# ---------- 4) 工具栏 + 脚本分支 ----------
old_branch = '''        js = (_JP_JS.replace("@@NOTES@@", json.dumps(payload))'''
new_branch = '''        if audio:
            js = (_JP_AUDIO_JS.replace("@@TEMPO@@", str(float(tempo)))
                             .replace("@@TOTAL@@", str(round(total_ql_all, 4))))
            toolbar = toolbar.replace(
                '<span id="warn"></span>',
                f'<span id="warn"></span>'
                f'<audio id="pAudio" src="{html.escape(audio)}" preload="auto"></audio>',
            )
        else:
            js = (_JP_JS.replace("@@NOTES@@", json.dumps(payload))'''
if old_branch in src and 'if audio:' not in src:
    src = src.replace(old_branch, new_branch, 1)
    # 原来后面还有三行 .replace(...)，缩进要跟着进 else 分支
    tail_old = '''                    .replace("@@TEMPO@@", str(float(tempo)))
                    .replace("@@TOTAL@@", str(round(total_ql_all, 4)))
                    .replace("@@BEAT@@", "1"))'''
    tail_new = '''                    .replace("@@TEMPO@@", str(float(tempo)))
                    .replace("@@TOTAL@@", str(round(total_ql_all, 4)))
                    .replace("@@BEAT@@", "1"))'''
    assert tail_old in src
else:
    assert 'if audio:' in src, "分支没插进去"

io.open(P, "w", encoding="utf-8").write(src)
print("parts.py patched:", len(src))
