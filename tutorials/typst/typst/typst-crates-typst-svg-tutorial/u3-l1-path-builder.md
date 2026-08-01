# SVG 路径构建器 path.rs

## 1. 本讲目标

SVG 用一段 `d` 属性的「路径数据」字符串描述任意矢量形状（矩形、曲线、字形轮廓……）。本讲聚焦 typst-svg 里**生成这段字符串**的那个工具类型 `SvgPathBuilder`。学完后你应当能够：

- 说清 `SvgPathBuilder` 为什么用**相对坐标**而不是绝对坐标，以及它如何借此压缩 SVG 文件体积；
- 画出 `move_to` / `line_to` / `curve_to` / `quad_to` / `arc` / `close` 每个命令的执行流程，并解释 `line_to` 在纯水平/纯垂直时退化成 `h` / `v` 的优化；
- 看懂 `with_translate` / `with_scale` / `empty` 三种构造方式的差异，以及为什么提取字形轮廓（`outline_glyph`）必须用 `with_scale`。

本讲是「矢量原语」单元的第一讲，承接上一讲 [`u2-l3`](u2-l3-write-abstraction.md) 里 `SvgWrite` / `SvgFormatter` 那支「笔」——`SvgPathBuilder` 正是握着这支笔、把几何坐标翻译成 SVG 文本的人。

## 2. 前置知识

阅读本讲前，最好已经了解以下概念（不熟悉也没关系，下面会顺带解释）：

- **SVG path 数据**：一段形如 `M 0 0 L 100 50 Z` 的字符串。大写命令（`M`/`L`）的参数是**绝对坐标**，小写命令（`m`/`l`）的参数是**相对坐标**（相对「当前画笔位置」的增量）。
- **贝塞尔曲线**：`c` 是三次贝塞尔（两个控制点），`q` 是二次贝塞尔（一个控制点）。TrueType 字形用二次曲线，OpenType CFF 字形用三次曲线。
- **`SvgWrite` trait**（上一讲）：定义了 `push_str` / `push_num` / `push_nums` 等底层写入方法，其中 `push_num` 会把数字四舍五入到 \(10^{-9}\) 精度、整数走 `itoa` 快路径（避免 `50.0`）、浮点走 `ryu` 最短表示。本讲里 `SvgPathBuilder` 正是通过 `SvgFormatter`（`SvgWrite` 的实现）来写字符串的。
- **`typst_utils::defer`**：一个借用工具，返回一个 `DerefMut` 句柄；句柄被 drop 时才执行你传入的闭包。本讲几乎所有命令都靠它实现「先用旧值算增量、再更新基准点」。

## 3. 本讲源码地图

