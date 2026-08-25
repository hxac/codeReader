# 构建、发布与二次开发路线图（u7-l5·全手册收官篇）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `npm run build` 背后发生的两件事——rollup 打包与 `cleanup-index-js.sh` 文本清洗——以及为什么要清洗。
2. 理解 WebLLM 的三层质量门禁：本地 pre-commit 钩子（Husky + lint-staged）、CI 三个 workflow（Linter/Tests/Build）、jest 覆盖率阈值。
3. 拿到一份「自定义功能扩展点清单」，知道加一个引擎 API、接一个模型、改一个采样行为分别应该动哪些文件。
4. 独立完成一次端到端二次开发：给 `MLCEngineInterface` 新增 `getContextTokensLeft()` 方法，在接口、引擎、Worker 协议三线打通，补单测、过门禁、构建出 `lib/` 并在示例页面验证。
5. 结合本手册前 26 讲所学，规划一个属于自己的基于 WebLLM 的小型 AI 应用。

## 2. 前置知识

本讲是收官篇，不再引入新的推理机制，但需要你熟悉以下工程侧概念（不熟悉的术语下面逐一解释）：

- **npm scripts 与包生命周期**：`package.json` 的 `scripts` 字段定义命令别名；其中 `prepare` 是 npm 的特殊生命周期钩子，在 `npm install`（装依赖）后自动执行，本仓库用它初始化 Husky。
- **打包器（bundler）与 ESM**：rollup 把多个 TypeScript 模块沿 import 关系合并成单个 JS 文件。`"type": "module"` 声明包使用 ESM（`import/export`）而非 CommonJS（`require`）。这一差异正是后文 `cleanup-index-js.sh` 一堆补丁的根源——被内联的依赖（tvmjs 运行时）内部残留 `require()` 调用，在浏览器/ESM 环境会炸。
- **tree-shaking 与 barrel file**：rollup 从入口出发只保留真正被引用的代码；`src/index.ts` 是一个纯再导出的「桶文件」（barrel file，u1-l3 已讲），它是唯一的打包入口。
- **CI 质量门禁**：GitHub Actions 在每次 push/PR 时自动跑 lint、测试、构建，不通过则禁止合并，相当于机器化的代码评审第一道关卡。
- **覆盖率阈值**：jest 统计测试执行到的语句/分支/函数/行占比，低于 `jest.config.cjs` 配置的阈值直接失败。
- **fork / branch / PR 工作流**：fork 仓库到自己账号 → 建特性分支 → 提交 → 向上游发 Pull Request。
- **前置讲义**：本讲直接建立在 u7-l4（测试体系）与 u5-l1/u5-l2（Worker 架构与消息协议）之上，并反复引用 u1-l3（源码地图）、u3-l4（KV cache 与上下文窗口）的结论。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rollup.config.js](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/rollup.config.js#L1-L28) | rollup 打包配置：入口、产物格式、插件链 |
| [package.json](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L1-L65) | npm 包身份、scripts、依赖与发布文件清单 |
| [cleanup-index-js.sh](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/cleanup-index-js.sh#L1-L39) | 对打包产物做 sed 文本修补，抹掉 Node 专有代码 |
| [src/index.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts#L1-L63) | 库入口 barrel file，打包的唯一起点 |
| [CONTRIBUTING.md](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/CONTRIBUTING.md#L1-L143) | 官方贡献指南：环境搭建、联调、PR 规范 |
| [jest.config.cjs](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/jest.config.cjs#L1-L21) | 测试环境与覆盖率阈值（u7-l4 详述） |
| [eslint.config.cjs](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/eslint.config.cjs#L1-L50) | ESLint 扁平配置，叠加 Prettier 规则 |
| [.lintstagedrc.json](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.lintstagedrc.json#L1-L3) / [.husky/pre-commit](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.husky/pre-commit#L1-L1) / [.prettierrc](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.prettierrc#L1-L3) | pre-commit 自动格式化链 |
| [.github/workflows/build.yaml](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.github/workflows/build.yaml#L17-L38) / [linter.yaml](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.github/workflows/linter.yaml#L11-L27) / [tests.yaml](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.github/workflows/tests.yaml#L17-L45) | CI 三道门禁 |
| [src/types.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L140-L243) | `MLCEngineInterface` 契约（二开实践的改动点①） |
| [src/engine.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1152-L1240) | 引擎实现与 `getLLMStates` 路由（改动点②） |
| [src/llm_chat.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L90-L120) | 管线私有状态 `filledKVCacheLength`/`contextWindowSize`（改动点③） |
| [src/message.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L15-L141) | Worker 消息协议（改动点④） |
| [src/web_worker.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L255-L330) | Handler 路由与主线程代理（改动点⑤） |
| [tests/web_worker_handler.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L1-L60) | 单测样板（u7-l4 的模块级 mock 模式） |

## 4. 核心概念与源码讲解

本讲三个最小模块：**rollup 构建链**、**lint/format/test 工具链**、**自定义功能扩展点清单**。

---

### 4.1 rollup 构建链：从 src/index.ts 到 npm 包

#### 4.1.1 概念说明

WebLLM 对外发布的形态是一个 npm 包 `@mlc-ai/web-llm`。观察 [package.json 的 files 字段](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L15-L17)：

```json
"files": ["lib"],
```

发布到 npm 的**只有 `lib/` 目录**——即构建产物，源码 `src/`、示例、测试统统不进包。包的入口由 [main 与 types 字段](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L5-L7) 指定为 `lib/index.js` 与 `lib/index.d.ts`，且 `"type": "module"` 声明这是 ESM 包。

为什么要打包成单文件？两个原因：

1. **运行时依赖极简**。看 [dependencies](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L58-L60)：唯一的运行时依赖是 `loglevel`。tvmjs 运行时（`@mlc-ai/web-runtime`）、tokenizer（`@mlc-ai/web-tokenizers`）、xgrammar（`@mlc-ai/web-xgrammar`）全部是 devDependencies，在构建时被**内联**进产物——用户 `npm install @mlc-ai/web-llm` 时不需要再拉这些包，也避免了版本漂移。
2. **CDN 直引友好**。单 ESM 文件可以被 `<script type="module">` 或 `import` 从 CDN 直接加载（u1-l2 的 get-started 就是这么用的）。

#### 4.1.2 核心流程

[构建脚本](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L8-L13) 是两步串联：

```bash
npm run build   # 等价于：rollup -c && ./cleanup-index-js.sh
```

流程如下：

```text
src/index.ts (barrel file, 唯一入口)
      │
      ▼  rollup（插件链依次生效）
 ├─ typescript 插件：按 tsconfig.json 做类型检查与转译
 ├─ nodeResolve({browser:true})：从 npm 视角解析 import，
 │    并优先选择依赖的「浏览器版」入口
 ├─ commonjs：把 CJS 依赖转成 ESM 以便合并
 └─ ignore(["fs","path","crypto",...])：把 Node 内置模块的
      import 替换成空实现（浏览器里没有这些模块）
      │
      ▼
lib/index.js + lib/index.js.map  （ESM、named exports、带 sourcemap）
      │
      ▼  cleanup-index-js.sh（sed 文本修补）
抹掉内联依赖残留的 require()/import('module') 等 Node 专有代码
      │
      ▼
最终发布产物（npm pack 只打包 lib/）
```

#### 4.1.3 源码精读

**（a）rollup 配置只有 27 行**，见 [rollup.config.js:L6-L27](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/rollup.config.js#L6-L27)：

- 第 7 行：`input: "src/index.ts"`——单入口，u1-l3 讲过这个 63 行的 barrel file 是全部公开 API 的收口，`export * from "./openai_api_protocols/index"`（[src/index.ts:L63](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts#L63)）把 OpenAI 协议类型也一并带出。
- 第 10-14 行：产物 `lib/index.js`，`format: "es"`（ESM）、`exports: "named"`（具名导出，使用者必须 `import { CreateMLCEngine }` 而不能默认导入）、`sourcemap: true`。
- [第 18 行](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/rollup.config.js#L18)：`ignore` 插件把 `fs`、`path`、`crypto`（含 `node:` 前缀变体）的引用变成空模块——这些是内联的 tvmjs 在 Node 环境下才会走的分支，浏览器里必须哑掉。

**（b）为什么构建完还要跑 shell 脚本？** 打包可以合并代码，但没法「理解」代码语义。被内联的依赖在源码里写死了若干 Node 专有调用，rollup 原样保留它们，于是产物在特定框架里会崩。[cleanup-index-js.sh](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/cleanup-index-js.sh#L1-L39) 用 `sed` 做四组字符串替换（同步修补 `.js` 与 `.js.map`，最后删备份）：

| 替换 | 替换为 | 解决的问题 |
| --- | --- | --- |
| [L4-L5](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/cleanup-index-js.sh#L4-L5)：`const{createRequire:createRequire}=await import('module');` | 空串 | Parcel 打包的 Chrome 扩展 worker 无法执行该顶层动态 import |
| [L8-L9](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/cleanup-index-js.sh#L8-L9)：`require("url").fileURLToPath(new URL("./",import.meta.url))` | `"./"` | Parcel 解析不了 `new URL('./', import.meta.url)` 形式的 scriptDirectory 初始化 |
| [L11-L20](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/cleanup-index-js.sh#L11-L20)：`new (require('u' + 'rl').URL)('file:' + __filename).href` 及其新版变体 | `"MLC_DUMMY_PATH"` | Next.js 编译期报 `require()` 错误（issue #383）；SvelteKit/Astro 的 ESM SSR 环境报 `require is not defined` |
| [L22-L35](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/cleanup-index-js.sh#L22-L35)：`import require$$N from 'perf_hooks'`、`import require$$N from 'ws'` 及 require 形式 | `"MLC_DUMMY_REQUIRE_VAR"` | 浏览器找不到 `perf_hooks`（issue #258、#127）与 `ws` 模块 |

注意一个细节：这些替换目标是**产物里 rollup 生成的中间变量名**（如 `require$$0`），所以脚本用正则匹配任意编号。这也解释了为什么 rollup 大版本升级（输出格式变化）可能需要同步改这个脚本——[L15-L17](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/cleanup-index-js.sh#L15-L17) 就同时处理了新旧两种 `u' + 'rl'` 写法。

**（c）发布流程**：仓库 `.github/workflows/` 下只有 build-site、security、tests、build、linter 五个 workflow，**没有自动发 npm 的 workflow**；结合 git 历史中独立成 PR 的 `[Version] Bump version to 0.2.84 (#825)` 提交（即 [package.json:L3](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L3) 的 `0.2.84`）可以推断：版本号通过 PR 手动 bump，npm 发布由维护者在本地执行 `npm run build` + `npm publish` 完成（此为基于仓库证据的推断，发布细节「待确认」）。CI 侧的兜底是 [build.yaml:L34-L38](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.github/workflows/build.yaml#L34-L38)：跑完 `npm run build` 后执行 `npm pack --dry-run`，干跑打包并校验即将发布的文件清单确实只含 `lib/`。

#### 4.1.4 代码实践：亲手构建一次并解剖产物

1. **实践目标**：验证「src → lib」的完整变换，亲眼看到 cleanup 脚本留下的痕迹。
2. **操作步骤**：
   ```bash
   npm install          # 会触发 prepare 钩子安装 husky
   npm run build
   ls lib/              # 观察产物文件清单
   grep -c "MLC_DUMMY_PATH" lib/index.js
   grep -c "MLC_DUMMY_REQUIRE_VAR" lib/index.js
   grep -n "createRequire" lib/index.js || echo "已清除"
   npm pack --dry-run   # 干跑发布，核对只含 lib/
   ```
3. **需要观察的现象**：`MLC_DUMMY_PATH` 与 `MLC_DUMMY_REQUIRE_VAR` 出现次数 ≥ 1；`createRequire` 无残留；`npm pack --dry-run` 列出的文件全部位于 `lib/` 下。
4. **预期结果**：与你对 cleanup-index-js.sh 四组替换的理解一一对应。若某个计数为 0，回到脚本对照是哪条 sed 没匹配上（可能与依赖版本升级有关）。
5. 本讲义写作环境未执行上述命令，具体产物文件名与计数「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `@mlc-ai/web-runtime` 放在 devDependencies 而不是 dependencies？
**答案**：它只在构建期被打包内联进 `lib/index.js`，运行时使用者加载的是产物里的代码，不需要独立安装；放 dependencies 会给每个下游项目多装一个包且引入版本漂移风险。运行时真正被产物 `import` 的外部包只有 `loglevel`。

**练习 2**：如果不跑 `cleanup-index-js.sh` 直接用 `lib/index.js`，最可能在哪类项目里崩？
**答案**：Next.js（编译期报 `require()`）、SvelteKit/Astro 的 ESM SSR（`require is not defined`）、Parcel 打包的 Chrome 扩展 worker（顶层 `await import('module')` 失败）以及任何触发 `perf_hooks`/`ws` 导入的浏览器环境。普通浏览器 + Vite 场景可能恰好不触发，属于「侥幸通过」。

**练习 3**：`exports: "named"` 对使用者意味着什么？
**答案**：包只暴露具名导出，使用者必须写 `import { CreateMLCEngine } from "@mlc-ai/web-llm"`，写 `import WebLLM from ...`（默认导入）会得到 `undefined`。这与 [src/index.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts#L1-L63) 全部使用 `export { ... }` 的写法一致。

---

### 4.2 lint / format / test 工具链：三层质量门禁

#### 4.2.1 概念说明

WebLLM 用三层防线保证合入 main 的代码质量：

1. **本地提交时**：Husky 挂载 git pre-commit 钩子 → lint-staged 只对暂存文件跑 `eslint --fix` + `prettier --write`，格式问题在你提交瞬间被自动修复。
2. **CI 提交后**：三个独立 workflow 各司其职——Linter 跑 `npm run lint`，Tests 跑 `npm test -- --ci` 并上传覆盖率产物，Build 跑 `npm run build` + `npm pack --dry-run`。
3. **测试内部**：jest 覆盖率阈值（global 25/20/20/25，`engine.ts` 单独更高：35/25/40/35）作为硬门禁，低了对不上（u7-l4 详述）。

术语解释：**ESLint** 负责代码质量规则（未使用变量、类型错误模式等）；**Prettier** 负责纯格式（缩进、引号、换行）；`eslint-plugin-prettier` 把两者接通，让 ESLint 能报告格式问题——所以本仓库 `npm run lint` 一条命令同时查两类问题。

#### 4.2.2 核心流程

```text
你改完代码
   │ git commit
   ▼
.husky/pre-commit → npx lint-staged
   │ 按 .lintstagedrc.json 对暂存的 js/ts/json 跑
   │   eslint --fix && prettier --write
   ▼（自动修复后若有残留错误，提交被阻止）
git push
   ▼
GitHub Actions 并行触发：
 ├─ Linter:  npm install && npm run lint
 ├─ Tests:   npm ci && npm run test -- --ci   （附覆盖率阈值检查）
 └─ Build:   npm ci && npm run build && npm pack --dry-run
   ▼ 全绿才可合并 PR
```

#### 4.2.3 源码精读

**（a）scripts 一览**，见 [package.json:L8-L13](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L8-L13)：

- `build`：`rollup -c && ./cleanup-index-js.sh`（上一节已拆解）。
- `lint`：`eslint ./src/ ./tests/ ./examples/` 加 `prettier --check`——注意检查范围包含 examples，改示例不格式化一样挂门禁。
- `test`：`jest --coverage`，覆盖率因此默认开启。
- `format`：`prettier --write`，修复用。
- `prepare: husky`：npm 生命周期钩子，`npm install` 后自动启用 git 钩子。

**（b）格式化配置三件套**：

- [.prettierrc](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.prettierrc#L1-L3) 只有一条规则 `trailingComma: "all"`——多行结构末尾强制逗号（看本讲引用的任何源码片段都能验证）。
- [.lintstagedrc.json](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.lintstagedrc.json#L1-L3)：对所有暂存的 `js/ts/jsx/tsx/json` 执行 `eslint --fix` 与 `prettier --write`。
- [.husky/pre-commit](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.husky/pre-commit#L1-L1)：仅一行 `npx lint-staged`。

**（c）ESLint 扁平配置**：[eslint.config.cjs](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/eslint.config.cjs#L16-L35) 继承 `eslint:recommended`、`@typescript-eslint/recommended` 与 `plugin:prettier/recommended` 三套预设，然后有针对性地放宽了 `no-explicit-any`、`no-empty-function`、`no-non-null-assertion` 三条（LLM 推理代码里 `any` 与非空断言常见）；另对 `examples/**` 关闭 `no-undef` 与 `no-unused-vars`（示例代码常省略）。`globalIgnores` 排除 `dist/debug/lib` 等生成目录。

**（d）CI 与覆盖率**：

- [linter.yaml:L26-L27](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.github/workflows/linter.yaml#L26-L27)：push/PR 到 main 即跑 lint，Node 版本由 [.nvmrc](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.nvmrc#L1-L1)（v24.11.1）钉死。
- [tests.yaml:L34-L45](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/.github/workflows/tests.yaml#L34-L45)：`npm ci` + `npm run test -- --ci`，且无论成败都上传 `coverage/` 为 artifact，失败时可下载报告定位没覆盖的分支。
- [jest.config.cjs:L7-L20](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/jest.config.cjs#L7-L20)：全局阈值之外给 `src/engine.ts` 单独设了更高门槛——引擎是所有请求的必经之路，理应测得更密。

**（e）官方贡献流程要点**（[CONTRIBUTING.md](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/CONTRIBUTING.md#L47-L80)）：

- 环境搭建就三步：`git clone` → `npm install`（[L49-L53](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/CONTRIBUTING.md#L49-L53)）。
- 迭代单个测试文件用 [L67-L70](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/CONTRIBUTING.md#L65-L70) 的快捷命令：`npx jest --coverage=false tests/<file>.test.ts`——跳过覆盖率，避免「改一行代码却因全局覆盖率下降而测试失败」的误伤。
- **本地包联调示例**（[L82-L93](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/CONTRIBUTING.md#L82-L93)）：把示例的 `package.json` 里 `"@mlc-ai/web-llm"` 改成 `"../.."`，`npm install` 后示例用的就是本地源码包——这是二开验证的标准姿势。
- PR 规范（[L107-L125](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/CONTRIBUTING.md#L107-L125)）：一次只解决一个问题、行为变更必须带测试、跑过 lint 与 test、PR 描述写清问题/方案/验证步骤/兼容性。

#### 4.2.4 代码实践：体验三层门禁

1. **实践目标**：感受「提交即修复、推送即检查」的自动化链路。
2. **操作步骤**：
   ```bash
   # 先确认基线是绿的
   npm run lint && npm test

   # 制造一处格式问题（例如把 src/types.ts 某行末尾逗号删掉、缩进改乱）
   # 然后不手动 format，直接提交：
   git add src/types.ts
   git commit -m "test: trigger lint-staged"
   git diff HEAD~1 -- src/types.ts   # 观察提交的版本是否已被自动修复

   # 再验证快捷单测命令：
   npx jest --coverage=false tests/generation_config.test.ts
   ```
3. **需要观察的现象**：commit 成功且提交进仓库的内容是**格式化后**的（pre-commit 把它修好了）；`generation_config.test.ts` 单文件快速跑通且不输出覆盖率表格。
4. **预期结果**：理解 lint-staged 修的是「暂存区副本」，你工作区里未暂存的其他文件不受影响。
5. 本环境未执行上述命令，现象「待本地验证」；若 lint-staged 未生效，检查 `npm install` 是否装上了 husky（`prepare` 钩子可能被 `--ignore-scripts` 跳过）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CI 的 Tests 用 `npm ci` 而 Linter 用 `npm install`？
**答案**：`npm ci` 严格按 `package-lock.json` 安装、速度更快且可复现（适合 CI）；Linter 早期配置沿用了 `npm install`，功能上也能跑通，但 `npm ci` 是更规范的 CI 实践——这是仓库现状的一个可改进点，也提醒你两 种命令的差异。

**练习 2**：为什么 `engine.ts` 的覆盖率门槛比全局高？
**答案**：`src/engine.ts` 是所有用户请求的必经编排层（路由、锁、usage 组装都在这），它的未覆盖分支意味着核心路径没被测试保护；而 `config.ts` 这类大体量数据文件（167 条模型记录）天然难以逐行覆盖，拉低了全局阈值，所以全局门槛定得相对宽松。

**练习 3**：给项目加了一个新源文件但忘了写测试，哪一层门禁会先拦住你？
**答案**：大概率是 CI 的 Tests 层——覆盖率是对 `src/**` 整体统计的（[jest.config.cjs:L6](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/jest.config.cjs#L6) `collectCoverageFrom: ["src/**/*.{ts,tsx}", ...]`），新文件 0 覆盖会把全局百分比拉低到阈值以下。本地 pre-commit 只查格式与 lint 规则，不管覆盖率。

---

### 4.3 自定义功能扩展点清单与二开路线

#### 4.3.1 概念说明

「扩展点」是项目作者预留的、不需要大改架构就能接入自定义能力的位置。学完前 26 讲，你已经见过 WebLLM 的全部主要扩展点；本节把它们收拢成一张清单，并以「新增一个引擎 API」为例做源码级预习（完整实操在第 5 节）。

理解扩展点的钥匙是 u1-l3 讲过的**运行时分层**：

```text
页面代码 → [协议门面层] → [引擎层 MLCEngine] → [管线层 LLMChatPipeline] → tvmjs/WebGPU
                ↕ Worker 代理层（WebWorkerMLCEngine ⇄ Handler ⇄ 真身 MLCEngine）
```

给引擎加一个方法之所以要「三线打通」，正是因为 **Worker 代理层与真身实现同一接口**：主线程的代理对象根本没有模型状态，它只能把调用编码成消息发给 worker（u5-l1）。所以一个新引擎 API = 接口契约 + 真身实现 + 消息协议 + Handler 路由 + 代理转发，缺一条线，主线程/Worker 两种模式的行为就不一致。

#### 4.3.2 核心流程

本手册覆盖的七类扩展点：

| # | 扩展点 | 动哪里 | 详见讲义 |
| --- | --- | --- | --- |
| 1 | 新引擎 API | `types.ts` 接口 + `engine.ts` 实现 + `message.ts` 协议 + `web_worker.ts` 路由与代理（+ 必要时 `llm_chat.ts` 暴露数据） | 本讲第 5 节 |
| 2 | 采样行为定制 | 实现 `LogitProcessor` 三件套，`setLogitProcessorRegistry` 按 modelId 注册（Worker 场景须在 worker 脚本内注册） | u3-l5 |
| 3 | 新模型接入 | `config.ts` 的 `AppConfig.model_list` 自定义记录；或给 `prebuiltAppConfig` 提 PR | u4-l4 |
| 4 | Worker 自定义消息 | 子类重写 `WebWorkerMLCEngineHandler.onmessage` 拦截 `customRequest`（基类空实现） | u5-l2 |
| 5 | 结构化输出 | 请求级 `response_format`（json_object 带 schema / structural_tag） | u6-l3、u6-l4 |
| 6 | 缓存后端 | `AppConfig.cacheBackend` 四选一，`cache_util.ts` 收口 | u4-l1 |
| 7 | 新示例/文档 | `examples/` 新目录 + README（本身就是官方欢迎的贡献类型） | u1-l2 |

其中扩展点 1 的数据流（以本讲要加的 `getContextTokensLeft` 为例）：

```text
页面调用 engine.getContextTokensLeft(modelId?)
   ├─ 主线程模式：MLCEngine.getContextTokensLeft
   │      → getLLMStates 选出管线 → pipeline.getContextTokensLeft()
   └─ Worker 模式：WebWorkerMLCEngine.getContextTokensLeft
          → postMessage{kind:"getContextTokensLeft", uuid, content:{modelId}}
          → Handler.onmessage 按.kind 路由 → this.engine.getContextTokensLeft(...)
          → return 消息（带同一 uuid）→ 代理的 Promise resolve
```

#### 4.3.3 源码精读

**(a) 接口契约**。`MLCEngineInterface` 定义在 [src/types.ts:L140-L243](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L140-L243)。新方法应该加在这个接口里，因为它是 `MLCEngine` 与 `WebWorkerMLCEngine`（以及 Service Worker 变体）共同实现的「静态契约」——加在这里，TypeScript 会立刻提醒你两处实现都必须补齐。仿照对象是 [L208-L218](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L208-L218) 的两个查询类方法：

```ts
getMaxStorageBufferBindingSize(): Promise<number>;
getGPUVendor(): Promise<string>;
```

它们的特点：无状态查询、可选 `modelId` 参数（多模型时定位用，见 [runtimeStatsText 的声明](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L175-L180)）——`getContextTokensLeft` 与此完全同构。

**(b) 引擎实现样板**。[src/engine.ts:L1315-L1323](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1315-L1323) 的 `runtimeStatsText` 是最佳模板：

```ts
async runtimeStatsText(modelId?: string): Promise<string> {
  // （省略：弃用警告日志）
  const [, selectedPipeline] = this.getLLMStates("runtimeStatsText", modelId);
  return selectedPipeline.runtimeStatsText();
}
```

两行核心：`getLLMStates`（[engine.ts:L1199-L1208](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1199-L1208)）完成「多模型三分支路由」（无模型抛错、单模型唯一、多模型歧义抛 `UnclearModelToUseError`，u7-l2），然后把工作委托给选中的管线。GPU 查询类方法则不需要管线，直接 `tvmjs.detectGPUDevice()`（[engine.ts:L1156-L1192](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1156-L1192)）——对比这两种样板，你就能判断新方法属于哪一类。

**(c) 数据来源在管线层，且是私有的**。剩余 token 数的定义：

\[ \text{tokensLeft} = \text{contextWindowSize} - \text{filledKVCacheLength} \]

两个字段都是 `LLMChatPipeline` 的私有成员（[src/llm_chat.ts:L97-L104](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L97-L104)：`private filledKVCacheLength = 0;`、`private contextWindowSize = -1;`）。引擎层无法直接读它们，所以严格说要动第 4 个文件：给管线加一个公开 getter。这个语义与项目自身的窗口检查完全一致——[llm_chat.ts:L2124-L2133](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2124-L2133) 判定输入超窗用的正是 `numPromptTokens + this.filledKVCacheLength > this.contextWindowSize`；且注意 L2126 的前提 `slidingWindowSize == -1`——滑动窗口模型用环形 KV 淘汰旧页（u3-l4），「剩余窗口」没有意义，新方法对这类模型应返回 `-1` 表示「无固定上限」。

**(d) Worker 消息协议**。三处改动都在 [src/message.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L15-L141)：

1. [RequestKind 联合类型（L18-L37）](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L18-L37) 加一个字符串字面量 `"getContextTokensLeft"`（现存 19 种）。
2. 仿照 [RuntimeStatsTextParams（L53-L55）](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L53-L55) 新增 `getContextTokensLeftParams { modelId?: string }`，并加进 [MessageContent 联合类型（L108-L131）](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L108-L131)——注意该联合里已有 `number` 成员，返回值无需新类型。
3. 消息信封 [WorkerRequest（L137-L141）](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L137-L141) 结构不变（`kind/uuid/content`）。

**(e) Handler 路由与代理转发**。worker 侧在 `WebWorkerMLCEngineHandler.onmessage` 加一个 case，样板是 [web_worker.ts:L306-L313](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L306-L313)（`getMaxStorageBufferBindingSize` 分支）：`handleTask` 包装异步调用，成功自动回 `return` 消息、异常退化为字符串回 `throw`（u5-l2）。主线程侧在 `WebWorkerMLCEngine` 加代理方法，样板是 [web_worker.ts:L547-L554](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L547-L554)：构造消息、`crypto.randomUUID()` 生成配对键、`getPromise` 等待回包。顺带一提：`ServiceWorkerMLCEngineHandler` 继承自 `WebWorkerMLCEngineHandler` 并原样复用路由（u5-l3），所以只要不动 reload/keepAlive 等特例，Service Worker 变体**不需要额外改动**。

**(f) 单测样板**。[tests/web_worker_handler.test.ts:L19-L36](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L19-L36) 用 `jest.mock("../src/engine")` 把引擎换成 mock 对象，再直接 `new WebWorkerMLCEngineHandler()` 并伪造 `postMessage`（[L47](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L47)），向 handler 喂一条 `WorkerRequest` 即可断言「引擎方法被调用 + 结果被 postMessage 回去」——这正是 u7-l4 说的模块级 mock 模式，也最适合测新加的 case 分支。

#### 4.3.4 代码实践：定位五处改动点（阅读热身）

1. **实践目标**：不写代码，先在真实源码里把综合实践要动的五个位置全部找出来，抄下准确行号。
2. **操作步骤**：按下表逐项用编辑器跳转并填写：

   | 改动 | 文件 | 锚点（本讲的参照物） | 你插入的位置（行号） |
   | --- | --- | --- | --- |
   | ① 接口声明 | src/types.ts | L218 `getGPUVendor()` 之后 | 待填 |
   | ② 管线 getter | src/llm_chat.ts | L97/L101 私有字段；参照 L636 `runtimeStatsText()` | 待填 |
   | ③ 引擎实现 | src/engine.ts | L1315 `runtimeStatsText` | 待填 |
   | ④ 消息协议 | src/message.ts | L18-L37 RequestKind；L53-L55 Params | 待填 |
   | ⑤ Handler case + 代理 | src/web_worker.ts | L306-L313 case；L547-L554 代理 | 待填 |

3. **需要观察的现象**：每一处锚点代码都能直接对照本节讲解；`getLLMStates` 的调用签名在 ②③ 之间如何衔接。
4. **预期结果**：五处位置都有明确行号与插入策略，综合实践即可「照表施工」。
5. 本实践为纯阅读，可直接完成，无需本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `getContextTokensLeft` 必须改 `message.ts`，而 `getMaxStorageBufferBindingSize` 也同样要经过 Worker 协议？
**答案**：主线程的 `WebWorkerMLCEngine` 是纯代理，没有任何本地状态；一切方法调用都要序列化成 `WorkerRequest` 跨线程传递。所以**每个**引擎方法（包括最简单的 GPU 查询）都对应一种 `RequestKind`——加新方法必然扩协议，这是代理模式的固定成本。

**练习 2**：如果只在 `MLCEngine`（engine.ts）里加了方法、没改 `WebWorkerMLCEngine`，会发生什么？
**答案**：TypeScript 编译期就会报错——两者都声明 `implements MLCEngineInterface`，接口新增成员后代理类缺实现即不满足契约。这正是把方法写进接口（而不是只写在具体类）的价值：编译器替你检查两种模式的行为一致性。（若用 `as any` 绕过，则运行时主线程模式可用、Worker 模式调用报 `UnknownMessageKindError`。）

**练习 3**：`getContextTokensLeft` 对滑动窗口模型应该返回什么？依据是哪一行代码？
**答案**：返回 `-1`（表示无固定上限）。依据 [llm_chat.ts:L2126](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2124-L2133)：窗口超限检查的前提是 `slidingWindowSize == -1`，注释明确说明滑动窗口模型对 contextWindowSize 没有限制（环形 KV 自动淘汰旧页，u3-l4）。

---

## 5. 综合实践：端到端二开 `getContextTokensLeft`

这是全手册的收官实践：走完一次完整的贡献闭环——fork、编码、单测、门禁、构建、示例验证、PR。以下代码为**示例代码**（本仓库当前 HEAD 并无此方法），但每一处插入位置与参照样板都是真实源码。

**任务定义**：给引擎新增 `getContextTokensLeft(modelId?: string): Promise<number>`，返回当前会话还能再容纳多少输入 token；滑动窗口模型返回 `-1`。

### 步骤 1：准备工作区

```bash
# 在 GitHub 上 fork mlc-ai/web-llm，然后：
git clone https://github.com/<你的用户名>/web-llm.git
cd web-llm
git checkout -b feature/get-context-tokens-left
npm install          # 触发 prepare 钩子启用 husky
npm run lint && npm test   # 确认基线是绿的
```

### 步骤 2：五处代码改动（示例代码）

**(a) `src/llm_chat.ts`** —— 在 `runtimeStatsText()`（约 L636）附近加管线 getter：

```ts
// 示例代码：返回剩余可容纳的 token 数；滑动窗口模型无固定上限，返回 -1
getContextTokensLeft(): number {
  if (this.slidingWindowSize !== -1) {
    return -1;
  }
  return this.contextWindowSize - this.filledKVCacheLength;
}
```

**(b) `src/types.ts`** —— 在 `getGPUVendor()`（L218）后追加接口声明（含 JSDoc，风格对齐相邻方法）：

```ts
/**
 * Returns the number of tokens that can still fit into the context window
 * of the current chat session. Returns -1 for sliding-window models, which
 * have no fixed context limit.
 * @param modelId Only required when multiple models are loaded.
 */
getContextTokensLeft(modelId?: string): Promise<number>;
```

**(c) `src/engine.ts`** —— 仿照 `runtimeStatsText`（L1315-L1323）：

```ts
async getContextTokensLeft(modelId?: string): Promise<number> {
  const [, selectedPipeline] = this.getLLMStates("getContextTokensLeft", modelId);
  return selectedPipeline.getContextTokensLeft();
}
```

**(d) `src/message.ts`** —— 三小步：`RequestKind` 加 `| "getContextTokensLeft"`；新增并挂入 `MessageContent`：

```ts
export interface getContextTokensLeftParams {
  modelId?: string;
}
```

**(e) `src/web_worker.ts`** —— Handler 加 case（样板 L306-L313）、代理类加方法（样板 L547-L554）：

```ts
// Handler 内（示例代码）
case "getContextTokensLeft": {
  this.handleTask(msg.uuid, async () => {
    const params = msg.content as getContextTokensLeftParams;
    const res = await this.engine.getContextTokensLeft(params.modelId);
    onComplete?.(res);
    return res;
  });
  return;
}

// WebWorkerMLCEngine 内（示例代码）
async getContextTokensLeft(modelId?: string): Promise<number> {
  const msg: WorkerRequest = {
    kind: "getContextTokensLeft",
    uuid: crypto.randomUUID(),
    content: { modelId: modelId },
  };
  return await this.getPromise<number>(msg);
}
```

### 步骤 3：补一个单测

在 `tests/web_worker_handler.test.ts` 里仿照既有用例：给 `mockEngineInstance` 加 `getContextTokensLeft: getContextTokensLeftMock`（返回如 `512`），构造 handler 后喂一条 `kind: "getContextTokensLeft"` 的 `WorkerRequest`，用 `flushMicrotasks()`（[L50-L52](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/web_worker_handler.test.ts#L50-L52)）等微任务排空，断言两件事：引擎 mock 被调用过一次、`postMessage` 收到 `kind: "return"` 且 `content === 512`。

### 步骤 4：过门禁、构建、验证

```bash
npm run lint          # eslint + prettier 检查
npm test              # jest --coverage，覆盖率阈值必须仍达标
npm run build         # 产出 lib/
npm pack --dry-run    # 核对发布清单只含 lib/

# 本地示例联调（CONTRIBUTING.md 的标准姿势）：
# 改 examples/get-started/package.json 中 "@mlc-ai/web-llm" 为 "../.."
cd examples/get-started && npm install && npm run start
```

在示例页面里加载一个小模型（如 Llama-3.2-1B），第一轮对话前打印 `await engine.getContextTokensLeft()`，应约等于模型的 context_window_size（如 4096，减去系统模板占用的 token）；每多聊一轮，该值按本轮 prompt+回复的 token 数递减——这与 u2-l2 讲的「多轮 KV cache 复用、只计增量」互相印证。若你的示例用的是 Worker 模式，则验证的是消息协议那条线。

### 步骤 5：提交 PR

按 [CONTRIBUTING.md:L107-L125](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/CONTRIBUTING.md#L107-L125) 的清单写 PR 描述：问题（无法查询剩余上下文）、方案（五处改动 + 协议扩展）、验证步骤（单测 + 示例截图/日志）、兼容性（纯新增方法，无破坏性变更；Worker 协议新增 kind，旧主线程配新 worker 或反之不涉及该调用即不受影响）。

> 说明：本讲义环境未执行以上任何命令，构建/测试/运行结果均「待本地验证」；首次 `npm test` 需要下载 jest 依赖，模型加载验证需要支持 WebGPU 的浏览器。

## 6. 本讲小结

- `npm run build` 是两步：rollup 以 `src/index.ts` 单入口打出 ESM 单文件 `lib/index.js`（运行时依赖仅 `loglevel`，tvmjs/tokenizers/xgrammar 全部内联），再用 `cleanup-index-js.sh` 的四组 sed 替换抹掉内联依赖残留的 Node 专有代码（`createRequire`、`require('url')`、`perf_hooks`、`ws`），否则 Next.js/SvelteKit/Parcel/浏览器环境会崩。
- npm 包只发布 `lib/` 目录（`files: ["lib"]`）；版本号通过独立 PR 手动 bump，CI 用 `npm pack --dry-run` 校验发布清单。
- 质量门禁有三层：pre-commit 的 Husky + lint-staged 自动修复格式；CI 的 Linter/Tests/Build 三 workflow；jest 覆盖率硬阈值（全局 25/20/20/25，`engine.ts` 提高到 35/25/40/35）。快捷迭代用 `npx jest --coverage=false tests/<file>.test.ts`。
- 本地包联调示例的官方姿势：把示例 `package.json` 里的 `"@mlc-ai/web-llm"` 改成 `"../.."`。
- 给引擎加新 API 是「五处打通」：`types.ts` 契约 → `llm_chat.ts` 数据 getter → `engine.ts` 实现（经 `getLLMStates` 路由）→ `message.ts` 协议（RequestKind + Params + MessageContent）→ `web_worker.ts`（Handler case + 代理方法）；Service Worker 变体靠继承自动获得。
- WebLLM 的七类扩展点（新 API、LogitProcessor、新模型、Worker 自定义消息、结构化输出、缓存后端、新示例）各自对应前文的专门讲义，二开时按表索引即可。

## 7. 下一步学习建议

到这里，整本手册（u1 到 u7 共 27 讲）已经完结。接下来建议按兴趣选方向深入：

1. **动手型：把综合实践做成真 PR**。`getContextTokensLeft` 是上游欢迎的实用小功能（多轮对话 UI 需要「剩余上下文」提示）；先按 CONTRIBUTING 的建议在 issue 里提方案再实现。
2. **应用型：做一个小项目串起全手册**。例如「本地知识库聊天」：u4-l4 接一个自定义小模型 + u2-l5 embedding 做检索 + u6-l3 json_schema 约束输出 + u5-l1 Worker 架构保 UI 流畅。
3. **引擎型：深入 tvmjs 与 MLC LLM**。WebLLM 之下的两层：`@mlc-ai/web-runtime`（PackedFunc/WebGPU 运行时）与 MLC LLM 的模型编译流程（u3-l1、u4-l4 已铺路），可阅读 [README.md](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md) 中 Bring Your Own Model 一节并跟进 mlc.ai 上游文档。
4. **跟踪型：关注 `prebuiltAppConfig` 的演进**。每次版本 bump 都会调整 `model_list`（模型兼容性的唯一事实来源，u1-l4），用 `git log -- src/config.ts` 观察新模型家族如何接入，是理解项目演进节奏的最佳窗口。
