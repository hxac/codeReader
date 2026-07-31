# 换行算法 linebreak

## 1. 本讲目标

本讲精读 `typst-layout` 段落排版四段管线（collect → prepare → **linebreak** → finalize）的第三段——**断行**。读完本讲你应当能够：

- 说清楚 `Simple`（first-fit 贪心）与 `Optimized`（Knuth-Plass 风格动态规划）两种断行策略的差异、取舍与默认选择时机。
- 理解 `Breakpoint` 的三种变体（`Normal` / `Mandatory` / `Hyphen(l, r)`），以及它如何决定行尾的裁剪（`trim`）。
- 看懂断点生成器 `breakpoints`：它如何用 ICU 的行分割器（LSTM 模型）枚举 UAX #14 断点、用 `hypher` 做词内连字符断点、对中文/日文走专用的 `CJ_SEGMENTER`、并对 URL 做专门处理。
- 推导一行的「成本」：`raw_ratio`（拉伸/压缩比 → badness）与 `raw_cost`（badness + penalty），并理解 `DEFAULT_HYPH_COST`、`DEFAULT_RUNT_COST`、`MIN_RATIO` 等常量的含义。
- 解释**为什么 Typst 把 hyphen cost 设为 135（远高于 Knuth-Plass 论文里的 50）**，而不是直接照搬论文。

## 2. 前置知识

本讲承接 [u5-l2](u5-l2-collect-bidi.md)。在进入 `linebreak` 之前，请确认你已理解：

- **四段管线**：`collect` 把异构 children 拍平成 `(String, Vec<Segment>, SpanMapper)`，`prepare` 跑 BiDi 并整形，得到 [`Preparation`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L14-L30)（预整形、带字节轴的 `items`/`indices`）。`linebreak` 的输入正是这份 `Preparation`，输出是 `Vec<Line>`（只含测量信息，不含最终帧），最后由 `finalize` 调 `commit` 渲染。
- **测量与渲染解耦**：断行阶段只调用 `line()` 构造候选行并读取其 `width`/`stretchability`/`shrinkability`/`justify`/`dash` 等数值，不真正画帧。这让我们能在动态规划里反复试不同断点组合而代价可控。
- **Knuth-Plass 基本直觉**：给每一行打一个「成本」，整个段落的成本是各行成本之和，目标是找总成本最小的断行方案。论文里用 box/glue/penalty 三类节点建模；Typst **没有 glue 概念**，而是把可拉伸性放在字形（glyph）上，这点会在第 4.4 节详细对比。

行内公式用 \( ... \)，独立公式用 \[ ... \]。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/inline/linebreak.rs` | **本讲主角**。断行调度、`Breakpoint`/`Trim`、`breakpoints` 断点枚举、`raw_ratio`/`raw_cost` 成本、`CostMetrics`、近似/精确两阶段 Knuth-Plass 动态规划、`Estimates`/`CumulativeVec` 累积数组。 |
| `src/inline/line.rs` | 定义 [`Line`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L31-L41)（一行的测量结果）、[`Dash`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L115-L123) 与 [`line()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L126-L194)（按区间构造/重整形一行）。断行器靠它来测量每个候选行。 |
| `src/inline/mod.rs` | 管线入口 `layout_inline_impl`，在 [第 174 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L174) 调用 `linebreak`；`configuration` 决定 `linebreaks` 取 `Simple` 还是 `Optimized`。 |
| `src/inline/prepare.rs` | `Preparation` 与 `prepare`，提供断行所需的预整形文本与 item 表。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. 两种断行策略总览（Simple vs Optimized）
2. `Breakpoint` 与行尾裁剪 `trim`
3. 断点生成 `breakpoints`（ICU segmenter、连字符、CJK）
4. 行的成本（`raw_ratio` / `raw_cost` / `CostMetrics`）
5. Optimized 的两阶段动态规划（近似上界 + bounded 精确搜索）

### 4.1 两种断行策略：Simple 与 Optimized

#### 4.1.1 概念说明

断行器的入口 [`linebreak`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L152-L161) 只做一件事：根据 `config.linebreaks` 把任务派发给两种实现。

