# 项目定位与功能总览

## 1. 本讲目标

本讲是整套学习手册的第一篇，目标是让你在**不看任何源码细节**的前提下，先搞清楚三件事：

1. `codemirror-live-markdown` 到底是什么，它解决什么问题。
2. 它为什么被称为「模块化插件集合」，以及「零强制依赖」是什么意思。
3. 它从 v0.1 到 v0.5 都增加了哪些功能，当前版本到底支持什么。

学完本讲，你应该能用自己的话向别人介绍这个项目，并能判断「我是否需要安装额外依赖才能用某个特性」。

---

## 2. 前置知识

本讲面向零基础读者，但有几个名词先解释清楚会更顺畅：

- **CodeMirror 6**：一个用 JavaScript 写的、可高度扩展的代码编辑器内核。它本身只提供编辑能力，所有高级特性（语法高亮、Markdown 预览等）都以「扩展（extension）」的形式由使用者自由组合。本项目就是一组这样的扩展。
- **Markdown**：一种用纯文本标记格式的轻量语言，例如 `**粗体**`、`# 标题`。
- **依赖（dependency）**：一个项目运行所依赖的其它 npm 包。**强制依赖**是必须装的，**可选依赖（optional dependency）**是只在用到对应功能时才需要装的。
- **扩展点（extension point）**：CodeMirror 6 中可以挂载功能的位置，本项目把每个特性做成一个独立的扩展，挂上去即用。

后面三个名词如果不熟没关系，本讲只是先用，后续讲义会深入。

---

## 3. 本讲源码地图

本讲主要读项目说明文档，不深入 `src` 源码。涉及的文件：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目门面：定位、功能矩阵、安装、快速开始、API 参考。是当前**最权威、最新**的功能清单。 |
| `ROADMAP.md` | 路线图：设计原则、版本规划、与同类项目的差异、版本对比表。 |
| `CHANGELOG.md` | 变更日志。注意它只详述到 v0.2.0，比项目实际版本**滞后**，不能当作最新功能来源。 |
| `package.json` | 包元数据。能确认**真实当前版本**与依赖结构（哪些强制、哪些可选）。 |

> 小提醒：三份文档之间存在版本信息不一致（CHANGELOG 最旧，README 与 `package.json` 最新）。后续凡涉及「当前版本支持什么」，一律以 `README.md` 的功能矩阵 + `package.json` 的版本号为准。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Live Preview（实时预览）概念**——它是一种什么样的编辑体验。
2. **模块化插件集合**——为什么这个库是「搭积木」式的。
3. **版本路线与功能矩阵**——每个功能是哪个版本加进来的、属于核心还是可选。

### 4.1 Live Preview（实时预览）概念

#### 4.1.1 概念说明

写 Markdown 的人通常面对两种视图，各有缺点：

- **源码模式（source mode）**：直接看到原始语法，比如 `**粗体**`。优点是好编辑，缺点是不直观，看不出最终排版。
- **阅读模式（reading mode）**：只看到渲染后的结果，比如加粗的「粗体」两个字。优点是直观，缺点是看不到也无法直接修改底层语法。

`README.md` 用一句话点出了这种两难：

> Most Markdown editors force you to choose: either see raw syntax or rendered output.

**Live Preview（实时预览）** 是第三条路：当光标不在某段文本上时，显示渲染后的结果；当光标进入这段文本时，自动把原始语法「淡入」显示出来供你编辑。这种体验由笔记软件 Obsidian 普及，因此本项目副标题就叫「Obsidian-style Live Preview」。

参见 README 的项目定位行：

[README.md:8](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L8) —— 用一句话点明项目定位：为 CodeMirror 6 提供 Obsidian 风格的 Live Preview，且强调它是一个「模块化插件集合（modular plugin collection）」。

#### 4.1.2 核心流程

三种视图模式的区别可以用下表概括：

