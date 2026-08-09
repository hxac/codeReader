# 测试策略：用 Vitest + jsdom 测试 ViewPlugin 与 Widget

## 1. 本讲目标

前面几讲我们一直在「读源码」：读 `livePreviewPlugin` 怎么隐藏标记、读 `MathWidget` 怎么造 DOM、读 `shouldShowSource` 怎么决策。本讲换一个视角——**写测试来证明这些行为真的成立**。读完本讲，你应该能够：

- 说清为什么本项目的测试必须跑在 `jsdom` 环境，以及 `vitest.config.ts` 里几个关键字段的作用；
- 独立用 `EditorState.create` + `new EditorView` 在测试里搭出一个真实的编辑器，并遍历它产出的 `Decoration` 来验证 `ViewPlugin` 行为；
- 掌握三种「把不可控的外部世界变得确定」的 mock 手法（`navigator.clipboard`、`loadImage`、全局 `Image`）；
- 理解 `widget.eq()` 在测试里怎么直接断言，以及它如何对应 CodeMirror 的「重渲染优化」。

> 本讲属于专家层。它不教你 CodeMirror 怎么用，而是教你**如何为 CodeMirror 插件写可信的自动化测试**。前置讲义是 `u2-l4（livePreviewPlugin）` 和 `u3-l2（mathPlugin）`，你会看到这两讲里的概念如何在测试里被「钉死」。

## 2. 前置知识

在开始前，先用大白话过一遍本讲涉及的几个概念：

- **测试运行器（test runner）**：自动跑你的 `it('应该……')` 断言并汇总「通过/失败」的工具。本项目用 **Vitest**，它的 API（`describe/it/expect`）和 Jest 几乎一样。
- **测试环境（environment）**：你的测试代码是跑在「纯 Node」还是「模拟浏览器」里。本项目的 `ViewPlugin`、`WidgetType.toDOM()` 会调用 `document.createElement`，必须有 DOM，所以用 **jsdom**——一个用 JS 实现的浏览器 DOM。
- **mock（打桩）**：把「真实会联网/会弹窗/会异步」的东西替换成「立即返回固定值」的替身。比如复制按钮真实会调用浏览器剪贴板，测试里我们换成 `vi.fn()`，既能确定返回值，又能断言「它被调用过、参数是什么」。
- **`widget.eq()`**：CodeMirror 6 的 `WidgetType` 要求实现的方法。当编辑器重算装饰时，CodeMirror 会拿「新 widget」和「旧 widget」比 `eq`：返回 `true` 就**复用旧 DOM、不重渲染**，返回 `false` 才销毁重建。它是一道「重渲染闸门」，也是性能优化的关键。
- **确定性测试**：同样的输入永远得到同样的输出，不依赖网络、时间、随机数。mock 的终极目的就是让测试「确定」。

## 3. 本讲源码地图

本讲会反复进出下面几个文件。它们分两类：**配置/工具**和**被测对象的测试**。

| 文件 | 作用 | 在本讲的角色 |
| --- | --- | --- |
| `vitest.config.ts` | Vitest 配置 | 讲清 jsdom 环境与 `globals` |
| `src/core/__tests__/shouldShowSource.test.ts` | 纯函数 `shouldShowSource` 的测试 | 「只需 State、不需 View」的最简测试范式 |
| `src/plugins/__tests__/livePreview.test.ts` | `livePreviewPlugin`（ViewPlugin）的测试 | 「构造 EditorView + 捕获装饰」范式 |
| `src/widgets/__tests__/codeBlockWidget.test.ts` | `CodeBlockWidget` 的测试 | mock `clipboard` 与 `widget.eq` 断言 |
| `src/utils/__tests__/imageLoader.test.ts` | `loadImage` 的测试 | 用「替换全局 `Image`」+ `vi.useFakeTimers` mock 异步 |
| `src/widgets/__tests__/imageWidget.test.ts` | `ImageWidget` 的测试 | 用 `vi.mock` 整模块替换 + 异步状态机测试 |
| `src/widgets/codeBlockWidget.ts` | 被测的 `CodeBlockWidget` 源码 | `eq`/`toDOM`/`ignoreEvent` 的实现 |
| `src/utils/inlineMarkdown.ts` | 综合实践的练习对象 | 给它补边界用例测试 |

