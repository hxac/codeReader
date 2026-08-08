# imageField：图片预览与加载状态

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `parseImageSyntax` 如何用一条正则把 `![alt](url)` 拆成 `src / alt / title / isLocal` 四个字段，以及它如何区分本地图与远程图。
- 读懂 `imageField` 这个 `StateField` 的 `create` / `update` 决策逻辑，理解它为什么**必须**用 StateField 而不是 ViewPlugin。
- 画出 `ImageWidget.toDOM` 的 `loading → loaded / error` 异步状态机，并理解 `eq` 与 `ignoreEvent` 在其中的作用。
- 说清 `shouldShowSource` 如何在「图片预览（渲染态）」与「原始语法高亮（源码态）」之间做切换，以及拖拽时为何要冻结装饰。

## 2. 前置知识

本讲是「图片与链接」单元的第一篇，默认你已经学过：

- **u2-l2 shouldShowSource**：它是整个 Live Preview 的「决策心脏」，返回 `true` 表示显示源码（标记可见、可编辑），返回 `false` 表示显示渲染结果。本讲的图片在两种态之间切换，正是由它一锤定音。
- **u3-l1 WidgetType 与 Decoration.replace**：图片预览用的是「替换渲染」策略——用 `Decoration.replace` 把 `![alt](url)` 原文藏起来，换成 `WidgetType.toDOM` 生成的真实 `<img>` DOM。你需要熟悉 `eq / toDOM / ignoreEvent` 三件套以及 `block: true` 的含义。

补充一句关于 CodeMirror 装饰的常识（u2-l1 已讲过，这里只复习要点）：

- **Decoration.replace**：把一段原文替换掉（可带 widget）。
- **Decoration.line**：给整行加 class（不改文字）。
- **StateField**：它的值属于 `EditorState`、能进状态快照；带 `block: true` 的 replace 装饰会改变行高，**必须**用 StateField（视口测量依赖它）。
- **ViewPlugin**：装饰随视口重算，但无法承载块级装饰。

还要注意：本库里的图片节点叫 `Image`，来自 `@codemirror/lang-markdown` 提供的 Lezer 语法树。`imageField` 就是靠遍历这棵树找到每一个 `Image` 节点的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/plugins/image.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/image.ts) | 图片插件主文件：`parseImageSyntax`（解析）、`buildImageDecorations`（产装饰）、`createImageField`/`imageField`（StateField 定义与导出）。 |
| [src/widgets/imageWidget.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts) | `ImageWidget`（继承 `WidgetType`）：`toDOM` 内部异步加载图片，实现 `loading → loaded / error` 状态机。 |
| [src/utils/imageLoader.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts) | `loadImage`（异步加载 + 超时）、双缓存、`resolveImagePath`（路径净化）。本讲会用到它的 `loadImage`，深入讲解留给 u6-l2。 |
| [src/core/shouldShowSource.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts) | 决策函数：决定一段区间显示源码还是渲染结果。 |
| [src/plugins/__tests__/image.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/image.test.ts) | 图片插件测试：覆盖 `parseImageSyntax` 各种输入、渲染态、更新、边界情况。 |
| [src/theme/default.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts) | 图片相关样式：`cm-image-widget`、`cm-image-loading`、`cm-image-error`、`cm-image-source` 等。 |

## 4. 核心概念与源码讲解

本讲按「一条远程图片从被识别到加载完成」的真实执行顺序，拆成三个最小模块：

1. **解析与编排**（4.1）：`parseImageSyntax` 把文本拆成结构化数据，`imageField` 遍历语法树把每个 `Image` 节点变成装饰。
2. **双模式切换**（4.2）：`shouldShowSource` 决定这一段走「渲染态 widget」还是「源码态 `cm-image-source`」。
3. **Widget 状态机**（4.3）：渲染态下 `ImageWidget.toDOM` 异步加载图片，经历 `loading → loaded / error`。

### 4.1 parseImageSyntax 与 imageField StateField

#### 4.1.1 概念说明

