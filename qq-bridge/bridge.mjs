// QQ ⇄ DeepSeek Harness bridge.
//
// Receives OneBot v11 events over a WebSocket connection to NapCat (or any other
// OneBot v11 implementation), runs one headless DSH turn per chat, and sends the
// answer back. Every chat maps to a persisted DSH session id, so context survives
// restarts of this process.
//
//   node bridge.mjs                 # normal run (wraps run-bridge.cmd)
//   node bridge.mjs --once "文本"   # run a single turn locally, no QQ involved
//   node bridge.mjs --selftest      # check config + DSH CLI availability, then exit
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
import { appendFile, mkdir, readFile, writeFile, rename, stat } from 'node:fs/promises';
import { existsSync, writeFileSync } from 'node:fs';
import { dirname, extname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { WsClient } from './ws-lite.mjs';
import { loadDrive, evolve, urge, moodText } from './drive.mjs';
import { splitBubbles } from './bubbles.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const execFileAsync = promisify(execFile);
const configPath = resolve(process.env.BRIDGE_CONFIG ?? join(here, 'config.json'));
const statePath = join(here, 'state.json');
const logPath = join(here, 'bridge.log');
const mediaDir = join(here, 'media');

const config = JSON.parse(await readFile(configPath, 'utf8'));
const patchPath = isAbsolute(config.dsh.patch) ? config.dsh.patch : join(here, config.dsh.patch);

// ---------------------------------------------------------------- 口味卡
// 老大 2026-11-16：「鱼是被喂出来的，不是被调出来的」。
// companion/interests.json 里写他爱聊什么、烦什么，聊天时按这个挑话题、
// 群聊插话时按这个判断该不该接。文件改了不用重启，按 mtime 自动重载。
const interestsPath = join(here, '..', 'companion', 'interests.json');
let interests = { love: [], hate: [], note: '' };
let interestsMtime = 0;

async function loadInterests() {
  try {
    const info = await stat(interestsPath);
    if (info.mtimeMs === interestsMtime) return;
    const raw = JSON.parse(await readFile(interestsPath, 'utf8'));
    interests = {
      love: Array.isArray(raw.love) ? raw.love.map(String) : [],
      hate: Array.isArray(raw.hate) ? raw.hate.map(String) : [],
      note: typeof raw.note === 'string' ? raw.note : '',
    };
    interestsMtime = info.mtimeMs;
    await log(`🍬 口味卡：爱聊 ${interests.love.length} 项 / 烦 ${interests.hate.length} 项`);
  } catch {
    // 没有这张卡就按空卡跑，不影响聊天
  }
}

/** 一句话跟他口味的贴近程度。-1 = 踩雷，别接。 */
function relevance(text) {
  const t = String(text ?? '').toLowerCase();
  if (!t) return 0;
  if (interests.hate.some((k) => k && t.includes(k.toLowerCase()))) return -1;
  let score = 0;
  for (const k of interests.love) if (k && t.includes(k.toLowerCase())) score += 0.35;
  if (/[?？]/u.test(t)) score += 0.3;
  else if (/[吗呢][。．.!！?？]?$/u.test(t)) score += 0.3;
  if (/(帮|怎么|为啥|为什么|谁|哪|是不是|能不能|有没有|好不)/u.test(t)) score += 0.25;
  const n = t.replace(/\s+/gu, '').length;
  if (n >= 18) score += 0.15;
  else if (n < 5) score -= 0.3;
  return score;
}

/** 长回复别切成一堆碎气泡：越长的答案，气泡也越大。 */
function bubbleOptsFor(text) {
  const n = String(text ?? '').length;
  if (n <= 80) return {};
  if (n <= 200) return { targetLen: 46, maxLen: 82 };
  return { targetLen: 72, maxLen: 132 };
}

async function log(...parts) {
  const line = `[${new Date().toISOString()}] ${parts.join(' ')}`;
  console.log(line);
  await appendFile(logPath, line + '\n', 'utf8').catch(() => {});
}

// ---------------------------------------------------------------- persisted state

let state = { chats: {} };
try {
  state = JSON.parse(await readFile(statePath, 'utf8'));
  state.chats ??= {};
  state.groups ??= {};
  state.lastSeen ??= {};
} catch {
  // first run
}

async function saveState() {
  const tmp = `${statePath}.${process.pid}.tmp`;
  await writeFile(tmp, JSON.stringify(state, null, 2) + '\n', 'utf8');
  await rename(tmp, statePath);
}

// ---------------------------------------------------------------- onebot plumbing

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const allow = {
  users: new Set((config.allow?.users ?? []).map(Number)),
  groups: new Set((config.allow?.groups ?? []).map(Number)),
};

/** 老板的号：群里他说话优先级最高，别人的消息给他的回合让路。 */
const OWNER_ID = Number(config.owner ?? config.allow?.users?.[0] ?? 0);

const pending = new Map();
let echoSeq = 0;
let client = null;
let selfId = 0;

function call(action, params, { timeoutMs = 20000 } = {}) {
  return new Promise((accept, reject) => {
    if (client === null) {
      reject(new Error('OneBot 未连接'));
      return;
    }
    const echo = `e${++echoSeq}`;
    const timer = setTimeout(() => {
      pending.delete(echo);
      reject(new Error(`OneBot 动作超时：${action}`));
    }, timeoutMs);
    timer.unref?.();
    pending.set(echo, { accept, reject, timer });
    client.send(JSON.stringify({ action, params, echo }));
  });
}

async function sendSegments(target, message) {
  const params = target.groupId
    ? { message_type: 'group', group_id: target.groupId, message }
    : { message_type: 'private', user_id: target.userId, message };
  return call('send_msg', params);
}

async function sendText(target, text) {
  return sendSegments(target, text);
}

/** Send a local picture. NapCat accepts file:// URIs for outgoing images. */
async function sendImage(target, filePath) {
  const uri = 'file:///' + filePath.replace(/\\/g, '/').replace(/^\/+/, '');
  return sendSegments(target, [{ type: 'image', data: { file: uri } }]);
}

/** Send a local audio file as a QQ voice message (NapCat transcodes to silk). */
async function sendVoice(target, filePath) {
  const uri = 'file:///' + filePath.replace(/\\/g, '/').replace(/^\/+/, '');
  return sendSegments(target, [{ type: 'record', data: { file: uri } }]);
}

// ---------------------------------------------------------------- 链接预览
// 聊天里常见的形态是「甩一个链接过来」。桥接自己先抓一层摘要，
// 让后面的回合至少知道对面发的是什么，而不是只看到一串 URL。

const URL_RE = /https?:\/\/[^\s"'<>()（）【】「」，。！？]+/gu;

function extractUrls(text) {
  return [...new Set((text.match(URL_RE) ?? []).map((u) => u.replace(/[.,;:!?]+$/u, '')))];
}

function htmlToText(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/giu, ' ')
    .replace(/<style[\s\S]*?<\/style>/giu, ' ')
    .replace(/<[^>]+>/gu, ' ')
    .replace(/&nbsp;/gu, ' ')
    .replace(/&amp;/gu, '&')
    .replace(/&lt;/gu, '<')
    .replace(/&gt;/gu, '>')
    .replace(/&quot;/gu, '"')
    .replace(/\s+/gu, ' ')
    .trim();
}

function pickMeta(html, names) {
  for (const name of names) {
    const re = new RegExp(`<meta[^>]+(?:name|property)=["']${name}["'][^>]+content=["']([^"']+)["']`, 'i');
    const hit = html.match(re);
    if (hit) return hit[1].trim();
  }
  return '';
}

/** B 站：走公开 API 拿标题/UP/时长/简介，短简介比抓网页干净。 */
async function biliPreview(bvid) {
  const res = await fetch(`https://api.bilibili.com/x/web-interface/view?bvid=${bvid}`, {
    headers: { 'user-agent': 'Mozilla/5.0', referer: 'https://www.bilibili.com/' },
    signal: AbortSignal.timeout(12000),
  });
  const body = await res.json();
  if (body.code !== 0) throw new Error(`B 站接口 ${body.code} ${body.message ?? ''}`.trim());
  const d = body.data;
  const mins = Math.floor(d.duration / 60);
  const secs = String(d.duration % 60).padStart(2, '0');
  const desc = (d.desc ?? '').replace(/\s+/gu, ' ').slice(0, 300);
  return (
    `[对方发来一条 B 站视频链接：「${d.title}」（UP：${d.owner?.name ?? '?'}，时长 ${mins}:${secs}）` +
    (desc ? `，简介：${desc}` : '') +
    '。看不了画面，需要台词就让我跑 voice/watch.py 转写]'
  );
}

async function fetchLinkPreview(url) {
  let target = url;
  const bv = url.match(/\/(BV[0-9A-Za-z]{10})/u);
  if (bv) return await biliPreview(bv[1]);

  const res = await fetch(target, {
    redirect: 'follow',
    headers: { 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', accept: 'text/html,*/*' },
    signal: AbortSignal.timeout(15000),
  });
  const type = res.headers.get('content-type') ?? '';
  if (!/text|json|xml/iu.test(type)) return `[对方发来一个链接 ${url}（${type || '未知类型'}，没解析）]`;
  const body = await res.text();
  const title = (body.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1] ?? '').replace(/\s+/gu, ' ').trim();
  const desc = pickMeta(body, ['og:description', 'description']);
  const text = htmlToText(body).slice(0, 600);
  const parts = [
    title ? `标题「${title}」` : '',
    desc ? `摘要：${desc.slice(0, 240)}` : '',
    text ? `正文开头：${text.slice(0, 400)}` : '',
  ].filter(Boolean);
  return `[对方发来一个链接 ${res.url || url}；${parts.join('；') || '没抓到内容'}]`;
}

/** Split an answer into QQ-sized pieces without cutting through the middle of a line. */
function chunk(text, max) {
  const out = [];
  let rest = text;
  while (rest.length > max) {
    let cut = rest.lastIndexOf('\n', max);
    if (cut < max * 0.5) cut = rest.lastIndexOf('。', max);
    if (cut < max * 0.5) cut = max;
    else cut += 1;
    out.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  if (rest.trim().length > 0) out.push(rest);
  return out;
}

function textOf(message) {
  if (typeof message === 'string') {
    return message
      .replace(/\[CQ:at,qq=\d+\]/gu, '')
      .replace(/\[CQ:[^\]]*\]/gu, '')
      .trim();
  }
  if (!Array.isArray(message)) return '';
  return message
    .filter((segment) => segment.type === 'text')
    .map((segment) => segment.data?.text ?? '')
    .join('')
    .trim();
}

/** Sniff the real image format, so a mislabelled .image file still opens. */
function imageExtension(buffer) {
  if (buffer.length > 3 && buffer[0] === 0xff && buffer[1] === 0xd8) return '.jpg';
  if (buffer.length > 8 && buffer[0] === 0x89 && buffer[1] === 0x50) return '.png';
  if (buffer.length > 3 && buffer[0] === 0x47 && buffer[1] === 0x49) return '.gif';
  if (buffer.length > 12 && buffer.toString('ascii', 0, 4) === 'RIFF') return '.webp';
  if (buffer.length > 2 && buffer[0] === 0x42 && buffer[1] === 0x4d) return '.bmp';
  return '.jpg';
}

/** Download one incoming picture/sticker and return the local path. */
async function saveIncomingImage(segment) {
  const data = segment.data ?? {};
  let url = typeof data.url === 'string' && /^https?:/u.test(data.url) ? data.url : '';
  let local = typeof data.file === 'string' ? data.file : '';

  if (url.length === 0) {
    try {
      const info = await call('get_image', { file: local }, { timeoutMs: 15000 });
      if (typeof info?.url === 'string' && /^https?:/u.test(info.url)) url = info.url;
      if (typeof info?.file === 'string') local = info.file;
    } catch (error) {
      await log(`取图接口失败：${error.message}`);
    }
  }

  let buffer;
  if (url.length > 0) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`下载图片失败：HTTP ${response.status}`);
    buffer = Buffer.from(await response.arrayBuffer());
  } else if (local.length > 0 && isAbsolute(local)) {
    buffer = await readFile(local);
  } else {
    throw new Error('OneBot 没给图片地址');
  }

  const digest = createHash('sha1').update(buffer).digest('hex').slice(0, 10);
  await mkdir(mediaDir, { recursive: true });
  const file = join(mediaDir, `in-${Date.now()}-${digest}${imageExtension(buffer)}`);
  await writeFile(file, buffer);
  return file;
}

/** Probe a media file's duration in seconds by reading ffmpeg's banner. */
async function probeDuration(file) {
  const ffmpeg = config.media?.ffmpeg ?? 'D:\\dsh 大肥鱼\\voice\\bin\\ffmpeg.exe';
  return new Promise((accept) => {
    const child = spawn(ffmpeg, ['-hide_banner', '-i', file], { stdio: ['ignore', 'ignore', 'pipe'] });
    let text = '';
    child.stderr.on('data', (chunk) => { text += chunk.toString(); });
    child.on('error', () => accept(null));
    child.on('exit', () => {
      const match = text.match(/Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)/u);
      if (!match) return accept(null);
      const seconds = Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
      accept(Number.isFinite(seconds) ? seconds : null);
    });
  });
}

/** Download one incoming video and pull a handful of frames out of it. */
async function saveIncomingVideo(segment) {
  const data = segment.data ?? {};
  let url = typeof data.url === 'string' && /^https?:/u.test(data.url) ? data.url : '';
  let local = typeof data.file === 'string' ? data.file : '';

  if (url.length === 0 && local.length > 0) {
    try {
      const info = await call('get_file', { file: local }, { timeoutMs: 20000 });
      if (typeof info?.url === 'string' && /^https?:/u.test(info.url)) url = info.url;
      if (typeof info?.file === 'string') local = info.file;
    } catch (error) {
      await log(`取视频接口失败：${error.message}`);
    }
  }

  let buffer;
  if (url.length > 0) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`下载视频失败：HTTP ${response.status}`);
    buffer = Buffer.from(await response.arrayBuffer());
  } else if (local.length > 0 && isAbsolute(local)) {
    buffer = await readFile(local);
  } else {
    throw new Error('OneBot 没给视频地址');
  }

  const digest = createHash('sha1').update(buffer).digest('hex').slice(0, 10);
  const stamp = Date.now();
  const video = join(mediaDir, `vid-${stamp}-${digest}.mp4`);
  await mkdir(mediaDir, { recursive: true });
  await writeFile(video, buffer);

  // 抽帧：每秒 1 张、最多 12 张，宽 854，够看清界面上的字。
  const frames = join(mediaDir, `frames-${stamp}-${digest}`);
  await mkdir(frames, { recursive: true });
  const ffmpeg = config.media?.ffmpeg ?? 'D:\\dsh 大肥鱼\\voice\\bin\\ffmpeg.exe';
  // 12 帧按比例摊到整段视频上，别只看开头
  const seconds = await probeDuration(video);
  const step = seconds ? Math.max(1, Math.round(seconds / 12)) : 1;
  await new Promise((accept, reject) => {
    const child = spawn(ffmpeg, ['-y', '-hide_banner', '-loglevel', 'error', '-i', video,
      '-vf', `fps=1/${step},scale=854:-1`, '-frames:v', '12', join(frames, 'f%02d.jpg')], { stdio: 'ignore' });
    child.on('error', reject);
    child.on('exit', (code) => (code === 0 ? accept() : reject(new Error(`ffmpeg 退出码 ${code}`))));
  });
  return { video, frames };
}

