# prepare——元素的首次准备

## 1. 本讲目标

上一讲（u2-l3）我们把 `verdict` 这位「预审法官」拆透了，它产出一份判决书 `Verdict { prepared, map, step }`。当时特意留了一个尾巴没展开：判决书里那个 `prepared: bool` 字段到底驱动了什么动作？答案就藏在 [`visit_show_rules`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L353-L433) 紧跟在 `verdict` 之后的两行里：

```rust
let mut tags = None;
if !prepared {
    tags = prepare(s.engine, s.locator, output.to_mut(), &mut map, styles)?;
}
```

这个 [`prepare`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L532-L589) 就是本讲的主角。源码注释一句话点明了它的定位——「只在元素**第一次**被访问时执行」。它做的四件事，是把一个「用户写出来的、字段还没就绪、还没身份」的元素，改造成一个「字段齐全、有唯一身份、可被内省」的元素，好让后续的 show 规则和排版能正确处理它。

学完本讲，你应该能够：

- 说清 `prepare` 触发的条件，以及它为什么能保证「每个元素只跑一次」（幂等性）。
- 解释 **location** 何时分配：只有 locatable、带 label 或已有 location 的元素才会拿到「身份证」。
- 区分并排准 **内置 `ShowSet`** 与 **正式 `synthesize`** 两步的先后，理解为什么 show-set 必须先于 synthesize。
- 理解 **`materialize`** 把样式链「烙印」进元素字段的意义，以及 **start/end tag** 如何包裹一个 locatable 元素供排版后内省。
- 用 u2-l3 已建立的 `lifecycle` 位集知识，解释 `mark_prepared` 为什么能让准备只执行一次。

本讲承接 u2-l3（`Verdict.prepared` 正是 `prepare` 的开关），也为 u3-l1（标签与内省）打下基础——start/end tag 正是在 `prepare` 里诞生的。

## 2. 前置知识

本讲承接 u1-l3、u2-l1、u2-l2、u2-l3，假定你已经了解：

- **`visit()` 8 步流水线**（u1-l3）：`visit_show_rules` 是第 3 步，返回 `false` 表示「不认领，交给后续步骤」。
- **`visit_show_rules` 的执行框架**（u2-l2）：先 `verdict` 拿判决书，再 `if !prepared { prepare(...) }`，然后应用 `ShowStep`（若有），最后把 start/end tag 当作 `TagElem` 推回 `visit()`。
- **`verdict` 与判决书**（u2-l3）：`Verdict.prepared` 表示元素「是否已准备过」；`map` 里收着 show-set 样式；`step` 是可选的替换型 show 规则。
- **`lifecycle` 位集**（u2-l3）：每个 `Content` 自带一个 `SmallBitSet`，**位 0 = 是否已 prepared**，位 ≥1 = 哪些 `RecipeIndex` 的 show 规则已套用。`RecipeIndex` 取值 ≥1，永不占位 0。

本讲新引入几个概念，先通俗解释：

- **location（位置）**：一个全局唯一标识，用来在「排版后的成品（frames）」里精确定位某个元素。query、`here`、`label` 等内省能力都依赖它。不是每个元素都需要 location——绝大多数普通文本不需要。
- **locatable**：一种元素「能力（capability）」。带有这种能力的元素（如 `heading`、`figure`、`rect` 等通过 `Locatable` 标注的元素）天然需要被 query，因此需要 location。
- **合成（synthesize）**：某些元素的字段不是用户直接给的，而是由其它字段或 query 结果**推导**出来的（例如 `figure.kind` 由它的 body 推导，`heading.numbered` 由编号查询推导）。`synthesize` 就是「把推导字段算出来并写回元素」的步骤。u2-l3 里 `verdict` 偷偷在克隆体上跑的那次叫**预合成**，本讲讲的是它在 `prepare` 里的**正式版**。
- **show-set（内置）**：u2-l3 讲过**用户**写的 `show x: set ...` 规则（被 `verdict` 收进 `map`）。本讲遇到的 `ShowSet` trait 是另一回事——它是元素**自带**的、写在 Rust 源码里的内置 show-set，能在用户 show 规则面前依然生效。
- **materialize（物化）**：把「靠样式链才能解析出来的字段值」就地写回元素自身，使元素脱离样式链也能读到这些值。
- **tag（标签，内省用）**：注意它和「给元素命名」的 `label` 不是一回事。这里的 `Tag` 是 realize 阶段生成的、夹在 locatable 元素首尾的**隐形标记元素**（`Tag::Start` / `Tag::End`），排版后用来「找到」这个元素在成品中的位置范围。

## 3. 本讲源码地图

