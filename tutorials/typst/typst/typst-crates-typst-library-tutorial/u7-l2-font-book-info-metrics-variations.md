# 字体管理：FontBook、FontInfo、metrics 与 variations

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 Typst 如何在「不读字体文件」的前提下完成字体发现与匹配，即 `FontBook` 的作用与内部结构。
- 说清一条字体匹配的完整链路：family → variant → fallback，以及 `select` / `select_fallback` 内部的打分逻辑。
- 理解 `FontInfo` 如何从 OpenType/TrueType 文件里抽取族名、变体（style/weight/stretch）、标志位与覆盖码点。
- 掌握 `FontMetrics` 提供的基线度量（ascender/cap_height/x_height 等）与数学常量的来源。
- 解释变量字体（variable font）的轴（`wght`/`wdth`/`ital`/`slnt`/`opsz`）是如何由 `FontVariations::resolve` 从 `FontVariant` 与字号推导出来的。

本讲是 u7-l1（`TextElem` 与字体变体）的延续。u7-l1 讲了「用户样式如何决定一个 `FontVariant`」，本讲回答「拿着这个 variant，Typst 怎么在字体集合里挑出最合适的字体并实例化它」。

## 2. 前置知识

阅读本讲前，最好已经了解：

- **OpenType/TrueType 字体的基本概念**：一个字体文件里有字形（glyph）、度量（metrics）、命名表（name table）和（可选的）变量轴（variation axes）。
- **字体族（family）与变体（variant）的区别**：「Noto Sans」是一个族；族下还能区分 Regular / Bold / Italic 等多个「面（face）」，区分它们的属性就是 `FontVariant`（style + weight + stretch）。
- **变量字体（variable font）**：单个文件内置若干「轴」，例如 `wght` 轴可在 100–900 间连续取值，于是「一份数据」能表达原本需要多个静态字体文件才能覆盖的字重。
- **u7-l1 的结论**：`TextElem` 几乎全是 ghost 字段，`variant()` 综合用户可见的 `style/weight/stretch` 与内部的 `delta/emph`，产出一个 `FontVariant`。本讲就从「拿到 `FontVariant`」这一步往下走。

> 名词约定：本讲把「字体集合里的一个条目」称为**面（face）**，对应一个 `FontInfo`；把「带上了具体变体坐标的、可参与排版的字体」称为**实例（instance）**，对应一个 `FontInstance`。

## 3. 本讲源码地图

本讲全部位于 `src/text/font/` 子模块内：

| 文件 | 作用 |
| --- | --- |
| [src/text/font/book.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs) | `FontBook`：字体集合的元数据索引，负责「发现」与「匹配」，打分挑出最合适的面。 |
| [src/text/font/info.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/info.rs) | `FontInfo`：单个面的元数据；从 ttf 文件抽取族名、变体、标志位、轴与 `Coverage`。 |
| [src/text/font/metrics.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/metrics.rs) | `FontMetrics`：基线度量（ascender/x_height 等）与数学排版常量 `MathConstants`。 |
| [src/text/font/variations.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs) | `FontAxis` / `FontVariations` / `StandardAxes`：变量字体轴的描述与「从 variant+字号推导坐标」的逻辑。 |
| [src/text/font/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs) | `Font` / `FontInstance`：真正的字体句柄；`instantiate` 把坐标烘焙进去。 |
| [src/text/font/variant.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs) | `FontVariant` / `FontWeight` / `FontStretch` / `FontStyle`：匹配打分所需的「距离」方法。 |

一个贯穿全讲的分工要点（u5-l1 已点明）：**发现（discovery）与加载（loading）是分离的**。`World::book()` 只返回轻量的 `FontBook`（里面全是 `FontInfo` 元数据，不含字体字节）；真正需要排版时，编译器才用 `World::font(id)` 按索引惰性加载 `Font`。所以 `FontBook` 可以又轻又快地反复查询。

## 4. 核心概念与源码讲解

### 4.1 FontBook：字体发现与匹配引擎

#### 4.1.1 概念说明

`FontBook` 是「字体集合的目录与匹配引擎」。它回答两类问题：

1. **发现**：系统里都装了哪些字体族？某个族是否存在？
2. **匹配**：给定一个族名 + 一个目标 `FontVariant`，挑出最接近的那一个面。

它只存 `FontInfo` 元数据，**不存字体字节**，所以极轻。这一点从它的字段就能看出——只有一张「族名 → 面索引」的映射表和一个 `FontInfo` 数组：

[src/text/font/book.rs:L13-L18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L13-L18) —— `FontBook` 的两个字段：族名索引（键已转小写）与元数据数组。

注意族名键是 **lowercase** 的（`info.family.to_lowercase()`），因此 `select` 的文档注释也特意提醒「The `family` should be all lowercase」——这是大小写不敏感匹配的实现方式。

#### 4.1.2 核心流程

匹配分三种入口，最终都汇入同一个打分函数 `find_best_variant`：