- **Simple（first-fit 贪心）**：从左到右扫描可断点，尽量把一行塞满；一旦当前候选放不下，就用上一个「还能放下」的断点收尾，开新行。优点是快、实现简单；缺点是只看局部，可能产生左右参差不齐的行（很满的一行挨着很空的一行）。
- **Optimized（Knuth-Plass 风格）**：用动态规划在「所有可能断点组合」里找总成本最小的方案，能为了改善后续行而主动把某一行收短。代价是慢得多，但排版更均衡美观。

默认值由 `configuration` 推导：**开启两端对齐（justify）时默认 `Optimized`，否则 `Simple`**（见下文源码）。用户也能用 `set par(linebreaks: ...)` 强制指定。

#### 4.1.2 核心流程

两种实现共享两个底层工具：

- `breakpoints(p, |end, bp| ...)`：一个闭包式枚举器，依次吐出「文本里所有合法断点」及其类型。Simple 和 Optimized 都靠它遍历候选断点。
- `line(engine, p, start..end, bp, pred)`：按区间 `[start, end)` 与断点类型构造一个 `Line`（必要时重整形），返回它的测量值。断行器靠它知道「这一行有多宽、能拉伸/压缩多少」。

派发逻辑（伪代码）：

```text
linebreak(engine, p, width):
    match p.config.linebreaks:
        Simple     -> linebreak_simple(engine, p, width)      # 贪心
        Optimized  -> linebreak_optimized(engine, p, width)   # 动态规划
```

两者最终都返回 `Vec<Line>`，交给 `finalize` 渲染。

#### 4.1.3 源码精读

派发入口非常薄，纯粹按配置二选一：

[src/inline/linebreak.rs:L152-L161](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L152-L161) —— `linebreak` 按 `config.linebreaks` 派发到 `linebreak_simple` 或 `linebreak_optimized`。

`Simple` 的默认推导逻辑在 `configuration`：`linebreaks` 字段未显式设置时，跟随 `justify`。

[src/inline/mod.rs:L194-L196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L194-L196) —— justify 时默认 `Optimized`，否则 `Simple`。

`linebreak_simple` 是典型的 first-fit，用一个 `last: Option<(Line, end)>` 记住「最近一个还放得下的断点」：

[src/inline/linebreak.rs:L167-L208](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L167-L208) —— 贪心断行：逐个断点尝试构造行；放不下且存在 `last` 时退回 `last` 收尾，遇到强制断点或放不下时收束当前行。

关键判断在第 183–189 行：

```rust
if !width.fits(attempt.width)
    && let Some((last_attempt, last_end)) = last.take()
{
    lines.push(last_attempt);
    start = last_end;
    attempt = line(engine, p, start..end, breakpoint, lines.last());
}
```

`width.fits(attempt.width)` 判断当前候选是否还塞得进可用宽度。塞不下就回退到上一个可行的断点（`last`）并重开。

#### 4.1.4 代码实践

1. **实践目标**：直观对比两种策略在同一段落上的排版差异。
2. **操作步骤**：写一个较窄的容器（例如 `#block(width: 12cm)[一段中等长度的英文 ...]`），分别用 `set par(linebreaks: false)`（Simple）与 `set par(linebreaks: true)`（Optimized）编译，并各加/去掉 `set par(justify: true)`。
3. **需要观察的现象**：Simple 下相邻行的「满/空」对比更明显；Optimized 下各行宽度更接近；两端对齐时差别最大。
4. **预期结果**：Optimized 行更均衡，但编译耗时更高。
5. 本地编译命令与耗时对比属于**待本地验证**（不要假设已运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Typst 默认在「非两端对齐」时选 `Simple`？
**答案**：两端对齐时，行的「松紧」会被空格拉伸抹平，但断点位置仍决定每行能塞多少内容，全局优化收益大；非对齐（ragged）时，行尾留白是允许的、甚至 desirable 的，贪心已足够且快得多，优化收益小、不值得付出 Knuth-Plass 的开销。

