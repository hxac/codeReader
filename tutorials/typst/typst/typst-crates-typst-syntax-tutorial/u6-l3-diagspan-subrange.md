# DiagSpan、SubRange 与外部范围

## 1. 本讲目标

前两讲（u6-l1、u6-l2）解决了「CST 节点如何拿到一个稳定的编号、编号如何分配」的问题。本讲继续在 `src/span.rs` 与 `src/source.rs` 里打转，但要回答的是两个更贴近**诊断与外部文件**的问题：

1. 当错误不是发生在 Typst 源码里，而是发生在 Typst **加载的外部文件**（例如一份格式错误的 JSON、一个数据文件）里时，span 系统如何定位它？
2. 当我们想高亮一个节点文本里的**某一小段**（例如只给 `head` 里的 `ea` 划重点），而不是整个节点时，怎么表达？

学完本讲，你应当能够：

- 区分 `Span`（8 字节）与 `DiagSpan`（16 字节），说清为什么诊断系统需要后者。
- 读懂 `DiagSpanKind` 的三个变体，以及「外部文件范围」与「内部 range span」的区别。
- 用 `SubRange` 表达节点内的子区间，并用 `to_absolute` 把它换算成文件字节范围。
- 理解 `Spanned<T, S>` 这个「带定位的值」容器，以及它为何能同时挂 `Span` 或 `DiagSpan`。
- 看懂 `RangeMapper` 如何把「拼接出来的文本」里的位置映射回「原始文本里可能不连续」的位置。
- 复现 `Source::range`：用 `SpanNumber` + `Option<SubRange>` 求出真实字节范围。

## 2. 前置知识

本讲假设你已经掌握：

- **Span 的位布局**（u6-l1）：`Span` 是 8 字节的 `NonZeroU64`，高 16 位是 `FileId`，低 48 位是「编号区」。
- **编号区四段划分**（u6-l1）：低 48 位被切成四段——detached 哨兵、Typst 源码编号、**外部文件起点**、内部 range span。
- **numberize 与编号不变量**（u6-l2）：父节点编号小于子节点、兄弟从左到右递增，`find_number` 据此做二分式剪枝。
- **CST 节点的 span 存储位置**（u5-l1、u5-l2）：`SyntaxNode` 顶层带一个 `span: Span`。

两个术语先在这里澄清，后面会反复用到：

- **诊断（diagnostic）**：编译器向用户报告的一条「错误」或「警告」消息，它必须带一个范围，好让编辑器能在源码里画波浪线。
- **外部文件（external file）**：Typst 通过 `#read("data.json")` 等方式加载的、本身不是 Typst 源码的文件。它不在 CST 里，所以不能复用「节点编号」这套机制来定位。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [src/span.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs) | Span 紧凑编码 | `DiagSpan` / `DiagSpanKind` / `SubRange` / `Spanned` / `RangeMapper` 全部在此 |
| [src/source.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs) | Source 文件抽象 | `Source::range` 把编号 + 子区间换算成字节范围 |

补充一个跨文件引用：`SubRange` 与 `RangeMapper` 在 [src/node.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs) 里被 `warn_at` / `hint_at` / `synthesize_mapped` / `build_diagnostic_hints` 消费，本讲会点到为止，详细机制见 u5-l4。

## 4. 核心概念与源码讲解

### 4.1 DiagSpan 与 DiagSpanKind：诊断范围的扩展

#### 4.1.1 概念说明

u6-l1 讲的 `Span` 是 CST 节点的身份证：8 字节、编号稳定、`Option<Span>` 也只要 8 字节。它很省，但有一个硬限制——**它只能定位「Typst 源码」里的东西**，要么是节点编号，要么是两个 23 位的下标（内部 range span，上限 \(2^{23}-1\)）。

可是诊断系统遇到的情况更杂：

- 用户 `#read("data.json")` 读进来一个 JSON，它在 `cvt`（typst 的转换层）里解析失败了——这条错误必须指向 **JSON 文件**里的某个字节范围，而那个文件可能有好几兆，远超 23 位能表达的范围。
- 我们想给一个节点上挂警告，但又想**只高亮节点文本的一小段**。

`DiagSpan`（diagnostic span）就是为此设计的「加宽版 span」：它在 `Span` 基础上**多加一个 `u64`**（`extra` 字段），用 16 字节的代价换来两件 `Span` 做不到的事：

