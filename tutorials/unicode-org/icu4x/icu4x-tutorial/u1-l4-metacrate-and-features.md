# icu 元 crate 与 feature / compiled_data 体系

## 1. 本讲目标

在上一讲里，你已经用 `cargo add icu` 跑通了第一个日期格式化程序。你也许会好奇：为什么只加了一个名叫 `icu` 的依赖，就同时拥有了日期、数字、排序、分段等几十种能力？这些能力是怎么被「打包」进来的？又为什么有的构造函数叫 `try_new`、有的却叫 `try_new_with_buffer_provider`？

学完本讲，你应当能够：

1. 说清楚 `icu` **元 crate（meta-crate）** 的定位——它本身不实现任何国际化算法，只是把一批组件 crate 重新导出（re-export）为模块。
2. 看懂 `icu` 的 Cargo feature 体系，尤其是 `compiled_data`、`serde`、`sync`、`datagen`、`logging`、`unstable` 各自控制什么。
3. 区分**编译期内嵌数据（compiled data）**与**显式 `DataProvider`** 两种数据使用方式，并理解它们对应不同的构造函数。

---

## 2. 前置知识

本讲默认你已经读过前几讲，了解以下概念（这里只做最简提示）：

- **crate**：Rust 的编译/分发单元。ICU4X 仓库里有近 90 个 crate。
- **Cargo workspace**：用根 `Cargo.toml` 把多个 crate 组织在一起；子 crate 用 `workspace = true` 继承共享配置（详见 u1-l2）。
- **Cargo feature**：一个 crate 可以用 `[features]` 声明若干「特性开关」，开启/关闭会改变编译产物包含哪些代码。Feature 之间可以互相启用，也能穿透到依赖的子 crate（如 `"icu_datetime/compiled_data"` 表示「启用 `icu_datetime` 的 `compiled_data` feature」）。
- **compiled data**：默认编译进二进制的 CLDR 数据（详见 u1-l3），让 `try_new` 这类构造函数无需你手动喂数据即可工作。
- **`DataProvider`**：ICU4X 可插拔的数据抽象（u5 会深入）。本讲只需把它理解为「一个能按需返回 locale 数据的对象」。

如果对 feature 的「穿透」机制不熟，记住这一句即可：**`icu` 的 feature 大多是「转发开关」——你在 `icu` 上开一个 feature，它会把这个开关透传给底下所有组件 crate。**

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`components/icu/src/lib.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs) | 元 crate 的全部源码：模块文档（含 feature 说明、数据管理说明）+ 一批 `pub use` 重新导出语句。**整个 crate 几乎就只有这一个有意义的源文件。** |
| [`components/icu/Cargo.toml`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml) | 声明 `icu` 依赖了哪些组件 crate、定义 `[features]`（`default`/`compiled_data`/`serde`/`sync`/`datagen`/`logging`/`unstable` 等）。 |
| [`Cargo.toml`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml)（根，workspace） | 在 `[workspace.dependencies]` 里集中声明 `icu` 及各组件的版本与 `path`。 |
| [`components/datetime/src/neo.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs) | `DateTimeFormatter` 的实现，用来佐证「`try_new` 受 `compiled_data` 门控、`try_new_with_buffer_provider` 受 `serde` 门控」。 |

> 链接说明：所有源码永久链接都指向当前 HEAD `67a0b91c6f`，行号与真实代码一一对应。

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：**元 crate 的 re-export 设计 → 核心 feature 体系 → compiled data vs 显式 provider**。

### 4.1 元 crate 的 re-export 设计

#### 4.1.1 概念说明

所谓**元 crate（meta-crate）**，是指一个本身几乎不含业务逻辑、只负责把其它多个 crate「汇总」到统一命名空间下的 crate。它的价值在于**方便使用者**：

- 你只需 `cargo add icu` 一行，就能用 `icu::datetime`、`icu::decimal`、`icu::collator`……而无需分别添加十几个依赖。
- 命名空间统一：所有能力都在 `icu::*` 之下，文档（docs.rs）也只有一页，便于检索。
- 它**不引入任何独有功能**——`icu::datetime` 背后就是独立的 `icu_datetime` crate。如果你只想用日期，也可以单独依赖 `icu_datetime`，效果完全一样。

