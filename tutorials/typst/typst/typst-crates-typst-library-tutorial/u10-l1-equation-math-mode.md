# EquationElem 与数学模式

## 1. 本讲目标

本讲进入 Typst 的**数学公式（math）子系统**，从入口出发回答四个问题：

1. 用户写的 `$ ... $` 公式背后，对应的标准库模块 `math` 是如何被装配出来的？里面装了哪些独有的元素和函数？
2. 公式的容器元素 `EquationElem` 有哪些字段？`block`/`size`/`numbering` 这些配置如何决定公式是"行内"还是"整块独占一行"？
3. 什么是"数学元素"？`Mathy` 这个空 trait 起什么作用？
4. 数学字号（display / script / scriptscript）是怎么层层缩小的？这和上一单元学过的 `TextSize::resolve` 是什么关系？

学完后，你应该能够：

- 说出 `math::module()` 注册的全部元素与函数的大致分类；
- 解释 `EquationElem` 的"几乎全是 ghost 字段"的设计，以及 `ShowSet` 如何根据 `block` 自动切换字号档位；
- 理解 `MathSize` 四档与 `script_scale` 两个百分比常数如何共同决定每个字形（glyph）的最终字号；
- 在源码中找到数学字体回退链（`families`）与五条间距常数（`THIN`/`MEDIUM`/`THICK`/`QUAD`/`WIDE`）。

## 2. 前置知识

本讲是「文本系统」单元（第 7 单元）的直接延伸，建议你已经读过：

- **u7-l1（TextElem 与字体变体）**：理解 `TextElem` 的"几乎全是 ghost 字段"——真正的文本节点只存 `text: EcoString`，其余字体/字号/字重字段都是 `#[ghost]`、只活在 `StyleChain` 里。本讲的 `EquationElem` 延续了这一思路，字号、字重、字体都被处理成 ghost 字段。
- **u4-l1（Styles、StyleChain 与 fold/resolve）**：理解 `Fold`（同类型折叠，如 `TextSize` 把多个线性函数相乘）与 `Resolve`（吃整条样式链、把相对值解析成绝对值）。本讲会反复用到 `styles.resolve(TextElem::size)`。
- **u3-l3（elem 宏、字段系统与 Packed）**：理解 `#[required]`/`#[default]`/`#[ghost]`/`#[synthesized]`/`#[fold]`/`#[internal]` 等字段标注，以及能力 trait（`Synthesize`/`ShowSet`/`Count`/`Refable`/`Outlinable`/`LocalName`）必须写在 `Packed<E>` 上。
- **u8-l2 / u8-l3（标题、引用、编号）**：理解 `Count`/`Refable`/`Outlinable` 三能力如何让标题号、`@ref` 引用号、`#outline()` 目录号"同源不冲突"。本讲的公式编号完全复用这套机制。

贯穿本讲的核心结论（与前面所有讲义一致）：**`typst-library` 只负责"定义元素 + 归一化配置数据"，真正把数学内容排成 `Frame` 的算法住在行为 crate `typst-math`，运行期经 `Routines` 函数指针回调。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`src/math/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs) | math 子模块总入口：装配 `module()`、定义间距常数、定义 `Mathy` trait、`ClassElem`/`AlignPointElem`、数学字体回退 `families()`。 |
| [`src/math/equation.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/equation.rs) | 公式容器元素 `EquationElem` 及其 `Synthesize`/`ShowSet`/`Count`/`LocalName`/`Refable`/`Outlinable` 实现。 |
| [`src/math/style.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/style.rs) | 数学字号枚举 `MathSize`、字号档位函数（`display`/`inline`/`script`/`sscript`）、各级上下标/分子分母的样式推导函数。 |
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs) | `LibraryBuilder::build()` 调用 `math::module()` 构造数学模块，`global()` 把它挂为 `math` 子命名空间。 |
| [`src/math/ir/resolve.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/resolve.rs) | 数学中间表示（IR）的解析层，多处用 `styles.resolve(TextElem::size)` 取基础字号，体现"数学字号 = 文本字号 × 缩放系数"。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**math::module() 装配**、**EquationElem 容器**、**Mathy trait**、**数学字号/间距常量与字体回退**。

### 4.1 math::module()：数学命名空间的装配

#### 4.1.1 概念说明

