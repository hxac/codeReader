# 行内排版：inline 与文本整形

## 1. 本讲目标

上一讲我们看清了块级世界：flow 如何把子内容分配进一页页 region。本讲钻进页面里最细的一层——**一个段落内部发生了什么**。学完本讲，你应该能够：

1. 说出一个段落从「realize 产出的元素列表」到「多行 Frame」经过的四个阶段：collect → prepare → linebreak → finalize。
2. 解释行内世界的统一货币 `Item`：文本、空格、盒子、内联公式、标签如何被拍平到同一条字符串上。
3. 描述字体选择发生在哪一层：`FontBook::select` / `select_fallback` 如何配合 `families()` 迭代器与 rustybuzz 完成整形（shaping），以及 tofu（缺字方块）回退链的尽头是什么。
4. 对比西文与 CJK（中日文）在整形与断行两条路径上的差异：fallback 字体切换、CJ 专用断行器、CJK-拉丁间距、行首行尾标点压缩。

## 2. 前置知识

- **整形（shaping）**：把「字符序列 + 字体」变成「字形序列」的过程。`f` + `i` 在合适字体里会合成一个 `ﬁ` 连字字形；一个汉字对应一个字形；阿拉伯文里字母形态还取决于邻居。Typst 用 rustybuzz（HarfBuzz 的 Rust 移植）做这件事。整形的产物是**字形（glyph）**，每个字形带 `glyph_id`、步进宽度（advance）和偏移。
- **簇（cluster）**：整形时若干字符可能合成一个不可拆分的字形组，rustybuzz 用 cluster 索引标记每个字形源自原文哪一段字节。这是「断行能否在此处切分」的判断依据之一。
- **UAX #14**：Unicode 的断行算法标准，回答「任意两个字符之间是否允许换行」。Typst 不手写这套规则，而是调用 ICU4X 的 `LineSegmenter`。
- **BiDi（双向文本）**：同一行里混排从左向右（如英文）与从右向左（如阿拉伯文、希伯来文）的文字时，需要按 Unicode 双向算法算出每段文字的「嵌入级别」，再重排出视觉顺序。Typst 用 `unicode_bidi` crate。
- **Knuth-Plass 断行**：TeX 采用的全局最优断行算法，核心思想是给每种断法算「成本」（行太松/太紧、行尾连字符都要罚分），用动态规划找总成本最低的方案。Typst 的 `Linebreaks::Optimized` 就是它。
- 承接上一讲：flow 在 collect 阶段遇到段落时会**提前**调用行内布局，因为段落断行不依赖具体 region 高度——这一点会在 4.1 解释。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-layout/src/inline/mod.rs` | 行内布局入口 `layout_par` / `layout_inline`，四阶段流水线的指挥台，全局 `Config` |
| `crates/typst-layout/src/inline/collect.rs` | 把 realize 产物收集成一条全文 String + `Segment`/`Item` 序列 + `SpanMapper` |
| `crates/typst-layout/src/inline/prepare.rs` | BiDi 分析，按段整形生成 `Preparation`，CJK-拉丁间距注入 |
| `crates/typst-layout/src/inline/shaping.rs` | 整形核心：字体选择、rustybuzz 调用、tofu 回退、`ShapedText`/`ShapedGlyph` |
| `crates/typst-layout/src/inline/linebreak.rs` | 断行候选枚举（UAX #14 + 连字典）与 Simple/Optimized 两种断行算法 |
| `crates/typst-layout/src/inline/line.rs` | 单行的构建、BiDi 重排、CJK 微调、`commit` 生成 Frame |
| `crates/typst-layout/src/inline/finalize.rs` | 决定最终宽度，把每行 commit 成 Frame，汇成 Fragment |
| `crates/typst-library/src/text/mod.rs` | `families()`、`variant()`、`features()`——从样式链解析字体信息 |
| `crates/typst-library/src/text/font/book.rs` | `FontBook`：字体元数据索引与 `select`/`select_fallback` 查询 |
| `crates/typst-library/src/layout/container.rs` | `InlineElem`/`InlineItem`：数学公式等「非文本行内物」的接入协议 |
| `crates/typst-layout/src/flow/collect.rs` | flow 侧的调用点：为什么段落在 collect 阶段就被断行 |

## 4. 核心概念与源码讲解

### 4.1 行内排版的入口与四阶段流水线

#### 4.1.1 概念说明

一个 `ParElem`（或 flow 里的一段连续行内内容）交给行内引擎时，手里只有 realize 产出的 `Vec<Pair>`（元素 + 生效样式链，见 u4-l2）。行内引擎要把它变成若干个一行高的 `Frame`。整个过程是一条固定的四阶段流水线：

1. **collect（收集）**：把所有孩子拍平成**一条字符串** `full text`，同时记录每个片段对应的样式或已布局产物。为什么是一条字符串？因为 BiDi 分析和 UAX #14 断行都是**全文级**的 Unicode 算法，必须一次看到整段文字。
2. **prepare（准备）**：对全文跑 BiDi 分析；把文本段按方向/文字系统切成 run 逐个整形；建立字节偏移 → 项索引的映射。
3. **linebreak（断行）**：枚举所有断行候选点，用贪心或 Knuth-Plass 算法选出实际断点，得到 `Vec<Line>`。此阶段只算宽度、**不真正生成 Frame**——因为优化算法需要反复试探上百种断法，每次都建帧代价太高。
4. **finalize（提交）**：对选中的每一行执行 `commit`，真正生成 Frame，按 region 汇成 `Fragment`。

#### 4.1.2 核心流程

```text
Packed<ParElem>
   │  realize(RealizationKind::Par)          ← u4-l2 讲过的实现化
   ▼
Vec<Pair>（元素 + 样式链）
   │  collect()   → (String 全文, Vec<Segment>, SpanMapper)
   │  prepare()   → Preparation { bidi, items: Vec<(Range, Item)>, ... }
   │  linebreak() → Vec<Line>（只量宽，不建帧）
   ▼