```text
select(family, variant)          // 给定族名，在该族内挑最接近 variant 的面
   └─> find_best_variant(None, variant, 族内的 ids)

select_family(family)            // 只关心族，返回该族全部面（用于「族是否存在」/补全）
   └─> 直接返回 families[family] 的索引切片

select_fallback(like, variant, text)  // 兜底：找一个能渲染 text 的面
   ├─ 取 text 第一个非空白、非 ignorable 字符 c
   ├─ 过滤出 coverage 包含 c 的所有面
   └─> find_best_variant(like, variant, 这些 ids)
```

而 `find_best_variant` 的打分逻辑，是本模块最精巧的部分。它对每个候选面算一个三元组分数，**取分数最高的那个**：

[src/text/font/book.rs:L150-L154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L150-L154) —— 三元组分数 `(相似度, Reverse(距离), 是否变量字体)`，逐元素字典序比较。

分数的三层优先级（从高到低）：

1. **相似度 `similarity`**：仅在做 fallback 时（`like` 非空）才起作用——希望兜底字体与「原字体」风格接近（同为等宽、同为衬线、族名共享前缀词更多、族名更短更「普通」）。
2. **距离 `distance` 的逆**：与目标 `variant` 的距离越小越好，用 `Reverse` 翻转后「越大越好」。
3. **是否变量字体**：平局时优先变量字体（因为变量字体更灵活，更可能精确满足需求）。

距离 `distance` 本身又是一个三元组，**style 最重要、其次 stretch、最后 weight**：

[src/text/font/book.rs:L200-L225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L200-L225) —— 三个分量组成 `(style_distance, stretch_distance, weight_distance)`。

这里有个关键的「变量字体友好」设计：对静态字体，距离就是它与目标 variant 的差；但若该面有对应的轴（`axes.wdth` / `axes.wght` / `axes.ital` / `axes.slnt`），则距离按「**目标落在轴区间内则距离为 0**」计算。换言之，一个覆盖 200–700 字重的变量字体，在匹配 100–730 字重的目标时几乎总能拿到 0 距离，因此在同等条件下会胜出。这正是 `find_best_variant` 测试里「变量字体被优先选择」的来源（见 4.1.4）。

#### 4.1.3 源码精读

**① 注册：族名 → 面索引**

[src/text/font/book.rs:L41-L46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L41-L46) —— `push` 把一条 `FontInfo` 追加进数组，并把（小写化的）族名登记到映射表，值是该面的下标。

**② 三个查询入口**

[src/text/font/book.rs:L75-L78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L75-L78) —— `select`：查族名得到面索引列表，交给打分函数。

[src/text/font/book.rs:L81-L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L81-L88) —— `select_family`：直接返回该族全部面索引，不做打分。`check_font_list`（见下）就用它来判断「族是否存在」。

[src/text/font/book.rs:L94-L115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L94-L115) —— `select_fallback`：先按第一个有效字符的 `coverage` 过滤候选面，再走打分。`like` 参数让兜底字体尽量贴近原字体。

**③ 打分：相似度与距离**

[src/text/font/book.rs:L168-L185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L168-L185) —— `similarity`：`(同为等宽, 同为衬线, 族名共享前缀词数, Reverse(族名长度))`。注释举了个例子：原字体是「Noto Sans」时，候选「Noto Sans Arabic」共享 2 个前缀词，胜过「IBM Plex Arabic」；而两个等优时选族名更短（更「不特殊」）的那个。

[src/text/font/book.rs:L229-L234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L229-L234) —— `shared_prefix_words`：逐词比对两族名前缀，统计连续相等的词数。

**④ 距离的「轴感知」**

[src/text/font/book.rs:L211-L223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L211-L223) —— 若面有 `wdth`/`wght` 轴，用 `axis.distance(...)`（4.4 节会讲，落在区间内为 0）；否则退化为静态 variant 的 `distance`。对 style 还会检查 `ital`/`slnt` 轴，使变量字体即便默认实例是 Normal，也能「宣称」可提供斜体/倾斜，从而缩小 style 距离。

**⑤ 上游调用点（位于 typst-layout）**

打分选出面索引后，谁来加载并实例化？匹配的真正调用方在 `typst-layout` 的 shaping 流程里（cross-crate，仅供理解链路）：

- `crates/typst-layout/src/inline/shaping.rs:599`：遍历样式链上的字体族，逐个 `book.select(family, variant)`，命中后 `world.font(id).instantiate(variant, size, &variations)`。
- `crates/typst-layout/src/inline/shaping.rs:593`：族都耗尽且开启 fallback 时，转而调用 `book.select_fallback(...)`。

而在本 crate 内，`TextElem` 的构造阶段会用 `select_family` 做一次**预警检查**：

[src/text/mod.rs:L1577-L1588](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1577-L1588) —— `check_font_list`：若用户写的字体族在 `FontBook` 里一个面都没有，就报一条 warning（`unknown font family`）。这是「发现」能力对用户的直接暴露。

#### 4.1.4 代码实践

**实践目标**：通过阅读单元测试，验证「变量字体在打分中优先于静态字体」这一结论。

**操作步骤**：

