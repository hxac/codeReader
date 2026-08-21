# 几何与颜色基础类型

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分 GPUI 中的三种长度单位 `Pixels`、`ScaledPixels`、`DevicePixels`（逻辑像素与物理像素），并能借助缩放系数（scale factor）在它们之间换算。
2. 熟练使用 `Point`、`Size`、`Bounds` 三个几何容器完成基础几何运算：求中心、求并集/交集、命中测试（contains）、按锚点定位，以及「把一个矩形居中到另一个矩形里」这类弹窗布局计算。
3. 掌握颜色的两种表示 `Rgba` 与 `Hsla`：会用 `rgb()`/`rgba()`/`hsla()` 构造颜色，理解 `alpha` 与 `opacity` 的区别、`blend` 混合公式，以及 RGB ↔ HSL 的双向转换。
4. 了解 `ColorSpace`（sRGB / Oklab）如何影响渐变插值，认识 `Background`、`linear_gradient` 与 `colors.rs` 中框架自带的默认调色板。

这一讲不涉及任何复杂机制——没有实体、没有渲染管线——但这里的类型会出现在后续**每一讲**的源码里：布局申报尺寸用 `Pixels`，绘制传给 GPU 用 `ScaledPixels`，命中测试用 `Bounds`，样式上色用 `Hsla`。把这几个基础类型吃透，后面读任何 gpui 源码都会顺畅很多。

## 2. 前置知识

阅读本讲前，你需要具备（u1、u2、u3 前几讲已建立的概念会直接沿用）：

- **会跑 gpui 示例**：`cargo run -p gpui --example hello_world`（见 u1-l2）。
- **知道「视图 = 实现了 Render 的实体」**，`render()` 每帧返回一棵元素树（见 u3-l1）。
- **知道 div 的链式样式 API**（`div().p_4().bg(...)`，见 u3-l2）。

再用通俗语言补三个本讲专属的背景概念：

### 2.1 逻辑像素与物理像素

- **物理像素（device pixel）**：屏幕上真实存在的发光点。一块 1920×1080 的显示器就有 1920×1080 个物理像素。
- **逻辑像素（logical pixel / CSS pixel）**：程序员「以为」自己在用的像素。在高密度屏（如 MacBook Retina、手机屏）上，一个逻辑像素对应 2×2 甚至 3×3 个物理像素，比值就是**缩放系数（scale factor / DPR，device pixel ratio）**。

为什么要分家？如果直接用物理像素写 UI，同一个按钮在 Retina 屏上会肉眼可见地变小。GPUI 的选择是：**布局和样式全部用逻辑像素 `Pixels`，只有最终交给 GPU 绘制时才乘上缩放系数**，变成 `ScaledPixels` 或 `DevicePixels`。

### 2.2 newtype 模式

`Pixels` 的定义是 `pub struct Pixels(pub(crate) f32)`——一个只包了一个 `f32` 的「新类型」。这是 Rust 里常见的手法：类型上它不是 `f32`，所以你不能把「宽度」和「字号」两个含义不同的数字不小心相加，也不能把 `Pixels` 当作 `ScaledPixels` 乱用；运行时它又和 `f32` 一样轻，零开销。因为内部字段是 `pub(crate)`（crate 外不可见），我们在示例里构造 `Pixels` 要用 `px(10.)` 函数或 `Pixels::from(10.)`，而不是 `Pixels(10.)`。

### 2.3 RGB 与 HSL 颜色模型

- **RGB**：用红、绿、蓝三个通道的强度拼出颜色，机器最友好（屏幕本身就是 RGB 发光），但人不好直觉思考——「淡一点的天蓝」在 RGB 里该怎么改？
- **HSL**：用色相（Hue，绕色环的角度）、饱和度（Saturation，灰到彩）、亮度（Lightness，黑到白）三个维度描述颜色，人最直觉。「同一颜色变淡」= 色相不动、亮度上调。GPUI 内部样式系统（`Style`、`Background`）统一使用 `Hsla`，而序列化（写 JSON、转十六进制）使用 `Rgba`。

## 3. 本讲源码地图

