# SVG 路径构建器 path.rs

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `SvgPathBuilder` 为什么用「相对坐标」记录路径数据，以及它如何把外界传入的「绝对坐标」转换成相对增量。
- 逐行解释 `move_to` / `line_to` / `curve_to` / `quad_to` / `close` 五个基本命令的写入逻辑，理解 `line_to` 在纯水平/纯垂直时退化为 `h` / `v` 命令的体积优化。
- 看懂 `arc` 圆弧命令的参数映射，以及 `with_translate` / `with_scale` / `empty` 三种构造方式各自的服务场景。
- 理解 `outline` 子模块如何为 `ttf_parser` 实现 `OutlineBuilder` trait，从而把字体里的字形轮廓「翻译」成 SVG 路径，并说清 `with_scale` 在其中的关键作用。

本讲只关心「路径数据 `d` 字符串是如何拼出来的」，不涉及这条路径最终如何被填色、描边——那是 `shape.rs` / `paint.rs`（后续讲义）的职责。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **SVG 的 `<path>` 与 `d` 属性**。SVG 用一条文本指令串描述任意矢量形状，例如：

  ```text
  M 10 20        移动到绝对坐标 (10,20)（大写=绝对）
  L 30 40        画直线到绝对坐标 (30,40)
  l 5 5          画直线，相对当前点位移 (5,5)（小写=相对）
  h 8            水平向右画 8（只有相对版本 h/H 的“水平”简写有意义，这里 h 是相对）
  v 8            垂直向下画 8（相对）
  c 1 1 2 2 5 5  三次贝塞尔，三个点都用相对位移
  q 2 2 4 0      二次贝塞尔，两个点都用相对位移
  a rx ry rot large sweep dx dy   椭圆弧（相对位移版本）
  Z              闭合子路径，笔尖回到本子路径起点
  ```

  关键区分：**大写命令用绝对坐标，小写命令用相对坐标（相对上一个命令的终点）**。typst-svg 几乎只用小写（相对）命令，这是本讲的核心动机。

- **typst-svg 的分层**（见前置讲义 u1-l2、u2-l3）。`path.rs` 是「路径原语」层，它本身不写 XML，只负责拼出 `d="..."` 里那段字符串；真正把 `<path d="...">` 写进 SVG 的是 `shape.rs`、`paint.rs`、`text.rs` 等上层模块。`path.rs` 不含任何 `impl SVGRenderer`，是一个独立工具类型。

- **`EcoString`**：typst 自带的轻量字符串类型，行为类似 `String` 但更省内存，适合拼装这种短文本。

- **`defer`（来自 `typst-utils`）**：`defer(&mut t, |t| { ... })` 返回一个可解引用为 `&mut T` 的临时句柄，**当该句柄离开作用域被 drop 时，闭包才执行**。这是本讲理解「相对坐标」机制的关键工具，下文会反复用到。

## 3. 本讲源码地图

本讲涉及的源码文件：

| 文件 | 角色 | 本讲用到的部分 |
| --- | --- | --- |
| `src/path.rs` | **主角**。`SvgPathBuilder` 的全部实现，以及为字体字形服务的 `outline` 子模块。 | 全文（仅约 200 行） |
| `src/shape.rs` | 上层调用者之一。把 `Geometry` / `Curve` 转成路径。 | `convert_geometry_to_path`、`convert_curve` |
| `src/text.rs` | 上层调用者之一。用 `with_scale` 提取字形轮廓。 | `render_glyph` 中 `outline_glyph` 的调用 |
| `src/lib.rs` | 用 `convert_curve` 生成裁剪路径。 | `render_group` 中的 clip 分支 |
| `crates/typst-utils/src/lib.rs` | 提供 `defer` 工具。 | `defer` 函数定义 |

阅读顺序建议：先看 `path.rs` 的结构体与 `map`，再看基本命令，最后看 `outline` 子模块，并回头到 `shape.rs` / `text.rs` 看它们如何调用。

## 4. 核心概念与源码讲解

### 4.1 SvgPathBuilder：相对坐标的核心思想

#### 4.1.1 概念说明

`SvgPathBuilder` 是一个「增量式」的字符串拼装器。它接收的每一个绘图命令（移动、画线、画弧……）的坐标参数，**都是绝对坐标**——也就是「世界坐标系下的真实位置」；但它写进字符串的，**全是相对坐标**——也就是「相对上一个命令终点的位移」。

