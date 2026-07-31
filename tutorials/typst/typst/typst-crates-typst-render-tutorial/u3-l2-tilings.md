# 平铺图案 Tiling

## 1. 本讲目标

本讲紧接 u3-l1（渐变填充），讲 typst-render 如何把 Typst 的**平铺图案（Tiling）**画进像素画布。平铺图案就是你熟悉的「壁纸」：一张小图（瓦片 / tile）在填充区域内无限重复。读完本讲你应当能够：

- 说清平铺与渐变同源——它们都遵循「形状走 Pattern 着色器、文本快路径走逐像素采样」的**两条路径**分流，只是平铺把「位置→颜色」换成了「位置→瓦片像素」。
- 推导 `TilingSampler::sample` 如何用 `rem_euclid`（欧几里得取模）把任意画布像素**折叠回**瓦片纹理内的一个像素，从而实现 `SpreadMode::Repeat` 的周期性重复。
- 读懂 `render_tiling_frame` 如何递归复用主派发函数 `render_frame`，把一个图案 `Frame` 光栅化成一张独立纹理，并解释 `spacing`（间距）为何要加进纹理尺寸。
- 解释 `to_sk_paint(Paint::Tiling)` 分支里 `fill_transform`、`pattern_transform`、`pre_scale(1/pixel_per_pt)`、`base_offset` 是如何一层层叠加，把瓦片纹理贴到正确位置上的。

本讲是 u3-l1 的直接后继，并预告 u3-l3（字形光栅化与像素级混合）和 u3-l4（记忆化与性能）。

## 2. 前置知识

进入平铺之前，请确认你已经理解下面这些在前置讲义中建立的概念：

- **Paint 与 to_sk_paint**：`Paint` 分 `Solid`（纯色）、`Gradient`（渐变）、`Tiling`（平铺）三种；`to_sk_paint(paint, state, on_text, pixmap, shape, include_stroke_in_bbox)` 是统一转换入口（见 u2-l3）。
- **PaintSampler trait**：把「在画布像素 \((x,y)\) 处取一个 `PremultipliedColorU8` 颜色」抽象成 `sample(pos)`，让 `write_bitmap` 用同一份循环代码服务纯色、渐变、平铺三种文本填充（见 u3-l1）。
- **State 状态背包**：含 `transform`（当前局部→画布）、`container_transform`（首个硬帧→画布）、`size`（首个硬帧尺寸）、`pixel_per_pt`（见 u2-l1、u2-l2）。
- **preconcat / post_concat 语义**：`A.pre_concat(B)` = 「先 B 后 A」= \(A \circ B\)；`A.post_concat(B)` = 「先 A 后 B」= \(B \circ A\)（见 u2-l1）。
- **RelativeTo::Self_ 与 Parent**：渐变/平铺的坐标系参考——`Self_` 相对被填充对象自身的包围盒，`Parent` 相对父容器（首个硬帧），二者决定 `fill_transform` 与 `container_size` 的取值（见 u3-l1）。
- **文本快/慢路径**：`render_outline_glyph` 在大字号、有描边、非均匀缩放时走「路径绘制」慢路径，否则走 `pixglyph` 光栅化快路径；只有快路径才用 `PaintSampler`（见 u2-l6）。

**关键直觉**：渐变是「颜色随位置连续变化」，平铺是「颜色随位置**周期性**变化」——走出一小块（瓦片）后，颜色序列原地重复。所以平铺的渲染机制几乎与渐变同构，只多了一个「取模回卷」动作。理解了 u3-l1，本讲的难点就只剩「这个回卷是怎么用 `rem_euclid` 算出来的」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/paint.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs) | 平铺渲染的主战场：`PaintSampler` trait、`TilingSampler`、`to_sk_paint` 的 `Paint::Tiling` 分支、`render_tiling_frame` |
| [src/text.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs) | 文本快路径如何为 `Paint::Tiling` 构造 `TilingSampler`，并在 `write_bitmap` 里逐像素采样 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs) | `State` 结构体与 `render_frame` 主派发（被 `render_tiling_frame` 反向复用） |
| ../typst-library/src/visualize/tiling.rs | `Tiling` 的定义及其 `size()` / `spacing()` / `transform()` / `frame()` / `unwrap_relative()` 方法 |

---

## 4. 核心概念与源码讲解

### 4.1 平铺的两条路径与 PaintSampler

#### 4.1.1 概念说明

平铺图案（`Tiling`）在 Typst 里由四样东西定义（见 `TilingInner`）：

- `frame`：瓦片的内容，本身是一棵普通的 `Frame` 场景树（可以是任意排版内容：方块、圆、甚至另一段文字）。
- `size`：单个瓦片的尺寸。
- `spacing`：瓦片之间的间距（gap）。
- `offset` + `angle`：整片瓦片网格的平移与旋转（合成 `transform()`）。

和渐变一样，平铺是「位置函数」——画布上每个像素要问：「这个位置落在瓦片纹理里的哪个像素？」而平铺比渐变多一层「周期性」：位置可以无限延伸，但颜色序列每走过一个瓦片就重复一次。