| 视图模式 | 显示原始语法？ | 显示渲染结果？ | 何时用 |
|----------|----------------|----------------|--------|
| 源码模式 | 一直显示 | 不显示 | 纯手写、排查格式 |
| 阅读模式 | 从不显示 | 一直显示 | 只读浏览 |
| **Live Preview** | **仅光标进入时显示** | **一直显示** | **边写边看（本项目主推）** |

Live Preview 的核心交互可以描述成下面这条状态切换：

```text
光标离开元素  ──►  显示渲染结果（隐藏语法标记）
光标进入元素  ──►  显示原始语法（标记淡入，可编辑）
```

技术上是靠「装饰（Decoration）」决定某段文本是渲染还是显示源码，背后由一个叫 `shouldShowSource` 的决策函数统一判断。这个函数是整套库的「大脑」，本讲只点到为止，第 u2-l2 讲会专门精读它。

#### 4.1.3 源码精读

README 的「Why This Library?」一节解释了为什么要做这个库：

[README.md:16-18](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L16-L18) —— 说明 Live Preview 的价值：不再二选一，语法只在光标进入时淡入，让你既能自然编辑，又能看到格式化结果。

注意，这里的「淡入（fades in）」不是随便一个形容词，它对应了项目里真实的 CSS 过渡动画——光标进出时标记用 `max-width` / `font-size` 的过渡动画平滑显隐，而不是生硬地 `display:none`。这个细节留到第 u7-l1（主题）讲再展开。

#### 4.1.4 代码实践

**实践目标**：直观感受 Live Preview 与源码模式、阅读模式的差别。

**操作步骤**：

1. 打开 README 顶部提供的在线 Demo 链接 [README.md:12](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L12)（`Live Demo` 那一行指向 `https://codemirror-live-markdown.vercel.app/`）。
2. 在编辑器里输入一行：`这是 **粗体** 文本`。
3. 把光标移到「粗体」两个字上面，再移走。

**需要观察的现象**：

- 光标不在粗体上时，你看到的是加粗的「粗体」，看不到 `**`。
- 光标进入「粗体」时，`**` 标记会平滑淡入出现，可以编辑。

**预期结果**：标记随光标位置平滑显隐，没有闪烁。这就是 Live Preview。

> 如果无法访问在线 Demo，这一步标注为「待本地验证」；第 u1-l3 讲会教你本地跑 demo。

#### 4.1.5 小练习与答案

**练习 1**：用一句话说明 Live Preview 与阅读模式的最大区别。

> **参考答案**：阅读模式完全不显示原始语法、不可编辑；Live Preview 在光标进入时才把原始语法淡入显示出来，既看到排版又能编辑。

**练习 2**：README 里说「syntax fades in」，这里的「fade（淡入）」在体验上意味着什么？

> **参考答案**：意味着标记的显隐是平滑过渡的（靠 CSS 过渡动画），而不是瞬间出现/消失，视觉上更柔和。

---

### 4.2 模块化插件集合

#### 4.2.1 概念说明

很多编辑器框架采用「全家桶」思路：一个 `setup()` 函数把所有功能打包给你，装一个就全有。本项目反其道而行，**每个特性都是一个独立扩展，你只用导入你需要的**。README 把它称为「Key differentiators（关键差异点）」，其中第一条就是 Modular（模块化）。

ROADMAP 把这个理念总结为四条设计原则：

1. **Modular by Design**——每个特性都是可独立导入的插件。
2. **Zero Forced Dependencies**——只装你需要的（要不要数学？要不要表格？你说了算）。
3. **Flexible Integration**——能和任何其它 CodeMirror 扩展并存。
4. **Simple API**——直接导入插件，没有复杂的配置系统。

#### 4.2.2 核心流程

「全家桶」与「搭积木」两种风格的对比，ROADMAP 给了很直接的代码对照：

```typescript
// 全家桶风格：要么全要，要么全不要
someBasicSetup()  // 包含一切

// 本项目风格：按需挑选
import { livePreviewPlugin, mathPlugin, tableField } from 'codemirror-live-markdown';

extensions: [
  livePreviewPlugin,
  mathPlugin,
  // tableField,  // 不需要表格？就不导入它
]
```

