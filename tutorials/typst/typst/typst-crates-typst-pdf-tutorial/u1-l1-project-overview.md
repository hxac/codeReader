# 项目总览：typst-pdf 是什么

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `typst-pdf` 在整个 Typst 工程中的**定位与边界**——它做什么、不做什么。
- 读懂它的 `Cargo.toml`，理解它依赖了哪些 crate，尤其是核心后端 `krilla`。
- 读懂 `src/lib.rs` 的模块声明与公共入口，建立「**Typst Frame 树 → krilla 调用 → PDF 字节**」的整体心智模型。
- 知道接下来每篇讲义会深入哪个模块。

## 2. 前置知识

在开始之前，先建立两个直觉。不需要你现在完全理解，只要有个大致印象即可。

**第一，Typst 的排版结果是一棵 Frame 树。**
当你用 Typst 写一份文档并编译时，Typst 先做「排版（layout）」，把文字、图形、图像等内容安排到一个个矩形「页面」里。排版的最终产物不是 PDF，而是一种内存中的数据结构——`PagedDocument`，它由一页页 `Frame` 组成，每个 `Frame` 里又装着若干 `FrameItem`（文字、图形、图像、链接、子分组等）。这棵树是「已经摆好位置、画好大小」的内容描述。

**第二，PDF 是一种有规范的字节流。**
PDF 文件本质是一段遵循 PDF 规范的字节流，里面有对象、交叉引用表、字体、图像、结构树等。把这些内容**正确地序列化成字节**本身就是一件复杂、繁琐且容易出错的工作（要处理压缩、版本、校验等）。

`typst-pdf` 的任务，就是在这两者之间搭一座桥：**把已排好版的 Frame 树，翻译成对底层 PDF 库的调用**，让底层库去生成最终字节。理解这一点，是理解整个 crate 的钥匙。

## 3. 本讲源码地图

本讲只涉及两个文件，它们是整个 crate 的「门面」：

| 文件 | 作用 |
|------|------|
| [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/Cargo.toml#L1-L40) | 声明包元数据（名字、版本、描述）与全部依赖。看完它就知道这个 crate 「站在谁的肩膀上」。 |
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L1-L332) | crate 的根模块。包含文档注释、子模块声明（模块地图）、对外暴露的入口函数 `pdf()` / `pdf_in_bundle()`，以及导出配置 `PdfOptions` / `PdfStandards`。 |

> 说明：本讲是总览，我们只精读上面两个文件。真正的转换逻辑在 `src/convert.rs`，会在第 4.4 节给出一个「鸟瞰」并留到后续讲义深入。

## 4. 核心概念与源码讲解

### 4.1 typst-pdf 的定位：适配器，而非字节拼装器

#### 4.1.1 概念说明

很多人第一次接触 PDF 导出，会以为 `typst-pdf` 自己一行一行地把 PDF 字节拼出来。**这是本讲最需要纠正的误解。**

实际上，`typst-pdf` 是一个「**适配器层（adapter layer）**」：

- 它**不直接**写 PDF 的交叉引用表、对象流、压缩过滤等底层结构。
- 它把这些脏活累活全部委托给一个叫 **`krilla`** 的第三方 PDF 生成库。
- `typst-pdf` 自己做的事是：读懂 Typst 的 `PagedDocument` / `Frame` 树，然后**翻译**成一系列对 `krilla` 的调用（创建文档、加页面、画文字、画图形、嵌图像、建书签、建无障碍结构树……），最后调用 `krilla` 的 `finish()` 拿到字节。

这样做的好处很直接：Typst 团队可以专注在「如何把 Typst 的内容语义准确地表达出来」，而 PDF 规范的合规性与序列化细节交给专门的库去维护。

#### 4.1.2 核心流程

用一句话概括数据流：

```text
Typst 源码
   │  （typst-layout 排版）
   ▼
PagedDocument（一页页 Frame，每个 Frame 含若干 FrameItem）
   │  （typst-pdf 翻译）
   ▼
对 krilla 的调用（创建 Document / 加 Page / 画文字图形图像 / 建书签结构树）
   │  （krilla 序列化）
   ▼
PDF 字节（Vec<u8>）
```

