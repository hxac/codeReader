# codeBlockField：三种交互模式 auto/toggle/inline

## 1. 本讲目标

本讲进入代码块子系统（专家级）的第二篇。学完后你应当能够：

- 看懂 `codeBlockField` 如何从 Lezer 语法树识别 `FencedCode` 节点、用 `CodeInfo` 子节点提取语言、用 `SKIP_LANGUAGES` 把 `math` 等特殊语言让给别的插件。
- 说清三种 `interaction`（`auto` / `toggle` / `inline`）在「**何时显示源码**」与「**何时重建装饰**」两件事上的差异。
- 理解 `setCodeBlockSourceMode`（StateEffect）与 `codeBlockSourceModeField`（StateField）这对「消息 + 状态」搭档，如何用一张区间数组记录每张代码块的源码态，并在文档编辑时随位置平移。

本讲聚焦在**决策与状态**层面（代码块「显示哪一面」「何时重算」）；至于渲染出的 DOM 长什么样、点击如何精确定位、inline 模式的 HAST 高亮细节，分别留给 [u5-l3](u5-l3-codeblock-widget-dom-interaction.md) 与 [u5-l4](u5-l4-inline-mode-hast-highlight.md)。

## 2. 前置知识

本讲承接以下已建立的概念，不再重复定义，只说明如何被代码块复用：

- **`shouldShowSource`**（[u2-l2](u2-l2-should-show-source.md)）：三步短路决策函数。光标/选区与目标区间相交则返回 `true`（显示源码）。`auto` 模式直接调用它。
- **`mouseSelectingField` / `setMouseSelecting`**（[u2-l3](u2-l3-facet-mouseSelecting-updatehelper.md)）：拖拽态 StateField。拖拽中 `shouldShowSource` 一律返回 `false` 以防闪烁。
- **`checkUpdateAction`**（[u2-l3](u2-l3-facet-mouseSelecting-updatehelper.md)）：为 ViewPlugin 设计的 `rebuild/skip/none` 决策。但 `codeBlockField` 是 **StateField**，它的 `update` 拿到的是 `Transaction` 而非 `ViewUpdate`，无法直接调用 `checkUpdateAction`——于是它把同样的「结构变化 → 拖拽结束 → 拖拽中 → 选区 → 无」判定顺序，用 Transaction 字段（`docChanged` / `selection` / `effects`、以及从 `startState` 与当前 state 读 `mouseSelectingField`）**手动复刻**了一遍。这与 [u3-l3](u3-l3-block-math-and-cache.md) 的 `blockMathField`、[u4-l2](u4-l2-table-field-read-only.md) 的 `tableField` 是同一套路。
- **块级装饰必须用 StateField**（[u2-l1](u2-l1-codemirror6-core-concepts.md)、[u3-l1](u3-l1-widgettype-and-decoration-replace.md)）：带 `block: true` 的 `Decoration.replace` 会改变行高，必须属于 `EditorState` 才能参与视口测量。代码块是块级装饰，所以 `codeBlockField` 是 StateField。
- **「StateEffect 当消息 + StateField 用区间数组存数据」搭档**（[u4-l3](u4-l3-table-editor-plugin.md) 的 `setTableSourceMode` / `tableSourceModeField`）：本讲的 `setCodeBlockSourceMode` / `codeBlockSourceModeField` 几乎是它的翻版，读完后你会发现两者结构一模一样。
- **两条高亮路线**（[u5-l1](u5-l1-code-highlight-lowlight.md)）：`highlightCode` 返回 HTML（服务 widget 的 `innerHTML`），`highlightCodeHast` 返回 HAST 树（服务 `Decoration.mark`）。`auto`/`toggle` 走 HTML 路线，`inline` 走 HAST 路线。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
|---|---|---|
| `src/plugins/codeBlock.ts` | 代码块插件主体：识别 `FencedCode`、三种模式分发、`codeBlockSourceModeField`、`update` 决策、对外 `codeBlockField` / `codeBlockEditorPlugin` | 几乎全部核心逻辑 |
| `src/plugins/codeBlockEffects.ts` | 仅定义 `setCodeBlockSourceMode` 这个 StateEffect 及其载荷类型 | 「消息」载体 |
| `src/widgets/codeBlockWidget.ts` | `CodeBlockWidget`（auto/toggle 渲染态）、`CodeBlockSourceToggleWidget`（源码态的 Code 回切按钮） | 源码态按钮如何回写 |
| `src/plugins/__tests__/codeBlock.test.ts` | 行为测试，验证三种模式的显隐与切换 | 实践依据 |

