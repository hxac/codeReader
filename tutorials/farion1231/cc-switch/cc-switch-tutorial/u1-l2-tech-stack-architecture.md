# 技术栈与分层架构总览

## 1. 本讲目标

本讲承接 [u1-l1 项目定位](u1-l1-project-overview.md)：你已经知道 cc-switch「做什么」（统一管理七种 AI CLI 工具的配置），本讲回答「用什么做、怎么组织」。

学完本讲，你应当能够：

1. 看懂 cc-switch 的**技术栈清单**，能指出前端（React 18 / TypeScript / Vite / TailwindCSS / TanStack Query / shadcn-ui）与后端（Tauri 2.8 / Rust / serde / tokio / rusqlite / axum）各自的职责。
2. 画出 cc-switch 的**分层架构图**，说清楚一次用户操作如何从 UI 一路下沉到数据库，再回到界面。
3. 理解贯穿全项目的**五大核心设计模式**：SSOT、双层存储、双向同步、原子写入、并发安全。
4. 具备「看依赖就能猜出项目用某库做什么」的直觉，为后续逐层精读源码打好基础。

本讲不深入任何单个模块的内部实现，那是后续单元的任务。本讲只建立**全局地图**。

## 2. 前置知识

阅读本讲前，建议你先了解以下几个通俗概念：

- **桌面应用（Desktop App）**：像 VS Code、浏览器一样安装在本机、有自己的窗口的程序。cc-switch 就是这样一个桌面应用，跨 Windows / macOS / Linux。
- **前端（Frontend）**：用户看到的窗口界面，本质是一个网页（HTML + CSS + JS），由 React 写成。
- **后端（Backend）**：界面背后真正干活的程序，这里用 Rust 语言写，负责读写文件、操作数据库、起本地代理服务。
- **Tauri**：一个把「网页前端 + Rust 后端」打包成一个桌面应用的框架。它不自带浏览器内核，而是调用系统自带的 WebView 来渲染前端界面，因此安装包很小。
- **IPC（Inter-Process Communication，进程间通信）**：前端（网页）和后端（Rust）是两套运行环境，需要通过一种「通信桥梁」互相调用。Tauri 提供的这种桥梁就叫 **Tauri IPC**，前端用 `invoke` 调用后端的命令，后端用 `emit` 给前端推送事件。
- **依赖（Dependency）**：项目里 `import` 进来、但不由自己写的第三方库。`package.json`（前端）和 `Cargo.toml`（后端）就是各自的「依赖清单」。

> 名词提示：本讲会反复出现「Provider（供应商）」「Live 文件（真实的 CLI 配置文件）」两个词，含义与 [u1-l1](u1-l1-project-overview.md) 一致。

## 3. 本讲源码地图

本讲主要看三份「清单 + 文档」，它们共同勾勒出技术栈与架构：

| 文件 | 角色 | 本讲解读重点 |
| --- | --- | --- |
| [package.json](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json) | 前端依赖清单与脚本 | 前端用了哪些库、对应哪个分层 |
| [src-tauri/Cargo.toml](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml) | 后端依赖清单与构建配置 | 后端用了哪些库、为什么需要它们 |
| [README.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md) | 项目说明（含架构图） | 权威的分层架构图与设计原则描述 |

辅助佐证（用来让架构图落地到真实目录）：

| 文件/目录 | 作用 |
| --- | --- |
| [src-tauri/src/database/schema.rs](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs) | 数据库建表语句，佐证「数据层」实体 |
| `src-tauri/src/commands/`、`src-tauri/src/services/`、`src-tauri/src/database/dao/` | 后端三层目录，佐证「Commands → Services → DAO」分层 |
| `src/components/`、`src/hooks/`、`src/lib/api/`、`src/lib/query/` | 前端四层目录，佐证「Components → Hooks → Query → API」分层 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：前端技术栈解读、后端技术栈解读、分层架构图、核心设计模式。建议按顺序阅读，因为「分层架构图」需要先认识前后端的库，「核心设计模式」需要先理解分层。

---

### 4.1 前端技术栈解读

#### 4.1.1 概念说明

在 Tauri 应用里，前端就是一张跑在系统 WebView 里的网页。cc-switch 的前端是一个**单页应用（SPA）**：用 Vite 打包、用 React 18 组织界面、用 TypeScript 保证类型安全。