**练习 2**：`linebreak_simple` 里为什么在遇到 `Mandatory` 断点时无条件收束当前行？
**答案**：`Mandatory`（如源码里的 `\` 换行、或文本末尾）表示「此处必须断」，任何行都不能跨过它，所以无论当前行宽窄都得在这里结束并重开。

### 4.2 可断点 Breakpoint 与行尾裁剪 trim

#### 4.2.1 概念说明

`Breakpoint` 描述「一个可断点是什么类型」，它直接决定行尾如何处理。它只有三个变体：

[src/inline/linebreak.rs:L65-L74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L65-L74) —— `Breakpoint` 三变体。

- `Normal`：普通断点（如空格之后），行尾的空白需要裁剪，但裁剪方式有讲究。
- `Mandatory`：强制断点（`\n` 之后或文本末尾），不可被优化忽略，且换行符本身要被裁掉。
- `Hyphen(l, r)`：词内连字符断点。`l`/`r` 是断点在词内「前/后」各有多少个字符，用于在成本计算里额外惩罚「贴近词边」的连字（如只断开 1 个字母）。

注意 `Breakpoint` 只描述「在哪断、怎么断」，**不含位置**；位置由调用方以字节偏移 `end` 单独传入。

#### 4.2.2 核心流程

`Breakpoint::trim(start, line)` 根据断点类型，算出该行末尾「从哪里开始不要参与布局 / 不要参与整形」，返回一个 `Trim { layout, shaping }`。它保证不变量 `layout <= shaping`：

```text
trim(start, line文本):
    Normal   -> 裁掉行尾空白与 default-ignorable，但 shaping 保留到原末尾
    Mandatory-> 均匀裁掉换行类字符（layout == shaping）
    Hyphen   -> 一点都不裁（layout == shaping == 原末尾）
```

为什么 `Normal` 要让 `layout < shaping`？因为行尾空格对**布局**是多余的（不应占宽度），但对**复制粘贴**和字形集群是有用的——所以整形时仍要生成这个空格字形，只是把它的 advance（推进宽度）置零。这样既不撑宽行，又能在复制时还原空格。

#### 4.2.3 源码精读

`trim` 的实现：

[src/inline/linebreak.rs:L78-L123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L78-L123) —— 按断点类型裁剪行尾。`Normal` 分支（第 96–104 行）正是「layout 截到去空白处，shaping 保留到原文末尾」的实现。

`Trim` 结构与不变量：

[src/inline/linebreak.rs:L131-L149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L131-L149) —— `Trim { layout, shaping }`，注释明确 `layout <= shaping`，并解释了为何保留零宽空格字形。

`trim` 的结果会在 `line.rs` 的 `line()` 里被消费——`trimmed_range = range.start..trim.layout` 用于边界检查，`trim.shaping` 用于决定文本整形到何处：

[src/inline/line.rs:L151-L153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L151-L153) —— 用 `trim` 把行的逻辑范围与整形范围区分开。

#### 4.2.4 代码实践

1. **实践目标**：理解 `Normal` 断点下「layout 与 shaping 分离」的意义。
2. **操作步骤**：阅读 [trim 的 Normal 分支](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L96-L104)，再阅读 `collect_range` 中 [按 `trim.layout` 裁掉行尾空白字形](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L326-L332) 的代码。
3. **需要观察的现象**：被裁掉的空格字形是否仍参与 `items`、但其 advance 被置零。
4. **预期结果**：行尾空格在最终 PDF 里不占宽度，但选中复制时仍能得到空格。
5. 是否真的「置零 advance」需结合 `shaping.rs` 的 `trim`/`build` 确认，此处为**待本地验证**的细节。

#### 4.2.5 小练习与答案

**练习 1**：`Hyphen(..)` 断点为什么 `trim` 时「一点都不裁」？
**答案**：连字符断点发生在词内部，行尾紧挨着的是字母而非空白/换行符，没有需要裁剪的字符；连字符号本身（`-`）是 `line()` 在行尾额外补上的字形（见 `line.rs` 第 171–178 行的 `END_HYPHEN` 处理），不来自原文。

**练习 2**：`Mandatory` 的 `trim` 依据什么判断要裁哪些字符？
**答案**：用 ICU 的 `LINEBREAK_DATA`（码点到 `LineBreak` 属性的映射），裁掉 `MandatoryBreak | CarriageReturn | LineFeed | NextLine` 这几类，对应 UAX #14 的 LB4/LB5 规则。

### 4.3 断点生成 breakpoints：ICU segmenter、连字符与 CJK

#### 4.3.1 概念说明

`breakpoints` 是断行器的「候选来源」——一个内部闭包式迭代器（之所以用闭包而非外部迭代器，是因为消费者不需要那种组合性，闭包让代码更简单）。它依次对每个可断点调用 `f(byte_offset, Breakpoint)`。

断点有三类来源：

1. **UAX #14 断点**：用 ICU 的 `LineSegmenter`（基于 LSTM 神经网络模型）确定文本里所有「允许换行」的位置。这是主流来源。
2. **词内连字符断点**：对纯字母词，用 `hypher` crate 按语言特定的音节模式（patterns）枚举可连字符断点，产出 `Breakpoint::Hyphen(l, r)`。
3. **URL 专用断点**：UAX #14 对链接处理不好，所以 `https://...`、`www.` 开头的链接走专门的 `linebreak_link`，在「字符类切换处」或（超长段时）逐字符给断点。

