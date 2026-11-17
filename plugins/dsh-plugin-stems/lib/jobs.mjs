// 每个分离任务一个目录，目录里的 job.json 是唯一的真相来源。
// 这样跨会话也能靠 jobId 把产物找回来，不用把路径记在对话里。
import { mkdir, readFile, readdir, stat, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { slugify, stamp } from './util.mjs';

export const MANIFEST_NAME = 'job.json';

export async function createJob(workRoot, { input, mode }) {
  const base = slugify(path.basename(String(input ?? 'media')));
  const jobId = `${stamp()}-${base}`;
  const jobDir = path.join(workRoot, jobId);
  await mkdir(jobDir, { recursive: true });
  return { jobId, jobDir, manifestPath: path.join(jobDir, MANIFEST_NAME) };
}

export async function writeManifest(manifestPath, manifest) {
  await mkdir(path.dirname(manifestPath), { recursive: true });
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
  return manifestPath;
}

export async function updateManifest(manifestPath, patch) {
  let current = {};
  try {
    current = JSON.parse(await readFile(manifestPath, 'utf8'));
  } catch {
    current = {};
  }
  const next = {
    ...current,
    ...patch,
    updatedAt: new Date().toISOString(),
  };
  await writeManifest(manifestPath, next);
  return next;
}

export async function readManifest(manifestPath) {
  const raw = await readFile(manifestPath, 'utf8');
  return JSON.parse(raw);
}

/** jobId / 目录路径 / job.json 路径，都能定位到同一个任务。 */
export async function resolveJob(workRoot, ref) {
  const raw = String(ref ?? '').trim();
  if (!raw) return null;
  const candidates = [];
  if (/[\\/]/.test(raw) || /^[a-zA-Z]:/.test(raw)) {
    candidates.push(path.join(raw, MANIFEST_NAME), raw);
  } else {
    candidates.push(path.join(workRoot, raw, MANIFEST_NAME));
  }
  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      try {
        const manifest = await readManifest(candidate);
        return { manifest, jobDir: path.dirname(candidate), manifestPath: candidate };
      } catch {
        /* 换下一个候选 */
      }
    }
  }
  return null;
}

export async function listJobs(workRoot, limit = 10) {
  let entries = [];
  try {
    entries = await readdir(workRoot, { withFileTypes: true });
  } catch {
    return [];
  }
  const jobs = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const manifestPath = path.join(workRoot, entry.name, MANIFEST_NAME);
    if (!existsSync(manifestPath)) continue;
    try {
      const manifest = await readManifest(manifestPath);
      let mtime = 0;
      try {
        mtime = (await stat(manifestPath)).mtimeMs;
      } catch {
        mtime = 0;
      }
      jobs.push({ ...manifest, jobDir: path.join(workRoot, entry.name), manifestPath, mtime });
    } catch {
      /* 坏掉的 manifest 跳过 */
    }
  }
  jobs.sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? '') || b.mtime - a.mtime);
  return jobs.slice(0, Math.max(1, limit));
}
