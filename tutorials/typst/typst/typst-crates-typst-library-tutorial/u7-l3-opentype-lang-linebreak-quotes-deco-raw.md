# OpenType 特性、语言、断行、引号、装饰与 raw

## 1. 本讲目标

本讲是「文本系统」单元的第三篇，承接 u7-l1（`TextElem` 与字体变体）和 u7-l2（`FontBook`/`FontInfo`/metrics/variations）。前两讲回答了「文本元素如何表示」「字体如何被挑选与实例化」，本讲回答一个更具体的问题：

> 当一段文本真正进入排版流水线时，**语言、OpenType 特性、断行、引号、装饰、原始代码**这些「环境因素」是从哪里读出来、又如何影响最终输出的？

学完后你应当能够：

- 读懂 `features()` 函数，说出 Typst 的每一个字体特性字段（`kerning`/`ligatures`/`number-type` 等）如何映射到 OpenType tag；
- 解释 `Lang`/`Region`/`WritingScript` 三者如何被解析、如何决定文字方向与本地化名称（`LocalName`）；
- 说清 `Costs` 成本结构如何被断行算法当作权重，以及它为何用 `#[fold]`；
- 理解智能引号如何用「零前瞻」状态机判断开闭，并随语言切换字符；
- 识别 `underline`/`overline`/`strike`/`highlight` 与 `raw` 这些「包装型」文本元素的设计共性。

---

## 2. 前置知识

本讲假设你已经读过 u7-l1 与 u7-l2，并掌握以下概念（不熟悉请先回看）：

- **ghost 字段**：`TextElem` 上绝大多数样式字段（如 `size`/`fill`/`lang`/`kerning`）都是 `#[ghost]`，它们不进入元素 struct，只活在 `StyleChain` 里。本讲涉及的 `kerning`/`ligatures`/`lang` 等字段**无一例外都是 ghost 字段**，所以读取它们必须经过 `styles.get(...)`（见 [u7-l1](u7-l1-textelem-and-font-variant.md)）。
- **`StyleChain` 与 `Fold`/`Resolve`**：样式查询沿链进行，`#[fold]` 字段会把多层值合并而非覆盖（见 u4-l1）。
- **`Smart<T>`**：`Auto` 表示「智能默认，交给消费方推导」，`Custom(v)` 表示显式给定（见 u4-l1）。
- **OpenType 特性（feature）**：字体内部用 4 字节 tag（如 `kern`、`liga`）标记的可开关的排版能力，rustybuzz/harfbuzz 在塑形（shaping）时读取它们决定字形替换与定位。
- **`#[elem]` 元素与 `#[func]` 函数**：本 crate 只定义元素与归一化数据，真正的算法住在 `typst-layout`，运行期经 `Routines` 回调（见 u3-l3、u5-l4）。

---

## 3. 本讲源码地图

本讲涉及的文件全部位于 `crates/typst-library/src/text/`（及一处 `model/`），外加断行算法在 `crates/typst-layout/` 的消费点：