为什么要这么做？因为 SVG 的相对命令更短：

- 绝对写法：`L 100 200 L 105 200 L 105 250`（每个点都要写完整坐标）。
- 相对写法：`l 5 0 l 0 50`（只写位移，数字更短更少）。

对一个动辄包含成千上万个字形轮廓、几十万条路径指令的 SVG 文档来说，这种压缩能显著减小文件体积。`SvgPathBuilder` 的全部设计都围绕「**输入绝对、输出相对**」这一转换展开。

#### 4.1.2 核心流程

转换靠三个状态字段协同完成：

```text
struct SvgPathBuilder {
    path: EcoString,          // 累积输出的字符串
    scale: Ratio,             // 对位移做整体缩放（字形用）
    last_close_point: Point,  // 最近一次 move_to 的点，供 Z 闭合后回到这里
    last_point: Point,        // 上一个命令的终点，相对坐标就是相对它算的
}
```

转换公式只有一行（即 `map`）：

\[ \text{relative}(p) = \text{scale} \times (p - \text{last\_point}) \]

也就是「目标点减去上一个终点，再乘以缩放」。得到相对位移后，把它格式化进 `path` 字符串；**然后把 `last_point` 更新为新的目标点**，为下一条命令做准备。

这里有一个精妙的执行顺序问题：「先算位移、再更新终点」。`path.rs` 用 `typst_utils::defer` 来保证这个顺序：在命令函数里先用「旧」的 `last_point` 算位移，等到函数返回、`defer` 句柄被 drop 时，才把 `last_point` 改成新值。这一点在 4.2 会详细展开。

#### 4.1.3 源码精读

结构体与字段定义（注意两个字段都带注释说明用途）：

[src/path.rs:7-15](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L7-L15) — `SvgPathBuilder` 的四个字段：输出字符串、缩放、闭合回退点、上一终点。

`map` 函数，整个相对坐标机制的「心脏」，只有一行：

[src/path.rs:61-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L61-L63) — 把绝对点 `pos` 转成「相对 `last_point` 的位移」并乘以 `scale`。

三种构造方式，对应三种使用场景：

[src/path.rs:18-30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L18-L30) — `with_translate(pos)`：先写一条**绝对** `M pos` 作为起点，`scale` 设为 1。用于普通形状/曲线（`shape.rs`）。

[src/path.rs:32-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L32-L40) — `with_scale(scale)`：起点 `M 0 0`，但 `scale` 设为给定值。用于字形轮廓（`text.rs`），下文 4.4 详述。

[src/path.rs:42-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L42-L50) — `empty()`：完全空白，连 `M` 都不写，`scale` 为 1。用于圆锥渐变里手工拼扇形（`paint.rs`）。

注意「取结果」的方法名是 `finsish`（多了一个 `s`），这是源码里的真实拼写，并非笔误，调用方也都按这个名字用：

[src/path.rs:52-55](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L52-L55) — `finsish(self)` 消费 builder，返回累积好的 `EcoString`。

`defer` 工具本身的定义在 typst-utils，它的 `Drop` 实现保证了「闭包在句柄销毁时才执行」：

[crates/typst-utils/src/lib.rs:426-457](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L426-L457) — `defer` 返回的 `DeferHandle` 在 `drop` 时取出闭包并执行；`DerefMut` 让你能像用 `&mut T` 一样用它。

#### 4.1.4 代码实践

**实践目标**：用「人脑解释器」跟踪 `with_translate(Point::zero())` 构造出的 builder，在调用 `rect(Size::new(w, h))` 后，`path` 字符串里到底累积了什么。这是典型的「源码阅读型实践」，无需运行。

**操作步骤**：

1. 打开 [src/path.rs:67-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L67-L73) 的 `rect` 方法，以及它调用的 `move_to`、`line_to`、`close`。
2. 假设 `w = 100`，`h = 60`，按下面顺序逐步推演，每一步记下 `map` 的结果和写入的命令。
3. 对照下表「预期结果」核对。

**需要观察的现象**：每条 `line_to` 写入的命令字母会因为位移是否落在坐标轴上而不同——这正是 4.2 要讲的优化。

**预期结果**：

