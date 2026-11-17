// A fake NapCat: speaks OneBot v11 over WebSocket so the bridge can be tested
// without logging in any QQ account.
//
//   node mock-napcat.mjs
//   私聊： p <user_id> <文本>
//   群聊： g <group_id> <user_id> <文本>      （不加 @ 不会触发机器人）
//   群里@： g <group_id> <user_id> @<文本>
//   退出： q
import http from 'node:http';
import readline from 'node:readline';
import { attachWebSocketServer } from './ws-lite.mjs';

const port = Number(process.env.MOCK_PORT ?? 3001);
const selfId = 10000;
let peer = null;
let replies = 0;
let scriptStarted = false;

const server = http.createServer((_req, res) => {
  res.writeHead(200, { 'content-type': 'text/plain' });
  res.end('mock napcat\n');
});

attachWebSocketServer(server, (connection) => {
  peer = connection;
  console.log('✓ 桥接进程已连上');
  connection.on('message', (raw) => {
    let message;
    try {
      message = JSON.parse(raw);
    } catch {
      return;
    }
    if (message.action === 'send_msg') {
      const params = message.params;
      const where = params.message_type === 'group' ? `群 ${params.group_id}` : `私聊 ${params.user_id}`;
      console.log(`\n──── 机器人 → ${where} ────\n${params.message}\n`);
      // 不计「处理中」提示，只数真正的回答
      if (!String(params.message ?? '').startsWith('🧠')) replies += 1;
      const expect = Number(process.env.MOCK_EXPECT ?? 0);
      if (expect > 0 && replies >= expect) {
        console.log(`（已收到 ${replies} 条回复，脚本模式结束）`);
        setTimeout(() => process.exit(0), 800);
      }
    }
    connection.send(JSON.stringify({ status: 'ok', retcode: 0, data: { message_id: 1 }, echo: message.echo }));
  });
  connection.on('close', () => {
    console.log('✗ 桥接进程断开');
    peer = null;
  });
  if (!scriptStarted && process.env.MOCK_SCRIPT) {
    scriptStarted = true;
    setTimeout(runScript, 1500);
  }
});

server.listen(port, '127.0.0.1', () => {
  console.log(`mock NapCat 监听 ws://127.0.0.1:${port}`);
  console.log('输入消息：p <QQ号> <文本> / g <群号> <QQ号> [@]文本 / q 退出');
});

function emit(chat) {
  if (peer === null) {
    console.log('⚠ 还没有桥接进程连上来，先启动 run-bridge.cmd');
    return;
  }
  const event =
    chat.kind === 'group'
      ? {
          post_type: 'message',
          message_type: 'group',
          self_id: selfId,
          group_id: chat.groupId,
          user_id: chat.userId,
          message_id: Date.now() % 100000,
          message: [
            ...(chat.at ? [{ type: 'at', data: { qq: String(selfId) } }] : []),
            { type: 'text', data: { text: chat.at ? ` ${chat.text}` : chat.text } },
          ],
          raw_message: chat.text,
        }
      : {
          post_type: 'message',
          message_type: 'private',
          self_id: selfId,
          user_id: chat.userId,
          message_id: Date.now() % 100000,
          message: [{ type: 'text', data: { text: chat.text } }],
          raw_message: chat.text,
        };
  console.log(`\n>>> 发出：${chat.kind === 'group' ? `群 ${chat.groupId}` : `私聊 ${chat.userId}`} ${chat.text}`);
  peer.send(JSON.stringify(event));
}

/** 无人值守模式：按脚本文件逐条发消息，收到预期条数的回复后退出。 */
async function runScript() {
  const { readFile } = await import('node:fs/promises');
  const text = await readFile(process.env.MOCK_SCRIPT, 'utf8');
  const lines = text
    .split(/\r?\n/u)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith('#'));
  for (const line of lines) {
    const parts = line.split(/\s+/u);
    if (parts[0] === 'p' && parts.length >= 3) {
      emit({ kind: 'private', userId: Number(parts[1]), text: parts.slice(2).join(' ') });
    } else if (parts[0] === 'g' && parts.length >= 4) {
      const rest = parts.slice(3).join(' ');
      emit({
        kind: 'group',
        groupId: Number(parts[1]),
        userId: Number(parts[2]),
        at: rest.startsWith('@'),
        text: rest.startsWith('@') ? rest.slice(1) : rest,
      });
    }
    await new Promise((resolve) => setTimeout(resolve, Number(process.env.MOCK_GAP_MS ?? 2500)));
  }
  console.log('（脚本发完，等待回复…）');
  setTimeout(() => {
    console.log('（超时：没有收到预期的回复条数）');
    process.exit(2);
  }, Number(process.env.MOCK_HOLD_MS ?? 180000));
}

const rl = readline.createInterface({ input: process.stdin, output: process.stdout, prompt: '> ' });
rl.prompt();
rl.on('line', (line) => {
  const trimmed = line.trim();
  if (trimmed === 'q' || trimmed === 'quit') process.exit(0);
  const parts = trimmed.split(/\s+/);
  if (parts[0] === 'p' && parts.length >= 3) {
    emit({ kind: 'private', userId: Number(parts[1]), text: parts.slice(2).join(' ') });
  } else if (parts[0] === 'g' && parts.length >= 4) {
    const rest = parts.slice(3).join(' ');
    emit({
      kind: 'group',
      groupId: Number(parts[1]),
      userId: Number(parts[2]),
      at: rest.startsWith('@'),
      text: rest.startsWith('@') ? rest.slice(1) : rest,
    });
  } else if (trimmed.length > 0) {
    console.log('看不懂。格式：p <QQ号> <文本> 或 g <群号> <QQ号> [@]文本');
  }
  rl.prompt();
});
