/** 承诺账本：她答应过的事，都得记账、到期、核销。
 *
 *   node promise.mjs add --text "明早七点叫他起床" [--due "2026-11-05 07:00"] [--chat private:1072609541]
 *   node promise.mjs list [--all]
 *   node promise.mjs keep <id>    # 做到了
 *   node promise.mjs drop <id>    # 这事不算了
 *   node promise.mjs due          # 到期且还没核销的（给 active.mjs 用）
 */
import { readFileSync, writeFileSync, mkdirSync, renameSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';

const HERE = dirname(fileURLToPath(import.meta.url));
export const STORE = join(HERE, '..', 'memory', 'schedule', 'promises.json');

function load() {
  try {
    return JSON.parse(readFileSync(STORE, 'utf8'));
  } catch {
    return { items: [] };
  }
}

function save(data) {
  mkdirSync(dirname(STORE), { recursive: true });
  const tmp = `${STORE}.tmp`;
  writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
  renameSync(tmp, STORE);
}

const pad = (n) => String(n).padStart(2, '0');
export function fmt(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 支持 "2026-11-05 07:00" / "11-05 07:00" / "07:00"(=今天或明天最近的那次) */
export function parseDue(text, now = new Date()) {
  if (!text) return null;
  const full = text.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})$/u);
  if (full) return new Date(+full[1], +full[2] - 1, +full[3], +full[4], +full[5], 0, 0);
  const md = text.match(/^(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})$/u);
  if (md) return new Date(now.getFullYear(), +md[1] - 1, +md[2], +md[3], +md[4], 0, 0);
  const hm = text.match(/^(\d{1,2}):(\d{2})$/u);
  if (hm) {
    const when = new Date(now.getFullYear(), now.getMonth(), now.getDate(), +hm[1], +hm[2], 0, 0);
    if (when <= now) when.setDate(when.getDate() + 1);
    return when;
  }
  return null;
}

export function list(filter = {}) {
  const data = load();
  const now = new Date();
  return data.items
    .filter((item) => (filter.all ? true : item.status === 'pending'))
    .filter((item) => (filter.dueNow ? item.due && Date.parse(item.due) <= now.getTime() && !item.remindedAt : true))
    .sort((a, b) => String(a.due ?? a.madeAt).localeCompare(String(b.due ?? b.madeAt)));
}

export function add({ text, due = null, chat = 'private:1072609541' }) {
  const data = load();
  const id = `pr${Date.now().toString(36).slice(-6)}${randomBytes(2).toString('hex')}`;
  const item = {
    id,
    text,
    due,
    chat,
    madeAt: fmt(new Date()),
    status: 'pending',
    remindedAt: null,
    lastAsked: null,
    keptAt: null,
  };
  data.items.push(item);
  save(data);
  return item;
}

export function mark(id, status) {
  const data = load();
  const item = data.items.find((entry) => entry.id === id);
  if (!item) return null;
  item.status = status;
  if (status === 'kept') item.keptAt = fmt(new Date());
  save(data);
  return item;
}

/** 到点要提醒的承诺：标记已提醒，避免重复。 */
export function takeDue(now = new Date()) {
  const data = load();
  const hit = [];
  for (const item of data.items) {
    if (item.status !== 'pending' || !item.due || item.remindedAt) continue;
    if (Date.parse(item.due) > now.getTime()) continue;
    item.remindedAt = fmt(now);
    hit.push(item);
  }
  if (hit.length) save(data);
  return hit;
}

/** 每晚汇总一次：今天还挂着、没有具体时间的承诺。 */
export function takeEvening(now = new Date()) {
  const data = load();
  const today = fmt(now).slice(0, 10);
  const hit = [];
  for (const item of data.items) {
    if (item.status !== 'pending' || item.due) continue;
    if ((item.lastAsked ?? '').startsWith(today)) continue;
    if ((item.madeAt ?? '').slice(0, 10) > today) continue;
    item.lastAsked = fmt(now);
    hit.push(item);
  }
  if (hit.length) save(data);
  return hit;
}

export function stats() {
  const items = load().items;
  const pending = items.filter((i) => i.status === 'pending');
  const kept = items.filter((i) => i.status === 'kept');
  return { total: items.length, pending: pending.length, kept: kept.length, pendingItems: pending };
}

if (process.argv[1] && process.argv[1].endsWith('promise.mjs')) {
  const [cmd, ...rest] = process.argv.slice(2);
  const flag = (name) => {
    const i = rest.indexOf(`--${name}`);
    return i >= 0 ? rest[i + 1] : undefined;
  };
  if (cmd === 'add') {
    const text = flag('text') ?? rest.filter((a) => !a.startsWith('--')).join(' ');
    if (!text) {
      console.error('缺 --text');
      process.exit(1);
    }
    const item = add({ text, due: parseDue(flag('due')) ? fmt(parseDue(flag('due'))) : null, chat: flag('chat') });
    console.log(`${item.id} ${item.due ?? '（没定时间）'} ${item.text}`);
  } else if (cmd === 'list') {
    const items = list({ all: rest.includes('--all') });
    if (!items.length) console.log('（没有挂着的承诺）');
    for (const item of items) console.log(`${item.id} ${item.status} ${item.due ?? '未定时'} ${item.text}`);
  } else if (cmd === 'keep' || cmd === 'drop') {
    const item = mark(rest[0], cmd === 'keep' ? 'kept' : 'dropped');
    console.log(item ? `${item.id} → ${item.status}` : '没这个 id');
  } else if (cmd === 'due') {
    for (const item of takeDue()) console.log(`${item.id} ${item.text}`);
  } else if (cmd === 'stats') {
    const s = stats();
    console.log(`共 ${s.total} 条，挂着 ${s.pending} 条，兑现 ${s.kept} 条`);
  } else {
    console.log('用法：add --text "…" [--due "2026-11-05 07:00"] | list [--all] | keep <id> | drop <id> | due | stats');
  }
}
