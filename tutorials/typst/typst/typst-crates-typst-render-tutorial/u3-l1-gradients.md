# 渐变填充

## 1. 本讲目标

本讲是专家层的第一篇，专门讲 typst-render 如何把 Typst 的**渐变（Gradient）**画进像素画布。读完本讲你应当能够：

- 说清渐变在 typst-render 中有**两条渲染路径**——形状走「预渲染纹理 + Pattern 着色器」，文本快路径走「逐像素采样」——以及为什么会有这种分流。
- 解释 `RelativeTo::Self_` 与 `RelativeTo::Parent` 两种坐标系参考的区别，以及它们如何改变 `container_size`、`fill_transform`、`gradient_map` 三个量。
- 推导 `GradientSampler::sample` 如何把一个画布像素坐标「映射回」渐变空间采样，并理解 `container_transform.invert()` 的桥梁作用。
- 读懂 `cached` 函数的 `comemo::memoize` 缓存机制，以及 `gradient_map` 为何只为负尺寸矩形生成。

本讲承接 u2-l3（纯色 Paint 转换）与 u2-l6（文本渲染基础），是 u3-l2（平铺 Tiling）的直接前置。

## 2. 前置知识

在进入渐变之前，请确认你已经理解下面这些在前置讲义中建立的概念：

- **Paint 与 to_sk_paint**：`Paint` 是 Typst 的填充描述，分 `Solid`（纯色）、`Gradient`（渐变）、`Tiling`（平铺）三种。`to_sk_paint` 把它转成 tiny-skia 的 `sk::Paint`（见 u2-l3）。
- **State 状态背包**：渲染递归中随身携带的不可变状态，含 `transform`（当前局部→画布）、`container_transform`（首个硬帧→画布）、`size`（首个硬帧尺寸）、`pixel_per_pt`、`mask`（见 u2-l1、u2-l2）。
- **pre_concat / post_concat 语义**：`A.pre_concat(B)` = 「先 B 后 A」= \(A \circ B\)；`A.post_concat(B)` = 「先 A 后 B」= \(B \circ A\)（见 u2-l1）。
- **文本快/慢路径**：`render_outline_glyph` 在 `ppem > 100`、有描边、非均匀缩放等情况下走「路径绘制」慢路径，否则走 `pixglyph` 光栅化快路径（见 u2-l6）。
- **预乘 alpha（premultiplied alpha）**：颜色以 \(C_{rgb}' = C_{rgb} \times \alpha\) 形式存储，是逐像素混合的基础（见 u3-l3，本讲只需知道 `PremultipliedColorU8` 这个类型）。

**关键直觉**：纯色填充每个像素都是同一个颜色，所以可以直接交给 tiny-skia 用一个 `sk::Color` 填；而渐变每个像素颜色不同，必须先想清楚「画布上这个像素，对应渐变里的哪个位置」。本讲的全部难点都围绕这个「坐标对应关系」展开。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/paint.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs) | 渐变渲染的主战场：`PaintSampler` trait、`GradientSampler`、`to_sk_paint` 的 `Paint::Gradient` 分支、`cached` 缓存 |
| [src/text.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs) | 文本字形如何调用 `GradientSampler`，以及在快路径里如何逐像素采样 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs) | `State` 结构体定义（`container_transform` / `transform` / `size` 字段） |
| [src/shape.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs) | `render_shape` 如何以 `Some(shape)` 调用 `to_sk_paint`，触发 `gradient_map` 计算 |
| ../typst-library/src/visualize/gradient.rs | `Gradient::sample_at`、`unwrap_relative`、`anti_alias`、`RelativeTo` 的定义 |

---

## 4. 核心概念与源码讲解

### 4.1 渐变渲染的两条路径与 PaintSampler

#### 4.1.1 概念说明

渐变和纯色最大的不同是：**纯色是常数函数，渐变是位置函数**——颜色随像素位置变化。这导致 typst-render 必须为渐变回答一个问题：「画布上的像素 \((x,y)\)，应该取渐变里的什么颜色？」

根据绘制对象不同，typst-render 用两条路径回答这个问题：