模块文档里把这层意思说得很直白：

[components/icu/src/lib.rs:25-31](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L25-L31) —— 文档原文：元 crate 不带来任何独特功能，只是把相关 crate 当作模块重新导出；每个模块对应的 crate 也能单独使用（如 `icu::list` 与独立的 `icu::list` 等价）。

#### 4.1.2 核心流程

元 crate 的「实现」其实就是一连串 `pub use`。其结构可以概括为：

```
icu (元 crate)
├── pub use icu_calendar     as calendar;      // 日历与日期类型
├── pub use icu_casemap      as casemap;       // 大小写映射
├── pub use icu_collator     as collator;      // 排序
├── pub use icu_datetime     as datetime;      // 日期时间格式化
├── pub use icu_decimal      as decimal;       // 十进制数字格式化
├── pub use icu_list         as list;          // 列表格式化
├── pub use icu_locale       as locale;        // locale 规范化/回退/方向
├── pub use icu_normalizer   as normalizer;    // Unicode 规范化
├── pub use icu_plurals      as plurals;       // 复数规则
├── pub use icu_properties   as properties;    // 字符属性
├── pub use icu_collections  as collections;   // 紧凑集合/trie 数据结构
├── pub use icu_segmenter    as segmenter;     // 文本分段
├── pub use icu_time         as time;          // 时间/时区类型
├── (unstable) pub use icu_experimental as experimental;
└── (unstable) pub use icu_pattern      as pattern;
```

执行过程（从「你写下 `icu::datetime::...`」到「真正调用到代码」）：

1. 编译器看到 `icu::datetime`，沿着 `pub use icu_datetime as datetime;` 找到真正的 crate `icu_datetime`。
2. 因为 `icu` 的 `Cargo.toml` 把 `icu_datetime` 列为普通依赖，`icu_datetime` 被编译进来。
3. 之后所有的类型、函数都直接来自 `icu_datetime`，`icu` 这一层**零运行期开销**——它纯粹是编译期的名字重定向。

#### 4.1.3 源码精读

re-export 的全部语句集中在 lib.rs，每条都是同一套写法 `#[doc(inline)] pub use icu_X as X;`：

[components/icu/src/lib.rs:140-177](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L140-L177) —— 13 条无条件 `pub use`，把 13 个稳定组件 crate 重新导出为 `calendar / casemap / collator / datetime / decimal / list / locale / normalizer / plurals / properties / collections / segmenter / time` 模块。

其中两条是「有条件」的：

[components/icu/src/lib.rs:179-185](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L179-L185) —— `experimental` 与 `pattern` 两个模块被 `#[cfg(feature = "unstable")]` 门控，只有开启 `unstable` feature 时才出现在 `icu::*` 下。这体现了 ICU4X 的版本策略：不稳定 API 不进默认命名空间，避免被误用为「稳定接口」。

`#[doc(inline)]` 的作用：让被重导出的模块在 docs.rs 上**内联**显示在 `icu` 下，而不是只留一个跳转链接，从而保证 `icu` 这一页文档自成一体。

再看依赖侧的对应关系。`icu` 把这些组件列为依赖（普通依赖为必需、`optional = true` 为可选）：

[components/icu/Cargo.toml:23-45](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L23-L45) —— `[dependencies]` 列出 13 个稳定组件（必需）+ `icu_experimental`、`icu_pattern`（`optional = true`）+ `icu_provider`（仅为文档链接与 feature 穿透）。注意部分组件显式带 `features = ["alloc"]`，因为元 crate 在 `no_std` 下仍需要 `alloc`。

`experimental` 与 `pattern` 是 `optional` 依赖，它们的「启用」由 `unstable` feature 通过 `dep:icu_experimental` / `dep:icu_pattern` 来触发（见 4.2.3）。lib.rs 的 `#[cfg(feature = "unstable")]` 与 Cargo.toml 的 `dep:` 声明必须**成对**出现：前者控制「模块是否被导出」，后者控制「可选依赖是否被编译」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`icu` 只是一层 re-export」，并体会 `#[cfg(feature)]` 对模块可见性的影响。

**操作步骤**：

