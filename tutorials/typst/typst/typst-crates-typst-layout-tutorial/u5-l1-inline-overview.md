# 段落布局总览：collect→prepare→linebreak→finalize

## 1. 本讲目标

本讲是「行内（段落）布局」单元（u5）的入口篇。在前面的单元里，我们已经看到 flow（块级布局）如何把页面切成区域、把块塞进区域；但当一个块的内容是「连续的文字、行内 box、链接、脚注标记」时，flow 会把整段正文交给**行内布局**（inline layout）去排成一行行文字。

本讲不深究整形（shaping）、断行算法（Knuth-Plass）等细节——那些留给 u5-l2～u5-l5。本讲只解决一个问题：**一段文字从进入 inline 模块到变成一帧帧文字图，到底经历了哪几步？每一步的输入输出是什么？谁在控制首行缩进？**

学完后你应当能够：

1. 画出 inline 模块「四段管线」的数据流图，并说出每一步的输入输出类型。
2. 理解 `Config` 这个贯穿全程的共享配置是怎么从 `ParElem` 的样式一步步解析出来的，`justify`/`linebreaks`/`hyphenate`/`lang` 等选项如何流入管线。
3. 区分 `ParSituation::First`/`Consecutive`/`Other` 三种段落情境，并解释它们如何决定首行缩进（含 `in_list` 判断）。

## 2. 前置知识

本讲承接 u2-l2（Regions）与 u2-l3（Frame/Fragment），你只需记住两点：

- **段落排版只吃一个尺寸，不吃多区域队列。** flow 那边可能有好几个候选区域（`Regions` 带 `backlog`），但段落入口 `layout_par` 收到的画布只有一个 `Size`（宽 `region.x`、高 `region.y`）加一个 `expand: bool`。这是因为段落内部「在哪里换行」完全由宽度决定，高度只是「能排多少行」的容器；排不下的行会被 flow 的多区域机制搬到下一页/下一列。换句话说，**段落的「跨区域断裂」是 flow 的职责，不是 inline 的职责**。
- **段落的产出是一个 `Fragment`（即若干 `Frame`）。** 每一行文字对应一帧（实际是一帧里的一行，但 finalize 会把多行叠成一帧）。最终这一串 frame 会作为 `LineChild` 回到 flow 的分发流程里。

另外请回忆 u1-l3 / u2-l1 的关键模式：入口函数普遍是「公开薄封装 → `#[comemo::memoize]` 的 `_impl` 函数」。inline 模块也遵循这个模式，但有个**重要例外**——下文会讲到。

## 3. 本讲源码地图

本讲主角是 [src/inline/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs)，它是 inline 子系统的「调度中心」，把四段管线串起来。

| 文件 | 作用 |
|------|------|
| `src/inline/mod.rs` | 入口 `layout_par`/`layout_inline`、调度函数 `layout_inline_impl`、配置 `configuration`、`Config`/`ConfigBase`/`ParSituation` 定义 |
| `src/inline/collect.rs` | **第一步 collect**：把异构 children 拍平成一个 `String` + `Vec<Segment>` |
| `src/inline/prepare.rs` | **第二步 prepare**：BiDi 分析 + 文本整形，产出 `Preparation` |
| `src/inline/linebreak.rs` | **第三步 linebreak**：断行，产出 `Vec<Line>` |
| `src/inline/finalize.rs` | **第四步 finalize**：把行提交成 frame，产出 `Fragment` |
| `src/inline/line.rs` | 单行 `Line` 类型与 `commit`（把一行的 item 落成 frame） |

`mod.rs` 顶部的 `mod` 声明揭示了整个子系统的组成（[src/inline/mod.rs:1-9](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L1-L9)）：除四段管线外，还有 `shaping`（整形，u5-l4）、`deco`（装饰，u5-l5）、`box`（行内盒）。本讲只看四段主线。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看四段管线全貌（4.1，对应最小模块 **inline**），再看贯穿全程的配置（4.2，对应最小模块 **Config**），最后看段落情境与首行缩进（4.3，对应最小模块 **ParSituation**）。