全仓库共有 **19 个** `__tests__/*.test.ts` 文件，按五层 `core/plugins/widgets/utils/theme` 分布。**读测试是读这个库的最佳捷径之一**——测试就是一份「行为说明书」。

---

## 4. 核心概念与源码讲解

### 4.1 Vitest + jsdom：测试环境与配置

#### 4.1.1 概念说明

这个库被测的对象有两大类：

1. **纯逻辑**（`shouldShowSource`、`parseMarkdownTable`、`tableSerializer`）：只依赖数据，不碰 DOM。这类测试理论上 Node 也能跑。
2. **DOM 相关**（`livePreviewPlugin`、`markdownStylePlugin`、所有 `WidgetType.toDOM()`）：会调用 `document.createElement`、`dom.querySelector` 等。没有 DOM 直接报错。

Vitest 的 `environment` 选项决定测试运行时「`document`、`window`、`navigator` 这些全局变量存不存在」。选 `'jsdom'` 后，Vitest 会在每个测试文件执行前注入一套用 JS 模拟的浏览器环境，让两类对象都能跑。这就是为什么全仓库只配了一个统一环境，而不需要为每个文件单独选环境。

#### 4.1.2 核心流程

配置项的协作关系可以用下面这张「数据流」描述：

```
vitest 启动
  └─ 读 vitest.config.ts
       ├─ environment: 'jsdom'  → 注入 document/window/navigator
       ├─ globals: true         → describe/it/expect 免 import 也可用（本项目仍显式 import）
       ├─ exclude: [...]        → 跳过 demo/dist/ProseMark 目录
       └─ coverage(v8)          → 生成覆盖率报告
```

两个最值得记住的字段：

- **`environment: 'jsdom'`**：没有它，`new EditorView(...)` 会因为找不到 `document` 而抛错；
- **`globals: true`**：开启后 `it`/`describe`/`expect` 会变成全局函数，可以不 import 直接用。本项目测试文件**仍然显式 `import { describe, it, expect } from 'vitest'`**（见各测试首行），这是一种更严谨的写法——既享受 `globals` 的便利，又保证类型提示完整、避免隐式全局的歧义。

#### 4.1.3 源码精读

