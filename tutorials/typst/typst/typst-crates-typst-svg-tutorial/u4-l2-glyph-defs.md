# 字形定义与符号复用 write_glyph_defs

## 1. 本讲目标

在上一讲（u4-l1）里，我们已经看到 `render_text` 如何逐字形推进笔位，并把每个字形分流成「轮廓字形」与「图像字形」两条路径——两条路径最终都只产生一个 `DedupId`，再用 `<use>` 引用它。但是被引用的那个「定义」到底是什么形状？它是何时、在哪里真正写进 SVG 文本的？

本讲就回答这两个问题。学完后你应当：

1. 理解 SVG 里 `<defs><symbol>` + `<use>` 的字形复用模型，知道 typst-svg 为什么要把字形集中写进 `<defs>`。
2. 能说出 `RenderedGlyph` 两个变体（`Path` 与 `Frame`）最终被写成什么样的 SVG 结构。
3. 掌握 `GlyphFrame` 的两种 `GlyphFrameItem`（`Tofu` / `Image`）如何**递归**调用 `render_shape` / `render_image`。
4. 理解 `write_glyph_defs` 末尾 `assert!(self.glyphs.is_empty())` 这条不变量（invariant）保护的是什么。

## 2. 前置知识

阅读本讲前，建议你先具备以下认知（已在 u1 / u2 / u4-l1 建立）：

- **SVG 的 `<defs>` 与 `<symbol>`**：SVG 允许把可复用的图形元素放进 `<defs>` 里「定义但不立即绘制」，再用 `<use href="#id">` 在多处实例化它。`<symbol>` 和 `<g>` 很像，但它天然就是「为复用而生」的容器，不会自己渲染，必须被 `<use>` 引用才显示。
- **Deduplicator 去重器**：typst-svg 用 `Deduplicator<T>` 给每个去重对象分配一个 `DedupId`（形如 `g3F9A...`，首字符是命名空间，后面是大写十六进制哈希）。字形用的命名空间字符是 `'g'`。详见 u2-l1 与 u6-l3。
- **字形的两条渲染路径**：`should_outline` 为真 → 轮廓字形（`Path`）；为假 → 彩色 / 位图 / SVG / tofu 字形（`Frame`）。这是 u4-l1 的核心结论。
- **RAII 元素 `SvgElem`**：构造即开标签、`Drop` 即关标签。详见 u2-l3。

一句话回顾数据流：`render_glyph` 把字形塞进 `self.glyphs` 去重表 → 渲染期只写一个引用它的 `<use>` → 全部页面渲染完毕后，`finalize` 统一调用 `write_glyph_defs` 把 `self.glyphs` 里的定义真正写出来。

## 3. 本讲源码地图

本讲只涉及极少量文件，但会跨到 `typst-library` 里的字形数据结构。

| 文件 | 作用 |
| --- | --- |
| `crates/typst-svg/src/text.rs` | `RenderedGlyph` 枚举定义；`render_glyph`（去重入表）；`render_path_glyph` / `render_image_glyph`（写 `<use>` 引用）；**`write_glyph_defs`（本讲主角）**。 |
| `crates/typst-svg/src/lib.rs` | `finalize` 编排（调用 `write_glyph_defs`）；`SVGRenderer.glyphs` 字段；`Deduplicator` / `DedupId`。 |
| `crates/typst-library/src/text/font/color.rs` | `GlyphFrame` 与 `GlyphFrameItem`（`Tofu` / `Image`）的定义，以及 `glyph_frame` / `should_outline`。这是 `Frame` 变体内容的真正来源。 |

> 说明：本讲引用的 typst-svg 文件用项目给定的 permalink base；typst-library 的文件改用仓库根的 permalink（已标注绝对路径）。

## 4. 核心概念与源码讲解

### 4.1 RenderedGlyph：字形渲染的两种结果

#### 4.1.1 概念说明

`render_glyph` 在去重时，并不只是记一个「这个字形出现过」，而是顺手把这个字形**渲染成什么形态**也存了下来——这就是 `RenderedGlyph`。它只有两个变体：

