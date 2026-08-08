# 源码目录结构与库入口

## 1. 本讲目标

上一讲我们从文档层面认识了 codemirror-live-markdown 是什么。本讲我们第一次真正打开源码，回答三个问题：

1. **源码是怎么组织的？** —— `src` 下为什么分成 `core / plugins / widgets / utils / theme` 五层，每一层各管什么。
2. **库的入口在哪里？** —— 使用者 `import` 一个名字时，这个名字到底来自哪个文件，`src/index.ts` 是怎么把所有东西汇聚起来的。
3. **依赖是怎么分层的？** —— 哪些是强制依赖，哪些是可选依赖，为什么这样设计。

学完本讲，你应该能在不打开 IDE 自动补全的情况下，准确说出 `livePreviewPlugin`、`mathPlugin`、`codeBlockField`、`imageField` 这四个导出分别来自哪个子模块文件。

## 2. 前置知识

在阅读本讲前，你需要了解几个基础概念（不熟悉没关系，下面会展开）：

- **模块（module）/ 包（package）**：在 JavaScript 生态里，一个 `.ts` 文件就是一个模块，它可以用 `export` 把变量、函数、类型暴露出去，别的文件用 `import` 引入。`package.json` 则声明了整个包的元信息和依赖。
- **Barrel 文件（桶文件）**：把一个目录下多个模块的导出，集中到单个文件里再次 `export`，方便外部一行 `import` 就拿到所有东西。`src/index.ts` 就是这样一个「桶」。
- **依赖（dependency）**：你的代码运行/构建时需要用到的别人发布的包。`dependencies` 是普通运行依赖；`peerDependencies`（对等依赖）表示「我不替你装它，但你的项目必须自己装它」；`peerDependenciesMeta` 里标记 `optional: true` 的，则是「你装了我就用，不装我也不报错」。
- **CodeMirror 6**：本项目是它的上层扩展库。这一讲你只需知道它由 `@codemirror/state`、`@codemirror/view`、`@codemirror/language`、`@codemirror/lang-markdown` 和 `@lezer/markdown` 几个包组成即可，细节后面专门讲。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `src/index.ts` | 库的统一导出入口（barrel 文件），把五层模块的导出按 Core / Plugins / Theme / Utils 分类汇总，再转手暴露给使用者。 |
| `package.json` | 声明包名、版本、入口（`main`/`module`/`types`/`exports`）、构建脚本，以及依赖分层（`peerDependencies` + `peerDependenciesMeta`）。 |
| `tsconfig.json` | TypeScript 编译配置，决定 `src` 如何被编译成 `dist`，是理解「源码目录 → 发布产物」的关键。 |

为了说明分层，我们还会「路过」每一层的一个代表文件（`core/facets.ts`、`plugins/livePreview.ts`、`widgets/mathWidget.ts`、`utils/mathCache.ts`、`theme/default.ts`），但只看它们的开头注释与 import，不深入实现。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 src 分层结构**：五层各管什么，依赖方向如何。
- **4.2 index.ts 统一导出**：桶文件如何把五层汇成一张对外清单。
- **4.3 peerDependencies 与可选依赖**：依赖分层的硬约定。

### 4.1 src 分层结构

#### 4.1.1 概念说明

一个 Live Preview 编辑器要同时做很多事：决定「显示源码还是渲染结果」、遍历语法树、生成 DOM、缓存计算结果、定义样式。如果全部塞在一个文件里会变成一团乱麻。这个项目用「分层」来解决——把职责相近的代码放到同一个目录，目录之间只允许单向依赖，从而保持清晰。

`src` 下一共五个子目录，每个目录就是一层：

