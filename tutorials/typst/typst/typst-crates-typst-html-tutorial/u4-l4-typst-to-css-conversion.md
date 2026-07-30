# Typst 类型到 CSS 类型的转换

## 1. 本讲目标

上一讲（u4-l3）我们看懂了 `css/` 子模块如何把「编译器生成的 CSS」收集到 `HtmlElement.css` 字段，并最终由 `resolve_inline_styles` 拼成内联 `style` 属性。但有一个关键问题被刻意跳过了：**Typst 的强类型值（如 `Length`、`Rel`、`Color`）究竟是怎么变成一串合法 CSS 文本的？**

本讲就专门打开这个黑盒。读完本讲，你应当能够：

1. 说清 `ToCss` trait 的「emit 模式」为什么比「返回 `String`」更适合做嵌套序列化。
2. 手动追踪一个 `Length` 或 `Rel`（例如 `2em + 3pt`、`50% + 2em`）是如何被 `CalcWriter` 逐步写成 `calc(...)` 表达式的，并解释「单操作数省略 `calc()`」的原因。
3. 理解 `Color`/`Rgb` 在多种色彩空间下的序列化分支，以及 `hex` 与 `rgb()` 的选择依据。
4. 掌握 `is_very_close` 的 12 位精度阈值，以及它与 `Number` 的「4 位有效数字」之间的呼应关系。

本讲全部源码集中在一个文件 `src/css/encode.rs`（共 518 行），是 typst-html 中最「算法密集」的一块。

## 2. 前置知识

在进入源码前，先建立几个直觉。这一节只讲概念，不引用代码。

### 2.1 为什么 Typst 的长度不能直接当 CSS 写

CSS 的长度单位是「扁平」的：你写 `5pt`、`2em`、`50%`，浏览器各自解释。但 Typst 内部把长度做成了**组合类型**：

- `Abs`：绝对长度，内部统一以 pt 存储（无论用户写的是 `1in`、`25.4mm` 还是 `72pt`，最后都是 pt）。
- `Em`：字体相对长度，内部就是一个 `f64`（多少个 em）。
- `Length = Abs + Em`：一个「混合长度」，同时含绝对部分和 em 部分，例如 `2em + 3pt`。
- `Ratio`：纯比例，如 `50%`。
- `Rel<Length> = Ratio + Length`：最一般的尺寸表达式，如 `50% + 2em`（相对部分 + 混合长度）。

CSS 没有一个单位能同时表达「em 加 pt」，所以当 Typst 长度同时含有多种成分时，必须借助 CSS 的 `calc()` 函数把它们「加」起来，例如 `calc(2em + 3pt)`。这就是本讲要解决的核心张力。

### 2.2 emit 模式 vs 返回字符串

最朴素的序列化思路是给每个类型实现 `fn to_css(&self) -> String`。但它有两个缺点：

- 每次调用都分配一个新 `String`，嵌套表达式（如颜色套在 `calc` 里）会产生大量临时字符串。
- 难以统一处理「这个值无法转成 CSS」的情况——返回 `Option<String>` 会把错误处理传染到每一层。

typst-html 采用「**emit 模式**」：序列化就是把值写进一个共享的 writer（缓冲区），再由外层统一判断是否成功。本讲的 `ToCss` trait 与 `CssWriter` 就是这个模式的实现。

### 2.3 浏览器对颜色的位数限制

CSS 颜色最终会被浏览器量化到屏幕能显示的位深。typst-html 假设业界合理的最高位深是 **12 位**（即每个分量有 \(2^{12}=4096\) 级），并据此决定颜色序列化的精度与「能否用简短的 `hex` 表示」。这个假设具体体现在 `is_very_close` 函数里，后面会精读。

---

## 3. 本讲源码地图

本讲只涉及一个文件，但它内部层次很丰富。按下表对照阅读：

| 文件 | 作用 |
|------|------|
| `src/css/encode.rs` | 全部内容。定义 `ToCss` trait、`CssWriter`、`CalcWriter`、`CallWriter`，以及为 `Abs/Em/Length/Angle/Ratio/Rel/Color/Rgb/...` 实现的序列化。 |

为理解类型本身的字段结构，本讲还会**附带引用**两个 typst-library 的文件（同一 commit，用于佐证 `Length` 与 `Rel` 的字段）：

| 文件 | 作用 |
|------|------|
| `crates/typst-library/src/layout/length.rs` | `Length { abs, em }` 的定义。 |
| `crates/typst-library/src/layout/rel.rs` | `Rel<T> { rel, abs }` 的定义。 |

回顾上下游（已在 u4-l3 讲过）：`PropertiesBuilder::push` 会调用本讲的 `CssWriter` 把一个 Typst 值序列化；序列化成功才加入 `Properties` 列表；最后 `resolve_inline_styles` 把列表写成 `style="..."`。

