# 栈布局 StackLayouter

## 1. 本讲目标

`stack`（栈）是 Typst 里最朴素也最通用的「线性」排版器：把一组子项沿一个方向首尾相接排列。它本身代码只有 400 余行，却同时承担两个职责——

1. 作为 `stack` 元素的排版入口（`#stack(...)`）；
2. 作为一个**可被其他排版器复用的积木**：列表（`list`/`enum`/`terms`）并不自己写一遍「纵向堆叠」逻辑，而是复用栈布局。

学完本讲，你应当能够：

- 说清 `layout_stack` / `layout_stack_internal` / `StackLayouter` 三者的分工，以及 `StackLayoutChild::CustomLayouter` 为何存在；
- 理解「主轴（main）/ 交叉轴（cross）」如何用一个 `GenericSize` 结构抽象出来，让同一份代码同时支持横向（LTR/RTL）与纵向（TTB/BTT）栈；
- 理解绝对间距与分数（`Fr`）间距的区别，以及 `Fr::share` 如何瓜分剩余空间；
- 理解 `finish_region` 中的「ruler（标尺）累积对齐」机制——为什么栈里后出现的子项可以被推到另一端；
- 给定一组 frame 与若干 `Fr` 间距，能手算它们在主轴上的最终位置。

## 2. 前置知识

本讲默认你已掌握前置讲义建立的认知，这里只做最简回顾并补三个本讲要用到的小概念。

- **Regions / Region（区域）**（见 u2-l2）：栈的可用画布。`regions.size` 是当前剩余尺寸，`regions.expand` 决定是否要填满区域，`regions.base()` 给出相对尺寸的基准。栈会消费 `regions`、把放不下的内容推到下一个区域。
- **Frame / Fragment（帧 / 片段）**（见 u2-l3）：排版的产物。一个 `Frame` 是一块带尺寸的二维画布；`Fragment` 是一组 `Frame`（可断裂内容会产出多帧）。栈把每个子项排成帧，再逐帧贴到输出帧上。
- **flow（流式块级布局）**（见 u4-l1）：栈不是 flow，但二者同属「块级」世界。栈只做最简单的「沿主轴拼接」，不处理脚注、浮动体、段落断行——那些是 flow 的事。栈排一个块时，会把块交给 `crate::layout_fragment`（内部最终走到 flow）。

本讲新增的三个小概念：

1. **`Fr`（fraction，分数）**：表示「按比例瓜分剩余空间」的量。`1fr` 的实际宽度不是固定值，而是「所有 `fr` 加起来后，按各自占比分摊剩余空间」。例如剩余 120pt、总共有 `2fr`，则一个 `1fr` 占 60pt。
2. **`Spacing`（间距种类）**：栈里子项之间的空白分两种——`Spacing::Rel`（绝对/相对长度，如 `10pt`、`20%`）与 `Spacing::Fr`（分数，如 `1fr`）。二者在 `finish_region` 里的处理方式完全不同。
3. **主轴 / 交叉轴**：栈沿「主轴」方向堆叠；垂直于主轴的方向叫「交叉轴」。纵向栈（TTB）主轴是 Y、交叉轴是 X；横向栈（LTR）主轴是 X、交叉轴是 Y。同一份代码要同时服务两种朝向，靠的就是后面要讲的 `GenericSize`。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| `src/stack.rs` | 栈布局的全部实现：公共入口 `layout_stack`、可复用入口 `layout_stack_internal`、核心状态机 `StackLayouter`、主/交叉轴抽象 `GenericSize`。 |

辅助理解的外部类型（定义在 `typst-library`，本 crate 只消费）：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-library/src/layout/fr.rs` | `Fr` 类型与 `share` 方法（分数分摊）。 |
| `crates/typst-library/src/layout/spacing.rs` | `Spacing` 枚举（`Rel` / `Fr`）。 |
| `crates/typst-library/src/layout/dir.rs` | `Dir`（四向）及其 `axis()` / `start()` / `is_positive()`。 |
| `crates/typst-library/src/layout/align.rs` | `FixedAlignment`（Start/Center/End）及其 `position()`。 |
| `crates/typst-library/src/layout/stack.rs` | `StackElem` / `StackChild` 的元素定义。 |
| `crates/typst-layout/src/lists.rs` | 复用 `layout_stack_internal` + `CustomLayouter` 的范例。 |
| `crates/typst-layout/src/rules.rs` | `STACK_RULE`：把 `layout_stack` 挂成 `stack` 元素的排版器。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：入口与可复用架构、主/交叉轴通用化、`StackLayouter` 状态与循环、间距处理、`finish_region` 定位与对齐。

### 4.1 入口与可复用架构

#### 4.1.1 概念说明

栈布局有**两个**入口函数，这是它和 flow/inline 等排版器最大的不同之处：

- `layout_stack`：给 `stack` 元素自己用，输入是 `Packed<StackElem>`；
- `layout_stack_internal`：泛型版，输入是一组 `StackLayoutChild`，**列表等其它排版器走这个**。

为什么要拆两个？因为 Typst 的元素（`Content`）有一个硬限制：元素内部**不能持有借用或生命周期泛型**。而列表排版时，每个列表项需要一个「能借用外部环境数据」的自定义排版闭包（例如借用预先量好的 marker 宽度）。这种闭包没法塞进 `Content`。所以 `layout_stack_internal` 接受一个额外的枚举 `StackLayoutChild`，其中 `CustomLayouter(F)` 变体允许传入任意「给我 Engine/StyleChain/Regions，我还你一个 Fragment」的闭包，绕开 `Content` 的限制。

#### 4.1.2 核心流程

```text
layout_stack(Packed<StackElem>)
   │  把 children 包装成 StackLayoutChild::StackChild
   ▼
