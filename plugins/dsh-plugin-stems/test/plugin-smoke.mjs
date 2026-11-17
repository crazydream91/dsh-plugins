// 用一个假的 ctx 把 lib/index.js 装一遍：确认模块能加载、工具能注册、selftest 能跑。
// 这样不用重启 DSH 也能验证改动（宿主的 ESM 缓存会把旧模块一直留在内存里）。
//
//   node test/plugin-smoke.mjs            只注册 + 列工具
//   node test/plugin-smoke.mjs selftest   再跑一遍自我检查
//   node test/plugin-smoke.mjs split <媒体文件>  真跑一次两轨分离（验新代码的 repoDir 路径）

import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const here = import.meta.dirname;
const pluginPath = resolve(here, '..', 'lib', 'index.js');
const mod = await import(pathToFileURL(pluginPath).href);

const registered = new Map();
const errors = [];

const ctx = {
  logger: () => () => {},
  tools: {
    register(definition) {
      if (!definition || !definition.name) {
        errors.push('注册了一个没有 name 的工具');
        return () => {};
      }
      registered.set(definition.name, definition);
      return () => registered.delete(definition.name);
    },
  },
  effect(fn) {
    try {
      fn();
    } catch (err) {
      errors.push(`effect 抛错: ${err?.message ?? err}`);
    }
  },
};

mod.apply(ctx, {});

console.log(`plugin: ${mod.name}`);
console.log(`inject: ${JSON.stringify(mod.inject)}`);
console.log(`tools : ${[...registered.keys()].join(', ')}`);

for (const [name, definition] of registered) {
  const problems = [];
  if (typeof definition.description !== 'string' || !definition.description.trim()) {
    problems.push('没有 description');
  }
  if (!definition.parameters || definition.parameters.type !== 'object') {
    problems.push('parameters 不是 object schema');
  }
  if (typeof definition.execute !== 'function') problems.push('没有 execute');
  if (!definition.output || typeof definition.output.render !== 'function') {
    problems.push('没有 output.render');
  }
  if (problems.length) errors.push(`${name}: ${problems.join('，')}`);
}

if (process.argv.includes('selftest')) {
  const tool = registered.get('stems_selftest');
  if (!tool) {
    errors.push('找不到 stems_selftest');
  } else {
    const controller = new AbortController();
    const value = await tool.execute({}, { signal: controller.signal, callId: 'smoke' });
    const blocks = tool.output.render({}, value);
    console.log('---- selftest ----');
    for (const block of blocks) {
      if (block?.type === 'text') console.log(block.text);
    }
  }
}

const splitIndex = process.argv.indexOf('split');
if (splitIndex > 0) {
  const tool = registered.get('stems_split');
  const target = process.argv[splitIndex + 1];
  if (!tool) errors.push('找不到 stems_split');
  else if (!target) errors.push('split 后面要跟媒体文件路径');
  else {
    const controller = new AbortController();
    const startedAt = Date.now();
    const value = await tool.execute(
      { path: target, mode: 'vocals' },
      { signal: controller.signal, callId: 'smoke-split' },
    );
    console.log('---- split ----');
    for (const block of tool.output.render({ path: target }, value)) {
      if (block?.type === 'text') console.log(block.text);
    }
    console.log(`wall: ${((Date.now() - startedAt) / 1000).toFixed(2)}s`);
  }
}

if (errors.length) {
  console.error('---- 问题 ----');
  for (const line of errors) console.error(`· ${line}`);
  process.exitCode = 1;
} else {
  console.log('ok');
}