之所以选择「网页技术做界面」，是因为 UI 开发效率高、跨平台一致；而把「读写文件、操作数据库、起网络服务」这些系统级工作交给 Rust 后端，是因为 Rust 安全、快、可控。这种**前端负责展示与交互、后端负责系统操作**的分工，是 Tauri 应用的核心思路。

#### 4.1.2 核心流程

前端的运行链路：

1. 启动时，Vite（开发）或打包产物（生产）提供一个 `index.html`。
2. 浏览器/WebView 加载 `index.html`，执行入口脚本 `src/main.tsx`。
3. `main.tsx` 把根组件 `App.tsx` 挂载到页面上，并装配若干全局 Provider。
4. 用户在界面上点击（如「切换供应商」）→ React 组件 → 自定义 Hook → TanStack Query 的 mutation → 封装好的 `invoke` → 经 Tauri IPC 进入 Rust 后端。

#### 4.1.3 源码精读

前端依赖集中在 [package.json:42-94](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L42-L94) 的 `dependencies` 字段。下面挑出与「分层」强相关的库逐一说明：

- **界面渲染与组件**：[package.json:85-86](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L85-L86) 的 `react` / `react-dom` 是整个 UI 的基石；配合大量 `@radix-ui/*`（[package.json:55-68](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L55-L68)）与 `class-variance-authority` / `tailwind-merge`（[package.json:76,L92](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L76-L92)）构成 **shadcn/ui** 组件库体系；`tailwindcss`（[package.json:37](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L37)）负责样式。
- **服务器状态与缓存**：[package.json:69](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L69) 的 `@tanstack/react-query` 负责把「从后端取数据 / 把改动写回后端」这件事缓存化、统一化，这是前端**数据层**的核心。
- **IPC 桥梁**：[package.json:71](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L71) 的 `@tauri-apps/api` 提供 `invoke` 与 `listen`，是前端调用后端的唯一通道。
- **编辑器**：[package.json:43-49](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L43-L49) 的一组 `@codemirror/*` 提供 JSON / Markdown 代码编辑器（编辑 provider 配置、Prompts 都用到）。
- **拖拽排序**：[package.json:50-52](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L50-L52) 的 `@dnd-kit/*` 实现 provider 列表的拖拽排序。
- **国际化**：[package.json:82,L88](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L82-L88) 的 `i18next` / `react-i18next` 支持多语言界面。
- **数据校验**：[package.json:93](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L93) 的 `zod` 配合 [package.json:87](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L87) 的 `react-hook-form`，对表单做类型安全的校验。

README 也给出了前端技术栈的一句话总结：[README.md:469](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L469)。

> 一句话直觉：**「React + shadcn/ui」管长什么样，「TanStack Query」管数据怎么来去，「@tauri-apps/api」管怎么跟后端说话。**

#### 4.1.4 代码实践

**目标**：建立「依赖 → 用途」的快速映射。

**步骤**：

1. 打开 [package.json:42-94](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L42-L94)。
2. 找到 `@tanstack/react-query`、`react`、`@tauri-apps/api` 三行。
3. 用全局搜索（编辑器里搜 `react-query`）看看它在 `src/lib/query/` 下被怎么使用。

**需要观察的现象**：你会发现 React Query 的配置被集中放在 `src/lib/query/queryClient.ts`，而不是散落各处——这印证了「数据层被统一管理」的设计。

**预期结果**：你能用自己的话说明这三者分别属于「数据 / 界面 / 通信」哪一类。

#### 4.1.5 小练习与答案

**练习 1**：`@codemirror/*` 这一组库为什么需要那么多子包（lang-json / lang-markdown / lint / theme-one-dark 等）？
**参考答案**：CodeMirror 6 采用模块化设计，核心只提供编辑器骨架；语言高亮、代码检查、主题都是可插拔的独立包，按需引入能减小打包体积。cc-switch 需要同时编辑 JSON（provider 配置）和 Markdown（Prompts），并支持暗色主题，所以引入了对应的语言包与主题包。