关键点：`typst-pdf` 负责中间这一段「翻译」；头尾两端分别由 `typst-layout` 和 `krilla` 负责。

#### 4.1.3 源码精读

这个定位最直接的证据，来自 `src/lib.rs` 顶部的文档注释：

[`src/lib.rs:1`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L1) 这一行只有一句话：

```rust
//! Exporting Typst documents to PDF.
```

「Exporting（导出）」这个词很关键——它强调的是**把已有的文档转写出去**，而不是从零生成。输入是已经排好版的 Typst 文档，输出是 PDF 字节。

而「委托给 krilla」的证据，要看转换核心 `src/convert.rs` 的开头——它大量从 `krilla` 引入类型：

[`src/convert.rs:4-16`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L4-L16)（节选）

```rust
use krilla::configure::{ Configuration, PdfVersion, ... };
use krilla::destination::NamedDestination;
use krilla::embed::EmbedError;
use krilla::error::{KrillaError, LimitError};
use krilla::geom::{PathBuilder, Rect};
use krilla::page::{PageLabel, PageSettings};
use krilla::pdf::PdfError;
use krilla::surface::Surface;
use krilla::tagging::ArtifactType;
use krilla::{Document, SerializeSettings};
```

可以看到：文档（`Document`）、页面（`PageSettings`）、几何（`PathBuilder`）、配置（`Configuration`）、错误（`KrillaError`）……这些「PDF 概念」全都来自 `krilla`。`typst-pdf` 只是「调用来、调用去」。

而入口函数 `pdf()` 本身极其简短，它几乎只是把请求转发给内部的 `convert::convert()`：

[`src/lib.rs:35-38`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L35-L38)

```rust
#[typst_macros::time(name = "pdf")]
pub fn pdf(document: &PagedDocument, options: &PdfOptions) -> SourceResult<Vec<u8>> {
    convert::convert(document, options, &[], None)
}
```

注意它的输入 `document: &PagedDocument`——这正是 `typst-layout` 排版后的产物；输出 `Vec<u8>`——这就是最终 PDF 字节。一个函数签名就把「桥梁」的角色交代清楚了。

#### 4.1.4 代码实践

**实践目标**：用「适配器」的视角重新描述 typst-pdf 的职责。

**操作步骤**：

1. 打开 [`src/convert.rs:1-32`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L1-L32)，只看顶部的 `use` 语句。
2. 数一下：有多少个 `use` 是以 `krilla` / `krilla_svg` 开头的，有多少是以 `typst_` 开头的。
3. 对比两组：`krilla::*` 提供的是「PDF 世界的概念」，`typst_library::*` / `typst_layout::*` 提供的是「Typst 世界的内容类型」。

**需要观察的现象**：`convert.rs` 的导入里，`krilla` 与 `typst` 两侧的类型几乎一一对应（`krilla::page::PageSettings` ↔ `typst_library::layout::Frame`，`krilla::geom::PathBuilder` ↔ Typst 的曲线），这印证了「转换」的本质。

**预期结果**：你会得出一个判断——`convert.rs` 里绝大多数代码在做「读 Typst 类型 → 构造 krilla 类型」的映射，而真正的字节输出由 `krilla` 在 `finish()` 时完成。

> 本实践为「源码阅读型实践」，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：如果有一天 Typst 团队把底层 PDF 库从 `krilla` 换成另一个库（假设叫 `foo-pdf`），`typst-pdf` 里**哪一部分代码受影响最大**，哪一部分几乎不变？

> **参考答案**：受影响最大的是直接 `use krilla::*` 并构造 krilla 类型的转换逻辑（如 `convert.rs` 以及 `text`/`shape`/`paint`/`image` 等内容翻译器，还有 `PdfStandards` 里的 `ConfigurationBuilder`）。几乎不变的是「如何遍历 Typst Frame 树、如何理解 Typst 内容语义」这部分——因为 Typst 侧的输入模型（`PagedDocument` / `Frame` / `FrameItem`）没有变。这正是适配器层的好处：两侧解耦。

**练习 2**：`pdf()` 函数上方的 `#[typst_macros::time(name = "pdf")]` 起什么作用？它和「适配器」定位有什么关系？