本讲的核心是一个文件，但它和外部有两条关键调用链：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/path.rs` | SVG path 数据构建器 | 全部内容（203 行） |
| `src/shape.rs` | 形状渲染 | `convert_geometry_to_path` / `convert_curve`：`SvgPathBuilder` 的两个主要调用方 |
| `src/text.rs` | 文本/字形渲染 | `render_path_glyph`：用 `with_scale` + `outline_glyph` 提取字形轮廓 |
| `crates/typst-utils/src/lib.rs` | 工具函数 | `defer`：相对坐标得以正确计算的关键 |

`SvgPathBuilder` 是 **crate 内部类型**（`path` 模块在 `lib.rs` 里是私有 `mod path;`，未对外 `pub use`），所以你无法在 typst-svg 之外直接 `use` 它。这也决定了本讲的实践以**源码阅读 + 手工推演**为主。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**(4.1)** `SvgPathBuilder` 结构与相对坐标模型；**(4.2)** 六个绘图命令及其体积优化；**(4.3)** `outline` 模块如何为字体字形提取轮廓。

### 4.1 SvgPathBuilder 与相对坐标模型

#### 4.1.1 概念说明

要把一个矢量形状写进 SVG，最朴素的做法是给每个顶点都写**绝对坐标**，例如一个 \(100 \times 50\) 的矩形：

```
M 0 0 L 0 50 L 100 50 L 100 0 Z
```

这里每个 `L` 后面都是完整的绝对坐标。问题是：真实文档里大量形状的顶点彼此**靠得很近**，绝对坐标动辄是 `347.82`、`891.05` 这样的「大数」，而它们的**增量**却可能是 `0`、`12`、`-3` 这样的小数。如果改写**相对坐标**，只记录「从上一个点挪了多少」，字符串就会短很多——这正是 `SvgPathBuilder` 的核心动机：**用相对坐标压缩 path 数据体积**。

为此它需要记住两件事：

- **`last_point`**：上一条命令的终点（绝对坐标），作为计算相对增量的基准。
- **`last_close_point`**：上一次 `move_to` 的位置，供 `Z`（close）命令知道「闭合回哪里」。

外加一个 `scale: Ratio` 字段（用于字形预缩放，见 4.3），以及累积输出字符串的 `path: EcoString`。

#### 4.1.2 核心流程

每条命令的模板是统一的「三步走」：

1. **登记延迟更新**：用 `defer(self, |b| b.last_point = target)` 创建一个句柄，它**会在 drop 时**才把 `last_point` 改成新目标。
2. **算增量**：趁句柄还活着、`last_point` 还是旧值，调用 `map(target)` 计算相对增量并写入字符串。
3. **自动提交**：函数返回、句柄 drop，`last_point` 才被更新成新目标。

`map` 是增量计算的核心：

\[
\Delta = \text{scale} \cdot \bigl(p_{\text{target}} - p_{\text{last}}\bigr)
\]

`defer` 的妙处在于它把「读旧值」和「写新值」用作用域天然隔开——你不必小心地排临时变量的顺序，编译器保证 `map` 看到的永远是**上一个** `last_point`。

#### 4.1.3 源码精读

结构体定义，四个字段各司其职：

[src/path.rs:8-15](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L8-L15) — `SvgPathBuilder` 持有输出字符串、缩放比、闭合基准点、当前画笔点。

`map` 把绝对目标换算成相对增量（再乘 `scale`）：

[src/path.rs:61-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L61-L63) — `map(pos) = scale * (pos - last_point)`，相对坐标的公式实现。

三种构造方式覆盖三类使用场景：

[src/path.rs:19-30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L19-L30) — `with_translate(pos)`：先写一个绝对 `M pos` 把画笔摆到起点，`scale = 1`。`shape.rs` 的 `convert_geometry_to_path` / `convert_curve` 用它。

[src/path.rs:33-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L33-L40) — `with_scale(scale)`：起点固定写 `M 0 0`，但把 `scale` 存下来。字形轮廓提取（`text.rs`）用它，见 4.3。

[src/path.rs:43-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L43-L50) — `empty()`：既无起点命令、`scale = 1`，从空字符串开始累积。

最后用 `finsish`（源码中如此拼写，注意不是 `finish`）交出字符串：

[src/path.rs:53-55](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L53-L55) — 消费 builder、返回内部的 `EcoString`。

`defer` 本身定义在 typst-utils，理解它的 drop 语义是看懂本讲所有命令的前提：

[crates/typst-utils/src/lib.rs:427-440](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L427-L440) — `defer` 返回的 `DeferHandle` 在 `Drop::drop` 里才调用闭包；闭包通过 `Deref`/`DerefMut` 拿到 `&mut T` 去改原对象。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「`map` 读旧值、`defer` 延迟写新值」的直觉。
2. **操作步骤**：打开 `src/path.rs`，在 `line_to`（见 4.2.3）里标出三处：① `defer` 创建句柄（延迟更新 `last_point`）、② `builder.map(pos)`（此刻读到的是**旧** `last_point`）、③ 函数末尾隐式 drop（此刻才写入新 `last_point`）。
3. **需要观察的现象**：把 ② 和 ③ 的顺序换一下会怎样？若先更新 `last_point` 再 `map`，则 `map` 算出的增量恒为 `0`，所有相对命令都会塌缩——这会让你切身理解为什么必须用 `defer` 把更新推迟到末尾。
4. **预期结果**：`map` 始终基于「上一个终点」计算增量，`last_point` 始终在命令结束后才前进到新终点。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SvgPathBuilder` 要单独维护 `last_close_point`，而不能在 `close()` 时直接沿用 `last_point`？