1. 支持指向**外部文件**的、最大 \(2^{46}-1\) 字节的范围。
2. 在普通编号 span 上额外携带一个**子区间**（`SubRange`）。

一句话总结区别：

> `Span` 服务于「CST 节点定位」；`DiagSpan` 服务于「诊断范围」，多了一个 `extra` 字段，能表达外部文件范围与节点内子区间。

#### 4.1.2 核心流程

`DiagSpan` 的 16 字节 = `span: Span`（8 字节）+ `extra: u64`（8 字节）。它的取值分三大类，对应 `DiagSpanKind` 的三个变体：

```
DiagSpan::get() 解码逻辑
├─ span 是 Detached          → DiagSpanKind::Detached
├─ span 是 Number { id, num }
│   ├─ num 落在「外部区」     → 外部文件范围：start 来自 span，end 来自 extra
│   │                          → DiagSpanKind::Range { id, range: start..end }
│   └─ 否则是普通编号         → 带 sub_range 的编号范围
│                              → DiagSpanKind::Number { id, num, sub_range }
└─ span 是 Range（内部 range）→ 可选用 extra 里的 sub_range 进一步收窄
                                 → DiagSpanKind::Range { id, range }
```

关键洞见：**外部文件范围在 8 字节的 `Span` 看来是「一个编号」**（因为它的值落在低 48 位的「外部区」段，而普通 `Span::get` 只认 Detached/Number/Range 三类）。只有 `DiagSpan` 凭借 `extra` 字段和「外部区」的判断，才能把它**还原成一个 range**。这就是「外部诊断必须用 `DiagSpan`」的根本原因。

#### 4.1.3 源码精读

`DiagSpan` 结构定义，注意它就两个字段：

[DiagSpan 结构（span.rs:218-222）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L218-L222) —— `span: Span` + `extra: u64`，共 16 字节，被 doc 注释明确标注「16 bytes and null-optimized」。

对应的展开枚举，比 `SpanKind` 多了一个 `sub_range` 字段：

[DiagSpanKind 三变体（span.rs:225-233）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L225-L233) —— 注意 `Number` 变体比 `SpanKind::Number` 多了 `sub_range: Option<SubRange>`。

构造一个**外部文件**诊断范围（这是 `DiagSpan` 独有的能力，`Span` 没有等价物）：

[DiagSpan::from_range 外部文件构造（span.rs:246-253）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L246-L253) —— start 饱和到 \(2^{46}-1\) 后塞进 `span` 的低 48 位（落在「外部区」），end 放进 `extra`。

把一个普通 `Span` 包成 `DiagSpan`，可选附带子区间：

[DiagSpan::from_span（span.rs:259-264）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L259-L264) —— `sub_range` 被打包进 `extra`：高 32 位放 `start`，低 32 位放 `end`。最常用的入口其实是 `From<Span> for DiagSpan`：

[From<Span> for DiagSpan（span.rs:321-325）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L321-L325) —— `span.into()` 等价于 `from_span(span, None)`，即不带子区间。

解码逻辑（本模块最重要的一段，建议逐行读注释）：

[DiagSpan::get 解码（span.rs:287-318）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L287-L318) —— 先复用 `span.get()` 拿到 `SpanKind`；若是 `Number`，再用 `num.checked_sub(EXTERNAL_BASE)` 判断是否落在「外部区」——若是，则把 `extra` 当作 end，还原成外部 range；若否，从 `extra` 拆出 `sub_range`。注释里那句「This `checked_sub` must come after the internal range check」点明了判断顺序：必须先排除内部 range（由 `span.get()` 完成），再去判外部区，否则会误判。

位宽常量与区域划分，回顾 u6-l1 但这里聚焦外部区与内部 range：

[Span 的位宽常量（span.rs:102-110）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L102-L110) —— `EXTERNAL_BASE = 2^47`、`EXTERNAL_VALUE_MAX = 2^46-1`、`RANGE_BASE = 2^47 + 2^46`、`RANGE_VALUE_MAX = 2^23-1`。

#### 4.1.4 代码实践

实践目标：直观体会「外部范围能用 `DiagSpan` 表达，但塞不进普通 `Span`」。

操作步骤（源码阅读型实践，结合 `cargo test`）：

