// dsh-plugin-stems —— 视频/音频里把人声、伴奏、乐器分开。
//
// 分层：Node 侧负责编排（ffmpeg 抽音频/混音/合回视频、任务目录、清单），
// Python 侧只负责 Demucs 分离（py/stems_worker.py）。
//
// 工具：
//   stems_probe  看一眼文件：时长、有没有画面、音轨参数
//   stems_split  分离；mode=vocals 出人声+伴奏，mode=instruments 出逐件乐器
//   stems_list   回看历史任务和产物
//   stems_mix    挑几轨重新叠一条（可带增益、可截一段）
//   stems_mux    把某条音轨合回原视频

import { existsSync, mkdirSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import {
  extractAudio,
  mixStems,
  muxAudio,
  probeMedia,
  trimAudio,
} from './media.mjs';
import {
  createJob,
  listJobs,
  resolveJob,
  updateManifest,
  writeManifest,
} from './jobs.mjs';
import { runSeparation } from './separate.mjs';
import { formatBytes, formatDuration, resolvePath, runCommand, slugify, stamp, tailLines } from './util.mjs';

export const name = 'dsh-plugin-stems';
export const inject = ['tools'];

const DEFAULTS = {
  pythonPath: process.env.DSH_STEMS_PYTHON
    || 'D:\\dsh 大肥鱼\\voice\\tts\\venv\\Scripts\\python.exe',
  ffmpegPath: process.env.DSH_STEMS_FFMPEG
    || 'D:\\dsh 大肥鱼\\voice\\bin\\ffmpeg.exe',
  torchHome: process.env.DSH_STEMS_TORCH_HOME
    || 'D:\\dsh 大肥鱼\\voice\\cache',
  workRoot: process.env.DSH_STEMS_WORK
    || 'D:\\dsh 大肥鱼\\stems',
  demucsRepo: process.env.DSH_STEMS_REPO
    || 'D:\\dsh 大肥鱼\\voice\\demucs-repo',
  baseDir: process.env.DSH_STEMS_BASE || 'D:\\dsh 大肥鱼',
  device: 'auto',
  defaultModel: 'htdemucs',
  segmentsModel: 'htdemucs_6s',
  maxDurationSec: 4 * 3600,
};

const MODE_PRESETS = {
  vocals: {
    label: '人声 / 伴奏',
    model: 'htdemucs',
    twoStems: 'vocals',
    note: '两轨：vocals 人声，no_vocals 是去掉人声后的全部伴奏/背景。',
  },
  instruments: {
    label: '逐件乐器',
    model: 'htdemucs_6s',
    twoStems: '',
    note: '六轨：drums 鼓、bass 贝斯、guitar 吉他、piano 钢琴、vocals 人声、other 其余乐器与氛围。',
  },
};

function safeLogger(ctx) {
  try {
    const logger = ctx.logger;
    if (logger && typeof logger.info === 'function') return logger;
  } catch {
    /* 没注入 logger 就算了 */
  }
  return { info() {}, warn() {}, error() {}, debug() {} };
}

function resolveConfig(config) {
  const baseDir = path.normalize(config.baseDir || DEFAULTS.baseDir);
  const workRoot = path.normalize(config.workRoot || DEFAULTS.workRoot);
  return {
    ...DEFAULTS,
    ...config,
    baseDir,
    workRoot,
    pythonPath: config.pythonPath || DEFAULTS.pythonPath,
    ffmpegPath: config.ffmpegPath || DEFAULTS.ffmpegPath,
    torchHome: config.torchHome ?? DEFAULTS.torchHome,
    demucsRepo: config.demucsRepo ?? DEFAULTS.demucsRepo,
    device: config.device || DEFAULTS.device,
    defaultModel: config.defaultModel || DEFAULTS.defaultModel,
    segmentsModel: config.segmentsModel || DEFAULTS.segmentsModel,
    progressLogStep: config.progressLogStep ?? 20,
  };
}

/** 命令名（不带路径分隔符）就交给 PATH，绝对路径必须存在。 */
function checkBinary(value, label, hint) {
  const raw = String(value ?? '').trim();
  if (!raw) throw new Error(`${label} 没配置。${hint}`);
  if (!/[\\/]/.test(raw)) return raw;
  if (!existsSync(raw)) throw new Error(`${label} 找不到：${raw}\n${hint}`);
  return raw;
}

function assertRuntime(cfg) {
  const pythonPath = checkBinary(
    cfg.pythonPath,
    'Python 解释器',
    '在插件 config.pythonPath 里指向装了 torch + demucs 的那个 venv。',
  );
  const ffmpegPath = checkBinary(
    cfg.ffmpegPath,
    'ffmpeg',
    '在插件 config.ffmpegPath 里指向 ffmpeg.exe（没有 ffprobe 也能跑）。',
  );
  if (cfg.workRoot && !existsSync(cfg.workRoot)) {
    mkdirSync(cfg.workRoot, { recursive: true });
  }
  return { pythonPath, ffmpegPath };
}

function assertInputFile(file) {
  if (!file) throw new Error('没给文件路径（path）。');
  let stat;
  try {
    stat = statSync(file);
  } catch {
    throw new Error(`文件不存在：${file}`);
  }
  if (!stat.isFile()) throw new Error(`不是一个文件：${file}`);
  return stat.size;
}

function textOutput(render) {
  return {
    schema: {
      type: 'object',
      properties: {
        summary: { type: 'string', description: '给模型和用户看的人类可读结果' },
        data: { type: 'object', description: '结构化细节' },
      },
      required: ['summary'],
    },
    render: (args, value) => [{ type: 'text', text: render ? render(args, value) : value.summary }],
  };
}

function stemLine(stem) {
  const bits = [formatDuration(stem.durationSec)];
  if (Number.isFinite(stem.peakDb)) bits.push(`峰值 ${stem.peakDb > -0.05 ? '0' : stem.peakDb} dB`);
  if (stem.bytes) bits.push(formatBytes(stem.bytes));
  return `${stem.name}.${stem.path.split('.').pop()}  ${bits.join('  ')}`;
}

function renderStems(stems) {
  return stems.map((stem) => `· ${stemLine(stem)}`).join('\n');
}

export function apply(ctx, config = {}) {
  const log = safeLogger(ctx);
  const cfg = resolveConfig(config);

  if (!ctx.tools || typeof ctx.tools.register !== 'function') {
    log.warn('dsh-plugin-stems: 没有 tools 服务，跳过注册');
    return;
  }

  const definitions = [
    probeTool(cfg, log),
    selftestTool(cfg, log),
    splitTool(cfg, log),
    listTool(cfg, log),
    mixTool(cfg, log),
    muxTool(cfg, log),
  ];

  ctx.effect(() => {
    const disposers = definitions.map((definition) => ctx.tools.register(definition));
    log.info(`dsh-plugin-stems: 注册了 ${definitions.length} 个工具（工作目录 ${cfg.workRoot}）`);
    return () => {
      for (const dispose of disposers.reverse()) {
        try {
          dispose();
        } catch {
          /* 卸载时忽略 */
        }
      }
    };
  });
}

// ---------------------------------------------------------------- stems_probe

function probeTool(cfg, log) {
  return {
    name: 'stems_probe',
    description:
      '探测一个视频/音频文件：时长、容器格式、有没有画面、音轨的采样率与声道。分离之前先看一眼，确认声音在不在、有多长。',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '媒体文件路径（绝对路径，或相对 baseDir）' },
      },
      required: ['path'],
      additionalProperties: false,
    },
    output: textOutput(),
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const { ffmpegPath } = assertRuntime(cfg);
      const file = resolvePath(args?.path, cfg.baseDir);
      assertInputFile(file);

      let probe;
      try {
        probe = await probeMedia({ ffmpeg: ffmpegPath, file, signal: exec?.signal });
      } catch (error) {
        throw new Error(error.message);
      }

      const lines = [`${path.basename(file)}`, `时长 ${formatDuration(probe.durationSec)}`];
      if (probe.sizeBytes) lines.push(`大小 ${formatBytes(probe.sizeBytes)}`);
      if (probe.video) {
        lines.push(`画面 ${probe.video.codec} ${probe.video.width}×${probe.video.height}${probe.video.fps ? ` @${probe.video.fps}fps` : ''}`);
      } else {
        lines.push('没有画面（纯音频）');
      }
      if (probe.audio) {
        lines.push(`音轨 ${probe.audio.codec} ${probe.audio.sampleRate ?? '?'}Hz ${probe.audio.layout ?? '?'}`);
      } else {
        lines.push('没有音轨——分离不了，先确认文件里有没有声音');
      }
      if (probe.audio) {
        lines.push('', '可以这样分离：', '· mode=vocals 先分人声和伴奏', '· mode=instruments 逐件乐器（鼓/贝斯/吉他/钢琴/人声/其他）');
      }

      return {
        summary: lines.join('\n'),
        data: { file, probe },
      };
    },
  };
}

