# 构建、测试与运行 demo

## 1. 本讲目标

本讲解决一个问题：**这个库从源码到能在浏览器里看到效果，中间要经过哪些工具、跑哪些命令？**

学完后你应该能够：

- 说清 `npm run dev`、`build`、`test`、`typecheck`、`lint` 这几条脚本各自做什么，并且明白**根目录的 `npm run dev` 和 demo 目录里的 `npm run dev` 是两回事**。
- 读懂 `tsup.config.ts`，知道它如何同时产出 ESM、CJS 和类型声明（`.d.ts`），以及为什么要把 CodeMirror 五个包标记为 `external`。
- 读懂 `vitest.config.ts`，理解为什么测试要跑在 jsdom 环境里。
- 读懂 `.github/workflows/ci.yml`，知道 CI 用 Node 18/20 矩阵跑了一条「lint → typecheck → test → build」的检查链。
- 在本地把测试跑起来，并启动 demo 看到 5 个真实编辑器。

## 2. 前置知识

在进入本讲前，你应该已经读过：

- **u1-l1 项目定位与功能总览**：知道这是一个「模块化插件集合」，奉行零强制依赖、按需引入；CodeMirror 五个包是对等依赖（peerDependencies），`katex`/`lowlight` 是可选依赖。
- **u1-l2 源码目录结构与库入口**：知道 `src/` 分 `core/plugins/widgets/utils/theme` 五层，`src/index.ts` 是统一导出的桶文件，`tsconfig.json` 里 `rootDir: src` / `outDir: dist` 划定了源码到产物的边界。

本讲会从「产物边界」继续往下走：`src/` 里的 TypeScript 源码，是怎么变成 `dist/` 里的发布包的？又怎么在 demo 里跑起来给人看？

几个名词先说清楚：

- **构建（build）**：把人写的 TypeScript 源码编译/打包成浏览器或 Node 能直接加载的产物（`.js` + 类型声明）。
- **打包器（bundler）**：本项目用 `tsup`（底层是 esbuild），把散落的多个 `.ts` 文件合并成少数几个产物文件。
- **模块格式**：`ESM`（`import/export`，浏览器与现代 Node 用）和 `CJS`（`require`，老 Node 用）。一个库要两种都提供，才能兼容不同使用者。
- **测试运行器（test runner）**：本项目用 `Vitest`，专门为 Vite 生态设计、启动快、原生支持 ESM。
- **jsdom**：一个用纯 JavaScript 实现的「假 DOM」。CodeMirror 的很多逻辑（如 `ViewPlugin`、`WidgetType.toDOM`）依赖 `document`、`DOM` 等 API，Node 默认没有这些 API，所以测试需要 jsdom 来模拟一个浏览器环境。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `package.json` | 定义 npm 脚本（dev/build/test/typecheck/lint）、发布产物（exports/files）、依赖分层（peerDependencies）。 |
| `tsup.config.ts` | tsup 构建配置：入口、模块格式、是否生成 `.d.ts`、哪些依赖不打包（external）。 |
| `vitest.config.ts` | Vitest 测试配置：是否注入全局 API、用哪个环境（jsdom）、排除哪些目录。 |
| `.github/workflows/ci.yml` | GitHub Actions CI：Node 18/20 矩阵跑 lint/typecheck/test/build，并单独跑覆盖率上报。 |
| `demo/package.json` | demo 的脚本（`dev` = `vite`）与依赖。 |
| `demo/vite.config.ts` | demo 的 Vite 配置：**关键**——用别名把 `codemirror-live-markdown` 指向源码 `../src/index.ts`。 |
| `demo/index.html` | demo 页面，包含 5 个编辑器容器。 |

> 注意：本讲引用的行号基于当前 HEAD `f1438ab`。链接均为永久链接，点击可直接跳转到对应代码。

## 4. 核心概念与源码讲解

### 4.1 npm scripts 全景与发布产物

#### 4.1.1 概念说明

`package.json` 的 `scripts` 字段是项目的「控制台」。每一条 `key: value` 都可以用 `npm run <key>` 触发，`value` 就是一条 shell 命令。理解一个项目，先看它的 `scripts` 就能知道这个项目「能干什么、怎么干」。

和 scripts 紧密相关的是「发布产物配置」——即使用者 `npm install` 这个包之后，到底拿到哪些文件、以哪种模块格式加载。这由 `exports`、`files`、`peerDependencies` 三个字段共同决定。本模块先把它们一起讲清楚，因为后续的构建、测试、运行 demo 都建立在「产出什么产物」之上。