typst-render 用与渐变完全对称的**两条路径**来回答这个问题：

1. **形状路径（含慢路径文本）**：先把瓦片内容**光栅化成一张 `Pixmap` 纹理**，再用 tiny-skia 的 `Pattern` 着色器贴回形状，由 tiny-skia 用 `SpreadMode::Repeat` 负责无限重复。
2. **文本快路径**：`pixglyph` 只给字形覆盖率位图，tiny-skia 无法直接填，于是 typst-render 用 `TilingSampler` **自己逐像素采样**瓦片纹理，再手动与字形覆盖率做 alpha 混合。

为了让 `write_bitmap` 的逐像素循环对纯色、渐变、平铺三种填充都通用，typst-render 在 u3-l1 已经抽象出 `PaintSampler` trait。本讲的 `TilingSampler` 就是它在平铺场景下的第三个实现。

#### 4.1.2 核心流程

`PaintSampler` 的接口只有一个方法——「在画布像素 \((x,y)\) 处取一个颜色」：

```
trait PaintSampler: Copy {
    fn sample(self, pos: (u32, u32)) -> PremultipliedColorU8;
}
```

它目前有三个实现，覆盖文本快路径的全部填充类型：

| 实现 | 对应 Paint | `sample` 行为 |
|---|---|---|
| `PremultipliedColorU8` | `Solid` | 忽略位置，返回自己（常数函数） |
| `GradientSampler` | `Gradient` | 把像素映射回渐变空间采样（连续位置函数） |
| `TilingSampler` | `Tiling` | 把像素映射回瓦片空间并**取模回卷**采样（周期位置函数） |

文本快路径在 `render_outline_glyph` 里按 `Paint` 类型三分支选 sampler，再统一交给 `write_bitmap`，所以三者的 `sample` 签名必须一致。

#### 4.1.3 源码精读

`PaintSampler` trait 与纯色实现（常数采样器）定义在：

[crates/typst-render/src/paint.rs:11-22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L11-L22) —— 定义 `sample(pos) -> PremultipliedColorU8`；纯色实现 `fn sample(self, _: (u32,u32))` 无视坐标直接返回 `self`。

文本快路径的三分支分流在：

[crates/typst-render/src/text.rs:125-143](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L125-L143) —— `Paint::Gradient` 构造 `GradientSampler`、`Paint::Solid` 直接传预乘颜色、`Paint::Tiling` 先 `render_tiling_frame` 再构造 `TilingSampler`，三者都交给同一个 `write_bitmap(canvas, &bitmap, &state, sampler)`。

`write_bitmap` 在无遮罩分支里对每个像素调 `sampler.sample((x as u32, y as u32))` 取色：

[crates/typst-render/src/text.rs:223](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L223) —— 注意传给 `sample` 的 `(x, y)` 是**画布像素坐标**（不是瓦片局部坐标），这一点决定了 `TilingSampler` 必须自己做坐标换算。

#### 4.1.4 代码实践

**实践目标**：确认「同一份 `write_bitmap` 代码服务三种 Paint」，以及 `Paint::Tiling` 在文本路径里要额外多调一次 `render_tiling_frame`。

**操作步骤**：

1. 打开 [crates/typst-render/src/text.rs:148-239](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L148-L239)，找到 `write_bitmap<S: PaintSampler>`。
2. 确认它的循环体里**没有**任何针对 `Solid`/`Gradient`/`Tiling` 的分支，取色完全靠 `sampler.sample(...)` 这一个调用。
3. 对比 [crates/typst-render/src/text.rs:125-143](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L125-L143) 三个分支：`Solid` 与 `Gradient` 都只构造采样器，唯独 `Tiling` 多了一行 `let pixmap = paint::render_tiling_frame(&state, tiling);`。

**需要观察的现象**：平铺在文本路径里每遇到一个字形，都要先光栅化一次瓦片；而渐变/纯色不需要。这说明 `render_tiling_frame` 没有像渐变的 `cached` 那样被 `comemo::memoize`（详见 4.3.4 与 u3-l4）。

