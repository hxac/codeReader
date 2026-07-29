# typst-bundle 是什么：多文件输出目标概览

## 1. 本讲目标

本讲是 typst-bundle 学习手册的第一篇。读完本讲后，你应该能够：

- 说清楚 Typst 的三种「输出目标（target）」——paged、html、bundle——分别是什么，以及 bundle 与前两者的根本区别。
- 理解为什么 Typst 需要一个「多文件输出」目标：一次编译同时产出多个文档（PDF/SVG/PNG/HTML）和原始 asset 文件。
- 知道 bundle 目前是**实验性特性**，必须通过 `--features bundle` 开启，否则会被拒绝或给出警告。
- 看懂 `typst-bundle` 这个 crate 在整个 typst 工作区里的位置，以及它依赖了哪些兄弟 crate。

本讲不要求你已经读过 Typst 的任何源码。我们会从最顶部的注释开始，一步一步往下看。

## 2. 前置知识

在进入源码之前，先用通俗的话把几个概念讲清楚。

### 2.1 什么是「编译目标（target）」

平时你用 Typst，最常见的是把一个 `.typ` 文件编译成**一个 PDF**。这件事背后，Typst 其实先做了一整套「排版（layout）」，得到一个内存里的 `PagedDocument`（分页文档），再把这份分页文档导出成 PDF / PNG / SVG。

不同输出形态对应不同的「目标」：

| 目标（target） | 典型产出 | 一次编译产出几个文件 |
| --- | --- | --- |
| `paged` | 一个 PDF / PNG / SVG | 1 个（或按页拆成多个图片） |
| `html` | 一个 HTML | 1 个 |
| `bundle` | 多个文档 + 多个原始文件 | **多个** |

`bundle` 就是本系列讲义的主角：它打破了「一次编译 = 一个文件」的限制。

### 2.2 「document」和「asset」是什么

在一个 bundle 里，最终落盘的文件被分成两类：

- **document（文档）**：由 Typst 真正排版/渲染出来的内容，格式可以是 PDF、SVG、PNG 或 HTML。对应 Typst 源码里的 `#document(...)` 元素。
- **asset（资产）**：不经过排版、原样写入的字节，比如一段 JSON、一张已经生成好的图片、一份纯文本。对应 `#asset(...)` 元素。

你可以把 bundle 想象成「一次编译，产出一整个目录的网站或资料包」。

### 2.3 什么是「实验性特性（feature）」

Typst 里有一些尚未稳定的能力，被放在「feature 开关」后面。CLI 通过 `--features <名字>` 来开启它们。bundle 就是其中之一。开启后 Typst 会明确告诉你「这是实验性的，行为可能随时变化」，避免你在生产环境里误用。

> 术语提示：本讲会出现两个层面的 `Target` 和 `Feature`——一个是编译器内核（`typst` / `typst-library`）里的枚举，一个是 CLI 参数解析（`typst-cli`）里的枚举。两者一一对应，后面会讲清楚它们怎么对接。

## 3. 本讲源码地图

本讲涉及的关键文件如下。先建立整体印象，再逐个精读。

| 文件 | 作用 |
| --- | --- |
| `crates/typst-bundle/src/lib.rs` | bundle crate 的入口。定义 `Bundle`、`BundleFile`、`BundleDocument` 等核心数据结构，以及编译主函数 `bundle()` / `bundle_impl()`。 |
| `crates/typst-bundle/Cargo.toml` | bundle crate 的依赖清单，能直接看出它在工作区里依赖了哪些兄弟 crate。 |
| `crates/typst-library/src/foundations/target.rs` | 定义内核里的 `Target` 枚举（`Paged` / `Html` / `Bundle`）和 `Output` trait。 |
| `crates/typst/src/lib.rs` | 编译器顶层入口。定义泛型编译函数 `compile::<T>()`，以及 bundle 的 feature 开关与警告 `warn_or_error_for_bundle()`。 |
| `crates/typst-library/src/lib.rs` | 定义内核里的 `Feature` 枚举（`Html` / `Bundle` / `A11yExtras`）。 |
| `crates/typst-cli/src/args.rs`、`world.rs`、`compile.rs` | CLI 侧把命令行参数映射成内核的 `Target` / `Feature`，并真正驱动 bundle 编译与落盘。 |

