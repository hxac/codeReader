# 行构建与装饰：line/deco/finalize

## 1. 本讲目标

本讲是行内（段落）布局单元的收尾篇。上一篇 u5-l3 讲完了「断行」：`linebreak` 把一整段已整形的文本切成若干 `Line`，但那时的 `Line` **只携带测量信息，还不是可以画出来的帧**。本讲负责把这堆「半成品行」变成最终的段落 `Frame`。

学完后你应该能够：

- 说清一条 `Line` 是如何从「字节区间」被装配出来的（连字符、行首/行尾空白裁剪、BiDi 重排）。
- 读懂 `commit` 如何把一行 item 提交成单行帧：悬挂缩进、标点悬挂（overhang）、两端对齐（justify）、基线对齐，以及为何最终要按「逻辑顺序」而非「视觉顺序」排序。
- 解释下划线/删除线/高亮等装饰是如何「绕开」字形降部（evade）的，以及 `evade: false` 时的差别。
- 说明 `finalize` 如何把多行帧堆叠成一个段落 `Fragment`，以及行号 marker 是怎么挂上去的。

本讲承接 u5-l3（断行）与 u5-l4（整形），是 `collect → prepare → linebreak → finalize` 四段管线的最后一段。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（在前序讲义中已建立，本讲不重复）：

- **四段管线**：段落排版依次经过 `collect`（拍平成字符串 + segment）、`prepare`（BiDi + 整形得到 `Preparation`）、`linebreak`（断行得到 `Vec<Line>`）、`finalize`（提交成帧）。见 u5-l1。
- **`Preparation`**：整段整形后的产物，包含 `text`（整段字符串）、`items`（带字节区间的整形文本与 item）、`indices`（字节→item 反查表）、`spans`（源码位置映射）。见 u5-l2、u5-l4。
- **`Item` 枚举**：行内元素的六种变体——`Text(ShapedText)`、`Absolute(Abs, weak)`、`Fractional(Fr, ..)`、`Frame`、`Tag`、`Skip`。见 [src/inline/collect.rs:32-46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L32-L46)。
- **`Trim` 与 `Breakpoint`**：断行点三态 `Normal`/`Mandatory`/`Hyphen`，`trim` 给出行尾在「布局轴」与「整形轴」上各自的截断位置（`layout <= shaping`）。见 [src/inline/linebreak.rs:64-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L64-L149)。
- **`Frame` 与 `FrameItem`**：排版结果是一棵 Frame 树，叶子项有 `Text`/`Shape`/`Image`/`Link`/`Tag`/`Group`，`Frame::soft` 是「尺寸可被父级重写」的软帧。见 u2-l3。
- **`ShapedText::build`**：把单个整形文本 run 变成帧，并在有装饰时调用 `decorate`。见 [src/inline/shaping.rs:325-464](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L325-L464)。

一个直觉性的提醒：本讲里反复出现「逻辑顺序（logical）」与「视觉顺序（visual）」的对照。BiDi 重排后，**屏幕上从左到右看到的顺序是视觉顺序**，而**文本在源码/字符串里的顺序是逻辑顺序**。两者在含 RTL 文字时不同。内省（query/counter）永远按逻辑顺序工作，所以建帧时要在视觉排布完成后「再按逻辑顺序排一次」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
|------|------|----------|
| [src/inline/line.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs) | 把一个字节区间装配成可测量的 `Line`，再把 `Line` 提交成单行帧 | 主角一（行构造 + 提交） |
| [src/inline/deco.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs) | 给一段已整形文本添加下划线/上划线/删除线/高亮 | 主角二（装饰与 evade） |
| [src/inline/finalize.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/finalize.rs) | 把若干 `Line` 提交并堆叠成段落 `Fragment` | 主角三（段落组装） |
| [src/inline/shaping.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs) | `ShapedText::build`，在产出文本帧后调用 `decorate` | 装饰的调用方 |
| [src/inline/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs) | 四段管线总调度，`configuration` 产出 `numbering_marker` 等 | 行号 marker 的来源 |

数据流总览：

```
linebreak 产出 Vec<Line>            （每行只有测量信息）
        │
        ├── line.rs::commit(每行)   → 单行 Frame（含装饰、行号 marker）
        │        ↑ 装饰由 shaping.rs::build 内部调 deco.rs::decorate 完成
        │
        └── finalize.rs::finalize   → Fragment（每区域一帧，多行堆叠）
```

---

## 4. 核心概念与源码讲解

### 4.1 行的构造：从字节区间到 `Line`

#### 4.1.1 概念说明