## 4. 核心概念与源码讲解

### 4.1 FencedCode 识别与 SKIP_LANGUAGES

#### 4.1.1 概念说明

Lezer 的 Markdown 语法树会把 ```` ``` ```` 围栏代码块解析成一个名为 **`FencedCode`** 的节点。这个节点有三个结构化子节点：

- 开围栏 ```` ``` ````（固定文本）
- **`CodeInfo`**：围栏语言标识，即 ```` ```javascript ```` 里的 `javascript`
- **`CodeText`**：真正的代码内容

`codeBlockField` 的工作循环就是：遍历语法树 → 对每个 `FencedCode` → 用 `CodeInfo` 取语言、用 `CodeText` 取代码 → 决定渲染态还是源码态 → 产出装饰。

但这里有一个**关键的拦截器：`SKIP_LANGUAGES`**。因为 ```` ```math ```` 围栏块并不是「代码」，而是**块级数学公式**，已经被 `blockMathField`（[u3-l3](u3-l3-block-math-and-cache.md)）接管。如果 `codeBlockField` 也去渲染它，就会和公式插件抢同一段文本。于是它用一个 `Set` 把 `math` 排除掉，**主动让出**这段区间。这是一种模块间的「管辖权约定」：靠语言名划分领地，互不越界。

#### 4.1.2 核心流程

用伪代码描述 `buildCodeBlockDecorations` 的主循环骨架：

```text
for node in syntaxTree(state).iterate:
    if node.name != 'FencedCode': continue
    codeInfo = node.node.getChild('CodeInfo')
    language = codeInfo ? doc.slice(codeInfo) : defaultLanguage
    if language in SKIP_LANGUAGES: continue      # 让给 math 插件
    codeText = node.node.getChild('CodeText')
    code     = codeText ? doc.slice(codeText) : ''
    showSource = <见 4.2 的决策>
    产出渲染态或源码态装饰
```

#### 4.1.3 源码精读

`SKIP_LANGUAGES` 目前只含一个语言——`math`：

[src/plugins/codeBlock.ts:60-63](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L60-L63) —— 定义「要跳过的语言集合」，注释说明这些语言由其他插件处理。

主循环里的识别与跳过逻辑（auto/toggle 共用的 `buildCodeBlockDecorations`，inline 走的 `buildCodeBlockInlineDecorations` 里有完全对应的一段）：

[src/plugins/codeBlock.ts:430-442](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L430-L442) —— 命中 `FencedCode` 后，先取 `CodeInfo` 切出语言，再 `SKIP_LANGUAGES.has(language)` 判定是否跳过（直接 `return` 跳过该节点），最后取 `CodeText` 切出代码正文。

注意语言提取的写法是 `state.doc.sliceString(codeInfo.from, codeInfo.to).trim()`：从文档里按节点区间切字符串。`node.node.getChild('CodeInfo')` 返回的是 Lezer 的子节点句柄，`.from/.to` 是它在文档中的绝对位置。若没有 `CodeInfo`（如无语言标注的 ```` ``` ```` 块），则回退到 `options.defaultLanguage`（默认 `'text'`）。

#### 4.1.4 代码实践

**实践目标**：验证 `SKIP_LANGUAGES` 真的把 `math` 让了出去。

**操作步骤**：阅读现有测试 `should ignore math code blocks`：

[src/plugins/__tests__/codeBlock.test.ts:140-149](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/codeBlock.test.ts#L140-L149) —— 文档含 ```` ```math\nx^2 + y^2 = z^2\n``` ````，断言 `.cm-codeblock-widget` 为 `null`（即 `codeBlockField` 不渲染它）。

**需要观察的现象**：在 `demo` 目录运行 `npm run dev`，对比同一个 ```` ```math ```` 块在「仅启用 codeBlockField」与「同时启用 blockMathField」时的表现——前者应是纯文本，后者应渲染成公式。

**预期结果**：`codeBlockField` 对 `math` 块视而不见（无 widget），把渲染权完整交给公式插件。