> 提示：本讲重点是「认识定位」，所以我们只读上面这些文件的**入口和声明部分**，深入到编译主流程、数据模型、导出、内省等细节会放到后续讲义。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块。

### 4.1 bundle 输出目标与定位

#### 4.1.1 概念说明

Typst 的编译入口是一个**泛型函数** `compile::<T>()`，其中的类型参数 `T` 就是「你想要哪种输出」。每种输出都实现了 `Output` trait，并且和 `Target` 枚举的一个变体一一对应。

bundle 的定位，源码顶部一句话就讲清楚了：

> `Multi-file output for Typst.`（Typst 的多文件输出。）

也就是说，`typst-bundle` 这个 crate 存在的意义，就是给 Typst 增加「一次编译、多个文件」这种输出能力。它是和「单文件 PDF/SVG」并列的第三类目标。

#### 4.1.2 核心流程

把「目标」和「输出类型」对应起来的逻辑可以概括为：

```text
用户选择 bundle 输出
   ──▶ compile::<Bundle>(world)        // T = Bundle
           ──▶ T::target() == Target::Bundle
           ──▶ T::create(engine, content, styles)
                   ──▶ bundle()  ──▶ bundle_impl()
```

- `Target` 是一个枚举，有三个变体：`Paged`、`Html`、`Bundle`。
- `Output` trait 要求每个输出类型提供 `target()`（声明自己属于哪个目标）和 `create()`（真正构造输出）。
- 对 bundle 来说，输出类型是 `Bundle`，它的 `target()` 返回 `Target::Bundle`，`create()` 调用 `bundle()`。

#### 4.1.3 源码精读

先看 bundle crate 最顶部的文档注释，这一行就是它的「自我介绍」：

[crates/typst-bundle/src/lib.rs:1](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L1)
说明：整个 crate 的定位——「Typst 的多文件输出」。

再看内核里 `Target` 枚举的定义，注意 `Bundle` 变体上的注释，它点明了 bundle 能「从一个 Typst 项目产出多个 document 和 asset」：