`linebreak` 决定的是「在哪断」，而本节的 `line()` 函数负责「把这一段区间真正装成一个 `Line` 对象」。`Line` 的设计哲学写在它的文档注释里：它**只包含测量信息，不包含最终帧**。这样断行算法可以反复测量不同区间的宽度，而无需为每次试探都付出「建帧」的昂贵代价；只有最终被选中的行才会进入 4.2 的 `commit` 去真正画帧。

一句话：`line()` 是「便宜的可测量视图」，`commit()` 是「昂贵的定稿建帧」。

#### 4.1.2 核心流程

`line()` 接收一个字节 `range`、断点 `breakpoint`、以及前驱行 `pred`，产出 `Line`：

1. 取出该区间的完整文本 `full`。
2. 判定是否两端对齐 `justify`、行尾是哪种连字符/破折号 `dash`。
3. 用 `breakpoint.trim()` 算出行尾裁剪位置 `trim`（布局轴与整形轴分离）。
4. 通过 `collect_items` 收集 item：对每个 BiDi run 调 `collect_range`，必要时按 RTL 反转视觉顺序。
5. 行首补连字符（若前驱行以硬连字符结尾且该语言要求重复）。
6. 行尾补软连字符（若断在软连字符处）。
7. 裁掉行首/行尾的弱间距，处理 CJ（中日韩）标点的避头尾、行尾字形可伸缩性。
8. 求和得到行宽 `width`。

```
range + breakpoint
   │
   ├─ trim ──► Trim{layout, shaping}
   ├─ reorder ──► 视觉顺序的 BiDi runs
   │     └─ collect_range ──► Item（文本则按需 reshape）
   ├─ 行首/行尾连字符
   ├─ trim_weak_spacing / adjust_cj / adjust_glyph_stretch
   └─ width = Σ item.natural_width
```

#### 4.1.3 源码精读

`Line` 结构本身极其精简，四个字段就够了：

[文件 src/inline/line.rs:31-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L31-L41) —— `items`（行内元素集合）、`width`（自然宽度）、`justify`（是否两端对齐）、`dash`（行尾连字符类型）。注意它借用了 `Preparation` 的生命周期 `'a`，所以几乎所有 item 都是**引用**而非拷贝。

`line()` 函数的开头先判定 `justify` 与 `dash`：

[文件 src/inline/line.rs:136-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L136-L149) —— 两端对齐成立当且仅当 `config.justify` 且断点非 `Mandatory`（强制断行不拉伸）；`dash` 区分软连字符（断词产生）、硬连字符（如 `beija-flor` 中的 `-`）、其它破折号（仅影响成本）。

关键点是 **BiDi 重排**。绝大多数纯 LTR 段落没有 BiDi 信息，`reorder` 会直接跳过；但混排时，同一逻辑区间会被切成多个视觉 run，每个 run 单独决定方向：

[文件 src/inline/line.rs:248-279](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L248-L279) —— `reorder` 找到包含本行的段落，调用 `bidi.visual_runs` 得到视觉顺序的子区间列表，对每个子区间回调 `f`，并告知是否 RTL。RTL 段会在 `collect_items` 里调用 `items.reorder(from)` 把该段在视觉上反转。

收集 item 的核心是 `collect_range`，它处理「文本 item 被行边界切开」的情形：

[文件 src/inline/line.rs:300-335](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L300-L335) —— `split` 判定 item 是否被行切开；被切开时调用 `shaped.reshape(engine, sliced)` 只对子区间重新整形（借助 `safe_to_break` 尽量复用，见 u5-l4）；同时按 `trim.layout` 把行尾空白字形的推进量清零（保留字形供复制粘贴，但不占布局宽度，呼应 `Trim` 的双轴设计）。

最后是**逻辑顺序索引** `LogicalIndex`，这是 4.2 提交时「按逻辑排序」的基础：

[文件 src/inline/line.rs:783-796](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L783-L796) —— 行首补的连字符用 `START_HYPHEN = 0`（最早），行尾补的用 `END_HYPHEN = usize::MAX`（最晚），普通 item 用 `from_item_index(i) = i + 1`（留出 0 给行首连字符）。这样无论视觉顺序如何，提交时按 `LogicalIndex` 排序就能恢复源码逻辑顺序。

#### 4.1.4 代码实践

**实践目标**：理解 `line()` 在不同断点下产出的 `dash` 与 `justify` 差异。

**操作步骤**（源码阅读型）：