本讲在五个文件之间穿梭：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) | 本讲主角 `prepare`，以及调用它的 `visit_show_rules`、决定它是否触发的 `verdict`。 |
| [crates/typst-library/src/foundations/content/element.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs) | `Synthesize`、`ShowSet` 两个 trait 的定义，以及 `is_locatable` / `is_tagged` 判定。 |
| [crates/typst-library/src/foundations/content/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs) | `Content` 上的 `set_location` / `is_prepared` / `mark_prepared` / `materialize` 等方法。 |
| [crates/typst-library/src/foundations/content/raw.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/raw.rs) | `Meta` 结构里的 `lifecycle` 位集——`mark_prepared` 操作的就是它的位 0。 |
| [crates/typst-library/src/introspection/tag.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/tag.rs) | `Tag` 枚举、`TagFlags` 结构、`TagElem`——start/end tag 的类型定义。 |

> 链接基准 HEAD：`32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。下面所有永久链接均基于此 HEAD。
>
> 本讲的所有「加日志」实践都需要重新编译 Typst。仓库默认构建目标是 CLI（`default-members = ["crates/typst-cli"]`），在仓库根目录执行 `cargo run -p typst-cli -- compile doc.typ` 即可用你修改过的 realize 代码编译文档，`eprintln!` 的输出会出现在 stderr。

## 4. 核心概念与源码讲解

### 4.1 prepare 的位置、触发与「只跑一次」

#### 4.1.1 概念说明

`prepare` 是 [`visit_show_rules`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L353-L433) 内部、紧跟 `verdict` 之后的一个**可选**步骤。它解决的核心问题是：用户写出来的元素往往是「半成品」——

- 它可能**没有身份**（没有 location，无法被 query / 定位）；
- 它的某些字段**还没算出来**（等着 synthesize 推导）；
- 它的某些样式相关字段**还散落在样式链里**，没有「烙」进元素自身。

`prepare` 就是在套用任何替换型 show 规则**之前**，把这些「半成品」问题一次性解决，产出一个「准备就绪」的元素。它只做四件事（外加收尾的 tag 与 mark）：

```text
prepare(elem, map, styles):
  1. 必要时分配 location      （给元素一张身份证）
  2. 应用内置 ShowSet          （把元素自带的 show-set 样式并入 map）
  3. 正式 synthesize           （推导并写回合成字段）
  4. materialize               （把样式链上的字段值烙进元素）
  → 生成 start/end tag（仅当元素有 location）
  → mark_prepared              （标记位 0，保证只跑一次）
```

「只跑一次」靠两点联手保证：一是 `visit_show_rules` 用 `if !prepared` 把关，二是 `prepare` 末尾的 `mark_prepared` 把位 0 置 1。下一节我们看它具体的触发与防重入逻辑。

#### 4.1.2 核心流程

`prepare` 的触发与「不重复执行」由 `visit_show_rules` 与 `verdict` 共同决定，完整链路如下：

```text
visit_show_rules(content, styles):
  verdict = verdict(engine, content, styles)   // 预审
  if verdict is None: return false             // 无可做 → 不认领，交回 visit() 后续步骤
  let Verdict { prepared, map, step } = verdict

  if !prepared:                                 // ← prepare 的唯一触发点
      tags = prepare(engine, locator, elem, map, styles)

  if let Some(step) = step: ... 应用 show 规则 ...
  push start tag (若有)
  visit_styled(realized, map, styles)           // 准备好的（可能被替换的）内容重新喂回流水线
  push end tag (若有)
```

关键在于：当一个元素**没有**匹配的替换型 show 规则（`step = None`）却仍需准备（例如带 label）时，`realized` 仍是原元素本身，它会被 `visit_styled` **再次喂回 `visit()`**。于是同一个元素会被遇到第二次。这一次会不会再次触发 `prepare`、会不会死循环？

不会。秘诀在 [`verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L437-L530) 末尾的早退判定：

```text
if step.is_none() && map.is_empty() && (prepared || { …无任何需准备的特征… }):
    return None
```

第二次遇到时，元素已是 `prepared = true`、且（对已准备元素）`verdict` 不会再往 `map` 里加 show-set（见 4.3），所以 `step = None` ∧ `map` 空 ∧ `prepared` → `verdict` 返回 `None` → `visit_show_rules` 返回 `false` → 元素不再走 show 路径，而是落到 `visit()` 后续步骤（分组 / 过滤 / sink）。**既不会重跑 `prepare`，也不会循环**。这就是 `prepared` 这个布尔出现在早退判定里（`prepared ||`）的根本原因。

#### 4.1.3 源码精读

[crates/typst-realize/src/lib.rs:L359-L372](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L359-L372) —— `visit_show_rules` 里 `verdict` 解构与 `prepare` 的唯一调用点。`output` 是 `Cow::Borrowed(content)`，`output.to_mut()` 在需要时克隆出一份可变副本交给 `prepare` 改写。