> **答案**：`Z` 命令的语义是「闭合回**本子路径的起点**」，也就是上一次 `move_to` 落笔的位置，而非「上一个任意命令的终点」。比如 `move_to(A) → line_to(B) → line_to(C) → close()`，闭合应该回到 `A`（即 `last_close_point`），而不是 `C`（`last_point`）。所以 `move_to` 会同时更新这两个字段，而其它命令只更新 `last_point`。

**练习 2**：`empty()` 和 `with_translate(Point::zero())` 都把 `scale` 设为 1，它们的输出字符串有何不同？

> **答案**：`empty()` 的 `path` 是空字符串，什么都不写；`with_translate(Point::zero())` 会预先写一个 `M 0 0`。所以「完全从零开始拼」用 `empty()`，而「需要一个明确起点」用 `with_translate`。

### 4.2 相对绘图命令（move / line / curve / quad / arc / close）

#### 4.2.1 概念说明

`SvgPathBuilder` 提供了与 SVG path 命令一一对应的方法。每个方法都遵循 4.1.2 的「三步走」模板，区别只在**写哪条命令字母**和**做哪些体积优化**：

| 方法 | 输出命令 | 含义 | 备注 |
| --- | --- | --- | --- |
| `move_to` | `m` | 相对移动（抬笔） | 增量为 0 时**整条命令跳过** |
| `line_to` | `l` / `h` / `v` | 相对直线 | 纯水平→`h`、纯垂直→`v`，省一个数 |
| `curve_to` | `c` | 三次贝塞尔（2 控制点 + 终点） | 6 个数 |
| `quad_to` | `q` | 二次贝塞尔（1 控制点 + 终点） | 4 个数；TrueType 字形用它 |
| `arc` | `a` | 椭圆弧 | 7 个参数 |
| `close` | `Z` | 闭合子路径 | 把画笔重置回 `last_close_point` |

#### 4.2.2 核心流程

所有「相对命令」方法的结构高度一致，伪代码如下：

```
fn xxx_to(target, ...):
    builder = defer(self, |b| b.last_point = target)   # 延迟更新基准点
    delta   = builder.map(target)                      # 用【旧】last_point 算增量
    f       = builder.write()                           # 拿到 SvgFormatter
    f.push_str("<命令字母> ")
    f.push_nums([...delta 各分量...])                  # 写入增量
    # —— 函数返回，builder drop，last_point = target 生效 ——
```

`close` 是唯一例外：它不接收目标点，只写 `Z `，然后把 `last_point` 重置回 `last_close_point`。

#### 4.2.3 源码精读

`move_to`：增量为零时直接跳过，避免写出无意义的 `m 0 0`：

[src/path.rs:102-114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L102-L114) — `move_to` 同时更新 `last_point` 和 `last_close_point`；若 `map(pos) == (0,0)` 则不输出任何字符。

`line_to`：本讲的体积优化典范——根据增量分情况选命令：

[src/path.rs:116-132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L116-L132) — `dx≠0 且 dy≠0` 用 `l dx dy`（两个数）；`dx≠0, dy=0` 退化为 `h dx`（一个数）；`dx=0, dy≠0` 退化为 `v dy`（一个数）。

`curve_to` 与 `quad_to`：标准贝塞尔，三个/两个点都过 `map` 转成增量：

[src/path.rs:134-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L134-L151) — 三次贝塞尔 `c`，控制点和终点全部用相对增量。

[src/path.rs:153-162](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L153-L162) — 二次贝塞尔 `q`，主要服务于 TrueType 字形轮廓（见 4.3）。

`arc`：相对椭圆弧 `a`，七个参数（rx, ry, x 轴旋转, large-arc-flag, sweep-flag, dx, dy）：

[src/path.rs:76-100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L76-L100) — `arc` 同样先用 `defer` 延迟更新 `last_point = pos`，再把 `radius` 与 `pos` 都过 `map` 转成相对量。

`close`：写 `Z ` 并把画笔重置回子路径起点：

[src/path.rs:164-167](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L164-L167) — `close` 写 `Z`，并令 `last_point = last_close_point`，使后续命令从子路径起点继续。

**组合示例：`rect()`**。矩形就是把上面这些命令串起来——从 `(0,0)` 出发，依次走到左下、右下、右上，再闭合。注意它**完全复用** `move_to`/`line_to`/`close`，于是自动享受 `h`/`v` 退化优化：