// ------------------------------------------------------------- stems_selftest

function selftestTool(cfg, log) {
  return {
    name: 'stems_selftest',
    description:
      '自检运行环境：Python/torch/demucs 能不能用、ffmpeg 在不在、本地模型权重齐不齐、显卡认不认。'
      + '第一次用、或者分离报错的时候先跑这个。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: textOutput(),
    isConcurrencySafe: () => true,
    timeoutMs: 180000,
    async execute(args, exec) {
      const rows = [];
      const problems = [];

      if (existsSync(cfg.ffmpegPath)) {
        const res = await runCommand(cfg.ffmpegPath, ['-version'], { signal: exec?.signal, tailBytes: 8192 });
        const first = `${res.stdout || ''}${res.stderr || ''}`.split(/\r?\n/)[0]?.trim();
        if (res.code === 0 && first) rows.push(`ffmpeg    ${first}`);
        else problems.push(`ffmpeg 跑不起来（exit ${res.code}）：${first || '没有输出'}`);
      } else {
        problems.push(`ffmpeg 不存在：${cfg.ffmpegPath}`);
      }

      let pythonOk = false;
      if (existsSync(cfg.pythonPath)) {
        const code = [
          'import torch',
          'print(torch.__version__)',
          'print(torch.cuda.is_available())',
          'print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else "-")',
        ].join('\n');
        const res = await runCommand(cfg.pythonPath, ['-c', code], { signal: exec?.signal, tailBytes: 16384 });
        const out = (res.stdout || '').trim().split(/\r?\n/);
        if (res.code === 0) {
          pythonOk = true;
          const gpu = out[1] === 'True' ? (out[2] || 'cuda') : '没有可用显卡，会走 CPU（慢很多）';
          rows.push(`torch     ${out[0] || '?'}`);
          rows.push(`显卡      ${gpu}`);
        } else {
          problems.push(`Python 跑不起来：${tailLines(res.stderr || '', 4)}`);
        }
      } else {
        problems.push(`Python 不存在：${cfg.pythonPath}`);
      }

      if (pythonOk) {
        const res = await runCommand(cfg.pythonPath, ['-c', 'import demucs, torchaudio; print(demucs.__version__)'], {
          signal: exec?.signal,
          tailBytes: 8192,
        });
        if (res.code === 0) rows.push(`demucs    ${(res.stdout || '').trim() || '?'}`);
        else problems.push(`demucs 没装好：${tailLines(res.stderr || '', 4)}`);
      }

      const repo = cfg.demucsRepo;
      if (repo && existsSync(repo)) {
        let weights = [];
        try {
          weights = readdirSync(repo).filter((name) => name.endsWith('.th'));
        } catch {
          /* 读不到就当空 */
        }
        rows.push(`模型库    ${repo}`);
        rows.push(`权重      ${weights.length ? weights.join(', ') : '（空，首次分离会联网下载）'}`);
      } else {
        problems.push(`本地模型库不存在：${repo || '（没配）'}——会退到在线仓库，国内可能卡好几分钟`);
      }

      rows.push(`工作目录  ${cfg.workRoot}`);
      rows.push(`基准目录  ${cfg.baseDir}`);

      const lines = [...rows];
      if (problems.length) {
        lines.push('', '有问题：', ...problems.map((item) => `· ${item}`));
      } else {
        lines.push('', '都齐了，可以直接 stems_split。');
      }

      return {
        summary: lines.join('\n'),
        data: { rows, problems, pythonPath: cfg.pythonPath, ffmpegPath: cfg.ffmpegPath, demucsRepo: repo, workRoot: cfg.workRoot },
      };
    },
  };
}

