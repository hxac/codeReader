# 构建、运行与目录结构

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 cc-switch 运行起来所需的工具链（Node.js、pnpm、Rust、Tauri CLI）及其最低版本。
- 独立完成「安装依赖 → 启动开发模式 → 类型检查 → 跑单元测试 → 构建」的完整前端工作流。
- 知道 Rust 后端如何用 `cargo fmt / clippy / test` 进行格式化、检查和测试，包括 `test-hooks` 特性的作用。
- 在不打开任何文件的前提下，凭记忆说出 `src/`（前端）和 `src-tauri/src/`（后端）各自的目录划分，并能解释每个一级目录的职责。
- 画出 `src/components/` 与 `src-tauri/src/commands/` 的二级目录树，理解「按领域（domain）组织」这一核心约定。

本讲是「动手」的第一讲：前两讲（u1-l1 项目定位、u1-l2 技术栈与分层架构）帮你建立了认知地图，本讲带你真正把项目跑起来、并熟悉代码放在哪里。后续每一讲都会频繁引用本讲建立的目录约定。

## 2. 前置知识

在阅读本讲前，建议你已经：

- 读过 u1-l1，知道 cc-switch 是一个管理七种 AI CLI 工具配置的 Tauri 2 桌面应用，并理解 Provider、Live 文件、SSOT 等术语。
- 读过 u1-l2，了解「前端 Components → Hooks → Query → API，后端 Commands → Services → DAO → Database」的分层架构，以及 Tauri IPC（`invoke`/`listen`）作为前后端桥梁的概念。

下面用通俗语言补充几个本讲会用到的基础概念：

- **Tauri 2 是什么**：一个用 Rust 写后端、用任意 Web 技术（这里是 React）写前端的桌面应用框架。它不像 Electron 那样打包一整个 Chromium，而是调用系统自带的 WebView，所以体积小、内存低。开发时，你用 `pnpm dev` 同时启动「Vite 前端热重载」和「Rust 后端编译」，前端通过 Tauri 提供的 `invoke` 函数调用后端命令。
- **pnpm 是什么**：一个比 npm/yarn 更快、更省磁盘空间的 Node.js 包管理器。本项目的脚本（`dev`、`build`、`test:unit` 等）都用 pnpm 定义。
- **crate 与 Cargo.toml**：Rust 的「包」叫 crate，`Cargo.toml` 就是 Rust 版的 `package.json`，记录依赖、版本和构建配置；`cargo` 是对应的构建/包管理命令行工具。
- **「按领域组织」**：与其按技术角色（all-controllers、all-models）分目录，cc-switch 更倾向于按业务领域分目录——比如 `commands/provider.rs`、`commands/mcp.rs`、`commands/skill.rs` 各管一个功能域。理解这一点，找代码会快很多。

## 3. 本讲源码地图

本讲涉及的文件主要不是「读逻辑」，而是「读配置与组织方式」：

| 文件 | 作用 |
| --- | --- |
| [package.json](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json) | 前端工程清单：定义脚本命令（`dev`/`build`/`test:unit` 等）、前端依赖、Node 侧开发依赖。 |
| [src-tauri/Cargo.toml](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml) | 后端工程清单：Rust 版本、crate 类型、Rust 依赖、`test-hooks` 特性、release 优化配置。 |
| [tsconfig.json](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/tsconfig.json) | TypeScript 编译配置：路径别名 `@/*`、严格模式、测试全局类型。 |
| [README.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md) | 官方说明：其中「Development Guide」「Project Structure」两节是本讲的主要事实来源。 |
| [src-tauri/src/commands/mod.rs](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/commands/mod.rs) | 后端命令层入口：用 `mod`/`pub use` 把按领域拆分的命令文件统一导出，是理解后端目录组织的钥匙。 |

> 说明：本讲引用的「源码」多为配置与组织声明，行号会随提交变化；本讲给出的行号基于当前 HEAD `55abd182`，若与本地不一致，以你本地文件为准。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开，建议按「先备好环境 → 学会命令 → 看懂前端目录 → 看懂后端目录」的顺序阅读。