```rust
pub enum RenderedGlyph {
    /// A frame that contains an image glpyh.
    Frame(GlyphFrame),
    /// A path is a sequence of drawing commands.
    Path(EcoString),
}
```

- `Path(EcoString)`：一段已经生成好的 SVG path 数据（形如 `M..L..C..Z`），对应**轮廓字形**。注意：这条路径在生成时**就已经被预缩放到了 pt 空间**（u3-l1 讲过的 `SvgPathBuilder::with_scale`），所以它的坐标单位是 pt，使用处无需再缩放。
- `Frame(GlyphFrame)`：一个**字体单位（font unit / upem）空间下的小 Frame**，里面装着 `Tofu`（缺失字形的豆腐块）或 `Image`（彩色 / 位图 / SVG 字形图）。注意：这个 Frame 的尺寸是 `Size::splat(upem)`，**不含字号 text.size**，缩放推迟到 `<use>` 使用处。

这种二分正是 u4-l1 的核心取舍「路径廉价、图像昂贵」的直接体现：轮廓路径小，于是连字号一起预缩放、每种字号各存一份；图像数据大，于是只在字体单位空间存一份、各字号在 `<use>` 处临时缩放。

#### 4.1.2 核心流程

`render_glyph` 如何把一个字形变成 `RenderedGlyph` 并入去重表：

```text
对字形 glyph_id：
  ├─ should_outline(font, glyph_id) == true（轮廓路径）
  │     scale = text.size / upem
  │     key  = (font, glyph_id, scale)        ← 键里带 scale
  │     insert_with_val(key, || 提取轮廓 → RenderedGlyph::Path)
  │     → 若成功，render_path_glyph 写 <use>
  │
  └─ else（图像 / 彩色 / tofu）
        key  = (font, glyph_id)               ← 键里不带 scale
        insert_with_val(key, || glyph_frame → RenderedGlyph::Frame)
        → 若成功，render_image_glyph 写 <use>
```

两条路径都用 `self.glyphs.insert_with_val(...)` 入表（命名空间 `'g'`），拿到 `(DedupId, &mut Option<RenderedGlyph>)`。同一个 key 第二次出现时，闭包**不再执行**——轮廓不会被重新提取、图像 Frame 不会被重新构建，这正是去重省时的关键。

#### 4.1.3 源码精读

`RenderedGlyph` 的定义见 [src/text.rs:16-23](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L16-L23)：这里把字形分为「图像帧」与「路径数据」两类，后面 `write_glyph_defs` 正是按这两个变体 `match` 分流的。

两条入表分支在 `render_glyph` 里：[src/text.rs:64-79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L64-L79) 是轮廓分支——用 `SvgPathBuilder::with_scale(scale)` 提取轮廓并返回 `RenderedGlyph::Path(builder.finsish())`（注：`finsish` 是源码中的既有拼写，非笔误）；[src/text.rs:80-93](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L80-L93) 是图像分支——调用 `typst_library` 的 `glyph_frame` 拿到 `GlyphFrame` 并包成 `RenderedGlyph::Frame`。

注意两处 `key` 的差别：

- 轮廓分支 `let key = (&text.font, glyph_id, scale);`（含 `scale`）
- 图像分支 `let key = (&text.font, glyph_id);`（不含 `scale`）

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：弄清「为什么轮廓字形的去重键带 `scale`，而图像字形不带」对最终 SVG 体积的含义。
2. **操作步骤**：
   - 打开 [src/text.rs:64-93](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L64-L93)。
   - 假设同一字形 `A` 在文档里以 12pt 和 24pt 各出现一次。分别推演两条路径下，`self.glyphs` 里会存几条记录、`<use>` 处各自的 `transform` 是什么。
