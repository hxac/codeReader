# 项目总览:Zed 是什么

> 本讲是 Zed 学习手册的第一讲。它是整个手册的地基:先弄清"Zed 是什么、由谁做的、仓库长什么样",之后再进入源码细节。

## 1. 本讲目标

学完本讲,你应该能够:

- 用自己的话说出 Zed 的产品定位与核心技术亮点(高性能、多人协作、Rust + 自研 GPUI)。
- 讲清 Zed 与 Atom、Tree-sitter 的历史渊源,理解"为什么这群人要做第二个编辑器"。
- 掌握一套"从 README 提取项目关键信息"的通用阅读方法。
- 认识 Zed 仓库根目录下每一类文件的作用,拿到官方文档、贡献指南、术语表等入口资源。

本讲不要求你写任何 Rust 代码,重点是**建立全局认知 + 学会自己找入口**。

## 2. 前置知识

本讲只需要以下常识,用通俗语言解释一遍:

- **代码编辑器**:程序员用来写代码的工具,例如 VS Code、Vim、Sublime Text。Zed 也是这样一款工具,只是它把"快"和"多人实时协作"当作第一目标。
- **Rust 与 Cargo**:Rust 是一门系统级编程语言,特点是内存安全、无垃圾回收、性能接近 C/C++。Cargo 是 Rust 官方的构建工具,`cargo build`、`cargo run`、`cargo test` 分别用来构建、运行、测试项目。
- **crate 与 workspace**:crate 是 Rust 的编译单元,可以粗略理解为"一个包/一个库"。一个大型项目可以把许多 crate 放进同一个 **workspace(工作区)**,共用一份依赖锁定和构建缓存。Zed 的 workspace 里有 **243 个 crate**(本讲义写作时用 `ls crates | wc -l` 实际统计过),这是一个超大型 Rust 项目。
- **Git 与 GitHub**:Git 是版本控制系统,GitHub 是托管 Git 仓库的平台。本讲提到的"仓库(repo)"就是这份 Zed 源码目录。
- **Atom 与 Tree-sitter**:Atom 是 GitHub 在 2011-2022 年间开发的编辑器;Tree-sitter 是一个增量语法解析库,如今被 GitHub、Neovim、Zed 等广泛用于语法高亮。两者的主要作者后来创立了 Zed Industries,继续做编辑器——所以 Zed 常被称作"精神上的 Atom 继任者"。
- **LSP(Language Server Protocol)**:语言服务器协议,让编辑器和语言分析服务(补全、跳转、诊断)标准化通信的协议。现在只需知道 Zed 深度使用它即可,第 6 单元会专门讲。

## 3. 本讲源码地图

本讲涉及的文件都以"阅读"为主,不涉及代码逻辑:

| 文件 | 角色 | 本讲怎么用它 |
| --- | --- | --- |
| [README.md](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/README.md) | 仓库门面 | 提取项目定位、安装方式、构建文档入口、许可证信息 |
| [CONTRIBUTING.md](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/CONTRIBUTING.md) | 贡献指南 | 了解贡献规则、核心 crate 鸟瞰图、术语表入口 |
| [docs/src/development/glossary.md](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/glossary.md) | 开发术语表 | 预习整个手册会反复出现的术语(Entity、Pane、Dock、Worktree 等) |
| [Cargo.toml](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml) | 工作区清单 | 确认 workspace 成员与默认构建目标 |
| [rust-toolchain.toml](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/rust-toolchain.toml) | 工具链固定 | 看出项目需要的 Rust 版本与编译目标 |

辅助入口(只指出位置,不展开):[docs/src/SUMMARY.md](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/SUMMARY.md) 是官方文档(mdBook)的目录页,`docs/src/development/` 下还有 `macos.md`、`linux.md`、`windows.md` 三份平台构建文档和 `feature-process.md` 功能提案流程。

## 4. 核心概念与源码讲解

### 4.1 README 阅读与信息提取