1. 打开 [src/text/font/book.rs:L244-L289](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L244-L289) 的 `test_find_best_variant`。
2. 阅读测试数据：`s0..s6` 是 7 个**静态**面（无轴），`v0`/`v1` 是两个**变量**面（`v0` 有 `wght`(200–700) 与 `ital`(0–1) 轴；`v1` 有 `slnt`(-40–40) 与 `wdth`(70–120) 轴）。
3. 对照断言表，理解每一行为什么选了那个面。

**需要观察的现象**（结合 `distance` 的轴感知）：

- `pick(Normal, 100, 100.0) == v0`：目标字重 100 低于 `v0` 的 wght 轴下限 200，`v0` 的轴距离是 `|200-100| = 100`；而静态面 `s0`(400) 的距离是 `|400-100| = 300`。`v0` 距离更小，于是在第二优先级（距离）上直接胜出——这里甚至用不到第三优先级的「变量」平局打破。

  > 关键点：即便目标落在轴范围之外（距离不为 0），变量字体仍按「到轴边界的距离」算分，往往比静态面的固定差距小。
  
- `pick(Normal, 760, 100.0) == s2`：目标 760 超出 `v0` wght 轴上限 700，距离为 60；而静态 `s2`(800) 距离仅 40。静态面距离更小，于是在第二优先级反超——**说明变量字体并非无条件胜出，距离才是主因**。
- `pick(Oblique, 400, 100.0) == v1`：`v1` 有 `slnt` 轴，能提供 oblique，距离 0；`v0` 只有 `ital` 轴没有 `slnt`，对 oblique 距离更大。故选 `v1`。

**运行方式**：在仓库根目录执行（待本地验证，因依赖完整 cargo 环境）：

```bash
cargo test -p typst-library --lib text::font::book::tests::test_find_best_variant -- --nocapture
```

**预期结果**：测试通过，说明你对打分优先级的理解与源码一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FontBook` 用 `BTreeMap<String, Vec<usize>>` 而不是 `HashMap`？

> **参考答案**：`BTreeMap` 按键有序，`families()` 迭代时族名顺序确定（便于稳定的 IDE 补全与文档列出）；同时 `FontBook` 派生了 `Hash`，有序结构更利于确定性哈希与 comemo 增量缓存。

**练习 2**：在 `find_best_variant` 的分数里，`Reverse(distance)` 中的 `Reverse` 起什么作用？

> **参考答案**：分数是「越大越好」，而距离是「越小越好」。用 `Reverse` 把距离翻转，使「距离更小」映射成「Reverse 值更大」，从而统一进同一个取最大值的比较里。

**练习 3**：`select_fallback` 为什么要取「第一个非空白、非 default-ignorable 字符」而不是直接用第一个字符？

> **参考答案**：空白和 default-ignorable 字符（如零宽空格、BOM）几乎被所有字体覆盖，拿它们去筛选会选不出有意义的兜底字体。用第一个「真正需要字形」的字符，才能挑到确实能渲染这段文字的面。

---

### 4.2 FontInfo：从字体文件抽取元数据

#### 4.2.1 概念说明

`FontInfo` 是「一个面的身份证」。它由 `FontInfo::from_ttf` 在解析字体文件时一次性抽取，之后只读地存放在 `FontBook` 里。它包含五项：

[src/text/font/info.rs:L13-L26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/info.rs#L13-L26) —— `family`（族名）、`variant`（默认实例的变体）、`flags`（MONOSPACE/SERIF/MATH/VARIABLE）、`axes`（变量轴）、`coverage`（码点覆盖集）。

要特别注意一点（注释也强调了）：**对变量字体，`variant` 描述的是它的「默认实例」**。也就是说，一个变量字体的 `variant` 只是它在出厂坐标下的样子；它的真实能力还要看 `axes`。这正是 4.1 里「距离」要兼顾 `axes` 的原因。

`FontInfo` 不存字体度量（ascender/x_height 等）——那些在 `FontMetrics` 里，且依赖具体变体坐标，要等到 `FontInstance` 阶段才算（见 4.3）。这条边界很重要：`FontInfo` 只存「不随坐标变化」的元数据。

#### 4.2.2 核心流程

`from_ttf` 的抽取流程，按字段顺序：

```text
1. 查 PostScript 名 → 查例外表 find_exception（修正错误标注的字体）
2. family：优先用例外的 family，否则读 name 表 ID=1，再 typographic_family() 剥离样式后缀
3. variant：
   ├─ style：优先例外，否则综合 ttf.style() + 全名关键词 (italic/oblique/slanted)
   ├─ weight：优先例外，否则 ttf.weight().to_number()
   └─ stretch：优先例外，否则 ttf.width().to_number()