对**中文/日文**还有第四层特殊处理：用专门的 `CJ_SEGMENTER` 替代通用 segmenter。

#### 4.3.2 核心流程

```text
breakpoints(p, f):
    选 segmenter: 中文/日文 -> CJ_SEGMENTER；否则 -> SEGMENTER
    for point in segmenter.segment_str(text):
        判断断点类型:
            文本末尾或强制换行类字符 -> Mandatory
            组合记号后紧跟对象替换符的特例 -> 跳过（issue #5489）
            其它 -> Normal
        在 [last, point] 区间内，对每个纯字母词调用 hyphenations 产出 Hyphen 断点
        f(point, breakpoint)
    链接特例: 用 linebreak_link 在 URL 内部插入 Normal 断点
```

`hyphenations` 还会过滤掉若干「禁止连字」的位置：音节末字符若属 `Glue | WordJoiner | ZWJ` 类则跳过；并依据 `hyphenate_at` 判断该处连字是否被局部样式关闭。

#### 4.3.3 源码精读

两个 segmenter（注意 `CJ_SEGMENTER` 的注释解释了它为何存在）：

[src/inline/linebreak.rs:L37-L58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L37-L58) —— 通用 LSTM segmenter 与 CJ 专用 segmenter。CJ 版本通过一个预生成 blob（`typst_assets::icu::ICU_CJ_SEGMENT`）把弯引号 `U+201C`/`U+201D` 的行断属性从 `QU` 改为 `OP`/`CP`，以修正中文/日文里引号的断行行为。

`CJ_SEGMENTER` 的选取依据是段落语言：

[src/inline/linebreak.rs:L701-L705](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L701-L705) —— 语言为中文或日文时用 CJ segmenter，否则用通用 segmenter。

主循环 `breakpoints`：

[src/inline/linebreak.rs:L692-L781](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L692-L781) —— 枚举所有断点。第 732–765 行判断 `Mandatory` vs `Normal`（含组合记号特例的 `continue`），第 768–775 行在词内插入连字符断点，第 778 行把 UAX 断点交给 `f`。

连字符断点生成：

[src/inline/linebreak.rs:L784-L825](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L784-L825) —— `hyphenations`：遍历 `hypher::hyphenate(word, lang)` 的音节，每个音节边界（除最后一个）产出一个 `Hyphen(l, r)`，并过滤禁用/禁止位置。

URL 专用断点：

[src/inline/linebreak.rs:L828-L883](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L828-L883) —— `linebreak_link`：按字符类（字母/数字/开括号/其它）切换处给断点，段超过 16 字符（如 URL 里的长 hash）则逐字符放断点。

#### 4.3.4 代码实践

1. **实践目标**：亲手枚举一段混合文本的断点，验证 CJK 与英文走不同规则。
2. **操作步骤**：取文本 `"hello world 你好世界 typst"`。对英文部分，断点应在空格后（`Normal`）及词内（`Hyphen`，若开启连字）；对中文部分，由于 `CJ_SEGMENTER`（中文用），每个汉字之间都可以是断点。
3. **需要观察的现象**：`hyphenations` 只对「纯字母」词触发（第 770 行的 `segment.chars().all(char::is_alphabetic)` 守卫），所以「你好世界」不会进入连字符逻辑，而是由 segmenter 直接给 `Normal` 断点。
4. **预期结果**：你能列出大致形如 `Normal`（hello 后）、`Hyphen(2,3)`（hello 内，假设）、`Normal`（world 后）、`Normal`（你好之间、你好世界之间……）的序列。
5. 确切的音节切分依赖 `hypher` 的语言模式表，具体断点**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CJ 文本需要专门的 segmenter，而不是直接用通用的？
**答案**：通用 LSTM segmenter 已经能让 CJK「字字可断」，但对中文/日文里的弯引号，ICU 默认的行断属性（`QU`）会导致断行不符合 CJ 排版习惯。Typst 用预生成 blob 把弯引号属性改为 `OP`/`CP`，修正这个问题；待 ICU4X 升级到 Unicode 17.0 后可合并回通用 segmenter（见源码注释）。

