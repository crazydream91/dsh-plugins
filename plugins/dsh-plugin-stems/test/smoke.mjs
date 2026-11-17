// 冒烟测试：直接把 Node 侧的几个模块跑一遍，不经过 DSH。
// 用法：node test/smoke.mjs <媒体文件> [工作目录]
import path from 'node:path';
import { mkdirSync } from 'node:fs';
import { probeMedia, extractAudio, mixStems } from '../lib/media.mjs';
import { runSeparation } from '../lib/separate.mjs';
import { formatBytes, formatDuration } from '../lib/util.mjs';

const FFMPEG = process.env.DSH_STEMS_FFMPEG || 'D:\\dsh 大肥鱼\\voice\\bin\\ffmpeg.exe';
const PYTHON = process.env.DSH_STEMS_PYTHON || 'D:\\dsh 大肥鱼\\voice\\tts\\venv\\Scripts\\python.exe';
const TORCH_HOME = process.env.DSH_STEMS_TORCH_HOME || 'D:\\dsh 大肥鱼\\voice\\cache';

const input = process.argv[2];
const work = process.argv[3] || path.join(process.cwd(), 'stems-smoke');
if (!input) {
  console.error('用法: node test/smoke.mjs <媒体文件> [工作目录]');
  process.exit(2);
}
mkdirSync(work, { recursive: true });

const step = (msg) => console.log(`\n== ${msg}`);

step('probe');
const probe = await probeMedia({ ffmpeg: FFMPEG, file: input });
console.log(JSON.stringify({
  format: probe.format,
  duration: formatDuration(probe.durationSec),
  size: formatBytes(probe.sizeBytes),
  video: probe.video,
  audio: probe.audio,
}, null, 2));

step('extract audio');
const audio = path.join(work, 'input.wav');
await extractAudio({ ffmpeg: FFMPEG, input, output: audio });
console.log('wrote', audio);

step('separate (htdemucs, vocals two-stems)');
const started = Date.now();
const { result } = await runSeparation({
  pythonPath: PYTHON,
  input: audio,
  outDir: path.join(work, 'stems'),
  model: 'htdemucs',
  device: 'auto',
  twoStems: 'vocals',
  format: 'wav',
  torchHome: TORCH_HOME,
  onProgress({ pct }) {
    process.stdout.write(`\r  ${Math.round(pct)}%   `);
  },
});
process.stdout.write('\n');
console.log(JSON.stringify(result, null, 2));
console.log('wall', ((Date.now() - started) / 1000).toFixed(1), 's');

step('mix drums+bass? (fetch 6s model too, only 15s audio)');
const six = await runSeparation({
  pythonPath: PYTHON,
  input: audio,
  outDir: path.join(work, 'stems6'),
  model: 'htdemucs_6s',
  device: 'auto',
  format: 'wav',
  torchHome: TORCH_HOME,
  onProgress({ pct }) {
    process.stdout.write(`\r  ${Math.round(pct)}%   `);
  },
});
process.stdout.write('\n');
console.log(six.result.stems.map((s) => `${s.name} ${formatDuration(s.durationSec)} peak ${s.peakDb}dB ${formatBytes(s.bytes)}`).join('\n'));

step('mix two stems');
const out = path.join(work, 'mix-test.wav');
await mixStems({
  ffmpeg: FFMPEG,
  inputs: six.result.stems.filter((s) => s.name === 'drums' || s.name === 'bass').map((s) => ({ path: s.path, gainDb: 0 })),
  output: out,
  format: 'wav',
});
console.log('wrote', out, formatBytes((await import('node:fs')).statSync(out).size));

step('done');
console.log(work);
