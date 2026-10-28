# 内省机制：counter、引用与两遍编译

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `Location` 与 `Locatable` 元素体系：哪些元素可以被查询、由谁决定、清单在哪里维护。
2. 描述 `Tag` 如何在 realize 阶段被"埋进"排版产物 `Frame`，以及 `PagedIntrospector` 如何从 `Frame` 中收集出一张可查询的表。
3. 说出 `query`/`counter` 的执行语义：它们为什么必须出现在 `context` 中，以及 `Introspect` 询问协议如何工作。
4. 描述编译主循环的迭代收敛判定：comemo 的 `constraint.validate` 快路径、`MAX_ITERS = 5` 的上限、不收敛时的警告生成方式。
5. 独立统计一篇文档实际进行了几轮排版，并用 `query(divider)` 验证 0.15 起新增的可定位元素。

## 2. 前置知识

- **两遍编译（multi-pass compilation）**：目录页码、交叉引用这类信息有个鸡生蛋问题——第 1 页的目录要写"第 3 章 在第 5 页"，但第 5 页的存在本身可能受目录长度影响。经典解法（LaTeX 也是如此）是"先排一遍、再回头填答案、再排一遍"，直到结果不再变化。Typst 把这个过程做成了编译器内建的循环，而不是让用户手动跑两遍命令。
- **不动点与收敛**：把排版看作函数 \( f \)，它读取上一轮的"文档事实"（内省器），产出新一轮文档。若第 \( n \) 轮与第 \( n-1 \) 轮产出完全相同，称为**收敛**（converged）；Typst 最多尝试 5 轮，超限则带着警告返回末轮结果。
- **`Location`**：元素的 128 位唯一身份证。与 u2 讲过的 `Span`（源码位置）不同，`Location` 标识的是**文档中排版后的元素实例**，由 realize 阶段的 `Locator` 分配。
- **comemo 记忆化**：u4-l1 已讲过 `#[comemo::track]` 与约束验证。本讲会用到它作为收敛判定的快路径，不再展开原理。
- **Frame 与 FrameItem**：排版产物是一棵 `Frame` 树，叶子项包括文本、形状、图像、链接，以及本讲的主角 `FrameItem::Tag`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-library/src/introspection/location.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs) | `Location` 类型、Locatable 元素的权威文档清单、位置类内省询问 |
| [crates/typst-library/src/introspection/locator.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/locator.rs) | realize/layout 阶段分配 `Location` 的 `Locator`，分层哈希策略 |
| [crates/typst-library/src/introspection/tag.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/tag.rs) | `Tag`/`TagFlags`/`TagElem`：埋进 Frame 的起止标记 |
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs) | `prepare` 函数：给元素分配 `Location` 并生成起止 `Tag` |
| [crates/typst-library/src/introspection/introspector.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs) | `Introspector` trait（comemo 追踪）、目标无关的 `ElementIntrospector` 查询引擎 |
| [crates/typst-layout/src/introspect.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/introspect.rs) | `PagedIntrospector`：从已排好的页面收集元素与位置 |
| [crates/typst-library/src/introspection/query.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/query.rs) | `query()` 函数与 `QueryIntrospection` 等询问类型 |
| [crates/typst-library/src/introspection/counter.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs) | `Counter` 及 `CounterAtIntrospection` 等：计数器语义 |
| [crates/typst-library/src/introspection/convergence.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs) | `MAX_ITERS`、`Introspect` trait、`History`、`analyze` 不收敛诊断 |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs) | `compile_impl` 主循环：求值一次 + 排版迭代至收敛 |
| [crates/typst-library/src/engine.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/engine.rs) | `Engine::introspect`：执行询问并登记到 `Sink` |
| [crates/typst-library/src/foundations/context.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/context.rs) | `Context` 与 `ContextElem`：`context` 表达式的挂起与回放 |
| [crates/typst-library/src/model/divider.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/divider.rs) | 0.15 起变为 `Locatable` 的 divider 元素 |
| [tests/suite/introspection/](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/introspection/convergence.typ) | 收敛警告测试集与 `switch.typ` 迭代开关工具 |

## 4. 核心概念与源码讲解

### 4.1 Locatable 元素体系与 Location

#### 4.1.1 概念说明

脚本能"看见"文档（目录、计数、引用）的前提是：排版后的文档里有一批元素是**可被查询的**。但让所有元素都可查询代价太高——每个元素都要分配身份、写入 Frame、进入索引。因此 Typst 用 `Locatable` 标记做了白名单：只有语义层元素（heading、figure、footnote……）默认可定位；装饰性元素（rect、text）默认不可。`Location` 则是被定位元素的身份证：一个不透明 的 128 位哈希值。

#### 4.1.2 核心流程

`Locatable` 能力沿着这条链路生效：

