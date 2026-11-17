// 主动开口 + 日程提醒：这台机器上唯一会"自己说话"的东西。
//
// 它每 20 秒醒一次，看三件事：
//   1. 老大交代的日程/提醒到点没有（memory/schedule/items.json）
//   2. 早上问好（proactive.morning）、睡前晚安（proactive.goodnight）到点没有
//   3. 现在是不是休息时间（config.quiet），是就闭嘴
// 到点了就开一个 headless 回合，把要说的前提交给模型，让它用自己的话说，
// 拿到文字后用 OneBot 直接发到老大的 QQ。
//
// 用法：
//   active.mjs              常驻（会被 run-active.cmd / start-all.ps1 拉起来）
//   active.mjs --once       只检查一轮，打印要发的什么，不真发
//   active.mjs --test morning [--send]   立刻演练某个 slot
//   active.mjs --test remind --id s123   立刻演练某条日程
import { readFileSync, writeFileSync, existsSync, unlinkSync, appendFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { WsClient } from './ws-lite.mjs';
import { loadItems, loadCare, loadState, saveState, nextOccurrence, fmt, dayKey, parseAt } from './schedule.mjs';
import { describeDay, classesOn } from '../timetable/engine.mjs';
import { loadDrive, saveDrive, evolve, shouldSpeak, recordSpoke, moodText } from './drive.mjs';
import { takeDue, takeEvening } from './promise.mjs';
import { splitBubbles } from './bubbles.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const config = JSON.parse(readFileSync(join(here, 'config.json'), 'utf8'));
const LOG = join(here, 'active.log');
const PID = join(here, 'active.pid');
const BRIDGE_STATE = join(here, 'state.json');

const OWNER = Number(config.owner ?? config.allow?.users?.[0] ?? 0);
const proactive = config.proactive ?? { enabled: true, morning: '08:10', goodnight: '20:45' };
const WINDOW_MS = 45 * 60 * 1000; // 到点后多久内还愿意补发
const SILENT = '[[静默]]';

function log(...parts) {
  const line = `[${new Date().toISOString()}] ${parts.join(' ')}`;
  console.log(line);
  try {
    appendFileSync(LOG, `${line}\n`, 'utf8');
  } catch {
    // 日志写不进去也要继续说
  }
}

const pad = (n) => String(n).padStart(2, '0');
const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

function minutesOf(text) {
  const m = String(text ?? '').match(/^(\d{1,2}):(\d{2})$/);
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

function isQuiet(now = new Date()) {
  const start = minutesOf(config.quiet?.start);
  const end = minutesOf(config.quiet?.end);
  if (start === null || end === null) return false;
  const cur = now.getHours() * 60 + now.getMinutes();
  return start <= end ? cur >= start && cur < end : cur >= start || cur < end;
}

function ownerSession() {
  try {
    const state = JSON.parse(readFileSync(BRIDGE_STATE, 'utf8'));
    return state.chats?.[`private:${OWNER}`]?.sessionId ?? null;
  } catch {
    return null;
  }
}

/** 今天/明天的安排，压缩成一行一句话，塞进 prompt 里省 tokens。 */
function agendaText(now = new Date()) {
  const items = (loadItems().items ?? []).filter((i) => !i.done);
  const today = dayKey(now);
  const tomorrow = dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1));
  const bucket = { [today]: [], [tomorrow]: [] };
  for (const item of items) {
    const occ = nextOccurrence(item, now);
    if (!occ) continue;
    const key = dayKey(occ);
    if (!bucket[key]) continue;
    bucket[key].push(`${hhmm(occ)} ${item.title}`);
  }
  const parts = [];
  if (bucket[today].length) parts.push(`今天：${bucket[today].sort().join('；')}`);
  if (bucket[tomorrow].length) parts.push(`明天：${bucket[tomorrow].sort().join('；')}`);
  try {
    const t1 = describeDay(now);
    const t2 = describeDay(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1), '明天');
    parts.push(t1, t2);
  } catch {
    /* 课表读不到就不提，不影响别的提醒 */
  }
  return parts.length ? parts.join('。') : '这两天没有特别的安排';
}

function careText() {
  const notes = (loadCare().notes ?? []).slice(-4);
  if (notes.length === 0) return '';
  return `关于老大身体和忌口，你记着：${notes.map((n) => n.text).join('；')}。`;
}

function baseRules() {
  return [
    '你是小晏，在 QQ 上主动找老大说话。',
    '只写要发出去的那几句话：不要提配置、文件、模型、提醒机制，不要列清单，不要解释你在做什么。',
    '一两句就够，像发消息，不像写报告。',
    `真的没什么想说的，就只输出 ${SILENT}。`,
  ].join('\n');
}