[crates/typst-realize/src/lib.rs:L514-L527](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L514-L527) —— `verdict` 的早退判定。注意 `prepared ||` 这个短路：对已准备元素，只要没有 `step`、`map` 为空，就一律返回 `None`，这正是「第二次遇到时跳过 show 路径」的闸门。而 `!prepared` 分支那一长串检查（无 label、无 location、无 `ShowSet`、不 locatable、不 tagged、无 `Synthesize`）说明：**绝大多数普通文本元素根本不会触发 `prepare`**——它们在 `verdict` 就被判为「无可做」而直接放行，落进 sink。

[crates/typst-realize/src/lib.rs:L532-L539](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L532-L539) —— `prepare` 的签名与那句关键注释「This is only executed the first time an element is visited.」。返回 `SourceResult<Option<(Tag, Tag)>>`：有 location 时返回 `(start, end)` 两个 tag，否则返回 `None`。

#### 4.1.4 代码实践

**实践目标**：验证 `prepare` 对同一个元素在一次 realize 过程中只执行一次，且第二次遇到时 `verdict` 返回 `None`。

**操作步骤**：

1. 在 [`prepare`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L533-L539) 函数体第一行（注释之后）加：
   ```rust
   eprintln!("[prepare] ENTER elem={:?}", elem.func().name());
   ```
2. 在 `verdict` 早退返回 `None` 前（L526 的 `return None;`）加：
   ```rust
   eprintln!("[verdict] None (prepared={prepared}, step={}, map_empty={}) on elem={:?}",
       step.is_some(), map.is_empty(), elem.func().name());
   ```
3. 编译一个带 label 的最小文档（label 会强制元素需要准备，见 4.2）：
   ```typst
   普通文本一行。
   = 标题 <my-title>
   又一行普通文本。
   ```

**需要观察的现象**：

- `[prepare] ENTER` 对 `heading`（带 label）出现**一次**；对两个普通文本行**不出现**（它们在 `verdict` 就被早退放行）。
- heading 在被 `visit_styled` 再次喂回后，`[verdict] None (prepared=true ...)` 出现一次——这就是「第二次遇到、跳过 show 路径」的信号。

**预期结果**：`prepare` 对每个需准备的元素恰好执行一次；普通文本完全不进入 `prepare`。**待本地验证**（受具体元素类型与内省轮次影响，日志行数可能多于一次 realize 的量级，但同一元素的「真正进入 prepare」应只有一次）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prepare` 的触发条件是 `if !prepared`，而不是「每次访问都跑一遍」？

> **参考答案**：因为 `prepare` 里的 location 分配、synthesize、materialize 都是「幂等且昂贵」的操作——重跑没有意义还会浪费性能，甚至可能破坏已推导字段的正确性。用 `prepared` 标志把关，配合末尾的 `mark_prepared`，保证一个元素在一次 realize 中只被准备一次。

**练习 2**：如果一个元素第二次进入 `visit_show_rules` 时 `verdict` 不返回 `None`，会发生什么？

> **参考答案**：会再次进入 `if !prepared` 分支——但因为 `mark_prepared` 已把 `prepared` 置真，`!prepared` 为假，`prepare` 仍被跳过；随后若 `step` 仍为 `None`、`map` 为空，`visit_styled` 会因 `local.is_empty()` 直接 `visit(s, content, outer)`，理论上仍可能回到 `visit_show_rules`。`verdict` 的 `prepared ||` 早退正是为了在这个点切断潜在的来回，避免无限循环。所以这道「早退返回 None」是防循环的关键一环。

---

### 4.2 第一步：分配 location——给元素一张「身份证」

#### 4.2.1 概念说明

`prepare` 的第一步是「在必要时给元素分配一个 location」。location 是元素在整篇文档里的全局唯一标识，所有内省能力（`query`、`locate`、`here`、交叉引用、编号）都建立在它之上。

但分配 location 有开销（要算哈希、要占用 `Locator` 的命名空间），所以 Typst **不是**给每个元素都分配，而是只给「确实需要被内省」的元素分配。判定标准收集在一个 [`TagFlags`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/tag.rs#L46-L55) 里：

- `introspectable`：元素是 locatable（自带 `Locatable` 能力），**或**带 label，**或**已经有 location（例如来自 query 的结果）。这三者任一为真，元素就会被插入 `Introspector`，可被 query。
- `tagged`：元素是 `Tagged`（用于 PDF 无障碍标注）。

只要这两者「至少一个为真」（`flags.any()`），且元素当前还没有 location，就分配一个。

#### 4.2.2 核心流程

```text
key  = hash128(&elem)                       // 元素的 128 位哈希，作为身份指纹
flags = TagFlags {
    introspectable: elem.is_locatable() || elem.label().is_some() || elem.location().is_some(),
    tagged:         elem.is_tagged(),
}
if elem.location().is_none() && flags.any():
    loc = locator.next_location(engine, key, elem.span())   // 向 Locator 申请一个新位置
    elem.set_location(loc)                                  // 写回元素
