/** 内驱力：让"想不想说话"由状态决定，而不是由闹钟决定。
 *
 * 状态量（都在 0..1）：
 *   energy    精神——按一天节律走，深夜低、上午高
 *   social    社交饥饿——时间越久越想说话，说完回落
 *   lonely    被冷落——你一直不出现就慢慢涨
 *   closeness 亲密度——互动越多越高，缓慢衰减
 *
 * 主动开口的条件：渴望度过了阈值 + 冷却期 + 当天次数没超 + 不在静音段。
 */
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const DRIVE_FILE = join(HERE, 'cache', 'drive-state.json');
export const USER_PING = join(HERE, 'cache', 'last-user-msg.txt');

const clamp = (v) => Math.max(0, Math.min(1, v));
const HOUR = 3600000;

function dayKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

export function loadDrive(now = new Date()) {
  let raw = {};
  try {
    raw = JSON.parse(readFileSync(DRIVE_FILE, 'utf8'));
  } catch {
    /* 第一次跑 */
  }
  const drive = {
    energy: 0.55,
    social: 0.35,
    lonely: 0.15,
    closeness: 0.5,
    lastTick: now.toISOString(),
    lastSpoke: null,
    spokenDay: dayKey(now),
    spokenCount: 0,
    lastUserAt: null,
    ...raw,
  };
  if (drive.spokenDay !== dayKey(now)) {
    drive.spokenDay = dayKey(now);
    drive.spokenCount = 0;
  }
  return drive;
}

export function saveDrive(drive) {
  writeFileSync(DRIVE_FILE, JSON.stringify(drive, null, 2), 'utf8');
}

/** 一天里的精神曲线：凌晨最低，上午十点最高，午后下坡，傍晚略回升。 */
function energyCurve(now) {
  const hours = now.getHours() + now.getMinutes() / 60;
  return clamp(0.35 + 0.5 * Math.sin(((hours - 4) / 24) * Math.PI * 2 + Math.PI / 2) * 0.5 + 0.2);
}

export function lastUserTime() {
  try {
    const text = readFileSync(USER_PING, 'utf8').trim();
    const t = Date.parse(text);
    return Number.isFinite(t) ? new Date(t) : null;
  } catch {
    return null;
  }
}

/** 把时间往前推：衰减 / 累积 / 吸收用户消息。 */
export function evolve(drive, now = new Date()) {
  const last = Date.parse(drive.lastTick);
  const dtH = Number.isFinite(last) ? Math.min(24, Math.max(0, (now - last) / HOUR)) : 0;

  // 精神往当天的曲线靠
  const target = energyCurve(now);
  drive.energy = clamp(drive.energy + (target - drive.energy) * Math.min(1, dtH / 2));

  // 社交饥饿随时间涨；亲密度缓慢衰减
  drive.social = clamp(drive.social + dtH * 0.16);
  drive.lonely = clamp(drive.lonely + dtH * 0.09);
  drive.closeness = clamp(drive.closeness - dtH * 0.012);

  // 用户来过：饿意清零、被冷落回落、亲密度上涨
  const userAt = lastUserTime();
  if (userAt && (!drive.lastUserAt || userAt.toISOString() !== drive.lastUserAt)) {
    drive.lastUserAt = userAt.toISOString();
    drive.social = clamp(drive.social - 0.55);
    drive.lonely = clamp(drive.lonely - 0.7);
    drive.closeness = clamp(drive.closeness + 0.06);
    drive.lastSpoke = drive.lastSpoke ?? null;
  }

  drive.lastTick = now.toISOString();
  return drive;
}

/** 渴望度：想说话 + 被冷落 + 精神好，再被亲密度加成。 */
export function urge(drive) {
  const base = 0.42 * drive.social + 0.33 * drive.lonely + 0.25 * drive.energy;
  return clamp(base * (0.8 + 0.4 * drive.closeness));
}

export function shouldSpeak(drive, options = {}, now = new Date()) {
  const threshold = options.threshold ?? 0.62;
  const minGap = (options.minGapMinutes ?? 90) * 60000;
  const cap = options.dailyCap ?? 6;

  if (drive.spokenCount >= cap) return { speak: false, reason: `今天已经主动说了 ${drive.spokenCount} 次，到上限` };
  if (drive.lastSpoke) {
    const since = now - Date.parse(drive.lastSpoke);
    if (since < minGap) return { speak: false, reason: `距上次 ${Math.round(since / 60000)} 分钟，还在冷却` };
  }
  const u = urge(drive);
  if (u < threshold) return { speak: false, reason: `渴望度 ${u.toFixed(2)} 未到 ${threshold}` };
  return { speak: true, urge: u };
}

export function recordSpoke(drive, now = new Date()) {
  drive.lastSpoke = now.toISOString();
  drive.spokenCount += 1;
  drive.social = clamp(drive.social - 0.6);
  drive.lonely = clamp(drive.lonely - 0.45);
  return drive;
}

/** 给 prompt 用的一句话心情。 */
export function moodText(drive) {
  const bits = [];
  if (drive.social > 0.6) bits.push('有点想找他说话');
  else if (drive.social < 0.25) bits.push('刚聊过，不是很急');
  if (drive.lonely > 0.55) bits.push('他好久没出现了');
  if (drive.energy < 0.35) bits.push('有点困');
  else if (drive.energy > 0.7) bits.push('精神不错');
  if (drive.closeness > 0.75) bits.push('跟他很亲近');
  return bits.length ? bits.join('，') : '情绪平稳';
}

if (process.argv[1] && process.argv[1].endsWith('drive.mjs')) {
  const drive = evolve(loadDrive(), new Date());
  saveDrive(drive);
  console.log(`精神 ${drive.energy.toFixed(2)} ｜ 想说话 ${drive.social.toFixed(2)} ｜ 被冷落 ${drive.lonely.toFixed(2)} ｜ 亲密 ${drive.closeness.toFixed(2)}`);
  console.log(`渴望 ${urge(drive).toFixed(2)} ｜ 今天已说 ${drive.spokenCount} 次 ｜ ${moodText(drive)}`);
  const verdict = shouldSpeak(drive, {});
  console.log(verdict.speak ? '→ 现在会开口' : `→ 不说话（${verdict.reason}）`);
}
