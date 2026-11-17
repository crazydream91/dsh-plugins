// Minimal WebSocket (RFC 6455) implementation: client + server handshake + frame codec.
// No dependencies — the DSH runtime's Electron-as-node has no npm packages available.
import { createHash, randomBytes } from 'node:crypto';
import { EventEmitter } from 'node:events';
import net from 'node:net';
import tls from 'node:tls';

const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

const acceptKey = (key) => createHash('sha1').update(key + GUID).digest('base64');

/** Build one unmasked/masked frame. */
export function encodeFrame(opcode, payload, mask = false) {
  const len = payload.length;
  let header;
  if (len < 126) {
    header = Buffer.alloc(2);
    header[1] = len;
  } else if (len < 65536) {
    header = Buffer.alloc(4);
    header[1] = 126;
    header.writeUInt16BE(len, 2);
  } else {
    header = Buffer.alloc(10);
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(len), 2);
  }
  header[0] = 0x80 | opcode;
  if (!mask) return Buffer.concat([header, payload]);
  header[1] |= 0x80;
  const key = randomBytes(4);
  const body = Buffer.allocUnsafe(len);
  for (let i = 0; i < len; i += 1) body[i] = payload[i] ^ key[i & 3];
  return Buffer.concat([header, key, body]);
}

/** Incremental frame parser: feed chunks, get complete messages. */
export class FrameDecoder {
  #buf = Buffer.alloc(0);
  #fragments = [];
  #fragmentOpcode = 0;

  push(chunk) {
    this.#buf = this.#buf.length === 0 ? chunk : Buffer.concat([this.#buf, chunk]);
    const frames = [];
    for (;;) {
      const frame = this.#take();
      if (frame === null) break;
      frames.push(frame);
    }
    return frames;
  }

  #take() {
    const b = this.#buf;
    if (b.length < 2) return null;
    const fin = (b[0] & 0x80) !== 0;
    const opcode = b[0] & 0x0f;
    const masked = (b[1] & 0x80) !== 0;
    let len = b[1] & 0x7f;
    let off = 2;
    if (len === 126) {
      if (b.length < 4) return null;
      len = b.readUInt16BE(2);
      off = 4;
    } else if (len === 127) {
      if (b.length < 10) return null;
      len = Number(b.readBigUInt64BE(2));
      off = 10;
    }
    let maskKey = null;
    if (masked) {
      if (b.length < off + 4) return null;
      maskKey = b.subarray(off, off + 4);
      off += 4;
    }
    if (b.length < off + len) return null;
    let payload = b.subarray(off, off + len);
    if (masked) {
      const plain = Buffer.allocUnsafe(len);
      for (let i = 0; i < len; i += 1) plain[i] = payload[i] ^ maskKey[i & 3];
      payload = plain;
    }
    this.#buf = b.subarray(off + len);
    if (opcode === 0x0) {
      this.#fragments.push(payload);
      if (!fin) return null;
      const whole = Buffer.concat(this.#fragments);
      this.#fragments = [];
      return { opcode: this.#fragmentOpcode, payload: whole };
    }
    if (opcode === 0x8 || opcode === 0x9 || opcode === 0xa) return { opcode, payload };
    if (!fin) {
      this.#fragments = [payload];
      this.#fragmentOpcode = opcode;
      return null;
    }
    return { opcode, payload };
  }
}

/** Wire a decoded frame set onto an EventEmitter, handling pings and closes. */
function pump(decoder, socket, emitter, maskOutgoing = false) {
  socket.on('data', (chunk) => {
    let frames;
    try {
      frames = decoder.push(chunk);
    } catch (error) {
      emitter.emit('error', error);
      return;
    }
    for (const frame of frames) {
      if (frame.opcode === 0x9) {
        socket.write(encodeFrame(0xa, frame.payload, maskOutgoing));
      } else if (frame.opcode === 0xa) {
        // pong: ignore
      } else if (frame.opcode === 0x8) {
        socket.end(encodeFrame(0x8, Buffer.alloc(0), maskOutgoing));
      } else if (frame.opcode === 0x1) {
        emitter.emit('message', frame.payload.toString('utf8'));
      } else if (frame.opcode === 0x2) {
        emitter.emit('binary', frame.payload);
      }
    }
  });
}

/** WebSocket client: `for await`-free, event based, reconnects are the caller's job. */
export class WsClient extends EventEmitter {
  #socket;