1. **形状路径（Shape / 慢路径文本）**：把整块渐变**预渲染成一张 `Pixmap` 纹理**，再用 tiny-skia 的 `Pattern` 着色器贴回形状。tiny-skia 内部负责把形状像素映射到纹理像素。
2. **文本快路径**：`pixglyph` 只给字形的**覆盖率位图（coverage bitmap）**，不是路径，tiny-skia 无法直接填。于是 typst-render **自己逐像素采样**渐变颜色，再手动和覆盖率做 alpha 混合。

为了在这两种文本场景（纯色 vs 渐变 vs 平铺）下复用同一套「逐像素填色」逻辑，typst-render 抽象出了 `PaintSampler` trait。

#### 4.1.2 核心流程

`PaintSampler` 把「在画布像素 \((x,y)\) 处取一个颜色」这件事抽象成一个方法：

```
trait PaintSampler {
    fn sample(self, pos: (u32, u32)) -> PremultipliedColorU8;
}
```

它有两个实现，正好对应「常数」与「位置函数」：

- `impl PaintSampler for PremultipliedColorU8`：纯色——`sample` 忽略位置，直接返回自己（常数函数）。
- `impl PaintSampler for GradientSampler`：渐变——`sample` 根据位置计算颜色（位置函数）。

这样 `write_bitmap`（字形位图写入）就能对纯色和渐变用**同一份循环代码**，只是传入的 `sampler` 不同。

#### 4.1.3 源码精读

`PaintSampler` trait 与纯色实现（常数采样器）在：

[src/paint.rs:11-22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L11-L22) —— 定义 `sample(pos)` 接口；纯色实现 `fn sample(self, _: (u32,u32))` 直接返回 `self`，无视坐标。

文本快路径里的分流——根据 `Paint` 类型选不同 sampler——在：

[src/text.rs:125-143](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L125-L143) —— `Paint::Gradient` 构造 `GradientSampler`、`Paint::Solid` 直接传预乘颜色、`Paint::Tiling` 构造 `TilingSampler`，三者都交给同一个 `write_bitmap(canvas, &bitmap, &state, sampler)`。

而 `write_bitmap` 内部对每个像素调 `sampler.sample((x as u32, y as u32))` 取色，见无遮罩分支：

[src/text.rs:223](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L223) —— 这里 `(x, y)` 是**画布像素坐标**（不是纹理局部坐标），这一点对 4.4 的坐标换算至关重要。

#### 4.1.4 代码实践

**实践目标**：确认「同一份 `write_bitmap` 代码服务三种 Paint」。

**操作步骤**：

1. 打开 [src/text.rs:148-239](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L148-L239)，找到 `write_bitmap<S: PaintSampler>`。
2. 注意它的泛型参数 `S: PaintSampler`，以及唯一取色入口 `sampler.sample(...)`。

**需要观察的现象**：函数体内**没有任何**对 `S` 是纯色还是渐变的判断——所有差异都封装在 `sample` 里。

**预期结果**：你应当看到 `write_bitmap` 是「字形覆盖率 × 采样颜色 → 混合」的通用循环，与具体 Paint 类型完全解耦。

#### 4.1.5 小练习与答案

**练习 1**：为什么纯色实现 `sample` 的参数 `pos` 用下划线 `_` 命名？
**答案**：因为纯色是常数函数，颜色不依赖坐标，`pos` 被忽略，用 `_` 显式表明「不用这个参数」。

**练习 2**：如果把渐变也实现成「预渲染纹理」给文本快路径用，会丢失什么信息？
**答案**：会丢失字形覆盖率（coverage）。文本快路径依赖 `pixglyph` 给出的每像素覆盖率来抗锯齿，`Pattern` 着色器无法利用这份覆盖率，所以必须逐像素采样并手动混合。

---

### 4.2 RelativeTo：渐变相对于谁

#### 4.2.1 概念说明

渐变需要知道「铺在多大的范围上」。Typst 用 `RelativeTo` 枚举给出两种参考：

- `RelativeTo::Self_`：相对于**元素自身**的边界框（bounding box）。一个 100pt×100pt 的矩形，渐变就铺满这 100pt×100pt。
- `RelativeTo::Parent`：相对于**父容器**（首个硬帧，通常是整页或整个文本块容器）。这样多个小元素共享同一个铺满整页的渐变，颜色在元素之间**连续过渡**。