| 文件 | 行数规模 | 作用 |
| --- | --- | --- |
| [src/geometry.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs) | 约 4000 行 | 全部几何与长度类型：`Point`/`Size`/`Bounds`/`Edges`/`Corners`、`Pixels`/`ScaledPixels`/`DevicePixels`/`Rems`/`Length` 家族、`px()`/`rems()` 等构造函数 |
| [src/color.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs) | 约 1070 行 | `Rgba`/`Hsla` 两种颜色、RGB↔HSL 转换、透明度与混合、`ColorSpace`、`Background`（纯色/渐变/图案） |
| [src/colors.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/colors.rs) | 约 122 行 | 框架默认调色板 `Colors`（明/暗两套），以 `GlobalColors` 全局量形式提供 |
| [examples/gradient.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/gradient.rs) | 273 行 | 渐变示例：sRGB/Oklab 一键切换、`linear_gradient`、canvas 中手工计算 `Bounds` 画渐变多边形 |
| [src/window.rs](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L2607-L2612) | — | 只看一处：`Window::scale_factor()`，缩放系数的运行时来源 |
| [src/platform.rs](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/platform.rs#L1825-L1831) | — | 只看一处：`WindowOptions::window_bounds`，综合实践中会用到 |

回顾 u1-l3 的结论：这两个模块都经 [gpui.rs:L99](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/gpui.rs#L99) 的 `pub use color::*;` 与 [gpui.rs:L106](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/gpui.rs#L106) 的 `pub use geometry::*;` 扁平化导出，所以你 `use gpui::*` 之后可以直接写 `px(10.)`、`Bounds::new(...)`。

## 4. 核心概念与源码讲解

### 4.1 长度单位：Pixels、ScaledPixels 与 DevicePixels

#### 4.1.1 概念说明

GPUI 用三个 newtype 表达「像素」的三个阶段：

| 类型 | 内部表示 | 语义 | 出现场景 |
| --- | --- | --- | --- |
| `Pixels` | `f32` | 逻辑像素，可带小数 | 布局申报、样式值、事件坐标 |
| `ScaledPixels` | `f32` | 逻辑像素 × 缩放系数，仍可带小数 | 绘制阶段传给渲染器的中间单位 |
| `DevicePixels` | `i32` | 物理像素，整数 | 与平台/缓冲区打交道的最终单位 |

配套还有一个 `Rems`（相对字号单位，1rem = 根元素字号），以及样式层的 `Length` 枚举家族（`px`/`rem`/`%`/`auto` 四选一），它们最终都会被折算成 `Pixels` 参与布局。本模块聚焦前三种像素单位的换算关系。

`px()` 是最常用的构造函数：`px(10.)` 产生 10 个逻辑像素。之所以需要它，是因为 `Pixels` 的字段是 `pub(crate)`——框架作者有意让「产生像素」这个动作带上单位名，避免裸数字满天飞。

#### 4.1.2 核心流程

一个长度值在 GPUI 里的完整旅程：

```text
样式/布局阶段          绘制阶段                    平台阶段
┌──────────┐   乘 scale_factor    ┌───────────────┐   ceil/round    ┌───────────────┐
│ Pixels   │ ──────────────────► │ ScaledPixels  │ ──────────────► │ DevicePixels  │
│ (f32)    │   Pixels::scale()   │ (f32)         │  From impl      │ (i32)         │
└──────────┘                     └───────────────┘                 └───────────────┘
      ▲                                                                    │
      └──────────────── 除 scale_factor（to_pixels）◄──────────────────────┘
```

换算公式（\(s\) 为缩放系数）：

\[ d = \mathrm{round}(p \cdot s) \qquad p = \frac{d}{s} \]

注意两点：

1. **方向不对称**：逻辑→物理用「乘完四舍五入」，物理→逻辑用「直接除」，不做逆映射保证。
2. `ScaledPixels → DevicePixels` 用的是 **ceil（向上取整）** 而不是 round，保证绘制区域只多不少，避免高分屏上出现发丝缝隙。

#### 4.1.3 源码精读

先看 `Pixels` 的定义与 `px()` 构造函数：

- [geometry.rs:L2660-L2677](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L2660-L2677) 定义 `pub struct Pixels(pub(crate) f32)`，并派生 `Add`/`Sub`/`Div` 等算术运算——两个 `Pixels` 可以直接相加，`Pixels / Pixels` 得到 `f32`（消除单位）。
- [geometry.rs:L3727-L3738](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L3727-L3738) 是 `px()` 函数与旁边的 `rems()`：`pub const fn px(pixels: f32) -> Pixels`。同一段里还有 [relative()](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L3693-L3707)（构造百分比长度）与 [auto()](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L3740-L3753)。
- [geometry.rs:L2781-L2792](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L2781-L2792) 提供 `Pixels::ZERO` 常量与 `as_f32()`；[geometry.rs:L2794-L2819](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L2794-L2819) 提供 `floor/round/ceil`。

接着是核心的缩放方法：

- [geometry.rs:L2821-L2832](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L2821-L2832) `Pixels::scale(&self, factor: f32) -> ScaledPixels`：唯一入口，把逻辑像素乘上缩放系数。文档注释明确说了这是为高 DPI / Retina 屏准备的。
- [geometry.rs:L2957-L2982](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L2957-L2982) 定义 `pub struct DevicePixels(pub i32)`，文档强调它「总是对应屏幕上真实的像素」。它的 [Debug 输出](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L3012-L3016) 是 `"10 px (device)"`，打印日志时能和 `Pixels` 的 `"10px"` 区分开。
- [geometry.rs:L3131-L3135](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L3131-L3135) `impl From<ScaledPixels> for DevicePixels`：注意用的是 `ceil()`——向上取整，宁可多画一像素也不留缝。

再看 `Size` 层面的双向换算（`Bounds` 层面还有一对，见 4.2）：

- [geometry.rs:L1648-L1656](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1648-L1656) `Size<Pixels>::to_device_pixels(scale_factor)`：宽度高度各自 `(值 × 系数).round() as i32`。
- [geometry.rs:L1638-L1646](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1638-L1646) `Size<DevicePixels>::to_pixels(scale_factor)`：反向除回逻辑像素。这两段代码是「公式 \[ d = round(p \cdot s) \]」的直接落点。

缩放系数从哪来？窗口知道：

- [window.rs:L2607-L2612](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L2607-L2612) `Window::scale_factor() -> f32`，文档注释举例：Retina 屏返回 `2.0`，表示一个逻辑像素实际渲染为 2×2 物理像素。测试环境还可用 [set_scale_factor](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L2614-L2619) 覆写（u7-l4 讲测试时会再遇到它）。

顺带认识两个「样式层」单位，它们最终都会落到 `Pixels`：

- [geometry.rs:L3227-L3251](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L3227-L3251) `Rems`：`to_pixels(rem_size)` 把 rem 乘上当前字号换算成像素——这正是 u3-l2 里 `p_4`、`gap_3` 这些「档位方法」背后的单位。
- [geometry.rs:L3454-L3465](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L3454-L3465) `DefiniteLength`：`Absolute`（px/rem）或 `Fraction`（父容器百分比）；再包一层 [Length](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L3609-L3616)（多一个 `Auto`）就是 `div().w(...)` 能接收的全部形态。本讲不展开，u4-l2 讲 Taffy 布局时会用到。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「同一个逻辑尺寸，在不同缩放系数下变成不同的物理尺寸」。

**操作步骤**（示例代码，基于 u1-l2 的 hello_world 骨架改写，可直接替换 `examples/hello_world.rs` 的 render 部分）：

```rust
// 示例代码：观察 scale_factor 与单位换算
use gpui::{div, px, render, size, App, Bounds, Context, Window, WindowOptions};

struct UnitProbe;

impl Render for UnitProbe {
    fn render(&mut self, window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        let factor = window.scale_factor();
        let logical = size(px(300.), px(200.));                 // 300x200 逻辑像素
        let device = logical.to_device_pixels(factor);          // 换算成物理像素
        println!(
            "scale_factor = {factor}, logical = {logical:?}, device = {device:?}"
        );

        div()
            .size_full()
            .flex()
            .items_center()
            .justify_center()
            .child(format!("scale factor: {factor}"))
    }
}
```

运行：`cargo run -p gpui --example hello_world`（改动了 hello_world.rs 的话）。窗口内容与终端会同时输出结果。

**需要观察的现象**：

1. 普通显示器上 `scale_factor` 打印 `1`，`device` 尺寸等于 `300x200`；
2. 如果系统开启了 HiDPI 缩放（如 200%），`scale_factor` 打印 `2`，`device` 变成 `600x400`；
3. `Bounds<Pixels>` 层面还有等价的 [`bounds.to_device_pixels(factor)`](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1702-L1711)，行为一致。

**预期结果**：终端输出形如 `scale_factor = 1, logical = Size { 300px × 200px }, device = Size { 600 px (device) × 400 px (device) }`（具体数值取决于你的显示器缩放设置，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Pixels` 的字段是 `pub(crate) f32`，而 `DevicePixels` 是 `pub i32`？这样设计各有什么好处？

**参考答案**：`Pixels` 隐藏字段，强制外部通过 `px()` / `From` 构造、通过方法访问，调用点上永远带着单位语义，也方便未来在不改 API 的前提下调整内部表示；`DevicePixels` 公开 `i32` 字段，因为它是对接平台层的「最终定稿」单位（帧缓冲、纹理尺寸都是整数），平台代码需要频繁直接读写原始值（例如 [`DevicePixels::to_bytes`](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L2984-L3010) 直接用 `self.0` 算缓冲区字节数），公开字段省掉一层样板。

**练习 2**：`ScaledPixels → DevicePixels` 为什么用 `ceil` 而不是 `round`？

**参考答案**：绘制矩形如果因为取整变小了，相邻图元之间可能出现一条没被覆盖的缝隙（半透明缝隙在高分屏上是经典的「发丝线」bug）；向上取整只会让图形多覆盖不到一个像素的边缘，视觉上无害。所以宁可多画、不可少画。

**练习 3**：一个窗口逻辑尺寸 `size(px(1280.), px(800.))`，缩放系数 `1.25`，物理尺寸是多少？

**参考答案**：宽 `round(1280 × 1.25) = 1600`，高 `round(800 × 1.25) = 1000`，即 `size(DevicePixels(1600), DevicePixels(1000))`。经 `to_device_pixels(1.25)` 计算可得。

### 4.2 Point、Size 与 Bounds：几何运算三件套

#### 4.2.1 概念说明

三个泛型容器构成 GPUI 的几何语言：

- `Point<T>`：二维位置，字段 `x`/`y`；
- `Size<T>`：二维尺寸，字段 `width`/`height`；
- `Bounds<T>`：轴对齐矩形，= `origin: Point<T>` + `size: Size<T>`，坐标系原点在**左上角**，y 轴向下。

它们都是「容器」而非具体单位：`Bounds<Pixels>`（逻辑像素矩形）、`Bounds<DevicePixels>`（物理像素矩形）、`Bounds<i32>`（网格坐标，如 uniform_list 的行列号）都存在。配套的还有 `Edges<T>`（四条边，表示 padding/margin）和 `Corners<T>`（四个角，表示圆角），本讲只作认识。

#### 4.2.2 核心流程

一次典型的几何运算流水线（也是 u4 元素绘制、u5 命中测试的骨架）：

```text
1. 布局引擎产出元素 Bounds<Pixels>（相对父元素）
2. 逐层累加 origin，得到窗口坐标系的 Bounds        （Bounds + Point）
3. needs_display 时 bounds.scale(factor) → ScaledPixels，交给 GPU
4. 鼠标事件到达 → bounds.contains(point) 命中测试
5. 命中后 bounds.localize(point) 把坐标转回元素局部系
```

常用的「矩形代数」操作一览：

| 操作 | 方法 | 一句话说明 |
| --- | --- | --- |
| 构造 | `Bounds::new(origin, size)` / `from_corners(tl, br)` / `from_anchor_and_size` | 三种姿势 |
| 居中 | `Bounds::centered_at(center, size)` / `center()` | 中心点减半宽半高 |
| 边与角 | `top()/bottom()/left()/right()`、`top_right()/bottom_left()` 等 | 全部现成 |
| 集合运算 | `intersect()` / `union()` / `intersects()` | 交、并、是否相交 |
| 命中 | `contains(&point)` / `is_contained_within(&other)` | 左闭右开区间 |
| 膨胀收缩 | `dilate(n)` / `inset(n)` | 四周各扩/缩 n |
| 局部化 | `localize(&point)` | 窗口坐标 → 元素内坐标 |

#### 4.2.3 源码精读

先看三个结构体本体：

- [geometry.rs:L66-L90](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L66-L90) `Point<T>`，派生了 `Add`/`Sub`/`Neg` 等——两个点可以直接相减得到位移向量；配套小写构造函数 [`point(x, y)`](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L92-L113)。
- [geometry.rs:L391-L401](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L391-L401) `Size<T>`；构造函数 [`size(width, height)`](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L410-L430)。注意 [From<Point<T>> for Size<T>](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L641-L648) 与反向的 [From<Size<T>> for Point<T>](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1275-L1282)：点可以当「从原点出发的尺寸」用。
- [geometry.rs:L703-L728](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L703-L728) `Bounds<T>` 只有 `origin` 和 `size` 两个字段——没有 right/bottom 字段，右下角永远是**算出来的**（[geometry.rs:L1365-L1370](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1365-L1370) `bottom_right()`）。

居中是一切弹窗布局的基础，看它怎么算：

- [geometry.rs:L874-L886](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L874-L886) `Bounds::centered_at(center, size)`：`origin = center - size.half()`。`half()` 来自 [`Half` trait](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L3822-L3834)，为 `i32/f32/Pixels/ScaledPixels/DevicePixels/Rems` 各实现一次（[geometry.rs:L3836-L3870](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L3836-L3870)）——整数除以 2 会截断，所以单独抽象成 trait 而不是通用的 `* 0.5`。
- 与之对应的反向操作 [geometry.rs:L998-L1003](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L998-L1003) `Bounds::center()`：`origin + size.half()`。
- 框架自己开窗口时也用这一套：[geometry.rs:L738-L751](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L738-L751) `Bounds::centered(display_id, size, cx)` 查显示器、取其中心，再委托 `centered_at`。

命中测试的边界规则值得细看：

- [geometry.rs:L1432-L1470](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1432-L1470) `Bounds::contains(&point)`：四个比较是 `>=` 起点与 `<` 终点——**左闭右开**。这意味着相邻两个矩形的公共边界上的点只属于右边/下边那个，鼠标命中不会「同时命中两个」。
- [geometry.rs:L1598-L1607](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1598-L1607) `localize(&point)`：先 `contains` 后 `relative_to(origin)`（[relative_to 定义](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L223-L234)），窗口坐标减去元素原点就是元素内坐标，不在范围内返回 `None`。

集合运算与构造：

- [geometry.rs:L787-L829](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L787-L829) `from_corners(top_left, bottom_right)`：由两角算 origin + size，是 `intersect`/`union` 的底层积木。
- [geometry.rs:L1104-L1147](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1104-L1147) `intersect()`：左上取 max、右下取 min（再用 max 钳住，保证不相交时得到空矩形而不是负尺寸）。
- [geometry.rs:L1149-L1187](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1149-L1187) `union()`：左上取 min、右下取 max。
- [geometry.rs:L925-L971](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L925-L971) `intersects()`：严格的四向开区间比较，仅共享一条边的两个矩形**不算**相交。
- [geometry.rs:L1064-L1070](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1064-L1070) `dilate(n)`：origin 减 n、size 加 2n，四周外扩；[inset](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1096-L1102) 就是 `dilate(-n)`。

最后是 `Bounds` 的单位换算对（与 4.1 的 `Size` 版对应）：

- [geometry.rs:L1702-L1711](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1702-L1711) `Bounds<Pixels>::to_device_pixels(factor)`：origin 逐坐标 round，size 走 `Size::to_device_pixels`。
- [geometry.rs:L1714-L1725](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1714-L1725) `Bounds<DevicePixels>::to_pixels(scale_factor)`：反向整除。
- [geometry.rs:L1658-L1700](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1658-L1700) `Bounds<Pixels>::scale(factor) -> Bounds<ScaledPixels>`：绘制管线的入口版本（不取整，保留子像素精度给抗锯齿）。

#### 4.2.4 代码实践

**实践目标**：用 `Bounds` 的 API 手工推导一个「300×200 弹窗居中在 800×600 父容器中」的结果，并用框架的 `centered_at` 验证自己没算错。

**操作步骤**：在 4.1.4 的示例里追加（示例代码）：

```rust
// 示例代码：手工推导 vs 框架 API
use gpui::{bounds, point, size, px, Bounds, DevicePixels};

fn centered_popup_bounds(
    window_size: Bounds<Pixels>,
    popup: Size<Pixels>,
    scale_factor: f32,
) -> Bounds<DevicePixels> {
    // 第一步：在逻辑像素坐标系里居中
    let logical = Bounds::centered_at(window_size.center(), popup);
    // 第二步：整体换算到物理像素
    logical.to_device_pixels(scale_factor)
}

// 验证（可放在 render 里打印）：
// let parent = bounds(point(px(0.), px(0.)), size(px(800.), px(600.)));
// let got = centered_popup_bounds(parent, size(px(300.), px(200.)), 2.0);
// assert_eq!(got.origin, point(DevicePixels(500), DevicePixels(400)));
// assert_eq!(got.size, size(DevicePixels(600), DevicePixels(400)));
```

**需要观察的现象**：`centered_at` 算出的 origin 是 `(250px, 200px)`（= (800−300)/2, (600−200)/2）；乘系数 2 后物理像素为 `(500, 400)`，尺寸 `600×400`。

**预期结果**：断言通过。把 `scale_factor` 换成 `1.0` 再跑一次，物理像素应与逻辑像素一致。若你的环境无法运行，可用纯逻辑推演验证：\[ o = c - \frac{s}{2} = (400, 300) - (150, 100) = (250, 200) \]（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`bounds_a.intersects(&bounds_b)` 与 `bounds_a.contains(&point)` 对边界的处理一致吗？

**参考答案**：一致地采用「起点包含、终点排除」。`contains` 用 `>=` 下界与 `<` 上界（[geometry.rs:L1465-L1470](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1465-L1470)）；`intersects` 用 `<` 对方右下、`>` 对方左上（[geometry.rs:L962-L970](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L962-L970)），所以仅共享一条边的两矩形返回 `false`。这样保证几何划分无歧义、不重不漏。

**练习 2**：`Bounds` 为什么不直接存 `top/left/bottom/right` 四个值？

**参考答案**：origin+size 是布局的天然语言（布局引擎算出的就是「位置 + 占多大」），四边表示需要在每次改尺寸时同步维护两个冗余字段；而四边的需求（`right()`、`bottom()`、`bottom_right()`）全是 origin+size 的一次加法（[geometry.rs:L1284-L1395](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1284-L1395)），按需计算即可。真正需要「四边各自独立」的场景（padding/margin、圆角）GPUI 另提供了 `Edges<T>`（[geometry.rs:L1727-L1759](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1727-L1759)）和 `Corners<T>`。

**练习 3**：写出一行代码：求同时覆盖 `a` 和 `b` 两个矩形的最小矩形。

**参考答案**：`a.union(&b)`。实现见 [geometry.rs:L1182-L1186](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L1182-L1186)：左上角取两 origin 的 `min`，右下角取两 `bottom_right()` 的 `max`，再 `from_corners` 组装。

### 4.3 Rgba 与 Hsla：颜色的两种写法

#### 4.3.1 概念说明

- `Rgba`：四个 `f32`（r/g/b/a，各 0..1），机器视角的颜色。常用于从设计稿/配置读入（`#336699` 这类十六进制），也是与平台 API 打交道的格式。
- `Hsla`：四个 `f32`（h/s/l/a，各 0..1），人视角的颜色。**注意 h 的取值是 0..1 而不是 0..360**，1.0 对应转满一圈回到起点；GPUI 的 `Display` 实现打印时会乘 360 显示成习惯的度数。

两者的关系是「同一颜色的两种坐标」：`From<Hsla> for Rgba` 与 `From<Rgba> for Hsla` 互转， gpui 的**样式系统内部统一存 `Hsla`**（`Style`、`Background` 的字段类型都是它），序列化到 JSON/十六进制时再转 `Rgba`。

预定义颜色：`gpui::red()/blue()/green()/yellow()/black()/white()` 返回 `Hsla`，色相分别为 0、2/3、1/3、1/6（绕色环等距的四个颜色）。`colors.rs` 里还有一套更完整的 `Colors` 调色板（文本、背景、边框等语义色）。

#### 4.3.2 核心流程

颜色的典型生命周期：

```text
输入侧                          框架内部                        输出侧
"#2e7d32" ──TryFrom──► Rgba ──From──► Hsla ──► Style/Background
rgb(0x2e7d32) ──────────►                             │
hsla(0.33, 0.6, 0.4, 1.) ────────────────────────────┤
                                                     ├──► opacity()/alpha() 调透明度
                                                     ├──► blend() 与底色混合
                                                     └──Serialize──► "#rrggbbaa" 字符串
```

透明度两个方法的区别（`Rgba` 与 `Hsla` 都有这对方法）：

- `alpha(a)`：**替换**透明度，`a` 会被 clamp 到 0..1；
- `opacity(factor)`：**乘上**现有透明度，适合「在主题色基础上打八折」的动态透明场景。

混合（blend）的语义：`self.blend(other)` 表示**把 other 画在 self 上面**，权重取 other 的 alpha：

\[ r_{out} = r_{self} \cdot (1 - a_{other}) + r_{other} \cdot a_{other} \]

#### 4.3.3 源码精读

先看构造侧：

- [color.rs:L13-L17](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L13-L17) `rgb(hex: u32)`：按大端字节序拆 `0xRRGGBB`，每字节除以 255 归一化，alpha 固定 1.0。
- [color.rs:L19-L23](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L19-L23) `rgba(hex: u32)`：同上但接受 `0xRRGGBBAA`，四个字节全用上。
- [color.rs:L36-L48](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L36-L48) `Rgba` 结构：四个公开 `f32` 字段。
- [color.rs:L331-L346](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L331-L346) `Hsla` 结构：注释明确写了 h/s/l/a 全是 0..1。
- [color.rs:L423-L431](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L423-L431) `hsla(h, s, l, a)` 构造函数：四个参数各自 `clamp(0., 1.)`——传 720° 这种越界色相会被悄悄钳回 1.0。

预定义颜色（色相刻度尺的关键参照）：

- [color.rs:L483-L521](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L483-L521) `red()`（h=0）、`green()`（h=1/3）、`blue()`（h=2/3）、`yellow()`（h=1/6）。连同 [black/white 系列](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L433-L481)，这些是示例代码里最常见的颜色来源。

透明度与混合：

- [color.rs:L73-L102](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L73-L102) `Rgba::alpha(a)`：替换 alpha 并 clamp；[color.rs:L104-L133](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L104-L133) `Rgba::opacity(factor)`：`a * factor.clamp(0., 1.)`。`Hsla` 的对应版本在 [color.rs:L637-L674](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L637-L674)。
- [color.rs:L56-L71](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L56-L71) `Rgba::blend`：公式本体。三个快路径：`other.a >= 1` 直接返回 other（不透明完全遮盖）；`other.a <= 0` 返回 self（完全透明等于没画）；中间态按 \( (1-a) : a \) 加权。**注意返回值的 alpha 用的是 `self.a`**——源码里写的是 `a: self.a`，也就是不重新合成不透明度。
- [color.rs:L569-L593](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L569-L593) `Hsla::blend`：HSL 空间没法直接线性混合（色相是环形量），所以实现是「转成 RGB → 用 `Rgba::blend` → 转回 HSL」。

双向转换（本讲最重要的两段算法）：

- [color.rs:L194-L222](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L194-L222) `From<Hsla> for Rgba`，标准 HSL→RGB 算法：

  \[ c = (1 - |2l - 1|) \cdot s, \quad x = c \cdot (1 - |(6h) \bmod 2 - 1|), \quad m = l - \frac{c}{2} \]

  然后按 \( \lfloor 6h \rfloor \) 落在 0..5 六个扇区之一，从 `(c, x, 0)` 的六个排列里选一个 RGB 骨架，最后各加 \( m \) 并 clamp 到 0..1。
- [color.rs:L677-L713](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L677-L713) `From<Rgba> for Hsla`，反向：\( l = (\max + \min) / 2 \)，饱和度按 \( l \) 是否过半分两段公式，色相由「最大通道是谁」决定，最后除以 6 归一化到 0..1。

序列化（u7 讲配置时会再遇到）：

- [color.rs:L224-L329](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L224-L329) `TryFrom<&str> for Rgba`：支持 `#rgb`/`#rgba`/`#rrggbb`/`#rrggbbaa` 四种十六进制写法（三位简写会复制数字，如 `#f09` = `#ff0099`）。
- [color.rs:L725-L741](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L725-L741) `Hsla` 的 `Serialize`/`Deserialize`：先转 `Rgba` 再按 `#rrggbbaa` 字符串走——JSON 里永远看不到 HSL。

框架默认调色板：

- [colors.rs:L8-L26](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/colors.rs#L8-L26) `Colors` 结构：`text`/`background`/`border`/`separator`/`container` 等八个**语义槽位**，类型全是 `Rgba`。
- [colors.rs:L44-L69](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/colors.rs#L44-L69) `dark()`/`light()` 两套具体值，都由 `rgb(0x......)` 构造；[colors.rs:L34-L41](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/colors.rs#L34-L41) `for_appearance(window)` 按系统明暗外观选择。
- [colors.rs:L77-L101](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/colors.rs#L77-L101) `GlobalColors(Arc<Colors>)` 实现了 u2-l4 学过的 `Global` trait，任何上下文里 `cx.default_colors()` 即可取用。

#### 4.3.4 代码实践

**实践目标**：用 `Hsla` 生成一组按色相均匀分布的色块（本讲规格任务的后半部分），直观建立「h ∈ 0..1 绕色环一圈」的手感。

**操作步骤**（示例代码，替换 hello_world.rs 的视图部分）：

```rust
// 示例代码：色相均匀分布的色带
use gpui::{div, hsla, px, App, Context, Render, Window, WindowOptions};

struct HueStrip;

impl Render for HueStrip {
    fn render(&mut self, _: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .size_full()
            .flex()
            .flex_col()
            .child(
                div()
                    .flex()
                    .flex_1()
                    .child(
                        (0..12).map(|i| {
                            div()
                                .flex_1()
                                .h(px(120.))
                                // 色相均匀取 12 等分；s=1、l=0.5 是最饱和的色环
                                .bg(hsla(i as f32 / 12.0, 1.0, 0.5, 1.0))
                        }),
                    ),
            )
            .child(
                div()
                    .flex()
                    .flex_1()
                    .child(
                        (0..12).map(|i| {
                            // 同一色相，把亮度从 0.9 递减到 0.1
                            div().flex_1().h(px(120.)).bg(hsla(
                                i as f32 / 12.0,
                                1.0,
                                0.9 - i as f32 * 0.8 / 11.0,
                                1.0,
                            ))
                        }),
                    ),
            )
    }
}
```

运行：`cargo run -p gpui --example hello_world`。

**需要观察的现象**：

1. 第一行 12 个色块依次是红→橙→黄→绿→青→蓝→紫→粉→回到红，相邻色块色相差恰为 1/12 圈；
2. 第 0 块与 `gpui::red()`（h=0）、第 2 块与 `gpui::yellow()`（h=1/6）、第 4 块与 `gpui::green()`（h=1/3）、第 8 块与 `gpui::blue()`（h=2/3）肉眼同色；
3. 第二行同一色相不同亮度，呈现同色系的明暗渐变——这就是 HSL 对人类直觉友好的体现。

**预期结果**：看到两条整齐的色带。子元素返回 `Iterator` 能直接作为 `.child()` 参数是 GPUI 的惯用法（元素树每帧重建，见 u3-l1）（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`rgba(0xff0000ff).opacity(0.5)` 与 `rgba(0xff0000cc).opacity(0.5)` 的 alpha 各是多少？

**参考答案**：前者 `1.0 × 0.5 = 0.5`；后者 `(0xcc/255) × 0.5 ≈ 0.8 × 0.5 = 0.4`。`opacity` 是**乘法**（[color.rs:L126-L133](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L126-L133)），而 `alpha(0.5)` 的话两者都会被**替换**成 0.5。

**练习 2**：`black().blend(hsla(0., 1., 0.5, 0.25))` 结果是什么颜色？

**参考答案**：半透明红叠在黑底上：\( r = 0 × 0.75 + 1 × 0.25 = 0.25 \)，g、b 为 0，即暗红色 `rgb` 约为 `0x400000`（alpha 保持 self.a = 1.0）。对照 [color.rs:L56-L71](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L56-L71) 的公式即可手算。

**练习 3**：为什么 gpui 的样式系统内部选 `Hsla` 而不是 `Rgba` 作为统一颜色类型？

**参考答案**：UI 编程的高频操作是「同色相变亮/变淡、按色相生成一组协调色、主题明暗切换」，这些在 HSL 空间是单分量加减，在 RGB 空间则要三个通道联动且结果不直观；而与外部交换（十六进制、JSON）才需要 RGB。所以内部 HSL、边界 RGB，转换集中在 [color.rs:L194-L222](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L194-L222) 与 [color.rs:L677-L713](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L677-L713) 两个 `From` 实现里。此外 Zed 的主题系统大量按色相/透明度派生颜色，HSL 表示让这类计算保持可读。

### 4.4 颜色空间与 Background：从纯色到渐变

#### 4.4.1 概念说明

`div().bg(...)` 能接收的不只是纯色，而是一个 `Background` 枚举语义的结构，共四种形态：

1. **Solid**：纯色（`solid_background(color)`，或直接 `From<Hsla>` 隐式转换）；
2. **LinearGradient**：双stop线性渐变（`linear_gradient(angle, from, to)`）；
3. **PatternSlash**：斜线填充图案；
4. **Checkerboard**：棋盘格图案。

**颜色空间（ColorSpace）**解决的问题：渐变是「在两个颜色之间插值」，而在哪个空间插值结果差异很大。sRGB 直接对 RGB 分量线性插值，中间会发灰发暗（经典的「蓝→红渐变中间脏一块」）；Oklab 是感知均匀空间，插值路径在人眼看来亮度平滑。CSS 的 `color-interpolation-method` 就是同一件事。GPUI 用 `ColorSpace::Srgb / Oklab` 二选一，默认 sRGB。

#### 4.4.2 核心流程

一个渐变背景的构造链：

```text
linear_gradient(角度, linear_color_stop(红, 0.), linear_color_stop(蓝, 1.))
    .color_space(ColorSpace::Oklab)   // 可选，默认 Srgb
        │
        ▼
Background { tag: LinearGradient, gradient_angle_or_pattern_height: 角度,
             colors: [LinearColorStop; 2], color_space }
        │  .bg(background)
        ▼
进入元素样式 ──绘制时──► GPU 上按 color_space 选择插值方式
```

`angle` 单位是度，0 等于从上往下（to top），顺时针增大，与 CSS `linear-gradient` 的角度约定一致（[color.rs:L858-L864](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L858-L864) 的文档注释）。每个 `LinearColorStop` 带 `percentage ∈ 0..1`，表示该颜色在渐变轴上的位置。

#### 4.4.3 源码精读

- [color.rs:L752-L765](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L752-L765) `ColorSpace` 枚举：`Srgb = 0`（默认）、`Oklab = 1`，注释直接链接到 MDN 与 CSS Color 4 规范。
- [color.rs:L776-L787](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L776-L787) `Background` 结构：注意 `#[repr(C)]` 手工布局——它要按固定的二进制形态跨进 GPU 侧，所以用 `tag` 判别字段 + `pad` 对齐，而不是普通 Rust 枚举。
- [color.rs:L858-L876](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L858-L876) `linear_gradient(angle, from, to)` 构造函数；[color.rs:L878-L898](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L878-L898) `LinearColorStop` 与 `linear_color_stop(color, percentage)`。
- [color.rs:L920-L926](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L920-L926) `Background::color_space(color_space)`：链式设置插值空间；旁边还有 [opacity](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L928-L937)（对 solid 与两个 stop 同时打折）与 [is_transparent](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L939-L948)。
- [color.rs:L850-L856](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L850-L856) `solid_background`：纯色形态；配合 [From<Hsla> for Background](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L950-L958)（[From<Rgba> 同理](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/color.rs#L960-L968)），这就是 `div().bg(gpui::red())` 能直接写的原因。

再精读 gradient 示例（本讲的参照工程）：

- [gradient.rs:L9-L19](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/gradient.rs#L9-L19) 视图只持有一个 `color_space` 字段，初始为 `ColorSpace::default()`（sRGB）。
- [gradient.rs:L51-L57](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/gradient.rs#L51-L57) 点击按钮在 `Srgb ↔ Oklab` 之间切换并 `cx.notify()`——这是 u2-l3 学过的「改状态 + 通知重绘」标准写法。
- [gradient.rs:L97-L102](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/gradient.rs#L97-L102) 典型的渐变用法：`linear_gradient(45., linear_color_stop(gpui::red(), 0.), linear_color_stop(gpui::blue(), 1.)).color_space(color_space)`——红蓝渐变正是观察两种插值差异最明显的组合。
- [gradient.rs:L209-L244](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/gradient.rs#L209-L244) canvas 绘制回调里手工用本讲的几何类型搭多边形：`bounds.size.width.half() - size.width.half()` 算水平居中偏移（与 4.2 的 `centered_at` 同一思想），再用 `square_bounds.bottom_left()`、`bottom_right()`、`top_right()` 取角点喂给 `PathBuilder`，最后 `window.paint_path(path, linear_gradient(...))` 把渐变刷进路径。这段是「几何 + 颜色」合流的最好示范，也为 u4-l4 的绘制原语预热。

#### 4.4.4 代码实践

**实践目标**：直观对比 sRGB 与 Oklab 两种插值空间在蓝→红渐变上的差异。

**操作步骤**：

1. 运行 `cargo run -p gpui --example gradient`；
2. 观察第三行第一格（45° 蓝→红 或按界面排布红↔蓝渐变条）；
3. 点击右上角黑色按钮（显示当前 ColorSpace 名），界面整体在 Srgb/Oklab 间切换。

**需要观察的现象**：

- sRGB 模式下，蓝→红渐变的中段明显发灰、偏暗；
- 切到 Oklab 后，中段过渡更接近「蓝紫红」的顺滑路径，明度基本不掉。

**预期结果**：能明显看出同一组 `linear_color_stop` 在两个颜色空间下的中间色差异；同时界面纯色块（红、蓝两块 Solid Color）不受影响——颜色空间只影响**插值**，不影响纯色（待本地验证，不同显示器色域下观感有差异）。

#### 4.4.5 小练习与答案

**练习 1**：`Background` 为什么要用 `tag + 固定字段 + pad` 的 `#[repr(C)]` 布局，而不是普通的 Rust 枚举？

**参考答案**：`Background` 最终要作为 uniform 数据传给 GPU 渲染器（quad 图元携带的背景参数），需要**稳定、对齐的二进制布局**，且四种形态共用同一块内存区域（solid 色与渐变的 angle 复用 `gradient_angle_or_pattern_height` 字段）。普通 Rust 枚举的内存布局不保证跨平台稳定，`repr(C)` + 显式 `pad` 才能确保 Rust 侧结构与 shader 侧读取逐字节对齐。

**练习 2**：`linear_gradient` 的 `angle` 为 `0.` 和 `90.` 分别是什么方向？

**参考答案**：`0.` 相当于 CSS 的 `to top`（渐变轴竖直，第一个 stop 在底部）；`90.` 顺时针转 90° 后相当于 `to right`（横向渐变，第一个 stop 在左侧）。见 [gradient.rs:L137-L166](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/gradient.rs#L137-L166) 中 0/90/180/360 四个方向渐变条的对照。

**练习 3**：想让一个渐变「两端各留 5% 的纯色，中间才过渡」，怎么写？

**参考答案**：把两个 stop 的 percentage 从 `0./1.` 改成 `0.05/0.95`：`linear_gradient(90., linear_color_stop(blue, 0.05), linear_color_stop(red, 0.95))`。percentage 就是 stop 在渐变轴上的位置，两端之外的区域各自保持端点纯色。示例 [gradient.rs:L170-L184](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/gradient.rs#L170-L184) 正是这样写的；把两个 stop 设成同一个 percentage（如都是 0.5）则得到一刀两断的硬边界（[gradient.rs:L191-L198](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/gradient.rs#L191-L198)）。

## 5. 综合实践

**任务**：把本讲四个模块串成一个小工具——「居中弹窗边界计算器 + 色相调色板」，并接入真实窗口验证。

**要求**：

1. 编写函数 `centered_popup_bounds(window: Bounds<Pixels>, popup: Size<Pixels>, scale_factor: f32) -> Bounds<DevicePixels>`：先用 `Bounds::centered_at` 在逻辑像素系里居中一个 300×200 的弹窗，再整体 `to_device_pixels`。
2. 写一个视图：整屏铺 12 个色相均分色块（4.3.4 的色带），中间用 `div().w(px(300.)).h(px(200.))` 画一个带圆角与半透明背景的「弹窗」，弹窗内用 `linear_gradient` 做标题栏渐变。
3. 验证：把第 1 步算出的物理 bounds 打印出来，与「逻辑结果 × 系数」的手工计算对照；再试着用 [platform.rs:L1825-L1831](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/platform.rs#L1825-L1831) 的 `WindowOptions { window_bounds: Some(WindowBounds::Windowed(bounds)), .. }`（[WindowBounds 枚举](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/platform.rs#L1965-L1974) 收 `Bounds<Pixels>`）真的开一个指定边界的窗口，对比框架的 [`Bounds::centered(None, size, cx)`](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/geometry.rs#L738-L751)（在显示器上居中）与你自己的居中算法的分工差异。

**参考骨架**（示例代码，核心部分）：

```rust
// 示例代码：综合实践骨架
use gpui::{
    hsla, linear_color_stop, linear_gradient, point, px, size, App, Bounds, Context, DevicePixels,
    Pixels, Render, Size, Window, WindowOptions, div,
};

fn centered_popup_bounds(
    window: Bounds<Pixels>,
    popup: Size<Pixels>,
    scale_factor: f32,
) -> Bounds<DevicePixels> {
    window.center().pipe(|c| Bounds::centered_at(c, popup)).to_device_pixels(scale_factor)
}
// 注：gpui 未提供 pipe，直接写两行更清晰：
// let logical = Bounds::centered_at(window.center(), popup);
// logical.to_device_pixels(scale_factor)
```

**预期结果**：程序窗口里呈现「色带背景 + 居中渐变弹窗」；控制台打印的物理边界满足 origin = (窗口尺寸 − 弹窗尺寸)/2 × 系数。完成后你就拥有了一个可复用的弹窗定位函数，u6-l3 讲 `anchored` 浮层定位时会看到框架如何把这类计算系统化。

## 6. 本讲小结

- GPUI 用 newtype 严格区分三种像素：`Pixels`（逻辑，f32，布局与样式的通用货币）、`ScaledPixels`（乘缩放系数后的绘制单位）、`DevicePixels`（物理，i32，平台边界单位）；换算入口是 `scale()` / `to_device_pixels()` / `to_pixels()`，系数来自 `window.scale_factor()`。
- 几何三件套 `Point`/`Size`/`Bounds` 都是泛型容器，`Bounds = origin + size`；居中用 `centered_at`（减半宽半高），命中测试 `contains` 是左闭右开，集合运算 `intersect/union/intersects` 齐备，`localize` 负责坐标系的局部化。
- 颜色内部统一 `Hsla`（h/s/l/a 全 0..1，h 转满一圈为 1）、边界交换用 `Rgba`（`rgb()/rgba()` 十六进制构造），`alpha` 是替换透明度、`opacity` 是乘法，`blend(other)` 把 other 按 \( (1-a):a \) 加权叠在自己上面。
- `From` 实现承担 HSL↔RGB 双向转换，`Hsla::blend` 靠「转 RGB 混完再转回来」绕开色相环不能线性插值的问题。
- 渐变背景 `Background` 有 Solid/LinearGradient/PatternSlash/Checkerboard 四形态，`linear_gradient(angle, from, to)` + `linear_color_stop` 构造，`ColorSpace::Srgb/Oklab` 决定插值路径；`colors.rs` 提供 `Colors` 语义调色板并以 `GlobalColors` 全局挂载。

## 7. 下一步学习建议

本讲之后，你已经备齐了「读得懂任何 gpui 绘制代码」的词汇表。建议路线：

1. **下一讲 u3-l5（内置元素一览）**：看 `text`/`img`/`svg`/`canvas` 如何以本讲的类型为参数完成实际渲染，其中 canvas 的绘制回调就是 `Bounds` + 颜色的练兵场。
2. **回看 u3-l3（Style 合成）**：现在可以带着「`Background` 才是 `bg` 的真实类型」的认知重读样式合并逻辑，理解主题为什么能表现为一叠低优先级补丁。
3. **预习 u4-l4（Scene 与绘制原语）**：`ScaledPixels` 与 `#[repr(C)] Background` 将在那里进入 GPU 侧的 `Primitive`，本讲的两个伏笔（ceil 取整、固定布局）都会回收。
4. 想练手的话，把综合实践的色带改成 HSL 圆柱体切片（固定 h，扫描 s×l 平面），用 `Bounds::from_corners` + `localize` 实现鼠标悬停取色——一个纯几何 + 颜色的小取色器。