### 4.1 段落排版的标准四步管线

#### 4.1.1 概念说明

把一段混合内容（普通文字、`#box`、链接、脚注引用、智能引号……）排成一行行整齐的文字，是个看似简单实则牵涉极多的问题：文字方向可能左右混排（BiDi）、断行要选最优位置、连字符要不要加、两端要不要对齐、CJK 与拉丁字之间要不要加间距……

Typst 的做法是把这个大问题切成**四个相对独立的阶段**，每个阶段只做一件事，前一阶段的输出恰好是后一阶段的输入：

```
collect    把异构 children 拍平成「一个字符串 + 一串 segment」
   ↓
prepare    对整串文字做 BiDi 分析 + 整形，产出「可直接测量的 item 列表」
   ↓
linebreak  在字符串上选断点，把 item 切成「一行一行的 Line」
   ↓
finalize   把每行 Line 提交成 frame，叠成一个 Fragment
```

这种分层的核心好处是**整形与断行解耦**：整形（很贵）在 prepare 里只做一次（或尽量少做），断行（要反复试错）在 linebreak 里只对「已经整形好的 item」做测量和切片，避免「每试一个断点就重新整形整段文字」。

#### 4.1.2 核心流程

完整的输入输出与数据流如下（`Pair` = 已 realize 的「元素 + 样式链」二元组，见 u1-l4）：

```
children: &[Pair]                 ← 来自 realize（layout_par）或上层（layout_inline）
        │
        │  configuration()        ← 一次性，求出共享 Config
        ▼
      Config ──────────────────────────────────────────┐ (被后续四步借用)
        │                                              │
        │  collect(children, engine, locator,          │
        │          config, region)                     │
        ▼                                              │
(String, Vec<Segment>, SpanMapper)                    │
   text    segments   spans                            │
        │                                              │
        │  prepare(engine, config, text, segments, spans)
        ▼                                              │
   Preparation { text, config, bidi, items, indices, spans }
        │                                              │
        │  linebreak(engine, &p, width = region.x - hanging_indent)
        ▼                                              │
   Vec<Line>                                           │
        │                                              │
        │  finalize(engine, &p, &lines, region, expand, locator)
        ▼                                              │
   Fragment  (= Vec<Frame>)                            │
```

每一步的类型签名（这是本讲最重要的「速查表」）：

| 阶段 | 函数 | 输入 | 输出 |
|------|------|------|------|
| 0 | `configuration` | `ConfigBase`, `&[Pair]`, `StyleChain`, `Option<ParSituation>` | `Config` |
| 1 | `collect` | `&[Pair]`, Engine, `SplitLocator`, `&Config`, `Size` | `(String, Vec<Segment>, SpanMapper)` |
| 2 | `prepare` | Engine, `&Config`, `&str`, `Vec<Segment>`, `SpanMapper` | `Preparation` |
| 3 | `linebreak` | `&Engine`, `&Preparation`, `Abs`(宽度) | `Vec<Line>` |
| 4 | `finalize` | Engine, `&Preparation`, `&[Line]`, `Size`, `bool`, `SplitLocator` | `Fragment` |

注意 `Config` 在第 0 步算好后，被后四步**反复借用**（`&config` / 打包进 `Preparation`），整段排版共享同一份配置。

#### 4.1.3 源码精读

四段管线的调度就集中在 `layout_inline_impl` 这 16 行里（[src/inline/mod.rs:153-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L153-L178)）：