[src/path.rs:67-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L67-L73) — `rect(size)` = `move_to(0,0)` → `line_to(左下)` → `line_to(右下)` → `line_to(右上)` → `close()`。

#### 4.2.4 代码实践（手工推演型）

1. **实践目标**：亲手验证 `rect()` 如何借助 `h`/`v` 退化，把一个矩形压到最短。
2. **操作步骤**：设 `size = (100pt, 50pt)`，按 [src/path.rs:67-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L67-L73) 的四步，逐步追踪 `last_point` 与 `map` 的输出（构造器为 `with_translate(Point::zero())`，故初始 `last_point = (0,0)`、`scale = 1`，且已写入 `M 0 0`）。
3. **需要观察的现象**：每一步记下「调用前 `last_point`」「`map` 算出的增量」「选了哪条命令」。重点看四个 `line_to` 里有哪些退化成了 `h`/`v`。
4. **预期结果**（待本地验证）：

   | 步骤 | 调用 | 调用前 last_point | 增量 | 输出 |
   | --- | --- | --- | --- | --- |
   | 1 | `move_to((0,0))` | `(0,0)` | `(0,0)` | （增量为 0，跳过） |
   | 2 | `line_to((0,50))` | `(0,0)` | `(0,50)` | `v 50` |
   | 3 | `line_to((100,50))` | `(0,50)` | `(100,0)` | `h 100` |
   | 4 | `line_to((100,0))` | `(100,50)` | `(0,-50)` | `v -50` |
   | 5 | `close()` | — | — | `Z ` |

   最终 `d = "M 0 0 v 50 h 100 v -50 Z "`。对比朴素绝对写法 `M 0 0 L 0 50 L 100 50 L 100 0 Z`，相对 + `h`/`v` 写法明显更短。可选：本地用 `typst compile` 生成一个 SVG，用文本编辑器打开、搜索对应 `<path>` 的 `d` 属性，验证是否出现 `h`/`v`（实际文档里矩形常带描边/填充属性，但 `d` 的命令字母规律一致）。

#### 4.2.5 小练习与答案

**练习 1**：`line_to` 为什么在 `dx=0, dy=0` 时**什么都不输出**，而不是写 `l 0 0`？

> **答案**：看 [src/path.rs:122-131](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L122-L131) 的三个分支——只有 `dx≠0` 或 `dy≠0` 时才进入某一个分支输出命令；`dx=0 且 dy=0` 时三个 `if` 都不成立，直接什么都不写。写 `l 0 0` 是无效移动，徒增体积，所以省略。

**练习 2**：`move_to` 里也有一处和「体积优化」相关的判断，它是什么？

> **答案**：`if pos != Point::zero()`（这里的 `pos` 是 `map` 后的增量）——若相对增量为 0，说明目标就是当前画笔位置，`m 0 0` 毫无意义，于是整条命令跳过。见 [src/path.rs:109-113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L109-L113)。

### 4.3 outline 模块：为 ttf_parser 提取字形轮廓

#### 4.3.1 概念说明

文字本质上是**字形轮廓的填充**。字体文件（`.ttf`/`.otf`）里每个字形存的是一组数学曲线（轮廓），需要由字体解析库「走一遍」轮廓、把曲线还原成路径数据。typst 用 `ttf_parser` 库做这件事，它的工作方式是**回调**：你实现一个 `ttf_parser::OutlineBuilder` trait，把它交给 `outline_glyph`，解析器在遍历轮廓的过程中**反向调用**你的 `move_to`/`line_to`/`quad_to`/`curve_to`/`close`。

`SvgPathBuilder` 恰好已经有这一组同名方法！所以在 `path.rs` 末尾有一个 `outline` 子模块，直接为 `SvgPathBuilder` 实现 `ttf_parser::OutlineBuilder`，把字体解析器的回调转发给自家的相对坐标命令——于是字形轮廓也自动享受相对坐标压缩。

关键点：**字形轮廓的原始坐标是「字体单位」（font units）**，通常是几千为单位的整数（以 `units_per_em` 为基准），而不是 pt。要让它出现在正确大小，必须按 `text.size / units_per_em` 缩放。这正是 `with_scale` 的用途。

#### 4.3.2 核心流程