```
src/
├── index.ts                      # 统一导出入口（本讲重点）
├── core/                         # 第 1 层：核心机制（无 UI）
│   ├── facets.ts
│   ├── mouseSelecting.ts
│   ├── shouldShowSource.ts
│   └── pluginUpdateHelper.ts
├── plugins/                      # 第 2 层：装饰提供者（ViewPlugin / StateField）
│   ├── livePreview.ts
│   ├── markdownStyle.ts
│   ├── math.ts
│   ├── table.ts
│   ├── tableEditor.ts
│   ├── tableEditorEffects.ts
│   ├── codeBlock.ts
│   ├── codeBlockEffects.ts
│   ├── image.ts
│   └── link.ts
├── widgets/                      # 第 3 层：DOM 渲染单元（WidgetType 子类）
│   ├── mathWidget.ts
│   ├── tableWidget.ts
│   ├── tableEditorWidget.ts
│   ├── codeBlockWidget.ts
│   ├── imageWidget.ts
│   └── linkWidget.ts
├── utils/                        # 第 4 层：纯函数工具与缓存
│   ├── mathCache.ts
│   ├── codeHighlight.ts
│   ├── imageLoader.ts
│   ├── tableParser.ts
│   ├── tableSerializer.ts
│   └── inlineMarkdown.ts
└── theme/                        # 第 5 层：主题样式
    └── default.ts
```

各层的职责一句话总结：

| 层 | 目录 | 职责 | 是否含 UI/DOM |
| --- | --- | --- | --- |
| 核心机制 | `core/` | 提供「开关」「拖拽状态」「显示源码判定」「更新动作判定」等被各插件复用的纯逻辑 | 否 |
| 装饰提供者 | `plugins/` | 遍历语法树，产出 Decoration（CodeMirror 的装饰对象），决定哪里渲染、哪里显示源码 | 间接（通过 Widget） |
| DOM 渲染单元 | `widgets/` | `WidgetType` 子类，把一段 Markdown 渲染成真实 DOM 元素 | 是 |
| 工具与缓存 | `utils/` | 纯函数：解析、序列化、KaTeX 渲染缓存、代码高亮、图片加载等 | 否（但产出 HTML/DOM） |
| 主题 | `theme/` | 用 `EditorView.theme` 定义 CSS 样式与动画 | 是（CSS） |

#### 4.1.2 核心流程：依赖方向

分层的关键不是「分了多少目录」，而是**目录之间只允许单向依赖**。通过观察每层文件的 `import` 语句，可以画出依赖方向：

```
        core  ←──────────── plugins ──────────→ widgets ──→ utils
        （被复用的纯逻辑）  （装饰编排者）       （DOM 单元）   （纯函数/缓存）
                                                                    ↑
        theme（独立，只依赖 @codemirror/view）                        │
                                                                    │
        index.ts（汇聚全部，不参与内部依赖，只 re-export）            │
```

规律是：

- `plugins` 既依赖 `core`（拿决策逻辑），又依赖 `widgets` 和 `utils`（拿渲染能力）。
- `widgets` 依赖 `utils`（比如 `mathWidget` 调 `mathCache` 的 `renderMath`）。
- `core` 和 `utils` 处于底层，**不依赖**本项目其它层，只依赖 CodeMirror/第三方，所以它们最容易被单独测试、单独复用。
- `theme` 相对独立，只用到 `@codemirror/view`。
- `index.ts` 不写业务逻辑，只做「转发」，因此它「看到」所有层但**不被任何层依赖**。

这种「核心逻辑下沉、编排逻辑居中、渲染与样式靠上」的结构，是这个库模块化理念的直接体现。

#### 4.1.3 源码精读

我们用每一层最短、最能体现职责的文件来印证上面的结论。

**core 层 —— 纯逻辑，只依赖 `@codemirror/state`。** 看 `src/core/facets.ts` 的开头：它定义了一个开关型 Facet（`collapseOnSelectionFacet`），决定 Live Preview 是否启用，整个文件没有 DOM、没有 UI，只有 `Facet.define`。

