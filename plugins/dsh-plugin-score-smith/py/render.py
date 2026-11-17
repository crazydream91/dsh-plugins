# -*- coding: utf-8 -*-
"""MusicXML → SVG/PNG + 自带播放器的预览页。

verovio 负责排版（它自带 Leipzig 字体资源，但要求资源路径里不能有中文，
所以插件把 venv 装在 C:\\dsh-tools\\score-venv）。
SVG 再交给 Edge 无头模式截成 PNG——这样不依赖 cairo，也不用装编辑器。

除了静态 PNG，还会生成一份自包含的 preview.html：
  - 页面里直接内嵌乐谱 SVG（不用联网、不用 verovio.js）
  - 用 Web Audio 按 score.json 把音符合成出来，可以播放/暂停/变速/静音单轨
  - 播放时高亮当前页、显示第几小节和当前和弦、自动滚动跟随
这样旁边没有乐器的人也能一边听一边看谱。
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile

DEFAULT_OPTIONS = {
    "pageWidth": 1600,
    "scale": 42,
    "adjustPageHeight": True,
    "footer": "none",
    "header": "none",
    "font": "Leipzig",
    "spacingStaff": 10,
    "spacingSystem": 12,
    "svgViewBox": True,
    "svgHtml5": True,
}


def _toolkit(resource_path: str | None = None):
    import verovio

    tk = verovio.toolkit()
    if resource_path:
        tk.setResourcePath(resource_path)
    return tk


def musicxml_to_svgs(musicxml_path: str, options: dict | None = None,
                     resource_path: str | None = None, max_pages: int = 24) -> list[str]:
    """把 MusicXML 排成若干页 SVG（默认 adjustPageHeight 后基本是一整页）。"""
    opts = dict(DEFAULT_OPTIONS)
    if options:
        opts.update({k: v for k, v in options.items() if v is not None})
    tk = _toolkit(resource_path)
    tk.setOptions(opts)
    with open(musicxml_path, "r", encoding="utf-8") as fh:
        xml = fh.read()
    # 用内容加载而不是路径：verovio 的 C++ 层读不了含中文的路径
    if not tk.loadData(xml):
        raise RuntimeError("verovio 无法解析这份 MusicXML")
    pages = min(int(tk.getPageCount()), max_pages)
    return [tk.renderToSVG(i + 1) for i in range(pages)]


def _svg_size(svg: str) -> tuple[float, float]:
    width = 1600.0
    height = 1200.0
    m = re.search(r'<svg[^>]*width="([\d.]+)px"', svg)
    if m:
        width = float(m.group(1))
    m = re.search(r'<svg[^>]*height="([\d.]+)px"', svg)
    if m:
        height = float(m.group(2))
    return width, height


def page_measure_span(svgs: list[str], total_measures: int) -> list[dict]:
    """每页画了多少小节 —— 用来做「播放时滚到当前页」和页码区间。"""
    spans = []
    first = 1
    for index, svg in enumerate(svgs):
        count = len(re.findall(r'data-class="measure"', svg)) or 0
        last = min(total_measures, first + max(0, count) - 1) if count else first
        spans.append({"page": index + 1, "firstMeasure": first, "lastMeasure": max(first, last)})
        first = max(first, last) + 1
    if spans:
        spans[-1]["lastMeasure"] = max(spans[-1]["lastMeasure"], total_measures)
    return spans


# ---------------------------------------------------------------- 静态 HTML（给 PNG 用）


def build_html(svgs: list[str], title: str, subtitle: str = "", width: int = 1600) -> tuple[str, int, int]:
    blocks = []
    total_height = 0.0
    for svg in svgs:
        _, h = _svg_size(svg)
        total_height += h + 24
        blocks.append(f'<div class="page">{svg}</div>')

    head = f"""
    <div class="head">
      <div class="title">{html.escape(title or '曲谱')}</div>
      <div class="sub">{html.escape(subtitle or '')}</div>
    </div>"""
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 36px 44px 44px; background: #ffffff;
         font-family: "Microsoft YaHei", "Segoe UI", sans-serif; color: #222; }}
  .head {{ margin-bottom: 18px; border-bottom: 1px solid #e6e6e6; padding-bottom: 14px; }}
  .title {{ font-size: 34px; font-weight: 600; letter-spacing: 1px; }}
  .sub {{ margin-top: 8px; font-size: 17px; color: #666; }}
  .page {{ margin-bottom: 24px; }}
  svg {{ display: block; }}
</style></head><body>{head}{''.join(blocks)}</body></html>"""
    height = int(total_height + 150)
    return body, width, height