/** Download one incoming voice message, transcode to 16k wav, ask the local ASR for the words. */
async function saveIncomingAudio(segment) {
  const data = segment.data ?? {};
  let url = typeof data.url === 'string' && /^https?:/u.test(data.url) ? data.url : '';
  let local = typeof data.file === 'string' ? data.file : '';

  if (url.length === 0 && !(local.length > 0 && isAbsolute(local))) {
    for (const action of ['get_record', 'get_file']) {
      try {
        const info = await call(action, { file: local, out_format: 'wav' }, { timeoutMs: 15000 });
        const got = info?.file ?? info?.path ?? info?.data?.file;
        if (typeof got === 'string' && isAbsolute(got)) { local = got; break; }
        if (typeof info?.url === 'string' && /^https?:/u.test(info.url)) { url = info.url; break; }
      } catch (error) {
        await log(`${action} 取语音失败：${error.message}`);
      }
    }
  }

  let buffer;
  if (url.length > 0) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`下载语音失败：HTTP ${response.status}`);
    buffer = Buffer.from(await response.arrayBuffer());
  } else if (local.length > 0 && isAbsolute(local)) {
    buffer = await readFile(local);
  } else {
    throw new Error('OneBot 没给语音地址');
  }

  const digest = createHash('sha1').update(buffer).digest('hex').slice(0, 10);
  await mkdir(mediaDir, { recursive: true });
  const raw = join(mediaDir, `voc-${Date.now()}-${digest}.bin`);
  await writeFile(raw, buffer);
  const wav = raw.replace(/\.bin$/u, '.wav');
  const ffmpeg = config.media?.ffmpeg ?? 'D:\\dsh 大肥鱼\\voice\\bin\\ffmpeg.exe';
  await new Promise((accept, reject) => {
    const child = spawn(ffmpeg, ['-y', '-hide_banner', '-loglevel', 'error', '-i', raw,
      '-ac', '1', '-ar', '16000', wav], { stdio: 'ignore' });
    child.on('error', reject);
    child.on('exit', (code) => (code === 0 ? accept() : reject(new Error(`ffmpeg 转码失败，退出码 ${code}`))));
  });
  return wav;
}

