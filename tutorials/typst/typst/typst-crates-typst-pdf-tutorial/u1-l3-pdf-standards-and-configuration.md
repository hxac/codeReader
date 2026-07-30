# PDF 标准与校验配置：PdfStandards 与 PdfStandard

## 1. 本讲目标

学完本讲，你应当能够：

- 区分三类不同的「PDF 目标」：**PDF 版本**（1.4–2.0）、**PDF/A 归档标准**（a-1b ~ a-4e）、**PDF/UA 无障碍标准**（ua-1），并理解它们各自约束什么。
- 读懂 `PdfStandards::new()` 如何把一串 `PdfStandard` 组装成 krilla 的 `Configuration`，以及它如何用三道「闭包门」拦截互相冲突的标准。
- 说出 `ConfigurationError` 的两种形态（版本区间不重叠、显式版本与校验器不匹配），以及 `version_hint()` 如何生成给用户的修正建议。
- 理解 `PdfOptions::accessibility_validator()` 的作用，以及「tagged PDF」与「PDF/UA 校验」之间的关键区别。
- 说清默认（`Default`）配置导出的 PDF 是什么版本、带不带校验器。

本讲承接上一讲的 `PdfOptions`，专门拆解其中最「重」的一个字段：`standards`。

## 2. 前置知识

在进入源码之前，先用大白话建立三个背景概念。

### 2.1 PDF 不只是一个版本号

PDF 是一份持续演进的标准。从 1993 年的 PDF 1.0 到今天的 PDF 2.0，每个版本都会引入新特性（如加密、透明度、可选内容、更现代的对象模型）。一份 PDF 文件必须在头部声明自己遵守的版本号，阅读器据此判断能否打开、以及哪些功能可用。typst-pdf 通过 `PdfStandard::V_1_4` 等 5 个变体让你显式指定版本。

### 2.2 PDF/A：用于「长期归档」的子集

PDF/A 不是新版本，而是建立在某个 PDF 版本之上的**归档规范（ISO 19005）**。它的核心思想是「**自包含、可长期还原**」：禁止依赖外部字体、禁止加密、禁止音频视频、要求嵌入色彩配置文件等。PDF/A 分多个级别：

- **b（basic）**：基本视觉保真。
- **u（unicode）**：基本保真 + 所有文字必须是可映射的 Unicode。
- **a（accessible）**：最高级，在 b/u 基础上还要求**结构化标签**（tagged PDF）、语言标注等无障碍特性。

每个 PDF/A 级别本身又绑定一个底层 PDF 版本：A-1 基于 PDF 1.4，A-2/A-3 基于 PDF 1.7，A-4 基于 PDF 2.0。

### 2.3 PDF/UA：用于「无障碍」的规范

PDF/UA（ISO 14289，UA = Universal Accessibility）专门约束 PDF 的**逻辑结构**：要求文档必须是 tagged PDF、所有图片要有替代文本、标题层级要连续、表格要有正确的表头结构等。它的目标是让屏幕阅读器、放大软件等辅助技术能正确解读文档。PDF/UA-1 基于 PDF 1.7。

### 2.4 一个关键直觉：版本号是「区间」，校验器是「检查员」

把这两类东西放在一起理解：

- **版本号**：一个确定的点，如「PDF 1.7」。
- **校验器（validator）**：PDF/A 或 PDF/UA 这类标准。每个校验器不仅规定「要检查什么」，还规定「**我能接受哪些 PDF 版本**」，也就是一个版本区间 \([min, max]\)。

当 typst-pdf 把「版本号 + 一堆校验器」交给 krilla 时，krilla 要做的关键一件事就是：**这些版本要求互相兼容吗？** 这正是本讲后半段 `ConfigurationError` 处理的核心。

