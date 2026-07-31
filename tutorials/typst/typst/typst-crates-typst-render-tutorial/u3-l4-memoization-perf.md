# 记忆化与性能优化

## 1. 本讲目标

typst-render 把排版结果画成像素，其中最贵的三类操作是：**字形光栅化**（每个字符都要做一次）、**图像纹理构建**（解码 + 重采样或矢量栅格化）、**渐变采样**（逐像素算颜色）。如果同一份文档被渲染多次（比如 `typst watch` 的重编译循环），这些操作会被原样重做一遍，造成巨大浪费。

本讲要回答三个问题：

1. typst-render 在哪三处用 `#[comemo::memoize]` 缓存了计算结果？它们的**缓存键**分别是什么？
2. 为什么字形缓存的键要用 `f32::to_bits()` 而不是直接用 `f32`？同一个字形在不同位置、相同字号下**能否命中缓存**？
3. `#[typst_macros::time]` 这个属性宏是怎么工作的？它在生产运行中有没有额外开销？

学完后，你应当能够：说出三处缓存各自的命中条件、解释 `to_bits` 的必要性、并能借助计时文件定位渲染热点。

## 2. 前置知识

在进入本讲前，你需要先建立以下直觉（这些都在前置讲义里讲过，这里只做一句话回顾）：

- **纯函数与缓存**：如果一个函数「相同输入永远得到相同输出」，我们就可以用一个「输入 → 输出」的表把它记下来，下次遇到相同输入直接查表，不必重算。这叫**记忆化（memoization）**。前提是：函数签名必须把**所有影响输出的因素**都作为参数暴露出来，否则会「查错表」——输入看着一样、实际不同，却返回了旧结果。
- **`Arc<T>` 引用计数共享**：`Arc` 让多个持有者共享同一份堆数据，clone 它只是把引用计数加一，代价极低。三处缓存都返回 `Arc<...>`，就是为了在多个调用者之间**零拷贝**地复用同一块像素缓冲。
- **`f32` 不可哈希**：Rust 的 `f32` 实现了 `PartialEq`，但**没有实现 `Eq`，也没有实现 `Hash`**。原因是 `NaN != NaN`（甚至 `NaN != 自身`），浮点比较不具备缓存键所需的自反性与一致性，因此不能直接拿 `f32` 当 HashMap / comemo 的键。
- **`comemo`**：Typst 自研的记忆化库，全 workspace 共享一个缓存池，靠 `comemo::evict(N)` 按代清理过期条目。
- **render 主流程**：`render(page, opts)` 是入口，递归 `render_frame` 派发到 `render_text`/`render_shape`/`render_image`（见 u1-l2、u1-l3）。

> 本讲是 u3-l3（字形光栅化）与 u2-l5（图像渲染）的直接后续：那两讲讲清了「怎么算」，本讲讲清「算完怎么存、怎么复用」。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
|------|----------------|
| `src/text.rs` | `rasterize` —— pixglyph 字形光栅化的缓存包装；调用处把 `tx/ty/ppem` 经 `to_bits` 传入 |
| `src/image.rs` | `build_texture` —— 光栅图/SVG/PDF 三类纹理构建的缓存包装 |
| `src/paint.rs` | `cached` —— 渐变逐像素采样结果的缓存包装；以及 `to_sk_paint` 中的调用 |
| `src/lib.rs` | `#[typst_macros::time(name = "render")]` 计时探针的挂载点 |
| `crates/typst-timing/src/lib.rs` | `TimingScope` 的真实实现：开关门控 + `Drop` 记录结束事件 |
| `crates/typst-macros/src/time.rs` | `#[time]` 宏展开：往函数体最前面插一行 `TimingScope` |
| `crates/typst-cli/src/watch.rs` | `comemo::evict(10)` —— watch 模式每轮编译后的缓存清理 |
| `crates/typst-kit/src/timer.rs` | `typst_timing::enable()` —— 打开计时收集 |

## 4. 核心概念与源码讲解

### 4.1 comemo::memoize 机制总览

#### 4.1.1 概念说明

`#[comemo::memoize]` 是 comemo 提供的**属性宏**。把它加在一个普通函数上，comemo 会在编译期改写这个函数：每次调用时，先按「所有参数的哈希」去一张**进程级缓存表**里查；命中就直接返回缓存的返回值（克隆一份 `Arc`），没命中才真正执行函数体，并把结果存进表里。

它和手写 `HashMap` 缓存的区别在于：