4. coverage：遍历 cmap 的 unicode 子表，收集全部码点 → Coverage::from_vec
5. flags：MONOSPACE/MATH/VARIABLE 直接取；SERIF 用 OS/2 的 PANOSE 字节判定
6. axes：读 fvar 的 variation_axes
```

其中第 5 步判定「是否衬线」用了一个有点 hacky 的方法——读 OS/2 表偏移 32..45 的 PANOSE 字节：

[src/text/font/info.rs:L131-L138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/info.rs#L131-L138) —— PANOSE 第一字节为 2（拉丁文本）、第二字节在 2..=10（带衬线样式）即判定为 SERIF。这是 fallback 匹配时 `similarity` 判断「同为衬线」的依据。

#### 4.2.3 源码精读

**① 族名规范化 `typographic_family`**

[src/text/font/info.rs:L206-L267](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/info.rs#L206-L267) —— 这是族名处理的核心。注释解释了**为什么不用 Name ID 16「Typographic Family」**：对某些字体，它会把超出 Style/Weight/Stretch 的变体（如 Noto 的 Display 变体）也合并进同一个族，导致部分变体在 Typst 里无法被单独选中。所以 Typst 改用 Name ID 1「Family」并**主动剥离样式后缀**（如 `ExtraBold`、`Semicondensed`、`Variable` 等）。

剥离逻辑用 `SUFFIXES`（样式后缀）+ `MODIFIERS`（修饰词 extra/semi/...）+ `SEPARATORS`（空格/连字符/下划线）反复循环裁剪，测试用例很直观：

[src/text/font/info.rs:L357-L372](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/info.rs#L357-L372) —— 如 `"Noto Sans Semicondensed Heavy"` → `"Noto Sans"`；`"Font Ultra Bold"` → `"Font"`（Ultra 是 modifier，Bold 是 suffix）；而 `"times new roman"` 因不含样式后缀，原样保留。

**② style 的综合判定**

[src/text/font/info.rs:L80-L103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/info.rs#L80-L103) —— 故意**不用** `ttf.is_italic()`（它会检查斜体角，对某些 oblique 字体误报），改为综合 `ttf.style()` 与全名里是否含 `italic`/`oblique`/`slanted` 关键词。注释引用了 issue #7479 作为这一调整的依据。

**③ 覆盖集 `Coverage`：游程编码**

`Coverage` 是本模块里一个值得专门看的「紧凑数据结构」。它用**交替游程（run-length）**编码一个码点集合：奇数位记录「不在集合内的连续个数」，偶数位记录「在集合内的连续个数」。

[src/text/font/info.rs:L269-L287](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/info.rs#L269-L287) —— 文档注释给了一个例子：集合 `{2,3,4,9,10,11,15,18,19}` 编码成 `[2,3,4,3,3,1,2,2]`。

这样设计的好处是：一个字体往往覆盖若干大段连续码点（如整个基本多文种平面的一块块区间），游程编码后体积远小于位图或哈希集合，且 `contains` 只需线性扫描这些游程：

[src/text/font/info.rs:L318-L331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/info.rs#L318-L331) —— `contains` 边走游程边翻转 `inside` 标志，命中区间即返回。`select_fallback` 正是用它来筛「能渲染这个字符的面」。

**④ 例外表 `exceptions`**

[src/text/font/exceptions.rs:L5-L7](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/exceptions.rs#L5-L7) 与 [src/text/font/exceptions.rs:L45-L60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/exceptions.rs#L45-L60) —— 一张编译期 `phf` perfect-hash 表，按 PostScript 名覆盖某些字体的错误元数据（如老版 Arial-Black 的 weight 被错标为 400，实应为 900）。`from_ttf` 一开始就先查这张表，命中则用人工修正值替代文件里的值。

#### 4.2.4 代码实践

**实践目标**：通过阅读 `typographic_family` 的测试，理解族名后缀剥离的边界条件。

**操作步骤**：

1. 打开 [src/text/font/info.rs:L356-L372](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/info.rs#L356-L372) 的 `test_trim_styles`。
2. 逐条解释每个断言。重点看这两条「反例」：
   - `"times new roman"` → `"times new roman"`（`roman` 不在 SUFFIXES 里，所以不裁剪）。
   - `"Font Ultra"` → `"Font Ultra"`（只有 modifier `Ultra` 但没有后缀，不裁剪）；而 `"Font Ultra Bold"` → `"Font"`（modifier + suffix 齐备才裁）。
3. 解释为什么 `MODIFIERS` 的应用要求「它前面必须有分隔符」——阅读 [src/text/font/info.rs:L256-L260](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/info.rs#L256-L260)。

**需要观察的现象**：单独出现的 modifier 词（无后缀跟随时）不会被误删，避免了把族名里的合法单词当修饰词裁掉。

**预期结果**：运行 `cargo test -p typst-library --lib text::font::info::tests::test_trim_styles`（待本地验证），全部通过。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FontInfo` 不把 `ascender`/`x_height` 这类度量存进去？

> **参考答案**：这些度量**依赖变体坐标**（变量字体在不同 wght/opsz 下度量不同）。`FontInfo` 只存「与坐标无关」的元数据，度量要等选定坐标、构造出 `FontInstance` 后才由 `FontMetrics::from_ttf` 计算。

**练习 2**：`Coverage` 为什么用游程编码而不是 `HashSet<u32>`？

> **参考答案**：字体覆盖的码点天然呈大段连续区间（如 U+0020..U+007E），游程编码能把这种区间压成两个数；相比 `HashSet`，既省内存又利于派生 `Hash`/序列化，`contains` 在区间数不多时也很快。

**练习 3**：`from_ttf` 在判定 `style` 时为什么不直接用 `ttf.is_italic()`？