> 名词速查：本讲反复出现的 `krilla::configure` 模块来自底层 PDF 库 [krilla](https://github.com/typst/krilla)。`Configuration`、`ConfigurationBuilder`、`PdfVersion`、`Archival`、`Accessibility`、`Validator`、`ConfigurationError` 都是 krilla 提供的类型，typst-pdf 只是把它们「翻译」给 Typst 用户。

## 3. 本讲源码地图

本讲几乎全部集中在 `src/lib.rs` 这一个文件里：

| 位置 | 作用 |
|---|---|
| `PdfStandard` 枚举（`src/lib.rs`） | 对外暴露的「标准」清单，用户用 `V_1_7`、`A_1a`、`Ua_1` 等选择目标。 |
| `PdfStandards` 结构体（`src/lib.rs`） | 把多个标准封装成一个 `krilla::configure::Configuration`。 |
| `PdfStandards::new()`（`src/lib.rs`） | 装配核心：闭包防冲突 + 组装 builder + 错误映射。 |
| `version_hint()`（`src/lib.rs`） | 把校验器的版本区间格式化成给用户的修正提示。 |
| `PdfStandards::default()` / `Hash`（`src/lib.rs`） | 默认配置（PDF 1.7、无校验器）与缓存用哈希。 |
| `PdfOptions::accessibility_validator()`（`src/lib.rs`） | 让下游模块（tags、link 等）查询「是否启用了 PDF/UA 校验」。 |
| `ValidatorsExt`（`src/util.rs`） | 把校验器列表格式化成逗号/「and」列表的显示工具。 |
| `SerializeSettings`（`src/convert.rs`） | 装配好的 `config` 最终被塞进这里，驱动整个导出。 |

## 4. 核心概念与源码讲解

### 4.1 PdfStandard 枚举：三类 PDF 目标

#### 4.1.1 概念说明

`PdfStandard` 是 typst-pdf 暴露给用户的「菜单」：用户在 `PdfOptions` 里通过 `standards` 字段告诉 typst-pdf「我希望这份 PDF 遵守哪些标准」。这个枚举把三类完全不同的东西统一到一个类型里：

1. **版本变体**（`V_1_4` / `V_1_5` / `V_1_6` / `V_1_7` / `V_2_0`）：只设定 PDF 版本号，不加任何校验。
2. **归档变体**（`A_1b` ~ `A_4e`）：设定一个 PDF/A 归档校验器，同时隐含约束了底层版本。
3. **无障碍变体**（`Ua_1`）：设定一个 PDF/UA 无障碍校验器。

注意它带有 `#[non_exhaustive]`，意味着未来可能继续增加新标准（比如注释里提到的 PDF/UA-2）。

#### 4.1.2 核心流程

用户视角的流程非常简单：

```text
用户传入 &PdfOptions { standards: PdfStandards, .. }
        │
        ▼
standards 内部其实是一个已经装配好的 krilla Configuration
        │
        ▼
PdfStandard 枚举只是「原料」，真正装配发生在 PdfStandards::new()
```

也就是说，`PdfStandard` 是**原料**，`PdfStandards` 是**成品**。本模块只看原料清单。

#### 4.1.3 源码精读

枚举定义在此，注意 `#[serde(rename = "...")]` 决定了 Typst 脚本里用户写法的字符串形式（如 `"1.7"`、`"a-1a"`、`"ua-1"`）：

[PdfStandard 枚举](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L273-L331)：定义了全部 17 个变体，分三组——5 个版本、11 个 PDF/A、1 个 PDF/UA。`#[non_exhaustive]` 表明后续会扩展。

注意 PDF/A 的命名规律：`A_<版本><级别>`，例如 `A_2a` = PDF/A-2 的 accessible 级别、`A_4f` = PDF/A-4 的「embedded files」变体、`A_4e` = PDF/A-4 的 engineering 变体。

#### 4.1.4 代码实践

**实践目标**：理解 `serde` 重命名如何决定用户输入字符串。

**操作步骤**：

1. 打开 [src/lib.rs 的 PdfStandard 定义](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L276-L331)。
2. 逐个变体读它的 `#[serde(rename = "...")]`，把「Rust 变体名 ↔ 用户字符串」列成一张表。

**需要观察的现象**：Rust 里是 `V_1_7`（大写带下划线），但序列化字符串是 `"1.7"`（带小数点）；`A_1a` 对应 `"a-1a"`（全小写带连字符）。

**预期结果**：你会得到类似这样的映射表：

| Rust 变体 | serde 字符串 |
|---|---|
| `V_1_7` | `"1.7"` |
| `A_2a` | `"a-2a"` |
| `Ua_1` | `"ua-1"` |

这正是 Typst 用户在写 `pdf(form: ..., standard: "a-2a")` 这类表达式时背后对应的字符串。

#### 4.1.5 小练习与答案

**练习 1**：为什么枚举要加 `#[non_exhaustive]`？如果去掉它，未来新增一个 `PdfStandard::Ua_2` 会带来什么问题？

> **参考答案**：`#[non_exhaustive]` 表示「这个枚举未来还会扩展」。在外部 crate 对 `PdfStandard` 做 `match` 时，编译器会强制要求写一个 `_ =>` 兜底分支；这样 typst-pdf 后续新增变体（如 PDF/UA-2）时，不会破坏依赖它的代码的编译。去掉它后，新增变体会让所有写满 `match` 的外部代码编译失败。

**练习 2**：`A_1a` 和 `A_2b` 这两个名字里的 `a`/`b` 分别代表 PDF/A 的什么概念？

> **参考答案**：后缀字母代表 PDF/A 的合规级别。`b` = basic（基本视觉保真），`a` = accessible（在 b 基础上额外要求 tagged PDF、语言标注等无障碍特性）。前缀数字 `1`/`2` 是 PDF/A 的「代」，对应不同的底层 PDF 版本。

---

### 4.2 PdfStandards::new()：闭包装配与防冲突

#### 4.2.1 概念说明

`PdfStandards` 是「成品」：它内部只装了一个字段——krilla 的 `Configuration`。

[PdfStandards 结构体](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L113-L117)：`config` 字段是 `krilla::configure::Configuration`，`pub(crate)` 意味着外部无法直接读取，只能通过 typst-pdf 自己的方法使用。

`PdfStandards::new()` 的工作是：接收一串 `PdfStandard` 原料 → 校验它们彼此兼容 → 装配成一个 `Configuration`。它要回答两个层面的问题：

1. **typst-pdf 自己的规则**：版本号只能有一个、PDF/A 校验器最多一个、PDF/UA 校验器最多一个。
2. **krilla 的规则**：这些版本与校验器的「版本区间」是否真的能共存。

#### 4.2.2 核心流程

`PdfStandards::new()` 的执行可以分成四步：

```text
① 准备三道「闭包门」
   ├─ set_version:             只允许写入一次版本号，第二次就 bail
   ├─ set_archival_validator:  只允许写入一个 PDF/A，第二个就 bail
   └─ set_accessibility_validator: 只允许写入一个 PDF/UA，第二个就 bail

② 遍历用户传入的 list，把每个 PdfStandard 路由到对应闭包
   ├─ V_1_x / V_2_0   → set_version
   ├─ A_xxx           → set_archival_validator
   └─ Ua_1            → set_accessibility_validator

③ 用 krilla 的 ConfigurationBuilder 把三件东西装进 builder
   builder.with_version(..).with_archival_validator(..).with_accessibility_validator(..)

④ builder.finish() 让 krilla 做版本区间兼容性检查
   ├─ 成功 → Ok(Self { config })
   └─ 失败(ConfigurationError) → 翻译成带 hint 的 Typst 诊断
```

关键设计：**第一层冲突由 typst-pdf 自己的闭包拦截（快速失败、信息直白），第二层冲突（版本区间）才交给 krilla 判断**。这样能把最常见的错误（比如「同时选了两个 PDF/A」）用最清晰的人话报出来。

#### 4.2.3 源码精读

先看三道闭包门。`set_version` 用一个 `Option<PdfVersion>` 当「是否已设过」的哨兵：

[set_version 闭包](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L129-L140)：一旦 `version` 已经是 `Some(prev)`，再调用就 `bail!("PDF cannot conform to {} and {} at the same time", ...)`，把两个互斥的版本号都打印出来。

[set_archival_validator 闭包](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L142-L149)：第二次写入 PDF/A 时 `bail!("choose at most one PDF/A standard")`。

[set_accessibility_validator 闭包](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L151-L158)：第二次写入 PDF/UA 时 `bail!("choose at most one PDF/UA standard")`。

接着是把每个 `PdfStandard` 路由到对应闭包的大 `match`：

[路由 match](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L160-L180)：注意它只是「翻译」——把 typst 的 `PdfStandard::A_2a` 翻译成 krilla 的 `Archival::A2_A`，等等。命名规律是对应的，只是下划线/大小写风格不同。

路由完成后，把三件东西装进 builder：

[builder 装配](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L182-L194)：三件东西都可能为 `None`（用户没选），所以分别用 `if let Some(..)` 条件性地调用 `with_version` / `with_archival_validator` / `with_accessibility_validator`。

最后一步 `builder.finish()` 才是真正交给 krilla 做版本区间检查的地方，下一节细讲它的错误映射。

**默认配置在哪里？** 注意 `PdfStandards::new()` **不是**默认实现。默认值由 `Default` 直接构造：

[Default for PdfStandards](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L250-L260)：直接 `ConfigurationBuilder::new().with_version(PdfVersion::Pdf17).finish().unwrap()`——**PDF 1.7，不带任何校验器**。这里用 `.unwrap()` 是安全的，因为「只设版本、不设校验器」永远不会触发 krilla 的区间冲突。

再往上，`PdfOptions::default()` 用的是 `standards: PdfStandards::default()`：

[PdfOptions::default](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L99-L111)：默认 `tagged: true`，但 `standards` 是 PDF 1.7 无校验器。**这是一个本讲最重要的区分点：默认会写 tagged PDF（结构树），但并不挂载 PDF/UA 校验器。** 详见 4.4。

#### 4.2.4 代码实践

**实践目标**：亲手追踪「同时指定 PDF/A-1a 和 PDF/A-2b」的报错路径，并确认默认配置。

**操作步骤**：

1. 假想调用 `PdfStandards::new(&[PdfStandard::A_1a, PdfStandard::A_2b])`。
2. 在 [路由 match](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L160-L180) 里跟踪：
   - 第一次循环 `A_1a` → `set_archival_validator(Archival::A1_A)` → `archival_validator` 从 `None` 变成 `Some(A1_A)`，返回 `Ok(())`。
   - 第二次循环 `A_2b` → `set_archival_validator(Archival::A2_B)` → 此时 `archival_validator.is_some()` 为真 → `bail!("choose at most one PDF/A standard")`。
3. 因此函数立即返回 `Err`，**根本走不到** `builder.finish()` 那一步。

**需要观察的现象**：报错信息是字面量 `"choose at most one PDF/A standard"`，而不是 krilla 的版本区间错误。这说明 typst-pdf 用自己的闭包提前拦下了「两个 PDF/A」这种最常见冲突。

**预期结果**：

- **为什么会报错？** 因为 `set_archival_validator` 闭包设计上只允许存在一个 PDF/A 校验器。PDF/A-1a 和 PDF/A-2b 虽然级别和底层版本都不同，但它们都属于「归档校验器」这一类，typst-pdf 不允许同时挂两个归档校验器（同理也不允许同时挂两个 PDF/UA）。用户应当只选其中一个最符合需求的级别。
- **默认配置导出什么版本？** 由 [Default for PdfStandards](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L250-L260) 决定：**PDF 1.7**。
- **默认带校验器吗？** **不带**。默认 `Configuration` 只 `.with_version(PdfVersion::Pdf17)`，既没有 `archival_validator` 也没有 `accessibility_validator`。也就是说默认导出的 PDF 不是 PDF/A，也不是 PDF/UA。

> 待本地验证：如果你在 typst workspace 里写一个最小测试调用 `PdfStandards::new(&[A_1a, A_2b])`，预期得到一条包含 `"choose at most one PDF/A standard"` 的 `Err`。

#### 4.2.5 小练习与答案

**练习 1**：用户传入 `&[V_1_4, V_1_7]` 会得到什么错误？为什么？

> **参考答案**：会得到 `"PDF cannot conform to 1.4 and 1.7 at the same time"`。因为两次都走 `set_version` 闭包，第一次把 `version` 设为 `Pdf14`，第二次发现 `version` 已是 `Some`，于是 `bail!`。一份 PDF 只能声明一个版本号。

**练习 2**：`PdfStandards::default()` 为什么敢用 `.unwrap()` 而 `PdfStandards::new()` 要仔细处理错误？

> **参考答案**：`Default` 只设置了一个版本号（PDF 1.7）、不挂任何校验器。krilla 的 `ConfigurationError` 只在「多个校验器版本区间不重叠」或「显式版本与校验器区间不符」时才发生；没有校验器就不可能冲突，所以 `finish()` 必然成功，`.unwrap()` 安全。而 `new()` 接受任意用户输入，必须处理可能的区间冲突。

**练习 3**：用户传入 `&[A_1a, Ua_1]`（一个 PDF/A + 一个 PDF/UA），闭包门会让它通过吗？

> **参考答案**：**会通过三道闭包门**。因为 `A_1a` 走的是 `set_archival_validator`、`Ua_1` 走的是 `set_accessibility_validator`，是两个不同的闭包，互不干扰。它能否最终成功，取决于 krilla 在 `builder.finish()` 里对这两个校验器的版本区间是否相容——这就引出下一节。

---

### 4.3 version_hint() 与 ConfigurationError：冲突的诊断信息

#### 4.3.1 概念说明

当用户的选择通过了 typst-pdf 的三道闭包门（即：至多一个版本、至多一个 PDF/A、至多一个 PDF/UA），还会面临 krilla 的最后一道关卡：**版本区间是否真的相容**。krilla 在 `builder.finish()` 时可能返回 `ConfigurationError`，typst-pdf 要把它翻译成 Typst 用户能看懂的、带「修正建议（hint）」的诊断信息。

这里有两类 krilla 错误：

1. **`NoOverlappingValidatorsRange`**：用户挂了多个校验器（一个 PDF/A + 一个 PDF/UA），但它们的版本区间**彼此不相交**。
2. **`VersionDoesNotMatchValidatorsRange`**：用户显式设了一个版本号（如 `V_1_4`），同时挂了一个不支持该版本的校验器（如 PDF/A-4 需要 PDF 2.0）。

#### 4.3.2 核心流程

错误映射的核心是把 krilla 的「冷冰冰的结构化错误」转换成「人话 + 可操作的 hint」：

```text
builder.finish() 返回 Err(ConfigurationError)
        │
        ├── NoOverlappingValidatorsRange(validators)
        │       → 消息: "<A> and <B> are mutually incompatible because
        │                they do not have any overlapping PDF versions"
        │       → 每个 validator 配一个 version_hint
        │
        └── VersionDoesNotMatchValidatorsRange(version, validators)
                → 消息: "<version> is not compatible with <A> and <B>"
                → 每个 validator 配一个 version_hint
        │
        ▼
HintedString { message, hints: [version_hint(v) for v in validators] }
```

`version_hint()` 的职责：读出某个校验器能接受的版本区间 \([min, max]\)，生成一句类似「PDF/A-1a requires version 1.4」「PDF/A-4 requires at least 2.0」的建议，告诉用户该选什么版本。

#### 4.3.3 源码精读

错误映射的完整逻辑在 `builder.finish().map_err(...)`：

[finish() 错误映射](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L196-L218)：先 `match` 出 `ConfigurationError` 的两个变体，各自拼出主消息（用 `validators.to_and_list()` 把校验器列表格式化成「A and B」），再 `with_hints(validators.into_iter().map(version_hint))` 给每个校验器附上一条版本建议。

注意这里用到的 `to_and_list()` 来自 util 模块的 `ValidatorsExt`：

[ValidatorsExt::to_and_list](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L145-L161)：把校验器列表按英文「and」连接（两人用「A and B」，三人及以上用牛津逗号「A, B, and C」），让错误消息读起来自然。

`version_hint()` 自身根据校验器的 `min()`/`max()` 三种情况生成不同措辞：

[version_hint 函数](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L225-L242)：

- `min == max`（只接受单一版本）：`"X requires version Y"`。
- `min` 和 `max` 都有且不等：`"X requires a version between A and B"`。
- 只有 `max`（即 `min` 为 `None`，表示「至少」）：`"X requires at least B"`。

> 说明：每个校验器具体能接受哪些版本，是 krilla 在 `Validator::min()` / `Validator::max()` 里定义的，本讲不展开 krilla 内部实现。typst-pdf 只是「读取并展示」这些区间。

#### 4.3.4 代码实践

**实践目标**：理解两种 `ConfigurationError` 的触发条件与对应的诊断输出。

**操作步骤**：

1. **场景 A（版本区间不重叠）**：假想 `PdfStandards::new(&[A_1a, Ua_1])`。回忆背景知识：PDF/A-1 基于 PDF 1.4、PDF/UA-1 基于 PDF 1.7，两者校验器的版本区间很可能不相交。
2. 跟踪代码：三道闭包门通过（一个是 archival、一个是 accessibility）→ 进入 [finish() 错误映射](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L196-L218) → krilla 判定区间不重叠 → 命中 `NoOverlappingValidatorsRange` 分支。
3. **场景 B（显式版本与校验器不符）**：假想 `PdfStandards::new(&[V_1_4, A_4])`。`V_1_4` 显式锁死版本为 1.4，而 PDF/A-4 需要 PDF 2.0。
4. 跟踪代码：`set_version(Pdf14)` 与 `set_archival_validator(A4)` 各走各的闭包、都通过 → krilla 发现「1.4 不在 A-4 的区间内」→ 命中 `VersionDoesNotMatchValidatorsRange` 分支 → 消息形如 `"1.4 is not compatible with PDF/A-4"`，并附 hint `"PDF/A-4 requires ... 2.0"`。

**需要观察的现象**：两种错误的主消息措辞不同——一个强调「两者彼此不兼容」，另一个强调「这个具体版本不兼容」。

**预期结果**：

| 场景 | 触发分支 | 主消息大意 |
|---|---|---|
| `A_1a` + `Ua_1` | `NoOverlappingValidatorsRange` | 「两者不兼容，因为没有重叠的 PDF 版本」 |
| `V_1_4` + `A_4` | `VersionDoesNotMatchValidatorsRange` | 「1.4 与 PDF/A-4 不兼容」 |

> 待本地验证：上面两个场景能否复现、krilla 校验器的精确版本边界，需要在本地 typst workspace 跑测试确认（krilla 内部的 `min()/max()` 实现可能随版本调整）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 typst-pdf 要把 krilla 的 `ConfigurationError` 重新包装成 `HintedString`，而不是直接把 krilla 的错误透传给用户？

> **参考答案**：为了让用户拿到**可操作的修正建议**。krilla 的错误是结构化的、面向库开发者的；typst-pdf 的用户（写 Typst 文档的人）需要的是「我该把版本号改成多少」。`version_hint()` 正是把校验器的版本区间翻译成一句人话，附在主消息后面，告诉用户如何修改输入。

**练习 2**：`version_hint` 在「`min == max`」时打印 `"requires version Y"`，在「只有 `max`」时打印 `"requires at least B"`。这两种措辞分别对应校验器的什么版本约束形态？

> **参考答案**：`min == max` 表示校验器**只接受唯一一个版本**（区间退化为一个点），所以用「requires version Y」。只有 `max`（`min` 为 `None`）表示校验器**接受该版本及以上**（无上界），所以用「requires at least B」。介于两者之间的开/闭区间则用「requires a version between A and B」。

---

### 4.4 PdfOptions::accessibility_validator()：校验器如何流向下游

#### 4.4.1 概念说明

装配好的 `Configuration` 会被传给 krilla 做最终序列化（见 `convert.rs`）。但 typst-pdf 自己在**转换过程中**也需要知道「现在是否处于 PDF/UA 校验之下」，因为有些行为在 PDF/UA 下必须更严格。为此 `PdfOptions` 提供了一个查询方法 `accessibility_validator()`。

> **关键区分（本讲最重要的概念之一）**：
> - **tagged PDF**：写入逻辑结构树（structure tree）。由 `PdfOptions::tagged` 控制，**默认 `true`**。
> - **PDF/UA 校验**：对结构树做严格合法性检查（标题层级、表格结构、替代文本等）。只有当 `standards` 里包含 `Ua_1` 时才启用。
>
> 也就是说：**默认配置会生成 tagged PDF，但不会做 PDF/UA 校验**。`accessibility_validator()` 返回 `Some` **当且仅当**标准里含 PDF/UA。

#### 4.4.2 核心流程

```text
PdfOptions { standards: PdfStandards { config }, tagged: bool }
        │
        │  standards.config.validators().accessibility()
        ▼
PdfOptions::accessibility_validator() -> Option<Accessibility>
        │
        │  返回 Some(UA1)  ⟺  用户在 standards 里选了 Ua_1
        ▼
下游模块据此决定是否做「严格版」行为：
   ├─ tags/resolve: 是否对结构树做 validate_children（PDF/UA 结构校验）
   ├─ tags/tree:    是否对标题层级跳级、空标题报错
   ├─ link:         是否合并多行链接注记、是否禁止 tiling 内的链接
   └─ ...
```

#### 4.4.3 源码精读

方法本身极简，只是一个对 krilla `config` 的查询：

[accessibility_validator 方法](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L91-L97)：`self.standards.config.validators().accessibility()`——从 `config` 取出全部校验器，再筛出「无障碍」那一个。文档注释明确写了「Returns `Some` for PDF/UA-1, and in the future maybe PDF/UA-2」。

它的消费点遍布整个 crate。举三个真实例子（后续讲义会深入，这里只需理解「它在驱动严格行为」）：

- **结构树校验**：[src/tags/resolve/mod.rs#L195-L197](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/resolve/mod.rs#L195-L197) 处，只有 `accessibility_validator().is_some()` 时才调用 `validate_children` 做表格/列表/标题的结构合法性检查。
- **标题层级**：[src/tags/resolve/mod.rs#L329-L331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/resolve/mod.rs#L329-L331) 处，只有 PDF/UA 下才对「标题从 H1 直接跳到 H3」这类跳级报错。
- **链接注记合并**：[src/link.rs#L145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L145) 处，PDF/UA 下用 quadpoints 把跨行链接合并成单个注记。

而装配好的 `config` 最终流向序列化设置：

[convert.rs 的 SerializeSettings](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L54-L64)：`configuration: options.standards.config` 把整个标准配置塞进 krilla 的 `SerializeSettings`，由 krilla 在写出 PDF 字节时统一执行校验。同一处 `enable_tagging: options.tagged` 则单独控制「是否写结构树」——再次印证 tagged 与 PDF/UA 是两条独立的开关。

最后补一个细节：`PdfStandards` 还实现了 `Hash`，用于增量编译缓存：

[Hash for PdfStandards](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L264-L271)：因为 krilla 的 `Configuration` 没有实现 `Hash`，typst-pdf 手动把「版本号 + 每个校验器」哈希进去。这样 `PdfOptions` 的 `Hash`（供增量编译判断配置是否变化）才能正常工作——上一讲提到的「`Hash` 供增量编译缓存复用」正落在这里。

#### 4.4.4 代码实践

**实践目标**：用源码验证「tagged PDF」与「PDF/UA 校验」是两条独立开关。

**操作步骤**：

1. 打开 [PdfOptions::default](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L99-L111)，确认 `tagged: true`、`standards: PdfStandards::default()`。
2. 打开 [Default for PdfStandards](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L250-L260)，确认默认 `config` 没有 accessibility validator。
3. 推理：在默认配置下，`accessibility_validator()` 调用 `self.standards.config.validators().accessibility()` 会返回 `None`（因为没有挂 `Ua_1`）。
4. 对比两种用户配置：
   - **配置甲**：`standards: PdfStandards::default()`、`tagged: true`（即默认）→ 写结构树，但**不做** PDF/UA 校验。
   - **配置乙**：`standards: PdfStandards::new(&[Ua_1]).unwrap()`、`tagged: true` → 写结构树，**且做** PDF/UA 校验（`accessibility_validator()` 返回 `Some`）。

**需要观察的现象**：`tagged` 字段控制「写不写结构树」，`standards` 里有没有 `Ua_1` 控制「要不要校验这棵树」。两者可以独立组合。

**预期结果**：能清楚说出——默认导出的 PDF 含结构树（可被辅助技术读取到一定程度），但若你需要保证完全符合 PDF/UA（标题不跳级、图片有 alt、表格结构合法），必须显式在 `standards` 里加入 `Ua_1`，此时 `accessibility_validator()` 才返回 `Some`，下游的严格检查才会开启。

> 待本地验证：可写一个最小测试，分别对默认 `PdfOptions` 和含 `Ua_1` 的 `PdfOptions` 调用 `accessibility_validator()`，断言前者为 `None`、后者为 `Some`。

#### 4.4.5 小练习与答案

**练习 1**：用户只设置了 `tagged: true`（其他默认），导出的 PDF 是 PDF/UA 文档吗？为什么？

> **参考答案**：**不是**。`tagged: true` 只保证写入了结构树，但 PDF/UA 还要求结构树通过一系列严格校验。默认 `standards` 不含 `Ua_1`，所以 `accessibility_validator()` 返回 `None`，下游的 `validate_children`、标题跳级检查等都不会执行，文档并未被验证为符合 PDF/UA。

**练习 2**：如果未来 krilla 的 `Configuration` 实现了 `Hash`，[Hash for PdfStandards](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L264-L271) 上方的注释说「可以改成 derive」。为什么现在不能直接 `#[derive(Hash)]`？

> **参考答案**：`#[derive(Hash)]` 要求结构体的**所有字段**都实现 `Hash`。`PdfStandards` 的唯一字段 `config: krilla::configure::Configuration` 目前没有实现 `Hash`，所以无法 derive。typst-pdf 用手动实现绕过：只哈希「版本号 + 各校验器」这些可哈希的子部分，恰好能区分不同的标准配置，满足增量编译缓存的需求。

**练习 3**：为什么 `accessibility_validator()` 的文档特意写「in the future maybe PDF/UA-2」？

> **参考答案**：因为 `PdfStandard` 带有 `#[non_exhaustive]`，typst-pdf 计划未来增加 `Ua_2` 等新标准。届时 `accessibility_validator()` 可能返回代表 PDF/UA-2 的 `Accessibility` 变体。注释提前说明这个方法「现在只对 PDF/UA-1 返回 `Some`，但语义上是为所有无障碍标准服务」，避免下游误以为它永远只对应 UA-1。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「配置诊断小任务」。

**任务背景**：有四位用户分别用下面四种 `PdfOptions`（只列关键字段）导出文档，请你预测每一种的结果（PDF 版本、是否 PDF/A、是否 PDF/UA、是否 tagged、会不会报错）：

| 用户 | `standards` | `tagged` |
|---|---|---|
| 甲 | `PdfStandards::default()` | `true` |
| 乙 | `new(&[V_1_4])` | `true` |
| 丙 | `new(&[A_2b, Ua_1])` | `true` |
| 丁 | `new(&[A_1a, A_2b])` | `false` |

**操作步骤**：

1. 对每一行，先判断会不会被 `PdfStandards::new()` 的**三道闭包门**拦下（版本重复？两个 PDF/A？两个 PDF/UA？）。
2. 若通过闭包门，再判断 krilla `finish()` 的**版本区间检查**是否可能拦下（回忆背景知识里各 PDF/A 代对应的底层版本）。
3. 对通过校验的，写出最终 PDF 版本与是否启用 `accessibility_validator()`。
4. 最后用一句话说明：把 `tagged` 从 `true` 改成 `false`（如用户丁那样）会影响什么、不影响什么。

**参考结论（请先自己推导再对照）**：

- **甲**：默认，PDF 1.7，非 PDF/A、非 PDF/UA，写 tagged 结构树但不校验。`accessibility_validator()` → `None`。
- **乙**：显式 PDF 1.4，无校验器，正常通过。`accessibility_validator()` → `None`。
- **丙**：一个 PDF/A-2（基于 PDF 1.7）+ 一个 PDF/UA-1（基于 PDF 1.7），闭包门通过；两者版本区间相容（均在 1.7 附近），大概率通过 krilla 检查 → PDF 1.7，同时是 PDF/A-2b 与 PDF/UA-1，`accessibility_validator()` → `Some`。（待本地验证精确区间。）
- **丁**：`A_1a` 与 `A_2b` 都走 `set_archival_validator`，第二个直接 `bail!("choose at most one PDF/A standard")`，**根本到不了**序列化阶段，`tagged: false` 在这种情形下毫无意义。

通过这个练习你会体会到：`standards` 决定「目标与校验」，`tagged` 决定「是否写结构树」，而 `accessibility_validator()` 是两者交汇处、供下游判断「要不要较真」的开关。

## 6. 本讲小结

- `PdfStandard` 是给用户的「原料菜单」，把三类目标（PDF 版本、PDF/A 归档、PDF/UA 无障碍）统一在一个 `#[non_exhaustive]` 枚举里，`#[serde(rename)]` 决定用户输入字符串。
- `PdfStandards::new()` 用三道闭包门（`set_version` / `set_archival_validator` / `set_accessibility_validator`）拦下「版本重复、两个 PDF/A、两个 PDF/UA」这三类最常见冲突，再交给 krilla 的 `ConfigurationBuilder` 装配。
- 真正的版本区间相容性检查发生在 `builder.finish()`，返回的 `ConfigurationError`（`NoOverlappingValidatorsRange` 或 `VersionDoesNotMatchValidatorsRange`）被翻译成带 `version_hint` 的 `HintedString`，给用户可操作的修正建议。
- 默认配置（`PdfStandards::default()`）是 **PDF 1.7、无任何校验器**；默认 `PdfOptions` 在此基础上 `tagged: true`。
- **tagged PDF ≠ PDF/UA**：`tagged` 控制写不写结构树，`Ua_1` 控制「校验」这棵树；`PdfOptions::accessibility_validator()` 返回 `Some` 当且仅当标准里含 PDF/UA，它驱动 tags/link 等下游模块的严格行为。
- 装配好的 `config` 经 `SerializeSettings.configuration` 流入 krilla 驱动序列化；`PdfStandards` 手动实现 `Hash` 以支持增量编译缓存。

## 7. 下一步学习建议

本讲结束后，你已经完整理解了 `PdfOptions` 的全部字段及其如何影响最终 PDF。接下来：

1. **进入转换主链路**：阅读 `u2-l5 convert() 编排`，看 `options.standards.config` 是如何被塞进 `SerializeSettings`、并贯穿整个 `convert()` 流程的——这是把「配置」变成「字节」的第一步。
2. **顺带关注 tags 子系统**：当你学到 `u5-l19 tagged PDF 概览` 时，会再次遇到 `accessibility_validator()`；届时你会看到它如何决定结构树是否做 PDF/UA 严格校验。本讲为那一讲打下了「tagged vs PDF/UA」的关键概念基础。
3. **想加深 krilla 理解**：可自行浏览 [krilla 仓库](https://github.com/typst/krilla) 的 `configure` 模块，看 `Validator::min()/max()` 与 `ConfigurationError` 的原始定义——这能帮你把本讲「待本地验证」的版本区间细节补全。