**预期结果**：你能指出「瓦片纹理的生成」是平铺相对渐变额外付出的成本，并能解释为什么 `write_bitmap` 本身仍然能保持三态通用。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PaintSampler` 要约束 `Copy`？

**参考答案**：`write_bitmap` 是泛型函数 `write_bitmap<S: PaintSampler>`，`sampler` 在内层逐像素循环里被反复调用 `sample`。约束 `Copy`（以及 `TilingSampler`/`GradientSampler` 派生 `Copy`）后，采样器按值复制即可，无需借用或克隆内部状态，调用开销最低。三个 sampler 都是小结构体（几个 `f32` / 引用），按值传递几乎免费。

**练习 2**：如果新增第四种 `Paint`（比如某种程序化纹理），需要改动 `write_bitmap` 吗？

**参考答案**：不需要改 `write_bitmap` 的循环体。只要为新 Paint 实现一个 `PaintSampler`，并在 [text.rs:125-143](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L125-L143) 的 match 里加一个分支构造它即可。这正是 `PaintSampler` 抽象的意义——「逐像素取色策略」与「字形位图混合逻辑」解耦。

---

### 4.2 render_tiling_frame：把图案帧光栅化成独立纹理

#### 4.2.1 概念说明

两条路径都需要同一份「瓦片纹理」——一张已经光栅化好的、包含单个瓦片像素的小 `Pixmap`。`render_tiling_frame` 就是生产这张纹理的函数，被形状路径与文本路径**共用**。

它的核心技巧是**「开一张独立画布，递归复用主派发」**：typst-render 不为「渲染瓦片」另写一套逻辑，而是临时造一个全新的 `State`（根状态），把瓦片的 `Frame` 当成一整页文档喂给现成的 `render_frame`。这与 u1-l3 里渲染整页 `Page` 的方式完全同构——本质上，一张瓦片就是一张极小的「子页面」。

#### 4.2.2 核心流程

`render_tiling_frame(state, tilings) -> Pixmap` 的流程：

1. 算纹理尺寸：`size = tilings.size() + tilings.spacing()`（瓦片尺寸 **加** 间距）。
2. 按 `pixel_per_pt` 把 `size` 换算成像素，开一张全新的 `Pixmap`（透明背景）。
3. 构造一个根 `State`：`transform = from_scale(pixel_per_pt, pixel_per_pt)`（即 pt→像素），`size = tilings.size()`（注意：是**不带间距**的纯瓦片尺寸）。
4. 调用 `crate::render_frame(&mut canvas, temp_state, tilings.frame())`，把瓦片内容画进这张画布。
5. 返回画布。

关键细节：**纹理画布是 `size + spacing` 大小，但内容只画在左上角 `size` 区域**，右侧/下侧的 `spacing` 留作透明边距。当这张纹理被无限平铺时，透明边距自然形成瓦片之间的 gap。

#### 4.2.3 源码精读

`render_tiling_frame` 全函数：

[crates/typst-render/src/paint.rs:296-309](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L296-L309) —— 注意三处对照：

- 第 297 行 `let size = tilings.size() + tilings.spacing();` —— 画布尺寸含间距。
- 第 298-302 行 `Pixmap::new(size.x.to_f32() * pixel_per_pt, ...)` —— 画布按含间距尺寸建。
- 第 306 行 `State::new(tilings.size(), ts, ...)` —— 但状态里的内容尺寸是**不含间距**的 `tilings.size()`，所以内容只画在左上角。

它调用的 `render_frame` 就是 u1-l3 讲过的主派发中枢：

[crates/typst-render/src/lib.rs:186-205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L186-L205) —— 遍历 `frame.items()`，把 `Group`/`Text`/`Shape`/`Image` 分派给对应子模块。`render_tiling_frame` 把瓦片内容树喂给它，等于「把瓦片当成一页来渲染」。

#### 4.2.4 代码实践

**实践目标**：在「像素层面」理解 `spacing` 为什么能产生瓦片之间的间隙。

**操作步骤**：

1. 设想一个最简瓦片：`size: (20pt, 20pt)`、`spacing: (10pt, 20pt)`、内容是一个填满 20×20pt 的黑方块、`pixel_per_pt = 2.0`。
2. 手算 `render_tiling_frame` 的输出：
   - 画布尺寸 = `(20pt + 10pt, 20pt + 20pt) = (30pt, 40pt)`，按 `pp=2.0` 换算 = **60×80 像素**。
   - 内容区域 = `tilings.size() = (20pt, 20pt)` = 40×40 像素，画在画布左上角。
   - 于是这张 60×80 的纹理里：左上 40×40 是黑，右侧 20 列与下侧 40 行是透明。
3. 想象这张 60×80 纹理被 `SpreadMode::Repeat` 横竖平铺：每贴一张，右边让出 20 像素（=10pt）、下边让出 40 像素（=20pt）的透明缝。

**需要观察的现象**：横向缝宽 10pt、纵向缝宽 20pt，正好等于 `spacing` 的两个分量。

**预期结果**：你得出结论——`spacing` 通过「把纹理画布撑大、但内容不撑大」来制造间隙；若 `spacing` 为负（小于零），纹理画布反而比内容小，相邻瓦片会**重叠**（这与 `Tiling` 构造器文档「If the spacing is lower than the size of the tiling, the tiling will overlap with itself」一致）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `State::new` 的第一个参数是 `tilings.size()` 而不是 `size`（含间距）？

**参考答案**：`State.size` 表示「当前帧的内容尺寸」，用于 `RelativeTo::Parent` 等参考系计算。瓦片的**内容**只占 `tilings.size()`，间距是内容之外的留白，不属于内容尺寸。若传含间距的 `size`，内容会以为可用空间更大而发生错位。同时内容坐标变换 `ts = from_scale(pp, pp)` 也只把 `tilings.size()` 范围内的 pt 映射到像素，超出部分本就是透明留白。

**练习 2**：`render_tiling_frame` 用了 `unwrap()` 来建 `Pixmap`（第 302 行），这安全吗？

**参考答案**：`Pixmap::new` 仅在宽或高为 0（或过大超限）时返回 `None`。这里的宽高是 `(tilings.size() + tilings.spacing()) * pixel_per_pt` 并 `round`，对一个合法的 `Tiling` 而言通常为正；即便退化，`round` 也可能得 0 从而 `unwrap` panic。这与 u1-l2 里主画布用 `.max(1.0)` 兜底不同，属于「调用方应保证瓦片尺寸合法」的约定。可视为待本地验证的边界情况。

---

### 4.3 to_sk_paint(Paint::Tiling)：形状路径的 Pattern 着色器

#### 4.3.1 概念说明

形状路径（`render_shape` 里的填充与描边，以及文本慢路径）不逐像素采样，而是交给 tiny-skia 的 `Pattern` 着色器。`Pattern` 接受一张 `Pixmap` 当纹理，配一个 `SpreadMode`（展开模式），tiny-skia 在填充形状时自动按这个模式把纹理无限延展。

平铺用的是 `SpreadMode::Repeat`——纹理边缘回到起点，周而复始。这等价于「壁纸式平铺」。所以形状路径里，「周期性重复」这件事是 **tiny-skia 替我们做的**；typst-render 只要把纹理造好、把坐标变换配对即可。

#### 4.3.2 核心流程

`to_sk_paint` 的 `Paint::Tiling` 分支流程：

1. 解析参考系：`relative = tilings.unwrap_relative(on_text)`（形状默认 `Self_`、文本默认 `Parent`）。
2. 算 `fill_transform`（把瓦片定位到正确参考点）：
   - `Self_`：用调用方据包围盒算好的 `fill_transform`（即 `bbox.min` 的平移）。
   - `Parent`：`container_transform.post_concat(transform.invert())`——把瓦片「挂」到首个硬帧坐标系。
3. 调 `render_tiling_frame(&state, tilings)` 生成瓦片纹理，装进 `pixmap`。
4. 算 `base_offset`（仅 `Self_` 且负尺寸矩形时非零，用于对齐描边与填充）。
5. 构造 `Pattern` 着色器：`SpreadMode::Repeat` + `FilterQuality::Nearest`，变换链为
   `fill_transform → pre_concat(tilings.transform()) → pre_scale(1/pp) → pre_translate(base_offset)`。

`RelativeTo::Self_` 与 `Parent` 的对照（与渐变 u3-l1 完全平行）：

| 量 | `Self_`（形状默认） | `Parent`（文本默认） |
|---|---|---|
| `fill_transform` | 包围盒左上角平移 | `container_transform.post_concat(transform.invert())` |
| `base_offset` | 负尺寸矩形时为 `-gradient_map.0`，否则零 | 恒为零 |
| 参考基准 | 被填充对象自身 bbox | 首个硬帧（父容器） |

#### 4.3.3 源码精读

`Paint::Tiling` 分支：

[crates/typst-render/src/paint.rs:248-279](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L248-L279) —— 重点看三段：

- 第 258 行 `let canvas = render_tiling_frame(&state, tilings);` —— 生成瓦片纹理（4.2 讲过）。
- 第 261-266 行 `base_offset`：`Self_` 时取 `gradient_map.map(|(offset, _)| -offset).unwrap_or_default()`。`gradient_map` 是 `to_sk_paint` 顶部据 `Geometry::Rect` 负尺寸算出的镜像信息（见 u3-l1）；这里取它的偏移量**取反**，抵消镜像带来的位移，使平铺与描边对齐。
- 第 269-278 行 `Pattern::new(...)`：五个参数依次是 **纹理 pixmap、`SpreadMode::Repeat`、`FilterQuality::Nearest`、opacity=1.0、变换链**。

变换链的叠加顺序（回忆 `pre_X` 表示「X 更内层、更先作用」）：

```
fill_transform
    .pre_concat(to_sk_transform(&tilings.transform()))  // 瓦片自身的 offset+angle
    .pre_scale(1.0 / pixel_per_pt, 1.0 / pixel_per_pt)  // 像素 → pt
    .pre_translate(base_offset.x.to_f32(), base_offset.y.to_f32()) // 镜像修正