和 `foundations`/`model`/`text` 等模块采用"直接注入式 `define(&mut global)`"不同，数学模块走的是 **"子模块挂载式"**（见 u1-l3）。也就是说，Typst 先在独立的 `Scope` 里把所有数学定义铺好，再把它包装成一个带名字的 `Module`，最后挂到全局命名空间的 `math` 键下。用户访问 `math.frac`、`#math.equation`、`sym.alpha` 等都经由这个挂载点。

这种挂载式的好处是：数学命名空间是**自包含**的——它有自己的 `Category::Math` 分类标签，定义不会和全局定义混在一起；同时它仍然可以被复制（`math.clone()`），一份挂到 `global`、一份存到 `Library.math` 字段供程序内部访问。

#### 4.1.2 核心流程

装配过程分五步：

1. 创建一个**去重作用域** `Scope::deduplicating()`（同一定义重复注册不报错，便于特性开关叠加）。
2. `start_category(Category::Math)` 给后续绑定盖上数学分类标签。
3. 批量 `define_elem::<E>()` 注册数学独有的元素（`EquationElem`/`FracElem`/`LrElem`/...）。
4. 批量 `define_func::<F>()` 注册数学函数（`abs`/`sqrt`/`bold`/`italic`/...）。
5. 注入文本算符（`op::define`）、五条间距常量、符号表（`symbols::define_math`），最后 `Module::new("math", ...)` 收口。

注意：`TextElem` 也被重新 `define_elem` 进了 math 作用域。这是因为数学公式内部仍然用 `TextElem` 承载普通文字，但需要一套独立的样式环境；把它显式注册进 math 作用域，方便数学函数（如 `#math.upright(...)`）引用。

#### 4.1.3 源码精读

`module()` 总装函数（[src/math/mod.rs:42-108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L42-L108)）：

```rust
pub fn module() -> Module {
    let mut math = Scope::deduplicating();
    math.start_category(crate::Category::Math);
    math.define_elem::<EquationElem>();
    math.define_elem::<TextElem>();
    math.define_elem::<LrElem>();
    // ... 大量 define_elem ...
    math.define_func::<abs>();
    math.define_func::<sqrt>();
    // ... 大量 define_func ...
    op::define(&mut math);              // 文本算子 sum/integral/...
    math.define("thin", HElem::new(THIN.into()).pack());   // 间距常量
    crate::symbols::define_math(&mut math);                 // 符号表 sym.*
    Module::new("math", math)
}
```

注册的元素可大致归类（行号见 mod.rs 第 46–73 行）：

- **容器**：`EquationElem`（公式本身）。
- **分数 / 根式**：`FracElem`、`BinomElem`、`RootElem`。
- **矩阵 / 向量**：`VecElem`、`MatElem`、`CasesElem`。
- **定界符与拉伸**：`LrElem`、`MidElem`、`StretchElem`。
- **上下标 / 重音 / 上下划线**：`AttachElem`、`ScriptsElem`、`LimitsElem`、`AccentElem`、`UnderlineElem`/`OverlineElem`/`UnderbraceElem`/`OverbraceElem`/... 一大家子。
- **杂项**：`ClassElem`（强制类）、`OpElem`、`PrimesElem`、`CancelElem`。

注册的函数（mod.rs 第 75–92 行）则是一组"格式化函数"，它们大多只是把 body 包上一层 `set(...)` 样式：

- **数学风格**：`bold`/`upright`/`italic`/`serif`/`sans`/`scr`/`cal`/`frak`/`mono`/`bb`（对应 LaTeX 的 `\mathbf`/`\mathrm`/...）。
- **字号档位**：`display`/`inline`/`script`/`sscript`。
- **便捷函数**：`abs`/`norm`/`round`/`sqrt`。

模块被构造后，在 `LibraryBuilder::build()` 里被两路使用（[src/lib.rs:222-224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L222-L224)）：

```rust
let math = math::module();
let global = global(self.routines, math.clone(), inputs, &self.features);
```

一份 `math.clone()` 喂给 `global()` 挂成用户可见的 `math` 子命名空间，另一份存进 `Library.math` 字段。`global()` 内的挂载只有一行（[src/lib.rs:346](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L346)）：`global.define("math", math);`。

#### 4.1.4 代码实践

**实践目标**：亲手核对 `math::module()` 注册的全部定义。

1. 打开 [src/math/mod.rs:43-108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L43-L108)，把所有 `define_elem` 和 `define_func` 调用各抄成一张表。
2. 在终端运行 `cargo doc -p typst-library --no-deps`，打开生成的文档，找到 `math` 模块页面。
3. 把文档里 `math` 模块下列出的函数/元素，与你抄的表逐一对齐，标出"哪些是元素、哪些是函数"。

