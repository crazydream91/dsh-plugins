/** 体检：一次看清"她还在不在正常工作"。
 *
 *   node doctor.mjs         # 人看的报告
 *   node doctor.mjs --json  # 给别的程序用
 */
import { readFileSync, existsSync, statSync, statfsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { connect } from 'node:net';
import { execFile } from 'node:child_process';
import { WsClient } from './ws-lite.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const config = JSON.parse(readFileSync(join(HERE, 'config.json'), 'utf8'));
const results = [];
const add = (level, name, detail) => results.push({ level, name, detail });

function portOpen(port, host = '127.0.0.1', timeout = 1200) {
  return new Promise((accept) => {
    const socket = connect({ port, host });
    const done = (value) => { socket.destroy(); accept(value); };
    socket.setTimeout(timeout);
    socket.once('connect', () => done(true));
    socket.once('timeout', () => done(false));
    socket.once('error', () => done(false));
  });
}

function pidAlive(file) {
  try {
    const pid = Number(readFileSync(file, 'utf8').trim());
    if (!pid) return null;
    process.kill(pid, 0);
    return pid;
  } catch {
    return null;
  }
}

function minutesSince(date) {
  return Math.round((Date.now() - date.getTime()) / 60000);
}

function tailLines(file, count = 60) {
  try {
    const text = readFileSync(file, 'utf8');
    return text.trimEnd().split('\n').slice(-count);
  } catch {
    return [];
  }
}

async function checkQQ() {
  if (!(await portOpen(3001))) {
    add('bad', 'QQ 连接', '3001 没在听，NapCat 可能没登录');
    return null;
  }
  const headers = {};
  if (config.onebot?.accessToken) headers.Authorization = `Bearer ${config.onebot.accessToken}`;
  const socket = WsClient.connect(config.onebot?.wsUrl ?? 'ws://127.0.0.1:3001', { headers });
  try {
    const info = await new Promise((accept, reject) => {
      const timer = setTimeout(() => reject(new Error('超时')), 6000);
      socket.once('error', (error) => { clearTimeout(timer); reject(error); });
      socket.on('message', (raw) => {
        let payload;
        try { payload = JSON.parse(raw); } catch { return; }
        if (payload.echo !== 'doctor') return;
        clearTimeout(timer);
        if (payload.retcode !== 0) reject(new Error(payload.wording ?? `retcode ${payload.retcode}`));
        else accept(payload.data);
      });
      socket.once('open', () => {
        socket.send(JSON.stringify({ action: 'get_login_info', params: {}, echo: 'doctor' }));
      });
    });
    add('ok', 'QQ 连接', `在线 ${info?.user_id ?? '?'}（${info?.nickname ?? '?'}）`);
    return info;
  } catch (error) {
    add('bad', 'QQ 连接', `3001 通但取账号信息失败：${error.message}`);
    return null;
  } finally {
    try { socket.close(); } catch { /* 已经关了 */ }
  }
}

async function checkEars() {
  try {
    const response = await fetch('http://127.0.0.1:8123/health', { signal: AbortSignal.timeout(3000) });
    const data = await response.json();
    add('ok', '耳朵（听写）', `在线，模型 ${data.model}`);
  } catch (error) {
    add('warn', '耳朵（听写）', `8123 不可用：${error.message}`);
  }
}

function checkWatchers() {
  const active = pidAlive(join(HERE, 'active.pid'));
  add(active ? 'ok' : 'bad', '主动开口服务', active ? `pid ${active}` : '没在跑（active.pid 失效）');

  const kick = pidAlive(join(HERE, 'kick-watch.pid'));
  add(kick ? 'ok' : 'warn', '掉线看门狗', kick ? `pid ${kick}` : '没在跑');

  const logPath = join(HERE, 'bridge.log');
  if (existsSync(logPath)) {
    const age = minutesSince(statSync(logPath).mtime);
    add('ok', '桥接日志', age <= 30 ? `${age} 分钟内有动静` : `最后更新在 ${age} 分钟前`);
  } else {
    add('warn', '桥接日志', '没有 bridge.log');
  }

  const errors = tailLines(logPath, 120).filter((line) => /失败|error|ECONNREFUSED|退出码/u.test(line));
  add(errors.length ? 'warn' : 'ok', '最近错误', errors.length ? `${errors.length} 行，最后一条：${errors.at(-1).slice(0, 90)}` : '近 120 行无报错');
}

function checkPromises() {
  try {
    const data = JSON.parse(readFileSync(join(HERE, '..', 'memory', 'schedule', 'promises.json'), 'utf8'));
    const pending = data.items.filter((item) => item.status === 'pending');
    const overdue = pending.filter((item) => item.due && Date.parse(item.due) < Date.now());
    const kept = data.items.filter((item) => item.status === 'kept').length;
    add(overdue.length ? 'warn' : 'ok', '承诺账本', `挂着 ${pending.length} 条（逾期 ${overdue.length}），已兑现 ${kept} 条`);
  } catch {
    add('warn', '承诺账本', '还没有账本文件');
  }
}

function checkSchedule() {
  const file = join(HERE, '..', 'memory', 'schedule', 'items.json');
  if (!existsSync(file)) {
    add('ok', '日程', '账本还是空的（还没往里写过日程）');
    return;
  }
  try {
    const raw = JSON.parse(readFileSync(file, 'utf8'));
    const list = Array.isArray(raw) ? raw : raw.items ?? [];
    const soon = list.filter((item) => {
      const at = Date.parse(item.at ?? item.time ?? '');
      return Number.isFinite(at) && at > Date.now() && at < Date.now() + 86400000;
    });
    add('ok', '日程', `共 ${list.length} 条，24 小时内有 ${soon.length} 条`);
  } catch (error) {
    add('warn', '日程', `items.json 读不懂：${error.message}`);
  }
}

function checkMemory() {
  try {
    const dir = join(HERE, '..', 'memory', 'projects');
    const cards = existsSync(dir) ? statSync(dir).isDirectory() : false;
    const files = cards ? readFileSync(join(HERE, '..', 'memory', 'INDEX.md'), 'utf8').split('\n').filter((l) => l.startsWith('|')).length - 2 : 0;
    const indexAge = minutesSince(statSync(join(HERE, '..', 'memory', 'INDEX.md')).mtime) / 60;
    add('ok', '记忆库', `${files} 张卡，索引 ${indexAge.toFixed(1)} 小时前生成`);
  } catch {
    add('warn', '记忆库', '索引读不到');
  }
}

function checkDisk() {
  for (const drive of ['C:', 'D:']) {
    try {
      const stat = statfsSync(`${drive}\\`);
      const free = (stat.bavail * stat.bsize) / 1024 ** 3;
      const level = free < 5 ? 'bad' : free < 15 ? 'warn' : 'ok';
      add(level, `${drive} 空间`, `剩 ${free.toFixed(1)} GB`);
    } catch (error) {
      add('warn', `${drive} 空间`, `读不到：${error.message}`);
    }
  }
}

function checkDrive() {
  try {
    const drive = JSON.parse(readFileSync(join(HERE, 'cache', 'drive-state.json'), 'utf8'));
    add('ok', '内驱力', `精神 ${Number(drive.energy ?? 0).toFixed(2)} · 想说话 ${Number(drive.social ?? 0).toFixed(2)} · 今天开口 ${drive.spokenCount ?? 0} 次`);
  } catch {
    add('warn', '内驱力', '还没有状态文件');
  }
}

/** 上游模型接口通不通、快不快。用于区分"我卡住了"和"api.deepseek.com 那头挂了"。 */
function checkUpstream() {
  const host = 'api.deepseek.com';
  const started = Date.now();
  return new Promise((accept) => {
    const socket = connect({ port: 443, host });
    let settled = false;
    const finish = (level, detail) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      add(level, '上游 api.deepseek.com', detail);
      accept();
    };
    socket.setTimeout(5000);
    socket.once('connect', () => finish(Date.now() - started > 1500 ? 'warn' : 'ok', `443 通，${Date.now() - started} ms`));
    socket.once('timeout', () => finish('bad', `5 秒连不上——这时候聊天会报 connect-timeout，不是消息丢了`));
    socket.once('error', (error) => finish('bad', `${error.code ?? error.message} —— 上游不通时消息发不出去`));
  });
}

