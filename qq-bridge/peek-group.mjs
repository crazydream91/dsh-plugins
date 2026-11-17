/** One-shot: dump the last N messages of a group, flagging videos and images.
 *
 *   node peek-group.mjs <群号> [条数]
 */
import { readFileSync } from 'node:fs';
import { WsClient } from './ws-lite.mjs';

const cfg = JSON.parse(readFileSync(new URL('./config.json', import.meta.url), 'utf8'));
const [groupArg, countArg] = process.argv.slice(2).filter((a) => !a.startsWith('--'));
const groupId = Number(groupArg);
const count = Number(countArg ?? 30);

const headers = {};
if (cfg.onebot?.accessToken) headers.Authorization = `Bearer ${cfg.onebot.accessToken}`;
const sock = WsClient.connect(cfg.onebot.wsUrl, { headers });

sock.once('open', () => {
  sock.send(JSON.stringify({
    action: 'get_group_msg_history',
    params: { group_id: groupId, count, reverse_order: false },
    echo: 'peek-1',
  }));
});

sock.on('message', (raw) => {
  let msg;
  try { msg = JSON.parse(raw); } catch { return; }
  if (msg.echo !== 'peek-1') return;
  const list = msg?.data?.messages ?? [];
  for (const m of list) {
    const sender = m.sender?.card || m.sender?.nickname || m.user_id;
    const when = new Date(Number(m.time) * 1000).toLocaleString('zh-CN', { hour12: false });
    const parts = [];
    let video = null;
    for (const seg of m.message ?? []) {
      if (seg.type === 'text') parts.push(seg.data.text);
      else if (seg.type === 'image') parts.push(`[image ${seg.data.file}]`);
      else if (seg.type === 'video') {
        video = seg.data;
        parts.push(`[VIDEO file=${seg.data.file} url=${(seg.data.url ?? '').slice(0, 80)}]`);
      } else if (seg.type === 'record') parts.push(`[record ${seg.data.file}]`);
      else parts.push(`[${seg.type}]`);
    }
    console.log(`${when} · ${sender}(${m.user_id}) · ${parts.join(' ').slice(0, 400)}`);
    if (video) console.log(`      message_id=${m.message_id} file=${video.file} url=${video.url ?? ''}`);
  }
  process.exit(0);
});

sock.on('error', (e) => { console.error('连接失败:', e?.message ?? e); process.exit(1); });
setTimeout(() => { console.error('超时：NapCat 没回应'); process.exit(1); }, 20000);