- **全自动**：你只要保证「相同参数 ⟹ 相同返回值」（即函数是**纯的**），剩下的查表/存储/共享 comemo 全包。
- **全 workspace 共享**：comemo 用 interning 把缓存挂在被跟踪对象（如 `&FontInstance`、`&Image`、`&Gradient`）上，跨 crate、跨多次调用都生效，直到被显式清理。
- **可代际清理**：`comemo::evict(N)` 会清掉「最近 N 代没被访问过」的条目，防止内存无限增长。

typst-render 把它用在了**三处最贵且可复用**的计算上：字形光栅化、图像纹理、渐变采样。这三处的共同特征是「输入维度小、输出大、且在重渲染时高度重复」。

#### 4.1.2 核心流程

一次经过 memoize 的函数调用，执行过程如下：

```text
调用 f(arg1, arg2, ...)
        │
        ▼
┌─────────────────────────┐
│ 计算 key = hash(各参数) │
└─────────────────────────┘
        │
        ▼
   key 在缓存表里? ──否──▶ 执行函数体，得结果 R
        │是                  │
        ▼                    ▼
   取出 Arc<R>          包成 Arc<R>，写回表
        │                    │
        └─────────┬──────────┘
                  ▼
            clone Arc<R> 返回（只加引用计数，不复制像素）
```

写成约束就是：

\[ \text{命中} \iff \forall i:\ \mathrm{bits}(arg_i^{\text{本次}}) = \mathrm{bits}(arg_i^{\text{上次}}) \]

只要有一个参数不同（哪怕只是浮点位不同），就是未命中。

#### 4.1.3 源码精读

三处 `#[comemo::memoize]` 分别挂在这里（本节先看挂载点，细节在 4.2–4.4 展开）：

- 字形光栅化 `rasterize`（`src/text.rs`）：[src/text.rs:105-106](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L105-L106) —— 这是文本快路径的缓存。
- 图像纹理 `build_texture`（`src/image.rs`）：[src/image.rs:68-69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L68-L69) —— 把解码/重采样/矢量栅格化的结果缓存。
- 渐变采样 `cached`（`src/paint.rs`）：[src/paint.rs:148-149](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L148-L149) —— 逐像素采样渐变得到的 pixmap 缓存。

缓存不会永远堆积。`typst watch` 在每一轮「编译 → 渲染」结束后会调用清理：