#### 4.1.2 核心流程

先看脚本全景：

| 脚本 | 命令 | 作用 |
|------|------|------|
| `prepare` | `tsup` | 特殊脚本：本地 `npm install` 或发布前**自动**执行一次构建，保证 `dist/` 存在。 |
| `dev` | `tsup --watch` | **库的**监视构建：源码一改就重新打包到 `dist/`。 |
| `build` | `tsup` | 一次性构建产出 `dist/`。 |
| `test` | `vitest run` | 跑一遍测试然后退出（CI 友好）。 |
| `test:watch` | `vitest` | 监视模式跑测试（开发时常用）。 |
| `test:coverage` | `vitest run --coverage` | 跑测试并生成覆盖率报告。 |
| `lint` | `eslint src --ext .ts` | 用 ESLint 检查 `src/` 下的代码风格。 |
| `format` | `prettier --write "src/**/*.ts"` | 用 Prettier 格式化源码。 |
| `typecheck` | `tsc --noEmit` | 只做类型检查、不产出文件。 |

⚠️ **本讲最大的陷阱**：根目录的 `npm run dev` 是 `tsup --watch`（重新打包库），**不是**启动网页 demo。启动网页 demo 要进 `demo/` 目录跑 `npm run dev`（那里是 `vite`）。这两条命令同名但含义完全不同，初学者极易混淆。

再看发布产物是怎么定义的：

1. `files: ["dist", "README.md", "LICENSE"]` 限定发布到 npm 时只带上 `dist/` 目录和两个文档，不会把源码、测试一起发出去。
2. `main` / `module` / `types` 三个「老式」入口字段分别指向 CJS、ESM、类型声明。
3. `exports`（更现代的「条件导出」）告诉打包器：遇到 `import` 用 ESM 产物、遇到 `require` 用 CJS 产物、需要类型时用 `.d.ts`。
4. `peerDependencies` 把 CodeMirror 五个包列为对等依赖——**使用者自己装**，库不会重复打包它们（详见 4.2 的 external）。

#### 4.1.3 源码精读

scripts 表：

[package.json:21-31](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L21-L31) — 九条脚本定义在这里，本表即据此整理。

发布产物的条件导出（`exports`）：

[package.json:9-15](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L9-L15) — `.` 的导出按 `types` → `import` → `require` 三个条件给出不同产物，体现了「一份源码、多格式产物」的设计。

发布的文件范围：

[package.json:16-20](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L16-L20) — 只发 `dist/`、README、LICENSE，保证包体积干净。

对等依赖：

[package.json:50-56](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L50-L56) — CodeMirror 五个包是 `peerDependencies`，由宿主提供，这是 4.2 里 external 的依据。

#### 4.1.4 代码实践

1. **目标**：在不打开文件的情况下，让命令行告诉你有哪些可用脚本。
2. **操作步骤**：在项目根目录执行 `npm run`（不带任何脚本名）。
3. **观察现象**：终端会列出所有可用脚本名及其命令。
4. **预期结果**：能看到 `dev`、`build`、`test`、`test:watch`、`test:coverage`、`lint`、`format`、`typecheck`、`prepare` 等条目。
5. **待本地验证**：实际输出以你本地 `npm run` 的打印为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `test` 用 `vitest run` 而 `test:watch` 只写 `vitest`？

**参考答案**：`vitest run` 跑完一轮就退出（适合 CI 和一次性检查）；裸 `vitest` 默认进入监视模式，源码一改就重跑（适合边写边测的开发场景）。两者只是同一个工具的两种运行姿态。

**练习 2**：`prepare` 脚本你在什么情况下会「没主动跑它，但它却执行了」？

**参考答案**：`prepare` 是 npm 的生命周期脚本，会在本地执行 `npm install`（无参数）以及 `npm publish` 之前自动运行。所以你刚装完依赖，`dist/` 就可能已经被构建出来了——这正是它存在的目的。

---

### 4.2 tsup 构建配置

#### 4.2.1 概念说明

`tsup` 是一个基于 esbuild 的零配置（也可配置）打包器，特点是**快**，并且能同时产出 ESM/CJS、生成 `.d.ts` 类型声明。本项目只用了一个简短的 `tsup.config.ts` 就完成了「多格式 + 类型」的全部工作。

本模块的核心要理解三件事：
- **入口（entry）**：从哪个文件开始打包。
- **多格式产出**：同时生成 ESM 和 CJS。
- **external（外部依赖）**：哪些包**不**打进产物，留给使用者自己提供。