---

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. **ToCss 与 CssWriter**：统一的 emit 序列化模型。
2. **CalcWriter**：`calc()` 表达式的惰性生成（本讲重点，对应实践任务）。
3. **长度类类型**：`Abs / Em / Length / Angle / Ratio / Rel`。
4. **颜色类类型**：`Color / Rgb` 与多色彩空间。
5. **is_very_close 与 Number**：精度边界与有效数字。

### 4.1 ToCss trait 与 CssWriter：统一的 emit 序列化模型

#### 4.1.1 概念说明

`ToCss` 是整个序列化体系的中心 trait，思路就是 2.2 节说的 emit 模式：实现者把「自己」写进一个 `CssWriter`，而不是返回字符串。

`CssWriter` 是一个有状态的低级写入器，它持有三样东西：

- `buf: EcoString`——累积输出的缓冲区（`EcoString` 是 ecow 提供的写时复制字符串）。
- `error: bool`——一旦某次序列化发现「无法表达」，就置为 `true`。
- `sink: &mut dyn WarningSink`——用来发出「某某值被忽略」的警告，让用户知道导出丢了信息。

这种设计让「成功/失败」的处理非常干净：`emit` 写到底，遇到不支持的构造就调 `fail()`；外层（`PropertiesBuilder`）只需看一眼 `writer.error` 就能决定要不要丢弃这条属性，错误处理不会污染每个 `impl`。

#### 4.1.2 核心流程

一个值从「Typst 强类型」变成「CSS 文本」的流程：

```text
PropertiesBuilder::push("width", rel_value)
        │
        ▼
   新建 CssWriter（空 buf, error=false）
        │
        ▼
   writer.emit(value)          // 即 value.emit(&mut writer)
        │  （值递归地把各成分写进 buf；遇不支持则 writer.fail()）
        ▼
   若 writer.error == false：
       Properties.push("width", writer.buf)   // 保留这条属性
   否则：
       丢弃（警告已由 fail 经 sink 发出）
```

关键点：**整个序列化共用同一个 `buf`**。所以 `Length` 写 `calc(...)`、颜色写 `rgb(...)` 时，都是往同一个缓冲区追加，靠各自的 writer（`CalcWriter`/`CallWriter`）记下「自己从哪个字节开始写的」，以便事后改写（见 4.2）。

#### 4.1.3 源码精读

`ToCss` trait 本体只有两个方法：核心的 `emit`，和一个便利方法 `to_css`（自带 writer，方便一次性转字符串）：

[encode.rs:300-311](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L300-L311) —— 定义 `ToCss`：`emit` 把自己写进 writer，`to_css` 是便捷封装。

`CssWriter` 的字段与几个关键方法：

[encode.rs:133-176](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L133-L176) —— `CssWriter` 持有 `sink/buf/error`；`emit` 转发到值的 `emit`；`fail` 既发警告又置 `error=true`，是「优雅降级」的开关。

谁在消费 `error` 标志？正是 `PropertiesBuilder::push`：

[encode.rs:94-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L94-L101) —— 序列化完成后，只有 `!writer.error` 才把结果 `push` 进属性列表。

这里也能看到 emit 模式的另一个好处：`CssWriter` 还提供了 `call()` 和 `calc()` 两个工厂方法（[encode.rs:146-153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L146-L153)），分别用来发起一次 CSS 函数调用（如 `rgb(...)`）和一次 `calc(...)`，它们返回的临时 writer 在析构（`Drop`）时自动补上右括号或 `calc()` 包裹。这种「RAII 写括号」的写法后面会反复用到。

#### 4.1.4 代码实践

**实践目标**：体会 emit 模式下「失败即丢弃」的优雅降级。

**操作步骤（源码阅读型）**：

1. 阅读 `PropertiesBuilder::push`（[encode.rs:94-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L94-L101)）。
2. 阅读 `CssWriter::fail`（[encode.rs:172-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L172-L175)）。
3. 找到一个会调用 `fail` 的真实 `ToCss` 实现——`Paint`（[encode.rs:386-394](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L386-L394)）：渐变（`Gradient`）和平铺（`Tiling`）都会被 `fail` 掉。

**需要观察的现象**：当一个颜色是渐变时，`fail` 会做两件事——经 `sink` 发出一条 `"gradient was ignored during HTML export"` 的警告，并把 `error` 置真，于是 `push` 不会把它加入属性列表。

**预期结果**：导出含渐变填充的元素时，用户会收到一条警告，对应的 CSS 属性被整体跳过（而不是输出一段非法 CSS）。