3. **需要观察的现象**：
   - 轮廓路径：12pt 与 24pt 因 `scale` 不同 → 两条不同的 `RenderedGlyph::Path`，两条 `<symbol>`；但每个 `<use>` **不带 scale**（路径已预缩放）。
   - 图像字形：12pt 与 24pt 因键相同 → **一条** `RenderedGlyph::Frame`；两个 `<use>` 各自带不同的 `scale`（见 `render_image_glyph`）。
4. **预期结果**：你能用一句话说清——「路径廉价所以按字号多存几份换 `<use>` 简单；图像昂贵所以只存一份、把缩放摊到每个 `<use>` 上」。

#### 4.1.5 小练习与答案

**练习 1**：如果硬要把图像字形的键也改成 `(font, glyph_id, scale)`，会有什么后果？
**答案**：每种字号都会单独构建并存储一份图像 Frame 与对应 `<symbol>`，文件体积会显著膨胀（尤其彩色 / 位图字形数据量大），但换来的是每个 `<use>` 可以不写 `scale`。typst-svg 选择了相反的取舍。

**练习 2**：`RenderedGlyph::Path` 里的 `EcoString` 为什么不是 `String`？
**答案**：`EcoString` 是 ecow 提供的廉价、可克隆（小字符串写时复制）的字符串类型。`RenderedGlyph` 派生了 `Clone`，字形记录可能在去重 / 渲染流程中被复制，用 `EcoString` 能降低克隆开销。

---

### 4.2 write_glyph_defs：写入 `<defs><symbol>` 的复用模型

#### 4.2.1 概念说明

整篇文档渲染期间，`render_glyph` 只负责「入表 + 写 `<use>` 引用」，**从不真正写字形的几何**。所有字形的几何定义被统一推迟到 `finalize` 阶段，由 `write_glyph_defs` 集中写入一个 `<defs>` 块。这样做有三个好处：

1. **复用**：同一个字形（同 key）无论在文档里出现多少次，只定义一次，N 次 `<use>` 引用。
2. **体积**：把定义集中在文末，正文里只剩短小的 `<use href="#g...">`。
3. **解耦**：渲染期不必关心「定义该写在哪」，只管记账（去重表）；定义的输出形态由 `write_glyph_defs` 一处决定。

复用模型就是经典的 **`<defs><symbol>` + `<use>`**：`<symbol id="gXXXX">` 是模板，`<use xlink:href="#gXXXX">` 是实例化。

#### 4.2.2 核心流程

`write_glyph_defs` 的执行步骤：

```text
1. 早退守卫：若 self.glyphs 里所有值都是 None → 直接 return（没有可写的字形）。
2. 打开一个 <defs> 元素。
3. glyphs = std::mem::take(&mut self.glyphs)   ← 把表「搬走」，self.glyphs 变空
4. 遍历 (id, glyph) in glyphs.iter()：
      跳过 None
      打开 <symbol id=id overflow="visible">
        match glyph:
          Frame(frame) → 在字体单位空间里递归渲染（见 4.3）
          Path(path)   → 写 <path d=path/>
      （<symbol> 在此随作用域结束而关闭）
5. （<defs> 随作用域结束而关闭）
6. assert!(self.glyphs.is_empty())   ← 不变量断言
```

这里有几个精妙点：

- **`std::mem::take`**：把 `self.glyphs` 的内容**搬走**到一个局部变量 `glyphs`，原字段被替换成 `Default::default()`（空表）。这样后面遍历时拿到的是「按值拥有」的表，而 `self` 的字段已空。
- **`overflow="visible"`**：`<symbol>` 默认会建立一个视口并裁剪超出范围的内容；字形轮廓常常延伸到基线上下（比如降部、升部、甚至负坐标），加 `overflow="visible"` 是为了确保字形不被意外裁掉。
- **`<symbol>` 没有 `width/height/viewBox`**：因此它直接沿用 `<use>` 处的坐标系，由使用处的 `transform` / `x` / `y` 决定最终落点。

#### 4.2.3 源码精读

`write_glyph_defs` 全文见 [src/text.rs:183-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L183-L219)。逐段说明：

