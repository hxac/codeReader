# 状态支撑：Facet、mouseSelecting 与 checkUpdateAction

## 1. 本讲目标

上一讲我们剖析了 Live Preview 的「决策心脏」`shouldShowSource`，它只有三个判定步骤却决定了每一段 Markdown 是显示源码还是渲染结果。但你可能注意到：它读取的两个东西——`collapseOnSelectionFacet` 和 `mouseSelectingField`——本身从哪来？又怎么变化？这一讲就来补上这块拼图。

本讲讲解支撑 Live Preview 运转的三块「状态基础设施」：

1. **`collapseOnSelectionFacet`**——用 Facet 定义的 Live Preview 总开关。
2. **`mouseSelectingField` + `setMouseSelecting`**——用 StateField + StateEffect 追踪「用户正在拖选」这一跨事务状态。
3. **`checkUpdateAction`**——把「文档改变 / 视口变化 / 配置重置 / 拖拽 / 选区改变」五类信号，统一归纳成 `rebuild / skip / none` 三个动作，告诉 ViewPlugin 这次更新要不要重新计算装饰。

学完本讲，你应当能够：

- 读懂 `Facet.define` 的 `combine` 用法，理解「多输入合并成单值」的开关语义。
- 读懂 StateField + StateEffect 这对搭档如何把一个布尔标志存进 `EditorState` 并随事务更新。
- 读懂 `checkUpdateAction` 的判定顺序与短路逻辑，并能预测它在不同更新下返回什么动作。
- 看懂这三块基础设施如何被 `shouldShowSource`、`livePreviewPlugin`、`mathPlugin` 共同复用。

## 2. 前置知识

本讲承接 [u2-l1 CodeMirror 6 核心概念速览](u2-l1-codemirror6-core-concepts.md) 与 [u2-l2 shouldShowSource](u2-l2-should-show-source.md)。这里用通俗语言把要用到的几个概念再收紧一次：

- **EditorState（编辑器状态）**：一份不可变的状态快照。任何修改都不是原地改，而是经 Transaction 算出一份新 State，视图再切换过去。
- **Transaction（事务）**：更新的「集装箱」，里面可以同时装三类信息：`changes`（文本改动）、`selection`（选区变化）、`effects`（自定义信号）。
- **StateField（状态字段）**：一个「存在 State 里的值」。它有 `create`（初值）和 `update(value, tr)`（根据事务算新值）。因为它属于 State，所以会被放进快照、能被任意代码用 `state.field(...)` 读到。
- **StateEffect（状态副作用/信号）**：挂在 Transaction 上的「带类型的消息」。它本身不改变文本，只是通知「发生了一件特定的事」（比如「鼠标按下开始拖选了」），由 StateField 在 `update` 里识别并响应。
- **Facet（侧面/配置总线）**：一种「只读配置」机制。多个扩展可以各自向同一个 Facet 提供值，CodeMirror 用一个 `combine` 函数把它们合并成单一结果，任何代码用 `state.facet(...)` 读取。它与 StateField 的关键区别：Facet 是装配时定死的配置，不随普通事务改变（除非整体重配）。
- **ViewUpdate（视图更新）**：ViewPlugin 的 `update(update)` 收到的参数，封装了一次更新前后的全部信息（`update.startState`、`update.state`、`update.docChanged`、`update.selectionSet` 等标志位）。

> 一句话定位：上一讲的 `shouldShowSource` 是「读状态做决策」的函数；本讲讲的正是它读的那两个状态（`collapseOnSelectionFacet`、`mouseSelectingField`）「从哪来、怎么变」，外加 ViewPlugin「什么时候该重新算」的统一裁判 `checkUpdateAction`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [src/core/facets.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/facets.ts) | 定义 `collapseOnSelectionFacet` | 主角 1：总开关 |
| [src/core/mouseSelecting.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts) | 定义 `setMouseSelecting` 与 `mouseSelectingField` | 主角 2：拖拽状态 |
| [src/core/pluginUpdateHelper.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts) | 定义 `checkUpdateAction` 与 `UpdateAction` 类型 | 主角 3：更新决策器 |
| [src/core/__tests__/pluginUpdateHelper.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/pluginUpdateHelper.test.ts) | `checkUpdateAction` 的单元测试 | 实践依据 |
| [src/core/shouldShowSource.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts) | 上一讲主角，消费 facet 与 field | 被读方（佐证用途） |
| [src/plugins/livePreview.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts)、[src/plugins/math.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts) | `update()` 里调用 `checkUpdateAction` | 消费方（佐证用途） |
| [demo/main.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts) | `setupMouseSelecting` 把鼠标事件接到 `setMouseSelecting` | 接线示范 |

