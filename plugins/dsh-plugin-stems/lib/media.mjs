// ffmpeg 编排：探测、抽音频、混音、把音频合回视频。
// 这台机器没有 ffprobe，所以媒体信息都靠 `ffmpeg -i` 的 stderr 解析。
import path from 'node:path';
import { mkdirSync, statSync } from 'node:fs';
import { CommandError, runCommand, tailLines } from './util.mjs';

const INPUT_HEADER = /^(Input #0|\[.*@ 0x)/;

/** 输出目录不存在时 ffmpeg 会直接失败，统一先建目录。 */
function ensureParent(file) {
  const dir = path.dirname(file);
  if (dir && dir !== '.') mkdirSync(dir, { recursive: true });
}

function n(v) {
  const x = Number(v);
  return Number.isFinite(x) ? x : null;
}

/** 解析 `ffmpeg -i <file>` 的 stderr。 */
export function parseProbe(stderr, file) {
  const text = String(stderr ?? '');
  const info = {
    path: file,
    format: null,
    durationSec: null,
    bitrateKbps: null,
    video: null,
    audio: null,
    streams: 0,
  };

  const inputLine = text.split(/\r?\n/).find((line) => INPUT_HEADER.test(line));
  if (inputLine && inputLine.startsWith('Input #0')) {
    const m = /Input #0,\s*([^,]+),/.exec(inputLine);
    if (m) info.format = m[1].trim();
  }

  const dur = /Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)/.exec(text);
  if (dur) {
    info.durationSec = Number(dur[1]) * 3600 + Number(dur[2]) * 60 + Number(dur[3]);
  }
  const br = /Duration:.*?bitrate:\s*(\d+)\s*kb\/s/.exec(text);
  if (br) info.bitrateKbps = Number(br[1]);

  const lines = text.split(/\r?\n/);
  for (const line of lines) {
    const stream = /Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^)]*\))?(?::\s*)?(Video|Audio):\s*(.+)$/.exec(line);
    if (!stream) continue;
    info.streams += 1;
    const kind = stream[1];
    const rest = stream[2];
    const codec = rest.split(',')[0].trim();

    if (kind === 'Video' && !info.video) {
      const res = /(\d{2,5})x(\d{2,5})/.exec(rest);
      const fps = /([\d.]+)\s*fps/.exec(rest);
      const pix = /,\s*(yuv[\w\d]+|rgb[\w\d]+|gray[\w\d]*)/.exec(rest);
      info.video = {
        codec,
        width: res ? Number(res[1]) : null,
        height: res ? Number(res[2]) : null,
        fps: fps ? Number(fps[1]) : null,
        pixFmt: pix ? pix[1] : null,
      };
    } else if (kind === 'Audio' && !info.audio) {
      const sr = /(\d+)\s*Hz/.exec(rest);
      const layout = /Hz,\s*([^,]+)/.exec(rest);
      const layoutName = layout ? layout[1].trim() : null;
      const channels = layoutName === 'mono' ? 1
        : layoutName === 'stereo' ? 2
          : layoutName && /^\d+ channels/.test(layoutName) ? Number(/(\d+)/.exec(layoutName)[1])
            : null;
      info.audio = {
        codec,
        sampleRate: sr ? Number(sr[1]) : null,
        layout: layoutName,
        channels,
      };
    }
  }

  // 封面图会被当成 video 流，只有真正的视频流才算"有画面"。
  if (info.video && (info.video.width === null || info.video.height === null)) info.video = null;
  return info;
}

export async function probeMedia({ ffmpeg, file, signal }) {
  const res = await runCommand(ffmpeg, ['-hide_banner', '-nostdin', '-i', file], { signal });
  if (res.spawnError) {
    throw new CommandError(`无法启动 ffmpeg（${ffmpeg}）：${res.spawnError.message}`, { stderr: '' });
  }
  const text = `${res.stderr}\n${res.stdout}`;
  if (/No such file or directory/i.test(text)) {
    throw new CommandError(`找不到文件：${file}`, { stderr: text });
  }
  if (/Invalid data found when processing input|could not find codec parameters/i.test(text)) {
    throw new CommandError(`这个文件 ffmpeg 读不了（格式或数据坏了）：${path.basename(file)}`, { stderr: text });
  }
  if (!/Input #0/.test(text)) {
    throw new CommandError(`ffmpeg 没认出这个媒体文件：${path.basename(file)}`, { stderr: tailLines(text) });
  }
  const info = parseProbe(text, file);
  try {
    info.sizeBytes = statSync(file).size;
  } catch {
    info.sizeBytes = null;
  }
  return info;
}

/**
 * 从视频/任意媒体里抽出标准立体声 PCM wav（Demucs 只吃音频）。
 * 已经是 44.1k 立体声 wav 时可以跳过，但统一转一遍最省心。
 */