| 调用 | defer 更新（drop 时） | `map`（用旧 last_point） | 写入 |
| --- | --- | --- | --- |
| 构造 `with_translate((0,0))` | — | — | `M 0 0`（初始，绝对） |
| `move_to((0,0))` | last_point=(0,0) | (0,0)-(0,0)=(0,0) | 空（位移为 0，跳过） |
| `line_to((0,60))` | last_point=(0,60) | (0,60)-(0,0)=(0,60) | `v 60`（x=0，垂直） |
| `line_to((100,60))` | last_point=(100,60) | (100,60)-(0,60)=(100,0) | `h 100`（y=0，水平） |
| `line_to((100,0))` | last_point=(100,0) | (100,0)-(100,60)=(0,-60) | `v -60`（x=0，垂直） |
| `close()` | last_point=last_close_point=(0,0) | — | `Z ` |

最终 `path = "M 0 0 v 60 h 100 v -60 Z "`。

**结论**：矩形只用了 3 个单参数命令（`v` / `h`）加一个 `Z`，没有一个完整 `l x y`。这就是相对坐标 + 轴向简写带来的体积收益。这个具体输出「待本地验证」（取决于 `push_num` 的精度格式，但命令字母与符号是可以确定的）。

#### 4.1.5 小练习与答案

**练习 1**：如果用 `with_translate(Point::zero())` 后直接 `line_to((10,20))` 再 `line_to((10,50))`，输出字符串是什么？

**参考答案**：第一条 `line_to((10,20))`：last_point 从 (0,0) 变 (10,20)，位移 (10,20)，写 `l 10 20`。第二条 `line_to((10,50))`：last_point 变 (10,50)，位移 (0,30)，x=0 故写 `v 30`。结果为 `M 0 0 l 10 20 v 30`。

**练习 2**：`last_close_point` 和 `last_point` 有什么区别？为什么需要两个？

**参考答案**：`last_point` 是「上一条命令的终点」，用于计算相对位移；`last_close_point` 是「最近一次 `move_to` 的起点」，用于 `Z` 闭合后让笔尖回到子路径起点。一个子路径里可能画很多线，`last_point` 一直在变；但闭合时应回到子路径的起点而非上一终点，所以需要单独记 `last_close_point`。`close()` 执行后会把 `last_point` 重置为 `last_close_point`，让后续命令从子路径起点继续算位移。

---

### 4.2 基本绘图命令：move_to / line_to / curve_to / quad_to / close

#### 4.2.1 概念说明

有了「输入绝对、输出相对」的机制后，五个基本命令只是「针对不同 SVG 指令」套用同一套模板：

- `move_to`：开始一个新子路径，写 `m dx dy`（或位移为 0 时省略）。
- `line_to`：画直线，写 `l dx dy`；但若位移只在单轴，退化成 `h dx` 或 `v dy`。
- `curve_to`：三次贝塞尔，写 `c dx1 dy1 dx2 dy2 dx dy`（两个控制点 + 终点，都相对）。
- `quad_to`：二次贝塞尔，写 `q dx1 dy1 dx dy`（一个控制点 + 终点）。
- `close`：写 `Z`，闭合。

每个命令都需要做两件事：**先把传入的绝对点经 `map` 转成相对位移并写入；再把 `last_point` 更新为该绝对点**。`path.rs` 用 `defer` 把「更新 `last_point`」这件事推迟到函数末尾，保证「先用旧值算位移、再用新值更新」的正确顺序。

#### 4.2.2 核心流程

以 `line_to` 为例的模板（其它命令同构）：

```text
fn line_to(&mut self, pos):
    builder = defer(self, |b| b.last_point = pos)   # ← 注意：闭包此刻不执行
    delta = builder.map(pos)                         # 用「旧」last_point 算位移
    f = builder.write()
    根据 delta 是否落在坐标轴上，写 l / h / v
    # 函数返回，builder 被 drop → 闭包执行 → last_point = pos（绝对值）
```

`defer` 的妙处：闭包里写的是「把 `last_point` 设成传入的**绝对** `pos`」，而 `map` 读取的是「**旧**的 `last_point`」。两者通过「先读后写」的 drop 顺序错开，于是相对位移永远基于上一个终点计算。如果不用 `defer`，就得先存一份旧值、算完位移再赋新值，代码会啰嗦很多。

`line_to` 额外做了一步轴向优化：

```text
若 delta.x ≠ 0 且 delta.y ≠ 0 → "l dx dy"   （一般直线）
若只有 delta.x ≠ 0           → "h dx"        （水平简写）
若只有 delta.y ≠ 0           → "v dy"        （垂直简写）
若 delta 全为 0               → 什么都不写    （零长度线段省略）
```

