# Feature flags 与可选依赖

## 1. 本讲目标

rust-i18n 把「翻译文件的解析器」（serde_yaml / serde_json / toml / globwalk 等）默认挡在**用户最终二进制之外**——它们只在编译期被 `i18n!` 用一次，运行时根本不需要。要做到这一点，又要在某些场景下（比如运行时动态加载翻译文件）把它们「按需放回来」，靠的就是 Cargo 的 **feature flags 与可选依赖**机制。

本讲学完后，你应该能够：

1. 说清 rust-i18n 暴露给用户的几个 feature（`load-path`、`log-miss-tr`）以及 support crate 内部的 `codegen` feature 各自做什么、如何向下游子 crate 传递。
2. 理解 support crate 用 `optional = true` + `dep:` 语法把 7 个解析器依赖做成「编译期独占」，默认不进运行时二进制。
3. 学会在运行时用 `try_load_locales` 加载 locale 文件（`load-path` feature），并与默认的编译期加载方式做对比。
4. 警惕一个易踩的「撞名陷阱」：cargo feature `load-path` 与 `[package.metadata.i18n]` 配置项 `load-path` 是**完全不同的两回事**。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个 Cargo 概念。

- **依赖（dependency）**：一个 crate 用到的别的 crate。写在 `Cargo.toml` 的 `[dependencies]` 里。
- **可选依赖（optional dependency）**：加了 `optional = true` 的依赖，**默认不会编译进来**，只有当某个 feature 显式启用它时才参与编译。这是「按需打包」的基础。
- **feature**：`Cargo.toml` 里 `[features]` 段定义的一组「开关」。一个 feature 可以「打开若干个可选依赖」，也可以「启用别的 feature」。`[features]` 里写 `xxx = ["dep:serde"]` 表示「启用 `xxx` 这个 feature 时，把可选依赖 `serde` 纳入编译」。
- **feature 传递（forwarding）**：一个 crate 可以在自己的 feature 定义里写 `my-feat = ["other-crate/some-feat"]`，表示「当我启用 `my-feat`，就顺带启用下游 `other-crate` 的 `some-feat`」。这是把用户的开关「接力」传给子 crate 的标准做法。
- **feature 合并（unification）**：Cargo 对同一依赖只编译**一份**，且取所有依赖方启用 feature 的**并集**。所以「某依赖是否带某 feature」要看整个依赖图里有没有任何一方打开了它。
- **`#[cfg(feature = "...")]`**：Rust 源码层的条件编译。被它守卫的代码，只有对应 feature 开启时才参与编译；否则那部分源码对编译器「不存在」。这就是 feature 能从「依赖」层面影响「源码」层面的桥梁。

一句话：**feature = Cargo.toml 里的开关 → 通过 `dep:` 控制可选依赖是否编译 + 通过 `#[cfg(feature=...)]` 控制源码块是否编译**。本讲通篇都在讲这两条线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml`（根 crate） | 定义面向用户的两个 feature `load-path`、`log-miss-tr`，并把它们「转发」给子 crate。 |
| `crates/support/Cargo.toml` | 定义内部 feature `codegen`，用 `dep:` 绑定 7 个可选解析器依赖；非可选的 4 个是运行时类型必需。 |
| `crates/macro/Cargo.toml` | 过程宏 crate **总是**启用 support 的 `codegen`（编译期需要解析器）；定义空的 `log-miss-tr` feature。 |
| `crates/support/src/lib.rs` | 用 `#[cfg(feature = "codegen")]` 守卫全部「加载/解析」逻辑（含 `try_load_locales`）；运行时类型则不带守卫、始终编译。 |
| `crates/macro/src/tr.rs` | `log_missing()` 在 `log-miss-tr` 开/关时生成不同的 token 流，注入到每次 `t!` 未命中处。 |
| `src/lib.rs`（根 crate 门面） | 仅当 `load-path` 开启时，用 `pub use` 把 `try_load_locales` 对外暴露。 |
| `examples/app-load-path/src/main.rs` | 示例：用 `i18n!("../locales")` 从 crate 外部路径做**编译期**加载（注意：它并不演示运行时 `try_load_locales`）。 |
| `README.md` | 对 `log-miss-tr`（需自备 `log`）与 `load-path` 的官方一句话说明。 |