finalize() → commit(每行) → Vec<Frame> = Fragment
```

另一个关键点：**段落在 flow 的 collect 阶段就被断行**，而不是 distribute 阶段。源码注释直说原因——断行只依赖行宽（region.x），与还剩多少高度无关。

#### 4.1.3 源码精读

flow 侧的调用点，[crates/typst-layout/src/flow/collect.rs:L160-L176](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/collect.rs#L160-L176) 说明 `par()` 在收集子内容时就调 `crate::inline::layout_par` 把段落变成一行行 `LineChild`——「这已经完成了行布局，因为它不依赖具体的 regions」。

行内布局入口 [crates/typst-layout/src/inline/mod.rs:L41-L64](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/mod.rs#L41-L64) 定义 `layout_par`：它只是把 `Engine` 拆解成被 comemo 追踪的各个句柄，转给真正干活的 `layout_par_impl`。内部实现先对段落 body 执行一次 realize（`RealizationKind::Par`，见 [mod.rs:L96-L103](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/mod.rs#L96-L103)），再进入共用主干。

四阶段流水线本体在 [crates/typst-layout/src/inline/mod.rs:L150-L175](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/mod.rs#L150-L175) 的 `layout_inline_impl`：`collect` → `prepare` → `linebreak`（注意宽度传的是 `region.x - hanging_indent`，悬挂缩进不参与断行）→ `finalize`，四行代码正好对应上面四个阶段。

`layout_par` 与 `layout_inline` 的区别在 [mod.rs:L123-L146](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/mod.rs#L123-L146)：后者服务「不是语义段落」的行内内容（如数学公式内部的文本，见 `crates/typst-layout/src/math/text.rs:31`），因此不传 `ParSituation`（首行缩进等段落语义失效）。

全局配置 `Config` 在 [mod.rs:L178-L239](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/mod.rs#L178-L239) 一次性解析完毕，字段覆盖 justify、linebreaks、首行/悬挂缩进、行号标记、方向、hyphenate、lang、fallback、cjk_latin_spacing 与 costs（见 [mod.rs:L266-L297](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/mod.rs#L266-L297) 的结构定义）。两个细节值得注意：

- [mod.rs:L191-L193](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/mod.rs#L191-L193)：`linebreaks` 未显式设置时，`justify: true` 默认走 `Optimized`（Knuth-Plass），否则走 `Simple`。
- [mod.rs:L300-L310](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/mod.rs#L300-L310) 的 `shared_get`：hyphenate、lang 这类设置只有当**所有孩子样式一致**时才取 `Some(_)`——段落里混了两种语言时，语言相关的全局优化只能放弃，退化为逐段判断。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到四阶段的执行顺序与耗时。
2. **操作步骤**：
   - 写一个多段落的 `par.typ`（如三个段落，每段三四行文字）。
   - 用 `cargo run -p typst-cli -- compile par.typ --timings tracing.json` 编译，打开生成的 JSON（Chrome Tracing 格式），搜索 `collect`、`prepare`、`linebreak`、`finalize` 事件名。
3. **需要观察的现象**：四个阶段各有一条 trace 记录，且顺序固定；`linebreak` 的事件数量可能与 `collect` 不同（一行事件对应一次 `linebreak` 调用）。
4. **预期结果**：能从 tracing JSON 中按时间戳排出 collect → prepare → linebreak → finalize 的顺序。具体耗时数值**待本地验证**（取决于机器与文档规模）。

#### 4.1.5 小练习与答案

**练习 1**：为什么断行阶段不直接生成 Frame，而是先产出「只有宽度信息的 Line」？

**答案**：`Linebreaks::Optimized`（Knuth-Plass）是动态规划算法，需要对同一段文字反复试探几十上百种断法来比较成本。`Line` 只借用 `Preparation` 里已整形的项并求和宽度（见 line.rs 的 `Line::width`），几乎零成本；而 `commit` 建帧要处理 justify、BiDi 重排、装饰等重活。把「量宽」和「建帧」分离，试探才付得起代价。

**练习 2**：`layout_par` 与 `layout_inline` 有什么区别？

**答案**：`layout_par` 接 `Packed<ParElem>`，自己先做一次 `RealizationKind::Par` 的 realize，并携带 `ParSituation`（首行缩进、悬挂缩进等段落几何）；`layout_inline` 接收已 realize 好的 children，`par` 参数传 `None`，用于数学公式内部等非语义段落的行内排版。

### 4.2 行内项的统一表示：collect、Item 与 Segment

#### 4.2.1 概念说明

段落里的成员五花八门：普通文字、空格、`#h(1em)` 水平间距、软换行 `\`、智能引号、`#box` 盒子、内联数学公式、内省标签……行内引擎的第一刀，是把它们**全部拍平到两个统一的层面**：

- **文本层面**：一条 `String`。文字片段直接拼进去；非文字的东西用替身字符占位——间距变成空格（`SPACING_REPLACE`），盒子/公式变成 `U+FFFC`（OBJECT REPLACEMENT CHARACTER，`OBJ_REPLACE`）。这样 BiDi 与 UAX #14 拿到的是一段「看起来全是字符」的全文。
- **项层面**：`Vec<Segment>`，每个 Segment 要么是「一段尚未整形的文本 + 样式链」（`Segment::Text`），要么是「已经准备好的项」（`Segment::Item`）。文本之所以此时**不**整形，是因为 BiDi 级别和文字系统（script）都依赖全文，整形 run 必须等分析完在级别边界处切开。

`Item` 就是行内世界的统一货币，七种变体覆盖了段内所有可能出现的角色。

#### 4.2.2 核心流程

collect 主循环对孩子列表逐个分发：

```text
SpaceElem   → push_text(" ")
TextElem    → 大小写变换(case) + 方向不符时包裹 BiDi 嵌入控制符
HElem       → Rel → Item::Absolute(weak 标记)；Fr → Item::Fractional
LinebreakElem → "\n"（或 justify 时的 U+2028 行分隔符）
SmartQuoteElem → SmartQuoter 依前文字符判定开/闭引号
InlineElem  → 包 LTR ISOLATE，调其回调产出 Space/Frame 项
BoxElem     → 宽度为 Fr → Fractional(带元素)；否则立即 layout_box 得 Frame
TagElem     → Item::Tag（内省标签，零宽）
其它        → 警告「不应出现在段落中」并忽略
```

收集器还有两条合并规则：相邻**同样式**的文本段合并成一个 Segment；相邻**弱间距**合并取最大值（`max`），这就是连续多个弱 `#h` 只留一个的原因。

#### 4.2.3 源码精读

替身字符与 BiDi 控制符常量在 [crates/typst-layout/src/inline/collect.rs:L18-L28](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L18-L28)：间距替换为 `" "`，对象替换为 `\u{FFFC}`；下一段是四个 Unicode 方向控制符（LTR/RTL EMBEDDING 与 POP）和 ISOLATE，用于把局部方向变化告诉 BiDi 算法。