**练习 2**：`hyphenate_at` 在什么情况下会让某处连字失效？
**答案**：当段落级 `config.hyphenate` 未显式设置（`None`）时，回退到逐 item 查局部样式；若该处文本的 `TextElem::hyphenate` 也未设，则「仅当 `justify` 开启时」才允许连字，否则该处不连字（见 [hyphenate_at](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L886-L896)）。

### 4.4 行的成本：ratio、badness、penalty 与 CostMetrics

> 这是本讲的核心，也是理解「为何 hyphen cost 设得高」的关键。

#### 4.4.1 概念说明

Optimized 断行靠「给每行打成本、选总成本最小」来做决策。成本由两部分拼成：

- **badness（不合适度）**：衡量这一行「离理想宽度差多远」。一行正好填满 → badness=0；太松或太紧 → badness 大。
- **penalty（罚分）**：对特定「不受欢迎的断法」加罚：孤行（runt，强制断点前只剩一个词）、连字符断行、连续两行都以连字符/破折号结尾。

最终的行成本公式（注意 Typst 对论文公式有调整）：

\[
\text{cost}_j = \bigl(1 + \text{badness}_j + \text{penalty}_j\bigr)^2
\]

而 badness 又由一个「拉伸/压缩比 ratio」算出。先把行的「需要拉伸或压缩的量」归一成一个无量纲比 \( r \)：

\[
\Delta = W_{\text{avail}} - W_{\text{line}}, \qquad
\text{adjustability} = \begin{cases}\text{stretchability} & \Delta \ge 0 \\ \text{shrinkability} & \Delta < 0\end{cases}, \qquad
r = \frac{\Delta}{\text{adjustability}}
\]

再代入 badness：

\[
\text{badness} = \begin{cases}
10^6 & r < \text{min\_ratio}\ (\text{溢出，硬性过大成本}) \\
100\,|r|^3 & \text{需两端对齐或需压缩} \\
0 & \text{ragged 且不需压缩（松一点也不扣分）}
\end{cases}
\]

`CostMetrics` 把这些阈值与成本常量集中起来，并在 `Optimized` 内部被近似阶段和精确阶段共用。

#### 4.4.2 核心流程

```text
ratio_and_cost(p, metrics, width, pred, attempt, breakpoint, unbreakable):
    ratio  = raw_ratio(width, attempt.width, stretch, shrink, justifiables)
    cost   = raw_cost(metrics, breakpoint, ratio, attempt.justify,
                      unbreakable, consecutive_dash, approx=false)
    return (ratio, cost)
```

`raw_ratio` 的几个要点：

- \( r \) 继承 \( \Delta \) 的符号（正=需拉伸，负=需压缩，0=完美）。
- 当 \( r > 1 \)（拉伸量超过空格的自然可拉伸性）时，进入「字形级 justification」：把超出部分均摊到每个 justifiable 字形上，并用字号一半做归一：\( r = 1 + \frac{\Delta - \text{adjustability}}{\max(\text{justifiables},1)\cdot \text{font\_size}/2} \)。
- NaN（常见于等宽字体/CJK，adjustability 为 0）被当作 0（完美拟合）。
- 最终 clamp 到 \([MIN\_RATIO - 1,\ 10]\)。

`raw_cost` 的要点见源码精读。

#### 4.4.3 源码精读

成本常量与那段著名的注释（解释为何比论文高）：

[src/inline/linebreak.rs:L20-L35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L20-L35) —— `Cost = f64`、`DEFAULT_HYPH_COST = 135.0`、`DEFAULT_RUNT_COST = 100.0`、`MIN_RATIO = -1.0`、`MIN_APPROX_RATIO = -0.5`、`BOUND_EPS = 1e-3`。注释说明：选比论文（50）更高的成本，是因为「在 Typst 里否则连字太激进，可能与没有 glue 概念导致 ratio 算出来不一样有关」。

`raw_ratio`：