#### 4.2.3 源码精读

`move_to`：注意闭包同时更新 `last_point` 和 `last_close_point`（新子路径起点）；位移为 0 时跳过输出，但状态照常更新：

[src/path.rs:102-114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L102-L114) — `move_to`：`defer` 推迟 `last_point`/`last_close_point` 的更新；`if pos != Point::zero()` 实现「移动到当前点」的省略。

`line_to`：本讲最重要的优化所在——三段 `if/else if` 分别对应 `l` / `h` / `v`：

[src/path.rs:116-132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L116-L132) — `line_to`：按位移是否落在 x/y 单轴，选择 `l`、`h`、`v` 三种写法；全零时不输出。

`curve_to`：三个点全部经 `map` 转相对；注意 `defer` 只把 `last_point` 设为终点 `end`（控制点不参与更新）：

[src/path.rs:134-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L134-L151) — `curve_to(p1, p2, end)`：输出相对版三次贝塞尔 `c ...`。

`quad_to`：结构同 `curve_to`，只是少一个控制点：

[src/path.rs:153-162](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L153-L162) — `quad_to(p1, end)`：输出相对版二次贝塞尔 `q ...`。

`close`：这里没用 `defer`（不需要算位移），直接写 `Z ` 并把 `last_point` 复位到 `last_close_point`：

[src/path.rs:164-167](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L164-L167) — `close`：写 `Z `，把笔尖（`last_point`）送回子路径起点。

> 小贴士：`SvgPathBuilder` 没有为「二次贝塞尔」提供独立于 `OutlineBuilder` 的对外方法名差异——`quad_to` 既是普通方法，也被 `outline` 子模块复用（见 4.4）。typst 的 `Curve` 曲线模型目前只用三次贝塞尔与直线，所以 `shape.rs` 调用的是 `move_to`/`line_to`/`curve_to`/`close`；`quad_to` 主要服务于字体字形（很多字体轮廓用二次贝塞尔）。

#### 4.2.4 代码实践

**实践目标**：亲手验证「先读后写」的 drop 顺序确实让相对坐标正确，并理解 `line_to` 的轴向优化在真实上层调用里如何生效。

**操作步骤**：

1. 打开 `src/shape.rs` 的 `convert_curve`，看它如何遍历 `Curve`：
   [src/shape.rs:190-201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L190-L201) — `convert_curve`：把 `CurveItem` 的四种变体（Move/Line/Cubic/Close）分发到 builder 的四个方法。
2. 设想一段曲线：`Move((0,0)) → Line((50,0)) → Line((50,30)) → Cubic((60,30),(70,30),(70,0)) → Close`。
3. 逐条推演 builder 输出。

**需要观察的现象**：第一条 `Line((50,0))` 会退化成 `h 50`，因为它从 (0,0) 到 (50,0) 只在 x 轴移动。

**预期结果**：

| 曲线项 | 写入 |
| --- | --- |
| 构造 `with_translate((0,0))` | `M 0 0` |
| `Move((0,0))` | （位移 0，空） |
| `Line((50,0))` | `h 50` |
| `Line((50,30))` | `v 30` |
| `Cubic(...)` 到 `(70,0)` | `c 10 0 20 0 20 -30` |
| `Close` | `Z ` |

最终约 `M 0 0 h 50 v 30 c 10 0 20 0 20 -30 Z `（精确数字「待本地验证」，取决于 `Cubic` 的控制点坐标，上表假设控制点为相对前点的位移推算）。注意：即使是一条普通折线，也大量复用了 `h`/`v` 简写。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `line_to` 里三个分支的判断顺序是「先判断 `l`，再 `h`，再 `v`」？如果先判断「`delta.x != 0`」会怎样？

**参考答案**：`l`（一般直线）需要同时 `delta.x ≠ 0` 且 `delta.y ≠ 0`，这是最严格的条件，必须先判断；`h` 只要 `delta.x ≠ 0`（此时 `delta.y == 0`），`v` 只要 `delta.y ≠ 0`。若先判断 `delta.x != 0` 就写 `h`，会把 `(5, 5)` 这样的斜向位移误判成水平线，丢掉 y 分量。条件从严到宽的顺序保证了匹配的唯一正确性。