这正是 Typst 里 `#rect(fill: gradient.linear(..))`（默认 `self`）与渐变文本（默认 `parent`，使一行字共享一个横跨整行的渐变）的区别。

#### 4.2.2 核心流程

`RelativeTo` 的取值由 `Gradient::unwrap_relative(on_text)` 决定。它处理一个特殊值 `auto`（Rust 里是 `None`）：

```
unwrap_relative(on_text):
  若显式指定了 self/parent → 用它
  否则（auto）→ on_text ? Parent : Self_
```

即**形状默认 Self_，文本默认 Parent**。这个默认值是理解后续 `container_size`、`fill_transform` 取值差异的钥匙。

#### 4.2.3 源码精读

`RelativeTo` 枚举定义在 typst-library：

[../typst-library/src/visualize/gradient.rs:1228-1234](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L1228-L1234) —— 两个变体 `Self_`（自身边界框）、`Parent`（父边界框）。

`unwrap_relative` 的默认逻辑：

[../typst-library/src/visualize/gradient.rs:985-989](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L985-L989) —— `auto`（`None`）时，文本走 `Parent`、非文本走 `Self_`。

typst-render 里**两处**都调用了它：形状分支 [src/paint.rs:203](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L203)（`on_text=false`），文本采样器 [src/paint.rs:42](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L42)（`on_text=true`）。

#### 4.2.4 代码实践

**实践目标**：验证「文本默认 Parent、形状默认 Self_」。

**操作步骤**：

1. 读 [src/paint.rs:42](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L42)，`GradientSampler::new` 第 4 参数 `on_text` 在文本快路径 [src/text.rs:127](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L127) 传的是 `true`。
2. 读 [src/paint.rs:203](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L203)，形状分支 `to_sk_paint` 的 `on_text` 在 [src/shape.rs:42](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L42) 传的是 `false`。

**需要观察的现象**：`on_text` 这个布尔值如何一路传到 `unwrap_relative`。

**预期结果**：确认默认参考方向与调用点的 `on_text` 实参一致。

#### 4.2.5 小练习与答案

**练习 1**：如果用户在 Typst 里给一段文字显式写了 `relative: "self"`，`unwrap_relative` 会返回什么？
**答案**：返回 `Self_`。`unwrap_relative` 只在值为 `auto`（`None`）时才回退到默认；显式指定优先。

**练习 2**：为什么文本默认是 `Parent` 而不是 `Self_`？
**答案**：若每个字形都相对自身铺渐变，那么一行字里每个字都重复一遍完整的渐变，颜色在字与字之间不连续；相对父容器则整行字共享一个渐变，颜色流畅过渡，这正是渐变文本的预期视觉效果。

---

### 4.3 形状路径：to_sk_paint 的 Paint::Gradient 分支

#### 4.3.1 概念说明

这是渐变最常走的路径——**形状填充与描边**，以及**大字号文本的慢路径**。它的策略是：

1. 先确定渐变要铺的「容器」有多大（`container_size`）。
2. 用 `cached` 把整块渐变**预渲染**成一张 `container_size × pixel_per_pt` 分辨率的 `Pixmap` 纹理。
3. 把这张纹理作为 `Pattern` 着色器挂到 `sk::Paint` 上，让 tiny-skia 在填充形状时自己从纹理里取色。

关键在于：纹理分辨率已经和最终输出一致，所以 Pattern 用 `FilterQuality::Nearest`（最近邻），避免二次插值模糊。

#### 4.3.2 核心流程

`to_sk_paint` 在 `Paint::Gradient` 分支的三步：

```
1. relative = gradient.unwrap_relative(on_text)
2. 依 relative 算三个量：
     container_size : 渐变铺多大（Self_=bbox, Parent=state.size）
     fill_transform : 如何把纹理对齐到画布
     gradient_map   : 负尺寸矩形的镜像映射（仅 Self_ 可能有值）
3. width/height = ceil(|container_size| * pixel_per_pt)
   pixmap = cached(gradient, width, height, gradient_map)   # 预渲染纹理
   sk_paint.shader = Pattern(pixmap, SpreadMode::Pad,
                             FilterQuality::Nearest, 1.0,
                             fill_transform.pre_scale(signum/ppp, signum/ppp))
```

`Self_` 与 `Parent` 三个量的对比（本讲综合实践的核心）：