## 4. 核心概念与源码讲解

### 4.1 Cargo feature 机制与 rust-i18n 的三个 feature

#### 4.1.1 概念说明

rust-i18n 里一共涉及「三个 feature」，但它们的**层级不同**，初学者最容易混在一起：

| feature 名 | 定义在哪 | 面向谁 | 作用 |
| --- | --- | --- | --- |
| `load-path` | 根 crate `Cargo.toml` | **用户** | 运行时加载翻译文件，把解析器放进二进制 |
| `log-miss-tr` | 根 crate `Cargo.toml`（接力到 macro） | **用户** | `t!` 命中失败时打印告警日志 |
| `codegen` | `crates/support/Cargo.toml` | **内部**（用户一般不直接开） | 总开关：启用 7 个解析器依赖 + 守卫所有加载/解析源码 |

关键区别：**`codegen` 不是面向用户的根 feature**，它是 support crate 的内部 feature。用户通常**不直接**写 `features = ["codegen"]`，而是通过开 `load-path` 间接打开它（见 4.4）。而真正编译期生成代码的 `macro` crate 会**无条件**打开 `codegen`——因为 `i18n!` 在编译期必须能读 YAML/JSON/TOML。

#### 4.1.2 核心流程

用户启用一个根 feature 后，开关如何「接力」到子 crate，可用下面这条链路描述：

```
用户 Cargo.toml:  rust-i18n = { features = ["load-path"] }
        │  根 [features] 里 load-path = ["rust-i18n-support/codegen"]
        ▼
support crate 的 codegen feature 被打开
        │  codegen = ["dep:serde", "dep:serde_yaml", ...]
        ▼
7 个可选依赖被纳入编译  ──►  #[cfg(feature="codegen")] 守卫的源码全部生效
        │
        ▼
try_load_locales 可用，且被根 crate 条件 pub use 暴露
```

`log-miss-tr` 同理，只是接力目标换成 `rust-i18n-macro`。

#### 4.1.3 源码精读

根 crate 的 `[features]` 段只有两行，是「转发壳」：[Cargo.toml:56-58](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L56-L58)。这两行说明：

- `log-miss-tr = ["rust-i18n-macro/log-miss-tr"]`：开它 = 开 macro 的同名 feature。
- `load-path = ["rust-i18n-support/codegen"]`：开它 = 开 support 的 `codegen`。

注意根 `[features]` **没有** `codegen` 这一项，也没有 `default`——所以这两个 feature 全部是**默认关闭、需用户显式开启**的。同时根 crate 对 support 的普通依赖**不带**任何 feature：[Cargo.toml:51-54](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L51-L54)（`rust-i18n-support.workspace = true` 没有跟 `features = [...]`）。这正是「默认不把解析器编进用户二进制」的根源。

#### 4.1.4 代码实践

**实践目标**：确认根 crate 的 feature 确实会接力到子 crate。

**操作步骤**：

1. 在仓库根目录新建一个临时 bin（或复用任意依赖 `rust-i18n` 的 crate）。
2. 分别运行下面两条命令，对比输出里 support / macro 的 feature 列表：

```bash
cargo tree -e features -p rust-i18n-support
cargo tree -e features -p rust-i18n-support --features rust-i18n/load-path
```

**需要观察的现象**：第二条命令的输出里，`rust-i18n-support` 会带上 `codegen` feature，并多出 `serde`、`serde_yaml`、`toml`、`globwalk` 等依赖节点；第一条则没有。

**预期结果**：`load-path` 这一个根 feature，确实「接力」打开了 support 的 `codegen` 及其全部可选依赖。

> 注：具体输出文本取决于本机 Cargo 版本与依赖树，关键看 support 节点是否出现 `feature "codegen"`。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么根 crate 不直接定义一个面向用户的 `codegen` feature，而要用 `load-path` 这个名字？

**参考答案**：因为 `codegen` 是**实现细节**（「是否把解析器编进来」），用户关心的是**能力**（「能否在运行时加载文件」）。用 `load-path` 这个语义化名字，隐藏了「背后要开 codegen」的内部细节，API 更稳定——将来内部重构不需要改用户配置。