# ---------------------------------------------------------------- 带播放器的预览页

_PLAYER_CSS = """
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: #f3f1ec; color: #232323;
               font-family: "Microsoft YaHei", "Segoe UI", system-ui, sans-serif; }
  .bar { position: sticky; top: 0; z-index: 20; background: rgba(255,255,255,.96);
         border-bottom: 1px solid #dcd8d0; padding: 10px 20px 8px; backdrop-filter: blur(8px); }
  .row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .ttl { font-size: 17px; font-weight: 600; margin-right: 6px; }
  .sub { color: #7a7566; font-size: 12.5px; }
  button { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 8px;
           border: 1px solid #cfcac0; background: #fff; color: #333; cursor: pointer; }
  button:hover { background: #f7f5f0; }
  button.primary { background: #2f6f4f; border-color: #2f6f4f; color: #fff; }
  button.primary:hover { background: #275f43; }
  .chip { font-size: 12.5px; padding: 4px 10px; border-radius: 999px; border: 1px solid #cfcac0;
          background: #fff; cursor: pointer; user-select: none; }
  .chip.off { opacity: .38; text-decoration: line-through; }
  .knob { display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: #5c574b; }
  input[type=range] { width: 110px; accent-color: #2f6f4f; }
  .readout { font-variant-numeric: tabular-nums; font-size: 13px; color: #2b2b2b; }
  .readout b { font-size: 16px; color: #2f6f4f; }
  .prog { position: relative; height: 6px; background: #e2ded6; border-radius: 3px; margin-top: 8px; cursor: pointer; }
  .prog > i { position: absolute; left: 0; top: 0; bottom: 0; width: 0%; background: #2f6f4f; border-radius: 3px; }
  .prog > s { position: absolute; top: -2px; bottom: -2px; width: 1px; background: #b9b3a6; }
  .pages { padding: 18px 16px 60px; }
  .page { background: #fff; margin: 0 auto 16px; max-width: 1460px; border-radius: 6px;
          box-shadow: 0 1px 4px rgba(0,0,0,.07); padding: 6px 10px; transition: box-shadow .2s, transform .2s; }
  .page.active { box-shadow: 0 0 0 3px #8fc3a8, 0 2px 10px rgba(0,0,0,.12); }
  .page svg { width: 100%; height: auto; display: block; }
  .tip { color: #8b8676; font-size: 12.5px; padding: 0 20px 10px; }
  .warn { background: #fff4e5; border-bottom: 1px solid #f0d6b0; color: #8a5a12;
          font-size: 13px; padding: 8px 20px; }
  .warn b { color: #7a3f00; }
  #level { font-variant-numeric: tabular-nums; color: #8b8676; }
  #level.on { color: #2f6f4f; font-weight: 600; }
  #level.dead { color: #b23c3c; font-weight: 600; }
"""

