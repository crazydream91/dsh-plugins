// List the bot account's QQ friends.
// Usage: node friends.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { WsClient } from './ws-lite.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const config = JSON.parse(readFileSync(join(here, 'config.json'), 'utf8'));
const headers = {};
if (config.onebot?.accessToken) headers.Authorization = `Bearer ${config.onebot.accessToken}`;

const client = WsClient.connect(config.onebot.wsUrl, { headers });

function call(action, params, timeoutMs = 20000) {
  return new Promise((accept, reject) => {
    const echo = `f${Date.now()}`;
    const timer = setTimeout(() => reject(new Error(`timeout: ${action}`)), timeoutMs);
    const onMessage = (text) => {
      let frame;
      try { frame = JSON.parse(text); } catch { return; }
      if (frame.echo !== echo) return;
      clearTimeout(timer);
      client.off('message', onMessage);
      accept(frame);
    };
    client.on('message', onMessage);
    client.send(JSON.stringify({ action, params, echo }));
  });
}

client.on('open', async () => {
  try {
    const res = await call('get_friend_list', {});
    const list = res?.data ?? [];
    for (const friend of list) {
      const name = friend.remark || friend.nickname || '';
      console.log(`${friend.user_id}\t${name}`);
    }
    if (list.length === 0) console.log('（一个好友都没有）');
    client.close();
    process.exit(0);
  } catch (error) {
    console.error('查询失败:', error.message);
    process.exit(1);
  }
});

client.on('error', (error) => { console.error('连接错误:', error.message); process.exit(1); });
setTimeout(() => { console.error('整体超时'); process.exit(1); }, 20000);