```

注意三处细节：

1. **`is_locatable` 查的是元素类型的 vtable**（一种静态能力），与「当前是否真的有 label」无关。所以即便用户没加 label，`heading`、`figure` 这类 Locatable 元素也会拿到 location。
2. **「已有 location」也计入 `introspectable`**。注释解释：来自 query 的元素可能「即便没经过 prepare 也已经带了 location」，这里要兼容这种情况。
3. **`key`（128 位哈希）后面还会用在 `Tag::End` 里**，用来在不存储整个元素副本的情况下标识「哪个元素的结束」。

#### 4.2.3 源码精读

[crates/typst-realize/src/lib.rs:L540-L556](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L540-L556) —— location 分配的完整逻辑。`TagFlags` 构造（L547-552）集中体现了「locatable ∨ label ∨ 已有 location」的三合一判定；L553-556 是「没有就补一个」的实际分配。

[crates/typst-library/src/foundations/content/element.rs:L89-L102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs#L89-L102) —— `is_locatable` / `is_tagged` 读的都是元素 vtable 里的 `introspection` 标志位，是元素类型层面的静态属性。

[crates/typst-library/src/foundations/content/mod.rs:L142-L145](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L142-L145) —— `set_location` 把 location 写进 `Meta.location`。

[crates/typst-library/src/introspection/tag.rs:L46-L61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/tag.rs#L46-L61) —— `TagFlags` 定义与 `any()` 方法（`introspectable || tagged`）。

#### 4.2.4 代码实践

**实践目标**：观察 label 如何触发 location 分配，并对比「不带 label 的普通文本」不分配。

**操作步骤**：

1. 在 L554 的 `let loc = locator.next_location(...)` 之后加：
   ```rust
   eprintln!("[prepare] alloc location for {:?} | flags={{introspectable:{}, tagged:{}}}",
       elem.func().name(), flags.introspectable, flags.tagged);
   ```
2. 分别编译两段文档：
   ```typst
   // 文档 A：给文本加 label
   Hello <hi> World
   ```
   ```typst
   // 文档 B：不给 label
   Hello World
   ```
3. 再编译一段含 Locatable 元素的文档（heading 天生 locatable）：
   ```typst
   = 一个标题
   正文
   ```

**需要观察的现象**：

- 文档 A：`TextElem`（带 `<hi>`）触发分配，`introspectable=true`（因 label）。
- 文档 B：不触发任何分配。
- 文档 C：`heading` 触发分配，`introspectable=true`（因 locatable，与 label 无关）；`正文` 的 TextElem 不触发。

**预期结果**：location 分配严格遵循「locatable ∨ label ∨ 已有 location」的判定，普通文本默认不分配。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一个元素「已经有 location 却还没经过 prepare」，这是怎么发生的？

> **参考答案**：当元素来自 `query` 的结果时，它在被查询时就已经被赋予了 location，但未必走过完整的 `prepare`。所以 `prepare` 在判定时把 `elem.location().is_some()` 也算作「需要 introspectable」，并在 L553 用 `elem.location().is_none()` 守门——已有 location 就不重复分配，避免覆盖 query 给的身份。

**练习 2**：为什么把「是否分配 location」与「是否 locatable」解耦——即允许一个非 Locatable 的元素因加了 label 也拿到 location？

> **参考答案**：因为 label 本身就是用户表达「我想引用 / 定位它」的意图（如 `<my-elem>` 配合 `query(<my-elem>)`）。是否可被内省应由「用户有没有给它身份」决定，而不是由元素类型的静态能力一刀切。所以判定是 `locatable ∨ label ∨ 已有 location` 的并集，把主动权也留给用户。

---

### 4.3 第二、三步：内置 show-set 与正式 synthesize

#### 4.3.1 概念说明

location 是「身份」，接下来的两步是「让字段就绪」。它们紧挨着，顺序极其重要。

**第二步：应用内置 `ShowSet`。** u2-l3 讲过**用户**写的 `show x: set ...` 规则——那些在 `verdict` 里被收进了 `map`。这里的 `ShowSet` 是另一回事：它是元素**自带**的、用 Rust 实现的内置 show-set。[`ShowSet`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs#L273-L281) trait 的文档说得很直白：它「比用户 show-set 更强大，因为它能访问元素的字段」，用途是「实现那些即使用户写了 show 规则也应当生效的效果」。这一步把元素自带的 show-set 样式并入 `map`。

**第三步：正式 `synthesize`。** [`Synthesize`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs#L265-L271) trait 负责推导「合成字段」（由其它字段或 query 结果算出的字段，如 `figure.kind`、标题编号等），并把它们写回元素。这正是 u2-l3 里 `verdict` 在克隆体上偷偷预演过的那一步——但预合成只是为了「让 selector 能按合成字段命中」，正式合成才是真正把字段写回、供后续 show 规则与排版使用。

为什么必须是 show-set **先于** synthesize？因为 synthesize 在推导字段时可能依赖样式（例如某些字段的推导要看当前样式），而内置 show-set 正是用来补充这些样式的。源码注释原话：「Do this after show-set so that show-set styles are respected.」——先并入 show-set 样式，再让 synthesize 在「已经包含这些样式」的链上跑。

#### 4.3.2 核心流程

两步都接收一个「合并了 `map` 的样式链」，保证它们看到的是「外层 styles + 累积的 show-set」：

```text
// 第二步：内置 ShowSet
if elem 可以 as dyn ShowSet:
    map.apply(show_set.show_set(styles))     // 把内置 show-set 样式折进 map