export async function extractAudio({ ffmpeg, input, output, sampleRate = 44100, channels = 2, signal }) {
  ensureParent(output);
  const args = [
    '-y', '-hide_banner', '-nostdin',
    '-i', input,
    '-vn', '-sn', '-dn',
    '-map', '0:a:0?',
    '-ac', String(channels),
    '-ar', String(sampleRate),
    '-c:a', 'pcm_s16le',
    '-f', 'wav',
    output,
  ];
  const res = await runCommand(ffmpeg, args, { signal });
  if (res.spawnError) {
    throw new CommandError(`无法启动 ffmpeg：${res.spawnError.message}`);
  }
  if (res.code !== 0) {
    if (/does not contain any stream|Stream map|matches no streams/i.test(res.stderr)) {
      throw new CommandError(`这个文件里没有音轨，抽不出声音：${path.basename(input)}`, { stderr: res.stderr });
    }
    throw new CommandError(`抽取音频失败（exit ${res.code}）：\n${tailLines(res.stderr, 8)}`, { stderr: res.stderr });
  }
  return output;
}

function codecArgs(format) {
  switch (format) {
    case 'mp3':
      return ['-c:a', 'libmp3lame', '-b:a', '320k', '-id3v2_version', '3'];
    case 'flac':
      return ['-c:a', 'flac'];
    case 'm4a':
      return ['-c:a', 'aac', '-b:a', '256k'];
    default:
      return ['-c:a', 'pcm_s16le'];
  }
}

/**
 * 把若干 stem 按各自增益叠成一条音轨。
 * inputs: [{ path, gainDb }]
 */
export async function mixStems({ ffmpeg, inputs, output, format = 'wav', signal }) {
  if (!inputs.length) throw new CommandError('没有要混的音轨。');
  ensureParent(output);

  const args = ['-y', '-hide_banner', '-nostdin'];
  for (const item of inputs) args.push('-i', item.path);

  const normalized = inputs.map((item, i) => {
    const gain = Number(item.gainDb ?? 0);
    return { index: i, gain: Number.isFinite(gain) ? gain : 0 };
  });

  let filter;
  if (normalized.length === 1) {
    const { gain } = normalized[0];
    filter = gain === 0 ? null : `[0:a]volume=${gain}dB[v]`;
  } else {
    const parts = normalized.map(({ index, gain }) =>
      gain === 0 ? `[${index}:a]anull[g${index}]` : `[${index}:a]volume=${gain}dB[g${index}]`);
    const labels = normalized.map(({ index }) => `[g${index}]`).join('');
    parts.push(`${labels}amix=inputs=${normalized.length}:normalize=0:duration=longest[mixed]`);
    parts.push('[mixed]alimiter=limit=0.98[out]');
    filter = parts.join(';');
  }

  if (filter) args.push('-filter_complex', filter, '-map', filter.includes('[out]') ? '[out]' : '[v]');
  else args.push('-map', '0:a:0');

  args.push('-ac', '2', '-ar', '44100', ...codecArgs(format), output);

  const res = await runCommand(ffmpeg, args, { signal });
  if (res.spawnError) throw new CommandError(`无法启动 ffmpeg：${res.spawnError.message}`);
  if (res.code !== 0) {
    throw new CommandError(`混音失败（exit ${res.code}）：\n${tailLines(res.stderr, 8)}`, { stderr: res.stderr });
  }
  return output;
}

/** 把一条音轨合回原视频，视频流不重新编码。 */
export async function muxAudio({ ffmpeg, video, audio, output, audioBitrate = '192k', signal }) {
  ensureParent(output);
  const args = [
    '-y', '-hide_banner', '-nostdin',
    '-i', video,
    '-i', audio,
    '-map', '0:v:0', '-map', '1:a:0',
    '-c:v', 'copy',
    '-c:a', 'aac', '-b:a', audioBitrate,
    '-shortest',
  ];
  if (/\.(mp4|m4v|mov)$/i.test(output)) args.push('-movflags', '+faststart');
  args.push(output);

  const res = await runCommand(ffmpeg, args, { signal });
  if (res.spawnError) throw new CommandError(`无法启动 ffmpeg：${res.spawnError.message}`);
  if (res.code !== 0) {
    throw new CommandError(`合回视频失败（exit ${res.code}）：\n${tailLines(res.stderr, 8)}`, { stderr: res.stderr });
  }
  return output;
}

/** 截取一段时间，用于试听/预览。 */
export async function trimAudio({ ffmpeg, input, output, startSec = 0, durationSec, format = 'wav', signal }) {
  ensureParent(output);
  const args = ['-y', '-hide_banner', '-nostdin', '-ss', String(Math.max(0, startSec)), '-i', input];
  if (Number.isFinite(durationSec) && durationSec > 0) args.push('-t', String(durationSec));
  args.push('-ac', '2', '-ar', '44100', ...codecArgs(format), output);

  const res = await runCommand(ffmpeg, args, { signal });
  if (res.spawnError) throw new CommandError(`无法启动 ffmpeg：${res.spawnError.message}`);
  if (res.code !== 0) {
    throw new CommandError(`截取失败（exit ${res.code}）：\n${tailLines(res.stderr, 8)}`, { stderr: res.stderr });
  }
  return output;
}