**待本地验证**：若你把 `SKIP_LANGUAGES` 临时改成 `new Set()`（空集），重新构建，观察是否会出现「公式块被代码块 widget 抢占」的冲突现象，以此反证跳过机制的必要性（验证完请还原源码——本讲禁止修改源码，这只是思考实验）。

#### 4.1.5 小练习与答案

**练习 1**：如果未来要新增对 `mermaid` 图表的支持（由另一个插件渲染），应该在 `codeBlock.ts` 的哪里改动？

**参考答案**：把 `'mermaid'` 加入 `SKIP_LANGUAGES`（即 [第 63 行](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L63) 的 `new Set(['math'])` 改为 `new Set(['math', 'mermaid'])`）。因为 `buildCodeBlockDecorations` 和 `buildCodeBlockInlineDecorations` 都靠 `SKIP_LANGUAGES.has(language)` 判定跳过，加进去即可让 `codeBlockField` 对 `mermaid` 块放手。

**练习 2**：`CodeInfo` 子节点和 `CodeText` 子节点分别对应源码里的什么文本？

**参考答案**：`CodeInfo` 对应开围栏 ```` ``` ```` 之后、换行之前的语言标识（如 `javascript`）；`CodeText` 对应两个围栏之间的代码正文（不含围栏行本身）。

---

### 4.2 三种 interaction 模式

#### 4.2.1 概念说明

`CodeBlockOptions.interaction` 有三个取值（[第 46-47 行](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L46-L47)），默认是 `auto`（[第 57 行](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L53-L58)）。三者的本质区别在于**「谁触发源码态」**和**「源码态/渲染态分别长什么样」**：

| 模式 | 谁触发源码态 | 渲染态实现 | 入口函数 |
|---|---|---|---|
| `auto` | 光标进入代码块（依赖 `shouldShowSource` + 选区） | 整块 `replace` 成 `CodeBlockWidget`（HTML 高亮，不可原地编辑） | `codeBlockField()`（默认） |
| `toggle` | 显式点击 MD / Code 按钮（依赖 `codeBlockSourceModeField`，与光标无关） | 整块 `replace` 成 `CodeBlockWidget`，但带 MD 按钮 | `codeBlockEditorPlugin()` |
| `inline` | 显式按钮（同 toggle，依赖 `codeBlockSourceModeField`） | **只替换围栏行**，代码正文留在 `contentDOM` 可原生编辑，高亮用 `Decoration.mark`（HAST 路线） | `codeBlockField({ interaction: 'inline' })` |

一句话区分：`auto` 跟着光标走、`toggle`/`inline` 跟着按钮走；`toggle` 把整块换成 widget、`inline` 只换围栏行而保留正文可编辑。

#### 4.2.2 核心流程

**(A) 显示哪一面（`showSource` 决策）**——auto/toggle 共用 `buildCodeBlockDecorations`：

```text
showSource =
  interaction === 'toggle'
    ? isCodeBlockInSourceMode(sourceRanges, from, to)   // 显式区间命中
    : shouldShowSource(state, from, to) || isDrag        // 光标相交 或 拖拽中
```

注意 `auto` 模式那个 `|| isDrag`：拖拽时 `shouldShowSource` 内部本就返回 `false`（拖拽短路，见 [u2-l2](u2-l2-should-show-source.md)），但这里再 `|| isDrag` 把结果**主动拉成 `true`**，让拖拽中的所有代码块一律退回稳定的源码态（不渲染重量级 widget），避免拖选时 widget 反复重绘抖动。这是与 [u3-l2](u3-l2-inline-math-plugin.md) 行内公式「拖拽中冻结装饰」同源的防抖思路，只是实现位置不同。

**(B) 何时重算（`createCodeBlockField` 的 `update` 决策）**——三种模式共用同一个 `update`，按下面顺序短路：

```text
① if docChanged 或 reconfigured 或 收到 setCodeBlockSourceMode effect → rebuild
② if interaction !== 'auto' → return deco          // toggle/inline 到此为止，不响应选区
// —— 以下仅 auto ——
③ if 拖拽刚结束(wasDragging && !isDragging) → rebuild
④ if 拖拽中(isDragging)            → return deco   // 冻结
⑤ if 选区变化(tr.selection)        → rebuild
   else return deco