| 量 | `RelativeTo::Self_` | `RelativeTo::Parent` |
|---|---|---|
| `container_size` | `item_size`（= 形状 bbox 尺寸） | `state.size`（首个硬帧尺寸） |
| `fill_transform` | `from_translate(bbox.min.x, bbox.min.y)`（来自 shape 块） | `container_transform.post_concat(transform.invert())` |
| `gradient_map` | `Some`（仅 `Rect` 负尺寸）或 `None` | 恒为 `None` |

#### 4.3.3 源码精读

`to_sk_paint` 入口与开头对 `shape` 计算 `item_size`/`fill_transform`/`gradient_map`：

[src/paint.rs:176-194](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L176-L194) —— 仅当 `shape: Some(shape)` 时（即形状场景），才算 `bbox`、`fill_transform = from_translate(bbox.min)` 与 `gradient_map`；否则三者取 `(Size::zero(), None, None)`（文本慢路径正是如此，见 4.4 末）。

`Paint::Gradient` 分支主体——`container_size` / `fill_transform` / `gradient_map` 三段 `match relative`：

[src/paint.rs:202-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L202-L219) —— 这就是上表三行的源码出处。

纹理分辨率计算与 `cached` 调用：

[src/paint.rs:221-231](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L221-L231) —— `width/height` 对 `pixel_per_pt.ceil()` 取下限，保证容器退化（尺寸为 0）时也至少有 1 像素纹理。

构造 `Pattern` 着色器：

[src/paint.rs:235-246](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L235-L246) —— `SpreadMode::Pad`（超出范围钳制到边缘色）、`FilterQuality::Nearest`（纹理已是原生分辨率，无需插值）、`anti_alias = gradient.anti_alias()`。

> **关于 `SpreadMode::Pad`**：渐变参数 \(t\) 超出 \([0,1]\) 时，Pad 模式把颜色钳制到最近的端点色（0 端或 1 端），而不是像平铺那样重复。这是渐变与平铺（`Repeat`，见 u3-l2）在着色器层面的本质区别。

形状填充与描边如何调用本分支：

- 填充：[src/shape.rs:41-42](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L41-L42) —— `to_sk_paint(fill, state, false, &mut pixmap, Some(shape), false)`。
- 描边：[src/shape.rs:64-71](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L64-L71) —— 注意第 6 参数 `include_stroke_in_bbox` 传 `matches!(paint, Paint::Gradient(_))`，即**渐变描边时把描边宽度算进 bbox**，好让渐变对齐覆盖描边区域。

#### 4.3.4 代码实践

**实践目标**：读懂 `Pattern` 的 transform 如何把预渲染纹理贴到画布。

**操作步骤**：

1. 读 [src/paint.rs:240-243](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L240-L243)，Pattern 的 transform 是 `fill_transform.pre_scale(signum_x/ppp, signum_y/ppp)`。
2. 对 `Self_` 模式展开：`fill_transform = from_translate(bbox.min)`，故 transform = `translate(bbox.min) ∘ scale(signum/ppp)`。

**需要观察的现象**：`pre_scale` 里的 `1/pixel_per_pt` 因子的作用。

**预期结果**：纹理是按 `container_size × ppp` 像素渲染的，`1/ppp` 把画布的像素坐标换算回纹理的像素坐标；`signum` 处理负尺寸矩形的镜像。这两者共同把纹理「贴」到 bbox 上。

#### 4.3.5 小练习与答案

**练习 1**：为什么形状描边用渐变时，`include_stroke_in_bbox` 要设为 `true`？
**答案**：描边会扩展形状的实际绘制范围（bbox 要加上描边宽度的一半）。渐变是相对 bbox 铺的，若 bbox 不含描边，渐变与描边/填充就会错位；设为 `true` 让 `gradient_map`/`fill_transform` 基于含描边的 bbox 计算，保证描边与填充的渐变对齐。

**练习 2**：`Pattern` 用 `FilterQuality::Nearest` 会不会让渐变出现锯齿？
**答案**：不会。纹理本身就是按最终输出分辨率（`container_size × pixel_per_pt`）预渲染的，像素一一对应，最近邻取色不会引入插值模糊或锯齿；源码注释也明确说明了这一点（[src/paint.rs:233-234](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L233-L234)）。