**练习 2**：`zod` 和 `react-hook-form` 通常搭配使用，它们各自负责什么？
**参考答案**：`react-hook-form` 负责管理表单状态（输入值、提交、校验触发），`zod` 负责定义「数据应该长什么样」的 schema 并执行校验；两者通过 `@hookform/resolvers`（[package.json:53](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L53)）桥接，让一份 schema 既做校验又能推导出 TS 类型。

---

### 4.2 后端技术栈解读

#### 4.2.1 概念说明

后端是 cc-switch 的「大脑」，用 Rust 编写。Rust 是一门强调**内存安全**与**高性能**的系统级语言——没有垃圾回收器（GC），却在编译期就能排除大量空指针、数据竞争问题。对于「要反复读写用户磁盘上的配置文件、操作数据库、还要常驻一个本地代理服务」的场景，Rust 既安全又省资源。

后端的所有依赖都列在 [src-tauri/Cargo.toml](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml)。Rust 的依赖管理工具叫 **Cargo**，对应的清单文件就叫 `Cargo.toml`。

#### 4.2.2 核心流程

后端的启动链路（具体细节在 [u1-l5 后端启动](u1-l5-backend-setup.md) 详解，这里只建立轮廓）：

1. `main.rs` 调用 `cc_switch_lib::run()`。
2. `lib.rs` 构造一个 Tauri `Builder`，依次 `.plugin(...)` 注册各插件。
3. 通过 `.setup(...)` 钩子完成初始化（建库、迁移、种子数据等），并把全局状态 `app.manage(...)` 注入。
4. `.invoke_handler(generate_handler![...])` 注册所有暴露给前端的命令。

#### 4.2.3 源码精读

后端依赖在 [src-tauri/Cargo.toml:25-82](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L25-L82)。按分层归类：

- **应用框架**：[Cargo.toml:30](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L30) 的 `tauri = "2.8.2"` 是整个桌面应用的骨架，开启了 `tray-icon`（系统托盘）、`protocol-asset`、`image-png` 等特性。
- **数据层**：[Cargo.toml:75](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L75) 的 `rusqlite`（开启 `bundled`/`backup`/`hooks` 特性）是 SQLite 的 Rust 绑定，对应 SSOT 数据库；`indexmap`（[Cargo.toml:76](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L76)）保证 JSON 字段顺序稳定。
- **序列化（配置多格式适配）**：[Cargo.toml:26-27](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L26-L27) 的 `serde` / `serde_json`；[Cargo.toml:40-41](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L40-L41) 的 `toml` / `toml_edit`；[Cargo.toml:69](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L69) 的 `serde_yaml`；[Cargo.toml:81-82](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L81-L82) 的 `json5` / `json-five`。这么多格式库，正是因为七种 CLI 工具的配置格式各不相同。
- **异步运行时**：[Cargo.toml:46](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L46) 的 `tokio` 是 Rust 生态最主流的异步运行时，支撑并发任务、定时器、同步原语。
- **本地代理 HTTP 服务**：[Cargo.toml:50-63](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L50-L63) 的一整套 `axum` / `tower` / `tower-http` / `hyper` / `hyper-rustls` / `rustls` 等，用来在应用内常驻一个 HTTP 代理服务器（详见 U7 单元）。
- **网络客户端**：[Cargo.toml:42](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L42) 的 `reqwest` 用于发起对上游 API 的请求。
- **Tauri 插件**：[Cargo.toml:31-38](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L31-L38) 的 `tauri-plugin-*` 系列，分别提供日志、自动更新、对话框、本地存储、Deep Link、窗口状态记忆等能力。
- **错误处理**：[Cargo.toml:66-67](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L66-L67) 的 `thiserror` / `anyhow`（统一错误类型详见 [u10-l2](u10-l2-backend-testing-errors.md)）。

另外，[Cargo.toml:102-108](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L102-L108) 的 `[profile.release]` 做了体积优化（`lto`、`opt-level = "s"`、`strip`），并特意设置 `panic = "unwind"`，注释说明这是为了让 panic hook 能捕获 backtrace——这是后端「崩溃可追溯」设计的体现。

README 对后端技术栈的总结见 [README.md:471](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L471)。

> 一句话直觉：**「rusqlite」是数据仓库，「tokio」是并发引擎，「axum/hyper」是内置代理服务器，「serde + 一堆格式库」是七种配置文件的翻译官。**

#### 4.2.4 代码实践