1. 打开 [src/inline/line.rs:136-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L136-L149)。
2. 假设有这样一段 Typst（开启了 `#set par(justify: true)`）：
   ```
   Critical information is conveyed here.
   ```
   断行器在 "conveyed" 后的空格处断行（`Breakpoint::Normal`）。
3. 手动追踪：`full.ends_with(LINE_SEPARATOR)` 为 false；`p.config.justify` 为 true 且断点不是 `Mandatory` → `justify = true`。
4. `full` 末尾不是软连字符/普通连字符/破折号 → `dash = None`。

**需要观察的现象**：对于 `Normal` 断点，行会被两端对齐（`justify = true`）；而对于 `Mandatory` 断点（如显式 `\` 换行或段落末行），`justify` 恒为 false——这正是为什么段落最后一行不会被拉伸。

**预期结果**：你能用自己的话解释「为什么段落最后一行不两端对齐」——因为它落在 `Mandatory` 断点上。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Line` 故意只存测量信息而不直接存帧？

**参考答案**：断行算法（尤其 `Optimized` 的 Knuth-Plass DP）需要对大量候选区间反复测量宽度以算成本。如果每次测量都建帧，开销会高到不可接受。`Line` 作为「便宜的可测量视图」让测量廉价，只有最终入选的行才在 `commit` 中付建帧代价。

**练习 2**：`LogicalIndex` 为什么用 `i + 1` 而不是 `i` 作为普通 item 的索引？

**参考答案**：索引 `0` 被预留给行首补的连字符（`START_HYPHEN`），所以普通 item 从 `1` 开始编号，保证行首连字符在逻辑顺序中排最前。

---

### 4.2 行的提交 `commit`：对齐、两端对齐与单行帧

#### 4.2.1 概念说明

`commit` 是「昂贵的定稿」：它把一条 `Line` 真正画成一个 `Frame`。这件事比看起来复杂，因为要同时处理四件相互纠缠的事：

1. **悬挂缩进（hanging indent）**与**标点悬挂（overhang）**改变可用宽度。
2. **两端对齐**：行太空时拉伸、太满时压缩，分两步分配多余空间。
3. **基线对齐**：行内每个子帧按各自基线对齐到统一的行基线。
4. **逻辑顺序还原**：把视觉顺序的 item 重新按逻辑顺序排进帧，保证内省正确。

此外，如果段落启用了行号，`commit` 还会在这里挂上 `ParLineMarker` 的 tag。

#### 4.2.2 核心流程

`commit` 的骨架（已省略细节）：

```
remaining = width - line.width - hanging_indent
offset    = 0  （LTR 时先加上 hanging_indent）

1. 标点悬挂：行首/行尾的悬挂标点把 amount 让给 remaining
2. 两端对齐：
     - 太满 (remaining<0) → 用 shrinkability 压缩，justification_ratio ∈ [-1,0]
     - 太空且 justify 且无 Fr → 用 stretchability 拉伸，ratio ∈ [0,1]
                              仍空则按 justifiables 均分 extra_justification
3. 遍历 item 建子帧：Text 调 build(ratio, extra)，Fractional 按 share 分配，Tag 建零尺寸软帧
   过程中累计 top/bottom（行高）
4. 按 LogicalIndex 排序子帧（逻辑顺序）
5. 逐个 push 到 output 帧，x = offset + align.position(remaining)，y = top - baseline
```

两端对齐的空间分配用两个量：`justification_ratio`（第一步，按字形可伸缩性等比拉伸/压缩）与 `extra_justification`（第二步，把剩余空间均分到每个「可对齐字形」）。形式化地：

\[
r_{\text{just}} = \mathrm{clamp}\!\left(\frac{\text{remaining}}{\text{stretchability}},\; -1,\; 1\right),\qquad
e = \frac{\text{remaining}_{\text{after stretch}}}{N_{\text{justifiables}}}
\]

第二步对应 W3C《中文版式需求》的「字间空间扩展」，用于 CJK 整版拉伸。

#### 4.2.3 源码精读

**悬挂缩进与标点悬挂**。LTR 段要把悬挂缩进加到起始偏移；RTL 段则因行宽天然包含它而无需额外处理：

[文件 src/inline/line.rs:494-525](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L494-L525) —— `overhang(glyph.c)` 返回该标点应悬挂进页边的比例（逗号、句号 0.8，连字符 0.55，破折号 0.2 等，见 [src/inline/line.rs:678-693](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L678-L693)）。注意条件 `(line.items.len() > 1 || text.glyphs.len() > 1)`：单字行不悬挂，避免把唯一一个字推出页面。

**两端对齐的两步分配**：