**需要观察的现象**：文档里 `math.frac`、`math.equation` 这类是元素（带 `where`/构造参数），而 `math.bold`、`math.display` 是函数（返回 `Content`）。注意 `math.abs`/`math.norm` 既是函数又隐含了定界符拉伸（它们由 `op` 或单独定义生成）。

**预期结果**：你会得到大约 28 个元素 + 约 18 个函数 + 5 条间距 + 一大批 `sym.*` 符号的清单。

### 4.2 EquationElem：数学公式的容器

#### 4.2.1 概念说明

`EquationElem` 是**一切数学内容的容器元素**。用户写的 `$ a^2 + b^2 = c^2 $` 在语法分析阶段会被包成一个 `EquationElem`，其 `body` 字段就是公式内部的所有内容（已解析好的 `Content` 树）。

它有两个层次的配置：

- **用户可见层**：`block`（行内还是整块）、`numbering`（编号）、`number_align`（编号对齐）、`supplement`（引用前缀）、`alt`（无障碍文本描述）。这些字段会出现在 `#math.equation(...)` 的参数列表和文档里。
- **内部传递层**：一组 `#[internal]` 的 ghost 字段（`size`/`variant`/`cramped`/`bold`/`italic`/`script_scale`），它们是数学排版引擎用来在每个子表达式上"携带当前样式环境"的载体。用户不直接写它们，而是由 `ShowSet`、`bold()`、`script()` 等机制自动设置。

#### 4.2.2 核心流程

公式从输入到渲染的关键流程：

1. **语法分析**：`$ ... $` 被解析成 `EquationElem`。是否前后带空白决定 `block`：带空白（如 `$ x $`）→ 整块；紧贴（如 `$x$`）→ 行内。这一步发生在解析器（行为 crate），结果把 `block` 字段写进元素。
2. **合成（Synthesize）**：补全 `supplement`（`auto` 时取本地化的 "Equation"/"公式"）与 `locale`，供引用和无障碍使用。
3. **ShowSet**：根据 `block` 把样式环境一次性设好——整块居中、不可分页、字号 `MathSize::Display`；行内则字号 `MathSize::Text`。同时统一字体为 `New Computer Modern Math`、字重 450。
4. **实现（Realize）与布局**：公式内部内容被展开、解析成数学中间表示（IR）、再排成 `Frame`。这一步在行为 crate `typst-math`，经 `Routines` 回调。

#### 4.2.3 源码精读