---

### 4.4 文本路径：GradientSampler 逐像素采样

#### 4.4.1 概念说明

文本快路径下，`pixglyph` 给出的是字形的**覆盖率位图**（每个像素一个 0–255 的 alpha），不是矢量路径。typst-render 必须自己决定每个字形像素涂什么渐变色，再用覆盖率混合上去。`GradientSampler` 就是「把画布像素坐标换算回渐变空间并取色」的采样器。

它的核心难点是**坐标换算**：`write_bitmap` 传进来的 `(x, y)` 是**画布像素坐标**，而渐变定义在**父容器的点坐标系**里，必须做一次逆变换。

#### 4.4.2 核心流程

`GradientSampler` 构造时缓存一个逆变换 `transform_to_parent`，采样时用它把画布像素映射回容器空间：

```
GradientSampler::new(gradient, state, item_size, on_text):
  relative = unwrap_relative(on_text)         # 文本默认 Parent
  container_size   = Parent ? state.size : item_size
  transform_to_parent = Parent ? container_transform.invert() : identity

GradientSampler::sample((x, y)):              # (x,y) 是画布像素
  point = (x, y)
  transform_to_parent.map_point(point)        # 画布像素 → 容器点坐标
  color = gradient.sample_at(point, container_size)
  return to_sk_color_u8(color).premultiply()
```

为什么 `Parent` 模式需要 `container_transform.invert()`？因为渐变纹理定义在**父容器坐标系**，而采样输入是**画布像素坐标**。`container_transform` 把容器点映射到画布像素（含 `pixel_per_pt` 缩放与所有平移），它的逆变换正好把画布像素「还原」回容器点空间，于是 `sample_at` 拿到的就是正确的容器内坐标。

#### 4.4.3 源码精读

`GradientSampler` 结构体（缓存逆变换）：

[src/paint.rs:28-33](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L28-L33) —— 字段 `gradient`、`container_size`、`transform_to_parent`。注释说明「缓存到父级的逆变换，避免对每个像素重复计算」。

构造函数 `new`：

[src/paint.rs:35-59](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L35-L59) —— 注意 `Parent` 分支用 `state.container_transform.invert().unwrap()`（[L50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L50)）；`Self_` 分支用单位变换。

采样实现 `impl PaintSampler`：

[src/paint.rs:61-79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L61-L79) —— 先 `map_point` 把画布像素映射回容器点，再调 `gradient.sample_at`，最后 `to_sk_color_u8(...).premultiply()`。

> **颜色量化差异（示例说明，非项目原有命名）**：形状路径的 `cached` 用 `to_sk_color(...).premultiply().to_color_u8()`（先 f32 预乘再转 u8，[src/paint.rs:168-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L168-L169)），而 `GradientSampler` 用 `to_sk_color_u8(...).premultiply()`（先 u8 再 u8 预乘，[src/paint.rs:69-77](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L69-L77)）。两者量化顺序不同，理论上可能有 1 LSB 级别的色差，但视觉上不可见。

文本快路径如何构造并使用采样器：

[src/text.rs:126-129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L126-L129) —— `GradientSampler::new(gradient, &state, Size::zero(), true)`；`item_size` 传 `Size::zero()` 是因为文本默认走 `Parent`，`container_size` 取 `state.size`，`item_size` 用不到。

文本慢路径（大字号）其实也走形状路径——它对 `to_sk_paint` 传 `shape=None`：

[src/text.rs:79-81](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L79-L81) —— `to_sk_paint(&text.fill, state_ts, true, &mut pixmap, None, false)`。`shape=None` 触发 [src/paint.rs:192-194](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L192-L194) 的 `else` 分支，`item_size` 为零、`fill_transform`/`gradient_map` 为 `None`；但 `on_text=true` 仍让渐变走 `Parent`，与快路径行为一致。

#### 4.4.4 代码实践

**实践目标**：手推一个画布像素到渐变颜色的完整换算链。

**操作步骤**：假设某行文本渲染在页面左上角附近，`state.container_transform` 是纯缩放 `from_scale(2.0, 2.0)`（`pixel_per_pt=2`），`state.size = (100pt, 100pt)`，线性渐变水平铺满。

