// 起 Python 侧的 demucs worker，把 stdout 的 JSON 事件翻译成回调和结果。
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { CommandError, runCommand, tailLines } from './util.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
export const WORKER_SCRIPT = path.join(here, '..', 'py', 'stems_worker.py');

export async function runSeparation({
  pythonPath,
  script = WORKER_SCRIPT,
  input,
  outDir,
  model,
  device = 'auto',
  shifts = 1,
  overlap = 0.25,
  segmentSec = 0,
  twoStems = '',
  format = 'wav',
  mp3Bitrate = 320,
  clipMode = 'rescale',
  saveOnly = '',
  torchHome,
  repoDir,
  signal,
  onProgress,
}) {
  const args = [script,
    '--input', input,
    '--out-dir', outDir,
    '--model', model,
    '--device', device,
    '--shifts', String(shifts),
    '--overlap', String(overlap),
    '--format', format,
    '--mp3-bitrate', String(mp3Bitrate),
    '--clip-mode', clipMode,
  ];
  if (Number(segmentSec) > 0) args.push('--segment', String(segmentSec));
  if (twoStems) args.push('--two-stems', twoStems);
  if (saveOnly) args.push('--save-only', saveOnly);
  if (repoDir) args.push('--repo-dir', repoDir);

  const env = {
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    PYTHONUNBUFFERED: '1',
    PYTHONDONTWRITEBYTECODE: '1',
  };
  if (torchHome) env.TORCH_HOME = torchHome;

  let resultEvent = null;
  let errorEvent = null;
  let lastPct = 0;
  let stderrTail = '';

  const res = await runCommand(pythonPath, args, {
    env,
    signal,
    onStdoutLine(line) {
      const text = line.trim();
      if (!text.startsWith('{')) return;
      let event;
      try {
        event = JSON.parse(text);
      } catch {
        return;
      }
      switch (event.type) {
        case 'progress':
          if (Number.isFinite(event.pct) && event.pct >= lastPct + 5) {
            lastPct = event.pct;
            onProgress?.({ pct: event.pct, stage: 'separate', message: '分离中' });
          }
          break;
        case 'phase':
          onProgress?.({ pct: event.pct ?? lastPct, stage: event.stage, message: event.msg });
          break;
        case 'start':
          break;
        case 'result':
          resultEvent = event;
          break;
        case 'error':
          errorEvent = event;
          break;
        default:
          break;
      }
    },
    onStderrLine(line) {
      stderrTail = `${stderrTail}\n${line}`.slice(-8000);
    },
  });

  if (res.spawnError) {
    throw new CommandError(
      `启动 Python 失败（${pythonPath}）：${res.spawnError.message}\n`
      + '检查插件配置里的 pythonPath 是不是那个装了 torch/demucs 的 venv。',
    );
  }

  if (res.code !== 0 || !resultEvent) {
    if (errorEvent?.message) {
      throw new CommandError(errorEvent.message, { traceback: errorEvent.traceback });
    }
    if (res.aborted) throw new CommandError('分离被中断。');
    throw new CommandError(
      `分离进程退出（exit ${res.code}）。\n${tailLines(stderrTail, 10) || '没有更多错误输出。'}`,
    );
  }

  return { result: resultEvent, stderrTail };
}
