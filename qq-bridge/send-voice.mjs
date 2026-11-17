/** One-shot: send a local audio file as a QQ voice message (tests the voice path).
 *
 *   node send-voice.mjs <音频路径> [--to QQ号] [--group 群号]
 */
import { readFileSync } from 'node:fs';
import { WsClient } from './ws-lite.mjs';

const cfg = JSON.parse(readFileSync(new URL('./config.json', import.meta.url), 'utf8'));
const args = process.argv.slice(2);
const file = args.find((a) => !a.startsWith('--') && !/^\d+$/.test(a));
if (!file) {
  console.error('用法: node send-voice.mjs <音频路径> [--to QQ号] [--group 群号]');
  process.exit(2);
}
const pick = (flag) => {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : undefined;
};
const to = Number(pick('--to') ?? cfg.owner ?? cfg.allow.users[0]);
const group = pick('--group') ? Number(pick('--group')) : null;
const uri = 'file:///' + file.replace(/\\/g, '/').replace(/^\/+/, '');

const headers = {};
if (cfg.onebot?.accessToken) headers.Authorization = `Bearer ${cfg.onebot.accessToken}`;
const sock = WsClient.connect(cfg.onebot.wsUrl, { headers });

const message = [{ type: 'record', data: { file: uri } }];
const params = group
  ? { message_type: 'group', group_id: group, message }
  : { message_type: 'private', user_id: to, message };

sock.once('open', () => {
  sock.send(JSON.stringify({ action: 'send_msg', params, echo: 'voice-1' }));
});

sock.on('message', (raw) => {
  let msg;
  try { msg = JSON.parse(raw); } catch { return; }
  if (msg.echo === 'voice-1') {
    console.log('结果:', JSON.stringify(msg));
    process.exit(msg.retcode === 0 ? 0 : 1);
  }
});

sock.on('error', (e) => { console.error('连接失败:', e?.message ?? e); process.exit(1); });
setTimeout(() => { console.error('超时：NapCat 没回应（3001 端口在听吗？）'); process.exit(1); }, 20000);