#### 4.2.2 核心流程

打包流程可概括为：

```
src/index.ts（入口，桶文件）
        │  tsup
        ▼
dist/index.js   （ESM）      ← 给现代 import 用
dist/index.cjs  （CJS）       ← 给老式 require 用
dist/index.d.ts （类型声明）   ← 给 TypeScript 使用者用类型用
dist/index.js.map / .cjs.map  （source map，便于调试）
```

关键决策点：

1. `entry: ['src/index.ts']` —— 只有一个入口，因为 `src/index.ts` 已经把五层所有导出汇聚好了（见 u1-l2）。
2. `format: ['esm', 'cjs']` —— 一次构建产出两种格式，对应 `package.json` 里的 `module`（ESM）和 `main`（CJS）。
3. `dts: true` —— 用 tsup 内置能力（底层 TypeScript Compiler API）生成 `.d.ts`，使用者才能得到类型提示。
4. `external: [...]` —— 把五个 CodeMirror/Lezer 包标记为外部，**不打包进 dist**。这与 `package.json` 的 `peerDependencies` 一唱一和：库声明「我需要它们但我不带它们」，使用者自己安装、保证宿主和库用的是**同一个** CodeMirror 实例（CodeMirror 要求单一实例，否则状态不互通）。
5. `minify: false` + `sourcemap: true` + `treeshake: true` —— 不压缩（便于调试）、带 source map、摇掉死代码。

#### 4.2.3 源码精读

tsup 配置全文很短，逐行看：