**目标**：把后端依赖和「为什么需要它」对应起来。

**步骤**：

1. 打开 [src-tauri/Cargo.toml:25-82](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L25-L82)。
2. 定位 `rusqlite`、`tokio`、`axum` 三行，注意它们各自开启的 `features`。
3. 对比 `toml` 与 `toml_edit` 两个看起来重复的库。

**需要观察的现象**：`rusqlite` 开启了 `bundled` 特性——这意味着 SQLite 的 C 源码会被一起编译进来，用户机器上不需要预装 SQLite。

**预期结果**：你能解释为什么后端要同时引入 `toml` 和 `toml_edit`（前者解析，后者在保留注释与格式的前提下编辑）。如果你不确定，可标注「待本地验证」后到 [src-tauri/src/codex_config.rs](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/codex_config.rs) 里求证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `serde_json` 要开启 `preserve_order` 特性（[Cargo.toml:26](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L26)）？
**参考答案**：默认情况下 `serde_json` 反序列化到 `HashMap` 时键的顺序不确定。而各 CLI 工具的 JSON 配置文件（如 Claude Desktop 的 `config.json`）对字段顺序、可读性有要求；开启 `preserve_order`（配合 `indexmap`）能让序列化结果保持原始键顺序，避免无谓的 diff，也减少对用户配置文件的破坏性改动。

**练习 2**：`tauri-plugin-single-instance` 为什么放在一个 `cfg(any(target_os = ...))` 条件块里（[Cargo.toml:84-85](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L84-L85)）？
**参考答案**：`cfg(...)` 是 Rust 的「条件编译」，表示这部分依赖只在满足条件（这里是三大桌面操作系统）时才编译。`single-instance` 插件用于防止同时打开多个应用实例，是桌面端才需要的能力，因此用条件块限定平台。

---

### 4.3 分层架构图

#### 4.3.1 概念说明

「分层（Layered）」是把一个复杂系统按职责切成若干层，每层只跟相邻层打交道。好处是**关注点分离**：改 UI 不会动数据库，改存储格式不会动按钮逻辑。cc-switch 在前后端各自分层，再用 Tauri IPC 把两套分层拼起来。

#### 4.3.2 核心流程

README 给出的权威架构图见 [README.md:346-362](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L346-L362)，用文字复述如下：

```
┌─────────────────── 前端（React + TS）────────────────────┐
│  Components(UI) ──► Hooks(业务逻辑) ──► TanStack Query(缓存/同步) │
└──────────────────────────┬──────────────────────────────┘
                           │ Tauri IPC（invoke 调用 / listen 事件）
┌──────────────────────────▼──────────────────────────────┐
│                   后端（Tauri + Rust）                    │
│  Commands(API 层) ──► Services(业务层) ──► Models/Config(数据) │
└──────────────────────────────────────────────────────────┘
```

一次「切换供应商」操作的完整下沉路径（伪代码）：

```
1. 用户点击「启用」           → ProviderCard 组件 (src/components/providers/)
2. 组件调用业务 Hook          → useProviderActions (src/hooks/)
3. Hook 调用 mutation         → useSwitchProviderMutation (src/lib/query/mutations.ts)
4. mutation 调用 API 封装      → api.providers.switch (src/lib/api/providers.ts)
5. API 调 invoke              → 经 Tauri IPC 进入 Rust
6. 命令层 commands/provider    → #[tauri::command] switch_provider (src-tauri/src/commands/)
7. 业务层 services/provider    → ProviderService::switch (src-tauri/src/services/provider/)
8. 数据层 database/dao         → 更新 providers 表 (src-tauri/src/database/dao/)
9. 同步层 live.rs              → 把新配置原子写入 Live 文件
```

> 注意：第 9 步「同步层」并非独立目录，而是 `services/provider/live.rs`，它是后端业务层的一部分，但承担了「数据库 ↔ Live 文件」双向同步的特殊职责。

#### 4.3.3 源码精读