```

作用顺序（从最内层往外读）：先把纹理像素按 `base_offset` 微移，再 `pre_scale(1/pp)` 把**像素**换算回 **pt**（因为纹理是以 `pp` 像素/pt 渲染的，要贴回 pt 空间几何必须除以 `pp`），再套上瓦片自身的 `transform()`（offset+angle），最后由 `fill_transform` 定位到 bbox 或父容器。`FilterQuality::Nearest` 关闭插值，保证瓦片像素 1:1 贴合、不模糊。

`tilings.transform()` 的定义（offset + angle 合成）：

[crates/typst-library/src/visualize/tiling.rs:374-377](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/tiling.rs#L374-L377) —— `Transform::translate(offset.x, offset.y).pre_concat(Transform::rotate(angle))`，即「先旋转、再平移」。

`unwrap_relative` 的默认值规则（形状默认 `Self_`、文本默认 `Parent`）：

[crates/typst-library/src/visualize/tiling.rs:366-370](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/tiling.rs#L366-L370) —— 与 `Gradient::unwrap_relative` 完全同构。

#### 4.3.4 代码实践

**实践目标**：验证形状路径用 `Pattern + SpreadMode::Repeat`、文本快路径用 `TilingSampler`，两者殊途同归到同一张瓦片纹理。

**操作步骤**：

1. 在 [crates/typst-render/src/paint.rs:269-278](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L269-L278) 确认形状路径的着色器是 `Pattern::new(.., SpreadMode::Repeat, FilterQuality::Nearest, 1.0, ..)`——周期性重复完全交给 tiny-skia。
2. 对比 [crates/typst-render/src/text.rs:138-142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L138-L142) 的文本路径：它**没有**用 `Pattern`，而是 `render_tiling_frame` 后构造 `TilingSampler`，由 `write_bitmap` 逐像素采样（4.4 详述）。
3. 在 [crates/typst-render/src/paint.rs:139-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L139-L146) 看 `to_sk_paint` 的签名：它把结果纹理通过 `pixmap: &mut Option<Arc<Pixmap>>` **向外回传**，因为形状路径里 `render_shape` 需要持有纹理直到 `fill_path`/`stroke_path` 调用结束（`Pattern` 借用这张 pixmap）。

**需要观察的现象**：同一个 `render_tiling_frame`，在形状路径经 `pixmap` 回传后被 `Pattern` 借用，在文本路径被 `TilingSampler` 直接持有；两条路都生成一次瓦片纹理。

**预期结果**：你能说出「`SpreadMode::Repeat`（tiny-skia 做）」与「`rem_euclid`（typst-render 自己做）」是同一件事的两种实现——这正是 4.4 的主题。

> 备注（性能，预告 u3-l4）：注意 `render_tiling_frame` **没有** `#[comemo::memoize]`，而渐变的对应物 `cached` 有。这意味着形状路径里每次 `to_sk_paint(Paint::Tiling)`、文本路径里每个字形都会重新光栅化瓦片。对比 [crates/typst-render/src/paint.rs:148-174](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L148-L174) 的 `cached`，可看到渐变如何按 `(gradient, width, height, gradient_map)` 缓存。原因待确认，可能与瓦片内容随 `state` 变化、不易做缓存键有关。