_PLAYER_JS = r"""
const SCORE = __SCORE__;
const PAGES = __PAGES__;

const NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
function noteName(p){ return NOTE_NAMES[((p%12)+12)%12] + (Math.floor(p/12)-1); }

const state = {
  ctx: null, master: null, analyser: null, buf: null, gains: [], muted: {}, rate: 1, volume: 0.75,
  playing: false, startCtxTime: 0, startQL: 0, totalQL: 0, events: [], idx: 0,
  timer: null, nodes: [], metronome: false, follow: true, curPage: -1, levelTimer: null
};

function buildTracks(){
  const host = document.getElementById('chips');
  SCORE.tracks.forEach((t, i) => {
    const el = document.createElement('span');
    el.className = 'chip';
    el.textContent = t.label + ' · ' + t.notes.length;
    el.onclick = () => { state.muted[i] = !state.muted[i]; el.classList.toggle('off', !!state.muted[i]);
                         if (state.gains[i]) state.gains[i].gain.value = state.muted[i] ? 0 : trackGain(t.instrument); };
    host.appendChild(el);
  });
}

function trackGain(inst){
  if (inst === 'drums') return 0.55;
  if (inst === 'bass') return 0.5;
  if (inst === 'vocals') return 0.42;
  if (inst === 'piano') return 0.3;
  if (inst === 'guitar') return 0.3;
  return 0.26;
}

function showWarn(html){
  const el = document.getElementById('warn');
  if (!el) return;
  el.innerHTML = html || '';
  el.style.display = html ? 'block' : 'none';
}

function ensureCtx(){
  if (!state.ctx){
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC){ showWarn('这个浏览器不支持 Web Audio，换 Edge / Chrome 打开这个文件。'); return null; }
    state.ctx = new AC({ latencyHint: 'interactive' });
    state.master = state.ctx.createGain();
    state.master.gain.value = state.volume;
    state.analyser = state.ctx.createAnalyser();
    state.analyser.fftSize = 2048;
    state.buf = new Float32Array(state.analyser.fftSize);
    state.master.connect(state.analyser);
    state.analyser.connect(state.ctx.destination);
    state.gains = SCORE.tracks.map((t, i) => {
      const g = state.ctx.createGain();
      g.gain.value = state.muted[i] ? 0 : trackGain(t.instrument);
      g.connect(state.master);
      return g;
    });
    let ql = 0;
    SCORE.tracks.forEach((t) => {
      t.notes.forEach(n => { ql = Math.max(ql, n.o + n.d); });
    });
    state.totalQL = ql;
    document.getElementById('dur').textContent = fmtTime(ql * 60 / SCORE.meta.tempo);
    watchLevel();
  }
  return state.ctx;
}

function rms(){
  if (!state.analyser) return 0;
  state.analyser.getFloatTimeDomainData(state.buf);
  let sum = 0;
  for (let i = 0; i < state.buf.length; i++){ const v = state.buf[i]; sum += v * v; }
  return Math.sqrt(sum / state.buf.length);
}

function levelDb(){ return 20 * Math.log10(Math.max(1e-6, rms())); }

function watchLevel(){
  if (state.levelTimer) return;
  state.levelTimer = setInterval(() => {
    const el = document.getElementById('level');
    if (!el || !state.ctx) return;
    const db = levelDb();
    if (state.ctx.state !== 'running'){
      el.textContent = '电平 浏览器挂起中';
      el.className = 'readout dead';
      return;
    }
    if (db > -55){ el.textContent = '电平 ' + db.toFixed(0) + ' dB'; el.className = 'readout on'; }
    else if (db > -75){ el.textContent = '电平 ' + db.toFixed(0) + ' dB'; el.className = 'readout'; }
    else { el.textContent = '电平 静音'; el.className = 'readout dead'; }
  }, 200);
}

async function unlockAudio(){
  const ctx = ensureCtx();
  if (!ctx) return null;
  if (ctx.state !== 'running'){
    try { await ctx.resume(); } catch (err) { /* 下面统一提示 */ }
  }
  if (ctx.state !== 'running'){
    showWarn('浏览器把音频上下文挂起了（state = <b>' + ctx.state + '</b>）。'
      + '在页面上随便点一下再按播放；还不行就看这个标签页是不是被静音了。');
  }
  return ctx;
}

function blip(t0){
  const ctx = state.ctx;
  if (!ctx) return;
  const osc = ctx.createOscillator(), g = ctx.createGain();
  osc.type = 'sine';
  osc.frequency.value = 1320;
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime(0.22, t0 + 0.006);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.14);
  osc.connect(g); g.connect(state.master);
  osc.start(t0); osc.stop(t0 + 0.16);
}

async function testSound(){
  const ctx = await unlockAudio();
  if (!ctx) return;
  const t0 = ctx.currentTime + 0.06;
  [60, 64, 67, 72].forEach((pitch, i) => {
    const osc = ctx.createOscillator(), env = ctx.createGain();
    osc.type = 'triangle';
    osc.frequency.value = 440 * Math.pow(2, (pitch - 69) / 12);
    const at = t0 + i * 0.17;
    env.gain.setValueAtTime(0.0001, at);
    env.gain.exponentialRampToValueAtTime(0.3, at + 0.01);
    env.gain.exponentialRampToValueAtTime(0.0001, at + 0.55);
    osc.connect(env); env.connect(state.master);
    osc.start(at); osc.stop(at + 0.6);
  });
  setTimeout(() => {
    const db = levelDb();
    if (ctx.state !== 'running') return;
    if (db < -70){
      showWarn('刚渲染了一段和弦，但输出电平只有 <b>' + db.toFixed(0) + ' dB</b>，浏览器确实没出声。'
        + '依次检查：① 系统音量 / 应用音量条 ② 浏览器标签页是否被静音（标签上的小喇叭）'
        + ' ③ 输出设备选的是不是耳机/音箱。');
    } else {
      showWarn('声音链路正常：刚才那段测试和弦的输出电平 <b>' + db.toFixed(0) + ' dB</b>。'
        + '听到了就说明播放没问题；听不到但电平常在 -20 dB 左右，那就是系统层被静音了。');
    }
  }, 560);
  return ctx;
}

let metronomeTimer = null;
function clickTick(t, accent){
  const ctx = state.ctx, osc = ctx.createOscillator(), g = ctx.createGain();
  osc.frequency.value = accent ? 1600 : 1100;
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(accent ? 0.16 : 0.09, t + 0.002);
  g.gain.exponentialRampToValueAtTime(0.0001, t + 0.05);
  osc.connect(g); g.connect(state.master);
  osc.start(t); osc.stop(t + 0.07);
}

function scheduleEvents(fromSec, untilSec){
  const ev = state.events;
  while (state.idx < ev.length && ev[state.idx].t < untilSec){
    const e = ev[state.idx++];
    if (e.t + e.dur < fromSec) continue;
    if (state.muted[e.ti]) continue;
    playNote(e);
  }
}

function playNote(e){
  const ctx = state.ctx, t = state.startCtxTime + e.t, g = state.gains[e.ti];
  const dur = Math.max(0.06, e.dur * 0.96);
  if (e.drum){ playDrum(e.drum, t, g); return; }
  const osc = ctx.createOscillator(), env = ctx.createGain();
  const inst = SCORE.tracks[e.ti].instrument;
  osc.type = (inst === 'bass') ? 'sine' : (inst === 'vocals' ? 'triangle' : 'triangle');
  osc.frequency.value = 440 * Math.pow(2, (e.p - 69) / 12);
  const peak = Math.min(0.5, 0.18 + (e.v || 0.7) * 0.22);
  env.gain.setValueAtTime(0.0001, t);
  env.gain.exponentialRampToValueAtTime(peak, t + 0.012);
  env.gain.exponentialRampToValueAtTime(peak * 0.6, t + Math.max(0.05, dur * 0.6));
  env.gain.exponentialRampToValueAtTime(0.0001, t + dur + 0.05);
  osc.connect(env); env.connect(g);
  osc.start(t); osc.stop(t + dur + 0.08);
}

function noiseBuffer(dur){
  const ctx = state.ctx, len = Math.max(1, Math.floor(ctx.sampleRate * dur));
  const buf = ctx.createBuffer(1, len, ctx.sampleRate);
  const data = buf.getChannelData(0);
  for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;
  return buf;
}

function playDrum(kind, t, out){
  const ctx = state.ctx;
  kind = (kind || 'snare').toLowerCase();
  if (kind === 'kick'){
    const osc = ctx.createOscillator(), env = ctx.createGain();
    osc.frequency.setValueAtTime(150, t);
    osc.frequency.exponentialRampToValueAtTime(45, t + 0.11);
    env.gain.setValueAtTime(0.6, t);
    env.gain.exponentialRampToValueAtTime(0.0001, t + 0.2);
    osc.connect(env); env.connect(out); osc.start(t); osc.stop(t + 0.22);
    return;
  }
  if (kind.startsWith('tom') || kind === 'tom'){
    const base = kind === 'tom1' ? 240 : kind === 'tom2' ? 185 : 140;
    const osc = ctx.createOscillator(), env = ctx.createGain();
    osc.frequency.setValueAtTime(base, t);
    osc.frequency.exponentialRampToValueAtTime(base * 0.62, t + 0.14);
    env.gain.setValueAtTime(0.4, t);
    env.gain.exponentialRampToValueAtTime(0.0001, t + 0.2);
    osc.connect(env); env.connect(out); osc.start(t); osc.stop(t + 0.22);
    return;
  }
  const src = ctx.createBufferSource();
  const isHat = kind.indexOf('hat') >= 0;
  const isCrash = kind === 'crash';
  const len = isCrash ? 0.7 : (isHat ? 0.09 : 0.16);
  src.buffer = noiseBuffer(len + 0.05);
  const filter = ctx.createBiquadFilter();
  filter.type = isHat || isCrash ? 'highpass' : 'bandpass';
  filter.frequency.value = isCrash ? 5200 : (isHat ? 7000 : 1750);
  filter.Q.value = isHat ? 0.9 : 1.4;
  const env = ctx.createGain();
  env.gain.setValueAtTime(0.0001, t);
  env.gain.exponentialRampToValueAtTime(isHat ? 0.18 : 0.3, t + 0.004);
  env.gain.exponentialRampToValueAtTime(0.0001, t + len);
  src.connect(filter); filter.connect(env); env.connect(out);
  src.start(t); src.stop(t + len + 0.02);
}

function buildEvents(){
  const ev = [];
  SCORE.tracks.forEach((t, ti) => {
    t.notes.forEach(n => {
      // o 必须留着：startAt() 每次重排都用 e.o 重算秒数，丢了 o 就全成了 NaN，一声都没有
      ev.push({ o: n.o, t: n.o, d: n.d, p: n.p, v: n.v, drum: n.drum ? (n.drumName || 'snare') : null, ti });
    });
  });
  ev.sort((a, b) => a.t - b.t);
  state.events = ev;
}

async function startAt(ql){
  const ctx = await unlockAudio();
  if (!ctx) return;
  if (!state.events.length) buildEvents();
  stopAll(true);
  const bpm = SCORE.meta.tempo * state.rate;
  const secPerQL = 60 / bpm;
  state.startQL = ql;
  state.startCtxTime = ctx.currentTime + 0.10;
  state.idx = 0;
  state.events.forEach((e) => { e.t = (e.o - ql) * secPerQL; e.dur = e.d * secPerQL; });
  // 重排 & 指向正确位置
  state.events.sort((a, b) => a.t - b.t);
  state.idx = state.events.findIndex(e => e.t >= -0.001);
  if (state.idx < 0) state.idx = state.events.length;
  blip(ctx.currentTime + 0.02);
  state.playing = true;
  document.getElementById('play').textContent = '⏸ 暂停';
  state.timer = setInterval(tick, 60);
}

function tick(){
  if (!state.playing) return;
  const elapsed = state.ctx.currentTime - state.startCtxTime;
  scheduleEvents(elapsed, elapsed + 0.25);
  const totalSec = state.totalQL * 60 / (SCORE.meta.tempo * state.rate);
  if (elapsed > totalSec + 0.4) { stopAll(); return; }
  updateReadout(elapsed);
}

function updateReadout(elapsed){
  const secPerQL = 60 / (SCORE.meta.tempo * state.rate);
  const ql = state.startQL + Math.max(0, elapsed) / secPerQL;
  const meter = SCORE.meta.meter;
  const measure = Math.floor(ql / meter) + 1;
  const total = Math.max(1, Math.ceil(state.totalQL / meter));
  const chord = (SCORE.chords || {})[String(measure - 1)] || '';
  document.getElementById('pos').innerHTML = '第 <b>' + measure + '</b> / ' + total + ' 小节';
  document.getElementById('chord').textContent = chord ? '和弦 ' + chord : '';
  document.getElementById('time').textContent = fmtTime(ql * secPerQL) + ' / ' + fmtTime(state.totalQL * secPerQL);
  const pct = Math.min(100, (ql / Math.max(1, state.totalQL)) * 100);
  document.getElementById('bar').style.width = pct + '%';
  markPage(measure);
}

function markPage(measure){
  let page = 0;
  PAGES.forEach((p, i) => { if (measure >= p.firstMeasure && measure <= p.lastMeasure) page = i; });
  if (page === state.curPage) return;
  state.curPage = page;
  document.querySelectorAll('.page').forEach((el, i) => el.classList.toggle('active', i === page));
  if (state.follow){
    const el = document.querySelectorAll('.page')[page];
    if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }
}

function stopAll(silent){
  state.playing = false;
  clearInterval(state.timer); state.timer = null;
  document.getElementById('play').textContent = '▶ 播放';
  if (!silent && state.ctx){
    // 立刻静音正在响的音（快速淡出，然后恢复主音量，别把音量永久压到 0）
    const now = state.ctx.currentTime;
    state.master.gain.cancelScheduledValues(now);
    state.master.gain.setValueAtTime(state.master.gain.value, now);
    state.master.gain.linearRampToValueAtTime(0.0001, now + 0.04);
    setTimeout(() => {
      if (!state.master || !state.ctx) return;
      state.master.gain.cancelScheduledValues(state.ctx.currentTime);
      state.master.gain.setValueAtTime(state.volume, state.ctx.currentTime);
    }, 120);
  }
}

function fmtTime(sec){
  sec = Math.max(0, sec);
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m + ':' + String(s).padStart(2, '0');
}

function currentQL(){
  if (!state.playing || !state.ctx) return state.startQL;
  const secPerQL = 60 / (SCORE.meta.tempo * state.rate);
  return state.startQL + (state.ctx.currentTime - state.startCtxTime) / secPerQL;
}

function init(){
  buildTracks();
  document.getElementById('play').onclick = () => {
    if (state.playing) stopAll();
    else startAt(currentQL() % Math.max(1, state.totalQL || 1));
  };
  document.getElementById('test').onclick = testSound;
  document.getElementById('stop').onclick = () => { stopAll(); state.startQL = 0; updateReadout(0); };
  document.getElementById('rate').oninput = (e) => {
    const wasPlaying = state.playing;
    const ql = currentQL();
    state.rate = parseFloat(e.target.value);
    document.getElementById('ratev').textContent = state.rate.toFixed(2) + '×';
    if (wasPlaying) startAt(ql);
  };
  document.getElementById('vol').oninput = (e) => {
    state.volume = parseFloat(e.target.value);
    if (state.master) state.master.gain.value = state.volume;
  };
  document.getElementById('follow').onchange = (e) => { state.follow = e.target.checked; };
  document.getElementById('metro').onchange = (e) => { state.metronome = e.target.checked;
    document.getElementById('metronote').textContent = state.metronome ? '（节拍器需要点播放才会响）' : ''; };
  document.getElementById('prog').onclick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const ql = ratio * state.totalQL;
    if (state.playing) startAt(ql); else { state.startQL = ql; updateReadout(0); markPage(Math.floor(ql / SCORE.meta.meter) + 1); }
  };
  document.querySelectorAll('.page').forEach(el => el.querySelector('svg').removeAttribute('width'));
  document.querySelectorAll('.page svg').forEach(svg => { svg.setAttribute('preserveAspectRatio', 'xMidYMid meet'); });
  let totalQL = 0;
  SCORE.tracks.forEach((t) => { t.notes.forEach(n => { totalQL = Math.max(totalQL, n.o + n.d); }); });
  state.totalQL = totalQL;
  document.getElementById('dur').textContent = fmtTime(totalQL * 60 / SCORE.meta.tempo);
  updateReadout(0);
  document.addEventListener('keydown', (e) => {
    if (e.code === 'Space'){ e.preventDefault(); document.getElementById('play').click(); }
  });
}
window.addEventListener('DOMContentLoaded', init);
"""