[src/core/facets.ts:L1-L10](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/facets.ts#L1-L10) —— 导入 `Facet` 并定义 `collapseOnSelectionFacet`，体现 core 层「只放被复用的纯逻辑」。

**plugins 层 —— 编排者，同时 import core + widgets + utils。** 看 `src/plugins/livePreview.ts` 的导入段：它从 `@codemirror/*` 拿基础设施，从 `../core/*` 拿决策函数（`shouldShowSource`、`mouseSelectingField`、`checkUpdateAction`），印证了「插件依赖核心」。

[src/plugins/livePreview.ts:L1-L13](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L1-L13) —— 同时引入 CodeMirror、core 层决策逻辑，是「编排者」的典型 import 结构。

**widgets 层 —— 渲染单元，依赖 utils。** 看 `src/widgets/mathWidget.ts`：它继承 `WidgetType`，并在渲染时调用 `../utils/mathCache` 的 `renderMath`，印证了「widget 依赖 utils」。

[src/widgets/mathWidget.ts:L1-L9](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/mathWidget.ts#L1-L9) —— 继承 `WidgetType` 并引入 `renderMath`，体现 widgets 层「把数据渲染成 DOM」的职责。

**utils 层 —— 纯函数与缓存，尽量不依赖 CodeMirror。** 看 `src/utils/mathCache.ts`：它维护一个 `Map` 缓存，键格式为 `"inline:formula"` 或 `"block:formula"`，动态 `import` KaTeX，是一个自给自足的工具模块。

[src/utils/mathCache.ts:L1-L12](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts#L1-L12) —— 用 `Map` 做渲染缓存，键设计清晰，体现 utils 层「纯函数 + 缓存」定位。

**theme 层 —— 独立样式，只依赖 `@codemirror/view`。** 看 `src/theme/default.ts`：它用 `EditorView.theme({...})` 以对象形式定义 CSS（包括行内/块级标记的过渡动画），与业务逻辑完全解耦。

[src/theme/default.ts:L1-L13](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L1-L13) —— 用 `EditorView.theme` 定义默认主题，体现 theme 层「只管样式」的独立性。

#### 4.1.4 代码实践

**实践目标**：亲手验证「分层只允许单向依赖」这条规律，而不是只听结论。

**操作步骤**：

1. 在项目根目录，用搜索工具统计每个子目录的 `import '../`（相对路径导入本项目其它层）语句。
2. 重点检查「底层是否反向依赖了上层」——也就是看 `core/` 和 `utils/` 里有没有出现 `import ... from '../plugins'` 或 `from '../widgets'`。

**需要观察的现象**：

- `core/` 下文件只会出现 `from '@codemirror/...'`，绝不会出现 `from '../plugins'`、`from '../widgets'`。
- `utils/` 下文件大多不引用本项目的其它层（个别会引用 CodeMirror 类型，但不会反向依赖 plugins/widgets）。
- `plugins/` 下文件则会同时引用 `../core` 与 `../widgets`、`../utils`。

**预期结果**：底层不依赖上层，依赖箭头始终「从上指向下」（plugins → core/widgets/utils，widgets → utils），符合 4.1.2 的依赖方向图。如果某天发现 `core/` 反向 import 了 `plugins/`，那就是一次需要警惕的架构倒退。

> 说明：本实践是「源码阅读型实践」，不运行命令，只通过阅读 import 关系得出结论。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `shouldShowSource`（目前在 `core/`）移到 `plugins/livePreview.ts` 里，会破坏分层吗？为什么？

**参考答案**：会破坏分层。`shouldShowSource` 是被 `math.ts`、`image.ts`、`table.ts` 等多个插件共同复用的决策逻辑，属于「被复用的纯逻辑」。放进某个具体插件后，其它插件就要反向依赖 `plugins/livePreview`，依赖箭头会从「插件 → 核心」变成「插件 → 插件」，分层被打破。这正是它被放在 `core/` 的原因。

**练习 2**：`mathCache.ts` 为什么放在 `utils/` 而不是 `widgets/mathWidget.ts` 里？

**参考答案**：因为缓存是「与渲染无关的纯计算」，并且未来可能被多处复用（行内公式、块级公式都要用它）。放在 `utils/` 让它成为无 DOM 依赖的底层工具，既符合分层，也便于单独测试。

---

### 4.2 index.ts 统一导出

#### 4.2.1 概念说明

使用者拿到这个库后，通常只会写类似下面这样的代码：

```ts
// 示例代码（说明使用者视角）
import { livePreviewPlugin, editorTheme } from 'codemirror-live-markdown';
```

但 `livePreviewPlugin` 实际定义在 `src/plugins/livePreview.ts`，`editorTheme` 定义在 `src/theme/default.ts`。使用者怎么用一行 `import` 就同时拿到两个不同文件里的东西？答案就是 **`src/index.ts`**：它是一个「桶文件（barrel）」，把分散在各层的导出，按类别 `re-export`（再次导出）一遍，对外形成一张统一清单。

`package.json` 里把 `src/index.ts`（编译后的 `dist/index.js` / `dist/index.cjs`）指定为包入口，于是 `import 'codemirror-live-markdown'` 就等价于「进入 `src/index.ts` 拿东西」。

#### 4.2.2 核心流程：分类汇总

`src/index.ts` 把所有导出分成 **五大块**，顺序如下：

1. **Core** —— core 层的决策逻辑与类型。
2. **Plugins** —— 各装饰插件及其配置类型。
3. **Theme** —— 默认主题。
4. **Utils** —— 工具函数与类型。
5. **便捷转出** —— 把 CodeMirror 自带的 `Extension`、`EditorView` 类型再转手导出一次，省得使用者额外装包找类型。

整体流程可以用伪代码表示：

```
src/index.ts 重新导出：
  Core   ← core/facets, core/mouseSelecting, core/shouldShowSource, core/pluginUpdateHelper
  Plugins← plugins/{livePreview, markdownStyle, math, table, tableEditor, codeBlock, image, link}
  Theme  ← theme/default
  Utils  ← utils/{mathCache, codeHighlight, imageLoader}
  便捷   ← @codemirror/state (Extension), @codemirror/view (EditorView)
```

注意一个细节：`export` 有两种形式——

- `export { X } from './path'`：转发一个**值**（函数、常量、类）。
- `export type { X } from './path'`：只转发一个**类型**（编译后会被擦除，不进运行时产物）。

`index.ts` 里凡是 `export type { ... }` 的，都是 TypeScript 类型（如 `UpdateAction`、`CodeBlockOptions`、`ImageOptions`、`LinkOptions`、`HighlightResult`、`LoadedImage` 等），它们在运行时不存在，只是给使用者做类型提示。

#### 4.2.3 源码精读

打开 `src/index.ts`，逐段对应到「分类」。文件最顶部的 `@packageDocumentation` 注释表明这个文件本身就是整个包的文档入口。

[src/index.ts:L1-L8](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L1-L8) —— 文件头部注释，标注这是整个包的入口文档。

**第一块：Core 导出**（值与类型各占一行）：

[src/index.ts:L9-L14](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L9-L14) —— 转发 `collapseOnSelectionFacet`、`mouseSelectingField`、`setMouseSelecting`、`shouldShowSource`、`checkUpdateAction`，以及类型 `UpdateAction`。

**第二块：Plugins 导出**（含各插件函数与其配置类型）：

[src/index.ts:L16-L31](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L16-L31) —— 转发 `livePreviewPlugin`、`markdownStylePlugin`、`mathPlugin`/`blockMathField`、`tableField`、`tableEditorPlugin` 等，以及 `CodeBlockOptions`、`ImageOptions`、`LinkOptions` 等配置类型。

**第三块：Theme 导出**：

[src/index.ts:L33-L34](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L33-L34) —— 转发唯一的 `editorTheme`。

**第四块：Utils 导出**：

[src/index.ts:L36-L41](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L36-L41) —— 转发 `renderMath`/`clearMathCache`、代码高亮一族、图片加载一族，以及 `HighlightResult`、`LoadedImage`、`LoadImageOptions` 类型。

**第五块：便捷类型转出**：

[src/index.ts:L43-L45](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/index.ts#L43-L45) —— 把 CodeMirror 的 `Extension`、`EditorView` 类型再导出一次，方便使用者。

这就是「桶文件」的全部魔法：没有一句业务逻辑，全是 `export ... from`。

#### 4.2.4 代码实践（本讲必做）

**实践目标**：在 `src/index.ts` 中追踪四个导出（`livePreviewPlugin`、`mathPlugin`、`codeBlockField`、`imageField`）分别来自哪个子模块文件，并画一张「导出关系图」。

**操作步骤**：

1. 打开 `src/index.ts`，逐个找到这四个名字对应的 `from '...'` 路径。
2. 把相对路径还原成实际文件（例如 `from './plugins/livePreview'` → `src/plugins/livePreview.ts`）。
3. 打开每个目标文件的开头注释，确认它确实 `export` 了这个名字。
4. 用一张图把「对外名字 → 源文件」的关系画出来。

**需要观察的现象**：四个名字全部落在 `src/plugins/` 目录下，但分属四个不同的文件，体现「每个特性一个插件文件」的组织方式。

**预期结果（导出关系图）**：

```
codemirror-live-markdown（包入口 dist/index.js  ←  src/index.ts）
        │
        ├── livePreviewPlugin  ──►  src/plugins/livePreview.ts
        ├── mathPlugin         ──►  src/plugins/math.ts
        ├── codeBlockField     ──►  src/plugins/codeBlock.ts
        └── imageField         ──►  src/plugins/image.ts
```

逐条对照 `src/index.ts`：

| 对外名字 | index.ts 中的来源 | 实际文件 |
| --- | --- | --- |
| `livePreviewPlugin` | `from './plugins/livePreview'` | `src/plugins/livePreview.ts` |
| `mathPlugin` | `from './plugins/math'` | `src/plugins/math.ts` |
| `codeBlockField` | `from './plugins/codeBlock'` | `src/plugins/codeBlock.ts` |
| `imageField` | `from './plugins/image'` | `src/plugins/image.ts` |

> 说明：本实践是「源码阅读型实践」。如果你想进一步动手，可以打开目标文件确认：`src/plugins/math.ts` 在文件注释里明确写了「Inline formulas use ViewPlugin; Block formulas use StateField」，与它同时导出 `mathPlugin`（行内）和 `blockMathField`（块级）相呼应。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `index.ts` 里有的用 `export { ... }`，有的用 `export type { ... }`？如果全写成 `export { ... }` 会怎样？

**参考答案**：`export type { ... }` 只导出类型，编译后会被完全擦除，不进入运行时产物（`dist/index.js`）。如果混在一起全写成 `export { ... }`，TypeScript 在「isolatedModules / verbatimModuleSyntax」之类的严格设置下会报错，因为它无法区分「值」和「类型」，而类型在运行时是不存在的。分开写既语义清晰，也避免编译问题。

**练习 2**：使用者写 `import { Extension } from 'codemirror-live-markdown'`，这个 `Extension` 真的是这个库定义的吗？

**参考答案**：不是。它来自 `@codemirror/state`，只是被 `src/index.ts` 第 44 行 `export type { Extension } from '@codemirror/state'` 转手导出了一次。这是一种「便捷转出」，让使用者不必额外从 `@codemirror/state` 单独引入类型。

**练习 3**：如果新增一个 `src/plugins/footnote.ts` 并导出 `footnotePlugin`，要让使用者能 `import { footnotePlugin } from 'codemirror-live-markdown'`，需要改哪个文件？

**参考答案**：只需在 `src/index.ts` 的 Plugins 那一块加一行 `export { footnotePlugin } from './plugins/footnote';`。这就是桶文件的好处——新增特性只动一个聚合点。

---

### 4.3 peerDependencies 与可选依赖

#### 4.3.1 概念说明

这个库奉行「零强制依赖、按需引入」（上一讲已建立）。落到 `package.json` 上，就是它**没有** `dependencies` 字段，而是把依赖几乎全部放进 `peerDependencies`，并把其中两个标成可选。三种依赖字段的区别：

| 字段 | 含义 | 本项目是否使用 |
| --- | --- | --- |
| `dependencies` | 安装本包时**自动**一并安装的运行依赖 | 否（刻意不设） |
| `peerDependencies` | **要求宿主项目自己安装**的包，本包不替你装 | 是（5 个 CodeMirror 包） |
| `peerDependenciesMeta` | 给 peer 依赖附加元信息，这里用来标记**可选** | 是（`katex`、`lowlight` 标 optional） |

为什么这样设计？因为这个库是 CodeMirror 6 的**扩展**，它必须和宿主项目里的 CodeMirror 用「同一份实例」，否则插件会失效。如果把 CodeMirror 放进 `dependencies`，npm 会给本库单独装一份 CodeMirror，导致「两份实例」冲突。放进 `peerDependencies` 则是明确告诉宿主：「你自己装 CodeMirror，我只要求版本兼容」。

而 `katex`（数学公式渲染）和 `lowlight`（代码高亮）只在用到对应特性时才需要，所以标成 `optional: true`——你不写公式就不用装 katex，不高亮代码就不用装 lowlight，库会优雅降级。

#### 4.3.2 核心流程：从源码到发布产物

理解依赖分层，还要看懂「源码如何变成可发布的包」。这条链路是：

```
src/**/*（源码，TS）
   │  tsc / tsup 编译（tsconfig.json 约束）
   ▼
dist/index.js（ESM） + dist/index.cjs（CJS） + dist/index.d.ts（类型）
   │  package.json 的 main / module / types / exports 指向它们
   ▼
使用者 import 'codemirror-live-markdown'
```

关键约束来自三处：

1. **`tsconfig.json`** 的 `rootDir: "./src"` 与 `outDir: "./dist"`：编译输入只能是 `src`，输出落到 `dist`；`include: ["src/**/*"]` 进一步限定。所以「发布给使用者的东西」本质上就是 `src` 编译后的产物，`src/index.ts` 是入口。
2. **`package.json` 的 `exports` / `main` / `module` / `types`**：把 `dist/index.*` 暴露成包入口，并按 `import`（ESM）/ `require`（CJS）/ `types`（类型解析）三种条件分别给文件。
3. **`peerDependencies` / `peerDependenciesMeta`**：在构建时，CodeMirror 系列会被当作 external（不打包进产物），由宿主提供；`katex`/`lowlight` 则在用到时才动态 `import`。

「动态 import」是可选依赖能优雅降级的关键：`utils/mathCache.ts` 里是用 `try { 动态 import('katex') }` 的方式，运行时才去找 katex，找不到就走降级路径。所以发布产物里既不强制捆绑 katex，也不会因为没装 katex 而崩溃。

#### 4.3.3 源码精读

**入口与脚本**：`package.json` 顶部声明了包名、版本（`0.5.2-alpha.1`）、`type: "module"`，以及三套入口字段。

[package.json:L1-L15](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L1-L15) —— `main`/`module`/`types`/`exports` 把 `dist/index.*` 暴露为包入口；`exports` 用条件映射区分 ESM、CJS 与类型解析。

**强制对等依赖**：5 个 CodeMirror/Lezer 包都在 `peerDependencies` 里，没有任何一个进 `dependencies`。

[package.json:L50-L56](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L50-L56) —— `@codemirror/lang-markdown`、`@codemirror/language`、`@codemirror/state`、`@codemirror/view`、`@lezer/markdown` 全部作为对等依赖，要求宿主自备。

**可选依赖**：`katex` 与 `lowlight` 在 `peerDependenciesMeta` 里被标为 `optional: true`。注意它们**没有**出现在上面的 `peerDependencies` 键里（不同 npm 版本对「meta 里出现但 peer 里没有」的容忍度不同），其真实安装版本由 `devDependencies`（`lowlight: ^3.3.0`）等提供线索。

[package.json:L57-L64](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/package.json#L57-L64) —— `katex`、`lowlight` 标记为可选，对应「数学公式」「代码高亮」两个可选特性。

**动态 import 优雅降级**：可选依赖在代码里体现为「运行时才去加载」。`utils/mathCache.ts` 用 `try` 包裹动态 import，所以即便没装 katex，整个库仍能正常加载，只是公式无法渲染。

[src/utils/mathCache.ts:L33-L35](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/mathCache.ts#L33-L35) —— `try` 内部动态 `import` KaTeX，是「可选依赖 + 优雅降级」在源码层面的直接体现（`@ts-expect-error` 注释说明 katex 不在固定类型里）。

**源码→产物的边界**：`tsconfig.json` 限定编译范围为 `src`，输出到 `dist`。

[tsconfig.json:L10-L11](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/tsconfig.json#L10-L11) —— `outDir: "./dist"`、`rootDir: "./src"`，决定了「发布产物 = src 编译结果」的边界，`src/index.ts` 是入口起点。

> 备注：构建实际由 `tsup`（`package.json` 的 `build` 脚本，下一讲详讲）完成，它会读取 `tsconfig.json` 并把外部依赖（CodeMirror 系列）external 掉，因此最终 `dist` 不会捆绑 CodeMirror，与 peerDependencies 的设计相呼应。

#### 4.3.4 代码实践

**实践目标**：验证「CodeMirror 没有被捆绑进产物」这一设计是否属实。

**操作步骤**：

1. 先确认依赖声明：打开 `package.json`，确认 `dependencies` 字段**不存在**，且 5 个 CodeMirror 包都在 `peerDependencies`。
2. （可选，待本地验证）运行 `npm install && npm run build`，然后查看 `dist/` 产物体积，并用搜索工具检查 `dist/index.js` 里是否出现被完整内联的 `@codemirror/view` 源码——预期是**只出现 import 语句**，而不是内联实现。

**需要观察的现象**：

- `package.json` 里没有 `dependencies`。
- 产物文件里对 CodeMirror 的引用应是 `import { ... } from '@codemirror/view'` 这类**外部 import**，而非把其源码拷进来。

**预期结果**：CodeMirror 由宿主提供，本库产物只含自身逻辑，体积小、且能与宿主的 CodeMirror 共用同一实例。

> 说明：步骤 2 标注「待本地验证」——是否内联取决于 tsup 的 external 配置（下一讲会讲 tsup.config.ts）。如果你现在还没跑过构建，可以先只做步骤 1 的声明层验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `@codemirror/view` 从 `peerDependencies` 挪到 `dependencies`，会出什么问题？

**参考答案**：npm 会为本库单独安装一份 `@codemirror/view`，于是宿主项目里出现「两份」CodeMirror view 实例。本库产出的 Decoration/Plugin 用的是自己那份实例的类型与运行时对象，而宿主编辑器用的是另一份，两者不匹配会导致插件失效或类型校验失败。这正是扩展库普遍使用 peerDependencies 的根本原因。

**练习 2**：用户项目里没装 `katex`，但也没写任何公式。库会崩溃吗？为什么？

**参考答案**：不会。`katex` 是可选依赖，源码里用「动态 import + try/catch」延迟加载。用户不写公式时，`renderMath` 根本不会被调用，动态 import 也不会触发；即使触发了，找不到 katex 也会走降级路径而非抛错崩溃。

**练习 3**：`peerDependenciesMeta` 里把 `lowlight` 标成 `optional: true`，对应了库里的哪个特性？

**参考答案**：对应「代码块语法高亮」特性。`lowlight` 负责把代码块文本高亮成带语法着色的 HTML/HAST（见 `src/utils/codeHighlight.ts`）。没装 lowlight 时，代码块会降级为不高亮的纯文本展示。

## 5. 综合实践

把本讲三块知识串起来，完成一个「**导出全景图**」小任务：

1. 打开 `src/index.ts`，按 Core / Plugins / Theme / Utils / 便捷 五块，把每个 `export` 的**对外名字**和它的**源文件路径**整理成一张表。
2. 在表上用箭头标注依赖方向：Plugins 块里的名字 → Core 块里的名字、→ Utils 块、→ Theme 块（提示：根据 4.1.2 的依赖图，Plugins 会同时依赖 Core 与 Utils；你可以打开某个 plugin 文件的 import 来验证）。
3. 在表旁标注每个对外名字是「值」还是「类型」（`export type` 的为类型）。
4. 最后，用一句话回答：**如果让你向新同事介绍「想找某个功能的实现该去哪个文件」，你会怎么用这张表？**

**验收标准**：你的表里至少应包含 `livePreviewPlugin`、`mathPlugin`、`codeBlockField`、`imageField`、`editorTheme`、`renderMath`、`shouldShowSource`、`highlightCode` 这八个名字的「源文件」列，且依赖箭头方向与 4.1.2 一致。

这个任务综合考察了你对「分层（4.1）」「桶文件（4.2）」「依赖分层（4.3）」三件事的理解——这正是本讲的全部核心。

## 6. 本讲小结

- `src` 分成 `core / plugins / widgets / utils / theme` 五层，外加入口 `index.ts`；各层职责清晰、依赖单向（plugins → core/widgets/utils，widgets → utils）。
- `core` 是被复用的纯逻辑（如 `shouldShowSource`、`collapseOnSelectionFacet`），`plugins` 是装饰编排者，`widgets` 是 DOM 渲染单元，`utils` 是纯函数与缓存，`theme` 是样式——这是模块化理念在目录结构上的直接投影。
- `src/index.ts` 是桶文件，把五层导出按 Core / Plugins / Theme / Utils 分类 `re-export`，并用 `export type` 单独转发类型；它是 `package.json` 指定的包入口。
- `livePreviewPlugin`、`mathPlugin`、`codeBlockField`、`imageField` 四个导出分别来自 `plugins/livePreview.ts`、`plugins/math.ts`、`plugins/codeBlock.ts`、`plugins/image.ts`。
- 依赖分层是硬约定：5 个 CodeMirror/Lezer 包是**对等依赖**（宿主自备），`katex`/`lowlight` 是**可选依赖**（运行时动态 import、优雅降级），刻意不设 `dependencies`。
- `tsconfig.json` 的 `rootDir: src` / `outDir: dist` 划定了「源码 → 发布产物」的边界，`src/index.ts` 是这条链路的起点。

## 7. 下一步学习建议

到这里，你已经知道「代码长什么样、入口在哪、依赖怎么分」，但还没有真正把项目跑起来、在浏览器里看到效果。建议下一步：

- **学习 u1-l3《构建、测试与运行 demo》**：搞懂 `tsup`（多格式构建）、`vitest`（jsdom 测试）、CI 矩阵，亲手跑一次 `npm install && npm test` 和 demo。
- 之后再进入 **u1-l4《快速集成》**，用本讲认识的 `livePreviewPlugin`、`editorTheme` 等扩展，搭出第一个能看见 Live Preview 效果的编辑器。

进阶阶段（u2 起）会逐层深入：先读 `core/shouldShowSource.ts`（决策核心），再读 `plugins/livePreview.ts`（标记隐藏）。届时你会感谢现在画好的这张「导出全景图」——它能让你随时快速定位到目标文件。