#### 4.3.5 小练习与答案

**练习 1**：变换链里为什么需要 `pre_scale(1.0 / pixel_per_pt, 1.0 / pixel_per_pt)`？

**参考答案**：瓦片纹理是按 `pixel_per_pt`（简称 `pp`）像素/pt 渲染的——纹理坐标单位是**像素**，而被填充的几何路径坐标单位是 **pt**。要把纹理贴回 pt 空间，必须把纹理像素坐标除以 `pp`（即 `pre_scale(1/pp)`），否则瓦片会被放大 `pp` 倍。这与 u2-l5 图像渲染里「纹理提前重采样到目标分辨率、再用 `Nearest` 贴回」是同一种思路。

**练习 2**：`FilterQuality::Nearest` 在这里为何合适？换成 `Bilinear` 会怎样？

**参考答案**：瓦片纹理本来就是按**目标分辨率**（`pp`）渲染的，纹理像素与画布像素一一对应，不需要插值。用 `Nearest` 既保证像素锐利、又最快。若用 `Bilinear`，tiny-skia 会在贴图时做双线性插值，瓦片边缘会发虚、相邻瓦片接缝处可能出现模糊带——对像素级对齐的平铺是有害的。

---

### 4.4 TilingSampler：文本路径的逐像素周期采样

#### 4.4.1 概念说明

文本快路径无法用 `Pattern` 着色器（pixglyph 给的是覆盖率位图，不是路径），必须自己逐像素取色。`TilingSampler` 就是这个逐像素取色器：给定一个画布像素坐标，返回它在瓦片纹理里对应的那个像素颜色。

它的核心难点是**「周期性回卷」**：画布像素坐标可以远大于瓦片尺寸（一个字形可能横跨很多个瓦片），但纹理只有一张瓦片大。必须把「超出部分」不断折回 `[0, 瓦片尺寸)` 区间——这就是 `rem_euclid`（欧几里得取模）的用武之地。它正是 `SpreadMode::Repeat` 在逐像素层面的数学实现。

#### 4.4.2 核心流程

`TilingSampler` 的构造（`new`）预先算好两个不变量，避免逐像素重复计算：

1. `transform_to_parent`：把画布像素映射回「瓦片局部 pt 空间」的合成变换。它由 `fill_transform`（参考系逆变换）与 `pattern_transform.invert()`（瓦片自身变换的逆）复合而成。
2. `size`：瓦片在**像素**单位的尺寸，即 `(tilings.size() + tilings.spacing()) * pixel_per_pt`，作为取模的周期。

`sample((x, y))` 的执行步骤：