layout_stack_internal(children, spacing, dir, ...)
   │  构造 StackLayouter
   │  for child in children:
   │      Spacing     → layouter.layout_spacing(...)
   │      Block       → layouter.layout_block(...)   // 内部调 crate::layout_fragment
   │      CustomLayouter → layouter.layout_custom_layouter(...)  // 调用户闭包
   │  layouter.finish()  → 逐区域 finish_region → Fragment
```

#### 4.1.3 源码精读

`layout_stack` 只是 `layout_stack_internal` 的一个薄封装——它把 `StackElem` 的字段拆出来，并把每个 child 用 `From::from` 转成 `StackLayoutChild::StackChild`：

[layout_stack 封装:src/stack.rs:14-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L14-L31) —— 注意它把泛型参数显式写成 `fn(&mut Engine, StyleChain, Regions) -> SourceResult<Fragment>`，因为这里只产生 `StackChild` 变体、并不会真正用到 `CustomLayouter`，但类型参数仍需指定。

`StackLayoutChild` 是关键的可复用扩展点：

[StackLayoutChild 枚举:src/stack.rs:36-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L36-L44) —— `StackChild` 变体与普通栈孩子等价；`CustomLayouter(F)` 变体携带一个自定义排版闭包。其文档注释直接点明了用途：「 Useful when using stack layout to create other layouters, such as that of lists.」

`layout_stack_internal` 的主循环把三种孩子分流处理。其中两个细节值得注意：**`h`/`v` 的透明处理**，以及 **`deferred`（延迟间距）机制**：

[layout_stack_internal 主循环:src/stack.rs:68-125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L68-L125)

- 当栈是横向（`Axis::X`）时，孩子里的 `h` 元素被当成普通间距处理；纵向时 `v` 元素同理（L92-104）。这解释了为什么在 `#stack(dir: rtl)[A][#h(1em)][B]` 里 `h` 表现得像间距。
- `deferred` 实现「自动间距」：栈的 `spacing` 字段（如 `#stack(spacing: 5pt)`）只在**两个实际内容之间**插入一次。遇到内容块时，若 `deferred` 有值就先补上间距，再把 `spacing` 存入 `deferred`；遇到显式间距则把 `deferred` 清空（避免与用户间距叠加）。这样连续两个块之间恰好一份 spacing，而末尾、首部不会多出间距。

`CustomLayouter` 的真实用法在 `lists.rs`：列表把每个列表项包装成一个自定义排版闭包，闭包内部调用 `layout_item`（同时排 marker 与 body），再把这一串闭包交给 `layout_stack_internal`：

[列表复用栈布局:src/lists.rs:280-303](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L280-L303) —— 注意它用 `Dir::TTB`（列表总是纵向），并把 gutter 作为 `spacing` 传入。这就是「列表不自己实现堆叠，复用栈」的落点。

而 `stack` 元素如何被驱动？通过 `rules.rs` 的 `STACK_RULE` 挂成 `BlockElem::multi_layouter`：

[STACK_RULE 注册:src/rules.rs:682-683](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L682-L683) —— 这与前置讲义 u1-l3 讲过的「挂 layouter 型规则」模式一致：`stack` 元素本身不在 `lib.rs` 导出，而是经此规则接入排版流程。

#### 4.1.4 代码实践

**实践目标**：确认「两个入口、一个复用点」的结构。

**操作步骤**：

1. 在 `src/stack.rs` 中找到 `layout_stack` 与 `layout_stack_internal` 两个函数的签名。
2. 在 `src/lists.rs` 中找到 `layout_stack_internal(` 的调用点，确认列表传的是 `CustomLayouter` 闭包序列、`Dir::TTB`。
3. 在 `src/rules.rs` 中找到 `STACK_RULE`，确认它把 `crate::stack::layout_stack` 挂成了 `multi_layouter`。

**需要观察的现象**：`layout_stack` 的函数体只有一行 `layout_stack_internal::<...>(...)` 调用；`layout_stack_internal` 是 `pub` 的，而 `StackLayouter` 是 `struct`（私有）——对外只暴露函数，不暴露内部状态机。

**预期结果**：能画出 `stack` 元素 → `layout_stack` → `layout_stack_internal` → `StackLayouter`，以及 `list` 元素 → `layout_stack_internal`（绕过 `layout_stack`）→ `StackLayouter` 的两条路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `layout_stack_internal` 要做成泛型 `F: Fn(...)`，而 `layout_stack` 不是？