/** 应用连跑太久时提醒重启（比重启电脑便宜）：连接池/DNS 老化只清得掉进程。 */
function checkAppUptime() {
  return new Promise((accept) => {
    const script = "$p = Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1; if ($p) { $p.StartTime.ToString('o') }";
    const b64 = Buffer.from(script, 'utf16le').toString('base64');
    execFile('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', b64], { timeout: 8000 }, (error, stdout) => {
      const raw = String(stdout ?? '').trim();
      const started = raw ? new Date(raw) : null;
      if (error || !started || Number.isNaN(started.getTime())) {
        add('warn', '应用运行时长', '读不到进程启动时间');
        return accept();
      }
      const hours = (Date.now() - started.getTime()) / 3600000;
      const age = hours >= 1 ? `${hours.toFixed(1)} 小时` : `${Math.round(hours * 60)} 分钟`;
      if (hours >= 12) add('warn', '应用运行时长', `${age}（从 ${started.toLocaleString('zh-CN')}）—— 再出现超时建议跑 restart-app.ps1，不用重启电脑`);
      else add('ok', '应用运行时长', age);
      accept();
    });
  });
}

async function main() {
  await checkQQ();
  await checkEars();
  await checkUpstream();
  await checkAppUptime();
  checkWatchers();
  checkPromises();
  checkSchedule();
  checkMemory();
  checkDrive();
  checkDisk();

  const bad = results.filter((r) => r.level === 'bad').length;
  const warn = results.filter((r) => r.level === 'warn').length;
  if (process.argv.includes('--json')) {
    console.log(JSON.stringify({ ok: bad === 0, bad, warn, results }, null, 2));
    return;
  }
  const icon = { ok: '✅', warn: '⚠️', bad: '❌' };
  console.log('小晏体检');
  for (const row of results) console.log(`${icon[row.level]} ${row.name}：${row.detail}`);
  console.log(`\n结论：${bad === 0 ? (warn === 0 ? '一切正常' : `${warn} 项要注意`) : `${bad} 项出问题了，先修`}`);
  process.exitCode = bad > 0 ? 1 : 0;
}

main();