// ---------------------------------------------------------------- stems_split

function splitTool(cfg, log) {
  return {
    name: 'stems_split',
    description:
      '把一个视频/音频文件分离成多条音轨。mode=vocals 出「人声」和「伴奏/背景音」两轨；'
      + 'mode=instruments 出逐件乐器（鼓、贝斯、吉他、钢琴、人声、其他）。视频会自动先抽成音频再分离，'
      + '产物落在任务目录里，返回 jobId 供后续 mix/mux 使用。',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '源视频/音频路径（绝对路径，或相对 baseDir）' },
        mode: {
          type: 'string',
          enum: ['vocals', 'instruments'],
          description: 'vocals=人声+伴奏（快、人声质量最好）；instruments=逐件乐器（6 轨）',
          default: 'vocals',
        },
        model: {
          type: 'string',
          description: '覆盖 demucs 模型名，例如 htdemucs_ft（慢 4 倍但更干净）。默认按 mode 选。',
        },
        stems: {
          type: 'array',
          items: { type: 'string' },
          description: '只要这些轨（可选），例如 ["vocals","no_vocals"] 或 ["guitar","piano"]。不给就全出。',
        },
        format: {
          type: 'string',
          enum: ['wav', 'flac', 'mp3'],
          description: '产物格式，默认 wav（无损，体积大）；投稿/分享用 mp3。',
          default: 'wav',
        },
        device: { type: 'string', description: 'auto / cuda / cpu，默认 auto（有显卡就用显卡）' },
        shifts: { type: 'integer', description: '1-10，越高越干净越慢，默认 1', minimum: 1, maximum: 10 },
        segmentSec: { type: 'number', description: '显存吃紧时按秒切片处理（例如 7.8），默认自动' },
        outputDir: { type: 'string', description: '自定义任务目录（默认在工作根目录下新建）' },
      },
      required: ['path'],
      additionalProperties: false,
    },
    output: textOutput(),
    timeoutMs: 4 * 3600 * 1000,
    isConcurrencySafe: () => false,
    presentCall(args) {
      const mode = MODE_PRESETS[args?.mode] ?? MODE_PRESETS.vocals;
      return { card: 'generic', title: `分离音轨 · ${mode.label}`, kind: 'execute', rawInput: args };
    },
    async execute(args, exec) {
      const { pythonPath, ffmpegPath } = assertRuntime(cfg);
      const modeKey = args?.mode === 'instruments' ? 'instruments' : 'vocals';
      const preset = MODE_PRESETS[modeKey];
      const model = String(args?.model || (modeKey === 'instruments' ? cfg.segmentsModel : cfg.defaultModel));
      const format = ['wav', 'flac', 'mp3'].includes(args?.format) ? args.format : 'wav';
      const device = String(args?.device || cfg.device);
      const shifts = Math.min(10, Math.max(1, Number(args?.shifts ?? 1) || 1));
      const segmentSec = Number(args?.segmentSec ?? 0) || 0;
      const saveOnly = Array.isArray(args?.stems) ? args.stems.map(String).filter(Boolean) : [];

      const file = resolvePath(args?.path, cfg.baseDir);
      assertInputFile(file);

      let probe;
      try {
        probe = await probeMedia({ ffmpeg: ffmpegPath, file, signal: exec?.signal });
      } catch (error) {
        throw new Error(error.message);
      }
      if (!probe.audio) {
        throw new Error(`这个文件里没有音轨，分离不了：${path.basename(file)}`);
      }
      if (Number.isFinite(probe.durationSec) && probe.durationSec > cfg.maxDurationSec) {
        throw new Error(
          `时长 ${formatDuration(probe.durationSec)} 超过上限 ${formatDuration(cfg.maxDurationSec)}，`
          + '先截一段再分离（用 stems_mix 的 trim 参数，或直接 ffmpeg 截）。',
        );
      }

      const job = args?.outputDir
        ? { jobId: slugify(path.basename(file)), jobDir: resolvePath(args.outputDir, cfg.baseDir) }
        : await createJob(cfg.workRoot, { input: file, mode: modeKey });
      if (args?.outputDir) {
        mkdirSync(job.jobDir, { recursive: true });
        job.manifestPath = path.join(job.jobDir, 'job.json');
      }
      mkdirSync(job.jobDir, { recursive: true });

      const manifestPath = path.join(job.jobDir, 'job.json');
      await writeManifest(manifestPath, {
        jobId: job.jobId,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        status: 'preparing',
        mode: modeKey,
        model,
        device,
        format,
        input: { path: file, probe },
        stems: [],
        outputs: [],
      });

      try {
        log.info(`stems: 抽音频 ${path.basename(file)} → ${job.jobDir}`);
        const audioPath = path.join(job.jobDir, 'input.wav');
        await extractAudio({ ffmpeg: ffmpegPath, input: file, output: audioPath, signal: exec?.signal });
        await updateManifest(manifestPath, { status: 'separating', audioPath });

        log.info(`stems: 分离中（${model} / ${device}）…`);
        let nextLog = 10;
        const { result } = await runSeparation({
          pythonPath,
          input: audioPath,
          outDir: job.jobDir,
          model,
          device,
          shifts,
          overlap: 0.25,
          segmentSec,
          twoStems: preset.twoStems,
          format,
          saveOnly: saveOnly.join(','),
          torchHome: cfg.torchHome,
          repoDir: cfg.demucsRepo,
          signal: exec?.signal,
          onProgress({ pct }) {
            if (pct >= nextLog) {
              log.info(`stems: ${Math.round(pct)}%`);
              nextLog += cfg.progressLogStep;
            }
          },
        });

        const manifest = await updateManifest(manifestPath, {
          status: 'done',
          samplerate: result.samplerate,
          elapsedSec: result.elapsedSec,
          stems: result.stems,
        });

        const lines = [
          `分离完成 · ${preset.label} · ${model}（${result.device}）· 用时 ${result.elapsedSec}s`,
          `job: ${job.jobId}`,
          `目录: ${job.jobDir}`,
          '',
          renderStems(result.stems),
          '',
          preset.note,
        ];
        if (modeKey === 'vocals') {
          lines.push('想再逐件拆乐器，对这个文件跑 mode=instruments；想调整某几轨的音量或重新组合，用 stems_mix。');
        } else {
          lines.push('注意：other.wav 是模型分不出来的那部分乐器（弦乐、合成器、人声和声等），不是"杂音"。');
        }

        return {
          summary: lines.join('\n'),
          data: {
            jobId: job.jobId,
            jobDir: job.jobDir,
            manifestPath,
            mode: modeKey,
            model,
            device: result.device,
            samplerate: result.samplerate,
            elapsedSec: result.elapsedSec,
            stems: result.stems,
            manifestStatus: manifest.status,
          },
        };
      } catch (error) {
        await updateManifest(manifestPath, {
          status: 'failed',
          error: String(error?.message ?? error).slice(0, 2000),
        }).catch(() => {});
        throw error;
      }
    },
  };
}