1. 打开 [span.rs 的 test_diag_span_range（span.rs:591-609）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L591-L609)。
2. 阅读这条用例：`roundtrip(0x3FFF_FFFF_FFFE..0x3FFF_FFFF_FFFF)`（即 \(2^{46}-2 .. 2^{46}-1\)）。这个范围远超 \(2^{23}-1\)，普通 `Span::from_range` 根本表达不了。
3. 在仓库根目录运行：

   ```bash
   cargo test -p typst-syntax test_diag_span_range
   ```

需要观察的现象：测试通过，说明 `DiagSpan::from_range` → `get()` 能完整往返（roundtrip）一个高达 \(2^{46}-1\) 的外部范围。

预期结果：`cargo test` 报告 `test result: ok. ...`，`test_diag_span_range` 通过。

若想进一步对比：把同一段范围改成走 `Span::from_range`（参考 [test_span_range_encoding（span.rs:573-588）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L573-L588)），会发现它最大只能往返到 \(2^{23}-1\)，更大的值会被**饱和截断**——这正是「外部诊断必须用 `DiagSpan`」的实证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DiagSpan` 需要 16 字节，而 `Span` 只要 8 字节就够？

参考答案：`Span` 的低 48 位编号区已经被 Typst 源码编号、detached 哨兵、内部 range（两个 23 位）占满；外部文件范围的上限是 \(2^{46}-1\)，单个数都塞不进 23 位。`DiagSpan` 额外加一个 `u64`（`extra`）来存外部范围的 end、或编号 span 的子区间，从而用 16 字节换取表达力。代价是体积翻倍，所以只用在「诊断范围」这条路径上，CST 节点仍用 8 字节的 `Span`。

**练习 2**：`DiagSpan::get` 里，判断「外部范围」的 `checked_sub(EXTERNAL_BASE)` 为什么必须放在 `span.get()` 之后？

参考答案：低 48 位有四段，内部 range 区（`RANGE_BASE` 及以上）的值比外部区（`EXTERNAL_BASE` 及以上）更大。`span.get()` 内部已经先用 `checked_sub(RANGE_BASE)` 把内部 range 识别出来并返回 `SpanKind::Range`；只有在 `span.get()` 判定为 `SpanKind::Number` 时，才轮到 `DiagSpan` 用 `checked_sub(EXTERNAL_BASE)` 去检查它是不是「伪装成 Number 的外部范围」。顺序反了会把外部范围误判成内部 range 或普通编号。

---

### 4.2 SubRange：节点内的子区间

#### 4.2.1 概念说明

一个 CST 节点的 span 通常覆盖**整个节点的文本**。但有时我们只想强调其中一小段。例如节点文本是 `head`，我们想给中间的 `ea` 画一条波浪线作为提示——这时候用一整个节点的 span 太粗了。

`SubRange` 就是为「节点内的相对子区间」准备的轻量类型：它存的是**相对于节点文本起点的两个偏移量**（`start`、`end`），而不是文件里的绝对字节下标。这样它就能脱离具体的节点位置独立存在，被 attach 到任意一个编号 span 上。

`SubRange` 的两个约束：

1. **非空**：必须 `start < end`，否则 `new` 返回 `None`。
2. **饱和到 32 位**：`start`/`end` 是 `u32`（`end` 用 `NonZeroU32`），超出会饱和到 `u32::MAX`，不会溢出 panic。

#### 4.2.2 核心流程

`SubRange` 自身只存相对偏移；要变成可用的字节范围，需要知道它「挂在哪个节点上」，即节点的绝对起点 `offset`：

```
节点文本:  h e a d          （绝对字节范围: 2..6，offset = 2）
相对偏移:  0 1 2 3
SubRange(1, 3) 表示相对偏移 1..3，即 "ea"

to_absolute(offset=2):
  start = 1 + 2 = 3
  end   = 3 + 2 = 5
  → 文件字节范围 3..5 = "ea"