def _notes_for_js(model: dict) -> list[dict]:
    tracks = []
    for track in model.get("tracks") or []:
        notes = []
        is_drum = track.get("kind") == "drums" or track.get("instrument") == "drums"
        for note in track.get("notes") or []:
            offset = float(note.get("offsetQL") or 0)
            duration = float(note.get("durationQL") or 0.25)
            item = {
                "o": round(offset, 4),
                "d": round(duration, 4),
                "p": int(note.get("pitch") or 60),
                "v": round(float(note.get("velocity") or 0.7), 3),
            }
            if is_drum:
                item["drum"] = True
                item["drumName"] = note.get("drum") or "snare"
            notes.append(item)
        tracks.append({
            "id": track.get("id"),
            "label": track.get("label") or track.get("id"),
            "instrument": track.get("instrument") or track.get("id"),
            "kind": track.get("kind") or "melodic",
            "notes": notes,
        })
    return tracks


def build_player_html(model: dict, svgs: list[str], title: str, subtitle: str = "") -> str:
    meter = int((model.get("meta") or {}).get("meter") or 4)
    tempo = float((model.get("meta") or {}).get("tempo") or 96)
    summary_measures = 1
    for track in model.get("tracks") or []:
        for note in track.get("notes") or []:
            end = float(note.get("offsetQL") or 0) + float(note.get("durationQL") or 0)
            summary_measures = max(summary_measures, int(end // meter) + 1)
    pages = page_measure_span(svgs, summary_measures)

    score_js = {
        "title": title,
        "meta": {"tempo": tempo, "meter": meter},
        "chords": {str(k): str(v) for k, v in (model.get("chords") or {}).items()},
        "tracks": _notes_for_js(model),
    }
    pages_js = [{"firstMeasure": p["firstMeasure"], "lastMeasure": p["lastMeasure"]} for p in pages]

    page_blocks = []
    for index, svg in enumerate(svgs):
        span = pages[index] if index < len(pages) else {"firstMeasure": 1, "lastMeasure": 1}
        page_blocks.append(
            f'<div class="page" data-page="{index + 1}" '
            f'data-measures="{span["firstMeasure"]}-{span["lastMeasure"]}">{svg}</div>'
        )

    js = _PLAYER_JS.replace("__SCORE__", json.dumps(score_js, ensure_ascii=False, separators=(",", ":")))
    js = js.replace("__PAGES__", json.dumps(pages_js, ensure_ascii=False))

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title or '曲谱')} · 曲谱预览</title>
<style>{_PLAYER_CSS}</style></head>
<body>
<div class="bar">
  <div class="row">
    <div>
      <div class="ttl">{html.escape(title or '曲谱')}</div>
      <div class="sub">{html.escape(subtitle or '')} · 空格键播放/暂停</div>
    </div>
    <button id="play" class="primary">▶ 播放</button>
    <button id="stop">⏹ 停止</button>
    <button id="test" title="渲染一段合成音并测电平，用来确认浏览器到底出不出声">🔊 测试声音</button>
    <span class="readout" id="pos">第 <b>1</b> 小节</span>
    <span class="readout" id="chord"></span>
    <span class="readout" id="time">0:00 / 0:00<span id="dur" style="display:none"></span></span>
    <span class="readout" id="level">电平 --</span>
    <span class="knob">速度 <input id="rate" type="range" min="0.5" max="1.5" step="0.05" value="1"><span id="ratev">1.00×</span></span>
    <span class="knob">音量 <input id="vol" type="range" min="0" max="1" step="0.05" value="0.75"></span>
    <label class="knob"><input id="follow" type="checkbox" checked> 跟随滚动</label>
    <label class="knob"><input id="metro" type="checkbox"> 节拍器</label>
    <span id="chips" class="row" style="gap:6px"></span>
  </div>
  <div class="prog" id="prog"><i id="bar"></i></div>