1. 把画布像素 `(x, y)` 经 `transform_to_parent` 映射，得到瓦片局部 **pt** 坐标 `(u, v)`。
2. 换算到像素：`u_px = u * pixel_per_pt`，`v_px = v * pixel_per_pt`。
3. 取模回卷：`x' = floor(rem_euclid(u_px, size.x))`，`y' = floor(rem_euclid(v_px, size.y))`。
4. 从纹理取色：`pixmap.pixel(x', y')`。

整条映射链可写作（\(W, H\) 为瓦片像素尺寸）：

\[
(x,y) \xrightarrow{\text{transform\_to\_parent}} (u,v) \xrightarrow{\times pp} (u_{px}, v_{px}) \xrightarrow{\operatorname{rem\_euclid}} (x', y') \in [0,W)\times[0,H) \xrightarrow{\text{pixmap.pixel}} \text{color}
\]

其中欧几里得取模定义为：

\[
a \operatorname{mod} W = a - W\left\lfloor \frac{a}{W} \right\rfloor \in [0, W)
\]

#### 4.4.3 源码精读

`TilingSampler` 结构体：

[crates/typst-render/src/paint.rs:85-91](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L85-L91) —— 四个字段：`size`（瓦片像素尺寸，以 `Size`/Abs 存储但数值已是像素）、`transform_to_parent`（合成逆变换）、`pixmap`（瓦片纹理引用）、`pixel_per_pt`。

构造函数 `new`：

[crates/typst-render/src/paint.rs:93-115](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L93-L115) —— 注意三处：

- 第 109 行 `size: (tilings.size() + tilings.spacing()) * state.pixel_per_pt as f64` —— **像素**尺寸 = (瓦片 pt 尺寸 + 间距) × `pp`，与 `render_tiling_frame` 的画布尺寸一致，保证取模周期与纹理尺寸吻合。
- 第 110-111 行 `transform_to_parent = fill_transform.post_concat(pattern_transform.invert().unwrap_or_default())` —— `fill_transform`（像素→父空间 pt）再复合瓦片变换的逆（父空间 pt → 瓦片局部 pt）。
- 第 105 行 `pattern_transform = to_sk_transform(&tilings.transform())` —— 把瓦片的 `transform()`（offset+angle）转成 tiny-skia 变换。

`sample` 实现（`PaintSampler` 的第三个实例）：

[crates/typst-render/src/paint.rs:117-132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L117-L132) —— 核心两行（第 124-127 行）：

```rust
let x = (point.x * self.pixel_per_pt).rem_euclid(self.size.x.to_f32()).floor() as u32;
let y = (point.y * self.pixel_per_pt).rem_euclid(self.size.y.to_f32()).floor() as u32;
```

- `point.x * pixel_per_pt`：瓦片局部 pt → 瓦片局部像素。
- `.rem_euclid(self.size.x.to_f32())`：以瓦片像素宽为周期取模，折回 `[0, W)`。
- `.floor() as u32`：得纹理像素索引。
- 第 130 行 `self.pixmap.pixel(x, y).unwrap()`：取该像素颜色，返回 `PremultipliedColorU8`，正好喂给 `write_bitmap` 的 alpha 混合。

**为什么用 `rem_euclid` 而不是 `%`？** Rust 的 `%` 是「截断取模」，结果符号跟随被除数——当 `point.x` 为负（旋转/偏移导致坐标落到原点左侧）时，`%` 会给出负值，`as u32` 会得到一个巨大的错误索引。`rem_euclid` 是「欧几里得取模」，对正除数总是返回非负结果，因此无论坐标正负都能正确回卷到 `[0, W)`。这是平铺能正确处理 `offset`/`angle`（产生负坐标）的关键。

#### 4.4.4 代码实践

**实践目标**：亲手推一遍「画布像素 → 瓦片纹理像素」的映射链，理解 `rem_euclid` 与 `spacing` 的作用（本讲的核心实践任务）。

**操作步骤**：

1. 设定参数：`pixel_per_pt = 2.0`，瓦片 `size = (10pt, 10pt)`、`spacing = (0pt, 0pt)`、`transform()` 为单位变换（无 offset/angle），参考系 `RelativeTo::Parent` 且 `container_transform` 恰好是 `from_scale(2.0, 2.0)`（即父空间 pt→画布像素）。
2. 推导 `TilingSampler::new`：
   - `fill_transform = container_transform.invert() = from_scale(0.5, 0.5)`（画布像素→父空间 pt）。
   - `pattern_transform = to_sk_transform(单位变换) = identity`，其逆仍为 `identity`。
   - `transform_to_parent = fill_transform.post_concat(identity) = from_scale(0.5, 0.5)`。
   - `size = (10pt + 0) * 2.0 = 20`（像素），即 `size.x.to_f32() = size.y.to_f32() = 20.0`。
