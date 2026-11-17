# dsh-plugin-hello

DSH 插件最小示例：注册一个 `hello_now` 工具（已在 DSH 0.2.0-rc.2 实测跑通）。

## 目录

```
package.json        声明入口、dsh.bundle.patch、engines
cordis.patch.yml    往 DSH 配置树里插入一行，把插件挂上
lib/index.js        导出 name / inject / apply
```

## 三个必踩的坑

1. **别 import `@deepseek-ai/dsh-tools`**。那是宿主提供的 peer，插件自己的
   node_modules 里没有；import 会抛 `ERR_MODULE_NOT_FOUND`，而且**静默失败**——
   插件不加载，界面和日志都不提示。工具直接写成纯对象。
2. **访问 `ctx.tools` 前必须 `export const inject = ['tools']`**，否则 cordis 报
   `Error: cannot get property "tools" without inject`。
3. **`file:` 安装是拷贝，改代码不生效**；开发时用 `link:`，它建的是 junction。

## 安装（开发用 link）

```powershell
dsh plugin --profile pt add "link:D:\dsh 大肥鱼\plugins\dsh-plugin-hello"
```

装完检查 profile 的 `package.json` 里 `dsh.profile.bundles` 是否有
`dsh-plugin-hello`；没有就手加一行。

## 验证

```powershell
'立刻调用 hello_now 工具（who 填 老大）' | dsh --profile pt --json -
```

应看到 `{"type":"tool_call","tool":"hello_now","input":{"who":"老大"}}`。