</div>
<div class="warn" id="warn" style="display:none"></div>
<div class="tip">点标题条上的乐器胶囊可以静音单轨；点进度条跳到任意位置。<b>没声音就先点「🔊 测试声音」</b>——右上角「电平」会显示浏览器到底有没有输出信号。<span id="metronote"></span></div>
<div class="pages">{''.join(page_blocks)}</div>
<script>{js}</script>
</body></html>"""


# ---------------------------------------------------------------- 截图


def _find_browser() -> str | None:
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def html_to_png(html_text: str, out_png: str, width: int = 1600, height: int = 1600,
                browser: str | None = None, timeout: int = 120) -> bool:
    """用无头浏览器把 HTML 截成 PNG。临时文件放在纯英文目录，避开编码坑。"""
    browser = browser or _find_browser()
    if not browser:
        return False
    tmp_dir = os.path.join(tempfile.gettempdir(), "score_smith_render")
    os.makedirs(tmp_dir, exist_ok=True)
    html_path = os.path.join(tmp_dir, "preview.html")
    png_path = os.path.join(tmp_dir, "preview.png")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    if os.path.exists(png_path):
        try:
            os.remove(png_path)
        except OSError:
            pass

    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--default-background-color=FFFFFFFF",
        f"--window-size={int(width)},{int(height)}",
        f"--screenshot={png_path}",
        f"file:///{html_path.replace(os.sep, '/')}",
    ]
    try:
        subprocess.run(cmd, check=False, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    if not os.path.isfile(png_path):
        return False
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    shutil.copyfile(png_path, out_png)
    return True


def render_preview(musicxml_path: str, out_dir: str, title: str, subtitle: str = "",
                   resource_path: str | None = None, browser: str | None = None,
                   options: dict | None = None, model: dict | None = None,
                   page_pngs: bool = True) -> dict:
    """MusicXML → SVG 文件 + PNG 预览图 + 可播放的 preview.html。"""
    os.makedirs(out_dir, exist_ok=True)
    svgs = musicxml_to_svgs(musicxml_path, options=options, resource_path=resource_path)
    svg_paths = []
    for index, svg in enumerate(svgs):
        svg_path = os.path.join(out_dir, f"preview-{index + 1}.svg")
        with open(svg_path, "w", encoding="utf-8") as fh:
            fh.write(svg)
        svg_paths.append(svg_path)

    body, width, height = build_html(svgs, title, subtitle)
    html_path = os.path.join(out_dir, "preview.html")
    player_html = build_player_html(model or {}, svgs, title, subtitle) if model else body
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(player_html)

    png_path = os.path.join(out_dir, "preview.png")
    ok = html_to_png(body, png_path, width=width, height=height, browser=browser)

    page_pngs_paths = []
    if page_pngs:
        for index, svg in enumerate(svgs):
            page_html = build_html([svg], f"{title}（第 {index + 1}/{len(svgs)} 页）", subtitle)[0]
            png = os.path.join(out_dir, f"preview-p{index + 1}.png")
            if html_to_png(page_html, png, width=width, height=1200, browser=browser):
                page_pngs_paths.append(png)

    return {
        "svg": svg_paths,
        "html": html_path,
        "png": png_path if ok else None,
        "pagePngs": page_pngs_paths,
        "pages": len(svgs),
        "width": width,
        "height": height,
    }
