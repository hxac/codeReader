# codeHighlight：lowlight 集成与懒加载

> 本讲是「代码块子系统（专家级）」单元的第一讲。代码块要呈现彩色语法高亮，背后需要一个独立的「高亮工具」——`src/utils/codeHighlight.ts`。它本身不碰 CodeMirror，却同时服务两种截然不同的渲染模式（替换渲染 vs 行内高亮）。先把它讲透，后续讲代码块插件（u5-l2 ~ u5-l4）就能聚焦在装饰编排上。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 lowlight 是什么、为什么本项目把它设为**可选依赖**并采用**懒加载**。
- 读懂 `codeHighlight.ts` 里「一次异步初始化 + 多次同步检查」的协同设计，以及 `lowlightAvailable` 三态标记的作用。
- 区分 `highlightCode`（返回 HTML 字符串）与 `highlightCodeHast`（返回原始 HAST 树）的适用场景，并能判断代码块两种模式各用哪一个。
- 用 `registerLanguage` 注册一个 lowlight 默认没有的语言，并解释「lowlight 缺失时」所有高亮函数如何优雅降级。

## 2. 前置知识

本讲依赖 [u2-l1（CodeMirror 6 核心概念速览）](u2-l1-codemirror6-core-concepts.md)建立的词汇表，尤其需要你心里有这两点：

- **Decoration 的四种类型**：`mark`（加类名）、`replace`（替换/隐藏原文，可带 widget）、`widget`（零宽插入）、`line`（整行）。本讲涉及的「行内高亮」最终产物就是一堆 `mark` 装饰。
- **utils 层的定位**：在 [u1-l2](u1-l2-source-structure-and-entry.md) 中我们说过，`src/utils` 存放**纯函数与缓存**，不依赖 CodeMirror。`codeHighlight.ts` 正是 utils 层的一员——它只负责「把一段代码文本变成高亮结果」，至于这个结果如何变成装饰，是 plugins/widgets 层的事。

此外补充两个本讲要用的外部概念：

- **lowlight**：一个把 [highlight.js](https://github.com/highlightjs/highlight.js) 包装成「返回语法树」的库。highlight.js 原本直接吐 HTML，而 lowlight 把高亮结果表达成一棵 **HAST（Hypertext Abstract Syntax Tree，超文本抽象语法树）**。HAST 节点的形状很简单，只有三种 `type`：
  - `root`：整棵树的根，`children` 里挂着若干子节点。
  - `element`：一个 HTML 元素，带 `tagName`、`properties.className`（如 `['hljs-keyword']`）、`children`。
  - `text`：纯文本叶子，带 `value`。
- **可选依赖（optional peer dependency）**：见 [u1-l1](u1-l1-project-overview.md)，本项目把 `lowlight` 写进 `peerDependenciesMeta` 并标 `"optional": true`，意味着使用方**可以装、也可以不装**。这就强制 `codeHighlight.ts` 必须在「lowlight 根本没装」时也不崩——这正是「优雅降级」的来源。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/utils/codeHighlight.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts) | **主角**。lowlight 的懒加载封装、两个高亮函数、语言注册、降级逻辑全在这里。无 CodeMirror 依赖。 |
| [src/utils/\_\_tests\_\_/codeHighlight.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/codeHighlight.test.ts) | 测试。展示了 `resetHighlighter → initHighlighter` 的标准初始化节奏，以及「highlighter 不可用时跳过」的测试写法。 |
| [src/widgets/codeBlockWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts) | **消费者之一**：auto/toggle 模式用 `highlightCode` 拿到 HTML，塞进 widget 自己的 DOM。 |
| [src/plugins/codeBlock.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts) | **消费者之二**：inline 模式用 `highlightCodeHast` 拿到 HAST，再转成 `mark` 装饰。 |
| [src/index.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts) | 桶文件，把这些工具 re-export 给使用方。 |
| [package.json](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json) | 声明 `lowlight` 为可选对等依赖 + devDependency。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**① lowlight 懒加载**、**② highlightCode vs highlightCodeHast**、**③ registerLanguage**。优雅降级是一条贯穿三者的暗线。

### 4.1 lowlight 的懒加载与同步/异步初始化

#### 4.1.1 概念说明

语法高亮是个「重」能力：highlight.js 自带上百种语言定义，全量打进主包会让产物臃肿。本项目把它定位成**可选依赖**——你不用代码高亮，就完全不需要装 lowlight。

