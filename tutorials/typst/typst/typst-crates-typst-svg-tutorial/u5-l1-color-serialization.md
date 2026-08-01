# 颜色序列化

## 1. 本讲目标

本讲解决一个问题：**Typst 内部一个 `Color` 对象，如何变成一段 SVG/CSS 能识别的颜色字符串？**

学完后你应当能够：

- 说出 `Color` 经 `SvgDisplay` 序列化时的「两条路径」，以及它们各自的判断依据。
- 手工推演任意一个色彩空间（RGB / Luma / CMYK / HSV / LinearRgb / Oklab / Oklch / HSL）最终写出的字符串格式。
- 解释 `round::<N>` 这个 const-generic 工具函数如何控制精度，以及它和 `SvgWrite::push_num` 之间的「双重舍入」关系。
- 解释 alpha（不透明度）通道在不同路径下分别以什么形式出现（hex 第 4 字节 vs ` / ` 后缀）。

本讲只覆盖**纯色**的颜色序列化。渐变（Gradient）和平铺（Tiling）如何生成自己的 SVG 引用，属于 u5-l2 / u5-l3 的内容。

## 2. 前置知识

本讲承接 u2-l3（`write.rs` 输出抽象层），你需要先知道：

- **`SvgDisplay` trait**：typst-svg 里「把一个值格式化成 SVG 文本」的统一接口，只有一个方法 `fn fmt(&self, f: &mut impl SvgWrite)`。任何能作为 SVG 属性值的类型都可以实现它。详见 [src/write.rs:213-215](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L213-L215)。
- **`SvgWrite` trait**：底层「笔」，提供 `push_str` / `push_num` / `push_nums` / `push` 等方法。`push_num` 会先把数字舍入到 9 位小数、整数走 `itoa` 快路径、小数走 `ryu` 最短表示。详见 [src/write.rs:104-141](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L104-L141)。
- 当某段代码写 `svg.attr("fill", color)` 时，`attr` 会要求 `color` 实现 `SvgDisplay`，于是调用 `color.fmt(...)` 把颜色翻译成字符串。

一个背景概念：**色彩空间**。不同颜色模型有不同的「坐标」：

- `Rgb`：常见的 sRGB，红/绿/蓝三个 0–1 通道。
- `Luma`：灰度，只有一个亮度通道。
- `Cmyk`：印刷四色 青/品红/黄/黑。
- `Hsv` / `Hsl`：色相（hue）/ 饱和度 / 明度或亮度，是 sRGB 的「柱坐标」变换。
- `LinearRgb`：**线性** sRGB（未做伽马编码），亮度与能量成正比，常用于物理正确的混合。
- `Oklab` / `Oklch`：感知均匀色彩空间（Oklab 是笛卡尔坐标，Oklch 是它的柱坐标：明度 / 彩度 / 色相），能表示比 sRGB 更宽的色域。

关键直觉：sRGB 系（Rgb/Luma/Hsv/Cmyk）都能「无损或几乎无损」地映射到 8 位 sRGB hex；而 LinearRgb / Oklab / Oklch / Hsl 要么语义不能被 hex 表达，要么 CSS 专门为它们提供了函数语法。这条直觉正是本讲「两条路径」划分的依据。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `src/paint.rs` | **本讲主战场**。`impl SvgDisplay for Color` 与 `round` 函数都在这里。 |
| `src/write.rs` | 提供 `SvgDisplay` / `SvgWrite` 两个 trait 与 `push_num` 的精度归一实现（u2-l3 已讲）。 |
| `typst-library/.../color.rs` | 定义 `Color` / `ProcessColor` 枚举，以及 `to_process()`、`to_hex()` 方法。本讲会引用它，但不展开。 |

> 说明：typst-svg 里 `Color` 类型本身来自 `typst-library::visualize`，本讲只关心 typst-svg 给它**附加**的 `SvgDisplay` 实现。

## 4. 核心概念与源码讲解

### 4.1 SvgDisplay for Color：颜色到字符串的总入口

#### 4.1.1 概念说明

typst-svg 渲染形状、描边、文本时，会反复写出诸如 `fill="#ff0000"`、`stop-color="oklch(...)"` 这样的属性。属性值里的颜色字符串，统一由 `impl SvgDisplay for Color` 这个实现负责生成。

它在概念上做一件事：**拿一个 `Color`，问它「你是哪个色彩空间的什么坐标」，然后挑一种 SVG/CSS 能识别的写法把它落成字符串。**

