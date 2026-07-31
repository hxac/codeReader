# State 状态与坐标变换

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 [`State`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L116-L128) 这个结构体的五个字段（`transform` / `container_transform` / `mask` / `pixel_per_pt` / `size`）各自代表什么，并理解它「不可变更新（immutable update）」的使用模式。
- 说出 [`AbsExt::to_f32`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L277-L286) 为什么要把 Typst 的长度统一换算成「以 pt 为单位的 f32」。
- 解释 [`to_sk_transform`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L264-L274) 如何把 Typst 的仿射矩阵 `Transform { sx, ky, kx, sy, tx, ty }` **逐字段**映射到 tiny-skia 的 `Transform::from_row`，并指出 `from_row` 参数顺序里那个容易踩坑的「交错排列」。
- 推导 [`pre_translate`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L142-L147) / [`pre_scale`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L149-L154) / [`pre_concat`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L157-L162) 三者叠加时的矩阵含义，并能回答：**「外层 pre_translate、内层 pre_scale」链式调用后，最终变换对局部坐标的表达式是什么。**

本讲是 u2 进阶层的「地基」。[u1-l3](u1-l3-frame-tree.md) 讲清楚了 `render_frame` 如何把场景树派发给各子模块，并反复出现一句「`state.pre_translate(*pos)` 把元素搬到它该在的位置」。但 `pre_translate` 到底叠加了什么、`state.transform` 又是怎么从画布原点一路累积到当前元素的——这些问题都要在本讲解开。搞懂本讲，后续讲 `render_group` 的裁剪（u2-l2）、渐变的「相对父容器」坐标系（u3-l1）才有抓手。

## 2. 前置知识

本讲承接 [u1-l3 Frame 场景树与 render_frame 派发](u1-l3-frame-tree.md)。那里我们确立了：

- Typst 的排版产物是一棵 `Frame` 树，`render_frame` 遍历它，按 `FrameItem` 种类派发。
- `Text` / `Shape` / `Image` 三个叶子分支在原地 `state.pre_translate(*pos)`；`Group` 把 `pos` 原样传给 `render_group` 内部组合。
- 每个元素的位置都是**相对其父帧左上角**的，最终落点由从根到叶的所有变换连乘决定——这句话里的「连乘」正是本讲的主题。

本讲需要你先接受几个名词（正文会逐一展开）：

| 名词 | 直觉解释 |
| --- | --- |
| 仿射变换（affine transform） | 「平移 + 线性部分（缩放/旋转/倾斜）」的组合，能用一个 2×3 矩阵表示。 |
| `sk::Transform` | tiny-skia 的仿射变换类型，typst-render 用它把 Typst 内部坐标换算成画布像素坐标。 |
| `pre_concat`（pre-乘） | 「在已有变换**之前**再插入一个变换」，新变换离被绘制的点更近（更内层）。 |
| pt（磅） | 印刷长度单位，1 inch = 72 pt。typst-render 里所有坐标都以 pt 为中间单位。 |

一句话：**`State` 是渲染递归时随身携带的「坐标上下文」；`transform` 字段记录从画布原点到「此刻正在画的点」的全部变换，每深入一层树就 `pre_concat` 一次。**

## 3. 本讲源码地图

本讲的主角是 typst-render 的 crate 根文件，外加仿射变换类型在 typst-library 里的定义。

| 文件 | 作用 |
| --- | --- |
| `crates/typst-render/src/lib.rs` | 定义 [`State`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L116-L128)、各 `pre_*` / `with_*` 方法、[`to_sk_transform`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L264-L274)、[`AbsExt`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L277-L286)，是本讲的核心。 |
| `crates/typst-render/src/text.rs` | [`render_text`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L15-L41) 里有一段真实的「`pre_translate` 套 `pre_scale`」两层叠加，是本讲实践任务的依据。 |
| `crates/typst-library/src/layout/transform.rs` | 定义 Typst 的 `Transform` 结构体及其 `rotate` / `pre_concat` / `invert` 等数学语义，用于反推矩阵排布。 |

> 说明：本讲的永久链接分属两个 crate。typst-render 内的文件用 `src/...`；typst-library 的文件用 `crates/typst-library/src/layout/transform.rs`。所有链接都锚定到当前 HEAD 提交。

## 4. 核心概念与源码讲解

### 4.1 State：渲染递归的不可变状态背包

#### 4.1.1 概念说明

[`render_frame`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L186-L205) 是递归的：根帧 → 子帧 → 孙帧……每深入一层，坐标上下文都会变化（「往右下挪一点」「整体放大两倍」「被裁剪到某个矩形里」）。如果把这些上下文作为一长串函数参数层层传递，签名会爆炸；如果在全局变量里改写，递归回溯时又难恢复。

typst-render 的解法是定义一个 `State` 结构体，把它当作渲染递归的「随身背包」，里面装着**当前这一层**需要的全部上下文。每深入一层，就用 `pre_*` / `with_*` 方法**生成一个新的 `State`** 传给下一层；回溯时旧 `State` 原封不动，无需「撤销」。

这背后的关键设计是「不可变更新（immutable update）」：`State` 标注了 `#[derive(Copy, Clone)]`，所有 `pre_*` 方法都接收 `self`（按值拷贝）、返回**新的** `State`，从不在原地上修改。于是同一个父帧的 `state` 可以安全地被多个子元素共用——这正是 `render_frame` 里 `state.pre_translate(*pos)` 能为每个 `item` 各算一份而不互相污染的根本原因。

#### 4.1.2 核心流程

`State` 的生命周期：