`Item` 枚举定义在 [collect.rs:L31-L49](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L31-L49)：`Text(ShapedText)`、`Absolute(Abs, bool)`（bool 标记弱间距）、`Indent(Abs)`（首行/悬挂缩进产生的几何间距）、`Fractional(Fr, Option<..BoxElem..>)`、`Frame(Frame)`（已布局的盒子或公式）、`Tag(&Tag)`（内省标签）、`Skip(&'static str)`（不可见但需占用文本位的控制字符）。每个 Item 都知道自己在全文里的**文本替身**（[collect.rs:L82-L92](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L82-L92) 的 `textual()`）和**自然宽度**（[collect.rs:L100-L108](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L100-L108)）。

「先收集、后整形」的原因写在 `Segment` 的文档注释里，[collect.rs:L111-L121](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L111-L121)：`Segment::Text(usize, StyleChain)` 记录该段在全文中的字节长度。

分发主循环是 [collect.rs:L137-L263](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L137-L263) 的 `collect()`。几个有代表性的分支：

- 文本分支 [collect.rs:L162-L184](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L162-L184)：局部文字方向与段落方向不一致时，在文本两侧塞入 LTR/RTL EMBEDDING 和 POP——`text.dir: rtl` 设置的实现原理就在这两行。
- 智能引号分支 [collect.rs:L202-L216](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L202-L216)：`SmartQuoter` 回看全文里最后一个「非默认不可见」字符来决定用开引号还是闭引号——注意它看的是**全文**，这也是为什么文本必须先汇成一条字符串。
- 内联容器分支 [collect.rs:L218-L234](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L218-L234)：`InlineElem` 用 `\u{2066}` LTR ISOLATE 包住，先调 `elem.layout(...)` 拿到 `Vec<InlineItem>`，Space 转成绝对间距项、Frame 项还要过一遍 `apply_shift`（基线对齐）。
- 盒子分支 [collect.rs:L235-L245](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L235-L245)：`#box(width: 2fr)` 这样的分数宽度盒子**此刻不布局**，把元素本体挂进 `Item::Fractional`，等 commit 阶段按行内剩余空间分配后再布局。

