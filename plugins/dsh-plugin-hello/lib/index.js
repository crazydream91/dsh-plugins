// dsh-plugin-hello —— 最小可用插件
//
// 一个 DSH 插件就是一个普通的 ESM 模块，导出 apply(ctx, config)。
// 想在 agent 里多一个"能调用的动作"，就往 ctx.tools 里注册一个工具。
//
// 注意：不要 import '@deepseek-ai/dsh-tools'。那个包是宿主提供的 peer，
// 插件自己的依赖里没有它，import 会直接抛 ERR_MODULE_NOT_FOUND，
// 而且失败是静默的——插件不加载，什么提示都没有。
// 正确做法是把工具写成"纯对象"，字段自己写全。
//
// 另外：访问 ctx.tools 之前必须先用 inject 声明，否则 cordis 会拦下来：
//   Error: cannot get property "tools" without inject

export const name = 'dsh-plugin-hello';

// 声明依赖的服务：tools 由 dsh-base 提供
export const inject = ['tools'];

export function apply(ctx, config = {}) {
  const greeting = config.greeting ?? '你好';

  // 声明过 inject 之后 ctx.tools 才拿得到；再兜一层，免得整个插件炸掉
  if (!ctx.tools || typeof ctx.tools.register !== 'function') {
    ctx.logger?.warn?.('dsh-plugin-hello: 没有 tools 服务，跳过注册');
    return;
  }

  // ctx.effect 包起来：插件卸载/热重载时，register 返回的 disposer 会被自动调用
  ctx.effect(() =>
    ctx.tools.register({
      name: 'hello_now',
      description: '跟用户打个招呼，并返回当前的日期时间。演示插件工具的最小实现。',

      // parameters 是标准 JSON Schema（不是简写的 { key: {type} }）
      parameters: {
        type: 'object',
        properties: {
          who: { type: 'string', description: '要打招呼的对象，例如"老大"' },
        },
        required: [],
        additionalProperties: false,
      },

      output: {
        schema: {
          type: 'object',
          properties: { text: { type: 'string' } },
          required: ['text'],
        },
        // render 决定这段结果以什么形态喂给模型
        render: (_args, value) => [{ type: 'text', text: value.text }],
      },

      // execute 的返回值必须符合上面的 output.schema
      execute(args) {
        const who = String(args?.who ?? '你').trim() || '你';
        const now = new Date().toLocaleString('zh-CN', { hour12: false });
        return { text: `${greeting}，${who}！现在是 ${now}。` };
      },
    }),
  );
}