1. 取字形某像素 `(x, y) = (40, 10)`（画布像素）。
2. `transform_to_parent = container_transform.invert() = from_scale(0.5, 0.5)`。
3. `map_point(40, 10) → (20, 5)`（容器点坐标，单位 pt）。
4. `sample_at((20, 5), (100, 100))`：`sample_at` 先归一化 `x/width = 20/100 = 0.2`，得渐变参数 \(t \approx 0.2\)，取该处颜色。

**需要观察的现象**：`map_point` 把像素除以 2 还原成 pt。

**预期结果**：画布第 40 像素列对应容器第 20pt，即渐变的 20% 处。若你改 `pixel_per_pt` 为 4，同一像素列会对应容器第 10pt（渐变 10% 处）——分辨率越高，同一画布像素映射到的渐变位置越靠左。

#### 4.4.5 小练习与答案

**练习 1**：`GradientSampler::new` 为什么要缓存 `transform_to_parent` 而不是每次 `sample` 都算？
**答案**：一个字形有成百上千个像素，每个像素都要采样一次。逆变换对整个字形是常数，提前算一次缓存进 `GradientSampler`（`Copy` 类型），避免每个像素重复求逆，大幅省时。

**练习 2**：`state.container_transform.invert()` 返回 `Option`，代码用 `unwrap()`。什么情况下逆变换不存在？
**答案**：当变换矩阵行列式为 0（退化，如某轴缩放为 0）时逆变换不存在。在正常排版里 `container_transform` 至少含 `pixel_per_pt` 的正缩放，行列式非 0，所以 `unwrap` 安全。

---

### 4.5 cached 记忆化缓存与 gradient_map 负尺寸镜像

#### 4.5.1 概念说明

形状路径里，把整块渐变预渲染成纹理是**最贵的一步**——要遍历 `width × height` 个像素逐个调 `sample_at`。但很多形状共享同一个渐变（同一种填充、同一分辨率），反复重算很浪费。typst-render 用 `comemo::memoize` 把「同渐变 + 同分辨率 + 同镜像图」的结果缓存成 `Arc<Pixmap>`，命中时零拷贝共享。

`gradient_map` 则是一个看似怪异、实则有用的修补：**负尺寸矩形的镜像对齐**。Typst 支持负宽高矩形（参见 u2-l4 的 `signum` 镜像），但渐变采样在 `cached` 里是按「正」坐标系遍历的，需要 `gradient_map` 把坐标偏移并按 `signum` 翻转，才能让负尺寸矩形的填充与描边渐变对齐。

#### 4.5.2 核心流程

`cached` 的缓存键与渲染逻辑：

```
#[comemo::memoize]
cached(gradient, width, height, gradient_map) -> Arc<Pixmap>:
  (offset, scale) = gradient_map.unwrap_or((zero, one))
  pixmap = Pixmap::new(width, height)
  for x in 0..width, y in 0..height:
    color = gradient.sample_at(
        ((x + offset.x) * scale.x, (y + offset.y) * scale.y),
        (width, height))
    pixmap[x,y] = to_sk_color(color).premultiply().to_color_u8()
  return Arc::new(pixmap)
```

`gradient_map` 的构造（仅 `Geometry::Rect` 且尺寸为负时）：

```
gradient_map = Some((
    Point(若 rect.x<0 { -bbox.w } else {0}, 若 rect.y<0 { -bbox.h } else {0}) * ppp,
    Axes(signum(rect.x), signum(rect.y))
))
```

即 offset 把坐标平移一个（负的）bbox 宽高，scale 用 `signum`（-1 或 1）翻转。二者合起来实现了「在纹理像素坐标里镜像采样」。

#### 4.5.3 源码精读

`cached` 函数（`comemo::memoize`）：

[src/paint.rs:148-174](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L148-L174) —— 缓存键为 `(gradient, width, height, gradient_map)` 四元组；逐像素 `sample_at`，`gradient_map` 的 `(offset, scale)` 用于变换采样坐标（[L155-156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L155-L156)）；返回 `Arc<Pixmap>` 供多处零拷贝共享。

`gradient_map` 的构造——`Geometry::Rect` 负尺寸分支：