```
render() 建一个初始 State（transform = pt→像素的缩放）
  └─ 传入 render_frame(根帧)
       └─ 遍历每个 (pos, item):
            ├─ 叶子 item: 用 state.pre_translate(pos) 生成新 state，交给子模块
            └─ Group:     把 pos 传给 render_group，内部再 pre_translate/pre_concat，递归 render_frame
```

每一步都「带着旧 state 进、产出新 state 出」，像函数式编程里的状态传递。背包里装的字段分两类：

- **本讲的主角**：`transform`——从画布原点到当前点的累积仿射变换。
- **后续讲义的主角**：`container_transform` / `mask` / `size`——分别服务渐变的「相对父容器」（u3-l1）、裁剪遮罩（u2-l2）、渐变采样尺寸（u3-l1）。本讲会点明它们的含义，但细节留到对应讲义。

#### 4.1.3 源码精读

[`State`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L116-L128) 的定义只有五个字段：

```rust
#[derive(Default, Copy, Clone)]
struct State<'a> {
    /// The transform of the current item.
    transform: sk::Transform,
    /// The transform of the first hard frame in the hierarchy.
    container_transform: sk::Transform,
    /// The mask of the current item.
    mask: Option<&'a sk::Mask>,
    /// The pixel per point ratio.
    pixel_per_pt: f32,
    /// The size of the first hard frame in the hierarchy.
    size: Size,
}
```

逐字段说明：

| 字段 | 类型 | 含义 | 谁会用到 |
| --- | --- | --- | --- |
| `transform` | `sk::Transform` | 「当前项」的变换：从画布原点到正在绘制的点的**全部**累积仿射变换 | 几乎所有子模块（形状、文本、图像落笔都用它） |
| `container_transform` | `sk::Transform` | 当前层级中**第一个硬帧（Hard frame）**的变换；用于把渐变坐标「相对父容器」换算回画布 | u3-l1 渐变 |
| `mask` | `Option<&'a sk::Mask>` | 当前项的裁剪遮罩（引用，零拷贝）；`None` 表示无裁剪 | u2-l2 Group 裁剪 |
| `pixel_per_pt` | `f32` | 分辨率（每 pt 多少像素），决定光栅化的粒度 | 文本字形光栅化、渐变采样画布大小 |
| `size` | `Size` | 当前层级中**第一个硬帧**的尺寸；渐变 `RelativeTo::Self_` 用它定坐标系 | u3-l1 渐变 |

注意 `mask` 用的是 `&'a sk::Mask`（引用），而 `transform` 是拥有的 `sk::Transform`——因为遮罩体积大、且是父层已建好的，传引用即可；变换体积小、且每层都要改，干脆按值持有。

构造入口 [`State::new`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L131-L139) 只在 [`render()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L21-L48) 里被调用一次，建立整页的初始 `State`：

```rust
fn new(size: Size, transform: sk::Transform, pixel_per_pt: f32) -> Self {
    Self {
        size,
        transform,
        container_transform: transform,   // 初始时 container_transform == transform
        pixel_per_pt,
        ..Default::default()              // mask 默认 None
    }
}
```

两个细节：

- 初始 `container_transform` 被设成和 `transform` 相同的值——因为在根帧这一层，「第一个硬帧」就是页面本身。
- `..Default::default()` 让其余字段（`mask`）取默认值 `None`，这也是 `State` 要派生 `Default` 的原因。

再看 [`render()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L29-L30) 里如何构造它（这是贯穿本讲的真实起点）：

```rust
let ts = sk::Transform::from_scale(pixel_per_pt, pixel_per_pt);  // pt → 像素的缩放
let state = State::new(size, ts, pixel_per_pt);
```

`from_scale(pixel_per_pt, pixel_per_pt)` 是一个纯缩放矩阵：它把 Typst 的 pt 坐标乘以 `pixel_per_pt`，得到像素坐标。随后在 [src/lib.rs:43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L43) 又做了一次 `state.pre_translate(bleed)` 把内容偏移出「出血区」。这两步如何叠加，会在 4.4 节详细推导。

#### 4.1.4 代码实践