> **参考答案**：这是一个计时宏，用于测量 PDF 导出阶段耗费的时间（便于性能剖析）。它说明 `typst-pdf` 是 Typst 编译流水线中一个**可独立测量、可被替换**的阶段——这进一步印证了它是一个边界清晰的适配器，而非与排版耦合在一起的代码。

---

### 4.2 Cargo.toml：包元数据与依赖清单

#### 4.2.1 概念说明

`Cargo.toml` 是一个 Rust crate 的「身份证 + 物料清单」。对 `typst-pdf` 来说，它尤其重要，因为**看懂它的依赖，就等于看懂了这个 crate 把工作委托给了谁**。

`typst-pdf` 是 Typst 主仓库（workspace）中的一个成员 crate，因此它的 `Cargo.toml` 里很多字段都写成 `{ workspace = true }`，表示「沿用 workspace 根目录的统一配置」。这一点初学者常困惑，需要特别解释。

#### 4.2.2 核心流程

读 `Cargo.toml` 的顺序：

1. 先看 `[package]` 段——这个 crate 叫什么、是什么。
2. 再看 `[dependencies]` 段——它依赖了谁。
3. 把依赖分成三类来理解：**Typst 内部 crate**、**PDF 后端 krilla**、**通用第三方工具库**。

#### 4.2.3 源码精读

**`[package]` 段**几乎全部继承自 workspace：

