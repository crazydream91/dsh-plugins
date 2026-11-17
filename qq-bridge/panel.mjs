/** 情绪面板：把"她此刻什么状态"画成一张图。
 *
 *   node panel.mjs            # 写到 media/panel-<时间>.png
 *   node panel.mjs --json     # 只打印数据
 */
import { writeFileSync, readFileSync, readdirSync, existsSync, mkdirSync } from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadDrive, evolve, urge, moodText } from './drive.mjs';

const run = promisify(execFile);
const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');
const PY = join(ROOT, 'voice', 'tts', 'venv', 'Scripts', 'python.exe');

function promises() {
  const file = join(ROOT, 'memory', 'schedule', 'promises.json');
  const empty = { pending: 0, kept: 0, items: [] };
  if (!existsSync(file)) return empty;
  try {
    const data = JSON.parse(readFileSync(file, 'utf8'));
    const pending = data.items.filter((item) => item.status === 'pending');
    const kept = data.items.filter((item) => item.status === 'kept').length;
    return {
      pending: pending.length,
      kept,
      items: pending
        .sort((a, b) => String(a.due ?? '9').localeCompare(String(b.due ?? '9')))
        .map((item) => ({
          text: item.text,
          due: item.due ? item.due.replace(/[-]/g, '/') : '',
          overdue: item.due ? Date.parse(item.due) < Date.now() : false,
        })),
    };
  } catch {
    return empty;
  }
}

function cards() {
  const dir = join(ROOT, 'memory', 'projects');
  try {
    return readdirSync(dir).filter((name) => name.endsWith('.md')).length;
  } catch {
    return 0;
  }
}

async function classes() {
  try {
    const engine = await import(join(ROOT, 'timetable', 'engine.mjs'));
    const now = new Date();
    return {
      dayLabel: engine.describeDay(now) || '今天',
      list: engine.classesOn(now).map((item) => ({
        time: item.time,
        course: item.course,
        room: item.room ?? '',
      })),
    };
  } catch {
    return { dayLabel: '今天', list: [] };
  }
}

function stamp(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`;
}

const now = new Date();
const drive = evolve(loadDrive(), now);
const day = await classes();
const snapshot = {
  now: `${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`,
  drive: {
    energy: drive.energy,
    social: drive.social,
    lonely: drive.lonely,
    closeness: drive.closeness,
  },
  urge: urge(drive),
  mood: moodText(drive),
  spokenCount: drive.spokenCount ?? 0,
  cards: cards(),
  dayLabel: day.dayLabel,
  classes: day.list,
  promises: promises(),
};

if (process.argv.includes('--json')) {
  console.log(JSON.stringify(snapshot, null, 2));
  process.exit(0);
}

const cache = join(HERE, 'cache');
mkdirSync(cache, { recursive: true });
const dataFile = join(cache, 'panel.json');
writeFileSync(dataFile, JSON.stringify(snapshot), 'utf8');

const out = join(HERE, 'media', `panel-${stamp(now)}.png`);
mkdirSync(dirname(out), { recursive: true });
const { stdout, stderr } = await run(PY, [join(HERE, 'panel.py'), dataFile, out], { timeout: 60000 });
if (stderr.trim()) process.stderr.write(stderr);
console.log((stdout || out).trim());