### 4.1 环境要求与工具链

#### 4.1.1 概念说明

在动手前，先搞清楚「跑这个项目需要装什么」。cc-switch 是一个 Tauri 2 应用，它的工具链由两部分组成：

- **前端工具链**：Node.js 运行时 + pnpm 包管理器。前端用 React + TypeScript + Vite 写，开发时靠 Vite 做热重载。
- **后端工具链**：Rust 编译器（`rustc`/`cargo`）+ Tauri CLI。后端是 Rust，编译产物会被打包进最终的桌面应用。

此外，由于 Tauri 依赖系统 WebView，不同操作系统还需要各自的原生依赖（例如 Linux 需要 webkit2gtk 系统库）。这些系统依赖通常由 Tauri 官方文档的「Prerequisites」一节描述，本讲聚焦项目自身声明的版本要求。

#### 4.1.2 核心流程

环境准备的整体流程：

1. 安装 Node.js（≥18），并启用 pnpm（≥8）。
2. 安装 Rust 工具链（≥1.85），通常通过 `rustup`。
3. 安装 Tauri CLI 2.8（本项目通过 `@tauri-apps/cli` 这个 devDependency 提供，`pnpm install` 后即可用 `pnpm tauri`）。
4. 按你的操作系统安装 Tauri 所需的原生依赖（macOS 需要 Xcode Command Line Tools；Linux 需要 webkit2gtk 等；Windows 需要 MSVC 工具链与 WebView2）。
5. 在项目根目录运行 `pnpm install` 拉取前端依赖。

#### 4.1.3 源码精读

项目版本要求明确写在 README 的「Environment Requirements」一节：

[README.md:387-392](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L387-L392) —— 这六行列出 Node.js 18+、pnpm 8+、Rust 1.85+、Tauri CLI 2.8+ 四项硬性要求。

Rust 的最低版本同时也在后端清单里被「锁死」，这样 `cargo` 在版本不够时会直接报错而不是产生奇怪的编译失败：

[Cargo.toml:1-9](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L1-L9) —— 注意第 9 行 `rust-version = "1.85.0"`，它就是 Rust 最低版本的权威来源；如果 README 与 Cargo.toml 不一致，**以 Cargo.toml 为准**，因为它是编译器实际校验的依据。

前端侧，Tauri CLI 以 devDependency 的形式随项目安装，无需你手动全局安装：

[package.json:21-23](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L21-L23) —— `@tauri-apps/cli: ^2.8.0` 出现在 `devDependencies` 中，`pnpm install` 后即可通过 `pnpm tauri` 调用，对应 README 里「Tauri CLI 2.8+」的要求。

#### 4.1.4 代码实践

**实践目标**：核对本地环境是否满足要求。

操作步骤：