但「可选」带来一个工程难题：源码顶层如果直接 `import lowlight from 'lowlight'`，那么在没装 lowlight 的环境里，这一行就会在加载阶段直接抛错，整个库都跑不起来。解决之道是**懒加载（lazy loading）**：

> 不在模块加载时引用 lowlight，而是把引用推迟到「真正需要高亮」的那一刻，用动态 `import()` 在运行时尝试加载；加载失败也不抛错，只是标记「不可用」，让上层走降级路径。

#### 4.1.2 核心流程

整个初始化建立在一个**三态标记**上：

| `lowlightAvailable` 的值 | 含义 | 行为 |
|--------------------------|------|------|
| `null` | 还没尝试过加载 | 需要触发一次加载 |
| `true` | 已加载成功，`lowlightInstance` 就绪 | 后续检查直接秒回 |
| `false` | 已尝试但失败（没装/加载报错） | 后续检查秒回 false，走降级 |

配合这个标记，有两条加载路径，分工明确：

1. **异步初始化（一次性，启动时）**：使用方在应用启动时 `await initHighlighter()`。它用动态 `import('lowlight')` 拉取模块，成功后 `createLowlight(common)` 造出实例，把 `lowlightAvailable` 置 `true`。
2. **同步检查（每次高亮调用）**：`highlightCode` / `highlightCodeHast` / `registerLanguage` 内部都会调 `initLowlightSync()`。因为前面异步初始化已经把标记置好，这里只是**读缓存**——`lowlightAvailable !== null` 就直接返回，避免每次高亮都重新加载。

伪代码描述这种协作：

```
应用启动：
  await initHighlighter()        # 异步：动态 import → 建实例 → 标记 = true

之后每次高亮：
  highlightCode(code, lang)
    └─ initLowlightSync()        # 同步：标记已是 true，立刻返回，不再 import
```

> 关键直觉：**把「昂贵的异步加载」与「廉价的同步查询」解耦**。前者只在启动时跑一次，后者每次高亮都跑，但它只读一个布尔标记。

#### 4.1.3 源码精读

模块顶部的两个模块级变量就是三态标记和实例本身：

[src/utils/codeHighlight.ts:22-24](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L22-L24) —— `lowlightInstance` 持有实例，`lowlightAvailable` 是三态标记（`boolean | null`）。

异步加载的「搬运工」是 `loadLowlightModule`，它把动态 `import` 的结果缓存进 `lowlightModule`，并用 `try/catch` 吞掉加载失败：

[src/utils/codeHighlight.ts:67-79](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L67-L79) —— 注意 `lowlightModule !== null` 时直接 `return true`，说明模块**只 import 一次**，之后命中缓存。

异步初始化 `initLowlightAsync` 串起「搬运 → 造实例」两步，任一步失败都把标记置 `false`：

[src/utils/codeHighlight.ts:115-134](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L115-L134) —— `createLowlight(lowlightModule.common)` 用 lowlight 自带的「常用语言集合」`common` 初始化，所以默认就能高亮 JS/Python/TS 等常见语言。

对外暴露的 `initHighlighter` 只是一层转发，语义是「应用启动时调用一次」：

[src/utils/codeHighlight.ts:149-151](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L149-L151) —— 对外 API 名字叫 `initHighlighter`，内部委托给 `initLowlightAsync`。

同步检查 `initLowlightSync` 是「每次高亮调用的入口」，它有三道关：

[src/utils/codeHighlight.ts:82-112](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L82-L112) —— ① 标记已确定就直接返回；② 异步路径已把 `lowlightModule` 装好就复用它；③ 否则退而求其次尝试同步 `require('lowlight')`（CJS 环境可用），都失败则置 `false`。

`isHighlighterAvailable` 是给上层「先问一句能不能高亮」的纯查询函数，组合两个条件（标记为真 **且** 实例非空）：

[src/utils/codeHighlight.ts:156-158](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L156-L158) —— 这是测试里频繁出现的「不可用就跳过」判定的依据。

测试文件展示了使用方应有的初始化节奏——先 `resetHighlighter()` 清空状态，再 `await initHighlighter()`：