#### 4.1.1 概念说明

README.md 是一个开源仓库的"门面":维护者把最想让陌生人第一眼看到的信息放在这里。学会高效读 README,是阅读任何大型开源项目的第一项基本功——它通常能回答五个问题:

1. **这是什么**(一句话定位)?
2. **用户怎么安装使用**?
3. **开发者怎么从源码构建**?
4. **怎么参与贡献**?
5. **许可证是什么**(法律边界)?

对学习者来说,第 3、4、5 三个问题的答案就是后续学习的"入口资源清单":构建文档决定你能不能把项目跑起来,贡献指南决定你能不能看懂代码组织,许可证决定你能怎么使用这些代码。

#### 4.1.2 核心流程

读 README 的一种固定套路(伪代码):

```text
打开 README.md
  ↓
第 1 步:开头找一句话定位            → 回答"是什么、谁做的"
第 2 步:看 Installation 章节        → 回答"用户怎么装"
第 3 步:看 Developing/Building 章节 → 拿到"从源码构建"的文档链接
第 4 步:看 Contributing 章节        → 拿到贡献指南链接
第 5 步:看 Licensing 章节           → 弄清许可证,解释仓库里为何有两个 LICENSE 文件
```

每一步产出一个"事实 + 它所在的位置(文件与行号)"。把位置记下来很重要——这就是你日后随时能回来查证的"锚点"。

#### 4.1.3 源码精读

**第一步:一句话定位。** README 开头就写明了 Zed 是什么:

