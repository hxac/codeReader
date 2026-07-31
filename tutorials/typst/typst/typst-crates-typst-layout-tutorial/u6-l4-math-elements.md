# 数学元素：分数、根号、上下标、重音等

## 1. 本讲目标

本讲是数学排版单元（u6）的第二篇，承接 u6-l3 建立的「公式树 → `MathContext` → `MathFragment`」骨架，深入到**具体数学结构**的排版实现。读完本讲，你应当能够：

- 说清楚分数（fraction）、根号（radical）这两类「带分隔线」的结构如何读取 OpenType MATH 表常量来决定线的粗细、间隙与基线位置。
- 彻底搞懂上下标（scripts）的排版：六个附着位置（pre/post 上下标 + 上下极限）如何各自定位，位置（shift）与缩放（scale）分别由哪两套机制决定——**这是本讲实践重点**。
- 区分「上下标 scripts」与「撇号 primes」两条独立路径的差别，理解为什么撇号要单独实现。
- 看懂多行公式（run）的对齐（alignment points）与行间距（leading）机制，以及矩阵/数组（table）如何复用 run 的能力。
- 理解重音（accent）、定界符（fenced）、删除线（cancel）这三类「修饰型」结构如何包裹一个底（base）并读取各自所需的 MATH 常量。

## 2. 前置知识

进入源码前，先用通俗语言把几个本讲反复用到的概念说清楚。

**回顾：公式是一棵递归排版树。** u6-l3 已建立这条心智模型：每个数学节点（分数、根号、上下标……）先把自己的子节点排成一批 `MathFragment`，再把它们合成一个更大的 `FrameFragment`，自底向上拼成最终 Frame。本讲的每个 `layout_*` 函数都遵循同一套路：先 `ctx.layout_into_fragment` 把子节点排成 frame，再用 MATH 表常量算出本结构的几何，最后 `ctx.push(FrameFragment::new(...))` 把成品塞回累加器。

**`props`：每个组件自带的「属性包」。** 本讲每个 layouter 都收到一个 `&MathProperties` 参数 `props`，它携带该组件的 `size: MathSize`（当前数学尺寸：Display/Text/Script/ScriptScript）、`class: Option<MathClass>`（数学类，决定间距）、`span`（用于诊断）等。`FrameFragment::new(props, styles, frame)` 会把这些属性连同 `font_size` 一起记进产物，使合成后的片段仍保有正确的 `math_size`/`class`，供外层（如 scripts 的 `is_text_like` 判定、spacing 计算）使用。

**OpenType MATH 表（再强调）。** 分数线粗细、分子上移量、上下标缩放比例、撇号位置……这些「排得好看」所需的具体数值**不是 Typst 硬编码的，而是从字体的 MATH 表读取**。代码里反复出现的 `constants.axis_height.at(size)`、`font.math().fraction_rule_thickness.at(size)` 就是在按当前字号 `size` 把表里的 em 值换算成绝对长度（`Abs`）。因此同一公式在不同数学字体下排版结果会不同——这正是 u6-l3 讲的 `style_for_script_scale` 要按字体注入缩放常量的根本原因。

**Display vs 非 Display 尺寸。** MATH 表里大量常量都有两份：一份给 `Display`（块级大公式，如 `fraction_numerator_display_style_shift_up`），一份给其余尺寸（`fraction_numerator_shift_up`）。本讲代码里频繁出现 `match math_size { MathSize::Display => …, _ => … }` 的二选一。

**`Abs` 与 `.at(size)`、`.max(...)`。** `Em` 值（字体相关单位）经 `.at(size)` 解析为 `Abs`（绝对长度 pt）；`.set_max(x)` 是 `self = self.max(x)` 的就地写法，本讲几何计算里随处可见（取各行/各槽最大值）。

**`Frame::soft` 与 baseline。** 排版产物 frame 用 `Frame::soft(size)` 创建（尺寸可被外层改写），再用 `frame.set_baseline(...)` 标记基线位置。基线决定该片段在更大结构里「坐」多高——分数线、根号底线、撇号位置都靠它对齐。这些已在 u2-l3 讲透。

> 本讲所有 `layout_*` 函数都由 `math/mod.rs` 的 `layout_realized` 按 `MathKind` 分派调用（u6-l3 已讲）。本讲聚焦每个函数**内部**的几何逻辑，不再重复分派表。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [src/math/fraction.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fraction.rs) | 分数 `layout_fraction`（带分数线）与无分数线堆叠 `layout_fraction` 的 else 分支；斜分数线 `layout_skewed_fraction`。 |
| [src/math/radical.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/radical.rs) | 根号 `layout_radical`：拉伸根号符号、画顶部横线、抬高 n 次根指数。 |
| [src/math/scripts.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/scripts.rs) | 上下标 `layout_scripts`（六槽 + 字距）、撇号 `layout_primes`，以及 shift/kern 计算函数群。**实践重点。** |
| [src/math/accent.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/accent.rs) | 重音 `layout_accent`：把帽子/向量箭头拉伸对齐到 base 上方/下方。 |
| [src/math/fenced.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fenced.rs) | 定界符 `layout_fenced`：计算括号拉伸高度（含 balanced 模式），拉伸 open/close/mid 符号。 |
| [src/math/cancel.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/cancel.rs) | 删除线 `layout_cancel`：画对角线（可交叉）穿过 base。 |
| [src/math/table.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/table.rs) | 矩阵/数组 `layout_table`：对齐各列、画增强线（augment lines），复用 run 的 `stack_rows`。 |
| [src/math/run.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/run.rs) | 多行公式 `layout_multiline`、行堆叠 `stack_rows`、行内断片 `into_par_items`、对齐点计算。 |
| [src/math/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs) | `style_for_script_scale`（注入上下标缩放常量）、`MathContext`、`layout_realized` 分派。 |
| [src/math/fragment/glyph.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/glyph.rs) | `GlyphFragment`：读取 `italics_correction`/`accent_attach`/`kern_at_height` 等 MATH 表字形常量。 |