```

**核心结论**：`toggle`/`inline` 的装饰只在 ①（文档变化 / 配置重置 / 显式按钮切换）时重建，对**选区变化和拖拽完全无感**——因为它们的源码态压根不依赖光标，光标在块里来回移动不该触发任何重算。`auto` 则必须随光标进出重建（⑤），但拖拽中冻结（④）防抖动，拖拽结束再补一次重建（③）。这个判定顺序与 `checkUpdateAction`（[u2-l3](u2-l3-facet-mouseSelecting-updatehelper.md)）的「结构变化 → 拖拽结束 → 拖拽中 → 选区 → 无」完全一致，只是用 Transaction 语言手写了一遍。

#### 4.2.3 源码精读

`interaction` 配置项与默认值：

[src/plugins/codeBlock.ts:46-48](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L46-L48) —— 三种取值的联合类型；[src/plugins/codeBlock.ts:53-58](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L53-L58) —— 默认 `interaction: 'auto'`。

`showSource` 的三分叉决策（注意 `auto` 分支末尾的 `|| isDrag`，以及 `isDrag` 仅在 auto 模式才读取）：

[src/plugins/codeBlock.ts:424-466](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L424-L466) —— 第 424-425 行先算 `isDrag`（非 auto 模式恒为 `false`），第 463-466 行据此决定 `showSource`。

`update` 的五段式决策：

[src/plugins/codeBlock.ts:528-561](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L528-L561) —— 对应上面伪代码的 ① ~ ⑤。重点看第 539-541 行：`if (options.interaction !== 'auto') return deco;` 是 toggle/inline「不响应选区」的闸门。

对外两个入口与扩展装配：

[src/plugins/codeBlock.ts:683-700](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L683-L700) —— `codeBlockField()` 合并默认选项后，始终装入 `codeBlockSourceModeField` + 主装饰 field；若 `interaction === 'inline'` 再追加 `codeBlockSelectionPlugin` + `codeBlockSelectionTheme`（因为 inline 的正文是真实可编辑 DOM，选区高亮需特殊处理，详见 [u5-l4](u5-l4-inline-mode-hast-highlight.md)）。`codeBlockEditorPlugin()` 仅是 `codeBlockField({ ...options, interaction: 'toggle' })` 的语法糖——它的选项类型 `CodeBlockEditorOptions` 用 `Omit<CodeBlockOptions,'interaction'>` **禁止**调用方传 `interaction`，强制锁定为 toggle（[第 50-51 行](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L50-L51)）。

#### 4.2.4 代码实践

**实践目标**：动手验证「toggle 模式光标进入代码块**不**切换源码态」这条关键差异。

**操作步骤**：阅读并运行现有测试：

[src/plugins/__tests__/codeBlock.test.ts:282-306](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/codeBlock.test.ts#L282-L306) —— 用 `codeBlockEditorPlugin()`（toggle），把光标放在块内（anchor: 18），断言仍有 `.cm-codeblock-widget`、且**没有** `.cm-codeblock-source`。对照 auto 模式的反例 [src/plugins/__tests__/codeBlock.test.ts:165-175](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/codeBlock.test.ts#L165-L175)（光标在块内 → 出现 `.cm-codeblock-source`）。

**需要观察的现象**：同一份文档、同一个光标位置，auto 与 toggle 的 DOM 产出截然不同。

**预期结果**：auto 光标入块即显源码；toggle 光标入块仍是 widget，必须点 MD 按钮才切源码。这正是 `update` 第 ② 行闸门（toggle 不响应选区）的直接体现。

**待本地验证**：在 `demo` 中打开「Code (Auto)」「Code (Toggle)」两个编辑器（见 [demo/main.ts:380-447](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L380-L447)），用键盘把光标移进代码块，对比两者行为。

#### 4.2.5 小练习与答案

**练习 1（决策表）**：填出下表——面对各类 transaction，`createCodeBlockField.update` 是否重建装饰（✓ 重建 / ✗ 不重建）。

| 事件 | `auto` | `toggle`/`inline` |
|---|---|---|
| 文档改变（`docChanged`） |  |  |
| 配置重置（`reconfigured`） |  |  |
| 收到 `setCodeBlockSourceMode` effect |  |  |
| 选区改变且非拖拽 |  |  |
| 拖拽进行中（`isDragging`） |  |  |
| 拖拽刚结束 |  |  |

**参考答案**：

| 事件 | `auto` | `toggle`/`inline` |
|---|---|---|
| 文档改变 | ✓（①） | ✓（①） |
| 配置重置 | ✓（①） | ✓（①） |
| 收到 `setCodeBlockSourceMode` effect | ✓（①） | ✓（①） |
| 选区改变且非拖拽 | ✓（⑤） | ✗（② 闸门，根本走不到选区判定） |
| 拖拽进行中 | ✗（④ 冻结） | ✗（②） |
| 拖拽刚结束 | ✓（③） | ✗（②） |

**练习 2**：为什么 `setCodeBlockSourceMode` effect 的重建判定放在第 ① 步、在 `interaction !== 'auto'` 闸门**之前**？

**参考答案**：因为 toggle/inline 的源码态完全由这个 effect 驱动。如果把它的判定放在「`interaction !== 'auto'` → return deco」之后，toggle/inline 点了 MD 按钮也不会重建，按钮就失效了。所以「显式切换 effect 必须无条件触发重建」是所有模式共享的前提，必须排在模式闸门之前。

**练习 3**：`auto` 模式 `showSource = shouldShowSource(...) || isDrag`，而 `update` 里拖拽中又会「冻结装饰不重建」。这两处都提到拖拽，是否冗余？

**参考答案**：不冗余，是两层防护。`update` 的冻结（④）处理的是「纯拖拽 effect」事务——此时直接返回旧 `deco`，根本不调 `build`。但如果拖拽**同时**伴随 `docChanged`（罕见但可能），会在 ① 先触发重建调用 `build`，此时 `|| isDrag` 就发挥作用：把所有代码块统一拉成源码态，避免拖拽中途重渲染重量级 widget。两者分别守住「不重算」和「若不得不重算则退回轻量源码态」两道关。

---

### 4.3 codeBlockSourceModeField

#### 4.3.1 概念说明

`toggle`/`inline` 的「源码态」需要一个**跨事务、能随文档编辑平移**的状态。本库的标准做法是「StateEffect 当消息 + StateField 当存储」（与 [u4-l3](u4-l3-table-editor-plugin.md) 的 `tableSourceModeField` 同构）：

- **`setCodeBlockSourceMode`**（StateEffect）：载荷是 `{ from, to, showSource }`，只当**一次性消息**，本身不存数据。
- **`codeBlockSourceModeField`**（StateField）：值是一张区间数组 `CodeBlockSourceRange[]`，记录「**哪些代码块当前处于源码态**」。

因为同一个文档可能有多个代码块，每张块独立切换，所以状态是「区间集合」而非单个布尔。读取时用 `isCodeBlockInSourceMode(ranges, from, to)` 判断某段是否落在任一源码区间内。

#### 4.3.2 核心流程

`codeBlockSourceModeField.update` 做两件事：

```text
1. 随文档变化平移所有现有区间：
   next = ranges.map(r => ({ from: mapPos(r.from, 1), to: mapPos(r.to, -1) }))
