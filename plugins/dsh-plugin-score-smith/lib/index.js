// dsh-plugin-score-smith —— 制谱师：听音频，出可编辑的曲谱。
//
// 流水线：
//   音频 → demucs 分轨（复用 dsh-plugin-stems 的 py/stems_worker.py）
//        → 逐轨识音（人声/贝斯用 pyin，吉他/钢琴用 basic-pitch，鼓用起音+频段分类）
//        → 量化到拍和小节 → score.json（真身，随便改）
//        → music21 出 MusicXML/MIDI → verovio 排版 → PNG 预览
//
// 工具：
//   score_doctor      自检环境
//   score_transcribe  音频 → 曲谱（主入口）
//   score_list        看历史曲谱项目 / 单个项目详情
//   score_snippet     读某几小节的音符（核对识别准不准）
//   score_edit        改谱：移调、删小节、改速度调号、删轨、改和弦、逐音增删改
//   score_render      重新排版出图（PNG / PDF）

import { existsSync, mkdirSync, statSync } from 'node:fs';
import path from 'node:path';
import {
  createProject,
  listProjects,
  projectFiles,
  readManifest,
  resolveProject,
  runWorker,
  WORKER_SCRIPT,
  writeManifest,
} from './score.mjs';
import { CommandError, existingFile, formatDuration, resolvePath, slugify, tailLines } from './util.mjs';

export const name = 'dsh-plugin-score-smith';
export const inject = ['tools'];

const DEFAULTS = {
  pythonPath: process.env.DSH_SCORE_PYTHON || 'C:\\dsh-tools\\score-venv\\Scripts\\python.exe',
  workRoot: process.env.DSH_SCORE_WORK || 'D:\\dsh 大肥鱼\\scores',
  baseDir: process.env.DSH_SCORE_BASE || 'D:\\dsh 大肥鱼',
  separatorPython: process.env.DSH_SCORE_SEPARATOR_PYTHON
    || 'D:\\dsh 大肥鱼\\voice\\tts\\venv\\Scripts\\python.exe',
  separatorWorker: process.env.DSH_SCORE_SEPARATOR_WORKER
    || 'D:\\dsh 大肥鱼\\plugins\\dsh-plugin-stems\\py\\stems_worker.py',
  separatorRepo: process.env.DSH_SCORE_SEPARATOR_REPO || 'D:\\dsh 大肥鱼\\voice\\demucs-repo',
  separatorTorchHome: process.env.DSH_SCORE_TORCH_HOME || 'D:\\dsh 大肥鱼\\voice\\cache',
  browser: process.env.DSH_SCORE_BROWSER || '',
  verovioResources: process.env.DSH_SCORE_VEROVIO_RES || '',
  device: 'auto',
  model: 'htdemucs_6s',
  grid: '1/16',
  maxDurationSec: 15 * 60,
};