1. `text.rs` 算出缩放比 `scale = text.size.to_pt() / font.units_per_em()`。
2. 用 `SvgPathBuilder::with_scale(scale)` 创建 builder（此时 `scale` 被存入字段，后续每次 `map` 都会乘上它）。
3. 调用 `font.ttf().outline_glyph(glyph_id, &mut builder)`，让 `ttf_parser` 反复回调 builder 的 `move_to`/`line_to`/`quad_to`/`curve_to`/`close`。
4. 每次回调里，`point(x, y)` 把字体单位的 `f32` 转成 `Point`，再由 `SvgPathBuilder` 的方法经 `map` **预缩放**成 pt 增量并写入。
5. `finsish()` 交出 `d` 字符串，包成 `RenderedGlyph::Path` 存入去重表。

为什么必须**预缩放**？因为字体轮廓是整数级的大数（如一个字宽 1200 字体单位），直接写进 SVG 会得到错误大小；而把 `scale` 烘进 `map` 之后，输出即「真实 pt 单位的相对增量」，这条 path 就能：

- 直接被描边/填充/图案正确作用（源码注释原话：「so strokes and fill patterns don't need to consider text size glyph scaling」）；
- 以 `<symbol>` + `<use>` 机制在多处复用，复用时只需平移、无需再缩放 path 数据（详见 [u4-l2](u4-l2-glyph-defs.md)）。

#### 4.3.3 源码精读

`OutlineBuilder` 实现——五个回调各自转发给 `SvgPathBuilder` 的同名方法：

[src/path.rs:176-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L176-L196) — 为 `SvgPathBuilder` 实现 `ttf_parser::OutlineBuilder`。注意 TrueType 字形主要触发 `quad_to`（二次曲线），OpenType CFF 字形才会触发 `curve_to`（三次曲线）。

`point` 辅助函数：把 `ttf_parser` 的 `f32` 字体单位坐标转成 `Point`（`Abs::pt`）：

[src/path.rs:199-201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L199-L201) — `point(x, y)` 把 `f32` 提升为 `f64` 再包成 `Abs::pt`，作为后续 `map` 的输入。

调用方在 `text.rs`：用 `with_scale` + `outline_glyph`，且把 `scale` 纳入去重键：

[src/text.rs:64-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L64-L73) — `render_path_glyph` 的去重插入：`scale = text.size / units_per_em`，键为 `(&font, glyph_id, scale)`，值里用 `with_scale(scale)` 建 builder 再 `outline_glyph`。

> 对比：`with_translate` 适合「已知一个绝对起点、坐标已是 pt」的形状（`shape.rs` 的几何体）；`with_scale` 适合「坐标是字体单位、需要整体缩放到 pt」的字形。两者的 `scale` 字段一为 1、一为 `units→pt` 比，这正是 4.1 里那个 `scale` 字段存在的意义。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：说清「为什么字形提取必须用 `with_scale`，而不是 `with_translate`」。
2. **操作步骤**：打开 [src/text.rs:64-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L64-L73)，找到 `scale` 的计算式 `text.size.to_pt() / text.font.units_per_em()`。假设某字体 `units_per_em = 1000`、`text.size = 10pt`，则 `scale = 0.01`。再追踪 `outline_glyph` 回调 `line_to(x=500, y=0)`（字体单位）时，`map` 会算出怎样的增量。
3. **需要观察的现象**：若改用 `with_translate`（`scale = 1`），`map` 会原样输出 `500`，这个数比真实需要的 \(500 \times 0.01 = 5\) 大 100 倍——字形会变成巨型。`with_scale(0.01)` 把缩放烘进 `map`，输出才是正确的 `5`。
4. **预期结果**：`with_scale` 让字形轮廓从「字体单位」自动换算到「pt」，是字形能以正确尺寸渲染的前提；同时去重键里带上 `scale`（见步骤 2 的 `key`），保证同一字形在不同字号下生成各自独立的路径。

#### 4.3.5 小练习与答案

**练习 1**：`outline_glyph` 是 `ttf_parser` 调用我们的代码，还是我们调用 `ttf_parser`？

> **答案**：是我们调用 `ttf_parser` 的 `outline_glyph(glyph_id, &mut builder)`，但具体轮廓的「遍历」由 `ttf_parser` 内部完成，它在遍历过程中**反向回调**我们传入的 builder（即 `SvgPathBuilder`）的 `OutlineBuilder` 方法。这种「我调用你、你回头调用我」的模式叫回调（callback）/控制反转。

