# 数学元素：frac、matrix、attach、accent、lr、root

## 1. 本讲目标

上一讲（u10-l1）我们建立起了 Typst 数学子系统的骨架：`math` 模块以「子模块挂载式」注册到全局 `math` 键下，`EquationElem` 作为数学内容容器，而 `Mathy` 标记 trait 让求值器识别「裸数学元素」并自动包进公式。本讲要进入这些「裸数学元素」本身。

具体来说，学完本讲你应该能够：

- 说出 `FracElem`、`MatElem`/`VecElem`、`AttachElem`、`AccentElem`、`LrElem`、`RootElem` 各自的字段构成与构造方式；
- 解释 `MathClass`（数学类）是什么，以及它如何同时决定**符号周围的间距**与**上下标是挂在脚标位还是上下方**；
- 看懂 `ClassElem` 如何强制改变一个符号的类，从而改变它的排版行为；
- 理解贯穿全讲的统一结论：`typst-library` 只负责「定义元素 + 归一化配置数据」，真正把内容排成 `Frame`（伸缩定界符、堆叠分子分母、摆放上下标）的算法住在行为 crate `typst-math`，运行期经 `Routines` 回调。

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个概念。

### 2.1 数学元素都是 `Mathy` 元素

本讲涉及的每一个 `#[elem(...)]`，括号里都会出现 `Mathy` 这个词，例如：