[src/inline/linebreak.rs:L570-L620](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L570-L620) —— 计算 ratio。第 588 行选 stretch/shrink，第 595 行做除法，第 606–612 行处理 \( r>1 \) 的字形级拉伸，第 619 行 clamp。

`raw_cost`（本讲最重要的一段）：

[src/inline/linebreak.rs:L626-L681](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L626-L681) —— badness 与 penalty 的合成。关键行：

- 第 636–648 行：badness 三分支（溢出 \(10^6\) / \(100|r|^3\) / 0）。
- 第 654–656 行：runt 罚分（孤行）。
- 第 659–667 行：连字符罚分，且对「贴近词边」的连字额外加 15%/步（`LIMIT=5`）：

```rust
const LIMIT: u8 = 5;
let steps = LIMIT.saturating_sub(l) + LIMIT.saturating_sub(r);
let extra = 0.15 * steps as f64;
penalty += (1.0 + extra) * metrics.hyph_cost;
```

- 第 672–674 行：连续两行以连字符/破折号结尾再加一份 `hyph_cost`。
- 第 680 行：最终 \((1 + \text{badness} + \text{penalty})^2\)。

`CostMetrics`：

[src/inline/linebreak.rs:L911-L940](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L911-L940) —— `compute` 里：`min_ratio` 在 justify 时为 `MIN_RATIO(-1)` 否则 0；`hyph_cost = DEFAULT_HYPH_COST * costs.hyphenation()`；`runt_cost = DEFAULT_RUNT_COST * costs.runt()`。注意 `costs` 来自用户可调的 `TextElem::costs`（[mod.rs 第 240 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L240)），默认乘数为 1。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：吃透成本常量与 `raw_cost`，并解释「为何 hyphen cost 是 135 而非论文的 50」。
2. **操作步骤**：
   - 精读 [成本常量与注释](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L20-L35) 与 [`raw_cost`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L626-L681)。
   - 做一个数值小算：设某行不连字时 badness=20；改为在词内连字可把 badness 降到 5。两种方案的总成本各是多少？（连字方案 penalty ≈ 135。）
3. **手算预期**：
   - 不连字：\((1 + 20 + 0)^2 = 441\)。
   - 连字：\((1 + 5 + 135)^2 = 141^{2} = 19881\)。
   - 结论：即便连字把 badness 从 20 降到 5，它带来的 penalty（135）也远大于省下的 badness，所以 Typst 几乎不会为了小幅 badness 改善而连字——这正是把 `DEFAULT_HYPH_COST` 抬高的效果。
4. **解释「为何比论文高（没有 glue）」**：Knuth-Plass 论文里行间空白是显式的 glue 节点，ratio = 期望拉伸量 / glue 的可拉伸性，量纲与数值分布与 Typst 不同；Typst 没有独立的 glue，可拉伸性来自字形级别的 adjustability，ratio 算出来不一样，若沿用论文的 50 会导致「连字相比留下的 badness 显得很划算」，从而过度连字。把成本抬到 135 是经验校准，使连字只在「能显著降低 badness」时才被采纳。
5. 若要实测：临时把 `DEFAULT_HYPH_COST` 改回 50 重新编译一段长英文，观察连字是否明显变多——**待本地验证**（不要声称已运行）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 overfull 行的 badness 直接给 \(10^6\) 而不是按公式算？
**答案**：溢出意味着这行物理上塞不下，必须避免被选为最优；用一个远大于任何正常 badness（正常 \(100\cdot 10^3 = 10^5\)，因为 ratio 被 clamp 到 10）的常数，保证溢出行只在「别无选择」时才出现。

**练习 2**：`consecutive_dash` 罚分为什么要单独加、而不是并进 badness？
**答案**：源码注释指出，K-P 论文也是在平方之后单独处理连续连字符罚分，但没解释原因。Typst 沿用了这种做法：连续两行都以连字符/破折号结尾在视觉上很糟糕（读者易误读为一个词），应当独立于 badness 强力抑制，所以作为独立 penalty 叠加。

### 4.5 Optimized 两阶段：近似上界 + bounded 精确搜索

#### 4.5.1 概念说明

`linebreak_optimized` 是 Knuth-Plass 动态规划（DP）。朴素的 DP 要为「每个断点 × 每个可能起点」调用一次 `line()` 真正构造行来测成本，非常慢。Typst 的优化是**两阶段**：