`InlineElem`/`InlineItem` 的协议定义在 typst-library 侧，[crates/typst-library/src/layout/container.rs:L164-L218](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/container.rs#L164-L218)：`InlineElem::layouter` 把任意「捕获的元素 + 布局回调」打包成一个行内元素，回调签名返回 `Vec<InlineItem>`（Space 或 Frame）。内联数学公式正是通过它接入段落——见 [crates/typst-layout/src/rules.rs:L823](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/rules.rs#L823)，realize 规则把方程元素包成 `InlineElem::layouter(elem, layout_equation_inline)`。这就是「数学公式、盒子、图片在段落里如何统一表示」的答案：**它们都是回调产出 Frame 的 `InlineElem`，最终都变成 `Item::Frame`，在全文里以 `\u{FFFC}` 占位**。

两条合并规则在 [collect.rs:L281-L319](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L281-L319)：`build_text` 合并相邻同样式文本段（[L293-L299](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L293-L299)），`push_item` 对相邻弱间距取 `max`（[L305-L312](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L305-L312)）。

最后，[collect.rs:L323-L350](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/collect.rs#L323-L350) 的 `SpanMapper` 把全文的字节偏移映射回源码 `Span`：commit 建帧时每个字形都要用它反查出处，PDF 文本提取、点击跳转都依赖这一层。

#### 4.2.4 代码实践

1. **实践目标**：验证「替身字符」与合并规则。
2. **操作步骤**：
   - 写 `items.typ`：`One #h(6pt) #h(3pt) two #box[box] three $x^2$ four.`，页面设窄（`#set page(width: 21cm, margin: 2cm)` 默认即可）。
   - 阅读型实践：对照 collect.rs 的分发表，**手工预测**每个孩子会变成哪种 `Item`、全文 String 长什么样（提示：`#h(6pt) #h(3pt)` 两个弱间距合并为 6pt 一项；`box` 和公式各占一个 `\u{FFFC}`）。
   - 可选动手版：在本地分支的 `collect()` 返回前临时加一段 `eprintln!`（示例代码）：
     ```rust
     // 示例代码：临时调试打点，验证后还原
     for (i, seg) in collector.segments.iter().enumerate() {
         eprintln!("segment {i}: {:?}", seg);
     }
     ```
     重新 `cargo build -p typst-cli` 后编译 `items.typ` 观察 stderr。
3. **需要观察的现象**：segments 数量明显少于孩子数量（文本段被合并、弱间距被合并）；`Item::Frame` 的 textual 长度是 3（`\u{FFFC}` 是 3 字节 UTF-8）。
4. **预期结果**：手工预测表与（可选的）日志输出一致；若执行了打点，**验证后务必还原源码**。

#### 4.2.5 小练习与答案

**练习 1**：为什么间距用空格、盒子用 `\u{FFFC}` 作替身，而不是统一用同一个字符？

**答案**：UAX #14 对不同字符类别给出不同断行机会。空格是天然的断行点；`\u{FFFC}`（Object Replacement）默认在前后提供 Contingent Break 机会（源码在 linebreak.rs 还有专门特判，见 4.3.3）；两者语义不同——间距处「可以断」，对象处「通常也可以断但要看邻居」。如果都用空格，BiDi 与断行算法就无法区分「这里有个可断的空隙」和「这里有个不可拆的盒子」。

**练习 2**：`#box(width: 1fr)` 和 `#h(1fr)` 都产生 `Item::Fractional`，区别在哪？

**答案**：`Item::Fractional(Fr, Option<(elem, loc, styles)>)` 的第二个字段：`#h(1fr)` 是纯间距，为 `None`，commit 时只把剩余空间按 Fr 份额摊给它；`#box(width: 1fr)` 带**元素本体**，commit 时先算出分得的空间 `amount`，再以此为区域调用 `layout_box` 真正布局盒子内容（见 line.rs 的 commit，4.5.3）。

### 4.3 字体查询与整形：FontBook、rustybuzz 与 tofu 回退

#### 4.3.1 概念说明

整形是行内排版中最「黑盒」的一步，但 Typst 的组织方式很清晰，分三层：

1. **字体从哪来**：`World` trait 的 `book()` 返回 `FontBook`（全部字体的**元数据索引**，不含字体数据本体），`font(index)` 按索引取真正的 `Font`。查询接口只有两个：`select`（按家族名 + 变体找最接近的）和 `select_fallback`（按「谁覆盖了这个字符」找回退字体）。
2. **选谁**：`families(styles)` 把样式链里的 `text.font` 列表（默认 `("libertinus serif",)`）与内置 emoji 回退家族串成一个迭代器；整形器按顺序逐个尝试，直到某个家族的字体能覆盖当前文本。
3. **怎么整**：对每个「BiDi 级别 + 文字系统」连续的 run，用 rustybuzz 整形。当前字体不覆盖的字符（glyph_id 为 0，即 tofu「豆腐块」）会被切出来，**递归地**用下一个家族重新整形——这就是字体回退（font fallback）的实现本质。

一个关键性能设计：整形结果 `ShapedText` 在 prepare 阶段**一次性算好并存起来**，断行试探时反复借用；只有当断点恰好落在 rustybuzz 标记为「不安全可断」（unsafe-to-break，通常是连字/合字内部）的位置时，才对该小片重新整形（`reshape`）。

#### 4.3.2 核心流程

```text
Segment::Text(len, styles)
  │ shape_range：按 (BiDi level, script) 切 run
  ▼ 每个run
shape(ctx, text, styles, dir, lang, region)
  │ 组装 ShapingContext{variant, features, variations, fallback, dir}
  ▼
shape_segment(ctx, text, families 迭代器)
  ├─ get_font_and_covers：
  │    逐家族 book.select(family, variant) → 命中即用
  │    全部落空且 fallback 开启 → book.select_fallback(like, variant, text)
  │    仍无 → 用已用字体 shape_tofus（glyph_id=0）
  ├─ UnicodeBuffer 填文本/语言/方向 → create_shape_plan(memoize)
  ├─ rustybuzz::shape_with_plan → glyph_infos / glyph_positions
  └─ 遍历结果：
       glyph_id≠0 且被 covers 覆盖 → 生成 ShapedGlyph(cluster区间, advance, safe_to_break…)
       否则 → 找出 tofu 序列边界 → 递归 shape_segment(下一字体)
```

#### 4.3.3 源码精读

**字体查询层**。`World` 的契约在 [crates/typst-library/src/lib.rs:L63-L91](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L63-L91)：`book()` 返回 `LazyHash<FontBook>`（可增量缓存），`font(index)` 按需加载。`FontBook` 本体在 [crates/typst-library/src/text/font/book.rs:L13-L18](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/font/book.rs#L13-L18)：一张「小写家族名 → 字体索引列表」的 BTreeMap 加一份 `FontInfo` 元数据数组——**查询永远不碰字体数据本体**，这就是 CLI 启动时能瞬间列出几百个字体的原因。

两个查询接口：

- [book.rs:L75-L78](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/font/book.rs#L75-L78) `select(family, variant)`：在家族的所有字形面（face）里找与目标变体（style/weight/stretch）最接近的，评分逻辑在 [L139-L163](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/font/book.rs#L139-L163) `find_best_variant`。
- [book.rs:L94-L115](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/font/book.rs#L94-L115) `select_fallback(like, variant, text)`：取出文本**第一个非空白、非默认不可见字符**，过滤出 coverage 包含它的所有字体，再从中挑与原字体最相似的（等宽与否、衬线与否、家族名共享前缀词数，见 [L168-L185](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/font/book.rs#L168-L185) 的 `similarity`）。**中西文混排时汉字走的就是这条路**：首选的衬线体不含汉字，fallback 按「像 libertinus serif + 覆盖‘汉’字」双条件筛出 CJK 字体。

家族列表的解析在 [crates/typst-library/src/text/mod.rs:L1106-L1122](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/mod.rs#L1106-L1122) 的 `families()`：`text.font` 样式值串上内置 emoji 回退家族（`fallback` 开启时）。变体解析在同文件 [L1125-L1146](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/mod.rs#L1125-L1146) 的 `variant()`：`weight` 加上 `delta`（`*` 强调的加粗量），`emph` 翻转斜体——「样式链 → 字体查询参数」的翻译就发生在这里。

**整形层**。整形的顶层分组是 [crates/typst-layout/src/inline/shaping.rs:L723-L774](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L723-L774) 的 `shape_range`：逐字节扫描 run，BiDi 嵌入级别或文字系统（script，如拉丁/汉字/阿拉伯）任一发生变化就切开一段，各段独立整形——**这是「一段中英混排文本会被切成多个整形 run」的直接原因**。

单次整形入口 [shaping.rs:L787-L835](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L787-L835) 的 `shape()`：组装 `ShapingContext`（含 `variant(styles)`、`features(styles)` 解析出的 OpenType 特性），调 `shape_segment`，最后依次做 `apply_spacing`（空格宽度归一）、`calculate_adjustability`（为 justify 预算伸缩空间）、`apply_tracking`。

字体选择与回退的枢纽是 [shaping.rs:L904-L955](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L904-L955) 的 `get_font_and_covers`：先按 `families` 迭动器逐个 `book.select`（[L919-L929](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L919-L929)）；全落空且 `fallback` 开启时走 `book.select_fallback`（[L932-L939](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L932-L939)）；仍无字体时用**已用过的**字体直接生成 tofu 字形（[L941-L947](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L941-L947) 调 `shape_tofus`）——回退链的尽头是「至少画个方块，宽度按该字体的 .notdef（glyph 0）步进」。

真正的 rustybuzz 调用在 [shaping.rs:L958-L1161](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L958-L1161) 的 `shape_segment`：

- [L981-L1000](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L981-L1000) 填 `UnicodeBuffer`：语言、脚本（`text.script` 可覆盖）、方向，并设 `REMOVE_DEFAULT_IGNORABLES` 避免不可见字符产生零宽字形干扰文本提取。
- [L1016-L1029](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L1016-L1029)：先 `create_shape_plan`（在 [L1222-L1237](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L1222-L1237)，标了 `#[comemo::memoize]`——计划只依赖字体/方向/脚本/语言/特性，与文本无关，跨段落缓存），再 `rustybuzz::shape_with_plan`。
- [L1046-L1158](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L1046-L1158) 遍历整形结果：合格字形生成 `ShapedGlyph`，注释里那段 `(glyph_id, cluster)` 序列示例（[L1053-L1064](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L1053-L1064)）解释了如何从 cluster 推导每个字形覆盖的字节区间；遇到 tofu 序列则定位其文本边界，**递归**调用 `shape_segment` 换下一个家族（[L1114-L1154](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L1114-L1154)）。

整形产物 `ShapedText` 与 `ShapedGlyph` 定义在 [shaping.rs:L39-L56](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L39-L56) 与 [L133-L169](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L133-L169)：每个字形带字体实例、glyph_id、步进与偏移（单位 em）、字号、cluster 区间、`safe_to_break`、是否可 justify 等信息。`Glyphs`（[L67-L129](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L67-L129)）是写时复制的字形集合，`kept` 范围支持把行尾空白「从布局里隐去但保留给 PDF 复制」。

重整形捷径在 [shaping.rs:L547-L573](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L547-L573) 的 `reshape`：若切割点在 cluster 边界且 rustybuzz 标记 safe-to-break，直接切片**借用**原字形（零拷贝）；否则才对该片段完整重跑 `shape`。

#### 4.3.4 代码实践

1. **实践目标**：观察中西文混排触发的字体回退，对比纯英文的单一字体路径。
2. **操作步骤**：
   - 准备 `en.typ`：`English only paragraph with some words.`；准备 `mixed.typ`：`这是中英混排 mixed 段落，含有汉字 Han characters。`（两个文件都设 `#set page(width: 16cm)` 保证不断行干扰）。
   - 在本地分支给 `get_font_and_covers` 加临时日志（示例代码）：
     ```rust
     // 示例代码：临时调试打点，验证后还原
     // 放在 get_font_and_covers 里、最后的 Some((font, covers)) 返回之前
     eprintln!("selected {:?} for {:?}", font.info().family, text);
     ```
   - `cargo run -p typst-cli -- compile en.typ` 与 `mixed.typ`，分别截取 stderr 中 shaping 相关行。
3. **需要观察的现象**：
   - `en.typ`：整段一次整形，家族列表只有 `libertinus serif` + emoji 回退，无 tofu 分支。
   - `mixed.typ`：同一段文本出现**多轮** shaping 日志——先是整段（或拉丁子串）用衬线体，随后汉字子串因 glyph_id 为 0 或 coverage 不含被切出，递归整形并触发 `select_fallback` 路径。若运行环境里没有任何覆盖汉字的字体，会看到 `shape_tofus` 分支生效、PDF 里出现「豆腐块」——这本身就是回退链尽头的直接证据。
4. **预期结果**：纯英文 = 一次整形、一个字体实例；混排 = 多个 run、至少两个字体实例（具体选中哪个 CJK 字体**待本地验证**，取决于环境的嵌入/系统字体）。验证后还原打点。

#### 4.3.5 小练习与答案

**练习 1**：`FontBook::select_fallback` 为什么只看文本的**第一个**字符？

**答案**：回退的典型场景是「一小段当前字体不覆盖的文字」（一个汉字、一个 emoji）。用第一个非空白字符去过滤 coverage，能以 O(1) 的代价找到覆盖该序列起点的候选字体；若选出的字体仍不覆盖后续字符，tofu 循环会再次触发回退、用下一个字符重试——正确性由递归保证，查询只需做到「快速给出一个合理候选」。

**练习 2**：`create_shape_plan` 为什么可以 memoize，memoize 的 key 是什么？

**答案**：ShapePlan 只取决于字体、方向、脚本、语言和 OpenType 特性列表——这些对一个文档里的大量文本 run 是重复组合，与具体文本内容无关。函数签名（font, direction, script, language, features）正好就是 key，comemo 据此跨段落复用计划，省去每次整形重建计划的成本。

**练习 3**：断行后某行从 cluster 中间切开，会发生什么？

**答案**：`collect_range` 发现切割点不满足 `safe_to_break`，走 `shaped.reshape(engine, sliced)` 对该小片**重新整形**（line.rs，见 4.5.3），保证连字等合字结果与「独立整形这一小段」一致，避免出现半截连字。

### 4.4 断行：UAX #14 候选、Simple 与 Optimized 两种算法

#### 4.4.1 概念说明

断行分两步：先枚举**所有允许断的位置**（候选集），再从候选集中**选出一组实际断点**（决策）。

候选集来自三处：

1. **UAX #14 断行机会**：ICU4X `LineSegmenter`（LSTM 模型）给出；中文/日文换用特制的 `CJ_SEGMENTER`（把两个弯引号的断行属性从「引号」改为「开/闭标点」，见源码注释，等 ICU4X 升级 Unicode 17 后可合并）。
2. **强制断点**：`\n`（Typst 的 `\` 换行）与文本末尾，类型为 `Breakpoint::Mandatory`。
3. **连字断点**：`hyphenate` 开启时，对**全字母**的词用 hypher 连字词典按音节切分，产生 `Breakpoint::Hyphen(l, r)`（记录连字符前后各有几个字符，供成本模型惩罚「只剩一两个字母」的断法）。

决策有两种：

- **Simple（贪心 first-fit）**：逐候选尝试，一行塞到塞不下为止。快，但行宽可能忽松忽紧。
- **Optimized（Knuth-Plass 动态规划）**：对每个候选断点计算「到此为止的最优方案总成本」，行太松/太紧、行尾连字符都罚分，最后回溯出全局最优。Typst 还先用一遍**近似成本**算出上界，再用上界剪枝跑精确优化。

CJK 与西文在这里的最大差异：**西文的可断点几乎只在空格与连字符处，汉字之间几乎处处可断**；且中文/日文根本不走 hypher 连字（词典只覆盖西方语言），而是走 CJ 断行器。

#### 4.4.2 核心流程

```text
breakpoints(p, f)：
  segmenter = lang ∈ {zh, ja} ? CJ_SEGMENTER : SEGMENTER
  对 segmenter 给出的每个机会 point：
    判定 Normal / Mandatory（查 UAX#14 属性）
    hyphenate 开启且词全为字母 → hypher 按音节生成 Hyphen(l, r)
    URL 特判：UAX#14 处理不好链接，自定义 linebreak_link

linebreak()：按 config.linebreaks 分发
  Simple   → 贪心：每个候选试排一行，塞不下就固化上一条可行行
  Optimized → Knuth-Plass：近似成本求上界 → 带上界的动态规划
```

#### 4.4.3 源码精读

两个断行器与差别的出处是 [crates/typst-layout/src/inline/linebreak.rs:L37-L58](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L37-L58)：`SEGMENTER` 是 ICU4X LSTM；`CJ_SEGMENTER` 从 `typst-assets` 加载定制 blob 并覆盖弯引号的断行属性。语言分流在 [L701-L705](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L701-L705)：`Some(Lang::CHINESE | Lang::JAPANESE) => CJ_SEGMENTER`——注意判定依据是**段落语言**（`config.lang`，所有孩子一致时才有值），不是逐字符判断。

候选类型 `Breakpoint` 在 [linebreak.rs:L64-L74](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L64-L74)。它的 `trim` 方法（[L76-L149](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L76-L149)）体现了一个精巧设计——`Trim` 区分 `layout` 与 `shaping` 两个截止位：普通断点处行尾空白「要整形（保留进 PDF 供复制粘贴）但不参与布局（步进清零）」；强制断点处换行符彻底剪掉。`Item::Text` 的 `Glyphs::trim` 与 4.3 提到的 `kept` 范围正相配合。

分发入口 [linebreak.rs:L152-L161](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L152-L161) 按 `config.linebreaks` 二选一。贪心实现 [L166-L208](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L166-L208) `linebreak_simple`：每到一个候选就 `line(...)` 试排；放不下时固化上一条可行的 `last`，从它的末尾重排当前行——注释明确说「结果行不能再被拆分」。

Knuth-Plass 实现在 [linebreak.rs:L227-L241](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L227-L241)：先 `CostMetrics::compute` 预估各项成本参数，再用近似指标跑一遍得到可行解的成本作为**上界**，最后 `linebreak_optimized_bounded` 带上界做精确动态规划——上界用来剪掉不可能更优的搜索分支。成本常量（连字符罚 135、孤行罚 100 等）与「为何比 Knuth 原文的 50 更大」的注释见 [L23-L35](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L23-L35)。

候选枚举主循环 [linebreak.rs:L692-L793](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L692-L793) `breakpoints()`：URL 特判（UAX #14 对 `://` 后跟数字等场景处理不佳，[L712-L732](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L712-L732)）；用 Unicode 断行属性表 `LINEBREAK_DATA` 判定强制断点（[L744-L777](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L744-L777)），其中还有一个针对 `\u{FFFC}`（就是 4.2 的对象替身字符！）与隔离符相邻时禁止断行的特判，避免「行首只剩一个不可见字符」；连字典路径 [L816-L857](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/linebreak.rs#L816-L857) `hyphenations()`：`hypher::hyphenate(word, lang)` 按音节切词，末音节后、被禁位置（Glue/WordJoiner/ZWJ 前）与局部关闭 hyphenate 的机会都被过滤。

#### 4.4.4 代码实践

1. **实践目标**：对比西文与中文的断点候选序列差异。
2. **操作步骤**：
   - 复用 4.3 的 `en.typ` / `mixed.typ`，但都改成窄页面 `#set page(width: 9cm)` 迫使断行，`mixed.typ` 里加 `#set text(lang: "zh")`。
   - 在本地分支给 `breakpoints()` 的 `f(point, breakpoint)` 调用前加临时日志（示例代码）：
     ```rust
     // 示例代码：临时调试打点，验证后还原
     eprintln!("breakpoint at {point:?} kind {breakpoint:?} after {:?}", &text[..point]);
     ```
   - 分别编译两个文件，对比 stderr 输出。
3. **需要观察的现象**：
   - 英文：断点大多落在空格后；`hyphenate` 默认 auto（justify 关闭时通常不启用）下 `Hyphen` 断点较少或没有；换行符处出现 `Mandatory`。
   - 中文：断点密集得多（相邻汉字间即可断）；不会出现汉字词内的 `Hyphen` 断点；若弯引号出现在中文段落，走的是 CJ 断行器的定制属性。
4. **预期结果**：两份日志的断点密度与类型分布差异显著；英文段每个断点后基本是空格或标点，中文段断点可紧跟任意汉字。具体断点偏移数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么中文/日文要单独一个 segmenter，而不是复用通用的？

**答案**：通用 LSTM segmenter 对 CJ 文本的行为受 Unicode 版本的断行属性影响；Typst 需要针对中文排版的定制行为（注释中举的例子：弯引号 `U+201C/U+201D` 的属性从 QU 改为 OP/CP，避免「行首出现闭引号前的开引号」一类问题）。用 typst-assets 里的定制 blob 可以不等待 ICU4X 升级就修好这些属性。

**练习 2**：`Trim` 为什么要有 `layout` 和 `shaping` 两个字段？

**答案**：行尾空白如果直接从文本里删掉，PDF 文本提取就会丢失词间空格（复制出来单词粘连）；如果保留进布局，行宽又被不可见的空白撑歪。拆成两个截止位：`shaping` 之前的照常整形（字形进 PDF），但 `layout` 之后的字形步进清零、不占宽度——两全其美。

**练习 3**：`justify: true` 时默认断行算法是什么？为什么这样默认？

**答案**：`Optimized`（Knuth-Plass），见 `configuration()` 的 `linebreaks` 兜底逻辑。两端对齐会把行的松紧不均暴露无遗，贪心断行的「忽松忽紧」在 justify 下尤其难看；而 Knuth-Plass 本来就是为 TeX 的两端对齐设计的全局最优算法。

### 4.5 行的构建与提交：BiDi 重排、CJK 微调与 Frame 生成

#### 4.5.1 概念说明

断行算法每试探一个候选，就要「凭空」拼出一行可测量的 `Line`。`Line` 借用 `Preparation` 里已整形的项，只有两端可能被切断的项需要重整形。真正被选中的行最后经 `commit` 变成 Frame。

这一阶段要处理三件「收尾」：

1. **BiDi 重排**：混排行里，RTL 片段的字形序与视觉序相反，要按 `visual_runs` 把每个方向 run 摆到正确的左右位置。
2. **CJK 微调**：中文排版规范（clreq）要求的行首行尾标点压缩、CJK-拉丁间距在行边界的还原。
3. **两端对齐（justification）**：两步拉伸——先按字形的 stretchability/shrinkability 按比例伸缩，不够再对 justifiable 字形（空格、CJK-拉丁边界后的汉字等）平均分配剩余空间；这正是 W3C 中文排版需求里「字间空隙扩展」的多步过程。

#### 4.5.2 核心流程

```text
line(range, breakpoint)：                     构建一行
  判定 justify / dash 种类
  trim → collect_item：
      reorder(BiDi visual runs) → 每个 run 内 collect_range
      collect_range：项被行切断 → reshape；行尾空白 → glyphs.trim
  行首重复连字符（特定语言）/ 行尾软连字符补 "-"
  trim_weak_spacing（行首尾弱间距清除）
  adjust_cj_at_line_start / end（CJK 标点压缩、CJK-拉丁间距还原）
  求和得 Line.width

commit(line, width)：                         生成 Frame
  悬挂缩进偏移 + 悬挂标点（overhang）
  remaining = width - line.width
  两步 justify：ratio（伸缩）→ extra（justifiables 均摊）
  逐项建帧（Fractional 现在才布局盒子；Text 调 shaped.build）
  帧内条目按 LogicalIndex 排序（内省需要逻辑序）
finalize(lines)：每行 commit → Vec<Frame> = Fragment
```

#### 4.5.3 源码精读

`Line` 结构在 [crates/typst-layout/src/inline/line.rs:L32-L42](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L32-L42)：`items`、精确宽度 `width`、是否 justify、行尾 dash 类型。它的 `justifiables()`（[L57-L74](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L57-L74)）统计可均摊空隙的字形数，并扣掉行尾的 CJK 字符——「行尾汉字不做拉伸」的中文排版惯例。

建行主函数 [line.rs:L127-L198](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L127-L198)：判定 justify（行以 U+2028 结尾 = justify 强制换行 `\\`；或全局 justify 且非强制断点，[L138-L139](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L138-L139)）；dash 分 Soft（软连字符断词）/Hard（固有连字符）/Other（en/em dash）三类；随后依次做 trim → 收集 → 连字符处理（[L160-L179](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L160-L179)，行首为某些语言重复上一行的连字符、行尾软连字符补一个整形的 `-`）→ 弱间距清除 → CJ 行首/行尾微调 → 求和宽度。

BiDi 重排在 [line.rs:L252-L283](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L252-L283) 的 `reorder`：`p.bidi` 为 `None`（整段方向一致，prepare 阶段就省掉了 BiDi 信息）时直接整行处理；否则找到包含该行的段落，`bidi.visual_runs` 算出视觉序（从左到右）的 run 列表，逐 run 收集，RTL 的 run 在项层面再 `items.reorder` 反序。

项收集与重整形在 [line.rs:L287-L340](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L287-L340) 的 `collect_range`：非文本项原样保留；文本项与行的交集为 `sliced`，若项被切断（[L310-L327](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L310-L327)）就 `shaped.reshape(engine, sliced)`；`trim.layout < range.end` 时把行尾空白字形 `glyphs.trim` 进不可见区（[L330-L336](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L330-L336)）。CJK-拉丁间距在 prepare 阶段已经以「加宽 x_advance」的方式烙进字形（见 [crates/typst-layout/src/inline/prepare.rs:L126-L175](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/prepare.rs#L126-L175) 的 `add_cjk_latin_spacing`：CJ 后跟拉丁字母/数字加 1/4 em 且可缩 1/8 em；拉丁后跟 CJ 同样加宽并把前一个拉丁字形标记为 justifiable）；当间距恰好落在行边界时，[line.rs:L362-L415](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L362-L415) 的 `adjust_cj_at_line_start/end` 负责还原（行首汉字撤掉左侧 1/4 em、行尾撤掉右侧），同时压缩行首行尾的全宽 CJK 标点（clreq 3.1.6.3）。

提交函数 [line.rs:L487-L636](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L487-L636) `commit`：

- [L495-L526](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L495-L526)：悬挂缩进偏移与**悬挂标点**（overhang：行首 RTL 的行尾标点、行尾 LTR 的行尾标点允许突出到版心外一点，`text.overhang` 控制）。
- [L533-L556](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L533-L556)：两步 justify。行超宽先用 shrinkability 收缩（`justification_ratio` 为负），有 justify 且无 Fr 间距时用 stretchability 拉伸；还有剩余空间则 `extra_justification = remaining / justifiables` 对每个可均摊字形（空格、CJK 边界的汉字）平均加宽——注释明确引用了 W3C 中文排版需求的「字间空隙扩展」多步过程。
- [L563-L607](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L563-L607)：逐项建帧。`Item::Fractional` **此刻**才按 `v.share(fr, remaining)` 算出分得空间并调 `layout_box`（呼应 4.2 的「盒子延迟布局」）；`Item::Text` 调 `shaped.build(engine, &p.spans, ratio, extra)`；`Item::Tag` 生成零尺寸帧承载内省标签。
- [L623-L633](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/line.rs#L623-L633)：关键收尾——帧内条目**按逻辑序排序**而非视觉序。BiDi 重排把摆放顺序变成了视觉序，但内省（counter、query）必须按文档逻辑序遍历元素；`LogicalIndex` 在收集时随项记录，这里 `sort_unstable_by_key` 恢复逻辑序。最后按 `align.position(remaining)` 对齐定位。

字形级建帧在 [shaping.rs:L326-L465](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L326-L465) 的 `ShapedText::build`：按 `(font, y_offset, size)` 分组生成 `TextItem`（一组连续的同字体字形才能进同一个 FrameItem::Text），justify 的份额在这一步摊进每个字形的 `x_advance/x_offset`（[L369-L398](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L369-L398)），Span 反查（[L404-L405](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L404-L405)）也在此时完成。

汇总在 [crates/typst-layout/src/inline/finalize.rs:L8-L35](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/finalize.rs#L8-L35)：决定最终宽度（需 expand 或存在 Fr 间距时取 region.x，否则取最长行的自然宽度与悬挂缩进之和——这就是非撑满段落的「收缩到内容宽」行为），对每行 `commit`，汇成 `Fragment::frames`。这批 Frame 交回 flow，成为 `LineChild`，回到上一讲的 distribute 世界。

#### 4.5.4 代码实践

1. **实践目标**：直观感受 CJK-拉丁间距与两端对齐两步 justify 的效果。
2. **操作步骤**：
   - 写 `justify.typ`：
     ```typst
     #set page(width: 12cm)
     #set par(justify: true)
     中文与 English 混排的段落，观察间距与行尾。
     ```
   - 编译三遍：原样；开头加 `#set text(cjk-latin-spacing: none)`；再加 `#set text(lang: "en")`（改变段落语言，观察断点行为变化导致的换行位置差异）。
   - 用 `cargo run -p typst-cli -- compile justify.typ out%n.png`（或分别输出三个 PNG）对比。
3. **需要观察的现象**：默认时汉字与英文字母间有约 1/4 em 的间隙；关闭后间隙消失；两端对齐下西文空格被拉宽、行尾汉字不加宽。
4. **预期结果**：三张 PNG 的断行位置与空隙肉眼可辨。lang 改为 `"en"` 后，段落不再使用 CJ 断行器，断行位置可能变化（该段文字是否真的变化**待本地验证**，取决于具体文本与宽度）。

#### 4.5.5 小练习与答案

**练习 1**：commit 里为什么最后要按 `LogicalIndex` 重排帧内条目？

**答案**：BiDi 重排后条目的**摆放**顺序是视觉序（左到右），但 Frame 的子项顺序会被 introspection 遍历（counter 编号、query 查询按文档序）。若按视觉序存放，一个 RTL 句子里的标签/元素在逻辑上就「错位」了。所以摆放位置用视觉坐标，条目顺序用逻辑序，两者分离。

**练习 2**：行内布局的输入宽度来自哪里？为什么段落断行「不依赖具体 regions」却仍与页面宽度有关？

**答案**：宽度是 flow 在 collect 阶段传入的 `region.x`（列宽减边距）。断行只依赖**宽度**这一个维度，与「当前页还剩多少高度」无关——高度协商是 flow/distribute 的事。所以段落可以在 flow 的 collect 阶段提前断行成一行行 `LineChild`，之后再参与页面分配。

**练习 3**：`finalize` 里什么时候用「最长行的自然宽度」而不是撑满 region.x？

**答案**：当 `expand` 为 false（容器不强制撑满，比如自然宽度的 block 内段落）且行内没有任何 Fr 间距时（[finalize.rs:L18-L27](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/finalize.rs#L18-L27)）。这解释了 `#box[短文]` 的盒子宽度收缩到内容而非占满外层容器。

## 5. 综合实践

**任务**：把 4.3、4.4 的打点整合起来，对同一段文字做「纯英文 vs 中英混排」的全链路对比，产出一张差异表。

1. 准备两个源文件（同样的窄页面，保证多行）：

   ```typst
   // en.typ
   #set page(width: 8cm)
   Typography is the art and craft of arranging type to make
   written language readable and beautiful, with hyphenation.
   ```

   ```typst
   // mixed.typ
   #set page(width: 8cm)
   #set text(lang: "zh")
   排版 Typography 是将文字与图像进行组织的艺术与工艺，
   涉及断行、对齐与间距，混合 English 词与汉字。
   ```

2. 在本地分支加三处临时日志（均为**示例代码**，验证后还原）：
   - `collect.rs` 的 `collect()` 返回前：打印 segments 数量与每个 segment 的种类。
   - `shaping.rs` 的 `get_font_and_covers` 命中字体后：打印 `family`（或 fallback 路径标记）与文本片段。
   - `linebreak.rs` 的 `breakpoints()` 内 `f(point, breakpoint)` 前：打印断点偏移与类型。
3. `cargo build -p typst-cli`，分别编译两个文件，把 stderr 存成 `en.log` 与 `mixed.log`。
4. 整理成对比表（列：整形容 run 数、用到的字体家族、断点密度、Hyphen 断点数、Mandatory 断点数、CJ 特有调整是否出现）。
5. **预期结果**（方向性，具体数值**待本地验证**）：
   - en：1 个字体家族贯穿全段；断点基本在空格后；hyphenate 未开（未 justify）时无 Hyphen 断点。
   - mixed：至少 2 个字体家族（衬线体 + 某个 CJK 字体，或环境无 CJK 字体时 tofu 分支）；断点可出现在相邻汉字间；无汉字内 Hyphen 断点；prepare 后字形带 1/4 em 的 CJK-拉丁间距（可再加一条 `x_advance` 日志验证）。
   - 对比结论应能回答：**整形路径差异 = 汉字触发 coverage 失败 → `select_fallback` 递归回退 → 独立整形 run；断行路径差异 = `lang: zh` 切换到 CJ 断行器 + 无连字典参与。**
6. 完成后 `git checkout -- crates/` 还原全部打点，确认 `git status` 干净。

## 6. 本讲小结

- 行内排版是固定四阶段流水线：`collect`（拍平成一条全文 String + Segment/Item 序列）→ `prepare`（BiDi 分析 + 按 run 整形 + 字节→项索引）→ `linebreak`（枚举候选、贪心或 Knuth-Plass 选点，只量宽不建帧）→ `finalize`（逐行 commit 成 Frame）。
- 段落里的一切都是 `Item`：文本、绝对/分数间距、缩进、已布局的盒子与公式（`InlineElem` 回调产出 Frame，以 `\u{FFFC}` 占位）、内省标签、不可见控制符；`SpanMapper` 维持字节偏移到源码 Span 的反查。
- 字体查询只有两个入口：`FontBook::select`（家族 + 变体）与 `select_fallback`（按首字符 coverage + 相似度）；整形用 rustybuzz，ShapePlan 跨文本 memoize；tofu 序列触发**递归** `shape_segment` 完成字体回退，回退链尽头是 `.notdef` 方块。
- 断行候选来自 ICU4X 断行器（中文/日文切换到定制 CJ 断行器）+ 强制断点 + hypher 连字词典；`Trim` 的 layout/shaping 双截止位让行尾空白「可复制但不占宽」。
- 西文 vs CJK 的两大路径差异：**整形**上汉字必然触发 fallback 与多 run；**断行**上汉字处处可断、无连字参与，外加 CJK-拉丁间距（1/4 em）、行首尾标点压缩、行尾汉字不拉伸等 clreq 微调。
- commit 是收尾集大成：悬挂缩进/标点、两步 justify（先比例伸缩后均摊）、Fractional 项此刻才布局、帧内条目按逻辑序（非视觉序）排列以服务内省。

## 7. 下一步学习建议

- 下一讲（u4-l6）转向网格世界：`grid.rs` 的轨道协商与 `math.rs` 数学排版——后者会反过来以 `InlineElem` 客人的身份重访本讲的行内机制。
- 想深挖字体层：读 `crates/typst-library/src/text/font/` 下的 `FontInfo`、coverage 位图与 `FontInstance` 实例化（可变字体轴），理解 `select_fallback` 评分的原料。
- 想深挖断行：对照 Knuth & Plass 1981 年原文《Breaking Paragraphs into Lines》读 `linebreak_optimized_bounded` 的动态规划表与 `CostMetrics`，理解 Typst 为何调高连字罚分。
- 观察本讲产物如何被消费：跳到 u5-l1（PDF 导出）看 `TextItem`/`Glyph` 如何被 krilla 写进 PDF 内容流；或到 u5-l2 看渲染器如何把同样的 Frame 栅格化。