1. 打开 [`components/icu/src/lib.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs)，数一数无条件的 `pub use` 共有多少条（应为 13 条，覆盖 13 个稳定模块）。
2. 阅读文件顶部的模块文档（第 18–135 行的 `//!` 注释），这是元 crate 唯一的「实质内容」。
3. 对照 [`components/icu/Cargo.toml`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml) 的 `[dependencies]`，确认每条 `pub use icu_X as X;` 都能在依赖列表里找到 `icu_X`。

**需要观察的现象**：lib.rs 里没有任何 `fn`、`struct`、`impl`（除了第 138 行一句 `use icu_provider as _;` 仅用于文档链接）。整个 crate 的「代码」就是模块文档 + re-export。

**预期结果**：你会直观感受到「元 crate 没有逻辑」这句话——它的源码长度几乎等于文档长度。

> 待本地验证：若你想进一步确认「单独依赖组件等价于走元 crate」，可以在一个测试项目里分别 `cargo add icu_datetime` 与 `cargo add icu`，比较 `use icu_datetime::...` 与 `use icu::datetime::...` 两种写法编译出的可执行文件体积是否接近。

#### 4.1.5 小练习与答案

**练习 1**：`icu::experimental` 和 `icu::pattern` 为什么不能像 `icu::datetime` 那样默认可用？

**参考答案**：因为它们对应的 `pub use` 被 `#[cfg(feature = "unstable")]` 门控（[lib.rs:179-185](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L179-L185)），且底层 `icu_experimental`/`icu_pattern` 是 `optional = true` 依赖。`experimental` 是孵化器 crate、`pattern` 尚在演进，ICU4X 不希望它们被当作稳定 API 默认暴露。

**练习 2**：如果你只想要排序功能，依赖 `icu`（元 crate）和直接依赖 `icu_collator` 哪个体积更小？为什么？

**参考答案**：直接依赖 `icu_collator` 通常更小。依赖 `icu` 会把 13 个稳定组件全部拉进来（即便开启 dead-code elimination，元数据与部分不可消除的代码仍会增加体积）；只要 `collator` 就只编译 `icu_collator` 及其传递依赖。元 crate 的便利性以「潜在更大的依赖图」为代价。

---

### 4.2 核心 feature 体系

#### 4.2.1 概念说明

`icu` 的 `[features]` 大多是**转发开关**：在 `icu` 上开一个 feature，它会把同名/相关 feature 透传给所有组件 crate。这样使用者只需在一处（`icu`）配置，而无需逐个组件去开。

理解这套体系的关键是分清两类 feature：

- **能力类 feature**：决定「某段代码/某类构造函数是否被编译」。典型：`compiled_data`、`serde`、`datagen`。
- **行为/平台类 feature**：决定「编译出的代码以何种方式运行」。典型：`sync`、`logging`。

#### 4.2.2 核心流程

各 feature 的作用一览（与 lib.rs 顶部「Features」文档完全对应）：

| Feature | 默认? | 作用 | 在 `icu` 层如何转发 |
| --- | --- | --- | --- |
| `compiled_data` | ✅ 默认 | 内嵌编译期数据，启用 `try_new`/`new` 这类「无需喂数据」的构造函数 | 透传 `icu_*/compiled_data` 给 13 个组件 |
| `serde` | ❌ | 为核心类型（如 `Locale`）启用 serde 实现；启用 `*_with_buffer_provider` 构造函数 | 透传 `icu_*/serde` |
| `sync` | ❌ | 让大多数 ICU4X 对象实现 `Send + Sync`（运行期数据时有小幅性能损耗） | `["icu_provider/sync"]` |
| `logging` | ❌ | 通过 `log` crate 输出日志 | `["icu_provider/logging", "icu_datetime/logging"]` |
| `datagen` | ❌ | 启用「仅在数据生成阶段需要」的功能（并拉入 `icu_provider_registry`、`memchr`） | 透传 `icu_*/datagen` + `dep:` |
| `unstable` | ❌ | 启用 `experimental`/`pattern` 模块及部分组件的不稳定 API | `dep:icu_experimental`/`dep:icu_pattern` + `icu_*/unstable` |
| `default` | ✅ | 启用所有组件各自的 `default`（组件的 default 通常含 `compiled_data`） | 透传 `icu_*/default` |

#### 4.2.3 源码精读

先看模块文档里对 feature 的权威说明（这段文档是面向使用者的「速查表」）：

[components/icu/src/lib.rs:109-120](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L109-L120) —— 文档逐条列出 `compiled_data`/`datagen`/`logging`/`serde`/`sync` 的含义。注意原文明确：`compiled_data`（默认）若关闭，则只剩带显式 `provider` 参数的构造函数；`serde` 会激活 `*_with_buffer_provider` 构造函数。

再看 Cargo.toml 里这些 feature 的真正定义。`default` 是「转发到每个组件的 default」：

[components/icu/Cargo.toml:59-74](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L59-L74) —— `default` 列出 13 个组件的 `.../default` 加上 `icu_experimental?/default`、`icu_pattern?/default`。结尾的 `?` 表示「当该可选依赖被启用时才转发」（弱依赖语法）。

`compiled_data` 与 `serde` 同样是纯转发，注意 `compiled_data` 没有转发给 `icu_pattern`（pattern 无 compiled data 概念）：

[components/icu/Cargo.toml:92-106](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L92-L106) —— `compiled_data` 透传给 13 个组件（含 `icu_experimental?`），不含 `icu_pattern`。

`sync` 与 `logging` 是「行为类」feature，转发面很窄——只穿透到真正相关的底层 crate：

[components/icu/Cargo.toml:137-138](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L137-L138) —— `sync = ["icu_provider/sync"]`、`logging = ["icu_provider/logging", "icu_datetime/logging"]`。`sync` 的本质是让 `icu_provider` 内部的数据载体使用 `RwLock` 等同步原语，从而满足 `Send + Sync`。

`unstable` 与 `datagen` 都会触发 `dep:` 来启用可选依赖：

[components/icu/Cargo.toml:127-136](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L127-L136) —— `unstable` 用 `dep:icu_experimental`、`dep:icu_pattern` 启用两个可选依赖，这正是 4.1.3 里 lib.rs `#[cfg(feature = "unstable")]` 所等待的「另一半」。

> 小贴士：`default-features = false` 的细节。根 `Cargo.toml` 在 workspace 层把 `icu` 声明为 `default-features = false`（[Cargo.toml:141](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/Cargo.toml#L141-L141)），但这只影响**仓库内部其它 crate** 通过 `workspace = true` 引用 `icu` 时的默认值。外部用户执行 `cargo add icu` 时，cargo 默认会开启 `icu` 自己的 `default` feature（即 `compiled_data`）。这就是上一讲你能直接 `try_new` 成功的原因。

#### 4.2.4 代码实践

**实践目标**：用一张表把 feature 与「它编译进/排除的代码」对应起来。

**操作步骤**：

1. 打开 [`components/icu/Cargo.toml`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L58-L138) 的 `[features]` 段。
2. 对 `compiled_data`、`serde`、`sync`、`datagen`、`unstable` 五个 feature，分别记录：「它透传给了哪些组件」「它启用了哪些 `dep:` 可选依赖」。
3. 对照 lib.rs 的「Features」文档（[lib.rs:109-120](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L109-L120)），确认文档描述与 Cargo.toml 定义一致。

**需要观察的现象**：能力类 feature（`compiled_data`/`serde`/`datagen`）的透传列表很长（覆盖 13 个组件）；行为类 feature（`sync`/`logging`）的透传列表很短（只到 `icu_provider` 等 1–2 个底层 crate）。

**预期结果**：你会得到一张「feature → 影响范围」对照表，今后配置依赖时能准确判断开/关某个 feature 会波及哪些代码。

> 待本地验证：在一个测试项目里分别 `cargo add icu` 与 `cargo add icu --no-default-features --features serde`，用 `cargo tree` 观察依赖图与 feature 启用情况的差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sync` 只转发到 `icu_provider/sync`，而不是像 `serde` 那样转发给全部 13 个组件？

**参考答案**：`Send + Sync` 的实现关键在**数据载体**（`DataPayload` 等）内部是否使用同步原语，而这由 `icu_provider` 决定。组件 crate 本身的代码不需要为 `sync` 改变编译内容——只要底层 payload 满足 `Send + Sync`，包裹它的组件对象自然也就满足。所以只需穿透一层到 `icu_provider`。而 `serde` 需要为每个组件的类型分别派生/实现 serde，故必须逐个转发。

**练习 2**：开启 `unstable` feature 后，`icu::pattern` 模块变得可用，但这需要哪两处配合？

**参考答案**：一是 Cargo.toml 里 `unstable` 通过 `dep:icu_pattern` 启用可选依赖（[Cargo.toml:135](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L135-L135)）；二是 lib.rs 里 `#[cfg(feature = "unstable")] pub use icu_pattern as pattern;`（[lib.rs:183-185](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L183-L185)）。二者缺一不可。

---

### 4.3 compiled data vs 显式 provider

#### 4.3.1 概念说明

ICU4X 的国际化算法大多是**数据驱动**的（依赖对 locale 专家的调研结果，即 CLDR 数据）。`icu` 提供两种获取这些数据的方式，它们对应**两套不同的构造函数**：

1. **编译期内嵌数据（compiled data）**：数据在编译期就「烘焙」进二进制（底层是代码生成的 `BakedDataProvider`）。优点是代码极简、零拷贝加载、未用数据可被 dead-code elimination 移除；代价是二进制体积变大、数据在编译期固定。对应**惯用构造函数** `new` / `try_new`。

2. **显式 `DataProvider`**：你在运行期把一个 provider 对象传给 ICU4X。优点是灵活——可从 blob 文件、文件系统、操作系统等任意来源取数据，可运行期更新、可按需包含/排除、可组合多个 provider；代价是代码更繁琐。对应**特殊构造函数**，如 `try_new_with_buffer_provider`、`try_new_unstable`。

一句话区分：**compiled data = 数据跟着代码走（编译期决定）；显式 provider = 数据由你喂数（运行期决定）。**

#### 4.3.2 核心流程

两种方式的构造路径对比（以 `DateTimeFormatter` 为例）：

```
【compiled data 路径】（需 compiled_data feature）
  locale!("es-US")
    └─> DateTimeFormatter::try_new(prefs, YMD::medium())   // 无 provider 参数
          └─> 内部自动用 crate::provider::Baked 取数据
                └─> 返回 Result（数据缺失时报错）

【显式 provider 路径】（需 serde feature + 自备 provider）
  let blob: Box<[u8]> = ...;
  BlobDataProvider::try_new_from_blob(blob)
    └─> LocaleFallbacker::try_new_with_buffer_provider(&provider)
          └─> LocaleFallbackProvider::new(provider, fallbacker)
                └─> DateTimeFormatter::try_new_with_buffer_provider(&provider, prefs, YMD::medium())
                      └─> 返回 Result
```

显式路径之所以更啰嗦，是因为你要自己负责：构造存储后端（blob/fs）、装配回退链（`LocaleFallbackProvider`）、再传给格式化器。compiled data 路径把这些全自动化了。

#### 4.3.3 源码精读

模块文档用两个代码示例清晰对比了两种方式。先看 compiled data 版（最简）：

[components/icu/src/lib.rs:39-50](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L39-L50) —— compiled data 示例：`DateTimeFormatter::try_new(locale!("es-US").into(), YMD::medium())`，没有 provider 参数，`expect` 断言 compiled data 包含 `es-US`。文档还点明：未使用的 compiled data 可被 dead-code elimination 优化掉，且可用 `icu4x-datagen --format mod` + `ICU4X_DATA_DIR` 自定义数据集。

再看显式 provider 版（完整数据管道）：

[components/icu/src/lib.rs:64-87](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L64-L87) —— 显式数据示例：从 `Box<[u8]>` 构造 `BlobDataProvider`，再用 `LocaleFallbacker` + `LocaleFallbackProvider` 包一层回退，最后调用 `try_new_with_buffer_provider`。注意构造函数名不同，且需要传入 `&provider`。

文档紧接着列出显式数据能带来的额外能力：

[components/icu/src/lib.rs:89-95](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L89-L95) —— 显式数据可用于：无回退访问数据、接入操作系统等自定义 provider、懒加载或运行期更新、组合多来源 provider、手动包含/排除数据等。

那么「构造函数受 feature 门控」这件事，在真实组件代码里长什么样？以 `DateTimeFormatter::try_new` 为例：

[components/datetime/src/neo.rs:228-245](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs#L228-L245) —— `try_new` 被 `#[cfg(feature = "compiled_data")]` 门控，并带有 `✨ *Enabled with the compiled_data Cargo feature.*` 的文档提示；其 `where` 子句要求 `crate::provider::Baked` 满足相应数据 marker 约束——也就是说，只有编译期内嵌的 `Baked` provider 备齐了所需数据，这个构造函数才类型检查通过。

与之对照，buffer provider 构造函数由宏生成，受 `serde` feature 控制：

[components/datetime/src/neo.rs:247-253](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs#L247-L253) —— 通过 `gen_buffer_constructors_with_external_loader!` 宏生成 `try_new_with_buffer_provider`（宏内部按 `serde` feature 条件编译）。这与 lib.rs 文档「`serde` 激活 `*_with_buffer_provider` 构造函数」的描述吻合。

> 关键结论：**关闭 `compiled_data` 后，`try_new`/`new` 这类无 provider 参数的构造函数会从 API 中消失，你只能使用带 `provider` 的构造函数（`*_with_buffer_provider` 需 `serde`、`*_unstable` 用于任意 `DataProvider`）。** 这正是本讲实践任务要验证的核心点。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`compiled_data` 控制 `try_new` 的存在性」，理解两种构造方式的边界。

**操作步骤（源码阅读型，无需运行）**：

1. 打开 [`components/datetime/src/neo.rs:228`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs#L228-L245)，确认 `DateTimeFormatter::try_new` 上方有 `#[cfg(feature = "compiled_data")]`。
2. 在同文件搜索 `try_new_with_buffer_provider`，确认它由宏生成、服务于显式数据路径。
3. 回到 [`components/icu/Cargo.toml:92-106`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L92-L106)，确认 `compiled_data` feature 会把这个开关透传给 `icu_datetime`。

**需要观察的现象**：`try_new` 的 `#[cfg]` 与 Cargo.toml 里 `compiled_data -> icu_datetime/compiled_data` 的透传，两者共同决定了「默认情况下 `try_new` 可用、关闭 `compiled_data` 后 `try_new` 消失」。

**预期结果**：你能用自己的话回答——「关闭 `compiled_data` 后，无 provider 参数的 `try_new` 不再编译，构造 `DateTimeFormatter` 必须改用 `try_new_with_buffer_provider`（需开 `serde`）或 `try_new_unstable`，并自行提供 `DataProvider`」。

> 待本地验证：在测试项目里 `cargo add icu --no-default-features --features compiled_data` 仍可 `try_new`；而 `--no-default-features`（不设 compiled_data）时，尝试调用 `DateTimeFormatter::try_new(...)` 应当编译失败，提示找不到该方法。

#### 4.3.5 小练习与答案

**练习 1**：一个嵌入式项目既在意二进制体积、又需要运行期替换 locale 数据，应选哪种数据方式？为什么？

**参考答案**：选显式 `DataProvider`（典型用 `BlobDataProvider`）。compiled data 会把数据编译进二进制、体积大且运行期不可变；blob 方式可把数据放在独立文件里、按需加载、运行期更新，体积可控。这正是 ICU4X 区分两种方式的设计动机。

**练习 2**：为什么 compiled data 路径的 `try_new` 仍然返回 `Result`（而非保证成功）？

**参考答案**：因为 compiled data 默认只覆盖**大部分常用** locale，并非全集。若你请求的 locale（及其回退链）在编译进二进制的小数据集里不存在，`try_new` 仍会失败。所以文档示例用 `.expect("compiled data should include 'es-US'")` 来表达「我确信该 locale 被包含」的断言。这也说明 compiled data 可通过 `icu4x-datagen` 自定义裁剪（见 4.3.3 引用的 lib.rs:55-57）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「配置推演」小任务。

**任务**：假设你要为一个 **多线程服务端程序** 配置 `icu`，需求是——(a) 需要在多个线程间共享格式化器对象；(b) 需要从用户上传的 blob 文件动态加载 locale 数据；(c) 不需要数据生成能力。请回答：

1. 你会在 `cargo add icu` 时开启哪些 feature？写出完整的 `--features` 参数。
2. 在这个配置下，构造 `DateTimeFormatter` 应该用 `try_new` 还是 `try_new_with_buffer_provider`？为什么？
3. 若此时误用 `try_new`，会发生什么？

**参考答案**：

1. 需要 `--features serde,sync`（并保留默认的 `compiled_data` 也无妨，但本题真正必需的是 `serde` 与 `sync`）。
   - `serde`：启用 `*_with_buffer_provider` 构造函数以支持 blob 数据（依据 [lib.rs:118-119](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L118-L119)）。
   - `sync`：让格式化器对象满足 `Send + Sync`，可跨线程共享（依据 [Cargo.toml:137](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/Cargo.toml#L137-L137)）。
   - 不需要 `datagen`（那是数据生成工具链才用的）。
2. 应当用 `try_new_with_buffer_provider`。因为数据来源是运行期的 blob 文件，属于显式 provider 路径；`try_new` 只能读编译期内嵌的 compiled data，无法消费运行期 blob。
3. 误用 `try_new`（在保留 `compiled_data` 时）虽然能编译，但它完全忽略你提供的 blob、只读编译期数据，导致「用户上传的数据不生效」的隐性 bug；若你关闭了 `compiled_data`，则 `try_new` 直接编译失败（依据 [neo.rs:231](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs#L231-L231) 的 `#[cfg(feature = "compiled_data")]`）。

> 待本地验证：按上述 `--features serde,sync` 搭建测试项目，写出 [lib.rs:64-87](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs#L64-L87) 的显式数据示例，确认它能编译通过并把 formatter 移到另一个线程使用。

---

## 6. 本讲小结

- `icu` 是**元 crate**：自身不含算法，仅用 `pub use icu_X as X;` 把 13 个稳定组件（calendar/casemap/collator/datetime/decimal/list/locale/normalizer/plurals/properties/collections/segmenter/time）重导出为模块，外加 `unstable` feature 下的 experimental、pattern。
- `icu` 的 feature 多为**转发开关**：`compiled_data`/`serde`/`datagen` 透传给全部组件；`sync`/`logging` 仅穿透到 `icu_provider` 等底层 crate；`unstable` 用 `dep:` 启用可选依赖。
- **`compiled_data`（默认）**控制 `try_new`/`new` 这类无 provider 参数的构造函数是否存在；关闭后这些构造函数从 API 消失。
- **`serde`** 激活核心类型的 serde 实现，并启用 `*_with_buffer_provider` 构造函数，是「显式数据」路径的入口。
- **compiled data** = 数据编译期内嵌、代码极简但体积大、运行期不可变；**显式 `DataProvider`** = 运行期喂数据、灵活可更新但代码更繁琐，二者对应不同的构造函数。
- 外部用户 `cargo add icu` 默认得到 `compiled_data`（因 `icu` 自身 `default` feature 开启它）；workspace 层的 `default-features = false` 只影响仓库内部消费者。

---

## 7. 下一步学习建议

本讲建立了「组件 + feature + 数据方式」的全局视图，接下来的学习路径：

- **想用 locale**：进入 u2 单元，从 [u2-l1 Locale 与 LanguageIdentifier 数据模型](u2-l1-locale-model.md) 开始，理解几乎所有组件共用的输入类型。
- **想深入数据机制**：直接跳到 u5 单元，尤其是 [u5-l1 DataProvider 核心抽象](u5-l1-dataprovider-core.md)，把本讲反复提到的 `DataProvider`/`DataMarker`/`DataPayload` 搞懂；之后 [u5-l4 存储后端](u5-l4-storage-backends.md) 会讲透 blob/fs/baked 三种 provider 的差异。
- **想理解 compiled data 的底层**：u6 单元的 [u6-l4 databake 编译期数据烘焙](u6-l4-databake.md) 会揭示「数据如何被烘焙成 Rust 代码」的原理，补全本讲 4.3 节背后的实现。
- **建议同步阅读**：[`components/icu/src/lib.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/icu/src/lib.rs) 顶部模块文档，以及仓库教程 [`tutorials/data-provider-runtime.md`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/tutorials/data-provider-runtime.md) 了解运行期加载数据的完整做法。