```rust
fn layout_inline_impl<'a>(
    engine: &mut Engine,
    children: &[Pair<'a>],
    locator: &mut SplitLocator<'a>,
    shared: StyleChain<'a>,
    region: Size,
    expand: bool,
    par: Option<ParSituation>,
    base: &ConfigBase,
) -> SourceResult<Fragment> {
    // Prepare configuration that is shared across the whole inline layout.
    let config = configuration(base, children, shared, par);

    // Collect all text into one string for BiDi analysis.
    let (text, segments, spans) = collect(children, engine, locator, &config, region)?;

    // Perform BiDi analysis and performs some preparation steps ...
    let p = prepare(engine, &config, &text, segments, spans)?;

    // Break the text into lines.
    let lines = linebreak(engine, &p, region.x - config.hanging_indent);

    // Turn the selected lines into frames.
    finalize(engine, &p, &lines, region, expand, locator)
}
```

这五句调用与上面的流程图一一对应，注释也直接点明了每步职责。注意第 4 步传给 `linebreak` 的宽度是 `region.x - config.hanging_indent`（[src/inline/mod.rs:174](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L174)）——悬挂缩进会缩窄每行可用宽度，因为它要从正文区里扣掉。

**两个入口，一个实现。** `layout_inline_impl` 是私有调度核心，它有两个公开入口：

- `layout_par`（[src/inline/mod.rs:44-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L44-L67)）→ `layout_par_impl`（[src/inline/mod.rs:70-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L70-L123)，**带 `#[comemo::memoize]`**）：语义段落入口。它先 `realize(RealizationKind::Par)` 把 `ParElem.body` 展开成 children，再用 `Some(situation)` 调 `layout_inline_impl`。
- `layout_inline`（[src/inline/mod.rs:126-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L126-L149)，**不带 memoize**）：非语义段落的行内布局入口（例如一个内部含行内元素的块、数学文本）。它收到的已经是 realize 好的 `&[Pair]`，用 `None` 情境调 `layout_inline_impl`。

> **关键区别**：memoize 边界在 `layout_par_impl`，不在 `layout_inline_impl`。所以「真正的段落」会享受 comemo 缓存（同样的元素+样式+区域直接命中），而 `layout_inline` 每次都实打实跑一遍。这与 u2-l1 讲的「公开薄封装 → memoize 的 `_impl`」模式一致，只是这里**只有段落入口**被 memoize。

两个入口在 flow 侧的使用也印证了这一点：flow 的收集器在排「真正的段落」时调 `layout_par` 并传入 `par_situation`（[src/flow/collect.rs:163-171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L163-L171)），排「非段落的行内内容」时调 `layout_inline`（[src/flow/collect.rs:120-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L120-L127)）。`par_situation` 正是由 flow 收集器维护的（见 u4-l2）。

#### 4.1.4 代码实践

**实践目标**：亲手在源码里把四段管线的输入输出类型对上号，建立「类型流」的肌肉记忆。

**操作步骤**：