[文件 src/inline/line.rs:536-555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L536-L555) —— 先尝试 `shrinkability`（压缩，`ratio` 为负），再尝试 `stretchability`（拉伸，`ratio` 为正，上限 1.0 防止字形被拉到变形）；若仍有剩余空间且存在可对齐字形，则 `extra_justification = remaining / justifiables`，把空间均摊到每个可对齐字形上。`justifiables()` 会把行尾 CJK 字符排除在外（见 [src/inline/line.rs:56-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L56-L73)），因为 CJK 不靠字间空隙对齐。

**遍历 item 建子帧**：

[文件 src/inline/line.rs:561-607](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L561-L607) —— 闭包 `push` 同时做两件事：把子帧连同当前 `offset` 与 `LogicalIndex` 暂存，并用 `top.set_max(frame.baseline())`、`bottom.set_max(frame.size().y - frame.baseline())` 累计行高。各类 item 分别处理：`Absolute` 只推进偏移、`Fractional` 按 `v.share(fr, remaining)` 分配并调 `layout_box`、`Text` 调 `shaped.build(ratio, extra)`（装饰在此内部完成）、`Tag` 建一个零尺寸软帧装入 tag。

**按逻辑顺序还原并拼装**：

[文件 src/inline/line.rs:614-632](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L614-L632) —— 先 `frames.sort_unstable_by_key(|(_, _, idx)| *idx)` 把视觉顺序的子帧排回逻辑顺序（这一步对内省/计数器至关重要），再用 `align.position(remaining)` 把整行水平定位（左/中/右对齐就是用剩余空间决定整体偏移），垂直方向 `y = top - frame.baseline()` 实现基线对齐。`output` 是一个 `Frame::soft`（软帧），尺寸 `Size::new(width, top + bottom)`，基线设为 `top`。

**基线偏移 `apply_shift`**：处理用户设置的 `baseline` 与上下标带来的水平/垂直补偿（用于上下标错位）：

[文件 src/inline/line.rs:457-483](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L457-L483) —— 注意它接收的不是 `Engine` 而是 `Tracked<dyn World>`，因为只需查字体度量；`scripts.kind`（上下标设置）会读取字体的 `vertical_offset`/`horizontal_offset` 度量，对帧做 `translate`。它只对 `Fractional` 里的 box 调用（见 [src/inline/line.rs:582](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L582)），普通文本的基线偏移已在 `shaped.build` 内部由 `shift` 处理。

**行号 marker**：如果 `config.numbering_marker` 存在，给本行帧挂一对 Start/End tag：

[文件 src/inline/line.rs:644-672](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L644-L672) —— 这里不经过完整的 realize 流程，而是**手工**造一个 `ParLineMarker`、用 `locator.next_location` 给它分配 Location、再用 `TagFlags { introspectable: false, tagged: false }` 包成 Start/End tag 压入帧。注释解释了原因：行号 marker 不需要真正「排版」，只需要在帧里留个可被检索的标记，后续在根 flow 里手动搜索它来显示行号。`pos` 只关心 `y`（对齐到行基线 `top`），`x` 不重要。

#### 4.2.4 代码实践

**实践目标**：理解「视觉顺序建帧、逻辑顺序入帧」对内省的影响。

**操作步骤**（源码阅读型）：

1. 打开 [src/inline/line.rs:622-632](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L622-L632)。
2. 阅读注释：「Ensure that the final frame's items are in logical order rather than in visual order. This is important because it affects the order of elements during introspection and thus things like counters.」
3. 想象一个混排行 `مرحبا World`（RTL 阿语 + LTR 英文）：BiDi 重排后视觉顺序是「阿语段（反向）+ 英文段」，但 `counter.visit` 必须按源码顺序遍历。

**需要观察的现象**：如果删掉 `frames.sort_unstable_by_key` 这一行（**不要真删源码，只在脑中模拟**），含 RTL 文本的段落在做 `counter` 查询时会出现什么？

**预期结果**：计数器/查询会按视觉顺序而非源码顺序解析，导致 `query`、`outline` 等内省结果错乱。这就是为什么必须排序——**建帧时视觉排布是为了眼睛，入帧时逻辑排序是为了内省**。待本地验证：可构造一个含 BiDi 与 label 的文档，对照内省顺序。

#### 4.2.5 小练习与答案

**练习 1**：两端对齐时，`justification_ratio` 与 `extra_justification` 分别解决什么问题？