**练习 2**：如果用户既不写 `i18n!`、也不开任何 feature，只 `t!`，能编译吗？

**参考答案**：能编译 `t!` 宏本身（它是 macro_rules 转发壳），但会因找不到 `crate::_rust_i18n_t!` 而报错（详见 u3-l1）。feature 与此无关——`t!` 不依赖任何可选 feature。

---

### 4.2 codegen：用 optional 依赖做「编译期独占」

#### 4.2.1 概念说明

`codegen` 是 support crate 的核心 feature，解决的问题是：**翻译文件的解析器（YAML/JSON/TOML/glob）只在编译期被 `i18n!` 用一次，不该进运行时二进制**。如果不加区分地把它们编译进来，用户的二进制会白白多出几百 KB 的解析器代码，还可能引入不必要的运行时依赖。

support crate 的做法是把依赖分成两组：

- **运行时必需**（非可选，永远编译）：`arc-swap`（`AtomicStr` 全局 locale）、`base62` + `siphasher`（`minify_key` 短键）、`triomphe`（`CowStr`）。
- **编译期独占**（可选，仅 `codegen` 开启才编译）：`serde`、`serde_json`、`serde_yaml`、`toml`、`globwalk`、`normpath`、`itertools`。

#### 4.2.2 核心流程

那么「编译期独占」到底是怎么落到实处的？关键在于**谁在编译期、谁在运行期**：

```
┌─────────────────────────────────────────────────────────────┐
│ 你的 app crate（运行期进二进制）                              │
│   依赖 rust-i18n ──► 默认不开 codegen ──► 解析器不进二进制    │
│      │                                                       │
│      └─ build 期：rust-i18n-macro 运行 i18n!                  │
│            macro crate 无条件开了 codegen ──► 在这里解析文件  │
└─────────────────────────────────────────────────────────────┘
```