2. 处理 setCodeBlockSourceMode effect（可能多个）：
   mapped = { from: mapPos(effect.from,1), to: mapPos(effect.to,-1) }
   next = effect.showSource ? addRange(next, mapped) : removeRange(next, mapped)
```

区间增删用统一的**闭区间相交**判定。两个区间 \([a_{from}, a_{to}]\) 与 \([b_{from}, b_{to}]\) 相交当且仅当：

\[
a_{from} \le b_{to} \;\land\; a_{to} \ge b_{from}
\]

- `addRange`：仅当**没有**已有区间与新区间重叠时才追加（去重，避免重复记录同一块）。
- `removeRange`：**过滤掉**所有与新区间重叠的旧区间（一次清掉覆盖该块的所有记录）。
- `isCodeBlockInSourceMode`：任一区间与查询区间相交即返回 `true`。

#### 4.3.3 源码精读

「消息」定义极简，整个文件只有它：

[src/plugins/codeBlockEffects.ts:3-10](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlockEffects.ts#L3-L10) —— `CodeBlockSourceModeToggle` 载荷类型 + `StateEffect.define<...>()`。

区间管理纯函数（注意它们与 [u4-l3](u4-l3-table-editor-plugin.md) 的同名工具是各自本地定义、非共享导入，但公式完全一致）：

[src/plugins/codeBlock.ts:70-97](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L70-L97) —— `rangesOverlap` / `removeRange` / `addRange` / `isCodeBlockInSourceMode`，全部基于上面的闭区间相交公式。

状态字段本体：

[src/plugins/codeBlock.ts:99-119](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L99-L119) —— `create: () => []`（初始无任何源码块）；`update` 先 `mapPos` 平移，再据 `showSource` 增删。

「写回」的两个方向——MD 按钮切到源码态、Code 按钮切回渲染态，都靠派发同一个 effect：

- 源码态 widget 的 **Code 按钮**派发 `showSource: false`：

[src/widgets/codeBlockWidget.ts:477-490](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L477-L490) —— `CodeBlockSourceToggleWidget`（源码态时显示）点击后 `view.dispatch({ effects: setCodeBlockSourceMode.of({ from, to, showSource: false }) })`，从区间集合里移除该块。

- 反方向（渲染态 widget 的 MD 按钮 → 切源码）在 `CodeBlockWidget` 与 inline 的 `CodeBlockHeaderWidget` 里，派发 `showSource: true`（见 [src/widgets/codeBlockWidget.ts:166-181](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/widgets/codeBlockWidget.ts#L166-L181) 与 [src/plugins/codeBlock.ts:200-209](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/codeBlock.ts#L200-L209)）。

#### 4.3.4 代码实践

**实践目标**：把 `codeBlockSourceModeField` 与 `tableSourceModeField`（[u4-l3](u4-l3-table-editor-plugin.md)）对照，确认二者是同一模式。

**操作步骤**：

1. 阅读测试 `should switch to source mode when toggle effect is dispatched`：

[src/plugins/__tests__/codeBlock.test.ts:202-221](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/codeBlock.test.ts#L202-L221) —— toggle 模式下手动 `view.dispatch({ effects: setCodeBlockSourceMode.of({ from:0, to:doc.length, showSource:true }) })`，断言随后出现 `.cm-codeblock-source` 与 `.cm-codeblock-source-toggle`。这证明「派发 effect → field 更新区间 → 装饰重建 → 切源码态」整条链路。

2. 再读「点 Code 按钮切回」的测试 [src/plugins/__tests__/codeBlock.test.ts:257-280](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/codeBlock.test.ts#L257-L280)，验证反方向。

**需要观察的现象**：effect 是「无数据记忆」的——只携带一次性载荷；真正的「哪块在源码态」记忆在 StateField 的区间数组里。

**预期结果**：手工派发 effect 即可驱动切换，无需操作光标。这正是 toggle/inline 区别于 auto 的根本——状态由 effect 推动，而非由选区推动。

#### 4.3.5 小练习与答案

**练习 1**：若文档里有两张代码块 A、B，用户对 A 点了 MD 按钮（进源码态），此时 `codeBlockSourceModeField` 的值大致长什么样？接着用户在 B 前面插入了一行文字，A 的区间会怎样？

**参考答案**：点 MD 后值约为 `[{ from: A.from, to: A.to }]`（仅 A）。在 B 前插入文字后，`update` 第 1 步用 `mapPos` 把 A 的 `from/to` 随文档变化**平移**到新位置（插入点在 A 之前，故 A 的 from/to 同步后移一个插入长度），区间仍准确覆盖 A，源码态不丢失。这就是「先平移、再处理 effect」中平移步骤的意义。

**练习 2**：`removeRange` 用的是「过滤掉所有重叠区间」而非「精确删除某个区间」。这种实现有什么取舍？

**参考答案**：简单稳健——不必要求 effect 携带的 `from/to` 与当初 `addRange` 时**完全相等**（经过若干次文档编辑后区间已被 `mapPos` 平移，很难精确相等）。只要新区间与旧区间相交即视为「同一块」予以清除。代价是无法表达「一块内部的部分区间切换」，但代码块的粒度本就是「整块」，所以这个取舍刚好合适。

---

## 5. 综合实践

**任务**：完成一份「auto vs toggle 更新决策表」并用最小测试钉住最关键的一行。

**步骤 1（填表）**：基于 4.2.2 的五段式 `update` 逻辑，填写下表（答案见 4.2.5 练习 1，先自己填再核对）。

| Transaction 内容 | `codeBlockField()`（auto）是否重建 | `codeBlockEditorPlugin()`（toggle）是否重建 | 理由（对应 ①~⑤） |
|---|---|---|---|
| `changes: {...}`（改文档） |  |  |  |
| `selection: { anchor }`（移动光标，非拖拽） |  |  |  |
| `effects: setMouseSelecting.of(true)`（开始拖拽） |  |  |  |
| 拖拽中的选区扩展 |  |  |  |
| `effects: setMouseSelecting.of(false)`（拖拽结束） |  |  |  |
| `effects: setCodeBlockSourceMode.of({showSource:true})` |  |  |  |

**步骤 2（钉住关键差异）**：写一个最小测试（参考 [codeBlock.test.ts:21-41](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/__tests__/codeBlock.test.ts#L21-L41) 的 `createEditor` 工具），对同一份文档 ```` ```javascript\nconst x = 1;\n``` ```` 分别用 `codeBlockField()` 与 `codeBlockEditorPlugin()`，初始光标在块外（widget 可见），然后 `view.dispatch({ selection: { anchor: 18 } })` 把光标移进块内。断言：

- auto：之后出现 `.cm-codeblock-source`、消失 `.cm-codeblock-widget`（选区变化触发重建，`shouldShowSource` 返回 true）。
- toggle：`.cm-codeblock-widget` **仍在**、`.cm-codeblock-source` **仍无**（第 ② 闸门，不响应选区）。

**步骤 3（运行）**：`npm test` 确认通过。

**预期结果**：这张表与这对测试合起来，就是对「三种模式更新差异」最精确的概括——auto 随选区重建、toggle/inline 只随文档与显式 effect 重建。

**待本地验证**：拖拽相关的两行（开始拖拽、拖拽结束）在 jsdom 里难以真实模拟鼠标，建议结合 demo 人工观察：在「Code (Auto)」编辑器里按住鼠标拖选穿过代码块，观察 widget 是否在拖拽期间保持稳定（冻结）、松手后才切换。

## 6. 本讲小结

- `codeBlockField` 遍历语法树找 `FencedCode`，用 `CodeInfo` 取语言、`CodeText` 取代码；`SKIP_LANGUAGES`（目前含 `math`）把特殊语言让给别的插件，靠语言名划分模块管辖权。
- 三种 `interaction` 的本质差异：`auto` 跟光标走（复用 `shouldShowSource`），`toggle`/`inline` 跟显式按钮走（读 `codeBlockSourceModeField`）；`toggle` 整块换成 widget，`inline` 只换围栏行、保留正文可编辑。
- 三种模式共用同一个 `update`：文档变化 / 配置重置 / `setCodeBlockSourceMode` effect 一律重建；`toggle`/`inline` 在此之后立即返回（不响应选区与拖拽）；仅 `auto` 继续走「拖拽结束重建 → 拖拽中冻结 → 选区变化重建」。这套顺序是 `checkUpdateAction` 的 Transaction 版手写复刻。
- `auto` 模式的 `showSource = shouldShowSource(...) || isDrag` 与 `update` 的拖拽冻结是**两层防护**：前者保证「不得不重算时退回轻量源码态」，后者保证「纯拖拽干脆不重算」。
- `setCodeBlockSourceMode`（StateEffect）+ `codeBlockSourceModeField`（StateField，区间数组）是「消息 + 状态」搭档，与 `tableSourceModeField` 同构；区间随 `mapPos` 平移、用闭区间相交公式增删查。

## 7. 下一步学习建议

- 想看渲染态 widget 的 DOM 结构、Copy/MD 工具栏、以及点击代码如何用 Canvas + 二分查找精确映射回源码字符位置，进入 [u5-l3：codeBlockWidget 的 DOM 渲染、复制按钮与点击定位](u5-l3-codeblock-widget-dom-interaction.md)。
- 想深入 `inline` 模式为何只替换围栏行、`highlightCodeHast` 的 HAST 树如何经 `hastToMarkDecorations` 递归转成 `Decoration.mark`、以及 `codeBlockSelectionPlugin` 如何处理行内选区高亮，进入 [u5-l4：inline 模式的可编辑高亮](u5-l4-inline-mode-hast-highlight.md)。
- 建议同步重读 [u4-l3](u4-l3-table-editor-plugin.md) 的 `tableSourceModeField`，对照体会「StateEffect + 区间数组 StateField」这一可复用模式在不同子系统中的同构实现。