const INSTRUMENT_LABELS = {
  vocals: '人声', guitar: '民谣吉他', piano: '钢琴', bass: '电贝司', drums: '架子鼓',
  sax: '萨克斯', ukulele: '尤克里里', violin: '小提琴', guzheng: '古筝', other: '其他乐器',
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
  return {
    ...DEFAULTS,
    ...config,
    baseDir: path.normalize(config.baseDir || DEFAULTS.baseDir),
    workRoot: path.normalize(config.workRoot || DEFAULTS.workRoot),
    pythonPath: config.pythonPath || DEFAULTS.pythonPath,
  };
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

function assertRuntime(cfg) {
  if (!existingFile(cfg.pythonPath)) {
    throw new Error(
      `制谱用的 Python 不在了：${cfg.pythonPath}\n`
      + '装法：python -m venv C:\\dsh-tools\\score-venv 然后用它 pip install librosa music21 verovio basic-pitch onnxruntime pretty-midi',
    );
  }
  if (!existingFile(WORKER_SCRIPT)) {
    throw new Error(`制谱内核脚本不见了：${WORKER_SCRIPT}`);
  }
  if (!existsSync(cfg.workRoot)) mkdirSync(cfg.workRoot, { recursive: true });
  return { pythonPath: cfg.pythonPath };
}

function assertInputFile(file) {
  if (!file) throw new Error('没给音频路径（path）。');
  let stat;
  try {
    stat = statSync(file);
  } catch {
    throw new Error(`文件不存在：${file}`);
  }
  if (!stat.isFile()) throw new Error(`不是一个文件：${file}`);
  return stat.size;
}

function separatorOptions(cfg) {
  return JSON.stringify({
    python: cfg.separatorPython,
    worker: cfg.separatorWorker,
    repoDir: cfg.separatorRepo,
    model: cfg.model,
    device: cfg.device,
    shifts: 1,
    torchHome: cfg.separatorTorchHome,
  });
}

function renderTrackLines(summary) {
  if (!summary?.tracks?.length) return '（没有识别出音轨）';
  const order = ['vocals', 'guitar', 'piano', 'bass', 'drums', 'other'];
  return summary.tracks
    .slice()
    .sort((a, b) => order.indexOf(a.instrument) - order.indexOf(b.instrument))
    .map((track) => {
      const label = track.label || INSTRUMENT_LABELS[track.instrument] || track.instrument;
      const range = track.range ? ` · ${track.range[0]}–${track.range[1]}` : '';
      return `· ${label}  ${track.notes} 个音${range}`;
    })
    .join('\n');
}

function renderSummaryBlock(summary) {
  if (!summary) return '';
  return [
    `《${summary.title}》 · ${summary.meta?.meter ?? summary.meter}/4 · ${summary.tempo} BPM · `
    + `${summary.key ?? `${summary.meta?.keyTonic} ${summary.meta?.keyMode}`} · ${summary.measures} 小节`,
    renderTrackLines(summary),
  ].join('\n');
}

export function apply(ctx, config = {}) {
  const log = safeLogger(ctx);
  const cfg = resolveConfig(config);

  if (!ctx.tools || typeof ctx.tools.register !== 'function') {
    log.warn('dsh-plugin-score-smith: 没有 tools 服务，跳过注册');
    return;
  }

  const definitions = [
    doctorTool(cfg, log),
    transcribeTool(cfg, log),
    listTool(cfg, log),
    snippetTool(cfg, log),
    editTool(cfg, log),
    renderTool(cfg, log),
    partsTool(cfg, log),
  ];

  ctx.effect(() => {
    const disposers = definitions.map((definition) => ctx.tools.register(definition));
    log.info(`dsh-plugin-score-smith: 注册了 ${definitions.length} 个工具（曲谱目录 ${cfg.workRoot}）`);
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

// ---------------------------------------------------------------- score_doctor

function doctorTool(cfg, log) {
  return {
    name: 'score_doctor',
    description: '制谱师自检：Python 环境、librosa/music21/verovio/basic-pitch 能不能导入、'
      + '分轨内核在不在、无头浏览器能不能用、verovio 能不能排版。第一次用或报错时先跑它。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: textOutput(),
    isConcurrencySafe: () => true,
    timeoutMs: 180000,
    async execute(args, exec) {
      const rows = [];
      const problems = [];
      assertRuntime(cfg);

      const { result } = await runWorker({
        pythonPath: cfg.pythonPath,
        args: ['selftest'],
        signal: exec?.signal,
      });
      for (const [key, value] of Object.entries(result.checks ?? {})) {
        rows.push(`${key.padEnd(16)} ${value === 'ok' || String(value).startsWith('ok') ? value : value}`);
        if (String(value).startsWith('FAIL')) problems.push(`${key}: ${value}`);
      }

      if (existingFile(cfg.separatorPython) && existingFile(cfg.separatorWorker)) {
        rows.push(`分轨内核         ${cfg.separatorWorker}`);
        rows.push(`分轨 Python      ${cfg.separatorPython}`);
      } else {
        problems.push(`分轨内核缺失：${cfg.separatorWorker} 或 ${cfg.separatorPython}`);
      }

      rows.push(`曲谱目录         ${cfg.workRoot}`);
      const lines = [...rows];
      if (problems.length) lines.push('', '有问题：', ...problems.map((item) => `· ${item}`));
      else lines.push('', '环境齐了，可以直接 score_transcribe（丢一个音频路径给它）。');

      return { summary: lines.join('\n'), data: { checks: result.checks, problems, ...cfg } };
    },
  };
}

// ------------------------------------------------------------ score_transcribe

function transcribeTool(cfg, log) {
  return {
    name: 'score_transcribe',
    description:
      '把一段音乐（mp3/wav/mp4 都行）做成可编辑曲谱。先自动分轨（人声/吉他/钢琴/贝斯/鼓/其他），'
      + '再逐轨识别音高和音符，量化成小节，输出 score.json（真身）、MusicXML、MIDI 和 PNG 预览图。'
      + '之后可以用 score_edit 改谱、score_snippet 核对、score_render 重新出图。',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '音频文件路径（绝对路径或相对 baseDir）' },
        title: { type: 'string', description: '曲名，默认用文件名' },
        mode: {
          type: 'string',
          enum: ['instruments', 'vocals'],
          description: 'instruments=六轨逐件乐器（默认，适合做总谱）；vocals=只分人声+伴奏（快）',
          default: 'instruments',
        },
        stemsDir: { type: 'string', description: '已经有分轨目录的话直接复用（里面有 vocals.wav / drums.wav …）' },
        only: {
          type: 'array',
          items: { type: 'string' },
          description: '只识别这些轨，例如 ["vocals","guitar"]。不给就全上。',
        },
        instrumentMap: {
          type: 'object',
          description: '把某条 stem 当成别的乐器，例如 {"guitar":"sax"}（分轨分出来的"吉他"其实是萨克斯时用）',
        },
        tempo: { type: 'number', description: '指定速度；不给就自动测' },
        meter: { type: 'integer', description: '指定拍号分子（3 或 4）；不给就自动猜' },
        key: { type: 'string', description: '指定调，例如 "G:major"、"e:minor"；不给就自动测' },
        grid: {
          type: 'string',
          enum: ['1/4', '1/8', '1/16', '1/32', '1/8t', '1/16t'],
          description: '量化精度，默认 1/16（十六分音符）。曲子有三连音的可以用 1/8t。',
          default: '1/16',
        },
        maxSeconds: { type: 'number', description: '只处理前 N 秒（试听/测试时用）' },
        outputDir: { type: 'string', description: '自定义输出目录' },
      },
      required: ['path'],
      additionalProperties: false,
    },
    output: textOutput(),
    timeoutMs: 4 * 3600 * 1000,
    isConcurrencySafe: () => false,
    presentCall(args) {
      const title = args?.title || path.basename(String(args?.path ?? '')) || '音频';
      return { card: 'generic', title: `制谱 · ${title}`, kind: 'execute', rawInput: args };
    },
    async execute(args, exec) {
      const { pythonPath } = assertRuntime(cfg);
      const file = resolvePath(args?.path, cfg.baseDir);
      const sizeBytes = assertInputFile(file);
      const mode = args?.mode === 'vocals' ? 'vocals' : 'instruments';
      const title = String(args?.title || path.basename(file).replace(/\.[^.]+$/, ''));

      const project = args?.outputDir
        ? { projectId: slugify(path.basename(String(args.outputDir))), projectDir: resolvePath(args.outputDir, cfg.baseDir), manifestPath: path.join(resolvePath(args.outputDir, cfg.baseDir), 'manifest.json') }
        : await createProject(cfg.workRoot, { title, input: file });
      mkdirSync(project.projectDir, { recursive: true });

      await writeManifest(project.manifestPath, {
        projectId: project.projectId,
        createdAt: new Date().toISOString(),
        status: 'running',
        mode,
        title,
        input: { path: file, bytes: sizeBytes },
        files: {},
      });

      const workerArgs = [
        'transcribe',
        '--input', file,
        '--work-dir', project.projectDir,
        '--title', title,
        '--grid', String(args?.grid || cfg.grid),
        '--map', Object.entries(args?.instrumentMap ?? {}).map(([k, v]) => `${k}=${v}`).join(','),
        '--only', Array.isArray(args?.only) ? args.only.join(',') : '',
        '--verovio-resources', cfg.verovioResources || '',
        '--browser', cfg.browser || '',
      ];
      if (Array.isArray(args?.only) && args.only.length) {
        workerArgs.push('--only', args.only.join(','));
      }
      if (args?.stemsDir) workerArgs.push('--stems-dir', resolvePath(args.stemsDir, cfg.baseDir));
      if (Number(args?.tempo) > 0) workerArgs.push('--tempo', String(args.tempo));
      if (Number(args?.meter) > 0) workerArgs.push('--meter', String(args.meter));
      if (args?.key) workerArgs.push('--key', String(args.key));
      if (Number(args?.maxSeconds) > 0) workerArgs.push('--max-seconds', String(args.maxSeconds));
      if (!args?.stemsDir) workerArgs.push('--separator', separatorOptions({ ...cfg, mode }));

      let nextLog = 10;
      let lastStage = '';
      try {
        log.info(`score-smith: 开始制谱 ${path.basename(file)} → ${project.projectDir}`);
        const { result } = await runWorker({
          pythonPath,
          args: workerArgs,
          signal: exec?.signal,
          onEvent(event) {
            if (event.type === 'phase') {
              if (event.stage !== lastStage) lastStage = event.stage;
              if (Number(event.pct) >= nextLog) {
                log.info(`score-smith: ${Math.round(event.pct)}% ${event.stage} ${event.msg ?? ''}`);
                nextLog += 15;
              }
            } else if (event.type === 'log') {
              log.info(`score-smith: ${event.msg}`);
            }
          },
        });

        await writeManifest(project.manifestPath, {
          projectId: project.projectId,
          createdAt: new Date().toISOString(),
          status: 'done',
          mode,
          title: result.title,
          input: { path: file, bytes: sizeBytes },
          meta: result.meta,
          summary: result.summary,
          files: result.files,
          elapsedSec: result.elapsedSec,
        });

        const lines = [
          `制谱完成 · 用时 ${result.elapsedSec}s`,
          renderSummaryBlock({ ...result.summary, meta: result.meta, tempo: result.meta?.tempo, meter: result.meta?.meter }),
          '',
          `目录: ${project.projectDir}`,
          `可编辑谱: ${result.files.json}`,
          `乐谱文件: ${result.files.musicxml}`,
        ];
        if (result.files.midi) lines.push(`MIDI: ${result.files.midi}`);
        if (result.files.previewPng) lines.push(`整谱长图: ${result.files.previewPng}`);
        const pagePngs = result.files.previewPagePngs ?? [];
        if (pagePngs.length) {
          lines.push(`分页图片: ${pagePngs.length} 张（preview-p1.png … preview-p${pagePngs.length}.png，就在上面这个目录里）`);
        }
        if (result.files.previewHtml) {
          lines.push(`可播放预览: ${result.files.previewHtml}`);
          lines.push('  （浏览器打开就能按谱听音频：播放/暂停、变速、单轨静音、跟随滚动）');
        }
        lines.push('', '想改：score_edit（移调 / 删小节 / 改速度调号 / 删轨 / 改和弦 / 逐音增删改），改完会自动重排并重新出图和预览页。');

        return {
          summary: lines.join('\n'),
          data: {
            projectId: project.projectId,
            projectDir: project.projectDir,
            manifestPath: project.manifestPath,
            files: result.files,
            summary: result.summary,
            meta: result.meta,
            elapsedSec: result.elapsedSec,
          },
        };
      } catch (error) {
        await writeManifest(project.manifestPath, {
          projectId: project.projectId,
          createdAt: new Date().toISOString(),
          status: 'failed',
          mode,
          title,
          input: { path: file, bytes: sizeBytes },
          error: String(error?.message ?? error).slice(0, 2000),
        }).catch(() => {});
        throw error;
      }
    },
  };
}

// --------------------------------------------------------------- score_list

function listTool(cfg, log) {
  return {
    name: 'score_list',
    description: '列出做过的曲谱项目（最近在前），或给 projectId/目录 看某个项目的详情和文件路径。',
    parameters: {
      type: 'object',
      properties: {
        projectId: { type: 'string', description: '项目 id 或目录；不给就列最近的' },
        limit: { type: 'integer', description: '最多列几个，默认 8', minimum: 1, maximum: 50 },
      },
      additionalProperties: false,
    },
    output: textOutput(),
    isConcurrencySafe: () => true,
    async execute(args) {
      const ref = String(args?.projectId ?? '').trim();
      if (ref) {
        const { projectId, dir, manifest } = await resolveProject(cfg.workRoot, ref);
        const files = projectFiles(dir);
        const lines = [
          `《${manifest.title ?? projectId}》`,
          `目录: ${dir}`,
          `状态: ${manifest.status}${manifest.elapsedSec ? `  用时 ${manifest.elapsedSec}s` : ''}`,
        ];
        if (manifest.summary) lines.push('', renderSummaryBlock({
          ...manifest.summary,
          meta: manifest.meta,
          tempo: manifest.summary.tempo ?? manifest.meta?.tempo,
          meter: manifest.summary.meter ?? manifest.meta?.meter,
        }));
        lines.push('', '文件：');
        for (const [key, file] of Object.entries(files)) {
          if (key === 'manifest') continue;
          if (existsSync(file)) lines.push(`· ${key.padEnd(10)} ${file}`);
        }
        return { summary: lines.join('\n'), data: { projectId, dir, manifest, files } };
      }

      const limit = Math.min(50, Math.max(1, Number(args?.limit ?? 8) || 8));
      const projects = await listProjects(cfg.workRoot, limit);
      if (!projects.length) return { summary: `还没有曲谱。目录：${cfg.workRoot}`, data: { projects: [] } };
      const lines = projects.map(({ projectId, manifest }) => {
        const meta = manifest.meta ?? {};
        const bits = [manifest.status];
        if (meta.tempo) bits.push(`${Math.round(meta.tempo)} BPM`);
        if (meta.meter) bits.push(`${meta.meter}/4`);
        if (meta.keyTonic) bits.push(`${meta.keyTonic} ${meta.keyMode}`);
        if (manifest.summary?.tracks?.length) bits.push(`${manifest.summary.tracks.length} 轨`);
        return `· ${projectId}  《${manifest.title ?? '?'}》  ${bits.join(' · ')}`;
      });
      return {
        summary: [`曲谱目录 ${cfg.workRoot}`, '', ...lines].join('\n'),
        data: { projects: projects.map(({ projectId, dir, manifest }) => ({ projectId, dir, manifest })) },
      };
    },
  };
}

// ------------------------------------------------------------- score_snippet

function snippetTool(cfg, log) {
  return {
    name: 'score_snippet',
    description: '读曲谱里某几小节的音符，用音名列出来（例如 "第 5 小节：G4 1拍、B4 半拍"）。'
      + '用来核对识别得准不准，或者看清楚某一段到底是什么音。',
    parameters: {
      type: 'object',
      properties: {
        projectId: { type: 'string', description: '项目 id 或目录；不给就用最近一个' },
        trackId: { type: 'string', description: '只看某一轨（vocals/guitar/drums…）' },
        fromMeasure: { type: 'integer', description: '从第几小节开始（1 起），默认 1' },
        measures: { type: 'integer', description: '看几小节，默认 4', minimum: 1, maximum: 32 },
      },
      additionalProperties: false,
    },
    output: textOutput(),
    isConcurrencySafe: () => true,
    async execute(args) {
      const { dir, manifest } = await resolveProject(cfg.workRoot, args?.projectId);
      const files = projectFiles(dir);
      if (!existsSync(files.json)) throw new Error(`这个项目没有 score.json：${dir}`);
      const model = JSON.parse(await readText(files.json));
      const meter = Number(model?.meta?.meter) || 4;
      const from = Math.max(1, Number(args?.fromMeasure ?? 1) || 1);
      const count = Math.min(32, Math.max(1, Number(args?.measures ?? 4) || 4));
      const lo = (from - 1) * meter;
      const hi = lo + count * meter;
      const trackId = String(args?.trackId ?? '').trim();

      const noteNames = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
      const beatsToText = (ql) => {
        if (Math.abs(ql - 4) < 1e-6) return '全音符';
        if (Math.abs(ql - 3) < 1e-6) return '附点二分';
        if (Math.abs(ql - 2) < 1e-6) return '二分';
        if (Math.abs(ql - 1.5) < 1e-6) return '附点四分';
        if (Math.abs(ql - 1) < 1e-6) return '四分';
        if (Math.abs(ql - 0.75) < 1e-6) return '附点八分';
        if (Math.abs(ql - 0.5) < 1e-6) return '八分';
        if (Math.abs(ql - 0.25) < 1e-6) return '十六分';
        return `${Math.round(ql * 100) / 100}拍`;
      };
      const noteText = (note) => {
        if (note.drum) return `鼓:${note.drum}`;
        const midi = Number(note.pitch);
        return `${noteNames[((midi % 12) + 12) % 12]}${Math.floor(midi / 12) - 1}`;
      };

      const blocks = [];
      for (const track of model.tracks ?? []) {
        if (trackId && track.id !== trackId) continue;
        const notes = (track.notes ?? []).filter((note) => {
          const offset = Number(note.offsetQL);
          return offset >= lo - 1e-6 && offset < hi - 1e-6;
        });
        const label = track.label || INSTRUMENT_LABELS[track.instrument] || track.id;
        if (!notes.length) {
          blocks.push(`${label}（${track.id}）：这一段是空的`);
          continue;
        }
        const grouped = new Map();
        for (const note of notes) {
          const measure = Math.floor(Number(note.offsetQL) / meter) + 1;
          if (!grouped.has(measure)) grouped.set(measure, []);
          grouped.get(measure).push(note);
        }
        const lines = [`${label}（${track.id}）`];
        for (const [measure, items] of [...grouped.entries()].sort((a, b) => a[0] - b[0])) {
          const text = items
            .sort((a, b) => Number(a.offsetQL) - Number(b.offsetQL))
            .map((note) => (note.drum
              ? noteText(note)
              : `${noteText(note)} ${beatsToText(Number(note.durationQL))}`))
            .join('、');
          lines.push(`  第 ${measure} 小节：${text}`);
        }
        blocks.push(lines.join('\n'));
      }

      return {
        summary: [
          `《${model.title ?? manifest?.title ?? '?'}》第 ${from}–${from + count - 1} 小节`
          + `（${meter}/4，${model?.meta?.tempo} BPM，${model?.meta?.keyTonic} ${model?.meta?.keyMode}）`,
          '',
          ...blocks,
        ].join('\n'),
        data: { dir, from, count, tracks: (model.tracks ?? []).map((t) => t.id) },
      };
    },
  };
}

// ---------------------------------------------------------------- score_edit

function editTool(cfg, log) {
  return {
    name: 'score_edit',
    description: '改曲谱。支持：整体或某轨移调、删掉若干小节的音符、改速度/拍号/调号/曲名/轨名、'
      + '删掉整轨、改和弦记号，以及逐音增删改（addNote / setNote / deleteNote / clearNotes，'
      + '用第几小节+第几拍定位，pitch 可以写 "C#4" 或 MIDI 数字）。改完自动重新生成 MusicXML/MIDI/预览图。',
    parameters: {
      type: 'object',
      properties: {
        projectId: { type: 'string', description: '项目 id 或目录；不给就用最近一个' },
        ops: {
          type: 'array',
          description: '编辑动作列表，按顺序执行',
          items: {
            type: 'object',
            properties: {
              op: {
                type: 'string',
                enum: [
                  'transpose', 'deleteMeasures', 'setMeta', 'renameTitle', 'renameTrack',
                  'removeTrack', 'setChords', 'addNote', 'setNote', 'deleteNote', 'clearNotes',
                ],
                description: '动作类型',
              },
              trackId: { type: 'string', description: '针对哪一轨（不给=所有轨/整曲）' },
              semitones: { type: 'integer', description: 'transpose：升几个半音，负数降' },
              from: { type: 'integer', description: 'deleteMeasures：起始小节（0 起）' },
              to: { type: 'integer', description: 'deleteMeasures：结束小节（0 起，含）' },
              key: { type: 'string', description: 'setMeta：tempo / meter / keyTonic / keyMode' },
              value: { description: 'setMeta / renameTitle：新值' },
              label: { type: 'string', description: 'renameTrack：新轨名' },
              chords: { type: 'object', description: 'setChords：{"0":"G","1":"Em"} 键是小节序号（0 起）' },
              measure: { type: 'integer', description: 'addNote/setNote/deleteNote：第几小节（1 起）' },
              beat: { type: 'number', description: '小节里的第几拍（1 起，可带小数，如 2.5）' },
              offsetQL: { type: 'number', description: '也可以直接给小节内的拍偏移（0 起）' },
              pitch: {
                description: '音高：如 "C#4" / "Bb3" / 60（MIDI）；deleteNote 不给则删该处所有音',
              },
              durationQL: { type: 'number', description: '时值（单位：四分音符 = 1）' },
              drum: { type: 'string', description: '鼓轨用：kick / snare / hat / openhat / crash / tom1-3' },
              toleranceQL: { type: 'number', description: 'setNote/deleteNote 的定位容差，默认 0.26 拍' },
            },
            required: ['op'],
            additionalProperties: true,
          },
        },
        noPreview: { type: 'boolean', description: '不重新出预览图（快一点）' },
      },
      required: ['ops'],
      additionalProperties: false,
    },
    output: textOutput(),
    timeoutMs: 20 * 60 * 1000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const { pythonPath } = assertRuntime(cfg);
      const { projectId, dir } = await resolveProject(cfg.workRoot, args?.projectId);
      const workerArgs = [
        'edit',
        '--project', dir,
        '--ops', JSON.stringify(args?.ops ?? []),
        '--verovio-resources', cfg.verovioResources || '',
        '--browser', cfg.browser || '',
      ];
      if (args?.noPreview) workerArgs.push('--no-preview');

      const { result } = await runWorker({ pythonPath, args: workerArgs, signal: exec?.signal });
      const manifest = (await readManifest(dir)) ?? {};
      await writeManifest(path.join(dir, 'manifest.json'), {
        ...manifest,
        status: 'done',
        title: result.summary?.title ?? manifest.title,
        meta: { ...(manifest.meta ?? {}), ...(result.summary?.meta ?? {}) },
        summary: result.summary,
        files: { ...(manifest.files ?? {}), musicxml: result.musicxml, midi: result.midi, json: result.json, previewPng: result.previewPng },
        editedAt: new Date().toISOString(),
      });

      const lines = [
        result.changed?.length ? `改好了：${result.changed.join('；')}` : '没有实际改动（ops 是空的？）',
        '',
        renderSummaryBlock(result.summary),
        '',
        `可编辑谱: ${result.json}`,
      ];
      if (result.previewPng) lines.push(`整谱长图: ${result.previewPng}`);
      if (result.previewPagePngs?.length) lines.push(`分页图片: ${result.previewPagePngs.length} 张`);
      if (result.previewHtml) lines.push(`可播放预览: ${result.previewHtml}`);
      return {
        summary: lines.join('\n'),
        data: { projectId, dir, changed: result.changed, summary: result.summary, files: { musicxml: result.musicxml, midi: result.midi, json: result.json, previewPng: result.previewPng, previewPagePngs: result.previewPagePngs, previewHtml: result.previewHtml } },
      };
    },
  };
}