```

换算公式：

\[
\text{abs\_start} = \text{sub\_start} + \text{offset}, \qquad
\text{abs\_end} = \text{sub\_end} + \text{offset}
\]

`to_relative` 不加偏移，直接把 `(start, end)` 还原成普通 `Range<usize>`；`to_absolute` 则加上 `offset`。

#### 4.2.3 源码精读

`SubRange` 结构与构造：

[SubRange 结构（span.rs:334-338）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L334-L338) —— `start: u32` + `end: NonZeroU32`，`end` 用非零类型是为了让 `Option<SubRange>` 也能省一个 null 标记位。

构造器，校验非空并饱和到 32 位：

[SubRange::new（span.rs:345-355）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L345-L355) —— `start < end` 才返回 `Some`；注释指出由此可推出 `end != 0`，所以 `NonZeroU32::new(...).unwrap()` 安全。饱和由 [to_u32_saturated（span.rs:375-377）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L375-L377) 完成。

两种换算方法：

[SubRange::to_relative 与 to_absolute（span.rs:358-371）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L358-L371) —— `to_relative` 原样返回 `(start, end)`；`to_absolute(offset)` 给两端各加 `offset`。

谁在构造 `SubRange`？主要是 `SyntaxNode` 上的 `warn_at` / `hint_at`：

[warn_at（node.rs:153-165）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L153-L165) —— 把传入的绝对范围 `(start, end)` 直接塞进 `SubRange::new`，这里隐含一个约定：`warn_at` 的参数是**相对节点起点**的偏移（注释里写「a particular sub-range of the node's text」）。注意它先 `assert!(end <= self.len())` 做了一道 `SubRange::new` 不会检查的上界校验。

#### 4.2.4 代码实践

实践目标：亲手验证 `SubRange::new(1, 3)` 配合节点起点能切出 `"head"` 里的 `"ea"`。

这是本讲的主实践，见第 5 节「综合实践」，那里会复现 `test_source_sub_ranges` 的完整调用链。这里先单测 `SubRange` 本身。

操作步骤：

1. 运行 `SubRange` 的构造器测试：

   ```bash
   cargo test -p typst-syntax test_sub_range_constructor
   ```

2. 阅读断言：`SubRange::new(0,0)`、`new(5,5)`、`new(5,4)` 都返回 `None`（空或反向），只有 `start < end` 才 `Some`。

需要观察的现象与预期结果：测试通过，确认 `SubRange::new` 的非空语义。

#### 4.2.5 小练习与答案

**练习 1**：节点文本是 `"hello"`（绝对范围 10..15），想高亮 `"ll"`，应该构造哪个 `SubRange`？经 `to_absolute(10)` 后得到什么字节范围？

参考答案：`"ll"` 是 `hello` 里相对偏移 2..4，所以 `SubRange::new(2, 4)`；`to_absolute(10)` 得 `start=2+10=12`、`end=4+10=14`，即文件字节范围 `12..14`，正好是 `"ll"`。

**练习 2**：为什么 `SubRange.end` 用 `NonZeroU32` 而 `start` 用普通 `u32`？

参考答案：构造器保证 `start < end`，于是 `end` 必然 `> 0`，可以用 `NonZeroU32` 表达。`start` 完全合法地等于 0（子区间从节点开头开始），所以不能是非零类型。让 `end` 非零还能让 `Option<SubRange>` 享受 null 优化（参见 u6-l1 类似的 `Span` 设计）。

---

### 4.3 Spanned：带定位的值

#### 4.3.1 概念说明

编译器里到处都是「一个值 + 它从哪来」的组合：一个解析出来的数字、一个标识符、一条提示消息……它们都需要带上 span 才能做错误报告。`Spanned<T, S>` 就是这个通用容器：把任意值 `T` 和一个定位 `S` 打包在一起。

它的妙处在于**泛型 `S`**：默认是 `Span`，但也可以是 `DiagSpan`。于是同一个容器既能装「带 CST span 的值」（日常解析），又能装「带诊断范围的值」（给用户的提示消息），复用同一套 `new` / `detached` / `map` API。

#### 4.3.2 核心流程

```
Spanned<T, S> = { v: T, span: S }
  - 默认 S = Span        → Spanned<T>      用于解析产物
  - S = DiagSpan         → Spanned<T, DiagSpan>  用于诊断提示