[src/paint.rs:180-190](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L180-L190) —— offset 在该轴为负时取 `-bbox.size()`（再乘 `pixel_per_pt`），scale 取 `Ratio::new(rect.x.signum())`；非 Rect 几何走 `_ => None`（[L189](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L189)）。注意 `gradient_map` 在 `Parent` 模式被强制设为 `None`（[src/paint.rs:216-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L216-L219)），因为负尺寸镜像只对「相对自身」有意义。

`cached` 的调用点：

[src/paint.rs:226-231](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L226-L231) —— 把 `gradient`、`width`、`height`、`gradient_map` 传入；`width`/`height` 对 `pixel_per_pt.ceil()` 取下限（防容器尺寸为 0）。

> `sample_at` 的数学：它先把坐标归一化为 \(x' = x/\text{width}\)、\(y' = y/\text{height}\)，再依渐变类型算参数 \(t\)。线性渐变（考虑宽高比校正后的角度 \(\alpha\)）：
> \[
> t = \frac{x'|\cos\alpha| + y'|\sin\alpha|}{|\sin\alpha| + |\cos\alpha|}
> \]
> 详见 [../typst-library/src/visualize/gradient.rs:908-972](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L908-L972)。typst-render 只负责把正确的 `(x, y)` 与 `(width, height)` 喂给它。

#### 4.5.4 代码实践

**实践目标**：理解 `gradient_map` 如何镜像负尺寸矩形的渐变。

**操作步骤**：

1. 读 [src/paint.rs:180-190](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L180-L190)。设想 `rect = (-50pt, 30pt)`（宽为负、高为正）。
2. 推导：`bbox.size().x = 50pt`，`rect.x.signum() = -1`，故 `offset.x = -50pt`（乘 ppp），`scale.x = -1`。
3. 在 `cached` 里，纹理像素 `x=0` 实际采样坐标 `(0 + (-50pt×ppp)) × (-1)` —— 即被翻转并平移。

**需要观察的现象**：`scale.x = -1` 让 `x` 递增时采样坐标递减，实现水平镜像。

**预期结果**：负宽矩形的渐变被水平翻转，使其与描边（描边路径本身也经过 `signum` 镜像，见 u2-l4）在视觉上对齐。**具体像素值待本地验证**，但镜像方向可由 `signum` 符号推断。

#### 4.5.5 小练习与答案

**练习 1**：`cached` 的缓存键里为什么要包含 `gradient_map`？
**答案**：因为同一个渐变在不同 `gradient_map`（正 vs 负尺寸矩形）下，纹理内容不同（一个正向、一个镜像）。若不把 `gradient_map` 算进键，就会错误地复用镜像翻转前的纹理。`gradient_map` 是 `Option<(Point, Axes<Ratio>)>`，派生了 `Hash`，可作键。

**练习 2**：`Parent` 模式下为什么 `gradient_map` 强制为 `None`？
**答案**：负尺寸镜像只对「相对自身」的矩形有意义——它修补的是矩形自身边界框的翻转。`Parent` 模式下渐变相对父容器铺，与矩形自身正负尺寸无关，故不需要镜像，强制 `None`（[src/paint.rs:216-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L216-L219)）。

**练习 3**：两个不同位置、但相同尺寸、相同渐变的矩形，能否命中同一个 `cached`？
**答案**：能。缓存键是 `(gradient, width, height, gradient_map)`，不含位置。位置差异由 `fill_transform`（`from_translate(bbox.min)`）在 Pattern 贴图阶段处理，纹理本身可复用——这正是记忆化的收益所在。

---

## 5. 综合实践

本任务把本讲的两条路径与 `RelativeTo` 两个模式串起来。

### 实践目标

亲手生成一张渐变文本与渐变矩形的 PNG，观察 `Self_` 与 `Parent` 的视觉差异，并在源码里印证 `container_size`、`fill_transform`、`gradient_map` 的取值差异。

### 操作步骤

1. **准备 Typst 源文件** `gradient.typ`（示例代码，非项目原有文件）：

   ```typst
   #set page(width: 200pt, height: 60pt, margin: 10pt)
   #let g = gradient.linear(red, blue)

   // 矩形默认 relative: "self"，渐变铺满矩形自身
   #rect(width: 180pt, height: 20pt, fill: g)

   // 文本默认 relative: "parent"，整行共享一个渐变
   #text(size: 20pt, fill: g)[Gradient Text]
   ```