// 第三步：正式 synthesize（注意链上挂的是 map）
if elem 可以 as dyn Synthesize:
    synthesizable.synthesize(engine, styles.chain(map))?
```

注意 `synthesize` 拿到的是 `styles.chain(map)`——也就是「外层样式 + 截至目前已收集的 show-set 样式」。这与 u2-l3 里预合成只传 `styles`（不带 `map`）不同：预合成发生在 `verdict` 收集 `map` 的过程中，那时 `map` 还没成型；正式合成在 `prepare` 里，`map` 已经收齐了用户 show-set 与内置 show-set，所以能挂在链上一起生效。

还有一个关键点：这两步都受 `prepared` 保护——对已经准备过的元素，`verdict` 不会往 `map` 里加用户 show-set（u2-l3 的 L478 `if !prepared`），`prepare` 整体也不会重跑，所以 synthesize 也不会被重复执行。

#### 4.3.3 源码精读

[crates/typst-realize/src/lib.rs:L558-L569](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L558-L569) —— 内置 show-set 与正式 synthesize 两步。`elem.with::<dyn ShowSet>()` / `elem.with_mut::<dyn Synthesize>()` 是「按能力查询」的 trait object 下转：只有声明实现了对应 trait 的元素才会进入对应分支，其它元素零成本跳过。

[crates/typst-library/src/foundations/content/element.rs:L265-L281](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs#L265-L281) —— `Synthesize` 与 `ShowSet` 两个 trait。注意 `Synthesize::synthesize` 是 `&mut self`（要写回推导字段），而 `ShowSet::show_set` 是 `&self`（只读字段、产出 `Styles`）。

[crates/typst-realize/src/lib.rs:L446-L459](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L446-L459) —— u2-l3 讲过的**预合成**（`verdict` 里在克隆体上跑的版本）。对照本讲的正式合成可以看到：预合成传 `styles`、用 `.ok()` 吞掉错误、写的是临时 `slot`；正式合成传 `styles.chain(map)`、用 `?` 传播错误、写回的是真正的 `elem`。两者一「预览」一「正式」，关系一目了然。

[crates/typst-realize/src/lib.rs:L477-L482](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L477-L482) —— `verdict` 里把**用户** show-set 收进 `map` 的地方（`if !prepared`）。与本讲 `prepare` 里的**内置** show-set 合在一起，`map` 就同时容纳了两类 show-set。

#### 4.3.4 代码实践

**实践目标**：观察「内置 show-set → 正式 synthesize」的执行顺序，以及 `map` 如何被挂进 synthesize 的样式链。

**操作步骤**：

1. 在 L560 的 `if let Some(show_settable) = ...` 分支体内加：
   ```rust
   eprintln!("[prepare] ShowSet on {:?}", elem.func().name());
   ```
2. 在 L567 的 `if let Some(synthesizable) = ...` 分支体内加：
   ```rust
   eprintln!("[prepare] synthesize on {:?} (with map chained)", elem.func().name());
   ```
3. 用一个会触发合成的元素编译（`figure` 会 synthesize 出 `kind`、`caption` 等字段）：
   ```typst
   #figure(image("glacier.jpg", width: 80%)) <fig>
   ```
   （若本地无该图片，可改用 `#figure(rect(width: 10pt)) <fig>`，重点是触发 figure 的 synthesize。）

**需要观察的现象**：

- 同一个 `figure` 上，`ShowSet` 日志先于 `synthesize` 日志出现。
- 若 figure 实现了 `ShowSet`，会看到两条；若只实现了 `Synthesize`，只看到 `synthesize` 一条。