- **前端分层目录**：README 的项目结构图 [README.md:481-502](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L481-L502) 标注了 `components/`（UI）、`hooks/`（业务逻辑）、`lib/api/`（IPC 封装）、`lib/query/`（缓存配置）四块，与架构图一一对应。
- **后端分层目录**：[README.md:503-511](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L503-L511) 标注了 `commands/`（API 层）、`services/`（业务层）、`database/`（DAO 层）。在真实仓库中，`src-tauri/src/commands/` 下有 `provider.rs`、`mcp.rs`、`proxy.rs` 等按领域切分的命令文件；`src-tauri/src/services/` 下有对应的 `provider/`、`mcp.rs`、`proxy.rs`；`src-tauri/src/database/dao/` 下则有 `providers.rs`、`mcp.rs`、`skills.rs` 等数据访问对象。
- **IPC 注册点**：后端在 [src-tauri/src/lib.rs](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/lib.rs) 中用 `.invoke_handler(tauri::generate_handler![...])` 把所有命令注册给前端——这就是「IPC 桥」的入口。
- **数据层实体**：[src-tauri/src/database/schema.rs:27-118](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L27-L118) 用一连串 `CREATE TABLE` 建出 `providers`、`mcp_servers`、`prompts`、`skills`、`settings` 等表，是「数据层」真正落地的地方。

#### 4.3.4 代码实践

**目标**：用真实目录验证架构图，把抽象分层落到具体文件。

**步骤**：

1. 对照 [README.md:478-516](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L478-L516) 的目录树，在本地仓库里逐个找到 `src/components/providers/`、`src/hooks/`、`src/lib/api/`、`src/lib/query/`、`src-tauri/src/commands/`、`src-tauri/src/services/`、`src-tauri/src/database/dao/` 这几个目录。
2. 数一数 `src-tauri/src/commands/` 和 `src-tauri/src/services/` 下各自有多少个按领域命名的文件，观察它们的命名是否成对（如 `commands/provider.rs` ↔ `services/provider/` ↔ `database/dao/providers.rs`）。

**需要观察的现象**：你会发现命令层、业务层、数据层几乎是「同名成对」的——同一个领域（provider / mcp / skill …）在三层各有一个文件，这正是分层架构的典型特征。

**预期结果**：你能画出一张「领域 × 三层」的对照小表，例如 provider 域横跨三个文件。

#### 4.3.5 小练习与答案

**练习 1**：架构图里前端的「TanStack Query」和后端的哪一层职责最接近？为什么它被放在前端而不是后端？
**参考答案**：它最接近后端的「数据层」，但侧重不同：它管的是「前端这一侧」对服务器数据的缓存、失效、重取。放在前端是因为它要直接服务于 React 组件的渲染（决定何时重新取数、何时展示 loading），后端无法替前端做这些 UI 相关的缓存决策。

**练习 2**：如果未来要新增一个「笔记（notes）」功能，按现有分层应该新增哪些文件？
**参考答案**：遵循成对分层的惯例，应同时新增：前端 `src/components/notes/`、`src/lib/api/notes.ts`、相关 query/mutation；后端 `src-tauri/src/commands/notes.rs`、`src-tauri/src/services/notes.rs`、`src-tauri/src/database/dao/notes.rs`，并在 `schema.rs` 里建 `notes` 表、在 `lib.rs` 的 `generate_handler!` 里注册命令。

---

### 4.4 核心设计模式

#### 4.4.1 概念说明

如果说「分层」是骨架，那么下面五个模式就是 cc-switch 处理「配置文件易坏、多设备要同步、要并发安全」这些现实难题的**套路**。它们决定了整个项目的代码风格，后续几乎每一讲都会遇到。权威描述见 [README.md:364-371](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L364-L371)。

| 模式 | 一句话说明 |
| --- | --- |
| **SSOT**（Single Source of Truth，单一事实源） | 所有可同步数据只存一份，放在 SQLite 数据库里，避免多处副本互相矛盾 |
| **双层存储**（Dual-layer Storage） | 可同步数据进 SQLite；设备级偏好（主题、语言）进 `settings.json`，且不参与同步 |
| **双向同步**（Dual-way Sync） | 切换时把数据库写进 Live 文件；编辑活跃配置时再从 Live 文件回读，保留用户手改内容 |
| **原子写入**（Atomic Writes） | 用「写临时文件 + 重命名」替换原文件，保证写到一半崩溃也不会损坏配置 |
| **并发安全**（Concurrency Safe） | 用 `Mutex` 保护数据库连接，避免多个命令并发写入时产生竞争 |