- `macro` crate 是**过程宏 crate**，它在**编译你的 app 时**执行（build 期），它无条件启用了 support 的 `codegen`：[crates/macro/Cargo.toml:16](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/Cargo.toml#L16)。所以解析器在「编译你的 app」这一刻是存在的，`i18n!` 能读 YAML。
- 但 `macro` crate 本身**不会**被链接进你的最终二进制（过程宏 crate 只在编译期存在）。解析器随之消失。
- 你的 app crate 直接依赖的 support，**默认不开 codegen**，所以运行时二进制里没有解析器。

这就是「编译期独占」的精髓：**解析器跟着 macro 在 build 期出现一次、干完活就消失，运行时二进制里看不到**。

#### 4.2.3 源码精读

先看 support 的 `[features]`，`codegen` 用 `dep:` 语法逐个点名 7 个可选依赖：[crates/support/Cargo.toml:10-19](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/Cargo.toml#L10-L19)。`dep:` 前缀是 Cargo 的明确写法，表示「这里启用的是名为 X 的**可选依赖**，而非同名 feature」，避免歧义。

这些依赖在 `[dependencies]` 里都标了 `optional = true`：[crates/support/Cargo.toml:28-34](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/Cargo.toml#L28-L34)。与之对照，4 个运行时必需依赖没有 `optional`（[crates/support/Cargo.toml:22-25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/Cargo.toml#L22-L25)），永远编译。

再看源码侧的「另一条线」：support 的 `lib.rs` 顶部，**运行时类型模块（atomic_str / backend / cow_str / minify_key）不带任何 cfg 守卫**，始终编译：[crates/support/src/lib.rs:1-11](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L1-L11)。而所有与「加载/解析」相关的东西——`config` 模块、`try_load_locales`、各种 `use`、类型别名——全部被 `#[cfg(feature = "codegen")]` 守卫：[crates/support/src/lib.rs:13-36](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L13-L36)。

这两条线必须**配套**：`Cargo.toml` 的 `dep:` 控制依赖是否编译，`#[cfg]` 控制用到这些依赖的源码是否编译。少了任何一条都会报「找不到 crate」或「多余依赖」。

#### 4.2.4 代码实践

**实践目标**：直观对比「开/不开 codegen」时 support 多带了多少依赖。

**操作步骤**：

1. 在仓库根目录运行：

```bash
cargo tree -p rust-i18n-support
cargo tree -p rust-i18n-support --features codegen
```

**需要观察的现象**：第一条只列出 `arc-swap`、`base62`、`siphasher`、`triomphe` 四个（及它们的传递依赖）；第二条额外多出 `serde`、`serde_json`、`serde_yaml`、`toml`、`globwalk`、`normpath`、`itertools` 整片子树。

**预期结果**：二进制体积差异正是来自这片子树。**待本地验证**（可用 `cargo build` 后比较 `target/` 产物大小，或直接看依赖数量差）。

#### 4.2.5 小练习与答案

**练习 1**：如果只删掉 `Cargo.toml` 里 `codegen` 的 `dep:serde_yaml`，但保留源码里 `#[cfg(feature="codegen")]` 下的 `serde_yaml::from_str` 调用，开 `codegen` 时会怎样？

**参考答案**：开 `codegen` 时，源码参与编译却找不到 `serde_yaml` 这个 crate，编译报「unresolved import / cannot find crate」错误。`dep:` 与 `#[cfg]` 必须一致。

**练习 2**：为什么 `arc-swap` 不能也做成可选？

**参考答案**：`arc-swap` 支撑的是 `AtomicStr`，而 `AtomicStr` 是全局 `CURRENT_LOCALE` 的实现，运行时 `set_locale`/`locale()`/`t!` 都要用。它属于「运行时必需」，必须永远编译。

---

### 4.3 log-miss-tr：编译期注入缺失翻译告警

#### 4.3.1 概念说明

`log-miss-tr` 解决的是「翻译漏了却没人知道」的问题：当 `t!("some.key")` 在后端里**查不到**（miss）时，默认是静默返回原始消息，开发者很难发现「这个键忘配翻译了」。开 `log-miss-tr` 后，每次 miss 都会在**编译期生成的代码**里插入一条 `log` 告警，把缺失的键、值、文件名、行号打到 `log` 的 warn 级别。

这个 feature 有一个**重要前提**（README 明确写了）：它要求**用户自己的项目**依赖 `log` crate。因为生成的代码直接调用 `log::log!(...)`，而 `rust-i18n` 并不替你引入 `log`。

#### 4.3.2 核心流程

`log-miss-tr` 的工作方式是「**编译期分支** + **生成期注入**」：

```
_tr! 解析一次 t! 调用
   │
   ├─ 计算 let logging = Self::log_missing();
   │       ├─ 若开 log-miss-tr：返回 quote!{ log::log!(... missing ...) }
   │       └─ 若关：返回 quote!{} （空）
   │
   └─ 在「未命中」分支里插入 #logging
           ├─ 开：运行时 miss 会触发 log warn
           └─ 关：未命中分支多一句空语句，零开销
```

#### 4.3.3 源码精读

macro crate 的 `log-miss-tr` 是个**空 feature**（仅作开关信号）：[crates/macro/Cargo.toml:28-29](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/Cargo.toml#L28-L29)。它不带任何 `dep:`，因为它不开启依赖，只控制源码生成分支。

真正的逻辑在 `tr.rs` 的 `log_missing()`，它有两个互斥的 cfg 版本：[crates/macro/src/tr.rs:378-388](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L378-L388)。

- 开启版本用 `quote!` 生成一段调用 `log::log!(target: "rust-i18n", log::Level::Warn, "missing: {} => {:?} @ {}:{}", ...)` 的 token 流，带上 `msg_key`、`msg_val`、`file!()`、`line!()`。
- 关闭版本直接返回空 `quote!{}`。

注意：生成的代码引用了 `log::log!` 与 `log::Level::Warn`，但 macro crate 自己**并不依赖 `log`**（可对照 `crates/macro/Cargo.toml` 的 `[dependencies]`，没有 `log`）。所以这些 token 是注入到**用户代码**里的，由用户的 crate 提供的 `log` 来解析。这正是 README 那句「the feature requires the `log` crate」的技术原因。

`log_missing()` 的产物通过一个变量 `logging` 注入到「未命中」分支：[crates/macro/src/tr.rs:432](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L432) 取出，在无参与有参两条生成路径的 `else`（miss）分支里分别用 `#logging` 展开：[crates/macro/src/tr.rs:441](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L441) 与 [crates/macro/src/tr.rs:458](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L458)。

#### 4.3.4 代码实践

**实践目标**：体会 `log-miss-tr` 开启后必须自备 `log`。

**操作步骤**：

1. 新建一个依赖 `rust-i18n` 的 crate，开启 feature：

```toml
[dependencies]
rust-i18n = { path = "../..", features = ["log-miss-tr"] }
```

2. 写一个故意查不到的 `t!("not.exist")`，先**不**加 `log` 依赖，执行 `cargo build`。

**需要观察的现象**：编译报错，提示找不到 `log`（如 `cannot find crate log` 或 `unresolved import log`）。

3. 补上 `log` 依赖与一个简单实现（如 `env_logger`），再次编译并运行：

```toml
log = "0.4"
env_logger = "0.11"
```

```rust
fn main() {
    env_logger::init();
    let _ = t!("not.exist"); // 运行时应打出 warn: missing: not.exist => ...
}
```

**预期结果**：补 `log` 后编译通过；运行时在 stderr/日志里看到 `missing: not.exist => ... @ src/main.rs:N` 的告警。**待本地验证**（具体日志格式与输出流取决于所用 log 后端）。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `log` 放进 macro crate 的依赖不能解决问题？

**参考答案**：因为生成的 `log::log!(...)` 出现在**用户 crate** 的代码里（被 `quote!` 注入），运行时由用户 crate 解析 `log` 路径；macro crate 是过程宏 crate，不进用户二进制，它的依赖对运行时代码不可见。

**练习 2**：关闭 `log-miss-tr` 时，「未命中」分支有性能损失吗？

**参考答案**：几乎没有。`log_missing()` 返回空 `quote!{}`，注入后是空语句；运行时「未命中返回原始消息」的逻辑本来就要执行，告警代码不增加额外开销。

---

### 4.4 load-path 与运行时加载 try_load_locales

#### 4.4.1 概念说明

绝大多数项目用**编译期加载**就够了：`i18n!("locales")` 在 build 期把翻译烤进二进制，运行时零文件 IO（详见 u2-l2）。但有些场景需要在**运行时**动态加载翻译文件——比如翻译文件随插件下发、或按需从磁盘读取。这时就需要 `load-path` feature，它解锁了 `rust_i18n::try_load_locales` 这个**运行时**函数。

⚠️ **撞名陷阱（务必分清）**：项目里存在两个都叫 `load-path`、但完全不同的东西：

| 名称 | 出现位置 | 本质 | 作用 |
| --- | --- | --- | --- |
| **cargo feature `load-path`** | 根 `[features]` | Cargo 编译开关 | 把解析器编进二进制 + 暴露 `try_load_locales` |
| **配置项 `load-path`** | `[package.metadata.i18n]` | `I18nConfig.load_path` 字段 | 仅告诉 `cargo i18n` CLI 去哪找翻译文件（见 u5-l1） |

前者是「能不能运行时加载」，后者是「CLI 工具去哪扫文件」，两者毫无关系，只是恰好同名。例如 `examples/app-metadata/Cargo.toml` 里的 `load-path = "locales"`（[examples/app-metadata/Cargo.toml:12-16](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-metadata/Cargo.toml#L12-L16)）就是**配置项**，不是 feature。

> 另需澄清：名为 `examples/app-load-path` 的示例**并不**演示运行时加载。它用的是编译期 `i18n!("../locales")`，只是路径指向 crate 外部的 `examples/locales/`：[examples/app-load-path/src/main.rs:1-16](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-load-path/src/main.rs#L1-L16)。它的名字容易让人误会，实际作用是「从外部路径做编译期加载」。

#### 4.4.2 核心流程

`load-path` 的接力链（见 4.1.2）最终让 support 的 `codegen` 在**用户二进制**里也打开，于是：

1. 解析器依赖（serde_yaml 等）进入用户二进制。
2. support 里 `#[cfg(feature="codegen")]` 守卫的 `try_load_locales` 参与编译。
3. 根 crate 在 `load-path` 开启时，把它 `pub use` 出去。

`try_load_locales` 的签名是：

```text
try_load_locales(
    locales_path: &str,
    ignore_if: F,                     // 回调：返回 true 的文件跳过
    report_file_lookup_errors: bool,  // true: 出错返回 Err；false: 出错返回空 Ok
) -> Result<BTreeMap<String /*locale*/, BTreeMap<String /*key*/, String /*value*/>>, String>
```

它的内部实现（glob 扫描、按扩展名选解析器、v1/v2 分发、合并、扁平化）与编译期 `i18n!` 用的是**同一份代码**——这正是把它放在 support 的 `codegen` 守卫下、由 `i18n!` 和运行时复用的好处（详见 u2-l2）。

#### 4.4.3 源码精读

根 crate 仅在 `load-path` 开启时才对外暴露 `try_load_locales`，靠一行带 cfg 的 `pub use`：[src/lib.rs:12-13](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L12-L13)。这意味着不开 `load-path` 时，`rust_i18n::try_load_locales` 这个符号根本不存在，调用会直接编译报错。

`try_load_locales` 本身被 `#[cfg(feature = "codegen")]` 守卫在 support 里：[crates/support/src/lib.rs:63-68](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L63-L68) 是函数头与签名。它的函数体走的就是 u2-l2 讲过的那条「glob → parse_file → v1/v2 → merge → flatten」管线（函数体见 [crates/support/src/lib.rs:117-163](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L117-L163)）。与编译期入口 `load_locales`（[crates/support/src/lib.rs:52-60](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L52-L60)）的区别仅在于：`try_load_locales` 返回 `Result` 而非 panic，并多一个 `report_file_lookup_errors` 参数控制错误策略。

README 对该 feature 的一句话定义：[README.md:25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L25)——「`load-path` feature for runtime locale file loading via `try_load_locales`. By default, YAML/TOML parsing deps are compile-time only and not included in the binary.」

#### 4.4.4 代码实践

**实践目标**：启用 `load-path`，在运行时加载一个 locale 文件并打印结果（仓库现有示例未覆盖此用法，需自行编写）。

**操作步骤**：

1. 新建一个 crate，开启 feature 并准备一个翻译文件：

```toml
# Cargo.toml
[dependencies]
rust-i18n = { path = "../..", features = ["load-path"] }
```

```yaml
# locales/en.yml
_version: 1
hello: "Hello at runtime"
```

2. 写 `main.rs`，运行时调用 `try_load_locales`：

```rust
fn main() {
    // 注意：第三个参数 false 表示「目录不存在等错误时返回空表，而不是 Err」
    let trs = rust_i18n::try_load_locales("locales", |_| false, false)
        .expect("load failed");
    for (locale, kvs) in &trs {
        println!("locale={locale}");
        for (k, v) in kvs {
            println!("  {k} => {v}");
        }
    }
}
```

**需要观察的现象**：程序运行时（而非编译期）读取 `locales/en.yml`，打印出 `locale=en` 与 `hello => Hello at runtime`。

**预期结果**：成功打印翻译表。把 `locales/en.yml` 改名或删除后重新运行，应得到空表（因第三参数为 `false`）；若把第三参数改 `true`，则返回 `Err`。

**对比编译期加载**：

- 编译期加载（默认）：改 yml 后必须 `cargo build` 才生效；二进制不含解析器，体积更小、依赖更少。
- 运行时加载（`load-path`）：改 yml 后无需重编译，重启程序即生效；但二进制多带 serde_yaml/toml/globwalk 等，体积与依赖都更大。

**预期对比结论**：用 `cargo build --release` 后比较「开 `load-path`」与「不开」两个版本的二进制大小，开启版本明显更大。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`try_load_locales` 的第三个参数 `report_file_lookup_errors` 在什么场景分别用 `true` / `false`？

**参考答案**：`true` 适合「翻译文件是必需品，丢了就应失败」（如启动期强校验），目录不存在会返回 `Err`；`false` 适合「翻译是可选增强，缺失就静默用空表」（如插件式按需加载）。编译期入口 `load_locales` 对应的是「panic」策略（见 [crates/support/src/lib.rs:57-60](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L57-L60)）。

**练习 2**：为什么 `try_load_locales` 能与 `i18n!` 复用同一套解析逻辑？

**参考答案**：两者都调用 support 里被 `codegen` 守卫的 `parse_file` / `merge_value` / `flatten_keys` 等函数。`i18n!` 在编译期调用，`try_load_locales` 在运行期调用，但走的代码路径相同，因此翻译结果一致。这是把加载逻辑集中在 support、用 feature 控制可见性的设计收益。

---

## 5. 综合实践

把本讲的 feature 机制串起来，完成一个小任务：

**任务**：做一个「双模式翻译加载器」demo，验证 feature 开关如何改变二进制的依赖与行为。

1. 建一个 crate，准备 `locales/en.yml`、`locales/zh-CN.yml`。
2. **模式 A（编译期，默认）**：用 `i18n!("locales")` + `t!`，不开任何 feature。运行 `cargo tree`，确认 support **不带** codegen、不带 serde_yaml。
3. **模式 B（运行时）**：开启 `features = ["load-path"]`，改用 `try_load_locales` 在 `main` 里加载并手动查表打印（参考 4.4.4 的代码）。再跑 `cargo tree`，确认 support **带** codegen 与 serde_yaml。
4. **加分项**：再开 `features = ["log-miss-tr"]`，补上 `log` 依赖，故意查一个不存在的键，观察运行时是否打出 `missing:` 告警。

**验收要点**：

- 能用 `cargo tree` 的差异「证明」feature 确实改变了依赖图。
- 能说清「模式 A 改 yml 要重编译、模式 B 不用」的原因。
- 能解释为什么开 `log-miss-tr` 后必须自己加 `log`。

体积与日志的具体数值**待本地验证**。

## 6. 本讲小结

- rust-i18n 的 feature 分两层：面向用户的有 `load-path`、`log-miss-tr`（根 crate 定义并转发到子 crate）；`codegen` 是 support 内部 feature，用户一般通过 `load-path` 间接打开。
- `codegen` 用 `optional = true` + `dep:` 把 7 个解析器依赖（serde/serde_json/serde_yaml/toml/globwalk/normpath/itertools）做成「编译期独占」，配合 `#[cfg(feature="codegen")]` 守卫源码；macro crate 无条件开 `codegen`，使 `i18n!` 在编译期能读文件，而这些解析器默认不进用户运行时二进制。
- `log-miss-tr` 在编译期给每次 `t!` 未命中处注入 `log::log!(...)` 告警；但 macro crate 不依赖 `log`，生成代码注入用户 crate，故**用户必须自备 `log`**。
- `load-path` 接力打开 `codegen`，让 `try_load_locales`（运行时加载）在根 crate 被条件 `pub use` 暴露，实现「改 yml 无需重编译」。
- **撞名陷阱**：cargo feature `load-path` 与 `[package.metadata.i18n]` 配置项 `load-path` 是两回事；`examples/app-load-path` 演示的是编译期外部路径加载，不是运行时 `try_load_locales`。
- feature 的两条线（`dep:` 控依赖、`#[cfg]` 控源码）必须配套，缺一即报错。

## 7. 下一步学习建议

- 若想看 `try_load_locales` 内部的完整解析流程（glob、v1/v2、合并、扁平化），回到 **u2-l2 编译期加载与解析本地化文件** 与 **u2-l3 多文件合并与键扁平化**，那里的代码正是 `load-path` 在运行时复用的同一条管线。
- 若想理解开 `load-path` 后 `try_load_locales` 返回的扁平表如何被 `t!` 查找与回退，继续 **u3 系列（运行时翻译机制）**，尤其是 u3-l4 翻译回退机制。
- 想做更复杂的「按需/远程加载翻译」，可结合 **u4 系列（Backend trait 与自定义后端）**：把 `try_load_locales` 的结果灌进自定义 `Backend`，再用 `i18n!(backend = ...)` 接入。