Typst 的 `Color` 内部可能很复杂（含「专色」Spot Color，用于印刷）。但序列化时只关心它的「工艺色」（Process Color）等价物——所以入口第一步是 `self.to_process()`，把任意 `Color` 归一成一个 `ProcessColor` 枚举。`ProcessColor` 一共 8 个变体：

```rust
pub enum ProcessColor {
    Luma(Luma), Oklab(Oklab), Oklch(Oklch), Rgb(Rgb),
    LinearRgb(LinearRgb), Cmyk(Cmyk), Hsl(Hsl), Hsv(Hsv),
}
```

详见 [crates/typst-library/src/visualize/color.rs:1434-1451](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L1434-L1451)。

#### 4.1.2 核心流程

`SvgDisplay::fmt` 的总体流程是一张「分发表」：

```text
Color
  └─ to_process() ──▶ ProcessColor（8 变体之一）
        └─ match
            ├─ Rgb | Luma | Cmyk | Hsv ──▶ to_hex()        路径①：hex 字符串
            ├─ LinearRgb                ──▶ color(srgb-linear ...)  路径②：函数语法
            ├─ Oklab                    ──▶ oklab(...)
            ├─ Oklch                    ──▶ oklch(...)
            └─ Hsl                      ──▶ hsl(...) 或 hsla(...)
```

两条路径的设计取舍：

- **路径① hex**：先 `to_rgb()` 转成 sRGB，再编码成 `#rrggbb`（含 alpha 时是 `#rrggbbaa`）。它**统一把颜色「压」进 8 位 sRGB**。对 Rgb/Luma/HSV 这几乎是无损的（HSV 本身就是 sRGB 的双射，Luma 转灰度 RGB 也无歧义）；对 CMYK 则是「务实的有损转换」（印刷色域没有通用的 CSS 写法，落回 sRGB 显示是合理兜底）。
- **路径② 函数语法**：保留该色彩空间的原始坐标，交给浏览器做最后的色域映射。LinearRgb / Oklab / Oklch 是宽色域或线性光空间，**无法**用 sRGB hex 无损表达；HSL 虽属 sRGB 系，但 CSS 有专门的 `hsl()` 语法，直接保留 hue/sat/lightness 更直观也更精确。

> 一句话记住：**能在 sRGB hex 里无损（或务实）落地的，走 hex；否则用 CSS 专门函数。**

注意 `to_hex()` 内部第一步恒为 `self.to_rgb()`，即「先把任何工艺色转成 sRGB」：

```rust
pub fn to_hex(&self) -> EcoString {
    let (r, g, b, a) = self.to_rgb().into_format::<u8, u8>().into_components();
    if a != 255 { eco_format!("#{r:02x}{g:02x}{b:02x}{a:02x}") }
    else        { eco_format!("#{r:02x}{g:02x}{b:02x}") }
}
```

详见 [crates/typst-library/src/visualize/color.rs:1524-1532](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L1524-L1532)。这就是为什么 Luma/CMYK/HSV 共用同一个 hex 出口——它们都被先转成 RGB。

#### 4.1.3 源码精读

入口与 `to_process` 归一（承接上文流程图的「前半段」）：

[src/paint.rs:451-459](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L451-L459) —— `fmt` 把 `self.to_process()` 得到的工艺色用 `match` 分发；其中 `Rgb/Luma/Cmyk/Hsv` 四种用一个 `@` 绑定模式合并到同一条 `to_hex()` 分支（路径①）。