**参考答案**：`justification_ratio` 利用字形的 `stretchability`/`shrinkability`（字体定义的可伸缩区间）按比例拉伸或压缩每个字形，是「第一步」；当第一步后仍有剩余空间，`extra_justification` 把余量均分到每个「可对齐字形」（`justifiables`），是「第二步」，主要用于 CJK 整版等比拉开。

**练习 2**：为什么行号 marker 用 `introspectable: false`？

**参考答案**：marker 本身不是用户可见的文档元素，不应出现在 `query` 结果里；它只是给根 flow 一个「可被检索的锚点」，用来定位每行以便绘制行号。所以它需要 Location（合法的 tag），但不需要进内省索引。

---

### 4.3 文本装饰 `deco.rs`：下划线如何「绕开」降部

#### 4.3.1 概念说明

下划线、上划线、删除线、高亮是四种文本装饰。其中前三种是「一条线」，高亮是「一块背景矩形」。它们都在 `ShapedText::build` 内部、文本帧产出后由 `decorate` 添加（见 [src/inline/shaping.rs:449-457](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L449-L457)）。

最有趣的细节是 **evade（规避）**：当下划线穿过带降部的字母（如 `g`、`y`、`p`）时，理想效果是下划线在降部处断开、绕过去，而不是直接横穿字形。`evade: true`（下划线/上划线默认）会计算下划线与每个字形轮廓的交点，把一条长线切成多段；`evade: false`（删除线默认）则画一条连续直线。

#### 4.3.2 核心流程

`decorate` 按装饰类型分流：

```
若 Highlight → 画背景矩形（determine_edges 算高度），prepend 到帧，return
否则（Underline/Overline/Strikethrough）：
   offset = 用户 offset 或字体度量位置；stroke = 用户 stroke 或字体粗细
   gap_padding = 0.08 * size；min_width = 0.162 * size
   start/end = 文本左右各扩 extent

   if !evade:
       画一整段直线（background 决定前/后景），return

   if evade:
       构造一条水平参考线 Line（在 offset 高度）
       for 每个字形:
           取字形轮廓 Bezier 路径（BezPathBuilder + ttf outline_glyph）
           仅当参考线穿过字形 bbox 时，求路径各段与参考线的交点
       交点排序后，相邻交点之间若够宽（> gap_padding）则画一段
```

几何上，evade 把「一条线」变成了「若干段避开字形轮廓的短线」：

\[
\text{可见段} = \bigcup_{i}\,[\,x_i + p,\; x_{i+1} - p\,],\quad x_i < x_{i+1} - p
\]

其中 \(p\) 是 `gap_padding`，\(x_i\) 是排序后的交点（含起止端点）。

#### 4.3.3 源码精读

`decorate` 的签名与高亮分支：

[文件 src/inline/deco.rs:13-36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L13-L36) —— 高亮走完全不同的路径：用 `determine_edges` 按 `top_edge`/`bottom_edge` 算出文本上下边界（逐字形取最大，见 [src/inline/deco.rs:139-159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L139-L159)），构造一个 `styled_rect`（带圆角/描边/填充的矩形），用 `frame.prepend_multiple` 压到帧**最底层**（背景），然后 `return`。注意 `origin.y = pos.y - top - shift`，所以高亮贴合字形顶边而非基线。

线型装饰的参数解析：

[文件 src/inline/deco.rs:38-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L38-L55) —— `Strikethrough` 强制 `evade = false`（删除线本就要穿过字形），`Underline`/`Overline` 取用户 `evade` 值。`offset` 缺省时用字体度量位置（`metrics.position`），`stroke` 缺省时用字体粗细（`metrics.thickness`）+ 文本填充色——这就是为什么 `#text(fill: red, underline[...])` 的下划线会自动变红。

画线段的闭包与 `!evade` 快速分支：

[文件 src/inline/deco.rs:63-81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L63-L81) —— `push_segment(from, to, prepend)` 画一段从 `from` 到 `to` 的水平线；`target.x >= min_width || !evade` 保证 evade 模式下过短的段不画、而非 evade 模式下无论如何都画。`prepend` 由 `background` 决定：`background: true` 时压到文本之下（prepend），否则画在文本之上（push）。`!evade` 时直接画一整段返回。

**evade 的核心：字形轮廓求交**：

[文件 src/inline/deco.rs:83-117](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L83-L117) —— 先构造水平参考线 `line`（在 `offset` 高度），再对每个字形：用 `BezPathBuilder`（实现 `ttf_parser::OutlineBuilder`，见 [src/inline/deco.rs:192-212](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L192-L212)）把字形的 TrueType 轮廓转成 `kurbo::BezPath`；**只有当参考线落在字形 bbox 的 y 范围内**时（`intersect` 判断）才做昂贵的线段求交，把所有交点的 x 坐标收进 `intersections`。注意 `y_min`/`y_max` 用了 `-bbox.y_max`/`-bbox.y_min`，因为字体坐标系 y 轴向上、而排版坐标系 y 轴向下。