1. **近似阶段**（`linebreak_optimized_approximate`）：不调用昂贵的 `line()`，而是用累积数组（`Estimates`/`CumulativeVec`）在 \(O(1)\) 内估算任意区间的宽度/可拉伸性等，跑一遍 DP 得到「一个大概不错的方案」，再对这个方案用真实 `line()` 算出**精确成本**，作为上界 `upper_bound`。
2. **精确阶段**（`linebreak_optimized_bounded`）：用真实的 `line()` 构造行、用真实的 `ratio_and_cost`，但凡是「累计成本已超过 `upper_bound`」的搜索分支就剪掉，大幅缩小搜索空间。

这本质上是「用一个便宜的下界去给昂贵精确搜索做剪枝上界」的思路。

#### 4.5.2 核心流程

精确阶段 DP 的状态：每个断点 `end` 对应 DP 表里的一条 `Entry { pred, total, line, end }`——`pred` 指向最优前驱、`total` 是到该断点的最小累计成本。对每个 `end`，遍历所有活跃前驱 `pred`，构造 `pred.end..end` 的行，算其成本，取 `total = pred.total + line_cost` 最小者。

几个关键剪枝/维护点：

- **活跃集 `active`**：维护「仍可能作为合法行起点」的前驱下标。当某行溢出（`ratio < min_ratio`）且它是最早的活跃前驱时，把它移出活跃集（更短的行反而更长，可能因负间距，需保守）。
- **`line_lower_bound`**：一旦发现某行已 underfull（\( r > 0 \)，且无负宽 item），更短的切片只会更 underfull，于是记下成本下界，后续若 `pred.total + 下界 > upper_bound` 就直接跳过。
- **mandatory 断点**：强制断点处，之前所有断点都失效（没有行能跨过它），故 `active = table.len()`。
- 最后从表尾回溯 `pred` 链，逆序收集行。

近似阶段在「重算精确成本」时，若发现某行 overfull（`ratio < min_ratio`），说明近似得到的方案不合法，立即返回 `Cost::INFINITY`，让上界失效（精确阶段退化为无界搜索，保证正确性）。

#### 4.5.3 源码精读

`linebreak_optimized` 编排两阶段：

[src/inline/linebreak.rs:L227-L241](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L227-L241) —— 先算 `CostMetrics`，用近似阶段拿 `upper_bound`，再跑精确 bounded 阶段。

精确 bounded DP：

[src/inline/linebreak.rs:L246-L374](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L246-L374) —— `Entry` 定义、DP 主循环、剪枝与回溯。第 280–284 行用 `line_lower_bound` 跳过、第 310–312 行维护活跃集、第 323–328 行设置下界、第 332–334 行按上界剪枝、第 344–346 行处理 mandatory、第 365–373 行回溯最优路径。

近似阶段：

[src/inline/linebreak.rs:L384-L530](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L384-L530) —— 用 `Estimates` 跑 DP，重算精确成本作上界；第 520–522 行 overfull 时 bail 返回 `INFINITY`。

累积数组（近似阶段的「快速测量尺」）：

[src/inline/linebreak.rs:L946-L1038](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L946-L1038) —— `Estimates` 为宽度/可拉伸/可压缩/justifiables 各建一个 `CumulativeVec`；`estimate(range)` 用前缀和 \(O(1)\) 估区间度量。

#### 4.5.4 代码实践

1. **实践目标**：理解上界为何能让搜索变快，以及何时上界会「失效」。
2. **操作步骤**：阅读 [linebreak_optimized](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L227-L241) 与 [近似阶段的 overfull bail](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L514-L522)，再对照 [精确阶段第 357–363 行的回退逻辑](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L357-L363)。
3. **需要观察的现象**：当近似方案不含溢出行时，上界有效、精确阶段大量剪枝；一旦近似方案某行溢出，上界变 `INFINITY`，精确阶段退化为「不剪枝」但仍正确（debug 模式甚至断言不应发生）。
4. **预期结果**：能解释「上界越紧、剪枝越多、越快；上界失效时退化为朴素 DP，结果仍最优」。
5. 实际耗时对比**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`line_lower_bound` 为什么在「行有负宽 item」时不设置？
**答案**：若一行包含负宽间距，逻辑上更短的切片未必更短（可能反而更长），underfull 的单调性假设不成立，无法断言「更短只会更糟」，所以保守地不下结论。