1. **实践目标**：在源码里把 `State` 的「生成—传递—更新」链路走一遍。
2. **操作步骤**：
   - 打开 [`State::new`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L131-L139)，确认它只在 [`render()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L21-L48) 里被调用一次。
   - 用 `Grep` 在 `crates/typst-render/src/` 下搜索 `State`，统计 `pre_*` / `with_*` 方法的调用点（应该集中在 `lib.rs` 的 `render` / `render_group`，以及 `text.rs`、`paint.rs`）。
   - 任选一个 `pre_*` 调用点，确认它**返回新 `State`** 且不修改调用方——体会「不可变更新」。
3. **需要观察的现象**：没有任何代码写 `state.transform = ...` 这种原地赋值；所有变更都通过 `Self { ..., ..self }` 结构体更新语法产出新值。
4. **预期结果**：你会看到「`state` 是值语义、按层拷贝、互不污染」的设计贯穿全 crate。
5. **备注**：源码阅读型实践，无需编译运行。

#### 4.1.5 小练习与答案

**练习 1**：`State` 为什么派生 `Copy`？如果去掉 `Copy` 会怎样？

> **参考答案**：`Copy` 让 `State` 按值传递时自动复制，配合 `pre_*(self)` 的签名，可以写出 `state.pre_translate(pos)` 这样「旧 `state` 不变、得到新值」的链式调用。去掉 `Copy` 后，`self` 会发生 move，调用 `state.pre_translate(pos)` 之后原 `state` 就不能再用了——而 `render_frame` 里同一个 `state` 要被多个 `item` 复用，会直接编译失败（或被迫到处 `clone()`）。

**练习 2**：`mask` 字段为什么是 `Option<&'a sk::Mask>`（引用）而不是 `Option<sk::Mask>`（拥有）？

> **参考答案**：遮罩 `Mask` 是一张和画布等大的位图，体积大。在递归里，子层通常直接复用父层（或祖父层）已经建好的遮罩，只需传引用；只有真正发生裁剪时才在 `render_group` 里新建一个（见 u2-l2）。用引用既省内存，也让「父遮罩向子层传递」变得廉价。

---

### 4.2 AbsExt：把任意长度单位统一成 pt-f32

#### 4.2.1 概念说明

Typst 的长度类型 `Abs` 是「带单位的物理长度」——它可以是 pt、mm、cm、inch 等。而 tiny-skia 的 `Transform` / `Path` 只认 `f32` 数字，本身没有单位概念。两者要对接，就必须有一个「把 `Abs` 拍扁成一个 `f32`」的转换。

typst-render 用一个本地 trait [`AbsExt`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L277-L286) 来承担这件事。它的唯一方法 `to_f32(self) -> f32` 约定：**把任意单位的 `Abs` 换算成「以 pt 为单位的 f32」**。为什么选 pt？因为 typst-render 在 `render()` 入口已经用 `from_scale(pixel_per_pt)` 把「1 个用户单位 = 1 pt」钉死了——pt 是 typst-layout 产出坐标与 tiny-skia 用户空间之间的共同语言。

#### 4.2.2 核心流程

换算链路非常直接：

```
Abs（可能是 mm/cm/inch/pt）
   │  AbsExt::to_f32
   ▼
self.to_pt()            // typst-library 提供：换算成 pt 的 f64
   │  as f32
   ▼
f32（以 pt 为单位）      // tiny-skia 的用户空间坐标
```

之后这个 f32 会被 `from_scale(pixel_per_pt)` 进一步乘成像素——但那一步发生在已经叠加好的 `transform` 里，`AbsExt` 只负责到 pt 为止。

#### 4.2.3 源码精读

[`AbsExt`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L277-L286) 的定义和实现只有几行：

```rust
trait AbsExt {
    /// Convert to a number of points as f32.
    fn to_f32(self) -> f32;
}

impl AbsExt for Abs {
    fn to_f32(self) -> f32 {
        self.to_pt() as f32
    }
}
```

关键点：

- 这是一个**本地 trait**（没有导出），只服务于 typst-render 内部——因为只有 typst-render 需要把 Typst 长度和 tiny-skia 的 `f32` 对接。
- 实现体就是 `self.to_pt() as f32`：`Abs::to_pt()` 由 typst-library 提供，返回换算成 pt 的 `f64`；再 `as f32` 降到 tiny-skia 使用的精度。
- `f64 → f32` 的降精度是刻意的：tiny-skia 内部全程 `f32`，统一精度避免反复转换的开销。

这个 trait 在全 crate 被大量使用，例如 [src/text.rs:69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L69) `text.size.to_f32()`、[src/shape.rs:20](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L20) `size.x.to_f32()`、[src/image.rs:25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L25) `size.x.to_f32()`、以及本讲的 [`to_sk_transform`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L264-L274) 里对 `tx`/`ty` 的转换。可以说，**凡是把 Typst 长度喂给 tiny-skia 的地方，都先过 `AbsExt::to_f32`**。

#### 4.2.4 代码实践

1. **实践目标**：直观感受「`Abs` 自带单位，`to_f32` 负责归一」。
2. **操作步骤**：
   - 用 `Grep` 在 `crates/typst-render/src/` 下搜索 `\.to_f32\(\)`，统计调用次数（应超过 30 处）。
   - 任取 [src/shape.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs) 里一处 `.to_f32()`（例如矩形的宽高），确认它前面的变量类型是 `Abs`，后面被喂给了 `sk::PathBuilder` 的 `f32` 参数。
3. **需要观察的现象**：`to_f32` 像一道「单位关卡」，把带单位的 Typst 长度统一变成 pt-f32 后才交给 tiny-skia。
4. **预期结果**：你会理解为什么 typst-render 几乎从不直接用 `Abs` 的原始数值，而是总先 `.to_f32()`。
5. **备注**：源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AbsExt::to_f32` 选 pt 作为换算目标，而不是像素或 mm？

> **参考答案**：因为 typst-layout 产出的所有坐标都以 pt 为基准，而 `render()` 一开始就用 `from_scale(pixel_per_pt)` 把 tiny-skia 用户空间的「1 单位」定义为 1 pt。所以 pt 是 Typst 坐标与 tiny-skia 用户空间之间唯一的共同单位；像素换算（乘 `pixel_per_pt`）是之后由 `transform` 统一完成的，不该在 `AbsExt` 这一层做。

**练习 2**：`self.to_pt() as f32` 里的 `as f32` 会损失精度，为什么 typst-render 仍这样做？

> **参考答案**：tiny-skia 全程使用 `f32`。如果 `AbsExt` 返回 `f64`，每次喂给 tiny-skia 都要再转一次 `f32`，徒增开销且容易在各处重复。在源头一次性降成 `f32`，与下游渲染器精度一致，对像素级渲染而言精度损失可以忽略。

---

### 4.3 to_sk_transform：Typst 仿射矩阵 → tiny-skia Transform

#### 4.3.1 概念说明

Typst 有自己的仿射变换类型 `Transform`（定义在 typst-library，排版层用它描述 `move`/`rotate`/`scale`/`skew`）；tiny-skia 也有一个 `sk::Transform`（渲染层用它真正做矩阵运算）。两者都是「2×3 仿射矩阵」，数学含义相同，但字段命名和构造方式不同。渲染时必须把前者翻译成后者——这正是 [`to_sk_transform`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L264-L274) 的职责。

仿射变换把一个点 \((x, y)\) 映射到 \((x', y')\)，公式是：

\[
\begin{aligned}
x' &= sx \cdot x + kx \cdot y + tx \\
y' &= ky \cdot x + sy \cdot y + ty
\end{aligned}
\]

写成矩阵（第三列是平移）：

\[
\begin{pmatrix} x' \\ y' \end{pmatrix}
= \begin{pmatrix} sx & kx \\ ky & sy \end{pmatrix}
  \begin{pmatrix} x \\ y \end{pmatrix}
+ \begin{pmatrix} tx \\ ty \end{pmatrix}
\]

其中 `sx`/`sy` 是 x/y 方向的缩放，`kx`/`ky` 是倾斜（剪切），`tx`/`ty` 是平移。Typst 和 tiny-skia **采用完全相同的矩阵排布**，所以转换是逐字段拷贝——但有一个坑：tiny-skia 的构造函数 `from_row` 的参数顺序是「交错」的，下文会展开。

#### 4.3.2 核心流程

```
Typst Transform { sx, ky, kx, sy, tx, ty }   （sx/ky/kx/sy 是 Ratio 无量纲，tx/ty 是 Abs 长度）
   │  to_sk_transform
   ▼
Ratio.get() as f32   →  sx/ky/kx/sy 的 f32
Abs.to_f32()         →  tx/ty 的 f32（pt）
   │
   ▼
sk::Transform::from_row(sx, ky, kx, sy, tx, ty)   ← 注意参数顺序是交错的
```

这里有两类单位换算：缩放/倾斜系数 `sx/ky/kx/sy` 是 `Ratio`（无量纲比值），用 `.get() as f32` 取值；平移 `tx/ty` 是 `Abs`（长度），用 [`AbsExt::to_f32`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L282-L285) 换算成 pt。

#### 4.3.3 源码精读

[`to_sk_transform`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L264-L274) 的实现：

```rust
fn to_sk_transform(transform: &Transform) -> sk::Transform {
    let Transform { sx, ky, kx, sy, tx, ty } = *transform;
    sk::Transform::from_row(
        sx.get() as f32,   // 第 1 个参数
        ky.get() as f32,   // 第 2 个参数
        kx.get() as f32,   // 第 3 个参数
        sy.get() as f32,   // 第 4 个参数
        tx.to_f32(),       // 第 5 个参数
        ty.to_f32(),       // 第 6 个参数
    )
}
```

**关于参数顺序的关键结论**：`from_row` 的参数顺序是 \((sx,\ ky,\ kx,\ sy,\ tx,\ ty)\)——即「x 缩放、y 倾斜、x 倾斜、y 缩放、x 平移、y 平移」，**不是**按行展开的 \((sx, kx, tx, ky, sy, ty)\)。这很容易写反。我们怎么确信这个顺序是对的？靠两个互相印证的证据：

**证据一：旋转矩阵的形状。** Typst 的 [`Transform::rotate`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/transform.rs#L291-L301) 构造出的字段是 `sx=cos, ky=sin, kx=-sin, sy=cos`：

```rust
pub fn rotate(angle: Angle) -> Self {
    let cos = Ratio::new(angle.cos());
    let sin = Ratio::new(angle.sin());
    Self {
        sx: cos,
        ky: sin,
        kx: -sin,
        sy: cos,
        ..Self::default()
    }
}
```

代入本讲的矩阵公式：\(x' = \cos\theta\cdot x - \sin\theta\cdot y\)，\(y' = \sin\theta\cdot x + \cos\theta\cdot y\)——这正是标准的**逆时针旋转矩阵**：

\[
\begin{pmatrix} \cos\theta & -\sin\theta \\ \sin\theta & \cos\theta \end{pmatrix}
\]

说明 `sx/kx` 在第一行、`ky/sy` 在第二行，与本节开头的矩阵排布一致。

**证据二：代码能正确渲染旋转。** 既然 Typst 的文档里 `#rotate(30deg)[文本]` 渲染出来确实是逆时针 30 度，而这条路径必然经过 `to_sk_transform`，那么 `from_row` 的参数顺序就**必须**是上面那个交错顺序——否则 `ky`（sin）会被塞进 `kx` 的位置，旋转方向和角度都会错乱。

> 小贴士：tiny-skia 的 `Transform` 字段也叫 `sx/ky/kx/sy/tx/ty`，且 [`render_outline_glyph`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L50-L60) 里直接读取 `ts.sy`、`ts.kx`、`ts.ky`、`ts.sx` 来判断「是否有非均匀缩放/倾斜」，进一步印证了字段对应关系。

**关于 `Transform` 本身的字段语义**，可对照 typst-library 的定义 [crates/typst-library/src/layout/transform.rs:244-251](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/transform.rs#L244-L251)：

```rust
pub struct Transform {
    pub sx: Ratio,
    pub ky: Ratio,
    pub kx: Ratio,
    pub sy: Ratio,
    pub tx: Abs,
    pub ty: Abs,
}
```

`sx/ky/kx/sy` 是无量纲 `Ratio`，`tx/ty` 是带单位的 `Abs`——这正是 `to_sk_transform` 对它们采用两种不同取值方式（`.get()` vs `.to_f32()`）的原因。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证 `from_row` 的参数顺序，而不是死记。
2. **操作步骤**：
   - 读 [`Transform::rotate`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/transform.rs#L291-L301)，写下它生成的 `sx/ky/kx/sy` 四个值。
   - 代入 4.3.1 的矩阵公式，化简出 \(x'\)、\(y'\) 的表达式，确认它等价于标准逆时针旋转。
   - 再看 [`to_sk_transform`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L264-L274)，确认 `from_row` 收到的第 2 个参数是 `ky`（不是 `kx`）。
3. **需要观察的现象**：`ky`（=sin）排在 `kx`（=−sin）之前，这正是「交错顺序」的体现。
4. **预期结果**：你能用自己的话解释「为什么把 `ky` 放第 2 个参数位是对的」——因为 tiny-skia `from_row(a,b,c,d,e,f)` 把 `b` 当作矩阵第二行第一列（即 `ky`）。
5. **备注**：本节涉及的精确矩阵乘法请继续在 4.4 节推导；此处只需建立「逐字段、交错序」的直觉。

#### 4.3.5 小练习与答案

**练习 1**：一个纯平移 `Transform::translate(10pt, 20pt)`，经过 `to_sk_transform` 后，得到的 `sk::Transform` 中哪些字段是非零的？

> **参考答案**：`translate` 的 `sx=sy=1`、`kx=ky=0`、`tx=10pt`、`ty=20pt`（见 [`Transform::translate`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/transform.rs#L267-L269)）。转换后 `sk::Transform` 的 `sx=1.0`、`sy=1.0`、`kx=0.0`、`ky=0.0`、`tx=10.0`（pt）、`ty=20.0`（pt）。非零的是 `sx`、`sy`、`tx`、`ty`。

**练习 2**：如果把 `to_sk_transform` 里 `from_row` 的第 2、3 个参数写反（即传成 `kx, ky`），旋转会出现什么错误？

> **参考答案**：`ky`（y 行 x 列，=sin）和 `kx`（x 行 y 列，=−sin）会被互换，旋转矩阵从 \(\begin{pmatrix}\cos&-\sin\\\sin&\cos\end{pmatrix}\) 变成 \(\begin{pmatrix}\cos&\sin\\-\sin&\cos\end{pmatrix}\)，即顺时针旋转、且角度的「正方向」反转。所有 `rotate` 出来的方向都会反掉。

---

### 4.4 pre_translate / pre_scale / pre_concat：坐标变换如何层层叠加

#### 4.4.1 概念说明

有了 `transform` 字段，接下来要解决「每深入一层树，如何把新变换叠加到旧 `transform` 上」。typst-render 用三个薄包装方法：[`pre_translate`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L142-L147)（叠加平移）、[`pre_scale`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L149-L154)（叠加缩放）、[`pre_concat`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L157-L162)（叠加任意 `sk::Transform`）。它们都委托给 tiny-skia 的 `pre_concat`，所以理解了 `pre_concat` 的矩阵语义，三者就都通了。

核心语义（下文会用 `render()` 的实例证明）：**`A.pre_concat(B)` 得到的合成变换，对点的作用顺序是「先 B，后 A」**。用复合函数记号写就是 \(A \circ B\)，即 \((A \circ B)(p) = A(B(p))\)。换句话说，新叠加上来的 `B` 离被绘制的点更近（更内层），原有的 `A` 离画布更近（更外层）。这正是 `pre-`（前缀）的含义——「在已有变换**之前**（更靠近点的一侧）插入」。

这条语义有个直接推论，也是本讲实践任务的核心：**链式调用 `state.pre_translate(...).pre_scale(...)` 时，写在后面的 `pre_scale` 反而先作用于点**。代码顺序与作用顺序是相反的——因为每多写一个 `pre_*`，就把新变换「塞到更内层」。

#### 4.4.2 核心流程

先用 `render()` 的真实代码证明 `pre_concat` 的语义，再给出一般化的叠加规则。

**证明**：[`render()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L29-L30) 先建纯缩放 `ts = from_scale(pixel_per_pt)`，它把 pt 乘以 `pixel_per_pt` 得到像素。接着 [src/lib.rs:43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L43) 做 `state.pre_translate(bleed)`，等价于 `ts.pre_concat(T_bleed)`，其中 \(T_{bleed}\) 是「按出血量平移」。我们要求的是：一个「出血区相对」的内容点 \(p\)（单位 pt）应当落在画布的哪个像素？

物理上，内容坐标系的原点应当出现在「画布上出血量的像素位置」，即期望结果是：

\[
p \;\mapsto\; \text{pixel\_per\_pt} \cdot (p + \text{bleed})
\]

也就是「先把 \(p\) 平移 bleed，再整体乘 `pixel_per_pt`」。而 `ts.pre_concat(T_bleed)` 若按「先 \(T_{bleed}\) 后 `ts`」作用，恰是 \(\text{ts}(T_{bleed}(p)) = \text{pixel\_per\_pt}\cdot(p + \text{bleed})\)，与期望吻合。

> 注意这里的「平移量 bleed」用的是 **pt**（因为 `pos.x.to_f32()` 经 [`AbsExt`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L282-L285) 换算成 pt），它处在 `ts`（pt→像素缩放）的**内层**，所以 bleed 也被 `ts` 缩放成了 `pixel_per_pt × bleed` 个像素——这正是「平移量会随外层缩放而缩放」的体现，也是 `pre_translate` 的精髓：**平移发生在内层局部坐标系里**。

**一般叠加规则**：若从初始变换 \(T_0\) 出发，依次做 `pre_concat(B₁)`、`pre_concat(B₂)`、……、`pre_concat(Bₙ)`，则最终

\[
T_{\text{final}} = T_0 \circ B_1 \circ B_2 \circ \cdots \circ B_n
\]

对点 \(p\) 的作用顺序是「\(B_n\) 先，……，\(B_1\) 中间，\(T_0\) 最后」。每个 `pre_concat` 都把新变换塞到最内层。

#### 4.4.3 源码精读

三个方法的实现都是「更新 `transform` 字段、其余字段用 `..self` 保留」的标准不可变更新：

```rust
/// Pre translate the current item's transform.
fn pre_translate(self, pos: Point) -> Self {
    Self {
        transform: self.transform.pre_translate(pos.x.to_f32(), pos.y.to_f32()),
        ..self
    }
}

fn pre_scale(self, scale: Axes<Abs>) -> Self {
    Self {
        transform: self.transform.pre_scale(scale.x.to_f32(), scale.y.to_f32()),
        ..self
    }
}

/// Pre concat the current item's transform.
fn pre_concat(self, transform: sk::Transform) -> Self {
    Self {
        transform: self.transform.pre_concat(transform),
        ..self
    }
}
```

注意它们只改 `transform`，**完全不动** `container_transform` / `mask` / `pixel_per_pt` / `size`——后者的更新由 `pre_concat_container` / `with_mask` / `with_size` 单独负责（详见 u2-l2、u3-l1）。这种「一个方法只动一个字段」的分工让递归中的状态变化可预测。

`pre_translate` / `pre_scale` 接收的是 Typst 的带单位类型（`Point`、`Axes<Abs>`），内部用 [`AbsExt::to_f32`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L282-L285) 转成 pt-f32 后，再交给 tiny-skia 的 `sk::Transform::pre_translate` / `pre_scale`——后者本质也是 `pre_concat(from_translate(...))` / `pre_concat(from_scale(...))`。

一个真实的「两层叠加」出现在 [`render_text`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L15-L41) 里，正好是本讲实践任务的现实原型：

```rust
let state = state
    .pre_translate(Point::new(x_offset, -y_offset))   // 外层：把笔移到字形位置
    .pre_scale(Axes::new(text_scale, text_scale));     // 内层：从字体单位(upem)缩放到 pt
```

这里 `pre_translate` 在外（先写），`pre_scale` 在内（后写）。按本节的规则，对一个**字体单位**坐标 \(g\)（glyph 设计空间，通常是 Y 向上），作用顺序是「先 `pre_scale`（把 upem 缩成 pt），再 `pre_translate`（搬到字形落笔位置），最后 `state.transform`（页→像素）」。也就是说：

\[
\text{transform}(g) \;=\; T_0\big(\,\text{text\_scale}\cdot g \;+\; (x_{\text{off}},\,-y_{\text{off}})\,\big)
\]

其中 \(T_0\) 是进入这个字形前的 `state.transform`。这正是「内层 scale 先生效、外层 translate 后生效」的体现。（`-y_offset` 的负号是因为字体设计坐标 Y 向上、而画布 Y 向下，需要翻转——细节属于 u2-l6，本讲只需关注叠加顺序。）

另一处真实叠加在 [`render_group`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L208-L223) 的软帧分支：

```rust
FrameKind::Soft => state.pre_translate(pos).pre_concat(sk_transform),
```

即「先按组的相对位置 `pos` 平移，再叠加组自带的变换 `sk_transform`」——同样是 `pre_concat` 把组变换塞到内层。硬帧（Hard）分支还要额外更新 `container_transform`，留待 u2-l2。

#### 4.4.4 代码实践

这是本讲的核心实践，对应规格里的两步推导。

**第 1 步：两层嵌套的坐标变换推导。**

设进入某层时的初始 `state.transform = T₀`（在 [`render()`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L29-L30) 里 \(T_0 = \text{from\_scale}(\text{pixel\_per\_pt})\)；若想简化可令 \(T_0 = I\) 单位矩阵）。现在依次执行：

- 外层：`state₁ = state.pre_translate((10, 10))`
- 内层：`state₂ = state₁.pre_scale(Axes::new(2.0, 2.0))`（`pre_scale` 收 `Axes<Abs>`，其 x/y 经 `.to_f32()` 取出 `2.0` 作为缩放系数）

请先自己在纸上画一张「层」的示意图：最外层是画布（像素），中间一层是 `T₀`，再内一层是平移 `(10,10)`，最内层是缩放 `2`，最里面是被绘制的局部点 \(p\)。然后完成：

1. 写出 `state₁.transform` 用复合函数表示的表达式。
2. 写出 `state₂.transform` 的表达式。
3. 写出对局部点 \(p=(p_x,p_y)\) 的最终映射（先令 \(T_0=I\)，再令 \(T_0=\text{from\_scale}(r)\)，\(r=\text{pixel\_per\_pt}\)）。

**参考推导**：

- `state₁.transform = T₀.pre_concat(translate(10,10))`，按「先内层后外层」即 \(T_0 \circ T_{(10,10)}\)。
- `state₂.transform = state₁.transform.pre_concat(scale(2,2)) = T_0 \circ T_{(10,10)} \circ S_{(2,2)}\)。
- 对点 \(p\)：

\[
\text{transform}(p) = T_0\big(T_{(10,10)}\big(S_{(2,2)}(p)\big)\big)
= T_0\big((2p_x,\,2p_y) + (10,10)\big)
= T_0(2p_x+10,\; 2p_y+10)
\]

  - 当 \(T_0 = I\)（单位）：\((2p_x+10,\;2p_y+10)\)。
  - 当 \(T_0 = \text{from\_scale}(r)\)（`render()` 的真实初值）：\((r(2p_x+10),\; r(2p_y+10))\) 像素。

> 结论速记：**代码里写在前面的 `pre_translate` 作用在后，写在后面的 `pre_scale` 作用在先**；缩放只作用于「更内层的局部量」（这里是 \(p\) 和紧贴它的平移 `(10,10)`），`T₀` 在最外层统一把 pt 换成像素。

1. **实践目标**：用推导（而非死记）掌握 `pre_*` 链的作用顺序。
2. **操作步骤**：
   - 先在纸上独立完成上述 1–3 小题，再对照参考推导。
   - 打开 [`render_text`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L29-L31)，把那里的 `pre_translate(x_offset,-y_offset).pre_scale(text_scale)` 中的 `x_offset`/`text_scale` 代入上面的模板，复述「字形字体单位坐标」是如何被 scale 再 translate 最终落到页面的。
3. **需要观察的现象**：无论怎么组合，都符合「`pre_*` 链越靠后越内层、越先作用于点」的规律。
4. **预期结果**：你能对任意 `pre_*` 链当场写出对局部点的映射表达式。
5. **备注**：本实践为纯纸笔推导 + 源码对照，无需运行；若想验证，可在本地给 `render_text` 临时打印 `state.transform` 的六个分量观察（属可选的本地验证）。

**第 2 步：复述 `to_sk_transform` 的 `from_row` 映射。**

结合 4.3 节，用自己的话回答：`to_sk_transform` 是如何把 `Transform` 的 `sx/ky/kx/sy/tx/ty` 映射到 `sk::Transform::from_row` 的？（要点：逐字段、参数交错序、`Ratio` 用 `.get() as f32`、`Abs` 用 `.to_f32()`。）答案见 4.3.5。

#### 4.4.5 小练习与答案

**练习 1**：若把 `state.pre_translate((10,10)).pre_scale((2,2))` 改写成 `state.pre_scale((2,2)).pre_translate((10,10))`（交换两行顺序），最终变换对点 \(p\) 的映射变成什么？和原来差在哪？

> **参考答案**：交换后是 \(T_0 \circ S_{(2,2)} \circ T_{(10,10)}\)，作用顺序「先平移 (10,10)，再缩放 2，再 \(T_0\)」，即 \(\text{transform}(p)=T_0(S_{(2,2)}(p+(10,10)))=T_0(2(p_x+10),\,2(p_y+10))=T_0(2p_x+20,\,2p_y+20)\)。和原来的 \(T_0(2p_x+10,\,2p_y+10)\) 相比，平移量也被放大了 2 倍（变成 20）。这正是「平移处在缩放的内层还是外层，决定了平移量是否被缩放」——`pre_translate` 把平移放在内层（不被后续外层缩放影响本层，但会被更外层 \(T_0\) 影响）；调换顺序后平移跑到缩放外层，缩放就会作用到它身上。

**练习 2**：为什么 `render_frame` 里给每个叶子元素都用 `state.pre_translate(*pos)` 生成**新** state，而不是直接修改 `state`？

> **参考答案**：因为同一个父帧的 `state` 要被该帧下的**所有** `item` 复用。每个 `item` 的 `pos` 不同，必须各自得到一份「叠加了自己位置」的 state 传给子模块。由于 `State` 是 `Copy` 且 `pre_*` 返回新值，「原地不改、各算一份」天然成立；若直接修改 `state`，前一个 `item` 的平移会污染后一个 `item`，导致所有元素叠在同一位置。

**练习 3**：`pre_concat` 与 typst-library 里 `Transform::post_concat`（见 [crates/typst-library/src/layout/transform.rs:346-348](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/transform.rs#L346-L348)）的语义有什么不同？

> **参考答案**：`pre_concat(other)` 把 `other` 放在**内层**（先作用于点），合成结果 \( \text{self} \circ \text{other} \)；`post_concat(next)` 等价于 `next.pre_concat(self)`，把 `next` 放在**外层**（后作用于点），合成结果 \( \text{next} \circ \text{self} \)。两者方向相反。typst-render 的 `State` 主要用 `pre_*`（因为渲染是「自顶向下深入场景树」，每深一层新变换都更靠近点）；而 `render_group` 的硬帧分支在更新 `container_transform` 时会用到 `post_concat`/`invert`（见 [src/lib.rs:213-L221](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L213-L221)，u2-l2 详讲）。

---

## 5. 综合实践

把本讲的四个最小模块（`State`、`AbsExt`、`to_sk_transform`、`pre_*`）串起来，完成下面这个「追踪一个字形变换」的贯穿性任务。

**任务：推导 `render_text` 里一个彩色字形（走 `glyph_frame` 分支）从「字体单位坐标」到「画布像素」的完整变换。**

背景：[`render_text`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L15-L41) 对不需要轮廓化的字形（如彩色 emoji）执行：

```rust
let upem = text.font.units_per_em();          // 字体的 units-per-em（字形单位个数，如 1000）
let text_scale = text.size / upem;            // text.size 是 Abs，结果是 Abs（≈ 0.012pt）
let state = state
    .pre_translate(Point::new(x_offset, -y_offset))
    .pre_scale(Axes::new(text_scale, text_scale));
if let Some(frame) = glyph_frame(&text.font, glyph.id) {
    crate::render_frame(canvas, state, &frame.into());
}
```

设进入这个字形时 `state.transform = T₀`（页面级累积变换，最外层含 `pixel_per_pt` 缩放），`text.size = 12pt`，字体 `upem = 1000`，`x_offset = 5pt`，`y_offset = 2pt`。请完成：

1. **换算单位**：先判断 `text_scale` 的类型（提示：看 [`pre_scale`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L149-L154) 的签名 `Axes<Abs>`，所以 `text_scale` 是 `Abs` 而非 `Ratio`），再用 [`AbsExt`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L282-L285) 算出它的 `.to_f32()` 值；`x_offset`、`y_offset` 经 `.to_f32()` 后是多少（单位是什么）。
2. **写变换链**：按本节 4.4.2 的规则，写出 `state.transform` 作为 \(T_0\) 与各 `pre_*` 的复合表达式（形如 \(T_0 \circ \ldots\)）。
3. **映射一个点**：设该 emoji 子帧里有一个设计坐标 \(g = (100, 200)\)（字体单位），求它在画布上的落点表达式（用 \(T_0\) 表示即可，不必算出像素数值）。
4. **映射工具**：指出本路径（`pre_translate`/`pre_scale`）是直接对已有的 `sk::Transform` 操作，还是经过 [`to_sk_transform`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L264-L274)？`text_scale` 与 `x_offset` 这两个值分别是什么类型、各走哪条「换算通道」（`.to_f32()` 还是 `.get() as f32`）进入 tiny-skia？

**参考思路**（建议先自己做再对照）：

1. `text_scale = text.size / upem = 12pt / 1000 = 0.012`，**类型是 `Abs`**（0.012 pt，因为 `pre_scale` 收 `Axes<Abs>`），其 `.to_f32() = 0.012`。`x_offset.to_f32() = 5.0`、`-y_offset → -2.0`，**单位是 pt**（`AbsExt::to_f32` 换算成 pt-f32）。
2. 链：`state.transform = T₀.pre_concat(translate(5, -2)).pre_concat(scale(0.012, 0.012))`，即 \(T_0 \circ T_{(5,-2)} \circ S_{(0.012,\,0.012)}\)。
3. 对 \(g=(100,200)\)：先 scale → \((1.2,\,2.4)\)（pt），再 translate → \((1.2+5,\;2.4-2)=(6.2,\,0.4)\)（pt），最后 \(T_0\)（含 `pixel_per_pt` 缩放）把它换成画布像素：\(\text{transform}(g)=T_0(6.2,\,0.4)\)。
4. 本路径（`pre_translate`/`pre_scale`）**不经过** `to_sk_transform`——它直接对已有的 `sk::Transform`（即 `state.transform`）操作，内部对 `text_scale`、`x_offset`、`y_offset` 这些 `Abs` 值统一调用 `AbsExt::to_f32` 得到 pt-f32，再喂给 tiny-skia 的 `pre_translate`/`pre_scale`。`to_sk_transform` 只在 `render_group` 处理 `group.transform`（以及 paint 的 tiling）时才登场，负责把 Typst 的 `Transform` 逐字段翻译成 `sk::Transform`；在那里 `Ratio` 字段（`sx/ky/kx/sy`）走 `.get() as f32`、`Abs` 字段（`tx/ty`）走 `.to_f32()`。注意 `text_scale` 数值虽是 0.012，**类型是 `Abs`**，所以也走 `.to_f32()` 而非 `.get()`。

> 这个任务之所以能一气呵成，是因为 typst-render 的坐标体系严格分层：**最外层 `T₀` 负责 pt→像素，中间层负责版面定位（translate），最内层负责把字体的设计单位缩放到 pt（scale）**。每一层都是一个 `pre_*`，层层向内。

## 6. 本讲小结

- [`State`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L116-L128) 是渲染递归的「随身背包」，五个字段里 `transform` 是本讲主角（其余 `container_transform`/`mask`/`size` 服务 u2-l2、u3-l1，`pixel_per_pt` 是分辨率）。它派生 `Copy`，所有 `pre_*`/`with_*` 方法走「不可变更新」，让同一父帧的 state 可被多个子元素安全复用。
- [`AbsExt::to_f32`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L277-L286) 把任意单位的 `Abs` 统一换算成「pt-f32」，是 Typst 长度与 tiny-skia `f32` 之间唯一的单位关卡；凡把长度喂给 tiny-skia 的地方都先过它。
- [`to_sk_transform`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L264-L274) 把 Typst 的 `Transform` 逐字段映射到 tiny-skia 的 `Transform::from_row`；矩阵排布相同（由 `rotate` 的 \(\begin{pmatrix}\cos&-\sin\\\sin&\cos\end{pmatrix}\) 印证），但 `from_row` 的参数顺序是交错的 \((sx,ky,kx,sy,tx,ty)\)，易写反。
- `Ratio` 字段（`sx/ky/kx/sy`）用 `.get() as f32`，`Abs` 字段（`tx/ty`）用 `.to_f32()`。
- `A.pre_concat(B)` 的语义是「先 B 后 A」，即 \(A \circ B\)；链式调用 `pre_*` 时，**写在前面的作用在后，写在后面的作用在先**（更内层、更靠近被绘制的点）。
- 真实叠加示例：[`render_text`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L29-L31) 的 `pre_translate(x_offset,-y_offset).pre_scale(text_scale)` 把「字体单位 → pt 缩放」放最内层、「字形落笔平移」放中层、「页→像素 \(T_0\)」放最外层。

## 7. 下一步学习建议

本讲把 `State` 的 `transform` 讲透了，但刻意回避了 `container_transform`、`mask`、`size` 的更新细节。接下来：

1. **[u2-l2 Group 渲染、裁剪与遮罩](u2-l2-groups-clipping-masks.md)**：把本讲只点到为止的 `render_group` 彻底展开——`FrameKind::Soft`/`Hard` 如何分别更新 `transform` 与 `container_transform`（硬帧那条用了 `post_concat`/`invert` 的链），以及裁剪曲线如何转成 `sk::Mask` 并经 `with_mask` 向下传递。这一篇会直接用到本讲的 `pre_concat` 语义。
2. **u2-l3 纯色 Paint 转换**：`transform` 确定后，下一步是「用什么颜色/画笔填」。`to_sk_paint` 会读取 `state` 的多个字段，本讲的 `State` 认知是前置。
3. **u3-l1 渐变填充（进阶）**：那里会真正用到 `container_transform`、`size` 与 `pixel_per_pt`，解释「渐变相对父容器」的坐标换算——本讲为它们埋好了伏笔。

建议你带着两个问题去读 u2-l2：(a) 硬帧分支里 `state.transform.post_concat(state.container_transform.invert())` 为什么能「重置」容器变换？(b) 遮罩为什么是「父遮罩与子裁剪求交」？这两个问题都建立在本讲的矩阵语义之上。