> 说明：本实践为源码阅读型，无需运行；结论直接来自上述源码行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ToCss::to_css` 要接收一个 `WarningSink` 参数，而不是直接返回 `Result<EcoString, ...>`？

**参考答案**：因为 emit 模式把「序列化」和「报错」解耦——序列化永远成功地把能写的部分写进 buf，无法表达的部分经 `sink` 发警告并置 `error`。用 `WarningSink` 而非 `Result`，可以让「部分失败」（某个属性被忽略，但其它属性继续）这种 HTML 导出里常见的场景被自然表达，而 `Result` 会强制「全有或全无」。

**练习 2**：`CssWriter` 为什么把 `buf`、`error`、`sink` 放在同一个结构体里，而不是让 `emit` 返回 `(String, bool)`？

**参考答案**：因为序列化是递归嵌套的（颜色里可能套长度、套 calc）。共享同一个 writer，所有内层调用都在同一个 buf 上追加、共用同一个 error 标志和 sink，避免了层层传递返回值与字符串拼接的开销，也让 `CalcWriter`/`CallWriter` 这种「记下起始字节、事后改写」的技巧成为可能。

---

### 4.2 CalcWriter 与 calc() 表达式的惰性生成

> 这是本讲的核心模块，也是实践任务所在。

#### 4.2.1 概念说明

正如 2.1 节所说，当 Typst 长度混合了不可公约的单位（如 `em` 和 `pt`，或 `%` 和长度）时，必须用 CSS 的 `calc()` 把它们相加。但问题是：**调用者一开始并不知道最终会有几个操作数。** 比如 `Length` 最多有 2 个成分（em、abs），`Rel` 最多有 3 个（ratio、em、abs），而其中为零的成分又该被省略。

`CalcWriter` 用一种「**惰性生成**」策略解决这个问题：它先假设不需要 `calc()`，边写边数操作数；到最后（`Drop` 时）再根据操作数个数决定要不要补上 `calc(...)` 包裹。

核心规则有两条：

- **零值省略**：任何为 0 的成分都不写入（`5pt` 里的 em 部分为 0，就不出现 em）。
- **单操作数不包 calc**：如果最终只有一个非零成分，那它本身就是一个合法 CSS 值（如 `5pt`），无需 `calc()` 包裹；只有 ≥2 个成分才用 `calc(a + b)`。

> 补充一个容易被忽略的点：CSS 里负数的减法。`CalcWriter::sum` 遇到负值时，会先取绝对值再写成 ` - `（减号），详见 4.2.3。

#### 4.2.2 核心流程

`CalcWriter` 的生命周期与 buf 内容演变（以 `Length = 2em + 3pt` 为例）：

```text
start()        : 记下 start_idx = buf 当前长度（此刻 0），count = 0
                  buf = ""
sum(Em 2)      : 非零 & count==0 → 直接 emit "2em"
                  buf = "2em",          count = 1
sum(Abs 3pt)   : 非零 & count>0 & 正值 → 写 " + " 再 emit "3pt"
                  buf = "2em + 3pt",    count = 2
Drop           : count==2 → 在 [start_idx..] 外面套 calc()
                  buf = "calc(2em + 3pt)"