[`Cargo.toml:1-13`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/Cargo.toml#L1-L13)

```toml
[package]
name = "typst-pdf"
description = "PDF exporter for Typst."
version = { workspace = true }
rust-version = { workspace = true }
authors = { workspace = true }
edition = { workspace = true }
...
```

这里只有两个字段是本 crate 自己写的：`name = "typst-pdf"` 和 `description = "PDF exporter for Typst."`（PDF 导出器）。其余 `version`、`edition`、`license` 等都写 `{ workspace = true }`，意思是从仓库根目录的 `Cargo.toml` 取统一值，避免每个 crate 重复维护版本号。所以你不会在本文件里看到具体版本号——它们在仓库根的 `Cargo.toml` 里集中管理。

**`[dependencies]` 段**是重头戏：

[`Cargo.toml:15-36`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/Cargo.toml#L15-L36)

```toml
[dependencies]
typst-assets = { workspace = true }
typst-library = { workspace = true }
typst-macros = { workspace = true }
typst-syntax = { workspace = true }
typst-timing = { workspace = true }
typst-utils = { workspace = true }
typst-layout = { workspace = true }
...
krilla = { workspace = true }
krilla-svg = { workspace = true }
...
```

同样，具体版本号写在 workspace 根目录。要了解 `krilla` 的版本与特性，得去仓库根的 [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml#L80-L81)：

```toml
krilla = { version = "0.8.2", default-features = false, features = ["raster-images", "comemo", "rayon", "pdf"] }
krilla-svg = "0.8.1"
```

注意 `krilla` 关闭了默认特性（`default-features = false`），只按需开启 `raster-images`（栅格图像）、`comemo`（与 Typst 共享同一套记忆化机制）、`rayon`（并行）、`pdf`（PDF 输出）。这种「按需启用」能控制二进制体积和编译时间。

为了便于理解，我把全部依赖按角色分成三类：

| 分类 | 依赖 | 在 typst-pdf 中扮演的角色 |
|------|------|---------------------------|
| **PDF 后端** | `krilla`、`krilla-svg` | 真正生成 PDF 字节的核心库；`krilla-svg` 负责把 SVG 字形/图像交给 krilla 渲染。 |
| **Typst 内部** | `typst-layout` | 提供 `PagedDocument`——即 `pdf()` 函数的**输入**。 |
| | `typst-library` | 提供 Typst 的核心类型：诊断（`SourceResult`）、`Frame`/`FrameItem`、`Smart`、`HeadingElem`、`FontInstance`、`Geometry`/`Paint` 等。 |
| | `typst-syntax` | 提供 `Span`，用于把错误定位回 Typst 源码位置。 |
| | `typst-macros` | 提供 `#[typst_macros::time]` 计时宏。 |
| | `typst-timing`、`typst-utils`、`typst-assets` | 计时、通用工具、捆绑资源。 |
| **第三方通用库** | `comemo` | 记忆化（缓存字体转换等昂贵计算）。 |
| | `ecow` | `EcoString` / `EcoVec`，用于构造错误与提示字符串。 |
| | `image` | 栅格图像解码。 |
| | `infer` | 依据文件头推断类型（用于附件是否压缩的判断）。 |
| | `indexmap`、`rustc-hash`、`smallvec`、`az`、`bytemuck` | 高效容器与字节/数值处理。 |
| | `flate2` | 压缩（DEFLATE）。 |
| | `codex` | 把 PDF 作为图像嵌入（见图像处理讲义）。 |
| | `serde` | 序列化（`PdfStandard` 枚举派生 `Serialize`/`Deserialize`）。 |

> 提示：本表里很多第三方库的具体用法要到后续讲义才会遇到，现在只需知道「它们各自管一摊」。本讲重点关注最核心的 `krilla`、`typst-layout`、`typst-library`。

#### 4.2.4 代码实践

**实践目标**：亲手梳理 `typst-pdf` 的依赖，并理解三大核心依赖各提供什么。

**操作步骤**：

1. 打开 [`Cargo.toml:15-36`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/Cargo.toml#L15-L36)，把 `[dependencies]` 下的每一项抄写下来，按上面三类分组。
2. 打开仓库根目录的 `Cargo.toml`，找到 `krilla`、`krilla-svg`、`typst-layout`、`typst-library` 这几行，记录它们的版本号与启用的 features。
3. 用自己的话写一段约 100 字的说明，回答：**为什么 `typst-pdf` 需要 `krilla`、`typst-layout`、`typst-library` 这三者，它们各自提供什么？**

**需要观察的现象**：
- `typst-layout` 与 `typst-pdf` 的衔接点就是 `PagedDocument`——它是前者产出、后者消费的「合同」。
- `typst-library` 提供的几乎全是「Typst 内容的类型定义」，而 `krilla` 提供的几乎全是「PDF 概念的类型定义」。

**预期结果**：你的说明应大致包含以下三点——
- `krilla`：真正生成 PDF 字节的后端，typst-pdf 把所有 PDF 结构（文档、页、字体、结构树）都委托给它。
- `typst-layout`：提供排好版的输入 `PagedDocument`，是 typst-pdf 的数据来源。
- `typst-library`：提供 Typst 内容的类型（Frame、文字、图形、颜色、标题等），typst-pdf 据此理解「要导出什么」。

> 本实践为「源码阅读型实践」，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Cargo.toml` 里几乎所有字段都写 `{ workspace = true }`，而不是直接写版本号？

> **参考答案**：因为 `typst-pdf` 是 Typst 主仓库 workspace 的一个成员 crate。把版本号、作者、license 等统一放在仓库根 `Cargo.toml`，可以让所有 crate 保持一致（比如一次升级所有依赖、统一版本号），避免重复维护和版本漂移。这种写法在多 crate 工程里是 Rust 的常见实践。

**练习 2**：`krilla` 为什么显式写 `default-features = false` 再按需启用 `["raster-images", "comemo", "rayon", "pdf"]`？

> **参考答案**：为了「按需引入」。关闭默认特性可以避免带入 typst-pdf 用不到的代码，减小编译产物体积和编译时间；再显式开启 typst-pdf 确实需要的能力（栅格图像支持、与 Typst 一致的 comemo 记忆化、rayon 并行、PDF 输出）。这体现了对依赖体积与编译成本的有意控制。

---

### 4.3 src/lib.rs：模块地图与公共入口

#### 4.3.1 概念说明

`src/lib.rs` 是这个 crate 的「大堂」：它声明了 crate 内部有哪些子模块（模块地图），以及对使用者暴露哪些函数与类型（公共 API）。读完它，你就能知道「这个 crate 由哪些房间组成、从哪个门进去」。

#### 4.3.2 核心流程

读 `lib.rs` 分三步：

1. 看 `mod` 声明——了解内部有哪些子模块。
2. 看 `pub use` / `pub fn`——了解对外暴露什么。
3. 看核心结构体（本讲只点到 `PdfOptions`，留个印象，后续讲义详讲）。

#### 4.3.3 源码精读

**模块声明**给出了完整的「房间分布图」：

[`src/lib.rs:3-14`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L3-L14)

```rust
mod attach;
mod convert;
mod image;
mod link;
mod metadata;
mod outline;
mod page;
mod paint;
mod shape;
mod tags;
mod text;
mod util;
```

注意它们都用 `mod`（私有）声明，说明这些是实现细节，外部不应直接使用。从名字就能猜出各自职责——为后续讲义铺路：

| 模块 | 职责（后续讲义详解） |
|------|----------------------|
| `convert` | 编排核心，把整个导出流程串起来。 |
| `page` | 单页导出：尺寸、出血框、页码标签。 |
| `text` | 文字与字体转换。 |
| `shape` | 图形与几何。 |
| `paint` | 颜色、渐变、图案填充。 |
| `image` | 栅格/SVG/嵌入 PDF 图像。 |
| `link` | 链接与目的地址。 |
| `outline` | 书签大纲。 |
| `metadata` | 元数据与时间戳。 |
| `attach` | 文件附件。 |
| `tags` | tagged PDF（无障碍结构树），内部又分 `groups`/`tree`/`resolve`/`context`/`util`。 |
| `util` | 类型转换工具集（Typst 类型 ↔ krilla 类型）。 |

**对外暴露**很克制。先看 `pub use`：

[`src/lib.rs:16`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L16)

```rust
pub use self::metadata::{Timestamp, Timezone};
```

只把「时间戳」相关两个类型重新导出——因为它们出现在 `PdfOptions` 的字段里，调用方需要用到。

**两个公共入口函数**：

[`src/lib.rs:32-54`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L32-L54)

```rust
/// Export a document into a PDF file.
/// Returns the raw bytes making up the PDF file.
#[typst_macros::time(name = "pdf")]
pub fn pdf(document: &PagedDocument, options: &PdfOptions) -> SourceResult<Vec<u8>> {
    convert::convert(document, options, &[], None)
}

/// Export a document into a PDF file as part of a bundle.
#[typst_macros::time(name = "pdf in bundle")]
pub fn pdf_in_bundle(
    document: &PagedDocument,
    options: &PdfOptions,
    anchors: &[(Location, EcoString)],
    link_resolver: Tracked<LateLinkResolver>,
) -> SourceResult<Vec<u8>> {
    convert::convert(document, options, anchors, Some(link_resolver))
}
```

两个函数都只是 `convert::convert(...)` 的薄封装，区别在于参数：

- `pdf()`：**独立导出**一个 PDF。传给 `convert` 的 `anchors` 为空 `&[]`、`link_resolver` 为 `None`。
- `pdf_in_bundle()`：**把 PDF 作为「打包集（bundle）」的一员导出**。它多收了 `anchors`（一组 `Location` → 命名字符串，用于让 bundle 内其它文档链接进这份 PDF）和 `link_resolver`（用于解析跨文档链接）。

这就是为什么后续讲义要深入 `convert::convert()`——两个入口最终都汇聚到它。

**导出配置 `PdfOptions`** 在这里只是「见一面」，本讲只需知道它**集中承载所有导出选项**：

[`src/lib.rs:57-89`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L57-L89)（结构体定义，字段见注释）

它的字段包括 `ident`（文档稳定标识）、`creator`（`/Creator` 元数据）、`timestamp`（创建时间）、`page_ranges`（只导出部分页）、`standards`（PDF 标准合规）、`tagged`（是否写 tagged PDF）、`pretty`（是否人类可读格式）。这些字段的具体含义会在第 1 单元后续讲义（u1-l2、u1-l3）逐一讲解。

它的默认值由 `Default` 实现给出，值得瞄一眼，因为这决定了「不传任何选项时的默认行为」：

[`src/lib.rs:99-111`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L99-L111)

```rust
impl Default for PdfOptions {
    fn default() -> Self {
        Self {
            ident: Smart::Auto,
            creator: Smart::Auto,
            timestamp: None,
            page_ranges: None,
            standards: PdfStandards::default(),
            tagged: true,
            pretty: false,
        }
    }
}
```

注意 `tagged: true`——也就是说，**默认就会生成 tagged PDF（带无障碍结构）**；`pretty: false`——默认输出压缩而非人类可读。这些默认值会影响最终 PDF 的体积与可访问性，后续讲义会反复回到这里。

#### 4.3.4 代码实践

**实践目标**：凭模块名预测职责，并用源码注释验证。

**操作步骤**：

1. 对照上面的 12 个模块表格，在不看后续讲义的前提下，用一句话写下你对每个模块职责的猜测。
2. 打开 `src/convert.rs`，找到 `convert::convert()` 函数体，观察它内部调用了哪些子模块的函数（你会看到类似 `handle_text`、`handle_shape`、`handle_image`、`handle_link`、`attach_files`、`build_metadata`、`build_outline`、`tags::...` 等调用）。
3. 把你在第 1 步的猜测，与第 2 步观察到的实际调用对照，修正理解。

**需要观察的现象**：`convert()` 几乎把每个子模块都「指挥」了一遍，它是名副其实的编排中心（orchestrator）。

**预期结果**：你会确认——`convert.rs` 是「总调度」，其余模块是「被调度的翻译器」，而 `lib.rs` 只负责把这一切收拢成两个公共入口。

> 本实践为「源码阅读型实践」，无需运行命令。如果你想在本地实际编译本 crate，可在仓库根目录运行 `cargo build -p typst-pdf`；若想运行其测试，可运行 `cargo test -p typst-pdf`（具体命令以仓库根的构建配置为准，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `attach`、`convert`、`text` 等模块都用 `mod`（私有）声明，而对外只 `pub use` 了 `Timestamp`、`Timezone` 两个类型？

> **参考答案**：因为这些模块是实现细节，外部使用者只需要「传入 `PagedDocument` + `PdfOptions`，拿回 PDF 字节」这一接口。把内部模块设为私有，可以自由重构而不破坏调用方。`Timestamp`/`Timezone` 之所以公开，是因为它们出现在公共类型 `PdfOptions` 的字段中，调用方构造选项时必须能命名它们。这体现了「最小暴露」的 API 设计原则。

**练习 2**：`pdf()` 和 `pdf_in_bundle()` 的函数体都只有一行 `convert::convert(...)`，为什么要分成两个公共函数？

> **参考答案**：因为它们面向两种不同的使用场景。`pdf()` 用于「单独导出一个 PDF」，参数简单；`pdf_in_bundle()` 用于「把 PDF 作为多文档打包集（bundle）的一员」，需要额外的 `anchors`（让别的文档能链接进来）和 `link_resolver`（解析跨文档链接）。用两个语义清晰的入口，比用一个带很多 `Option` 参数的函数更易用、更不易出错。

---

### 4.4 整体心智模型：Frame 树 → krilla 调用 → PDF 字节

#### 4.4.1 概念说明

把前三个模块串起来，我们得到本讲最重要的一个心智模型。请把它牢牢记住，它会贯穿整本手册。

`typst-pdf` 是一条**单向流水线上的翻译器**：上游送来排好版的 Frame 树，它翻译成对 `krilla` 的调用，下游 `krilla` 吐出 PDF 字节。它本身**无状态地消费输入、产出输出**，所有「PDF 规范怎么写」的知识都封装在 `krilla` 里。

#### 4.4.2 核心流程

把这条流水线再展开一层，标出每一阶段由谁负责：

```text
┌─────────────────┐   ┌──────────────────────────────┐   ┌──────────┐
│  Typst 源码      │──▶│  typst-layout：排版           │──▶│ PagedDoc │
│  (.typ 文件)     │   │  产出 PagedDocument           │   │ (Frame 树)│
└─────────────────┘   └──────────────────────────────┘   └────┬─────┘
                                                                │
                              typst-pdf 的职责从这里开始 ───────┘
                                                                │
┌──────────────────────────────────────────────────────────────▼─────────┐
│  typst-pdf（适配器 / 翻译器）                                            │
│  ├─ lib.rs：pdf() / pdf_in_bundle() 入口 → 转 convert::convert()         │
│  ├─ convert.rs：编排（建 krilla Document、转页面、建书签/元数据/结构树）│
│  ├─ page/text/shape/paint/image/link/outline/metadata/attach：内容翻译 │
│  └─ tags/：tagged PDF 无障碍结构树                                       │
└──────────────────────────────────────────────────────────────┬─────────┘
                                                                │
                                对 krilla 的调用从这里进入 ─────┘
                                                                │
┌──────────────────────────────────────────────────────────────▼─────────┐
│  krilla + krilla-svg：真正的 PDF 生成库                                  │
│  接收 Document / Page / Surface / TagTree 等调用 → 序列化 → Vec<u8> 字节 │
└──────────────────────────────────────────────────────────────┬─────────┘
                                                                │
                                                     PDF 字节 ◀┘
```

要点：
1. **输入边界**是 `PagedDocument`（来自 `typst-layout`）。
2. **输出边界**是 `Vec<u8>`（由 `krilla` 的 `finish()` 产生）。
3. **中间所有代码**都在做「把 Typst 内容翻译成 krilla 调用」。

#### 4.4.3 源码精读

这条心智模型在 `convert::convert()` 的开头体现得最清楚——它一开始就把 `PdfOptions` 翻译成 `krilla` 的 `SerializeSettings`，并据此创建 krilla 文档：

[`src/convert.rs:48-66`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L48-L66)（节选）

```rust
pub fn convert(
    typst_document: &PagedDocument,
    options: &PdfOptions,
    anchors: &[(Location, EcoString)],
    link_resolver: Option<Tracked<LateLinkResolver>>,
) -> SourceResult<Vec<u8>> {
    let settings = SerializeSettings {
        compress_content_streams: !options.pretty,
        ...
        configuration: options.standards.config,
        enable_tagging: options.tagged,
        ...
    };

    let mut document = Document::new_with(settings);
    ...
}
```

可以看到：`PdfOptions`（Typst 侧的选项）被逐字段映射成 `krilla::SerializeSettings`（krilla 侧的选项），然后用它创建一个 krilla `Document`。这就是「翻译」在最高层的体现。后续每一页、每一段文字、每一个图形，都是同样的「读 Typst 类型 → 调 krilla 接口」模式。至于 `Document` 内部如何最终变成字节，则完全是 `krilla` 的事，不在 `typst-pdf` 的关注范围内。

#### 4.4.4 代码实践

**实践目标**：把本讲的三个关键文件（`Cargo.toml`、`lib.rs`、`convert.rs` 开头）连成一条完整的故事线。

**操作步骤**：

1. 从 [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/Cargo.toml#L1-L40) 确认：输入侧依赖 `typst-layout`，输出侧依赖 `krilla`。
2. 从 [`src/lib.rs:35-38`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L35-L38) 确认：入口函数签名是「`PagedDocument` 进，`Vec<u8>` 出」。
3. 从 [`src/convert.rs:48-66`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L48-L66) 确认：第一步就是把 Typst 选项翻译成 krilla 设置并创建 krilla `Document`。
4. 用一句话写下这条故事线。

**需要观察的现象**：三个文件正好覆盖了「依赖谁 → 入口签名 → 内部第一步」三个角度，它们互相印证同一个结论。

**预期结果**：你的故事线应类似——「typst-pdf 依赖 typst-layout 拿到排好版的 `PagedDocument`，通过 `pdf()` 入口接收它，在 `convert()` 里把选项翻译成 krilla 设置、创建 krilla 文档，再逐项翻译所有内容，最后由 krilla 输出 PDF 字节」。

> 本实践为「源码阅读型实践」，无需运行命令。

#### 4.4.5 小练习与答案

**练习 1**：如果有人问「typst-pdf 里哪个函数是真正的『把字节写出来』？」你应该怎么回答？

> **参考答案**：**不在 typst-pdf 里**。typst-pdf 只负责调用 krilla；真正把字节序列化出来的是 krilla（具体是 krilla 在文档 `finish()` 时完成的）。在 typst-pdf 的 `convert::convert()` 末尾会调用 krilla 的收尾逻辑拿到 `Vec<u8>`。这再次印证了 typst-pdf 的适配器定位——它「指挥」，krilla「施工」。

**练习 2**：本节的流水线图里，typst-pdf 与上游 typst-layout、下游 krilla 之间的「合同（接口）」分别是什么？

> **参考答案**：与上游 typst-layout 的合同是 `PagedDocument`（及其包含的 `Frame`/`FrameItem` 树）；与下游 krilla 的合同是 krilla 自己定义的一组类型与接口（`Document`、`PageSettings`、`Surface`、`SerializeSettings`、`TagTree` 等）。typst-pdf 的全部工作就是在这两份合同之间做翻译。

## 5. 综合实践

把本讲所有知识点串起来，完成下面这个总览任务：

**任务**：假设你要向一位刚加入团队的同事介绍 `typst-pdf`，请基于真实源码准备一份「5 分钟入门说明」，必须包含：

1. **一句话定位**：引用 [`src/lib.rs:1`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L1) 的文档注释与 [`Cargo.toml:3`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/Cargo.toml#L3) 的 `description`，说明它是什么。
2. **架构定位**：用第 4.4 节的流水线图，解释它是「适配器」，并说明它**不**做哪些事（不排版、不直接拼 PDF 字节）。
3. **依赖三件套**：解释 `krilla`、`typst-layout`、`typst-library` 各提供什么（可引用第 4.2 节的表格）。
4. **模块地图**：列出 [`src/lib.rs:3-14`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L3-L14) 的 12 个模块，并指出 `convert` 是编排核心。
5. **入口与默认行为**：说明 `pdf()` 的输入输出类型，以及 `PdfOptions::default()` 中 `tagged: true`、`pretty: false` 的含义。

**验收标准**：说明里出现的每一个结论，都应能在本讲引用的源码片段中找到依据，不夹带未在源码中出现的内容。

## 6. 本讲小结

- `typst-pdf` 是 Typst 的 **PDF 导出器**，定位是**适配器层**：它不自己拼装 PDF 字节，而是把 Typst 排版产物翻译成对底层库 `krilla` 的调用。
- 它的输入是 `typst-layout` 产出的 `PagedDocument`（一棵 Frame 树），输出是 `Vec<u8>`（PDF 字节），核心心智模型是「**Frame 树 → krilla 调用 → PDF 字节**」。
- `Cargo.toml` 的依赖可分三类：PDF 后端（`krilla` / `krilla-svg`）、Typst 内部 crate（`typst-layout` 提供输入、`typst-library` 提供内容类型等）、第三方通用库。
- `src/lib.rs` 声明了 12 个内部子模块（`convert` 是编排核心，其余是内容翻译器与 `tags` 结构树），对外只暴露 `pdf()` / `pdf_in_bundle()` 两个入口、`PdfOptions` / `PdfStandards` 配置，以及 `Timestamp` / `Timezone` 两个类型。
- 两个入口函数最终都汇聚到 `convert::convert()`，它先把 `PdfOptions` 翻译成 `krilla::SerializeSettings` 并创建 krilla `Document`，再逐项翻译所有内容。
- 默认配置下 `tagged: true`（生成无障碍结构）、`pretty: false`（输出压缩），这些默认值会影响最终 PDF 的可访问性与体积。

## 7. 下一步学习建议

本讲建立了全局认知，接下来建议按学习路线继续：

- **下一步必读**：u1-l2《公共 API：pdf()、pdf_in_bundle() 与 PdfOptions》——逐字段详解 `PdfOptions` 的每个选项与默认值含义。
- **紧接着**：u1-l3《PDF 标准与校验配置》——理解 `PdfStandard` / `PdfStandards` 如何与 krilla 的配置协作。
- **想看模块全貌**：u1-l4《目录结构与构建运行方式》——通览 `src/` 与 `tags/` 子系统。
- **源码深入起点**：当你完成第一单元后，可进入 u2-l5《convert() 编排》，真正走进 `src/convert.rs` 的转换主链路。

建议你现在就打开 [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L1-L332) 与 [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/Cargo.toml#L1-L40) 对照本讲再过一遍，确认每个结论都有源码依据，再进入下一篇。