2. **渲染为 PNG**（需本地安装 typst CLI，待本地验证命令与路径）：

   ```bash
   typst compile --format png --ppi 144 gradient.typ gradient.png
   ```

3. **观察输出**：
   - 矩形：红→蓝横向铺满整个矩形（`Self_`）。
   - 文本：红→蓝横跨整行文字连续过渡，而不是每个字重复一遍（`Parent`）。

4. **源码印证**：填写下表（答案见 4.3.2 与 4.4）。

   | 量 | `Self_`（矩形） | `Parent`（文本） |
   |---|---|---|
   | `container_size` | ? | ? |
   | `fill_transform` | ? | ? |
   | `gradient_map` | ? | ? |

5. **坐标换算**：对文本中某个字形像素 `(x,y)`，写出 `GradientSampler::sample` 的三步换算（`map_point` → `sample_at` → `to_sk_color_u8().premultiply()`），并解释为何需要 `container_transform.invert()`。

### 预期结果

- 表格答案：`container_size` = (bbox 尺寸 vs `state.size`)；`fill_transform` = (`from_translate(bbox.min)` vs `container_transform.post_concat(transform.invert())`)；`gradient_map` = (Rect 负尺寸时 `Some` 否则 `None` vs 恒 `None`)。
- 坐标换算：画布像素 →（`container_transform.invert()`）→ 容器点坐标 →（`sample_at` 归一化）→ 渐变参数 \(t\) → 颜色。`invert()` 是因为渐变锚定在父容器，而采样输入是画布像素，必须「逆」一次才能还原到容器空间。
- 若无 typst CLI，本任务可降级为纯源码阅读：直接据 4.3、4.4 的源码精读完成表格与换算推导。

---

## 6. 本讲小结

- 渐变在 typst-render 有**两条路径**：形状（含大字号慢路径文本）走「`cached` 预渲染纹理 + `Pattern` 着色器」；文本快路径走「`GradientSampler` 逐像素采样 + 手动混合」。`PaintSampler` trait 统一了纯色、渐变、平铺三者的逐像素采样接口。
- `RelativeTo::Self_` 相对自身 bbox、`RelativeTo::Parent` 相对父容器；`unwrap_relative` 对 `auto` 的默认是「文本 Parent、形状 Self_」。
- 形状路径的 `to_sk_paint` 依 `relative` 算出 `container_size`/`fill_transform`/`gradient_map` 三量，调 `cached` 出纹理，挂 `Pattern`（`SpreadMode::Pad`、`Nearest`）。
- `GradientSampler` 缓存 `container_transform.invert()` 作 `transform_to_parent`，采样时把画布像素映射回容器点空间再 `sample_at`——这是 `Parent` 模式坐标换算的核心。
- `cached` 用 `comemo::memoize` 缓存 `(gradient, width, height, gradient_map)` → `Arc<Pixmap>`，位置不同的同尺寸同渐变形状可命中同一纹理。
- `gradient_map` 只为负尺寸 `Rect` 生成（`Self_` 模式），用 `offset` + `signum` scale 镜像渐变，使负尺寸矩形的填充与描边对齐；`Parent` 模式恒为 `None`。

## 7. 下一步学习建议

- **u3-l2 平铺图案 Tiling**：`TilingSampler` 与 `to_sk_paint` 的 `Paint::Tiling` 分支，结构与渐变高度对称，但用 `rem_euclid` 实现周期性采样、`SpreadMode::Repeat`。学完本讲再读 u3-l2 会非常顺。
- **u3-l3 字形光栅化与像素级混合**：本讲的 `write_bitmap`、`blend_src_over`、`alpha_mul` 都在那里详解，包括遮罩下「先渲染到带 1px padding 的临时 pixmap 再 `draw_pixmap`」的策略。
- **u3-l4 记忆化与性能**：把本讲的 `cached` 与 `rasterize`、`build_texture` 三处 `comemo::memoize` 一起看，理解 typst-render 的缓存全景。
- **延伸阅读**：`Gradient::sample_at` 的完整数学（线性/径向/锥形）在 [../typst-library/src/visualize/gradient.rs:908-972](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/gradient.rs#L908-L972)，理解它能让你的坐标换算推导形成闭环。
