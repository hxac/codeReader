# 项目定位：CC Switch 解决什么问题

## 1. 本讲目标

本讲是整个 cc-switch 学习手册的**第一讲**，目标是让你在不动手写任何代码的前提下，先在脑子里建立起对一个清晰的认识：

- **CC Switch 到底是什么**——它是一个桌面应用，还是一个 CLI？它面向谁？
- **它要解决的痛点是什么**——为什么我们需要它，没有它之前大家是怎么折腾的？
- **它统一管理哪七种 AI CLI 工具**——记住这七种工具的名字和它们各自的配置格式差异。
- **它的核心功能有哪些**——provider 切换、MCP/Skills 管理、本地代理、用量统计、云同步分别是什么。
- **数据存在哪里**——理解 SSOT（单一事实源）这个核心设计理念，知道 SQLite 数据库的位置。

学完本讲，你应该能用自己的话向别人解释「CC Switch 是干嘛的」，并为后续阅读源码（从数据库到 UI 的完整链路）打下基础。

> 本讲只读 README、`package.json` 和用户手册，不涉及 Rust / React 代码细节。技术栈与分层架构的深入讲解放在 **u1-l2**，启动流程放在 **u1-l4 / u1-l5**。

---

## 2. 前置知识

本讲从零开始，但下面几个概念能帮你更快理解，不熟悉的也没关系，文中都会解释：

| 概念 | 通俗解释 |
|------|----------|
| **AI CLI 工具** | 像 Claude Code、Codex、Gemini CLI 这样，在终端里运行、用 AI 辅助编程的命令行工具。 |
| **Provider（供应商）** | 提供 AI 能力的服务方，比如 Anthropic 官方、OpenAI 官方，或各种第三方中转服务。每个 provider 对应一组「API Key + 请求地址」。 |
| **配置文件** | CLI 工具用来读取 API Key、模型、MCP 服务器等设置的文件，常见格式有 JSON、TOML、YAML、`.env`。 |
| **MCP** | Model Context Protocol（模型上下文协议），一种让 AI 工具调用外部服务器（如数据库、搜索）的协议。 |
| **SSOT** | Single Source of Truth，单一事实源。意思是「所有数据只在一个地方存一份」，避免多处副本互相打架。 |
| **SQLite** | 一个轻量级的单文件数据库，不需要单独启动数据库服务，非常适合桌面应用。 |
| **Tauri** | 一个用 Rust 写后端、用前端技术（React/Vue）写界面的桌面应用框架，产物体积小、性能好。 |

如果你完全不熟悉最后一项（Tauri、Rust、React），也不用担心——本讲不会深入它们，后续讲义会逐步展开。

---

## 3. 本讲源码地图

本讲涉及的关键文件都在项目根目录或文档目录下，作用如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md) | 项目英文主文档，包含「为什么需要它 / 功能特性 / 架构总览 / 安装与开发指南」。本讲主要依据它。 |
| [README_ZH.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README_ZH.md) | README 的中文镜像，内容与英文版基本对应，方便中文读者对照。 |
| [package.json](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json) | 前端项目的清单文件，记录了项目名、版本号、简介、脚本命令和依赖。 |
| [docs/user-manual/zh/5-faq/5.1-config-files.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/5-faq/5.1-config-files.md) | 用户手册中的「配置文件说明」章节，详细列出了七种工具各自的原始配置文件路径与格式，是本讲代码实践的重要依据。 |

> 本讲的「源码」主要是**文档与项目清单**。真正的 Rust / TypeScript 源码精读会从 [u1-l3](u1-l3-build-and-structure.md) 开始。

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：项目背景与定位 → 支持的七种工具 → 核心功能矩阵 → 数据存储位置。

### 4.1 项目背景与定位

#### 4.1.1 概念说明

在 AI 编程时代，开发者的工具箱里往往同时装着好几个 AI CLI 工具：Claude Code、Codex、Gemini CLI……而**每个工具都用不同的格式存配置**。当你想在「官方账号」和「第三方中转服务」之间切换时，传统做法是：