- [src/text.rs:185-187](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L185-L187)：早退守卫。`insert_with_val` 的值类型是 `Option<RenderedGlyph>`，`None` 表示「键存在但字形无法渲染」（例如某些字体提取轮廓返回 `None`）。若**全部**都是 `None`，就没有任何字形要写，直接返回，连 `<defs>` 都不生成。
- [src/text.rs:189-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L189-L196)：打开 `<defs>`，`mem::take` 搬走表，循环里为每个有效字形打开 `<symbol>` 并写 `id`（即 `DedupId`）与 `overflow="visible"`。
- [src/text.rs:198-213](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L198-L213)：按变体分流——`Frame` 走 4.3 讲的递归渲染，`Path` 最简单：`symbol.elem("path").attr("d", path)`，即在 `<symbol>` 里塞一个 `<path d="..."/>`。
- [src/text.rs:216-218](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L216-L218)：注释说明「glyphs 已被 take 走，写定义过程中不该再产生新字形」，然后用 `assert!(self.glyphs.is_empty())` 强制保证（详见 4.3.4）。

`write_glyph_defs` 的调用点是 `finalize`，见 [src/lib.rs:411-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419)。`finalize` 按固定顺序写出全部 8 类定义，字形定义排在最前（[src/lib.rs:412](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L412)），随后才是裁剪路径、渐变、平铺等。

`self.glyphs` 字段本身定义在 [src/lib.rs:191-192](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L191-L192)，构造时用命名空间字符 `'g'`（[src/lib.rs:275](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L275)），所以所有字形 `DedupId` 都以 `g` 开头。

#### 4.2.4 代码实践（本讲主实践 · 源码阅读型）

1. **实践目标**：分别追踪 `RenderedGlyph::Path` 与 `RenderedGlyph::Frame` 最终被写成什么样的 SVG 结构。
2. **操作步骤**：
   - 读 `write_glyph_defs` 的 `match`（[src/text.rs:198-213](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L198-L213)）。
   - 对照引用侧：`render_path_glyph` 写的 `<use>`（[src/text.rs:143-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L143-L146)）与 `render_image_glyph` 写的 `<use>`（[src/text.rs:110-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L110-L112)）。
3. **需要观察的现象**：手画出两种完整结构（`XXX` 代表哈希）。

   `Path` 变体的定义（轮廓字形）：

   ```xml
   <symbol id="gXXX" overflow="visible">
     <path d="M..L..C..Z"/>     <!-- 已预缩放到 pt -->
   </symbol>
   ```

   引用处（轮廓字形，无 scale，路径已含字号）：

   ```xml
   <use xlink:href="#gXXX" x="x_offset" y="y_offset" fill=".." .. />
   ```

   `Frame` 变体的定义（图像 / tofu 字形，内容在字体单位空间，由 4.3 递归生成）：

   ```xml
   <symbol id="gXXX" overflow="visible">
     <!-- Tofu：单个 <path>；Image：单个 <image> -->
   </symbol>
   ```

   引用处（图像字形，带 scale 与第二次 Y 翻转）：

   ```xml
   <use xlink:href="#gXXX"
        transform="translate(x_offset,y_offset) scale(scale,-scale)"/>
   ```

4. **预期结果**：你能指出——两边的 `id` 都是 `g` 开头的同一个 `DedupId`；`Path` 侧 `<use>` 不缩放而 `Frame` 侧 `<use>` 缩放，对应 4.1 的键差异。
5. 若想用真实输出验证（编译一份含彩色 emoji 与普通正文的 typst 文档，导出 SVG 后搜索 `<symbol id="g`）：**待本地验证**（需 typst-cli 环境，本讲未实际运行）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `write_glyph_defs` 要在循环外只开**一个** `<defs>`，而不是每个字形开一个？
**答案**：`<defs>` 只是「定义区」的容器，合并成一个能减少标签数量、减小体积；具体复用是靠每个 `<symbol>` 各自的 `id` 实现的，与外层 `<defs>` 个数无关。

