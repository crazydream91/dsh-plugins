// 日程库：老大交代的事、课程表、要提醒的节点，全放在这里。
// 数据落在 memory/schedule/，跟着记忆系统一起走。
//
// 用法（在本机跑，Node 运行时用 DSH 自带的）：
//   schedule.mjs add --title "高数课" --at "2026-10-30 14:00" --before 30,10
//   schedule.mjs add --title "交作业" --at "2026-11-02 23:00" --type reminder --note "第三章"
//   schedule.mjs add --title "晨跑" --repeat daily --at "2026-10-29 06:30"
//   schedule.mjs add --title "例会" --repeat "weekly:1,3" --at "2026-10-29 19:00"
//   schedule.mjs list [--days 7] [--all]
//   schedule.mjs done --id s123 ;  schedule.mjs rm --id s123
//   schedule.mjs care add --topic 胃 --text "少吃辣、冰的、咖啡"
//   schedule.mjs care list
import { readFileSync, writeFileSync, mkdirSync, existsSync, renameSync, openSync, closeSync, unlinkSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
export const SCHED_DIR = join(here, '..', 'memory', 'schedule');
export const ITEMS_FILE = join(SCHED_DIR, 'items.json');
export const CARE_FILE = join(SCHED_DIR, 'care.json');
export const STATE_FILE = join(SCHED_DIR, 'state.json');

const WEEK = ['日', '一', '二', '三', '四', '五', '六'];

function ensureDir() {
  if (!existsSync(SCHED_DIR)) mkdirSync(SCHED_DIR, { recursive: true });
}

export function readJson(file, fallback) {
  try {
    return JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

export function writeJson(file, value) {
  ensureDir();
  const tmp = `${file}.tmp`;
  writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
  renameSync(tmp, file);
}

export function loadItems() {
  return readJson(ITEMS_FILE, { updated: null, items: [] });
}

/**
 * 读-改-写要排队：两个进程同时加日程时，不加锁会互相盖掉。
 * 用 items.lock 做互斥，最多等 2 秒。
 */
export function withLock(fn) {
  ensureDir();
  const lock = join(SCHED_DIR, 'items.lock');
  let fd = null;
  for (let i = 0; i < 40; i++) {
    try {
      fd = openSync(lock, 'wx');
      break;
    } catch {
      // 别人正拿着，等一下再试
      const until = Date.now() + 50;
      while (Date.now() < until) { /* busy wait */ }
    }
  }
  try {
    return fn();
  } finally {
    if (fd !== null) {
      try { closeSync(fd); } catch { /* ignore */ }
      try { unlinkSync(lock); } catch { /* ignore */ }
    }
  }
}

export function saveItems(store) {
  store.updated = new Date().toISOString();
  writeJson(ITEMS_FILE, store);
}

export function loadCare() {
  return readJson(CARE_FILE, { notes: [] });
}

export function loadState() {
  return readJson(STATE_FILE, { fired: {}, slots: {} });
}

export function saveState(state) {
  writeJson(STATE_FILE, state);
}

/** "2026-10-30 14:00" / "2026/10/30 14:00" / "14:00"（今天）→ Date */
export function parseAt(text, base = new Date()) {
  const s = String(text).trim();
  const full = s.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})$/);
  if (full) return new Date(+full[1], +full[2] - 1, +full[3], +full[4], +full[5], 0, 0);
  const hm = s.match(/^(\d{1,2}):(\d{2})$/);
  if (hm) return new Date(base.getFullYear(), base.getMonth(), base.getDate(), +hm[1], +hm[2], 0, 0);
  throw new Error(`看不懂的时间：${text}（要 2026-10-30 14:00 或 14:00）`);
}

export function fmt(date) {
  const p = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())} ${p(date.getHours())}:${p(date.getMinutes())}`;
}

export function dayKey(date) {
  const p = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}`;
}

/** 下一次发生时间：一次性的就是 at；daily/weekly 按重复规则往后找。 */
export function nextOccurrence(item, now = new Date()) {
  const at = parseAt(item.at);
  if (item.done) return null;
  if (!item.repeat) return at.getTime() >= now.getTime() ? at : null;

  const [kind, arg] = String(item.repeat).split(':');
  const hh = at.getHours();
  const mm = at.getMinutes();

  if (kind === 'daily') {
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hh, mm, 0, 0);
    if (today.getTime() >= now.getTime() && today.getTime() >= at.getTime()) return today;
    return new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, hh, mm, 0, 0);
  }

  if (kind === 'weekly') {
    const days = String(arg ?? '')
      .split(',')
      .map((d) => Number(d.trim()))
      .filter((d) => Number.isInteger(d) && d >= 0 && d <= 6);
    if (days.length === 0) return null;
    for (let i = 0; i < 8; i++) {
      const cand = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i, hh, mm, 0, 0);
      if (cand.getTime() < now.getTime() || cand.getTime() < at.getTime()) continue;
      if (days.includes(cand.getDay())) return cand;
    }
  }
  return null;
}