**练习 2**：`curve_to` 的 `defer` 闭包只把 `last_point` 设为 `end`，为什么不设成控制点 `p1` 或 `p2`？

**参考答案**：贝塞尔曲线的「终点」是 `end`，控制点 `p1`/`p2` 只是引导曲线形状、不落在曲线上。下一条命令的相对位移必须相对曲线真正的终点算，所以 `last_point` 只能更新为 `end`。

---

### 4.3 圆弧命令 arc

#### 4.3.1 概念说明

SVG 的椭圆弧指令 `a`（相对）格式为：

```text
a rx ry x-axis-rotation large-arc-flag sweep-flag dx dy
```

含义：以 `rx`、`ry` 为椭圆两轴半径，椭圆 x 轴旋转 `x-axis-rotation` 度，在 `large-arc-flag`（0=小弧/1=大弧）与 `sweep-flag`（0=逆时针/1=顺时针）共同决定的四条候选弧里选一条，画到「相对当前点位移 `(dx, dy)`」处。

`SvgPathBuilder::arc` 不计算几何，只负责把参数格式化进字符串，并和其它命令一样：**对半径和位移都先经 `map` 转成相对+缩放后的值**。

#### 4.3.2 核心流程

```text
fn arc(radius, x_axis_rot, large_arc_flag, sweep_flag, pos):
    builder = defer(self, |b| b.last_point = pos)   # 终点更新推迟
    radius_rel = builder.map(radius.to_point())      # 半径也做相对+缩放
    pos_rel    = builder.map(pos)                    # 终点位移
    写 "a rx ry rot large sweep dx dy"
```

两个 flag 是 `u32`，通过 `as f64` 转成数字写入。注意半径也会被 `map` 处理——在 `with_scale`（字形）场景下，这意味着字形的圆弧半径会被同步缩放，与其它字形坐标一致。

#### 4.3.3 源码精读

[src/path.rs:75-100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L75-L100) — `arc`：`defer` 推迟 `last_point` 更新；半径与终点都经 `map`；七个参数按 `a` 指令顺序写入。

值得注意的点：

- `defer(self, |b| b.last_point = pos)` 与 `line_to` 完全同构——圆弧的终点更新逻辑和直线一样。
- `radius` 先 `to_point()` 转成点，再 `map`。在 `scale == 1` 的普通形状场景下，`map` 退化为「减去 last_point」——但半径是「大小」而非「位置」，减去 last_point 看似奇怪。实际上在 `arc` 的真实调用场景里（`paint.rs` 的圆锥渐变扇形，使用 `empty()` 构造、`last_point` 为 0），减去 0 等于不减，所以半径保持原值；只有在非零 `last_point` 时这个减法才会有影响。这一点「待确认」是否会在字形圆弧中产生预期外行为，但当前上游调用（圆锥渐变）满足 `last_point = 0` 的前提。

#### 4.3.4 代码实践

**实践目标**：找到 `arc` 在真实代码里的唯一调用点，理解它服务的场景。

**操作步骤**：

1. 在 `src/paint.rs` 中搜索 `builder.arc(` 或查看圆锥渐变扇形的构建处：
   [src/paint.rs:200-235](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L200-L235) — 圆锥渐变用 `SvgPathBuilder::empty()` 起手，逐段用 `move_to`/`line_to`/`arc`/`close` 拼出 360 个扇形。
2. 阅读这段代码，确认它用的是 `empty()`（`last_point = 0`）。

**需要观察的现象**：每个扇形由「从圆心 `move_to` → 沿半径 `line_to` 到圆周 → 沿圆周 `arc` 到下一段 → `close` 回圆心」四步构成。

**预期结果**：`arc` 在 typst-svg 中目前只为圆锥渐变服务，那里的 builder 由 `empty()` 构造，`last_point` 始终从圆心或圆周点开始，半径经 `map` 后保持原值。结论：`arc` 的 `map` 半径在现有调用链下是「无害」的。圆锥渐变的完整原理属于专家层讲义 u5-l4，本讲只需记住「`arc` 把七参数原样格式化为相对 `a` 指令」。

#### 4.3.5 小练习与答案

**练习 1**：`arc` 里两个 flag 为什么用 `u32` 类型而不是 `bool`？

**参考答案**：SVG 规范里这两个标志是 `0` 或 `1` 的数字，直接以数字字符写入字符串最方便。用 `u32` 后 `as f64` 再经 `push_num` 输出 `0`/`1`，避免 `bool` 还要额外 match 转字符。这是「贴近输出格式」的务实选择。