1. 翻到某个隐藏目录，找到对应的配置文件；
2. 手动把 JSON / TOML / `.env` 里的 API Key、请求地址改掉；
3. 改错了格式，工具直接报错或崩溃；
4. 多个工具之间还没有统一管理 MCP、Skills 的方式。

CC Switch 的定位就是**解决这个混乱**：它用一个跨平台桌面应用，把所有支持的 AI 工具的配置收拢进一个可视化界面，你只需要「点一下」就能切换供应商，背后由可靠的 SQLite 数据库和原子写入机制保护你的配置不被损坏。

一句话定位：**CC Switch 是七种 AI CLI 工具的「统一配置管家」。**

#### 4.1.2 核心流程

从用户视角看，CC Switch 的核心使用闭环非常简单：

```text
① 选择/添加一个 provider（供应商）
        │
        ▼
② 在数据库中保存该 provider 的配置（SSOT）
        │
        ▼
③ 点击「启用」或从托盘点击 → 把配置写入对应 CLI 工具的真实配置文件（Live 文件）
        │
        ▼
④ （多数工具）重启终端/CLI 工具后生效；Claude Code 支持热切换
```

这里的**关键理念**是：数据库是「唯一真相」，真实配置文件只是数据库内容「投射」出去的副本。这种设计在后续讲义（U2 数据存储、U3 Provider 机制）中会反复出现。

#### 4.1.3 源码精读

项目在 README 的开头就明确了它的定位——标题点出了它要管理的全部七种工具：

[README.md:5-5](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L5-L5) —— 项目副标题，说明 CC Switch 是 Claude Code / Claude Desktop / Codex / Gemini CLI / OpenCode / OpenClaw / Hermes 七种工具的「All-in-One Manager」（一站式管理器）。

紧接着的「Why CC Switch?」段落，直接点出了**痛点**：

[README.md:158-160](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L158-L160) —— 这两段说明了痛点（每个工具配置格式不同、切换要手动改 JSON/TOML/`.env`、缺乏统一管理 MCP/Skills 的方式）以及 CC Switch 的解决方案（单一桌面应用 + 可视化界面 + 50+ 预设 + SQLite + 原子写入）。

中文读者可以对照阅读完全等价的段落：