这三个文件都位于 `src/core/` 目录——回顾 [u1-l2](u1-l2-source-structure-and-entry.md)，`core` 层存放的就是「被各插件复用的纯逻辑」，本讲三件套正是其中的典型代表。

---

## 4. 核心概念与源码讲解

### 4.1 collapseOnSelectionFacet：用 Facet 定义总开关

#### 4.1.1 概念说明

**Facet 解决的问题是「配置的汇聚」。** 在 CodeMirror 6 里，一个编辑器由许多扩展（extension）拼装而成。假如「是否开启 Live Preview」这个配置散落在各处、各自为政，就很难统一读取。Facet 提供了一条「总线」：

- 任意扩展都可以用 `myFacet.of(值)` 向同一个 Facet「投递」一个值。
- CodeMirror 收集所有投递的值，交给一个 `combine` 函数，算出**唯一的合并结果**。
- 任何代码用 `state.facet(myFacet)` 读取这个唯一结果。

`collapseOnSelectionFacet` 就是这样一个布尔 Facet，扮演 Live Preview 的**总开关**：合出来的值是 `true` 就开启、是 `false` 就关闭。`shouldShowSource` 把它作为第一道关卡——开关没开，后面所有判定都免谈。

#### 4.1.2 核心流程

整个开关的生命周期可以画成：

```text
装配阶段                          运行阶段
┌──────────────────────┐         ┌────────────────────────────┐
│ 扩展里写              │         │ shouldShowSource(state,...) │
│ collapseOnSelection  │         │   ↓                         │
│ Facet.of(true)       │ ──合并→ │ state.facet(               │
│                      │         │   collapseOnSelectionFacet) │
│ combine:             │         │   ↓                         │
│  values[0] ?? false  │         │ true → 继续后续判定          │
│                      │         │ false → 直接 return false   │
└──────────────────────┘         └────────────────────────────┘
```

关键点是 `combine` 的实现 `values[0] ?? false`：

- `values` 是所有 `.of()` 投递值的数组；
- `values[0]` 取**第一个**投递值（CodeMirror 按扩展注册顺序收集）；
- `?? false` 表示「如果没有投递任何值，默认 `false`」。

所以这个开关的语义是：**有人显式给 `true` 才开；否则一律关。**

#### 4.1.3 源码精读

整个 Facet 的定义只有三行，却承载了「开关」的全部语义：

```ts
// src/core/facets.ts
export const collapseOnSelectionFacet = Facet.define<boolean, boolean>({
  combine: (values) => values[0] ?? false,
});
```