export function describe(item, now = new Date()) {
  const occ = nextOccurrence(item, now);
  const when = occ ? fmt(occ) : '（已过）';
  const repeat = item.repeat ? ` 重复:${item.repeat}` : '';
  const before = item.remindBefore?.length ? ` 提前:${item.remindBefore.join('/')}分` : '';
  return `${item.id}  ${when}  ${item.title}${repeat}${before}${item.note ? `  (${item.note})` : ''}${item.done ? '  [已完成]' : ''}`;
}

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (!token.startsWith('--')) {
      out._.push(token);
      continue;
    }
    const key = token.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('--')) out[key] = true;
    else {
      out[key] = next;
      i++;
    }
  }
  return out;
}

function newId(prefix) {
  return `${prefix}${Date.now().toString(36)}${Math.floor(Math.random() * 90 + 10)}`;
}

function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const args = parseArgs(rest);
  const now = new Date();

  if (cmd === 'add') {
    if (!args.title || !args.at) throw new Error('add 需要 --title 和 --at');
    const before = args.before
      ? String(args.before)
          .split(',')
          .map((n) => Number(n.trim()))
          .filter((n) => Number.isFinite(n) && n >= 0)
      : args.type === 'reminder'
        ? []
        : [30];
    const item = {
      id: newId('s'),
      title: String(args.title),
      at: fmt(parseAt(String(args.at))),
      type: args.type === 'reminder' ? 'reminder' : 'event',
      repeat: args.repeat ? String(args.repeat) : null,
      remindBefore: before,
      note: args.note ? String(args.note) : '',
      urgent: args.urgent === true,
      created: now.toISOString(),
      done: false,
    };
    withLock(() => {
      const store = loadItems();
      store.items ??= [];
      store.items.push(item);
      saveItems(store);
    });
    console.log(`已记下：${describe(item, now)}`);
    return;
  }

  if (cmd === 'list') {
    const store = loadItems();
    const days = args.days ? Number(args.days) : 7;
    const limit = args.all ? Infinity : new Date(now.getFullYear(), now.getMonth(), now.getDate() + days).getTime();
    const rows = (store.items ?? [])
      .filter((i) => args.all || (nextOccurrence(i, now)?.getTime() ?? Infinity) <= limit)
      .sort((a, b) => (nextOccurrence(a, now)?.getTime() ?? Infinity) - (nextOccurrence(b, now)?.getTime() ?? Infinity));
    if (rows.length === 0) {
      console.log('（这几天没有安排）');
      return;
    }
    console.log(`现在 ${fmt(now)}，接下来 ${args.all ? '全部' : `${days} 天`}：`);
    for (const item of rows) console.log(`  ${describe(item, now)}`);
    return;
  }

  if (cmd === 'done' || cmd === 'rm') {
    if (!args.id) throw new Error(`${cmd} 需要 --id`);
    let title = args.id;
    withLock(() => {
      const store = loadItems();
      const item = (store.items ?? []).find((i) => i.id === args.id);
      if (!item) throw new Error(`没有这个安排：${args.id}`);
      title = item.title;
      if (cmd === 'rm') store.items = store.items.filter((i) => i.id !== args.id);
      else item.done = true;
      saveItems(store);
    });
    console.log(cmd === 'rm' ? `删掉了：${args.id}` : `标记完成：${title}`);
    return;
  }

  if (cmd === 'care') {
    const sub = args._[0] ?? 'list';
    const care = loadCare();
    care.notes ??= [];
    if (sub === 'add') {
      if (!args.text) throw new Error('care add 需要 --text');
      const note = { id: newId('c'), topic: args.topic ? String(args.topic) : '身体', text: String(args.text), since: dayKey(now) };
      withLock(() => {
        const fresh = loadCare();
        fresh.notes ??= [];
        fresh.notes.push(note);
        writeJson(CARE_FILE, fresh);
      });
      console.log(`记下了（${note.topic}）：${note.text}`);
      return;
    }
    if (sub === 'rm') {
      withLock(() => {
        const fresh = loadCare();
        fresh.notes = (fresh.notes ?? []).filter((n) => n.id !== args.id);
        writeJson(CARE_FILE, fresh);
      });
      console.log(`删掉了：${args.id}`);
      return;
    }
    if (care.notes.length === 0) console.log('（还没有记过身体状况和忌口）');
    else for (const n of care.notes) console.log(`${n.id}  [${n.topic}] ${n.text}`);
    return;
  }

  if (cmd === 'now') {
    console.log(`现在 ${fmt(now)} 星期${WEEK[now.getDay()]}`);
    return;
  }

  console.log('schedule.mjs add|list|done|rm|care|now  —— 详细用法看文件头注释');
}

// 直接运行时执行 CLI；被 active.mjs import 时只导出函数。
if (process.argv[1] && /schedule\.mjs$/i.test(process.argv[1])) {
  try {
    main();
  } catch (error) {
    console.error(`出错：${error.message}`);
    process.exit(1);
  }
}