**练习 2**：为什么去重键是 `(&font, glyph_id, scale)` 而不是只 `(&font, glyph_id)`？

> **答案**：同一个字形在不同字号下，经 `with_scale` 产生的 `d` 字符串不同（数字被不同比例缩放）。若键不含 `scale`，10pt 和 20pt 的同一字形会命中同一条缓存，导致其中一个字号渲染错误。带上 `scale` 后，每个字号各占一条缓存项，既正确又能在同字号内复用。

## 5. 综合实践

把本讲三个模块串起来，做一次「从几何到 `d` 字符串」的完整追踪：

- **任务**：解释一段 `Geometry::Rect(Size::new(Abs::pt(100.0), Abs::pt(50.0)))` 是如何最终变成 `<path d="...">` 写进 SVG 的，并对比「绝对写法」与 typst-svg 实际产出的「相对 + h/v 写法」的长度。
- **步骤**：
  1. 从 [src/shape.rs:177-188](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L177-L188) `convert_geometry_to_path` 入手，确认它用 `with_translate(Point::zero())` 建 builder，并对 `Geometry::Rect(size)` 调用 `builder.rect(size)`。
  2. 进入 [src/path.rs:67-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L67-L73) `rect()`，按 4.2.4 的表格逐行推演，得到 `d = "M 0 0 v 50 h 100 v -50 Z "`。
  3. 回到 `shape.rs`，看 [src/shape.rs:46-47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L46-L47) 把这条字符串作为 `d` 属性挂到 `<path>` 上。
  4. 数一数字符数：绝对写法 `M 0 0 L 0 50 L 100 50 L 100 0 Z` vs 相对写法 `M 0 0 v 50 h 100 v -50 Z`。体会相对坐标 + `h`/`v` 退化对体积的节省（这只是一个矩形；真实文档里成千上万个字形顶点的累积节省非常可观）。
- **延伸思考**：如果是字形而不是矩形，路径会从 `with_translate` 换成 `with_scale`（4.3），但「相对增量 + 退化命令」的压缩逻辑完全不变——这就是把 `SvgPathBuilder` 设计成统一工具类型的价值。

## 6. 本讲小结

- `SvgPathBuilder` 用**相对坐标**记录路径，每个命令只写「相对上一点的增量」，从而压缩 SVG 体积。
- 相对增量的正确性依赖 `typst_utils::defer`：它把「更新 `last_point`」推迟到命令末尾，使 `map` 总是基于**上一个**终点计算增量。
- `line_to` 在纯水平/纯垂直时退化为 `h`/`v`（只写一个数），`move_to` 在增量为零时整条跳过——这是除相对坐标之外的第二层体积优化。
- `rect()` 完全复用 `move_to`/`line_to`/`close`，因此自动获得 `h`/`v` 退化，一个 \(100\times50\) 矩形只需 `M 0 0 v 50 h 100 v -50 Z`。
- 三种构造方式分工：`with_translate` 给已知绝对起点的形状、`with_scale` 给需要预缩放的字形轮廓、`empty` 给完全自定义的拼接。
- `outline` 子模块为 `SvgPathBuilder` 实现 `ttf_parser::OutlineBuilder`，让字体字形轮廓直接复用相对坐标压缩；`text.rs` 用 `with_scale(text.size / units_per_em)` 把字体单位换算到 pt。

## 7. 下一步学习建议

本讲只解决了「**路径数据怎么生成**」。接下来：

- 想看 `SvgPathBuilder` 如何被形状渲染调用，且 `d` 如何挂到 `<path>` 上、填充与描边如何配合，请读 [u3-l2 形状渲染与描边 shape.rs](u3-l2-shape-rendering.md)。
- 想看字形 path 如何被收进 `<defs><symbol>` 并通过 `<use>` 复用，请读 [u4-l2 字形定义与符号复用 write_glyph_defs](u4-l2-glyph-defs.md)。
- 若想再深入一层，可以跟踪 `shape.rs` 里 `convert_geometry_to_path` 上的 `#[comemo::memoize]` 注解——它把同一几何体的 path 字符串缓存起来，是相对坐标压缩之外的「跨调用」级优化。