**练习 2**：如果 `arc` 的终点 `pos` 恰好等于当前 `last_point`（位移为 0），会发生什么？

**参考答案**：代码没有像 `line_to` 那样对零位移做特判，仍会写一条完整的 `a ... 0 0`。这种「原地弧」在 SVG 规范下几何上未定义（起终点重合），通常表示一个完整的椭圆。当前上游不会产生这种情况，所以未做省略优化。

---

### 4.4 outline 模块：把字体字形翻译成路径

#### 4.4.1 概念说明

文字是 SVG 体积的大头，而文字的本质是「一个一个字形的矢量轮廓」。typst-svg 对**轮廓字形**（outline glyph）的处理思路是：调用 `ttf_parser` 解析字体文件，拿到字形的贝塞尔轮廓，再用 `SvgPathBuilder` 把轮廓翻译成 SVG 的 `d` 字符串，最后通过 `<defs><symbol>` + `<use>` 复用（复用机制见讲义 u4-l2）。

`ttf_parser` 提供了一个 `OutlineBuilder` trait：它会在遍历字形轮廓时，**回调** `move_to` / `line_to` / `quad_to` / `curve_to` / `close` 这几个方法，坐标是**字体的设计单位**（font units，通常是 1000 或 2048 per em）。`SvgPathBuilder` 恰好也有同名方法！于是 `path.rs` 在一个私有 `outline` 子模块里，直接为 `SvgPathBuilder` 实现了 `ttf_parser::OutlineBuilder`，把回调桥接成自己的绘图命令。

但字体设计单位 ≠ SVG 里的点（pt）。一个字形若以原始设计单位写出，尺寸会差好几个数量级。这里就引出了 `with_scale` 的关键作用。

#### 4.4.2 核心流程

字形的缩放比例：

\[ \text{scale} = \frac{\text{text\_size (pt)}}{\text{units\_per\_em}} \]

例如字号 12pt、字体 `units_per_em = 1000`，则 `scale = 0.012`。`with_scale(scale)` 把这个比例塞进 builder，于是每条命令经 `map` 时，相对位移会被乘以 `scale`，等价于「在写入时就预先把字形从设计单位缩放到目标字号」。

这样做的好处（来自 `text.rs` 的注释）：**预先缩放后，字形的描边和填充图案就不必再考虑字号缩放了**。也就是说，去重缓存里的字形路径已经是「目标尺寸」，可以直接用 `<use>` 放置，描边宽度等也按 1:1 处理。

桥接流程：

```text
text.font.ttf().outline_glyph(glyph_id, &mut builder)
                 ↓ ttf_parser 回调
OutlineBuilder::move_to(x, y)  →  point(x,y)  →  SvgPathBuilder::move_to
OutlineBuilder::line_to(x, y)  →  point(x,y)  →  SvgPathBuilder::line_to
OutlineBuilder::quad_to(...)   →  SvgPathBuilder::quad_to
OutlineBuilder::curve_to(...)  →  SvgPathBuilder::curve_to
OutlineBuilder::close          →  SvgPathBuilder::close
```

`point(x, y)` 是个小帮手，把 `f32` 的字体坐标包成 `Point::new(Abs::pt(x as f64), Abs::pt(y as f64))`。

#### 4.4.3 源码精读

桥接实现，整个 `outline` 子模块的核心——五个方法各自把 `ttf_parser` 的 `f32` 坐标转成 `Point`，再委托给 `SvgPathBuilder` 同名方法：

[src/path.rs:170-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L170-L196) — 为 `SvgPathBuilder` 实现 `ttf_parser::OutlineBuilder`，把字体回调转成路径命令。注意它放在私有 `mod outline` 内，是为了避免与 `SvgPathBuilder` 自身同名方法产生命名混淆。

坐标转换帮手：

[src/path.rs:198-201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L198-L201) — `point(x, y)`：把字体的 `f32` 坐标包成 `Point`（`Abs::pt`）。

上游调用点，展示 `with_scale` 如何参与字形去重：

[src/text.rs:64-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L64-L73) — `render_glyph` 中：算出 `scale = text.size / units_per_em`，用 `with_scale(scale)` 建 builder，调用 `outline_glyph` 让 ttf_parser 回调填充路径，最后 `finsish()` 取出字符串，包成 `RenderedGlyph::Path`。注意去重键 `(&font, glyph_id, scale)` 含 `scale`——不同字号的同名字形会生成不同的预缩放路径。