这带来三个直接好处（对应 ROADMAP 的「Best for」）：

- 给**已有的** CodeMirror 编辑器加上 Live Preview，而不必换框架。
- 对功能有**精细控制**。
- 通过只打包用到的特性来**减小体积**。

对应到依赖管理上，就是「依赖分层」：

- **强制依赖（peerDependencies）**：CodeMirror 6 的几个包，这是地基，必须装。
- **可选依赖（optional peer dependencies）**：`katex`（数学）、`lowlight`（代码高亮），只在用到对应功能时才装。

#### 4.2.3 源码精读

ROADMAP 的设计原则与对比是模块化理念的文字表述：

[ROADMAP.md:7-13](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/ROADMAP.md#L7-L13) —— 四条设计原则：模块化、零强制依赖、灵活集成、简单 API。

[ROADMAP.md:14-30](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/ROADMAP.md#L14-L30) —— 用代码对照说明「全家桶」与「搭积木」的差异。

而依赖分层是**真正落到 `package.json` 里**的硬约定。这才是「零强制依赖」能成立的技术依据：

[package.json:50-64](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L50-L64) —— 注意两层结构：`peerDependencies` 列出了 CodeMirror 的 5 个包（地基，必须装）；`peerDependenciesMeta` 则把 `katex` 和 `lowlight` 标记为 `optional: true`，即「装了才能用对应功能，不装也不影响库本身安装」。

> 术语解释：`peerDependencies`（对等依赖）表示「这个库假设宿主项目会提供这些包，而不是自己打包进去」。这样能避免 CodeMirror 被重复打包、版本冲突。`peerDependenciesMeta` 里的 `optional: true` 进一步声明某个对等依赖是可选的。

README 的安装章节也对应了这个分层，要求装 CodeMirror 对等依赖，并单独说明可选依赖：

[README.md:45-54](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L45-L54) —— 先列必须装的 CodeMirror 对等依赖，再用注释说明 `katex`、`lowlight` 是「按需安装」。

#### 4.2.4 代码实践

**实践目标**：亲手验证依赖分层，理解「零强制依赖」不是空话。

**操作步骤**：

1. 打开 `package.json`，定位到 `peerDependencies` 与 `peerDependenciesMeta`（即上面引用的 [package.json:50-64](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L50-L64)）。
2. 把里面出现的每个包，按「强制 / 可选」填进下表：

| 包名 | 强制还是可选 | 用途 |
|------|--------------|------|
| `@codemirror/state` | ? | ? |
| `@codemirror/view` | ? | ? |
| `@codemirror/lang-markdown` | ? | ? |
| `@codemirror/language` | ? | ? |
| `@lezer/markdown` | ? | ? |
| `katex` | ? | ? |
| `lowlight` | ? | ? |

**需要观察的现象**：哪些包出现在 `peerDependencies` 但**没有**在 `peerDependenciesMeta` 里被标 `optional: true`？哪些被标了？

**预期结果**：

| 包名 | 强制还是可选 |
|------|--------------|
| 5 个 `@codemirror/*` / `@lezer/markdown` | 强制（地基） |
| `katex` | 可选（数学） |
| `lowlight` | 可选（代码高亮） |

**待本地验证**：如果你愿意，可以新建一个空项目，`npm install codemirror-live-markdown`（不装 katex/lowlight），只接入核心扩展，确认库本身能正常加载——这能直观证明「零强制依赖」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CodeMirror 的包要用 `peerDependencies` 而不是 `dependencies`？

> **参考答案**：因为 CodeMirror 6 通常由宿主项目统一管理版本，用对等依赖可以避免本库自己再打包一份、造成版本冲突和体积浪费；它声明「我需要宿主提供这些」，而不是「我自己带一份」。

**练习 2**：如果我只想用 Live Preview + 数学公式，但完全不用代码高亮，需要装 `lowlight` 吗？

> **参考答案**：不需要。`lowlight` 在 `peerDependenciesMeta` 中被标记为 `optional: true`，不导入 `codeBlockField` / `codeBlockEditorPlugin` 这类扩展就不会用到它。

---

### 4.3 版本路线与功能矩阵

#### 4.3.1 概念说明

要了解「当前版本到底支持什么」，最可靠的两份资料是：

- **README 的 Features 表**：当前最全的功能清单，标注了每个功能「Since（自哪个版本）」。
- **`package.json` 的 version 字段**：真实的当前发布版本。

注意 **CHANGELOG 滞后**：它只详述到 v0.2.0-alpha.1，而项目实际已经到了 0.5.x。所以看最新功能别依赖 CHANGELOG。

#### 4.3.2 核心流程

README 的功能矩阵按版本递增展开（从 v0.1 到 v0.5.1），可整理成下面的「功能 → 版本 → 归属」表：

| 功能 | 自版本 | 属于核心还是可选 | 额外依赖 |
|------|--------|------------------|----------|
| Live Preview（标记隐藏） | v0.1.0 | 核心 | 无 |
| Inline Formatting（粗体/斜体等） | v0.1.0 | 核心 | 无 |
| Block Elements（标题/列表/引用） | v0.1.0 | 核心 | 无 |
| Math（行内与块级公式） | v0.2.0 | 可选 | `katex` |
| Tables（GFM 表格渲染） | v0.3.0 | 可选 | `@lezer/markdown` Table |
| Code Blocks（语法高亮） | v0.4.0 | 可选 | `lowlight` |
| Images（图片预览） | v0.5.0 | 可选 | 无 |
| Links（链接渲染） | v0.5.0 | 可选 | 无 |
| Editable Tables（可编辑表格） | v0.5.1 | 可选 | `@lezer/markdown` Table |

其中「核心还是可选」一列来自 README 的 API Reference：它把导出分成 **Core Extensions**（核心扩展）和 **Feature Extensions**（特性扩展）两组。

#### 4.3.3 源码精读

先看真实当前版本，这是判断「功能矩阵是否最新」的基准：

[package.json:3](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L3) —— 当前真实版本为 `0.5.2-alpha.1`，比 CHANGELOG 记录的最新条目（v0.2.0）新得多，说明 CHANGELOG 没跟上。

README 的 Features 表是功能矩阵本体：

[README.md:27-37](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L27-L37) —— 列出 9 个功能及各自起始版本，覆盖 v0.1.0 到 v0.5.1。这是当前最权威的功能清单。

而「核心 vs 可选」的划分在 API Reference：

[README.md:187-209](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L187-L209) —— 「Core Extensions」表列出 5 个核心导出（`livePreviewPlugin`、`markdownStylePlugin`、`editorTheme`、`mouseSelectingField`、`collapseOnSelectionFacet`）；「Feature Extensions」表列出可选特性导出，并标注各自的 `Requires`（如 `mathPlugin` Requires `katex`）。

ROADMAP 还有一个版本对比总表，适合看整体演进与稳定性：

[ROADMAP.md:204-212](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/ROADMAP.md#L204-L212) —— 把 0.1.x 到 1.0.0 各版本的「状态 / 累计功能 / 稳定性 / 适用人群」并排列出；当前 0.5.x 标记为「Current（当前）」，稳定性标为 Low，提示 1.0 前 API 仍可能变化。

> 版本策略提醒（见 [ROADMAP.md:42-48](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/ROADMAP.md#L42-L48)）：0.x.x 阶段是初期开发，API 可能变动；1.0.0 才是首个稳定版。所以当前适合体验和反馈，生产环境要留意可能的破坏性变更。

#### 4.3.4 代码实践

**实践目标**：把 README 的功能矩阵读活，能对每个功能说出它的扩展点归属。

**操作步骤**：

1. 读 README 的 Features 表 [README.md:27-37](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L27-L37)。
2. 对照 API Reference 的两张表 [README.md:187-209](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md#L187-L209)。
3. 为 Features 表里**每一个**功能，写一句话：它属于「核心扩展」还是「可选特性扩展」，对应哪个导出名、需要什么额外依赖。

**需要观察的现象**：哪些功能完全没有额外依赖（连可选依赖都不需要）？哪些功能依赖一个 CodeMirror 之外的包？

**预期结果**（示例片段，请自行补全）：

- Live Preview → 核心，导出 `livePreviewPlugin`，无额外依赖。
- Math → 可选，导出 `mathPlugin` / `blockMathField`，需要 `katex`。
- Code Blocks → 可选，导出 `codeBlockField`，需要 `lowlight`。
- Images → 可选，导出 `imageField()`，无额外依赖。
- ……

#### 4.3.5 小练习与答案

**练习 1**：为什么看「当前支持哪些功能」应该信 README 的 Features 表，而不是 CHANGELOG？

> **参考答案**：因为 CHANGELOG 目前只详述到 v0.2.0-alpha.1，而 `package.json` 显示真实版本已是 `0.5.2-alpha.1`，CHANGELOG 严重滞后；README 的 Features 表与 `package.json` 版本一致，是最新最全的功能来源。

**练习 2**：ROADMAP 的版本对比表里，当前 0.5.x 的「Stability（稳定性）」标为什么？这对使用者意味着什么？

> **参考答案**：标为 Low（低）。意味着当前还处于 alpha 阶段，API 在 1.0 之前可能变动，适合体验、测试和反馈，生产使用要关注可能的破坏性变更。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「项目介绍卡」任务，当作你向同事介绍这个项目的草稿。

**任务**：基于本讲读过的文档，独立完成下面这张表（先空着填，再对照 README 核对）：

| 项目 | 你的回答 |
|------|----------|
| 一句话定位 | ? |
| 解决的核心痛点 | ? |
| Live Preview 与源码模式、阅读模式的差别 | ? |
| 依赖分层（强制 vs 可选各举一例） | ? |
| 当前真实版本（来自 package.json） | ? |
| 9 个功能各自属于核心还是可选 | ? |

**自检要点**：

- 定位里要出现「CodeMirror 6」「Obsidian 风格 Live Preview」「模块化插件集合」这几个关键词。
- 依赖分层要能区分 CodeMirror 5 包（强制）与 katex/lowlight（可选）。
- 版本号要写 `0.5.2-alpha.1`（来自 `package.json`），**不要**照抄 CHANGELOG 的 v0.2.0。
- 功能归属要能区分「核心扩展（5 个）」与「可选特性扩展」。

这是阅读理解型实践，无需运行代码；重点是建立对项目整体的认识。

---

## 6. 本讲小结

- **Live Preview 是第三种视图**：光标进入时才淡入显示原始语法，平时只看渲染结果，兼顾直观与可编辑。
- **项目定位**：为 CodeMirror 6 提供 Obsidian 风格 Live Preview 的**模块化插件集合**，不是全家桶。
- **依赖分层是硬约定**：CodeMirror 的 5 个包是强制对等依赖；`katex`、`lowlight` 是可选对等依赖（`peerDependenciesMeta` 标 `optional: true`）。
- **功能矩阵看 README**：9 个功能从 v0.1.0（核心 Live Preview）逐步加到 v0.5.1（可编辑表格）；核心扩展 5 个，其余为可选特性扩展。
- **文档要会挑**：README 功能矩阵 + `package.json` 版本最权威；CHANGELOG 滞后，别拿它当最新功能来源。
- **当前处于 0.x alpha 阶段**：稳定性 Low，1.0 前 API 可能变动。

---

## 7. 下一步学习建议

本讲只看了文档，还没碰源码。下一讲 **u1-l2「源码目录结构与库入口」** 会带你进入 `src`，看清 `core/plugins/widgets/utils/theme` 五个分层各自做什么，以及 `src/index.ts` 如何把所有扩展统一导出——那才是真正读懂这个库的开始。

建议同步阅读：

- `README.md` 的 Quick Start 与 API Reference，建立对「有哪些导出」的整体印象（为下一讲追踪导出关系做准备）。
- `ROADMAP.md` 的「How It Differs from Similar Projects」一节，加深对模块化理念的理解。