3. 对画布像素 `(x, y) = (45, 7)` 跑一遍 `sample`：
   - `point = (45.0, 7.0)`；`map_point` 后（÷2）得 `(u, v) = (22.5, 3.5)`（pt）。
   - `u_px = 22.5 * 2.0 = 45.0`；`x' = floor(rem_euclid(45.0, 20.0)) = floor(5.0) = 5`。
   - `v_px = 3.5 * 2.0 = 7.0`；`y' = floor(rem_euclid(7.0, 20.0)) = floor(7.0) = 7`。
   - 取色 `pixmap.pixel(5, 7)`。
4. 再试一个落在第二块瓦片的像素 `(x, y) = (50, 7)`：`u=25, u_px=50`，`x' = floor(rem_euclid(50.0, 20.0)) = floor(10.0) = 10`。可见横跨瓦片边界时坐标被正确折回。

**需要观察的现象**：`x=45` 与 `x=50` 这两个相距 5 像素的画布点，分别映射到瓦片纹理的 `x'=5` 与 `x'=10`；而 `x=40` 会映射到 `x'=0`（新一块瓦片的起点）。每走过 20 个画布像素（= 10pt = 一个瓦片宽），纹理索引回到 0。

**预期结果**：你画出完整映射链

\[
(45,7) \xrightarrow{\div 2} (22.5, 3.5)_{\text{pt}} \xrightarrow{\times 2} (45, 7)_{\text{px}} \xrightarrow{\operatorname{rem\_euclid}_{20}} (5, 7) \to \text{pixmap.pixel}(5,7)
\]

并得出结论：`rem_euclid` 以 `size`（含 spacing）为周期，把无界的画布折叠进一张瓦片纹理；`size` 里包含 `spacing`，使得间距区域（透明）也参与周期，于是在画布上表现为瓦片之间留出 gap。

**关于 `spacing` 的作用（回答实践任务第二问）**：`size = (tilings.size() + tilings.spacing()) * pixel_per_pt` 把间距纳入取模周期。因为 `render_tiling_frame` 生成的纹理画布是 `size + spacing` 大小、但内容只占 `size`、间距部分透明，所以取模周期放大后，每个周期里有一段（对应 spacing）必然采到透明像素——这就是平铺间隙的来源。若 `spacing` 为零，瓦片紧密相连；为正留缝；为负则纹理画布比内容小，相邻周期重叠。

> 待本地验证：上述数值推导基于「`container_transform = from_scale(2.0, 2.0)`」的假设；真实文档里 `container_transform` 由硬帧层级累积而成（见 u2-l2），数值会不同，但映射链的结构不变。

#### 4.4.5 小练习与答案

**练习 1**：把 `rem_euclid` 换成 Rust 的 `%`，在什么情况下会出错？

**参考答案**：当 `point.x * pixel_per_pt` 为负时。例如瓦片设了 `offset` 或 `angle`，使某些画布像素映射回的 `u` 为负，`u_px` 随之为负。`%` 对负被除数返回负值，`as u32` 会把它解释成一个巨大的正索引（或下溢），`pixmap.pixel` 要么越界 panic、要么取到错误像素。`rem_euclid` 对正除数恒返回 `[0, W)` 内的非负值，从而安全。

**练习 2**：`TilingSampler::new` 里 `pattern_transform.invert().unwrap_or_default()` 用了 `unwrap_or_default()`，而 `GradientSampler` 里 `container_transform.invert()` 却直接 `unwrap()`。为什么平铺这里更「谨慎」？

**参考答案**：`pattern_transform` 来自 `tilings.transform()`（offset + rotate），理论上是可逆仿射变换（旋转加平移行列式非零），但在极端输入或浮点退化下可能不可逆。`unwrap_or_default()` 在不可逆时退化为单位变换（不施加瓦片自身变换），是一种「降级但不崩溃」的保守策略；而 `container_transform` 由 typst-render 内部保证可逆（硬帧变换链），故直接 `unwrap`。两者体现了「内部不变量 vs 外部输入」的不同信任程度。此差异的触发条件待本地验证。

**练习 3**：`TilingSampler` 与 `GradientSampler` 的 `sample` 在结构上几乎一样（都先 `map_point` 再采样），主要差别在哪？

**参考答案**：差别在「映射后的取色方式」：
- `GradientSampler` 映射到 pt 空间后，调 `gradient.sample_at((point.x, point.y), container_size)` **解析地算**颜色（连续函数）。
- `TilingSampler` 映射到 pt 空间后，再 `× pp` 转像素、`rem_euclid` 折回、`pixmap.pixel` **查表**取色（离散周期函数）。
一个是对连续坐标求值，一个是对离散纹理查表加周期回卷——这正是「渐变」与「平铺」在数学上的本质区别。

---

## 5. 综合实践

把本讲四条主线串起来，完成下面这个「从 Typst 源码到像素」的端到端追踪任务。

**任务**：写一个最小的 Typst 文档，分别用同一个平铺图案填充一个矩形和一段文字，然后追踪它们在 typst-render 里分别走哪条路径。

**Typst 源码**（示例代码，基于 `Tiling` 构造器文档示例改写）：