**练习 2**：为什么精确阶段回溯时若发现 `table[idx].end != p.text.len()`，release 模式下要用 `Cost::INFINITY` 重跑一次？
**答案**：这表示上界过紧、剪枝把所有到文本末尾的路径都剪掉了，结果不完整。用 `INFINITY`（即不剪枝）重跑保证一定能找到完整的最优解——这是正确性的兜底。

## 5. 综合实践

把本讲的知识串起来，做一次「手动追踪断行」。

**任务**：取一段约 3 行的英文（例如 30 个单词），在脑海中（或纸上）完成：

1. **列断点**：用第 4.3 节的规则，标出所有 `Normal`（空格后）与可能的 `Hyphen(l, r)`（词内）断点。
2. **估成本**：对 Simple 选出的断行方案，用第 4.4 节的公式手算每行的 `ratio`、`badness`、`penalty` 与行成本，求和。再设想一个「把第二行末尾的词连字断开」的替代方案，重算并比较——体会 `DEFAULT_HYPH_COST=135` 如何压制不必要的连字。
3. **验证两阶段价值**：阅读 [linebreak_optimized](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L227-L241)，说明如果没有近似上界，精确阶段最坏要调用多少次 `line()`（大致正比于「断点数 × 活跃前驱数」），从而理解剪枝的必要性。
4. **可选实测**：临时在 `linebreak_optimized` 与 `linebreak_optimized_bounded` 之间各插一条 `eprintln!` 打印 `upper_bound` 与最终 `total`，编译运行观察上界是否合理紧致（**待本地验证**）。

完成后，你应能用一句话回答同事：「Typst 为什么连字这么克制？」——因为成本模型把连字罚分定得远高于它能省下的 badness。

## 6. 本讲小结

- `linebreak` 按 `config.linebreaks` 派发到 `Simple`（first-fit 贪心，快）或 `Optimized`（Knuth-Plass 动态规划，均衡但慢）；默认随 `justify` 推导。
- `Breakpoint` 三变体 `Normal`/`Mandatory`/`Hyphen(l,r)` 决定行尾如何裁剪（`Trim { layout, shaping }`，保持 `layout <= shaping` 以兼顾布局与复制粘贴）。
- 断点来自三处：ICU LSTM segmenter 的 UAX #14 断点、`hypher` 的词内连字符断点、`linebreak_link` 的 URL 断点；中文/日文走修正了弯引号属性的专用 `CJ_SEGMENTER`。
- 行成本 = \((1 + \text{badness} + \text{penalty})^2\)，badness 由 ratio 算（\(100|r|^3\)，溢出行给 \(10^6\)），penalty 来自孤行、连字（贴边再加 15%/步）、连续连字符。
- `DEFAULT_HYPH_COST = 135`、`DEFAULT_RUNT_COST = 100`、`MIN_RATIO = -1`，连字成本刻意高于论文的 50，因为 Typst 没有 glue 概念、ratio 分布不同，沿用 50 会过度连字。
- Optimized 采用「近似上界 + bounded 精确搜索」两阶段：近似阶段用累积数组 \(O(1)\) 估区间度量跑 DP 得上界，精确阶段用真实 `line()` 但按上界剪枝；上界失效时退化为无界 DP，保证正确。

## 7. 下一步学习建议

本讲产出的是 `Vec<Line>`（仅测量）。接下来建议：

- **[u5-l4 shaping（文本整形）](u5-l4-shaping.md)**：本讲反复用到 `ShapedText` 的 `stretchability`/`shrinkability`/`justifiables`/`natural_width`，以及 `reshape`（断点不安全时重整形）——这些正是 `ratio` 与 `line()` 的输入来源，值得回到 `shaping.rs` 把这条供给链看清楚。
- **[u5-l5 line/deco/finalize（行构建与装饰）](u5-l5-line-deco-finalize.md)**：本讲的 `Line` 由 `line()` 构造、由 `commit` 渲染成帧；两端对齐时 `commit` 如何把 `justification_ratio`/`extra_justification` 真正施加到字形上，是本讲成本模型的「兑现」环节。
- 若对全局优化感兴趣，可延伸阅读 Knuth & Plass 原论文 *Breaking Paragraphs into Lines*（1981），对照体会 Typst 在「无 glue」前提下的取舍。