[tsup.config.ts:4-11](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/tsup.config.ts#L4-L11) — 入口、格式、dts、splitting、sourcemap、clean、treeshake、minify 八个核心开关。

external 列表：

[tsup.config.ts:12-18](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/tsup.config.ts#L12-L18) —— 这五个包与 [package.json:50-56](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L50-L56) 的 peerDependencies 一一对应，体现了「对等依赖 → 打包时不内联」的硬约定。

#### 4.2.4 代码实践

1. **目标**：亲手跑一次构建，看看 `dist/` 里到底生成了什么。
2. **操作步骤**：在根目录执行 `npm run build`，然后查看 `ls dist`。
3. **观察现象**：`dist/` 下应出现 `index.js`、`index.cjs`、`index.d.ts` 及对应的 `.map` 文件。
4. **预期结果**：产物体积不大（因为 CodeMirror 已 external 掉），并且打开 `index.js` 顶部能看到 ESM 的 `import` 语句仍引用 `@codemirror/state` 等（说明它们没被打包进来）。
5. **待本地验证**：实际文件清单以本地 `npm run build` 产出为准。

> 配套现象：因为 `clean: true`，每次构建前 `dist/` 会被清空，所以你不会看到过期文件残留。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `external` 里删掉 `@codemirror/state`，会发生什么不好的事？

**参考答案**：tsup 会把 `@codemirror/state` 的代码直接打进 `dist/index.js`。于是使用者（他自己也装了 `@codemirror/state`）会同时加载两份副本，CodeMirror 状态系统要求「全应用单一实例」，两份实例会导致编辑器状态/事务互不识别、行为错乱，包体积也会无谓变大。

**练习 2**：`dts: true` 生成的 `dist/index.d.ts` 是给谁用的？删掉它会怎样？

**参考答案**：给 TypeScript 使用者用的类型声明。删掉后，使用者在自己的项目里 `import { livePreviewPlugin } from 'codemirror-live-markdown'` 时拿不到类型，IDE 不会提示参数和返回类型，等于退化为「无类型的 any 库」。

---

### 4.3 Vitest + jsdom 测试环境

#### 4.3.1 概念说明

`Vitest` 是测试运行器。这个库的测试对象很特殊：大量逻辑（`ViewPlugin`、`WidgetType.toDOM`、`Decoration` 生成）依赖浏览器 DOM。Node 原生没有 `document`、`window`，直接跑会报错。`jsdom` 就是一个「在 Node 里模拟出来的浏览器 DOM」，让这些 DOM 相关代码能在命令行里被测试。

本模块要理解：测试在什么环境跑、哪些目录被排除、覆盖率怎么配置。

#### 4.3.2 核心流程

测试运行流程：

```
npm test  →  vitest run
                │
                ├─ 读取 vitest.config.ts
                ├─ environment: 'jsdom'  ← 注入 document/window
                ├─ globals: true          ← describe/it/expect 全局可用
                └─ 扫描 src/**/__tests__/*.test.ts（排除 dist/demo/ProseMark）
```

测试文件分布在五层目录的 `__tests__/` 子目录里，共 19 个测试文件（如 `src/core/__tests__/shouldShowSource.test.ts`、`src/plugins/__tests__/livePreview.test.ts`、`src/widgets/__tests__/codeBlockWidget.test.ts` 等）。这些测试会和后续每一讲的源码讲解一一对应——可以说「读测试」就是读这个库的最佳捷径之一。

#### 4.3.3 源码精读

Vitest 配置全文：

[vitest.config.ts:3-13](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/vitest.config.ts#L3-L13) —— 整个测试配置都在这 11 行里。

两个关键开关：

- [vitest.config.ts:5](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/vitest.config.ts#L5) `globals: true` —— `describe`、`it`、`expect` 不用 import 就能用，测试文件更简洁。
- [vitest.config.ts:6](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/vitest.config.ts#L6) `environment: 'jsdom'` —— 注入 DOM，注释里写明了原因：`ViewPlugin` 等需要 DOM。

排除目录：

[vitest.config.ts:7](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/vitest.config.ts#L7) —— 排除 `node_modules`、`dist`（产物）、`demo`（示例工程）、`ProseMark`（实验目录），避免把它们当成测试来跑。

覆盖率配置：

[vitest.config.ts:8-12](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/vitest.config.ts#L8-L12) —— 用 `v8` provider 采集覆盖率，输出 `text`（终端）、`json`、`html` 三种报告。

#### 4.3.4 代码实践

1. **目标**：跑通测试套件，确认本地环境可用。
2. **操作步骤**：在根目录先 `npm install` 装好依赖（注意：这会自动触发 `prepare` 即 `tsup`，顺带把 `dist/` 构建出来），再执行 `npm test`。
3. **观察现象**：终端会逐个文件打印测试结果，最后给出通过/失败数量。
4. **预期结果**：19 个测试文件全部通过（这是 CI 的验收标准之一）。
5. **待本地验证**：实际用例数与通过情况以本地运行结果为准；若 `npm install` 因网络/权限失败，请先解决依赖安装问题。

#### 4.3.5 小练习与答案

**练习 1**：为什么这个项目必须用 jsdom，而一个纯函数工具库（比如只做字符串拼接）就不需要？

**参考答案**：CodeMirror 的 `ViewPlugin`、`WidgetType.toDOM` 在执行时会调用 `document.createElement` 等浏览器 API。没有 jsdom，这些调用在 Node 里直接抛错。纯字符串工具不碰 DOM，Node 原生环境就够了，不需要 jsdom。

**练习 2**：`globals: true` 省掉了测试文件里的什么语句？它的代价是什么？

**参考答案**：省掉了 `import { describe, it, expect } from 'vitest'`，让测试文件更短。代价是这些名字变成「魔法全局变量」，IDE/TypeScript 可能需要额外配置（如 `vitest/globals` 类型）才能识别它们的类型，否则会有类型提示缺失。

---

### 4.4 CI 矩阵（Node 18/20）与覆盖率

#### 4.4.1 概念说明

CI（Continuous Integration，持续集成）是指每次推送代码或提 PR 时，服务器自动跑一遍检查，确保改动没有破坏构建或测试。本项目用 GitHub Actions 实现，配置文件在 `.github/workflows/ci.yml`。

核心机制是 **matrix strategy（矩阵策略）**：同一条检查链，在多个 Node 版本上各跑一遍，提前发现「在我机器上能跑、换个版本就挂」的问题。本项目选了 Node 18.x 和 20.x 两个版本。

#### 4.4.2 核心流程

CI 有两个 job（并行任务）：

**Job 1：`test`（矩阵 Node 18.x / 20.x）**

```
checkout 代码
   → setup-node（带 npm 缓存）
   → npm ci              ← 严格按 lockfile 安装
   → npm run lint        ← 代码风格检查
   → npm run typecheck   ← 类型检查
   → npm test            ← 跑测试
   → npm run build       ← 确认能构建出 dist
```

这条链的顺序有讲究：先 lint/typecheck（最便宜的静态检查），再 test（动态检查），最后 build（产出验证）。任何一步失败，CI 就标红。

**Job 2：`coverage`（Node 20.x）**

```
checkout 代码
   → setup-node
   → npm ci
   → npm run test:coverage        ← 跑测试并采集覆盖率
   → 上传 coverage-final.json 到 Codecov
```

这个 job 单独跑覆盖率并把结果上报到 Codecov（一个覆盖率展示服务）。

#### 4.4.3 源码精读

触发条件：

[ci.yml:3-7](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/.github/workflows/ci.yml#L3-L7) —— 在向 `main` 分支 push 或 PR 时触发。

矩阵定义：

[ci.yml:13-15](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/.github/workflows/ci.yml#L13-L15) —— `node-version: [18.x, 20.x]`，这就是「Node 18/20 矩阵」的来源，会让下面的步骤各自跑两遍。

test job 的检查链：

[ci.yml:17-39](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/.github/workflows/ci.yml#L17-L39) —— 依次执行 checkout → setup-node（缓存 npm）→ `npm ci` → `npm run lint` → `npm run typecheck` → `npm test` → `npm run build`。

注意它用的是 `npm ci`（见 [ci.yml:26-27](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/.github/workflows/ci.yml#L26-L27)），比 `npm install` 更严格——完全按 lockfile 安装、不修改它，保证 CI 环境确定可复现。

coverage job 与上报：

[ci.yml:41-63](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/.github/workflows/ci.yml#L41-L63) —— 单独跑 `npm run test:coverage`，再用 `codecov/codecov-action@v3` 把 `./coverage/coverage-final.json` 上报。

#### 4.4.4 代码实践

1. **目标**：在本地复现 CI 的检查链，做到「推代码前自己先验一遍」。
2. **操作步骤**：在根目录依次执行：
   ```bash
   npm run lint
   npm run typecheck
   npm test
   npm run build
   ```
3. **观察现象**：每条命令是否都成功退出（退出码 0）。
4. **预期结果**：四条全绿，说明你的改动基本能通过 CI。
5. **待本地验证**：若 `npm run lint` 报风格问题，可先 `npm run format` 自动修复；若测试失败，按报错定位具体用例。注意本地需先 `npm install`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CI 用 `npm ci` 而不是 `npm install`？

**参考答案**：`npm ci` 严格按 `package-lock.json` 安装，安装前会删掉 `node_modules`、安装中不会改写 lockfile，结果确定且可复现。CI 需要的就是这种确定性；而 `npm install` 可能解析出新的依赖版本、改写 lockfile，会引入「昨天绿今天红」的不稳定。

**练习 2**：matrix 设成 `[18.x, 20.x]` 的实际价值是什么？去掉一个版本会损失什么？

**参考答案**：能在两个长期支持版本上各验一遍，捕捉到「只在某版本才暴露」的问题（如某 API 在 18 里有、在 20 里行为变了）。去掉一个版本就丢失了那个版本的覆盖，使用者一旦用被去掉的版本遇到问题，CI 不会提前预警。

---

## 5. 综合实践

本实践把「构建 → 测试 → 运行 demo」串成一条完整链，并重点验证那个最容易踩的坑：**根目录的 `npm run dev` ≠ demo 的 `npm run dev`**。

### 实践目标

亲手把库跑起来，并在浏览器里看到 5 个真实编辑器，理解 demo 是如何「直接吃源码」的。

### 操作步骤

**第一步：在根目录安装依赖并跑测试**

```bash
npm install      # 会自动触发 prepare(tsup)，顺带构建出 dist/
npm test         # 跑 19 个测试文件
```

预期：测试全部通过。**待本地验证。**

**第二步：理解 demo 的「别名吃源码」机制（关键）**

demo 用 Vite，配置里有一条别名把包名直接指向源码：

[demo/vite.config.ts:12-21](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/vite.config.ts#L12-L21) —— 其中 [demo/vite.config.ts:14](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/vite.config.ts#L14) 把 `codemirror-live-markdown` 指向 `../src/index.ts`。

注释 [demo/vite.config.ts:13](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/vite.config.ts#L13) 写明原因：「直接指向源码，避免开发时用到过期的 dist 构建」。意思是：你改 `src/` 里的源码，demo 立刻就能反映，无需先 `npm run build`。

注意 demo 还把五个 `@codemirror/*` 包别名到 demo 自己的 `node_modules`（[demo/vite.config.ts:15-20](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/vite.config.ts#L15-L20)），目的是确保 demo 用到的是**它自己安装的那一份** CodeMirror，避免和根目录的实例混淆。

**第三步：启动 demo（注意是 demo 目录里的 dev！）**

```bash
cd demo
npm install     # 安装 demo 自己的依赖（含 vite、@codemirror/*）
npm run dev     # 这里是 vite，不是 tsup！
```

按 [demo/vite.config.ts:8-10](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/vite.config.ts#L8-L10)，Vite 监听 **5173** 端口。打开浏览器访问 `http://localhost:5173`。

### 需要观察的现象

页面加载后应看到 **5 个编辑器**，对应 [demo/index.html:140-182](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html#L140-L182) 的五个 `demo-section`：

1. **Basic Table**（默认表格插件，带 Live Preview / Source Mode 按钮）
2. **Advanced Table**（可编辑单元格）
3. **Code Block Auto**（光标进入显示源码）
4. **Code Block Inline Editing**（围栏隐藏、代码可原地编辑）
5. **Code Block Toggle**（MD/Code 按钮切换源码态）

它们由 [demo/index.html:190](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html#L190) 的 `<script type="module" src="/main.ts">` 引导，而 `demo/main.ts` 顶部 [demo/main.ts:8-24](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L8-L24) 从 `codemirror-live-markdown` 导入全部插件（经别名解析到源码）。

> 补充：[demo/index.html:8-12](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/index.html#L8-L12) 通过 CDN 加载了 KaTeX 的 CSS 和 JS，所以数学公式能渲染——这正印证了 u1-l1 讲的「katex 是可选依赖，demo 用 CDN 方式提供」。

### 预期结果

- 光标移入粗体、表格、代码块等格式化文本时，原始 Markdown 标记淡入可编辑；移出后只显示渲染结果。
- 5 个编辑器都能正常渲染，无控制台报错（除了可能因网络拉不到 KaTeX CDN 时的警告）。

### 待本地验证

- `npm install` 在 demo 目录是否能正常解析 `codemirror-live-markdown`（demo/package.json 里写的是 `^0.1.0-alpha.1`，但 Vite 别名会接管，开发态实际用的是源码）。若安装阶段因该包未发布而报错，可关注 demo 是否已带可用的 `package-lock.json`（本仓库 demo 目录下确有该文件）。
- 浏览器实际渲染效果以本地为准。

### 关键陷阱总结

| 命令 | 在哪里执行 | 实际跑什么 |
|------|-----------|-----------|
| `npm run dev` | **根目录** | `tsup --watch`（重新打包库到 dist/） |
| `npm run dev` | **demo 目录** | `vite`（启动网页 demo，端口 5173） |
| `npm run build` | 根目录 | `tsup`（一次性构建库产物） |
| `npm run build` | demo 目录 | `vite build`（构建 demo 静态站点） |

## 6. 本讲小结

- `package.json` 的 `scripts` 是项目控制台：`build`/`dev` 用 tsup 打包，`test` 用 Vitest，`typecheck` 用 tsc，`lint` 用 eslint；`prepare` 会在安装/发布时自动构建。
- **根目录 `npm run dev`（tsup --watch）和 demo 的 `npm run dev`（vite）是两回事**——这是本讲最重要的易错点。
- tsup 一次性产出 ESM + CJS + `.d.ts` 三种产物，并把五个 CodeMirror 包 external 掉，与 peerDependencies 对应，保证宿主单一实例。
- 测试跑在 jsdom 环境里，因为 CodeMirror 的 `ViewPlugin`/Widget 依赖 DOM；测试文件按五层 `__tests__/` 分布，共 19 个。
- CI 用 Node 18.x/20.x 矩阵跑 `lint → typecheck → test → build` 检查链，并单独跑覆盖率上报 Codecov，用 `npm ci` 保证可复现。
- demo 通过 Vite 别名**直接吃 `src/index.ts` 源码**，改源码即时生效；页面有 5 个编辑器展示不同特性。

## 7. 下一步学习建议

现在你已经能让项目跑起来，下一步建议：

- **u1-l4 快速集成：搭建第一个 Live Preview 编辑器**：精读 `demo/main.ts`，看它如何组合扩展、接线 `mouseSelecting`，然后仿写一个最小编辑器。这是把「跑 demo」升级为「自己集成」的关键一步。
- 进入第二单元前，可以随手挑一个测试文件（如 `src/core/__tests__/shouldShowSource.test.ts`）读一读——它既是行为的权威说明，也是后续 u2-l2 的素材。
- 如果你对构建产物好奇，构建后用编辑器打开 `dist/index.js` 顶部，验证 CodeMirror 包确实没被打包进来（仍是 `import` 语句），这会加深你对 external 的理解。
