# 特性开关与 PDF/HTML 输出模块

## 1. 本讲目标

本讲是「高级与扩展」单元的第一篇。前面十几讲我们看到的都是「默认就注册进标准库」的定义，本讲要回答一个新问题：**当某些功能还在开发中、或依赖了外部行为 crate 时，Typst 如何把它们「按需」地、可控地装配进标准库？**

学完本讲你应当能够：

- 说清 `Features` / `Feature` 这套位图开关的数据结构与装配时机。
- 解释三个具体开关（`Html` / `Bundle` / `A11yExtras`）各自「多注册了哪些定义」，并能画出对照表。
- 读懂 `pdf` 模块的用户可见元素（`AttachElem`、`ArtifactElem`）与实验性无障碍函数。
- 说清为什么 `html` 模块体不在本 crate、而要通过 `routines.html_module` 函数指针在运行期注入——也就是把 u5-l4 的「crate 分离」机制落到一个具体例子上。

## 2. 前置知识

本讲承接两篇讲义，请确认你已经掌握：

- **u1-l3 标准库的装配**：`Library` 的七字段、`LibraryBuilder::build()` 会调用总装函数 `global()`、`define_elem`/`define_func` 把定义注入 `Scope`。
- **u5-l4 Routines 与 crate 分离机制**：`Routines` 是一张「本质上的动态链接」函数指针表，行为 crate（`typst-eval`/`typst-realize`/`typst-layout`/`typst-html`）依赖本 crate 的类型，反向回调则经这张表注入，从而保持依赖单向无环。

补充几个本讲会用到的术语（前几讲已解释，这里只做一句话提示）：

- **元素（Element）**：用 `#[elem]` 宏定义、可被 `set`/`show` 规则作用的类型化节点；用户写 `#pdf.attach(...)` 就是在构造一个元素。
- **原生函数（NativeFunc）**：用 `#[func]` 宏定义的 Rust 函数，经 `define_func::<T>()` 注册为用户可调用的函数。
- **`Scope`**：名字到 `Binding` 的有序映射；`global.define("pdf", module)` 把一个子模块挂到全局作用域的 `pdf` 键下。

还有一个工程直觉先放在前面，本讲会反复印证：**本 crate 只做「类型定义 + 配置数据归一化」，真正把 PDF 写出来、把 HTML 序列化出来的算法住在行为 crate，运行期经 `Routines` 回调。** 特性开关本身只是「注册哪些定义」的总开关，它不改变这一分工。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/lib.rs` | 定义 `Features`/`Feature`、`LibraryBuilder::with_features`、`build()`，以及总装函数 `global()`（特性开关的「分发中心」）。 |
| `src/pdf/mod.rs` | `pdf` 子模块的装配函数 `module(features)`，含 `A11yExtras` 判断。 |
| `src/pdf/accessibility.rs` | `ArtifactElem`（PDF 伪 artifact）、`table_summary`/`header_cell`/`data_cell`（实验性无障碍函数）、`PdfMarkerTag`（内部用）。 |
| `src/pdf/attach.rs` | `AttachElem`（文件附件）与 `AttachedFileRelationship`。 |
| `src/routines.rs` | `routines!` 宏与 `html_module` 例程的签名声明（形状在本 crate，实现在别处）。 |
| `crates/typst/src/lib.rs`（顶层 crate，跨 crate 引用） | `static ROUTINES` 真正把 `html_module` 指向 `typst_html::module`；`warn_or_error_for_html/bundle` 在编译期二次校验开关。 |

## 4. 核心概念与源码讲解

### 4.1 Features 与 Feature：标准库的「按需注册」开关

#### 4.1.1 概念说明

Typst 的标准库里，绝大多数定义（`heading`、`list`、`rect`、`rgb`……）一装配就全局可用。但有三类定义属于「还在开发中 / 实验性 / 耦合了未稳定输出目标」的范畴，不能默认打开，否则用户文档会意外依赖不稳定 API。`Feature` 就是这些「在研特性」的枚举标签，`Features` 是「哪些特性被启用了」的集合。

它的设计哲学是：**这些 API 没有任何稳定性保证**（源码注释原话是 "No guarantees whatsover!"）。用户必须显式 opt-in（CLI 传 `--features html` 或设环境变量 `TYPST_FEATURES`），才能让对应定义出现在标准库作用域里。于是「特性开关」在装配期就起到了两道闸门的作用：

1. **决定注册哪些定义**：装配函数 `global()` 和子模块的 `define()` 在 `define_elem`/`define_func` 之前用 `features.is_enabled(...)` 判断，决定要不要把某个定义注入作用域。
2. **决定编译期能否放行**：顶层 crate 在真正执行 HTML/Bundle 导出前，会再次检查同一个 `Features`（见 4.3.3），没开关就 `bail!`。

#### 4.1.2 核心流程

从命令行到标准库定义的全链路（注意每一段落在哪个 crate）：

```text
CLI: --features html        （typst-cli）
  │  解析成 typst_cli::args::Feature 枚举（clap ValueEnum）
  ▼
