# shouldShowSource：显示源码还是渲染

## 1. 本讲目标

学完本讲，你应当能够：

1. 读懂 `shouldShowSource` 这个函数的三步判定流程，并解释每一步为什么「短路返回」。
2. 理解光标 / 选区与一段目标区间是否「相交」的判定公式 `range.from <= to && range.to >= from`。
3. 明白为什么在鼠标拖拽选区时函数一律返回 `false`（保持渲染态），以及这是如何消除闪烁的。
4. 能够脱离项目，独立构造 `EditorState`，对任意区间预测 `shouldShowSource` 的返回值。

本讲是整个 Live Preview 机制的「决策心脏」。后面所有的特性插件（粗体斜体标记、公式、表格、图片、链接、代码块）渲染与否，最终都要先问它一句：「这段文字，现在该显示源码，还是显示渲染结果？」

## 2. 前置知识

本讲假设你已经掌握上一讲（u2-l1）建立的 CodeMirror 6 词汇表，至少要熟悉下面几个概念：

- **EditorState**：编辑器的不可变状态快照。光标位置、选区、各个 StateField 的当前值，都从这个对象读取。
- **Facet**：一种「多输入、combine 汇总」的配置容器。本讲的 `collapseOnSelectionFacet` 就是一个 Facet，充当 Live Preview 的总开关。
- **StateField / StateEffect**：StateField 是「属于 State、会进快照」的可变状态；StateEffect 是改它的指令。本讲的 `mouseSelectingField` 用这对组合追踪「用户是否正在拖拽」。
- **Selection / Range**：选区由若干 `Range` 组成，每个 Range 有 `from`（起点）和 `to`（终点）。一个单纯的光标，是一个 `from === to` 的退化 Range。
- **Decoration（装饰）**：决定文本「怎么显示」的标记。本讲不直接写装饰，但要理解：调用方拿到 `shouldShowSource` 的布尔返回值后，会据此决定给某段文本套哪个 CSS 类。

如果这些概念还模糊，建议先回到 u2-l1 复习，再继续本讲。

一个贯穿全讲的背景直觉：

> **Live Preview 的本质，是在「渲染结果」和「原始 Markdown 源码」之间来回切换。** 光标离开时，你看到的是渲染后的样子（粗体真的加粗、公式真的渲染成数学符号）；光标进入时，原始的 `**`、`$` 等标记淡入回来，让你可以直接编辑。

`shouldShowSource` 就是那个回答「现在该显示哪一面」的判官。返回 `true` = 显示源码（标记可见、可编辑），返回 `false` = 显示渲染结果（标记隐藏）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/core/shouldShowSource.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts) | 本讲主角。一个纯函数，三步判定，决定一段区间显示源码还是渲染结果。 |
| [src/core/facets.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/facets.ts) | 定义 `collapseOnSelectionFacet`，Live Preview 的总开关。 |
| [src/core/mouseSelecting.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts) | 定义 `setMouseSelecting` Effect 与 `mouseSelectingField` StateField，追踪拖拽状态。 |
| [src/core/__tests__/shouldShowSource.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/shouldShowSource.test.ts) | 用 Vitest 写的单元测试，是理解函数行为的最佳捷径。 |
| [src/plugins/livePreview.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts) | 真正的调用现场。它用 `shouldShowSource` 的返回值决定标记是否加 `visible` 类。 |

注意：前三个文件都在 `src/core/` 目录下——`core` 存放的是「被多方复用的纯逻辑」，不含任何 DOM 操作，因此 `shouldShowSource` 可以被任何插件、甚至被你的单元测试直接调用。

## 4. 核心概念与源码讲解

### 4.1 collapseOnSelectionFacet：Live Preview 的总开关

#### 4.1.1 概念说明

`collapseOnSelectionFacet` 是一个 Facet，作用是回答一个全局问题：

> 「整个编辑器，到底开没开 Live Preview？」