| 文件 | 作用 |
| --- | --- |
| [`src/text/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs) | `TextElem` 的字体特性字段、`features()` 收集函数、`Costs` 成本结构、`language()` 转换 |
| [`src/text/lang.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs) | `Lang`/`Region`/`WritingScript` 类型、`LocalName` trait、翻译表加载 |
| [`src/text/linebreak.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/linebreak.rs) | 手动换行元素 `LinebreakElem`（注意：优化算法在 typst-layout） |
| [`src/text/smartquote.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/smartquote.rs) | 智能引号元素 `SmartQuoteElem`、零前瞻替换器 `SmartQuoter`、语言相关引号表 `SmartQuotes` |
| [`src/text/deco.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/deco.rs) | `UnderlineElem`/`OverlineElem`/`StrikeElem`/`HighlightElem` 及统一装饰数据 `DecoLine` |
| [`src/text/raw.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/raw.rs) | 原始文本元素 `RawElem`、语法高亮、`ShowSet` |
| [`src/model/par.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs) | 段落元素中的 `Linebreaks` 枚举（Simple/Optimized） |

一个贯穿全讲的认知：本 crate 负责**定义元素与把用户输入归一化为数据**，而真正「把文本排成帧」的塑形、断行、装饰绘制算法住在 `typst-layout`，运行期经 `Routines` 回调。

---

## 4. 核心概念与源码讲解

### 4.1 OpenType 特性收集：features() 与字体特性字段

#### 4.1.1 概念说明

OpenType 字体把「字符如何变成字形」的能力拆成一个个可开关的**特性（feature）**，每个特性用一个 4 字节 tag 标识，例如：

- `kern` —— 字偶距（kerning），让 `To` 这样的字母对靠得更紧；
- `liga`/`clig` —— 标准连字，把 `fi` 合并成一个字形；
- `lnum`/`onum` —— 数字用「齐线数字」还是「老式数字」；
- `smcp` —— 小型大写字母。

用户在 Typst 里写 `#set text(ligatures: false)`、`#set text(number-type: "lining")`，但这些字段本身只是 ghost 样式值（见 u7-l1）。真正要塑形文本时，需要一个函数把这些零散的样式**翻译成一串 `(tag, value)` 对**喂给 rustybuzz。这个翻译器就是 `features()`。

#### 4.1.2 核心流程

`features(styles)` 接收一条 `StyleChain`，逐个查询字体特性字段，把命中的字段转成一个 `Vec<Feature>`，流程是：

1. 建一个空的 `tags` 列表和闭包 `feat(tag, value)`，闭包内部 `Feature::new(Tag::from_bytes(tag), value, ..)`（`..` 表示「对所有字形生效」）。
2. 对每个字段判断「默认开还是默认关」：
   - **Harfbuzz 默认开的特性**（如 `kern`、`liga`/`clig`）只在用户**关闭**时才写入（写 `value=0`）；
   - **Harfbuzz 默认关的特性**（如 `dlig`、`hlig`、`frac`）只在用户**开启**时才写入（写 `value=1`）。
3. 枚举型字段（`number-type`/`number-width`）用 `match Smart` 分派，`auto` 时跳过。
4. 最后把用户通过 `text(features: ...)` 直接传入的「原始 tag」原样追加。

这样设计的好处：列表只记录「与默认不同的项」，尽量短。

#### 4.1.3 源码精读

`features()` 的全貌在 [src/text/mod.rs:1396-1469](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1396-L1469)。其中核心闭包与「默认开/关」分流的写法：

```rust
let mut feat = |tag: &[u8; 4], value: u32| {
    tags.push(Feature::new(ttf_parser::Tag::from_bytes(tag), value, ..));
};

// 默认开的特性：仅当关闭时写入
if !styles.get(TextElem::kerning) { feat(b"kern", 0); }

// 默认关的特性：仅当开启时写入
if styles.get(TextElem::discretionary_ligatures) { feat(b"dlig", 1); }

// 枚举型：auto 时跳过
match styles.get(TextElem::number_type) {
    Smart::Auto => {}
    Smart::Custom(NumberType::Lining) => feat(b"lnum", 1),
    Smart::Custom(NumberType::OldStyle) => feat(b"onum", 1),
}
```

完整映射表如下（请对照源码逐行核对）：

| Typst 字段（ghost） | OpenType tag | 写入条件 | value |
| --- | --- | --- | --- |
| `kerning` | `kern` | 仅当 `false`（默认开） | 0 |
| `smallcaps` | `smcp`（+ `c2sc` 若 `All`） | 当为 `Some` | 1 |
| `alternates` | `salt` | 非 0 | 该整数值 |
| `stylistic_set` | `ss01`–`ss20` | 集合中每个 set | 1 |
| `ligatures` | `liga`、`clig` | 仅当 `false`（默认开） | 0 |
| `discretionary_ligatures` | `dlig` | 仅当 `true`（默认关） | 1 |
| `historical_ligatures` | `hlig` | 仅当 `true` | 1 |
| `number-type: "lining"` | `lnum` | 非 `auto` | 1 |
| `number-type: "old-style"` | `onum` | 非 `auto` | 1 |
| `number-width: "proportional"` | `pnum` | 非 `auto` | 1 |
| `number-width: "tabular"` | `tnum` | 非 `auto` | 1 |
| `slashed_zero` | `zero` | 仅当 `true` | 1 |
| `fractions` | `frac` | 仅当 `true` | 1 |
| 数学脚本字号 | `ssty` | 数学模式 script/scriptscript | 1 或 2 |
| `features`（原始） | 用户给定 | 直接透传 | 用户给定 |

其中 `stylistic_set` 的 tag 是**运行期拼出来的**——把数字拆成两位 ASCII 存进 `[b's', b's', b'0'+set/10, b'0'+set%10]`（见 [src/text/mod.rs:1420-1423](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1420-L1423)）。`NumberType`/`NumberWidth` 这两个枚举定义在 [src/text/mod.rs:1328-1346](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1328-L1346)。

用户可见字段则在 `TextElem` 上声明，例如 `kerning`/`ligatures`/`number_type`/`features` 都标了 `#[ghost]`（见 [src/text/mod.rs:608-788](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L608-L788)），印证了 u7-l1 的结论：**样式属于环境、不属于单个字符**，一条样式链可复用于海量文本节点。

#### 4.1.4 代码实践

**目标**：亲手追踪一个 `#set text(number-type: "lining")` 最终产生了哪个 OpenType tag。

**操作步骤**：

1. 打开本讲义引用的 `features()` 源码 [src/text/mod.rs:1396-1469](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1396-L1469)。
2. 写一个最小 Typst 文档：

   ```typ
   #set text(font: "Noto Sans", number-type: "lining")
   Number 9.
   ```

   编译后观察 `9` 的字形是否变为齐线风格。
3. 把 `"lining"` 改成 `"old-style"`，观察 `9` 是否下沉成老式数字（对应 tag 从 `lnum` 变为 `onum`）。

**需要观察的现象**：数字字形随 `number-type` 改变；这正是 `features()` 把字段翻译成 `lnum`/`onum` tag、再由 rustybuzz 塑形的结果。

**预期结果**：`lining` 时数字底部齐线、`old-style` 时数字有高低差。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `kerning` 字段在 `features()` 里用 `if !styles.get(...)` 判断，而 `discretionary_ligatures` 用 `if styles.get(...)`？

**答案**：因为 `kern` 是 Harfbuzz 默认开启的特性，只需在用户关闭时显式写 `value=0` 来禁用；`dlig` 默认关闭，只需在用户开启时写 `value=1`。这种「只记录偏离默认」的策略让特性列表尽量短。

**练习 2**：用户写 `#set text(features: ("frac",))` 时，这个 `frac` 走的是 `features()` 的哪一段？和 `#set text(fractions: true)` 有何异同？

**答案**：走的是函数末尾 [src/text/mod.rs:1464-1466](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1464-L1466)，用户给定的原始 tag 被原样追加。两者最终都产出 `(frac, 1)`，但 `fractions: true` 是「语义化字段」，而 `features: ("frac",)` 是「直通通道」，后者可控制 `fractions` 字段不覆盖的非布尔特性（如 `swsh`）。

---

### 4.2 语言、区域、脚本与本地化

#### 4.2.1 概念说明

排版高度依赖语言。同样是 `"`，德语和法语该渲染成不同的引号；同样是段落，阿拉伯语要从右向左排。Typst 用三个维度刻画「这段文字属于哪种语言环境」：

- **`Lang`**：自然语言，ISO 639-1/2/3 二到三字母码（`en`/`de`/`zh`）。
- **`Region`**：地区，ISO 3166-1 alpha-2 二字母码（`US`/`CH`），可空。语言相同而地区不同可能选用不同的字形或引号（如德语在瑞士 vs 德国）。
- **`WritingScript`**：书写脚本，ISO 15924 三到四字母码（`latn`/`grek`），决定字体特性的适用范围。设为 `auto` 时按字符的 Unicode 脚本自动选择。

此外，`Lang` 还隐含一个关键信息：**默认文字方向**（`dir`），阿拉伯/希伯来等语言默认 RTL。

#### 4.2.2 核心流程

这三个值都来自 `TextElem` 的 ghost 字段（见 [src/text/mod.rs:473-515](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L473-L515)）。它们流向两个消费方：

1. **塑形侧**：`language(styles)` 把 `lang + region` 拼成 BCP 47 字符串（如 `de-CH`），交给 rustybuzz 作为 `Language`（见 [src/text/mod.rs:1473-1480](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1473-L1480)）。
2. **本地化侧**：`LocalName` trait 让每个元素（标题、图、目录项……）能根据 `lang`/`region` 返回它在该语言下的名称（如英文 `Figure`、中文 `图`）。

`Lang` 的内部表示很讲究：`Lang([u8; 3], u8)`——用 3 字节存码、另用 1 字节存实际长度，这样 2 字母和 3 字母码共用一个定长结构，`Copy` 廉价、可哈希（见 [src/text/lang.rs:155-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L155-L156)）。`Region` 则是定长 `[u8; 2]`（[src/text/lang.rs:538-540](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L538-L540)），`WritingScript` 与 `Lang` 同构（[src/text/lang.rs:576-578](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L576-L578)）。

#### 4.2.3 源码精读

`Lang::dir` 用一个 `match` 列出所有默认 RTL 的语言，其余回落 LTR（[src/text/lang.rs:483-498](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L483-L498)）：

```rust
pub fn dir(self) -> Dir {
    match self {
        Lang::ARABIC | Lang::HEBREW | Lang::PERSIAN
        | Lang::URDU | Lang::YIDDISH /* ... */ => Dir::RTL,
        _ => Dir::LTR,
    }
}
```

字符串到类型的解析在 `FromStr` 里：`Lang::from_str` 校验长度为 2–3 且为 ASCII，拷进定长字节数组并小写化（[src/text/lang.rs:501-516](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L501-L516)）。`cast!` 进一步给出友好提示：当用户误把 `"de-DE"` 整串塞进 `lang` 时，提示应把 `DE` 拆到 `region` 参数（[src/text/lang.rs:518-536](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L518-L536)）。

本地化机制是 `LocalizedStr` + `LocalName` trait。`LocalName` 是个很小的 trait，元素只需声明一个 `KEY`（如 `"figure"`），即可通过 `local_name_in(styles)` 拿到当前语言的名称（[src/text/lang.rs:615-632](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L615-L632)）。`localized_str` 的回退顺序是「语言+地区 → 仅语言 → 英语」（[src/text/lang.rs:638-650](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L638-L650)），翻译数据用 `include_str!` 在编译期嵌入约 110 个语言文件（[src/text/lang.rs:11-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L11-L118)），并用 `#[comemo::memoize]` 缓存解析结果。

#### 4.2.4 代码实践

**目标**：验证 `lang` 如何同时影响「方向」与「本地化名称」。

**操作步骤**：

1. 写如下文档并编译：

   ```typ
   #set text(lang: "ar")
   المقدمة
   #outline()
   ```

2. 阅读本讲引用的 `Lang::dir` 源码，确认 `Lang::ARABIC` 在 RTL 分支里。
3. 把 `lang` 改为 `"en"`，对比 `outline` 标题文字（如「目录」类名）的变化。

**需要观察的现象**：阿拉伯文从右向左排布；本地化名称随 `lang` 切换。

**预期结果**：`lang: "ar"` 时文字 RTL、目录标题为阿拉伯语；`lang: "en"` 时为英语。若本地无阿拉伯字体，方向行为仍成立，但字形可能缺失（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`Lang` 为什么不直接存 `String` 或 `EcoString`，而要用 `([u8;3], u8)`？

**答案**：定长表示让 `Lang` 成为 `Copy` 类型，复制无需分配；同时可直接派生 `Hash`/`Eq`/`Ord`，便于在 `match` 与哈希表里高效使用。语言码最长 3 字符，用 3 字节数组 + 长度即可覆盖。

**练习 2**：`localized_str` 找不到某语言的翻译时会发生什么？

**答案**：先回退到「仅语言」的翻译包，再回退到英语；英语翻译必须存在，否则 `english_bundle[key]` 会 panic（见 [src/text/lang.rs:648-649](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L648-L649)）。这保证任意语言都能拿到一个名称。

---

### 4.3 断行：LinebreakElem、Linebreaks 与 Costs 成本

#### 4.3.1 概念说明

把一段文字切成若干行，有两大流派：

- **简单（first-fit）**：贪心地逐词填充，填满即换行，快但不美观；
- **优化（Knuth-Plass 风格）**：把整段当成一个最优化问题，为每种切法算一个**总成本**，取总成本最低的切法。Typst 默认在 `#set par(justify: true)` 时启用优化。

「成本」就是衡量「这一行/这次换行有多难看」的数值：行太松（词间距被拉得很开）成本高、在词中间断开（加连字符）成本高、段落末行只剩一个词（runt）成本高。`Costs` 结构让用户用**比率**微调这些权重——`200%` 表示「让引擎减半地愿意做这件事」。

#### 4.3.2 核心流程

本 crate 在断行上做了三件事，分别对应三个源码点：

1. **`LinebreakElem`**（`linebreak.rs`）：表示用户手写的一个换行（`\` 后跟空白）。它只是一个带 `justify` 字段的元素，本身不参与算法，而是作为一个「强制断点」标记被排版器识别。
2. **`Linebreaks` 枚举**（`model/par.rs`）：`Simple`/`Optimized` 二选一，挂在 `ParElem::linebreaks` 上，告诉排版器用哪种算法。
3. **`Costs` 结构**（`text/mod.rs`）：四项比率权重 `hyphenation`/`runt`/`widow`/`orphan`，被排版器读出后参与成本计算。

成本计算的数学本质是：对整段的每一种合法切分，总成本是各分项之和：

\[
\text{total} \;=\; \sum_{\text{line }i} \text{badness}_i \;+\; \sum_{\text{断字}} c_{\text{hyph}} \;+\; \sum_{\text{runt 行}} c_{\text{runt}} \;+\; \dots
\]

其中断字单位成本被 `Costs` 缩放：

\[
c_{\text{hyph}} \;=\; \text{DEFAULT\_HYPH\_COST} \times r_{\text{hyphenation}}, \qquad r_{\text{hyphenation}} = \text{costs.hyphenation}() \in [0,1]\text{ 比率}
\]

默认 `DEFAULT_HYPH_COST = 135.0`、`DEFAULT_RUNT_COST = 100.0`（见 typst-layout 的 [src/inline/linebreak.rs:29-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L29-L30)）。`CostMetrics::compute` 把比率与默认常数相乘得到实际权重（见 [src/inline/linebreak.rs:919-932](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L919-L932)）。

#### 4.3.3 源码精读

`LinebreakElem` 极简——一个 `justify` 字段加一个全局共享实例（[src/text/linebreak.rs:22-46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/linebreak.rs#L22-L46)）：

```rust
#[elem(title = "Line Break", since = "forever")]
pub struct LinebreakElem {
    #[default(false)]
    pub justify: bool,
}

impl LinebreakElem {
    /// 全局共享的换行元素。
    pub fn shared() -> &'static Content {
        singleton!(Content, LinebreakElem::new().pack())
    }
}
```

注意 `shared()` 用 `singleton!`（见 u12-l2）缓存一个静态 `Content`，因为源码里 `\` 换行极常见，复用一个实例可省去海量分配。

`Linebreaks` 枚举在 [src/model/par.rs:625-635](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L625-L635) 定义：

```rust
pub enum Linebreaks {
    /// 简单首适应。
    Simple,
    /// 对整段做最优化（更均匀的行）。
    Optimized,
}
```

`Costs` 是本讲的重点（[src/text/mod.rs:1502-1512](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1502-L1512)）。它的四个字段都是 `Option<Ratio>`，访问器在 `None` 时回落到 `Ratio::one()`（即 100%）：

```rust
#[non_exhaustive]
pub struct Costs {
    hyphenation: Option<Ratio>,
    runt: Option<Ratio>,
    widow: Option<Ratio>,
    orphan: Option<Ratio>,
}
```

关键在于它的 `Fold` 实现——**用 `Option::or` 合并**，内层（更晚的）值优先（[src/text/mod.rs:1536-1546](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1536-L1546)）：

```rust
impl Fold for Costs {
    fn fold(self, outer: Self) -> Self {
        Self {
            hyphenation: self.hyphenation.or(outer.hyphenation),
            // ... runt / widow / orphan 同理
        }
    }
}
```

这与 u7-l1 里 `TextSize` 的「函数相乘」式折叠不同：`Costs` 是「谁显式设了就用谁，没设就继承外层」的覆盖式折叠。`Costs` 挂在 `TextElem::costs` 上，标了 `#[fold]` 与 `#[ghost]`（[src/text/mod.rs:608-610](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L608-L610)）。真正的消费在 `typst-layout`：断字与 runt 成本用于行级最优化（[src/inline/linebreak.rs:929-930](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L929-L930)），widow/orphan 用于跨页时是否把孤行挪走（[src/flow/collect.rs:195-197](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L195-L197)）。

