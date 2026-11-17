/** 盯着 NapCat 日志：一旦被 QQ 踢下线，就把二维码弹到桌面 + 弹一条通知。
 *
 *   连续被踢 5 次之后不再主动弹（只记日志），成功登录一次就重新计数。
 *
 *   node kick-watch.mjs [--no-toast]
 */
import { readdirSync, statSync, readFileSync, writeFileSync, existsSync, copyFileSync, appendFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { connect } from 'node:net';
import { uptime } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const LOG_DIR = join(HERE, 'napcat', 'napcat', 'logs');
const QR = join(HERE, 'napcat', 'napcat', 'cache', 'qrcode.png');
const QR_LATEST = join(HERE, 'qrcode-latest.png');
const STATE = join(HERE, 'cache', 'kick-state.json');
const WATCH_LOG = join(HERE, 'kick-watch.log');
const MAX_POPS = 5;
const TICK_MS = 5000;

const noToast = process.argv.includes('--no-toast');

const log = (...parts) =>
  appendLine(`[${new Date().toISOString()}] ${parts.join(' ')}`);

function appendLine(line) {
  try {
    appendFileSync(WATCH_LOG, line + '\n', 'utf8');
  } catch {
    /* ignore */
  }
  console.log(line);
}

function loadState() {
  try {
    return JSON.parse(readFileSync(STATE, 'utf8'));
  } catch {
    return { pops: 0, kicked: 0, lastFile: '', lastSize: 0, bootAt: 0 };
  }
}

function saveState(state) {
  writeFileSync(STATE, JSON.stringify(state, null, 2), 'utf8');
}

/** 这次开机的时刻（取整到分钟，避免每轮抖动）。 */
function bootStamp() {
  const ms = Date.now() - uptime() * 1000;
  return Math.floor(ms / 60000) * 60000;
}

/** 3001 有没有在听 = QQ 这会儿是登录着的。 */
function qqOnline() {
  return new Promise((accept) => {
    const socket = connect({ port: 3001, host: '127.0.0.1' });
    const finish = (value) => { socket.destroy(); accept(value); };
    socket.setTimeout(800);
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false));
    socket.once('error', () => finish(false));
  });
}

function newestLog() {
  try {
    const files = readdirSync(LOG_DIR)
      .filter((f) => f.endsWith('.log'))
      .map((f) => ({ f, m: statSync(join(LOG_DIR, f)).mtimeMs }))
      .sort((a, b) => b.m - a.m);
    return files[0]?.f ?? null;
  } catch {
    return null;
  }
}

function popup() {
  if (!existsSync(QR)) {
    log('二维码文件还不存在，跳过弹窗');
    return;
  }
  copyFileSync(QR, QR_LATEST);
  spawn('cmd.exe', ['/c', 'start', '', QR_LATEST], { detached: true, stdio: 'ignore' }).unref();
}

function toast(title, text) {
  if (noToast) return;
  const script = join(HERE, 'toast.ps1');
  if (!existsSync(script)) return;
  spawn('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-Title', title, '-Text', text], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
  }).unref();
}

const PIDFILE = join(HERE, 'kick-watch.pid');

function alreadyRunning() {
  try {
    const pid = Number(readFileSync(PIDFILE, 'utf8').trim());
    if (!pid || pid === process.pid) return false;
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function main() {
  if (alreadyRunning()) {
    console.log('已经有一个看门狗在跑，这个退出');
    return;
  }
  writeFileSync(PIDFILE, String(process.pid), 'utf8');
  let state = loadState();

  // 每次开机都从零开始数：关机重启之后，5 次额度重新给满。
  const boot = bootStamp();
  if (state.bootAt !== boot) {
    state.bootAt = boot;
    state.pops = 0;
    state.kicked = 0;
    saveState(state);
    log(`新的一次开机（${new Date(boot).toLocaleString('zh-CN')}），弹窗计数清零`);
  }

  log(`看门狗启动 pid=${process.pid}（最多主动弹 ${MAX_POPS} 次；重启开机或登录成功后重新计数）`);

  // 只管"启动之后新写进来的行"：老日志里的掉线记录不再翻出来重弹。
  const first = newestLog();
  if (first) {
    try {
      state.lastFile = first;
      state.lastSize = statSync(join(LOG_DIR, first)).size;
      saveState(state);
    } catch { /* 读不到就算了 */ }
  }

  setInterval(async () => {
    // 登录着就说明这次掉线已经处理完，额度跟着刷新。
    if ((state.pops ?? 0) > 0 && (await qqOnline())) {
      state.pops = 0;
      log('QQ 在线，弹窗计数清零');
      saveState(state);
    }

    const file = newestLog();
    if (!file) return;
    const path = join(LOG_DIR, file);
    let size;
    try {
      size = statSync(path).size;
    } catch {
      return;
    }
    // 换了日志文件（NapCat 重启）：只记位置，不回头翻旧账。
    if (file !== state.lastFile) {
      state.lastFile = file;
      state.lastSize = size;
      saveState(state);
      return;
    }
    const fromStart = size < (state.lastSize ?? 0);
    const startAt = fromStart ? 0 : state.lastSize ?? 0;
    if (size === startAt) return;

    let chunk = '';
    try {
      const fd = readFileSync(path, { encoding: 'utf8' });
      chunk = fd.slice(startAt);
    } catch {
      return;
    }
    state.lastFile = file;
    state.lastSize = size;

    const kicked = /KickedOffLine|账号已离线|登录已失效/.test(chunk);
    const back = /登录成功|账号状态变更为在线|WebSocket 服务 .*已启动/.test(chunk);

    if (back && state.pops !== 0) {
      state.pops = 0;
      log('登录恢复，计数清零');
    }

    if (kicked) {
      state.kicked = (state.kicked ?? 0) + 1;
      state.pops = (state.pops ?? 0) + 1;
      if (state.pops <= MAX_POPS) {
        log(`检测到掉线（第 ${state.pops}/${MAX_POPS} 次），弹二维码`);
        popup();
        toast('小晏掉线了', 'QQ 被踢下线，二维码已经弹到桌面，扫一下就能接上。');
      } else {
        log(`掉线第 ${state.pops} 次，已超过 ${MAX_POPS} 次，不再主动弹窗`);
      }
    }

    saveState(state);
  }, TICK_MS);

  process.on('SIGINT', () => process.exit(0));
  process.on('SIGTERM', () => process.exit(0));
}

main();