1. 元素声明处写 `#[elem(Locatable, ...)]`；
2. `#[elem]` 过程宏把它写进元素 vtable 的 `IntrospectionCapabilities`；
3. 运行时通过 `Element::is_locatable()` 查询；
4. `query()` 的参数类型 `LocatableSelector` 在 cast 阶段用 `is_locatable()` 做门禁，不满足直接报错；
5. realize 阶段据此决定是否给元素分配 `Location` 并生成 `Tag`（见 4.2）。

`Location` 本身的生成采用**分层哈希**：每一层布局用"本层内第几个具有某 span 的元素"作为局部键 \( k_n \)，与上层哈希递归复合 \( h_n = \mathrm{hash}(h_{n-1}, k_n) \)。这既保证跨迭代同一元素得到同一 `Location`，又保证文档局部编辑不会像自增计数器那样让后续所有 ID 平移（保护增量编译缓存），还避免了全局可变状态（允许并行布局）。

#### 4.1.3 源码精读

`Location` 就是一个包装哈希的类型，外加派生"变体位置"的能力（文献目录用它从一个位置派生多个可链接条目）：

- [crates/typst-library/src/introspection/location.rs:L55-L78](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L55-L78) — `pub struct Location(u128)` 与 `variant(n)`：对 `(自身哈希, n)` 再哈希，造出语义上隶属于它的新位置。

Locatable 元素的**权威清单不是代码生成的，而是手写在文档注释里**的：

- [crates/typst-library/src/introspection/location.rs:L26-L51](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L26-L51) — 文档列出各类别中的可定位元素：Model 类的 `asset`、`bibliography`、`cite`、`divider`、`document`、`emph`、`enum`、`figure`、`footnote`、`heading`、`link`、`list`、`outline`、`par`、`quote`、`ref`、`strong`、`table`、`terms`、`title`；Text 类的 `raw` 与四种装饰；Introspection 类的 `metadata`；以及例外 `math.equation`、`image`、`pdf.attach`。

最近一次清单更新来自 `Make divider locatable` 提交，divider 元素声明里现在带有 `Locatable`：