> `to_process()` 的定义见 [crates/typst-library/src/visualize/color.rs:1383-1388](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs#L1383-L1388)：工艺色原样返回，专色取其 `fallback()`。

**LinearRgb 分支（路径②的「线性光」代表）**：

[src/paint.rs:460-468](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L460-L468) —— 输出 `color(srgb-linear R G B)`，三个分量用 `round::<5>` 舍到 5 位小数，用 `push_nums` 以空格分隔写出。alpha 仅在不为 1.0 时追加 ` / <alpha>`。

**Oklab 分支**：

[src/paint.rs:469-479](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L469-L479) —— 输出 `oklab(L% a b)`。注意明度 `l` 被 `×100` 写成百分数且只用 `round::<3>`（3 位），而 `a`/`b` 用 `round::<5>`（5 位）。

**Oklch 分支（本讲代码实践的主角）**：

[src/paint.rs:480-492](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L480-L492) —— 输出 `oklch(L% chroma hue)`。明度 `l` 同样 `×100` 取 `round::<3>`；彩度 `chroma`、色相 `hue.into_degrees()` 取 `round::<5>`；alpha 不为 1.0 时追加 ` / <alpha>`。这里色相要先经 `.into_degrees()` 转成角度值（Typst 内部 `hue` 是带单位的 `Angle`）。

**Hsl 分支（唯一根据 alpha 改函数名的路径）**：

[src/paint.rs:493-509](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L493-L509) —— alpha≠1.0 时写 `hsla(...)`，否则写 `hsl(...)`。色相 `hue.into_degrees()`、饱和度 `×100`、亮度 `×100` 都用 `round::<3>`；alpha 用 `round::<5>`。这里用的是 `f.push(round::<3>(...))` 而非 `f.push_num(...)`——但 `f64` 实现了 `SvgDisplay` 且其 `fmt` 就是调用 `push_num`（见 [src/write.rs:247-251](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L247-L251)），所以二者**功能等价**，只是写法不同。

把上述分支汇总，alpha 在两条路径下的写法对比：

| 路径 | alpha 写法 | 触发条件 |
|------|-----------|---------|
| ① hex | 作为第 4 字节 `#rrggbbaa` | `a != 255`（在 `to_hex` 内部判定） |
| ② 函数 | 追加 ` / <alpha>` | `alpha != 1.0`（在 `fmt` 各分支内判定） |

#### 4.1.4 代码实践

> 实践目标：给定一个 Oklch 颜色的内部坐标，**手工**推演 `SvgDisplay` 输出的字符串；并解释路径划分的原因。

**步骤 1：设定一个示例 Oklch 颜色。** 假设某 `Oklch` 颜色的内部坐标为（以下为「示例值」，仅用于演示算法，非某个具体 Typst 内置色的真实值）：

- 明度 `l = 0.70`
- 彩度 `chroma = 0.15`
- 色相 `hue = 200deg`
- alpha `= 0.8`

**步骤 2：按 [Oklch 分支](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L480-L492) 逐段推导。**

1. 写前缀 `oklch(`。
2. 明度：`round::<3>(100.0 * 0.70) = round::<3>(70.0) = 70.0` → 经 `push_num`，70.0 是整数，输出 `70`，再接 `% ` → `oklch(70% `。
3. 彩度：`round::<5>(0.15) = 0.15` → `push_num` 输出 `0.15`，接一个空格 → `oklch(70% 0.15 `。
4. 色相：`round::<5>(200.0) = 200.0` → 整数，输出 `200` → `oklch(70% 0.15 200`。
5. alpha≠1.0：追加 ` / ` 再 `round::<5>(0.8) = 0.8` → `0.8` → `oklch(70% 0.15 200 / 0.8`。
6. 收尾 `)`。

**预期结果**：

```text
oklch(70% 0.15 200 / 0.8)
```

> 待本地验证：真实 `f32` 运算中 `0.70`、`0.15`、`0.8` 并非精确表示，`×100` 后可能产生极小误差，但 `round::<3>` / `round::<5>` 与 `push_num` 的二次舍入会把它们压回上述「干净」值；若你用调试器单步打印，看到的中间浮点值可能带有尾噪声，最终字符串仍应与上面一致。

**步骤 3：解释路径划分。** 回答规格里的问题：为什么 RGB/CMYK/HSV 走 `to_hex`，而 LinearRgb/Oklab/Oklch/Hsl 走函数语法？

- RGB 是 sRGB 本身，hex 既无损又最短。
- Luma 转 sRGB 后 R=G=B，hex（如 `#888888`）无损且紧凑。
- HSV 没有 CSS 函数（CSS 只有 `hsl()`），但它是 sRGB 的双射，转 hex 无损——所以走 hex。
- CMYK 是印刷色域，无通用 CSS 写法，转 sRGB hex 是务实的显示兜底（接受色域损失）。
- 这四种都被 `to_hex()` 内部的 `self.to_rgb()` 统一拉回 sRGB。
- 反观 LinearRgb 是**线性光**，sRGB hex 含伽马编码会破坏其语义；Oklab/Oklch 是**宽色域**感知空间，sRGB 8 位 hex 容不下其色域；CSS 恰好为它们提供了 `color(srgb-linear …)` / `oklab()` / `oklch()`，于是保留原坐标。HSL 虽属 sRGB 系，但 CSS 有 `hsl()`，直接保留 hue/sat/lightness 更精确直观，故也走函数语法。

#### 4.1.5 小练习与答案

**练习 1**：一个 `Hsl` 颜色，`hue=120deg, saturation=0.5, lightness=0.5, alpha=1.0`，写出 `SvgDisplay` 的输出。

答案：`hsl(120deg 50% 50%)`。alpha 为 1.0，函数名是 `hsl`（不是 `hsla`），且不追加 ` / alpha`；色相保留 `deg` 单位字面量（`push_str("deg ")`），饱和度/亮度各 `×100` 取 `round::<3>`。

**练习 2**：一个完全不透明的 `LinearRgb` 颜色，`red=1.0, green=0.0, blue=0.0`，写出输出。

答案：`color(srgb-linear 1 0 0)`。三个分量经 `round::<5>` 后 `1.0`、`0.0` 在 `push_num` 里都是整数，输出 `1`、`0`；alpha=1.0 故无 ` / ` 后缀。

### 4.2 round：精度归一的 const-generic 工具函数

#### 4.2.1 概念说明

颜色坐标是浮点数。如果原样输出，像 `0.1526315789473684` 这样的长串会污染 SVG 文件体积、且不同机器/编译版本可能产出微小差异（不利于可复现构建）。`round::<DIGITS>` 就是用统一的舍入位数来「截短」浮点、同时保证视觉无损。

它用 **const generic**（编译期常量泛型 `const DIGITS: u32`）来参数化「保留几位小数」——这样不同调用点可以写 `round::<3>(x)` 或 `round::<5>(x)`，编译器为每个 `DIGITS` 实例化一份代码，零运行期开销。

#### 4.2.2 核心流程

把「保留 `DIGITS` 位小数」翻译成乘除法：

\[
\text{round}_{N}(x) = \frac{\bigl( x \times 10^{N} \bigr).\text{round}()}{10^{N}}
\]

即：放大 \(10^{N}\) 倍 → 取最近整数 → 再缩回。输入是 `f32`、输出升级为 `f64`（更高精度承载后续格式化）。

#### 4.2.3 源码精读

[src/paint.rs:514-517](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L514-L517) —— `factor = 10_u32.pow(DIGITS)`，`(num as f64 * factor).round() / factor`。短短三行实现了上面的公式。

调用点的精度选择有规律：

- **3 位**（`round::<3>`）：用于「人眼友好」的百分比量与色相——Oklab/Oklch 的明度 `l`、HSL 的 hue/sat/lightness。这些量感知上平滑，0.1% 精度已绰绰有余，且 3 位更省字节。
- **5 位**（`round::<5>`）：用于「需要忠实往返」的原始坐标——Oklab 的 a/b、Oklch 的 chroma/hue、LinearRgb 的 RGB、所有 alpha。这些数值参与精确色彩计算，留 5 位降低累积误差。

**注意「双重舍入」**：`round::<N>` 的结果随后会传给 `push_num`，而 `push_num` 内部**还会**再舍入到 9 位小数（见 [src/write.rs:104-120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L104-L120)）。也就是说颜色数值实际经历了两次舍入：先用 `round::<3/5>` 决定**有效精度**，再用 `push_num` 保证**全局确定性**并选择最短文本表示（整数走 itoa、小数走 ryu）。前者控制「我们想保留多少信息」，后者控制「文件里最终长什么样」。

#### 4.2.4 代码实践

> 实践目标：用纸笔模拟 `round` + `push_num` 的双重舍入，理解最终文本为何常常「很干净」。

**步骤 1**：计算 `round::<5>(0.1 + 0.2)`（`f32` 下约 0.30000001192092896）。
- `factor = 10^5 = 100000`。
- `0.30000001192092896 * 100000 = 30000.001192...`，`.round() = 30000`，`/ 100000 = 0.3`。
- 再经 `push_num(0.3)`：舍入到 9 位仍是 `0.3`，非整数走 ryu → 输出 `0.3`。

**步骤 2**：计算 `round::<3>(0.12765)`。
- `factor = 1000`。
- `0.12765 * 1000 = 127.65`，`.round() = 128`，`/ 1000 = 0.128`。
- `push_num(0.128)` → 输出 `0.128`。

**需要观察的现象**：即便输入有浮点噪声，只要它落在「半个最小精度单位」之内，`round::<N>` 都会把它抹平成「漂亮」的 N 位小数；这正是 typst-svg 输出的颜色字符串通常简洁的原因。

**预期结果**：`0.3` 与 `0.128`（而非一长串尾数）。

> 待本地验证：不同 `f32` 字面量在编译期被舍入成不同的二进制近似，但只要原始值与「目标干净值」之差小于 \(0.5 \times 10^{-N}\)，结果就稳定。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `round` 的输入是 `f32` 而输出是 `f64`？

答案：颜色分量在 `ProcessColor` 各结构体里以 `f32` 存储（见 `components` 方法里大量 `f64::from(c.x)` 的转换），所以输入只能是 `f32`；输出升格为 `f64` 是为了避免后续 `×100`、`push_num` 的二次计算再次损失精度。

**练习 2**：若把 Oklch 明度的 `round::<3>` 改成 `round::<5>`，会对 SVG 产生什么影响？

答案：明度字符串会从如 `70%` 变成可能带小数的 `70.00001%` 之类（取决于 `l` 的真实值），文件略微变大、可读性略降，但视觉无差别——这正是作者用 3 位而非 5 位的原因：明度是感知平滑量，3 位足够且更省。

## 5. 综合实践

把本讲两条路径与精度控制串起来，完成一次「反向读图」任务：

1. **阅读** [src/paint.rs:451-512](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L451-L512) 的完整 `impl SvgDisplay for Color`。
2. **自造一张表**，列出全部 8 个 `ProcessColor` 变体，填入三列：「走哪条路径」「输出模板」「alpha 写法」。例如 `Cmyk → to_hex → #rrggbb / #rrggbbaa（a!=255）`。
3. **选两个颜色**（一个 Oklch、一个 HSL，含 alpha），按 4.1.4 的方式手工推出完整字符串。
4. **验证思路**：写一个临时 Rust 小程序，`use typst_svg::*;` 之外直接构造 `Color`（或用 `typst` 的 `rgb`/`lch`/`oklch` 构造函数），再用一个实现了 `SvgWrite` 的简单 `String` 包装调用 `color.fmt(...)` 打印结果，与你手推的字符串对比。若不便搭环境，可在 typst-svg 现有集成测试目录里加一个临时断言（本 crate 无颜色序列化单元测试，颜色正确性由 typst 全局测试套件覆盖）。
5. **思考题**：`Hsv` 走 hex、`Hsl` 走函数，同为「sRGB 柱坐标」，为什么待遇不同？请结合「CSS 是否提供专门函数」作答。

> 提示：CSS 历史上只有 `hsl()/hsla()`，没有 `hsv()`；`Hsv` 要么转成 `hsl`、要么转回 sRGB hex，作者选了后者（无损且简短）。

## 6. 本讲小结

- `Color` 经 `to_process()` 归一成 `ProcessColor`，再由 `impl SvgDisplay for Color` 的 `match` 分发成字符串。
- **两条路径**：Rgb/Luma/Cmyk/Hsv 走 `to_hex()`（内部先 `to_rgb()` 再 `#rrggbb`/`#rrggbbaa`）；LinearRgb/Oklab/Oklch/Hsl 走 CSS 函数语法（`color(srgb-linear …)`/`oklab()`/`oklch()`/`hsl()|hsla()`）。
- 划分依据：能否在 sRGB hex 中无损/务实落地——能则 hex，否则用 CSS 专门函数保留原坐标。
- alpha：hex 用第 4 字节（`a != 255` 时），函数用 ` / <alpha>` 后缀（`alpha != 1.0` 时）；HSL 还会据 alpha 切换 `hsl`/`hsla`。
- `round::<DIGITS>` 是 const-generic 精度归一工具：感知量用 3 位、原始坐标用 5 位；其结果再经 `push_num` 二次舍入（9 位 + itoa/ryu），共同保证「短而确定」的输出。

## 7. 下一步学习建议

颜色序列化只是「绘制系统」的开胃菜。建议继续阅读：

- **u5-l2 填充/描边入口与去重引用模型**：看 `write_fill` 如何把本讲的纯色（`Paint::Solid`）与渐变、平铺统一分发，以及 `Paint::Gradient`/`Paint::Tiling` 如何用「源 + ref」两层去重。
- **u5-l3 线性与径向渐变实现**：渐变停靠点的 `stop-color` 仍依赖本讲的 `to_hex()` 与 `SvgDisplay for Color`，可观察颜色序列化如何嵌入到更大的 SVG 结构中。
- 若想深挖色彩空间本身，直接读 `typst-library` 的 [color.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/color.rs) 中 `to_rgb` / `to_oklab` 等转换实现。