// ---------------------------------------------------------------- stems_list

function listTool(cfg, log) {
  return {
    name: 'stems_list',
    description: '列出历史分离任务（最近在前），或给定 jobId 查看某个任务的全部产物路径。用来找回之前分出来的轨。',
    parameters: {
      type: 'object',
      properties: {
        jobId: { type: 'string', description: '任务 id 或任务目录；不给就列最近的任务' },
        limit: { type: 'integer', description: '最多列几个，默认 8', minimum: 1, maximum: 50 },
      },
      additionalProperties: false,
    },
    output: textOutput(),
    isConcurrencySafe: () => true,
    async execute(args) {
      const ref = String(args?.jobId ?? '').trim();
      if (ref) {
        const found = await resolveJob(cfg.workRoot, ref);
        if (!found) throw new Error(`找不到任务：${ref}（在 ${cfg.workRoot} 下没有对应的 job.json）`);
        const { manifest, jobDir } = found;
        const lines = [
          `job: ${manifest.jobId}`,
          `目录: ${jobDir}`,
          `状态: ${manifest.status}  模式: ${MODE_PRESETS[manifest.mode]?.label ?? manifest.mode}  模型: ${manifest.model}`,
          `源: ${manifest.input?.path ?? '?'}（${formatDuration(manifest.input?.probe?.durationSec)}）`,
        ];
        if (manifest.stems?.length) {
          lines.push('', '音轨：', renderStems(manifest.stems));
        } else {
          lines.push('', '这个任务还没有产物。');
        }
        if (manifest.outputs?.length) {
          lines.push('', '再加工产物：');
          for (const output of manifest.outputs) lines.push(`· ${output.kind}  ${output.path}`);
        }
        return { summary: lines.join('\n'), data: { manifest, jobDir } };
      }

      const limit = Math.min(50, Math.max(1, Number(args?.limit ?? 8) || 8));
      const jobs = await listJobs(cfg.workRoot, limit);
      if (!jobs.length) {
        return { summary: `还没有任务。工作目录：${cfg.workRoot}`, data: { jobs: [] } };
      }
      const lines = [`最近 ${jobs.length} 个任务（${cfg.workRoot}）：`];
      for (const job of jobs) {
        const stems = (job.stems ?? []).map((stem) => stem.name).join(', ') || '—';
        lines.push(`· ${job.jobId}  [${job.status}]  ${formatDuration(job.input?.probe?.durationSec)}  ${stems}`);
      }
      lines.push('', '用 stems_list jobId=<id> 看某个任务的完整产物。');
      return { summary: lines.join('\n'), data: { jobs: jobs.map((job) => ({ jobId: job.jobId, status: job.status, jobDir: job.jobDir })) } };
    },
  };
}