- [crates/typst-library/src/model/divider.rs:L40-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/divider.rs#L40-L41) — `#[elem(since = "0.15.0", Locatable, ShowSet)] pub struct DividerElem {}`。

能力标记最终落在元素 vtable 中，由三个布尔位组成：

- [crates/typst-library/src/foundations/content/vtable.rs:L318-L326](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/content/vtable.rs#L318-L326) — `IntrospectionCapabilities { locatable, unqueriable, tagged }`：`unqueriable` 给 `place` 这类"可定位但用户不该查"的元素用。
- [crates/typst-library/src/foundations/content/element.rs:L89-L97](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/content/element.rs#L89-L97) — `is_locatable()`/`is_unqueriable()` 读取 vtable 位。

`query` 的参数类型在 cast 时就用这个位做门禁，所以错误在**求值期**就会被拦下，根本轮不到排版：

- [crates/typst-library/src/foundations/selector.rs:L421-L461](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/selector.rs#L421-L461) — `FromValue for LocatableSelector` 的 `validate`：元素选择器若 `!elem.is_locatable() || elem.is_unqueriable()` 则报 `"{name} is not locatable"`，并递归校验 `or`/`and`/`before`/`after`/`within` 的子选择器。

分层哈希策略的完整论证（为什么不用自增计数器）写在 `Locator` 的文档里，值得精读：

- [crates/typst-library/src/introspection/locator.rs:L49-L112](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/locator.rs#L49-L112) — 三种策略对比，最终方案：\( h_n = \mathrm{hash}(h_{n-1}, k_n) \)，逐层累积、无全局状态。

#### 4.1.4 代码实践

**实践目标**：亲手验证 Locatable 门禁与 divider 的可定位性。

1. 在任意目录新建 `locatable.typ`：

   ```typ
   #context query(divider).len()  // 预期输出 0（还没有 divider）
   #divider()
   #context query(divider).len()  // 预期输出 1
   ```

2. 用仓库里构建的 CLI 运行：`cargo run -p typst-cli -- compile locatable.typ`，打开 PDF 查看两处数字。
3. 再把第一行换成 `#context query(rect).len()` 重新编译，观察报错。

**需要观察的现象**：第 2 步两个数字分别是 0 和 1（query 看到的是**上一轮**的文档，这正是多轮迭代的直接证据）；第 3 步报错 `rect is not locatable`。

**预期结果**：divider（0.15 起 Locatable）可查询；rect 在 cast 阶段被拒。若你的本地二进制版本低于 0.15，`query(divider)` 也会报同样错误。具体输出文案以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

1. **练习**：`query(text)`、`query(<my-label>)`、`query(raw)` 分别能通过编译吗？
   **答案**：`query(text)` 不能，text 非 Locatable，在 [selector.rs:L426-L429](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/selector.rs#L426-L429) 被拦；`query(<my-label>)` 能，标签选择器无需 Locatable；`query(raw)` 能，raw 在白名单里（location.rs 文档 Text 类）。
2. **练习**：为什么 `Location` 用分层哈希而不是文档序自增整数？
   **答案**：三条理由见 [locator.rs:L50-L61](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/locator.rs#L50-L61)：自增 ID 会因内省生成的新内容而整体平移（跨轮不稳定）、会被局部编辑打乱（增量编译缓存失效）、需要全局计数器状态（阻碍并行）。
3. **练习**：想给某个第三方或自定义元素加上可定位能力，最少要改几处？
   **答案**：元素声明加 `Locatable` 标记即可；但按惯例还应同步更新 location.rs 的手写清单文档（divider 的提交就是两处一起改的）。

### 4.2 Tag 与 Introspector 收集：从 realize 到 Frame 再到查询表

#### 4.2.1 概念说明

`Location` 分配完只是"有了身份证"，还要让排版产物里**物理地留下痕迹**，下一轮编译才能据此回答查询。这个痕迹就是 `Tag`：realize 阶段为每个可内省元素生成一对 `Tag::Start` / `Tag::End`，作为 `TagElem` 混入内容流，最终被排版器原样写进 `Frame`（`FrameItem::Tag`），自身不占空间。一轮排版结束后，`PagedIntrospector` 扫描所有页面的 Frame，把 Tag 收集成一张"元素 → 文档位置"的表，供下一轮（以及导出后的 `typst eval`）查询。

#### 4.2.2 核心流程

```
realize 阶段（每轮排版内部）
  prepare(elem):
    key   = hash(elem)                       # 元素内容哈希，用于测量匹配
    flags = { introspectable, tagged }        # 4.1 的能力位 + 有无标签
    若 elem 无 location 且需要 → locator 分配
    产出 (Tag::Start(elem, flags), Tag::End(loc, key, flags))
        ↓ 排版
Frame 树（含 FrameItem::Tag）
        ↓ PagedIntrospector::new（每轮结束时）
discover_frame: 递归遍历 Frame
  Tag::Start → 记录 (元素, 页号+坐标)，seen 去重
  Tag::End   → 闭合该 location 的区间范围
  Group(parent) → 插入组：乱序内容（如脚注）挂回父元素的文档序
        ↓
ElementIntrospector: elems 列表 + locations/labels/keys 索引 + 查询缓存
```

#### 4.2.3 源码精读

realize 的 `prepare` 是身份与痕迹的诞生地：

- [crates/typst-realize/src/lib.rs:L540-L556](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs#L540-L556) — 注释明确说"只给显式标记为 locatable 和带标签的元素生成 location，因为有开销"；`introspectable` 三条件：`is_locatable() || label().is_some() || location().is_some()`（第三个条件覆盖来自 query 结果的元素）。
- [crates/typst-realize/src/lib.rs:L575-L582](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs#L575-L582) — 在合成（synthesize）与样式物化**之后**创建起止 Tag，所以 Tag 里携带的 Content 已含合成字段（如 heading 的编号）；`key` 存进 `Tag::End` 只是为了让两个变体内存尺寸均衡。

Tag 的数据结构：

- [crates/typst-library/src/introspection/tag.rs:L11-L24](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/tag.rs#L11-L24) — `Tag::Start(Content, TagFlags)` / `Tag::End(Location, u128, TagFlags)`。
- [crates/typst-library/src/introspection/tag.rs:L46-L61](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/tag.rs#L46-L61) — `TagFlags { introspectable, tagged }`：前者决定是否进入 Introspector，后者服务 PDF 标签树。
- [crates/typst-library/src/introspection/tag.rs:L63-L83](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/tag.rs#L63-L83) — `TagElem` 是承载 Tag 的元素包装，文档注释点破机制："held element becomes available for introspection in the **next** compiler iteration"。

收集端——`PagedIntrospector`：

- [crates/typst-layout/src/introspect.rs:L37-L58](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/introspect.rs#L37-L58) — `PagedIntrospector::new(&[Page])`：逐页记录页码编号（numbering）与补充文本（supplement），对每页 Frame 调 `discover_frame`，闭包把 Frame 内坐标加上页号包装成 `PagedPosition`。
- [crates/typst-layout/src/introspect.rs:L177-L207](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/introspect.rs#L177-L207) — `discover_frame`：遇 `FrameItem::Tag` 上报；遇 `Group` 递归并累积变换矩阵；若 Group 带 `parent`（如脚注区），用插入组把它挂回父元素的文档序；遇 `Link` 记录链接目标集合 `frame_link_targets`。

目标无关的查询引擎 `ElementIntrospector`：

- [crates/typst-library/src/introspection/introspector.rs:L170-L190](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L170-L190) — 核心字段：`elems`（文档序元素列表）、`locations`（`Location → Range<usize>`，把"元素及其全部概念后代"编码成区间，使 `before`/`after`/`within` 选择器可以二分查找）、`labels`、`keys`（测量辅助）、`queries`（查询缓存）。
- [crates/typst-library/src/introspection/introspector.rs:L513-L530](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L513-L530) — `discover_tag`：`seen` 集合按 location 去重——测量（measure）会让同一内容被排版多次，只有一份进入索引。
- [crates/typst-library/src/introspection/introspector.rs:L578-L631](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L578-L631) — `finalize`/`visit`：按 Start/End 事件流重建嵌套区间，Start 时先记 `idx..idx+1`，遇到 End 时把区间终点扩到当前长度，并把插入组填进父元素的范围内。
- [crates/typst-library/src/introspection/introspector.rs:L194-L342](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L194-L342) — `query()` 按 `Selector` 变体派发：元素选择器线性过滤、标签查索引、`Or` 用 BTreeSet 归并排序、`And` 取最小列表再二分交集、`Before`/`After`/`Within` 基于 `locations` 区间二分；开头先查 `QueryCache`（注释说明：不同顶层查询常有共享子查询，例如多个 counter 查询都依赖同一个全局 counter 序列）。

收集的触发点在 `PagedDocument` 构造时，一轮排版结束即得到该轮的内省器：

- [crates/typst-layout/src/document.rs:L29-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs#L29-L41) — `PagedDocument::new` 内部 `PagedIntrospector::new(&pages)`。
- [crates/typst-layout/src/document.rs:L59-L66](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs#L59-L66) — `Hash for PagedDocument` 故意不哈希 introspector：它完全由 pages 派生，这样哈希更快且语义等价。

#### 4.2.4 代码实践

**实践目标**：通过打点观察"每轮排版各发现多少个可内省元素"。

1. 在本地分支给 `discover_tag` 加一行临时日志（**示例代码**，观察完请还原）：

   ```rust
   // crates/typst-library/src/introspection/introspector.rs::discover_tag 开头
   if flags.introspectable {
       eprintln!("discover {:?}", tag);
   }
   ```

2. 重新 `cargo build -p typst-cli`，编译一篇含 `#outline()` 与若干标题的文档，把 stderr 重定向到文件。
3. 统计：`grep -c 'Discover(heading' log.txt`（按 Tag 的 Debug 输出格式调整，见 [tag.rs:L36-L44](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/tag.rs#L36-L44) 的 `Start(heading, Location(..))` 格式）。

**需要观察的现象**：同一批 heading 的 Tag 会在日志中**成组重复出现**——每个布局轮次收集一次；元素数量随轮次增长（outline 条目本身也可定位）。

**预期结果**：重复组数 = 实际布局轮数；同一组内 `Location` 值逐轮相同（分层哈希的跨轮稳定性）。具体输出待本地验证。

#### 4.2.5 小练习与答案

1. **练习**：为什么 `Tag::Start` 里携带的 Content 要在 synthesize 与 materialize 之后才克隆？
   **答案**：[realize/lib.rs:L575-L579](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-realize/src/lib.rs#L575-L579) 注释：这样 query 拿到的元素包含合成字段与已物化样式，`show heading: it => it.numbering` 这类规则才拿得到编号。
2. **练习**：`discover_tag` 的 `seen` 去重防的是什么场景？
   **答案**：防测量——同一内容被 `measure` 或试探布局多次排版时会产生多份相同 `Location` 的 Tag，[introspector.rs:L517-L520](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L517-L520) 只让第一份进入索引，避免 query 结果重复。
3. **练习**：脚注正文排在页面底部，为什么 `query(footnote.entry)` 的结果仍按正文顺序而非页面位置顺序？
   **答案**：脚注 Frame 是带 `parent` 的 Group，`discover_frame` 用 `start_insertion`/`end_insertion`（[introspect.rs:L191-L197](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/introspect.rs#L191-L197)、[introspector.rs:L562-L574](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/introspector.rs#L562-L574)）把它的条目插回父元素的文档序区间。

### 4.3 query/counter 语义：Introspect 询问协议

#### 4.3.1 概念说明

脚本层的 `query()`、`counter().at()`、`here().page()`、`locate()` 形态各异，但在编译器内部它们是同一种东西：一次**内省询问（Introspection）**——"带着这个问题去问上一轮的内省器，把答案记下来"。统一抽象的好处有二：其一，comemo 追踪 `Introspector` trait 的每次调用，天然知道本轮排版"看过"上轮文档的哪些部分，这是收敛判定的基础；其二，每个询问类型自带 `diagnose`，不收敛时能生成贴近用户语言的警告（"第 3 个标题的页码未稳定"），而不是干巴巴的内部错误。`counter` 与 `state` 之所以不用现成的 `QueryIntrospection` 而自定义询问类型，就是为了定制诊断文案（见 convergence.rs 的 trait 文档）。

#### 4.3.2 核心流程

```
#context { query(heading) }          ← 用户代码
  求值期：context 体被包成 ContextElem（Locatable）挂起
  realize 期：ContextElem 的内置 show 规则回放闭包，此刻才有 location/styles
    query():
      context.introspect()?           ← 门禁：没有 context 直接报错
      engine.introspect(QueryIntrospection(selector, span))
        → introspection.introspect(engine, tracked_introspector)
            → introspector.query(&selector)      ← 真正查表
        → sink.introspection(...)                ← 登记询问，供不收敛诊断用
```

counter 的读取同样走询问：`CounterAtIntrospection` 先把 counter 键翻译成选择器，扫描文档序中该选择器之前的全部 `CounterUpdateElem` 得到状态序列，再取目标位置之前的那一份。

#### 4.3.3 源码精读

`context` 的门禁与挂起机制：

- [crates/typst-library/src/foundations/context.rs:L47-L51](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/context.rs#L47-L51) — `Context::introspect()`：要求至少有 location 或 styles 之一，否则报 "can only be used when context is known"。这就是 `query` 必须包在 `context` 里的根源。
- [crates/typst-library/src/foundations/context.rs:L64-L87](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/context.rs#L64-L87) — `ContextElem` 本身是 `Locatable` 的（它需要位置才能给出 `here()`），其内置 show 规则 `CONTEXT_RULE` 用元素自身的 location + 当前样式链构造 `Context` 后调用用户闭包——u3-l7 讲过的"context 表达式延迟到 realize 期回放"在此落地。

`query` 函数本体：

- [crates/typst-library/src/introspection/query.rs:L159-L176](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/query.rs#L159-L176) — 两行核心：`context.introspect()?` 门禁 + `engine.introspect(QueryIntrospection(target.0, span))`；参数类型 `LocatableSelector` 即 4.1 的门禁类型。
- [crates/typst-library/src/introspection/query.rs:L179-L203](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/query.rs#L179-L203) — `QueryIntrospection`：`Output = EcoVec<Content>`；`diagnose` 里有个精巧设计——先比较**长度**历史，长度不稳就报 "number of heading elements did not stabilize" 并附逐轮数量 hint，长度稳但内容不稳则只报 "query ... did not stabilize"。
- 同文件还有 `QueryFirstIntrospection`、`QueryUniqueIntrospection`、`QueryLabelIntrospection`（L205-L273），分别对应 `first`/唯一匹配/标签查找，`@ref` 的解析用的正是 `QueryLabelIntrospection`：[crates/typst-library/src/model/reference.rs:L215-L221](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/reference.rs#L215-L221) — `RefElem::synthesize` 里 `let Ok(found)` 的写法表明：第一轮标签还不存在时不立刻报错（错误是询问的 `Output` 的一部分，收敛后仍失败才真正浮出）。

`Engine::introspect` 是所有询问的唯一入口：

- [crates/typst-library/src/engine.rs:L104-L117](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/engine.rs#L104-L117) — 取出被 comemo 追踪的 introspector 引用、执行询问、把询问登记进 `Sink`。注释点明副作用："If the document does not converge, the recorded introspections are used to determine the cause"。
- [crates/typst-library/src/engine.rs:L150-L172](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/engine.rs#L150-L172) — `Sink` 的字段表：`introspections`（询问记录）、`delayed`（延迟错误——前几轮 introspector 未就绪时 show 规则抛出的错误先压着，末轮仍在才升级为致命错误）、`warnings`。

counter 的读取语义：

- [crates/typst-library/src/introspection/counter.rs:L383-L447](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L383-L447) — `Counter::display`：`at` 缺省取 `context.location()`（即 `here()`），然后 `engine.introspect(CounterAtIntrospection / CounterBothIntrospection)`；`both: true` 用于 `page numbering: "1 / 1"` 这类"当前页 + 总页数"。
- [crates/typst-library/src/introspection/counter.rs:L788-L819](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L788-L819) — `CounterAtIntrospection::introspect`：`counter.select()` 把键翻译为选择器 → `sequence()` 扫描全部更新元素 → `query_count_before` 二分出目标位置之前有几个 → 取序列中该偏移的状态；若这是**页计数器**，还要用 `introspector.page(loc)` 与扫描时记录的物理页号之差做校正——因为逻辑页码（可被 `counter(page).update` 改写）与物理页号可能脱节。
- [crates/typst-library/src/introspection/counter.rs:L751-L773](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L751-L773) — `ManualPageCounter::visit`：布局期间的物理页计数器直接扫 Frame 里的 `Tag::Start(CounterUpdateElem)` 应用更新——页码这种"边排边走"的计数不需要走完整询问协议。

位置类询问与诊断文案：

- [crates/typst-library/src/introspection/location.rs:L99-L102](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L99-L102) 与 [L144-L148](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L144-L148) — `location().page()` / `location().position()` 分别对应 `PageIntrospection` / `PositionIntrospection`；文档特别提醒：`page()` 返回物理页号，要逻辑页码请用 `counter(page).at(loc)`。
- [crates/typst-library/src/introspection/location.rs:L393-L417](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L393-L417) — `format_convergence_warning`：用最终内省器反查该 location 对应的元素名，生成 "heading position did not stabilize" + "heading was created here"（附源码 span）+ 逐轮观测值列表。

#### 4.3.4 代码实践

**实践目标**：复现"影响自身的查询"，亲眼看收敛警告与逐轮观测值。

1. 新建 `self-query.typ`：

   ```typ
   = Real

   #context {
     let elems = query(heading)
     let count = elems.len()
     count * [= Fake]
   }
   ```

2. 编译：`cargo run -p typst-cli -- compile self-query.typ`，观察终端警告。
3. 运行仓库现成的测试对照：`cargo testit convergence-query`（见 [tests/suite/introspection/convergence.typ:L3-L47](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/introspection/convergence.typ#L3-L47)，其断言了 "run 1: 0 → run 5: 4 → final: 5" 的完整序列）。

**需要观察的现象**：警告 "document did not converge within five attempts"，hint 逐轮列出查询数量 `run 1: 0, run 2: 1, … final: 5`；PDF 里最终有 5 个 Fake 标题（Typst 放弃迭代前的末轮结果）。

**预期结果**：与测试文件注释的断言一致；这是理解"鸡生蛋查询永不收敛"的最小样本。

#### 4.3.5 小练习与答案

1. **练习**：把 `query(heading)` 写在 `context` 外面会发生什么？为什么？
   **答案**：编译错误 "can only be used when context is known"，hint 建议包进 `context`；根源是 [query.rs:L173](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/query.rs#L173) 的 `context.introspect()?`——query 的答案依赖"问的人在哪"，没有 location 就没有稳定答案。
2. **练习**：`#set page(numbering: "1")` 后 `counter(page).at(here())` 与 `here().page()` 何时会不相等？
   **答案**：当文档中有 `counter(page).update(3)` 之类手动改写时：`at()` 返回逻辑计数（可跳号），`page()` 返回物理页号（恒从 1 连续递增），见 [location.rs:L83-L90](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/location.rs#L83-L90) 与 [counter.rs:L805-L812](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L805-L812) 的 delta 校正。
3. **练习**：为什么 `Sink` 要区分 `delayed` 错误与普通错误？
   **答案**：前几轮 introspector 里标签/文献可能暂不存在，show 规则抛错是**预期内**的；[engine.rs:L160-L165](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/engine.rs#L160-L165) 注释：先吞掉继续排，只有末轮仍存在的错误才在主循环结束时升级（[typst/src/lib.rs:L188-L192](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L188-L192)）。

### 4.4 迭代收敛判定与两遍编译

#### 4.4.1 概念说明

"两遍编译"是通俗说法：Typst 实际是**求值一遍、排版最多五遍**。求值（eval）只跑一次，因为它不依赖内省结果；`context` 块被挂起为 `ContextElem`，`@ref` 的解析、outline 的条目生成都发生在每轮排版的 realize 里。于是主循环的任务是：用上一轮文档的内省器排这一轮的版，然后判断"这一轮读过的那些答案，在新文档里是否仍然成立"。判定有两条路径：**快路径**由 comemo 完成——本轮所有对 tracked `Introspector` 的调用都记录了约束，`constraint.validate` 检查新内省器是否满足全部约束，代价低；**慢路径**只在五轮耗尽时触发——`analyze` 把本轮登记的每个询问在六个历史内省器上重放一遍，构造 `History`，比较末两轮输出，生成针对每个不稳定询问的警告。

#### 4.4.2 核心流程

```
compile_impl:
  eval(main) → content            # 只有一次
  history = []                    # 容量 MAX_ITERS - 1 = 4
  loop:
    introspector = history.last()?.introspector() 或 EmptyIntrospector（首轮）
    constraint = comemo::Constraint::new()
    engine.introspector = introspector.track_with(&constraint)   # 带约束追踪
    document = T::create(...)      # realize + layout，期间所有询问被追踪+登记
    if constraint.validate(document.introspector()):
        收敛，break                 # 快路径
    if history 满（已排 5 轮）:
        analyze(world, 六个内省器, subsink.introspections())
        → 逐询问 History 比较 hash(第4轮) == hash(第5轮) ?
        → 汇总 "document did not converge within five attempts" 警告，break
    history.push(document)
  末轮 document 仍作为产物返回（不收敛不报错，只警告）
```

轮次与警告里 "run N / final" 的对应关系：`run 1` 是首轮排版时查询的**空**内省器，`run k`（k ≥ 2）是第 k−1 轮产出的内省器，`final` 是第 5 轮产物——所以不收敛时总共排了 5 遍版。

#### 4.4.3 源码精读

主循环全部集中在 `compile_impl` 后半段：

- [crates/typst/src/lib.rs:L118-L132](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L118-L132) — 主文件求值恰好一次，得到 `content`；循环里再未出现 `eval`。
- [crates/typst/src/lib.rs:L134-L145](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L134-L145) — `history` 是容量 `MAX_ITERS - 1` 的 ArrayVec；每轮开一个名为 `iter (N)` 的计时作用域（u4-l1 讲过的 `--timings` 就靠它数轮数）；首轮的 introspector 是 `EmptyIntrospector`——这解释了 4.3 实践里 "run 1: 0"：第一轮任何查询都得到空结果。
- [crates/typst/src/lib.rs:L147-L157](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L147-L157) — 关键一行 `introspector.track_with(&constraint)`：comemo 在追踪调用的同时记录约束；`T::create` 即 realize+layout。
- [crates/typst/src/lib.rs:L159-L163](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L159-L163) — 快路径收敛判定：`constraint.validate(document.introspector())` 为真则本轮 subsink 并入总 sink 并 break。
- [crates/typst/src/lib.rs:L164-L183](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L164-L183) — 五轮耗尽：组装 `[empty, history[0..3], 最终]` 共 `MAX_ITERS + 1 = 6` 个内省器交给 `analyze`，其产出的警告逐条 `sink.warn`；**然后仍然 break 并返回 document**——不收敛不是错误。
- [crates/typst/src/lib.rs:L185](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L185) — 未满则推入 history 进入下一轮。

收敛模块的公共参数与诊断入口：

- [crates/typst-library/src/introspection/convergence.rs:L16-L20](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L16-L20) — `MAX_ITERS = 5`、`ITER_NAMES`（"iter (1)"…"iter (5)"）、`INSTANCES = MAX_ITERS + 1`。
- [crates/typst-library/src/introspection/convergence.rs:L25-L70](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L25-L70) — `analyze`：逐询问 `diagnose`；开头的长注释解释一个微妙情形——若询问把 comemo 观察到的数据**过滤后**才输出（比如查询后只取布尔值），可能 comemo 校验失败而文档实际已收敛，此时 `diags` 为空，就不再发收敛警告。
- [crates/typst-library/src/introspection/convergence.rs:L93-L140](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L93-L140) — `Introspect` trait：`type Output: Hash`（输出越精简可能越早一轮收敛，文档举了 `location.page()` vs `location.position().page` 的例子）、`introspect`（执行）、`diagnose`（不收敛时如何报警）。
- [crates/typst-library/src/introspection/convergence.rs:L174-L187](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L174-L187) — 类型擦除后的 `diagnose`：对六个内省器重放询问得到 `History`，`!history.converged()` 才生成诊断。
- [crates/typst-library/src/introspection/convergence.rs:L240-L250](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L240-L250) — `converged` 的实现是比较第 5、6 份输出的哈希；注释强调用哈希而非 `==`：`0.0`（float）变 `0`（int）虽相等但可被下一轮观察出差异，不算收敛。
- [crates/typst-library/src/introspection/convergence.rs:L268-L280](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L268-L280) — `hint`：把观测值格式化为 "run 1..run 5 / final" 列表，正是 4.3 实践中看到的警告形态。

性能视角的两点补充：收敛判定本身是 O(询问数) 的哈希比较而非 O(文档)；而 comemo 的增量性意味着第二轮起 realize/layout 的记忆化结果在输入（Content、样式、以及**被满足的约束**）不变时直接复用——这就是 watch 模式下小改动通常只需毫秒级重排的原因（u5-l8 会展开）。

#### 4.4.4 代码实践

**实践目标**：数出一篇真实文档的布局轮数，并验证"去掉内省依赖 → 轮数下降"。

1. 新建 `toc.typ`（含目录、页码、交叉引用三个内省源）：

   ```typ
   #set page(numbering: "1")
   #outline(title: [目录])

   = 引言 <intro>
   相关讨论见 @discussion。

   = 方法
   #lorem(60)

   = 讨论 <discussion>
   呼应 @intro 的观点。
   ```

2. 带计时编译：`cargo run -p typst-cli -- compile --timings trace.json toc.typ`（`--timings` 参数见 [crates/typst-cli/src/args.rs:L465-L471](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/args.rs#L465-L471)）。
3. 统计 trace.json（Chrome Tracing 格式）中名字形如 `iter (N)` 的事件条数，即布局轮数。
4. 把 `#outline(...)` 一行和两处 `@...` 引用全部删掉，重复第 2、3 步。

**需要观察的现象**：第 3 步得到 2 或 3（outline 条目与引用页码需要后续轮次补齐）；第 4 步得到 1——没有任何询问时首轮 `constraint.validate` 即通过。

**预期结果**：轮数随内省依赖移除单调下降；具体数值依赖文档结构（待本地验证）。也可用 `cargo testit convergence` 一次跑完整个收敛测试组做回归参照。

#### 4.4.5 小练习与答案

1. **练习**：为什么 eval 在循环外而 layout 在循环内？
   **答案**：eval 的输入只有源码与全局库，输出 Content 与内省结果无关；而 `context` 回放、`@ref` 解析、outline 生成都发生在 realize（每轮 layout 内部），必须逐轮重跑，见 [typst/src/lib.rs:L124-L157](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L124-L157) 的结构。
2. **练习**：警告里的 "run 5" 与 "final" 各对应哪次排版的观察结果？
   **答案**：`run k` = 第 k 轮排版时所用内省器的答案（run 1 恒为空）；`final` = 第 5 轮产物内省器的答案，格式化逻辑在 [convergence.rs:L268-L275](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/convergence.rs#L268-L275)。
3. **练习**：文档不收敛时编译会失败吗？
   **答案**：不会。五轮后 `analyze` 只生成警告（汇总警告 + 每个不稳定询问的细节），随后照常 break 并返回末轮 document；只有 `sink.delayed()` 非空（末轮仍有真错误）才返回 `Err`，见 [typst/src/lib.rs:L164-L194](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L164-L194)。

## 5. 综合实践

**任务**：做一份"内省依赖体检报告"，把本讲四个模块串起来。

1. **建文档** `report.typ`：包含 `#outline()`、3 个带标签的章节、两处 `@` 交叉引用、一个 `#counter(eq).display()` 编号的公式块，以及一个 `#divider()`。
2. **数轮数**：`cargo run -p typst-cli -- compile --timings trace1.json report.typ`，统计 `iter (N)` 事件数，记为 R1。
3. **看逐轮观测**：把交叉引用改成 `#context locate(<intro>).position()`，再编译一次并观察是否出现 "did not stabilize" 警告；对照 [tests/suite/introspection/convergence.typ:L77-L88](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/introspection/convergence.typ#L77-L88) 的收敛位置测试理解 hint。
4. **减依赖**：删除 outline 与全部引用/counter 显示，得到 `trace2.json`，轮数记为 R2。验证 R2 < R1 且 R2 = 1。
5. **验可定位性**：在文档末尾加 `#context query(divider).len()`，确认输出 1（4.1 的门禁体系 + 4.3 的询问协议 + 4.4 的多轮机制三者协同的最终验证）。
6. **（可选，打点还原后）** 依 4.2.4 的日志法交叉核对：日志中 Tag 重复组数 == 计时文件中的轮数。

**验收标准**：能口头回答——R1 为什么大于 1？R2 为什么等于 1？第 5 步的 `1` 是哪一轮排版给出的答案？

## 6. 本讲小结

- **Locatable 白名单**：元素声明 `#[elem(Locatable)]` → vtable 能力位 → `LocatableSelector` cast 门禁；权威清单手写在 location.rs 文档中，divider 自 0.15 起加入（`@divider` 现可查询）。
- **Location 是分层哈希不是序号**：\( h_n = \mathrm{hash}(h_{n-1}, k_n) \)，兼顾跨轮一致、跨编辑稳定与无状态并行。
- **Tag 是留痕**：realize 的 `prepare` 为可内省元素分配 Location 并产出起止 Tag，排进 Frame；`PagedIntrospector::new` 每轮结束扫 Frame 收集出带位置/标签/区间索引与查询缓存的 `ElementIntrospector`。
- **一切查询皆是询问**：`query`/`counter`/`position` 统一为 `Introspect` 询问，经 `Engine::introspect` 执行并登记到 Sink；`Output: Hash` 的粒度直接影响收敛速度与诊断质量。
- **收敛双路径**：comemo `constraint.validate` 是每轮的快路径；五轮耗尽才由 `analyze` 重放询问、比较末两轮哈希并生成 "run 1..final" 逐轮警告；不收敛只警告不失败，返回末轮文档。
- **求值一次、排版至多五次**：`MAX_ITERS = 5`，"两遍编译"是习惯说法；`iter (N)` 计时作用域让轮数可被 `--timings` 直接观测。

## 7. 下一步学习建议

- **u5-l1（PDF 导出）**：`TagFlags::tagged` 与 `frame_link_targets` 在导出层如何变成 PDF 标签树与内部链接，是本讲 Tag 体系的下游消费者。
- **u5-l8（增量编译与 comemo）**：本讲的 `track_with(&constraint)` 只是 comemo 约束机制的冰山一角，该讲系统展开 track/memo 失效与 watch 场景。
- **继续阅读源码**：`introspection/state.rs`（state 与 counter 同构，但诊断多一条 help 链接）；`introspection/locate.rs`（`locate()` 函数 = resolve_unique + 位置询问的组合）；以及 [tests/suite/introspection/](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/introspection/convergence.typ) 全目录——`switch.typ` 这个"按轮次切换行为"的测试工具本身就是内省机制的绝佳教材。