**练习 2**：早退守卫（`all |(_, g)| g.is_none()`）去掉会怎样？
**答案**：若没有任何有效字形，仍会生成一个空的 `<defs></defs>`。功能上不会出错，但会多输出无意义的空标签，略微增加体积。守卫正是为了避免输出空 `<defs>`。

**练习 3**：`<symbol>` 上的 `overflow="visible"` 改成默认（不写）会怎样？
**答案**：某些字形轮廓（尤其延伸到包围盒外、或坐标为负的降部）可能被 `<symbol>` 视口裁剪，导致字形边缘缺失。`visible` 确保完整绘制。

---

### 4.3 GlyphFrame 的递归渲染与 `mem::take` 不变量

#### 4.3.1 概念说明

`RenderedGlyph::Frame(GlyphFrame)` 里的 `GlyphFrame` 来自 `typst-library`（不是 typst-svg 自己的类型）。它代表「一个字形在**字体单位空间**下的微型 Frame」：

```rust
pub struct GlyphFrame {
    pub upem: Abs,
    pub item: GlyphFrameItem,
}

pub enum GlyphFrameItem {
    Tofu(Point, Shape),       // 缺失字形的「豆腐块」矩形
    Image(Point, Image, Size), // 彩色 / 位图 / SVG 字形图
}
```

关键点：

- `upem` 是字体的 `units_per_em`（每 em 的字体单位数）。`GlyphFrame::size()` 返回 `Size::splat(upem)`——一个 upem 见方的正方形，这就是 symbol 内部使用的坐标空间。
- `GlyphFrameItem::pos()` 返回该 item 在父 Frame 内的偏移点（`Tofu` 与 `Image` 各自携带一个 `Point`）。
- `Tofu` 是当字体里找不到字形、且 `glyph_frame` 无法正常绘制时的兜底矩形；`Image` 是真正可绘制的彩色 / 位图 / SVG 字形。

「递归」的含义：`write_glyph_defs` 在 `Frame` 分支里，**并不自己手写**这个字形的几何，而是调用 typst-svg 已有的高层渲染器——`render_shape`（画 Tofu 矩形）或 `render_image`（画 Image）。也就是说，字形定义复用了和正文形状 / 图像完全相同的渲染路径。

#### 4.3.2 核心流程

`Frame` 分支的执行：

```text
let state = State::new(frame.size())          // size = Size::splat(upem)，transform = 单位矩阵
               .pre_translate(frame.item.pos()); // 把 item 的偏移吸收进 transform

match &frame.item {
    Tofu(_, shape)  => self.render_shape(&mut symbol, &state, shape),
    Image(_, image, size) => self.render_image(&mut symbol, &state, image, size),
}
```

这里构造的 `State` 是一个**全新的、字体单位空间下的渲染上下文**：`transform` 起步为单位矩阵（`State::new` 的语义，见 u2-l1），再用 `pre_translate` 垫上 item 的位置偏移。`render_shape` / `render_image` 就像画正文里的普通形状 / 图像一样，把这个字形的几何画进当前 `<symbol>`。

为什么可以这样「重用」？因为 `render_shape` 和 `render_image` 都接收 `&State`，对它们而言调用者是「正文渲染」还是「字形定义渲染」没有区别——它们只关心「在这个 State 下把这个形状 / 图像写进给定的 `SvgElem`」。这里 `SvgElem` 就是当前的 `<symbol>`。

接着是本讲最容易被忽略却最重要的一行——**不变量断言**：

```rust
assert!(self.glyphs.is_empty());
```

它的含义是：`write_glyph_defs` 执行完毕后，`self.glyphs` 必须为空。要理解为什么这是**必然成立的**，需要回答一个问题：在写字形定义的过程中，会不会产生新的字形？答案是**不会**——`render_shape` 只写填充 / 描边 / 路径（可能产生渐变、平铺、裁剪路径，但**不会**调用 `render_text` / `render_glyph`），`render_image` 只写一个 `<image>` 元素（同样不碰字形）。因此写字形定义绝不会往 `self.glyphs` 里新增条目。`mem::take` 在开头已把表搬空，写定义期间又没有新增，所以结尾必然为空。`assert!` 把这个「不成文的约定」变成**运行期硬保证**：一旦将来有人改坏了这个性质（例如让 `render_image` 里嵌入了文本），这行 assert 会立刻在测试 / 导出时炸出来。