**交点排序后切段**：

[文件 src/inline/deco.rs:119-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L119-L135) —— 先把起止点（带 `gap_padding` 外延）加入交点集，排序后用 `windows(2)` 取相邻交点对：若两点间距 > `gap_padding`，则在 `[l + gap_padding, r - gap_padding]` 画一段。这样下划线就会在降部两侧各留一点空隙，视觉上「绕开」了字形。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲眼对比 `evade: true`（默认）与 `evade: false` 下，下划线在带降部字母处的绘制差别，并理解 `decorate` 的切段逻辑。

**操作步骤**：

1. 阅读测试文件 [tests/suite/text/deco.typ](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/text/deco.typ)，注意第 13 行已经有一个对照案例：
   ```
   // Different color.
   #underline(stroke: red, evade: false)[Critical information is conveyed here.]
   ```
   它显式关闭了 evade。而其它 `#underline[...]` 用默认 `evade: true`。
2. 自己写一个最小对比文档（**示例代码，非项目原有**）：
   ```typ
   #underline[playing with glyphs like g, y, p]
   #underline(evade: false)[playing with glyphs like g, y, p]
   ```
3. 在 typst 仓库根目录运行（仅当你本地有工具链时；否则跳到步骤 4）：
   ```
   cargo run -- compile 对比.typ
   ```
   打开生成的 PDF 放大观察 `g`、`y`、`p` 下方的下划线。
4. 对照源码 [src/inline/deco.rs:119-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L119-L135)：第一行（evade）的下划线应在每个降部字母处断成多段、两侧各留 `gap_padding`（约 `0.08 × 字号`）的空隙；第二行（`evade: false`）则是穿过字形的一条连续红线。

**需要观察的现象**：
- evade 开启时，下划线在 `g`/`y`/`p` 的降部圆圈处断开，看起来像「绕了过去」。
- evade 关闭时，下划线直接横穿这些降部。
- 颜色方面：第 16 行 `#text(fill: red, underline[...])` 的下划线自动继承红色，对应 [src/inline/deco.rs:52-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L52-L55) 中 stroke 缺省时取 `text.fill.as_decoration()`。

**预期结果**：你能指着 PDF 解释「这段下划线为什么在 g 这里断了」——因为 `decorate` 求出了下划线参考线与 g 的字形轮廓的交点，并按 `gap_padding` 把相邻交点之间的可见段切了出来。若无法本地运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么删除线（`Strikethrough`）强制 `evade = false`，而下划线默认 `evade = true`？

**参考答案**：删除线的设计意图就是横穿字形中部（划掉文字），所以必须画连续直线；下划线位于基线下方，若穿过降部圆圈会与字形粘连、不美观，所以默认断开绕行。

**练习 2**：`decorate` 里 `intersect` 判断（`offset >= y_min && offset <= y_max`）的作用是什么？去掉它会怎样？

**参考答案**：它是一个廉价预筛——只有当装饰线的高度 `offset` 落在字形 bbox 的纵向范围内时，才去做昂贵的 Bezier 线段求交。去掉后会对每个字形都做求交，性能下降，但结果不变（因为不在 bbox 内的字形本来就没有交点）。

---

### 4.4 段落组装 `finalize.rs`：把行堆叠成段落

#### 4.4.1 概念说明

`finalize` 是四段管线的最后一步，但它**出奇地短**——只有二十多行。原因是：所有重活（建帧、对齐、装饰、行号）都已在 4.2 的 `commit` 里做完了。`finalize` 只剩两件事：

1. **决定段落宽度**：是撑满区域宽度，还是收缩到最宽行的宽度。
2. **逐行 `commit` 并堆叠**成一个 `Fragment`。

之所以「逐行 commit」放在这里而不是 `linebreak` 里，正是为了贯彻 4.1 的设计：`linebreak` 只产测量信息，`finalize` 才触发定稿建帧。

#### 4.4.2 核心流程

```
finalize(engine, p, lines, region, expand, locator) -> Fragment:
    1. 算段落宽度 width：
         - 区域无限宽 或 (不 expand 且无 Fr 间距) → 收缩到 hanging_indent + max(line.width)
         - 否则 → 用 region.x（撑满）
    2. 对每行调 commit(engine, p, line, width, region.y, locator) → Frame
    3. Fragment::frames(frames)
```