#### 4.3.4 代码实践

**目标**：观察 `Costs` 如何改变断行决策（本讲实践任务之二）。

**操作步骤**：

1. 写一个两端对齐、容易产生断字的段落：

   ```typ
   #set par(justify: true)
   #set text(lang: "en")
   #lorem(30)
   ```

2. 阅读 typst-layout 的 `CostMetrics::compute`（[src/inline/linebreak.rs:919-932](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L919-L932)），确认 `hyph_cost = 135.0 * costs.hyphenation().get()`。
3. 在文档顶部加 `#set text(costs: (hyphenation: 1000%))`（即把断字成本放大 10 倍），重新编译，对比断字数量。

**需要观察的现象**：提高 `hyphenation` 成本后，引擎会尽量避免在词中间断开——表现为更少（甚至没有）连字符 `-`。

**预期结果**：默认设置下若出现 2–3 处断字，设为 `1000%` 后断字显著减少，但行可能变得更稀疏（因为拒绝断字就得拉大词距）。

#### 4.3.5 小练习与答案

**练习 1**：`Costs` 的 `Fold` 为什么用 `Option::or`，而不是像 `TextSize` 那样把两个值相乘/相加？

**答案**：因为成本是「用户偏好」：用户在某层显式设了 `hyphenation: 200%`，就应当以这个偏好为准，而不是和外层再叠加。`Option::or` 实现「内层有值就用内层，否则继承外层」，正好表达「更内层的 set 规则覆盖更外层」。`None` 代表「本层未指定」。