要在编辑器里预览图片，第一步得把 `![alt text](https://example.com/a.png "标题")` 这串纯文本「读懂」，拆出真正有用的信息：图片地址 `src`、替代文字 `alt`、可选标题 `title`，以及它是本地图还是远程图 `isLocal`。这件纯文本解析工作由 `parseImageSyntax` 完成，它是一个**纯函数**，不碰 CodeMirror、不碰 DOM，输入字符串、输出结构或 `null`。

解析完之后，还需要一个「编排者」：它遍历整篇文档的语法树，找到所有图片节点，调用解析器，再根据策略产出装饰。这个编排者就是 `imageField`——一个 `StateField<DecorationSet>`。

#### 4.1.2 核心流程

`parseImageSyntax` 的正则拆解（关键一行）：

```
/^!\[([^\]]*)\]\((.+?)(?:\s+["']([^"']+)["'])?\)$/
```

逐段对照：

| 片段 | 含义 | 对应字段 |
| --- | --- | --- |
| `!\[` | 字面量 `![` | —— |
| `([^\]]*)` | 任意非 `]` 字符 | `alt` |
| `\]\(` | 字面量 `](` | —— |
| `(.+?)` | 非贪婪，至少 1 字符 | `src` |
| `(?:\s+["']([^"']+)["'])?` | 可选：空白+引号包裹的标题 | `title` |
| `\)$` | 字面量 `)` 并结尾 | —— |

两个要点：

- **标题可选**：`(?:…)?` 让 `title` 可有可无，所以 `![alt](a.png)` 和 `![alt](a.png "t")` 都能匹配。
- **URL 支持括号**：`src` 用非贪婪 `(.+?)`，但因结尾被 `\)$` 锚定，引擎会一直扩展 `src` 直到**最后一个** `)`。因此 `![alt](https://x/image_(1).png)` 能正确得到含括号的 URL。

`isLocal` 判定很直白：只要不是 `http://` / `https://` / `data:` 开头，就当作本地图。

`imageField` 的编排流程（伪代码）：

```
buildImageDecorations(state):
    遍历 syntaxTree(state):
        若 node.name === 'Image':
            text = 截取该节点原文
            data = parseImageSyntax(text)
            若 data 为 null：跳过
            决定走渲染态还是源码态（见 4.2）
            渲染态：push 一条 replace({widget, block:true}).range(from, to)
            源码态：push 一条 Decoration.line({class:'cm-image-source'})
    返回排好序的 DecorationSet
```

#### 4.1.3 源码精读