// -------------------------------------------------------------- score_render

function renderTool(cfg, log) {
  return {
    name: 'score_render',
    description: '把曲谱重新排版，输出 PNG 预览图（和 HTML）。改了 score.json 之后用它出图。',
    parameters: {
      type: 'object',
      properties: {
        projectId: { type: 'string', description: '项目 id 或目录；不给就用最近一个' },
      },
      additionalProperties: false,
    },
    output: textOutput(),
    timeoutMs: 10 * 60 * 1000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const { pythonPath } = assertRuntime(cfg);
      const { projectId, dir } = await resolveProject(cfg.workRoot, args?.projectId);
      const { result } = await runWorker({
        pythonPath,
        args: ['build', '--project', dir, '--verovio-resources', cfg.verovioResources || '', '--browser', cfg.browser || ''],
        signal: exec?.signal,
      });
      const lines = [
        `重排完成 · 《${result.summary?.title ?? '?'}》`,
        renderSummaryBlock(result.summary),
        '',
        `乐谱: ${result.musicxml}`,
      ];
      if (result.previewPng) lines.push(`整谱长图: ${result.previewPng}`);
      if (result.previewPagePngs?.length) lines.push(`分页图片: ${result.previewPagePngs.length} 张（preview-pN.png）`);
      if (result.previewHtml) lines.push(`可播放预览: ${result.previewHtml}`);
      if (!result.previewPng && !result.previewHtml) lines.push('预览没出成：无头浏览器不可用？');
      return { summary: lines.join('\n'), data: { projectId, dir, files: result } };
    },
  };
}