```

三组常用方法：

- `new(v, span)`：正常构造。
- `detached(v)`：构造一个「不指向任何文件」的值（`span` 取 `S::SPAN_DETACHED`）。
- `map(f)`：只变换值 `v`，保留原来的 `span`（用于把 `Spanned<i64>` 变成 `Spanned<f64>` 而不丢定位）。

#### 4.3.3 源码精读

`Spanned` 定义，注意默认类型参数 `S = Span` 与一个私有 bound：

[Spanned 结构（span.rs:380-387）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L380-L387) —— `#[expect(private_bounds)]` 是为了用私有 trait `SpanDetached` 做 bound 而不暴露该 trait。

构造、detached、as_ref、map：

[Spanned 的方法（span.rs:390-410）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L390-L410) —— `detached` 用关联常量 `S::SPAN_DETACHED` 实现，对 `Span` 和 `DiagSpan` 各有一个实现：

[SpanDetached 的两个实现（span.rs:424-430）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L424-L430) —— 这就是泛型 `S` 同时支持两种 span 的关键。

`Spanned<EcoString, DiagSpan>` 的真实用法在 node.rs 的诊断提示汇总里：

[build_diagnostic_hints 返回 Spanned<EcoString, DiagSpan>（node.rs:1045-1059）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1045-L1059) —— 一条提示若有 `SubRange`，就用 `DiagSpan::from_span(parent_span, Some(sr))` 把它挂到父节点 span 的某个子区间上；否则用 `Spanned::detached(msg)`，不指向具体位置。

#### 4.3.4 代码实践

实践目标：理解「`hint_at` 产生的带子区间提示」如何经 `build_diagnostic_hints` 变成 `Spanned<EcoString, DiagSpan>`。

操作步骤（源码阅读型实践）：

1. 阅读 [hint_at（node.rs:180-189）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L180-L189)：它把一条提示连同 `Some(sub_range)` 推进 hints 列表。
2. 顺着 [build_diagnostic_hints（node.rs:1045-1059）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1045-L1059) 看：那个 `Some(sr)` 正是被 `DiagSpan::from_span(parent_span, Some(sr))` 消费，最终生成一条带子区间的 `Spanned<EcoString, DiagSpan>`。

需要观察的现象与预期结果：你能复述出「相对子区间 → attach 到父 span → 包成 `Spanned`」这条数据流，并指出若没有 `SubRange`（`None` 分支）则退化为 `Spanned::detached`。

#### 4.3.5 小练习与答案

**练习 1**：`Spanned::map` 为什么只变换 `v`、不改 `span`？

参考答案：`map` 的典型场景是类型转换（如解析出的数字字符串转成数值），转换前后指的是同一处源码，定位不该变。保留原 `span` 让后续错误报告仍能指回原始位置。

**练习 2**：为什么 `Spanned::detached` 要借助私有 trait `SpanDetached`，而不是直接写 `span: Span::detached()`？

参考答案：因为 `Spanned<T, S>` 是泛型的，`S` 可能是 `Span` 也可能是 `DiagSpan`，两者的「detached 值」不同（`Span::detached()` vs `DiagSpan::detached()`）。`SpanDetached` trait 用关联常量 `SPAN_DETACHED` 把这个差异抽象掉，让泛型代码 `S::SPAN_DETACHED` 一行搞定；标为私有（`#[expect(private_bounds)]`）则避免把这个内部 trait 暴露成公共 API。

---

### 4.4 RangeMapper：非连续文本的范围映射

#### 4.4.1 概念说明

最后两个最小模块解决一个更刁钻的真实场景。考虑 Rust 风格的文档注释：

```rust
/// #let x = 1
/// #let y = 2
```

Typst 会把每行去掉前导的 `/// ` 后**拼接**成一段连续的 Typst 代码来解析。于是 CST 里某个节点的「派生文本偏移」(derived offset) 与它在原始 Rust 源码里的「原始偏移」(original offset) 不再是一一对应的——中间隔了被剥掉的 `/// ` 和换行。

`RangeMapper` 就是这张「派生偏移 ↔ 原始偏移」的映射表。它接受一组**原始文本里的区段**（按顺序、会被拼接成派生文本），之后能把任意一个派生文本里的范围**反向映射**回原始文本的范围。典型用法是配合 `SyntaxNode::synthesize_mapped`：给一棵解析自「派生文本」的 CST 重新盖章 span，让每个 span 都精确指向原始文件里的位置。

它的一条核心不变量：对任意映射点，派生偏移恒 `<=` 原始偏移（`old <= new`）。因为拼接只会**跳过**原始文本里的空白/前缀，派生文本总是更短或等长。