## 4. 核心概念与源码讲解

### 4.1 分数 fraction：分数线为何总在「轴」上

#### 4.1.1 概念说明

分数 \(\frac{a}{b}\) 是最典型的二维结构：分子在上、分母在下、中间一条横线。这条分数线不是随便放的——它**永远落在数学横轴（axis）上**，比文字基线高出 `axis_height`。这样一行里的多个分数（哪怕分子分母高度不同）的分数线都能对齐成一条直线，视觉上才整齐。

Typst 区分三种「分数」形态，全部由 `layout_fraction` + `layout_skewed_fraction` 处理：

- **带分数线**（`item.line == true`）：标准分数 \(\frac{a}{b}\)，分子分母夹一条线。
- **无分数线堆叠**（`item.line == false`）：如二项式系数 \(\binom{n}{k}\) 的裸堆叠（线由外层定界符之外的逻辑决定），用一组 `stack_*` 常量。
- **斜分数线**（`SkewedFractionItem`）：\(a/b\) 用一个被纵向拉伸的斜杠 `/` 连接，交给独立的 `layout_skewed_fraction`。

#### 4.1.2 核心流程

带分数线情形的几何计算（伪代码）：

```
axis        = axis_height             # 横轴高度
thickness   = fraction_rule_thickness # 分数线粗细
shift_up    = 分子基线相对横轴的上移量（Display/非Display 二选一）
shift_down  = 分母基线相对横轴的下移量
num_min     = 分子与线的最小间隙
denom_min   = 分母与线的最小间隙

num_gap   = max(shift_up - (axis + thickness/2) - num.descent,  num_min)
denom_gap = max(shift_down + (axis - thickness/2) - denom.ascent, denom_min)

宽度 = max(num.width, denom.width) + 2*padding
高度 = num.height + num_gap + thickness + denom_gap + denom.height
基线 = 线的 y 坐标 + axis
```

关键点是 `num_gap`/`denom_gap` 用了「`max(理论值, 最小间隙)`」：理论上间隙由常量减去字体实际降部/升部得到，但绝不能小于字体规定的最小间隙，防止分子分母的笔画贴到线上。

无分数线情形更简单：没有线，间隙用 `stack_top_shift_up`/`stack_bottom_shift_down`/`stack_*_gap_min`，且基线落在分子与间隙中点附近。

斜分数情形（`layout_skewed_fraction`）先把斜杠按「分子高 + 分母高 + 垂直间隙」纵向拉伸，再把分子放左上、分母放右下，基线取 `fraction_height/2 + axis` 使斜杠中心落在轴上。

#### 4.1.3 源码精读

先看带分数线分支读哪些常量、如何算间隙与基线：