function promptMorning() {
  return `${baseRules()}\n\n[现在 ${hhmm(new Date())}，老大刚开始这一天]\n${agendaText()}\n${careText()}\n主动跟他打个招呼，顺手提一句今天最要紧的那件事（有的话）。`;
}

function promptGoodnight() {
  return `${baseRules()}\n\n[现在 ${hhmm(new Date())}，老大快到睡觉的点了]\n${agendaText()}\n${careText()}\n跟他说句晚安，有要早起的、要准备的就顺口提一句。`;
}

function promptSpontaneous(drive) {
  return `${baseRules()}\n\n[不是到点提醒，是你自己忽然想找他说句话。你现在的心情：${moodText(drive)}；现在 ${hhmm(new Date())}]\n${agendaText()}\n就照你此刻的心情说一句。别报时，别总结今天，别问"有什么可以帮你"。`;
}

function promptClassSoon(c) {
  const start = String(c.time || '').split('-')[0];
  const room = String(c.room || '').replace(/^\*/, '');
  const teachers = (c.teachers ?? []).join('、');
  return `${baseRules()}\n\n[现在 ${hhmm(new Date())}，老大 ${start} 有课：${c.course}；地点 ${room}${teachers ? `；老师 ${teachers}` : ''}]\n提醒他该准备出门了，别拖到迟到。一句就够，别念课表。`;
}

function promptReminder(item, occ, offsetMin) {
  const now = new Date();
  const when = offsetMin > 0 ? `还有 ${offsetMin} 分钟` : '现在就是';
  return `${baseRules()}\n\n[提醒] 「${item.title}」定在 ${fmt(occ)}${item.note ? `（${item.note}）` : ''}，${when}。现在是 ${hhmm(now)}。\n${careText()}\n用你自己的话提醒老大这一件事，带上具体时间。`;
}

/** 开一个 headless 回合，拿它最后说的话。 */
function runTurn(prompt, sessionId) {
  return new Promise((accept) => {
    const args = [config.dsh.cliJs, '--profile', 'headless', '--patch', join(here, config.dsh.patch ?? './dsh-overlay.yml'), '--json'];
    if (sessionId) args.push('--session-id', sessionId);
    args.push('-');

    const child = spawn(config.dsh.exe, args, {
      cwd: config.dsh.cwd,
      env: { ...process.env, ELECTRON_RUN_AS_NODE: '1', DSH_PERMISSION_MODE: 'read-only' },
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    let out = '';
    let err = '';
    let text = '';
    let sid = sessionId;
    let settled = false;
    const done = (patch = {}) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      accept({ text, sessionId: sid, ...patch });
    };
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* ignore */ }
      done({ error: '超时' });
    }, config.limits?.timeoutMs ?? 300000);
    timer.unref?.();

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (piece) => {
      out += piece;
      let i;
      while ((i = out.indexOf('\n')) >= 0) {
        const line = out.slice(0, i).trim();
        out = out.slice(i + 1);
        if (!line) continue;
        try {
          const event = JSON.parse(line);
          if (event.type === 'session') sid = event.sessionId;
          else if (event.type === 'text') text += event.text ?? '';
          else if (event.type === 'final' && typeof event.text === 'string' && event.text) text = event.text;
          else if (event.type === 'status' && event.phase === 'turn_end' && event.reason?.kind === 'error') {
            done({ error: event.reason.error?.message ?? '未知错误' });
          }
        } catch { /* 非 JSON 行忽略 */ }
      }
    });
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (piece) => { err += piece; });
    child.on('close', (code) => done(code === 0 ? {} : { error: (err || `退出码 ${code}`).slice(0, 300) }));
    child.stdin.end(`${prompt}\n`);
  });
}

/** 用 OneBot 发一条私聊。 */
function sendText(to, text) {
  return new Promise((accept) => {
    const headers = {};
    if (config.onebot?.accessToken) headers.Authorization = `Bearer ${config.onebot.accessToken}`;
    const message = [{ type: 'text', data: { text } }];
    const client = WsClient.connect(config.onebot.wsUrl, { headers });
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { client.close(); } catch { /* ignore */ }
      accept(value);
    };
    const timer = setTimeout(() => finish({ ok: false, reason: '发送超时' }), 20000);
    client.on('error', (error) => finish({ ok: false, reason: error.message }));
    client.on('open', () => {
      const echo = `a${Date.now()}`;
      const listener = (raw) => {
        let frame;
        try { frame = JSON.parse(raw); } catch { return; }
        if (frame.echo !== echo) return;
        client.off('message', listener);
        finish({ ok: frame.status === 'ok' || frame.retcode === 0, reason: JSON.stringify(frame).slice(0, 200) });
      };
      client.on('message', listener);
      client.send(JSON.stringify({ action: 'send_private_msg', params: { user_id: Number(to), message }, echo }));
    });
  });
}