`EquationElem` 的字段定义（[src/math/equation.rs:47-171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/equation.rs#L47-L171)）。先看用户可见字段：

```rust
pub struct EquationElem {
    #[default(false)]
    pub block: bool,                  // 行内(false) 还是整块(true)

    pub numbering: Option<Numbering>, // 编号，如 "(1)"

    #[default(SpecificAlignment::Both(OuterHAlignment::End, VAlignment::Horizon))]
    pub number_align: SpecificAlignment<OuterHAlignment, VAlignment>,

    pub supplement: Smart<Option<Supplement>>, // 引用前缀，auto 时取本地化名
    pub alt: Option<EcoString>,                // 无障碍文本描述

    #[required]
    pub body: Content,                // 公式内容
    ...
}
```

再看内部 ghost 字段——这是本讲的重点之一（[src/math/equation.rs:132-165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/equation.rs#L132-L165)）：

```rust
#[internal] #[default(MathSize::Text)] #[ghost] pub size: MathSize,
#[internal] #[ghost] pub variant: Option<MathVariant>,
#[internal] #[default(false)]         #[ghost] pub cramped: bool,
#[internal] #[default(false)]         #[ghost] pub bold: bool,
#[internal]                           #[ghost] pub italic: Option<bool>,

#[internal] #[default((70, 50))] #[ghost]
pub script_scale: (i16, i16),   // 取自字体的 MathConstants 表
```

回忆 u3-l3：`#[ghost]` 字段**不进 struct、只活样式链**。这里 `size`/`cramped`/`bold`/`variant`/`script_scale` 全是 ghost，因为同一个公式的不同子部分（分子、下标、下标的下标）需要不同的字号/字重环境，而环境必须沿 `StyleChain` 流动而非写死在元素实例里。`#[default((70, 50))]` 表示在字体没有提供 `MathConstants` 表时，上下标分别缩到 70% 和 50%。

`ShowSet` 实现是切换字号档位的枢纽（[src/math/equation.rs:196-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/equation.rs#L196-L214)）：

```rust
impl ShowSet for Packed<EquationElem> {
    fn show_set(&self, styles: StyleChain) -> Styles {
        let mut out = Styles::new();
        if self.block.get(styles) {
            out.set(AlignElem::alignment, Alignment::CENTER); // 整块居中
            out.set(BlockElem::breakable, false);             // 默认不分页
            out.set(ParLine::numbering, None);                // 不参与行号
            out.set(EquationElem::size, MathSize::Display);   // 整块用 Display 档
        } else {
            out.set(EquationElem::size, MathSize::Text);      // 行内用 Text 档
        }
        out.set(TextElem::weight, FontWeight::from_number(450));
        out.set(TextElem::font, FontList(vec![FontFamily::new("New Computer Modern Math")]));
        out
    }
}
```

这就是为什么 `#set math.equation(block: true)` 会改变字号档位——`ShowSet` 在元素被展示时自动注入这套样式（见 u4-l2 的 show-set 机制）。

**编号与引用同源**。`EquationElem` 同时实现了 `Count`/`Refable`/`Outlinable`/`LocalName`（[src/math/equation.rs:216-262](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/equation.rs#L216-L262)）。`Count::update` 只有在"整块且配置了 numbering"时才返回 `CounterUpdate::Step(1)`（[equation.rs:216-221](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/equation.rs#L216-L221)），驱动 `counter(math.equation)` 前进。这与 u8-l2/u8-l3 讲的"标题号、引用号、目录号同源"完全一致——公式号、`@ratio` 引用号、`#outline()` 都读同一个计数器 `Counter::of(EquationElem::ELEM)`，故永不冲突。

#### 4.2.4 代码实践

**实践目标**：理解 `block` 如何通过 `ShowSet` 切换字号档位，并观察两者差异。

1. 打开 [src/math/equation.rs:196-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/equation.rs#L196-L214)，逐行标注 `ShowSet` 对 `block == true` 与 `block == false` 各设了哪些样式。
2. 在你本地的 Typst 环境写一个最小文档（**示例代码，非项目原有**）：

   ```typst
   #set text(font: "New Computer Modern")
   求和符号对比：行内 $sum_(k=1)^n k$ 与整块
   $ sum_(k=1)^n k = (n(n+1)) / 2 $
   ```

3. 观察求和符号 $\sum$ 的尺寸与上下标位置：整块（`MathSize::Display`）下，$\sum$ 更大、上下标会"骑"在符号上下（limits 风格）；行内（`MathSize::Text`）下，$\sum$ 较小、上下标贴在右肩（scripts 风格）。

**需要观察的现象**：同一行 `$ ... $`，去掉两端空白（变行内）后，大算子（sum/integral/product）的渲染形态明显变小、上下标位置改变。

**预期结果**：这正是 `ShowSet` 中 `MathSize::Display` vs `MathSize::Text` 的视觉体现。注意：渲染本身在行为 crate 完成，本 crate 只通过 ghost 字段 `size` 把"当前档位"传递下去。

### 4.3 Mathy trait：识别"数学元素"

#### 4.3.1 概念说明

`Mathy` 是一个**空的标记 trait**（marker trait）：

```rust
pub trait Mathy {}
```

它没有任何方法，唯一的用途是给元素打上"我是一个数学元素"的标签。这个标签由 `#[elem(...)]` 宏在元素定义时附带（如 `#[elem(since = "forever", Mathy)]`），宏会自动生成 `impl Mathy for Packed<FracElem> {}`。

为什么需要它？因为 Typst 的数学函数里，用户可以直接写裸的数学元素，比如 `#math.frac(1, 2)`、`#math.bold(x)`，而**不一定**非要用 `$ ... $` 把它们包起来。当这种"裸数学元素"出现在普通文本流中时，求值器需要识别它、并自动用一个 `EquationElem` 包裹起来，否则它没法进入数学排版流水线。`Mathy` trait 就是这个"是否需要自动包裹"判定的依据。

> 判定与自动包裹的实际逻辑在行为 crate（`typst-eval`/`typst-realize`），本 crate 只提供 `Mathy` 这个标签。

#### 4.3.2 核心流程

1. 某个元素在 `#[elem(...)]` 里声明了 `Mathy`。
2. `elem` 宏为 `Packed<该元素>` 生成空的 `impl Mathy`。
3. 求值/实现阶段，遇到 `Content` 时用能力查询 `can::<dyn Mathy>()`（见 u3-l2 的 `can::<C>()` 能力查询机制）判断它是否数学元素。
4. 若是、且未在公式环境内，自动包进一个 `EquationElem`。

#### 4.3.3 源码精读

trait 定义（[src/math/mod.rs:110-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L110-L111)）：

```rust
/// Trait for recognizing math elements and auto-wrapping them in equations.
pub trait Mathy {}
```

两个本模块内直接定义的 `Mathy` 元素示例。`ClassElem`——强制指定数学类的元素（[src/math/mod.rs:141-150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L141-L150)）：

```rust
#[elem(since = "forever", Mathy)]
pub struct ClassElem {
    #[required] pub class: MathClass,  // 如 "relation"/"binary"/...
    #[required] pub body: Content,
}
```

对齐点 `AlignPointElem`——数学公式里 `&`/`&&` 对齐标记（[src/math/mod.rs:114-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L114-L122)）：

```rust
#[elem(title = "Alignment Point", since = "forever", Mathy)]
pub struct AlignPointElem {}

impl AlignPointElem {
    pub fn shared() -> &'static Content {
        singleton!(Content, AlignPointElem::new().pack())  // 全局共享单例
    }
}
```

`AlignPointElem` 是零字段元素，用 `singleton!` 全局共享同一个实例（避免每个 `&` 都分配新 `Content`）。它是 `Mathy`，说明对齐点本身也被视为数学元素。其余 `Mathy` 元素（`FracElem`/`VecElem`/...）分布在各自子模块（如 [src/math/frac.rs:24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/frac.rs#L24) 的 `#[elem(title = "Fraction", since = "forever", Mathy)]`）。

注意：`EquationElem` 自身**不是** `Mathy`——它已经是容器，不需要再被自动包裹。

#### 4.3.4 代码实践

**实践目标**：找出哪些元素是 `Mathy`，并理解自动包裹。

1. 在 `crates/typst-library/src/math/` 目录下，用编辑器搜索 `Mathy]`（带右括号，匹配 `#[elem(... Mathy)]`），列出所有命中元素。
2. 对比：`EquationElem` 的 `#[elem(...)]`（[equation.rs:47-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/equation.rs#L47-L57)）里**没有** `Mathy`，而 `FracElem`（[frac.rs:24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/frac.rs#L24)）有。
3. 写一个最小文档验证自动包裹（**示例代码，非项目原有**）：

   ```typst
   // 不写 $...$，直接调用数学函数
   一半是 #math.frac(1, 2) 啦。
   ```

**需要观察的现象**：`#math.frac(1, 2)` 不在 `$...$` 里，却仍能正确渲染成竖直分数——因为 `FracElem` 是 `Mathy`，被自动包进了 `EquationElem`。

**预期结果**：渲染输出一个竖直的 $\frac{1}{2}$，证明自动包裹生效。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `EquationElem` 不需要 `Mathy`？

**参考答案**：`Mathy` 的作用是让"裸数学元素"被自动包进公式容器。`EquationElem` 本身就是那个容器，若它再标 `Mathy`，自动包裹逻辑会无限递归地把容器再包进容器。所以只有"公式内部的内容元素"才标 `Mathy`。

**练习 2**：`AlignPointElem` 用 `singleton!` 共享单例的好处是什么？

**参考答案**：数学公式里 `&` 对齐点可能极多（每个公式行都有）。零字段元素的内容完全相同，用全局单例可避免每次 `&` 都分配新的 `Content`（`Arc` + 堆分配），把对齐点降到"几乎零成本指针比较"。

### 4.4 数学字号、间距常量与字体回退

#### 4.4.1 概念说明

数学排版有一个文本排版没有的概念：**字号档位（size level）**。同一个符号，在公式主体、在下标、在下标的下标里，字号是层层缩小的。TeX 把它分成四档：

- **Display**：整块公式主体（`displaystyle`）。
- **Text**：行内公式主体（`textstyle`）。
- **Script**：上下标（`scriptstyle`）。
- **ScriptScript**：上下标的上下标（`scriptscriptstyle`）。

Display 与 Text 的**字号本身相同**（都是 100%），区别在 `displaystyle`——大算子（$\sum$、$\int$）是否带上下限（limits）而非右肩小标。真正缩小字号的是 Script（默认 70%）和 ScriptScript（默认 50%）。

字体缩放百分比来自字体的 `MathConstants` 表里的 `scriptPercentScaleDown` 和 `scriptScriptPercentScaleDown` 两条记录；本 crate 用 `script_scale: (i16, i16)` ghost 字段携带它，默认 `(70, 50)`。

**间距常量**则是数学排版专用的五种水平间距，都以 `Em`（相对字号）表示：`THIN`、`MEDIUM`、`THICK`、`QUAD`、`WIDE`。它们被注册成 `math.thin`/`math.med`/... 供用户写 `$a thin b$` 这类表达式。

#### 4.4.2 核心流程

数学字号的最终值由两层相乘：

\[ \text{实际字号} \;=\; \underbrace{\text{TextSize::resolve}(styles)}_{\text{基础文本字号}} \;\times\; \underbrace{\text{scale}(\text{MathSize},\, \text{script\_scale})}_{\text{数学缩放系数}} \]

其中缩放系数为：

\[
\text{scale}(\text{MathSize}) =
\begin{cases}
100\% & \text{Display 或 Text} \\
\text{script\_scale}.0\% & \text{Script} \quad (\text{默认 }70\%) \\
\text{script\_scale}.1\% & \text{ScriptScript} \quad (\text{默认 }50\%)
\end{cases}
\]

基础文本字号 `TextSize::resolve(styles)` 的来路（见 u4-l1、u7-l1）：`TextElem::size` 是 `#[fold]` 的 `TextSize`——多个 `#set text(size: ...)` 相乘；`resolve` 把 `Length`（含 `Em` 部分）代入当前字号解出绝对 `Abs`。在数学 IR 的解析层，多处可见这一调用。

档位之间的切换由 `style.rs` 里的一组函数完成。例如进入上标时，档位这样变化（[src/math/style.rs:315-322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/style.rs#L315-L322)）：

```rust
pub fn style_for_superscript(styles: StyleChain) -> LazyHash<Style> {
    EquationElem::size
        .set(match styles.get(EquationElem::size) {
            MathSize::Display | MathSize::Text => MathSize::Script,
            MathSize::Script | MathSize::ScriptScript => MathSize::ScriptScript,
        })
        .wrap()
}
```

即：Display/Text 进入上标 → Script；Script/ScriptScript 进入上标 → ScriptScript（不会再变大）。分子分母档位见 `style_for_numerator`（[style.rs:343-351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/style.rs#L343-L351)）：Display → Text → Script → ScriptScript。

#### 4.4.3 源码精读

`MathSize` 枚举（[src/math/style.rs:260-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/style.rs#L260-L282)），注意它派生了 `Ord`，大小顺序为 ScriptScript < Script < Text < Display，后续用 `s <= MathSize::Script` 判定"是否处于小字号档"：

```rust
#[derive(Debug, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash, Cast)]
pub enum MathSize {
    ScriptScript, // scriptlevel 2
    Script,       // scriptlevel 1
    Text,         // 行内主体
    Display,      // 整块主体
}
```

四档对应的用户函数（[src/math/style.rs:172-249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/style.rs#L172-L249)），都只是 `body.set(EquationElem::size, MathSize::X)`：

```rust
pub fn display(body: Content, ...) -> Content {
    body.set(EquationElem::size, MathSize::Display).set(EquationElem::cramped, cramped)
}
pub fn script(body: Content, ...) -> Content {
    body.set(EquationElem::size, MathSize::Script).set(EquationElem::cramped, cramped)
}
// inline → MathSize::Text, sscript → MathSize::ScriptScript
```

数学 IR 解析层取基础字号的两处典型（[src/math/ir/resolve.rs:643-645](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/resolve.rs#L643-L645) 与 [resolve.rs:878-881](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/resolve.rs#L878-L881)）：

```rust
let size = elem.size.get(styles);
let font_size = styles.resolve(TextElem::size);   // 基础文本字号（Abs）
item.update_stretch(StretchInfo::from_size(size, Em::zero(), font_size));
```

`styles.resolve(TextElem::size)` 正是 u4-l1 讲的 `Resolve`——把折叠后的 `TextSize`（一个 `Length = {abs, em}`）代入字号解出绝对值。数学缩放系数随后在行为 crate `typst-math` 里乘上去。

五条间距常量（[src/math/mod.rs:36-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L36-L40)），全部以 `Em` 表示、随字号缩放：

```rust
pub const THIN:   Em = Em::new(1.0 / 6.0);   // ≈0.1667em
pub const MEDIUM: Em = Em::new(2.0 / 9.0);   // ≈0.2222em
pub const THICK:  Em = Em::new(5.0 / 18.0);  // ≈0.2778em
pub const QUAD:   Em = Em::new(1.0);         // 1em
pub const WIDE:   Em = Em::new(2.0);         // 2em
```

它们在 `module()` 里被包成 `HElem` 注册（[mod.rs:98-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L98-L102)），所以 `$a thin b$` 等价于在 a、b 之间插入一个 1/6 em 的水平空白。

数学字体回退链 `families`（[src/math/mod.rs:176-193](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L176-L193)）：

```rust
pub fn families(styles: StyleChain<'_>) -> impl Iterator<Item = &'_ FontFamily> + Clone {
    let fallbacks = singleton!(Vec<FontFamily>, {
        ["new computer modern math", "libertinus serif",
         "twitter color emoji", "noto color emoji",
         "apple color emoji", "segoe ui emoji"]
        .into_iter().map(FontFamily::new).collect()
    });
    let tail = if styles.get(TextElem::fallback) { fallbacks.as_slice() } else { &[] };
    styles.get_ref(TextElem::font).into_iter().chain(tail.iter())
}
```

这与 u7-l1 讲的文本字体回退（`FontList` + `fallback` 字段）同构：用户字体列表在前，仅当 `TextElem::fallback` 为真时追加数学专用回退族（先 `new computer modern math` 兜数学符号，再 serif 兜普通字母，最后各路 emoji 字体兜彩色符号）。回退表用 `singleton!` 全局缓存一次。

#### 4.4.4 代码实践

**实践目标**：追踪"数学字号 = 文本字号 × 缩放系数"的数据通路。

1. 打开 [src/math/equation.rs:160-165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/equation.rs#L160-L165)，确认 `script_scale` 默认值 `(70, 50)`，并阅读注释："Values of `scriptPercentScaleDown` and `scriptScriptPercentScaleDown` respectively in the current font's MathConstants table."
2. 打开 [src/math/ir/resolve.rs:643-645](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/resolve.rs#L643-L645)，确认基础字号来自 `styles.resolve(TextElem::size)`——这就是 `TextSize::resolve`，即 u4-l1 讲的把 `Length` 解析成 `Abs` 的 `Resolve` trait。
3. 用纸笔追踪一个嵌套下标的字号：设文档基础字号为 11pt。
   - `$a$`（Text 档）：\( 11 \times 100\% = 11 \) pt。
   - `$a_b$` 中 b 是下标（Script 档）：\( 11 \times 70\% = 7.7 \) pt。
   - `$a_(b_c)$` 中 c 是下标的下标（ScriptScript 档）：\( 11 \times 50\% = 5.5 \) pt。

**需要观察的现象**：在追踪过程中，你会看到本 crate 只提供 `MathSize`（档位标签）+ `script_scale`（百分比）+ 基础 `TextElem::size`（经 `resolve` 得到的 `Abs`），而**真正把它们相乘得到最终字形字号**的代码不在 `typst-library`，而在行为 crate `typst-math`。

**预期结果**：你能画出这样一条数据链：

\[
\underbrace{\text{TextElem::size}}_{\#[\text{fold}]\text{ 的 TextSize}}
\;\xrightarrow{\text{resolve}}\; \text{Abs}
\;\xrightarrow[\times\,\text{scale}(\text{MathSize})]{\text{在 typst-math}}\;
\text{字形最终字号}
\]

若无法在本地编译运行（待本地验证），可改为阅读型实践：在 `crates/typst-math/`（行为 crate，若可访问）中搜索 `script_scale` 或 `MathSize::Script` 的读取点，确认相乘发生在那里。

#### 4.4.5 小练习与答案

**练习 1**：`MathSize::Display` 和 `MathSize::Text` 的字号缩放系数都是 100%，那它们的区别是什么？

**参考答案**：区别在 `displaystyle` 行为，不在字号。Display 档下，大算子（$\sum$、$\prod$、$\lim$ 等）默认把上下标放在符号**正上下方**（limits 风格）；Text 档下则贴在**右肩**（scripts 风格）。这就是为什么同一个求和符号在整块公式里显得"更大更舒展"。

**练习 2**：`script_scale` 为什么是 ghost 字段而不是普通字段？为什么默认 `(70, 50)`？

**参考答案**：是 ghost 字段，因为它记录的是**当前字体的 MathConstants 表**里的两条记录，属于样式环境、不属于某个元素实例——不同字体可能有不同缩放比，必须沿 `StyleChain` 流动。默认 `(70, 50)`（即 70%、50%）是 OpenType MATH 表规范和 TeX 的通用约定，在字体未提供该表时兜底。

**练习 3**：`families()` 为什么要把 emoji 字体放进数学回退链？

**参考答案**：用户可能在公式里写彩色 emoji 或某些 Unicode 符号（如表情、几何图形），而纯数学字体（New Computer Modern Math）通常不覆盖这些码点。把 emoji 字体放在回退链末尾，可保证缺失码点仍能被某款已安装字体兜住（参见 u7-l1 的 `Covers` 覆盖集与回退机制）。

## 5. 综合实践

把本讲的四个模块串起来，完成一个"源码阅读 + 文档实验"综合任务：

**任务**：解释一句 Typst 代码 `$ sum_(k=1)^n k_n $` 从输入到拿到字号数据的全过程，标注每一步发生在哪个文件。

**步骤**：

1. **识别容器**：`$ ... $`（带空白）被解析成 `EquationElem`，`block = true`（解析在行为 crate）。
2. **ShowSet 切档**：因 `block` 为真，[equation.rs:199-203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/equation.rs#L199-L203) 的 `ShowSet` 把 `EquationElem::size` 设为 `MathSize::Display`，并设字体为 New Computer Modern Math。
3. **下标切档**：进入 `k_n` 的下标 `n` 时，[style.rs:332-334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/style.rs#L332-L334) 的 `style_for_subscript` 调 `style_for_superscript`，把档位从 Display 切到 Script（[style.rs:317-319](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/style.rs#L317-L319)），同时设 `cramped`。
4. **取基础字号**：IR 解析层用 [resolve.rs:644](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/resolve.rs#L644) 的 `styles.resolve(TextElem::size)` 取得基础文本字号。
5. **缩放**：行为 crate 用 `script_scale.0`（默认 70%）乘基础字号，得到 `n` 的实际字号；主体 `k` 和 `sum` 则用 100%。
6. **字体回退**：每个字形按 [mod.rs:176-193](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/mod.rs#L176-L193) 的 `families()` 匹配字体。

**交付物**：一张流程图或编号清单，把上述 6 步、对应的源码行号、以及"本 crate 提供什么 / 行为 crate 做什么"的分工写清楚。

## 6. 本讲小结

- **装配方式**：math 是「子模块挂载式」模块，由 `math::module()` 在独立 `Scope` 里铺好全部定义，再挂到全局 `math` 键下；它注册了约 28 个数学元素、约 18 个函数、5 条间距常量和一批 `sym.*` 符号。
- **EquationElem 是容器**：用户可见字段（`block`/`numbering`/`supplement`/...）控制形态，内部 ghost 字段（`size`/`cramped`/`bold`/`variant`/`script_scale`）携带样式环境；`ShowSet` 按 `block` 自动在 `MathSize::Display` 与 `MathSize::Text` 间切档。
- **编号同源**：`EquationElem` 实现 `Count`/`Refable`/`Outlinable`，公式号、`@ref` 引用号、`#outline()` 都读 `Counter::of(EquationElem::ELEM)`，与标题号机制一致、永不冲突。
- **Mathy 是标记 trait**：空 trait，由 `#[elem(... Mathy)]` 附着，供求值器识别"裸数学元素"并自动包进 `EquationElem`；`EquationElem` 自身不是 `Mathy`。
- **数学字号双层相乘**：基础字号来自 `TextSize::resolve`（`styles.resolve(TextElem::size)`），数学缩放系数由 `MathSize` 档位 + `script_scale` 百分比决定（Script 70%、ScriptScript 50%），相乘发生在行为 crate `typst-math`。
- **间距与字体回退**：五条 `Em` 间距常量（`THIN`/`MEDIUM`/`THICK`/`QUAD`/`WIDE`）注册为 `math.thin` 等；`families()` 提供以 New Computer Modern Math 为首、emoji 字体兜底的数学专用回退链。

## 7. 下一步学习建议

- **u10-l2（数学元素：frac、matrix、attach、accent、lr、root）**：本讲只看了容器与装配，下一讲深入各个具体数学元素的字段与 `MathClass` 如何决定符号间距。
- **u10-l3（数学中间表示 IR）**：本讲多次提到"解析成 IR 再排成 Frame"，下一讲正式打开 `src/math/ir/` 子模块，看 `MathItem`/`process`/`multiline` 如何把 `Content` 树转成可布局的中间表示。
- **延伸阅读**：若想看真正的字号相乘与排版算法，可阅读行为 crate `typst-math`（仓库 `crates/typst-math/`），重点关注它如何消费 `MathSize`、`script_scale` 与 `families()`。