**练习 2**：`LinebreakElem` 和 `Linebreaks` 有什么区别？

**答案**：`LinebreakElem` 是**用户手写的单次强制换行**（`\`），是文档内容里的一个断点标记；`Linebreaks` 是**段落级的算法选择**（Simple/Optimized），决定整段用什么策略寻找断点。前者是「点」，后者是「策略」。

---

### 4.4 智能引号：SmartQuoteElem、SmartQuoter 与 SmartQuotes

#### 4.4.1 概念说明

在源码里直接打 `"` 或 `'`，Typst 会自动把它们转成排版上正确的「弯引号」（如 `"…" → "…"`）。这件事有两层难题：

1. **判断开/闭**：同一个 `"` 字符，在「`He said, "`」里是开引号，在「`... done."`」里是闭引号。Typst 选择**零前瞻**策略——只看「当前引号栈状态 + 紧邻前一个字符」就做决定，不向后看。
2. **语言相关**：不同语言的「正确引号」不同。德语用 `„…"`（低-高），法语用 `« … »`（带窄不换行空格），英语用 `"…"`。

#### 4.4.2 核心流程

智能引号由三个类型协作（均在 `smartquote.rs`）：

1. **`SmartQuoteElem`**：用户可见的元素，字段 `double`/`enabled`/`alternative`/`quotes` 控制行为。
2. **`SmartQuotes`**：根据 `lang`/`region`/`alternative` 选出 4 个具体引号字符（单开/单闭/双开/双闭）。这是「查表」。
3. **`SmartQuoter`**：维护一个引号嵌套栈（最大 32 层），对每个引号字符用零前瞻规则判定开/闭。这是「状态机」。