**解析函数** [src/plugins/image.ts:27-50](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/image.ts#L27-L50)：用一条正则匹配 `![alt](url)` 三种变体（带/不带 title、单/双引号标题），失败返回 `null`；成功则取 `src/alt/title` 三个捕获组，并据前缀算出 `isLocal`。

**本地图判定** 在 [src/plugins/image.ts:39-42](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/image.ts#L39-L42)：三个 `!startsWith` 的与运算，任一前缀命中即为远程/data 图，`isLocal` 为 `false`。

**编排主循环** [src/plugins/image.ts:73-106](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/image.ts#L73-L106)：`syntaxTree(state).iterate({ enter })` 遍历语法树；命中 `Image` 节点时，用 `state.doc.sliceString(from, to)` 取出原文交给解析器；解析返回 `null`（节点不像图片）则 `return` 跳过，这是「语法树节点名匹配但内容不合法」的安全兜底。

**渲染态装饰** 在 [src/plugins/image.ts:90-96](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/image.ts#L90-L96)：注意 `Decoration.replace({ widget, block: true })`。**`block: true` 是关键**——图片预览独占一整行、会改变行高，所以必须走块级装饰，这正是 `imageField` 必须是 StateField 的原因。

**StateField 定义** [src/plugins/image.ts:117-151](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/image.ts#L117-L151)：`create` 调一次 `buildImageDecorations`；`update` 决定何时重建（4.2 详述）；`provide` 把字段的 `DecorationSet` 交给 `EditorView.decorations`。

**实例缓存** [src/plugins/image.ts:178-199](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/image.ts#L178-L199)：`imageField(options)` 用模块级 `cachedField`/`cachedOptions` 缓存 StateField 实例。若新旧 options 五个字段全相等就复用，避免每次调用都新建字段。这也意味着**同一份 options 对应的 widget 配置是恒定的**——这条性质在 4.3 讨论 `eq` 时会用到。

#### 4.1.4 代码实践

**实践目标**：验证 `parseImageSyntax` 的拆解行为，亲手感受正则的边界。

**操作步骤**：

1. 进入项目根目录，运行现有测试，只跑图片这一组：
   ```bash
   npm test -- image.test
   ```
2. 打开 [src/plugins/\_\_tests\_\_/image.test.ts:60-114](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/image.test.ts#L60-L114)，对照 `parseImageSyntax` 测试用例阅读断言。
3. 仿照现有用例，**在测试文件里**临时加一条断言（这是在测试里加，不是改源码），覆盖一个新输入，例如含中文 alt 与中文标题：
   ```ts
   it('should support CJK alt and title', () => {
     const r = parseImageSyntax('![一只猫](cat.png "我家猫")');
     expect(r?.alt).toBe('一只猫');
     expect(r?.title).toBe('我家猫');
     expect(r?.src).toBe('cat.png');
   });
   ```
   再跑一次 `npm test -- image.test` 确认通过。

**需要观察的现象**：正则用 `[^\]]*` 和 `[^"']+` 匹配，对中文（多字节）无障碍；`isLocal` 对 `cat.png` 应为 `true`。

**预期结果**：新加用例通过；`not an image`、`[link](url)`、`![incomplete` 三类输入返回 `null`（见 [src/plugins/\_\_tests\_\_/image.test.ts:109-113](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/image.test.ts#L109-L113)）。若你的环境未装依赖导致命令无法运行，则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`parseImageSyntax('![alt](https://example.com/image_(1).png)')` 的 `src` 是什么？为什么？

> **答案**：`src` 为 `https://example.com/image_(1).png`。因为 `src` 用非贪婪 `(.+?)` 但结尾被 `\)$` 锚定，引擎把 `src` 扩展到最后一个 `)`，于是中间的 `(1)` 被正确包含。这正是 [src/plugins/\_\_tests\_\_/image.test.ts:222-228](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/image.test.ts#L222-L228) 验证的行为。

**练习 2**：`![](data:image/png;base64,AAAA)` 的 `isLocal` 是多少？

> **答案**：`false`。`isLocal` 判定排除 `http://` / `https://` / `data:` 三种前缀，data URL 命中 `data:`，故为远程/data 类。注意 `data:` 在后续 `resolveImagePath` 中会被原样放行（见 u6-l2）。

### 4.2 shouldShowSource 双模式：渲染态 vs 源码态

#### 4.2.1 概念说明

光看图片还不够——Live Preview 的精髓是「光标进去就变回可编辑的源码，光标移开就变回预览」。对图片而言，这两种态分别长这样：

- **渲染态**（光标不在图片上）：`![alt](url)` 原文被 `replace` 藏起来，换成 `ImageWidget` 画出的 `<img>`。
- **源码态**（光标进入图片区间）：不替换原文，而是给该行套一个 `cm-image-source` 背景色，提示「这是一张图、且你现在能直接改它的语法」。

到底走哪一态，完全由 `shouldShowSource` 决定——它就是 u2-l2 讲过的那个纯函数，本讲只是复用，不重复它的内部细节。

#### 4.2.2 核心流程

`shouldShowSource(state, from, to)` 三步短路（回顾）：

1. 读 `collapseOnSelectionFacet`：总开关没开 → 返回 `false`（始终渲染）。
2. 读 `mouseSelectingField`：正在拖拽 → 返回 `false`（防闪烁）。
3. 遍历选区每个 range，用 `range.from <= to && range.to >= from` 判相交：任一命中 → 返回 `true`（显示源码）。

`buildImageDecorations` 据此分流：

```
isTouched = shouldShowSource(state, from, to)
isDrag    = state.field(mouseSelectingField, false)

if (!isTouched && !isDrag):
    渲染态 → replace({widget, block:true})
else:
    源码态 → line({class: 'cm-image-source'})
```

这里有两层拖拽防护（与 u3-l2、u4-l2 同构）：`shouldShowSource` 内部已对拖拽短路，`build` 又叠加一道 `!isDrag`。再加上 `update` 在拖拽期间**直接冻结**装饰（见 4.2.3），三者共同保证拖选时图片不抖动。

#### 4.2.3 源码精读

**分流判断** [src/plugins/image.ts:88-103](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/image.ts#L88-L103)：第 88 行调 `shouldShowSource` 得 `isTouched`；第 90 行 `!isTouched && !isDrag` 为真走渲染态 widget，否则走源码态。

**源码态用 line 装饰** [src/plugins/image.ts:98-103](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/image.ts#L98-L103)：`Decoration.line({ class: 'cm-image-source' }).range(line.from)`。注意它只给整行染色、**不替换文字**，所以原始 `![alt](url)` 仍在 DOM 里可逐字符编辑——这正是「进入即可编辑」的来源。`line.from` 用 `state.doc.lineAt(from)` 取得，`Decoration.line` 只需一个定位点（区别于 mark 需 `from, to`）。

**`cm-image-source` 样式** [src/theme/default.ts:308-310](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L308-L310)：一个半透明粉色背景 `rgba(236, 72, 153, 0.1)`，纯视觉提示，不含动画。

**决策函数本体** [src/core/shouldShowSource.ts:31-53](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L31-L53)：三步短路，拖拽中（第 39-42 行）返回 `false`，与选区相交（第 45-50 行）返回 `true`。`imageField` 只是它的调用方。

**StateField.update 的冻结策略** [src/plugins/image.ts:122-147](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/image.ts#L122-L147)：这是「拖拽不抖」的真正主防线。决策顺序与 u2-l3 的 `checkUpdateAction`、u4-l2 的 `tableField` 一致（StateField 手动内联了同一套更新语义）：

| 触发条件（按顺序短路） | 动作 | 对应行 |
| --- | --- | --- |
| 文档改变 / 配置重置 (`docChanged \|\| reconfigured`) | 重建 | 124-126 |
| 拖拽**刚结束** (`wasDragging && !isDragging`) | 重建 | 132-134 |
| 拖拽**进行中** (`isDragging`) | 冻结（返回旧 deco） | 137-139 |
| 选区改变 (`tr.selection`) | 重建 | 142-144 |
| 以上都不满足 | 冻结 | 146 |

关键在第 137-139 行：拖拽期间无论选区怎么变，`update` 直接 `return deco`，**不重算**。这就避免了「光标扫过图片 → widget 频繁增删 → 闪烁」。

#### 4.2.4 代码实践

**实践目标**：在真实浏览器里观察「光标进出图片」的双模式切换，并与源码一一对应。

**操作步骤**：

1. 启动 demo（u1-l3 已说明：进入 `demo/` 目录跑 `npm run dev`，根目录的 `npm run dev` 只是 watch 打包，不是网页）。
2. 在任一编辑器里输入一行：
   ```
   ![示例图片](https://placehold.co/300x100)
   ```
3. 把光标点到这一行的**上方或下方**（不触碰图片），观察现象。
4. 用鼠标或方向键把光标**移进** `![…](…)` 这段文本内部，再观察现象。

**需要观察的现象**：

- 光标在外：看到的是渲染后的图片预览（block 装饰替换了原文）。
- 光标进入：图片预览消失，整行变成带粉色背景的原始 `![示例图片](…)` 文本，可编辑。

**预期结果**：进出切换平滑。切换的根因正是 `shouldShowSource`：光标相交区间时返回 `true` → 走 `cm-image-source` 源码态。若网络无法访问该占位图地址，预览态会显示错误占位（见 4.3），但不影响「光标进出切换」本身。**待本地验证**实际浏览器表现。

#### 4.2.5 小练习与答案

**练习 1**：为什么源码态用 `Decoration.line` 而不是 `Decoration.replace`？

> **答案**：源码态的目的是「让原始语法可见且可编辑」。`Decoration.line` 只给整行加背景 class，**不改动文字**，用户能直接逐字符编辑 `![alt](url)`；若用 `replace`，文字会被藏起来，反而无法编辑。

**练习 2**：如果用户在拖拽选区时正好扫过一张图片，会发生什么？为什么图片不闪？

> **答案**：图片保持拖拽**开始前**的状态、不抖动。因为 `update` 在第 137-139 行检测到 `isDragging` 为真时直接 `return deco`，冻结装饰、跳过重建。拖拽结束后（`wasDragging && !isDragging`）才补一次重建。

### 4.3 ImageWidget：loading → loaded/error 状态机

#### 4.3.1 概念说明

渲染态下真正「画出图片」的是 `ImageWidget`（继承 `WidgetType`，u3-l1 已讲过三件套）。它最特别的地方是：**图片是异步加载的**。`toDOM` 返回 DOM 时图片还没下载完，于是它必须经历一个状态机：

```
        toDOM 被调用
             │
             ▼
   ┌── loading（loading 占位 + spinner）
   │            │
   │      loadImage(src).then(...)
   │            │
   │      ┌─────┴─────┐ 成功            失败/超时
   │      ▼           ▼
   │  loaded        error
   │  (<img>)      (⚠ 占位)
   └──────────────────────────────
```

`loadImage` 来自 `src/utils/imageLoader.ts`，它的双缓存与路径净化会在 u6-l2 深讲；本讲只需把它当作「返回 `Promise<LoadedImage>`、其中 `loaded` 标识成败」的异步函数。

#### 4.3.2 核心流程

`toDOM` 的执行（伪代码）：

```
toDOM():
    container = <div class="cm-image-widget">
    if showLoading:
        container 加一个 loading 占位（spinner + "Loading..."）
    loadImage(src, {basePath}).then(result):
        移除 loading 占位
        if result.loaded:
            追加 <img>（设 src/alt/title/maxWidth）
            if showAlt && alt: 追加 <div class="cm-image-alt">alt
        else:
            追加 <div class="cm-image-error">⚠ errorPlaceholder
    return container   // 立即返回，不等图片下载
```

`loadImage` 内部（本讲视角）：用 `new Image()` 触发浏览器下载，设了 10 秒超时；成功就缓存结果，失败则**不缓存**（允许重试）。它返回的 `LoadedImage` 含 `loaded: boolean` 和可选 `error`。

#### 4.3.3 源码精读

**类定义与构造** [src/widgets/imageWidget.ts:43-49](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts#L43-L49)：持有 `data: ImageData`（来自 4.1 的解析）与 `options: ImageOptions`。

**eq 相等性** [src/widgets/imageWidget.ts:54-60](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts#L54-L60)：只比 `src / alt / title` 三个字段。回忆 u3-l1 的 eq 心法——「eq 比较的字段集合应等于决定 toDOM 输出的字段集合」。这里 `toDOM` 还用到了 `options`（maxWidth/showAlt 等），为何 eq 不比 options？因为 4.1 提到 `imageField` 的实例缓存保证**同一字段生命周期内 options 恒定**，所以 options 不会变化、无需进 eq。而 `isLocal` 由 `src` 派生，也不必单独比。eq 命中时 CodeMirror 复用旧 DOM、**不重跑 `toDOM`、不重发 `loadImage`**，这是性能关键。

**loading 占位** [src/widgets/imageWidget.ts:80-88](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts#L80-L88)：受 `showLoading`（默认 `true`）控制，注入 spinner + "Loading..."。spinner 样式见 [src/theme/default.ts:279-286](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/theme/default.ts#L279-L286)（`animation: spin 0.8s linear infinite`）。

**异步加载与状态切换** [src/widgets/imageWidget.ts:91-126](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts#L91-L126)：

- 第 91 行调用 `loadImage(src, { basePath })`，拿到 Promise 后 `.then`。
- 第 93-96 行：加载完成后**先移除 loading 占位**（`container.querySelector('.cm-image-loading')`）。
- 第 98-115 行：`result.loaded` 为真 → 创建 `<img>`，设 `src`（注意用的是 `result.src`，即 `loadImage` 解析后的地址）、`alt`、`title`、`maxWidth`、`draggable = false`，追加进容器；若 `showAlt && alt` 再追加一个 `cm-image-alt` 子元素显示 alt 文本。
- 第 116-125 行：失败分支 → 追加 `cm-image-error`（⚠ + `errorPlaceholder` 文案）。

**ignoreEvent** [src/widgets/imageWidget.ts:136-138](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts#L136-L138)：返回 `false`，表示「事件不要被 widget 吞掉，交给 CodeMirror」。于是点击图片预览时，CodeMirror 能把光标定位进去 → 触发 4.2 的源码态切换。这与代码块 widget「按事件类型区分 ignoreEvent」不同，图片统一放行。

**loadImage 的成败契约** [src/utils/imageLoader.ts:78-160](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L78-L160)：成功设 `loaded: true` 并写缓存（第 113-127 行）；失败/超时设 `loaded: false` 且**不写缓存**（第 129-144 行），允许下次重试。`handleSuccess`/`handleError` 都用 `resolved` 标志防重入。

#### 4.3.4 代码实践

**实践目标**：在浏览器 DevTools 里亲眼看到 `loading → loaded` 的状态机切换，理解「toDOM 立即返回、图片异步到达」。

**操作步骤**：

1. 启动 demo（`demo/` 目录 `npm run dev`）。
2. 打开浏览器 DevTools，切到 **Network** 面板，勾选「Disable cache」以便看清请求；再切一个 **Elements** 面板。
3. 在编辑器输入一张**较大**的远程图（人为放慢加载，更容易抓到 loading 态）：
   ```
   ![big](https://placehold.co/2000x1000)
   ```
4. 在 Elements 面板里找到 `.cm-image-widget` 节点，观察它的子节点随时间变化。

**需要观察的现象**：

- 初始：`.cm-image-widget` 下只有 `.cm-image-loading`（spinner + Loading...）。
- 加载中：Network 面板有一次对该图片 URL 的 GET 请求。
- 加载完成：`.cm-image-loading` 被移除，出现 `<img>`；若有 alt 还会出现 `.cm-image-alt`。
- 故意写一个不存在的地址 `![x](https://example.com/no-such.png)`：最终出现 `.cm-image-error`（⚠ + "Failed to load image"）。

**预期结果**：DOM 变化与 [src/widgets/imageWidget.ts:91-126](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/imageWidget.ts#L91-L126) 的三个分支一一对应。受网络与图片实际大小影响，loading 态可能转瞬即逝；若抓不到，可在 DevTools Network 限速（Slow 3G）放大现象。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`ImageWidget.eq` 为何不比较 `options`？会不会导致「改了 maxWidth 却不重渲染」？

> **答案**：不会。`options` 由 `imageField` 的实例缓存保证在同一字段生命周期内恒定（见 4.1.3）。改变 options 时 `imageField()` 会创建**新的** StateField（旧缓存失效），CodeMirror 重新挂载字段、widget 也会以新 options 重建。所以 eq 只需比较会真正影响 toDOM 数据内容的 `src/alt/title` 即可。

**练习 2**：图片加载失败时，第二次进入渲染态会再次尝试加载吗？为什么？

> **答案**：会。`loadImage` 对失败结果**不写缓存**（[src/utils/imageLoader.ts:129-144](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/utils/imageLoader.ts#L129-L144)），下次再请求同一地址会重新发起 `new Image()` 下载。这是「允许重试」的设计取舍——图片失败可能是临时网络问题，缓存失败反而会让它永远显示错误。

**练习 3**：`ignoreEvent` 返回 `false` 对用户意味着什么？

> **答案**：意味着点击图片预览不会被 widget 拦截，事件透传给 CodeMirror，光标能落进图片文本里，从而触发源码态、可直接编辑语法。

## 5. 综合实践

把三个模块串起来，完成一次「远程图片全链路追踪」。

**任务**：在 demo 编辑器中输入 `![logo](https://placehold.co/120x40 "占位")`，按下表填写这条图片「从文本到画面」的完整旅程。第二列写该步骤**做了什么**，第三列写它**在哪个文件/函数被触发**。

| 顺序 | 触发的函数 | 职责 | 所在文件 |
| --- | --- | --- | --- |
| 1 | `syntaxTree.iterate` 命中 `Image` 节点 | 找到图片区间 (from, to) | image.ts |
| 2 | ? | ? | ? |
| 3 | ? | ? | ? |
| 4 | ? | ? | ? |
| 5 | ? | ? | ? |

**参考答案**（填完后再对照）：

| 顺序 | 触发的函数 | 职责 | 所在文件 |
| --- | --- | --- | --- |
| 1 | `syntaxTree.iterate`（命中 `Image`） | 在语法树中定位图片区间 | image.ts |
| 2 | `parseImageSyntax`（`sliceString` 取原文后） | 正则拆出 src/alt/title/isLocal | image.ts |
| 3 | `shouldShowSource` | 光标不在区间内 → 返回 `false` → 渲染态 | shouldShowSource.ts |
| 4 | `createImageWidget`（由 `buildImageDecorations` 调用） | 构造 `ImageWidget`，包进 `replace({block:true})` | image.ts / imageWidget.ts |
| 5 | `ImageWidget.toDOM` → `loadImage` | 先画 loading 占位，异步下载成功后替换为 `<img>` | imageWidget.ts / imageLoader.ts |

**进阶**：把光标移进图片文本，重新追踪——此时第 3 步 `shouldShowSource` 返回 `true`，第 4 步变为 `Decoration.line({class:'cm-image-source'})`，于是图片预览消失、整行变为可编辑源码。这正是「双模式」的切换点。

## 6. 本讲小结

- `parseImageSyntax` 用一条正则把 `![alt](url "title")` 拆成 `src/alt/title/isLocal`，支持可选标题、单双引号、URL 内含括号；失败返回 `null`。
- `imageField` 是 `StateField<DecorationSet>`：遍历语法树找 `Image` 节点 → 解析 → 据决策产出装饰；因图片是 `block: true` 块级装饰，**必须**用 StateField。
- 两种态由 `shouldShowSource` 统一裁决：光标相交走源码态（`Decoration.line` + `cm-image-source`，文字可编辑），否则走渲染态（`replace` + `ImageWidget`）。
- `update` 用「结构变化→拖拽结束→拖拽中冻结→选区」的短路顺序，拖拽期间直接冻结装饰，是图片不闪烁的真正主防线。
- `ImageWidget.toDOM` 实现异步状态机：立即返回带 loading 占位的容器，`loadImage` 成功后换成 `<img>`（成功有缓存），失败换错误占位（失败不缓存、可重试）。
- `eq` 只比 `src/alt/title`（options 由字段缓存保证恒定），命中则复用 DOM、不重跑 toDOM；`ignoreEvent` 返回 `false` 让点击能进入源码态。

## 7. 下一步学习建议

- **u6-l2 imageLoader**：本讲只用了 `loadImage` 的「成败契约」。下一篇深入它的双缓存（`imageCache` + `loadingPromises` 去重）、10 秒超时，以及 `resolveImagePath` 如何处理相对路径与 `../` 路径穿越净化——这些是图片加载的性能与安全基石。
- **u6-l3 linkPlugin**：链接 `[](url)` 与图片 `![](url)` 语法相近，但 linkPlugin 走 ViewPlugin 且多了「跳过代码块」与 URL 危险协议拦截，可对照本讲体会两种解析路径（语法树 vs 正则）的差异。
- 若想强化「StateField 块级装饰 + 手动内联 checkUpdateAction」这套模式，可回看 u4-l2 的 `tableField`，它与 `imageField` 几乎同构，对照阅读能加深理解。
