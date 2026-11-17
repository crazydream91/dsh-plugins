// 进程调用与格式化的小工具（从 dsh-plugin-stems 抄过来，插件之间不互相 import）。
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';

export function formatDuration(sec) {
  if (!Number.isFinite(sec) || sec < 0) return '—';
  const total = Math.round(sec);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

export function slugify(text, fallback = 'score') {
  const cleaned = String(text ?? '')
    .replace(/\.[a-z0-9]{1,5}$/i, '')
    .replace(/[^0-9A-Za-z\u4e00-\u9fa5_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48);
  return cleaned || fallback;
}

export function stamp(date = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}${p(date.getMonth() + 1)}${p(date.getDate())}-${p(date.getHours())}${p(date.getMinutes())}${p(date.getSeconds())}`;
}

/** 相对路径按 base 解析，绝对路径原样返回。 */
export function resolvePath(input, base) {
  const raw = String(input ?? '').trim();
  if (!raw) return '';
  if (/^[a-zA-Z]:[\\/]/.test(raw) || raw.startsWith('\\\\') || raw.startsWith('/')) {
    return path.normalize(raw);
  }
  return path.resolve(base || process.cwd(), raw);
}

export function existingFile(file) {
  return Boolean(file) && existsSync(file);
}

export class CommandError extends Error {
  constructor(message, extra = {}) {
    super(message);
    this.name = 'CommandError';
    Object.assign(this, extra);
  }
}

/**
 * 起一个子进程，收集输出。成功/失败都返回；调用方自己决定怎么抛错。
 * opts: { cwd, env, signal, onStdoutLine, onStderrLine, tailBytes }
 */
export function runCommand(command, args, opts = {}) {
  const { cwd, env, signal, onStdoutLine, onStderrLine, tailBytes = 64 * 1024 } = opts;

  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command, args, {
        cwd,
        env: env ? { ...process.env, ...env } : process.env,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      resolve({ code: -1, stdout: '', stderr: '', spawnError: error });
      return;
    }

    let stdoutTail = '';
    let stderrTail = '';
    let stdoutLines = '';
    let stderrLines = '';
    let aborted = false;

    const onAbort = () => {
      aborted = true;
      try {
        child.kill('SIGKILL');
      } catch {
        /* 进程可能已经退了 */
      }
    };
    if (signal) {
      if (signal.aborted) onAbort();
      else signal.addEventListener('abort', onAbort, { once: true });
    }

    if (child.stdout) {
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (chunk) => {
        stdoutTail = (stdoutTail + chunk).slice(-tailBytes);
        if (onStdoutLine) {
          stdoutLines += chunk;
          const parts = stdoutLines.split(/\r?\n/);
          stdoutLines = parts.pop() ?? '';
          for (const line of parts) onStdoutLine(line);
        }
      });
    }
    if (child.stderr) {
      child.stderr.setEncoding('utf8');
      child.stderr.on('data', (chunk) => {
        stderrTail = (stderrTail + chunk).slice(-tailBytes);
        if (onStderrLine) {
          stderrLines += chunk;
          const parts = stderrLines.split(/\r?\n/);
          stderrLines = parts.pop() ?? '';
          for (const line of parts) onStderrLine(line);
        }
      });
    }

    child.on('error', (error) => {
      signal?.removeEventListener?.('abort', onAbort);
      resolve({ code: -1, stdout: stdoutTail, stderr: stderrTail, spawnError: error });
    });
    child.on('close', (code, sig) => {
      signal?.removeEventListener?.('abort', onAbort);
      if (onStdoutLine && stdoutLines) onStdoutLine(stdoutLines);
      if (onStderrLine && stderrLines) onStderrLine(stderrLines);
      resolve({ code, signal: sig, stdout: stdoutTail, stderr: stderrTail, aborted });
    });
  });
}

/** stderr 的最后几行，用来拼人类可读的错误。 */
export function tailLines(text, count = 6) {
  return String(text ?? '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(-count)
    .join('\n');
}