#### 4.4.2 核心流程

构造：`RangeMapper::new(segments)`，`segments` 是原始文本里若干 `Range<usize>`，要求**按起点递增**。内部为每段记录一个 `Mapping { old, new }`，其中 `old` 是该段在派生文本里的累计偏移，`new` 是该段在原始文本里的起点：

```
原始文本 base:  "-- Hello\n-- world\n"
segments:       (3..9)="Hello\n", (12..18)="world\n"
派生文本:       "Hello\nworld\n"   （把两段拼起来）

Mapping 表:
  段0: old=0,  new=3    （派生 0..6 ↔ 原始 3..9）
  段1: old=6,  new=12   （派生 6..12 ↔ 原始 12..18）

映射举例（派生 → 原始）:
  map(2..3)  = 5..6      （派生 "l" ↔ 原始 "l"）
  map(6..8)  = 12..14    （派生 "wo" ↔ 原始 "wo"，跨段边界对齐到段1起点）
  map(2..12) = 5..18     （跨段时，把两段间的 gap "-- " 也包进来）
```

查询：`map(range)` 把派生范围映回原始范围；`map_sub_range(offset, sub_range)` 把「某个 offset 处的子区间」映回原始范围（用于把 4.2 的 `SubRange` 一起搬过去）。跨段范围会把段间的 gap 一并吞进结果。

#### 4.4.3 源码精读

`RangeMapper` 与 `Mapping` 定义：

[RangeMapper 结构（span.rs:439-443）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L439-L443) —— `vec: Vec<Mapping>` + `total`（派生文本总长）。doc 注释举的正是「doc comment 里的 Typst 代码」这个例子。

[Mapping 与 old<=new 不变量（span.rs:445-450）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L445-L450)。

构造器，校验段有序：

[RangeMapper::new（span.rs:463-485）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L463-L485) —— 遇到 `start > end` 或段间乱序（`map.new > start`）就返回 `Err`。

核心查询 `map`，区分空范围与跨段：

[RangeMapper::map（span.rs:498-516）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L498-L516) —— 三分支：`end==0`、空范围（`start==end`，边界取靠前位置）、正常范围（`start<end`，分别映射首尾）。底层用 `partition_point` 做二分，见 [map_start（span.rs:530-538）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L530-L538) 与 [map_end（span.rs:543-550）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L543-L550)——两者只在边界处差一个 `<=` 与 `<`，决定「光标落在段边界上时偏左还是偏右」。

把子区间也搬过去的 `map_sub_range`：

[RangeMapper::map_sub_range（span.rs:521-527）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L521-L527) —— 先 `to_absolute` 把子区间变成派生范围，映射后减去新的段起点，重新封成一个 `SubRange`。

与 CST 联动的入口在 node.rs：

[SyntaxNode::synthesize_mapped（node.rs:354-378）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L354-L378) —— 先校验文本长度不超过 mapper 总长，再对每个节点用 `Span::from_range(id, mapper.map(offset..offset+len))` 盖章，并用 `mapper.map_sub_range` 同步搬迁节点上挂着的子区间。这就是「派生文本的 CST → 原始文件 span」的工厂方法。

#### 4.4.4 代码实践

实践目标：用测试用例验证 `map` 的跨段 gap 行为。

操作步骤：

1. 运行：

   ```bash
   cargo test -p typst-syntax test_range_mapper
   ```

2. 对照 [test_range_mapper（span.rs:639-656）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L639-L656)，重点看 `m.map(2..12) == 5..18` 这一条：派生范围 `2..12` 横跨了段0（`Hello\n`）与段1（`world\n`）之间的 gap（原始文本里的 `-- `），结果把这段 gap 也包进了 `5..18`。

需要观察的现象与预期结果：测试通过。你应当能解释「跨段映射会把段间 gap 吞进结果」——这对错误范围是合理的：错误跨越了被拼接的两段，原始文本里夹在中间的内容自然也属于这段范围。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `RangeMapper` 要求 `old <= new`（派生偏移不超过原始偏移）？