// -------------------------------------------------------------- score_parts

function partsTool(cfg, log) {
  const KIND_NAME = { jianpu: '简谱', tab: '六线谱', staff: '五线谱' };
  return {
    name: 'score_parts',
    description:
      '出「演奏谱」：按乐器分别出谱并分目录存放——人声出简谱（可带歌词）、吉他/贝斯出六线谱（带品位、扫弦箭头、和弦名）、'
      + '钢琴和其他乐器出五线谱、鼓出打击乐谱。每份谱单独一个目录，互不混排。',
    parameters: {
      type: 'object',
      properties: {
        projectId: { type: 'string', description: '项目 id 或目录；不给就用最近一个' },
        lyrics: { type: 'string', description: '歌词文件路径（UTF-8，一行一句）；给了就印在简谱下面' },
        only: {
          type: 'array',
          items: { type: 'string' },
          description: '只出这些轨（track id），例如 ["guitar"]；不给就全部',
        },
      },
      additionalProperties: false,
    },
    output: textOutput(),
    timeoutMs: 20 * 60 * 1000,
    isConcurrencySafe: () => true,
    presentCall: (args) => ({
      card: 'generic',
      title: '出分乐器演奏谱',
      kind: 'execute',
      rawInput: args ?? {},
    }),
    async execute(args, exec) {
      const { pythonPath } = assertRuntime(cfg);
      const { projectId, dir } = await resolveProject(cfg.workRoot, args?.projectId);
      const workerArgs = ['parts', '--project', dir];
      if (args?.lyrics) workerArgs.push('--lyrics', String(args.lyrics));
      if (Array.isArray(args?.only) && args.only.length) workerArgs.push('--only', args.only.join(','));
      if (cfg.browser) workerArgs.push('--browser', String(cfg.browser));
      const { result } = await runWorker({ pythonPath, args: workerArgs, signal: exec?.signal });

      const parts = result.parts ?? [];
      const lines = [
        `演奏谱出好了 · 《${result.summary?.title ?? '?'}》`,
        renderSummaryBlock(result.summary),
        '',
      ];
      for (const part of parts) {
        const kind = KIND_NAME[part.kind] ?? part.kind;
        lines.push(`· ${part.label}（${kind}）`);
        if (part.png) lines.push(`  谱面: ${part.png}`);
        if (part.musicxml) lines.push(`  可导入: ${part.musicxml}`);
      }
      lines.push('', `总目录: ${result.partsDir}`, `索引页: ${result.partsIndex}`);
      lines.push('（每个乐器一个独立目录：吉他目录里只有六线谱，人声目录里只有简谱）');
      return {
        summary: lines.join('\n'),
        data: { projectId, dir, partsDir: result.partsDir, partsIndex: result.partsIndex, parts },
      };
    },
  };
}

async function readText(file) {
  const { readFile } = await import('node:fs/promises');
  return readFile(file, 'utf8');
}