#### 4.4.2 核心流程

这五个模式协同工作，构成一次完整的「切换供应商」流程：

```
用户点击「切换」
   │
   ▼
[并发安全] 命令获取数据库锁，避免与其他写入冲突
   │
   ▼
[SSOT]   在 SQLite 的 providers 表里更新「当前激活」标记
   │
   ▼
[原子写入] 把新激活 provider 的配置写到「临时文件」→ rename 成 Live 文件
   │          （Claude Code 等读到新配置，即刻生效）
   ▼
[双向同步] 若用户之后在 Live 文件里手改了内容，下次编辑时会被回读(backfill)回数据库
   │
   ▼
[双层存储] 整个过程不动 settings.json；设备级偏好始终独立
```

关于数据存放位置，README 在 [README.md:257-265](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L257-L265) 给出清单：数据库在 `~/.cc-switch/cc-switch.db`，本地设置在 `~/.cc-switch/settings.json`，备份在 `~/.cc-switch/backups/` 等。这正是「双层存储」的落点。

#### 4.4.3 源码精读

- **五大模式定义**：[README.md:366-371](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L366-L371) 逐条列出 SSOT、双层存储、双向同步、原子写入、并发安全。
- **数据存储落点**：[README.md:259-260](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L259-L260) 写明数据库（SQLite）与本地设置（JSON）的分工，是「双层存储」的依据。
- **SSOT 的实体**：[src-tauri/src/database/schema.rs:27-118](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L27-L118) 建出的所有表（`providers`、`mcp_servers`、`prompts`、`skills`、`settings`、`proxy_config` 等），就是「单一事实源」的物理体现——所有可同步数据都汇聚到这里。
- **并发安全**：后端在 [Cargo.toml:46](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L46) 引入 `tokio`（含 `sync` 特性），数据库连接以 `Mutex` 包裹后注入全局状态（具体在 U2 单元精读）。Mutex 的含义是「同一时刻只允许一个任务访问连接」，从而排除数据竞争。

> 关于原子写入与双向同步的**代码级**实现，分别在 `src-tauri/src/services/provider/live.rs` 与 `src-tauri/src/config.rs`。本讲是总览，点到为止；它们是 [u3-l3 Live 文件双向同步与原子写入](u3-l3-live-sync-and-atomic-write.md) 的主题。

#### 4.4.4 代码实践

**目标**：从「文档模式」过渡到「能在仓库里指出证据」。

**步骤**：

1. 读 [README.md:366-371](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L366-L371)，把五条模式抄下来。
2. 在 [schema.rs:27-118](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L27-L118) 中找到 `settings` 表的建表语句，注意它和别的表（如 `providers`）结构上的不同。

**需要观察的现象**：`settings` 表的结构是 `(key TEXT PRIMARY KEY, value TEXT)`（见 schema.rs 第 118 行附近），是一个**键值对**表，而不是像 `providers` 那样有固定列。这与「双层存储」呼应：数据库里也有一个 `settings` 键值表，但 README 说的「本地设置 settings.json」是另一套设备级偏好——两者分工不同，初学时容易混淆，值得留意。

**预期结果**：你能区分「数据库里的 settings 键值表」与「磁盘上的 settings.json 文件」各自存什么。若一时分不清，可标注「待确认」，留待 [u2-l1](u2-l1-database-schema.md) 与 [u9-l4](u9-l4-update-settings-auth.md) 详解。

#### 4.4.5 小练习与答案

**练习 1**：「原子写入（临时文件 + rename）」为什么能防止配置损坏？请用你自己的话解释。
**参考答案**：操作系统的 `rename`（重命名）在同一个文件系统上通常是「原子」的——要么整个完成，要么完全不发生，不会出现「半个文件」的中间状态。因此做法是：先把新内容完整写到一个临时文件，确认写成功后再用 rename 把它替换掉正式配置文件。这样即便写入过程中断电/崩溃，正式配置文件要么还是旧的完整版本，要么已经是新的完整版本，绝不会是写了一半的残缺文件。