// ---------------------------------------------------------------- stems_mix

function normalizeMixInputs(raw, manifest) {
  const items = Array.isArray(raw) ? raw : [];
  if (!items.length) throw new Error('stems 不能是空的：至少给一轨。');
  const byName = new Map((manifest.stems ?? []).map((stem) => [stem.name, stem]));
  return items.map((item) => {
    const entry = typeof item === 'string' ? { name: item } : item ?? {};
    const stemName = String(entry.name ?? entry.stem ?? '').trim();
    const stem = byName.get(stemName);
    if (!stem) {
      const available = [...byName.keys()].join(', ') || '（这个任务没有音轨）';
      throw new Error(`任务里没有叫 ${stemName || '(空)'} 的轨。可用的：${available}`);
    }
    return { name: stemName, path: stem.path, gainDb: Number(entry.gainDb ?? entry.gain ?? 0) || 0 };
  });
}

function mixTool(cfg, log) {
  return {
    name: 'stems_mix',
    description:
      '把某个任务里的若干条 stem 按给定增益叠成一条新音轨（比如只有鼓+贝斯、或人声+钢琴）。'
      + '可以顺带截一段（trim）。用来做伴奏、做试听、或者去掉某件乐器。',
    parameters: {
      type: 'object',
      properties: {
        jobId: { type: 'string', description: '任务 id（stems_split 返回的）' },
        stems: {
          type: 'array',
          description: '要叠的轨：[{name, gainDb}]，也接受字符串数组 ["drums","bass"]',
          items: {
            type: 'object',
            properties: {
              name: { type: 'string' },
              gainDb: { type: 'number', description: '增益，单位 dB，默认 0' },
            },
            required: ['name'],
          },
        },
        format: { type: 'string', enum: ['wav', 'flac', 'mp3'], default: 'wav' },
        output: { type: 'string', description: '输出路径；不给就放在任务目录里' },
        trim: {
          type: 'object',
          description: '截一段：{startSec, durationSec}',
          properties: {
            startSec: { type: 'number' },
            durationSec: { type: 'number' },
          },
        },
      },
      required: ['jobId', 'stems'],
      additionalProperties: false,
    },
    output: textOutput(),
    timeoutMs: 30 * 60 * 1000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const { ffmpegPath } = assertRuntime(cfg);
      const ref = String(args?.jobId ?? '').trim();
      if (!ref) throw new Error('jobId 不能为空。先用 stems_list 看看有哪些任务。');
      const found = await resolveJob(cfg.workRoot, ref);
      if (!found) throw new Error(`找不到任务：${ref}`);

      const { manifest, jobDir, manifestPath } = found;
      const inputs = normalizeMixInputs(args?.stems, manifest);
      const format = ['wav', 'flac', 'mp3'].includes(args?.format) ? args.format : 'wav';
      const trim = args?.trim && Number.isFinite(Number(args.trim.startSec))
        ? {
          startSec: Math.max(0, Number(args.trim.startSec)),
          durationSec: Number(args.trim.durationSec) > 0 ? Number(args.trim.durationSec) : undefined,
        }
        : null;

      const names = inputs.map((item) => item.name).join('+');
      const output = args?.output
        ? resolvePath(args.output, cfg.baseDir)
        : path.join(jobDir, `mix-${names}-${stamp()}.${format}`);
      mkdirSync(path.dirname(output), { recursive: true });

      const tempMix = trim ? path.join(jobDir, `.tmp-mix-${stamp()}.wav`) : output;

      await mixStems({
        ffmpeg: ffmpegPath,
        inputs,
        output: trim ? tempMix : output,
        format: trim ? 'wav' : format,
        signal: exec?.signal,
      });
      if (trim) {
        await trimAudio({
          ffmpeg: ffmpegPath,
          input: tempMix,
          output,
          startSec: trim.startSec,
          durationSec: trim.durationSec,
          format,
          signal: exec?.signal,
        });
        try {
          const { rmSync } = await import('node:fs');
          rmSync(tempMix, { force: true });
        } catch {
          /* 临时文件删不掉不影响结果 */
        }
      }

      const size = existsSync(output) ? statSync(output).size : null;
      log.info(`stems: 混音写出 ${output}`);
      await updateManifest(manifestPath, {
        outputs: [...(manifest.outputs ?? []), {
          kind: 'mix',
          path: output,
          note: names,
          createdAt: new Date().toISOString(),
        }],
      });

      const lines = [
        `混音完成：${names}`,
        `输出: ${output}${size ? `  (${formatBytes(size)})` : ''}`,
        inputs.length > 1
          ? `增益: ${inputs.map((item) => `${item.name} ${item.gainDb > 0 ? '+' : ''}${item.gainDb}dB`).join(', ')}`
          : '',
        trim ? `截取: ${formatDuration(trim.startSec)} 起${trim.durationSec ? `，长 ${formatDuration(trim.durationSec)}` : '，到结尾'}` : '',
      ].filter(Boolean);

      return {
        summary: lines.join('\n'),
        data: { jobId: manifest.jobId, output, inputs: inputs.map(({ name: n, gainDb }) => ({ name: n, gainDb })) },
      };
    },
  };
}