From<cli Feature> for typst::Feature   （typst-cli/world.rs）
  │  cli 的 Feature 映射成 typst（即 typst_library）的 Feature
  ▼
Library::builder().with_features(features).build()   （typst-cli/world.rs）
  │  把 Features 存进 LibraryBuilder
  ▼
LibraryBuilder::build()   （本 crate lib.rs）
  │  调 global(routines, math, inputs, &self.features)
  │     └─ model::define(&mut global, features)   → 查 Bundle
  │     └─ pdf::module(features)                  → 查 A11yExtras
  │     └─ if Feature::Html { global.define("html", routines.html_module()) }
  ▼
Library.features 字段也保存下来，供编译期 warn/error 二次校验
```

数据结构层面非常轻量：

- `Feature` 是一个 `#[non_exhaustive]` 的 C 风格枚举，目前三个变体 `Html`/`Bundle`/`A11yExtras`。`#[non_exhaustive]` 意味着未来可以加新变体而不破坏下游匹配。
- `Features` 内部就是一个 [`SmallBitSet`](src/lib.rs#L240-L241)（来自 `typst-utils` 的小型位图）。`Feature as usize` 当作位下标，`is_enabled` 就是位测试。这样 `Features` 既 `Copy` 又可哈希（`#[derive(Hash)]`），可以安全地参与 `Library` 的哈希与 comemo 增量记忆化。

#### 4.1.3 源码精读

**`Features` 与 `Feature` 的定义**：[`src/lib.rs:L237-L284`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L237-L284) 给出了开关的全部数据结构。要点：

- `Features(SmallBitSet)` 是 newtype，`Default` 即「全关」（`none()` 等价于默认）。
- `all()` 经 `Feature::all().collect()` 把三个变体全置位。
- `is_enabled(feature)` 调 `self.0.contains(feature as usize)`——这里把枚举变体当位下标用。
- `FromIterator<Feature>` 把多个 `Feature` 累积进位图，这正是 `with_features` 接收的形态。

```rust
#[derive(Debug, Default, Clone, Hash)]
pub struct Features(SmallBitSet);

impl Features {
    pub fn is_enabled(&self, feature: Feature) -> bool {
        self.0.contains(feature as usize)
    }
    // ...
}

#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]
#[non_exhaustive]
pub enum Feature {
    Html,
    Bundle,
    A11yExtras,
}

impl Feature {
    pub fn all() -> impl Iterator<Item = Self> {
        [Self::Html, Self::Bundle, Self::A11yExtras].into_iter()
    }
}
```

注意 `Feature::all()` 用的是写死的数组，而不是 `Self::variants()` 之类的自省——新增变体必须同步改这里。这是一个小而真实的「手动维护注册表」的例子。

**装配入口 `LibraryBuilder`**：[`src/lib.rs:L195-L235`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L195-L235) 定义了 builder。`with_features` 只是把 `Features` 存进 builder，真正的「按开关注册」发生在 `build()` 里：

```rust
pub fn with_features(mut self, features: Features) -> Self {
    self.features = features;
    self
}

pub fn build(self) -> Library {
    let math = math::module();
    let inputs = self.inputs.unwrap_or_default();
    let global = global(self.routines, math.clone(), inputs, &self.features);
    Library {
        routines: self.routines,
        global: global.clone(),
        math,
        styles: Styles::new(),
        rules: (self.routines.rules)(),
        std: Binding::detached(global),
        features: self.features,   // 保存下来，供编译期二次校验
    }
}
```

`Library.features` 字段被特意保留（[`src/lib.rs:L181-L183`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L181-L183)），这就是 4.3.3 里顶层 crate 能拿到它的原因。

**分发中心 `global()`**：[`src/lib.rs:L329-L355`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L329-L355) 是装配期查开关的三处调用点中的两处：

```rust
fn global(routines: &Routines, math: Module, inputs: Dict, features: &Features) -> Module {
    let mut global = Scope::deduplicating();
    self::foundations::define(&mut global, inputs);
    self::model::define(&mut global, features);          // ① 传给 model，查 Bundle
    self::text::define(&mut global);
    // ...其余无 feature 判断的模块...
    global.define("math", math);
    global.define("pdf", self::pdf::module(features));   // ② 传给 pdf，查 A11yExtras
    if features.is_enabled(Feature::Html) {              // ③ 本地查 Html
        global.define("html", (routines.html_module)());
    }
    prelude(&mut global);
    Module::new("global", global)
}
```

这里有一个值得注意的细节：**同样是「按 feature 注册」，三种实现方式不同**。`Bundle` 和 `A11yExtras` 把 `features` 参数一路下传给子模块的 `define()`，由子模块自己判断；而 `Html` 的判断写在 `global()` 本地。原因在下文 4.3 讲：html 模块体来自 `routines.html_module`，它的判断必须和「调用 routine」这一步放在一起。

#### 4.1.4 代码实践

**实践目标**：把「启用某 feature 会多注册哪些定义」这件事用一张对照表固化下来，验证你对装配流程的理解。

**操作步骤**：

1. 打开 [`src/lib.rs`](src/lib.rs) 的 `global()`（L329 起）与 `build()`（L221 起），确认 `features` 是怎么从 builder 流到三个判断点的。
2. 全仓搜索 feature 判断点。本 crate 内一共三处（`model/mod.rs`、`pdf/mod.rs`、`lib.rs`），可用关键字 `is_enabled(Feature::` 定位。
3. 逐处读出「命中时多注册的定义」，填进下表。

**需要观察的现象 / 预期结果**——下表即为参考答案：

| Feature | 判断点（文件:行） | 启用后多注册的定义 | 注册方式 |
| --- | --- | --- | --- |
| `Feature::Bundle` | [`model/mod.rs:L56`](src/model/mod.rs#L56) | `AssetElem`（用户函数 `#asset(...)`，向 bundle 输出追加原始文件） | 子模块 `define()` 内 `define_elem` |
| `Feature::A11yExtras` | [`pdf/mod.rs:L18`](src/pdf/mod.rs#L18) | `table_summary`、`header_cell`、`data_cell` 三个 `pdf.*` 函数 | 子模块 `module()` 内 `define_func` |
| `Feature::Html` | [`lib.rs:L348`](src/lib.rs#L348) | 整个 `html` 子模块（由 `routines.html_module` 注入） | `global()` 内 `global.define("html", …)` |

> **注意**：`pdf` 模块里的 `AttachElem`、`ArtifactElem` **不**受任何 feature 开关保护，它们默认就注册（见 4.2）。只有三个 a11y 函数在 `A11yExtras` 之后。

**进阶观察**：对比 `Feature::all()`（[L281-L283](src/lib.rs#L281-L283)）与你刚填的表，会发现「枚举变体」与「判断点」并不是一一对应——`Html` 的判断在 `lib.rs`，另两个分别在 `model`、`pdf` 子模块。这说明开关的「消费点」由各自模块自行决定，`Feature` 枚举只是统一了「开关集合」的表示。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Features` 用 `SmallBitSet` 而不是 `HashSet<Feature>`？

> **答案**：特性数量极少（目前 3 个），位图只需几个比特，栈上 `Copy`、零分配；同时它派生 `Hash` 后能直接参与 `Library` 的哈希与 comemo 增量记忆化。`HashSet` 语义上能做到同样的事，但带来堆分配且不便 `Copy`。

**练习 2**：若将来新增一个 `Feature::Foo`，需要改哪些地方才能让它「启用时多注册某个定义」？

> **答案**：① 在 `Feature` 枚举加变体（受 `#[non_exhaustive]` 约束，不会破坏已有 match）；② 在 `Feature::all()` 的数组里加上它；③ 在某个 `define()`/`module()` 或 `global()` 里加 `if features.is_enabled(Feature::Foo) { … }` 分支；④ 如果它对应新的导出目标（如 html/bundle），还要在顶层 crate 的 `warn_or_error_for_*` 增加二次校验。

---

### 4.2 pdf 模块：AttachElem、ArtifactElem 与无障碍（a11y）函数

#### 4.2.1 概念说明

`pdf` 子模块里集中了「只在导出 PDF 时有意义、或主要服务于 PDF 无障碍（accessibility, 简称 a11y）」的定义。它是「子模块挂载式」装配的典型：`global()` 用 `global.define("pdf", self::pdf::module(features))` 把一个独立 `Module` 挂到全局 `pdf` 键下（见 [u1-l2](u1-l2-directory-structure.md) 的两种注册模式）。

模块里有两类用户可见定义：

- **默认注册的元素**：`AttachElem`（文件附件）、`ArtifactElem`（标记为 PDF artifact，即「屏幕阅读器应跳过」的内容）。它们对非 PDF 输出会被忽略，但定义本身稳定（`since = "0.14.0"`），所以默认可见。
- **实验性无障碍函数**：`table_summary`、`header_cell`、`data_cell`，专门帮助辅助技术（Assistive Technology，如屏幕阅读器）理解复杂表格。这些 API 明确标注「temporary」，故用 `A11yExtras` 开关保护。

要强调的是：这些元素/函数**几乎只做配置数据归一化**，真正把附件写进 PDF、把结构化标签（tagged PDF）序列化出去的逻辑住在行为 crate（`typst-pdf`），本 crate 不实现。

#### 4.2.2 核心流程

`pdf` 模块的装配 [`src/pdf/mod.rs:L13-L24`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/pdf/mod.rs#L13-L24)：

```rust
pub fn module(features: &Features) -> Module {
    let mut pdf = Scope::deduplicating();
    pdf.start_category(crate::Category::Pdf);
    pdf.define_elem::<AttachElem>();          // 默认注册
    pdf.define_elem::<ArtifactElem>();        // 默认注册
    if features.is_enabled(Feature::A11yExtras) {
        pdf.define_func::<table_summary>();   // 实验性，开关保护
        pdf.define_func::<header_cell>();
        pdf.define_func::<data_cell>();
    }
    Module::new("pdf", pdf)
}
```

两个元素、三个函数的行为概览：

- `AttachElem`：携带一个文件（路径或裸字节）与若干 PDF 附件元数据（relationship/mime-type/description）。导出 PDF 时由 `typst-pdf` 读取它的 `data` 字段写入附件树；导出其它格式时忽略。
- `ArtifactElem`：把 body 标记为某种 `ArtifactKind`（Header/Footer/Watermark/PageNumber/Layout/Background/Other…），告诉 PDF 阅读器「这是装饰性内容，不要读屏」。Typst 还会**自动**把页眉页脚、背景、行号等标记为 artifact。
- `table_summary`/`header_cell`/`data_cell`：分别给 `TableElem` 注入「表格摘要」「把单元格标为表头/数据单元格」的信息。它们本质上是 `TableElem` 上方法的薄包装（见 4.2.3）。

#### 4.2.3 源码精读

**ArtifactElem——一个「带标记能力」的纯标记元素**：[`src/pdf/accessibility.rs:L36-L53`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/pdf/accessibility.rs#L36-L53)

```rust
#[elem(since = "0.14.0", Tagged)]
pub struct ArtifactElem {
    #[default(ArtifactKind::Other)]
    pub kind: ArtifactKind,
    #[required]
    pub body: Content,
}
```

两个关键点：

1. `#[elem(... Tagged)]` 赋予它 `Tagged` 能力（u3-l2 讲过的内省能力字段），使得 `typst-pdf` 在写 tagged PDF 时能把这对标签盖章到结构树上。
2. 字段只有 `kind`（默认 `Other`）与 `body`，`ArtifactKind` 是个 13 变体的枚举（[`accessibility.rs:L56-L93`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/pdf/accessibility.rs#L56-L93)），分类越精确，PDF 阅读器在重排（reflow）/复制时处理得越好。注意源码注释明确说「标记成 artifact 后，其内部内容不能再变回可访问」——这是一个不可逆的语义门。

**AttachElem——典型的「配置载体 + `#[parse]` 归一化」元素**：[`src/pdf/attach.rs:L31-L73`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/pdf/attach.rs#L31-L73)。它有两个 `#[required]` 字段都用 `#[parse]` 自定义解析，回顾 u3-l3 的字段标注：

```rust
#[elem(since = "0.14.0", keywords = ["embed"], Locatable)]
pub struct AttachElem {
    #[required]
    #[parse(
        let Spanned { v: path, span } = args.expect::<Spanned<PathOrStr>>("path")?;
        let resolved = path.resolve_if_some(span.id()).at(span)?;
        let derived = resolved.vpath().get_without_slash().into();
        Derived::new(path, derived)
    )]
    pub path: Derived<PathOrStr, EcoString>,

    #[positional]
    #[required]
    #[parse(
        match args.eat::<Bytes>()? {
            Some(data) => data,
            None => engine.world.file(resolved.intern()).at(span)?,
        }
    )]
    pub data: Bytes,
    // relationship / mime_type / description 三个普通 settable 字段
}
```

`data` 字段的 `#[parse]` 体现了 u5-l1 的 `World` 接口：用户若没直接给裸字节，就用 `engine.world.file(...)` 经 `World` 回调读取文件。注意**这里就是本 crate 真正去「碰」文件 IO 的少数地方之一**——但 IO 本身仍委托给 `World` 实现，本 crate 不碰磁盘。

`Derived<PathOrStr, EcoString>` 是 u2-l3 出现过的「输入值 + 派生值」模式：用户给的是 `PathOrStr`，派生出虚拟根相对路径字符串供 PDF 写入用（[attach.rs:L38-L46](src/pdf/attach.rs#L38-L46)）。

**a11y 函数——`TableElem` 方法的薄包装**：以 [`table_summary`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/pdf/accessibility.rs#L137-L144) 为例（[`header_cell`/`data_cell`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/pdf/accessibility.rs#L203-L272) 同构）：

```rust
#[func(since = "0.14.0")]
pub fn table_summary(
    #[named] summary: Option<EcoString>,
    table: TableElem,
) -> Content {
    table.with_summary(summary).pack()
}
```

它的全部工作就是**把信息塞回 `TableElem` 然后 `.pack()` 成 `Content`**。`header_cell` 调 `cell.with_kind(Smart::Custom(TableCellKind::Header(...)))`、`data_cell` 调 `with_kind(Smart::Custom(TableCellKind::Data))`。也就是说，本 crate 只负责把「这是个表头单元格 / 这段是摘要」这些元数据**归一化进表格/单元格元素**，真正消费这些元数据去写 PDF 结构标签的是行为 crate。这再次印证了「定义 + 归一化在本 crate、行为在别处」的主线。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码与文档注释，理解 `ArtifactElem` 与 a11y 函数各自「标记了什么、被谁消费」。

**操作步骤**（源码阅读型实践）：

1. 读 [`ArtifactElem` 的文档注释](src/pdf/accessibility.rs#L13-L35)，列出 Typst 会**自动**标记为 artifact 的内容有哪些（页眉/页脚/背景/前景、形状路径、行号、表头表尾重复……）。
2. 读 [`AttachedFileRelationship`](src/pdf/attach.rs#L76-L86) 的四个变体注释，说明哪个常用于「发票类机器可读数据」附件（提示：ZUGFeRD/Factur-X，对应 Supplement 或 Alternative，结合 attach.rs 顶部文档）。
3. 追踪 `table_summary` 的返回值 `table.with_summary(summary).pack()`：它没有自己产 PDF，只是改了 `TableElem` 的一个内部字段。在 `src/model/table.rs` 里搜索 `summary`（用 `Grep`），确认该字段最终被谁读取。

**预期结果**：你会确认 a11y 函数是「无副作用」的纯配置函数——它们改的是元素字段，真正的 PDF 结构树写入在 `typst-pdf`。这一点在本 crate 里**无法直接验证输出**，因为本 crate 不含 PDF writer；若要观察最终效果，需在本地用 `typst compile --features a11y-extras` 编译含 `pdf.table-summary(...)` 的文档并用支持 tagged PDF 的阅读器检查（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AttachElem`/`ArtifactElem` 默认注册，而 a11y 函数要藏在 `A11yExtras` 后面？

> **答案**：前两者的文档明确标注 API 稳定（`since = "0.14.0"`，无「temporary」字样），是用户可依赖的 PDF 特性；而 a11y 三个函数的注释反复写「The API of this feature is temporary」「may move out of the `pdf` module」，属于尚未稳定的实验 API，必须用 feature 开关隔离，避免用户过早依赖。

**练习 2**：`AttachElem` 的 `#[parse]` 里 `engine.world.file(...)` 失败时返回什么类型的错误？为什么用 `.at(span)`？

> **答案**：`world.file` 返回 `FileResult<Bytes>`（即 `Result<Bytes, FileError>`）。`.at(span)`（u5-l3 的 `At` trait）把这个**无位置**的文件错误升级为带调用处 span 的 `SourceResult`，使诊断能精确定位到 `#pdf.attach(...)` 这一行。

---

### 4.3 html_module routine：HTML 输出为何按需经 routine 装配

#### 4.3.1 概念说明

三个特性开关里，`Html` 是最特殊的一个：它的「多注册定义」不是一个本 crate 写的元素或函数，而是**整个 `html` 子模块**。而这个模块体根本不在 `typst-library` 里——它在 `typst-html` 这个行为 crate 里。

这就直接命中了 u5-l4 讲过的核心矛盾：行为 crate（`typst-html`）依赖 `typst-library` 的类型，反过来 `typst-library` 又需要在装配标准库时把 `typst-html` 提供的 `html` 模块挂进全局作用域。**直接 `use typst_html` 会让 `typst-library` 反向依赖 `typst-html`，形成循环。** 解法就是 `Routines`：把「构造 html 模块」这件事声明成一个函数指针 `html_module`，形状（签名）留在本 crate，真正的实现（`typst_html::module`）在顶层 crate 装配 `ROUTINES` 时注入。

所以「`html` 是否注册」与「`html_module` 是否经 routine 调用」是同一步——这解释了 4.1.3 末尾的细节：只有 `Html` 的判断写在 `global()` 本地，因为它必须和 `(routines.html_module)()` 这行调用绑定。

#### 4.3.2 核心流程

```text
本 crate (typst-library)                         顶层 crate (typst)
─────────────────────────                        ────────────────────
routines! 宏声明 html_module 签名                static ROUTINES = Routines {
  fn html_module() -> Module            ◀────────  html_module: typst_html::module,
        │                                              …
        │  (仅声明形状；实现在别处)                    │  (在此处把指针指向真实实现)
        ▼                                              ▼
global():                                          Library::builder()
  if features.is_enabled(Feature::Html) {            .from_routines(&ROUTINES)
      global.define("html",                          .with_features(features)
          (routines.html_module)()   ──── 调用 ────▶     .build()
      );                                           // build() 内部 global() 才真正
  }                                                // 经指针回调 typst_html::module
```

调用形式 `(engine.library.routines.html_module)()` 是 u5-l4 的统一写法：通过函数指针「间接调用」，编译期 `typst-library` 不认识 `typst_html`，运行期才动态绑定。源码注释称之为「essentially dynamic linking」。

#### 4.3.3 源码精读

**routine 的签名声明**：[`src/routines.rs:L100-L101`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L100-L101)

```rust
/// Constructs the `html` module.
fn html_module() -> Module
```

注意它没有 `world`/`engine` 等参数——构造模块是纯装配行为，不需要编译上下文。这条声明由 [`routines!` 宏](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L21-L48) 展开成 `Routines` 结构体的一个字段（同时生成空的 `Hash` impl，保证对 comemo 透明）。

**调用点（本 crate 内）**：[`src/lib.rs:L348-L350`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L348-L350)

```rust
if features.is_enabled(Feature::Html) {
    global.define("html", (routines.html_module)());
}
```

只有 `Html` 开启时，才会**调用函数指针**拿到 `Module` 并挂到全局 `html` 键。若开关关闭，`html` 键根本不存在于作用域，用户写 `#html.div(...)` 会在求值期得到「unknown variable」错误。

**真实实现的注入（跨 crate，顶层 typst crate）**：[`crates/typst/src/lib.rs:L311-L325`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L311-L325)

```rust
static ROUTINES: LazyLock<Routines> = LazyLock::new(|| Routines {
    rules: || { /* 注册 layout/html 的 show 规则 */ },
    eval_string: typst_eval::eval_string,
    eval_closure: typst_eval::eval_closure,
    realize: typst_realize::realize,
    layout_frame: typst_layout::layout_frame,
    html_module: typst_html::module,          // ← html_module 的真实实现
    html_mathml_body: typst_html::html_mathml_body,
    html_span_filled: typst_html::html_span_filled,
});
```

这就是「形状在本 crate、实现在顶层 crate」的全貌：顶层 crate 同时依赖 `typst-library`（拿类型/签名）和各行为 crate（拿实现），它充当「胶水层」把两者拼成一张完整的 `Routines`，再以 `&'static Routines` 挂到 `Library` 上。

**编译期的二次校验**：顶层 crate 不仅用 routine 注入实现，还用 `Library.features` 在导出前再查一次。[`crates/typst/src/lib.rs:L246-L266`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L246-L266) 的 `warn_or_error_for_html`：

```rust
fn warn_or_error_for_html(features: &Features, sink: &mut Sink) -> SourceResult<()> {
    if features.is_enabled(Feature::Html) {
        sink.warn(warning!(Span::detached(),
            "html export is under active development and incomplete"; /* hints */));
    } else {
        bail!(Span::detached(),
            "html export is only available when `--features html` is passed"; /* hints */);
    }
    Ok(())
}
```

也就是说，即便用户绕过作用域直接触发 HTML 导出，`Html` 关闭时会 `bail!`；开启时则发一条警告，说明 HTML 导出仍在开发中。`Bundle` 有完全同构的 [`warn_or_error_for_bundle`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L270-L286)（[`crates/typst/src/lib.rs:L270-L286`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L270-L286)）。这道闸门与 4.1 的装配期闸门用的是**同一个 `Features` 对象**（`Library.features`），二者一致。

#### 4.3.4 代码实践

**实践目标**：亲手把「CLI 开关 → builder → routine 注入」这条链走一遍，验证 `html` 模块确实来自 `typst_html::module` 而非本 crate。

**操作步骤**（源码阅读型实践，跟踪调用链）：

1. 在 `typst-cli` 里定位 CLI 解析：[`crates/typst-cli/src/args.rs:L646-L652`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/args.rs#L646-L652) 的 `typst_cli::args::Feature` 枚举（clap `ValueEnum`，变体与 `--features html` 对应，还支持 `TYPST_FEATURES` 环境变量、逗号分隔）。
2. 读 [`crates/typst-cli/src/world.rs:L349-L357`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/world.rs#L349-L357) 的 `From<cli Feature> for typst::Feature`，确认 CLI 的 `Feature` 与本 crate `Feature` 的一一映射。
3. 读 [`crates/typst-cli/src/world.rs:L67`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/world.rs#L67)：`Library::builder().with_inputs(inputs).with_features(features).build()`——这一行把 CLI 开关送进本 crate 的 builder。
4. 回到本 crate [`build()`](src/lib.rs#L221-L234) → [`global()`](src/lib.rs#L329-L355) → `(routines.html_module)()`，再到顶层 [`ROUTINES.html_module = typst_html::module`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L322)。

**需要观察的现象**：你会看到「开关」以 `Feature` 枚举值的形式跨越了**三个 crate**（`typst-cli` 的 CLI 枚举 → 本 crate 的核心枚举 → 顶层 crate 的二次校验），而「`html` 模块体」以**函数指针**的形式跨越了**两个 crate**（本 crate 声明 → 顶层 crate 注入 → 行为 crate 实现）。这是 crate 拆分在数据（开关）与行为（模块体）两条线上的对称设计。

**预期结果**：你能在一张图里画出「同一个 `Feature::Html` 同时驱动了两件事：①装配期注册 `html` 模块；②导出期放行 HTML 输出」，而这两件事的实现分别落在不同 crate。

> 本实践只跟踪源码，不运行编译。若要在本地观察效果：准备一个含 `#html.div[hi]` 的 `.typ` 文件，分别用 `typst compile` 与 `typst compile --features html --format html` 编译，对比前者报「unknown variable: html」、后者产出 HTML（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：假如 `typst-library` 直接在 `global()` 里写 `global.define("html", typst_html::module())`，会出现什么问题？

> **答案**：会让 `typst-library` 反向依赖 `typst-html`，而 `typst-html` 本身又依赖 `typst-library` 的类型，形成循环依赖，Cargo 无法编译。这正是引入 `Routines` 函数指针表的根本动机（u5-l4）。

**练习 2**：为什么 `pdf` 模块不需要走 routine，而 `html` 模块必须走 routine？

> **答案**：因为 `pdf` 模块里的 `AttachElem`/`ArtifactElem`/a11y 函数**都定义在 `typst-library` 内部**，本 crate 自己就能 `define_elem`/`define_func`，无需回调行为 crate。而 `html` 模块的**模块体**（一组 html 元素与函数）定义在 `typst-html`，本 crate 拿不到，只能通过 `html_module` routine 在运行期注入。判据是：定义是否在本 crate 内。

**练习 3**：`html_module` routine 没有 `engine`/`world` 参数，而 `eval_string`/`realize` 等 routine 有大量参数。为什么？

> **答案**：构造 `html` 模块是**纯装配**（把已知的 html 元素/函数注册进一个 `Scope`），不依赖任何编译上下文与外部资源；而 `eval_string`/`realize`/`layout_frame` 是真正的编译行为，需要 `World`、`Engine`、`Locator` 等运行期状态。参数列表反映的是该 routine 是否需要「活」的编译环境。

---

## 5. 综合实践

**任务**：作为一名想给 Typst 加新导出目标的扩展者，请设计一个假想的 `Feature::Epub`（仅纸面设计，不改源码），并回答下列问题，把本讲三块知识串起来：

1. **数据结构**：在 [`Feature`](src/lib.rs#L271-L284) 与 [`Feature::all()`](src/lib.rs#L281-L283) 各加什么？
2. **装配策略选择**：若 `epub` 模块体在 `typst-library` 内部，应仿照 `pdf::module` 还是仿照 `html_module`？若模块体在一个新的行为 crate `typst-epub` 里呢？
3. **二次校验**：参照 [`warn_or_error_for_html`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L246-L266)，在顶层 crate 加一个 `warn_or_error_for_epub`，说明它何时 `bail!`、何时 `sink.warn`。
4. **CLI 链路**：[`typst_cli::args::Feature`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/args.rs#L646-L652) 与 [`From` impl](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/world.rs#L349-L357) 要各加什么分支？

**参考思路**（请先自己作答再对照）：

1. `Feature` 加 `Epub` 变体；`all()` 数组追加 `Self::Epub`。
2. 模块体在本 crate：仿 `pdf::module`——写 `pub fn module(features: &Features) -> Module`，在 `global()` 里 `global.define("epub", self::epub::module(features))`，不走 routine。模块体在行为 crate：仿 `html_module`——在 `routines!` 加 `fn epub_module() -> Module`，在顶层 `ROUTINES` 注入 `epub_module: typst_epub::module`，并在 `global()` 用 `if features.is_enabled(Feature::Epub) { global.define("epub", (routines.epub_module)()) }`。
3. `Epub` 关闭时 `bail!("epub export is only available when --features epub is passed")`；开启时 `sink.warn` 提示「experimental」。
4. CLI 枚举加 `Epub`；`From` impl 加 `Feature::Epub => typst::Feature::Epub`。

完成本任务后，你就把「开关数据结构 → 装配期注册 → 导出期校验 → CLI 入口」四段打通了，并理解了「模块体在本 crate vs 在行为 crate」这两种装配策略的取舍。

## 6. 本讲小结

- `Features` 是基于 `SmallBitSet` 的 `Copy` 位图，`Feature` 是 `#[non_exhaustive]` 的在研特性枚举（`Html`/`Bundle`/`A11yExtras`），二者不带稳定性保证，用户须显式 opt-in。
- 装配期共有三处 feature 判断：`Feature::Bundle`→`AssetElem`（model）、`Feature::A11yExtras`→三个 a11y 函数（pdf）、`Feature::Html`→整个 `html` 子模块（global 本地）。
- `pdf` 模块用「子模块挂载式」装配；`AttachElem`（文件附件，经 `World::file` 取字节）与 `ArtifactElem`（PDF artifact 标记）默认注册且 API 稳定，a11y 函数则是 `TableElem`/cell 的方法薄包装、藏在 `A11yExtras` 后。
- `html` 模块体不在本 crate，必须经 `routines.html_module` 函数指针在运行期注入（形状在本 crate、实现在顶层 crate 拼成 `ROUTINES`、指向 `typst_html::module`），目的是避免 `typst-library ↔ typst-html` 循环依赖。
- 顶层 crate 还用同一个 `Library.features` 在导出前二次校验（`warn_or_error_for_html/bundle`），形成「装配期注册」与「导出期放行」双重闸门。
- 判别「某定义是否需要走 routine」的准则：**定义是否在本 crate 内**——在内（如 pdf 元素）直接 `define_elem`，在外（如 html 模块）经 routine 注入。

## 7. 下一步学习建议

- **下一篇 u12-l2 性能与并发**：本讲的 `Features` 派生 `Hash` 是为了参与 comemo 增量记忆化，`Routines` 故意写空 `Hash` impl 也是同理——下一篇会系统讲 comemo `tracked`、`LazyHash`、`singleton!` 等贯穿全 crate 的性能手段。
- **u12-l3 扩展 typst-library**：如果你在综合实践里对「新增元素/函数」感兴趣，下一篇会演示用 `#[elem]`/`#[func]` 宏与 `define_elem`/`define_func` 把自定义定义（包括加 feature 开关）真正写出来。
- **继续阅读源码**：想深入了解 PDF 结构树与附件写入，可跟踪 `AttachElem.data`、`TableCellKind` 等字段在 `typst-pdf`（行为 crate，不在本仓库本目录）里是如何被消费的；想了解 HTML 装配细节，可读 `typst-html` 的 `module()` 实现。