#### 4.4.4 代码实践

**实践目标**：理解 `with_scale` 为什么对字形轮廓提取至关重要，并对比「预缩放」与「使用时缩放」两种策略的差异（呼应讲义 u4-l1 的去重取舍）。

**操作步骤**：

1. 打开 [src/text.rs:64-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L64-L73)，找到 `scale = Ratio::new(text.size.to_pt() / text.font.units_per_em())`。
2. 假设字体 `units_per_em = 1000`，字形「A」的某条竖线在设计单位下从 `(100, 0)` 到 `(100, 700)`。
3. 分别推演：用 `with_scale(Ratio::new(0.012))`（12pt）和 `with_scale(Ratio::new(0.024))`（24pt）时，`line_to((100,700))` 写入的位移分别是什么。

**需要观察的现象**：`map` 会把位移乘以 `scale`，所以同一字形在不同字号下，写入的位移数值不同——这正是去重键要包含 `scale` 的原因。

**预期结果**：

| 构造 | `line_to((100,700))` 的位移（假设上一终点为 `(100,0)`） | 写入 |
| --- | --- | --- |
| `with_scale(0.012)` | `0.012 * ((100,700)-(100,0)) = (0, 8.4)` | `v 8.4` |
| `with_scale(0.024)` | `0.024 * (0,700) = (0, 16.8)` | `v 16.8` |

**结论**：`with_scale` 让字形轮廓在**生成 `d` 字符串的同时**就完成了字号缩放。这样存进 `<defs><symbol>` 的字形已经是目标尺寸，渲染时 `<use>` 不必再带缩放变换，描边/填充的相对参照也统一。代价是同一字形在每个字号下各占一份缓存（去重键含 `scale`）。这与「图像字形在使用处才缩放」的策略相反——那是空间（缓存条数）与体积（每条 `<use>` 是否带 transform）之间的取舍，详见讲义 u4-l1。精确数字「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `impl ttf_parser::OutlineBuilder for SvgPathBuilder` 要放在私有 `mod outline` 里，而不是和主 `impl` 并列？

**参考答案**：因为这个 impl 里的 `move_to` / `line_to` / `curve_to` / `close` 与 `SvgPathBuilder` 自身已有的同名方法签名不同（trait 版本接收 `f32` 坐标、无返回值，自身版本接收 `Point`）。若并列在同一个 `impl` 块或同一模块顶层，会引起方法解析的混淆。放进独立的私有子模块，既隔离了命名，也清晰地表达「这部分是专门为 ttf_parser 服务」的意图。

**练习 2**：如果改用 `with_translate` 而非 `with_scale` 来提取字形轮廓，会发生什么？

**参考答案**：`with_translate` 的 `scale` 固定为 1，`map` 不会缩放位移。那么写出的 `d` 字符串里的坐标仍是字体设计单位（几百到上千的数量级），尺寸远大于目标字号，直接 `<use>` 会得到一个巨大的字形；并且每个字号的同名字形会得到**相同**的路径字符串，去重键里 `scale` 也失去区分意义，导致不同字号共享同一份错误尺寸的轮廓。所以 `with_scale` 不可替代。

---

## 5. 综合实践

**任务**：本讲的所有机制都围绕「输入绝对、输出相对」展开。请你把这条主线串起来，完成一次「人造字形」的路径推演。

设想一个极简字形「┐」（一个直角），在设计单位（`units_per_em = 1000`）下的轮廓为：

```text
move_to (200, 0)
line_to (200, 500)
line_to (800, 500)
close
```

要求：

1. 用 `SvgPathBuilder::with_scale(Ratio::new(0.02))`（即 20pt 字号）构造 builder，逐命令推演 `map` 的结果与写入的字符串。注意 `with_scale` 的初始 `M` 是 `M 0 0`，`last_point` 初值为 `(0,0)`。
2. 指出哪些命令会触发 `line_to` 的 `h`/`v` 轴向简写。
3. 解释：若把这个结果存进 `glyphs` 去重缓存（键含 `scale`），再把同一字形以 30pt 再渲染一次，缓存会不会命中？为什么？

**参考答案要点**：

