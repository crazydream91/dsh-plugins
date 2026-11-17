// 把一段回复拆成「像人发消息」的几条短消息。
// 空行/换行先分块；块内按句号问号感叹号再切；太短的并到邻句，太长的按逗号补切。
// 导出给 bridge.mjs 用，也方便单独测试（node bubbles.mjs "..."）。

const SENTENCE_END = /(?<=[。！？!?…~～；;])/u;

function soft(s) {
  return String(s).replace(/\s+/gu, ' ').trim();
}

export function splitBubbles(text, opts = {}) {
  const targetLen = opts.targetLen ?? 26;
  const maxLen = opts.maxLen ?? 44;
  const lines = String(text ?? '')
    .replace(/\r\n/gu, '\n')
    .split(/\n+/u)
    .map(soft)
    .filter(Boolean);

  const raw = [];
  for (const line of lines) raw.push(...line.split(SENTENCE_END).map(soft).filter(Boolean));

  const bubbles = [];
  let buf = '';
  for (const piece of raw) {
    if (!buf) buf = piece;
    else if (buf.length + piece.length <= targetLen) buf += piece;
    else {
      bubbles.push(buf);
      buf = piece;
    }
    while (buf.length > maxLen) {
      const cut = Math.max(buf.lastIndexOf('，'), buf.lastIndexOf(','), buf.lastIndexOf(' '));
      const at = cut > 18 ? cut + 1 : maxLen;
      bubbles.push(soft(buf.slice(0, at)));
      buf = soft(buf.slice(at));
    }
  }
  if (buf) bubbles.push(buf);
  return bubbles.length ? bubbles : [soft(text)];
}

if (process.argv[1] && process.argv[1].endsWith('bubbles.mjs') && process.argv[2]) {
  for (const b of splitBubbles(process.argv.slice(2).join(' '))) console.log(`· ${b} (${b.length})`);
}