// ---------------------------------------------------------------- stems_mux

function muxTool(cfg, log) {
  return {
    name: 'stems_mux',
    description:
      '把某条 stem（默认 no_vocals 伴奏）合回原视频，视频流不重新编码，只换音轨。'
      + '做"去人声的 MV"、或者把逐件乐器轨贴回画面时用。',
    parameters: {
      type: 'object',
      properties: {
        jobId: { type: 'string', description: '任务 id' },
        stem: { type: 'string', description: '要合回去的轨名，默认 no_vocals' },
        video: { type: 'string', description: '视频路径；默认用任务原来的源文件（必须是视频）' },
        output: { type: 'string', description: '输出路径，默认任务目录里 <原名>-<stem>.mp4' },
        audioBitrate: { type: 'string', description: 'AAC 码率，默认 192k' },
      },
      required: ['jobId'],
      additionalProperties: false,
    },
    output: textOutput(),
    timeoutMs: 30 * 60 * 1000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const { ffmpegPath } = assertRuntime(cfg);
      const ref = String(args?.jobId ?? '').trim();
      if (!ref) throw new Error('jobId 不能为空。');
      const found = await resolveJob(cfg.workRoot, ref);
      if (!found) throw new Error(`找不到任务：${ref}`);
      const { manifest, jobDir, manifestPath } = found;

      const stemName = String(args?.stem ?? 'no_vocals').trim() || 'no_vocals';
      const stem = (manifest.stems ?? []).find((item) => item.name === stemName);
      if (!stem) {
        const available = (manifest.stems ?? []).map((item) => item.name).join(', ') || '（没有音轨）';
        throw new Error(`任务里没有叫 ${stemName} 的轨。可用的：${available}`);
      }
      if (!existsSync(stem.path)) throw new Error(`音轨文件不见了：${stem.path}`);

      const video = args?.video ? resolvePath(args.video, cfg.baseDir) : manifest.input?.path;
      if (!video) throw new Error('没有源视频，用 video 参数指定一个。');
      assertInputFile(video);

      let probe;
      try {
        probe = await probeMedia({ ffmpeg: ffmpegPath, file: video, signal: exec?.signal });
      } catch (error) {
        throw new Error(error.message);
      }
      if (!probe.video) {
        throw new Error(`${path.basename(video)} 没有画面，合不回视频；要换音轨直接看 stems_mix 的输出就行。`);
      }

      const base = path.basename(video).replace(/\.[^.]+$/, '');
      const output = args?.output
        ? resolvePath(args.output, cfg.baseDir)
        : path.join(jobDir, `${base}-${stemName}.mp4`);
      mkdirSync(path.dirname(output), { recursive: true });

      await muxAudio({
        ffmpeg: ffmpegPath,
        video,
        audio: stem.path,
        output,
        audioBitrate: String(args?.audioBitrate ?? '192k'),
        signal: exec?.signal,
      });

      const size = existsSync(output) ? statSync(output).size : null;
      log.info(`stems: 合回视频 ${output}`);
      await updateManifest(manifestPath, {
        outputs: [...(manifest.outputs ?? []), {
          kind: 'mux',
          path: output,
          note: stemName,
          createdAt: new Date().toISOString(),
        }],
      });

      return {
        summary: [
          `已合回视频：${stemName} 音轨 + 原画面`,
          `输出: ${output}${size ? `  (${formatBytes(size)})` : ''}`,
          `视频流未重编码（快、无损）；音频换成 AAC。`,
        ].join('\n'),
        data: { jobId: manifest.jobId, output, stem: stemName, source: video, audio: stem.path },
      };
    },
  };
}
