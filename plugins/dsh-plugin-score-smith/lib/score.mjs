// 起 Python 侧的制谱内核，把 stdout 的 JSON 事件翻译成回调。
import { existsSync } from 'node:fs';
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { CommandError, slugify, stamp, tailLines, runCommand } from './util.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
export const WORKER_SCRIPT = path.join(here, '..', 'py', 'score_worker.py');

export function pythonEnv(extra = {}) {
  return {
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    PYTHONUNBUFFERED: '1',
    PYTHONDONTWRITEBYTECODE: '1',
    ...extra,
  };
}

export async function runWorker({ pythonPath, script = WORKER_SCRIPT, args = [], env, signal, onEvent }) {
  let resultEvent = null;
  let errorEvent = null;
  let stderrTail = '';

  const res = await runCommand(pythonPath, [script, ...args], {
    env: pythonEnv(env),
    signal,
    onStdoutLine(line) {
      const text = line.trim();
      if (!text || !text.startsWith('{')) return;
      let event;
      try {
        event = JSON.parse(text);
      } catch {
        return;
      }
      if (event.type === 'result') resultEvent = event;
      else if (event.type === 'error') errorEvent = event;
      try {
        onEvent?.(event);
      } catch {
        /* 回调里出错不影响主流程 */
      }
    },
    onStderrLine(line) {
      stderrTail = `${stderrTail}\n${line}`.slice(-12000);
    },
  });

  if (res.spawnError) {
    throw new CommandError(
      `启动 Python 失败（${pythonPath}）：${res.spawnError.message}\n`
      + '检查 config.pythonPath 是不是指向 C:\\dsh-tools\\score-venv\\Scripts\\python.exe。',
    );
  }
  if (errorEvent?.message) {
    throw new CommandError(errorEvent.message, { traceback: errorEvent.traceback });
  }
  if (res.code !== 0 || !resultEvent) {
    if (res.aborted) throw new CommandError('制谱被中断。');
    throw new CommandError(
      `制谱进程退出（exit ${res.code}）。\n${tailLines(stderrTail, 12) || '没有更多错误输出。'}`,
    );
  }
  return { result: resultEvent, stderrTail };
}

// ---------------------------------------------------------------- 项目目录

export async function createProject(workRoot, { title, input }) {
  const projectId = `${stamp()}-${slugify(title || path.basename(input || 'score'))}`;
  const projectDir = path.join(workRoot, projectId);
  await mkdir(projectDir, { recursive: true });
  return { projectId, projectDir, manifestPath: path.join(projectDir, 'manifest.json') };
}

export async function readManifest(projectDir) {
  const file = path.join(projectDir, 'manifest.json');
  if (!existsSync(file)) return null;
  try {
    return JSON.parse(await readFile(file, 'utf8'));
  } catch {
    return null;
  }
}

export async function writeManifest(manifestPath, manifest) {
  const payload = { ...manifest, updatedAt: new Date().toISOString() };
  await writeFile(manifestPath, JSON.stringify(payload, null, 2), 'utf8');
  return payload;
}

export async function listProjects(workRoot, limit = 10) {
  if (!existsSync(workRoot)) return [];
  let names = [];
  try {
    names = await readdir(workRoot, { withFileTypes: true });
  } catch {
    return [];
  }
  const dirs = names.filter((entry) => entry.isDirectory()).map((entry) => entry.name).sort().reverse();
  const out = [];
  for (const name of dirs.slice(0, Math.max(limit * 3, limit))) {
    const dir = path.join(workRoot, name);
    const manifest = await readManifest(dir);
    if (!manifest) continue;
    out.push({ projectId: name, dir, manifest });
    if (out.length >= limit) break;
  }
  return out;
}

/** 支持 projectId、项目目录、或 score.json 路径。 */
export async function resolveProject(workRoot, ref) {
  const raw = String(ref ?? '').trim();
  if (!raw) {
    const recent = await listProjects(workRoot, 1);
    if (!recent.length) throw new CommandError(`还没有任何曲谱项目（工作目录 ${workRoot}）。`);
    return recent[0];
  }
  const candidates = [];
  if (/[\\/]/.test(raw)) {
    candidates.push(raw);
    candidates.push(path.join(raw, '..'));
  } else {
    candidates.push(path.join(workRoot, raw));
  }
  for (const dir of candidates) {
    const manifest = await readManifest(dir);
    if (manifest) return { projectId: path.basename(dir), dir, manifest };
  }
  const recent = await listProjects(workRoot, 50);
  const hit = recent.find((item) => item.projectId === raw || item.manifest.title === raw);
  if (hit) return hit;
  throw new CommandError(`找不到这个曲谱项目：${raw}（工作目录 ${workRoot}）`);
}

export function projectFiles(projectDir) {
  return {
    musicxml: path.join(projectDir, 'score.musicxml'),
    midi: path.join(projectDir, 'score.mid'),
    json: path.join(projectDir, 'score.json'),
    previewPng: path.join(projectDir, 'preview.png'),
    previewHtml: path.join(projectDir, 'preview.html'),
    manifest: path.join(projectDir, 'manifest.json'),
  };
}