关键判据是一个「撑满 vs 收缩」的选择：

\[
\text{width} = \begin{cases}
\min(\text{region.x},\;\text{hanging\_indent} + \max_i \text{line}_i.\text{width}) & \text{若区域无限宽，或不 expand 且无 Fr}\\
\text{region.x} & \text{否则}
\end{cases}
\]

当段落里含 `Fr`（分数）间距（如 `#h(1fr)`）时，必须撑满才能让分数间距有意义；反之纯文本段落会收缩到最宽行，避免行右端拖一条多余空白。

#### 4.4.3 源码精读

`finalize` 全文：

[文件 src/inline/finalize.rs:8-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/finalize.rs#L8-L35) —— 标注了 `#[typst_macros::time]`（纳入排版耗时统计）。宽度判定见 [src/inline/finalize.rs:18-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/finalize.rs#L18-L27)：`lines.iter().all(|line| line.fr().is_zero())` 判断本段是否完全不含分数间距。随后 [src/inline/finalize.rs:30-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/finalize.rs#L30-L34) 用 `lines.iter().map(|line| commit(...))` 逐行建帧，`.collect()` 成 `Vec<Frame>` 后交给 `Fragment::frames`。

注意 `commit` 接收的是 `region.y`（整区域高度）作为 `full` 参数，而不是当前行的高度——这是因为行内的 `Fractional` box（如行内 `#box` 配 `1fr`）可能需要按整区域高度排版。

需要强调：**`finalize` 产出的 `Fragment` 只对应「一个区域」**。段落跨多行/多区域的断裂不是 inline 的事，而是上层 flow 的职责（见 u4 系列）。inline 永远只吃单个 `Size`。

行号 marker 的来源在管线总调度里：

[文件 src/inline/mod.rs:221-231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L221-L231) —— `configuration` 从 `ParLine::numbering` 读取行号格式，若非空就构造一个 `ParLineMarker`（打包了 numbering、number_align、number_margin、number_clearance），存进 `config.numbering_marker`。这个 marker 随后在每个 `commit` 里被 `add_par_line_marker` 挂成 tag（见 4.2.3）。`number_clearance` 故意延后解析（存原始值），避免字号变化导致间距不一致。

#### 4.4.4 代码实践

**实践目标**：验证「撑满 vs 收缩」宽度判定，并跟踪行号 marker 的挂载链路。

**操作步骤**（源码阅读型）：

1. 打开 [src/inline/finalize.rs:18-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/finalize.rs#L18-L27)。
2. 场景 A：一段普通文字 `Hello world.`，无 `Fr`，页面宽 400pt，`expand = true`。手动判定：`!expand` 为假，且 `all(|line| line.fr().is_zero())` 为真——但条件是 `(!expand && ...)`，整体为假，走 else 分支 → `width = region.x = 400pt`（撑满）。
3. 场景 B：同样文字，但 `expand = false`。此时 `(!expand && fr.is_zero())` 为真 → `width = min(400, hanging_indent + max_line_width)`，即收缩到最宽行。
4. 跟踪行号：在文档里写 `#set par.line(numbering: "1")`，它会经 [mod.rs:221-231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L221-L231) 生成 `numbering_marker`，在每行 [line.rs:618-620](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L618-L620) 被 `add_par_line_marker` 挂成 tag。

**需要观察的现象**：
- 场景 A 与 B 的差别揭示了：即使 `expand = true`，纯文本段落也会被画成整宽（但行内多余空间由 `align.position(remaining)` 决定是左对齐留白还是两端对齐消化）。
- 行号 marker 的 tag 带的是 `introspectable: false`，所以不会污染 `query`，仅供 flow 层手动检索。

**预期结果**：你能复述 marker 的完整路径：`ParLine::numbering`（样式）→ `configuration`（构造 `ParLineMarker`）→ `commit`/`add_par_line_marker`（挂 tag）→ 根 flow 检索并绘制。待本地验证场景 A/B 的实际帧宽。

#### 4.4.5 小练习与答案

**练习 1**：为什么段落里含 `#h(1fr)` 时，`finalize` 必须用 `region.x` 而不能收缩？

**参考答案**：`Fr` 是分数间距，其物理宽度 = `剩余空间 × 分数占比`。如果段落收缩到最宽行，就没有「剩余空间」可分，`1fr` 会变成 0 宽。所以只要有 `Fr`，就必须撑满区域宽度，让 `commit` 里的 `v.share(fr, remaining)` 能算出有意义的空间。

**练习 2**：`finalize` 为什么把「逐行 commit」放在自己这里，而不是在 `linebreak` 里就建好帧？

**参考答案**：断行算法需要反复测量候选区间的宽度来算成本，`Line` 作为「只含测量信息的便宜视图」让测量廉价；只有最终入选的行才该付建帧代价。把 `commit` 放在 `finalize`，保证了「测量」与「定稿」的清晰分离，避免断行阶段产生大量一次性帧的开销。

---

## 5. 综合实践

把本讲四个模块串起来：跟踪一条带装饰、两端对齐、且开了行号的行，从「字节区间」到「最终入帧」的完整旅程。

**任务**：给定下面这段 Typst（**示例代码**）：

```typ
#set par(justify: true, line(numbering: "1"))
#set underline(stroke: red)
This is #underline[playing] with glyphs.
```

请按顺序回答并对照源码验证：

1. **构造**（4.1）：第一行断在哪个断点？`justify` 与 `dash` 各为何值？引用 [line.rs:136-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L136-L149)。
2. **装饰触发**（4.3）：`playing` 这段文本的红色下划线是在哪里、由谁调用 `decorate` 画的？引用 [shaping.rs:449-457](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L449-L457) 与 [deco.rs:38-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L38-L55)。注意 `p`、`g` 处会触发 evade 切段（[deco.rs:119-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L119-L135)）。
3. **提交**（4.2）：两端对齐时 `justification_ratio` 走拉伸还是压缩？为什么最终帧的子项要按 `LogicalIndex` 排序？引用 [line.rs:536-555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L536-L555) 与 [line.rs:622-632](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L622-L632)。
4. **行号**（4.2 + 4.4）：行号 marker 从哪个样式字段来，经过哪两个函数最终变成帧里的 tag？引用 [mod.rs:221-231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L221-L231) 与 [line.rs:644-672](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L644-L672)。
5. **组装**（4.4）：整段无 `Fr`，`finalize` 在 `expand = true` 与 `expand = false` 两种情况下宽度分别取何值？引用 [finalize.rs:18-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/finalize.rs#L18-L27)。

**预期产出**：一张标注了「调用点 → 源码行号 → 做了什么」的清单。完成后，你就把 inline 子系统的四段管线从输入到输出完整走过了一遍。

## 6. 本讲小结

- `line()` 把字节区间装配成**只含测量信息**的 `Line`（`items`/`width`/`justify`/`dash`），便宜且可反复测量；真正的建帧推迟到 `commit`。
- `Line` 借 `LogicalIndex`（行首连字符 `0`、普通 `i+1`、行尾连字符 `MAX`）记录逻辑顺序，提交时按它排序，保证内省/计数器按源码顺序而非视觉顺序工作。
- `commit` 是「昂贵的定稿」：处理悬挂缩进与标点悬挂（overhang）、两端对齐两步分配（`justification_ratio` + `extra_justification`）、基线对齐（`top`/`bottom` 取 max），并把行号 marker 手工挂成 `introspectable: false` 的 tag。
- `decorate` 区分高亮（背景矩形）与线型装饰；下划线/上划线的 `evade` 通过求装饰线与字形轮廓 Bezier 的交点，把一条长线切成避开降部的若干段，删除线则强制连续。
- `finalize` 极短：只决定段落宽度（撑满 `region.x` vs 收缩到最宽行，含 `Fr` 时必须撑满），再逐行 `commit` 堆成 `Fragment`；inline 只吃单个 `Size`，跨区域断裂属 flow 职责。

## 7. 下一步学习建议

本讲结束了 inline（行内段落）单元。建议接下来：

- **横向串读**：回到 [src/inline/mod.rs:153-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L153-L178) 的 `layout_inline_impl`，从 `collect` 到 `finalize` 完整走一遍，确认四段管线在脑中闭环。
- **向上走**：进入 u6 单元（网格/数学/栈/列表/图形等专用 layouter），看它们如何回调 `layout_fragment`/`layout_frame`——这些容器型 layouter 会把 inline 当作「孩子排版原语」来用。
- **深化装饰**：如果想更深入 evade 的几何，建议阅读 `kurbo` crate 的 `BezPath::segments` 与 `intersect_line` 文档，以及 `ttf_parser::OutlineBuilder` trait，理解 TrueType 轮廓如何变成 Bezier 路径。
- **行号端到端**：行号 marker 在 inline 层只是「挂 tag」，真正的「检索并绘制」发生在根 flow 层。学完 u6/u7 后可回过头追踪这条链路，理解 inline 与 flow 的边界。