流程为：先 `SmartQuotes::get_in(styles)` 拿到当前语言的 4 个字符 → 排版时逐个遇到引号，调用 `SmartQuoter::quote(before, quotes, double)` 得到替换字符串。

#### 4.4.3 源码精读

`SmartQuoter` 的状态用一个 `u32` 位图记录每层是单还是双引号（[src/text/smartquote.rs:99-106](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/smartquote.rs#L99-L106)）。核心判定逻辑 `quote()`（[src/text/smartquote.rs:116-155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/smartquote.rs#L116-L155)）按优先级分四种情况：

```rust
pub fn quote<'a>(&mut self, before: Option<char>, quotes: &SmartQuotes<'a>, double: bool) -> &'a str {
    let opened = self.top();
    let before = before.unwrap_or(' ');
    // ① 数字后且未开同种引号 → 撇号/双撇号（如 5' 5"）
    if before.is_numeric() && opened != Some(double) {
        return if double { "″" } else { "′" };
    }
    // ② 单引号、字母后、未开单引号 → 撇号（apostrophe，如 don't）
    if !double && opened != Some(false) && (before.is_alphabetic() || before == '\u{FFFC}') {
        return "’";
    }
    // ③ 栈顶正是同种引号且不像嵌套 → 闭合
    if opened == Some(double) && !before.is_whitespace() && !is_newline(before) && !is_opening_bracket(before) {
        self.pop();
        return quotes.close(double);
    }
    // ④ 否则 → 开新引号
    self.push(double);
    quotes.open(double)
}
```

注意它**完全不看后面**的字符——这就是「零前瞻」。代价是少数情况下判断不够智能，但换来可在流式处理中即时替换。

语言相关引号由 `SmartQuotes::get` 用一个巨型 `match lang` 决定（[src/text/smartquote.rs:225-290](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/smartquote.rs#L225-L290)，节选）：

```rust
let default = ("‘", "’", "“", "”);      // 英语
let low_high = ("‚", "‘", "„", "“");    // 德/捷克等

let (...) = match lang {
    Lang::GERMAN if matches!(region, Some("CH" | "LI")) => /* 瑞士用法 */,
    Lang::FRENCH => ("“", "”", "«\u{202F}", "\u{202F}»"),  // 法语 guillemet
    Lang::RUSSIAN => ("„", "“", "«", "»"),
    _ if lang.dir() == Dir::RTL => ("’", "‘", "”", "“"),   // RTL 回退
    _ => default,
};
```

这里与 4.2 联动：`lang.dir()` 直接复用了 `Lang::dir`。`\u{202F}` 是「窄不换行空格」，法语引号内测需要它。`SmartQuoteElem` 元素本身在 [src/text/smartquote.rs:32-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/smartquote.rs#L32-L90)。

#### 4.4.4 代码实践

**目标**：体会「语言切换引号字符」这一行为。

**操作步骤**：

1. 写：

   ```typ
   "in English"
   #set text(lang: "de")
   "auf Deutsch"
   #set text(lang: "fr")
   "en français"
   ```

2. 编译，对照 `SmartQuotes::get` 的 `match` 分支核对每行用的引号字符。

**需要观察的现象**：英语 `“…”`、德语 `„…"`（底部双引号开头）、法语 `« … »`。

**预期结果**：与源码表一致。可临时 `#set smartquote(enabled: false)` 关掉智能替换，对照「原始直引号」。

#### 4.4.5 小练习与答案

**练习 1**：`SmartQuoter::quote` 的第②条规则（撇号）为什么要求 `opened != Some(false)`？

**答案**：`Some(false)` 表示栈顶最近开的是一个单引号，此时若再出现单引号，应优先闭合它而非当成撇号；只有「当前没有未闭合的单引号」时，字母后的单引号才被解读为撇号（apostrophe）。这是用状态栈消歧义。

**练习 2**：为什么法语双引号用 `«\u{202F}` 而不是直接 `«`？

**答案**：法语排版惯例要求 guillemet `«` `»` 与文字之间插入一个**窄不换行空格**（U+202F），既视觉上留白，又不能在此换行。Typst 把这个空格直接编进引号字符串里。

---

### 4.5 文本装饰与原始文本：deco 与 RawElem

本模块把两类「包装型」文本元素放在一起讲，因为它们结构同构：都接受一个 `body: Content`，都通过把样式套在 body 上来改变其外观，而真正的绘制/排版在 `typst-layout`。

#### 4.5.1 概念说明

**装饰元素（deco）**：`underline`/`overline`/`strike`/`highlight` 在文字下方/上方/中间/背景画线或色块。它们共享一组字段：`stroke`（线型，`#[fold]`）、`offset`（相对基线的位置，`Smart`）、`extent`（向两侧延伸量）。`Smart::Auto` 的 `offset`/`stroke` 表示「从字体表读取默认值」。

**原始文本（raw）**：`raw` 把内容**逐字**显示（忽略 `*strong*` 等标记语法），默认用等宽字体 `DejaVu Sans Mono`、字号 `0.8em`，并可带语言标签做语法高亮。它的核心特殊性在于：raw 是一个**会主动改写自身样式**的元素（通过 `ShowSet`）。

#### 4.5.2 核心流程

装饰元素都标了 `Locatable` + `Tagged`（见 u9-l1），这样排版时能在正确的位置回填装饰。它们在 `TextElem` 上通过 `#[ghost] #[fold]` 的 `deco` 字段（一个 `SmallVec<[Decoration; 1]>`）汇聚——即装饰最终被「摊平」成一串 `Decoration`，随每个文本节点携带（见 [src/text/mod.rs:880-884](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L880-L884)）。统一的运行期数据是 `DecoLine` 枚举（Underline/Strikethrough/Overline/Highlight 四变体，[src/text/deco.rs:295-322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/deco.rs#L295-L322)）。

`RawElem` 的特殊之处是实现了 `ShowSet`（见 u4-2）：它在自己被 show 之前，先**主动给自己套一组样式**——关掉 overhang、关掉连字符、固定字体与字号、设语言为英语。这样 raw 文本天然「不参与」普通排版特性。

#### 4.5.3 源码精读

四个装饰元素字段几乎一致，以 `UnderlineElem` 为例（[src/text/deco.rs:12-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/deco.rs#L12-L72)）：

```rust
#[elem(since = "forever", Locatable, Tagged)]
pub struct UnderlineElem {
    #[fold] pub stroke: Smart<Stroke>,  // 线型，auto 时取文字色+字体厚度
    pub offset: Smart<Length>,           // 相对基线，auto 时读字体表
    pub extent: Length,                  // 两侧延伸
    #[default(true)] pub evade: bool,    // 是否避开字形下凹处
    #[default(false)] pub background: bool,
    #[required] pub body: Content,
}
```

`HighlightElem`（[src/text/deco.rs:213-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/deco.rs#L213-L282)）用 `fill`（背景色，默认半透明黄）和 `top_edge`/`bottom_edge`（背景矩形的上下边界，默认 ascender/descender）。`DecoLine` 把这些用户字段解析成绝对单位后的运行期表示（[src/text/deco.rs:295-322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/deco.rs#L295-L322)）。

`RawElem` 的定义带了一大串能力（[src/text/raw.rs:252-264](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/raw.rs#L252-L264)），关键字段是 `text`、`block`（[src/text/raw.rs:323-324](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/raw.rs#L323-L324)）、`lang`（[src/text/raw.rs:355](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/raw.rs#L355)）。它的 `ShowSet` 实现是本模块的点睛之笔（[src/text/raw.rs:651-665](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/raw.rs#L651-L665)）：

```rust
impl ShowSet for Packed<RawElem> {
    fn show_set(&self, styles: StyleChain) -> Styles {
        let mut out = Styles::new();
        out.set(TextElem::overhang, false);
        out.set(TextElem::lang, Lang::ENGLISH);
        out.set(TextElem::hyphenate, Smart::Custom(false));
        out.set(TextElem::size, TextSize(Em::new(0.8).into()));
        out.set(TextElem::font, FontList(vec![FontFamily::new("DejaVu Sans Mono")]));
        out.set(TextElem::cjk_latin_spacing, Smart::Custom(None));
        if self.block.get(styles) {
            out.set(ParElem::justify, false);   // 块级 raw 默认不两端对齐
        }
        out
    }
}
```

这正是 u7-l1 讲过的「内置 show-set 规则」模式：raw 元素**反向**改写了一堆 `TextElem` 的 ghost 字段。注意它写在 `Packed<RawElem>` 上而非 `RawElem` 上——这是 u3-l3 强调的约定：能力 trait 必须落在 `Packed<E>` 上，因为 vtable 与能力调用都以 `Packed<E>` 为对象类型。

#### 4.5.4 代码实践

**目标**：验证 raw 的默认字号是 `0.8em`，并能用 show-set 改写。

**操作步骤**：

1. 写：

   ```typ
   Normal text `inline raw`.
   ```

   编译观察行内 raw 字号约为正文的 80%。
2. 阅读本讲引用的 `ShowSet` 源码，确认 `TextSize(Em::new(0.8).into())`。
3. 加一条 show-set 把它放大：

   ```typ
   #show raw: set text(size: 1em)
   Normal text `inline raw`.
   ```

**需要观察的现象**：默认 raw 较小；加 show-set 后与正文等大。

**预期结果**：与源码 `0.8em` 一致；show-set 能覆盖默认（因为 show-set 产生的样式作用在更内层）。

#### 4.5.5 小练习与答案

**练习 1**：装饰元素为什么都标 `Locatable` + `Tagged`？

**答案**：装饰需要在排版后、知道每个文本片段的精确位置与字形轮廓时才能绘制（`evade` 还要避开字形下凹处）。`Tagged` 让元素获得文档位置、`Locatable` 让它能被定位/查询，二者配合使装饰绘制阶段能找到对应的内容范围。

**练习 2**：为什么 raw 默认字号是 `0.8em` 而不是 `1em`？

**答案**：等宽字体（monospace）在视觉上通常比同字号的非等宽字体显得更大、更松，因此 Typst 把 raw 默认缩到 80% 以与正文视觉协调（见 raw.rs 顶部 Styling 注释）。用户可用 `#show raw.where(block: true): set text(1em / 0.8)` 重置块级 raw。

---

## 5. 综合实践

本实践串联本讲四个核心：**OpenType 特性、语言、断行成本、智能引号**。设计一份「排版参数对照表」文档。

**任务**：写一个 Typst 文档，分四段，每段用注释标出它依赖的本讲机制，并对照源码解释行为：

```typ
// ① OpenType 特性：关连字、开老式数字
#set text(font: "Noto Sans", ligatures: false, number-type: "old-style")

// ② 语言与方向：阿拉伯语 RTL
#set text(lang: "ar")
بسم الله

// ③ 断行成本：放大断字成本，减少连字符
#set par(justify: true)
#set text(lang: "en", costs: (hyphenation: 500%))
#lorem(40)

// ④ 智能引号：法语 guillemet
#set text(lang: "fr")
"C'est typographique."
```

**完成后请回答**：

1. 第①段对应的 `features()` 输出里有哪几个 tag？（`liga`/`clig`=0、`onum`=1）
2. 第②段为何整段右对齐式地从右排起？（`Lang::ARABIC` 在 `dir()` 的 RTL 分支）
3. 第③段相比默认设置，断字变多了还是少了？为什么？（少了，因为 `hyph_cost = 135 × 5.0`）
4. 第④段的引号被替换成了什么字符？（`« C'est typographique. »`，单引号变撇号 `’`）

这个任务把「用户写的一行 set 规则」与「源码里的函数/枚举/常量」一一对应起来，是检验本讲掌握程度的最佳方式。若某项行为与预期不符，回到对应模块的源码精读重新核对。

---

## 6. 本讲小结

- **`features()`**（[src/text/mod.rs:1396-1469](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1396-L1469)）把 `TextElem` 上的字体特性 ghost 字段翻译成 `(OpenType tag, value)` 列表，遵循「默认开的特性仅在关闭时写入、默认关的仅在开启时写入」以保持列表精简。
- **`Lang`/`Region`/`WritingScript`** 用定长字节表示（`Copy`+可哈希），三者共同决定塑形语言（`language()` 拼成 BCP 47）、默认方向（`Lang::dir`）与本地化名称（`LocalName` 回退到英语）。
- **断行**分三层：`LinebreakElem` 是手动断点、`Linebreaks` 枚举选算法、`Costs`（`#[fold]`，用 `Option::or` 覆盖式折叠）给最优化算法提供 `hyphenation`/`runt`/`widow`/`orphan` 四项权重，真正消费在 typst-layout。
- **智能引号**用零前瞻状态机 `SmartQuoter`（位图引号栈）判断开闭，由 `SmartQuotes::get` 按 `lang`/`region` 查表选字符，是语言相关排版的典型案例。
- **装饰元素**（underline/overline/strike/highlight）结构同构，最终摊平成 `DecoLine` 运行期数据；**`RawElem`** 通过 `ShowSet` 主动反向改写 `TextElem` 的 ghost 字段（字号 `0.8em`、关连字、固定字体），是内置 show-set 规则的范例。

---

## 7. 下一步学习建议

- 本讲是 u7「文本系统」单元的收尾。接下来进入 **u8 文档模型**（`Document`/`ParElem`/`Heading`/`List`/`Figure`/`Outline`），届时你会看到 `ParElem` 上的 `justify`/`linebreaks`/`leading` 字段如何与本讲的 `Costs`、`Linebreaks` 联动。
- 想深入断行算法本身，可跳读 `crates/typst-layout/src/inline/linebreak.rs`，对照本讲的 `CostMetrics::compute` 看 `DEFAULT_HYPH_COST`/`DEFAULT_RUNT_COST` 如何进入行级最优化。
- 想理解塑形（shaping）如何使用本讲产出的 `features()` 与 `language()`，可阅读 u7-l2 提到的 `Font::instantiate_impl` 与 rustybuzz 调用链。
- 本地化（`LocalName`）与内省的关系将在 **u9 内省与上下文** 进一步展开——`Locatable`/`Tagged` 元素如何获得位置是那里的核心。
