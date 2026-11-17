/** One-shot: send a text message to a group or a private chat.
 *
 *   node send-text.mjs --group 1029363018 --text "你好"
 *   node send-text.mjs --to 1072609541 --file reply.txt
 */
import { readFileSync } from 'node:fs';
import { WsClient } from './ws-lite.mjs';

const cfg = JSON.parse(readFileSync(new URL('./config.json', import.meta.url), 'utf8'));
const args = process.argv.slice(2);
const pick = (flag) => {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : undefined;
};

const group = pick('--group') ? Number(pick('--group')) : null;
const to = Number(pick('--to') ?? cfg.owner ?? cfg.allow.users[0]);
const text = pick('--file')
  ? readFileSync(pick('--file'), 'utf8').trim()
  : pick('--text');
if (!text) {
  console.error('用法: node send-text.mjs [--group 群号 | --to QQ号] --text "内容" | --file path');
  process.exit(2);
}

const headers = {};
if (cfg.onebot?.accessToken) headers.Authorization = `Bearer ${cfg.onebot.accessToken}`;
const sock = WsClient.connect(cfg.onebot.wsUrl, { headers });

const params = group
  ? { message_type: 'group', group_id: group, message: [{ type: 'text', data: { text } }] }
  : { message_type: 'private', user_id: to, message: [{ type: 'text', data: { text } }] };

sock.once('open', () => {
  sock.send(JSON.stringify({ action: 'send_msg', params, echo: 'text-1' }));
});

sock.on('message', (raw) => {
  let msg;
  try { msg = JSON.parse(raw); } catch { return; }
  if (msg.echo !== 'text-1') return;
  console.log('结果:', JSON.stringify(msg));
  process.exit(msg.retcode === 0 ? 0 : 1);
});

sock.on('error', (e) => { console.error('连接失败:', e?.message ?? e); process.exit(1); });
setTimeout(() => { console.error('超时：NapCat 没回应（3001 端口在听吗？）'); process.exit(1); }, 20000);