[crates/typst-cli/src/watch.rs:82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/watch.rs#L82) —— `comemo::evict(10);` 保留最近 10 代内访问过的条目，清掉更老的。

这意味着：**同一次渲染内**，缓存全程可用；**多次渲染之间**，只要参数不变就一直命中（watch 重渲染同一份文档的典型场景）；只有当代际过期或输入真的变了，才会重新计算。

#### 4.1.4 代码实践

**实践目标**：用一个表格把三处缓存的「输入维度」列清楚，为后续逐个分析做准备。

**操作步骤**：

1. 打开上面三个源码链接，分别抄下每个被 memoize 函数的**完整参数列表**。
2. 用下表组织（先自己填，再对照 4.2–4.4 的答案）：

| 函数 | 参数 1 | 参数 2 | 参数 3 | 参数 4 | 返回类型 |
|------|--------|--------|--------|--------|----------|
| `rasterize` | ? | ? | ? | ? | ? |
| `build_texture` | ? | ? | ? | — | ? |
| `cached` | ? | ? | ? | ? | ? |

**需要观察的现象**：三个函数的返回值都是 `Arc<...>`（共享句柄，而非裸数据）。

**预期结果**：你能说出「缓存键 = 全部参数的逐位相等」，并且意识到返回 `Arc` 让命中时几乎是零成本。

#### 4.1.5 小练习与答案

**练习 1**：假如有人给 `rasterize` 加了一个不参与签名的「全局开关」参数（比如读一个 `static` 布尔来切换是否抗锯齿），缓存还能正确工作吗？

**答案**：不能。这个开关会影响输出，却不在签名里，于是开关切换前后两套「正确结果」会被同一个键命中，导致返回**错误**的旧结果。这正是「纯函数 + 全部影响因素入参」原则的意义：memoize 的正确性**完全依赖**于「签名捕获了所有影响输出的输入」。

**练习 2**：为什么三个被缓存的函数都返回 `Arc<T>`，而不是 `T`？

**答案**：`Arc` 让缓存表里的同一块像素缓冲被多个调用者共享，命中时只需 `clone`（引用计数 +1），不必复制整块像素数据。返回裸 `T` 的话每次命中都要深拷贝一整张 pixmap，缓存省下的算力会被拷贝吃掉。

---

### 4.2 字形光栅化缓存 rasterize（text.rs）

#### 4.2.1 概念说明

文本渲染中，**绝大多数字形走的是 pixglyph 快路径**（见 u3-l3）：把字形轮廓按当前字号、当前亚像素位置光栅化成一张**覆盖率位图（coverage bitmap）**，再和颜色做 src-over 混合写进画布。这一步是整个渲染管线里调用最频繁、单次也不算便宜的操作。

关键观察：`pixglyph` 的光栅化结果取决于「哪个字体的哪个字形 + 字号 ppem + 亚像素平移」。typst-render 把这些因素全部编码进 `rasterize` 的签名，再用 `#[comemo::memoize]` 包起来，于是**完全相同的（字体、字形、位置、字号）**只会光栅化一次。

#### 4.2.2 核心流程

`rasterize` 的缓存键可写成：

\[ K_{\text{rasterize}} = \big(\text{font},\ \text{id},\ \mathrm{bits}(t_x),\ \mathrm{bits}(t_y),\ \mathrm{bits}(\text{ppem})\big) \]

其中 \( \mathrm{bits}(x) = \texttt{f32::to\_bits}(x) \in \mathbb{U32} \)，\( t_x, t_y \) 是当前 `state.transform` 的平移分量（字形原点在画布上的绝对像素坐标），`ppem` 是「每 em 像素数」= 字号 × 缩放的 sy 分量。

调用方传入键的过程：

```text
ts = state.transform               // 当前累积变换（画布原点 → 字形原点）
ppem = text.size.to_f32() * ts.sy  // 字形在画布上的真实像素尺寸
rasterize(&text.font, id,
          ts.tx.to_bits(),         // ← 绝对像素 X，转成 u32
          ts.ty.to_bits(),         // ← 绝对像素 Y，转成 u32
          ppem.to_bits())          // ← 字号，转成 u32
```

#### 4.2.3 源码精读

被缓存的函数本体：[src/text.rs:105-119](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L105-L119)

```rust
#[comemo::memoize]
fn rasterize(
    font: &FontInstance,
    id: GlyphId,
    x: u32,   // 实为 ts.tx.to_bits()
    y: u32,   // 实为 ts.ty.to_bits()
    size: u32, // 实为 ppem.to_bits()
) -> Option<Arc<Bitmap>> {
    let glyph = pixglyph::Glyph::load(font.ttf(), id)?;
    Some(Arc::new(glyph.rasterize(
        f32::from_bits(x),
        f32::from_bits(y),
        f32::from_bits(size),
    )))
}
```

注意一个精巧之处：签名里收的是 `u32`，函数体内用 `f32::from_bits` 还原回 `f32` 再交给 pixglyph。**为什么不在签名里直接写 `f32`？** 因为 comemo 要对参数求哈希来构造键，而 `f32` 没有实现 `Hash`/`Eq`，根本过不了编译。`to_bits`/`from_bits` 这对操作是「把不可哈希的 f32 包装成可哈希的 u32」的标准手法，位表示完全可逆、无精度损失。

调用处：[src/text.rs:123-124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L123-L124)

```rust
let bitmap =
    rasterize(&text.font, id, ts.tx.to_bits(), ts.ty.to_bits(), ppem.to_bits())?;
```

这里 `ts.tx`、`ts.ty` 是字形在**画布上的绝对像素位置**。这一点决定了缓存的命中行为（见下方实践）。

#### 4.2.4 代码实践

**实践目标**：搞清「同一字形、相同字号、不同位置」能不能命中 `rasterize`，并解释 `to_bits` 的必要性。

**操作步骤（源码阅读型）**：

1. 在 [src/text.rs:50-51](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L50-L51) 确认 `ts = &state.transform`，`ppem = text.size.to_f32() * ts.sy`。
2. 假设字母 `e` 在一页里出现 100 次，字号都是 12pt，但分布在 100 个不同的像素位置（\( t_x, t_y \) 各不相同）。
3. 对照键 \( K_{\text{rasterize}} \) 推断：第 2 个 `e` 的键和第 1 个一样吗？

**需要观察的现象 / 推理**：

- 100 个 `e` 的 `font / id / ppem` 三项完全相同。
- 但它们的 \( t_x, t_y \) 各不相同 ⟹ \( \mathrm{bits}(t_x), \mathrm{bits}(t_y) \) 各不相同 ⟹ 键不同。

**预期结果**：**不能命中**。同一字形在同一页的不同位置，会被光栅化 100 次，互相之间不共享。`rasterize` 缓存真正发挥作用是在**重渲染同一份文档**时（如 watch 重编译）：只要版面没变，每个字形的绝对像素位置不变，第二次渲染就全部命中。这是「以绝对位置入键」的必然结果——因为返回的 `Bitmap` 里烘焙了 `left`/`top`（绝对落位，`write_bitmap` 会用到），位置变了结果就变了，键必须包含位置才能保证正确。

**关于 `to_bits` 而非 `f32`**：除了「`f32` 不实现 `Hash` 编不过」这一硬性原因外，`to_bits` 还保证了**逐位精确**的比较——只有两个浮点位模式完全相同才算同一个键。这对亚像素抗锯齿至关重要：两个相差一个 ULP 的亚像素位置，光栅化出的覆盖率会有细微差别，绝不能被错误地共享。

> 「待本地验证」：如果你想亲眼确认「不同位置不命中」，可以在 `rasterize` 函数体第一行临时加一行 `eprintln!("rasterize id={:?} tx={:?}", id, f32::from_bits(x));`，渲染一个含大量重复字母的文档，观察输出里同一个 `id` 是否出现了多次（预期：会）。

#### 4.2.5 小练习与答案

**练习 1**：把 `rasterize` 的键从「绝对位置 \( t_x, t_y \)」改成「只取亚像素小数部分」（即 `ts.tx.fract()`），命中率会变高吗？会带来什么问题？

**答案**：命中率会大幅变高——同一字号下，所有落在相同亚像素相位上的同字形都能共享同一份覆盖率。但 pixglyph 返回的 `Bitmap` 带有 `left`/`top` 绝对落位字段，落位取决于整数部分；只缓存小数部分的话，调用方还得自己重建 `left`/`top`，否则 `write_bitmap` 会把字贴错位置。当前实现选择了「键含绝对位置、结果即取即用」的简单+正确路线。

**练习 2**：`rasterize` 为什么把 `x/y/size` 收成 `u32` 而不是直接传 `f32`？

**答案**：comemo 要对全部参数求哈希构造缓存键；`f32` 既没实现 `Eq` 也没实现 `Hash`，无法做键。`u32` 两者都有，`to_bits`/`from_bits` 又是完全可逆的位级转换，于是用 `u32` 当键、函数体内 `from_bits` 还原，既满足了 comemo 的类型约束，又无精度损失。

---

### 4.3 纹理构建缓存 build_texture（image.rs）

#### 4.3.1 概念说明

`render_image` 渲染一张图时，要先把原图**重采样到目标分辨率**（光栅图）或**栅格化**（SVG/PDF），得到一块 `sk::Pixmap` 纹理，再用 `Pattern` 贴回画布（见 u2-l5）。重采样和矢量栅格化都很贵，而「同一张图以同一尺寸出现多次」在文档里很常见（比如同一个 logo 反复用）。

`build_texture` 把这一步用 `#[comemo::memoize]` 包起来：**同一张图 + 同一目标宽高 ⟹ 只构建一次**，之后命中即拿 `Arc<Pixmap>` 共享。

#### 4.3.2 核心流程

缓存键很简单：

\[ K_{\text{texture}} = (\text{image},\ w,\ h) \]

- `image: &Image` —— Typst 的 `Image` 是被 comemo 跟踪的 interned 类型，身份稳定，可直接做键。
- `w, h: u32` —— 目标纹理的整数像素尺寸。

函数内部分三个分支（Raster / Svg / Pdf），与缓存本身无关——缓存只关心「这个键算出来的整块纹理」。

```text
build_texture(image, w, h)
   │
   ▼
match image.kind() ── Raster ──▶ 新建 pixmap → 按需 resize → premultiply
                   ── Svg ─────▶ resvg 栅格化
                   ── Pdf ─────▶ hayro 栅格化（build_pdf_texture）
   │
   ▼
包成 Arc<Pixmap> 返回（同时被 comemo 存进表）
```

#### 4.3.3 源码精读

挂载点与签名：[src/image.rs:68-69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L68-L69)

```rust
#[comemo::memoize]
fn build_texture(image: &Image, w: u32, h: u32) -> Option<Arc<sk::Pixmap>> {
```

值得区分的一点：函数体内部有一个**与缓存无关的局部优化**——当目标尺寸等于原图尺寸时跳过 `resize`：[src/image.rs:78-90](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L78-L90)

```rust
let resized = if (w, h) == (dynamic.width(), dynamic.height()) {
    dynamic                         // 尺寸没变，直接用，省一次 resize 分配
} else {
    let upscale = w > dynamic.width();
    let filter = match image.scaling() {
        Smart::Custom(ImageScaling::Pixelated) => FilterType::Nearest,
        _ if upscale => FilterType::CatmullRom,
        _ => FilterType::Lanczos3,   // 缩小
    };
    buf = dynamic.resize_exact(w, h, filter);
    &buf
};
```

别把这两个层次的优化搞混：

- **局部优化**（`if (w,h) == 原图尺寸`）：在「未命中、必须构建」时，省掉一次无意义的 resize。
- **comemo 缓存**：在「命中」时，连解码和 pixmap 分配都省了，直接共享。

#### 4.3.4 代码实践

**实践目标**：判断 `build_texture` 在哪些场景命中、哪些未命中。

**操作步骤（源码阅读型）**：

1. 假设文档里同一张 `logo.png` 出现 3 次，显示尺寸完全相同（都是 200×100 像素）。
2. 再假设同一张图还出现 1 次，但显示尺寸是 400×200。
3. 对照 \( K_{\text{texture}} = (\text{image}, w, h) \) 推断命中情况。

**预期结果**：

- 3 个 200×100 的实例：第 1 个未命中（构建并缓存），第 2、3 个命中（共享同一 `Arc`）。
- 1 个 400×200 的实例：键不同（\(w,h\) 变了），未命中，单独构建一份新纹理。
- 这正是返回 `Arc<Pixmap>` 的价值：3 个相同尺寸的实例共享同一块像素内存，没有 3 份拷贝。

**观察要点**：`render_image` 在 [src/image.rs:46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L46) 调用 `build_texture(image, w, h)`，`w/h` 由坐标变换推算而来（u2-l5 讲过）。所以「同一张图、不同显示尺寸会各算一份」是必然的——因为不同尺寸的纹理像素内容本就不同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `build_texture` 的键是 `(image, w, h)`，而不像 `rasterize` 那样包含绝对位置 \( t_x, t_y \)？

**答案**：纹理的内容只取决于「原图 + 目标尺寸」，与它最终贴在画布的哪个位置无关——贴位是由 `render_image` 末尾的 `Pattern` + `fill_rect(ts, ...)` 用 `state.transform` 完成的，不在纹理数据里。所以位置不必入键。字形则相反：pixglyph 的 `Bitmap` 烘焙了 `left/top` 落位，必须把位置入键。

**练习 2**：`(w, h) == 原图尺寸` 时跳过 resize，这算不算 comemo 缓存命中？

**答案**：不算。这是「未命中、正在构建纹理」时函数体内部的快路径，省掉一次无意义的重采样分配。comemo 命中发生在更外层——连 `build_texture` 的函数体都不执行，直接返回表里已有的 `Arc`。两层优化正交。

---

### 4.4 渐变采样缓存 cached（paint.rs）

#### 4.4.1 概念说明

当形状（或大字号慢路径文本）用**渐变**填充时，`to_sk_paint` 的 `Paint::Gradient` 分支会把渐变**预渲染成一张 pixmap 纹理**，再交给 tiny-skia 的 `Pattern` 着色器贴上去。预渲染是一个 \( O(w \times h) \) 的逐像素采样循环，对每个像素都调一次 `gradient.sample_at`，开销不小。

`cached` 函数把这个逐像素结果缓存起来：**同一渐变 + 同一宽高 + 同一 gradient_map ⟹ 共享同一张预渲染纹理**。于是「同一个渐变填多个相同尺寸的形状」只需采样一次。

#### 4.4.2 核心流程

缓存键：

\[ K_{\text{cached}} = \big(\text{gradient},\ \text{width},\ \text{height},\ \text{gradient\_map}\big) \]

其中 `gradient_map: Option<(Point, Axes<Ratio>)>` 只在「负尺寸矩形 + `RelativeTo::Self_`」时为 `Some`（用于把渐变镜像对齐到描边，见 u3-l1），其余情况（非矩形、或 `Parent` 模式）为 `None`。

逐像素采样循环（被缓存的部分）：

```text
对 pixmap 的每个像素 (x, y):
    应用 gradient_map（offset 平移 + scale 镜像）
    sample_at → 得到一个颜色
    premultiply 后写入像素
包成 Arc<Pixmap> 返回
```

#### 4.4.3 源码精读

挂载点与函数体：[src/paint.rs:148-174](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L148-L174)

```rust
#[comemo::memoize]
fn cached(
    gradient: &Gradient,
    width: u32,
    height: u32,
    gradient_map: Option<(Point, Axes<Ratio>)>,
) -> Arc<sk::Pixmap> {
    let (offset, scale) =
        gradient_map.unwrap_or_else(|| (Point::zero(), Axes::splat(Ratio::one())));
    let mut pixmap = sk::Pixmap::new(width.max(1), height.max(1)).unwrap();
    for x in 0..width {
        for y in 0..height {
            let color = gradient.sample_at(/* 应用 offset+scale 后的坐标 */, (width, height));
            pixmap.pixels_mut()[/* ... */] =
                to_sk_color(color.to_process()).premultiply().to_color_u8();
        }
    }
    Arc::new(pixmap)
}
```

调用处：[src/paint.rs:226-231](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L226-L231)

```rust
*pixmap = Some(cached(
    gradient,
    width.max(state.pixel_per_pt.ceil() as u32),
    height.max(state.pixel_per_pt.ceil() as u32),
    gradient_map,
));
```

注意 `gradient_map` 的取值由上面的 `match relative` 决定（[src/paint.rs:216-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L216-L219)）：`Self_` 模式下沿用算出的 `gradient_map`（负尺寸矩形才非 `None`），`Parent` 模式下强制为 `None`。所以「`Parent` 模式的等尺寸渐变形状」会比「`Self_` + 负尺寸矩形」更容易命中缓存（键更简单）。

> 对比：`to_sk_paint` 的 `Paint::Tiling` 分支**没有**用 `cached`/`#[comemo::memoize]`，而是每次填充都重新调 `render_tiling_frame` 渲染瓦片（见 u3-l2）。这是当前实现里渐变与平铺的一处不对称。

#### 4.4.4 代码实践

**实践目标**：理解 `gradient_map` 对缓存键的影响。

**操作步骤（源码阅读型）**：

1. 读 [src/paint.rs:176-194](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L176-L194)，确认 `gradient_map` 何时为 `Some`、何时为 `None`。
2. 读 [src/paint.rs:216-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L216-L219)，确认两种 `RelativeTo` 下传给 `cached` 的 `gradient_map`。

**需要观察的现象**：

- 普通的正尺寸矩形 / 曲线形状：`gradient_map = None`。
- 负尺寸矩形（`Self_` 模式）：`gradient_map = Some((offset, signum scale))`。
- `Parent` 模式：恒为 `None`。

**预期结果**：两个「同渐变、同尺寸」的普通正尺寸矩形会命中同一份 `cached` 纹理；但一个正尺寸矩形和一个负尺寸矩形（即便尺寸绝对值相同）不会命中，因为 `gradient_map` 不同（`None` ≠ `Some(...)`）。

**「待本地验证」**：若想确认，可在 `cached` 函数体首行加 `eprintln!("cached {}x{} map={:?}", width, height, gradient_map.is_some());`，渲染含多个渐变形状的文档，观察打印次数与形状数的关系。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Parent` 模式下 `gradient_map` 强制为 `None`？

**答案**：`Parent` 模式是相对父容器坐标系采样，纹理尺寸取自容器（`state.size`），与单个形状的 bbox 无关；负尺寸矩形那种「为对齐描边而镜像渐变」的考量只在 `Self_`（相对自身 bbox）模式下才有意义。所以 `Parent` 下没有 `gradient_map`，统一为 `None`，也顺带让等尺寸的 `Parent` 渐变更容易共享缓存。

**练习 2**：把 `cached` 的逐像素循环从 `#[comemo::memoize]` 里拆出来（即不缓存），单次渲染结果会变吗？

**答案**：渲染结果**完全不变**——`cached` 是纯函数，给定 `(gradient, width, height, gradient_map)` 输出唯一确定。拆掉缓存只会让每次渐变填充都重跑一遍 \( O(w \times h) \) 采样，变慢，但像素结果一致。这正是它能被安全 memoize 的前提。

---

### 4.5 计时探针 #[typst_macros::time]

#### 4.5.1 概念说明

缓存是用来「省」的，`#[typst_macros::time]` 则是用来「量」的：它给一个函数包上一层**计时探针**，记录「这个函数从进入到返回花了多久」。typst-render 只在一个地方挂了它——入口函数 `render`，用来度量整页光栅化的耗时。

它的设计有两个关键点：

1. **零开销开关**：默认**关闭**。关闭时，宏展开后的代码只多读一个原子布尔，几乎不耗时；只有显式 `enable()` 后才真正记录事件。
2. **可导出**：记录的事件能导出成 Chrome Tracing 格式的 JSON，直接拖进 `chrome://tracing` 可视化，看到每个计时段的嵌套与耗时。

#### 4.5.2 核心流程

`#[typst_macros::time(name = "render")]` 在编译期被改写：在函数体最前面插一行构造 `TimingScope` 的语句。`TimingScope` 在构造时记一个 Start 事件，在**被 drop 时**记一个 End 事件——利用了 RAII，函数无论是正常返回还是 `?` 提前返回，作用域结束都会 drop，计时都准确。

```text
fn render(...) {
    let __scope = TimingScope::new("render");   // ← 宏插入；关闭时返回 None
    /* 原函数体 */
}   // ← 作用域结束，__scope 被 drop，记 End 事件（若 __scope 是 Some）
```

关闭时的门控：

\[ \text{is\_enabled()} = \text{false} \implies \text{TimingScope::new}(\cdot) = \text{None} \implies \text{不记录任何事件} \]

#### 4.5.3 源码精读

typst-render 的挂载点：[src/lib.rs:20](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L20)

```rust
#[typst_macros::time(name = "render")]
pub fn render(page: &Page, opts: &RenderOptions) -> sk::Pixmap { ... }
```

宏展开的真相——它就是往函数体插入一行：[crates/typst-macros/src/time.rs:36-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs#L36-L43)

```rust
item.block.stmts.insert(
    0,
    parse_quote! {
        let __scope = ::typst_timing::TimingScope::new(#name);
    },
);
```

「是否真的计时」的门控在 `TimingScope::with_span`：[crates/typst-timing/src/lib.rs:172-177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L172-L177)

```rust
pub fn with_span(name: &'static str, span: Option<NonZeroU64>) -> Option<Self> {
    if is_enabled() {
        return Some(Self::new_impl(name, span));   // 开启：记 Start 事件
    }
    None                                            // 关闭：直接返回 None，啥也不干
}
```

`is_enabled()` 读的是一个 `AtomicBool`，默认 `false`（[crates/typst-timing/src/lib.rs:61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L61)）。因此默认运行 `render` 时，宏插入的那行只是拿到一个 `None`，函数体照常执行，几乎无额外开销。

End 事件靠 `Drop` 记录：[crates/typst-timing/src/lib.rs:194-205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205)

```rust
impl Drop for TimingScope {
    fn drop(&mut self) {
        let timestamp = Timestamp::now();
        EVENTS.lock().push(Event { kind: EventKind::End, timestamp, /* ... */ });
    }
}
```

开关由谁打开？`typst-kit` 的 `Timer::new` 调用 `typst_timing::enable()`：[crates/typst-kit/src/timer.rs:35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L35)。即只有用户显式要计时（如 CLI 传了计时相关参数）时，`Timer` 才会 `enable()`，平时 `render` 上的探针是「免费的」。

#### 4.5.4 代码实践

**实践目标**：打开计时、渲染一份文档、把结果可视化，从而看到 `render` 段的耗时。

**操作步骤**：

1. 在 typst CLI 中找到启用计时的入口（`typst-kit` 的 `Timer`，由 CLI 的计时参数触发；具体参数名「待确认」，可查阅 `typst compile --help` 中与 timing/trace 相关的选项）。
2. 用类似下面的命令渲染（参数名以本地 `--help` 为准，**待本地验证**）：
   ```bash
   typst compile --xxx-timing=trace.json doc.typ  # 参数名待确认
   ```
3. 用浏览器打开 `chrome://tracing`，点 Load 加载生成的 JSON。
4. 在时间轴里找到名为 `render` 的区段。

**需要观察的现象**：时间轴上出现一个 `render` 区段，其长度即整页光栅化耗时；多次渲染会看到多个并列的 `render` 段。

**预期结果**：你能直观看到 `render` 占整体编译时间的比例，从而判断「光栅化是不是瓶颈」。如果 `render` 段很短，说明字形/图像/渐变那三处缓存把成本压得很低。

> 「待本地验证」：本实践依赖 typst CLI 当前的计时参数名，请以本地 `typst compile --help` 实际输出为准；若参数不可用，可改为在测试中直接调用 `typst_timing::enable()` + `render()` + `typst_timing::export_json(...)` 的源码阅读型实践。

#### 4.5.5 小练习与答案

**练习 1**：`#[typst_macros::time]` 在生产（默认）运行中会拖慢 `render` 吗？

**答案**：几乎不会。默认 `is_enabled() == false`，`TimingScope::new` 直接返回 `None`，不分配、不上锁、不写 `EVENTS`，只剩一次原子布尔的 `Relaxed` 读。计时是「常驻但关闭」的，需要时 `enable()` 即可点亮。

**练习 2**：为什么用 `Drop` 来记 End 事件，而不是在函数末尾显式调一个 `end()`？

**答案**：`render` 里有大量 `?` 提前返回与控制流。用 RAII（`Drop`）能保证**无论从哪条路径离开作用域**，End 事件都准确记录；显式 `end()` 则容易在某个提前返回的分支漏调，导致 Start/End 不配对、时间轴错乱。

---

## 5. 综合实践

把本讲的三处缓存与计时串起来，做一个「命中率推断 + 计时验证」的小任务。

**场景**：写一份 Typst 文档 `doc.typ`，内容包括：

- 一段重复 200 次字母 `e` 的普通正文（同字体、同字号）。
- 同一张图片 `logo.png` 以相同尺寸插入 5 次。
- 一个线性渐变填充的矩形，复制 3 份（同渐变、同尺寸、`RelativeTo::Self_`、正尺寸）。

**任务**：

1. **推断**：对每个元素，分别判断它在**单次渲染内**和**第二次重渲染**时，能否命中对应缓存（`rasterize` / `build_texture` / `cached`）？填下表：

   | 元素 | 单次渲染内是否共享 | 第二次重渲染是否命中 | 理由 |
   |------|--------------------|----------------------|------|
   | 200 个 `e` | ? | ? | 键含绝对位置 \( t_x, t_y \) |
   | 5 个同尺寸 logo | ? | ? | 键为 `(image, w, h)` |
   | 3 个同渐变矩形 | ? | ? | 键含 `gradient_map`（此处为 `None`） |

   参考答案：200 个 `e` 单次渲染内**不共享**（位置各异），重渲染**全命中**；5 个 logo 单次渲染内**后 4 个命中**，重渲染**全命中**；3 个渐变矩形单次渲染内**后 2 个命中**（`gradient_map` 同为 `None`），重渲染**全命中**。

2. **验证（源码阅读型 + 可选运行）**：在 `rasterize`、`build_texture`、`cached` 三个函数体首行各加一条 `eprintln!`，渲染一次，统计各自被实际执行的次数，与你的推断对照。再渲染第二次（不改动文档），确认执行次数大幅下降（命中上升）。

3. **计时**：若本地 CLI 支持计时参数，对比「第一次渲染」与「第二次渲染」的 `render` 段耗时，应当看到第二次明显变快——这就是三处缓存的累积收益。参数名以本地 `typst compile --help` 为准（**待本地验证**）。

> 注意：临时 `eprintln!` 属于修改源码做调试，验证完毕请还原，不要提交。本任务只读源码即可完成「推断」部分。

## 6. 本讲小结

- typst-render 在三处用 `#[comemo::memoize]` 缓存昂贵计算：`rasterize`（字形光栅化，`text.rs`）、`build_texture`（图像纹理，`image.rs`）、`cached`（渐变采样，`paint.rs`）。缓存键 = **全部参数的逐位相等**，命中时返回 `Arc<T>` 实现零拷贝共享。
- 三键各有侧重：`rasterize` 键含 `f32::to_bits(tx/ty/ppem)`；`build_texture` 键为 `(image, w, h)`，**不含位置**（贴位由 Pattern 完成）；`cached` 键含 `gradient_map`，仅负尺寸矩形 + `Self_` 时非 `None`。
- **`f32` 不能直接做键**（未实现 `Eq`/`Hash`），故用 `to_bits` 转 `u32`；这同时保证了亚像素级的精确比较。同一字形在不同位置、相同字号下**不命中** `rasterize`，因为绝对位置入键；缓存的收益主要体现在**重渲染同一版面**（watch 循环）。
- comemo 缓存全 workspace 共享，由 `comemo::evict(10)`（watch 每轮编译后调用）按代清理，避免内存膨胀。
- `#[typst_macros::time]` 给 `render` 挂计时探针：宏插入一行 `TimingScope`，Start 在构造、End 在 `Drop`；默认 `is_enabled() == false` 时返回 `None`，**近乎零开销**，`enable()` 后才记录并可导出 Chrome Tracing JSON。
- 渐变 `cached` 有缓存，而平铺 `Tiling` 当前**没有** memoize（每次填充重渲染瓦片）——这是一处可留意的性能不对称。

## 7. 下一步学习建议

- **横向对比 typst-svg**：typst-svg 输出矢量而非像素，没有光栅化/纹理/逐像素混合，自然也不需要这三处缓存。对比两者处理同一 `Frame` 的策略，能加深对「光栅化专属优化」的理解。
- **深入 comemo**：阅读 comemo 的 interning 与代际清理机制，理解「为什么缓存能挂在 `&Image`/`&FontInstance` 上且跨 crate 生效」「`evict(N)` 的 N 代具体怎么计」，这能解释 watch 模式下缓存的生存期。
- **动手做一次性能实验**：用第 5 节的综合实践，配合计时文件，量化「关掉某处缓存（临时去掉 `#[comemo::memoize]`）后 `render` 变慢多少」，从而体会每处缓存的实际贡献（属实验性改动，勿提交）。
- **回到渲染主线**：本讲是 typst-render 学习路线的收官篇。建议回头重读 u3-l3（字形像素级混合）与 u2-l5（图像渲染），带着「这里的结果会被谁缓存」的视角，把整条「排版 Frame → 像素」的链路彻底打通。