[文件路径:L27-L84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fraction.rs#L27-L84) —— 带分数线分支。要点：
- L28-29：`axis` 与 `thickness` 直接来自 MATH 表，按当前字号 `size` 解析。
- L30-49：`shift_up`/`shift_down`/`num_min`/`denom_min` 四组常量，每组都按 `MathSize::Display` 与否二选一——这正是块级大公式与行内小分数间距不同的来源。
- L51-53：`num_gap`/`denom_gap` 的 `max(理论, 最小)` 公式。
- L66：`baseline = line_pos.y + axis`——分数线落在轴上，基线随之确定。
- L74-83：分数线本身按 `TextElem::stroke` 是否设置，分别用「填充矩形」或「线段」绘制（颜色取文字 `fill`）。

无分数线堆叠分支：

[文件路径:L85-L118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fraction.rs#L85-L118) —— 用 `stack_top/bottom_shift_*` 与 `stack_*_gap_min` 常量，没有线，基线由 `num.ascent + shift_up + (gap_min - gap).max(0)/2` 给出。

斜分数：

[文件路径:L126-L188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fraction.rs#L126-L188) —— `layout_skewed_fraction`。要点：
- L135-137：读 `skewed_fraction_vertical_gap`/`skewed_fraction_horizontal_gap`/`axis_height`。
- L149：`item.slash.set_stretch_relative_to(fraction_height, Axis::Y)` 把斜杠纵向拉伸到分数总高（拉伸机制见 u6-l3 的 `GlyphFragment::stretch`）。
- L155-156：若斜杠溢出，抬高总高并记录垂直偏移。
- L179：`set_baseline(fraction_height/2 + axis)`，斜杠中心对齐横轴。

#### 4.1.4 代码实践

**实践目标**：验证「分数线落在轴上」并观察 Display 与非 Display 尺寸下分子间隙的差异。

**操作步骤**（源码阅读型）：
1. 打开 `src/math/fraction.rs` L30-49，列出 `shift_up` 在 `Display` 与 `_`（非 Display）两个分支分别用哪个常量。
2. 思考一个块级公式 `$ a/b = frac(a, b) $`（Display）与行内 `$a/b$`（Text）的分子上移量分别由 `fraction_numerator_display_style_shift_up` 与 `fraction_numerator_shift_up` 决定。
3. 对照 L66 的 `baseline = line_pos.y + axis`，回答：若把 `axis` 项去掉，分数线会整体上移还是下移 `axis_height`？

**需要观察的现象 / 预期结果**：
- 去掉 `axis` 后，分数线会**下移** `axis_height`。推导：frame 被放进上下文时，其 `baseline()` 对齐到文字基线；分数线画在 `line_pos.y`。正常情况下线相对基线在 `line_pos.y - (line_pos.y + axis) = -axis`（即轴上方 `axis_height` 处，正好落在横轴上）。若改成 `baseline = line_pos.y`，线相对基线变为 0（落在文字基线上），相比正确位置下移了 `axis_height`。这一点**待本地验证**：可在本地 fork 里临时把 L66 改为 `line_pos.y`，编译后对比一个含分数的文档。
- Display 分支的 shift_up 通常大于非 Display 分支，故块级分数分子离线更远。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `num_gap` 用 `max(...)` 而不是直接取 `shift_up - (axis + thickness/2) - num.descent()`？
**答案**：直接取理论值在分子降部（descent）很大时会得到负数或过小间隙，导致分子笔画贴到甚至穿过分数线。`max(..., num_min)` 保证间隙不小于字体规定的最小值 `fraction_numerator_gap_min`。

**练习 2**：斜分数里为什么要 `vertical_offset = max(slash_size.y - fraction_height, 0) / 2`？
**答案**：当被拉伸的斜杠比「分子+分母+间隙」还高时，需要把分子分母在垂直方向上向外撑开居中，避免斜杠上下溢出 frame。

---

### 4.2 根号 radical：拉伸符号 + 手画顶部横线

#### 4.2.1 概念说明

根号 \(\sqrt{x}\) 看似只是一个符号盖在被开方数上，实则由**两部分**拼成：
1. **根号符号本身**（surd √）：一个会被**纵向拉伸**的字形（拉伸机制见 u6-l3 的 `GlyphFragment::stretch`，从字体的 `MathVariants` 表选预制变体或用 `GlyphAssembly` 拼装 extenders）。
2. **顶部横线**：盖在被开方数上方的那条水平线——它**不是字体里的**，而是 Typst 自己画的一条 `Geometry::Line`/矩形。

n 次根 \(\sqrt[n]{x}\) 还多一个指数 `n`，它要被抬到根号符号的右上角，抬升量由 `radical_degree_bottom_raise_percent` 决定。源码注释明确引用了 TeXbook 第 443、360 页与 MathML Core 规范，说明这套几何是行业标准的实现。

#### 4.2.2 核心流程

```
1. 先排被开方数 radicand（一个 frame）
2. 用 radicand 高 + 线粗 + gap 算出根号符号的「目标高度」target
3. item.sqrt.set_stretch_relative_to(target, Axis::Y)  # 拉伸根号
4. 读常量：thickness, extra_ascender, kern_before/after_degree,
          raise_factor, gap（Display/非Display 二选一）
5. gap = max(原gap, (sqrt.height - thickness - radicand.height + gap)/2)
   # TeXbook p.443：多余空间上下均分
6. 若有 index：sqrt_offset = kern_before + index.width + kern_after
              shift_up = raise_factor * (inner_ascent - descent) + index.descent
7. 画顶部横线（line_pos 在 radicand 正上方 gap 处）
8. 基线 = ascent（≈ radicand.ascent + gap + thickness + extra_ascender）
```

#### 4.2.3 源码精读

先看「目标高度」如何算、根号如何拉伸：

[文件路径:L26-L42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/radical.rs#L26-L42) —— `target = radicand.height + thickness + gap`（`gap` 仍按 Display 二选一），随即 `item.sqrt.set_stretch_relative_to(target, Axis::Y)`。

常量读取与间隙均分（TeXbook p.443 item 11）：

[文件路径:L45-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/radical.rs#L45-L80) —— 要点：
- L46-50：`radical_rule_thickness`、`radical_extra_ascender`、`radical_kern_before_degree`、`radical_kern_after_degree`、`radical_degree_bottom_raise_percent`。
- L76：`gap = gap.max((sqrt.height - thickness - radicand.height + gap) / 2.0)`——这是 TeXbook 的关键公式：若拉伸后的根号符号比被开方数高出不少，把多余空间上下均分，使被开方数在根号内大致居中。

n 次根指数的抬升（TeXbook p.360，带 Typst 的修正）：

[文件路径:L86-L96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/radical.rs#L86-L96) —— `shift_up = raise_factor * (inner_ascent - descent) + index.descent()`。注释指出 `+ index.descent()` 是 Typst 相对 TeX 的改动：没有它，指数的降部（如字母 `g` 当指数）会撞到根号符号——MS Word 也做了类似调整。

顶部横线的手绘（两种风格）：

[文件路径:L120-L146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/radical.rs#L120-L146) —— 若用户设了 `TextElem::stroke`，用 `styled_rect` 画一条带描边的矩形线；否则用 `Geometry::Line` + `FixedStroke::from_pair(fill, thickness)` 画一条纯色线段。注意 L127 注释「Omit the left-side edge」：矩形左边被省略，因为左边是根号符号自身的弯钩，不能重复画线。

#### 4.2.4 代码实践

**实践目标**：理解根号符号拉伸与顶部横线是「两个独立绘制物」。

**操作步骤**：
1. 在 `layout_radical` 里找到唯一的根号拉伸调用 `item.sqrt.set_stretch_relative_to(target, Axis::Y)`（[L41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/radical.rs#L41)）与「画顶部横线」的 `frame.push(line_pos, ...)`（[L145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/radical.rs#L145)）。
2. 回答：如果把 L120-L146 那段画横线的代码整段注释掉（仅本地实验，勿提交），渲染 `\sqrt{x}` 会变成什么样？
3. 追踪 `gap` 在 L76 的 `max` 之后变大时，被开方数 `radicand_pos`（L107 `radicand_y = ascent - radicand.ascent()`）会如何随之移动。

**需要观察的现象 / 预期结果**：
- 注释掉画线后，根号只剩弯钩 √ 而没有顶部覆盖线——证实「横线是 Typst 自绘、非字体提供」。**待本地验证**。
- `gap` 变大 → `sqrt_ascent`（L78）变大 → `ascent` 变大 → `radicand_y = ascent - radicand.ascent()` 变大 → 被开方数整体下移，从而在更高的根号内部居中。

#### 4.2.5 小练习与答案

**练习 1**：为什么根号符号要先排一次（L28）算出 `target`，拉伸后（L42）还要再排一次？
**答案**：第一次排版是为了拿到被开方数的真实高度，从而算出根号该拉伸到多高（`target`）；第二次是真正用拉伸后的根号符号产出最终 frame。两次排版的对象不同：第一次的结果只用于测量 `target`，第二次才是产出。

**练习 2**：`radical_kern_before_degree` 与 `radical_kern_after_degree` 分别控制什么？
**答案**：前者是 n 次根指数与根号符号弯钩之间的水平间距，后者是指数之后、根号主体开始之前的间距（见 L87 `sqrt_offset = kern_before + index.width + kern_after`，它决定了主体根号在 x 方向的起始偏移）。

---

### 4.3 上下标 scripts：位置由 MATH 常量决定，缩放由 script_scale 决定（实践重点）

#### 4.3.1 概念说明

上下标是数学排版里最常见也最精细的结构。Typst 用一个统一的 `ScriptsItem` 表达**六个附着位置**，对应 OpenType MATH 规范的全部 attachment 槽位：

| 字段 | 含义 | 示例 |
|------|------|------|
| `top_left` / `bottom_left` | 前（pre）上标 / 前下标 | \({}^b a\) |
| `top` / `bottom` | 上极限 / 下极限（limit） | \(\sum_i^N\) |
| `top_right` / `bottom_right` | 后（post）上标 / 后下标 | \(x_i^2\) |

`layout_scripts` 把这六个槽位连同 base 一起排进一个 frame。注意区分两类附着：
- **scripts（上下标）**：贴在 base 右上/右下（或左上/左下），尺寸缩小、位置由一组 `superscript_*`/`subscript_*` 常量决定。如 \(x^2\)。
- **limits（极限）**：堆在大算子（∑∏∫）的正上方/正下方，不缩小、居中，位置由 `upper_limit_*`/`lower_limit_*` 或 `stretch_stack_*` 常量决定。如 \(\sum_{i}\)。

**位置（shift）与缩放（scale）是两套独立机制**，这是本讲最易混淆也最重要的点：
- **缩放**（上下标变小）由 `MathSize` 降级 + `script_percent_scale_down` 常量共同实现。
- **位置**（上下标抬高/降低多少）由 `compute_script_shifts` 里读取的一组 MATH 常量决定。

而 **primes（撇号 ′ ″ ‴ …）走完全独立的 `layout_primes` 路径**，不经过 shift/kern 这套逻辑。注意：只有 **5 个及以上**的撇号才会构造为 `PrimesItem` 进入 `layout_primes`（1～4 个撇号有专用 Unicode 码位 ′ ″ ‴ ⁗，按普通字形渲染）。

#### 4.3.2 核心流程

`layout_scripts` 的主流程：

```
1. 先排 top/bottom（极限），量出它们的宽度，用于决定 base 的水平拉伸量
2. base 按该宽度尝试水平拉伸（set_stretch_relative_to, Axis::X）
3. 排 base，并排六个槽位 [tl, t, tr, bl, b, br]
4. 调 layout_attachments 统一计算几何并 push
```

`layout_attachments`（核心几何）：

```
A. compute_script_shifts  → (tx_shift, bx_shift)：上下标基线相对 base 基线的位移
B. compute_limit_shifts   → (t_shift, b_shift)：极限基线相对 base 基线的位移
C. ascent/descent = 各槽位「位移 + 自身 ascent/descent」取 max（决定 frame 高）
D. compute_limit_widths   → 极限左右超出 base 宽度的量（含 italic correction 半值偏移）
E. compute_pre_script_widths  → 前上下标向左超出的量（含 space_after_script）
F. compute_post_script_widths → 后上下标向右超出的量（含 per-corner kern）
G. 宽 = pre + base + post；逐个 push_frame
```

上下标位移 `compute_script_shifts` 的取 max 逻辑（伪代码，以上标 shift_up 为例）：

```
shift_up = max(
    superscript_shift_up(_cramped),                       # 字体规定的基本上移
    is_text_like ? 0 : base_ascent - sup_drop_max,        # 非文字则下拉不超过阈值
    sup_bottom_min + sup.descent,                         # 上标底部不低于最小值
)
# 若同时有上下标且二者间隙 < gap_min：把差额按 sup_bottom_max_with_sub 上限分配
```

#### 4.3.3 源码精读

先看主入口与六槽排布：

[文件路径:L26-L68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/scripts.rs#L26-L68) —— `layout_scripts`。要点：
- L43-46：先排 `top`/`bottom`，量出 `relative_to_width`，再让 base 按此宽度水平拉伸（仅当 base 是 stretchy 的，如可拉伸箭头）。
- L51-58：六槽位按 `[tl, t, tr, bl, b, br]` 顺序排好。
- L60-67：交给 `layout_attachments`。

上下标位移计算（本节几何核心）：

[文件路径:L344-L408](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/scripts.rs#L344-L408) —— `compute_script_shifts`。要点：
- L351-356：上标基本位移在 `cramped`（紧凑，如根号下、分数下的上标要矮一截）时取 `superscript_shift_up_cramped`，否则 `superscript_shift_up`。
- L362-365：读 `superscript_bottom_min`、`superscript_bottom_max_with_subscript`、`superscript_baseline_drop_max`、`sub_superscript_gap_min`、`subscript_shift_down`、`subscript_top_max`、`subscript_baseline_drop_min`。
- L371-378：`shift_up` 取多个约束的 `max`——「基本位移」「非文字型 base 的下拉上限」「上标底部最小高度」。
- L389-405：**同时有上下标时的间隙修正**：若上标底与下标顶的间隙 `gap < gap_min`，把差额 `increase` 尽量分配给上标（不超过 `sup_bottom_max_with_sub`），剩余的上下标各分一半。这是上下标「不打架」的关键。

极限位移（区分 stretchy base 与否）：

[文件路径:L299-L339](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/scripts.rs#L299-L339) —— `compute_limit_shifts`。`stretchy == true`（如 ∑）用 `stretch_stack_*` 常量并按 gap 计算；否则用 `upper_limit_baseline_rise_min`/`lower_limit_baseline_drop_min` 等。

每槽字距（MathKernInfo）：

[文件路径:L415-L451](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/scripts.rs#L415-L451) —— `math_kern`。它对每个角（tl/tr/bl/br）按「校正高度」查 base 与 script 两者的 `kern_at_height` 表并相加，取较小 kern（即较大值）。L446-450 注释指出 OpenType 规范此处有措辞 bug（规范说取 min，但因 kern 常为负，实际应取较小 kern 值以防字形相撞）。

现在看**缩放**这一套（与位置分离）。`style_for_script_scale` 把字体的两个缩放百分比注入样式链：

[文件路径:L608-L615](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L608-L615) —— `style_for_script_scale` 把 `script_percent_scale_down` 与 `script_script_percent_scale_down`（MATH 表常量，默认 70 / 50）写入 `EquationElem::script_scale`。它在两个入口（L64、L119）与换字体时（L454）链接进样式链。

真正把百分比乘到字号上的是 typst-library 里 `TextElem::size` 的解析：

[文件路径:L1143-L1150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1143-L1150) —— 按当前 `MathSize` 选取缩放因子：`Display/Text → 1.0`、`Script → script_scale.0/100`（默认 0.7）、`ScriptScript → script_scale.1/100`（默认 0.5）。

而 `MathSize` 本身何时降级（Display/Text → Script，Script/ScriptScript → ScriptScript）发生在排版前的 resolve 阶段（typst-library 的 `style_for_superscript`/`style_for_subscript`），不在本 crate——本 crate 只消费已降级后的 `MathSize` 与注入的百分比。

撇号走另一条路：

[文件路径:L72-L97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/scripts.rs#L72-L97) —— `layout_primes`。要点：
- L78-80：用 `GlyphFragment::synthetic` 合成一个 `'′'`（`PRIME_CHAR`，U+2032）字形，合成失败则直接返回（空撇号）。
- L84：`width = prime.width * (count + 1) / 2`——总宽按「半重叠」排布。
- L88-93：每个撇号放在 `prime.width * (i/2)` 处，相邻撇号半宽重叠，视觉上紧贴成 ′ ″ ‴。
- L95：`with_text_like(true)`——撇号标记为 text-like，使其在更大结构里被当作普通文字处理。

#### 4.3.4 代码实践（本讲重点）

**实践目标**：对照 `style_for_script_scale` 与字体 MATH 表常量，解释上下标的「缩放」如何由 `script_percent_scale_down` 决定；并说明「位置」由谁决定；最后对比 primes 与 scripts 的差别。

**操作步骤**：
1. 打开 [src/math/mod.rs:L608-L615](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L608-L615)，确认 `script_scale` 写入的是字体 `script_percent_scale_down`（scriptscript 用 `.1`）。
2. 追踪这个 `script_scale` 被谁消费：跳到 [typst-library/src/text/mod.rs:L1143-L1150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1143-L1150)，写下 `MathSize::Script` 对应的缩放因子公式。
3. 现在区分两件事：
   - **缩放**：base 字号 \(s\)，上标 \(= s \times \text{script\_percent\_scale\_down}/100\)（默认 \(0.7s\)），上上标 \(= s \times 0.5\)。
   - **位置**：由 [compute_script_shifts:L344-L408](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/scripts.rs#L344-L408) 里的 `superscript_shift_up`/`superscript_bottom_min` 等常量决定，**与缩放百分比无关**。
4. 对比 [layout_primes:L72-L97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/scripts.rs#L72-L97) 与 `layout_scripts`，列出二者差异。

**需要观察的现象 / 预期结果**：
- 缩放因子公式：`factor = match MathSize { Display|Text => 1.0, Script => scale.0/100, ScriptScript => scale.1/100 }`，最终字号 `= factor * 基础字号`。
- 「位置 vs 缩放」结论：上标变小靠 `script_percent_scale_down`（经 `TextElem::size` 解析）；上标抬高多少靠 `compute_script_shifts` 读的 `superscript_*` 常量。改 `script_percent_scale_down` 只改大小、不改抬起高度；改 `superscript_shift_up` 只改抬起高度、不改大小。
- primes 与 scripts 的差别（应能列出）：
  - **触发对象**：primes 只处理 **5 个及以上**的撇号序列（1～4 个有专用 Unicode 码位 ′ ″ ‴ ⁗，按普通字形渲染、不进 `layout_primes`）；scripts 处理一切 \(x^2\)、\(x_i\)、\({}^b a\)、\(\sum_i^N\) 等通用附着。
  - **几何来源**：primes 不读任何 MATH 常量，仅按「半重叠」固定排布；scripts 读 `superscript_*`/`subscript_*`/`upper_limit_*` 等大量常量并做字距（kern）。
  - **位置槽**：primes 只有一条水平排布；scripts 有六个槽位 + 极限居中。
  - **text_like**：primes 强制 `text_like(true)`；scripts 的 text-likeness 由 base 继承。
  - **缩放**：primes 沿用当前 `MathSize`（撇号本身已在 script 尺寸下解析）；scripts 的各槽位各自带 `MathSize`（已在 resolve 阶段降级）。

> 若想本地直观验证缩放，可临时在 `style_for_script_scale` 里把 `.0`/`.1` 改成固定值（如 `(100, 100)`，仅本地实验），编译一个含 \(x^{y^z}\) 的文档，观察上下标不再变小——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`cramped` 样式如何影响上标位置？给出代码依据。
**答案**：`compute_script_shifts` 在 L351-356 按 `cramped` 选择 `superscript_shift_up_cramped`（更小）而非 `superscript_shift_up`。 cramped 在 resolve 阶段被设置于根号被开方数、分数分母等处的上标（见 typst-library `style_cramped`），使这些位置的上标矮一截，避免顶到外层结构。

**练习 2**：为什么 `layout_scripts` 要先排 `top`/`bottom` 再排 base（L43-48）？
**答案**：为了量出极限（top/bottom）的宽度 `relative_to_width`，从而让 base 在 `set_stretch_relative_to(relative_to_width, Axis::X)` 时按需水平拉伸（例如带极限的可拉伸箭头要拉到与极限同宽）。base 排版必须发生在拉伸设定之后。

**练习 3**：撇号 `width = prime.width * (count + 1) / 2` 中，`(count+1)/2` 这个系数是怎么来的？
**答案**：每个撇号放在 `prime.width * (i/2)`（i 从 0 到 count-1），相邻撇号重叠半个宽度，故 count 个撇号占据 `(count-1)/2 + 1 = (count+1)/2` 个 prime 宽度（见 L84 与 L88-93）。

---

### 4.4 多行公式与矩阵：run 的对齐机制与 table 的复用

#### 4.4.1 概念说明

数学里常有「多行对齐」需求：方程组按 `=` 对齐、矩阵按列对齐。Typst 用一套通用的「对齐点（alignment point）」机制统一处理：

- **`MathRun = Vec<MathFragment>`**：两个对齐点之间的一串片段，称为一个「cell」。
- **`AlignedRow`**：一行被对齐点切成若干 cell。
- **对齐点交替**：列按「右对齐、左对齐、右对齐、左对齐……」交替（`LeftRightAlternator`），这正是 `a &= b` 里 `=` 左右的默认对齐方式——`=` 左侧的 `a` 右对齐、右侧的 `b` 左对齐。

`layout_multiline` 排多行公式（如 `cases`、对齐方程组），`layout_table` 排矩阵/数组——二者都复用 `run.rs` 的 `stack_rows` 把多行堆起来。区别在于 table 还要算列宽、画增强线（augment lines，如矩阵的竖线 `|`）。

行内单行公式则走 `into_par_items`：在二元运算符/关系符处切成多个 `InlineItem`，让外层段落能在这些位置换行。

#### 4.4.2 核心流程

`layout_multiline`：

```
1. 逐行 layout_aligned_row → 每行切成若干 MathRun（cell）
2. 对每列取所有 cell 宽度的 max → col_widths
3. measure_row 量每行的 ascent/descent
4. 选 leading：Text/Display 用普通段落 leading；Script/ScriptScript 用 0.25em 紧凑行距
5. stack_rows：按对齐点把每行排成 line frame，再逐行堆叠（行间加 leading）
```

`stack_rows` → `row_into_line_frame` 把一行的各 cell 放到累积对齐点上：

```
对齐点位置 points = 累积列宽（cumulative_alignment_points）
对每个 cell：右对齐列放在 point - width；左对齐列放在前一个 point
```

`layout_table`：先独立排每个 cell、算各列（含子列）最大宽度与各行 ascent/descent，再用 `stack_rows` 堆叠，最后按 `augment` 的 hline/vline 偏移画线，基线取 `height/2 + axis`（矩阵整体居中在轴上）。

#### 4.4.3 源码精读

多行入口与列宽/行距：

[文件路径:L27-L69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/run.rs#L27-L69) —— `layout_multiline`。要点：
- L39-47：`col_widths[c]` 取该列所有 cell 的最大宽度。
- L49-53：`leading` 按 `MathSize` 选——Script 及更小用 `TIGHT_LEADING`（0.25em），否则用普通 `ParElem::leading`。这就是为什么小尺寸多行结构（如矩阵里的对齐）行距更紧。

行堆叠与对齐点：

[文件路径:L113-L147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/run.rs#L113-L147) —— `stack_rows`。L121 `cumulative_alignment_points` 算出各对齐点的 x 坐标；L137 每行按 `row_ascent - sub.ascent()` 定位；无对齐点时（L138-140）按 `align` 整体对齐。

把一行 cell 放到对齐点：

[文件路径:L302-L345](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/run.rs#L302-L345) —— `row_into_line_frame`。L320-331：遇对齐点则按 `alternator`（Right→`point - width`，Left→`prev_point`）放置，实现「右对齐列贴在对齐点左侧、左对齐列从对齐点开始」。

行内断片（让段落能在算符处换行）：

[文件路径:L173-L253](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/run.rs#L173-L253) —— `into_par_items`。L191-199：在 `Binary`（非后接 Closing）与 `Relation`（非后接 Relation/Closing）处切开 frame，产生独立的 `InlineItem::Frame` 与可伸缩的 `InlineItem::Space`，从而行内长公式能在这里换行。

矩阵复用 run：

[文件路径:L129-L164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/table.rs#L129-L164) —— `layout_table` 逐列用 `stack_rows`（L137）堆叠各 cell，列间按 `gap` 推进 `x`，遇 `vline` 偏移画竖线。

[文件路径:L184-L191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/table.rs#L184-L191) —— 矩阵基线 `height/2 + axis`，整体居中在横轴上（与分数线、大算子同理）。

#### 4.4.4 代码实践

**实践目标**：理解「右对齐列 / 左对齐列」交替如何实现按 `=` 对齐。

**操作步骤**：
1. 在 `row_into_line_frame`（L302-345）里找到 `LeftRightAlternator::Right => point - width`（L324）与 `_ => prev_point`（L325）。
2. 设想一个两行公式：
   - 第 1 行 cell 宽度 `[w(a), w(= b)]`，第 2 行 `[w(c+d), w(= e)]`。
   - `col_widths = [max(w(a),w(c+d)), max(w(=b),w(=e))]`，对齐点 `point0 = col_widths[0]`。
3. 手算：第 1 行的 `a` 放在哪？`= b` 放在哪？

**需要观察的现象 / 预期结果**：
- 第 0 列（右对齐）：`a` 放在 `point0 - w(a)`，`c+d` 放在 `point0 - w(c+d)`——二者右端都对齐到 `point0`，即 `=` 的左侧。
- 第 1 列（左对齐）：`= b` 与 `= e` 都从 `point0` 开始——`=` 左对齐。
- 于是两行的 `=` 在同一条垂直线上，实现按 `=` 对齐。这是「右对齐列 + 左对齐列」交替的直接效果。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Script/ScriptScript 尺寸下多行结构要用 `TIGHT_LEADING`（0.25em）而不是普通段落行距？
**答案**：小尺寸结构（如矩阵单元内部、上下标里的多行）若用正常行距会显得松散不成比例；0.25em 的紧凑行距让小字多行视觉上更紧凑。见 run.rs L49-53。

**练习 2**：`into_par_items` 为什么不在每个 `Relation` 处都切开？
**答案**：L195-197 排除了「连续两个 Relation」与「Relation 后接 Closing」两种情况——连续等号（如 `a == b`）不应在中间断行，关系符后紧跟右括号也不宜断开。这避免了不自然的换行点。

**练习 3**：矩阵为何要把基线设为 `height/2 + axis`？
**答案**：矩阵作为一个整体应像大算子、分数一样居中在横轴上，使其在与同行其他数学对象并列时（如 `A + mat(..)`）视觉对齐到轴而非基线。见 table.rs L188。

---

### 4.5 修饰型结构：重音 accent、定界符 fenced、删除线 cancel

这三类结构共同点是「包裹一个 base 并叠加修饰」，但读取的 MATH 常量与几何各不相同，放在一起对比。

#### 4.5.1 概念说明

- **重音（accent）**：把一个帽子（\(\hat{x}\)）、向量箭头（\(\vec{x}\)）等放在 base 上方/下方。关键问题：重音符号要**水平拉伸**到 base 宽度，且要按 base 与重音各自的「重音附着点（accent_attach）」对齐，而不是简单居中。base 太高时还要换用「扁平变体（flac）」防止重音飞太高。
- **定界符（fenced）**：`(...)`、`[...]` 的括号要**纵向拉伸**到内容高度。关键问题：拉到多高？这取决于内容里最高片段；`balanced` 模式（分数等轴对称内容）按「2 倍到轴距离」算，使括号刚好包住对称结构。
- **删除线（cancel）**：画一条对角线（可交叉成 ×）穿过 base。它**几乎不读 MATH 表**（只读 base 自身度量），几何是纯计算：默认角度是对角线 `atan(width/height)`，默认长度是对角线长。

#### 4.5.2 核心流程

accent：拉伸重音 → 按 `accent_attach` 对齐 → 算间隙 `gap = -accent.descent - base.ascent.min(accent_base_height)` → 组 frame 并把 base 的 `base_ascent`/`italics_correction`/`accent_attach` 透传给产物（供外层 scripts/accent 复用）。

fenced：算 `relative_to`（拉伸目标高度）→ 对 open/close/mid 逐个 `set_stretch_relative_to(relative_to, Axis::Y)` → 排成 fragments 推回累加器（注意 fenced 不合成单 frame，而是把 open/body/close 各自作为独立 fragment push，保留它们各自的 `MathClass` 以便间距规则生效）。

cancel：排 base → 按角度/长度画线（`invert` 翻转得第二条）→ `background` 决定线在 base 之下还是之上 → 透传 base 的 `italics_correction`/`text_like`/`accent_attach`。

#### 4.5.3 源码精读

重音对齐与扁平变体：

[文件路径:L17-L54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/accent.rs#L17-L54) —— `layout_accent`。要点：
- L24-28：若 base 升部超过 `flattened_accent_base_height`，调用 `accent.set_flac()` 换扁平变体——高 base（如大写字母）上方重音不应飞得太高。
- L30-31：重音按 base 宽度水平拉伸，并按字号设拉伸字号。
- L39-53：按 `accent_attach`（base 与重音各自的「附着点」x 坐标）对齐，而非简单居中——这是重音正确「戴」在字母正上方的关键。

定界符拉伸高度（balanced 与否）：

[文件路径:L84-L103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fenced.rs#L84-L103) —— `relative_to_from_fragments`。`balanced` 时取 `2 * max(ascent - axis, descent + axis)`（对称结构按轴对称算高度），否则取 `height()`。

[文件路径:L40-L61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fenced.rs#L40-L61) —— open/close 经 `set_stretch_relative_to(relative_to, Axis::Y)` 纵向拉伸后 push。

删除线默认角度与长度：

[文件路径:L129-L148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/cancel.rs#L129-L148) —— `default_angle = atan(width/height)`（相对 y 轴），`default_length = 对角线 hypot`。

[文件路径:L44-L69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/cancel.rs#L44-L69) —— `background` 时用 `prepend_frame` 把线放在 base 之下，否则 `push_frame` 放之上；`cross` 为真再画第二条（`invert` 翻转角度）。

#### 4.5.4 代码实践

**实践目标**：对比三类结构「读取 MATH 常量的多寡」。

**操作步骤**：
1. 在 accent.rs 里数出读取的 MATH 常量（`flattened_accent_base_height`、`accent_base_height`，约 2 个）。
2. 在 fenced.rs 里确认 `relative_to_from_fragments` 用到 `axis_height`（balanced 分支 L95）。
3. 在 cancel.rs 里确认 `draw_cancel_line`/`default_angle` **完全没有调用 `font.math()`**——几何纯由 base 的 `size` 决定。

**需要观察的现象 / 预期结果**：
- accent 依赖字体（重音字形、附着点、扁平阈值都来自 MATH 表）；fenced 依赖字体（拉伸变体、轴高度）；cancel **不依赖任何 MATH 常量**，只画几何线。这解释了为什么 cancel 对非数学字体也能工作得不错，而 accent/fenced 在非数学字体下效果差。

#### 4.5.5 小练习与答案

**练习 1**：fenced 为什么把 open/body/close 各自作为独立 fragment push，而不像 fraction 那样合成单 frame？
**答案**：括号是 `Opening`/`Closing` 类、body 可能含 `Normal`/`Binary` 等类，把它们合成单 frame 会丢失内部 `MathClass` 边界，导致相邻片段间的间距规则失效。保留独立 fragment 让外层的 spacing 逻辑能按类自动加间距。

**练习 2**：accent 里 `gap = -accent.descent - base.ascent.min(accent_base_height)`，为什么用 `base.ascent.min(accent_base_height)`？
**答案**：`accent_base_height` 是字体规定的「正常 base 高度阈值」。base 比它矮时按实际 ascent 算间隙；base 比它高时封顶用 `accent_base_height`，防止重音被推得过高（L61-62）。即矮字母按实际、高字母封顶。

**练习 3**：cancel 的 `invert` 在 `cross` 模式下起什么作用？
**答案**：第一条线用原始角度（或用户角度），`cross` 时第二条线强制 `invert=true`（L56 传 `true`），使角度取反（L108-110 `angle *= -1`），从而两条线方向相反、交叉成 × 形。

## 5. 综合实践

把本讲知识串起来，追踪一个稍复杂的块级公式从 `MathKind` 分派到几何的全过程。

**任务**：给定块级公式

```
$ sum_(i=0)^n a_i / sqrt(b_i + c_i) = vec(d_i''''', accent: arrow) $
```

（`d_i` 后接 5 个撇号，用以强制走 `PrimesItem` 路径——见下方提示。）

请完成以下分析（纯源码阅读，无需运行）：

1. **分派**：在 [layout_realized:L502-L540](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L502-L540) 里，列出公式中的 `∑`（大算子带极限）、分数、根号、向量重音、撇号序列分别命中哪个 `MathKind::*` 分支、调用哪个 `layout_*`。
2. **缩放链路**：追踪 `a_i` 的下标 `i` 的字号如何变小——指出 `style_for_script_scale`（[mod.rs:L608](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L608)）注入的百分比如何经 [text/mod.rs:L1143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1143) 的 `MathSize::Script` 分支落到 `0.7 × base`。
3. **位置 vs 缩放**：`d_i'''''` 的 5 个撇号走 `layout_primes`，而 `a_i` 的下标 `i` 走 `layout_scripts`——二者的**位置**分别由什么决定（撇号的半重叠排布 vs `compute_script_shifts` 的 MATH 常量）？
4. **拉伸**：`sqrt(...)` 的根号符号何时被拉伸（[radical.rs:L41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/radical.rs#L41)）？向量重音 `vec(.., accent: arrow)` 的箭头何时被水平拉伸（[accent.rs:L30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/accent.rs#L30)）？

**预期产出**：一张表，列出每个子结构 → `MathKind` 分支 → 调用的 layouter → 它读取的关键 MATH 常量 → 它是「合成单 frame push」还是「push 多个独立 fragment」。完成后，你应能解释这条公式里**每一次字号缩小、每一次符号拉伸、每一条自绘线**分别在源码哪一行发生。

> 提示：`∑` 的极限走 `compute_limit_shifts`（[scripts.rs:L299](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/scripts.rs#L299)）的 `stretchy == true` 分支（用 `stretch_stack_*` 常量）。关于撇号：`PrimesItem` 的 `count` **恒不小于 5**（[item.rs:L868](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/item.rs#L868)），1～4 个撇号有专用 Unicode 码位（′ ″ ‴ ⁗）按普通字形渲染、不走 `layout_primes`；故本题用 5 个撇号确保命中 `MathKind::Primes`。

## 6. 本讲小结

- **分数**：分数线恒落在横轴上（`baseline = line_pos.y + axis`），间隙用 `max(理论值, 最小间隙)` 保证不贴线；Display 与非 Display 用不同常量组。斜分数单独拉伸斜杠。
- **根号**：根号符号纵向拉伸 + 顶部横线**自绘**两件事分开；多余空间按 TeXbook p.443 上下均分；n 次根指数按 `radical_degree_bottom_raise_percent` 抬升。
- **上下标（核心）**：**位置**由 `compute_script_shifts` 读 `superscript_*`/`subscript_*` 常量决定，**缩放**由 `style_for_script_scale` 注入的 `script_percent_scale_down`（默认 70%）经 `TextElem::size` 解析决定——两套机制独立。同时有上下标时按 `sub_superscript_gap_min` 修正间隙。
- **primes ≠ scripts**：撇号走独立路径，半重叠固定排布、不读 MATH 常量、强制 `text_like(true)`；scripts 是六槽通用附着 + 字距。
- **run/table**：对齐靠「右对齐列 + 左对齐列」交替（`LeftRightAlternator`）实现按 `=` 对齐；Script 尺寸用紧凑行距；矩阵复用 `stack_rows` 且整体居中在轴上。
- **accent/fenced/cancel**：重音按 `accent_attach` 对齐并可能换扁平变体；定界符按 `balanced` 与否算拉伸高度；cancel 不读 MATH 常量、纯几何画线。

## 7. 下一步学习建议

- 本讲覆盖了 `layout_realized` 分派表里大部分 `MathKind` 分支，剩下几个叶子分支（`Glyph`/`Text`/`Number`/`Box`/`External`/`Mathml`/`Group`）在 `src/math/text.rs` 与 `mod.rs` 的 `layout_box`/`layout_external` 中，建议接着阅读，补全「叶子如何变成 `MathFragment`」。
- 下一讲 **u6-l5 StackLayouter** 将离开数学，进入栈布局——它是 u6-l6 列表布局的底座，与本讲并无强依赖，但 `Fr` 分数分配与 `ruler` 对齐的思想会再次出现。
- 若想验证本讲的几何直觉，强烈建议本地 fork typst，在 `fraction.rs`/`scripts.rs` 关键行临时改一个常量（如 `script_percent_scale_down`、`superscript_shift_up`），编译一个含对应结构的文档对比渲染——这是理解「MATH 表常量驱动排版」最直接的方式。
- 继续向后可读 u7 单元（rules.rs），看 `EQUATION_RULE` 如何把本讲的 `layout_equation_inline`/`layout_equation_block` 挂进排版流程，形成「show 规则 → layouter」的闭环。