1. 推演（位移 = `0.02 * (目标 - last_point)`）：
   - 构造：`M 0 0`，last_point=(0,0)。
   - `move_to((200,0))`：位移 `0.02*((200,0)-(0,0))=(4,0)`，非零 → 写 `m 4 0`；last_point 更新为绝对 (200,0)（注意：写入的是缩放后的相对位移，但 `defer` 存回的 `last_point` 是**未缩放**的绝对 `pos`，因为 `map` 只用于格式化、`last_point` 始终保存原始绝对坐标）。
   - `line_to((200,500))`：位移 `0.02*((200,500)-(200,0))=(0,10)`，x=0 → `v 10`。
   - `line_to((800,500))`：位移 `0.02*((800,500)-(200,500))=(12,0)`，y=0 → `h 12`。
   - `close()`：`Z `。
   - 结果约 `M 0 0 m 4 0 v 10 h 12 Z `。
2. 两条 `line_to` 都触发了轴向简写：第一条退化 `v`（垂直），第二条退化 `h`（水平）。`move_to` 写的是相对 `m`，不是轴向简写。
3. **不会命中**。去重键是 `(&font, glyph_id, scale)`，20pt 对应 `scale=0.02`，30pt 对应 `scale=0.03`，键不同，所以会重新生成一份预缩放路径。这正是「预缩放」策略的代价：每个字号各占一份缓存，换取 `<use>` 处无需额外缩放变换。

> 关于第 1 步的一个细节：`defer` 存回 `last_point` 的是传入的**绝对** `pos`（未缩放），而写入字符串的是 `map` 算出的**缩放后相对位移**。这一点容易看走眼——`map` 既「减 last_point」又「乘 scale」只作用于输出文本，状态里的 `last_point` 始终是原始绝对坐标。可对照 [src/path.rs:102-114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L102-L114) 与 [src/path.rs:61-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs#L61-L63) 反复确认。

## 6. 本讲小结

- `SvgPathBuilder` 是一个「输入绝对、输出相对」的路径字符串拼装器，核心转换 `map(p) = scale × (p − last_point)`，目的是压缩 SVG 文件体积。
- 三种构造方式分工：`with_translate` 给普通形状（绝对 `M` 起手、scale=1）；`with_scale` 给字形轮廓（`M 0 0` 起手、scale=字号/units_per_em）；`empty` 给圆锥渐变等需要完全自控的场景。
- `defer` 工具实现了「先用旧 `last_point` 算位移、drop 时再更新为新值」的精妙顺序，是相对坐标正确性的关键。
- `line_to` 在位移仅落单轴时退化为 `h`/`v`，这是路径体积优化中最直观的一处；`move_to` 在位移为 0 时整条省略。
- `curve_to` / `quad_to` 把贝塞尔控制点也转成相对；`close` 写 `Z` 并把 `last_point` 复位到子路径起点 `last_close_point`。
- 私有 `outline` 子模块为 `SvgPathBuilder` 实现 `ttf_parser::OutlineBuilder`，把字体字形回调桥接成路径命令；`with_scale` 让字形在生成字符串时就预缩放到目标字号，使后续 `<use>` 复用无需额外变换（代价是去重键须含 `scale`）。

## 7. 下一步学习建议

- **紧接着读 `shape.rs`**（讲义 u3-l2）。它通过 `convert_geometry_to_path` / `convert_curve` 调用本讲的 `with_translate`，把 `Geometry::Rect` / `Geometry::Line` / `Curve` 转成路径，并处理填充、描边、`RelativeTo` 变换——你会看到 `SvgPathBuilder` 产出的 `d` 字符串如何被挂到 `<path>` 上。
- **随后读 `text.rs`**（讲义 u4-l1、u4-l2）。那里会用 `with_scale` + `outline_glyph` 提取字形，并通过 `RenderedGlyph::Path` 把本讲产出的字符串送进 `<defs><symbol>` 去重复用。
- **进阶可读 `paint.rs`**（讲义 u5）。你会看到 `empty()` 构造的 builder 如何在圆锥渐变里逐段拼扇形（`move_to` + `line_to` + `arc` + `close`），把本讲的 `arc` 命令真正用起来。
- 动手建议：在本讲「人造字形」推演的基础上，尝试自己写一个最小 Rust 测试，`new` 一个 `SvgPathBuilder`，调用几个命令后 `finsish()` 打印字符串，与你的手推结果对照（typst-svg 目前没有为 `path.rs` 写单元测试，你可以补一个来加深理解）。