**练习 2**：为什么「双层存储」要把设备级偏好（主题、语言）单独放进 `settings.json`，而不是也存进数据库？
**参考答案**：因为这些偏好是「跟设备相关」的——同一台机器上你想要暗色主题，但通过云同步把数据搬到另一台机器时，并不希望覆盖那台机器的主题设置。把设备级偏好排除在同步范围之外（放进独立的 JSON 文件），就能让「可跨设备同步的业务数据」与「本机专属的界面偏好」互不干扰。

---

## 5. 综合实践

**任务**：完成规格要求的对照练习——在 `package.json` 与 `Cargo.toml` 中，各找出三个分别对应「UI / 业务逻辑 / 数据」分层的关键依赖，并说明作用。

**操作步骤**：

1. 打开 [package.json:42-94](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L42-L94)，按下表「前端」一栏挑选三个依赖。
2. 打开 [src-tauri/Cargo.toml:25-82](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/Cargo.toml#L25-L82)，按下表「后端」一栏挑选三个依赖。
3. 把你的选择填进下面的表格，每项用一句话写明作用。

**参考填法**（你可以给出不同的合理答案）：

| 分层 | 前端依赖（package.json） | 后端依赖（Cargo.toml） | 作用 |
| --- | --- | --- | --- |
| UI / 界面 | `react` / `@radix-ui/*`(shadcn) | `tauri`（提供窗口与 WebView 容器） | 前者渲染界面组件；后者承载整个桌面应用骨架 |
| 业务逻辑 / 交互 | `@tanstack/react-query` 或 `react-hook-form` | `tokio`（异步运行时）/ `axum`（本地代理） | 前者管理数据的取/存/缓存；后者驱动后端并发与代理服务 |
| 数据 / 存储 | `@tauri-apps/api`（`invoke` 走 IPC 取后端数据） | `rusqlite`（SQLite）/ `serde_json` | 前者是通向后端数据的桥梁；后者是数据真正落地与序列化的地方 |

**进阶**：在前端 `src/lib/api/` 下找到一个 `invoke` 调用，在后端 `src-tauri/src/commands/` 下找到它对应的 `#[tauri::command]` 函数，亲手把第 4.3 节那条「下沉路径」走一遍。这一步把本讲三个最小模块（技术栈、分层、IPC）串了起来，是进入下一讲的最好热身。

> 说明：本实践为**源码阅读型实践**，无需运行程序；若你想顺便验证开发环境，可执行 `pnpm typecheck`（[package.json:12](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L12)）观察是否通过，命令的实际运行结果待本地验证。

## 6. 本讲小结

- cc-switch 用 **Tauri 2** 把「React + TS 前端」和「Rust 后端」打包成一个跨平台桌面应用，两者经 **Tauri IPC** 通信。
- 前端技术栈以 **React 18 / Vite / TailwindCSS / shadcn-ui / TanStack Query** 为核心，外加 CodeMirror 编辑器、dnd-kit 拖拽、i18next 国际化等。
- 后端技术栈以 **Tauri 2.8 / Rust / tokio / rusqlite / serde / axum-hyper** 为核心，并引入 toml/yaml/json5 等多格式库来适配七种 CLI 工具的异构配置。
- 整体是清晰的**分层架构**：前端 Components → Hooks → Query → API，后端 Commands → Services → DAO → Database，靠 IPC 拼接。
- 五大**核心设计模式**——SSOT、双层存储、双向同步、原子写入、并发安全——贯穿全项目，是理解后续每一讲的钥匙。
- 看 `package.json` 与 `Cargo.toml` 两份依赖清单，就能大致判断某个能力由谁提供、属于哪一层。

## 7. 下一步学习建议

本讲建立了全局地图，接下来建议沿两条路径深入：

1. **先把「怎么跑起来」搞清楚**：进入 [u1-l3 构建、运行与目录结构](u1-l3-build-and-structure.md)，动手执行 `pnpm install` / `pnpm dev`，把本讲讲的目录在本地真实跑一遍。
2. **再分别进入两条启动链路**：
   - 前端启动链路 → [u1-l4 前端启动流程：从 main.tsx 到 App.tsx](u1-l4-frontend-bootstrap.md)
   - 后端启动链路 → [u1-l5 后端启动与 Tauri 应用装配](u1-l5-backend-setup.md)

之后，U2 单元会从「数据层」开始自下而上精读，届时本讲提到的 SSOT、原子写入、并发安全都会有真实的代码佐证。
