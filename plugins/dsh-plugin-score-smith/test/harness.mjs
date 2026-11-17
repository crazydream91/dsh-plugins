// 本地冒烟测试：不开 DSH，直接拿假 ctx 调 apply()，然后逐个跑工具。
// 用法：node test/harness.mjs [doctor|list|snippet|edit|render]
import { apply, name, inject } from '../lib/index.js';

const registered = [];
const ctx = {
  logger: {
    info: (...args) => console.log('[log]', ...args),
    warn: (...args) => console.warn('[warn]', ...args),
    error: (...args) => console.error('[err]', ...args),
    debug: () => {},
  },
  tools: {
    register(definition) {
      registered.push(definition);
      return () => {};
    },
  },
  effect(fn) {
    fn();
    return () => {};
  },
};

apply(ctx, {});

console.log(`plugin ${name} inject=${JSON.stringify(inject)} tools=${registered.length}`);
for (const tool of registered) console.log(`  - ${tool.name}: ${tool.description.slice(0, 60)}`);

const which = process.argv[2] || 'doctor';
const targets = registered.filter((tool) => tool.name === `score_${which}`);
if (!targets.length) {
  console.error(`没有 score_${which}`);
  process.exit(1);
}

const exec = {
  signal: new AbortController().signal,
  sessionId: 'harness',
  toolCallId: 'harness-1',
};

const argsByTool = {
  doctor: {},
  list: {},
  transcribe: {
    path: `${process.env.TEMP}\\score-synth\\mix.wav`,
    stemsDir: `${process.env.TEMP}\\score-synth`,
    title: 'harness 全流程',
    maxSeconds: 20,
  },
  snippet: {
    fromMeasure: 1,
    measures: 2,
  },
  edit: {
    ops: [{ op: 'setMeta', tempo: 98 }],
    preview: false,
  },
  render: {},
};

const tool = targets[0];
const started = Date.now();
const value = await tool.execute(argsByTool[which] ?? {}, exec);
console.log(`\n=== ${tool.name} (${Date.now() - started}ms) ===`);
console.log(JSON.stringify(value, null, 2).slice(0, 4000));
const rendered = tool.output?.render?.(argsByTool[which] ?? {}, value);
if (rendered) {
  console.log('\n--- rendered ---');
  console.log(rendered.map((part) => part.text).join('\n').slice(0, 2000));
}