**参考答案**：`layout_stack` 只处理来自 `StackElem` 的静态 children，全部转成 `StackLayoutChild::StackChild`，不产生 `CustomLayouter`，因此不需要泛型 `F`；但因为它调用 `layout_stack_internal`，仍需指定一个具体的 `F` 类型参数（写成函数指针类型占位）。`layout_stack_internal` 必须泛型，才能让 lists 等传入借用外部数据的闭包。

**练习 2**：`deferred` 机制中，为什么遇到显式 `StackChild::Spacing` 时要把 `deferred = None`？

**参考答案**：显式间距表示用户已主动指定空白，此时不应再叠加自动 `spacing`；清空 `deferred` 可避免「自动间距 + 用户间距」重复。

---

### 4.2 主轴/交叉轴的通用化：GenericSize

#### 4.2.1 概念说明

栈要支持四个方向（`ltr`/`rtl`/`ttb`/`btt`）。如果在代码里到处写 `if axis == X { 用 x } else { 用 y }`，会既啰嗦又易错。Typst 的做法是用一个小结构 `GenericSize<T>`，把坐标**重命名**成与朝向无关的两个分量：

- `main`：沿主轴的分量（堆叠方向上的位置/长度）；
- `cross`：沿交叉轴的分量。

这样，栈的算法只用 `main`/`cross` 表达，至于它俩到底对应 `x` 还是 `y`，只在「与外界交互」（读 `Size`、写 `Point`）时通过 `Axis` 转换一次。

#### 4.2.2 核心流程

`GenericSize` 的两个关键转换：

```text
从具体坐标构造（已知 axis）：
  Axis::X → GenericSize { cross: size.y, main: size.x }   // 横向栈：主轴是 x
  Axis::Y → GenericSize { cross: size.x, main: size.y }   // 纵向栈：主轴是 y

还原成具体坐标（已知 axis）：
  Axis::X → Axes { x: main, y: cross }
  Axis::Y → Axes { x: cross, y: main }
```

注意 `GenericSize` 字段顺序是 `cross, main`（见结构定义），构造时 `GenericSize::new(cross, main)` 别传反。

#### 4.2.3 源码精读

`GenericSize` 是个极简结构，定义在 stack.rs 文件末尾：

[GenericSize 定义:src/stack.rs:381-402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L381-L402) —— 字段是 `cross` 在前、`main` 在后；`into_axes(main)` 按 main 轴还原成具体的 `Axes<T>`。

它的实际用法出现在两处。一是在 `layout_fragment` 里把帧的具体尺寸转成通用尺寸来累计：

[帧尺寸转通用尺寸:src/stack.rs:284-290](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L284-L290) —— `used.main += generic_size.main`（主轴累加），`used.cross.set_max(generic_size.cross)`（交叉轴取最大，因为栈的交叉尺寸由最宽的孩子决定）。

二是在 `finish_region` 里把通用坐标还原成 `Point` 贴帧：

[通用坐标还原成 Point:src/stack.rs:355-357](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L355-L357) —— `GenericSize::new(cross, main).to_point(self.axis)` 把 (cross, main) 还原成具体 (x, y)，再 `output.push_frame(pos, frame)`。

而 `Axis` 本身（`X`/`Y`）及其 `other()` 定义在 typst-library：