```typst
#let pat = tiling(
  size: (20pt, 20pt),
  spacing: (5pt, 5pt),
  relative: "parent",
  place(dx: 5pt, dy: 5pt, circle(radius: 5pt, fill: black)),
)

// 形状路径
#rect(width: 100%, height: 30pt, fill: pat)

// 文本路径
#set text(fill: pat)
#lorem(5)
```

**操作步骤**：

1. 把上述源码存为 `tiling.typ`，用 typst CLI 渲染成 PNG（命令待本地验证）：
   ```
   typst compile tiling.typ --ppi 144 tiling.png
   ```
   `--ppi 144` 对应 `pixel_per_pt = 144/72 = 2.0`（见 u1-l1）。
2. **形状追踪**：`rect(fill: pat)` → `render_shape` → `to_sk_paint(Paint::Tiling, ..., Some(shape), ...)`（[paint.rs:248-279](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L248-L279)）→ `render_tiling_frame`（[paint.rs:296-309](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L296-L309)）→ `Pattern + SpreadMode::Repeat`，由 tiny-skia 平铺。
3. **文本追踪**：`text(fill: pat)` 的每个字形 → `render_outline_glyph` 快路径 → `Paint::Tiling` 分支（[text.rs:138-142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L138-L142)）→ `render_tiling_frame` + `TilingSampler::new`（[paint.rs:93-115](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L93-L115)）→ `write_bitmap` 逐像素 `sample`（[paint.rs:117-132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L117-L132)）。
4. **观察点**：
   - 矩形里的圆点应呈网格状均匀分布，圆点之间有 5pt 透明缝（对应 `spacing`）。
   - 文字笔画内应能看到被裁切到字形轮廓内的同一套圆点网格，且与矩形里的网格**对齐**（因为都用了 `relative: "parent"`，参考同一父容器）。
   - 把 `relative` 改成 `"self"`，重新渲染：文字网格会相对每个字形 bbox 重新定位，不再与矩形对齐。

**预期结果**：你能用一句话解释矩形与文字虽然在代码里共用同一个 `pat`，却在 typst-render 内部走了两条完全不同的渲染路径，最终在视觉上呈现为「对齐的同一套壁纸」。

> 待本地验证：实际渲染依赖本地构建 typst CLI；若暂无法编译，可只做源码追踪部分（步骤 2-3 的调用链阅读），同样能完成本讲目标。

## 6. 本讲小结

- **平铺与渐变同构**：两者都是「位置→颜色」的位置函数，都走「形状用 `Pattern` 着色器、文本快路径用 `PaintSampler` 逐像素采样」的两条路径分流；平铺多出的是「周期性回卷」。
- **PaintSampler 的第三个实现**：`TilingSampler` 与 `PremultipliedColorU8`（纯色）、`GradientSampler`（渐变）并列，让 `write_bitmap` 的逐像素循环对三种填充通用。
- **render_tiling_frame 复用主派发**：开一张独立画布、造根 `State`、调 `render_frame`，把瓦片内容「当一页渲染」成纹理；纹理画布含 `spacing`、内容只占 `size`，于是间距变成透明缝。
- **形状路径靠 tiny-skia 平铺**：`to_sk_paint(Paint::Tiling)` 用 `Pattern + SpreadMode::Repeat + Nearest`，变换链 `fill_transform → tile transform → pre_scale(1/pp) → base_offset` 把纹理贴回 pt 空间几何。
- **文本路径靠 rem_euclid 平铺**：`TilingSampler::sample` 把画布像素映射回瓦片局部像素后，用 `rem_euclid(size)` 折回 `[0, 瓦片尺寸)`，再查纹理像素——这是 `SpreadMode::Repeat` 在逐像素层面的数学实现；用 `rem_euclid` 而非 `%` 是为了正确处理负坐标。
- **性能差异（预告 u3-l4）**：与渐变的 `cached`（被 `comemo::memoize`）不同，`render_tiling_frame` 未被缓存，形状/文本每次填充都会重渲染瓦片。

## 7. 下一步学习建议

- **u3-l3 字形光栅化与像素级混合**：本讲里 `TilingSampler.sample` 返回的 `PremultipliedColorU8` 是如何与字形 `coverage` 位图做 `blend_src_over` / `alpha_mul` 混合的？答案在 `write_bitmap` 的逐像素循环与 `blend_src_over` 的位运算里，下一讲会完整推导。
- **u3-l4 记忆化与性能**：本讲多次提到 `render_tiling_frame` 未被 `comemo::memoize`、而渐变的 `cached` 与图像的 `build_texture` 被缓存。下一讲会系统对比三处缓存的缓存键设计与命中条件，并分析平铺为何（暂）不缓存。
- **回头读 typst-library 的 `Tiling`**：[tiling.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/tiling.rs) 里瓦片 `Frame` 是如何由 Typst 源码 `tiling(size:, spacing:, offset:, ..)` 构造出来的，能帮你理解「瓦片内容树」从何而来。
- **对比 typst-svg 的平铺实现**：typst-svg 输出矢量，平铺用 SVG 的 `<pattern>` 元素而非像素纹理；对比两者能加深对「同一概念在不同后端的落地」的理解。