```

`Drop` 的三分支判定是「省略 calc」的全部秘密：

```text
count == 0   → 写 "0"        （空 calc 非法，0 对长度也是合法兜底）
count == 1   → 什么都不做     （单操作数，无需 calc 包裹）★
count >= 2   → 把 [start_idx..] 切出来，前后包上 "calc(" / ")"
```

★ 标出的 `count == 1` 分支正是「单操作数省略 calc()」的来源。

#### 4.2.3 源码精读

`CalcWriter` 的字段——关键是 `start_idx`（记下自己开始写之前的 buf 长度）和 `count`（已写入的非零操作数个数）：

[encode.rs:224-235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L224-L235) —— `CalcWriter` 结构与 `start()`：记录起始字节下标，count 归零。

`sum` 方法是唯一添加操作数的入口，承担「跳过零值」「首项直写」「后续项加号/减号」三件事：

[encode.rs:247-269](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L247-L269) —— `sum`：零值直接 return；首项 `emit` 直写；非首项正值写 `" + "`、负值取绝对值后写 `" - "`。源码注释解释了「`"+ {val}` 等价于 `"- {val.neg()}`」这一前提。

`Drop` 实现按 count 三分支处理，其中 `count >= 2` 分支用「拼接新 buf」的方式在已写文本外面包 `calc()`（因为 `EcoString` 当时还没有 `insert_str`，所以采用了「前缀 + calc( + 中段 + ) 」的重建方式）：

[encode.rs:272-298](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L272-L298) —— `Drop`：`0 ⇒ "0"`、`1 ⇒ ()`、`2.. ⇒ 包 calc()`。`1 ⇒ ()` 这一行就是「单操作数省略 calc」。

谁创建了 `CalcWriter`？正是 `Length` 和 `Rel` 的 `emit`（见 4.3）。它们调 `w.calc()`（[encode.rs:151-153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L151-L153)）拿到一个 `CalcWriter`，连着调几次 `.sum(...)`。

#### 4.2.4 代码实践（对应本讲主任务）

**实践目标**：手动追踪 `CalcWriter`，说清 `2em + 3pt` 这类值如何变成 `calc()`，以及单操作数为何省略包裹。

> 准确性提示：`2em + 3pt` 在 Typst 里其实是一个 **`Length`**（只有 em 与 abs 两部分、没有比例成分），并非带 `%` 的 `Rel`。本实践先用它演示 `Length` 路径，再用一个真正的 `Rel`（`50% + 2em`）演示三操作数路径。两者都用同一个 `CalcWriter`。

**步骤 A：源码逐行追踪 `Length(2em + 3pt)`**

1. 入口 `Length::emit`（[encode.rs:360-364](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L360-L364)）：`w.calc().sum(self.em).sum(self.abs)`。此处 `self.em = Em(2.0)`、`self.abs = Abs(3pt)`。
2. `CalcWriter::start`（[encode.rs:231-235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L231-L235)）：`start_idx = 0`，`count = 0`，buf 为空。
3. `sum(Em 2.0)`（[encode.rs:247-269](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L247-L269)）：非零；`count==0` 分支 → `emit` 写出 `"2em"`；`count=1`。
4. `sum(Abs 3pt)`：非零；`count>0` 且正值 → 写 `" + "` 再 `emit` 写 `"3pt"`；buf=`"2em + 3pt"`；`count=2`。
5. `Drop`（[encode.rs:272-298](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L272-L298)）：`count==2` → 重建 buf = `"calc(" + "2em + 3pt" + ")"`。
6. **最终输出**：`calc(2em + 3pt)`。

**步骤 B：追踪单操作数 `Length(5pt)`（解释为何省略 calc）**

1. `sum(em = Em(0))`：`value == T::zero()` → 直接 return，`count` 仍为 0。
2. `sum(abs = Abs(5pt))`：`count==0` → 写 `"5pt"`；`count=1`。
3. `Drop`：命中 `1 => ()` 分支，**什么都不做**。
4. **最终输出**：`5pt`（无 calc 包裹）。

**为什么省略？** 因为 `5pt` 本身就是一个合法的 CSS 长度；CSS 规范里单个值就是合法的「`<calc-sum>`」，`calc(5pt)` 是冗余写法。typst-html 选择生成最简输出，`calc()` 只在真正需要混合运算（≥2 个操作数）时才出现。

**步骤 C：追踪真正的 `Rel(50% + 2em)`**

入口 `Rel::emit`（[encode.rs:380-384](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L380-L384)）：

```text
w.calc().sum(self.rel).sum(self.abs.em).sum(self.abs.abs)
          └ Ratio(0.5) ┘  └ Em(2) ┘     └ Abs(0) ┘