#### 4.3.3 源码精读

`Frame` 分支在 [src/text.rs:199-209](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L199-L209)：构造字体单位空间的 `State`，按 `GlyphFrameItem` 两变体分发到 `render_shape` / `render_image`。

`GlyphFrame` / `GlyphFrameItem` 的定义在 typst-library：

- [`GlyphFrame` 结构体（含 `upem` 与 `item`）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L31-L36)
- [`GlyphFrame::size()` 返回 `Size::splat(upem)`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L38-L43)
- [`GlyphFrameItem` 枚举（`Tofu` / `Image`）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L62-L67)
- [`GlyphFrameItem::pos()` 取偏移点](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L69-L77)
- [`glyph_frame()`：构造 `GlyphFrame`（含 tofu 兜底逻辑）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L89-L103)

注意 `GlyphFrame` 还实现了 `From<GlyphFrame> for Frame`（就在 `pos()` 上方几行），把 tofu / image 推进一个 `Frame::soft`——这与 typst-svg 这里手写 `match` 分发是「同一份内容的两种消费方式」（typst-library 提供「变成通用 Frame」的入口，typst-svg 选择直接 match `item` 自己分发，避免多构造一层 Frame）。

最后，`assert` 见 [src/text.rs:218](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L218)。配合 `mem::take`（[src/text.rs:190](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L190)）一起读，正是 4.3.2 描述的不变量。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：论证「为什么写完字形定义后 `self.glyphs` 必然为空」。
2. **操作步骤**：
   - 读 `mem::take`（[src/text.rs:190](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L190)）与 `assert`（[src/text.rs:218](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L218)）。
   - 在 `Frame` 分支跟踪 `render_shape` 与 `render_image` 的实现（[src/shape.rs:13](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L13)、[src/image.rs:20](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L20)）。
3. **需要观察的现象**：确认这两个函数体内**没有任何**对 `self.glyphs` 的写入，也没有调用 `render_text` / `render_glyph`。
4. **预期结果**：你能写出推理链——`mem::take` 置空 → 写定义只调 `render_shape`/`render_image` → 二者不产生新字形 → `self.glyphs` 仍为空 → `assert` 成立。若有人未来在 `render_image` 里塞了嵌套文本（从而产生字形），`assert` 会在 `finalize` 时 panic，保护不变量。
5. **待本地验证**：可写一个单元测试，构造一个 `SVGRenderer`，往 `glyphs` 里塞一条 `RenderedGlyph::Frame`，调用 `write_glyph_defs` 后断言 `glyphs.is_empty()`。

#### 4.3.5 小练习与答案

**练习 1**：`Frame` 分支里 `State::new(frame.size())` 的 `transform` 初值是什么？为什么是它？
**答案**：`State::new` 把 `transform` 初始化为单位矩阵。因为这里进入的是一个全新的「字体单位空间」，需要从零开始建立坐标系，不能继承外层正文的累积变换。

**练习 2**：假如 `render_image` 的实现被改成「对图像做 OCR 再把识别出的文字当文本画出来」，`write_glyph_defs` 末尾的 `assert` 会怎样？
**答案**：OCR 会调用文本渲染，从而往 `self.glyphs` 写入新字形，`mem::take` 后原本为空的 `self.glyphs` 不再为空，`assert!(self.glyphs.is_empty())` 会 panic。这正是这条 assert 的价值——它把「写字形定义不应产生新字形」这一架构假设变成了可检测的硬约束。

**练习 3**：为什么 `GlyphFrame` 用 `upem`（字体单位）而不是 `text.size`（pt）来度量自己？
**答案**：因为图像字形在去重时**不区分字号**（键不含 scale，见 4.1）。把内容存在字体单位空间，就能让同字形的不同字号共用同一份定义，缩放由使用处的 `<use transform="scale(...)">` 完成。