它的取值只有两种：

- `true`：开启 Live Preview。光标不触碰的行，标记会被折叠隐藏，只显示渲染结果。
- `false`：关闭 Live Preview（源码模式）。所有标记始终可见。

`shouldShowSource` 在做任何精细判断之前，必须先看这个开关。如果开关本身就是关的，后面关于「光标在不在区间里」的讨论就毫无意义——直接给结论。

#### 4.1.2 核心流程

Facet 的关键在于 `combine` 函数：当多个扩展都往同一个 Facet 写值时，`combine` 负责把它们汇总成最终值。这里用的是 `values[0] ?? false`：

- 取第一个被写入的值（`values[0]`）。
- 如果一个值都没写（`undefined`），就用 `false` 作为默认——也就是说，**默认不开启 Live Preview**。

这是一个「安全默认」：宿主必须显式写 `collapseOnSelectionFacet.of(true)`，Live Preview 才会生效。

#### 4.1.3 源码精读

Facet 的定义非常短：

[定义总开关 Facet，combine 取首个输入、缺省为 false](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/facets.ts#L8-L10)

```ts
export const collapseOnSelectionFacet = Facet.define<boolean, boolean>({
  combine: (values) => values[0] ?? false,
});
```

然后在 `shouldShowSource` 里，第一步就是读它：

[第一步：若总开关未开启，直接返回 false（短路）](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L33-L36)

```ts
const shouldCollapse = state.facet(collapseOnSelectionFacet);
if (!shouldCollapse) {
  return false;
}
```

注意 `state.facet(...)` 这个 API：它从当前 State 快照里读出 Facet 的汇总值。读到 `false` 就立刻 `return false`，不再往下走——这就是「短路」。

> ⚠️ **关于返回值语义的提醒**：函数顶部的文档注释明确规定 `true = 显示源码`、`false = 显示渲染结果`。所以这一步 `return false` 的含义是「显示渲染结果」。一个细节是：在典型的集成里（见 u1-l4），切到源码模式时通常会直接把 `livePreviewPlugin` 从扩展列表里移除，让所有标记天然可见；因此本步更多是一个**防御性守卫**，确保即便插件被误用、开关未开，函数也能安全返回而不报错。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 Facet 的 `combine` 默认值。
2. **操作步骤**：在一个临时脚本（或测试文件）里，创建两个 State——一个不写 Facet，一个写 `collapseOnSelectionFacet.of(true)`——分别读 `state.facet(collapseOnSelectionFacet)`。
3. **需要观察的现象**：不写 Facet 时读到 `false`；写了 `of(true)` 时读到 `true`。
4. **预期结果**：与 `values[0] ?? false` 的语义一致。
5. 说明：本实践为源码阅读型，无需启动 demo。

#### 4.1.5 小练习与答案

**练习 1**：如果同时写两个 `collapseOnSelectionFacet.of(false)` 和 `collapseOnSelectionFacet.of(true)`，最终 Facet 值是多少？为什么？

**参考答案**：是 `false`。因为 `combine` 用 `values[0]`，只取第一个输入的值；后写的不会覆盖先写的。这是该 Facet「先到先得」的设计。

---

**练习 2**：为什么默认值要设计成 `false`（不开启），而不是 `true`？

**参考答案**：这是一种安全默认。Live Preview 会隐藏原始标记、改变编辑器外观，属于「有副作用的增强功能」；让宿主显式 opt-in（`of(true)`），可以避免「只装了库、没想开预览」的编辑器被意外改变行为。

### 4.2 mouseSelectingField：拖拽中的状态追踪

#### 4.2.1 概念说明

当用户用鼠标按住拖拽来选中一段文字时，选区会连续不断地变化。如果 Live Preview 在每一次选区变化时都重新判断「该不该显示源码」、并据此重建装饰，标记就会在「显示 / 隐藏」之间疯狂跳变——这就是**闪烁（flickering）**。

为了消除闪烁，项目用一个 StateField 记录「用户当前是否正在拖拽选区」。`shouldShowSource` 在第二步读到「正在拖拽」时，直接返回 `false`（保持渲染态），从而让拖拽期间画面稳定。

#### 4.2.2 核心流程

拖拽状态的维护分两部分：

1. **谁负责写状态？** 一个 StateEffect `setMouseSelecting`，携带一个布尔值，表示「开始拖拽 / 结束拖拽」。
2. **谁负责存状态？** 一个 StateField `mouseSelectingField`，默认 `false`；每次 transaction 到来时，遍历它的 effects，若遇到 `setMouseSelecting` 就更新值，否则保持不变。

至于这个 Effect 何时被派发——那是宿主（集成方）的责任：监听 `mousedown` 派发 `setMouseSelecting.of(true)`、监听 `mouseup` 派发 `setMouseSelecting.of(false)`。这部分在 u1-l4 的「mouseSelecting 接线」里讲过，本讲只关心：状态一旦为 `true`，`shouldShowSource` 第二步就会短路返回 `false`。

#### 4.2.3 源码精读

先看 Effect 与 StateField 的定义：

[setMouseSelecting：携带布尔值的拖拽状态指令](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts#L6-L6)

```ts
export const setMouseSelecting = StateEffect.define<boolean>();
```

[mouseSelectingField：默认 false，仅响应 setMouseSelecting 来更新](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts#L13-L23)

```ts
export const mouseSelectingField = StateField.define<boolean>({
  create: () => false,
  update(value, tr) {
    for (const effect of tr.effects) {
      if (effect.is(setMouseSelecting)) {
        return effect.value;
      }
    }
    return value;
  },
});
```

要点：`create: () => false` 给出初始值；`update` 里只有遇到 `setMouseSelecting` 才改值，否则原样返回（`return value`）。这意味着文档变化、选区变化本身都**不会**改动拖拽态——只有显式派发 Effect 才会。

再看 `shouldShowSource` 的第二步：

[第二步：若正在拖拽，直接返回 false，避免重建装饰引发闪烁](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L39-L42)

```ts
const isDragging = state.field(mouseSelectingField, false);
if (isDragging) {
  return false;
}
```

`state.field(field, false)` 的第二个参数 `false` 是「若该字段不存在，不要抛错，返回 `undefined`」。这是一种防御性读法：即便宿主忘了挂载 `mouseSelectingField`，函数也能正常运行（`undefined` 为 falsy，不会进入 `if`）。

#### 4.2.4 代码实践（源码阅读型：审视一个「名不副实」的测试）

1. **实践目标**：阅读测试文件里名为「should return false when dragging」的用例，判断它**是否真的测了拖拽**。
2. **操作步骤**：打开测试文件，定位下面这段：

[名为「拖拽时返回 false」的测试](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/shouldShowSource.test.ts#L17-L29)

```ts
// Simulate dragging state
const newState = state.update({
  effects: [],
}).state;
expect(shouldShowSource(newState, 6, 15)).toBe(false);
```

3. **需要观察的现象**：注意 `effects: []` 是一个空数组——它**并没有**派发 `setMouseSelecting.of(true)`。因此 `mouseSelectingField` 的值始终是初始的 `false`，`isDragging` 为 false。
4. **预期结果 / 结论**：该用例之所以断言成功，是因为它随后走到了第三步「选区相交判定」，而此时光标默认在位置 0、与目标区间 `(6, 15)` 不相交，于是返回 `false`。换句话说，**这个测试并没有真正走到第二步的拖拽分支**。要真正验证拖拽行为，应当这样写（示例代码，非项目原有）：

```ts
// 示例代码：真正把拖拽态置为 true
const draggingState = state.update({
  effects: setMouseSelecting.of(true),
}).state;
expect(shouldShowSource(draggingState, 6, 15)).toBe(false); // 此时才是第二步短路
```

5. 这是「读测试即读库」时常见的陷阱：测试通过 ≠ 测试覆盖了它声称的分支。学会审视测试，是理解源码的进阶能力。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mouseSelectingField.update` 在没有遇到 `setMouseSelecting` 时要 `return value`，而不是 `return false`？

**参考答案**：因为拖拽状态必须能「持续」。用户在拖拽过程中会产生大量 transaction（选区变化、文档变化等），这些都不应改变拖拽态；只有显式的 `setMouseSelecting` 才是「开始 / 结束」信号。若改成 `return false`，任何一次普通 transaction 都会把拖拽态重置为 false，导致拖拽期间标记又跳回可编辑态，重新引发闪烁。

---

**练习 2**：`state.field(mouseSelectingField, false)` 的第二个参数为什么传 `false`？

**参考答案**：它表示「字段不存在时不要抛异常，返回 `undefined`」。这样即便某个宿主没挂载 `mouseSelectingField`，`shouldShowSource` 也不会崩溃；`undefined` 是 falsy，逻辑上等同于「没有在拖拽」，是安全的退化。

### 4.3 选区相交判定：光标是否触碰目标区间

#### 4.3.1 概念说明

开关开了、也没在拖拽，剩下的核心问题就是：

> 「当前的光标或选区，有没有『碰到』我关心的这段 Markdown？」

这里的「这段 Markdown」可能是 `**world**` 这串加粗标记、一个 `$...$` 公式、一张图片的源码等。调用方会把它的起点 `from` 和终点 `to` 传进来，`shouldShowSource` 用一个数学判定来回答「碰没碰到」。

碰到了 → 返回 `true`（显示源码、可编辑）；没碰到 → 返回 `false`（显示渲染结果）。

#### 4.3.2 核心流程

判定两个区间是否相交，是一条经典的几何不等式。设光标选区为闭区间 \([a, b]\)（其中 \(a = \text{range.from}\)、\(b = \text{range.to}\)），目标区间为 \([c, d]\)（其中 \(c = \text{from}\)、\(d = \text{to}\)），则二者相交当且仅当：

\[
a \le d \;\land\; c \le b
\]

换成代码里的写法就是：

\[
\text{range.from} \le \text{to} \;\land\; \text{range.to} \ge \text{from}
\]

直觉解释：两个区间不相交的唯一可能是「一个整体在另一个的左边或右边」。排除掉这两种情况，剩下的就是相交。上面这条式子正是「既不整体在左、也不整体在右」的紧凑写法。

注意一个边界细节：光标本身是一个退化的区间，满足 \(a = b\)（`from === to`）。此时判定退化为「光标位置是否落在 \([from, to]\) 之内（含端点）」。所以光标正好停在区间的起点或终点时，判定为相交，返回 `true`。

CodeMirror 的选区可能包含多个 Range（按住 Ctrl/Cmd 多选），所以代码里要**遍历每一个 Range**，只要有一个相交就返回 `true`。

#### 4.3.3 源码精读

[第三步：遍历所有选区 Range，任一与目标区间相交即返回 true](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L45-L52)

```ts
for (const range of state.selection.ranges) {
  if (range.from <= to && range.to >= from) {
    return true;
  }
}
return false; // No intersection, show rendered output
```

对照测试用例可以验证判定逻辑。以文档 `"Hello **world** test"` 为例，各位置编号如下：

```
H  e  l  l  o     *  *  w  o  r  l  d  *  *     t  e  s  t
0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19
```

目标区间 `(6, 15)` 正好覆盖 `**world**`。

- 光标在 5（空格处）：`5 <= 15` 真，但 `5 >= 6` 假 → 不相交 → `false`。见 [测试：光标在区间外返回 false](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/shouldShowSource.test.ts#L31-L39)。
- 光标在 10（`world` 中间）：`10 <= 15` 真，`10 >= 6` 真 → 相交 → `true`。见 [测试：光标在区间内返回 true](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/shouldShowSource.test.ts#L41-L49)。
- 选区 4–12：`4 <= 15` 真，`12 >= 6` 真 → 相交 → `true`。见 [测试：选区与区间重叠返回 true](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/shouldShowSource.test.ts#L51-L59)。
- 光标恰在起点 6：`6 <= 15` 真，`6 >= 6` 真 → 相交 → `true`。见 [测试：光标落在边界返回 true](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/shouldShowSource.test.ts#L61-L69)。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证「端点包含」与「多选区」两种边界行为。
2. **操作步骤**：在测试文件里新增（或本地运行）两个用例：
   - 光标在位置 15（目标区间右端点），断言对 `(6, 15)` 返回 `true`。
   - 用 `EditorState.create({ selection: { anchor: 2 } })` 之外再叠加一个选区，模拟 Ctrl 多选，让其中一个 Range 落在区间内，断言返回 `true`。
3. **需要观察的现象**：端点 15 命中相交；多选区只要有一个命中即返回 `true`。
4. **预期结果**：与公式 `range.from <= to && range.to >= from` 一致。
5. 若无法确定运行结果，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么判定式是 `range.from <= to && range.to >= from`，而不是 `range.from >= from && range.to <= to`（后者是「选区被目标完全包含」）？

**参考答案**：因为 Live Preview 需要的是「只要碰到就显示源码」，是相交关系，不是包含关系。用户哪怕只选中了目标区间的一小部分、甚至只是把光标停在边上，都应能编辑这段 Markdown；用「包含」会把「部分相交」的情况漏掉，导致光标明明在标记上、标记却还藏着。

---

**练习 2**：若目标区间传成了 `from > to`（起点大于终点，理论上不该发生），这条判定还可靠吗？

**参考答案**：仍然能跑，但语义会变得奇怪。比如 `from=15, to=6` 时，对于光标在 10 的 Range：`10 <= 6` 为假 → 直接判为不相交。也就是说颠倒区间会让几乎所有光标都判为「没碰到」。代码本身不校验 `from <= to`，因此调用方有责任保证传入有序区间——这也是上一模块 4.2.4 里强调「审视输入」的体现。

### 4.4 三步合一：shouldShowSource 的完整决策与调用现场

#### 4.4.1 概念说明

前三模块拆开了三步判定，本模块把它们合起来看完整的决策链，并展示真实的调用现场——`shouldShowSource` 的返回值到底是怎么变成屏幕上的「标记显隐」的。

它是一个**纯函数**：输入只有 `state`、`from`、`to`，不读全局变量、不碰 DOM、没有副作用。这意味着它极易测试，也极易被任何插件复用。整个 Live Preview 体系对「何时显示源码」保持唯一口径，根源就在这里。

#### 4.4.2 核心流程

完整的决策可以用下面的伪代码概括：

```
shouldShowSource(state, from, to):
  ① 若 collapseOnSelectionFacet 未开启 → return false   # 总开关关了
  ② 若 mouseSelectingField 为 true（拖拽中）→ return false # 防闪烁
  ③ 遍历选区的每个 Range：
       若与 [from, to] 相交 → return true              # 触碰了，显示源码
  ④ return false                                        # 都没碰到，显示渲染结果
```

三步构成一个「漏斗」：先粗筛（全局开关），再剔干扰（拖拽），最后精判（相交）。每一步失败都立刻短路返回，避免无谓计算。

真值表小结：

| Facet 开启？ | 拖拽中？ | 光标/选区触碰区间？ | 返回值 | 含义 |
| :-: | :-: | :-: | :-: | --- |
| 否 | — | — | `false` | 显示渲染结果（防御性） |
| 是 | 是 | — | `false` | 显示渲染结果（防闪烁） |
| 是 | 否 | 是 | `true` | 显示源码（可编辑） |
| 是 | 否 | 否 | `false` | 显示渲染结果 |

#### 4.4.3 源码精读

完整的函数体一气呵成：

[shouldShowSource 全貌：三步短路判定](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L31-L53)

它顶部的文档注释还贴心地用三个例子说明了语义，正是本讲综合实践要复现的场景：

[文档注释里的三个使用示例，规定了返回值语义](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L16-L29)

那么返回值在真实插件里如何被消费？以 `livePreviewPlugin` 处理行内标记（如 `**`）为例：

[调用现场：用返回值决定标记是否加 visible 类](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L128-L132)

```ts
const isTouched = shouldShowSource(state, node.from, node.to);
const cls =
  isTouched && !isDrag
    ? 'cm-formatting-inline cm-formatting-inline-visible'
    : 'cm-formatting-inline';
```

可以看到：`shouldShowSource` 返回 `true`（且不在拖拽中）时，标记会额外获得 `cm-formatting-inline-visible` 类，从而通过 CSS 的 `max-width` 过渡动画显现出来（这正是 u7-l1「标记过渡动画」要讲的技巧）；返回 `false` 时只有基础类，标记保持折叠隐藏。**决策与表现彻底分离**：`core` 只管「该不该显示」，`plugins` + `theme` 只管「怎么显示」。

#### 4.4.4 代码实践

1. **实践目标**：跟踪一次「光标移入加粗」的完整链路，把决策函数与视觉表现串起来。
2. **操作步骤**：
   - 在 demo 里（u1-l3 / u1-l4 已介绍如何启动）打开一个启用 `livePreviewPlugin` 的编辑器。
   - 输入 `Hello **world** test`，先把光标停在 `Hello` 中间，再把光标用方向键移进 `world`。
3. **需要观察的现象**：光标在 `Hello` 时，`**` 不可见（渲染态）；光标进入 `world` 时，两边的 `**` 以动画淡入（源码态）。
4. **预期结果**：这一显一隐，背后正是 `shouldShowSource` 对区间 `(6, 15)` 从 `false` 翻转为 `true`，进而切换 `cm-formatting-inline-visible` 类。
5. 说明：若暂未跑通 demo，可改为纯源码阅读——对照 4.4.3 的两段代码，口述「光标进入 → 相交 → 返回 true → 加 visible 类 → 标记显现」这条链路。

#### 4.4.5 小练习与答案

**练习 1**：假设你要新增一个特性「双击某段渲染文本时，即使光标不在区间内也临时显示源码」，你会改动 `shouldShowSource`，还是改动调用方？

**参考答案**：倾向不改 `shouldShowSource`。它是被所有插件共享的单一决策点，改它会影响公式、表格、图片等所有特性。更好的做法是用一个新的 StateField 记录「双击临时态」，由调用方在拿到 `shouldShowSource` 返回值后，再 `||` 上这个临时态来决定是否加 `visible` 类——把「通用规则」和「特定交互」分层。

---

**练习 2**：用一句话概括 `shouldShowSource` 在整个架构中的角色。

**参考答案**：它是 Live Preview 的「单一事实来源（single source of truth）」——所有特性插件关于「现在显示源码还是渲染结果」的判断都汇聚到这一个纯函数，从而保证全库行为一致、可测、可复用。

## 5. 综合实践

把前三模块的知识串起来，完成下面这个独立的函数测试任务（与函数文档注释里的示例对应）。

**任务**：给定文档 `"Hello **world** test"`，构造三种 `EditorState`，断言 `shouldShowSource(state, 6, 15)` 的返回值符合预期。

位置编号提醒（再次列出方便对照）：

```
H  e  l  l  o     *  *  w  o  r  l  d  *  *     t  e  s  t
0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19
```

三种情形：

1. **光标在位置 5**（`Hello` 后面的空格，区间之外）：预期返回 `false`。
2. **光标在位置 10**（`world` 中间，区间之内）：预期返回 `true`。
3. **选区 4–12**（与区间 `(6,15)` 部分重叠）：预期返回 `true`。

参考实现（示例代码，仿照项目现有测试风格）：

```ts
// 示例代码：独立的 shouldShowSource 行为验证
import { EditorState } from '@codemirror/state';
import { shouldShowSource } from '../core/shouldShowSource';
import { collapseOnSelectionFacet } from '../core/facets';
import { mouseSelectingField } from '../core/mouseSelecting';

const base = (selection: { anchor: number; head?: number }) =>
  EditorState.create({
    doc: 'Hello **world** test',
    selection,
    extensions: [collapseOnSelectionFacet.of(true), mouseSelectingField],
  });

// 情形 1：光标在区间外
console.log(shouldShowSource(base({ anchor: 5 }), 6, 15)); // 预期 false
// 情形 2：光标在区间内
console.log(shouldShowSource(base({ anchor: 10 }), 6, 15)); // 预期 true
// 情形 3：选区与区间重叠
console.log(shouldShowSource(base({ anchor: 4, head: 12 }), 6, 15)); // 预期 true
```

**进阶**：在上述基础上再加两例自测——

- 关掉开关：把 `collapseOnSelectionFacet.of(true)` 改成 `of(false)`，光标即便在位置 10，也应返回 `false`（第一步短路）。
- 真正进入拖拽态：对某个 state 派发 `setMouseSelecting.of(true)` 后再调用，无论光标在哪都应返回 `false`（第二步短路）。

把这段代码放进一个临时测试文件运行（参照 u1-l3 的 `npm test`），或直接 `node`/`vitest` 跑一遍，观察输出是否与预期一致。若环境不具备，至少手工推演每条断言，并标注「待本地验证」。

## 6. 本讲小结

- `shouldShowSource(state, from, to)` 是 Live Preview 的决策心脏，返回 `true` 表示显示源码、`false` 表示显示渲染结果。
- 第一步读 `collapseOnSelectionFacet`：总开关未开则短路返回 `false`；该 Facet 的 `combine` 用 `values[0] ?? false`，默认不开启。
- 第二步读 `mouseSelectingField`：拖拽中一律返回 `false`，避免选区连续变化引发标记闪烁；该状态只由 `setMouseSelecting` Effect 改写。
- 第三步遍历选区 Range，用相交判定 `range.from <= to && range.to >= from` 决定是否返回 `true`；端点包含，多选区任一命中即可。
- 它是纯函数、单一事实来源；调用方（如 `livePreviewPlugin`）只把返回值翻译成 CSS 类切换，决策与表现彻底分离。
- 读测试时要警惕「名不副实」的用例：通过 ≠ 真正覆盖了所声称的分支（见 4.2.4 的拖拽测试）。

## 7. 下一步学习建议

理解了决策核心之后，建议按以下顺序继续：

1. **u2-l3（Facet、mouseSelecting 与 checkUpdateAction）**：本讲只用了 `mouseSelectingField` 的「读」的一面，下一讲会完整讲清 Facet 的 `combine`、StateField/StateEffect 的配合，以及 `checkUpdateAction` 如何统一决定插件何时重建装饰——那是 `livePreviewPlugin` 更新策略的另一半。
2. **u2-l4（livePreviewPlugin）**：去看 `shouldShowSource` 的返回值是如何驱动 `cm-formatting-inline-visible` 类、配合 CSS `max-width` 动画实现标记平滑显隐的。这是「决策」落地为「表现」的完整一跃。
3. 顺手回看本讲的测试文件 [src/core/__tests__/shouldShowSource.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/shouldShowSource.test.ts)，尝试补上 4.2.4 里指出的「真正拖拽」用例，作为本讲的收尾练习。