> **参考答案**：`is_italic()` 还会检查字体的 italic angle，对某些 oblique 字体会误判为 italic。Typst 改为只看 `ttf.style()` 与全名关键词，避免这类假阳性（见 issue #7479）。

---

### 4.3 FontMetrics：字体度量与垂直基线

#### 4.3.1 概念说明

`FontMetrics` 是「一个面在某组坐标下的尺寸参数」。和 `FontInfo` 不同，它**在实例化阶段才算**——因为变量字体换一组轴值，度量就可能变。它由 `FontMetrics::from_ttf` 从（已设好变体坐标的）rustybuzz face 抽取：

[src/text/font/metrics.rs:L9-L32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/metrics.rs#L9-L32) —— 注意所有垂直量都用 `Em`（相对字号的单位），而非绝对长度。这是因为度量要随字号缩放，用 em 存储后，乘以字号即得绝对值（u6-l1 讲过 `Em::resolve`）。

度量分四组：

- **文本度量**：`ascender`/`cap_height`/`x_height`/`descender`/`units_per_em`。
- **装饰线**：`strikethrough`/`underline`/`overline`，各是一个 `LineMetrics { position, thickness }`。
- **上下标**：`subscript`/`superscript`，各是 `Option<ScriptMetrics>`。
- **数学常量**：`math: OnceLock<Box<MathConstants>>`——惰性计算，只在用到数学排版时才填充。

#### 4.3.2 核心流程

抽取时有一个统一的换算函数 `to_em`，把「字体单位」转成 `Em`：

[src/text/font/metrics.rs:L36-L43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/metrics.rs#L36-L43) —— `units_per_em` 是字体内部的缩放基准（通常 1000 或 2048），`to_em(units) = Em::from_units(units, units_per_em)`。

\[
\text{em} = \frac{\text{font units}}{\text{units\_per\_em}}
\]

换算成 em 后，绝对高度就是 `em × font_size`（即 `Em::at(font_size)`）。

很多度量有「字体没提供就用默认值」的兜底：

[src/text/font/metrics.rs:L41-L42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/metrics.rs#L41-L42) —— `cap_height`/`x_height` 若字体没给或为 0，就**回退到 ascender**。这是个保守兜底：宁可高估也别低估，避免文本被裁切。

[src/text/font/metrics.rs:L48-L65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/metrics.rs#L48-L65) —— 三条装饰线：strikeout/underline 优先用字体给的值，缺失时分别用 `Em::new(0.25)`/`Em::new(-0.2)` 作位置、`Em::new(0.06)` 作粗细的硬编码默认；`overline` 没有对应字体表，直接用 `cap_height + 0.1em` 推算。

#### 4.3.3 源码精读

**① 统一的垂直度量查询 `vertical`**

[src/text/font/metrics.rs:L97-L105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/metrics.rs#L97-L105) —— 把 `VerticalFontMetric` 枚举映射到具体 em 值。这个枚举是 `TextElem` 的 `top-edge`/`bottom-edge` 字段背后的类型：

[src/text/font/metrics.rs:L407-L419](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/metrics.rs#L407-L419) —— `VerticalFontMetric` 五个变体。用户写 `#set text(top-edge: "ascender")` 时，最终就是经这里取值，决定一行文本的顶/底边界。

**② 数学常量的惰性初始化**

[src/text/font/mod.rs:L207-L210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L207-L210) —— `FontInstance::math()` 用 `OnceLock::get_or_init` 只在首次访问时计算 `MathConstants`。绝大多数文本排版用不到数学常量，惰性计算能省下这笔开销。

[src/text/font/metrics.rs:L201-L214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/metrics.rs#L201-L214) —— `MathConstants::new`：先算 `space_width`（取空格字形水平推进，缺失则用数学间距常量 `THICK`），再从 OpenType MATH 表读取；MATH 表不存在则走 `fallback`。

**③ 数学常量的 fallback**

[src/text/font/metrics.rs:L337-L403](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/metrics.rs#L337-L403) —— 当字体没有 MATH 表（大部分非数学字体都如此）时，用 MathML Core 规范的默认值兜底，许多值由 `underline.thickness` 与 `x_height` 推导。这意味着**即便没有专门的数学字体，Typst 也能勉强排出可读的公式**——代价是精度不如真正的数学字体。

> 提示：`MathConstants` 字段极多（约 50 个），覆盖分数、根式、上下标、极限符等所有数学排版的间距与位移规则。它们的具体含义属于数学排版（u10 单元）的范畴，本讲只需知道「它们来自 MATH 表，缺失时用 MathML 默认值」。

#### 4.3.4 代码实践

**实践目标**：理解 `top-edge`/`bottom-edge` 如何经由 `FontMetrics::vertical` 影响行盒高度。

**操作步骤**：

1. 在 [src/text/font/mod.rs:L253-L292](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L253-L292) 阅读 `FontInstance::edges`：它接收 `top_edge`/`bottom_edge` 与字号，对 `Metric(metric)` 分支调用 `self.metrics().vertical(metric)`，乘以字号得到绝对顶/底偏移。
2. 追踪 `TopEdge`/`BottomEdge` 的来源——它们是 `TextElem` 的字段（在 `src/text/mod.rs`，可用 `Grep` 搜 `top_edge`/`TopEdge`）。
3. 想象用户写 `#set text(top-edge: "x-height", bottom-edge: "baseline")`：说明此时行盒的顶 = `x_height × font_size`，底 = `0`（baseline）。

**需要观察的现象**：改 `top-edge` 会让同样字号的文本占据不同的行高，因为选用了不同的度量作为「顶」。

**预期结果**：能用自己的话说出「行盒边界 = `vertical(metric).at(font_size)`」这条链路。（实际渲染效果待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么所有度量都用 `Em` 而不是 `Abs` 存储？

> **参考答案**：度量是字体内部几何，与字号无关；用 em（相对单位）存后，只需乘以当前字号即可适配任意大小，避免在每个字号下重新解析字体。

**练习 2**：`cap_height`/`x_height` 缺失时回退到 `ascender`，这样做的代价是什么？

> **参考答案**：会高估大写/小写字母的实际高度。当用户用 `top-edge: "x-height"` 想压紧行高时，效果会比预期「松」。但这是保守选择——低估会导致字母顶部被相邻行裁切，比高估更糟。

**练习 3**：`MathConstants` 为何用 `OnceLock` 而不是在 `FontMetrics::from_ttf` 时一并算好？

> **参考答案**：大多数文本排版根本不碰数学，而 `MathConstants` 体积大、计算不便宜。惰性初始化让普通文本字体省掉这笔开销，只在真正用数学（`font.math()`）时才算。

---

### 4.4 variations：变量字体轴

#### 4.4.1 概念说明

变量字体用「轴（axis）」表达可变维度。OpenType 预定义了五个**标准轴**，Typst 用四个字母的 tag 标识：

| 用户的写法 | 标准 tag | 含义 |
| --- | --- | --- |
| `weight` | `wght` | 字重（100–900） |
| `stretch` | `wdth` | 宽窄（百分比） |
| `style: "italic"` | `ital` | 斜体（0–1） |
| `style: "oblique"` | `slnt` | 倾斜角度（度） |
| （字号） | `opsz` | 视觉字号（optical size） |

`StandardAxes` 就是这五个的集合：

[src/text/font/variations.rs:L85-L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L85-L92) —— 五个 `Option<&FontAxis>` 字段，`parse` 时从字体的全部轴里挑出这五个标准的（[L105-L118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L105-L118)）。其余自定义轴（如 `GRAD`、`INFI`）不在标准集合里，但用户能用 `font-variants`/`font-variations` 手动指定。

本模块要回答的核心问题是：**给定一个目标 `FontVariant` 和字号，怎样把它们翻译成一组「tag → 数值」的坐标？** 这正是 `FontVariations::resolve` 的工作。

#### 4.4.2 核心流程

坐标推导发生在 `Font::instantiate` 内，分三步：

```text
Font::instantiate(variant, size, custom):
  1. automatic = FontVariations::resolve(axes, variant, size)   // 由 variant+字号自动推
  2. full = automatic.chain(custom).normalized()                // 叠加用户自定义，去重（后者覆盖前者）
  3. instantiate_impl(full)                                      // 把坐标喂给 rustybuzz，算 metrics
```

[src/text/font/mod.rs:L107-L118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L107-L118) —— `instantiate` 的三步。它被 `#[comemo::memoize]` 包裹，所以同一 `(Font, variant, size, custom)` 只算一次。

`resolve` 内部，对每个标准轴分别决定坐标：

- **ital/slnt（style）**：用 `(variant.style, ital, slnt)` 三元 match 决定。要 italic 就设 `ital` 到 1.0（不超轴上限）；要 oblique 就设 `slnt`（优先负值即反时针，无负值则取正）；且二者互为 fallback——要 italic 但只有 slnt 轴时，用 slnt 顶上。
- **wdth（stretch）**：`set(wdth, variant.stretch.to_wdth())`。
- **wght（weight）**：`set(wght, variant.weight.to_wght())`。
- **opsz（size）**：`set(opsz, size.to_pt())`。

每一步都用 `AxisValue::clamp(axis)` 把值夹进轴的合法区间，避免给 rustybuzz 越界坐标。

#### 4.4.3 源码精读

**① 轴距离 `FontAxis::distance`（4.1 打分的依据）**

[src/text/font/variations.rs:L31-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L31-L51) —— 这是 4.1 节 `distance` 函数对变量字体算「轴距离」时调用的方法。逻辑：目标值小于轴 min，距离是 `distance(min, target)`；落在 [min, max] 内，距离为 0；大于 max，距离是 `distance(target, max)`。即**目标落在轴范围内即零距离**——这是变量字体在匹配中占优的数学根源。

**② style → ital/slnt 的 match**

[src/text/font/variations.rs:L149-L173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L149-L173) —— 这段 match 把「想要的 style」与「字体实际有哪些斜体轴」对齐。要点：

- `Normal` 或字体两轴都没有 → 不做事。
- 要 `Italic` 且有 `ital` 轴 → 设 ital = min(max, 1.0)。
- 要 `Oblique` 且有 `slnt` 轴（或要 Italic 但只有 slnt） → 设 slnt。注释特别说明：倾斜角是顺时针为正，而典型 italic 是逆时针，所以**负值更理想**；若轴不支持负值，则宁取正值也比不倾斜好（[L167-L171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L167-L171)）。

**③ weight/stretch/opsz 的直接映射**

[src/text/font/variations.rs:L175-L185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L175-L185) —— wdth 用 `variant.stretch.to_wdth()`，wght 用 `variant.weight.to_wght()`，opsz 直接用字号的磅值。这三个轴的映射是线性的，对应方法在 `variant.rs`：

[src/text/font/variant.rs:L118-L130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs#L118-L130) —— `FontWeight::to_wght`/`from_wght`：weight 数值即 wght 坐标。

[src/text/font/variant.rs:L253-L265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs#L253-L265) —— `FontStretch::to_wdth`/`from_wdth`：stretch 用百分比，`wdth = ratio × 100`（100% 即 normal）。

**④ 归一化 `normalized`：坐标去重**

[src/text/font/variations.rs:L198-L208](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L198-L208) —— 自动坐标与用户自定义坐标拼接后，可能对同一 tag 有多个值。`normalized` 先按 tag 稳定排序，再用 `rdedup_by_key` **反向去重（保留最后一个，即让用户自定义覆盖自动值）**。注释明确说不能用标准 `dedup_by_key`（它会保留第一个）。

**⑤ 把坐标真正喂给字体**

[src/text/font/mod.rs:L131-L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L131-L136) —— `instantiate_impl` 里，对归一化后的每个 `(tag, value)` 调 `rusty.set_variation(tag, value)`，然后用这个设好坐标的 face 重新算 `FontMetrics`。到这一步，「变量字体在某个具体坐标下的实例」就成型了。

**⑥ `FontVariations` 的折叠与转换**

[src/text/font/variations.rs:L211-L215](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L211-L215) —— `FontVariations` 实现了 `Fold`（u4-l1 讲过 fold 语义），让 `#set text(font-variations: ...)` 这类 set 规则可以层层叠加。

[src/text/font/variations.rs:L217-L232](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L217-L232) —— `cast!` 让用户在 Typst 里用字典 `(wght: 548)` 表达变体坐标，键会被 cast 成 `Tag`、值 cast 成 `AxisValue`，并附带提示告诉用户出错的位置。

#### 4.4.4 代码实践

**实践目标**：把「用户写法 → 标准轴坐标」的映射表，对照源码逐一验证。

**操作步骤**：

1. 假设有一个变量字体，轴为 `wght`(200–900)、`wdth`(70–125)、`ital`(0–1)、`opsz`(8–72)，没有 `slnt`。
2. 用户在某段文字上设了 `#set text(weight: 548, stretch: 75%, style: "italic", size: 14pt)`。
3. 在 [src/text/font/variations.rs:L141-L188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L141-L188) 的 `resolve` 里逐行推演，写下最终 `FontVariations` 的内容。

**需要观察的现象与预期结果**：

| 标准 tag | 推导来源 | 计算式 | 结果 |
| --- | --- | --- | --- |
| `ital` | style="italic"，有 ital 轴 | `min(max=1.0, 1.0)` | `1.0` |
| `slnt` | 无 slnt 轴 | — | 不设 |
| `wdth` | stretch=75% | `0.75 × 100` | `75`（夹进 [70,125] 仍为 75） |
| `wght` | weight=548 | `548`（夹进 [200,900] 仍为 548） | `548` |
| `opsz` | size=14pt | `14` | `14`（夹进 [8,72] 仍为 14） |

4. 接着验证 `clamp` 的作用：把 `stretch` 改成 `50%`（即 wdth=50），它低于轴 min=70，应被夹成 70。阅读 [src/text/font/variations.rs:L59-L63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L59-L63) 确认。

5. 最后验证「用户自定义覆盖」：若用户额外设 `#set text(font-variations: (wght: 700))`，则 `normalized` 后 wght 应为 700 而非 548（因为反向去重保留后者）。阅读 [src/text/font/variations.rs:L191-L208](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L191-L208) 确认顺序：`automatic.chain(custom)` 把 custom 放后面，反向去重让后面的赢。

**注意**：上表数值是依据源码逻辑的推演结果，实际字体的轴范围不同会改变夹取后的值——「待本地验证」部分指用真实变量字体核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `FontAxis::distance` 把「目标落在 [min, max] 内」算作距离 0？

> **参考答案**：变量字体在这个范围内能精确取到目标值（通过设坐标），所以「实际能提供」与「想要」之间没有差距，距离自然为 0。这正是变量字体比静态字体（只能在固定几个点取值）更灵活的数学体现。

**练习 2**：用户写 `#set text(style: "oblique")`，但字体只有 `ital` 轴没有 `slnt` 轴，会发生什么？

> **参考答案**：看 `resolve` 的 match 第 154–158 行——`(Oblique, Some(ital), None)` 会落入「用 ital 顶替 slnt」的分支，把 ital 设成 1.0。即「要 oblique 但只能给 italic」时，Typst 用 italic 近似。

**练习 3**：`normalized` 为什么要用「反向去重」而非标准 `dedup_by_key`？

> **参考答案**：自动坐标（由 variant 推出）在前、用户自定义坐标（`font-variations`）在后。希望用户自定义**覆盖**自动值，所以要保留后面的；标准 `dedup_by_key` 保留前面的，会把用户值丢掉，故必须反向去重。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「端到端追踪」：从用户的一行 set 规则，一直追到字体实例化。

**任务背景**：用户写了

```typst
#set text(font: ("Noto Sans", "DejaVu Sans"),
            weight: "bold",
            style: "italic",
            size: 12pt,
            font-variations: (opsz: 12))
```

**要求**：画出这条规则在字体子系统里的完整处理时序，并在每一步标注对应的源码位置。建议按下面框架完成：

1. **样式归集（u7-l1 + u4）**：`weight`/`style`/`size`/`font-variations` 都是 `TextElem` 的（ghost）字段，进入 `StyleChain`；`variant()` 综合 style/weight/stretch 产出一个 `FontVariant`。
2. **族遍历与匹配（4.1）**：shaping 时遍历 `("Noto Sans", "DejaVu Sans")`，对每个族调 `FontBook::select(family_lowercased, variant)`（[book.rs:L75-L78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L75-L78)），内部 `find_best_variant` 用 `(similarity, Reverse(distance), is_variable)` 打分（[book.rs:L139-L163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L139-L163)）。
3. **轴感知距离（4.1 + 4.4）**：对变量字体，`distance` 用 `FontAxis::distance`（[variations.rs:L31-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L31-L51)），落在轴范围内即零距离。
4. **实例化（4.4）**：拿到面索引后 `Font::instantiate(variant, size, &custom)`（[mod.rs:L107-L118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L107-L118)）：`resolve` 把 variant+字号翻译成 `wght`/`ital`/`opsz` 坐标（[variations.rs:L149-L185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L149-L185)），用户的 `(opsz: 12)` 经 `normalized` 反向去重覆盖自动 opsz（[variations.rs:L198-L208](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variations.rs#L198-L208)），最终 `set_variation` 喂给 rustybuzz（[mod.rs:L131-L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L131-L136)）。
5. **度量（4.3）**：用设好坐标的 face 重算 `FontMetrics`（[mod.rs:L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L136)），后续行盒边界经 `vertical(metric)`（[metrics.rs:L97-L105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/metrics.rs#L97-L105)）取值。

**交付物**：一张时序图或编号清单，把上面 5 步、每步的关键函数与源码行号对应起来，并解释「为什么第 4 步用户的 `(opsz: 12)` 能覆盖自动推导的 opsz」。这一步同时检验你对 4.1（匹配）、4.4（坐标推导）、4.3（度量）三者的综合理解。

## 6. 本讲小结

- `FontBook` 是只含 `FontInfo` 元数据的轻量索引，用「族名（小写）→ 面索引」映射 + 元数据数组组织；发现（`select_family`/`contains_family`）与匹配（`select`/`select_fallback`）分离。
- 匹配打分是一个三元组 `(similarity, Reverse(distance), is_variable)`，字典序取最大；距离又是 `(style, stretch, weight)` 三元组，style 最重要。
- 变量字体在匹配中占优的根源是 `FontAxis::distance`：目标落在轴区间内即零距离，故变量字体常胜静态字体，但「距离」仍是主因——距离更大时静态字体会反超。
- `FontInfo::from_ttf` 一次性抽取族名（用 `typographic_family` 剥离样式后缀而非 Name ID 16）、style（综合关键词而非 `is_italic`）、flags（PANOSE 判衬线）、`Coverage`（游程编码的码点集）与轴；例外表修正错误字体。
- `FontMetrics` 在实例化后由 `from_ttf` 抽取，垂直量一律用 `Em`；`cap_height`/`x_height` 缺失回退 ascender；`MathConstants` 走 `OnceLock` 惰性初始化，无 MATH 表时用 MathML Core 默认值兜底。
- `FontVariations::resolve` 把目标 `FontVariant` + 字号映射到 `wght`/`wdth`/`ital`/`slnt`/`opsz` 坐标，每步 `clamp` 进轴区间；用户自定义坐标经 `normalized` 反向去重覆盖自动值，最终在 `Font::instantiate_impl` 里喂给 rustybuzz。

## 7. 下一步学习建议

- **u7-l3（OpenType 特性、语言、断行…）**：本讲讲的是「选哪个字体、设哪些坐标」，u7-l3 讲的是「选定字体后，启用哪些 OpenType feature（kern/liga/…）以及如何断行、加引号」。二者衔接紧密，建议紧接着读。
- **u10（数学公式）**：本讲埋下的 `MathConstants`、`FontFlags::MATH`、数学字号常量，在 u10 单元会全面展开。
- **源码延伸阅读**：若想看清「字体匹配后被加载并 shaping」的完整链路，可跨 crate 阅读 `crates/typst-layout/src/inline/shaping.rs` 中调用 `book.select` / `select_fallback` 的那段（本讲 4.1.3 已给出行号），那是 `FontBook` 与真实排版的接口。