参考答案：派生文本是把原始文本的若干区段**拼接**而成，拼接过程只会丢弃内容（如 doc comment 的 `/// ` 前缀与换行），不会新增。所以派生文本任意位置的累计长度必然不超过它在原始文本里对应位置的累计长度，即 `old <= new`。这条不变量让 `map_sub_range` 里 `start - new_offset`、`end - new_offset` 的减法不会下溢。

**练习 2**：`map_start` 和 `map_end` 都用 `partition_point` 做二分，唯一区别是谓词里的 `<=` 与 `<`。这个差别在什么边界情况下才有影响？

参考答案：只有当查询偏移正好落在某个**段边界**上时才有影响。例如派生偏移 6 既是段0的末尾又是段1的起点：`map_start`（`old <= offset`）倾向取段1（偏右），`map_end`（`old < offset`）倾向取段0（偏左）。文档注释里 test 的 `m.map(6..6) == 9..9` 就体现了空范围在边界上「偏左」的选择。

---

### 4.5 Source::range：编号与子区间换算成字节范围

#### 4.5.1 概念说明

前面四个模块定义了「诊断范围怎么存」。最后一个模块回答「怎么把它变回文件里的字节范围」，这就是 `Source::range`。它把一个 `SpanNumber`（来自 `Span::get` 解包）加上一个可选的 `SubRange`，换成 `Range<usize>`。

这是 span 系统的**反向查询**入口（正向是「文本 → CST → numberize → span」，反向是「span → 字节范围」）。它内部复用了 u5-l3 的 `LinkedNode::find_number` 做编号定位，再用 `SubRange::to_absolute` 做子区间收窄。

#### 4.5.2 核心流程

```
Source::range(num, sub_range):
  1. LinkedNode::new(root).find_number(num)?   // 用编号不变量定位节点，O(近似 log)
       → 得到节点的整体字节范围 overall（如 "head" = 2..6）
  2. 若 sub_range 是 None：
       → 直接返回 overall
  3. 若 sub_range 是 Some(sr)：
       → range = sr.to_absolute(overall.start)  // 相对偏移 + 节点起点
       → assert!(range.end <= overall.end)       // 不能超出节点
       → 返回 range（如 SubRange(1,3) + offset 2 → 3..5 = "ea"）
```

关键：`SubRange` 存的是**相对偏移**，所以必须知道节点起点 `overall.start` 才能落地。`Source::range` 正是把「节点定位」与「子区间收窄」粘合在一起的地方。

#### 4.5.3 源码精读

`Source::range` 全貌：

[Source::range（source.rs:124-142）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L124-L142) —— 先 `find_number(num)` 定位，再视 `sub_range` 决定是否 `to_absolute(overall.start)`。doc 注释提示「通常更推荐用 `WorldExt::range`」，因为后者能直接吃一个 `Span` 而不必先手动解包出 `SpanNumber`。

它依赖的反向定位（u5-l3 已详讲）：

[LinkedNode::find_number（node.rs:1129-1134）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1129-L1134) —— 命中即返回，否则按编号单调性下钻子树。

#### 4.5.4 代码实践

见第 5 节综合实践（复现 `test_source_sub_ranges`），那里完整演练了 `Source::range(num, Some(SubRange::new(1,3)))` → `"ea"`。

#### 4.5.5 小练习与答案

**练习 1**：`Source::range` 里的 `assert!(range.end <= overall.end)` 防的是什么？

参考答案：`SubRange` 构造时只校验了 `start < end`，并不校验它是否落在节点长度内（`warn_at` 在外层用 `assert!(end <= self.len())` 补了这道校验，见 node.rs:159）。`Source::range` 这里再加一道断言，确保子区间经 `to_absolute` 落地后不会越过节点末端，防止切出越界字节范围。

**练习 2**：为什么 `Source::range` 接受的是 `SpanNumber` 而不是 `Span`？

参考答案：`Span` 同时编码了 `FileId` 与编号，而 `Source` 本身已经知道自己对应哪个文件（`self.id()`）。让调用方先用 `Span::get` 解包出 `SpanNumber`（或用 `WorldExt::range` 自动处理 `FileId` 路由），`Source::range` 就能专注做「编号 → 节点 → 字节范围」这一件事，职责更单一。

---

## 5. 综合实践

**任务**：复现 [source.rs 的 test_source_sub_ranges（source.rs:162-181）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L162-L181)，亲手验证 `SubRange::new(1, 3)` 能从 `"head"` 里切出 `"ea"`，并把本讲四件事（编号定位、子区间、`to_absolute`、`Source::range`）串起来。