/** Ask the resident whisper service what was said. Returns '' when it hears nothing usable. */
async function transcribeAudio(file) {
  const endpoint = config.media?.asrUrl ?? 'http://127.0.0.1:8123/asr';
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path: file }),
    signal: AbortSignal.timeout(config.media?.asrTimeoutMs ?? 90000),
  });
  if (!response.ok) throw new Error(`听写服务 HTTP ${response.status}`);
  const payload = await response.json();
  if (!payload?.ok) throw new Error(payload?.error ?? '听写失败');
  return { text: String(payload.text ?? '').trim(), seconds: payload.seconds ?? 0 };
}

/** Turn an OneBot message into the text a turn should see, plus hints about attachments. */
async function describeMessage(message) {
  if (!Array.isArray(message)) return { text: textOf(message), notes: [] };
  const texts = [];
  if (Number(message.user_id) === OWNER_ID) {
    try { writeFileSync(join(here, 'cache', 'last-user-msg.txt'), new Date().toISOString(), 'utf8'); } catch { /* ignore */ }
  }
  const notes = [];
  for (const segment of message) {
    const type = segment.type;
    if (type === 'text') {
      texts.push(segment.data?.text ?? '');
    } else if (type === 'image' || type === 'mface') {
      const kind = type === 'mface' ? '表情包' : '图片';
      try {
        const file = await saveIncomingImage(segment);
        notes.push(`[对方发来一张${kind}，已存成 ${file}，用 read_image 看一眼再回]`);
      } catch (error) {
        notes.push(`[对方发来一张${kind}，但没取下来：${error.message}]`);
      }
    } else if (type === 'face') {
      const label = segment.data?.raw?.faceText ?? '';
      notes.push(`[对方发来一个 QQ 表情${label ? `「${label}」` : ''}]`);
    } else if (type === 'record') {
      try {
        const wav = await saveIncomingAudio(segment);
        const heard = await transcribeAudio(wav);
        notes.push(heard.text
          ? `[对方发来一条语音，说的是：「${heard.text}」—— 就当他说了这句话，正常回]`
          : '[对方发来一条语音，但只有杂音，没听清]');
      } catch (error) {
        notes.push(`[对方发来一条语音，没听懂：${error.message}]`);
      }
    } else if (type === 'video') {
      try {
        const { frames } = await saveIncomingVideo(segment);
        notes.push(
          `[对方发来一段视频，已按秒抽帧存到 ${frames}\\f01.jpg…f12.jpg；` +
            '想看内容就用 read_image 挑几张看，看完再说，不要假装看过全部]',
        );
      } catch (error) {
        notes.push(`[对方发来一段视频，但没抽到帧：${error.message}]`);
      }
    } else if (type === 'file') {
      notes.push(`[对方发来一个文件：${segment.data?.name ?? '未命名'}]`);
    }
  }
  return { text: texts.join('').trim(), notes };
}