function clean(text) {
  return String(text ?? '')
    .replace(/\[\[\s*img\s*:[^\]]*\]\]/giu, '')
    .replace(/\s+$/u, '')
    .trim();
}

/** 说一句话：跑模型 → 过滤 → 发出去。 */
async function speak(label, prompt, { dry = false } = {}) {
  const sessionId = ownerSession();
  log(`▶ ${label} 生成中（会话 ${sessionId ?? '新'}）`);
  if (dry) {
    log(`── dry prompt ──\n${prompt}`);
    return { ok: true, dry: true };
  }
  const turn = await runTurn(prompt, sessionId);
  if (turn.error) {
    log(`✗ ${label} 生成失败：${turn.error}`);
    return { ok: false, retry: /already owned|active write handle/i.test(turn.error) };
  }
  const text = clean(turn.text);
  if (!text || text.includes(SILENT)) {
    log(`· ${label} 没什么想说的，跳过`);
    return { ok: true, skipped: true };
  }
  const bubbles = splitBubbles(text);
  let sent = { ok: true };
  for (let i = 0; i < bubbles.length; i += 1) {
    sent = await sendText(OWNER, bubbles[i]);
    if (!sent.ok) break;
    if (i < bubbles.length - 1) {
      const gap = Math.min(2000, 300 + bubbles[i].length * 26 + Math.floor(Math.random() * 450));
      await new Promise((r) => setTimeout(r, gap));
    }
  }
  if (sent.ok) log(`← ${label} 已发送 ${bubbles.length} 条 / ${text.length} 字`);
  else log(`✗ ${label} 发送失败：${sent.reason}`);
  return { ok: sent.ok, text };
}

async function tick(options = {}) {
  const now = new Date();
  const state = loadState();
  state.fired ??= {};
  state.slots ??= {};
  state.attempts ??= {};
  const today = dayKey(now);
  const quiet = isQuiet(now);
  const results = [];

  // 1) 日程提醒
  for (const item of loadItems().items ?? []) {
    if (item.done) continue;
    const occ = nextOccurrence(item, now);
    if (!occ) continue;
    const offsets = [0, ...(item.remindBefore ?? [])];
    for (const offset of offsets) {
      const trigger = new Date(occ.getTime() - offset * 60000);
      const key = `${item.id}|${fmt(occ)}|${offset}`;
      if (state.fired[key]) continue;
      const late = now.getTime() - trigger.getTime();
      if (late < 0 || late > WINDOW_MS) continue;
      if (quiet && !item.urgent) {
        log(`· ${item.title} 到点但现在是休息时间，先不发`);
        continue;
      }
      if ((state.attempts[key] ?? 0) >= 3) {
        state.fired[key] = 'giveup';
        continue;
      }
      state.attempts[key] = (state.attempts[key] ?? 0) + 1;
      const r = await speak(`提醒:${item.title}@${offset}分`, promptReminder(item, occ, offset), options);
      if (r.ok) {
        state.fired[key] = new Date().toISOString();
        delete state.attempts[key];
        results.push(r);
      }
    }
  }

  // 2) 早上问好 / 睡前晚安
  if (proactive.enabled !== false) {
    for (const [slot, time, builder] of [
      ['morning', proactive.morning ?? '08:10', promptMorning],
      ['goodnight', proactive.goodnight ?? '20:45', promptGoodnight],
    ]) {
      const target = minutesOf(time);
      if (target === null) continue;
      const cur = now.getHours() * 60 + now.getMinutes();
      const slack = cur - target;
      if (slack < 0 || slack > 30) continue; // 只在半小时内补发
      const key = `${today}|${slot}`;
      if (state.slots[key]) continue;
      // 问好在休息时间里不开口，免得吵醒又吵到
      if (quiet) {
        log(`· ${slot} 落在休息时间里，跳过`);
        state.slots[key] = 'quiet';
        continue;
      }
      const r = await speak(slot, builder(), options);
      if (r.ok) state.slots[key] = new Date().toISOString();
      results.push(r);
    }
  }

  // 3) 上课前提醒
  if (proactive.classReminder?.enabled !== false) {
    const before = Number(proactive.classReminder?.minutesBefore ?? 25);
    for (const c of classesOn(now)) {
      const [hh, mm] = String(c.time || '').split('-')[0].split(':').map(Number);
      if (!Number.isFinite(hh) || !Number.isFinite(mm)) continue;
      const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hh, mm);
      const slack = start.getTime() - now.getTime();
      if (slack > before * 60000 || slack < -10 * 60000) continue;
      const key = `${today}|class|${c.course}|${c.start}`;
      if (state.slots[key]) continue;
      if (quiet) {
        state.slots[key] = 'quiet';
        continue;
      }
      const r = await speak(`课前:${c.course}`, promptClassSoon(c), options);
      if (r.ok) state.slots[key] = new Date().toISOString();
      results.push(r);
    }
  }

  // 4) 内驱力：不是闹钟，是她自己想说了
  const driveCfg = proactive.drive ?? {};
  const drive = evolve(loadDrive(), now);
  if (driveCfg.enabled !== false && !quiet) {
    const verdict = shouldSpeak(drive, driveCfg, now);
    if (verdict.speak) {
      const r = await speak(`内驱力(渴望${verdict.urge.toFixed(2)})`, promptSpontaneous(drive), options);
      if (r.ok && !r.dry) recordSpoke(drive, now);
      results.push(r);
    } else if (options.verbose) {
      log(`· ${verdict.reason}`);
    }
  }
  saveDrive(drive);

  // 5) 承诺账本：到点了就提醒，别让"说过的话"烂尾
  if (!quiet) {
    for (const item of takeDue(now)) {
      const prompt = `${baseRules()}\n\n[你之前答应过他一件事：${item.text}；时间到了 ${item.due}。]\n发一条提醒，用你自己的话。不要提"承诺系统""记录"这类词，也别解释为什么会想起来。`;
      const r = await speak(`承诺:${item.id}`, prompt, options);
      results.push(r);
    }
  }

  // 6) 每晚一次：今天还挂着的承诺，问一句算不算数
  const eveningAt = proactive.promiseEvening ?? '20:50';
  if (!quiet && hhmm(now) >= eveningAt) {
    const key = `${dayKey(now)}|promises`;
    if (!state.slots[key]) {
      const pending = takeEvening(now);
      if (pending.length) {
        const lines = pending.map((item) => `· ${item.text}`).join('\n');
        const prompt = `${baseRules()}\n\n[今晚收账：你今天答应过他这些事——\n${lines}\n]\n问他一句做到了没，语气自然，别像催债，也别一次列清单念给他听。`;
        const r = await speak(`承诺汇总(${pending.length}条)`, prompt, options);
        if (r.ok) state.slots[key] = new Date().toISOString();
        results.push(r);
      } else {
        state.slots[key] = 'empty';
      }
    }
  }

  saveState(state);
  return results;
}