**预期结果**：顺序固定为「内置 ShowSet 在前、正式 synthesize 在后」，与源码注释一致。**待本地验证**（具体哪些元素实现了这两个 trait，需结合 typst-library 里各元素的定义确认）。

#### 4.3.5 小练习与答案

**练习 1**：用户的 `show figure: set text(red)` 与 figure 自带的 `ShowSet`，分别在哪里进入 `map`？

> **参考答案**：用户的那条在 [`verdict`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L477-L482) 里被识别为 `Transformation::Style`、折进 `map`（且仅当 `!prepared`）；figure 自带的 `ShowSet` 在 [`prepare`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L558-L562) 里通过 `map.apply(...)` 并入。两者最终都进 `map`，但来源与时机不同。

**练习 2**：如果把 `prepare` 里 show-set 与 synthesize 两步的顺序对调，会出什么问题？

> **参考答案**：synthesize 可能依赖 show-set 补充的样式来推导字段（源码注释明说「so that show-set styles are respected」）。对调后，synthesize 看到的样式链里还没有内置 show-set，推导出的合成字段可能与预期不符，进而导致依赖合成字段的 selector、show 规则或排版结果出错。所以这个顺序是刻意为之。

---

### 4.4 第四步：materialize、start/end tag 与 mark_prepared 幂等

#### 4.4.1 概念说明

字段就绪后，`prepare` 还剩三件收尾的事。

**第四步：materialize（物化）。** 很多元素字段的「最终值」要结合样式链才能解析（例如 `text.size` 可能继承自外层 `set text`）。在 realize 之前，这些值散落在样式链上、字段本身可能是「未设」。[`materialize`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L231-L236) 把「靠样式链解析出的值」就地写回字段本身。这样后续的规则即便只拿到元素（不带样式链），也能读到解析后的值。注释里的「Resolve all fields with the styles and save them in-place」就是这个意思。它接收的同样是 `styles.chain(map)`——物化的是「外层样式 + 全部 show-set」共同作用后的结果。