- [README.md:L6-L6](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/README.md#L6-L6) —— "Welcome to Zed, a high-performance, multiplayer code editor from the creators of Atom and Tree-sitter."(欢迎来到 Zed:一款高性能、支持多人协作的代码编辑器,由 Atom 和 Tree-sitter 的创作者打造。)

这一行浓缩了三个关键事实:

| 短语 | 含义 |
| --- | --- |
| high-performance(高性能) | 用 Rust 编写、GPU 渲染,把帧时间预算定得很苛刻(贡献指南里要求交互帧不超过 8ms,即 120fps) |
| multiplayer(多人协作) | 多人可实时共享同一个项目/会话协同编辑,这是 Zed 区别于多数编辑器的核心功能 |
| from the creators of Atom and Tree-sitter | 团队血统:Zed Industries 的主力正是 Atom 与 Tree-sitter 的原作者 |

**第二步:安装方式。**

- [README.md:L10-L16](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/README.md#L10-L16) —— 说明了 macOS、Linux、Windows 三个平台都可以从官网直接下载,或用各平台包管理器安装;Web 版仍在跟踪讨论中,尚未发布。

**第三步:开发者构建入口(对学习者最重要)。**

- [README.md:L18-L22](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/README.md#L18-L22) —— "Developing Zed" 章节给出三份平台构建文档链接:`docs/src/development/macos.md`、`linux.md`、`windows.md`。下一讲(u1-l2)的构建实践就以此为准。

**第四步:贡献入口。**

- [README.md:L24-L26](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/README.md#L24-L26) —— 指向 CONTRIBUTING.md;同页还提到他们在招聘([README.md:L28-L28](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/README.md#L28-L28))。

**第五步:许可证。**

- [README.md:L30-L40](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/README.md#L30-L40) —— Zed 源码"主要以 GPL-3.0-or-later 许可,部分标注处为 Apache-2.0"。这解释了仓库根目录为什么同时存在 `LICENSE-GPL` 和 `LICENSE-APACHE` 两个文件。此外这段还说明 CI 用 `cargo-about` 工具自动校验第三方依赖的许可证合规——所以给 Zed 加依赖时,许可证不是小事。

**第六步:谁在维护。**

- [README.md:L42-L48](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/README.md#L42-L48) —— Zed 由营利性公司 Zed Industries, Inc. 开发,可通过 GitHub Sponsors 赞助,赞助只作为公司收入、不附带特权。

README 读完后,再扫一眼 CONTRIBUTING.md 里的三块"规则性"内容,它们决定了你日后以什么姿势参与这个社区:

- [CONTRIBUTING.md:L5-L8](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/CONTRIBUTING.md#L5-L8) —— 参与贡献前需遵守行为准则,并签署贡献者许可协议(CLA)才能合并。
- [CONTRIBUTING.md:L16-L22](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/CONTRIBUTING.md#L16-L22) —— 官方最欢迎的 PR 类型:修文档、修 bug、让现有功能适配更多平台/模式的小增强、小功能(如键位与 action),以及明确的社区计划项。想做大功能?见 [CONTRIBUTING.md:L33-L33](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/CONTRIBUTING.md#L33-L33):**不要直接开 PR**,先读功能提案流程,去 GitHub Discussions 讨论。
- [CONTRIBUTING.md:L49-L63](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/CONTRIBUTING.md#L49-L63) —— 提高 PR 被合并概率的清单:确认改动是社区想要的、说清解决什么问题、带测试、UI 改动附截图、一个 PR 只做一件事。

#### 4.1.4 代码实践

**实践:按五步法精读 README,并交叉验证两个事实。**

1. **实践目标**:把上面"五步读 README 法"实际走一遍,产出 5 条"事实 + 位置"的笔记,并验证 README 的说法与仓库实际内容一致。

2. **操作步骤**:
   - 打开仓库根目录的 `README.md`,按"定位 → 安装 → 构建 → 贡献 → 许可证"的顺序找到对应章节,记录每条信息的行号。
   - 验证一:README 说许可证是"GPL 为主 + 部分 Apache-2.0",在仓库根目录执行 `ls LICENSE*`,确认 `LICENSE-GPL` 与 `LICENSE-APACHE` 两个文件确实存在。
   - 验证二:README 的 "Developing Zed" 给出三个文档链接,执行 `ls docs/src/development/`,确认 `macos.md`、`linux.md`、`windows.md` 确实存在(本讲义写作时已核实存在,你复现时应看到同样结果)。

3. **需要观察的现象**:README 中的每个说法都能在仓库里找到对应的实体文件或链接目标,没有"死链"。

4. **预期结果**:得到一份类似下面的笔记表:

   | 事实 | 位置 |
   | --- | --- |
   | 高性能多人协作编辑器,Atom/Tree-sitter 作者出品 | README.md 第 6 行 |
   | 支持 macOS/Linux/Windows 安装 | README.md 第 10-16 行 |
   | 构建文档在 docs/src/development/{macos,linux,windows}.md | README.md 第 18-22 行 |
   | 贡献指南是 CONTRIBUTING.md | README.md 第 24-26 行 |
   | GPL-3.0-or-later 为主,Apache-2.0 组件标注;CI 校验许可证 | README.md 第 30-40 行 |

#### 4.1.5 小练习与答案

**练习 1**:Zed 官方用哪一句话定位自己?这句话透露了哪三个关键信息?

> **答案**:"a high-performance, multiplayer code editor from the creators of Atom and Tree-sitter"(README.md 第 6 行)。三个信息:① 高性能(技术路线是 Rust + GPU 渲染);② 多人协作(核心差异化功能);③ 出身 Atom/Tree-sitter 原班人马(团队血统)。

**练习 2**:仓库根目录为什么有两个 LICENSE 文件?CI 和许可证有什么关系?

> **答案**:因为 Zed 以 GPL-3.0-or-later 为主体许可,部分组件采用 Apache-2.0,所以 `LICENSE-GPL` 与 `LICENSE-APACHE` 并存。项目用 `cargo-about` 自动生成依赖许可证清单来合规,CI 不通过时通常要检查新 crate 是否 `publish = false`、或是否需要把依赖的许可证 SPDX 标识加进 `script/licenses/zed-licenses.toml`。

**练习 3**:你想给 Zed 加一个比较大的新功能,第一步该做什么?

> **答案**:不要先写 PR。按 CONTRIBUTING.md 第 33 行的指引,先阅读 `docs/src/development/feature-process.md` 了解功能设计流程,然后到 GitHub Discussions(而不是 Issues)发提案,与官方确认方向后再动手。

### 4.2 仓库顶层文件巡览

#### 4.2.1 概念说明

一个陌生仓库到手,除了 README,最值得看的就是**根目录的文件清单**。根目录是项目的"目录页":构建体系、开发环境、CI、本地服务编排的配置都集中在这里。能认出这些文件的类型和作用,你就知道这个项目"用什么构建、在哪些平台跑、怎么保证质量"。

Zed 是一个**超大型 Cargo workspace 单体仓库(monorepo)**:所有子模块(243 个 crate)、官方文档、内置资源、扩展、CI 脚本都在同一个 git 仓库里。理解"monorepo + workspace"这个组织方式,是理解 Zed 代码结构的第一把钥匙——后面第 3 讲(u1-l3)会专门按层次给 crate 分类。

#### 4.2.2 核心流程

给根目录条目分类的流程(伪代码):

```text
对根目录每个条目,按关注点归类:
  Cargo.toml / Cargo.lock / rust-toolchain.toml     → Rust 构建体系
  flake.nix / default.nix / shell.nix / flake.lock / nix/ → Nix 声明式开发环境
  Dockerfile-* / compose.yml / Procfile* / livekit.yaml   → 本地协作服务编排
  clippy.toml / rustfmt.toml / typos.toml / lychee.toml / renovate.json → 代码质量与自动化工具配置
  crates/ assets/ docs/ extensions/ script/ tooling/ ci/ legal/ → 按职责划分的目录
```

归类之后,再对每个"构建体系"文件问三个问题:谁读它?什么时候读?它决定了什么?

#### 4.2.3 源码精读

**入口一:工作区清单。**

- [Cargo.toml:L3-L3](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L3-L3) —— 根 `Cargo.toml` 从第 3 行开始声明 `members = [...]`,把 `crates/` 下全部 243 个 crate 纳入同一个 workspace,共用一份 `Cargo.lock` 与构建缓存。
- [Cargo.toml:L265-L265](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/Cargo.toml#L265-L265) —— `default-members = ["crates/zed"]`:直接执行 `cargo build` / `cargo run` 时**只构建主程序 crate `zed`**,而不是全部 243 个成员。这个设计让日常开发循环快很多,也提示我们:`crates/zed` 是整个应用的汇合点。

**入口二:工具链固定。**

- [rust-toolchain.toml:L1-L9](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/rust-toolchain.toml#L1-L9) —— 把 Rust 工具链钉在 `1.97.1`,并预置了三个编译目标,每个都带注释:`wasm32-wasip2`(扩展系统基于 WASM)、`wasm32-unknown-unknown`(GPUI 的 Web 版)、`x86_64-unknown-linux-musl`(远程开发服务器)。一个 9 行的小文件,已经透露了三大架构事实:**扩展跑在 WASM 沙箱里、GPUI 计划支持 Web、Zed 有远程开发组件**。

**入口三:根目录文件速查表**(本表在本仓库根目录逐一核对过,均已存在):

| 文件 / 目录 | 类别 | 作用说明 |
| --- | --- | --- |
| `Cargo.toml` / `Cargo.lock` | 构建 | workspace 清单与依赖版本锁定,保证所有人构建可复现 |
| `rust-toolchain.toml` | 构建 | 固定 Rust 1.97.1 + wasm/musl 目标(见上文) |
| `flake.nix` / `default.nix` / `shell.nix` / `flake.lock` / `nix/` | 环境 | Nix 声明式开发环境,一条命令拉齐所有系统依赖 |
| `clippy.toml` / `rustfmt.toml` | 质量 | clippy 与 rustfmt 的项目级规则 |
| `typos.toml` | 质量 | 拼写检查的白名单配置 |
| `lychee.toml` | 质量 | 文档链接有效性检查 |
| `renovate.json` | 自动化 | 依赖更新机器人配置 |
| `livekit.yaml` | 协作 | 本地 LiveKit 音视频服务器的开发配置(协作语音/视频用) |
| `Procfile` / `Procfile.web` / `compose.yml` / `Dockerfile-*` | 协作 | 本地一键起 collab 服务、LiveKit、对象存储等整套协作基础设施 |
| `crates/` | 源码 | 243 个 crate,全部业务代码所在 |
| `assets/` | 资源 | 内置资源:默认设置(`settings/`)、键位(`keymaps/`)、主题(`themes/`)、图标、字体、提示词等 |
| `docs/` | 文档 | mdBook 文档源码,入口是 `docs/src/SUMMARY.md` |
| `extensions/` | 扩展 | 内置扩展源码(glsl、html、proto 等)与扩展开发说明 |
| `script/` | 脚本 | 构建/打包/检查辅助脚本(`bootstrap`、`clippy`、`bundle-*` 等) |
| `tooling/` | 工具 | 仓库辅助工具(`xtask`、`lints`、`perf`、`compliance`) |
| `ci/` / `.github/workflows/` | CI | CI 辅助 Dockerfile 与 GitHub Actions 工作流 |
| `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` | AI 规范 | 给 AI 编程助手看的仓库编码规范(本手册的写作约束也参考了它) |
| `CODE_OF_CONDUCT.md` / `CONTRIBUTING.md` / `REVIEWERS.conl` / `legal/` | 社区 | 行为准则、贡献指南、审阅人配置、法律文件 |

**入口四:官方亲自画的"鸟瞰图"。** CONTRIBUTING.md 末尾有一节 "Bird's-eye view of Zed",是官方给新人准备的 crate 导览:

- [CONTRIBUTING.md:L164-L166](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/CONTRIBUTING.md#L164-L166) —— 官方建议新人把术语表(glossary)放在手边,再开始读代码。
- [CONTRIBUTING.md:L170-L182](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/CONTRIBUTING.md#L170-L182) —— 核心 crate 一句话清单,摘录如下:

| crate | 官方描述(意译) |
| --- | --- |
| `gpui` | GPU 加速的 UI 框架,提供 Zed 的所有 UI 基础构件(官方推荐优先熟悉其根级文档) |
| `editor` | 核心 `Editor` 类型,驱动代码编辑器和 Zed 内所有输入框,并承载 Inlay Hints、补全等 LSP 显示层 |
| `project` | 管理文件树与导航,是 Zed 侧的 LSP 通信方 |
| `workspace` | 处理本地状态序列化,把项目组织成窗口 |
| `vim` | 在 `editor` 之上的一层薄薄的 Vim 工作流 |
| `lsp` | 与外部语言服务器通信 |
| `language` | 驱动编辑器的语言理解:符号列表、语法映射等 |
| `collab` | 协作服务器本体,支撑项目共享等协作功能 |
| `rpc` | 定义与协作服务器交换的消息 |
| `theme` / `ui` | 主题系统与通用 UI 组件库 |
| `cli` / `zed` | CLI 入口;`zed` 是一切汇合处与 `main` 入口 |

这张表不用背,它是第 3 讲(u1-l3)"crate 分层"的预习材料。

**入口五:术语表预览。** [docs/src/development/glossary.md](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/glossary.md) 定义了全代码通用的词汇,先预览三组(第 2、4 单元会正式学习):

- [glossary.md:L21-L27](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/glossary.md#L21-L27) —— 命名约定:`AnyName` 是类型擦除版本(类似 `Box<dyn NameTrait>`);`NameStore` 是抽象"本地运行还是远程运行"的包装类型。
- [glossary.md:L33-L43](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/glossary.md#L33-L43) —— GPUI 状态管理核心词:`App`(持有全部应用状态的单例,只在 UI 线程)、`Context`(针对特定 Entity 的 App 包装)、`AsyncApp`(异步版)、`Entity`(GPUI 管理的强类型引用)、`WeakEntity`(可能已失效的弱引用)。
- [glossary.md:L78-L96](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/glossary.md#L78-L96) —— 界面结构词:`Window` → `Pane`(窗口中心放置编辑器/终端的区域)→ `Dock`(左/右/底部可开合、装 Panel 的区域);`Project` 是一个或多个 `Worktree`;`Multibuffer` 是多个编辑器的拼接视图(项目搜索、go-to-definition 的载体)。
- [glossary.md:L100-L104](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/glossary.md#L100-L104) —— `Buffer`:一个"文件"的内存表示,连同语法树、git 状态、诊断等相关数据。

#### 4.2.4 代码实践

**实践:根目录"寻宝"——给 5 类文件写出作用,并用 git 验证活跃度。**

1. **实践目标**:不看资料,仅凭文件名和浏览内容,说出根目录 5 个文件/目录的作用,并用 `git log` 佐证它们是"活的"配置。

2. **操作步骤**:
   - 在仓库根目录执行 `ls -1`,通读清单,按 4.2.2 的分类法在纸上把条目分成"构建 / 环境 / 协作编排 / 质量工具 / 目录"五组。
   - 挑 5 个你认识的文件类型(如 `Cargo.lock`、`flake.nix`、`livekit.yaml`、`renovate.json`、`typos.toml`),用 `head -20 <文件>` 看看内容,验证你的猜测。
   - 执行 `git log --oneline -3 -- rust-toolchain.toml` 与 `git log --oneline -3 -- livekit.yaml`,观察这两个文件的修改历史。

3. **需要观察的现象**:
   - `head -20 rust-toolchain.toml` 能看到钉死的工具链版本与三个编译目标注释;
   - `git log` 对不同文件返回的提交数量不同——构建体系文件改动频繁(工具链升级),而像 `livekit.yaml` 这类本地编排文件相对稳定。

4. **预期结果**:你能不假思索地回答"如果我要改 clippy 规则/加 Nix 依赖/调本地协作服务端口,分别该动哪个文件"。以上命令均为只读操作,在任何平台都能安全执行;若你在自己 clone 的仓库中运行,输出应与本讲义描述一致(具体提交数随时间推移会变化,属正常现象)。

#### 4.2.5 小练习与答案

**练习 1**:`rust-toolchain.toml` 里为什么要编译 `wasm32-wasip2` 目标?文件里的注释是怎么说的?

> **答案**:因为 Zed 的扩展系统运行在 WASM 沙箱中,扩展需要编译到 `wasm32-wasip2`;文件中该行的注释就写着 `# extensions`。同时还能看到 `wasm32-unknown-unknown`(`# gpui on the web`)和 `x86_64-unknown-linux-musl`(`# remote server`)两个目标。

**练习 2**:在这个仓库直接运行 `cargo build`,实际会构建哪些 crate?为什么?

> **答案**:只构建 `crates/zed` 及其依赖链。因为根 `Cargo.toml` 第 265 行声明了 `default-members = ["crates/zed"]`,避免每次构建全部 243 个成员。想构建整个 workspace 需要显式 `cargo build --workspace`。

**练习 3**:根据 glossary,`Pane` 和 `Dock` 有什么区别?`Editor` 是 `Panel` 吗?

> **答案**:`Pane` 是窗口中心区域的分块,里面放置编辑器、终端等条目;`Dock` 是左/右/底部可开合的区域,里面装的是 `Panel`(如 ProjectPanel、AgentPanel),最多三个同时打开。`Editor` 不实现 `Panel` trait,它作为条目活在 `Pane` 中。

## 5. 综合实践

**任务:完成本讲的完整实践闭环——"读懂门面 + 认全目录"。**

1. **实践目标**:通读 README.md 与 CONTRIBUTING.md,用三句话总结 Zed 的定位;并列出仓库根目录下 5 个你认识的文件类型及其作用。

2. **操作步骤**:
   - 克隆仓库:`git clone https://github.com/zed-industries/zed.git && cd zed`(若已克隆可跳过;本手册即是在该仓库内编写)。
   - 通读 `README.md`(全文约 50 行)与 `CONTRIBUTING.md`(重点读 "Contribution ideas"、"Sending changes"、"Bird's-eye view of Zed" 三节)。
   - 写下三句话定位总结。参考示例(欢迎形成自己的版本):
     1. Zed 是一款用 Rust 编写、GPU 渲染的高性能代码编辑器,交互帧预算 8ms(120fps);
     2. 它的差异化能力是多人实时协作,后台由自研的 collab 服务器与 LiveKit 音视频支撑;
     3. 它出自 Atom 与 Tree-sitter 原作者团队(Zed Industries),以 GPL-3.0-or-later 开源,扩展系统基于 WASM。
   - 执行 `ls -1` 列出根目录,挑 5 个文件类型查证作用,可参考下表格式记录:

   | 文件 | 我的判断 | 查证方式 |
   | --- | --- | --- |
   | `Cargo.lock` | 锁定全部依赖精确版本,保证可复现构建 | `head -20 Cargo.lock`,看到大量 `[package]` 版本条目 |
   | `flake.nix` | Nix 声明式开发环境入口 | `head -30 flake.nix` |
   | `livekit.yaml` | 本地 LiveKit 音视频服务器配置 | `cat livekit.yaml`,看到端口 7880 与 devkey |
   | `rust-toolchain.toml` | 固定 Rust 工具链与编译目标 | `cat rust-toolchain.toml` |
   | `compose.yml` | Docker Compose 编排本地协作服务 | `cat compose.yml` |

3. **需要观察的现象**:三句话总结能覆盖"是什么 / 凭什么快 / 谁做的怎么开源"三个维度;5 个文件的作用判断与文件实际内容吻合。

4. **预期结果**:产出一份《Zed 仓库初见笔记》,包含三句话定位 + 一张根目录文件表。这份笔记就是你后续学习的手边地图——第 2 讲构建环境、第 3 讲 crate 分层都会直接复用它的结论。

## 6. 本讲小结

- Zed 的官方定位:**高性能、多人协作的代码编辑器**,由 Atom 与 Tree-sitter 的创作者打造([README.md:L6](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/README.md#L6-L6))。
- 五步读 README 法:定位 → 安装 → 构建文档 → 贡献指南 → 许可证;每条信息都记下"事实 + 位置"。
- Zed 是 GPL-3.0-or-later + Apache-2.0 双许可的 monorepo,CI 用 `cargo-about` 校验依赖许可证合规。
- 仓库是包含 243 个 crate 的 Cargo workspace,`default-members = ["crates/zed"]` 让日常构建只编译主程序。
- `rust-toolchain.toml` 钉死 Rust 1.97.1 与 wasm/musl 目标,顺带透露了扩展(WASM)、GPUI Web、远程开发三大架构事实。
- 入口资源三件套:平台构建文档 `docs/src/development/{macos,linux,windows}.md`、贡献指南 `CONTRIBUTING.md`、开发术语表 `docs/src/development/glossary.md`。

## 7. 下一步学习建议

- **下一讲(u1-l2)**:按照 README 指向的平台构建文档,在你自己的机器上把 Zed 从源码编译并运行起来——这是后续所有"改代码看效果"实践的前提。
- **提前浏览**:通读一遍 [glossary.md](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/docs/src/development/glossary.md) 全文(约 120 行),不求记住,混个眼熟即可。
- **源码预告**:`crates/zed/src/zed.rs` 是应用汇合点,第 4 讲(u1-l4)会从 `main()` 开始逐段阅读启动流程;本讲可以先 `ls crates/zed/src/` 感受一下入口模块的规模。