function alreadyRunning() {
  try {
    const pid = Number(readFileSync(PID, 'utf8').trim());
    if (!pid) return false;
    process.kill(pid, 0);
    return pid !== process.pid;
  } catch {
    return false;
  }
}

async function main() {
  const argv = process.argv.slice(2);
  const dry = !argv.includes('--send');

  if (argv.includes('--once')) {
    const results = await tick({ dry });
    log(`一轮结束，处理 ${results.length} 条`);
    return;
  }

  if (argv.includes('--test')) {
    const which = argv[argv.indexOf('--test') + 1];
    if (which === 'morning') await speak('test:morning', promptMorning(), { dry });
    else if (which === 'goodnight') await speak('test:goodnight', promptGoodnight(), { dry });
    else if (which === 'remind') {
      const id = argv[argv.indexOf('--id') + 1];
      const item = (loadItems().items ?? []).find((i) => i.id === id);
      if (!item) {
        log(`没有这个安排：${id}`);
        return;
      }
      const occ = nextOccurrence(item, new Date()) ?? parseAt(item.at);
      await speak(`test:${item.title}`, promptReminder(item, occ, 0), { dry });
    } else if (which === 'class') {
      const [h, m] = [new Date().getHours(), new Date().getMinutes()];
      const upcoming = [];
      for (let d = 0; d <= 1; d += 1) {
        const day = new Date();
        day.setDate(day.getDate() + d);
        for (const c of classesOn(day)) upcoming.push({ c, at: new Date(`${day.toISOString().slice(0, 10)}T${String(c.time).split('-')[0]}:00`) });
      }
      const next = upcoming.filter((x) => x.at.getTime() > Date.now()).sort((a, b) => a.at - b.at)[0];
      if (!next) log('接下来两天没有课');
      else await speak(`test:class:${next.c.course}`, promptClassSoon(next.c), { dry });
      void [h, m];
    } else log('--test 后面接 morning / goodnight / class / remind --id sXXX');
    return;
  }

  if (alreadyRunning()) {
    log('已经有一个在跑了，这个退出');
    return;
  }
  writeFileSync(PID, String(process.pid), 'utf8');
  log(`主动开口服务启动 pid=${process.pid}`);
  const bye = () => {
    try { unlinkSync(PID); } catch { /* ignore */ }
    process.exit(0);
  };
  process.on('SIGINT', bye);
  process.on('SIGTERM', bye);

  for (;;) {
    try {
      const results = await tick();
      if (results.length) log(`本轮处理 ${results.length} 条`);
    } catch (error) {
      log(`✗ 出错：${error.message}`);
    }
    await new Promise((r) => setTimeout(r, 20000));
  }
}

main().catch((error) => {
  log(`✗ 顶层错误：${error.message}`);
  process.exit(1);
});