## 5. 综合实践

把本讲三块知识串起来：**在一份 typst-svg 导出的 SVG 里，定位并解释一个具体的 `<symbol>` 字形定义。**

1. 准备一份 typst 文档，同时包含：一段普通正文（产生轮廓字形 `Path`）、一个彩色 emoji 或位图字形（产生 `Frame`/`Image`）、一个故意用不存在字形渲染的字符（产生 `Frame`/`Tofu`，例如某种缺字的字体下的生僻字）。
2. 用 typst 导出为 SVG（命令行 `typst compile doc.typ doc.svg`，**待本地验证**）。
3. 在输出里搜索 `<symbol id="g`，找到若干字形定义。对其中三个分别判断：
   - 它是 `Path` 变体（`<symbol>` 内只有一个 `<path d=...>`）还是 `Frame` 变体（内部是 `<path>` 即 tofu，或 `<image>` 即图像字形）？
   - 在文档正文中找到引用它的 `<use xlink:href="#g...">`，观察：`Path` 侧 `<use>` 是否带 `scale`？`Frame` 侧 `<use>` 的 `transform` 是否含 `scale(scale,-scale)`（两次 Y 翻转之一）？
4. 解释：为什么 `<defs>` 里所有 `<symbol id>` 都以 `g` 开头？（提示：`Deduplicator::new('g')`，命名空间字符。）
5. 解释：为什么这些 `<symbol>` 全部集中在文档末尾的一个 `<defs>` 里，而不是散落在正文各处？（提示：`finalize` 集中调用 `write_glyph_defs`。）

完成此实践后，你应该能对着任意一段 typst-svg 输出，把「正文里的 `<use>`」与「`<defs>` 里的 `<symbol>`」一一对应起来，并说清每个字形走的是 `Path` 还是 `Frame` 路径。

## 6. 本讲小结

- `RenderedGlyph` 把字形渲染结果分成两类：`Path`（已预缩放到 pt 的轮廓路径）与 `Frame`（字体单位空间的图像 / tofu 帧），对应「路径廉价、图像昂贵」的取舍。
- `render_glyph` 只做「入 `self.glyphs` 去重表 + 写 `<use>` 引用」；字形几何的真正写出被推迟到 `finalize` 里的 `write_glyph_defs`。
- `write_glyph_defs` 把所有字形集中写进**一个** `<defs>`，每个字形是一个带 `overflow="visible"` 的 `<symbol id="g...">`；`Path` 变体内部是 `<path d>`，`Frame` 变体内部由递归渲染产生。
- `Frame` 分支构造一个字体单位空间的全新 `State`，按 `GlyphFrameItem` 的 `Tofu` / `Image` 两变体**递归调用** `render_shape` / `render_image`，复用正文的高层渲染器。
- `std::mem::take` 把去重表搬空，结尾 `assert!(self.glyphs.is_empty())` 强制保证「写字形定义不会产生新字形」这一架构不变量。

## 7. 下一步学习建议

字形定义写完了，但 `finalize` 还要写出另外 7 类定义。建议接下来：

1. **u5 系列（绘制系统）**：`render_path_glyph` 里 `<use>` 的 `fill` / `stroke` 由 `write_fill` / `write_stroke` 产生，而它们会向 `self.gradients` / `self.tilings` 等去重表里记账。学完 u5 你就能把「字形 `<use>` 的渐变填充」与「`finalize` 里的 `<linearGradient>` 定义」也对应起来。
2. **u6-l3（Deduplicator 与 ID 编码）**：深入 `DedupId` 如何把 `u128` 哈希编成大写十六进制、为什么用 `hash128` 而非存原 key，理解本讲里所有 `g...` ID 的来历。
3. **u6-l4（链接、锚点与集成）**：看 `finalize` 如何把 8 类 `write_*` 串成完整定义区，以及 `<use>`/`<symbol>` 模式与 bundle / HTML 集成的关系。