[crates/typst-library/src/foundations/target.rs:65-76](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/target.rs#L65-L76)
说明：`Target` 枚举三变体；`Bundle` 注释解释了它的多文件本质。

`Output` trait 和 `Target` 是「1-1 关系」，这点写在 trait 文档里：

[crates/typst-library/src/foundations/target.rs:10-17](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/target.rs#L10-L17)
说明：`Output` trait，要求实现 `target()` 与 `create()`，并和 `Target` 变体一一对应。

最后看 `Bundle` 类型如何实现 `Output`，把上面三处串起来：

[crates/typst-bundle/src/lib.rs:56-72](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L56-L72)
说明：`impl Output for Bundle`：`target()` 返回 `Target::Bundle`，`create()` 委托给 `bundle()`。

```rust
impl Output for Bundle {
    fn introspector(&self) -> &dyn Introspector { self.introspector.as_ref() }
    fn target() -> Target { Target::Bundle }
    fn create(engine: &mut Engine, content: &Content, styles: StyleChain) -> SourceResult<Self> {
        bundle(engine, content, styles)
    }
}
```

`Bundle` 结构体本身的文档注释，给出了一个非常贴切的类比：**`Bundle` 之于 bundle 输出，就像 `PagedDocument` 之于 pdf/png/svg 输出**。

[crates/typst-bundle/src/lib.rs:37-54](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L37-L54)
说明：`Bundle` 是编译产物，是 bundle 输出格式下的「顶层文档对象」。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手确认「目标 ↔ 输出类型」的对应关系。

1. 实践目标：在源码里找出三种 target 分别由哪个输出类型承载。
2. 操作步骤：
   - 打开上面引用的 `target.rs:65-76`，记住三个 `Target` 变体。
   - 用编辑器搜索 `impl Output for`（在整个 `crates/` 下），找到 `PagedDocument`、`HtmlDocument`、`Bundle` 三处实现，分别记录它们的 `target()` 返回值。
3. 需要观察的现象：每个输出类型的 `target()` 都精确返回 `Target` 的某一个变体，三者互不重叠。
4. 预期结果：`Bundle::target()` 返回 `Target::Bundle`，与你刚看到的枚举变体一致。
5. 待本地验证：你可以在本地仓库执行搜索，确认实现位置。

#### 4.1.5 小练习与答案

**练习 1**：`Output` trait 和 `Target` 枚举是什么关系？

> **答案**：一一对应。每个实现了 `Output` 的类型通过 `target()` 声明自己属于 `Target` 的哪一个变体。

**练习 2**：为什么说 `Bundle` 之于 bundle，就像 `PagedDocument` 之于 pdf？

> **答案**：因为它们都是各自输出格式下「编译出来的顶层产物对象」。pdf 导出消费 `PagedDocument`，bundle 编译产出的就是 `Bundle`。

---

### 4.2 多文件输出的价值：document 与 asset

#### 4.2.1 概念说明

为什么要搞一个「多文件」目标？因为很多真实产出**本来就是一个文件集合**，而不是孤零零一个文件：

- 一个小网站：若干 HTML 页面 + 图片 + `manifest.json`。
- 一份资料包：一份 PDF 主文档 + 若干 PNG 附图 + 一份机器可读的 `meta.json`。

在传统 paged/html 目标下，你最多得到「一个」输出文件，额外的资源要么手动管理，要么塞进单一文件里。bundle 把这件事变成了编译器的一等能力：**你在 Typst 源码里用 `#document(...)` 声明文档、用 `#asset(...)` 声明原始文件，Typst 一次性把它们都生成出来。**

#### 4.2.2 核心流程

bundle 产出的所有文件，最终都被装进一个「路径 → 文件」的映射里：

```text
Bundle
  └─ files: IndexMap<VirtualPath, BundleFile>
                          │
                          ├── BundleFile::Document(BundleDocument)   // 排版/渲染出来的文档
                          └── BundleFile::Asset(Bytes)               // 原始字节，直通
```

- 路径用 `VirtualPath` 表示，可以带斜杠（自动创建中间目录）。
- `BundleFile` 是个枚举，区分「文档」和「资产」两种文件。
- 文档进一步分 `Paged`（PDF/SVG/PNG）和 `Html` 两种，详见后续讲义。

#### 4.2.3 源码精读

`Bundle` 结构体持有两个字段：所有文件 `files`，以及一个用于内省的 `introspector`（内省是进阶话题，本讲只认识它的存在）：

[crates/typst-bundle/src/lib.rs:44-54](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L44-L54)
说明：`Bundle` 由 `files`（路径到文件的映射）和 `introspector`（整体内省器）组成。

`BundleFile` 枚举把文件分成「文档」与「资产」两类，注释也点明了它们分别来自 `document` 和 `asset` 元素：

[crates/typst-bundle/src/lib.rs:74-82](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L74-L82)
说明：`BundleFile::Document` 来自 `document` 元素；`BundleFile::Asset(Bytes)` 是 `asset` 元素的原始字节。

```rust
pub enum BundleFile {
    Document(BundleDocument),
    Asset(Bytes),
}
```

文档又细分两种格式家族：

[crates/typst-bundle/src/lib.rs:84-92](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L84-L92)
说明：`BundleDocument` 分 `Paged`（分页格式，附额外信息）与 `Html` 两类。

用户侧的两个元素在 `typst-library` 里定义。`document` 元素的第一个字段就是 `path`，并且明确标注「只在 bundle 目标下支持」：

[crates/typst-library/src/model/document.rs:126-142](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L126-L142)
说明：`DocumentElem` 的 `path`（在 bundle 中的落盘路径）和 `format`（导出格式，可由扩展名推断）。

`asset` 元素同样以 `path` + 原始 `data` 为核心：

[crates/typst-library/src/model/asset.rs:53-66](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/asset.rs#L53-L66)
说明：`AssetElem` 的 `path` 与 `data`（字符串按 UTF-8 编码，或直接给字节）。

> 这两个元素的完整字段与「元数据如何跨文档传播」是第 3 单元（u3-l2）的主题，本讲只需建立「document = 排版产物、asset = 原始字节」的印象。

#### 4.2.4 代码实践

这是一个**源码阅读 + 小写作型实践**。

1. 实践目标：把「文件类型」和「源码里的类型」对上号。
2. 操作步骤：
   - 阅读上面的 `BundleFile` 与 `BundleDocument` 定义。
   - 在纸上完成下表的填写：`a.pdf`、`b.svg`、`c.png`、`d.html`、`data.json`（原始 JSON）分别会落到哪个 `BundleFile` / `BundleDocument` 变体。
3. 需要观察的现象：四个带扩展名的文件会被识别成 document；纯数据文件会被当成 asset。
4. 预期结果：`a.pdf/b.svg/c.png` → `BundleDocument::Paged`；`d.html` → `BundleDocument::Html`；`data.json` → `BundleFile::Asset`。
5. 待本地验证：格式如何由扩展名推断，会在 u2-l2 的 `determine_format_from_path` 里详讲。

#### 4.2.5 小练习与答案

**练习 1**：如果我想要在 bundle 里输出一段机器可读的 JSON，应该用 `#document` 还是 `#asset`？为什么？

> **答案**：用 `#asset`。JSON 是原始字节，不需要 Typst 排版；`#asset` 会原样写入文件。

**练习 2**：`BundleDocument` 为什么要把 `Paged` 和 `Html` 分开？

> **答案**：因为分页格式（PDF/SVG/PNG）和 HTML 的排版/渲染管线完全不同，分别对应 `typst-layout` 与 `typst-html`，所以产物类型也分开。

---

### 4.3 实验性 feature 开关与警告

#### 4.3.1 概念说明

bundle 目前还是**实验性特性**。Typst 用两层机制来管理它：

1. **feature 开关**：内核里有一个 `Feature` 枚举，`Bundle` 是其中一项。CLI 通过 `--features bundle` 来开启它。
2. **编译期把关**：泛型编译函数 `compile::<T>()` 在真正开始排版之前，会检查目标对应的 feature 有没有被开启。对 bundle 来说，函数 `warn_or_error_for_bundle()` 负责「开了就警告、没开就报错」。

这种设计既允许早期用户试用，又防止不知情的人在生产环境依赖尚未稳定的行为。

#### 4.3.2 核心流程

```text
compile::<Bundle>(world)
  └─ compile_impl::<Bundle>
       └─ match T::target() {
              Target::Bundle => warn_or_error_for_bundle(features, sink)
                                  ├─ Feature::Bundle 已开启 → sink.warn(...)   // 警告，继续编译
                                  └─ 未开启             → bail!(...)           // 直接报错
          }
```

也就是说：**没开 feature 直接编译 bundle 会被拒绝**；开了则会收到一条「这是实验特性」的警告，然后正常往下编译。

#### 4.3.3 源码精读

泛型编译入口 `compile::<T>()`，类型参数 `T: Output`：

[crates/typst/src/lib.rs:63-82](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L63-L82)
说明：`compile` 按 `T: Output` 分发，返回 `Warned<SourceResult<T>>`。

`compile_impl` 在编译开始前，按目标做 feature 把关——这正是 bundle 被「拦下来检查」的地方：

[crates/typst/src/lib.rs:104-109](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L104-L109)
说明：根据 `T::target()` 分派；`Target::Bundle` 走 `warn_or_error_for_bundle`。

`warn_or_error_for_bundle` 的实现：开启时给警告，未开启时 `bail!`（致命错误）：

[crates/typst/src/lib.rs:268-286](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L268-L286)
说明：bundle feature 的「警告或报错」逻辑。

```rust
fn warn_or_error_for_bundle(features: &Features, sink: &mut Sink) -> SourceResult<()> {
    if features.is_enabled(Feature::Bundle) {
        sink.warn(warning!(.., "bundle export is experimental"; ..));
    } else {
        bail!(.., "bundle export is only available when `--features bundle` is passed"; ..);
    }
    Ok(())
}
```

内核里的 `Feature` 枚举，`Bundle` 与 `Html`、`A11yExtras` 并列：

[crates/typst-library/src/lib.rs:270-284](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/lib.rs#L270-L284)
说明：`Feature` 枚举定义；`Bundle` 是其中一项。

CLI 侧的对接：命令行参数里的 `Feature` 枚举被转换成内核的 `typst::Feature`：

[crates/typst-cli/src/world.rs:349-357](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-cli/src/world.rs#L349-L357)
说明：CLI 的 `Feature::Bundle` 映射到 `typst::Feature::Bundle`。

CLI 侧「输出格式」枚举也有 `Bundle` 一项，由 `--format` 参数选择：

[crates/typst-cli/src/args.rs:589-604](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-cli/src/args.rs#L589-L604)
说明：`OutputFormat` 含 `Bundle` 变体，`--format bundle` 即选此项。

> 关键区分：`--format bundle` 决定「**编译成什么**」（驱动 `compile::<Bundle>`），`--features bundle` 决定「**允不允许用这个实验特性**」（决定是警告还是报错）。两者通常要**一起**给。

#### 4.3.4 代码实践

这是一个**可运行实验**，用来亲眼看到 feature 开关的区别。前置条件：你已用 `cargo build --features bundle`（或安装了带 bundle 支持的 typst）构建好 CLI。待本地验证。

1. 实践目标：观察「未开启 feature 报错」与「开启 feature 仅警告」两种行为。
2. 操作步骤：
   - 写一个最小源文件 `main.typ`（内容下一节综合实践会给，这里先用一行 `#document("a.html")[Hi]` 即可）。
   - **不**带 feature 编译：`typst compile --format bundle main.typ out/`（注意没有 `--features bundle`）。
   - 再带上 feature 编译：`typst compile --format bundle --features bundle main.typ out/`。
3. 需要观察的现象：第一次应出现致命错误，提示需要 `--features bundle`；第二次应出现一条「bundle export is experimental」的警告，但继续生成文件。
4. 预期结果：与 `warn_or_error_for_bundle` 的两个分支一一对应。
5. 待本地验证：具体报错/警告文案以你本地的 Typst 版本为准。

#### 4.3.5 小练习与答案

**练习 1**：如果用户只写了 `--features bundle` 但忘了 `--format bundle`，会发生什么？

> **答案**：不会产出 bundle。`--features bundle` 只开启了实验能力（并把致命错误降级为警告），真正决定编译成 bundle 的是 `--format bundle`。此时 Typst 仍按默认的 paged/pdf 目标编译。

**练习 2**：为什么 Typst 选择「警告」而不是默默放行实验特性？

> **答案**：实验特性行为可能随时变化，警告能提醒用户不要在生产环境依赖它，同时又允许早期试用。

---

### 4.4 crate 依赖关系

#### 4.4.1 概念说明

`typst-bundle` 在 typst 工作区里是一个**编排层（orchestration layer）**：它自己不做底层排版或具体格式编码，而是把各兄弟 crate 的能力组合起来——用 `typst-layout` 做分页排版、用 `typst-html` 做 HTML、用 `typst-pdf` / `typst-svg` / `typst-render` 做格式导出，并依赖 `typst-library` 提供的数据模型与诊断。

看懂它的 `Cargo.toml` 依赖清单，就能立刻明白它在架构里的位置。

#### 4.4.2 核心流程

```text
typst-bundle（编排）
   ├── typst-library   // 数据模型、Engine、诊断、Target/Output、document/asset 元素
   ├── typst-layout    // 分页排版（PDF/SVG/PNG 的 PagedDocument）
   ├── typst-html      // HTML 文档
   ├── typst-pdf       // PDF 导出
   ├── typst-svg       // SVG 导出
   ├── typst-render    // PNG（栅格）渲染
   ├── typst-syntax    // VirtualPath 等语法/路径类型
   ├── typst-macros    // #[time] 等过程宏
   ├── typst-timing    // 性能打点
   ├── typst-utils     // LazyHash、Protected 等工具
   └── comemo / ecow / indexmap / rayon / rustc-hash  // 记忆化、紧凑集合、并行、哈希
```

#### 4.4.3 源码精读

bundle crate 的依赖清单：

[crates/typst-bundle/Cargo.toml:15-31](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/Cargo.toml#L15-L31)
说明：bundle 依赖的兄弟 crate 与第三方库。

这些依赖在源码顶部被真正引入，能互相印证：

[crates/typst-bundle/src/lib.rs:19-35](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L19-L35)
说明：从各 crate 引入的关键类型，例如 `typst_html::HtmlDocument`、`typst_layout::PagedDocument`、`typst_library` 的 `Output/Target/TargetElem`、`comemo`、`indexmap::IndexMap`、`rayon`（间接用于并行）等。

把依赖清单和 `use` 语句对照看，就能得出每个依赖的角色：

| 依赖 | 在 bundle 里的角色 |
| --- | --- |
| `typst-library` | `Output` / `Target` / `Engine` / `World` / `AssetElem` / `DocumentElem` 等核心抽象 |
| `typst-layout` | 产出 `PagedDocument`（PDF/SVG/PNG 的分页文档） |
| `typst-html` | 产出 `HtmlDocument` |
| `typst-pdf` / `typst-svg` / `typst-render` | 把文档导出成 PDF / SVG / PNG（在 `export.rs` 中调用，后续讲义精读） |
| `comemo` | 记忆化（`bundle_impl` 等函数标了 `#[comemo::memoize]`） |
| `indexmap` | `files: IndexMap<VirtualPath, BundleFile>`，保留插入顺序 |
| `rayon` | 并行编译各文档、并行导出 |

> 小提示：`bundle_impl` 上的 `#[comemo::memoize]`（见 [lib.rs:138-150](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L138-L150)）是性能与「内省收敛」的关键，本讲先记住它的存在，原理留到第 5 单元。

#### 4.4.4 代码实践

这是一个**源码阅读型实践**。

1. 实践目标：把 `Cargo.toml` 的每个依赖对应到它的实际用途。
2. 操作步骤：
   - 打开 `Cargo.toml:15-31`。
   - 对每个 `typst-*` 依赖，在 `lib.rs` 顶部的 `use` 语句里找到它贡献的类型，填进上面那张表。
3. 需要观察的现象：几乎所有依赖都能在 `use` 语句里找到对应类型，说明 bundle 确实是「组合者」而非「底层实现者」。
4. 预期结果：完成「依赖 → 角色」对照表。
5. 待本地验证：`typst-pdf/svg/render` 的具体调用在 `export.rs`，可自行打开确认。

#### 4.4.5 小练习与答案

**练习 1**：bundle 自己实现 PDF 编码吗？

> **答案**：不实现。它依赖 `typst-pdf` 来做 PDF 编码，自己只负责编排（决定何时、用什么参数调用导出函数）。

**练习 2**：为什么 `files` 用 `IndexMap` 而不是普通 `HashMap`？

> **答案**：`IndexMap` 保留插入顺序，使 bundle 内文件有一个稳定的先后顺序，便于确定性输出与调试。

---

## 5. 综合实践

现在把本讲的知识串起来，做一个端到端的小实践：**写一个最小 bundle，编译它，观察多文件目录结构**。待本地验证。

### 5.1 实践目标

亲手体验「一次编译、多文件输出」，并用一句话总结 bundle 相对单文件 PDF/SVG 的核心区别。

### 5.2 操作步骤

1. 准备目录与源文件 `main.typ`，内容如下（**示例代码**）：

   ```typst
   // 一个 HTML 文档：扩展名 .html 会被推断为 HTML 导出
   #document("hello.html")[
     #html.elem("h1")[Hello, bundle!]
   ]

   // 一个原始 asset：原样写入字节，不参与排版
   #asset("note.txt", "This is a raw asset file.")
   ```

   > 说明：`#document(path, body)` 的第一个参数是 bundle 内的落盘路径，`body` 是文档内容；`#asset(path, data)` 把 `data` 原样写到 `path`。本例用到了 HTML，因此还需同时开启 html feature。

2. 编译（注意三个要点：`--format bundle` 选目标、`--features bundle` 开实验特性、`--features html` 开 HTML）：

   ```bash
   typst compile --format bundle --features bundle,html main.typ out/
   ```

3. 查看 `out/` 目录。

### 5.3 需要观察的现象

- `out/` 下应出现两个文件：`hello.html` 和 `note.txt`。
- `note.txt` 的内容应与你写入的字符串完全一致（原始字节直通）。
- 终端应出现一条「bundle export is experimental」的警告（对应 4.3 节）。

### 5.4 预期结果

```text
out/
├── hello.html      // 来自 #document，HTML 渲染产物
└── note.txt        // 来自 #asset，原始字节
```

### 5.5 收尾思考

用一句话总结：**bundle 与单文件 PDF/SVG 的核心区别在于——它把一次编译的产物从一个文件扩展成「多个文档 + 多个原始资产」的文件集合。** 这正好对应你在源码里看到的 `Bundle { files: IndexMap<...>, ... }`。

> 如果你本地暂时无法编译，可以退化为「源码阅读型实践」：对照 4.2 节的 `BundleFile` 定义，预测上面这份 `main.typ` 会产生哪两个 `BundleFile` 条目，并说明各自的变体（`Document(Html)` 与 `Asset(Bytes)`）。

## 6. 本讲小结

- Typst 有三种输出目标：`Paged`、`Html`、`Bundle`；`bundle` 是唯一的「多文件」目标。
- `Bundle` 是 bundle 输出下的顶层产物对象，之于 bundle 就像 `PagedDocument` 之于 pdf/png/svg。
- bundle 把文件分成 `Document`（排版/渲染产物：PDF/SVG/PNG/HTML）和 `Asset`（原始字节）两类，整体装在 `files: IndexMap<VirtualPath, BundleFile>` 里。
- bundle 是实验性特性：未开 `--features bundle` 会被 `warn_or_error_for_bundle` 直接报错，开启后降级为警告。
- CLI 侧 `--format bundle` 决定编译成 bundle，`--features bundle` 决定是否允许使用该实验特性，二者通常一起使用。
- `typst-bundle` 是一个编排层，依赖 `typst-layout` / `typst-html` / `typst-pdf` / `typst-svg` / `typst-render` / `typst-library` 等兄弟 crate，自身不实现底层排版与格式编码。

## 7. 下一步学习建议

下一讲（u1-l2《目录结构与编译入口》）会接着本讲的结尾往下走：

- 精读 `typst-bundle` 的四个源码文件（`lib.rs` / `export.rs` / `introspect.rs` / `link.rs`）各自的职责。
- 跟踪 `typst::compile::<Bundle>(world)` 是如何通过 `Output::create` 一路调用到 `bundle()` / `bundle_impl()` 的。
- 解释 `bundle_impl` 上的 `#[comemo::memoize]` 为什么是后续「多文档互相内省」的基础。

建议你在进入下一讲前，先在本讲引用的 `lib.rs` 里通读一遍 `bundle()` 和 `bundle_impl()` 两个函数的签名与整体步骤（不用看懂每行），为下一讲打好地图。