[README_ZH.md:157-161](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README_ZH.md#L157-L161) —— 「为什么选择 CC Switch？」中文版，把同样的痛点与解决方案用中文复述了一遍。

`package.json` 则从工程角度确认了项目身份：

[package.json:2-4](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/package.json#L2-L4) —— 项目名 `cc-switch`、版本 `3.16.3`、简介 `All-in-One Assistant for Claude Code, Codex & Gemini CLI`（注：简介字段里只举了三种代表工具，但实际支持范围以 README 标题为准，是七种）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手从项目文档中确认 CC Switch 的定位，而不是相信讲义的一面之词。

1. **实践目标**：用你自己的话，写一句话概括 CC Switch 是什么。
2. **操作步骤**：
   - 打开 [README.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md)，阅读第 156–168 行的「Why CC Switch?」。
   - 思考：如果删除这一节，仅看标题和功能列表，你能否推断出它的定位？
3. **需要观察的现象**：注意第 158 行里出现的「JSON、TOML、`.env`」三种文件格式——它们分别对应不同工具，这正是后面 4.2 节要讲的「配置格式碎片化」。
4. **预期结果**：你能写出类似「CC Switch 是一个用 Tauri 2 构建的跨平台桌面应用，用一个可视化界面统一管理七种 AI CLI 工具的供应商配置，并基于 SQLite 做单一事实源」这样的概括。

#### 4.1.5 小练习与答案

**练习 1**：CC Switch 是命令行工具（CLI）还是图形界面应用（GUI）？
> **参考答案**：是图形界面桌面应用（GUI），基于 Tauri 2 构建，支持 Windows / macOS / Linux。它本身不是 CLI，而是用来**管理**那些 CLI 工具的配置。

**练习 2**：为什么 README 强调「atomic writes（原子写入）」？如果不做原子写入会怎样？
> **参考答案**：因为切换供应商时要重写 CLI 工具的真实配置文件。如果写入过程中程序崩溃或断电，文件可能写到一半就损坏，导致 CLI 工具无法启动。原子写入（临时文件 + 重命名）能保证文件要么完整更新成功，要么保持原样，不会出现「半成品」。这一点会在 u3-l3 详解。

---

### 4.2 支持的七种工具

#### 4.2.1 概念说明

这是本讲**必须记住**的一个核心知识点。CC Switch 支持以下七种 AI 工具，每种都有自己的配置目录、配置文件格式和 MCP/Skills 机制：

| # | 工具 | 默认配置目录 | 主要配置文件（格式） |
|---|------|--------------|----------------------|
| 1 | **Claude Code** | `~/.claude/` | `settings.json`（JSON）；MCP 在 `~/.claude.json` |
| 2 | **Claude Desktop** | （桌面应用专属目录） | 由用户手册 `2.6-claude-desktop.md` 说明 |
| 3 | **Codex** | `~/.codex/` | `auth.json` + `config.toml`（TOML） |
| 4 | **Gemini CLI** | `~/.gemini/` | `.env` + `settings.json`（JSON） |
| 5 | **OpenCode** | `~/.config/opencode/` | `opencode.json`（JSON） |
| 6 | **OpenClaw** | `~/.openclaw/` | `openclaw.json`（JSON5） |
| 7 | **Hermes** | `~/.hermes/` | `config.yaml`（YAML）+ `.env` |

> 注意：表格中 **Claude Desktop** 的具体配置路径在 `5.1-config-files.md` 中没有单列（它属于桌面应用而非 CLI），其管理方式在用户手册 [2.6-claude-desktop](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/2-providers/2.6-claude-desktop.md) 中专门讲解，源码侧对应 `src-tauri/src/claude_desktop_config.rs`（待后续讲义精读）。

这张表揭示了一个重要事实：**七种工具的配置格式几乎互不相同**（JSON / TOML / `.env` / YAML / JSON5），这正是 CC Switch 要「统一」的对象。CC Switch 内部为每种工具实现了独立的「配置写入器」，在 U4（多工具配置写入器）中会专门讲解。

#### 4.2.2 核心流程

CC Switch 管理「工具」的核心思路：

```text
对七种工具中的「每一种」：
   ① 该工具在数据库里有自己的一组 provider（供应商）记录
   ② 当前只有一个 provider 处于「激活（active）」状态
   ③ 切换时：把激活 provider 的配置 → 写成该工具对应的格式 → 落到该工具的真实配置文件
```

也就是说，**同一个数据库，会向七个不同的「出口」写入七种不同格式的配置文件**。这就是 README 提到的「Universal providers（通用供应商）」——一份配置可以同步到 Claude Code、Codex、Gemini CLI 等多个工具。

一个重要细节：**大多数工具切换后需要重启终端才能生效，唯一例外是 Claude Code，它支持热切换。**

#### 4.2.3 源码精读

README 的功能特性区用一行总结了「七种工具 + 五十多个预设」：

[README.md:182-184](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L182-L184) —— 「Provider Management」小节，明确列出七种工具名称，并提到「复制 key 即可一键导入」和「通用供应商——一份配置同步到 Claude Code、Codex、Gemini CLI」。

FAQ 里也专门有一问确认了这七种工具：

[README.md:216-218](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L216-L218) —— 「Which AI tools does CC Switch support?」回答：Claude Code、Claude Desktop、Codex、Gemini CLI、OpenCode、OpenClaw、Hermes 七种，每种都有专属预设。

关于「切换后是否需要重启」，FAQ 同样给了明确答案：

[README.md:222-224](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L222-L224) —— 多数工具需重启终端/CLI，**例外是 Claude Code，支持热切换**。

七种工具各自的原始配置文件路径与格式，集中在用户手册里：

[docs/user-manual/zh/5-faq/5.1-config-files.md:69-97](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/5-faq/5.1-config-files.md#L69-L97) —— Claude Code 的配置目录 `~/.claude/`、主文件 `settings.json`、MCP 文件 `~/.claude.json`，以及 `settings.json` 的字段表。

[docs/user-manual/zh/5-faq/5.1-config-files.md:120-154](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/5-faq/5.1-config-files.md#L120-L154) —— Codex 的配置目录 `~/.codex/`、`auth.json` 与 `config.toml`（注意是 TOML 格式，与 Claude 的 JSON 不同）。

[docs/user-manual/zh/5-faq/5.1-config-files.md:156-190](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/5-faq/5.1-config-files.md#L156-L190) —— Gemini CLI 的 `.env`（存放 API Key）与 `settings.json`。

[docs/user-manual/zh/5-faq/5.1-config-files.md:211-234](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/5-faq/5.1-config-files.md#L211-L234) —— Hermes 使用 **YAML**（`config.yaml`），是七种工具里唯一用 YAML 的，CC Switch 把 MCP 写入其 `mcp_servers`、供应商写入 `custom_providers`。

#### 4.2.4 代码实践

1. **实践目标**：亲手列出七种工具各自的配置目录与主配置文件，加深对「格式碎片化」的印象。
2. **操作步骤**：
   - 打开 [docs/user-manual/zh/5-faq/5.1-config-files.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/5-faq/5.1-config-files.md)。
   - 按工具分别找到 Claude Code、Codex、Gemini CLI、OpenCode、Hermes、OpenClaw 六个小节的「配置目录」与「主要文件」。
   - 在 [docs/user-manual/zh/2-providers/2.6-claude-desktop.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/2-providers/2.6-claude-desktop.md) 中找到 Claude Desktop 的管理说明。
3. **需要观察的现象**：注意六种 CLI 工具用了至少四种不同的文件格式（JSON / TOML / `.env` / YAML / JSON5）。
4. **预期结果**：你能口述出「Codex 用 TOML、Hermes 用 YAML、Claude Code 用 JSON」这种差异——这正是 CC Switch 要用「配置写入器矩阵」来抹平的东西。
5. **若本地已安装这些 CLI 工具**：可以实际查看（如 `ls ~/.claude/`）验证；若未安装，本步骤可跳过，标记「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：七种工具中，哪一种用 YAML 配置？哪一种用 TOML？
> **参考答案**：Hermes 用 YAML（`config.yaml`），Codex 用 TOML（`config.toml`）。

**练习 2**：README 说「通用供应商可以同步到 Claude Code、Codex、Gemini CLI」。请结合配置格式差异，思考这意味着后端要做什么工作？
> **参考答案**：后端要把同一份供应商数据，分别序列化成 Claude Code 的 JSON、Codex 的 TOML、Gemini 的 `.env`+JSON，再写到各自目录。这就是 U4「多工具配置写入器」要做的事。

---

### 4.3 核心功能矩阵

#### 4.3.1 概念说明

除了「切换供应商」这个基本盘，CC Switch 还把很多相关功能打包到了一起。理解这张功能矩阵，能帮你快速建立对整个项目模块边界的认知（这恰好对应后续讲义的单元划分）：

| 功能域 | 干什么 | 对应后续单元 |
|--------|--------|--------------|
| **供应商管理（Provider）** | 增删改查供应商、一键切换、托盘切换、拖拽排序、导入导出 | U3 |
| **本地代理与故障转移（Proxy）** | 内置 HTTP 代理，做格式转换、自动故障转移、熔断器、健康检查 | U7 |
| **MCP / Prompts / Skills** | 统一面板管理三类扩展，跨应用双向同步 | U6 |
| **用量统计（Usage）** | 追踪支出、请求数、Token 用量、趋势图、自定义定价 | U8 |
| **会话管理（Session）** | 浏览/搜索/恢复多个工具的对话历史 | U8 |
| **平台集成** | 系统托盘、Deep Link 导入、云同步（WebDAV/S3）、自动更新、多账户认证 | U9 |

记住这张表的右列，你就能大致明白本手册为什么是「十单元」的结构——几乎每个功能域都是一个独立单元。

#### 4.3.2 核心流程

各功能域虽然各自独立，但它们都**共享同一份数据库（SSOT）**，并通过「写入 Live 文件」影响真实工具：

```text
                      ┌─────────────────────────────┐
                      │   cc-switch.db (SQLite SSOT) │
                      │  providers / mcp / skills ...│
                      └──────────────┬──────────────┘
            ┌────────────┬───────────┼───────────┬─────────────┐
            ▼            ▼           ▼           ▼             ▼
       供应商切换      MCP 同步    Skills 分发   代理路由      用量记录
            │            │           │           │             │
            └────────────┴─────┬─────┴───────────┴─────────────┘
                               ▼
                     写入各工具的 Live 配置文件
                     （JSON / TOML / .env / YAML）
```

也就是说，**所有功能最终都会「落地」到那七种工具的真实配置文件上**——这是理解整个项目的钥匙。

#### 4.3.3 源码精读

README 用一组要点概括了核心卖点：

[README.md:162-168](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L162-L168) —— 七个要点：一个应用管七种工具、告别手动编辑、统一 MCP/Skills 管理、托盘快速切换、云同步、跨平台、内置小工具。

更细的功能分块在「Features」区，按功能域组织：

[README.md:187-190](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L187-L190) —— 「Proxy & Failover」小节：本地代理热切换、格式转换、自动故障转移、熔断器、健康监控、整流器，以及应用级接管（独立代理某个工具的某个供应商）。

[README.md:192-195](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L192-L195) —— 「MCP, Prompts & Skills」小节：统一 MCP 面板（双向同步 + Deep Link 导入）、Prompts（Markdown 编辑器、跨应用同步、回填保护）、Skills（从 GitHub/ZIP 安装、符号链接与文件复制）。

[README.md:198-199](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L198-L199) —— 「Usage & Cost Tracking」小节：用量仪表盘，追踪支出/请求/Token，带趋势图、请求日志、自定义模型定价。

[README.md:207-210](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L207-L210) —— 「System & Platform」小节：云同步、Deep Link（`ccswitch://`）、主题、开机自启、自动更新、原子写入、自动备份、国际化（zh/zh-TW/en/ja）。

#### 4.3.4 代码实践

1. **实践目标**：把 README 的功能列表「对号入座」到本手册的单元结构，建立全局地图。
2. **操作步骤**：
   - 打开 [README.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md) 的 Features 区（第 176–210 行附近）。
   - 拿一张纸，左边抄下功能域名，右边写上你猜测的「最相关单元」（参考本讲 4.3.1 的表）。
3. **需要观察的现象**：你会发现「代理与故障转移」是功能描述最长的一块——它也是整个项目最复杂的子系统（U7 占了 4 讲）。
4. **预期结果**：你能说出一句话：「CC Switch 的复杂度集中在代理与故障转移子系统上。」

#### 4.3.5 小练习与答案

**练习 1**：CC Switch 的本地代理能做哪些「可靠性」相关的事？
> **参考答案**：自动故障转移（failover）、熔断器（circuit breaker）、供应商健康监控（health check）、请求整流（rectifier），以及应用级接管。

**练习 2**：Prompts 功能里的「回填保护」是为了解决什么问题？
> **参考答案**：当你直接手动改了活跃 provider 对应的 Live 文件（如 `CLAUDE.md`）后，回到 CC Switch 编辑时，回填保护能把你手改的内容读回来，避免保存时覆盖掉你的改动。详见 U6。

---

### 4.4 数据存储位置说明

#### 4.4.1 概念说明

理解 CC Switch 的数据存在哪里，是理解它**为什么不会弄坏你的配置**的关键。CC Switch 采用 **SSOT（单一事实源）** 设计：所有「可同步」的数据只在一个 SQLite 数据库里存一份；而「设备级偏好」（如语言、主题）单独存在一个 JSON 文件里，不参与同步。

默认情况下，所有数据都在用户主目录下的 `~/.cc-switch/` 目录里：

| 路径 | 内容 | 是否同步 |
|------|------|----------|
| `~/.cc-switch/cc-switch.db` | SQLite 数据库：供应商、MCP、Prompts、Skills 等（SSOT） | ✅ 可同步 |
| `~/.cc-switch/settings.json` | 设备级 UI 偏好（语言、主题、各工具配置目录等） | ❌ 不同步 |
| `~/.cc-switch/backups/` | 自动备份，保留最近 10 个 | ❌ |
| `~/.cc-switch/skills/` | 技能的统一存储目录（默认软链接到各应用） | — |
| `~/.cc-switch/skill-backups/` | 卸载技能前的备份，保留最近 20 个 | ❌ |

这种「双层存储」设计是后续 U2（数据存储与 SSOT 机制）的核心。

#### 4.4.2 核心流程

数据写入与同步的优先级（来自用户手册「配置优先级」）：

```text
① CC Switch 数据库（cc-switch.db）   ← 唯一事实源（SSOT）
        │ 切换/启用供应商时
        ▼
② Live 配置文件（~/.claude/、~/.codex/ 等）   ← 数据库「投射」出的副本
        │ 当你手动编辑了活跃 provider 的 Live 文件后
        ▼
③ 回填（backfill）：CC Switch 编辑时从 Live 文件读回你的改动
```

这个三段式优先级很重要：**数据库是老大**，Live 文件是它的投射，回填是为了不丢失用户在手改 Live 文件时产生的改动。

#### 4.4.3 源码精读

README 的 FAQ「Where is my data stored?」直接给出了数据位置清单：

[README.md:258-264](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L258-L264) —— 列出数据库、本地设置、备份、Skills、技能备份五个位置的默认路径与策略。中文对照见 [README_ZH.md:261-266](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README_ZH.md#L261-L266)。

README「架构总览」里的「Core Design Patterns」给出了数据存储背后的设计原则：

[README.md:366-371](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md#L366-L371) —— 六大设计模式：SSOT（单一事实源）、双层存储、双向同步、原子写入、并发安全（Mutex）、分层架构。中文对照见 [README_ZH.md:369-374](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README_ZH.md#L369-L374)。

用户手册则给出了数据库里具体有哪些表：

[docs/user-manual/zh/5-faq/5.1-config-files.md:13-39](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/5-faq/5.1-config-files.md#L13-L39) —— `~/.cc-switch/` 的目录结构，以及 `cc-switch.db` 里包含的表（providers、provider_endpoints、mcp_servers、prompts、skills、skill_repos、proxy_config、proxy_request_logs、provider_health、model_pricing、settings 等）。

[docs/user-manual/zh/5-faq/5.1-config-files.md:42-59](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/5-faq/5.1-config-files.md#L42-L59) —— `settings.json` 存储的设备级设置示例（语言、主题、窗口行为、各工具配置目录等），并强调「这些设置不会跨设备同步」。

#### 4.4.4 代码实践

1. **实践目标**：搞清楚「哪些数据会同步、哪些不会」，并理解 SSOT 的边界。
2. **操作步骤**：
   - 阅读 [docs/user-manual/zh/5-faq/5.1-config-files.md:13-59](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/5-faq/5.1-config-files.md#L13-L59)。
   - 列出数据库（同步）和 `settings.json`（不同步）各自存了什么。
3. **需要观察的现象**：注意 `settings.json` 里有一组 `xxxConfigDir` 字段（`claudeConfigDir`、`codexConfigDir` 等）——它们记录了每个工具的配置目录，因此是「设备相关」的，所以不参与跨设备同步。
4. **预期结果**：你能解释「为什么把主题设置放在 `settings.json` 而不是数据库里」——因为不同设备可能想要不同主题，而供应商配置则应该跨设备一致。
5. **若本地已安装并运行过 CC Switch**：可以 `ls ~/.cc-switch/` 看看这些文件是否真实存在；若没有，标记「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：SSOT 是什么意思？CC Switch 的 SSOT 是哪个文件？
> **参考答案**：SSOT = Single Source of Truth（单一事实源），即所有可同步数据只在一个地方存一份。CC Switch 的 SSOT 是 `~/.cc-switch/cc-switch.db`（SQLite 数据库）。

**练习 2**：为什么 `settings.json` 不放进数据库一起同步？
> **参考答案**：因为 `settings.json` 存的是设备级偏好（如语言、主题、各工具在本机的配置目录路径），这些值在不同设备上往往不同（比如 Windows 和 macOS 的路径就不一样），同步反而会造成混乱，所以它被设计为「不同步」。

---

## 5. 综合实践

**任务**：用本讲学到的全部知识，为 CC Switch 画一张「全景速查图」，并在图中标注数据与配置的流向。

具体步骤：

1. **阅读 README 全貌**：打开 [README.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/README.md)，通读「Why CC Switch?」「Features」「Where is my data stored?」「Architecture Overview」四块。
2. **列出七种工具**：在一张表里写出七种工具的名字、默认配置目录、主配置文件格式（依据 [5.1-config-files.md](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/5-faq/5.1-config-files.md)）。Claude Desktop 在手册 `2.6` 中查阅并标注。
3. **画出数据流向图**：画出「`cc-switch.db`（SSOT）→ 切换 → 各工具 Live 文件 → 回填」的箭头，并标注格式转换（JSON/TOML/YAML/.env）发生在哪一步。
4. **对应单元**：在功能域旁边，标注它对应的后续单元（U3 供应商、U6 MCP/Skills、U7 代理、U8 用量/会话、U9 平台集成）。
5. **验证**：把这张图讲给一个没听过 CC Switch 的人（或对着自己讲一遍），看他能否听懂。如果卡壳，就回到对应模块重读。

**预期成果**：一张你自己画的全景图，它将是你后续阅读源码时的「导航地图」。完成这张图，本讲的目标就达成了。

> 提示：如果你完全没装过这些 CLI 工具，不影响完成本实践——它的重点是把 README + 用户手册读懂、理清结构，而不是真的去运行什么命令。

---

## 6. 本讲小结

- **CC Switch 是什么**：一个基于 Tauri 2 的跨平台桌面应用，用一个可视化界面统一管理七种 AI CLI 工具的供应商配置。
- **痛点**：七种工具配置格式互不相同（JSON/TOML/.env/YAML/JSON5），手动切换供应商既繁琐又容易写坏文件。
- **七种工具**：Claude Code、Claude Desktop、Codex、Gemini CLI、OpenCode、OpenClaw、Hermes——其中 Claude Code 支持热切换，其余切换后通常需重启。
- **核心功能**：供应商管理、本地代理与故障转移、MCP/Prompts/Skills 统一管理、用量统计、会话管理、平台集成（托盘/Deep Link/云同步/更新/认证）。
- **数据存储**：SSOT 是 `~/.cc-switch/cc-switch.db`（SQLite），设备级偏好放 `~/.cc-switch/settings.json`（不同步），并配有备份与原子写入。
- **设计关键词**：SSOT、双层存储、双向同步、原子写入、并发安全、分层架构——这六个词会在后续讲义反复出现。

---

## 7. 下一步学习建议

本讲只建立了「CC Switch 是什么」的认知，还没有碰任何代码。建议按以下顺序继续：

1. **下一讲 [u1-l2：技术栈与分层架构总览](u1-l2-tech-stack-architecture.md)**：深入理解前端（React/Vite/Tailwind/TanStack Query）和后端（Tauri/Rust/SQLite）技术栈，以及「Commands → Services → DAO → Database」分层架构。
2. **[u1-l3：构建、运行与目录结构](u1-l3-build-and-structure.md)**：学会 `pnpm dev` / `pnpm test:unit` 等开发命令，并熟悉 `src/` 与 `src-tauri/src/` 的目录组织——从这里开始真正进入源码。
3. **想先动手装一下试试**：可参考用户手册 [1.2 安装指南](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/docs/user-manual/zh/1-getting-started/1.2-installation.md) 下载安装包，获得直观感受后再回来读源码会更轻松。

> 阅读源码时，请始终带着本讲的两把钥匙：**「数据库是 SSOT」** 和 **「所有功能最终都落地到七种工具的 Live 配置文件」**。