```

- `sum(Ratio 0.5)`：写 `"50%"`，count=1。
- `sum(Em 2)`：写 `" + 2em"`，count=2。
- `sum(Abs 0)`：零值，跳过。
- `Drop`：count==2 → **`calc(50% + 2em)`**。

> 你可以对照 `Rel` 与 `Length` 的字段定义佐证上面的字段访问：
> [length.rs:44-49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/length.rs#L44-L49)（`Length { abs, em }`）、
> [rel.rs:78-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/rel.rs#L78-L83)（`Rel<T> { rel, abs }`，默认 `T = Length`）。正因为 `Rel.abs` 是 `Length`，才能写 `self.abs.em` 与 `self.abs.abs`。

**预期结果**：三类输入的输出分别是 `calc(2em + 3pt)`、`5pt`、`calc(50% + 2em)`。

> 说明：以上为基于源码的确定性推导，结论可直接由上述行号验证；步骤 D 给出一个可运行的观测实验。

#### 4.2.5 小练习与答案

**练习 1**：若一个 `Rel` 恰好是 `100%`（比例 1.0，em 和 abs 都为 0），序列化结果是什么？

**参考答案**：`sum(Ratio 1.0)` 写 `"100%"`、count=1；`sum(Em 0)` 与 `sum(Abs 0)` 都因零值跳过；`Drop` 命中 `1 => ()`。最终输出 `100%`，不带 calc。

**练习 2**：`CalcWriter::sum` 在遇到负值时为什么不直接写 `"+ -1pt"`，而要先取绝对值写成 `" - 1pt"`？

**参考答案**：源码注释（[encode.rs:240-246](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L240-L246)）说明它假定 `"+ {val}"` 与 `"- {val.neg()}"` 等价。直接写 `+ -1pt` 在 CSS calc 里是非法的（两个运算符相连），而 ` - 1pt` 是合法的减法。取绝对值保证了输出总是合法的 calc 求和表达式。

**练习 3**：为什么 `CalcWriter` 要在 `Drop` 里用「重建 buf」而不是「原地插入」来加 `calc()`？

**参考答案**：因为它要在**已经写好的文本前面**插入 `calc(`。当时的 `EcoString` 尚未提供 `insert_str`（见 [encode.rs:282-283](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L282-L283) 的 TODO 注释），所以采用「前缀 + `calc(` + 中段 + `)`」拼接一个新 buf 的方式。它依赖一个合理假设：每个值的 `ToCss` 只改写自己写进 buf 的那段文本（[encode.rs:286-288](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L286-L288)）。

---

### 4.3 长度类类型：Abs / Em / Length / Angle / Ratio / Rel

#### 4.3.1 概念说明

这一组是 CSS 里最常出现的数值类型，各自序列化策略不同：

- `Abs`：绝对长度。Typst 内部统一以 pt 存储，故 CSS 输出恒为 `...pt`。
- `Em`：字体相对长度，输出 `...em`。
- `Length`：`Abs + Em` 的混合，需要 `calc()`（见 4.2）。
- `Angle`：角度，内部以 deg 存储，输出 `...deg`。
- `Ratio`：纯比例，输出百分比 `...%`。
- `Rel`：`Ratio + Length`，最一般形式，需要 `calc()`（见 4.2）。

注意：Typst 用户可以写 `1in`、`25.4mm`，但导出到 CSS 后**只会出现 pt**，因为 `Abs::emit` 调的是 `to_pt()`。这是「Typst 统一内部表示」在导出端的体现。

#### 4.3.2 核心流程

```text
Abs   → Number(to_pt()) + "pt"
Em    → Number(get())    + "em"
Angle → Number(to_deg()) + "deg"
Ratio → NumberWithPrecision(get()*100, 2) + "%"
Length → calc(em + abs)        // 0/1/2 个操作数，按需 calc
Rel    → calc(rel + em + abs)  // 0..3 个操作数，按需 calc
```

其中 `Number` 是「4 位有效数字」封装（见 4.5），`NumberWithPrecision(_, 2)` 是「保留约 2 位小数」。

#### 4.3.3 源码精读

四个「单成分」类型的实现都很短：

[encode.rs:346-378](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L346-L378) —— `Abs`/`Em`/`Angle`/`Ratio` 的 `emit`：前三个都是「数值 + 单位」，`Ratio` 是「百分比」。

两个「混合」类型委托给 `CalcWriter`：

[encode.rs:360-364](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L360-L364) —— `Length::emit`：`w.calc().sum(self.em).sum(self.abs)`。

[encode.rs:380-384](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L380-L384) —— `Rel::emit`：`w.calc().sum(self.rel).sum(self.abs.em).sum(self.abs.abs)`。

真实调用点在 `rules.rs` 的图片规则里——图片的 `width`/`height` 被设置成 `Rel` 时，会通过 `css.push("width", rel)` 走到这里的 `Rel::emit`：

[rules.rs:800-811](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L800-L811) —— 图片 `width`/`height` 经 `PropertiesBuilder::push` 序列化为 `Rel`（其中 803 行 `css.push("width", rel)`、809 行 `css.push("height", rel)`）。这正是一条「用户写的 `Rel` → CSS `calc()`」的完整链路。

#### 4.3.4 代码实践

**实践目标**：观察「用户写的 `1in` 在导出后变成 pt」。

**操作步骤（源码阅读型）**：

1. 在 `Abs::emit`（[encode.rs:346-351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L346-L351)）确认它调 `self.to_pt()`——无论原单位是什么，都先折算成 pt。
2. 由此推断：在 Typst 里给某元素设置 `1in` 的尺寸并导出 HTML，对应 CSS 值会是 `72pt`（1in = 72pt）。

**预期结果**：CSS 文本里只会出现 `pt` 单位，不会出现 `in`/`mm`/`cm`。

> 待本地验证：可写一段含 `#image(..., width: 1in)` 的 Typst 文档导出 HTML，检查 `style` 中的 `width` 是否为 `72pt`。

#### 4.3.5 小练习与答案

**练习 1**：`Abs::emit` 为什么不需要 `CalcWriter`？

**参考答案**：`Abs` 只有单一成分（pt），不存在单位混合，直接写 `<数值>pt` 即可，无需 calc。

**练习 2**：`Ratio(0.5)` 的 CSS 输出大致是什么？为什么用 `NumberWithPrecision(_, 2)` 而不是 `Number`（4 位有效数字）？

**参考答案**：`0.5 * 100 = 50.0`，保留约 2 位小数后约等于 `50`，加 `%` 得 `50%`。百分比用「2 位小数」更符合 CSS 习惯（如 `33.33%`），而 4 位有效数字对百分比而言精度过高、显得冗长。

---

### 4.4 颜色类类型：Color / Rgb 与多色彩空间

#### 4.4.1 概念说明

Typst 的 `Color` 是一个「跨色彩空间」的统一类型（sRGB、OkLab、OkLch、HSL、HSV、CMYK、Luma、Linear-sRGB 等）。CSS 同样支持多种颜色函数（`#hex`、`rgb()`、`oklab()`、`oklch()`、`hsl()`、`color(srgb-linear ...)`）。所以颜色序列化的核心是：**把 Typst 的某种空间映射到 CSS 对应的写法**。

`Color::emit` 的策略是先把颜色归一到一个「工艺色」`ProcessColor`（专色 spot color 会退到它的 fallback），再按 `ProcessColor` 的变体分派到不同的 CSS 写法。其中最常见、也最有讲究的是 `Rgb`：

- **优先用 `#hex`**：如果颜色的 f32 精度能被 8 位整数「无损」往返（round-trip），就写成简短的 `#rrggbb`（或带 alpha 的 `#rrggbbaa`）。
- **退回 `rgb()`**：如果精度损失超过阈值，就改用 `rgb(r g b / a)` 的浮点比例写法，保留更多信息。

alpha 通道有专门处理：只有当 alpha「不接近 1」时才追加（用 `/` 分隔），完全不透明就不写 alpha。

#### 4.4.2 核心流程

```text
Color::emit
  │  to_process()   ← 归一为 ProcessColor（spot color 取 fallback）
  ▼
  match ProcessColor:
    Rgb/Cmyk/Luma  → to_rgb() → Rgb::emit   （CMYK/Luma 也先转 RGB）
    Oklab          → oklab(...)
    Oklch          → oklch(...)
    LinearRgb      → color(srgb-linear ...)
    Hsl/Hsv        → to_hsl() → hsl(...)     （HSV 先转 HSL）

Rgb::emit
  │  把 f32 量化到 u8 再放回 f32，检查是否 is_very_close
  ├── 很接近 → #rrggbb（alpha≠255 时追加 aa）
  └── 不接近 → rgb(r g b) + 可选 / a
```

#### 4.4.3 源码精读

`Color::emit` 的分派——注意 CMYK/Luma 都被统一转成 RGB 再输出（CSS 没有广泛支持的 cmyk 语法）：

[encode.rs:396-410](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L396-L410) —— `Color::emit`：先 `to_process()`，再按变体选择 CSS 函数；`Rgb/Cmyk/Luma` 一律走 `to_rgb()`。

`Rgb::emit` 的「hex 优先、rgb 兜底」判定，是本模块最精巧之处：

[encode.rs:412-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L412-L438) —— 先把颜色量化到 `u8` 再放回 `f32`（`low` → `high`），逐通道用 `is_very_close` 比较；全都很接近就写 `#hex`，否则写 `rgb()`。alpha 为 `u8::MAX`（255）时不追加，否则追加两位 hex。

其余色彩空间的实现，都是「`call(函数名, 空格分隔)` + 各通道参数 + 可选 alpha」的同一套模板：

[encode.rs:440-479](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L440-L479) —— `Oklab`/`Oklch`/`LinearRgb`/`Hsl` 的 `emit`：分别映射到 `oklab()`、`oklch()`、`color(srgb-linear ...)`、`hsl()`。

「可选 alpha」由 `MaybeAlpha` trait 统一处理——只有 alpha 不接近 1 时才用 `/` 加一个参数：

[encode.rs:488-498](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L488-L498) —— `maybe_alpha_arg`：`!is_very_close(value, 1.0)` 时才追加 alpha。

辅助函数 `to_ratio`/`to_angle` 只是把裸 `f64` 包成 `Ratio`/`Angle`，复用它们的 `ToCss`：

[encode.rs:501-508](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L501-L508) —— `to_angle`（→ `...deg`）与 `to_ratio`（→ `...%`）。

#### 4.4.4 代码实践

**实践目标**：理解 `Rgb` 在「能 hex」与「不能 hex」两种情况下的输出差异。

**操作步骤（源码阅读型）**：

1. 读 `Rgb::emit`（[encode.rs:412-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L412-L438)）。
2. 假设一个 `Rgb` 的 red=1.0、green=0.0、blue=0.0、alpha=1.0（纯红）：量化到 u8 是 (255, 0, 0, 255)，放回 f32 后与原值「很接近」，于是走 hex 分支，alpha=255 不追加 → 输出 `#ff0000`。
3. 假设一个落在 8 位网格「缝隙」里的颜色（如 red=0.5000001 这种量化后无法很接近还原的值）：`is_very_close` 不成立 → 走 `rgb(...)` 分支，输出 `rgb(50% 0% 0%)`（各通道用 `to_ratio` 转百分比）。

**预期结果**：常见颜色得到简短 hex；高精度「网格缝」颜色退回 `rgb()` 浮点写法，避免精度损失。

> 待本地验证：可在 Typst 中分别用 `rgb("#ff0000")` 与一个非整数值（如 `color.rgb(0.5000001, 0, 0)`）导出 HTML，对比 `style` 中颜色写法。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Color::emit` 里 CMYK 颜色也走 `to_rgb()`？

**参考答案**：CSS 没有被广泛支持的 `cmyk()` 语法（`device-cmyk()` 兼容性差）。typst-html 选择把 CMYK 折算成最通用的 sRGB 表达，保证浏览器能渲染；损失的是 CMYK 的「印刷精确性」，但换来跨浏览器可用性。

**练习 2**：一个 alpha = 0.5 的红色，`Rgb::emit` 大致输出什么？

**参考答案**：量化后 (255,0,0,128) 与原值很接近 → hex 分支；alpha=128 ≠ 255 → 追加两位 hex → `#ff000080`。（`80` 即十进制 128 的十六进制。）

---

### 4.5 is_very_close 与 Number：精度边界与有效数字

#### 4.5.1 概念说明

本模块把 4.4 里反复出现的「很接近」讲清楚，并解释 `Number` 的「4 位有效数字」从何而来。

`is_very_close(a, b)` 判断两个分量是否「在 12 位色深内没有差别」。所谓 12 位色深，指每个通道有 \(2^{12}=4096\) 级，相邻两级差 \(\frac{1}{4096}\)，半级即精度边界：

\[
\mathrm{EPS} = \frac{0.5}{2^{12}} = \frac{1}{8192} \approx 0.000122
\]

只要 \(|a-b| < \mathrm{EPS}\)，就认为「人眼/屏幕看不出差别」，可以放心用简短写法。

而 `Number` 用「4 位有效数字」来打印数值。对于 \([0,1]\) 区间的数，4 位有效数字给出的精度是 \(\frac{1}{10000} = 0.0001\)，比上面的 EPS（≈0.000122）**更细**。源码注释明确点出了这层关系：4 位有效数字「比 12 位还多」。

这条呼应的意义在于：**数值打印精度（4 位有效数字）不亚于颜色判定精度（12 位），所以序列化不会因为「打印时多舍入了一位」而意外跨越 `is_very_close` 的边界**，整套精度设计是自洽的。

#### 4.5.2 核心流程

```text
is_very_close(a, b):
  EPS = 0.5 / 2^12          // ≈ 0.000122
  return |a - b| < EPS

Number<T>(t).emit:
  → NumberWithPrecision(t, 4)   // 4 位有效数字

NumberWithPrecision<T>(t, n).emit:
  → write_fmt(round_with_precision(t, n))   // 四舍五入到 n 位
```

#### 4.5.3 源码精读

`is_very_close` 全貌：

[encode.rs:510-517](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L510-L517) —— `MAX_BIT_DEPTH = 12`，`EPS = 0.5 / 2^12`，返回 `|a-b| < EPS`。注释说明 12 位是「业界合理最高色深」。

`Number` 与 `NumberWithPrecision`：

[encode.rs:325-344](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L325-L344) —— `Number` 固定 4 位有效数字（注释解释「4 位有效数字 ≈ 1/10000，比 12 位更细」）；`NumberWithPrecision` 可指定位数，经 `typst_utils::round_with_precision` 四舍五入后用 `Display` 打印。

`is_very_close` 的两个真实用武之地：

- `Rgb::emit` 里逐通道判断能否用 hex（[encode.rs:420-424](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L420-L424)）。
- `maybe_alpha_arg` 里判断 alpha 是否为 1（[encode.rs:494](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L494)）。

#### 4.5.4 代码实践

**实践目标**：用一个具体数验证「4 位有效数字比 12 位精度更细」。

**操作步骤（纸笔型）**：

1. 取一个 \([0,1]\) 内的数，如 `0.314159`。
2. 4 位有效数字打印：`0.3142`（四舍五入到第 4 位有效数字），与原值差 \(\approx 0.000041\)。
3. 对照 EPS ≈ `0.000122`：`0.000041 < 0.000122`，说明**即使经过了 4 位有效数字的舍入，误差仍远小于 12 位色深的判定阈值**。

**预期结果**：确认 4 位有效数字的舍入误差不会把一个「本来 is_very_close 成立」的颜色推到「不成立」一侧——精度设计自洽。

> 说明：本实践为纸笔推导，结论直接来自上述常量。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `MAX_BIT_DEPTH` 从 12 调成 8，`Rgb::emit` 的行为会怎样变化？

**参考答案**：EPS 会变大（\(0.5/2^8 = 1/512 \approx 0.00195\)），`is_very_close` 更容易成立，于是更多颜色被判为「可 hex」，输出更短；代价是允许更大的量化误差（最多约 0.002 的通道偏差）。

**练习 2**：`Number` 用「有效数字」而非「小数位数」，对 `Abs::emit`（输出 pt）有什么好处？

**参考答案**：有效数字对大数和小数都合适——`1234pt` 会被表示为约 `1234`（4 位有效数字），`0.001234pt` 会被表示为约 `0.001234`（同样是 4 位有效数字）。若用固定小数位数，要么截断大数、要么给小数拖一堆无意义零，都不理想。

---

## 5. 综合实践

**任务**：用一条真实可运行的链路，把本讲五个模块串起来——从「用户写一个 `Rel` 尺寸」到「HTML 里出现 `calc()`」，再到「颜色被写成 hex」。

**步骤 D（可运行观测实验）**：

1. 准备一张图片 `photo.png`（任意小图即可；若没有，可用任意 Typst 能识别的图片）。
2. 写一个 Typst 文件 `demo.typ`：

   ```typst
   // 示例代码
   #image("photo.png", width: 50% + 2em)
   #image("photo.png", width: 100pt)
   #image("photo.png", width: 2em + 3pt)

   #box(fill: rgb("#ff0000"))[红]
   ```

3. 用 typst-cli 导出 HTML：

   ```bash
   typst compile --format html demo.typ demo.html
   ```

4. 在 `demo.html` 中查找 `<img>` 的 `style` 属性（可用文本搜索 `width:`）。

**需要观察的现象与预期结果**（依据本讲源码推导）：

| 输入 | 经由 | 预期 CSS（在 `style` 中） |
|------|------|--------------------------|
| `width: 50% + 2em` | `Rel::emit` → CalcWriter（count=2） | `width: calc(50% + 2em)` |
| `width: 100pt` | `Rel::emit` → CalcWriter（只有 abs 非零，count=1） | `width: 100pt`（无 calc） |
| `width: 2em + 3pt` | 这是 `Length`（图片尺寸内部会包成 Rel，em+abs 非零，count=2） | `width: calc(2em + 3pt)` |
| `fill: rgb("#ff0000")` | `Color::emit` → `Rgb::emit`（hex 分支） | 颜色以 `#ff0000` 形式出现 |

> 注意：图片 `width` 在 `rules.rs` 中是按 `Rel` 处理的（`css.push("width", rel)`）。`50% + 2em` 与 `2em + 3pt` 都会进入 `Rel::emit` 的 `CalcWriter`；纯 `100pt` 因只有绝对部分非零而省略 calc。这正是 4.2 的「单操作数省略 calc」在真实导出中的体现。
>
> 待本地验证：实际 `style` 的确切文本（如是否还含 `image-rendering` 等其它属性、calc 内空格风格）以本地导出结果为准；上表的 `calc(...)` 与 hex 结论由源码确定。

完成本实践后，你应当能把「Typst 类型 → `ToCss::emit` → `CssWriter`/`CalcWriter` → buf 文本 → `PropertiesBuilder` → `style` 属性」这条链路从头讲到尾。

## 6. 本讲小结

- **emit 模式**：`ToCss` trait 把序列化定义为「写进共享的 `CssWriter`」，而非返回字符串；失败经 `fail()` 置 `error` 标志并由 `PropertiesBuilder` 丢弃，实现优雅降级。
- **CalcWriter 的惰性策略**：先记下起始字节下标、边写边数操作数；`Drop` 时按 count 三分支——`0 ⇒ "0"`、`1 ⇒ 不包裹`、`≥2 ⇒ 包 calc()`。**单操作数省略 calc** 是核心优化。
- **零值省略 + 负值转减法**：`sum` 跳过零成分，负值取绝对值写成 ` - `，保证输出始终是合法的 calc 求和。
- **长度类**：`Abs`/`Em`/`Angle` 是「数值 + 单位」（Abs 恒为 pt）；`Length`/`Rel` 因混合单位而走 `calc()`；`Ratio` 输出百分比。
- **颜色类**：`Color` 按色彩空间分派到不同 CSS 函数；`Rgb` 优先 hex（靠 `is_very_close` 判定 8 位往返无损），否则退回 `rgb()`；alpha 不接近 1 才追加。
- **精度自洽**：`is_very_close` 用 12 位色深阈值（EPS ≈ 0.000122），`Number` 用 4 位有效数字（≈ 0.0001），后者更细，故打印舍入不会跨越颜色判定边界。

## 7. 下一步学习建议

本讲讲完了「单个 Typst 值如何变成 CSS 文本」。接下来可以：

1. **回看 u4-l3 的 `resolve_inline_styles`**：现在你能完全理解它把 `HtmlElement.css`（本讲序列化产物）与用户手写 `style` 合并时，「编译器 CSS 在前、用户值在后」的覆盖语义。
2. **进入 u5（编码层）**：本讲产出的是「CSS 值文本」，而这些值最终要被 `encode.rs`（DOM 编码器，注意与本讲的 `css/encode.rs` 同名但不同文件）写入 HTML 属性并做字符转义——建议接着读 u5-l1（DOM 到 HTML 字符串的编码）与 u5-l2（字符集与转义），把「值」放进「属性」的最后一环补上。
3. **扩展阅读**：若想了解 CSS `calc()` 在浏览器里的合法语法细节，可对照 [CSS Values and Units 规范的 calc() 一节](https://www.w3.org/TR/css-values-4/#calc-internal)；typst-html 的 `CalcWriter` 正是该规范 `<calc-sum>` 的最小实现。
