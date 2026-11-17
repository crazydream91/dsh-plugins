// One-shot pusher: send a text or image to a QQ through the running NapCat.
// Usage: node push.mjs <qq> text "你好"
//        node push.mjs <qq> image "D:\\path\\to\\pic.png"
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { WsClient } from './ws-lite.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const config = JSON.parse(readFileSync(join(here, 'config.json'), 'utf8'));

const [to, kind, ...rest] = process.argv.slice(2);
const payload = rest.join(' ');
if (!to || !kind || !payload) {
  console.error('usage: node push.mjs <qq> <text|image> <payload>');
  process.exit(2);
}

const headers = {};
if (config.onebot?.accessToken) headers.Authorization = `Bearer ${config.onebot.accessToken}`;

const message = kind === 'image'
  ? [{ type: 'image', data: { file: payload } }]
  : [{ type: 'text', data: { text: payload } }];

const client = WsClient.connect(config.onebot.wsUrl, { headers });
let done = false;

/** Send one action and resolve with its response. */
function call(action, params, timeoutMs = 20000) {
  return new Promise((accept, reject) => {
    const echo = `p${Date.now()}`;
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
    let res = await call('send_private_msg', { user_id: Number(to), message });
    console.log('第一次尝试:', JSON.stringify(res));

    // some NapCat builds refuse bare local paths for images: retry as base64
    if (kind === 'image' && res?.status !== 'ok' && res?.retcode !== 0) {
      const b64 = readFileSync(payload).toString('base64');
      res = await call('send_private_msg', {
        user_id: Number(to),
        message: [{ type: 'image', data: { file: `base64://${b64}` } }],
      });
      console.log('base64 重试:', JSON.stringify(res).slice(0, 200));
    }

    done = true;
    client.close();
    process.exit(0);
  } catch (error) {
    console.error('发送失败:', error.message);
    process.exit(1);
  }
});

client.on('error', (error) => {
  if (!done) console.error('连接错误:', error.message);
  process.exit(1);
});

setTimeout(() => { if (!done) { console.error('整体超时'); process.exit(1); } }, 30000);