> 👉 [src/core/facets.ts:8-10](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/facets.ts#L8-L10)：`Facet.define` 的两个泛型 `<boolean, boolean>` 分别是「单个输入值类型」和「合并后结果类型」，这里都是布尔。`combine` 决定了多个输入如何收敛成一个输出。

它的「被读方」在上一讲已经见过——`shouldShowSource` 的第一步：

```ts
// src/core/shouldShowSource.ts（节选）
const shouldCollapse = state.facet(collapseOnSelectionFacet);
if (!shouldCollapse) {
  return false; // 开关未开，始终返回 false（显示渲染结果）
}
```

> 👉 [src/core/shouldShowSource.ts:33-36](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L33-L36)：`state.facet(...)` 读取合并后的开关值。源码注释把 `false` 描述为「Source mode（显示所有标记）」——注意这是**设计意图**：在 demo 的「源码模式」里，开关被设为 `false` 的同时还会移除 `livePreviewPlugin`，让原始标记作为纯文本直接显示（见 demo/main.ts 的 Source 按钮）。也就是说，开关 `false` 的含义是「关闭折叠决策」，具体表现为「不再按光标位置显隐标记」。

demo 里启用开关的写法：

```ts
// demo/main.ts（节选）
extensions: [
  collapseOnSelectionFacet.of(true),   // 投递 true → 开启 Live Preview
  mouseSelectingField,
  livePreviewPlugin,
  // ...
]
```

> 👉 [demo/main.ts:331](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L331)：`.of(true)` 就是「向这个 Facet 投递一个 `true`」，配合 `combine: values[0] ?? false`，最终 `state.facet(...)` 读到的就是 `true`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `combine: values[0] ?? false` 的合并行为。

**操作步骤**（下面是**示例代码**，不是项目原有代码；可在项目根目录用 `npx tsx` 临时运行，或写成一个小测试放进 `src/core/__tests__/`）：

```ts
// 示例代码：演示 collapseOnSelectionFacet 的 combine 行为
import { EditorState } from '@codemirror/state';
import { collapseOnSelectionFacet } from '../src/core/facets';

const s1 = EditorState.create({ extensions: [] }); // 不投递任何值
const s2 = EditorState.create({ extensions: [collapseOnSelectionFacet.of(true)] });
const s3 = EditorState.create({ extensions: [collapseOnSelectionFacet.of(false)] });
// 多次投递：combine 取 values[0]
const s4 = EditorState.create({
  extensions: [collapseOnSelectionFacet.of(true), collapseOnSelectionFacet.of(false)],
});

console.log(s1.facet(collapseOnSelectionFacet)); // 预期 false（默认值）
console.log(s2.facet(collapseOnSelectionFacet)); // 预期 true
console.log(s3.facet(collapseOnSelectionFacet)); // 预期 false
console.log(s4.facet(collapseOnSelectionFacet)); // 预期 true（第一个投递值胜出）
```

**需要观察的现象**：四行输出依次为 `false / true / false / true`。

**预期结果**：`s4` 证明了「多个 `.of()` 投递时，`values[0]`（即第一个 `true`）胜出」，第二个 `false` 被忽略。若你对 `??` 操作符不熟，可把它换成 `values.length ? values[0] : false` 理解，效果一致。

> 待本地验证：若你的运行环境没有 `@codemirror/state`，可先 `npm install` 再运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `combine` 改成 `(values) => values.some(Boolean)`，开关的语义会变成什么？

> **答案**：变成「只要任意一个扩展投递了 `true` 就开启」。`some(Boolean)` 只要数组里有真值就返回 `true`，与原实现「只认第一个」不同。原实现用 `values[0]` 是为了让「后装配的扩展不能意外覆盖先装配的」。

**练习 2**：为什么 `shouldShowSource` 要把 Facet 判定放在最前面，而不是放在选区相交判定之后？

> **答案**：因为它是**总开关**。开关一关，整段 Live Preview 逻辑都不该生效，提前 `return false` 可以短路掉后面所有（更昂贵的）选区遍历，既清晰又高效。

---

### 4.2 mouseSelectingField：用 StateField + StateEffect 追踪拖拽状态

#### 4.2.1 概念说明

**问题**：用户用鼠标拖拽选区时，选区会**高频、连续地变化**（每移动一个像素都可能产生一次选区更新）。如果插件每次都跟着选区重建装饰，标记会随着选区扫过而反复显隐，造成明显的**闪烁**，也会带来不必要的性能开销。

**解决思路**：用一个布尔标志记录「用户当前是否正在拖选」。拖拽期间，决策函数和插件都据此「按兵不动」。

**为什么用 StateField + StateEffect 这对搭档？**

- 这个标志需要**跨多次事务存活**（按下到松手之间会有很多次事务），所以它必须被存进 `EditorState`，成为状态快照的一部分——这正是 StateField 的职责。
- 而改变这个标志的「事件」（鼠标按下 / 松手）并不改变文本，它是一种**自定义信号**——这正是 StateEffect 的职责。

二者配合的标准模式是：

1. 外部代码派发一个 `setMouseSelecting.of(值)` 效果；
2. `mouseSelectingField.update(value, tr)` 从事务的效果列表里认出它，返回新值；
3. 任何代码用 `state.field(mouseSelectingField)` 读取当前是否在拖拽。

#### 4.2.2 核心流程

拖拽标志是一个简单的两态状态机：

```text
       mousedown（派发 setMouseSelecting.of(true)）
false ─────────────────────────────────────────────→ true
 ↑                                                      │
 │              mouseup（派发 setMouseSelecting.of(false)）│
 └──────────────────────────────────────────────────────┘
```

对应的代码流转：

```text
demo: setupMouseSelecting
  mousedown  → view.dispatch({ effects: setMouseSelecting.of(true)  })
  mouseup    → view.dispatch({ effects: setMouseSelecting.of(false) })  (套一层 requestAnimationFrame)
                        │
                        ▼
mouseSelectingField.update(value, tr):
  遍历 tr.effects → 命中 setMouseSelecting → 返回 effect.value
                        │
                        ▼
两个消费方读取：
  · shouldShowSource：读到 true → 返回 false（不显示源码）
  · checkUpdateAction：读到 true → 返回 'skip'（不重建装饰）
```

这里有个**防御性读取**的小细节贯穿全项目：调用方都写成 `state.field(mouseSelectingField, false)`。第二个参数 `false` 的含义是「如果该字段不在扩展里，不要抛错，返回 `undefined`」。这样即使有人忘了把 `mouseSelectingField` 加进扩展，程序也不会崩——`undefined` 是假值，会被当作「没在拖拽」处理。

#### 4.2.3 源码精读

先看 Effect 的定义——一行代码定义一个「带类型的信号」：

```ts
// src/core/mouseSelecting.ts
export const setMouseSelecting = StateEffect.define<boolean>();
```

> 👉 [src/core/mouseSelecting.ts:6](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts#L6)：`StateEffect.define<boolean>()` 产出一个「携带布尔值」的效果类型。用 `setMouseSelecting.of(true)` 造实例，用 `effect.is(setMouseSelecting)` 判断类型。

再看 StateField——核心是 `update` 里对效果的识别：

```ts
// src/core/mouseSelecting.ts
export const mouseSelectingField = StateField.define<boolean>({
  create: () => false,                 // 初值：没在拖拽
  update(value, tr) {
    for (const effect of tr.effects) {
      if (effect.is(setMouseSelecting)) {   // 命中我们的效果
        return effect.value;                 // 用效果携带的值替换
      }
    }
    return value;                       // 没命中则保持不变
  },
});
```

> 👉 [src/core/mouseSelecting.ts:13-23](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/mouseSelecting.ts#L13-L23)：`update` 遍历事务里的所有效果，只认 `setMouseSelecting`，其它效果一律忽略（`return value` 保持原值）。这是 StateField 处理「命令式信号」的标准写法。

接线在 demo 里完成——把浏览器鼠标事件翻译成效果派发：

```ts
// demo/main.ts（节选）
function setupMouseSelecting(view: EditorView) {
  view.contentDOM.addEventListener('mousedown', () => {
    view.dispatch({ effects: setMouseSelecting.of(true) });
  });
  document.addEventListener('mouseup', () => {
    requestAnimationFrame(() => {
      view.dispatch({ effects: setMouseSelecting.of(false) });
    });
  });
}
```

> 👉 [demo/main.ts:309-319](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/demo/main.ts#L309-L319)：`mousedown` 挂在编辑器内容区（`contentDOM`），`mouseup` 挂在 `document`（因为松手时鼠标可能已移出编辑器）。`mouseup` 里套 `requestAnimationFrame`，是为了把「结束拖拽」的效果推迟到下一帧，让它发生在浏览器本次鼠标事件的默认处理之后，避免与同一次选区更新产生竞态（精确时序待本地验证）。

最后是被读方——上一讲 `shouldShowSource` 的第二步：

```ts
// src/core/shouldShowSource.ts（节选）
const isDragging = state.field(mouseSelectingField, false);
if (isDragging) {
  return false; // 拖拽中一律返回 false（不显示源码），避免闪烁
}
```

> 👉 [src/core/shouldShowSource.ts:39-42](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/shouldShowSource.ts#L39-L42)：注意第二个参数 `false`——即使字段不存在也不抛错，返回 `undefined`（假值）。拖拽中直接返回 `false`，让标记保持隐藏。

#### 4.2.4 代码实践

**实践目标**：亲手派发效果，观察 `mouseSelectingField` 随之翻转。

**操作步骤**（**示例代码**，可放进 `src/core/__tests__/mouseSelecting.demo.ts` 临时运行，或直接对照项目里 [pluginUpdateHelper.test.ts:63-80](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/pluginUpdateHelper.test.ts#L63-L80) 的「拖拽开始/结束」用例阅读）：

```ts
// 示例代码：观察 mouseSelectingField 随 effect 变化
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { mouseSelectingField, setMouseSelecting } from '../src/core/mouseSelecting';

const view = new EditorView({
  state: EditorState.create({ doc: 'hello', extensions: [mouseSelectingField] }),
  parent: document.createElement('div'),
});

console.log(view.state.field(mouseSelectingField)); // 预期 false（create 默认）
view.dispatch({ effects: setMouseSelecting.of(true) });
console.log(view.state.field(mouseSelectingField)); // 预期 true（按下）
view.dispatch({ effects: setMouseSelecting.of(false) });
console.log(view.state.field(mouseSelectingField)); // 预期 false（松手）
```

**需要观察的现象**：三次读取依次为 `false / true / false`，与状态机一致。

**预期结果**：再追加一次「派发一个无关效果」（比如自定义另一个 `StateEffect.define()`），`field` 值应**保持不变**——验证 `update` 只认 `setMouseSelecting`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mouseup` 监听挂在 `document` 而不是 `view.contentDOM`？

> **答案**：用户按下后可能把鼠标拖出编辑器再松手。如果只挂在 `contentDOM`，这种情况下 `mouseup` 收不到，`mouseSelectingField` 会永远卡在 `true`，导致后续所有选区变化都被判为「拖拽中」而跳过重建。挂在 `document` 才能保证松手事件一定被捕获。

**练习 2**：如果用户没把 `mouseSelectingField` 加入扩展，`shouldShowSource` 会崩溃吗？行为是什么？

> **答案**：不会崩溃。因为读取用了 `state.field(mouseSelectingField, false)`，字段缺失时返回 `undefined`（假值），`if (isDragging)` 不成立，相当于「永远不认为在拖拽」。代价是拖拽时不再防闪烁——这正是 demo 里务必同时装配 `mouseSelectingField` 与接线 `setupMouseSelecting` 的原因。

---

### 4.3 checkUpdateAction：统一的更新决策器

#### 4.3.1 概念说明

ViewPlugin 的 `update(update)` 方法在**每一次事务后都会被调用**。如果插件每次都重新遍历整棵语法树重建装饰，既浪费（大多数事务根本不影响装饰），又会闪烁（尤其在拖拽时）。

`checkUpdateAction` 把「这次更新我到底该干嘛」这个反复出现的判断**抽成一处**，返回三种动作之一：

| 动作 | 含义 | 调用方通常的响应 |
| --- | --- | --- |
| `'rebuild'` | 装饰需要重建 | 重新遍历语法树，重算 `this.decorations` |
| `'skip'` | 本次跳过（如拖拽中） | 什么都不做，沿用旧装饰 |
| `'none'` | 无需动作 | 什么都不做，沿用旧装饰 |

> `'skip'` 与 `'none'` 对调用方而言行为相同（都不重建），但语义上区分了「主动跳过」与「确实没事」，便于阅读和将来扩展。

它综合了两类信息：

1. **ViewUpdate 的标志位**：`docChanged`（文本变了）、`viewportChanged`（视口滚动、新行进入可见区）、`selectionSet`（选区/光标变了）、`transactions.some(t => t.reconfigured)`（扩展被重配，如 Live/Source 模式切换）。
2. **拖拽状态的前后对比**：通过 `update.state`（更新后）与 `update.startState`（更新前）分别读 `mouseSelectingField`，比较出「拖拽是否刚结束」。

#### 4.3.2 核心流程

判定是**按优先级顺序短路**的，顺序非常关键：

```text
checkUpdateAction(update):
  ① docChanged || viewportChanged || 任一事务 reconfigured
        └─ 是 → return 'rebuild'        // 结构性变化，必须重算

  ② 读取 isDragging（新状态）、wasDragging（旧状态）

  ③ wasDragging && !isDragging
        └─ 是 → return 'rebuild'        // 拖拽刚结束，补回拖拽期间跳过的重建

  ④ isDragging
        └─ 是 → return 'skip'           // 拖拽进行中，按兵不动防闪烁

  ⑤ selectionSet
        └─ 是 → return 'rebuild'        // 光标移动（非拖拽），重算显隐

  ⑥ 否则 → return 'none'               // 无关更新
```

**为什么 ④（拖拽中）要排在 ⑤（选区改变）之前？** 因为拖拽时选区本来就在持续变化——`selectionSet` 此时也是 `true`。若 ⑤ 排在前面，拖拽时就会不断重建、产生闪烁。把 ④ 放前面，就能把「拖拽中的选区变化」拦截成 `'skip'`，只有非拖拽的光标移动（比如键盘移动光标、点击定位）才会落到 ⑤ 触发 `'rebuild'`。

同理 ③（拖拽刚结束）排在 ④ 之前：松手那一次更新里 `isDragging` 已变 `false`，不能被 ④ 误判为「没事」，必须补一次重建刷新到正确终态。

#### 4.3.3 源码精读

类型定义一目了然：

```ts
// src/core/pluginUpdateHelper.ts
export type UpdateAction = 'rebuild' | 'skip' | 'none';
```

> 👉 [src/core/pluginUpdateHelper.ts:10](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts#L10)：三种动作的字面量联合类型。

函数体按上面流程图的顺序展开：

```ts
// src/core/pluginUpdateHelper.ts（节选，已分段对应流程）
export function checkUpdateAction(update: ViewUpdate): UpdateAction {
  // ① 文档/视口/配置变化：必须重建
  if (
    update.docChanged ||
    update.viewportChanged ||
    update.transactions.some((t) => t.reconfigured)
  ) {
    return 'rebuild';
  }

  // ② 读取拖拽前后状态
  const isDragging = update.state.field(mouseSelectingField, false);
  const wasDragging = update.startState.field(mouseSelectingField, false);

  // ③ 拖拽刚结束：重建
  if (wasDragging && !isDragging) {
    return 'rebuild';
  }

  // ④ 拖拽中：跳过
  if (isDragging) {
    return 'skip';
  }

  // ⑤ 选区改变：重建
  if (update.selectionSet) {
    return 'rebuild';
  }

  return 'none'; // ⑥ 无关更新
}
```

> 👉 [src/core/pluginUpdateHelper.ts:35-65](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts#L35-L65)：注意 ② 用 `update.startState.field(...)` 拿「更新前」的拖拽态、用 `update.state.field(...)` 拿「更新后」的拖拽态——前后对比正是识别「拖拽结束」的关键。两处都带第二参数 `false`，防御字段缺失。

**消费方 1：`livePreviewPlugin`**——只关心是不是 `'rebuild'`：

```ts
// src/plugins/livePreview.ts（节选）
update(update: ViewUpdate) {
  if (checkUpdateAction(update) === 'rebuild') {
    this.decorations = this.build(update.view);
  }
}
```

> 👉 [src/plugins/livePreview.ts:55-59](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/livePreview.ts#L55-L59)：`'skip'` 和 `'none'` 都被忽略，沿用上一份 `this.decorations`。`mathPlugin` 的行内公式插件采用**完全相同**的写法（[src/plugins/math.ts:44-48](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L44-L48)）——这正是把判定抽成公共函数的收益。

**消费方 2（等价复刻）：块级公式的 `blockMathField`**。回顾 [u2-l1](u2-l1-codemirror6-core-concepts.md)：块级装饰必须用 StateField，而 StateField 拿到的是 `Transaction` 不是 `ViewUpdate`，没法直接调 `checkUpdateAction`。于是它**把同一套判定逻辑用 StateField 的语言重写了一遍**：

```ts
// src/plugins/math.ts（blockMathField.update 节选）
update(deco, tr) {
  if (tr.docChanged || tr.reconfigured) {              // 对应 ①
    return buildBlockMathDecorations(tr.state);
  }
  const isDragging = tr.state.field(mouseSelectingField, false);
  const wasDragging = tr.startState.field(mouseSelectingField, false);
  if (wasDragging && !isDragging) {                    // 对应 ③
    return buildBlockMathDecorations(tr.state);
  }
  if (isDragging) {                                    // 对应 ④
    return deco;                                       // 沿用旧装饰 ≈ 'skip'
  }
  if (tr.selection) {                                  // 对应 ⑤
    return buildBlockMathDecorations(tr.state);
  }
  return deco;                                         // 对应 ⑥ 'none'
}
```

> 👉 [src/plugins/math.ts:157-178](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/plugins/math.ts#L157-L178)：把这段与上面的 `checkUpdateAction` 逐行对照——判定顺序、短路逻辑完全一致，只是「返回 rebuild」变成了「返回重新构建的装饰」，「返回 skip/none」变成了「返回旧 `deco`」。这说明 `checkUpdateAction` 抽象出的决策规则是整个项目的通用模式，只是 ViewPlugin 与 StateField 两条路径各自用自己的语法表达。

#### 4.3.4 代码实践

**实践目标**：用项目自带的测试，验证 `checkUpdateAction` 在五种典型更新下的返回值。

**操作步骤**：

1. 在项目根目录运行：

   ```bash
   npx vitest run src/core/__tests__/pluginUpdateHelper.test.ts
   ```

2. 打开 [src/core/__tests__/pluginUpdateHelper.test.ts](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/pluginUpdateHelper.test.ts) 阅读。该测试用一个「捕获插件」把每次 `update` 的动作记录下来（[L10-28](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/pluginUpdateHelper.test.ts#L10-L28) 的 `createCapturePlugin`），再 `view.dispatch(...)` 触发不同更新。

3. 把每个用例的 dispatch 与断言对应到 4.3.2 的流程步骤：
   - 改文本（[L49-61](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/pluginUpdateHelper.test.ts#L49-L61)）→ 命中 ① → `'rebuild'`；
   - 拖拽结束（[L63-80](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/pluginUpdateHelper.test.ts#L63-L80)）→ 命中 ③ → `'rebuild'`；
   - 改选区（[L82-94](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/pluginUpdateHelper.test.ts#L82-L94)）→ 命中 ⑤ → `'rebuild'`；
   - 拖拽中改选区（[L98-116](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/pluginUpdateHelper.test.ts#L98-L116)）→ 命中 ④ → `'skip'`；
   - 空事务（[L119-129](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/__tests__/pluginUpdateHelper.test.ts#L119-L129)）→ 命中 ⑥ → `'none'`。

**需要观察的现象**：5 个用例全部通过（`Test Files 1 passed`）。

**预期结果**：测试通过即证明 4.3.2 的判定顺序与代码一致。特别注意「拖拽中改选区」用例：尽管 `selectionSet` 为真，函数仍返回 `'skip'`——这验证了 ④ 排在 ⑤ 之前。

#### 4.3.5 小练习与答案

**练习 1**：如果把 ④（`if (isDragging) return 'skip'`）和 ⑤（`if (update.selectionSet) return 'rebuild'`）的顺序对调，会发生什么？

> **答案**：拖拽时选区不断变化，`selectionSet` 恒为真，于是每次都返回 `'rebuild'`，装饰会随选区扫过反复重算——正是我们想消除的闪烁。所以 ④ 必须在 ⑤ 前。

**练习 2**：为什么 `blockMathField` 不直接调用 `checkUpdateAction`，而要重写一遍逻辑？

> **答案**：因为 `checkUpdateAction` 接收的是 `ViewUpdate`（ViewPlugin 专用），而 StateField 的 `update(deco, tr)` 收到的是 `Transaction`，两者 API 不同（如 `tr.docChanged` vs `update.docChanged`、`tr.selection` vs `update.selectionSet`）。StateField 拿不到 `ViewUpdate`，只能用 Transaction 的语言复刻同一套规则。

**练习 3**：一次「先改文本、同时又在拖拽」的更新（`docChanged=true` 且 `isDragging=true`），函数返回什么？为什么？

> **答案**：返回 `'rebuild'`。因为 ① 排在最前，`docChanged` 直接命中 `'rebuild'`，根本走不到拖拽判定。这是合理的：文本都变了，装饰必然要重算，不能因为「在拖拽」就跳过。

---

## 5. 综合实践

本实践要求你把三块基础设施串起来，画出一张完整的「更新决策表」。它综合考察你对 `collapseOnSelectionFacet`（配置重置路径）、`mouseSelectingField`（拖拽前后状态）、`checkUpdateAction`（判定顺序）三者的理解。

**实践目标**：针对「文档改变 / 拖拽中 / 拖拽刚结束 / 选区改变 / 配置重置」五种典型场景，写出 `checkUpdateAction` 的返回动作并解释理由。

**操作步骤**：

1. 复习 [src/core/pluginUpdateHelper.ts:35-65](https://github.com/blueberrycongee/codemirror-live-markdown/blob/f1438ab14f4641b761af40ccf764653624c923f1/src/core/pluginUpdateHelper.ts#L35-L65) 的判定顺序（4.3.2 流程图）。
2. 填写下面这张表（先自己写，再对照下方的参考答案）：

   | 场景（主触发） | 关键信号 | 返回动作 | 理由 |
   | --- | --- | --- | --- |
   | 文档改变 | `docChanged=true` | ？ | ？ |
   | 拖拽中 | `isDragging=true`（`selectionSet` 也为真但被先拦截） | ？ | ？ |
   | 拖拽刚结束 | `wasDragging=true && isDragging=false` | ？ | ？ |
   | 选区改变（非拖拽） | `selectionSet=true` 且非拖拽 | ？ | ？ |
   | 配置重置 | `transactions.some(t=>t.reconfigured)` | ？ | ？ |

3. 进阶：为每一行标注「它依赖了本讲哪一块基础设施」。例如「配置重置」依赖 Facet（因为 `reconfigured` 正是 `Compartment.reconfigure`——demo 里切 Live/Source 模式时改变 `collapseOnSelectionFacet` 与插件组合时产生的）。

**参考答案**：

| 场景 | 关键信号 | 返回动作 | 理由 | 依赖的基础设施 |
| --- | --- | --- | --- | --- |
| 文档改变 | `docChanged=true` | `rebuild` | 文本变了，标记区间可能增删移位，必须重算 | ①（ViewUpdate 标志位） |
| 拖拽中 | `isDragging=true` | `skip` | 拖拽时选区高频变化，重建会闪烁；沿用旧装饰 | ②`mouseSelectingField` |
| 拖拽刚结束 | `wasDragging=true && !isDragging` | `rebuild` | 拖拽期间被 `skip` 跳过的重建，在松手后补上，刷新到正确终态 | ②`mouseSelectingField` |
| 选区改变（非拖拽） | `selectionSet=true` 且非拖拽 | `rebuild` | 光标移动（键盘/点击定位），需重新评估哪些标记该显示 | ①（ViewUpdate 标志位） |
| 配置重置 | `reconfigured=true` | `rebuild` | `Compartment.reconfigure` 改变了扩展集合（如 Live↔Source 切换），必须从头重建 | ①+`collapseOnSelectionFacet`（开关随重配改变） |

**需要观察的现象**：你能不查代码、仅凭判定顺序准确预测每一行；并能指出「拖拽中」那一行之所以是 `skip` 而非 `rebuild`，是因为 ④ 排在 ⑤ 之前。

**预期结果**：表格与参考答案一致。若某一行判断错误，回到 4.3.2 的流程图定位是哪一步短路出了问题。

---

## 6. 本讲小结

- **`collapseOnSelectionFacet`** 是用 `Facet.define` 定义的布尔总开关，`combine: values[0] ?? false` 表示「取第一个投递值、无则默认关」；`shouldShowSource` 第一步读它，关则直接返回 `false`。
- **`mouseSelectingField` + `setMouseSelecting`** 是「StateField + StateEffect」的标准搭档：外部派发 `setMouseSelecting.of(布尔)`，字段 `update` 识别该效果并翻转值，记录「是否正在拖选」这一跨事务状态。
- **拖拽防闪烁**是双保险：`shouldShowSource` 读到拖拽返回 `false`、`checkUpdateAction` 读到拖拽返回 `'skip'`，从「决策」和「刷新」两端同时按兵不动。
- **`checkUpdateAction`** 按 `①结构性变化 → ③拖拽结束 → ④拖拽中 → ⑤选区改变 → ⑥无` 的顺序短路，返回 `rebuild/skip/none`；`livePreviewPlugin` 与 `mathPlugin` 都只认 `'rebuild'`。
- **同一套决策规则**在 StateField（`blockMathField`）里用 `Transaction` 语言等价复刻——ViewPlugin 与 StateField 两条装饰路径共享一致的更新语义。
- 调用方普遍使用 `state.field(mouseSelectingField, false)` 的**防御性读取**（第二参数 `false` 表示字段缺失时不抛错），保证缺失装配也能优雅降级。

## 7. 下一步学习建议

本讲的三件套是「读状态、判动作」的底层支撑。接下来建议：

1. **[u2-l4 livePreviewPlugin](u2-l4-live-preview-plugin.md)**：看 `build()` 如何在 `checkUpdateAction` 放行后，遍历语法树、结合 `shouldShowSource` 决定给每个标记加不加 `-visible` 类——把本讲的「何时重建」与上一讲的「显示哪面」连成完整一条线。
2. **[u2-l5 markdownStylePlugin](u2-l5-markdown-style-plugin.md)**：对比它为何不需要 `shouldShowSource`（内容样式不随光标显隐），体会职责分离。
3. 后续的 math / table / code / image / link 各特性插件，都会复用本讲的 `shouldShowSource`、`checkUpdateAction`、`mouseSelectingField`——读到它们的 `update()` 时，回头看本讲的决策表会非常顺畅。