[src/utils/\_\_tests\_\_/codeHighlight.test.ts:16-20](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/codeHighlight.test.ts#L16-L20) —— 每个 `beforeEach` 重置 + 异步初始化，保证用例之间互不污染。

而真实使用方（demo）正是在创建编辑器之前 `await initHighlighter()`：

[demo/main.ts:302-305](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L302-L305) —— `await initHighlighter()` 之后再创建编辑器，确保第一个代码块出现时 highligher 已就绪。

#### 4.1.4 代码实践

**实践目标**：亲眼看一次「三态标记」的变化，理解 `initHighlighter` 的异步性质。

**操作步骤**（源码阅读型实践，无需改源码）：

1. 在 demo 目录的浏览器控制台或一个临时脚本里，**先不调用** `initHighlighter()`，直接打印 `isHighlighterAvailable()`。
2. 然后 `await initHighlighter()`，再打印一次。
3. 阅读 [src/utils/codeHighlight.ts:82-112](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L82-L112)，对照你观察到的两次结果。

**需要观察的现象**：第一次调用 `isHighlighterAvailable()` 的返回值是什么？`await initHighlighter()` 之后又变成什么？

**预期结果**：调用 `initHighlighter()` 之前，如果此前从未触发任何高亮，`lowlightAvailable` 多半仍是 `null`（取决于环境里同步 `require` 是否可用），`isHighlighterAvailable()` 返回 `false`；`await` 之后变为 `true`。如果环境里同步 `require('lowlight')` 可用（如打包后的 CJS），则第一次也可能已经 `true`——这正是「同步路径作为异步路径的兜底」的体现。

> 如果无法确定运行结果，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接在 `codeHighlight.ts` 顶部写 `import lowlight from 'lowlight'`？

> **参考答案**：因为 lowlight 是可选依赖，使用方可能根本没装。顶部静态 import 会在模块加载阶段就执行，一旦 lowlight 不存在，整个库都会因 import 失败而无法加载。懒加载（动态 `import()`）把引用推迟到运行时，并用 `try/catch` 把失败降级成「标记为不可用」，从而保证库本身永远能加载。

**练习 2**：`initLowlightSync` 为什么要先检查 `lowlightModule`，再去尝试 `require`？

> **参考答案**：异步路径 `loadLowlightModule` 已经把动态 import 的模块缓存进 `lowlightModule`。如果它非空，说明异步初始化已经成功拉取过模块，直接复用即可，不必再走（可能失败的）同步 `require`，也避免重复加载。

---

### 4.2 highlightCode vs highlightCodeHast：两种高亮输出

#### 4.2.1 概念说明

lowlight 的高亮结果本质上是一棵 HAST 树。但「上层要什么」决定了 `codeHighlight.ts` 该返回什么形式。本模块导出**两个**高亮函数，对应两种消费方式：

| 函数 | 返回类型 | 给谁用 | 怎么用 |
|------|----------|--------|--------|
| `highlightCode(code, lang?)` | `HighlightResult`（含 `html` 字符串） | auto/toggle 模式的 **widget** | 把 `html` 直接塞进 widget 自己 DOM 的 `innerHTML` |
| `highlightCodeHast(code, lang?)` | 原始 HAST 树（或 `null`） | inline 模式的 **plugin** | 遍历 HAST，转成一串 `Decoration.mark` |

为什么会有两种？这对应代码块的两种渲染策略（u5-l2 / u5-l4 详讲）：

- **auto/toggle 模式**：代码内容被 `Decoration.replace` 整个替换进 widget，文本住在 widget 自己造的 DOM 里。这种情况下「直接要一段 HTML」最简单——`innerHTML` 一赋值就完事。
- **inline 模式**：为了保留可编辑性，代码文本必须留在 CodeMirror 的 `contentDOM` 里（这样光标能进去、能改字）。此时不能用 `innerHTML` 覆盖原文，只能用 `Decoration.mark` 给每个 token 套 CSS 类。而 `mark` 装饰需要的是「精确的字符区间 + 类名」，这正好能从 HAST 树的遍历中算出来。

#### 4.2.2 核心流程

`highlightCode` 的判定是一个**逐层降级**的漏斗，每层失败都落到「返回转义纯文本」这个安全出口：

```
highlightCode(code, lang?)
 ├─ code 为空？                      → 返回 {html:'', ...}（早退）
 ├─ initLowlightSync() 失败？        → 返回 escapeHtml(code)（lowlight 没装）
 ├─ 指定了 lang？
 │    ├─ lang 已注册？               → highlight(lang, code) → HAST → hastToHtml
 │    └─ lang 未注册？               → 返回 escapeHtml(code)（纯文本）
 ├─ 未指定 lang？                    → highlightAuto(code) 自动检测 → HAST → hastToHtml
 └─ 以上任意抛错（catch）？          → 返回 escapeHtml(code)
```

`highlightCodeHast` 流程更瘦——它把「HAST → HTML」这一步省掉了，直接把 lowlight 返回的 HAST 树交出去；不可用或抛错时返回 `null`（而不是转义文本），因为调用方（plugin）拿到 `null` 就知道「这次别加高亮 mark」。

#### 4.2.3 源码精读

先看返回类型 `HighlightResult`，三个字段各有用途：`html` 是高亮后的 HTML、`language` 是实际使用的语言、`detected` 标识是否为自动检测：

[src/utils/codeHighlight.ts:13-20](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L13-L20) —— `detected` 区分「指定语言」与「自动检测」，自动检测时 `language` 取自 lowlight 的 `result.data.language`。

`highlightCode` 的逐层降级实现：

[src/utils/codeHighlight.ts:167-222](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L167-L222) —— 重点看四个分支：空代码早退（168-175）、lowlight 不可用降级（177-185）、指定语言且已注册（188-196）、语言未注册降级（197-204）、自动检测（205-213）、总兜底 catch（214-221）。其中「语言未注册」也降级为纯文本，这是后面 `registerLanguage` 存在的原因。

注意 `lowlightInstance.registered(lang)` 这个判断——它决定「这个语言 highligher 认不认得」，不认得就不强行高亮，直接给纯文本：

[src/utils/codeHighlight.ts:190-204](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L190-L204) —— `registered(lang)` 为真才调 `highlight`，否则降级。

HAST → HTML 的桥是内部的 `hastToHtml`，它递归处理 `root/element/text` 三种节点，给 `element` 拼回 `<tag class="...">`，给 `text` 做 HTML 转义：

[src/utils/codeHighlight.ts:41-61](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L41-L61) —— 注意 `text` 节点调 `escapeHtml`，保证代码里的 `<`、`>` 不会被当成 HTML 标签，防 XSS。

`highlightCodeHast` 精简得多，不做 HTML 转换，不可用时返回 `null`：

[src/utils/codeHighlight.ts:250-265](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L250-L265) —— 与 `highlightCode` 共享同一套「空/不可用/未注册/抛错」降级逻辑，但产物是原始 HAST。

**消费侧对照**——看真实调用者如何各取所需：

auto/toggle 模式的 widget 用 `highlightCode` 拿 HTML，按行拆分后塞进 DOM：

[src/widgets/codeBlockWidget.ts:234-247](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L234-L247) —— 先对整块代码高亮再按 `\n` 拆行；若拆出的行数与原文不符（高亮 HTML 里夹了换行），就退化为「逐行单独高亮」。

inline 模式的 plugin 用 `highlightCodeHast` 拿 HAST，再经 `hastToMarkDecorations` 转成 mark 装饰：

[src/plugins/codeBlock.ts:383-394](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L383-L394) —— 拿到 HAST 后转成 mark 装饰，并做边界校验 `mark.from >= 0 && mark.to <= state.doc.length`。

`hastToMarkDecorations` 把 HAST 树「摊平」成按字符偏移排列的 mark 区间——这正是 inline 模式需要 HAST 而非 HTML 的根本原因：

[src/plugins/codeBlock.ts:254-286](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L254-L286) —— 递归 `walk`，遇到 `text` 就按其长度推进 `offset` 并产出一条 `Decoration.mark({class: cls})`，类名取自最近祖先 `element` 的 `className`。

#### 4.2.4 代码实践

**实践目标**：用一组对照输入，看清「高亮成功 / 语言未注册 / lowlight 缺失」三种情况下 `highlightCode` 的返回差异。

**操作步骤**（阅读测试 + 本地验证）：

1. 阅读 [src/utils/\_\_tests\_\_/codeHighlight.test.ts:23-33](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/codeHighlight.test.ts#L23-L33)（JS 高亮，断言 `html` 含 `hljs`）与 [src/utils/\_\_tests\_\_/codeHighlight.test.ts:56-61](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/codeHighlight.test.ts#L56-L61)（未知语言，断言 `html` 就是原文）。
2. 在测试环境里运行下面的断言（示例代码，非项目原有）：

   ```ts
   // 示例代码
   import { resetHighlighter, initHighlighter, highlightCode } from '../src/utils/codeHighlight';

   await resetHighlighter(); await initHighlighter();
   const a = highlightCode('const x = 1;', 'javascript'); // 已注册
   const b = highlightCode('const x = 1;', 'rust');        // lowlight.common 默认不含 rust
   console.log(a.html.includes('hljs'), b.html);            // true ; 'const x = 1;'（纯文本）
   ```

**需要观察的现象**：`a.html` 是否含 `hljs-keyword` 之类类名？`b.html` 是否就是原始字符串、不含任何标签？

**预期结果**：`a` 含 `hljs` 类（高亮成功，`detected=false`）；`b` 因 `rust` 未注册而降级为 `escapeHtml(code)` 的纯文本，`language` 仍是 `'rust'`、`detected=false`。`rust` 这个「未注册」正是下一节 `registerLanguage` 要解决的场景。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `highlightCodeHast` 在不可用时返回 `null`，而 `highlightCode` 返回转义纯文本？

> **参考答案**：两者的消费者不同。`highlightCode` 服务 widget，widget 的 DOM 总得显示点东西，所以给一段安全的转义纯文本作为兜底；`highlightCodeHast` 服务 plugin 的 mark 装饰，调用方拿到 `null` 的语义是「这次别加任何高亮 mark」，代码文本照常以普通文本显示即可，不需要兜底字符串。

**练习 2**：`detected` 字段在什么情况下为 `true`？

> **参考答案**：仅当调用 `highlightCode(code)` 且**未传 `lang`**、走 `highlightAuto` 自动检测分支时为 `true`；只要显式指定了语言（无论该语言是否注册），`detected` 都是 `false`。

---

### 4.3 registerLanguage：注册额外语言

#### 4.3.1 概念说明

lowlight 用 `common` 集合初始化，默认只覆盖几十种常见语言。如果你想高亮 `rust`、`go`、`dockerfile` 等 `common` 里没有的语言，就需要**注册**它。

`registerLanguage(name, syntax)` 就是这个入口。它的第二个参数 `syntax` 是 highlight.js 的 `LanguageFn`——通常这样取：

```ts
// 示例代码：从 highlight.js 取某语言定义
import rust from 'highlight.js/lib/languages/rust';
registerLanguage('rust', rust);
```

注册之后，`lowlightInstance.registered('rust')` 变为 `true`，`highlightCode(code, 'rust')` 就会真正高亮而不是降级为纯文本。

#### 4.3.2 核心流程

```
registerLanguage(name, syntax)
 ├─ initLowlightSync() 失败？  → console.warn 提示，直接 return（lowlight 没装，没法注册）
 ├─ lowlightInstance.register({ [name]: syntax })
 └─ register 抛错？            → console.warn 记录，吞掉异常
```

两个设计要点：

- **注册依赖 highligher 已就绪**：lowlight 都没装，注册无意义，所以先 `initLowlightSync()` 检查，不可用就警告并退出。
- **重复注册不报错**：测试里专门验证「重复注册同一个语言不应抛错」（见 4.3.4），实现用 `try/catch` 兜住。

#### 4.3.3 源码精读

`registerLanguage` 的实现，注意它对「不可用」和「注册失败」两种情况都只警告、不抛：

[src/utils/codeHighlight.ts:230-241](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L230-L241) —— `lowlightInstance.register({ [name]: syntax })` 用「对象简写」把语言名映射到定义函数。

配套的查询函数 `isLanguageRegistered`，是测试和上层判断「某语言能不能高亮」的依据：

[src/utils/codeHighlight.ts:273-279](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L273-L279) —— 同样先 `initLowlightSync()`，不可用直接返回 `false`。

测试验证了「注册前查询为 false、注册后为 true」以及「重复注册不抛错」：

[src/utils/\_\_tests\_\_/codeHighlight.test.ts:104-128](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/codeHighlight.test.ts#L104-L128) —— 用一个 mock 的 `LanguageFn` 注册 `testlang`，断言 `isLanguageRegistered('testlang')` 变 `true`；并验证连续两次注册 `testlang2` 不抛异常。

桶文件把这些函数统一导出，使用方只需从包名引入：

[src/index.ts:38-39](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L38-L39) —— `highlightCode`、`highlightCodeHast`、`registerLanguage`、`isLanguageRegistered`、`initHighlighter`、`isHighlighterAvailable` 全部 re-export，`HighlightResult` 用 `export type` 单独转发。

依赖侧，`lowlight` 既是可选对等依赖（使用方按需装），也是 devDependency（保证测试与 demo 能跑）：

[package.json:61-63](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L61-L63) —— `peerDependenciesMeta.lowlight.optional = true`，这就是「优雅降级」契约的源头。

#### 4.3.4 代码实践

**实践目标**：注册一个 `common` 集合没有的语言，验证高亮从「降级纯文本」变为「真高亮」；并说明 lowlight 缺失时的行为。

**操作步骤**：

1. 参照 [README](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/README.md) 的可选依赖说明，确保已 `npm install lowlight`，并额外装 `highlight.js`（取语言定义用）。
2. 在 demo 的入口（[demo/main.ts:304](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L304) 的 `await initHighlighter()` 之后）加入下面的示例代码：

   ```ts
   // 示例代码
   import rust from 'highlight.js/lib/languages/rust';
   import { registerLanguage, isLanguageRegistered, highlightCode } from 'codemirror-live-markdown';

   await initHighlighter();
   console.log('注册前 rust:', isLanguageRegistered('rust'));     // 多半 false
   registerLanguage('rust', rust);
   console.log('注册后 rust:', isLanguageRegistered('rust'));     // true
   const r = highlightCode('fn main() { println!("hi"); }', 'rust');
   console.log(r.html);                                            // 含 hljs-keyword 等
   ```

3. 在编辑器里输入一个 ```` ```rust ```` 代码块，观察是否出现彩色高亮。

**需要观察的现象**：注册前后 `isLanguageRegistered('rust')` 的值变化；`highlightCode` 返回的 `html` 是否含 `hljs` 类名。

**预期结果**：注册前 `false`、高亮降级为纯文本；注册后 `true`、`html` 含 `hljs-keyword`/`hljs-string` 等类，代码块出现高亮配色。

**关于「lowlight 缺失时」**：如果使用方**没有安装** lowlight，那么 `initLowlightSync()` 始终返回 `false`。此时：

- `registerLanguage` 会打印 `[codeHighlight] lowlight not available, cannot register language` 并直接返回（见 [src/utils/codeHighlight.ts:231-234](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L231-L234)），注册无效。
- `highlightCode` 走降级分支，返回 `{ html: escapeHtml(code), language: lang || 'text', detected: false }`（见 [src/utils/codeHighlight.ts:177-185](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/codeHighlight.ts#L177-L185)）——即**经过 HTML 转义的原始代码文本**，不含任何 `hljs` 类，等于「不高亮但安全」。
- `highlightCodeHast` 返回 `null`，`isLanguageRegistered` 一律返回 `false`。

整个库不会因为缺 lowlight 而崩溃，代码块仍可正常编辑、只是没有彩色高亮——这就是优雅降级。

#### 4.3.5 小练习与答案

**练习 1**：如果在调用 `registerLanguage` **之前**没有 `await initHighlighter()`，会发生什么？

> **参考答案**：`registerLanguage` 内部会调 `initLowlightSync()`。若环境支持同步 `require('lowlight')`，它仍能成功初始化并完成注册；若不支持（典型 ESM/打包环境）且此前没有异步初始化过，则 `initLowlightSync` 返回 `false`，函数打印警告并直接返回，注册失败。因此稳妥做法仍是先 `await initHighlighter()`。

**练习 2**：为什么 `registerLanguage` 对「重复注册同一语言」也要用 `try/catch` 保护？

> **参考答案**：lowlight/register 在某些情况下对重复注册可能抛错，而使用方（尤其在热重载或多次初始化的场景）很容易重复注册。用 `try/catch` 把它降级为一条 `console.warn`，避免一个非致命操作打断整个应用。

---

## 5. 综合实践

把三个模块串起来，完成一次「从零到高亮」的完整接线。

**任务**：在一个最小 CodeMirror 6 编辑器里，让 ```` ```rust ```` 代码块出现彩色高亮，并理解背后每一步。

**步骤**：

1. 安装依赖：`npm install lowlight highlight.js`（lowlight 提供高亮引擎，highlight.js 提供语言定义）。
2. 写一个最小入口（示例代码）：

   ```ts
   // 示例代码
   import { EditorView, EditorState } from '@codemirror/state'; // 实际从 view 取 EditorView
   import { EditorView } from '@codemirror/view';
   import { markdown } from '@codemirror/lang-markdown';
   import rust from 'highlight.js/lib/languages/rust';
   import {
     codeBlockField, initHighlighter, registerLanguage, isHighlighterAvailable,
   } from 'codemirror-live-markdown';

   await initHighlighter();           // ① 异步加载 lowlight（模块 4.1）
   registerLanguage('rust', rust);    // ② 注册 rust（模块 4.3）

   new EditorView({
     state: EditorState.create({
       doc: '```rust\nfn main() {}\n```',
       extensions: [markdown(), codeBlockField()], // codeBlockField 内部调用 highlightCode（模块 4.2）
     }),
     parent: document.body,
   });
   ```

3. 打开页面，确认 rust 代码块出现高亮。
4. **断点追踪**：在 [src/widgets/codeBlockWidget.ts:234](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L234) 的 `highlightCode(code, language)` 处加日志，确认它被调用、且 `result.html` 含 `hljs` 类。

**思考题（贯穿三模块）**：

- 如果第 ① 步 `initHighlighter()` 因为没装 lowlight 而失败，`isHighlighterAvailable()` 会是什么？此时代码块还能正常显示和编辑吗？（答：返回 `false`；代码块仍正常显示为普通文本，只是无高亮——降级而非崩溃。）
- `codeBlockField` 默认走 auto/toggle 模式，用的是 `highlightCode`（HTML）。若改用 inline 模式，高亮数据会从哪个函数取得？（答：`highlightCodeHast`，见 [src/plugins/codeBlock.ts:385](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L385)。）

> 本实践的运行结果取决于本地是否正确安装 lowlight/highlight.js，若未装请标注「待本地验证」。

## 6. 本讲小结

- `codeHighlight.ts` 是 utils 层的纯工具，用**懒加载**（动态 `import`）集成可选依赖 lowlight，靠一个**三态标记** `lowlightAvailable`（`null`/`true`/`false`）实现「一次异步初始化 + 多次同步查询」。
- `initHighlighter()`（异步，启动时调一次）负责真正加载并造实例；`initLowlightSync()`（同步，每次高亮调）只读缓存标记，二者协作把昂贵加载与廉价查询解耦。
- `highlightCode` 返回 **HTML 字符串**（服务 widget 的 `innerHTML`，auto/toggle 模式），`highlightCodeHast` 返回**原始 HAST 树**（服务 plugin 的 `mark` 装饰，inline 模式）——同一棵 lowlight 结果树，两种产物对应两种渲染策略。
- 所有高亮函数都有**逐层降级**：空代码、lowlight 不可用、语言未注册、运行抛错，最终都安全落到「转义纯文本」或 `null`，绝不崩库。
- `registerLanguage(name, syntax)` 用 highlight.js 的 `LanguageFn` 注册 `common` 集合之外的语言；lowlight 缺失时它只警告并返回，`highlightCode` 则返回转义纯文本。
- 测试用 `resetHighlighter()` + `await initHighlighter()` 控制初始化时机，并用 `isHighlighterAvailable()` 守卫「lowlight 没装就跳过」的用例。

## 7. 下一步学习建议

本讲只讲了「高亮结果怎么来」。接下来三讲进入**代码块插件如何编排这些结果**：

- **u5-l2（codeBlockField：三种交互模式 auto/toggle/inline）**：看插件如何识别 `FencedCode` 节点、区分三种 interaction，以及为何 auto 依赖选区而 toggle/inline 不重建——本讲的 `highlightCode`/`highlightCodeHast` 正是被这些编排逻辑调用。
- **u5-l3（codeBlockWidget：DOM 渲染、复制按钮与点击定位）**：深入 auto/toggle 模式 widget 如何把 `highlightCode` 的 HTML 按行渲染、如何用 Canvas 二分查找把点击映射回源码位置。
- **u5-l4（inline 模式：保留 contentDOM 的可编辑高亮）**：聚焦 `highlightCodeHast` + `hastToMarkDecorations` 如何把 HAST 转成 `mark` 装饰，让代码既高亮又可原生编辑。

建议在读 u5-l2 之前，回头再翻一眼本讲的 4.2.3，把「HTML 路线 vs HAST 路线」的差异记牢——它是理解 auto/toggle 与 inline 两条渲染链的分叉点。