1. 打开 [src/inline/mod.rs:153-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L153-L178)，在 `let config = configuration(...)`、`let (text, segments, spans) = collect(...)`、`let p = prepare(...)`、`let lines = linebreak(...)`、`finalize(...)` 这五行旁边，分别跳转到对应函数的定义处确认签名：
   - `collect` 签名见 [src/inline/collect.rs:125-131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L125-L131)，返回 `SourceResult<(String, Vec<Segment<'a>>, SpanMapper)>`。
   - `prepare` 签名见 [src/inline/prepare.rs:66-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L66-L72)，返回 `SourceResult<Preparation<'a>>`。
   - `linebreak` 签名见 [src/inline/linebreak.rs:152-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L152-L156)，返回 `Vec<Line<'a>>`。
   - `finalize` 签名见 [src/inline/finalize.rs:8-15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/finalize.rs#L8-L15)，返回 `SourceResult<Fragment>`。
2. 画一张表，把每行的「变量名 → 类型 → 进入哪个下一步」填上，例如：
   - `config: Config` → 借给 collect、prepare；打包进 Preparation 后再被 linebreak/finalize 用。
   - `(text, segments, spans)` → 三者原封不动整体喂给 prepare。
   - `p: Preparation` → 借给 linebreak 和 finalize。

**需要观察的现象**：你会发现 `Config` 是**值**（第 0 步算出后移动/借用），而 `Preparation` 之后全部是**借用**（`&p`），没有再次克隆整段整形结果——这正是「整形只做一次」的体现。

**预期结果**：你能不看源码复述出「`Pair` → `(String, Segment, Span)` → `Preparation` → `Vec<Line>` → `Fragment`」这条类型链。无需运行命令，这是源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `linebreak` 收到的宽度是 `region.x - config.hanging_indent`，而 `finalize` 里决定最终 frame 宽度时又会把 `hanging_indent` 加回来？

**答案**：悬挂缩进（hanging indent）会让「除第一行外的所有行」向右缩进。排每行文字时，可用宽度必须先扣掉这部分（所以 linebreak 用减后的宽度）；但整段 frame 的总宽度要包含这个缩进区域（finalize 在 [src/inline/finalize.rs:21-24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/finalize.rs#L21-L24) 计算 fit-to-width 时会加上 `p.config.hanging_indent`），否则 frame 会比正文区窄。

**练习 2**：`layout_par_impl` 被 `#[comemo::memoize]` 标注，但 `layout_inline_impl` 没有。如果一个非段落行内布局（走 `layout_inline`）被反复调用，会发生什么？

**答案**：`layout_inline_impl` 每次都会实打实跑完四段管线，不享受 comemo 缓存。这是有意为之：非段落行内布局通常出现在已经被上层缓存的语境里（如数学排版、flow 内联收集），再加一层 memoize 反而增加缓存键计算开销。真正的段落走 `layout_par_impl`，在那里被缓存。

---

### 4.2 Config：贯穿全程的共享配置

#### 4.2.1 概念说明

段落排版有大量「整段统一」的设置：要不要两端对齐、用哪种断行策略、要不要连字符、文字方向、字体大小、CJK-拉丁间距……这些设置如果每一步都重新从样式链里读一遍，既慢又容易不一致。

Typst 的做法是：在四段管线**启动前**，用 `configuration` 函数一次性把这些设置读出来、解析好，存进一个 `Config` 结构体；之后四步都只借用这个 `Config`。这样既保证了「整段配置一致」，也让 `Config` 成为 comemo 缓存键的一部分——同样的 `Config` + 同样的输入就能命中缓存。

这里还区分了两个类型：

- `ConfigBase`：直接从 `ParElem` / 样式链取出的**原始值**（如 `first_line_indent: FirstLineIndent` 这种还带「是否对所有段落生效」语义的对象、`linebreaks: Smart<Linebreaks>` 这种可被自动推导的值）。
- `Config`：经过 `configuration` 解析后的**就绪值**（如 `first_line_indent: Abs` 已经是算好的具体长度、`linebreaks: Linebreaks` 已经定死）。

#### 4.2.2 核心流程

`configuration` 的解析逻辑可以归纳为三类：

1. **直接搬运**：`justify`、`font_size`、`dir`、`fallback`、`cjk_latin_spacing`、`costs` 等直接从样式链 resolve/get。
2. **带默认值的推导**：最典型的是 `linebreaks`——如果用户没显式设（`Smart::Auto`），就按「`justify` 为真则用 `Optimized`，否则用 `Simple`」推导（[src/inline/mod.rs:194-196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L194-L196)）。即「两端对齐的段落默认用优化断行」。
3. **情境相关的解析**：`first_line_indent` 和 `hanging_indent` 都依赖 `situation`（详见 4.3）。
4. **「全段统一才生效」的提取**：`hyphenate` 和 `lang` 用辅助函数 `shared_get`，只有当**所有 children 的样式链里这个值都相同**时才返回 `Some`，否则返回 `None`（[src/inline/mod.rs:303-313](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L303-L313)）。这样后续步骤可以据此决定是否启用整段连字符/整段语言优化，而不是逐字处理。

`Config` 的字段定义在 [src/inline/mod.rs:269-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L269-L300)，每个字段都有注释说明用途。

#### 4.2.3 源码精读

`configuration` 的全貌（[src/inline/mod.rs:181-242](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L181-L242)）。挑几处关键：

`linebreaks` 的自动推导：

```rust
linebreaks: base.linebreaks.unwrap_or_else(|| {
    if justify { Linebreaks::Optimized } else { Linebreaks::Simple }
}),
```

`hyphenate` 的「全段统一」提取——只有整段都设了同一个 `hyphenate` 才返回值，且若用户没设则回退到「是否 justify」：

```rust
hyphenate: shared_get(children, shared, |s| s.get(TextElem::hyphenate))
    .map(|uniform| uniform.unwrap_or(justify)),
```

注意 `shared_get` 的实现：它用 `group_by_key` 把 children 按样式链分组，再检查每组取出的值是否都等于「外层样式链取出的值」（[src/inline/mod.rs:303-313](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L303-L313)）。这段逻辑保证了「段落里混了不同语言的文字时，`lang` 会变成 `None`」，从而避免用单一语言规则去断一段多语言文字。

`ConfigBase` 与 `Config` 的对照（[src/inline/mod.rs:261-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L261-L300)）：

| 字段 | ConfigBase（原始） | Config（就绪） |
|------|------|------|
| 断行策略 | `linebreaks: Smart<Linebreaks>` | `linebreaks: Linebreaks`（已推导） |
| 首行缩进 | `first_line_indent: FirstLineIndent`（含量+all 语义） | `first_line_indent: Abs`（已按情境算成具体长度） |
| 悬挂缩进 | `hanging_indent: Abs` | `hanging_indent: Abs`（非段落时归零） |

#### 4.2.4 代码实践

**实践目标**：验证 `Config` 的「自动推导」与「全段统一」两条规则。

**操作步骤**：

1. 读 `linebreaks` 的推导分支（[src/inline/mod.rs:194-196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L194-L196)），预测以下两种 Typst 源码分别会走哪种断行：
   - `#set par(justify: true); 某段很长的中文与英文混排文字...`
   - `#set par(justify: false); 某段很长的文字...`
2. 读 `shared_get`（[src/inline/mod.rs:303-313](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L303-L313)），思考：一段文字里前半部分 `#set text(lang: "en")`、后半部分 `#set text(lang: "zh")`，`config.lang` 会是什么？

**需要观察的现象 / 预期结果**：

- 第 1 种（justify 为真、未显式设 linebreaks）→ `Linebreaks::Optimized`（Knuth-Plass 优化断行）；第 2 种 → `Linebreaks::Simple`（first-fit 简单断行）。
- 第 2 题：因为两段样式链不同，`shared_get` 的 `all(...)` 返回 `false`，`config.lang = None`。后续断行不会强行套用单一语言规则。

> 待本地验证：若想实测，可在一个 Typst 文档里用 `#context` + 自定义 show 规则观察断行效果差异，但本实践以源码推理为主，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `lang` 要用 `shared_get`（全段统一才返回 `Some`），而不是直接 `shared.get(TextElem::lang)` 取一个值？

**答案**：因为断行/连字符规则是语言相关的（英文按音节连字、中文可任意位置断行）。如果段落里混了多种语言，强行用某一个语言的规则去断整段会出错。`shared_get` 在「不一致」时返回 `None`，让 linebreak 回退到更保守的通用策略。

**练习 2**：`numbering_marker`（行号标记）在 `Config` 里是一个 `Option<Packed<ParLineMarker>>`，它如何流入后续步骤？

**答案**：`configuration` 从 `ParLine::numbering` 取出行号格式，若存在则打包成一个 `ParLineMarker`（[src/inline/mod.rs:221-231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L221-L231)）。它随 `Config` 进入 finalize，由 finalize 在每行 frame 上挂载行号 marker（详见 u5-l5 的 finalize）。

---

### 4.3 ParSituation：段落情境与首行缩进

#### 4.3.1 概念说明

同样是「首行缩进 2 字符」，读者通常希望：**一篇文章的第一段不缩进，第二段及之后才缩进**（这是中文排版和西文排版的常见习惯）。但「第一段」是个**上下文相关**的概念——它取决于这段文字前面有没有别的段落、是不是紧跟在分栏符后、是不是列表项里。

`ParSituation` 就是用来描述这个「上下文」的枚举。它由 flow 的收集器在收集过程中维护（见 u4-l2），排完一段就把状态翻成 `Consecutive`，遇到分栏/分页等边界翻成 `First`。`layout_par` 收到这个情境后，把它传给 `configuration`，由后者决定首行缩进到底要不要生效。

#### 4.3.2 核心流程

`ParSituation` 的三个变体（[src/inline/mod.rs:249-258](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L249-L258)）：

| 变体 | 含义 |
|------|------|
| `First` | 段落是 flow（容器或 page run）里的第一个孩子，或紧接一个分栏符之后 |
| `Consecutive` | 段落紧跟在另一个段落之后 |
| `Other` | 任何其他情况 |

而在 `Option<ParSituation>` 形态下，`None` 表示「这不是一个语义段落」（即走 `layout_inline` 的非段落行内布局）。

首行缩进的生效逻辑用一段决策表就能说清。设用户配置的首行缩进量为 `amount`（非零时）、`all` 标志为「是否对所有段落生效」：

```
对每个 situation，首行缩进是否生效（在 amount 非零的前提下）：

  First        →  all 为真  且  不在列表里(in_list=false)
  Consecutive  →  总是生效
  Other        →  all 为真
  None         →  从不生效
```

并且还要额外满足：文字水平对齐方向等于文字起始方向（`alignment.x == dir.start()`），即「左对齐的 LTR 文字」或「右对齐的 RTL 文字」才有首行缩进——居中/右对齐（对 LTR 而言）的段落不缩进。

`FirstLineIndent` 这个类型本身定义在 typst-library（[crates/typst-library/src/model/par.rs:641-646](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L641-L646)），它有 `amount()` 和 `all()` 两个方法分别返回缩进量和「是否全部段落生效」（[crates/typst-library/src/model/par.rs:681-689](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L681-L689)）。

#### 4.3.3 源码精读

首行缩进的完整判定就在 `configuration` 里这一段（[src/inline/mod.rs:197-215](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L197-L215)）：

```rust
first_line_indent: {
    let amount = base.first_line_indent.amount();
    let all = base.first_line_indent.all();
    if !amount.is_zero()
        && match situation {
            // First-line indent for the first paragraph after a list
            // bullet just looks bad.
            Some(ParSituation::First) => all && !in_list(shared),
            Some(ParSituation::Consecutive) => true,
            Some(ParSituation::Other) => all,
            None => false,
        }
        && shared.resolve(AlignElem::alignment).x == dir.start().into()
    {
        amount.at(font_size)
    } else {
        Abs::zero()
    }
},
```

逐句拆解：

1. `!amount.is_zero()`：用户没设首行缩进就直接归零，省事。
2. `match situation`：套用上面的决策表。注意 `First` 分支的 `all && !in_list(shared)`——源码注释点明「列表项后面第一段的首行缩进看起来很丑」，所以即使在 `First` 且 `all` 为真时，如果当前在列表/枚举/术语里，也不缩进。
3. `alignment.x == dir.start()`：只有贴着起始边对齐的文字才缩进。
4. 满足全部条件 → `amount.at(font_size)` 把相对长度（如 `2em`）按字体大小解析成绝对长度 `Abs`；否则 `Abs::zero()`。

`in_list` 的实现（[src/inline/mod.rs:319-323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L319-L323)）检查三种「列表祖先」：

```rust
fn in_list(styles: StyleChain) -> bool {
    styles.get(ListElem::depth).0 > 0
        || !styles.get_cloned(EnumElem::parents).is_empty()
        || styles.get(TermsElem::within)
}
```

即：列表（`ListElem::depth > 0`）、枚举（`EnumElem::parents` 非空）、术语（`TermsElem::within`）三者之一为真，就算「在列表里」。注释也坦言这只是临时方案，「等支持更通用的祖先机制后会更优雅」。

**这条缩进是怎么被消费的**：`configuration` 算出的 `config.first_line_indent` 会进入 `collect`，后者在整段文字**最前面**插一个 `Item::Absolute(first_line_indent)`（[src/inline/collect.rs:135-138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L135-L138)）。因为它是第一行开头的额外间距，自然只影响第一行——这就是首行缩进的物理实现。

#### 4.3.4 代码实践

**实践目标**：搞清楚 `first_line_indent` 在三种 `ParSituation` 下分别何时生效，并理解 `in_list` 的作用。

**操作步骤**：

1. 打开 [src/inline/mod.rs:197-215](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L197-L215)，对照决策表，预测以下四种 `situation` 在「用户设了 `#set par(firstline-indent: 2em)`（此时 `all` 默认为 `false`）」下的首行缩进结果：
   - `First`
   - `Consecutive`
   - `Other`
   - `None`
2. 再预测：如果用户设的是 `#set par(firstline-indent: (amount: 2em, all: true))`（`all = true`），四种情境的结果又如何？
3. 思考：一段位于 `- 列表项` 内部的、`situation = First` 的段落，在 `all = true` 时会不会缩进？依据是 [src/inline/mod.rs:319-323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L319-L323) 的 `in_list`。

**需要观察的现象 / 预期结果**（`amount` 非零、对齐方向满足的前提下）：

| situation | `all=false`（默认） | `all=true` | 列表内且 `all=true` |
|-----------|---------------------|------------|---------------------|
| `First` | 不缩进 | 缩进 | **不缩进**（`in_list` 拦截） |
| `Consecutive` | 缩进 | 缩进 | 缩进 |
| `Other` | 不缩进 | 缩进 | 缩进 |
| `None` | 不缩进 | 不缩进 | 不缩进 |

> 待本地验证：可写一个最小 Typst 文档（几段文字 + 一个列表），分别观察默认与 `all: true` 下的首行缩进效果。但本实践以源码推理为主。

#### 4.3.5 小练习与答案

**练习 1**：为什么「第一段（`First`）默认不缩进，而第二段（`Consecutive`）缩进」是 Typst 的默认行为？源码里是哪个条件实现的？

**答案**：这是排版习惯（文章首段顶格、后续段落缩进）。源码里 `First` 分支要求 `all` 为真才缩进，而 `all` 默认为 `false`（`FirstLineIndent::all` 默认值，见 [crates/typst-library/src/model/par.rs:687-689](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L687-L689)），所以默认 `First` 不缩进；而 `Consecutive` 分支无视 `all` 总是缩进。

**练习 2**：`None` 情境（走 `layout_inline`）下，`hanging_indent` 会生效吗？

**答案**：不会。`configuration` 里 `hanging_indent` 的解析是 `if situation.is_some() { base.hanging_indent } else { Abs::zero() }`（[src/inline/mod.rs:216-220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L216-L220)），非段落行内布局直接归零。这与首行缩进的 `None => false` 一致：缩进是「段落级」语义，非段落行内布局不该有。

## 5. 综合实践

把本讲三个模块串起来，做一个「**追踪一个首行缩进从配置到生效**」的小任务：

1. **起点**：用户在 Typst 里写 `#set par(firstline-indent: 2em);` 加两段文字。这会生成两个 `ParElem`，被 flow 收集器依次用 `situation = First`（第一段）和 `situation = Consecutive`（第二段）调 `layout_par`（参见 [src/flow/collect.rs:163-182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L163-L182)，注意排完第一段后 `par_situation` 被置为 `Consecutive`）。
2. **进入 inline**：`layout_par_impl` realize 后调 `layout_inline_impl`，第一步 `configuration` 根据 `situation` 算出 `config.first_line_indent`：
   - 第一段 `First` + `all=false` → `0pt`（不缩进）。
   - 第二段 `Consecutive` → `2em` 按字体大小解析成绝对长度。
3. **物理实现**：追踪到 `collect`（[src/inline/collect.rs:135-138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L135-L138)），确认 `config.first_line_indent` 被当作整段最前面的一个 `Item::Absolute` 间距插进去。
4. **验证理解**：解释为什么这个插在最前面的间距「只影响第一行」——因为它是行首的额外 advance，断行后只有第一行以它开头。

**产出**：一段文字说明 + 一张标注了 `First`/`Consecutive` 两段各自 `config.first_line_indent` 取值与最终第一行位置的草图。

## 6. 本讲小结

- inline 模块是一条**四段管线** `collect → prepare → linebreak → finalize`，调度核心是私有函数 `layout_inline_impl`（[src/inline/mod.rs:153-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L153-L178)），类型链为 `&[Pair] → (String, Vec<Segment>, SpanMapper) → Preparation → Vec<Line> → Fragment`。
- **段落入口 `layout_par` 被 memoize，非段落入口 `layout_inline` 不被 memoize**；两者唯一差别是 `ParSituation` 传 `Some` 还是 `None`，以及是否先 realize。
- `Config` 是在第 0 步 `configuration` 一次性算出的**贯穿全程的共享配置**；它含直接搬运值、带默认推导值（如 `linebreaks` 随 `justify` 推导）、情境相关值（缩进）和「全段统一才生效」值（`hyphenate`/`lang` 经 `shared_get`）。
- `ParSituation`（`First`/`Consecutive`/`Other`/`None`）由 flow 收集器维护，决定首行缩进是否生效：默认只缩进 `Consecutive`；`all=true` 时除 `First` 外都缩进；`First` 且在列表内时即使 `all=true` 也不缩进（`in_list`）；`None`（非段落）一律不缩进。
- 首行缩进的**物理实现**是 collect 在整段最前面插一个 `Item::Absolute` 间距；悬挂缩进则会缩窄 linebreak 的可用宽度并在 finalize 时加回 frame 宽度。
- 段落排版**只吃一个 `Size`**（不是多区域 `Regions`），跨区域断裂是 flow 的职责；inline 只负责「在给定宽度内把文字排成若干行」。

## 7. 下一步学习建议

本讲只搭了「骨架」。后续讲义会逐一展开四段管线的内部：

- **u5-l2 文本收集与 BiDi 准备**：深入 `collect.rs`（`Item`/`Segment` 如何把 `#box`、智能引号、行内盒映射成字符串占位与 segment）和 `prepare.rs`（BiDi 如何切分 level、`Preparation.items` 如何组织）。
- **u5-l3 换行算法 linebreak**：`linebreak.rs` 的 simple vs optimized 两种策略、cost/badness/penalty、连字符与 CJK 断行。
- **u5-l4 文本整形 shaping**：`shaping.rs` 用 rustybuzz 整形、字体回退、shy 软连字符。
- **u5-l5 行构建与装饰**：`line.rs`（单行 `commit` 成 frame）、`deco.rs`（下划线/删除线 evade）、`finalize.rs`（行号 marker 挂载）。

建议先读 u5-l2，因为它直接展开本讲的 collect/prepare 两步；之后再按 u5-l3 → u5-l4 → u5-l5 的顺序补全线断行、整形与行装配细节。