1. 运行 `node -v`，确认版本 ≥ 18。
2. 运行 `pnpm -v`，确认版本 ≥ 8（若未安装：`npm install -g pnpm` 或用 `corepack enable`）。
3. 在 `src-tauri/` 目录下运行 `rustc --version`，确认 ≥ 1.85（若未安装：通过 [rustup](https://rustup.rs/) 安装）。
4. 运行 `pnpm tauri --version`（需先 `pnpm install`），确认 Tauri CLI 为 2.8 系列。

需要观察的现象：四条命令都能正常输出版本号，且不低于上述要求。

预期结果：四项全部满足即环境就绪；任何一项不满足都需要先补齐再继续。

> ⚠️ 待本地验证：以上命令的实际输出取决于你的机器，请以本地结果为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Rust 的最低版本要在 `Cargo.toml` 里用 `rust-version` 字段声明，而不仅是写在 README？

**参考答案**：`Cargo.toml` 里的 `rust-version` 会被 `cargo` 在编译时实际校验——版本不达标会直接报错；README 只是给人看的文档，不会强制。把约束写在「机器会检查」的地方，比写在「只给人看」的地方更可靠。

**练习 2**：如果同事说他全局装了 `tauri` CLI 也能跑，为什么本项目更推荐用 `pnpm tauri`（即项目内的 `@tauri-apps/cli`）？

**参考答案**：项目在 `devDependencies` 里锁定了 `@tauri-apps/cli ^2.8.0`，用 `pnpm tauri` 能保证所有贡献者用同一主版本、行为一致；全局版本可能不同，容易导致「在我机器上能跑」的环境差异。

---

### 4.2 开发命令清单

#### 4.2.1 概念说明

环境就绪后，日常开发就是反复用一组脚本命令。cc-switch 的命令分两组：

- **前端 / 全工程命令**：在项目根目录用 `pnpm <script>` 运行，定义在 `package.json` 的 `scripts` 里。这些命令大多最终转发给 Tauri 或 Vite。
- **Rust 后端命令**：进入 `src-tauri/` 目录后用 `cargo <subcommand>` 运行，是 Rust 生态的标准命令。

理解命令「转发链」很重要：比如 `pnpm dev` 其实是 `pnpm tauri dev`，它内部又会让 Vite 起前端、让 cargo 编译后端。知道这一点，遇到报错时你才知道去哪一侧排查。

#### 4.2.2 核心流程

一个典型的前端开发循环：

```
pnpm install          # 首次或依赖变化时
   ↓
pnpm dev              # 启动开发模式（前端热重载 + 后端编译）
   ↓ （开发中：改代码，保存，窗口自动刷新）
pnpm typecheck        # 提交前：类型检查
pnpm format:check     # 提交前：格式检查
pnpm test:unit        # 提交前：跑前端单测
   ↓
pnpm build            # 打包发布版
```

Rust 后端的开发循环（在 `src-tauri/` 目录下）：

```
cargo fmt             # 格式化 Rust 代码
cargo clippy          # 静态检查（Rust 的 linter）
cargo test            # 跑后端测试
cargo test --features test-hooks   # 启用 test-hooks 特性跑测试
```

#### 4.2.3 源码精读

前端脚本全部定义在 `package.json` 的 `scripts` 字段：

[package.json:6-17](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L6-L17) —— 逐条解读：

| 脚本 | 实际执行 | 作用 |
| --- | --- | --- |
| `dev` | `pnpm tauri dev` | 启动开发模式：Vite 起前端 + cargo 编译后端 + 弹出应用窗口 |
| `build` | `pnpm tauri build` | 打包发布版桌面应用（.dmg / .msi / .AppImage 等） |
| `dev:renderer` | `vite` | 只起前端渲染层（不编译 Rust），调试纯前端时更快 |
| `build:renderer` | `vite build` | 只构建前端产物 |
| `typecheck` | `tsc --noEmit` | 只做 TypeScript 类型检查，不产出文件 |
| `format` | `prettier --write ...` | 用 Prettier 自动格式化前端代码 |
| `format:check` | `prettier --check ...` | 只检查格式是否合规（不修改文件），用于 CI |
| `test:unit` | `vitest run` | 跑前端单元测试（一次跑完即退出） |
| `test:unit:watch` | `vitest watch` | 跑测试并监听文件变化（开发时推荐） |

注意 `dev` 和 `build` 都委托给了 `tauri`，这就是「前端命令转发到 Tauri」的体现。

Rust 后端命令则在 README 的「Rust Backend Development」一节给出：

[README.md:425-444](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L425-L444) —— 列出 `cargo fmt`、`cargo clippy`、`cargo test`、`cargo test test_name`、`cargo test --features test-hooks`。

其中 `--features test-hooks` 用到的 `test-hooks` 特性定义在后端清单里：

[Cargo.toml:18-20](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L18-L20) —— `[features] default = []` 且 `test-hooks = []`，说明这是一个默认关闭、仅测试时按需开启的特性开关。它用来在测试环境下挂载一些只在测试中需要的「钩子」（例如数据库的测试用 hook），正式构建不受影响。

Rust 后端的测试与开发依赖也写在清单末尾：

[Cargo.toml:110-112](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L110-L112) —— `serial_test`（让某些测试串行执行，避免并发争抢数据库连接）和 `tempfile`（创建临时文件/目录做隔离测试）是后端测试的关键依赖，后续 u10-l2 会详讲。

#### 4.2.4 代码实践

**实践目标**：跑通前端「类型检查 + 单元测试」最小闭环。

操作步骤：

1. 在项目根目录运行 `pnpm install` 安装依赖。
2. 运行 `pnpm typecheck`，观察是否有类型错误。
3. 运行 `pnpm test:unit`，观察测试用例数量与通过/失败情况。

需要观察的现象：

- `pnpm typecheck` 正常时无输出、退出码 0；有类型错误时会打印报错文件与行号。
- `pnpm test:unit` 会逐个打印测试文件，最后给出通过/失败统计。

预期结果：类型检查通过、单元测试全绿（具体用例数随版本变化）。

> ⚠️ 待本地验证：本讲不假设你已经运行过上述命令；请实际执行并记录输出。如果 `pnpm test:unit` 有失败用例，先把失败信息记下来——后续 u10-l1 会讲如何调试前端测试。

#### 4.2.5 小练习与答案

**练习 1**：`pnpm dev` 与 `pnpm dev:renderer` 有什么区别？什么时候该用后者？

**参考答案**：`pnpm dev` = `pnpm tauri dev`，会同时编译 Rust 后端并启动整个桌面应用；`pnpm dev:renderer` 只用 Vite 起前端渲染层，不编译 Rust。当你只改前端样式/组件、且不需要后端命令时，用 `dev:renderer` 更快（跳过了 Rust 编译）。

**练习 2**：为什么 `typecheck` 用 `tsc --noEmit` 而不是 `tsc`？

**参考答案**：`--noEmit` 表示「只做类型检查、不生成 .js 文件」。本项目的实际打包由 Vite 负责，TypeScript 在这里只充当类型检查器，所以不需要它产出编译结果。

**练习 3**：`test-hooks` 特性为什么默认关闭？

**参考答案**：它挂载的是只在测试中需要的钩子（如数据库测试 hook）。默认关闭可以保证发布构建干净、不引入测试专用代码路径；只有跑 `cargo test --features test-hooks` 时才启用，实现「生产代码」与「测试辅助」的隔离。

---

### 4.3 前端目录结构（src/）

#### 4.3.1 概念说明

前端代码全部在 `src/` 下。cc-switch 的前端目录遵循一个清晰的约定：**同一功能域的代码尽量聚在一起，而不是按技术角色散落**。这和 u1-l2 讲的分层架构（Components → Hooks → Query → API）是一致的——每一层都是一个一级目录，同一领域在这几层里成对出现（例如 `components/providers/` 配 `hooks/useProviderActions.ts` 配 `lib/api/providers.ts`）。

#### 4.3.2 核心流程

当你想找「某个功能的前端代码」时，按这个顺序定位：

```
1. 先到 components/<领域>/     找 UI 组件
2. 再到 hooks/use<领域>Actions.ts  找业务逻辑组合
3. 再到 lib/query/              找数据缓存与变更
4. 最后到 lib/api/<领域>.ts     找对后端的 invoke 调用
```

掌握这条链路，你就能从任何一个 UI 按钮一路追到后端命令。

#### 4.3.3 源码精读

`src/` 的一级目录（经实际目录核对）：

```
src/
├── App.tsx              # 应用根组件，编排各功能面板（详见 u1-l4）
├── main.tsx             # 前端入口，装配全局 Provider（详见 u1-l4）
├── index.html           # Vite 的 HTML 模板
├── index.css            # 全局样式（含 Tailwind 指令）
├── vite-env.d.ts        # Vite 环境类型声明
├── types.ts             # 前端共享类型（Provider 等核心类型）
├── assets/              # 静态资源（图片、字体等）
├── components/          # UI 组件（按领域分子目录，见 4.3.4）
├── config/              # 预设数据（provider/mcp 预设，见下）
├── contexts/            # React Context（跨组件状态注入）
├── hooks/               # 自定义 Hook（业务逻辑层）
├── i18n/                # 国际化初始化（zh/zh-TW/en/ja 等语言）
├── icons/               # 图标资源/组件
├── lib/                 # 工具与基础设施：api/、query/、utils/ 等
├── types/               # 按领域拆分的类型定义（env/proxy/usage 等）
└── utils/               # 通用工具函数
```

几点要点：

- `config/` 装的是**预设数据**，不是运行时配置。例如 `claudeProviderPresets.ts`、`codexProviderPresets.ts`、`geminiProviderPresets.ts` 等内置了官方推荐的 provider 模板。这是 u3-l4「预设」要讲的内容。
- `lib/api/` 是前后端唯一通信桥梁（封装 Tauri `invoke`/`listen`），u5-l1 详讲；`lib/query/` 是 TanStack Query 配置层，u5-l2 详讲。
- `types.ts`（根级）放跨领域共享的核心类型，而 `types/`（目录）按领域再细分（如 `env.ts`、`proxy.ts`、`usage.ts`）。

> 提示：README 的「Project Structure」图（[README.md:480-502](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L480-L502)）把本地化目录写成 `locales/`，但当前仓库实际使用的是 `i18n/`。这是一个典型的「文档略滞后于代码」的例子——遇到不一致时，**以实际目录为准**。这也是本讲鼓励你亲手 `ls` 的原因。

`src/components/` 的二级目录（按领域组织，已核对）：

```
src/components/
├── providers/      # 供应商管理（ProviderCard、ProviderList、增删改对话框）
├── mcp/            # MCP 面板
├── prompts/        # Prompts 管理
├── skills/         # Skills 管理
├── sessions/       # 会话管理器（Session Manager）
├── proxy/          # 本地代理面板
├── usage/          # 用量统计
├── settings/       # 设置（终端/备份/关于）
├── env/            # 环境变量管理
├── universal/      # 跨应用通用配置
├── deeplink/       # Deep Link 导入
├── agents/         # Agents 相关
├── hermes/         # Hermes 配置面板
├── openclaw/       # OpenClaw 配置面板
├── workspace/      # 工作区
├── common/         # 跨领域通用组件
├── icons/          # 图标组件
├── ui/             # shadcn/ui 组件库（Button、Dialog 等基础组件）
└── （若干顶层 .tsx：AppSwitcher、JsonEditor、MarkdownEditor、UpdateBadge 等）
```

可以看出，几乎每一个一级业务域（providers/mcp/skills/proxy/usage…）都对应一个 `components/` 子目录，这正是「按领域组织」的体现。

#### 4.3.4 代码实践

**实践目标**：在浏览器/编辑器里亲手核对 `src/components/` 的二级结构，并把它画成树。

操作步骤：

1. 在项目根目录执行 `ls src/components/`（或用编辑器展开该目录）。
2. 把结果整理成一棵二级目录树（区分「子目录」与「顶层 .tsx 文件」）。
3. 给每个子目录写一句话职责说明（可参考上表）。

需要观察的现象：你会看到大约 18 个子目录，外加若干顶层 `.tsx` 文件（如 `AppSwitcher.tsx`、`JsonEditor.tsx`、`MarkdownEditor.tsx`、`UpdateBadge.tsx`）。

预期结果：你画出的树应与本节 4.3.3 给出的结构一致；若多出/少了目录，说明你本地版本与本讲 HEAD 不同，属正常。

#### 4.3.5 小练习与答案

**练习 1**：如果要新增一个「日志查看」功能的前端面板，按 cc-switch 的约定，你会在哪些目录下分别创建文件？

**参考答案**：至少四处——`src/components/logs/`（UI 组件）、`src/hooks/useLogs.ts`（业务逻辑 Hook）、`src/lib/query/` 里相关查询/变更、`src/lib/api/logs.ts`（对后端的 invoke 调用）。这体现了「同一领域在多层成对出现」。

**练习 2**：`types.ts`（文件）和 `types/`（目录）为什么要分开？

**参考答案**：`types.ts` 放跨领域、被大量复用的核心类型（如 Provider），保持在根级方便导入；`types/` 则把领域专属类型按文件拆分（如 `proxy.ts`、`usage.ts`），避免单文件膨胀。这是「共享 vs. 领域专属」的组织取舍。

---

### 4.4 后端目录结构（src-tauri/src/）

#### 4.4.1 概念说明

后端代码在 `src-tauri/src/` 下。它比前端目录更「扁平」——大量 `.rs` 文件直接放在根级，因为这些文件是各 CLI 工具的**配置写入器**（config writers），各自独立、地位平等。而真正分层的业务逻辑则进入子目录（`commands/`、`services/`、`database/` 等）。

后端目录同样遵循「按领域组织」，最典型的是 `commands/`：每一个 `.rs` 文件对应一个功能域，最后由 `commands/mod.rs` 统一汇总导出。

#### 4.4.2 核心流程

一个 Tauri 命令从「被前端调用」到「落库」的目录路径：

```
前端 invoke("provider_switch")
   ↓ Tauri IPC
src-tauri/src/commands/provider.rs   # 命令层：参数校验、调用 service
   ↓
src-tauri/src/services/provider/     # 业务层：编排业务逻辑
   ↓
src-tauri/src/database/dao/providers.rs   # DAO 层：拼 SQL
   ↓
src-tauri/src/database/              # 数据库：SQLite 连接
```

这就是 u1-l2 所说「Commands → Services → DAO → Database」分层在目录上的具象体现。

#### 4.4.3 源码精读

`src-tauri/src/` 的一级结构（已核对）：

```
src-tauri/src/
├── main.rs            # 程序入口（平台特殊处理后调用 lib::run，详见 u1-l5）
├── lib.rs             # Tauri 应用装配：插件注册、setup 钩子、命令注册（详见 u1-l5）
├── error.rs           # 统一错误类型 AppError
├── panic_hook.rs      # panic 兜底钩子（记录 backtrace）
├── init_status.rs     # 初始化状态记录
├── store.rs           # AppState 全局状态定义
├── settings.rs        # 设备级设置（settings.json）读写
├── config.rs          # 通用配置读写/原子写入工具
├── provider.rs        # Provider 数据模型
├── provider_defaults.rs  # 内置 provider 预设（种子数据）
├── auto_launch.rs     # 开机自启
├── tray.rs            # 系统托盘
│
├── commands/          # 命令层（Tauri command，按领域拆分）
├── services/          # 业务逻辑层
├── database/          # SQLite 与 DAO（含 dao/ 子目录）
├── proxy/             # 本地代理模块（项目最庞大的子系统）
├── session_manager/   # 会话管理器（含 providers/、terminal/）
├── deeplink/          # Deep Link 解析与导入
├── mcp/               # MCP 同步适配器
├── resources/         # 打包资源
│
└── （根级配置写入器：各 CLI 工具的 *_config.rs / *_mcp.rs）
    ├── claude_desktop_config.rs   claude_mcp.rs   claude_plugin.rs
    ├── codex_config.rs            codex_history_migration.rs
    ├── gemini_config.rs           gemini_mcp.rs
    ├── hermes_config.rs
    ├── opencode_config.rs
    ├── openclaw_config.rs
    ├── app_config.rs              # AppType 枚举与写入器总调度
    └── app_store.rs
```

要点：

- 根级那一堆 `*_config.rs` / `*_mcp.rs` 是「多工具配置写入器」——每一种 CLI 工具一种配置格式，一种写入器。它们是 u4 单元的主题。
- `app_config.rs` 里的 `AppType` 枚举统管这七种工具，是理解写入器矩阵的钥匙（u4-l1）。
- `database/` 下还有 `dao/` 子目录，按表拆分（`providers.rs`、`mcp.rs`、`skills.rs`、`prompts.rs` 等），是 u2 单元的主题。

`commands/mod.rs` 是理解后端目录组织的最佳入口，它用 `mod` + `pub use` 把按领域拆分的命令文件汇总：

[src-tauri/src/commands/mod.rs:1-35](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/commands/mod.rs#L1-L35) —— 这里密集的 `mod provider;`、`mod mcp;`、`mod skill;`、`mod proxy;`、`mod settings;`、`mod usage;` 等声明，就是「每个领域一个命令文件」的目录约定的**权威清单**。想知道某个功能域有没有后端命令，直接在这里搜关键词即可。

`src-tauri/src/commands/` 的二级目录（即每个 `.rs` 命令文件，已核对）：

```
src-tauri/src/commands/
├── mod.rs              # 模块汇总入口（见上）
├── provider.rs         # 供应商命令（CRUD/switch）
├── mcp.rs              # MCP 命令
├── prompt.rs           # Prompts 命令
├── skill.rs            # Skills 命令
├── proxy.rs            # 代理命令
├── failover.rs         # 故障转移命令
├── global_proxy.rs     # 全局代理命令
├── session_manager.rs  # 会话管理命令
├── usage.rs            # 用量统计命令
├── settings.rs         # 设置命令
├── env.rs              # 环境变量命令
├── config.rs           # 配置导入导出命令
├── import_export.rs    # 导入导出
├── auth.rs             # 托管认证命令
├── copilot.rs          # Copilot OAuth 命令
├── codex_oauth.rs      # Codex OAuth 命令
├── coding_plan.rs      # 编码套餐命令
├── subscription.rs     # 订阅命令
├── balance.rs          # 余额查询命令
├── model_fetch.rs      # 模型列表拉取命令
├── stream_check.rs     # 流式检查命令
├── deeplink.rs         # Deep Link 命令
├── sync_support.rs     # 同步支持
├── webdav_sync.rs      # WebDAV 同步命令
├── s3_sync.rs          # S3 同步命令
├── workspace.rs        # 工作区命令
├── hermes.rs           # Hermes 命令
├── openclaw.rs         # OpenClaw 命令
├── omo.rs              # OMO 相关命令
├── plugin.rs           # 插件命令
├── lightweight.rs      # 轻量模式命令
└── misc.rs             # 杂项命令
```

可以看到，命令文件的数量与功能域基本一一对应——这是「按领域组织」在后端最直接的证据。

#### 4.4.4 代码实践

**实践目标**：核对 `src-tauri/src/commands/` 的文件清单，并验证「领域 ↔ 命令文件」的对应关系。

操作步骤：

1. 执行 `ls src-tauri/src/commands/`。
2. 数一下共有多少个 `.rs` 文件（除 `mod.rs` 外）。
3. 打开 [src-tauri/src/commands/mod.rs](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/commands/mod.rs)，确认每个文件都在 `mod` 声明里出现。

需要观察的现象：目录下的文件名与 `mod.rs` 中的 `mod xxx;` 声明应一一对应；若目录里有个文件没在 `mod.rs` 声明，它就不会被编译进命令层。

预期结果：文件清单与本节 4.4.3 给出的树一致，且都能在 `mod.rs` 中找到对应的 `mod` 声明。

> 提示：这一步纯本地核对，无需运行命令，可立即完成。

#### 4.4.5 小练习与答案

**练习 1**：后端为什么把 `claude_desktop_config.rs`、`codex_config.rs` 等写入器放在 `src-tauri/src/` 根级，而不是放进 `commands/` 或 `services/`？

**参考答案**：这些写入器是「无业务策略的格式适配器」——只负责把统一的数据结构写成某种 CLI 工具的特定格式（JSON/TOML 等），不含命令注册或业务编排逻辑。它们被 `services/` 和 `commands/` 共同复用，放在根级体现其「底层工具」定位；而 `commands/`（命令入口）和 `services/`（业务编排）是更高层。

**练习 2**：如果你想确认「代理」功能在后端有哪些入口文件，最快的方法是什么？

**参考答案**：直接在 `commands/mod.rs` 里搜 `proxy`，能看到 `mod proxy;`、`mod global_proxy;`、`mod failover;` 等声明；再结合 `services/proxy.rs` 和 `proxy/` 目录，就能一次性看清代理功能的命令层、业务层与子系统层入口。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从零跑通 + 画出目录地图」的小任务：

1. **环境核对**（对应 4.1）：按 4.1.4 的步骤确认 Node.js / pnpm / Rust / Tauri CLI 版本，把四条版本号记录下来。
2. **安装并自检**（对应 4.2）：运行 `pnpm install`，再依次运行 `pnpm typecheck` 与 `pnpm test:unit`，记录是否通过、测试用例数。> ⚠️ 命令实际输出待本地验证。
3. **画前端地图**（对应 4.3）：画出 `src/components/` 的二级目录树，并标注每个子目录的职责。
4. **画后端地图**（对应 4.4）：画出 `src-tauri/src/commands/` 的文件清单，并指出哪个文件是「模块汇总入口」。
5. **串联思考**：任选一个功能域（比如 `provider`），在地图上标出它的「前端 components → hooks → lib/api」与「后端 commands → services → database」两套路径，体会「同一领域在多层成对出现」。

完成后，你应当拥有一份属于自己的「cc-switch 代码地图」，后续每一讲都可以在这张地图上定位。

## 6. 本讲小结

- cc-switch 的工具链要求：Node.js 18+、pnpm 8+、Rust 1.85+（以 `Cargo.toml` 的 `rust-version` 为准）、Tauri CLI 2.8+（随 `@tauri-apps/cli` 安装）。
- 前端日常工作流：`pnpm install` → `pnpm dev`（开发）→ `pnpm typecheck` / `format:check` / `test:unit`（自检）→ `pnpm build`（打包）；`dev`、`build` 都转发给 `tauri`。
- Rust 后端工作流（在 `src-tauri/` 下）：`cargo fmt` / `cargo clippy` / `cargo test`，测试可加 `--features test-hooks` 启用测试专用钩子。
- 前端 `src/` 按层 + 按领域组织：`components/`、`hooks/`、`lib/`(api/query)、`config/`(预设)、`types/` 等，同一领域在多层成对出现。
- 后端 `src-tauri/src/` 把配置写入器放根级、业务逻辑进 `commands/`(命令) → `services/`(业务) → `database/`(DAO) 子目录，`commands/mod.rs` 是命令层的权威汇总入口。
- 遇到文档与实际目录不一致（如 README 的 `locales/` vs 实际 `i18n/`）时，**以实际目录为准**——养成亲手 `ls` 核对的习惯。

## 7. 下一步学习建议

本讲帮你把项目跑起来、建立了代码地图，下一讲建议：

- **u1-l4 前端启动流程：从 main.tsx 到 App.tsx**——深入 `src/main.tsx` 的 bootstrap 过程，看前端是如何装配全局 Provider、加载 i18n、处理初始化错误的，并理解 `App.tsx` 如何组合本讲看到的那些 `components/` 面板。
- **u1-l5 后端启动与 Tauri 应用装配**——从 `src-tauri/src/main.rs` 进入 `lib.rs::run()`，看 setup 钩子如何完成数据库初始化、迁移、种子等步骤，以及本讲提到的 `store.rs` 里的 `AppState` 是怎么构建的。

如果想在动手之前再巩固全局认知，可以回头重读 u1-l2 的分层架构图（[README.md:346-362](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L346-L362)），把本讲的目录一一对应到那张图的「Components / Hooks / TanStack Query」与「Commands / Services / Models」三层上。