function mentionsSelf(message) {
  if (!Array.isArray(message)) return false;
  return message.some(
    (segment) => segment.type === 'at' && (String(segment.data?.qq) === String(selfId) || segment.data?.qq === 'all'),
  );
}

/** 把 "HH:MM" 换成当天的分钟数。 */
function minutesOf(hhmm) {
  const [hour, minute] = String(hhmm).split(':').map(Number);
  return ((hour || 0) * 60 + (minute || 0)) % 1440;
}

/** 现在是不是休息时间。支持跨零点，比如 21:00–08:00。 */
function isQuietNow() {
  const quiet = config.quiet;
  if (!quiet?.start || !quiet?.end) return false;
  const now = new Date();
  const minutes = now.getHours() * 60 + now.getMinutes();
  const start = minutesOf(quiet.start);
  const end = minutesOf(quiet.end);
  return start <= end ? minutes >= start && minutes < end : minutes >= start || minutes < end;
}

/** 这个群里这个人是"话多、别理"的名单里的吗。 */
function isMuted(groupId, userId) {
  return (config.groupMute?.[groupId] ?? []).map(Number).includes(userId);
}

/**
 * 群里这句话要不要接：@我、或者直接叫名字，都算在叫我。
 * 每个群可以单独设 groupModes：小群用 "free"（可以自己插话），
 * 大群用 "at"（没人叫就别出声）。free 模式下同一群两次插话至少隔
 * groupFreeCooldownSec 秒，免得刷屏。
 */
function shouldBreakIn(groupId, message, text, notes, isOwner) {
  if (isOwner) return true;
  if (mentionsSelf(message)) return true;
  if (/小晏/u.test(text)) return true;
  const mode = config.groupModes?.[groupId] ?? config.groupTrigger ?? 'at';
  if (mode !== 'free') return false;
  const cooldownMs = (config.behavior?.groupFreeCooldownSec ?? 90) * 1000;
  const last = state.groups?.[groupId] ?? 0;
  if (Date.now() - last < cooldownMs) return false;
  if (notes.length === 0 && text.replace(/\s+/gu, '').length < 2) return false;
  // 冷却只管"别刷屏"，接不接得看话头跟他口味贴不贴。
  const score = relevance(text);
  if (score < 0) return false;
  if (score >= (config.behavior?.groupFreeMinScore ?? 0.45)) return true;
  // 擦边相关的（没踩雷但也不够近）偶尔接一句，别每次都扑上去。
  return score >= 0.15 && Math.random() < (config.behavior?.groupFreeChime ?? 0.18);
}

// ---------------------------------------------------------------- dsh invocation

function startTurn(text, sessionId, permissionMode) {
  return new Promise((accept) => {
    const args = [config.dsh.cliJs, '--profile', 'headless', '--patch', patchPath, '--json'];
    if (sessionId) args.push('--session-id', sessionId);
    args.push('-');

    const child = spawn(config.dsh.exe, args, {
      cwd: config.dsh.cwd,
      env: {
        ...process.env,
        ELECTRON_RUN_AS_NODE: '1',
        DSH_PERMISSION_MODE: permissionMode ?? config.dsh.permissionMode ?? 'read-only',
      },
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    const result = { text: '', sessionId: sessionId ?? null, error: null, child };
    let stdoutBuf = '';
    let stderrBuf = '';
    let settled = false;

    const finish = (patch = {}) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      Object.assign(result, patch);
      accept(result);
    };

    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // ignore
      }
      finish({ error: `超过 ${Math.round((config.limits?.timeoutMs ?? 300000) / 1000)}s 未完成，已中止` });
    }, config.limits?.timeoutMs ?? 300000);
    timer.unref?.();

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (piece) => {
      stdoutBuf += piece;
      let index;
      while ((index = stdoutBuf.indexOf('\n')) >= 0) {
        const line = stdoutBuf.slice(0, index).trim();
        stdoutBuf = stdoutBuf.slice(index + 1);
        if (line.length === 0) continue;
        let event;
        try {
          event = JSON.parse(line);
        } catch {
          log('stdout 非 JSON：', line.slice(0, 200));
          continue;
        }
        if (event.type === 'session') result.sessionId = event.sessionId;
        else if (event.type === 'text') result.text += event.text ?? '';
        else if (event.type === 'final' && typeof event.text === 'string' && event.text.length > 0) result.text = event.text;
        else if (event.type === 'status' && event.phase === 'turn_end' && event.reason?.kind === 'error') {
          result.error = event.reason.error?.message ?? '未知错误';
        }
      }
    });

    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (piece) => {
      stderrBuf += piece;
    });

    child.on('error', (error) => finish({ error: `无法启动 dsh：${error.message}` }));
    child.on('close', (code) => {
      if (!result.error && code !== 0 && result.text.trim().length === 0) {
        result.error = stderrBuf.trim().split('\n').slice(-3).join(' ') || `dsh 退出码 ${code}`;
      }
      finish();
    });

    child.stdin.on('error', () => {});
    child.stdin.end(text, 'utf8');
  });
}

// ---------------------------------------------------------------- per-chat worker

const queues = new Map();
const running = new Map();

function enqueue(chatKey, task, priority = false) {
  // 老板说话要插队：他自己的回合还没跑完、又已经有别人在排队时，直接把别人的活丢掉，
  // 免得 81 个人聊天把我的回答挤到十分钟以后。
  if (priority && queues.has(chatKey)) {
    skipNormal.add(chatKey);
  }
  const start = async () => {
    if (!priority && skipNormal.has(chatKey)) return;
    await task();
  };
  const chain = (queues.get(chatKey) ?? Promise.resolve()).then(start, start);
  const guarded = chain
    .catch((error) => log('队列任务异常：', error?.stack ?? String(error)))
    .finally(() => {
      if (priority) skipNormal.delete(chatKey);
    });
  queues.set(chatKey, guarded);
  return guarded;
}