  static connect(url, { headers = {}, timeoutMs = 15000 } = {}) {
    const client = new WsClient();
    client.#handshake(url, headers, timeoutMs);
    return client;
  }

  #handshake(url, headers, timeoutMs) {
    const parsed = new URL(url);
    const secure = parsed.protocol === 'wss:';
    if (!secure && parsed.protocol !== 'ws:') throw new Error(`只支持 ws:// 或 wss://（不支持 ${parsed.protocol}）`);
    const key = randomBytes(16).toString('base64');
    const port = Number(parsed.port || (secure ? 443 : 80));
    const socket = secure
      ? tls.connect({ host: parsed.hostname, port, servername: parsed.hostname })
      : net.connect(port, parsed.hostname);
    this.#socket = socket;
    let handshakeDone = false;
    let pending = Buffer.alloc(0);

    const timer = setTimeout(() => {
      if (!handshakeDone) {
        socket.destroy();
        this.emit('error', new Error('WebSocket 握手超时'));
      }
    }, timeoutMs);
    timer.unref?.();

    socket.on('connect', () => {
      const lines = [
        `GET ${parsed.pathname || '/'}${parsed.search} HTTP/1.1`,
        `Host: ${parsed.host}`,
        'Upgrade: websocket',
        'Connection: Upgrade',
        `Sec-WebSocket-Key: ${key}`,
        'Sec-WebSocket-Version: 13',
      ];
      for (const [name, value] of Object.entries(headers)) lines.push(`${name}: ${value}`);
      socket.write(lines.join('\r\n') + '\r\n\r\n');
    });

    socket.on('data', (chunk) => {
      if (handshakeDone) return;
      pending = Buffer.concat([pending, chunk]);
      const end = pending.indexOf('\r\n\r\n');
      if (end === -1) return;
      const head = pending.subarray(0, end).toString('latin1');
      const rest = pending.subarray(end + 4);
      if (!/^HTTP\/1\.1 101/u.test(head)) {
        clearTimeout(timer);
        socket.destroy();
        this.emit('error', new Error(`WebSocket 握手被拒绝：${head.split('\r\n')[0]}`));
        return;
      }
      if (!head.includes(acceptKey(key))) {
        clearTimeout(timer);
        socket.destroy();
        this.emit('error', new Error('WebSocket 握手响应校验失败'));
        return;
      }
      clearTimeout(timer);
      handshakeDone = true;
      const decoder = new FrameDecoder();
      if (rest.length > 0) {
        for (const frame of decoder.push(rest)) {
          if (frame.opcode === 0x1) this.emit('message', frame.payload.toString('utf8'));
        }
      }
      pump(decoder, socket, this, true);
      this.emit('open');
    });

    socket.on('close', () => {
      clearTimeout(timer);
      this.emit('close');
    });
    socket.on('error', (error) => this.emit('error', error));
  }

  send(text) {
    if (this.#socket === null || this.#socket.destroyed) throw new Error('WebSocket 未连接');
    this.#socket.write(encodeFrame(0x1, Buffer.from(text, 'utf8'), true));
  }

  close() {
    try {
      this.#socket?.end(encodeFrame(0x8, Buffer.alloc(0), true));
    } catch {
      // ignore
    }
  }
}

/** Accept WS upgrades on a plain http.Server. */
export function attachWebSocketServer(server, onConnection) {
  server.on('upgrade', (req, socket) => {
    const key = req.headers['sec-websocket-key'];
    if (typeof key !== 'string' || req.headers.upgrade?.toLowerCase() !== 'websocket') {
      socket.destroy();
      return;
    }
    socket.write(
      [
        'HTTP/1.1 101 Switching Protocols',
        'Upgrade: websocket',
        'Connection: Upgrade',
        `Sec-WebSocket-Accept: ${acceptKey(key)}`,
        '\r\n',
      ].join('\r\n'),
    );
    socket.setNoDelay(true);
    const peer = new EventEmitter();
    peer.socket = socket;
    peer.send = (text) => socket.write(encodeFrame(0x1, Buffer.from(text, 'utf8')));
    peer.close = () => socket.destroy();
    socket.on('close', () => peer.emit('close'));
    pump(new FrameDecoder(), socket, peer);
    onConnection(peer);
  });
}