**生成 start/end tag。** 如果元素有 location（即上一步分配或保留了 location），就生成一对 [`Tag`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/tag.rs#L10-L24)：`Tag::Start(elem, flags)` 与 `Tag::End(loc, key, flags)`。它们是两个隐形的标记元素，会在 `visit_show_rules` 里被包成 `TagElem` 推回流水线，**夹在** locatable 元素的内容首尾。排版后，内省系统靠这对 tag 确定「这个元素在成品里占据了哪段区间」，从而支撑 query、交叉引用、PDF 标注等。注意 tag 在 synthesize 与 materialize **之后**生成，注释解释：这样 tag 里携带的元素副本就包含了合成字段；而又在 `mark_prepared` **之前**生成，是为了让 show-set 规则在元素被 query 时仍能生效。

**mark_prepared。** 最后，`elem.mark_prepared()` 把 `lifecycle` 位集的**位 0** 置 1。这就是 u2-l3 介绍过的那位「prepared 标记位」。置位之后，元素再次进入 `verdict` 时 `is_prepared()` 返回真，`prepare` 不再重跑——这就是幂等性的落点。

#### 4.4.2 核心流程

```text
// 第四步：materialize
elem.materialize(styles.chain(map))           // 把解析值烙进字段

// 生成 tag（仅有 location 时）
let tags = elem.location().map(|loc| (
    Tag::Start(elem.clone(), flags),
    Tag::End(loc, key, flags),
));

// 标记已准备
elem.mark_prepared()                          // lifecycle.insert(0)

return Ok(tags)                               // Some((start,end)) 或 None
```

`lifecycle` 这一个位集同时承载两类信息（u2-l3 已建立，这里复用）：

\[ \text{lifecycle} = \{0\}_{\text{prepared}} \;\cup\; \{\, n \ge 1 : \text{已套用第 } n \text{ 条 recipe}\,\} \]

因为 `RecipeIndex = depth - r ∈ [1, depth]`（永不为 0），位 0 永远不会被任何 recipe guard 占用，于是可以安全地挪用为 prepared 标记。`mark_prepared` 插入位 0、`is_prepared` 查询位 0，与 guard 机制共用同一套 `SmallBitSet` API。

生成的 tag 回到 `visit_show_rules` 后这样被消费：

```text
let (start, end) = tags.unzip();
if let Some(tag) = start { visit(TagElem::packed(tag)) }   // 先推 start tag
visit_styled(realized, map, styles)                        // 再处理元素内容
if let Some(tag) = end   { visit(TagElem::packed(tag)) }   // 最后推 end tag
```

也就是说，locatable 元素在 sink 里最终被「start tag + 内容 + end tag」三明治式包裹。

#### 4.4.3 源码精读

[crates/typst-realize/src/lib.rs:L571-L586](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L571-L586) —— materialize、tag 生成、mark_prepared 三件收尾。注意 L580-582 用 `elem.location().map(...)`：有 location 才有 tag，没有则 `tags = None`。

[crates/typst-realize/src/lib.rs:L411-L415](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L411-L415) 与 [L427-L430](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L427-L430) —— `visit_show_rules` 里 start / end tag 的 push 点，夹住中间的 `visit_styled`。

[crates/typst-library/src/foundations/content/mod.rs:L231-L236](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L231-L236) —— `materialize` 遍历元素所有字段，逐个调用字段级 `materialize(styles)`，把解析值写回。

[crates/typst-library/src/foundations/content/mod.rs:L158-L166](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L158-L166) —— `is_prepared` / `mark_prepared`，操作的都是 `lifecycle` 的位 0。

[crates/typst-library/src/foundations/content/raw.rs:L83-L88](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/raw.rs#L83-L88) —— `Meta.lifecycle` 字段及其文档注释：位 0 = prepared，位 n = 第 n 条 recipe（从顶数、从 1 起）已 guard。

[crates/typst-library/src/introspection/tag.rs:L10-L24](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/tag.rs#L10-L24) —— `Tag` 枚举。`Start` 存整个元素副本（含合成字段），`End` 只存 `(Location, key, flags)`——注释说明这是为了「让两个变体大小更均衡，压低 `Tag` 的内存占用」，没有语义原因。另外 [L75-L83](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/tag.rs#L75-L83) 的 `TagElem::packed` 在打包时**主动调用 `mark_prepared()`**——tag 元素自身不需要再被准备，这是个值得注意的「快捷跳过」。

#### 4.4.4 代码实践

**实践目标**：观察 materialize、tag 生成、mark_prepared 的执行顺序，以及 start/end tag 在 sink 中如何三明治式包裹一个 locatable 元素。

**操作步骤**：

1. 在 `prepare` 的 L573（`elem.materialize(...)` 之后）加：
   ```rust
   eprintln!("[prepare] materialized {:?}, has_location={}", elem.func().name(), elem.location().is_some());
   ```
2. 在 L586（`elem.mark_prepared();` 之后）加：
   ```rust
   eprintln!("[prepare] mark_prepared {:?} | tags={}", elem.func().name(), tags.is_some());
   ```
3. 在 `visit_show_rules` 的 L413（`if let Some(tag) = start`）分支内加：
   ```rust
   eprintln!("[tags] push START for {:?}", content.func().name());
   ```
   在 L428（`if let Some(tag) = end`）分支内加：
   ```rust
   eprintln!("[tags] push END for {:?}", content.func().name());
   ```
4. 编译带 label 的文档：
   ```typst
   起点
   = 标题 <t>
   终点
   ```

**需要观察的现象**：

- 对 `heading`：依次出现 `materialized heading, has_location=true` → `mark_prepared heading | tags=true`。
- 紧随其后：`[tags] push START for heading`，然后是 heading 内容的处理，最后 `[tags] push END for heading`——三明治结构。
- 普通文本（`起点` / `终点`）：不出现上述任何日志（它们没进 `prepare`，也没有 tag）。

**预期结果**：locatable/labelled 元素被「start tag + 内容 + end tag」包裹；普通文本裸进 sink。`mark_prepared` 恰在 tag 生成之后执行，保证再次遇到时 `prepare` 被跳过。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 tag 要在 `synthesize` / `materialize` **之后**、`mark_prepared` **之前**生成？

> **参考答案**：「之后」是因为 `Tag::Start` 里存了元素副本，要让它包含合成字段与物化后的字段值，这样内省系统拿到的才是「最终态」的元素；「之前」是因为源码注释指出——在 mark_prepared 之前生成，能让 show-set 规则在元素被 query 时仍然适用（一旦 mark_prepared，某些「仅在未准备时收集」的逻辑（如 `verdict` 的 `if !prepared` 分支）就不再执行）。所以顺序是 synthesize/materialize → tag → mark_prepared。

**练习 2**：`Tag::End` 为什么只存 `(Location, key, flags)` 而不像 `Tag::Start` 那样存整个元素副本？

> **参考答案**：纯粹是为了内存。`Start` 需要完整元素副本（供内省读取字段）；`End` 只需要标识「哪个元素的结束」，用 location + key 哈希就够了。若 `End` 也存整份副本，`Tag` 这个枚举的每个实例都要按「两份 Content」分配内存，开销翻倍。注释明说这是大小均衡的考量，没有语义区别。

**练习 3**：`TagElem::packed` 为什么在打包时主动 `mark_prepared()`？

> **参考答案**：因为 `TagElem` 本身只是个承载 `Tag` 的隐形标记元素，不需要分配 location、不需要 synthesize、也不该再被任何 show 规则处理。主动 `mark_prepared` 让它在流经 `verdict` 时直接被判为「已准备」，跳过 `prepare` 与不必要的处理，是一种针对性的快捷跳过。

---

## 5. 综合实践

把本讲四步串成一条完整的「元素准备」追踪链。请完成下面的综合任务：

**任务**：用一个文档同时触发「分配 location + 内置 show-set + 正式 synthesize + materialize + start/end tag + mark_prepared」，绘制一个 locatable 元素从进入 `visit_show_rules` 到落入 sink 的完整轨迹。

**文档**：

```typst
#set figure(placement: bottom)

#figure(
  table(columns: 2, [A], [B]),
  caption: [一张表],
) <tbl>

看 #ref(<tbl>) 这个表。
```

**要求**：

1. 打开本讲四个小节里建议的所有日志点（`prepare` 入口、location 分配、`ShowSet`、`synthesize`、`materialize`、`mark_prepared`、start/end tag push、`verdict` 早退）。
2. 跑一次编译，按时间顺序整理成一张「轨迹表」，列：`步骤 | 发生在 | 关键观察`。重点标注：
   - `figure` 何时拿到 location（因 `<tbl>` label）；
   - `ShowSet` 与 `synthesize` 的先后（figure 会合成 `kind: table`、`caption` 等字段）；
   - `materialize` 之后、`mark_prepared` 之前生成 tag；
   - start tag → 内容 → end tag 的三明治顺序；
   - `figure` 被 `visit_styled` 再次喂回时，`verdict` 返回 `None`（`prepared=true`），`prepare` 不再重跑。
3. 用一段话解释幂等性的意义：如果没有 `mark_prepared`（或 `verdict` 没有 `prepared ||` 早退），`figure` 会在哪一步陷入重复处理？为什么这会拖慢编译甚至改变结果？
4. 进阶思考：`#ref(<tbl>)` 的解析依赖 `figure` 的 location 与 tag。结合本讲，说明「ref 能找到 figure」这件事在 realize 阶段埋下了哪两颗「种子」（提示：location 与 start/end tag）。

**预期**：你能用「location → 内置 ShowSet → 正式 synthesize → materialize → tag → mark_prepared」这一整条逻辑，自洽地解释日志里每一次输出，并说清幂等性为何是这套机制能正确、高效运转的前提。具体日志**待本地验证**。

## 6. 本讲小结

- `prepare` 由 `visit_show_rules` 在 `if !prepared` 时调用，是元素「第一次被访问」时的专属准备步骤；`verdict` 末尾的 `prepared ||` 早退判定保证元素第二次进入时跳过 show 路径，从而「只跑一次」、不循环。
- **第一步分配 location**：只给 locatable、带 label 或已有 location 的元素分配（判定收集在 `TagFlags` 里），普通文本默认不分配——这是性能与内省需求的折中。
- **第二、三步**先应用**内置 `ShowSet`**（元素自带的、能访问字段的 show-set，并入 `map`），再跑**正式 `synthesize`**（推导并写回合成字段，传 `styles.chain(map)`）；顺序不可颠倒，否则合成字段可能拿不到 show-set 补的样式。
- **第四步 `materialize`** 把样式链解析出的字段值就地烙进元素，使元素脱离样式链也能读到解析值；接收的同样是 `styles.chain(map)`。
- 有 location 的元素会生成 **`Tag::Start` / `Tag::End`** 一对 tag，在 `visit_show_rules` 里被 `TagElem` 三明治式夹在内容首尾推回流水线，供排版后内省（query / ref / PDF 标注）。
- **`mark_prepared`** 把 `lifecycle` 位集的位 0 置 1，与 u2-l3 的 `RecipeIndex` guard（位 ≥1）共用同一个 `SmallBitSet`；这是幂等性的落点，保证昂贵的准备步骤不重复执行。

## 7. 下一步学习建议

- **u3-l1（标签与内省 TagElem）** 将接着本讲的 start/end tag 往下讲：tag 如何在分组裁剪（`finish_grouping`）里跨边界被纳入或排除（before / within / after 三个集合），是本讲 tag 生成的自然延续。
- 想看「判决执行端」如何把 prepared 元素与 show 规则串起来，可回看 **u2-l2（visit_show_rules）**；想看 `prepared` 这个布尔是怎么算出来的，可回看 **u2-l3（verdict）**。
- 对「元素能力（capability）」如 `Locatable` / `Synthesize` / `ShowSet` 如何在元素类型上声明感兴趣，可翻阅 typst-library 里具体元素（如 `figure`、`heading`）的 `#[elem(...)]` 定义，对照本讲看它们各自实现了哪些 trait。
- 对「内省循环」与防递归的第二道防线（`MAX_SHOW_RULE_DEPTH`、分组深度上限）感兴趣，可提前阅读 **u3-l6**。