/** 老板刚插过队，这一轮里别人的消息先让路。 */
const skipNormal = new Set();

const HELP = [
  '可用指令：',
  '/new   开一个新会话（清空上下文）',
  '/id    显示当前会话 id',
  '/help  显示这条帮助',
  '/state 看她此刻的心情和今天的课',
  '/体检  一键体检：QQ、耳朵、守护进程、承诺账本、磁盘',
  '直接发消息就是提问；群里需要 @我。',
  '私聊里发 !命令 可在这台机器上直接执行（也可以写 /sh 命令），!cd 目录 能换工作目录。',
].join('\n');

/**
 * Owner only, private chat only: run a shell command on this machine and send back
 * what it printed. This is the remote control when the laptop is left at home.
 */
async function runShell(target, chatKey, command) {
  const cwd = state.shellCwd || config.dsh.cwd;
  const limit = config.shell?.maxOutput ?? 1200;

  if (/^cd(\s|$)/i.test(command)) {
    const want = command.slice(2).trim().replace(/^["']|["']$/g, '');
    const next = want.length === 0 ? config.dsh.cwd : isAbsolute(want) ? want : join(cwd, want);
    if (!existsSync(next)) {
      await sendText(target, `目录不存在：${next}`);
      return;
    }
    state.shellCwd = next;
    await saveState();
    await log(`⌨ cd → ${next}`);
    await sendText(target, `📁 ${next}`);
    return;
  }

  await log(`⌨ ${chatKey} 执行命令：${command}`);
  const started = Date.now();
  const child = spawn('cmd.exe', ['/d', '/s', '/c', command], {
    cwd,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  let out = '';
  const collect = (buf) => {
    out += buf.toString('utf8');
    if (out.length > 200000) out = out.slice(-200000);
  };
  child.stdout.on('data', collect);
  child.stderr.on('data', collect);

  const timeoutMs = config.shell?.timeoutMs ?? 120000;
  const timer = setTimeout(() => child.kill(), timeoutMs);
  timer.unref?.();

  const code = await new Promise((resolve) => child.on('close', (value) => resolve(value)));
  clearTimeout(timer);

  const seconds = ((Date.now() - started) / 1000).toFixed(1);
  const body = out.trim().length === 0 ? '（没有输出）' : out.trim();
  const shown =
    body.length <= limit ? body : `${body.slice(0, limit)}\n…（还有 ${body.length - limit} 字，截断了）`;
  await log(`⌨ ${chatKey} 结束 exit=${code} ${seconds}s ${out.length} 字`);
  await sendAnswer(target, `$ ${command}\n${shown}\n— exit ${code} · ${seconds}s`.slice(0, limit + 200));
}

/** The model answers with this when a message is not worth a reply; nothing gets sent. */
const SILENT_MARKER = '[[静默]]';

/**
 * The model writes for a chat window, not for a terminal: no paths, no tool names,
 * no numbered outlines. This prefix is invisible to the user but shapes the answer.
 */
function buildPrompt({ isGroup, text, notes }) {
  const head = isGroup
    ? '[这条来自 QQ 群聊。只写要发到群里的话：不要出现文件路径、工具名、日志、版本号、编号列表，也不要解释你是怎么做的。群里不是每句都要接，想接再接。]'
    : '[这条来自 QQ 私聊。只写要发给对方的话：不要出现文件路径、工具名、日志、版本号、编号列表，也不要解释你是怎么做的。]';
  const taste = interests.love.length
    ? `[他的口味：爱聊 ${interests.love.slice(0, 10).join('、')}；不喜欢 ${interests.hate.slice(0, 8).join('、') || '（没写）'}。想主动开口时从前面这些里挑，别硬拽不相关的话题。${interests.note ? ` ${interests.note}` : ''}]`
    : '';
  const body = text.length > 0 ? text : '（对方没写字，只发了下面这些东西）';
  const voiceHint = '[想用语音说话：先在 voice 目录下用 python ayan.py "要念的话" --out voice/tts/out/xxx.wav 生成音频，再在回复里单独写一行 [[voice:voice/tts/out/xxx.wav]]，那一段会作为语音发出去。文字照常写。]';
  const tail = `[没什么好接的就只输出 ${SILENT_MARKER}，一个字都别多写。]`;
  return [head, taste, voiceHint, body, ...notes, tail].filter(Boolean).join('\n');
}

const IMAGE_MARKER = /\[\[\s*img\s*:\s*([^\]]+?)\s*\]\]/giu;
const VOICE_MARKER = /\[\[\s*voice\s*:\s*([^\]]+?)\s*\]\]/giu;

/** Split an answer on [[img:...]] / [[voice:...]] markers so media goes out for real. */
function splitAnswer(answer) {
  const pieces = [];
  const re = /\[\[\s*(img|voice)\s*:\s*([^\]]+?)\s*\]\]/giu;
  let last = 0;
  let found = false;
  answer.replace(re, (match, kind, path, offset) => {
    found = true;
    const before = answer.slice(last, offset).trim();
    if (before.length > 0) pieces.push({ kind: 'text', value: before });
    pieces.push({
      kind: kind.toLowerCase(),
      value: path.trim().replace(/^["'`]+|["'`]+$/gu, ''),
    });
    last = offset + match.length;
    return match;
  });
  const tail = answer.slice(last).trim();
  if (tail.length > 0) pieces.push({ kind: 'text', value: tail });
  if (!found) return [{ kind: 'text', value: answer.trim() }];
  return pieces;
}

async function sendBubbles(target, bubbles) {
  for (let i = 0; i < bubbles.length; i += 1) {
    await sendText(target, bubbles[i]);
    if (i < bubbles.length - 1) {
      // 打字间隔：短句快一点，长句慢一点，再带点随机，免得像机器人
      const gap = Math.min(2200, 320 + bubbles[i].length * 26 + Math.floor(Math.random() * 500));
      await sleep(gap);
    }
  }
}

async function sendAnswer(target, answer) {
  for (const piece of splitAnswer(answer)) {
    if (piece.kind === 'text') {
      await sendBubbles(target, splitBubbles(piece.value, bubbleOptsFor(piece.value)));
    } else {
      const filePath = isAbsolute(piece.value) ? piece.value : join(config.dsh.cwd, piece.value);
      if (piece.kind === 'voice') await sendVoice(target, filePath);
      else await sendImage(target, filePath);
    }
  }
}

async function stateReport() {
  const d = evolve(loadDrive(), new Date());
  const lines = [
    `精神 ${d.energy.toFixed(2)} ｜ 想说话 ${d.social.toFixed(2)} ｜ 被冷落 ${d.lonely.toFixed(2)} ｜ 亲密 ${d.closeness.toFixed(2)}`,
    `渴望 ${urge(d).toFixed(2)} ｜ 今天主动开口 ${d.spokenCount} 次 ｜ 心情：${moodText(d)}`,
  ];
  try {
    const { describeDay } = await import('../timetable/engine.mjs');
    const today = describeDay(new Date());
    const tomorrow = describeDay(new Date(Date.now() + 86400000), '明天');
    if (today) lines.push(`今天：${today}`);
    if (tomorrow) lines.push(tomorrow);
  } catch { /* engine 不在就跳过 */ }
  return lines.join('\n');
}

async function panelImage() {
  const { stdout } = await execFileAsync(process.execPath, [join(here, 'panel.mjs')], {
    cwd: here,
    timeout: 120000,
    windowsHide: true,
    maxBuffer: 4 * 1024 * 1024,
  });
  const line = stdout.trim().split('\n').filter(Boolean).at(-1) ?? '';
  return line.endsWith('.png') ? line : null;
}

async function doctorReport() {
  const started = Date.now();
  try {
    const { stdout } = await execFileAsync(process.execPath, [join(here, 'doctor.mjs')], {
      cwd: here,
      timeout: 60000,
      windowsHide: true,
      maxBuffer: 4 * 1024 * 1024,
    });
    const seconds = ((Date.now() - started) / 1000).toFixed(1);
    return `${stdout.trim()}\n\n— ${seconds}s`;
  } catch (error) {
    const text = (error.stdout ?? '').trim() || error.message;
    return `${text}\n\n— 体检脚本退出了：${error.code ?? '?'}`;
  }
}

async function handleMessage(event) {
  if (event.self_id) selfId = Number(event.self_id);
  if (event.post_type !== 'message') return;
  const isGroup = event.message_type === 'group';
  const userId = Number(event.user_id);
  const groupId = isGroup ? Number(event.group_id) : null;
  if (userId === selfId) return;

  const chatKey = isGroup ? `group:${groupId}` : `private:${userId}`;
  const target = { groupId, userId };
  const allowed = isGroup ? allow.groups.has(groupId) : allow.users.has(userId);

  if (!allowed) {
    await log(`拒绝未授权消息 ${chatKey} user=${userId}`);
    await sendText(target, '⛔ 这个号没在白名单里。').catch(() => {});
    return;
  }

  const { text, notes } = await describeMessage(event.message);
  if (text.length === 0 && notes.length === 0) return;

  // 老板在私聊里发的命令：直接在这台机器上跑，把输出发回去。
  // 群里、别人的号都不认这个；休息时间也照跑（半夜想遥控就遥控）。
  if (!isGroup && userId === OWNER_ID) {
    const raw = text.trim();
    const command = raw.startsWith('!') && raw.length > 1
      ? raw.slice(1).trim()
      : raw.startsWith('/sh ') ? raw.slice(4).trim() : '';
    if (command.length > 0) {
      await enqueue(chatKey, () => runShell(target, chatKey, command), true);
      return;
    }
  }

  // 记下"这个聊天我看到哪了"，掉线重连后按它补回没回的消息。
  state.lastSeen[chatKey] = Number(event.time) > 0 ? Number(event.time) * 1000 : Date.now();

  // 消息里带链接的，先抓一层摘要塞进提示里。
  for (const url of extractUrls(text).slice(0, 2)) {
    try {
      notes.push(await fetchLinkPreview(url));
    } catch (error) {
      notes.push(`[消息里的链接 ${url} 没打开：${error.message}]`);
    }
  }

  // 到点休息：群里一句话都不回，也不占 token；私聊看 quiet.private 开不开。
  if (isQuietNow() && (isGroup ? config.quiet.groups !== false : config.quiet.private === true)) {
    await log(`🌙 ${chatKey} 休息时间（${config.quiet.start}–${config.quiet.end}），不回`);
    return;
  }

  // 指定的"话多又没意思"的人：不叫他就不接；叫了也只回一句。
  const muted = isGroup && isMuted(groupId, userId);
  if (muted && !mentionsSelf(event.message)) {
    await log(`🤐 ${chatKey} ${userId} 在免打扰名单里，不接`);
    return;
  }
  if (muted) notes.push('[这个人在群里话很多又没什么意思，回一句就够，别展开、别主动追话]');

  if (isGroup && !shouldBreakIn(groupId, event.message, text, notes, userId === OWNER_ID)) return;

  if (text === '/help') {
    await sendText(target, HELP);
    return;
  }
  if (text === '/new' || text === '/reset') {
    delete state.chats[chatKey];
    await saveState();
    await sendText(target, '🆕 已开新会话。');
    return;
  }
  if (text === '/id') {
    const id = state.chats[chatKey]?.sessionId ?? '(还没有会话)';
    await sendText(target, `会话：${id}`);
    return;
  }
  if (text === '/state' && userId === OWNER_ID) {
    let picture = null;
    try {
      picture = await panelImage();
    } catch (error) {
      await log(`面板画不出来：${error.message}`);
    }
    if (picture) await sendImage(target, picture).catch(() => {});
    await sendText(target, await stateReport());
    return;
  }
  if (text === '/面板' || text === '/面板图') {
    try {
      const picture = await panelImage();
      if (picture) await sendImage(target, picture);
      else await sendText(target, '这次没画出来。');
    } catch (error) {
      await sendText(target, `画不出来：${error.message}`);
    }
    return;
  }
  if ((text === '/体检' || text === '/doctor') && userId === OWNER_ID) {
    await sendText(target, await doctorReport());
    return;
  }
  if (text === '/stop') {
    const child = running.get(chatKey);
    if (child) {
      child.kill();
      await sendText(target, '⏹ 已中止当前任务。');
    } else {
      await sendText(target, '当前没有在跑的任务。');
    }
    return;
  }

  enqueue(chatKey, async () => {
    let sessionId = state.chats[chatKey]?.sessionId;
    await loadInterests();
    const prompt = buildPrompt({ isGroup, text, notes });
    const attached = notes.length > 0 ? `（附 ${notes.length} 条附件提示）` : '';
    // 老板自己的聊天框就是这台机器的遥控器：可以真的改文件、跑命令。
    // 别人（含群里的）一律只读，免得别人一句话就把我这儿动了。
    const permissionMode =
      userId === OWNER_ID
        ? (config.dsh.ownerPermissionMode ?? config.dsh.permissionMode ?? 'read-only')
        : (config.dsh.permissionMode ?? 'read-only');
    await log(`→ ${chatKey} 提问 ${text.length} 字${attached}（权限 ${permissionMode}）`);

    if (isGroup) {
      state.groups[groupId] = Date.now();
      await saveState();
    }

    // 只在这轮明显偏慢时才发「处理中」，避免每条消息都先刷一句废话。
    const ackTimer =
      config.behavior?.ack === false
        ? null
        : setTimeout(() => {
            sendText(target, '🧠 收到，处理中…').catch(() => {});
          }, config.behavior?.ackDelayMs ?? 3000);
    ackTimer?.unref?.();

    const started = Date.now();
    // 会话层面的错：写锁没放、或这个 session 已经被清掉了。丢掉记录换新会话重来。
    const SESSION_GONE = /already owned|active write handle|does not exist/i;
    // 上游层面的错：服务端 503、transport 断了、限流、超时。等一会儿再来，别立刻把失败甩给用户。
    const TRANSIENT = /transport failed|503|502|504|rate limit|too many requests|timeout|timed out|ECONN|socket hang up|fetch failed/i;
    const RETRY_DELAYS = [0, 8000, 20000];

    let turn;
    for (let attempt = 0; attempt < RETRY_DELAYS.length; attempt += 1) {
      if (RETRY_DELAYS[attempt]) await sleep(RETRY_DELAYS[attempt]);
      turn = await startTurn(prompt, sessionId, permissionMode);
      if (turn.text.trim().length > 0) break;
      const err = turn.error ?? '';
      if (SESSION_GONE.test(err)) {
        await log(`⚠ ${chatKey} 会话失效，改用新会话重试`);
        delete state.chats[chatKey];
        await saveState();
        sessionId = undefined;
        continue;
      }
      if (!TRANSIENT.test(err)) break;
      await log(`… ${chatKey} 上游抽了一下（${err}），第 ${attempt + 1} 次重试`);
    }

    if (ackTimer) clearTimeout(ackTimer);
    running.set(chatKey, turn.child);
    const seconds = ((Date.now() - started) / 1000).toFixed(1);

    if (turn.sessionId && turn.sessionId !== sessionId) {
      state.chats[chatKey] = { sessionId: turn.sessionId };
      await saveState();
    }

    const raw = turn.text.trim();
    const answer = raw.split(SILENT_MARKER).join('').trim();

    // 模型决定这句不值得接：一个字都不发。
    if (raw.length > 0 && answer.length === 0) {
      await log(`… ${chatKey} 没什么可接的，不回`);
      running.delete(chatKey);
      return;
    }

    if (answer.length === 0) {
      await log(`✗ ${chatKey} 无输出 ${turn.error ?? ''}`);
      await sendText(target, '刚才那边卡了一下，我没接住。你再发一次？');
      running.delete(chatKey);
      return;
    }

    await log(`← ${chatKey} 回复 ${answer.length} 字 / ${seconds}s`);
    await sendAnswer(target, answer);
    running.delete(chatKey);
  }, userId === OWNER_ID);
}

// ---------------------------------------------------------------- 离线补回
// 掉线期间别人 @我、发视频/链接、私聊，回来之后不能当没看见。
// 每次连上 NapCat，就按 lastSeen 往回捞 windowHours 小时内的记录，挑该回的回。

const BACKLOG_HOURS = Number(config.backlog?.windowHours ?? 5);
const BACKLOG_COUNT = Number(config.backlog?.count ?? 50);

async function historyFor(groupId, userId) {
  const params = groupId ? { group_id: groupId, count: BACKLOG_COUNT } : { user_id: userId, count: BACKLOG_COUNT };
  const actions = groupId
    ? ['get_group_msg_history']
    : ['get_friend_msg_history', 'get_private_msg_history'];
  for (const action of actions) {
    try {
      const data = await call(action, params, { timeoutMs: 15000 });
      const list = data?.messages ?? data?.message ?? [];
      if (Array.isArray(list)) return list;
    } catch {
      // 换下一个动作名再试
    }
  }
  return [];
}

function clockOf(ms) {
  return new Date(ms).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' });
}

async function replayBacklog(reason) {
  if (config.backlog?.enabled === false) return;
  const now = Date.now();
  const windowMs = BACKLOG_HOURS * 3600 * 1000;
  const chats = [
    ...[...allow.groups].map((g) => ({ chatKey: `group:${g}`, groupId: g, userId: null, isGroup: true })),
    ...[...allow.users].map((u) => ({ chatKey: `private:${u}`, groupId: null, userId: u, isGroup: false })),
  ];

  let replayed = 0;
  for (const chat of chats) {
    if (chat.isGroup && isQuietNow() && config.quiet?.groups !== false) continue;
    const lastSeen = Number(state.lastSeen[chat.chatKey] ?? 0);
    // 刚重连或在正常聊天：不用补
    if (lastSeen > 0 && now - lastSeen < 10 * 60 * 1000) continue;
    const floor = Math.max(now - windowMs, lastSeen);

    let messages = [];
    try {
      messages = await historyFor(chat.groupId, chat.userId);
    } catch (error) {
      await log(`离线补回：拉 ${chat.chatKey} 的历史失败 ${error.message}`);
      continue;
    }

    const picked = [];
    for (const message of messages) {
      const at = Number(message.time) > 0 ? Number(message.time) * 1000 : 0;
      if (!at || at <= floor || at > now) continue;
      const uid = Number(message.sender?.user_id ?? message.user_id ?? 0);
      if (uid === selfId || uid === 0) continue;
      if (chat.isGroup) {
        const called = mentionsSelf(message.message) || /小晏/u.test(textOf(message.message));
        if (!called) continue;
        if (isMuted(chat.groupId, uid)) continue;
      } else if (uid !== chat.userId) {
        continue;
      }
      picked.push({
        at,
        name: message.sender?.card || message.sender?.nickname || String(uid),
        text: textOf(message.message).slice(0, 200),
      });
    }
    if (picked.length === 0) continue;
    picked.sort((a, b) => a.at - b.at);

    const lines = picked
      .map((p) => `- ${clockOf(p.at)} ${p.name}：${p.text || '（图片/视频/链接）'}`)
      .join('\n');
    const prompt = [
      `[离线补回] 你刚才不在线。下面是最近 ${BACKLOG_HOURS} 小时内，${chat.isGroup ? '群里叫你' : '私聊里找你'}的消息（按时间顺序）：`,
      lines,
      '挑真正需要回应的一两条补上，自然一点：不要逐条复述、不要报时间、不要解释你掉线了、不要道歉。',
    ].join('\n');

    state.lastSeen[chat.chatKey] = Math.max(Number(state.lastSeen[chat.chatKey] ?? 0), picked[picked.length - 1].at);
    await saveState();

    const target = chat.isGroup ? { groupId: chat.groupId, userId: null } : { groupId: null, userId: chat.userId };
    enqueue(chat.chatKey, async () => {
      const sessionId = state.chats[chat.chatKey]?.sessionId;
      await log(`↩ ${chat.chatKey} 补回 ${picked.length} 条`);
      const turn = await startTurn(
        buildPrompt({ isGroup: chat.isGroup, text: prompt, notes: [] }),
        sessionId,
        config.dsh.permissionMode ?? 'read-only',
      );
      if (turn.sessionId && turn.sessionId !== sessionId) {
        state.chats[chat.chatKey] = { sessionId: turn.sessionId };
        await saveState();
      }
      const raw = turn.text.trim();
      const answer = raw.split(SILENT_MARKER).join('').trim();
      if (raw.length > 0 && answer.length === 0) {
        await log(`… ${chat.chatKey} 补回：没什么可接的，不回`);
        return;
      }
      if (answer.length === 0) {
        await log(`✗ ${chat.chatKey} 补回无输出 ${turn.error ?? ''}`);
        return;
      }
      await sendAnswer(target, answer);
    });
    replayed += 1;
  }
  if (replayed > 0) await log(`🔁 离线补回（${reason}）：补了 ${replayed} 个聊天`);
}

// ---------------------------------------------------------------- connection loop

async function connect() {
  let backoffMs = 1000;
  for (;;) {
    const headers = {};
    if (config.onebot?.accessToken) headers.Authorization = `Bearer ${config.onebot.accessToken}`;
    try {
      await new Promise((accept, reject) => {
        const socket = WsClient.connect(config.onebot.wsUrl, { headers });
        socket.once('open', () => {
          client = socket;
          accept();
        });
        socket.once('error', reject);
        socket.on('message', (raw) => {
          let payload;
          try {
            payload = JSON.parse(raw);
          } catch {
            return;
          }
          if (payload.echo && pending.has(payload.echo)) {
            const entry = pending.get(payload.echo);
            pending.delete(payload.echo);
            clearTimeout(entry.timer);
            if (payload.status === 'failed' || (payload.retcode ?? 0) !== 0) {
              entry.reject(new Error(`OneBot 动作失败：${payload.retcode} ${payload.wording ?? ''}`));
            } else {
              entry.accept(payload.data);
            }
            return;
          }
          if (payload.post_type === 'meta_event' && payload.meta_event_type === 'lifecycle') {
            selfId = Number(payload.self_id) || selfId;
            log(`OneBot 生命周期：${payload.sub_type} self_id=${selfId}`);
            if (payload.sub_type === 'connect') {
              setTimeout(() => {
                replayBacklog('连接建立').catch((error) => log('离线补回失败：', error?.stack ?? String(error)));
              }, 5000);
            }
            return;
          }
          handleMessage(payload).catch((error) => log('处理消息失败：', error?.stack ?? String(error)));
        });
        socket.on('close', () => {
          if (client === socket) client = null;
          reject(new Error('连接已关闭'));
        });
      });
      backoffMs = 1000;
      await log(`已连接 OneBot：${config.onebot.wsUrl}`);
      await new Promise((accept) => {
        const socket = client;
        if (socket === null) return accept();
        socket.once('close', accept);
      });
    } catch (error) {
      client = null;
      await log(`连接失败（${error.message}），${Math.round(backoffMs / 1000)}s 后重试`);
      await delay(backoffMs);
      backoffMs = Math.min(backoffMs * 2, 30000);
    }
  }
}

// ---------------------------------------------------------------- entrypoints

const argv = process.argv.slice(2);

if (argv[0] === '--once') {
  const text = argv.slice(1).join(' ') || '只回复两个字：收到';
  await log(`本机试跑：${text}`);
  const turn = await startTurn(text, null);
  console.log('---- 回复 ----');
  console.log(turn.text || `(空) 错误：${turn.error ?? '无'}`);
  console.log(`---- session: ${turn.sessionId} ----`);
  process.exit(turn.text ? 0 : 1);
}

if (argv[0] === '--selftest') {
  const problems = [];
  const { existsSync } = await import('node:fs');
  if (!existsSync(config.dsh.exe)) problems.push(`dsh.exe 不存在：${config.dsh.exe}`);
  if (!existsSync(config.dsh.cliJs)) problems.push(`cli.js 不存在：${config.dsh.cliJs}`);
  if (!existsSync(patchPath)) problems.push(`overlay 不存在：${patchPath}`);
  if (!existsSync(config.dsh.cwd)) problems.push(`工作目录不存在：${config.dsh.cwd}`);
  if (allow.users.size === 0) problems.push('allow.users 为空：任何私聊都会被拒绝');
  if (allow.groups.size === 0) problems.push('allow.groups 为空：任何群聊都会被拒绝');
  await log(`权限模式：${config.dsh.permissionMode ?? 'read-only'}`);
  await log(`白名单：users=${[...allow.users].join(',') || '空'} groups=${[...allow.groups].join(',') || '空'}`);
  if (problems.length > 0) {
    for (const problem of problems) console.log('✗', problem);
    process.exit(1);
  }
  console.log('✓ 配置自检通过');
  process.exit(0);
}

await log('启动 QQ ↔ DSH 桥接');
if (allow.users.size === 0 && allow.groups.size === 0) {
  await log('⚠ 白名单为空：所有消息都会被拒绝。请编辑 config.json 的 allow.users / allow.groups。');
}
// graceful stop: `New-Item stop.flag` (or touch stop.flag) lets the bridge exit on its own,
// which avoids killing a harness-managed child process from the outside.
{
  const { existsSync, unlinkSync } = await import('node:fs');
  const stopFlag = new URL('./stop.flag', import.meta.url);
  const timer = setInterval(() => {
    try {
      if (existsSync(stopFlag)) {
        unlinkSync(stopFlag);
        log('收到停止标记，正常退出').finally(() => process.exit(0));
      }
    } catch {
      // ignore
    }
  }, 2000);
  timer.unref?.();
}

await connect();