**操作步骤**：

1. 直接运行现成测试，先确认行为：

   ```bash
   cargo test -p typst-syntax test_source_sub_ranges
   ```

2. 阅读测试体（source.rs:164-181），梳理这条调用链：

   ```
   text = "= head <label>"
   source = Source::detached(text)            // parse + numberize + Lines
   head = LinkedNode::new(source.root())
            .leaf_at(2, Side::After)          // 光标在 offset 2（"=" 和 "h" 之间），
            .unwrap().span()                  //   Side::After 取右侧叶子 → "head" 节点
   num = SpanNumber(head.number())            // 解包出编号

   source.range(num, None)                    // 不收窄 → "head" 的整体范围 2..6
   source.range(num, Some(SubRange::new(1,3)))// 收窄 → to_absolute(2) → 3..5 → "ea"
   ```

3. 自己手算几条（测试里都有断言，可对照）：

   | 调用 | 计算 | 结果 |
   |---|---|---|
   | `get(head, None)` | `find_number` → 2..6 | `"head"` |
   | `get(head, SubRange::new(1,3))` | `to_absolute(2)` → 3..5 | `"ea"` |
   | `get(head, SubRange::new(0,4))` | `to_absolute(2)` → 2..6 | `"head"` |
   | `get(root, SubRange::new(3,10))` | root 起点 0 → 3..10 | `"ead <la"` |

**需要观察的现象**：`SubRange` 的两个参数是**相对节点起点**的偏移，而不是文件绝对字节下标。对 `head`（节点起点 = 2）用 `SubRange(1,3)`，要加上 2 才得到文件范围 3..5。

**预期结果**：`test_source_sub_ranges` 测试通过；你能不看源码复述出「`leaf_at` 取节点 → `number()` 取编号 → `Source::range(num, sub_range)` 用 `to_absolute` 收窄」整条链。

> 说明：以上命令在仓库根目录执行；若只读到测试源码不运行，结论也可由断言直接读出，标记为「源码阅读型实践」同样成立。

## 6. 本讲小结

- `DiagSpan` 是 `Span` 的「诊断加宽版」：16 字节 = `Span` + `extra: u64`，多出的字段让它能表达 `Span` 表达不了的**外部文件范围**（上限 \(2^{46}-1\)）和**节点内子区间**。
- 外部文件范围在 8 字节 `Span` 看来「伪装成一个编号」，只有 `DiagSpan::get` 凭 `extra` 与「外部区」判断才能把它还原成 range——这就是「外部诊断必须用 `DiagSpan`」的根因。
- `SubRange` 存**相对节点起点**的两个偏移（非空、饱和到 32 位）；`to_absolute(offset)` 加上节点起点即可落地为文件字节范围。
- `Spanned<T, S>` 是「带定位的值」的通用容器，泛型 `S` 默认 `Span`、也可取 `DiagSpan`，靠私有 trait `SpanDetached` 同时支持两者的 `detached()`。
- `RangeMapper` 把「拼接派生文本」里的偏移反向映射回「原始文本里可能不连续」的偏移，配合 `SyntaxNode::synthesize_mapped` 为 doc comment 等非连续源代码盖准确的 span。
- `Source::range(num, sub_range)` 是反向查询入口：先用 `find_number` 定位节点，再用 `SubRange::to_absolute` 收窄，把编号 + 子区间换成真实字节范围。

## 7. 下一步学习建议

本讲讲完 span 系统的「存」与「查」，至此 U6（Span 系统）单元结束。建议：

- 进入 **U7（AST）**：AST 节点是 CST 节点的类型化视图（u7-l1），你会看到 `Spanned<T>` 在求值层如何大量承载「带定位的值」。
- 若对「span 如何随编辑保持稳定」更感兴趣，可跳到 **U9（增量重解析）**：那里会用到 u6-l2 的 `upper` 字段与编号区间，以及本讲 `RangeMapper` 的搬迁逻辑在编辑后如何维护。
- 想看 `DiagSpan` 真正被消费的地方，可追踪 `SyntaxDiagnostic`（u5-l4）如何带着 `Spanned<EcoString, DiagSpan>` 的提示列表流向 typst 的诊断渲染层。