整个配置非常短，集中在 [vitest.config.ts:4-14](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/vitest.config.ts#L4-L14)，这段代码做四件事：`globals:true` 让全局 API 可用、`environment:'jsdom'` 提供 DOM、`exclude` 把 `demo`（一个完整的 vite 子项目）和 `ProseMark`（一个独立目录）排除出测试扫描、`coverage` 用 `v8` provider 产出 `text/json/html` 三种报告。

对应的 npm 脚本（见 `package.json` 的 `scripts`）形成一条测试链：

| 脚本 | 命令 | 用途 |
| --- | --- | --- |
| `npm test` | `vitest run` | **跑一次**全部测试（CI/本讲实践用） |
| `npm run test:watch` | `vitest` | 监听模式，改文件自动重跑 |
| `npm run test:coverage` | `vitest run --coverage` | 跑测试并产出覆盖率 |

> 易错点：`vitest run` 与 `vitest`（不带 `run`）不同——前者只跑一次就退出（适合 CI 和脚本），后者进入 watch 常驻。本讲实践一律用 `npm test`。

#### 4.1.4 代码实践

1. **实践目标**：确认测试环境能正常启动，并看到 19 个测试文件全部被执行。
2. **操作步骤**：
   - 在项目根目录执行 `npm install`（首次需要）。
   - 执行 `npm test`。
3. **需要观察的现象**：终端输出 `Test Files` 与 `Tests` 两行汇总，以及每个 `__tests__` 文件的绿色对勾。
4. **预期结果**：所有测试文件被收集、`Tests` 全部 passed（0 failed）。
5. **如果 lowlight 未安装**：少数高亮相关测试可能打印 `Highlighter not available ... skipped` 警告但**仍判通过**（见 4.3 节的优雅降级），这不影响测试整体绿色。若环境异常导致失败，请先确认 `npm install` 完成。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `environment` 从 `'jsdom'` 改成 `'node'`，`livePreview.test.ts` 会发生什么？

> **参考答案**：`createTestView` 里的 `new EditorView({ state, parent: container })` 以及 `document.createElement('div')` 会因为 `document` 未定义而抛 `ReferenceError`，该文件下所有测试报错失败。这正是为什么 DOM 相关测试必须用 jsdom。

**练习 2**：`globals: true` 已经让 `it` 全局可用，为什么本项目测试文件还要 `import { describe, it, expect } from 'vitest'`？

> **参考答案**：显式 import 能让 TypeScript 提供完整的类型提示与跳转，也让依赖关系一目了然，属于「既享受便利又保持严谨」的写法，不依赖隐式全局。

---

### 4.2 EditorView 构造与装饰捕获：验证 ViewPlugin 行为

#### 4.2.1 概念说明

测试 CodeMirror 插件有**两种深浅不同的范式**，本节把它们对比清楚：

- **浅范式（只需 State）**：被测对象是纯函数或只读 `EditorState`（如 `shouldShowSource`）。你只要 `EditorState.create({ doc, selection, extensions })` 造一个状态，直接调用函数断言即可，**不需要 `EditorView`，不需要真实 DOM**。
- **深范式（需要 View）**：被测对象是 `ViewPlugin`（如 `livePreviewPlugin`），它的 `build`/`update` 要依赖视口、要被 CodeMirror 实例化、要把装饰挂到 `view` 上。这时必须造一个真正的 `EditorView`，再从 `view.plugin(插件对象)` 取回它的 `decorations` 来断言。

`livePreview.test.ts` 和 `shouldShowSource.test.ts` 分别是这两种范式的范本，对照阅读收益最大。

#### 4.2.2 核心流程

深范式的「构造 → 捕获 → 断言」三步流程：

```
1. 构造
   EditorState.create({ doc, selection, extensions: [markdown(), mouseSelectingField, collapseOnSelectionFacet.of(true), livePreviewPlugin] })
   └─ new EditorView({ state, parent: 一个临时的 div })
2. 捕获
   const plugin = view.plugin(livePreviewPlugin)
   └─ plugin.decorations  // 一个 DecorationSet
3. 断言
   plugin.decorations.between(0, doc.length, (from, to, deco) => { 检查 deco.spec.class })
   或直接 plugin.decorations.size
```

关键细节：**`view.plugin(插件)` 返回该插件的实例句柄**，上面挂着的 `decorations` 就是它在当前状态下产出的装饰集合。`DecorationSet` 提供 `.between(from, to, callback)` 方法遍历区间内的所有装饰，回调参数是 `(from, to, decoration)`，其中 `decoration.spec` 携带你当初 `Decoration.mark({ class: ... })` 写进去的 `class`。测试就靠读 `spec.class` 来判断「标记有没有被加上 visible 类」。

#### 4.2.3 源码精读

**先看浅范式**——`shouldShowSource` 的测试根本不碰 DOM。它只造 `EditorState` 就够了：

[shouldShowSource.test.ts:9-15](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/shouldShowSource.test.ts#L9-L15) 演示「Live Preview 关闭（facet 投递 `false`）时永远返回 `false`」这条分支；再看 [shouldShowSource.test.ts:41-49](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/shouldShowSource.test.ts#L41-L49) 与 [shouldShowSource.test.ts:51-59](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/shouldShowSource.test.ts#L51-L59)，分别断言「光标在区间内」「选区与区间相交」时返回 `true`。这些用例把 `shouldShowSource` 的三步短路逻辑（见 `u2-l2`）逐条钉死，是「用最小状态测纯函数」的标准写法。

**再看深范式**——`livePreview.test.ts` 的工厂函数 `createTestView`：

[livePreview.test.ts:12-27](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/livePreview.test.ts#L12-L27) 把「装好必备扩展 + 挂到一个临时 div」封装成一个函数，`cursorPos` 参数控制初始光标位置。注意四件扩展缺一不可：`markdown()` 给语法树、`mouseSelectingField` 提供拖拽状态、`collapseOnSelectionFacet.of(true)` 打开总开关、`livePreviewPlugin` 是被测对象本身——这正是 `u1-l4` 讲过的「最小核心扩展组合」。

捕获装饰的范本在 [livePreview.test.ts:121-130](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/livePreview.test.ts#L121-L130)：用 `decorations.between` 全区间遍历，把 `deco.spec.class` 强转为 `{ class?: string }` 再判断是否包含 `cm-formatting-inline`。这段是「在测试里读出 ViewPlugin 真实产出」的核心套路，后续 `livePreview.test.ts` 里所有断言（visible 类、block 类、计数）都复用它。

#### 4.2.4 代码实践

1. **实践目标**：动手验证「光标位置如何改变装饰的 class」这一核心行为，熟悉 `between` 遍历。
2. **操作步骤**：
   - 复制 `livePreview.test.ts` 的 `createTestView` 工厂到一个临时测试文件（例如 `src/plugins/__tests__/_explore.test.ts`，**注意实践后删除，不要提交**）。
   - 写两个用例：文档都设为 `**bold** text`，一个 `createTestView(doc, 4)`（光标在粗体内），一个 `createTestView(doc, 13)`（光标在文末）。
   - 在两个用例里都用 `between(0, 8, ...)` 遍历前 8 个字符的装饰，统计 class 含 `visible` 的条数。
3. **需要观察的现象**：光标在粗体内时，`**` 标记的 mark 装饰带 `visible` 类（标记可见可编辑）；光标在文末时则不带。
4. **预期结果**：前者 `hasVisible` 为 `true`，后者为 `false`。这与 `u2-l2`/`u2-l4` 的讲解一致。
5. **若不确定**：可对照 [livePreview.test.ts:113-155](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/livePreview.test.ts#L113-L155) 里现成的两个对照用例，预期行为已写在断言里。

#### 4.2.5 小练习与答案

**练习 1**：为什么测 `shouldShowSource` 不需要 `new EditorView`，而测 `livePreviewPlugin` 必须 `new EditorView`？

> **参考答案**：`shouldShowSource` 是纯函数，只读传入 `state` 的 `facet`/`field`/`selection`，`EditorState.create(...)` 足以提供这些；而 `livePreviewPlugin` 是 `ViewPlugin`，它的 `build` 要由 CodeMirror 在视图层实例化、装饰要挂在 `view` 上，没有真实 `EditorView` 就拿不到 `view.plugin(...).decorations`。

**练习 2**：`decorations.between(from, to, cb)` 的回调第三个参数 `deco`，怎么拿到当初 `Decoration.mark({ class: 'x' })` 里写的 `class`？

> **参考答案**：读 `deco.spec.class`。`Decoration` 对象的 `spec` 字段保留了构造时传入的选项对象，测试里通常 `const spec = deco.spec as { class?: string }` 强转后取用。

---

### 4.3 mock clipboard 与 loadImage：获得确定性测试

#### 4.3.1 概念说明

很多被测代码会触碰「真实世界」：复制按钮会调浏览器剪贴板、图片加载会发网络请求、超时会等真实秒数。这些在测试里都不可控、慢、且不稳定。**mock 的目的就是把它们换成确定、瞬时、可观测的替身**。

本项目用到**三种不同粒度**的 mock 手法，分清它们是本节的核心：

| 手法 | 替换对象 | 代码位置 | 适用场景 |
| --- | --- | --- | --- |
| `Object.defineProperty(navigator, 'clipboard', {...})` | 浏览器 API 属性 | `codeBlockWidget.test.ts` | 替换单个宿主对象属性 |
| `vi.mock('模块路径', 工厂)` | 整个 ES 模块 | `imageWidget.test.ts` | 替换被测代码 `import` 的依赖 |
| 替换 `globalThis.Image = class {...}` | 全局构造函数 | `imageLoader.test.ts` | 替换 `new Image()` 的真实行为 |

此外还有一个「时间」层面的 mock：`vi.useFakeTimers()` 让 `setTimeout` 立即推进，用来测超时逻辑而不必真等。

#### 4.3.2 核心流程

**手法 A——mock `navigator.clipboard`**（`codeBlockWidget.test.ts`）：

```
1. 模块顶层定义 mockClipboard = { writeText: vi.fn().mockResolvedValue(undefined) }
2. Object.defineProperty(navigator, 'clipboard', { value: mockClipboard, writable: true })
3. 测试里点击复制按钮 → 断言 mockClipboard.writeText.toHaveBeenCalledWith('预期代码')
4. 失败场景：mockClipboard.writeText.mockRejectedValueOnce(new Error(...)) 单次抛错
```

**手法 B——`vi.mock` 整模块**（`imageWidget.test.ts`）：因为 `ImageWidget` 内部 `import { loadImage } from '../../utils/imageLoader'`，测试不想真去加载图片，于是用 `vi.mock` 把整个 `imageLoader` 模块替换成一个工厂函数返回的假对象。

**手法 C——替换全局 `Image`**（`imageLoader.test.ts`）：`loadImage` 内部 `new Image()`，测试用一个自己写的 `MockImage` 类替换全局 `Image`，在构造函数里用 `setTimeout` 模拟「10ms 后加载完成/失败」。

**时间 mock**：测超时时用 `vi.useFakeTimers()` + `vi.advanceTimersByTime(150)`，把「等 100ms 超时」压缩成同步推进。

#### 4.3.3 源码精读

**手法 A 的范本**：[codeBlockWidget.test.ts:10-17](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L10-L17) 在模块顶层（任何 `describe` 之外）定义 `mockClipboard` 并用 `Object.defineProperty` 装到 `navigator` 上，`writable: true` 保证后续能改回。这样全文件所有测试的 `navigator.clipboard` 都是这个桩。配套的「优雅降级」测试在 [codeBlockWidget.test.ts:206-244](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L206-L244)：先备份 `navigator.clipboard` 与 `document.execCommand`，把 `clipboard` 设成 `undefined` 模拟「无剪贴板 API」、给 `execCommand` 挂 `vi.fn().mockReturnValue(true)`，验证复制按钮会回退到 `execCommand('copy')`，最后在 `finally` 里恢复原值——这种「备份→篡改→finally 恢复」是改全局状态时**必须遵守的卫生规范**，否则会污染其它测试。

复制成功/失败的断言用 [codeBlockWidget.test.ts:144-157](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L144-L157)：点击按钮后直接断言 `mockClipboard.writeText.toHaveBeenCalledWith('const x = 1;')`——这正是 mock 的「可观测」收益，你能验证「调没调、参数对不对」。

**手法 B 的范本**：[imageWidget.test.ts:14-32](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/imageWidget.test.ts#L14-L32) 用 `vi.mock('../../utils/imageLoader', () => ({...}))`，工厂返回的 `loadImage` 根据 `src` 是否含 `'error'` 立即 `Promise.resolve` 成功或失败结果。由于 `loadImage` 是 `toDOM` 内部异步触发的，测试需要等它 settle，于是用 [imageWidget.test.ts:58-68](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/imageWidget.test.ts#L58-L68) 的 `await new Promise(resolve => setTimeout(resolve, 50))` 让微任务/宏任务跑完再去 `dom.querySelector('img')`。

**手法 C + 时间 mock 的范本**：[imageLoader.test.ts:14-33](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L14-L33) 定义 `MockImage`，构造函数里 `setTimeout` 10ms 后按 `src` 是否含 `error` 触发 `onload` 或 `onerror`；[imageLoader.test.ts:39-47](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L39-L47) 在 `beforeEach` 里 `clearImageCache()` 并替换全局 `Image`、在 `afterEach` 里恢复原 `Image` 并 `vi.clearAllTimers()`——又是「篡改→恢复」卫生。超时测试见 [imageLoader.test.ts:79-103](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/imageLoader.test.ts#L79-L103)：用一个永不触发的 `SlowImage` 配合 `vi.useFakeTimers()` + `advanceTimersByTime(150)`，让 100ms 的超时分支瞬间触发，最后断言 `result.error` 含 `'timeout'` 并 `vi.useRealTimers()` 收尾。

> 对比 B 与 C：B 替换的是「模块导出的函数」，连 `loadImage` 的内部实现都跳过了；C 替换的是「全局 `Image` 构造器」，让 `loadImage` **真实跑一遍**只是底层 `new Image()` 被换皮。测 `imageLoader` 本身只能用 C，因为它就是被测对象；测 `ImageWidget` 用 B 更省事，因为关心的不是 `loadImage` 而是 widget 如何消费结果。

#### 4.3.4 代码实践

1. **实践目标**：体验「mock 失败 → 断言降级」的完整闭环。
2. **操作步骤**：
   - 仿照 [codeBlockWidget.test.ts:181-204](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L181-L204) 的失败用例，新建一个测试：先用 `mockClipboard.writeText.mockRejectedValueOnce(new Error('denied'))` 让复制抛错，再点击按钮。
   - 用 `await vi.waitFor(() => expect(copyBtn.textContent).toBe('Failed'))` 等待异步失败态。
3. **需要观察的现象**：即便剪贴板拒绝，按钮也不会抛未捕获异常，而是显示 `Failed` 文案。
4. **预期结果**：按钮文案变为 `Failed`，对应源码 `codeBlockWidget.ts` 复制逻辑里的 `catch` 分支（见 `u5-l3`）。
5. **关键观察**：`vi.waitFor` 会反复重试断言直到通过或超时——处理「异步但不确定何时 settle」的标准工具，比固定 `setTimeout(50)` 更稳健。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `imageLoader.test.ts` 用「替换全局 `Image`」而不是 `vi.mock` 整个模块？

> **参考答案**：`imageLoader` 本身就是被测对象，`vi.mock` 整模块会把它替换成假实现，那样测的就不是真代码了。替换全局 `Image` 则让 `loadImage` 的真实逻辑跑一遍，只把最底层的 `new Image()` 换成可控的桩，既隔离了网络又测到了真实流程。

**练习 2**：`codeBlockWidget.test.ts` 的 mock 在模块顶层定义、`imageLoader.test.ts` 在 `beforeEach` 里替换，两种位置各有什么含义？

> **参考答案**：clipboard 桩是「全文件恒定」的（writeText 默认总成功），放模块顶层最简单，个别失败用 `mockRejectedValueOnce` 临时覆盖；全局 `Image` 桩需要每个测试前清缓存、每测试后恢复原值，放 `beforeEach`/`afterEach` 才能保证测试隔离。选择哪种位置取决于「桩是否需要每用例重置」。

---

### 4.4 widget.eq 测试：对应 CodeMirror 重渲染优化

#### 4.4.1 概念说明

回顾 `u3-l1`：`WidgetType` 三件套是 `toDOM / eq / ignoreEvent`。其中 `eq` 是**性能优化的闸门**：

- 当文档/视口变化导致某段 widget 装饰被重算时，CodeMirror 会构造**新 widget 实例**，再调用 `newWidget.eq(oldWidget)`。
- 返回 `true` → 复用旧 widget 的 DOM 节点，**不重新 `toDOM`**。
- 返回 `false` → 销毁旧 DOM，调用新 widget 的 `toDOM` 重建。

所以 `eq` 的正确性直接决定「会不会无谓地重建 DOM」。一个常犯的错是：`eq` 漏比某个字段，导致 `toDOM` 输出变了却仍判相等，DOM 不刷新——视觉 bug；或 `eq` 多比了无关字段，导致每次都重建——白费性能（回顾 `u3-l1` 的心法：`eq` 比较的字段集合须等于决定 `toDOM` 输出的字段集合）。

测试 `eq` 极其简单：**直接构造两个 widget 实例，调用 `widget1.eq(widget2)` 断言布尔值**——不需要 `EditorView`、不需要 DOM 渲染。这是单元测试里最纯粹的一类。

#### 4.4.2 核心流程

`eq` 测试的固定三步：

```
1. 造两份 CodeBlockData（或 ImageData）
2. 各自 new CodeBlockWidget(data) / new ImageWidget(...)
3. 断言 widget1.eq(widget2) === 期望值
   - 数据完全相同 → true（可复用）
   - 任一关键字段不同 → false（必须重建）
```

本项目测了两个 widget 的 `eq`，模式一致但字段不同：

- `CodeBlockWidget.eq` 比 `code / language / showLineNumbers / showCopyButton / showSourceToggle / from`；
- `ImageWidget.eq` 比 `src / alt / title`（options 由字段实例缓存保证恒定，见 `u6-l1`）。

#### 4.4.3 源码精读

先看被测实现 [codeBlockWidget.ts:47-56](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L47-L56)：`eq` 只比较 `code/language/showLineNumbers/showCopyButton/showSourceToggle/from` 六个字段，**刻意省略了派生字段 `to` 和 `lineStarts`**。原因正如 `u5-l3` 所讲：`to`/`lineStarts` 是从文档位置派生的，只要决定渲染外观的六字段没变，DOM 就不必重建。

测试对照实现逐字段验证。[codeBlockWidget.test.ts:247-285](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L247-L285) 的 `equality` describe 块有四个用例：

- 「identical data → true」（[248-255](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L248-L255)）；
- 「different code → false」（[257-262](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L257-L262)）；
- 「different language → false」（[264-273](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L264-L273)）；
- 「different options → false」（[275-284](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L275-L284)）。

注意 `createTestData(overrides)` 工厂（[codeBlockWidget.test.ts:22-36](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/codeBlockWidget.test.ts#L22-L36)）：用「默认值 + Partial 覆盖」构造数据，既保证基线一致，又能精准只改一个字段——这正是 `eq` 测试能逐字段排查的前提。

异步 widget 的 `eq` 范本在 [imageWidget.test.ts:215-252](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/__tests__/imageWidget.test.ts#L215-L252)：`ImageWidget` 内部有 loading→loaded/error 的异步状态机，但 `eq` 只比 `src/alt/title` 这些**初始数据**，与异步结果无关。所以测 `eq` 时**不必 `await` 加载完成**——直接构造两个 widget 比较即可，断言的是「CodeMirror 层面的复用判定」，不是「加载后的 DOM」。

> 把 4.4 与 4.2 联起来看：`widget.eq` 是「不等就不重建」的优化，而 `toDOM` 是「重建时怎么造 DOM」。测试也分层——`eq` 用最轻的纯断言测「要不要重建」，`toDOM` 用 mock（4.3）+ DOM 查询测「造出来对不对」。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证「`eq` 省略派生字段不会误判」，加深对优化闸门的理解。
2. **操作步骤**：
   - 用 `createTestData` 造两个 widget：`w1 = createCodeBlockWidget(createTestData({ from: 0, lineStarts: [14] }))`，`w2 = createCodeBlockWidget(createTestData({ from: 0, lineStarts: [999] }))`——即 `from` 相同但 `lineStarts` 不同。
   - 断言 `expect(w1.eq(w2)).toBe(true)`。
3. **需要观察的现象**：尽管 `lineStarts` 完全不同，`eq` 仍返回 `true`。
4. **预期结果**：断言通过。这印证了源码 [codeBlockWidget.ts:47-56](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L47-L56) 刻意不比 `lineStarts` 的设计。
5. **进一步思考**：若哪天发现「点击定位错位但代码内容没变」，要警惕是不是某次重建被 `eq` 错误跳过了——这就是「`eq` 漏比导致不刷新」的典型症状。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CodeBlockWidget.eq` 不比较 `to` 和 `lineStarts`，却比较 `from`？

> **参考答案**：`from` 标识代码块在文档中的身份位置，块整体移动（`from` 变了）意味着渲染上下文变了，应重建；而 `to`/`lineStarts` 是从位置派生、仅服务于点击定位的派生量，不影响 `toDOM` 产出的视觉外观，比了只会导致无谓重建，故省略。这正是 `u3-l1` 「eq 字段集合 = 决定 toDOM 输出的字段集合」心法的体现。

**练习 2**：测 `ImageWidget.eq` 时为什么不需要 `await` 加载完成？

> **参考答案**：`eq` 只比较构造时传入的初始数据（`src/alt/title`），与异步加载后的 loading/loaded/error 状态无关。CodeMirror 调用 `eq` 的时机也是在重算装饰时、加载发生之前，所以测试直接构造两实例比较即可，无需等待异步 settle。

---

## 5. 综合实践

把本讲四种手法（环境、构造 View 捕获装饰、mock、`eq` 断言）串起来，完成一个**真实可提交**的小任务：为 `renderInlineMarkdown` 或 `parseLinkSyntax` 选一个，参考现有 `__tests__` 风格补一个**边界用例**测试，并运行 `npm test` 确认通过。

推荐选 `renderInlineMarkdown`（更纯、更容易），步骤如下：

1. **实践目标**：为新场景补一条当前测试未覆盖的边界用例，体会「先想清楚期望行为，再写断言」的测试驱动思路。

2. **操作步骤**：
   - 打开 [src/utils/__tests__/inlineMarkdown.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/__tests__/inlineMarkdown.test.ts)，阅读已有用例（粗体/斜体/转义/行内代码优先级）。
   - 阅读 [src/utils/inlineMarkdown.ts:20-49](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/inlineMarkdown.ts#L20-L49) 理解处理顺序：先 `escapeHtml`，再行内代码占位保护，再 bold/italic/strike/highlight，最后还原代码块。
   - 仿照现有风格新增一个用例，覆盖一个边界场景。例如空字符串：

     ```ts
     it('should return empty string for empty input', () => {
       expect(renderInlineMarkdown('')).toBe('');
     });
     ```

     或未闭合标记（应原样保留、不误解析）：

     ```ts
     it('should leave unclosed markers as plain text', () => {
       expect(renderInlineMarkdown('**unclosed bold')).toBe('**unclosed bold');
     });
     ```

3. **需要观察的现象**：新用例被 Vitest 收集并执行。
4. **预期结果**：运行 `npm test` 后该用例 passed，且不破坏其它测试（总数 +1）。
5. **进阶（可选）**：若想练 mock + 异步，改去 [src/plugins/__tests__/link.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/link.test.ts)，为 `parseLinkSyntax` 补一个危险协议边界（如 `javascript:alert(1)`），断言解析层只认语法、URL 原样保留（安全拦截在渲染层，见 `u6-l3`）。注意该文件已用 `vi.mock('../../widgets/linkWidget', ...)` 替换了 widget，专注于解析逻辑本身。
6. **若不确定行为**：先单独调用 `renderInlineMarkdown('你的输入')` 打印结果，确认期望值后再写进断言——**不要假装已运行**，请实际执行 `npm test` 看红绿。

> 这个任务同时呼应了 `u7-l3` 的「解析与安全分层」结论：解析层对所有输入一视同仁，安全拦截发生在更上层。测试用例就是你把这种分层「白纸黑字」钉死的方式。

## 6. 本讲小结

- **环境**：全仓库测试统一跑在 `vitest.config.ts` 配置的 `jsdom` 环境里，因为 `ViewPlugin`/`WidgetType.toDOM` 必须有 DOM；`globals:true` 让 `it/describe/expect` 全局可用，但项目仍显式 import。
- **两种测试范式**：测纯函数（`shouldShowSource`）只需 `EditorState.create`；测 `ViewPlugin`（`livePreviewPlugin`）必须 `new EditorView` 再用 `view.plugin(...).decorations.between(...)` 捕获装饰、读 `deco.spec.class` 断言。
- **三种 mock 粒度**：`Object.defineProperty(navigator,'clipboard')` 替换宿主属性、`vi.mock` 替换整个依赖模块、替换 `globalThis.Image` 让被测真实代码跑通；改全局状态务必「备份→篡改→finally/afterEach 恢复」；`vi.useFakeTimers` 用来瞬时测超时。
- **异步与确定性**：用 `vi.fn().mockResolvedValue/Rejected` 或 `vi.waitFor` 取代真实网络/剪贴板/延时，把测试变得瞬时、稳定、可观测（能断言「调没调、参数是什么」）。
- **`widget.eq` 测试**：直接 `widget1.eq(widget2)` 断言布尔，是验证 CodeMirror「重渲染优化闸门」最纯粹的方式；`eq` 比较的字段集合须等于决定 `toDOM` 输出的字段集合，本项目刻意省略 `CodeBlockWidget` 的派生字段 `to/lineStarts`。
- **测试即文档**：19 个 `__tests__` 文件按五层分布，读测试是读懂这个库行为的捷径；每个 `eq` 测试、每个 mock 都对应源码里一条可被验证的设计取舍。

## 7. 下一步学习建议

- **承接架构总览**：本讲是测试视角，下一讲 `u7-l3（整体架构与设计取舍）` 会从架构视角把「三视图模式、两种装饰策略、ViewPlugin vs StateField、性能优化（含拖拽跳过重建、KaTeX/图片缓存）」串成一张全局图，建议结合本讲的「`eq` 优化」「mock 加载」一起理解性能取舍。
- **深入被测对象**：想更理解 `CodeBlockWidget.eq` 为何省略 `lineStarts`，可回看 `u5-l3（codeBlockWidget：DOM 渲染、复制按钮与点击定位）`；想理解 `ImageWidget` 的异步状态机，可回看 `u6-l1（imageField）`。
- **动手自定义**：`u7-l4（二次开发：编写自己的 Live Preview 插件）` 会让你综合运用本讲的测试能力——为新插件配套写 `eq` 断言与 mock 测试，形成「写代码 + 写测试」的完整闭环。
- **继续读测试**：挑一个还没细读的测试文件（如 `src/plugins/__tests__/tableEditor.test.ts` 或 `src/core/__tests__/pluginUpdateHelper.test.ts`），用本讲学到的范式去读，验证你是否能独立讲清它验证了什么行为。