[src/math/frac.rs:L24-L25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/frac.rs#L24-L25)：`FracElem` 的定义头上挂着 `Mathy`。

`Mathy` 是一个空的标记 trait（marker trait）：

[src/math/mod.rs:L110-L111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L110-L111)：`pub trait Mathy {}`。

它的作用在 u10-l1 已经讲过：求值器看到任何实现了 `Mathy` 的元素出现在数学模式之外时，会自动用 `EquationElem` 把它包起来。本讲的全部元素都属于这一类。

### 2.2 什么是「数学类」（MathClass）

Unicode 给每个数学符号都分配了一个「类」，记录在 `unicode-math-class` 这个外部 crate 里。常见类有：

| 类名 | 含义 | 典型字符 |
|------|------|----------|
| `Normal` / `Alphabetic` | 普通字符、字母 | `a`、`x` |
| `Binary` | 二元运算符 | `+`、`×` |
| `Relation` | 关系符 | `=`、`<`、`≤` |
| `Opening` / `Closing` | 左/右定界符 | `(`、`)`、`[` |
| `Fence` | 居中栅栏符 | `|` |
| `Large` | 大型运算符 | `∑`、`∫` |
| `Punctuation` | 标点 | `,` |
| `Unary` | 一元运算符 | 负号 |
| `Vary` | 可变运算符（视上下文当二元或一元） | `−`、`±` |

这个类是 Typst 数学排版的核心信号：它同时决定了**符号前后留多少间距**和**这个符号上的上下标该怎么摆**。本讲会反复用到它。

### 2.3 间距常量

Typst 在 [src/math/mod.rs:L35-L40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L35-L40) 定义了五个以 `Em`（相对字号）为单位的间距常量，它们会在「类→间距」的映射表里被反复引用：

- `THIN` = 1/6 em
- `MEDIUM` = 2/9 em
- `THICK` = 5/18 em
- `QUAD` = 1 em
- `WIDE` = 2 em

这些 `Em` 是相对长度，最终要乘上当前字号才得到像素——回顾 u6-l1，`Em::resolve` 必须吃 `StyleChain` 取字号。

### 2.4 统一结论（贯穿全讲）

本 crate 的 `math` 元素大多是「**只定义元素结构 + 把用户的灵活输入归一化为规整数据**」。真正把数据排成像素帧的算法不在本 crate。每当我们需要排版时，都是经 `Routines` 函数指针回调到 `typst-math`。你会在下面反复看到这一模式。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [src/math/frac.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/frac.rs) | 分数 `FracElem`、二项式 `BinomElem`、分数样式 `FracStyle` |
| [src/math/matrix.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs) | 向量 `VecElem`、矩阵 `MatElem`、分段 `CasesElem`，以及定界符抽象 `Delimiter`/`DelimiterPair`/`Augment` |
| [src/math/attach.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs) | 上下标附加 `AttachElem`、强制脚本/极限 `ScriptsElem`/`LimitsElem`、拉伸 `StretchElem`、极限判定 `Limits` |
| [src/math/accent.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/accent.rs) | 重音 `AccentElem`、重音字符 `Accent`、自动生成的重音函数表 |
| [src/math/lr.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/lr.rs) | 定界符伸缩 `LrElem`、居中栅栏 `MidElem`、`floor`/`ceil`/`abs` 等函数 |
| [src/math/root.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/root.rs) | 根式 `RootElem`、平方根函数 `sqrt` |
| [src/math/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs) | 类元素 `ClassElem`、间距常量、`module()` 装配 |
| [src/math/op.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/op.rs) | 文本运算符 `OpElem`，含一个 `ClassElem` 的真实用例（`dif`） |
| [src/math/ir/process.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/process.rs) | 「类→间距」映射表 `spacing()`、可变运算符升格为 `Binary` 的逻辑（数学 IR，详见 u10-l3） |

> 提示：`ir/` 子模块（数学中间表示）整体属于 u10-l3 的内容。本讲只在第 4.1 节借用它的 `spacing()` 表来回答「类如何决定间距」，其余 IR 细节留待 u10-l3。

## 4. 核心概念与源码讲解

本讲把七个最小模块组织成五节：先讲贯穿全讲的「数学类与间距」（4.1），再依次讲分数（4.2）、矩阵族（4.3）、上下标附加（4.4）、重音/定界符/根式（4.5）。

---

### 4.1 数学类与间距：ClassElem 与 MathClass

#### 4.1.1 概念说明

每个数学符号天生带一个 `MathClass`，它从 Unicode 属性查得（`typst_utils::default_math_class`）。但有时用户想**强行改变**某个符号的类。最典型的例子是微分算子 `d`：在 `dx` 里，`d` 应当被当作「一元运算符」而非普通字母，这样它和 `x` 之间才会有正确的间距。

`ClassElem` 就是为此而生——它把任意内容**包装成指定的类**：

[src/math/mod.rs:L141-L150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L141-L150)：`ClassElem` 两个字段——`class: MathClass`（要套用的类）和 `body: Content`（被包装的内容）。

文档注释（第 124–130 行）点明了两件受类影响的事：

> The class of a symbol defines the way it is laid out, including **spacing around it**, and **how its scripts are attached by default**.

也就是说，类同时控制**间距**和**上下标位置**两件事。我们这一节先看间距，上下标位置留到 4.4。

#### 4.1.2 核心流程

「类 → 间距」的映射表不在 `mod.rs`，而在数学 IR 的预处理阶段 [src/math/ir/process.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/process.rs) 的 `spacing()` 函数里。它的核心是一个模式匹配：取**左符号的右类** `l.rclass()` 与**右符号的左类** `r.lclass()`，决定在两者之间插入多少间距。

伪代码（省略 script 字号的特判）：

```
match (左符号的右类, 右符号的左类):
    (_, Punctuation)        → 不插间距   # 标点前不留
    (Punctuation, _)        → 左右 THIN   # 标点后留薄间距
    (Opening, _) | (_, Closing) → 不插间距   # 左括号后、右括号前不留
    (Relation, Relation)    → 不插间距   # 关系符紧挨关系符
    (Relation, _)           → 左右 THICK  # 关系符周围留厚间距
    (_, Relation)           → 右左 THICK
    (Binary, _)             → 左右 MEDIUM # 二元运算符周围留中等间距
    (_, Binary)             → 右左 MEDIUM
    (Large, Opening|Fence)  → 不插间距   # 大型运算符遇到开括号不留（TeXBook p170）
    (Large, _)              → 左右 THIN
```

> 这里的 THIN/MEDIUM/THICK 就是 2.3 节那几个 `Em` 常量。

此外，预处理阶段还有一步**可变运算符升格**：`Vary` 类的符号（如 `−`、`±`）当前面跟着普通字符/字母/右括号/栅栏时，会被升格为 `Binary`：

[src/math/ir/process.rs:L216-L229](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/process.rs#L216-L229)：`Vary` 在合适语境下被 `set_class(MathClass::Binary)`。

这正是为什么 `a - b` 中间的减号有中等间距（当二元），而 `-b`（单独负号）没有（当前面无字符，仍是 `Vary`/`Unary`）。

#### 4.1.3 源码精读

**`spacing()` 映射表本体**——这是「类如何决定间距」的直接答案：

[src/math/ir/process.rs:L277-L309](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/process.rs#L277-L309)：`spacing()` 函数，逐类用 `set_rspace`/`set_lspace` 在符号上写下左右间距。

注意它在 `MathSize::Script`（上下标尺寸）下会跳过大部分间距——这就是为什么小字号的脚本里几乎不留间距。

**`ClassElem` 的真实用例——微分算子 `dif`**：

[src/math/op.rs:L49-L54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/op.rs#L49-L54)：`dif` 用 `ClassElem::new(MathClass::Unary, upright(SymbolElem::packed(d)))` 把正体的 `d` 强制标成一元类，并在前面插一个弱薄间距 `THIN`。

注意它用的是 `MathClass::Unary`——一元类不在上面那张间距表里特判，所以 `dif` 自带一个 `HElem` 来手动控制间距。这是一个很好的「自定义类 + 手动微调」范例。

**装配**：`ClassElem` 在 `module()` 中注册：

[src/math/mod.rs:L72-L72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L72)：`math.define_elem::<ClassElem>()`。

#### 4.1.4 代码实践

> 这是本讲的核心实践任务（对应规格中的 practice_task 第一部分）。

1. **实践目标**：通过源码确认 `relation`/`binary` 等类如何决定符号间距，并亲手用 `math.class` 改变一个符号的类。
2. **操作步骤**：
   - 打开 [src/math/ir/process.rs:L277-L309](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/process.rs#L277-L309)，逐条读 `spacing()` 的 `match` 臂。
   - 再读 [src/math/mod.rs:L124-L150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L124-L150) 的 `ClassElem` 文档与字段。
   - 写一段 Typst 标记对比：

   ```typst
   #let loves = math.class("relation", sym.suit.heart)

   $x loves y and y loves 5$
   $x sym.suit.heart y$   // 对照：未改类时 ♥ 周围几乎没有间距
   ```

3. **需要观察的现象**：第一行里 `♥` 被当成关系符，它和 `x`、`y` 之间会出现 `THICK`（5/18 em）厚间距，视觉上像 `=` 那样两边留白；第二行的裸 `♥` 则紧贴字母。
4. **预期结果**：`relation` 类触发 `(Relation, _) => THICK` 与 `(_, Relation) => THICK` 两条臂；`binary` 类则触发 `MEDIUM`。渲染效果**待本地验证**（请用 `typst compile` 实际编译查看）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `$a - b$` 的减号两边有中等间距，而 `$-b$` 的负号几乎不空？

> **答案**：减号 `−` 的 Unicode 类是 `Vary`。在 [src/math/ir/process.rs:L216-L229](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/process.rs#L216-L229) 中，当它前面有普通字符/字母/右括号/栅栏时会被升格为 `Binary`，从而触发 `MEDIUM` 间距；当它出现在最前面（前面无此类字符），不被升格，于是没有中等间距。

**练习 2**：若想强行让 `×` 在所有位置都当作一元符号（去掉周围间距），该用哪个函数？

> **答案**：用 `math.class("unary", times)` 把它包起来。注意 `Unary` 不在 `spacing()` 表里，所以不会自动留间距；如需精确控制，再像 `dif` 那样手动插 `HElem`。

---

### 4.2 分数与二项式：FracElem 与 BinomElem

#### 4.2.1 概念说明

分数是最常见的数学结构。Typst 给它专门的语法：在数学模式里用斜杠 `/` 把相邻表达式变成分数（多原子要用圆括号分组）。`FracElem` 就是分数元素。

它有一个关键的设计点：**分数有多种排布形态**——经典堆叠式（分子分母中间一条横线）、斜线式（用斜杠分隔）、行内式（分子分母平铺）。这由 `FracStyle` 控制。

`BinomElem`（二项式）结构与分数同源，但只展示上、下两个角标，外面套括号，表达 $\binom{n}{k}$。

#### 4.2.2 核心流程

`FracElem` 的装配与字段：

1. 用户写 `$a/b$` 或 `$frac(a, b)$`，解析器/函数构造出 `FracElem { num, denom, style, ... }`；
2. `style` 默认是 `Vertical`（堆叠带横线），可被 `set math.frac(style: ...)` 全局改写；
3. 解析器在把 `(a+b)/b` 变成分数时会**剥掉分组圆括号**，并用两个 `#[internal]` 字段记下「分子/分母原本被括号包过」这一事实，供后续选择样式时参考；
4. 真正画出那条横线、把分子分母居中堆叠的算法在 `typst-math`。

分数的内外边距常量 `FRAC_PADDING = 0.1 em` 定义在本 crate，供行为 crate 取用。

#### 4.2.3 源码精读

**`FracElem` 字段**：

[src/math/frac.rs:L24-L111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/frac.rs#L24-L111)：`FracElem` 的全部字段。

关键字段解读：

- `num`、`denom`：分子、分母，均为 `#[required] Content`。
- `style: FracStyle`：`#[default(FracStyle::Vertical)]`，可 `set`。
- `num_deparenthesized`、`denom_deparenthesized`：`#[internal]` 且 `#[parse(None)]`、`#[default(false)]`——这两个字段**不会**来自用户参数（解析时填 `None`），而是由解析器在剥括号时写入，记录「括号被剥过」。

**`FracStyle` 枚举**：

[src/math/frac.rs:L113-L124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/frac.rs#L113-L124)：三种分数形态。`Vertical` 为默认（`#[default]`）。

**内边距常量**：

[src/math/frac.rs:L9-L9](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/frac.rs#L9)：`FRAC_PADDING: Em = Em::new(0.1)`。

**`BinomElem`**：

[src/math/frac.rs:L133-L151](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/frac.rs#L133-L151)：`upper` 必填，`lower` 是 `#[variadic]` 且用 `#[parse]` 守护「至少一个下标」，否则 `bail!("missing argument: lower")`——这阻止了无意义的单项二项式。

#### 4.2.4 代码实践

1. **实践目标**：观察 `FracStyle` 三种形态的差异，以及括号剥离行为。
2. **操作步骤**：编译下面标记，对照三种 `style`：

   ```typst
   $ frac(x, y, style: "vertical") $
   $ frac(x, y, style: "skewed") $
   $ frac(x, y, style: "horizontal") $

   #set math.frac(style: "horizontal")
   $ (a + b) / b $   // horizontal 时不剥括号
   #set math.frac(style: "vertical")
   $ (a + b) / b $   // vertical 时括号被剥
   ```

3. **需要观察的现象**：`vertical` 是分子在上、分母在下加横线；`skewed` 是斜杠分隔；`horizontal` 是平铺且**保留** `(a+b)` 的括号，而 `vertical`/`skewed` 会**剥掉**分组括号。
4. **预期结果**：剥括号与否由 `num_deparenthesized` 字段标记，行为 crate 据此决定。渲染效果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`num_deparenthesized` 为什么用 `#[internal]` 和 `#[parse(None)]`？

> **答案**：`#[internal]` 让它对用户不可见（不能 `set`/不能出现在文档参数表里）；`#[parse(None)]` 表示从函数参数解析时永远取 `None`。它的真实值由语法解析器在构造元素时直接写入，记录「分子原本被圆括号包过且已被剥除」这一历史事实，仅供后续排版参考。

**练习 2**：`binom(n)`（只给一个参数）会发生什么？

> **答案**：会报错 `"missing argument: lower"`。见 [src/math/frac.rs:L142-L149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/frac.rs#L142-L149) 的 `#[parse]` 守护：`lower` 是 `#[variadic]`，但解析逻辑在 `values.is_empty()` 时主动 `bail!`。

---

### 4.3 矩阵、向量与分段：VecElem、MatElem、CasesElem 与 Delimiter

#### 4.3.1 概念说明

这一族元素都由「**一组内容 + 一对定界符**」构成：

- `VecElem`：列向量，内容纵向排列，默认包圆括号；
- `MatElem`：矩阵，内容是二维网格，用逗号分列、分号分行；
- `CasesElem`：分段函数（case distinction），默认包花括号，分支纵向排列。

三者的差异主要体现在「内容的维度」和「默认定界符」上，而它们共享同一套定界符抽象 `Delimiter` / `DelimiterPair`。

#### 4.3.2 核心流程

矩阵元素最值得看的是 `MatElem` 的 `#[parse]`：它要把用户灵活的输入**归一化成一个规整的二维 `Vec<Vec<Content>>`**：

1. 收集所有参数为 `Spanned<Value>`；
2. 若其中有任意一个是数组，就按「数组 = 一行」解析（二维输入）；否则全部当成单行；
3. 找出最宽的一行 `width`，把短行用 `Content::empty()` 补齐，得到矩形网格。

向量与分段则简单些，子项是一维 `Vec<Content>`。

定界符方面，`Delimiter::char(c)` 会校验 `c` 必须是 `Opening`/`Closing`/`Fence` 类的字符，否则报错；`find_matching` 推断右定界符（圆括号配对查表，其余按 Unicode 码点 `+1`/`-1` 推开/闭配对）。

#### 4.3.3 源码精读

**`VecElem`**：

[src/math/matrix.rs:L32-L68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L32-L68)：`delim`（默认 `DelimiterPair::PAREN`）、`align`（默认 `Center`）、`gap`（默认 `DEFAULT_ROW_GAP`）、`#[variadic] children`。

**`MatElem`**：

[src/math/matrix.rs:L91-L222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L91-L222)：注意几个要点：

- `augment: Option<Augment>` 带 `#[fold]`——多条 augment 配置可折叠合并（见 `Augment::fold`，[src/math/matrix.rs:L380-L395](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L380-L395)）。回顾 u4-l1，`Fold` 让同名字段相加而非覆盖。
- `gap` 是 `#[external]`（只出现在文档，不进 struct），真正的 `row_gap`/`column_gap` 各自带 `#[parse]`，且都先读 `gap` 再读自己的细分名，`row-gap`/`column-gap` 优先于 `gap`：
  [src/math/matrix.rs:L172-L187](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L172-L187)。
- `rows` 的 `#[parse]` 做上面说的二维归一化：
  [src/math/matrix.rs:L197-L221](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L197-L221)。

**`CasesElem`**：

[src/math/matrix.rs:L237-L273](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L237-L273)：默认 `DelimiterPair::BRACE`（花括号），多了 `reverse: bool` 字段（方向反转）。

**定界符抽象 `Delimiter`**：

[src/math/matrix.rs:L281-L325](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L281-L325)：`char()` 校验类、`find_matching()` 推断配对。

[src/math/matrix.rs:L329-L369](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L329-L369)：`DelimiterPair` 与其 `PAREN`/`BRACE` 常量。

**间距常量**：

[src/math/matrix.rs:L15-L16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L15-L16)：`DEFAULT_ROW_GAP = 0.2 em`，`DEFAULT_COL_GAP = 0.5 em`。

#### 4.3.4 代码实践

1. **实践目标**：理解 `MatElem` 如何把二维输入归一化为矩形网格，并体会定界符校验。
2. **操作步骤**：
   - 阅读 [src/math/matrix.rs:L197-L221](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L197-L221) 的 `rows` 解析逻辑。
   - 试着写一个「参差」矩阵（各行长度不同），观察补齐行为：

   ```typst
   $ mat(1, 2, 3; 4, 5) $          // 第二行短，会被 Content::empty() 补齐
   #set math.mat(delim: "[")
   $ mat(1, 2; 3, 4) $
   $ mat(delim: none, 1, 2; 3, 4) $ // 无定界符
   ```

3. **需要观察的现象**：第一行的矩阵被自动补成 2×3，缺位为空；`delim: "["` 把左右推断为 `[`/`]`；`delim: none` 没有定界符。
4. **预期结果**：定界符的伸缩（拉到与内容等高）由行为 crate 完成；本 crate 只交付规整的 `rows` 与 `delim` 配置。渲染效果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`Delimiter::char('a')` 会成功吗？为什么？

> **答案**：不会。`'a'` 的默认数学类是 `Alphabetic`，不属于 `Opening`/`Closing`/`Fence`，所以 [src/math/matrix.rs:L297-L305](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L297-L305) 会 `bail!("invalid delimiter: \"a\"")`。

**练习 2**：`augment` 为什么用 `#[fold]`？

> **答案**：因为用户可能在不同层级（如 `set` 与函数实参）各指定一部分 augment（一条竖线、一条横线、一种 stroke）。`Fold` 让这些配置合并而非后者覆盖前者，见 [src/math/matrix.rs:L380-L395](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/matrix.rs#L380-L395) 的 `Augment::fold`。

---

### 4.4 上下标附加：AttachElem 及其控制元素与 Limits

#### 4.4.1 概念说明

数学表达式里，`x^2` 的 `2` 是上标，`∑_{i=1}` 的 `i=1` 是「极限」（limit，挂在运算符正下方）。这两种「附件」在 Typst 里都由 `AttachElem` 表达。

关键设计：`AttachElem` 有六个槽位——`t`/`b`（上/下，智能定位）、`tl`/`bl`（左上/左下）、`tr`/`br`（右上/右下）。其中 `t`/`b` 的位置**不是固定的**，而是**根据底座（base）的 `MathClass` 智能决定**摆成脚本（脚标位）还是极限（正上下方）。

这正好把本节与 4.1 的 `MathClass` 串了起来：**类不仅决定间距，还决定上下标位置**。

为了让用户能覆盖这种智能判断，Typst 提供 `ScriptsElem`（强制脚本）和 `LimitsElem`（强制极限）两个包装元素；还有 `PrimesElem`（撇号）和 `StretchElem`（拉伸底座）。

#### 4.4.2 核心流程

判定 `t`/`b` 摆脚本还是极限，由 `Limits` 枚举的三态决定：

```
Limits::Never   → 永远当脚本（脚标位）
Limits::Display → 仅在 display 数学里当极限，否则当脚本
Limits::Always  → 永远当极限（正上下方）
```

而默认采用哪一态，取决于底座字符的 `MathClass`：

- `Large` 类（如 `∑`、`∏`）→ `Display`（display 模式下挂上下方）；
  - 例外：积分符 `∫..∳`、`⨋..⨜` → `Never`（积分的上下标挂脚标位）；
- `Relation` 类（如 `=`、`→`）→ `Always`（永远挂上下方）；
- 其余 → `Never`（脚本位）。

用户可用 `limits(base)`/`scripts(base)` 强制覆盖。

`Limits::active(styles)` 最终拍板：`Always` 恒真；`Display` 仅当当前数学尺寸是 `MathSize::Display`；`Never` 恒假。

> 真正把附件画到对应位置的算法在 `typst-math`；本 crate 只交付 base、六个附件槽，以及「该用脚本还是极限」的判定结果。

#### 4.4.3 源码精读

**`AttachElem` 六槽位**：

[src/math/attach.rs:L19-L49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L19-L49)：`base` 必填，`t`/`b`/`tl`/`bl`/`tr`/`br` 均为 `Option<Content>`。注释说明 `t`/`b` 是「smartly positioned」。

**`Limits` 枚举——判定的核心**：

[src/math/attach.rs:L148-L196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L148-L196)：三态枚举与三个查询方法。

重点方法：

- `for_char_with_class`（[L165-L177](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L165-L177)）：按字符 + 类给默认判定。`Large`（非积分）→ `Display`，`Relation` → `Always`，其余 → `Never`。
- `for_class`（[L180-L186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L180-L186)）：只按类判定（用于非单字符底座）。
- `active`（[L189-L195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L189-L195)）：`Display` 态查 `EquationElem::size == MathSize::Display`。

**积分特判**：

[src/math/attach.rs:L199-L201](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L199-L201)：`is_integral_char` 圈定两段积分码点区间。

**控制元素**：

[src/math/attach.rs:L73-L97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L73-L97)：`ScriptsElem`（强制脚本）、`LimitsElem`（强制极限，带 `inline` 开关，默认 `true`——即默认连行内公式也强制极限，故全局 `show` 规则常需关掉它）。

[src/math/attach.rs:L61-L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L61-L66)：`PrimesElem { count }`——撇号语法 `a'''` 会把上标「挤」到更高一层。

[src/math/attach.rs:L114-L145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L114-L145)：`StretchElem`——拉伸字符（如把 `=` 拉长以承载附件），`size` 相对于「字符与其附件的最大尺寸」。

#### 4.4.4 代码实践

> 这对应规格中 practice_task 的第二部分：追踪 attach 如何把上下标挂到 base 上。

1. **实践目标**：确认「底座的类决定 `t`/`b` 摆脚本还是极限」，并学会强制覆盖。
2. **操作步骤**：
   - 通读 [src/math/attach.rs:L148-L201](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L148-L201)，画出「类 → `Limits` → 脚本/极限」的判定链。
   - 编译下面四组对比（行内 vs 块级）：

   ```typst
   // Large 类：display 下挂上下方
   $ sum_(i=1)^n a_i $        // 块级：上下标在正上/正下
   #math.inline[$ sum_(i=1)^n a_i $]  // 行内：上下标在脚标位

   // 关系类：永远挂上下方
   $ x =^"def" y $

   // 强制覆盖
   $ scripts(sum)_1^2 $       // 强制 sum 用脚标位
   $ limits(A)_1^2 $          // 强制 A 用上下方
   ```

3. **需要观察的现象**：`sum`（`Large` 类）在块级公式里把 `i=1`/`n` 摆在正下/正上（`Display` 态激活），在行内公式里摆成脚标（`Display` 态不激活）；`=` 是 `Relation`，即便行内也挂上下方（`Always`）；`scripts`/`limits` 能覆盖默认。
4. **预期结果**：判定逻辑全部落在 [src/math/attach.rs:L165-L195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L165-L195)。渲染效果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `$integral_0^1$` 的 `0`/`1` 挂在脚标位而不是正下方，即便在块级公式里？

> **答案**：积分符属于 `Large` 类，但 [src/math/attach.rs:L166-L173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L166-L173) 对积分符特判返回 `Limits::Never`，所以永远当脚本。这是排版惯例：积分号通常很高，上下标挂脚标位更紧凑。

**练习 2**：用 `LimitsElem` 做全局 show 规则时，为什么建议把 `inline` 设为 `false`？

> **答案**：`inline` 默认 `true`，意味着「行内公式也强制极限」。若通过 `show` 把某符号全局设为 `limits`，通常只希望在块级公式里挂上下方，行内仍想紧凑，故应 `limits(base, inline: false)`，见 [src/math/attach.rs:L85-L97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L85-L97)。

---

### 4.5 重音、定界符伸缩与根式：AccentElem、LrElem、RootElem

#### 4.5.1 概念说明

最后这一节把三个相对独立的元素放在一起讲，因为它们共享「**内容 + 一个修饰**」的简单结构：

- `AccentElem`：给底座加一个重音（hat、tilde、箭头……）。重音会被拉伸以盖住底座。
- `LrElem`：把一组内容（含左右定界符）包起来，让定界符纵向伸缩到与内容等高。`floor`/`ceil`/`abs`/`norm`/`round` 都是它的语法糖。
- `RootElem`：根式。`sqrt` 函数是平方根的语法糖。

三者都用一个 `Em` 常量表达「允许比目标短多少」的容差（`ACCENT_SHORT_FALL`、`DELIM_SHORT_FALL`），供行为 crate 在选 glyph 时回退。

#### 4.5.2 核心流程

**重音**：`AccentElem` 有 `base`、`accent`（一个 `Accent` 字符）、`size`（相对底座宽度，默认 100%）、`dotless`（默认 `true`，对 `i`/`j` 开启 OpenType `dtls` 特性以去掉上面的点）。`Accent` 在构造时被规范化为「组合用」码点（`normalize`），并能判断自己是顶部重音还是底部重音（`is_bottom`，用 ICU 的 `CanonicalCombiningClass`）。

一个有趣的设计：每个重音还会被**自动生成一个同名函数**（如 `arrow`、`hat`），所以用户写 `$arrow(a)$` 等价于 `$accent(a, ->)$`。这是 `FUNCS` 惰性表 + `create_accent_func_data` 干的。

**定界符伸缩**：`LrElem` 只有两个字段——`size`（相对内容高度，默认 100%）和 `body`。`body` 的 `#[parse]` 把多个位置参数用逗号拼起来。`floor` 等函数调用内部的 `delimited(body, left, right, size)`，构造一个内容为「左符 + body + 右符」的 `LrElem`。同样地，每个定界符对（如 `(` `)`、`|` `|`）也会被自动生成包装函数（`FUNCS` + `DELIMS` 表），让 `floor(x)`、`abs(x)` 甚至符号直呼都可用。

**根式**：`RootElem { index: Option<Content>, radicand: Content }`。`index` 是 `#[positional]`（可选位置参数，`root(3, x)` 的 `3`）。`sqrt(x)` 函数只是 `RootElem::new(radicand).pack()`。

#### 4.5.3 源码精读

**`AccentElem`**：

[src/math/accent.rs:L32-L171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/accent.rs#L32-L171)：`base`/`accent` 必填，`size` 默认 `Rel::one()`，`dotless` 默认 `true`。

**`Accent` 类型与重音表**：

[src/math/accent.rs:L174-L209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/accent.rs#L174-L209)：`normalize` 选组合码点、`is_bottom` 判底部重音。

[src/math/accent.rs:L219-L285](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/accent.rs#L219-L285)：`ACCENTS` 重音表 + `FUNCS` 惰性生成的重音函数（`create_accent_func_data` 在 `Bump` arena 上构造 `NativeFuncData`）。

[src/math/accent.rs:L329-L341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/accent.rs#L329-L341)：`Accent` 的 `cast!`，接受字符串、符号值、内容（`SymbolElem`）三种输入并归一化为单个码点。

**容差常量**：

[src/math/accent.rs:L18-L18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/accent.rs#L18)：`ACCENT_SHORT_FALL = 0.5 em`。

**`LrElem` 与 `delimited`**：

[src/math/lr.rs:L23-L38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/lr.rs#L23-L38)：`size` + `body`，`body` 的 `#[parse]` 把变参用逗号拼接。

[src/math/lr.rs:L269-L286](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/lr.rs#L269-L286)：`delimited()` 把内容拼成 `[左符, body, 右符]` 的 `Content::sequence` 再包进 `LrElem`。

**语法糖函数与自动生成表**：

[src/math/lr.rs:L57-L140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/lr.rs#L57-L140)：`floor`/`ceil`/`round`/`abs`/`norm` 五个 `#[func]`，各自调用 `delimited` 配不同定界符对。

[src/math/lr.rs:L160-L207](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/lr.rs#L160-L207)：`DELIMS` 定界符对表 + `FUNCS` 惰性生成的包装函数。

[src/math/lr.rs:L144-L157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/lr.rs#L144-L157)：`get_lr_wrapper_func`——让符号（如 `|`）能像函数一样被调用（`|x|`）。

**`MidElem`**（居中栅栏，伸缩到最近的 `lr()` 组）：

[src/math/lr.rs:L45-L50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/lr.rs#L45-L50)。

**容差常量**：

[src/math/lr.rs:L17-L17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/lr.rs#L17)：`DELIM_SHORT_FALL = 0.1 em`。

**根式**：

[src/math/root.rs:L11-L34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/root.rs#L11-L34)：`sqrt` 函数 + `RootElem { index, radicand }`。

#### 4.5.4 代码实践

1. **实践目标**：体会重音、定界符伸缩、根式三者的「内容 + 修饰」结构，以及自动生成的同名函数。
2. **操作步骤**：
   - 阅读 [src/math/accent.rs:L219-L285](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/accent.rs#L219-L285) 与 [src/math/lr.rs:L160-L207](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/lr.rs#L160-L207)，理解「表 + 惰性函数」模式。
   - 编译：

   ```typst
   $ grave(a) = accent(a, `) $       // 同名函数 vs accent 调用
   $ arrow(A B C) $                   // 重音拉伸盖住多字母底座
   $ hat(dotless: #false, i) $        // 保留 i 上的点

   $ abs(x/2) $   $ floor(x/2) $      // 定界符语法糖
   $ lr(] a + b [) $                  // 不匹配定界符手动伸缩

   $ sqrt(3 - 2 sqrt(2)) $
   $ root(3, x) $                     // 立方根
   ```

3. **需要观察的现象**：`arrow(A B C)` 的箭头被拉长盖住三个字母；`abs`/`floor` 的定界符随内容高度伸缩；`lr()` 能伸缩**不匹配**的定界符；`root(3, x)` 的 `3` 出现在根号左上角。
4. **预期结果**：拉伸/伸缩的具体 glyph 选择（含 `*_SHORT_FALL` 回退）由行为 crate 完成。渲染效果**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`$arrow(a)$` 是怎么变成「给 `a` 加箭头重音」的？它并没有直接定义名为 `arrow` 的函数。

> **答案**：见 [src/math/accent.rs:L244-L285](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/accent.rs#L244-L285)。`FUNCS` 在首次访问时遍历 `ACCENTS` 表，为每个重音用 `create_accent_func_data` 在 `Bump` arena 上构造一个 `NativeFuncData`，其闭包就是「取 `base`/`size`/`dotless`，构造 `AccentElem::new(base, Accent(accent)).pack()`」。`arrow` 这个名字由符号系统映射到该函数。

**练习 2**：`LrElem` 的 `body` 为何要用 `#[parse]` 把多个变参用逗号拼起来，而不是直接 `#[variadic]`？

> **答案**：因为 `lr()` 的语义是「把传入的所有内容（含定界符本身）视为一个整体序列，让其中的定界符随整体高度伸缩」。直接用 `#[variadic]` 会得到一个数组，而这里需要一个**单一** `Content`（序列），见 [src/math/lr.rs:L31-L37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/lr.rs#L31-L37)：第一个参数作 body 开头，其余用 `SymbolElem::packed(',') + arg` 追加。

---

## 5. 综合实践

把本讲的知识串起来：**用 `ClassElem` 自定义一个「关系符」，并让它带着上下标与重音，最后用矩阵组织起来**。

任务：写一段 Typst 标记，定义一个自定义关系符 `equiv`（用 `sym.dots.double` 之类符号充当），并：

1. 用 `math.class("relation", ...)` 让它两侧自动有 `THICK` 间距；
2. 用 `limits(...)` 让它上面的文字标签挂在正上方（验证 `Relation → Always` 的默认行为，再用 `limits` 二次确认）；
3. 给它套一个重音（`hat`），观察重音拉伸；
4. 把若干这样的表达式放进一个 `mat(...)` 矩阵，观察定界符随最高单元格伸缩。

参考写法（**示例代码**，非项目原有）：

```typst
#let equiv = math.class("relation", sym.eq.quest)

$ equiv^"定义" x $              // relation 默认挂上下方（Always）
$ hat(equiv) $                  // 重音盖住整个 class 元素
$ mat(
  equiv^"定义" x, y;
  a, hat(equiv);
) $
```

完成后，请回到源码核对：

- `equiv` 的间距来自 [src/math/ir/process.rs:L297-L299](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/process.rs#L297-L299) 的 `(Relation, _) / (_, Relation) => THICK`；
- 它的上下标位置来自 [src/math/attach.rs:L174-L177](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/attach.rs#L174-L177) 的 `Relation => Limits::Always`；
- 重音拉伸与矩阵定界符伸缩的算法在 `typst-math`，本 crate 只交付结构化数据。

> 渲染效果**待本地验证**。通过这个任务，你应该能清楚区分「本 crate 定义了什么」与「行为 crate 计算了什么」。

## 6. 本讲小结

- 本讲的全部元素都实现 `Mathy` 标记 trait，故可裸出现在数学模式中并自动包进 `EquationElem`。
- `MathClass`（数学类）是贯穿全讲的信号：它**同时**决定符号周围间距（`spacing()` 表：`Relation→THICK`、`Binary→MEDIUM`、标点→`THIN`、定界符→无）和上下标位置（`Large→Display`、`Relation→Always`、其余→`Never`，积分特判 `Never`）。
- `ClassElem` 用 `math.class("relation", body)` 强制改变一个内容的类，`op.rs` 的微分算子 `dif` 是真实用例。
- `FracElem`/`BinomElem`：字段 + `FracStyle` 三态 + 解析器写入的 `_deparenthesized` 内部标记；`MatElem`/`VecElem`/`CasesElem`：共享 `DelimiterPair`，`MatElem` 用 `#[parse]` 把灵活输入归一化为矩形 `rows`。
- `AttachElem` 六槽位 + `Limits` 三态判定；`ScriptsElem`/`LimitsElem`/`StretchElem`/`PrimesElem` 提供控制与拉伸。
- `AccentElem`/`LrElem`/`RootElem` 都是「内容 + 一个修饰」的简单结构；重音与定界符会**惰性生成同名函数**（`FUNCS` 表），让 `arrow(a)`、`abs(x)`、符号直呼都能工作。
- **统一结论**：本 crate 只定义元素与归一化配置数据（含各类 `*_SHORT_FALL`、`*_GAP`、`*_PADDING` 等 `Em` 常量），真正画线、堆叠、伸缩、摆放附件的算法住在 `typst-math`，经 `Routines` 回调。

## 7. 下一步学习建议

- **u10-l3（数学中间表示 IR）**：本讲多次引用的 [src/math/ir/process.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/process.rs) 与 `item.rs`、`multiline.rs` 正是下一讲的主角。你将看到 `Content` 如何被转成可布局的 `MathItem`、`spacing()` 如何在预处理阶段把间距写进每个 item、以及多行公式 `&`/`&&` 对齐如何处理。
- **回顾 u9（内省）**：`EquationElem` 同时实现 `Count`/`Refable`/`Outlinable`，公式编号与标题号、图表号同源，理解这一点能让你把数学放进更大的文档编号体系。
- **若要扩展**：想新增一个数学元素，可仿照 `RootElem`（最简单的双字段元素）起步，记得在元素头上加 `Mathy`，并在 `module()` 的 [src/math/mod.rs:L46-L74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L46-L74) 区域 `define_elem::<你的元素>()`。完整的「新增元素/函数」流程见 u12-l3。