[Axis 与 other():crates/typst-library/src/layout/axes.rs:170-194](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/axes.rs#L170-L194) —— `other()` 返回垂直于自己的另一条轴，用于「主轴的对面就是交叉轴」。

`Dir` 决定主轴以及方向正负：

[Dir::axis:crates/typst-library/src/layout/dir.rs:97-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/dir.rs#L97-L102) 与 [Dir::is_positive:crates/typst-library/src/layout/dir.rs:38-43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/dir.rs#L38-L43) —— `ltr`/`ttb` 为正方向，`rtl`/`btt` 为负方向。正负会影响后面 `finish_region` 中 cursor 的增长方向与 ruler 取 max 还是 min。

#### 4.2.4 代码实践

**实践目标**：体会「同一份算法，两个朝向」的抽象。

**操作步骤**：

1. 在 `src/stack.rs:284-290` 处确认：无论 `axis` 是 X 还是 Y，`used.main` 与 `used.cross` 的累计代码只有一份。
2. 想象 `axis == Axis::Y`（纵向栈）时，一个宽 40pt、高 30pt 的帧：`GenericSize::new(40, 30)`，即 `cross=40`（宽度）、`main=30`（高度）。

**需要观察的现象**：算法主体完全不出现 `x`/`y` 字面量，只操作 `main`/`cross`；具体坐标只在入口（读 `regions.size`/`frame.size()`）和出口（`to_point`）出现。

**预期结果**：能说清「为什么横向栈和纵向栈可以共用 `finish_region` 这一份代码」——因为主/交叉轴已经被 `GenericSize` 抽象掉了。

#### 4.2.5 小练习与答案

**练习**：`GenericSize::new(cross, main)` 的参数顺序为什么是 `cross` 在前？

**参考答案**：这是为了与 `Axes::new(x, y)` 在纵向栈（`Axis::Y`）下保持「字段顺序一致」——纵向栈里 `cross` 对应 `x`、`main` 对应 `y`，于是 `GenericSize { cross, main }` 与 `Axes { x, y }` 顺序对齐，`into_axes(Axis::Y)` 直接得到 `Axes { x: cross, y: main }`。横向栈则由 `into_axes(Axis::X)` 做一次交换。这是一种方便记忆的约定，不是功能要求。

---

### 4.3 StackLayouter 的状态与排版循环

#### 4.3.1 概念说明

`StackLayouter` 是栈布局的核心状态机。它的设计哲学是**「先收集，后定位」**：

- 排版循环里，每来一个子项就把它排成帧、累计尺寸，但**不立即计算最终坐标**——因为有 `Fr` 间距时，每段的精确位置要等所有 `fr` 收齐、剩余空间确定后才能算出来。
- 所以布局器维护一个 `items: Vec<StackItem>` 暂存「这一区域里所有已排好的项」，等到 `finish_region` 时一次性算清坐标、贴到输出帧上。

放不下的内容会被推到下一个区域，于是 `finish_region` 会被多次调用，每次产出一张帧，最后 `finish` 把所有帧打包成 `Fragment`。

#### 4.3.2 核心流程

`StackLayouter` 的关键字段与生命周期：

```text
new() 初始化：
  expand        ← regions.expand（栈自身是否填满区域）
  regions.expand.set(axis, false)   ← 关掉孩子在主轴上的 expand
  initial       ← regions.size（本区域起始尺寸，作为相对尺寸基准与 fill 上限）
  used = GenericSize::zero()        ← 本区域已用 main / 最大 cross
  fr   = 0                          ← 本区域 Fr 之和
  items = []                        ← 本区域暂存项

排版循环（由 layout_stack_internal 驱动）：
  layout_spacing / layout_block / layout_custom_layouter
    → 削减 regions.size、累计 used.main、把 StackItem 压入 items
    → 若 regions.is_full() 则提前 finish_region()

finish()：
  finish_region()  ← 把 items 贴成一张帧，push 进 finished
  Fragment::frames(finished)
```

#### 4.3.3 源码精读

`StackLayouter` 的字段定义本身就讲清了它的状态：

[StackLayouter 字段:src/stack.rs:128-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L128-L154) —— 重点：`expand`（栈自身填满开关）、`initial`（本区域起始尺寸）、`used`（已用通用尺寸）、`fr`（本区域 Fr 之和）、`items`（暂存项）、`finished`（已完成帧）。

`StackItem` 是暂存项的三种形态：

[StackItem 枚举:src/stack.rs:157-164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L157-L164) —— `Absolute(Abs)`、`Fractional(Fr)`、`Frame(Frame, Axes<FixedAlignment>)`。前两种是间距，第三种携带该帧的主/交叉轴对齐。

`new()` 里有一个非常重要的动作——**关掉孩子在主轴上的 expand**：

[new 关闭孩子主轴 expand:src/stack.rs:168-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L168-L195) —— 栈**保存自己的** `expand`（决定输出尺寸是否填满区域），但把孩子 `regions.expand` 的**主轴分量置为 false**。含义：孩子沿主轴取自然高度，不自行撑满；撑满是栈的职责（通过 `expand` 与 `Fr` 实现）。交叉轴 expand 保留，所以孩子仍会填满栈的宽度。

`layout_block` 是排版一个块孩子的入口，它把块交给通用的 `crate::layout_fragment`（最终走到 flow），并对齐信息做了特殊提取：

[layout_block:src/stack.rs:221-250](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L221-L250) —— 注意它对 `AlignElem` 的处理：如果块本身是 `AlignElem`，取其显式 alignment；若是 `StyledElem`，在子样式链上查；否则取继承的 alignment。这保证了 `#align(center)[...]` 作为栈孩子时其主轴对齐能被栈尊重。`layout_custom_layouter`（L253-268）逻辑相同，只是把「排块」换成「调用户闭包」。

可断裂块会产出多帧，`layout_fragment` 把每帧都登记，并在帧之间自动断区域：

[layout_fragment 处理多帧:src/stack.rs:271-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L271-L300) —— 逐帧削减 `regions.size.y`、累计 `used`、压入 `items`；当 `i + 1 < len`（还有后续帧）时立即 `finish_region()`，把当前帧单独成页。

#### 4.3.4 代码实践

**实践目标**：验证「栈关掉孩子主轴 expand、保留交叉轴 expand」这一关键设定。

**操作步骤**：

1. 阅读 `src/stack.rs:176-179`，确认 `expand` 先被整体保存、再把主轴分量关掉。
2. 在 `finish_region`（L303-370）中找到 `size` 的计算（L306-309），确认它用的是**栈自己保存的** `self.expand`，而不是孩子的。

**需要观察的现象**：孩子拿到的 `regions.expand` 主轴分量为 false，所以一个 `block` 孩子在栈里不会自行撑满纵向；纵向撑满只能靠栈顶层的 `expand` 或 `1fr` 间距。

**预期结果**：能解释「为什么 `#stack(spacing: 1fr)[A][B]` 能把 A、B 推到顶与底，而单独的 `#block[A]` 不会自己变高」——前者由栈的 `Fr` 分配剩余空间，后者孩子主轴 expand 被关、取自然高。

#### 4.3.5 小练习与答案

**练习**：`StackLayouter::new` 为什么要先 `let expand = regions.expand;` 再 `regions.expand.set(axis, false);`？

**参考答案**：先把栈自身需要的原始 `expand` 拷贝保存（值类型 `Axes<bool>` 是 `Copy`），再改写 `regions` 关掉孩子的主轴 expand。这样 `self.expand` 记录的是「栈要不要填满区域」，而传给孩子的 `self.regions.expand` 主轴已被关掉。二者职责分离：栈管填满，孩子管自然尺寸。

---

### 4.4 间距处理：绝对间距与 Fr 分数

#### 4.4.1 概念说明

栈里的间距分两种，处理时机完全不同：

- **绝对间距 `Spacing::Rel`**：长度是确定的（如 `10pt`，或相对父尺寸的 `10%`）。排版循环里就能算出具体值，立刻削减 `regions.size`、累计进 `used.main`。
- **分数间距 `Spacing::Fr`**：长度未知，取决于「本区域还剩多少空间」和「总共有多少 fr」。排版循环里只把分数累加进 `self.fr` 并暂存，**真正分摊推迟到 `finish_region`**。

`Fr::share` 是分摊的核心公式。设本区域所有 fr 之和为 \(F\)、剩余空间为 \(R\)，则某个 \(v\) fr 分得的绝对长度为：

\[
\text{share}(v) = \frac{v}{F} \cdot R,\quad \text{且钳制到} \ge 0
\]

其中剩余空间 \(R = \text{full} - \text{used.main}\)（区域起始主轴尺寸 − 已用主轴尺寸）。

#### 4.4.2 核心流程

```text
layout_spacing(kind):
  Spacing::Rel(v):
    resolved = v 相对 base() 解析成绝对值
    limited  = min(resolved, 剩余空间)        // 不超出本区域
    (纵向栈) regions.size.y -= limited
    used.main += limited
    items.push(Absolute(resolved))            // 注意：存的是 resolved（未钳制）

  Spacing::Fr(v):
    self.fr += v
    items.push(Fractional(v))                 // 不占立即尺寸，待 finish_region 分摊

finish_region 中（见 4.5）：
  remaining = full - used.main
  每个 Fractional(v): cursor += v.share(self.fr, remaining)
```

#### 4.4.3 源码精读

`layout_spacing` 的分流处理：

[layout_spacing:src/stack.rs:198-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L198-L218) —— `Rel` 分支用 `relative_to(regions.base().get(axis))` 把相对长度（如 `%`）解析成绝对值，再 `min(*remaining)` 钳制；`Fr` 分支只累加 `self.fr`、压入暂存项，不削减当前尺寸。

`Fr` 与 `share` 定义在 typst-library：

[Fr::share:crates/typst-library/src/layout/fr.rs:54-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fr.rs#L54-L61) —— `ratio = self / total`（`Fr / Fr` 得 `f64`），若 ratio 与 remaining 都有限则返回 `max(ratio * remaining, 0)`，否则 0。`max(..., Abs::zero())` 保证负数或异常情况下不分得负空间。

`Spacing` 枚举本身：

[Spacing 枚举:crates/typst-library/src/layout/spacing.rs:130-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/spacing.rs#L130-L136) —— 只有 `Rel(Rel<Length>)` 与 `Fr(Fr)` 两个变体，对应上面两条路径。

一个重要推论：**只有当本区域存在 `Fr` 间距且 `full` 有限时，栈才会把本区域主轴尺寸撑满到 `full`**。这是 `finish_region` 里 `if self.fr.get() > 0.0 && full.is_finite()` 分支做的事（详见 4.5）。也就是说，`1fr` 不仅分摊空间，还「声明」了「这个区域应当被填满」。

#### 4.4.4 代码实践

**实践目标**：用具体数字验证 `Fr::share` 公式。

**操作步骤**：

1. 假设一个纵向栈，区域起始高 `full = 200pt`，两个块各高 30pt、50pt，已用 `used.main = 80pt`。
2. 栈里有三个 `Fr` 间距：`1fr`、`2fr`、`1fr`，总和 `F = 4fr`。
3. 计算剩余空间 \(R = 200 - 80 = 120\text{pt}\)。
4. 套公式：`1fr` → \(120 \cdot 1/4 = 30\text{pt}\)；`2fr` → \(120 \cdot 2/4 = 60\text{pt}\)；`1fr` → 30pt。

**需要观察的现象**：三段加起来恰好 120pt（= R），把剩余空间分摊殆尽；占比越大的 fr 分得越多。

**预期结果**：手算结果与 `Fr::share` 的定义一致——`share(v) = v/F · R`。可对照 [fr.rs:54-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fr.rs#L54-L61) 逐项核对。

#### 4.4.5 小练习与答案

**练习**：若 `full` 是无穷大（内容宽度无限的场景），`Spacing::Fr` 还会分到空间吗？

**参考答案**：不会。`Fr::share` 在 `remaining` 非有限时返回 0；同时 `finish_region` 的撑满分支有 `full.is_finite()` 守卫，不会把 `used.main` 设成无穷。所以在无限宽的行内环境里，`1fr` 实际退化为 0 宽度。

---

### 4.5 finish_region：定位、Fr 分配与 ruler 累积对齐

#### 4.5.1 概念说明

`finish_region` 是栈布局的「收口」：把本区域暂存的 `items` 贴成一张输出帧。它要解决三件事：

1. **决定输出尺寸**：栈要不要填满区域？有没有 `Fr`？
2. **分摊 `Fr` 间距**：用 `Fr::share` 把剩余空间分给每个 `Fractional` 项。
3. **逐项定位**：维护一个 `cursor`（光标）沿主轴推进；对每个帧，用 **ruler（标尺）** 决定它在可用空间里的对齐位置，再用 cursor 偏移。

其中 **ruler 累积对齐** 是栈最特别的行为，值得单独说清。

`ruler` 初始化为 `dir.start()`（正方向的起点：纵向栈是 Start/顶）。遍历每个帧时，ruler 会**单调更新**——正方向取 `max`、负方向取 `min`——而每个帧用**更新到此刻的 ruler** 来定位。效果是：栈里后出现的、对齐更「靠尾」的子项，会把自身及之后的游标整体推向尾端。这就是 Typst 栈的「累积对齐」：你可以让前面的项贴起点、后面的项贴终点，剩余空间自然落到中间。

#### 4.5.2 核心流程

`finish_region` 的三段：

```text
① 决定尺寸
   size = expand.select(initial, used.into_axes()).min(initial)
   full  = initial.get(axis)
   remaining = full - used.main
   if fr > 0 且 full 有限:
       used.main = full          // 撑满
       size.set(axis, full)
   若 size 非有限 → 报错 "stack spacing is infinite"

② 逐项定位（cursor 从 0 开始，ruler = dir.start()）
   for item in items:
     Absolute(v)        → cursor += v
     Fractional(v)      → cursor += v.share(fr, remaining)
     Frame(frame, align):
       ruler = (正方向) ruler.max(align.main)  /  (负方向) ruler.min(align.main)
       free  = size.main - used.main           // 可对齐的空闲量
       base  = ruler.position(free)            // 空闲量里这组内容的起点偏移
       main  = base + (正方向 ? cursor : used.main - child - cursor)
       cross = align.cross.position(size.cross - frame.cross)
       pos   = GenericSize::new(cross, main).to_point(axis)
       cursor += child
       output.push_frame(pos, frame)

③ 推进区域
   regions.next(); initial = regions.size; used/fr 清零; finished.push(output)
```

两个要点：

- 当存在 `Fr` 时，`used.main` 被设成 `full`，于是 `free = size.main - used.main = 0`，`ruler.position(0) = 0`——**有 Fr 时对齐失效**，因为剩余空间被分数间距吃光了。ruler 只在「没有 Fr、且栈 expand 填满区域」时才有空闲量可调。
- 正方向 `main = base + cursor`：内容从起点依次往后贴；负方向 `main = base + (used.main - child - cursor)`：内容从终点依次往前贴（`rtl`/`btt`）。

#### 4.5.3 源码精读

`finish_region` 的尺寸决定与分摊准备：

[finish_region 尺寸与 Fr 撑满:src/stack.rs:303-322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L303-L322) —— `size` 用 `expand.select(initial, used...)` 二选一（填满取 initial、收缩取 used），再 `.min(initial)` 兜底；`remaining` 在 `used.main` 被改写**之前**算出，供 `Fr::share` 使用；`fr > 0 && full.is_finite()` 时撑满并改写尺寸；尺寸非有限时报「stack spacing is infinite」。

逐项定位循环（本讲最核心的代码段）：

[finish_region 逐项定位:src/stack.rs:323-360](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L323-L360)

逐行拆解：

- L325 `ruler: FixedAlignment = self.dir.start().into()`：标尺初值取方向起点（`Side → FixedAlignment`，Top/Left → Start，Bottom/Right → End）。
- L333-337：ruler 单调更新——`is_positive()` 取 `max`，否则 `min`。`FixedAlignment` 派生了 `Ord`（Start < Center < End），所以 `max`/`min` 直接可用。
- L340-347 `main`：`ruler.position(parent - self.used.main)` 是「整组内容在空闲量里的起点」，再加 cursor（正方向）或 `used.main - child - cursor`（负方向）。`parent = size.get(axis)`。
- L350-353 `cross`：交叉轴对齐独立计算，用 `align.get(other).position(size.get(other) - frame.size().get(other))`。
- L355-357：还原成 `Point` 贴帧。

支撑这两个对齐计算的外部方法：

[FixedAlignment::position:crates/typst-library/src/layout/align.rs:715-721](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L715-L721) —— `Start → 0`、`Center → extent/2`、`End → extent`。给定空闲量 extent，返回内容起点的偏移。

[Dir::start:crates/typst-library/src/layout/dir.rs:129-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/dir.rs#L129-L136) 与 [Side → FixedAlignment:crates/typst-library/src/layout/align.rs:733-741](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L733-L741) —— `ttb.start() = Top → Start`，`btt.start() = Bottom → End`，以此类推。

推进区域与收尾：

[finish_region 推进与 finish:src/stack.rs:362-376](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L362-L376) —— `regions.next()` 切到下一区域（见 u2-l2 的队列游走），重置 `initial/used/fr`，把输出帧 push 进 `finished`；`finish()` 调一次 `finish_region()` 收口当前区域，再 `Fragment::frames(self.finished)` 返回所有帧。

#### 4.5.4 代码实践

**实践目标**：给定一组 frame 与若干 `Fr` 间距、一个有限 `full` 高度，手算各 frame 在主轴上的最终位置。本实践分为两小题，分别覆盖「Fr 分配」与「ruler 对齐」。

**操作步骤**

先在源码里定位公式（对照 [src/stack.rs:303-360](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L303-L360)），再按下述数据手算。

**题目 A（Fr 分配）**：纵向栈 `Dir::TTB`（正方向），`expand.y = true`，区域起始高 `full = 200pt`。子项顺序为：

1. 块 A，高 30pt，主轴对齐 Start；
2. 间距 `1fr`；
3. 块 B，高 50pt，主轴对齐 Center；
4. 间距 `1fr`。

**手算过程**：

- `used.main`（绝对）= 30 + 50 = 80pt；`self.fr` = 2。
- `remaining = full - used.main = 200 - 80 = 120pt`。
- `fr > 0 且有限` → `used.main = full = 200`，`size.y = 200`。于是 `free = size.y - used.main = 200 - 200 = 0`。
- cursor=0，ruler=Start。
  - A（align Start）：`ruler = max(Start, Start) = Start`；`main = Start.position(0) + cursor(0) = 0`；cursor → 30。
  - `1fr`：`cursor += 1.share(2, 120) = 0.5·120 = 60`；cursor → 90。
  - B（align Center）：`ruler = max(Start, Center) = Center`；`main = Center.position(0) + cursor(90) = 0 + 90 = 90`；cursor → 140。
  - `1fr`：`cursor += 60`；cursor → 200。

**预期结果（A）**：A 位于 `y = 0..30`，B 位于 `y = 90..140`，两段 `1fr` 各 60pt（A 与 B 之间 60pt，B 之后到帧底 60pt）。注意因为有 `Fr`，`free = 0`，ruler 对齐**不生效**（A、B 的对齐差异被忽略，位置完全由 cursor/Fr 决定）。

**题目 B（ruler 累积对齐）**：同样的纵向栈、`expand.y = true`、`full = 200pt`，但**没有 Fr 间距**。子项：

1. 块 A，高 30pt，主轴对齐 Start；
2. 块 B，高 50pt，主轴对齐 End。

**手算过程**：

- `used.main = 80pt`；`self.fr = 0` → 不撑满，但 `expand.y = true` 故 `size.y = initial = 200`。
- `free = size.y - used.main = 200 - 80 = 120pt`。
- cursor=0，ruler=Start。
  - A（align Start）：`ruler = max(Start, Start) = Start`；`main = Start.position(120) + 0 = 0`；cursor → 30。
  - B（align End）：`ruler = max(Start, End) = End`；`main = End.position(120) + cursor(30) = 120 + 30 = 150`；cursor → 80。

**预期结果（B）**：A 位于 `y = 0..30`（贴顶），B 位于 `y = 150..200`（贴底），中间 120pt 空闲落在 A 与 B 之间。这正是「累积对齐」——A 用了当时的 ruler=Start 贴顶，B 把 ruler 推到 End、自身贴底。

> 「待本地验证」：以上为依据源码公式的手算。若想核对，可写一个最小 Typst 文档（如 `#set page(height: 200pt); #stack(spacing: 1fr)[A][B]`）编译后用 PDF 测量坐标，但本实践聚焦源码阅读与手算，不要求运行。

#### 4.5.5 小练习与答案

**练习 1**：为什么题目 A 中块 A、B 的不同对齐（Start vs Center）没有改变它们的最终位置？

**参考答案**：因为存在 `Fr` 间距，`finish_region` 进入 `fr > 0 && full.is_finite()` 分支，把 `used.main` 设为 `full`，导致 `free = size.main - used.main = 0`；而 `main = ruler.position(free) + cursor` 中 `ruler.position(0)` 恒为 0，对齐失效。位置完全由 cursor（绝对尺寸 + Fr 分摊）决定。

**练习 2**：把题目 B 的方向改成 `Dir::BTT`（自底向上、负方向），块 A 仍 align Start、块 B 仍 align End，ruler 会如何更新？

**参考答案**：负方向下 ruler 取 `min`。`btt.start() = Bottom → End`，故 ruler 初值 = End。处理 A（align Start）：`ruler = min(End, Start) = Start`；处理 B（align End）：`ruler = min(Start, End) = Start`。即负方向下 ruler 向 Start 收敛，对应内容从底端（BTT 的「起点」是底部）向上贴齐。

## 5. 综合实践

把本讲的知识串起来，做一个「源码阅读 + 手算验证」的小任务。

**任务**：阅读 `src/stack.rs` 全文后，画一张「`StackLayouter` 状态流转图」，并用手算验证一个综合场景。

**步骤**：

1. **画状态流转图**：标出 `new`（初始化 expand/initial/used/fr/items）、排版循环（`layout_spacing`/`layout_block`/`layout_custom_layouter` 改写哪些字段）、`finish_region`（消费 items、产出帧、重置状态）、`finish`（收口）。在箭头上注明每个字段何时被读、何时被写。
2. **综合手算场景**：纵向栈 `Dir::TTB`、`expand.y = true`、`full = 300pt`。子项依次为：块 A（高 40，align Start）、间距 `10pt`、块 B（高 60，align Center）、间距 `1fr`、块 C（高 50，align End）。求 A、B、C 的主轴坐标区间，以及 `1fr` 分得多少。

**参考解答**：

- `used.main`（绝对）= 40 + 10 + 60 + 50 = 160pt；`self.fr = 1`。
- `remaining = 300 - 160 = 140pt`；`fr > 0` → `used.main = 300`，`size.y = 300`，`free = 0`。
- cursor=0，ruler=Start。
  - A（Start）：`ruler = Start`；`main = 0 + 0 = 0`；cursor → 40。
  - `10pt`：cursor → 50。
  - B（Center）：`ruler = Center`；`main = Center.position(0) + 50 = 50`；cursor → 110。
  - `1fr`：`cursor += 1.share(1, 140) = 140`；cursor → 250。
  - C（End）：`ruler = End`；`main = End.position(0) + 250 = 250`；cursor → 300。
- 结果：A `0..40`、B `50..110`、C `250..300`；`1fr` = 140pt（B 与 C 之间）。因 `free=0`，对齐差异再次不生效。

**观察要点**：只要存在 `Fr`，ruler 对齐就让位给 Fr 分配；只有「无 Fr + expand 填满」时，ruler 才用 `free` 主导定位。这条规律能帮你快速预判任何栈的布局结果。

## 6. 本讲小结

- 栈布局有**两个入口**：`layout_stack`（服务 `stack` 元素）与泛型 `layout_stack_internal`（服务列表等复用者）；后者通过 `StackLayoutChild::CustomLayouter` 接受自定义排版闭包，绕开 `Content` 不能持有借用的限制。
- 主/交叉轴用 `GenericSize<T>`（字段 `cross, main`）抽象，配合 `Dir::axis()`，让同一份算法同时支持 LTR/RTL/TTB/BTT 四个朝向；具体坐标只在入口/出口转换。
- `StackLayouter` 采用「**先收集、后定位**」：排版循环里只累计 `used.main`/`used.cross`/`fr` 并把项压入 `items`，最终坐标统一在 `finish_region` 算清。
- **栈关掉孩子在主轴的 expand**（交叉轴保留），所以孩子沿主轴取自然尺寸，填满区域是栈自己的职责。
- 绝对间距立刻削减尺寸；**`Fr` 间距只累加 `self.fr` 并暂存**，到 `finish_region` 用 `Fr::share(v) = v/F · remaining` 分摊；存在 `Fr` 时栈把本区域撑满到 `full`。
- `finish_region` 的 **ruler 累积对齐**：标尺从 `dir.start()` 出发，逐帧单调更新（正方向 `max`、负方向 `min`），每帧用「此刻的 ruler」定位，产生「前项贴起点、后项贴终点」的累积效果；但有 `Fr` 时 `free=0`、对齐失效。

## 7. 下一步学习建议

- **接 u6-l6（列表布局）**：列表是 `layout_stack_internal` + `CustomLayouter` 最主要的复用者。读 `src/lists.rs` 时，重点看它如何用自定义闭包同时排 marker 与 body、如何用 `Dir::TTB` 复用栈的纵向堆叠，以及 `body_indent` 如何与 marker 配合形成悬挂缩进。
- **回看 u4-l1（flow）对比**：栈是「最简单的线性块级排版」，flow 是「带脚注/浮动/段落断行的复杂块级排版」。对比二者能让你看清「块级世界」里哪些是栈的职责、哪些是 flow 在栈之上补的能力。
- **延伸阅读 `Fr` 的其它用例**：`Fr` 不仅用于栈间距，也用于 grid 的 `fr` 列/行（u6-l1）、段落的 `fr` 间距（u5）。`Fr::share` 是贯穿 Typst 的「按比例分摊」原语，理解它等于理解了一大类布局行为。
