# 工作区结构：四个 crate 如何协作

## 1. 本讲目标

在上一讲（u1-l1）里，我们知道了 rust-i18n 的整体定位：它在**编译期**把翻译文件代码生成进二进制，运行时用全局 `t!` 宏取文本，而 `t!` 只是一个转发壳。本讲我们要回答一个更结构化的问题：

> 这套「编译期生成 + 运行时查找」的能力，在源码里到底是被**拆成了几个零件**，这些零件之间**谁依赖谁**，用户又只通过哪一个 crate 就能用上全部功能？

学完本讲，你应当能够：

1. 看懂根 `Cargo.toml` 的 `[workspace] members` 清单，说出 rust-i18n 为什么用「根 package 同时兼任 workspace 根」这种结构。
2. 准确区分 `rust-i18n`（门面）、`rust-i18n-support`（运行时类型）、`rust-i18n-macro`（过程宏）、`rust-i18n-extract` / `rust-i18n-cli`（提取器）五个 crate 各自的职责。
3. 画出这五个 crate 之间的依赖关系图，并解释为什么 `extract` 和 `cli` 不被根 crate 依赖。
4. 理解 `src/lib.rs` 如何通过 `pub use` 把 support / macro 的符号对外暴露，形成「门面模式（Facade）」。
5. 说清楚为什么 `rust-i18n-macro` 必须在 `Cargo.toml` 里设置 `proc-macro = true`。

## 2. 前置知识

本讲假设你已经读过 u1-l1，了解下面这些上一讲建立的术语，这里只做最简短回顾，不展开：

- **i18n / l10n / locale**：国际化、本地化、语言区域标识（如 `en`、`zh-CN`）。
- **编译期代码生成（codegen）**：在 `cargo build` 阶段，而不是程序运行阶段，就把翻译数据编译进二进制。
- **`t!` 是转发壳**：根 crate 的 `t!` 宏并不真正查找文本，它转发到 `i18n!` 在编译期生成的内部宏 `crate::_rust_i18n_t!`。

此外，本讲会用到几个 Cargo / Rust 的基础概念，初学者不熟悉的话先记下面这几条「直觉」：

| 术语 | 直觉解释 |
| --- | --- |
| **package（包）** | 一个 `Cargo.toml` 描述的发布单元，对应 crates.io 上的一个 crate。 |
| **crate** | 一次编译产生的产物（一个库或一个二进制），通常一个 package 产生一个 lib crate。 |
| **workspace（工作区）** | 多个 package 共享同一套 `Cargo.lock`、同一个 `target/` 输出目录的组织方式，方便一起开发。 |
| **`pub use` 再导出** | 把别的 crate 里的符号「借」过来，用自己的名字对外提供，这样用户只需要依赖你一个 crate。 |
| **门面模式（Facade）** | 提供一个统一入口，把背后多个子系统的能力打包暴露给外部，外部不需要知道子系统细节。 |
| **过程宏（proc-macro）** | 一种特殊的 crate，在编译期接收 Rust 源码 token 流、产出新的 token 流，`i18n!` 就是它。 |

记牢「门面」这个词，它是本讲理解根 crate 行为的钥匙。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml`（根） | 同时是根 package `rust-i18n` 和整个 workspace 的清单：声明成员、共享依赖版本、根 crate 自己的依赖与 feature。 |
| `src/lib.rs` | 根 crate 的库入口：用 `pub use` 再导出 support/macro 的符号，并定义 `set_locale`/`locale` 等运行时函数与 `t!`/`tkv!`/`available_locales!` 转发宏。 |
| `crates/support/src/lib.rs` | `rust-i18n-support` 的入口：声明运行时类型（`Backend`、`SimpleBackend`、`AtomicStr` 等），并通过 `codegen` feature 条件编译进文件加载/解析逻辑。 |
| `crates/macro/src/lib.rs` | `rust-i18n-macro` 的入口：定义 `i18n`、`_tr`、`_minify_key` 三个过程宏入口。 |
| `crates/*/Cargo.toml` | 四个子 crate 各自的依赖声明，是画依赖关系图的直接依据。 |

## 4. 核心概念与源码讲解

### 4.1 Cargo workspace 与 crate 成员清单

#### 4.1.1 概念说明

一个项目变大以后，把所有代码塞进一个 crate 会有几个麻烦：编译变慢、职责混在一起、不同部分想用不同的 feature。Cargo 的 **workspace（工作区）** 就是为解决这个问题准备的——它让你把多个 package 放在一起，**共享一份 `Cargo.lock` 和一个 `target/` 目录**，但又各自保持独立的 `Cargo.toml` 和发布身份。

rust-i18n 采用了一种很常见的写法：**根 package 本身就是 workspace 根**。也就是说，最外层那个 `Cargo.toml` 同时承担两个角色：

1. 它声明了一个名叫 `rust-i18n` 的 package（即对外发布的那个库）。
2. 它又用 `[workspace]` 段把几个子 crate 和示例纳管进来。

这样做的好处是：用户只要 `cargo add rust-i18n`，就只拿到这一个 package，背后的子 crate 是被它「私下」依赖和打包好的；而项目开发者在一个目录里就能一起构建、测试所有 crate。

#### 4.1.2 核心流程

理解 workspace 结构，可以按下面这个顺序读 `Cargo.toml`：

```text
1. [package]            → 这是「根 package」，名字 rust-i18n，就是用户依赖的那个
2. [workspace.dependencies] → 全 workspace 共享的依赖版本表，子 crate 用 xxx.workspace = true 引用
3. [dependencies]       → 根 package 自己运行时需要哪些 crate
4. [features]           → 根 package 对外暴露的 feature 开关
5. [workspace] members  → workspace 纳管了哪些子 package（子 crate + 示例）
```

关键点：`members` 里列出的每一个目录，都是一个**独立可单独构建的 package**；它们和根 package 是平级的「workspace 成员」，并不是根 package 的子目录代码。

#### 4.1.3 源码精读

先看根 `Cargo.toml` 末尾的 workspace 成员清单（注意：根 package 自身不在 `members` 里，因为它就是 workspace 根，自动包含）：

[Cargo.toml:75-86](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L75-L86) — 声明 workspace 纳管 4 个 `crates/` 子 crate 和 5 个 `examples/` 示例 package。

```toml
[workspace]
members = [
    "crates/cli",
    "crates/extract",
    "crates/support",
    "crates/macro",
    "examples/app-egui",
    "examples/app-load-path",
    "examples/app-metadata",
    "examples/app-minify-key",
    "examples/foo",
]
```

这里有三个值得注意的细节：

- `crates/` 下正好是 **support、macro、extract、cli** 四个 crate，这正是本讲标题说的「四个 crate」。
- `examples/` 下注册为成员的有 `app-egui`、`app-load-path`、`app-metadata`、`app-minify-key`、`foo` 五个；它们和子 crate 一样共享 workspace，方便一起 `cargo build`。
- 你可能注意到主示例 `examples/app` **不在** `members` 里——它是通过根 `Cargo.toml` 里的 `[[example]]` 配置（`name = "app"`）作为根 package 的内置 example 来构建的，走的是另一条机制，下一讲 u1-l3 会用到它。

再看共享依赖版本表。这一段不是依赖声明本身，而是一张「版本字典」，让所有 workspace 成员引用同一版本，避免版本漂移：

[Cargo.toml:21-49](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L21-L49) — workspace 级共享依赖，子 crate 用 `xxx.workspace = true` 复用。

```toml
[workspace.dependencies]
# ... 省略大部分 ...
rust-i18n = { path = "." }
rust-i18n-extract = { path = "./crates/extract", version = "4.1.0" }
rust-i18n-macro = { path = "./crates/macro", version = "4.1.0" }
rust-i18n-support = { path = "./crates/support", version = "4.1.0" }
```

注意这里每个子 crate 都同时写了 `path`（本地开发用）和 `version`（发布到 crates.io 后用）。这样在 workspace 内开发时走 `path` 本地联动，发布后用户走 `version` 从 crates.io 拉。`rust-i18n = { path = "." }` 是根 package 自引用，供子 crate（比如 macro 的 dev-dependency）反过来依赖根 crate。

最后看根 package 自身如何发布——它在 `[package]` 段排除了 `crates` 和 `tests` 目录：

[Cargo.toml:7](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L7) — `exclude = ["crates", "tests"]`，发布 `rust-i18n` 时不打包子 crate 源码（它们各自独立发布到 crates.io）。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认 workspace 的成员构成，理解「根 package 即 workspace 根」。
2. **操作步骤**：
   - 打开根 `Cargo.toml`，找到 `[workspace] members`。
   - 数一数成员数量，把 `crates/` 成员和 `examples/` 成员分开记下。
   - 在项目根目录运行 `cargo metadata --no-deps --format-version 1`（这是只读命令，只读不改），在输出的 JSON 里找到 `"packages"` 数组，对比 `members` 是否一一对应。
3. **需要观察的现象**：`cargo metadata` 输出的 packages 数量应该 = workspace 成员数 + 根 package 自己。
4. **预期结果**：你能列出 4 个 `crates/` crate（support、macro、extract、cli）和 5 个 `examples/` package；根 `rust-i18n` 作为第 10 个 package 单独出现。
5. 若本地没有 cargo 环境无法运行，标注「待本地验证」，仅靠阅读 `Cargo.toml` 也能完成成员清点。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `members` 里没有列出根 `rust-i18n` 本身？

**参考答案**：因为根 `Cargo.toml` 既是 `[package]` 又是 `[workspace]`，根 package 自动就是 workspace 的隐含成员；显式再列会重复。Workspace 根 package 不需要、也不能在 `members` 里再次声明自己。

**练习 2**：`examples/app` 没有出现在 `members` 里，它是怎么被 cargo 知道的？

**参考答案**：它通过根 `Cargo.toml` 的 `[[example]] name = "app"` 段被注册为根 package 的内置 example，而不是一个独立的 workspace 成员 package。

---

### 4.2 四个子 crate 的职责与依赖关系

#### 4.2.1 概念说明

rust-i18n 把能力拆成五个 crate（一个根 + 四个子），并不是随便切的，而是按「**什么阶段用到**」来分的：

| crate | 阶段 | 一句话职责 |
| --- | --- | --- |
| `rust-i18n`（根） | 编译期 + 运行时 | **门面**：用户唯一需要依赖的 crate，再导出后面几个 crate 的能力。 |
| `rust-i18n-support` | 运行时 | 提供**运行时类型**：`Backend` trait、`SimpleBackend`、`AtomicStr`（全局 locale 的线程安全载体）、`MinifyKey`、`CowStr` 等。 |
| `rust-i18n-macro` | 编译期 | 提供**过程宏**：`i18n!`（初始化并代码生成）、`_tr!`、`_minify_key!`。 |
| `rust-i18n-extract` | 开发期（CLI 用） | 提供**提取器库**：扫描源码里的 `t!` 调用，收集待翻译文案。 |
| `rust-i18n-cli` | 开发期 | 把 extract 包装成 `cargo i18n` **命令行工具**，生成 `TODO.yml`。 |

一个关键认知：**`extract` 和 `cli` 是「开发期工具」，它们不会被用户程序的二进制依赖**。用户写应用时只用 `rust-i18n`（根），根只依赖 `support` + `macro`。`extract`/`cli` 是给翻译流程用的独立二进制，和你的应用二进制无关。

为什么要把 support 和 macro 分开？因为**过程宏 crate 和普通库 crate 的编译模型完全不同**（见 4.2.4 的实践）。而且 support 里的运行时类型既被「过程宏生成的代码」引用，也被「用户代码」引用——它必须是一个独立的普通库，两边才能共享同一份类型定义。

#### 4.2.2 核心流程

把五个 crate 的依赖关系画成图（箭头表示「依赖于」）：

```text
                       ┌──────────────────────────┐
                       │   rust-i18n  （门面）     │  ← 用户只依赖它
                       │   src/lib.rs             │
                       └────────────┬─────────────┘
                  ┌─────────────────┼──────────────────┐
                  │ (pub use)       │ (pub use)
                  ▼                 ▼
        ┌─────────────────┐   ┌──────────────────────┐
        │ rust-i18n-      │   │ rust-i18n-macro       │
        │ support         │◄──┤  (proc-macro = true)  │
        │ 运行时类型       │   │  i18n! / _tr! /       │
        └─────────────────┘   │  _minify_key!         │
              ▲               └──────────────────────┘
              │                       │
              │ (codegen feature)     │ 生成代码里引用 rust_i18n::
              │                       │  SimpleBackend / BackendExt ...
              │                       ▼
              │               （生成的代码引用根 crate的再导出符号）
              │
   ┌──────────┴───────────┐
   │ rust-i18n-extract    │   rust-i18n-cli ──► rust-i18n-extract
   │ 扫描源码取 t!        │   (cargo i18n 二进制)
   └──────────────────────┘
```

读图要点：

1. `rust-i18n`（根）→ `rust-i18n-support`、`rust-i18n-macro`。
2. `rust-i18n-macro` → `rust-i18n-support`（带 `codegen` feature），因为 `i18n!` 宏在编译期需要调用 support 里的文件加载/解析函数（`load_locales` 等）。
3. `rust-i18n-extract` → `rust-i18n-support`（带 `codegen` feature）。
4. `rust-i18n-cli` → `rust-i18n-extract` + `rust-i18n-support`。
5. **没有**任何箭头从根 crate 指向 `extract` 或 `cli`——它们是工具，不进应用二进制。

还有一条「看似循环」的边：`rust-i18n-macro` 的 `dev-dependencies` 里写了 `rust-i18n`（见 `crates/macro/Cargo.toml` 第 22-23 行）。这只在 macro crate 自己的测试/文档示例里用到，**不构成真正的循环依赖**，dev-dependency 不会进入发布产物。

#### 4.2.3 源码精读

**根 crate 的运行时依赖**——只有三项，注意 `smallvec` 是纯第三方库：

[Cargo.toml:51-54](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L51-L54) — 根 package 运行时只依赖 support、macro 和 smallvec。

```toml
[dependencies]
rust-i18n-support.workspace = true
rust-i18n-macro.workspace = true
smallvec.workspace = true
```

**根 crate 的 feature 转发**——根自己不实现 feature，而是把开关下发给子 crate：

[Cargo.toml:56-58](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L56-L58) — `load-path` 转发到 support 的 `codegen`，`log-miss-tr` 转发到 macro。

```toml
[features]
log-miss-tr = ["rust-i18n-macro/log-miss-tr"]
load-path = ["rust-i18n-support/codegen"]
```

**macro crate 依赖 support（带 codegen）**——过程宏在编译期要用 support 的解析能力：

[crates/macro/Cargo.toml:12-20](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/Cargo.toml#L12-L20) — macro 依赖 `syn`/`quote`/`proc-macro2` 做语法解析，并依赖 support 的 codegen 能力加载翻译文件。

```toml
[dependencies]
glob.workspace = true
proc-macro2.workspace = true
quote.workspace = true
rust-i18n-support = { workspace = true, features = ["codegen"] }
serde.workspace = true
serde_json.workspace = true
serde_yaml.workspace = true
syn.workspace = true
```

**macro crate 必须标记为过程宏**——这是本讲实践任务的重点：

[crates/macro/Cargo.toml:25-26](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/Cargo.toml#L25-L26) — `[lib] proc-macro = true` 告诉 cargo：这是一个过程宏 crate。

```toml
[lib]
proc-macro = true
```

对应的三个过程宏入口函数，每个都标注了 `#[proc_macro]`：

[crates/macro/src/lib.rs:248-249](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L248-L249) — `i18n!` 的过程宏入口，接收 token 流、返回生成代码。

```rust
#[proc_macro]
pub fn i18n(input: proc_macro::TokenStream) -> proc_macro::TokenStream {
    let args = parse_macro_input!(input as Args);
    // ... 加载翻译文件 → generate_code → 返回 token 流
}
```

[crates/macro/src/lib.rs:448-452](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L448-L452) — `_tr!` 过程宏入口（`t!` 最终转发的目标之一）。

**support crate 的「双面性」**——它的运行时依赖很轻，重的解析器（serde_yaml/toml/globwalk 等）都做成 `optional`，只有开了 `codegen` feature 才编进来：

[crates/support/Cargo.toml:21-34](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/Cargo.toml#L21-L34) — 运行时只带 4 个轻依赖，解析器全部 optional。

```toml
[dependencies]
arc-swap.workspace = true
base62.workspace = true
siphasher.workspace = true
triomphe.workspace = true

# codegen-only deps
serde = { workspace = true, optional = true }
serde_json = { workspace = true, optional = true }
serde_yaml = { workspace = true, optional = true }
toml = { workspace = true, optional = true }
globwalk = { workspace = true, optional = true }
normpath = { workspace = true, optional = true }
itertools = { workspace = true, optional = true }
```

这就是为什么用户应用默认**零文件 IO、解析器不进二进制**：根 crate 默认不开 support 的 `codegen`，那些解析器就不会被链接进去。（`codegen` feature 的细节在 u5-l3 讲。）

**extract 与 cli 的依赖**——同样依赖 support 的 codegen，但彼此独立：

[crates/extract/Cargo.toml:12-23](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/Cargo.toml#L12-L23) — 提取器库依赖 support(codegen) + syn/ignore/regex 等，负责扫描源码。

[crates/cli/Cargo.toml:10-14](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/Cargo.toml#L10-L14) — CLI 只依赖 support(codegen) + extract + clap，把提取器包成命令行。

```toml
[dependencies]
anyhow.workspace = true
clap.workspace = true
rust-i18n-support = { workspace = true, features = ["codegen"] }
rust-i18n-extract.workspace = true
```

#### 4.2.4 代码实践

1. **实践目标**：亲手画出依赖关系图，并从源码角度论证 `proc-macro = true` 的必要性。
2. **操作步骤**：
   - 分别打开 `crates/support/Cargo.toml`、`crates/macro/Cargo.toml`、`crates/extract/Cargo.toml`、`crates/cli/Cargo.toml` 以及根 `Cargo.toml`。
   - 为每个 crate 列出它的 `[dependencies]` 里出现的「rust-i18n-*」成员，画出 4.2.2 的依赖图。
   - 单独打开 `crates/macro/Cargo.toml`，定位 `[lib] proc-macro = true`（第 25-26 行）。
3. **需要观察的现象 / 思考**：
   - 根 crate 的 `[dependencies]` 里**没有** `rust-i18n-extract` 和 `rust-i18n-cli`——这说明你的应用二进制不会把提取器链进去。
   - `rust-i18n-macro` 的 `[lib]` 里有一行 `proc-macro = true`。
4. **预期结果（依赖图）**：
   ```
   rust-i18n ──► rust-i18n-support
   rust-i18n ──► rust-i18n-macro ──► rust-i18n-support(codegen)
   rust-i18n-extract ──► rust-i18n-support(codegen)
   rust-i18n-cli ──► rust-i18n-extract, rust-i18n-support(codegen)
   ```
5. **关于 `proc-macro = true` 的说明**（这是本题核心）：`i18n!`、`_tr!`、`_minify_key!` 必须在**编译用户的 crate 时**执行，而不是在「程序运行时」执行。Rust 规定，凡是想提供「编译期介入」能力的库，必须是一个**专门的「过程宏 crate」**，它由 cargo 在**编译宿主（host）上**单独编译成一段动态库，再被 rustc 调用来处理宏调用。`[lib] proc-macro = true` 就是把 `rust-i18n-macro` 标记成这种特殊 crate。如果不设它，`#[proc_macro]` 属性会直接编译报错，而且这个 crate 就会被当成普通库，无法在任何宏位置被调用——`i18n!` 也就无从工作。此外，过程宏 crate 还有一条硬约束：**它不能导出除过程宏以外的任何普通 item**，这也是为什么要把它和运行时类型（support）拆开的根本原因。
6. 若想验证，可在 `crates/macro/Cargo.toml` 里**临时**注释掉 `proc-macro = true` 再 `cargo build -p rust-i18n-macro`，观察编译器报错——但这属于「修改源码」的探索，做完请还原，正式环境不要留下改动。无 cargo 环境则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：用户的应用程序二进制里，会包含 `rust-i18n-extract` 的代码吗？为什么？

**参考答案**：不会。根 `rust-i18n` 的 `[dependencies]` 里只有 `rust-i18n-support`、`rust-i18n-macro`、`smallvec`，没有 `rust-i18n-extract`。extract 只被 `rust-i18n-cli` 依赖，是一个独立的开发期工具，不会链接进用户应用。

**练习 2**：`rust-i18n-macro` 和 `rust-i18n-support` 为什么不能合并成一个 crate？

**参考答案**：因为过程宏 crate 有两条硬约束——必须标记 `proc-macro = true`，且不能导出除过程宏以外的普通 item。而 `Backend`、`SimpleBackend`、`AtomicStr` 这些是普通 struct/trait，需要被「过程宏生成的代码」和「用户代码」同时当作普通类型引用。把它们放进过程宏 crate 会违反约束，所以必须拆成一个普通库（support）+ 一个过程宏 crate（macro）。

**练习 3**：`rust-i18n-macro` 的 `Cargo.toml` 里 `dev-dependencies` 写了 `rust-i18n`，这会造成循环依赖吗？

**参考答案**：不会造成发布产物的循环。dev-dependency 只在该 crate 自己的测试/示例编译时启用，不进入正式的依赖图，也不进入任何依赖它的下游产物。它只是为了 macro crate 自己写文档示例/测试时能用到 `rust_i18n::t!`。

---

### 4.3 根 crate 的门面模式与 re-export

#### 4.3.1 概念说明

有了上一节那张依赖图，一个新问题出现了：用户的代码里写的是 `rust_i18n::t!`、`rust_i18n::Backend`、`rust_i18n::SimpleBackend`，但 `t!` 其实来自 macro crate，`Backend` 其实来自 support crate。用户为什么不用分别依赖 `rust-i18n-macro` 和 `rust-i18n-support`？

这就是 **门面模式（Facade）** + Rust 的 **`pub use` 再导出** 联手的效果。根 crate `src/lib.rs` 用几行 `pub use`，把 macro 和 support 里的关键符号「借」到自己名下，于是用户只依赖 `rust-i18n` 一个 crate，就能用 `rust_i18n::` 前缀访问到全部能力。这带来三个好处：

1. **用户依赖最小化**：只 `cargo add rust-i18n`，不用手动拼一堆依赖。
2. **屏蔽内部结构**：用户不需要知道有 macro/support 之分。
3. **生成代码可以「指名道姓」**：`i18n!` 生成的代码会写 `rust_i18n::SimpleBackend::new()`、`rust_i18n::_tr!(...)` 这样的全路径，正是因为这些符号被再导出到了根 crate。

#### 4.3.2 核心流程

根 crate `src/lib.rs` 的对外暴露分三类，对应三种不同的「可见性策略」：

```text
第 1 类：过程宏符号，标记 #[doc(hidden)]
   pub use rust_i18n_macro::{ i18n, _tr, _minify_key }
   ↑ i18n 是用户要直接写的（i18n!()），但 _tr/_minify_key 是给生成代码用的，
     所以用 #[doc(hidden)] 隐藏文档，避免出现在 API 文档里误导用户。

第 2 类：运行时类型，完全公开（pub use 不加 doc(hidden)）
   pub use rust_i18n_support::{ Backend, BackendExt, SimpleBackend, AtomicStr,
       CowStr, MinifyKey, DEFAULT_MINIFY_KEY*, ... }
   ↑ 这些是用户实现「自定义后端」「读取 locale」时要直接用的公开 API。

第 3 类：按 feature 条件再导出
   #[cfg(feature = "load-path")]
   pub use rust_i18n_support::try_load_locales;
   ↑ 只有开了 load-path feature 才暴露运行时加载函数。
```

理解这条线的关键是：**生成代码与用户代码共用同一组符号名**。`i18n!` 宏在 `generate_code` 里写的是 `rust_i18n::SimpleBackend::new()`（见 `crates/macro/src/lib.rs` 第 291 行），它之所以能这么写，就是因为根 crate 再导出了 `SimpleBackend`。这是一个「自洽」的设计：macro 生成代码 → 代码引用根 crate符号 → 根 crate 再导出 support/macro 符号。

#### 4.3.3 源码精读

看 `src/lib.rs` 开头的再导出三段：

[src/lib.rs:5-6](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L5-L6) — 再导出三个过程宏，用 `#[doc(hidden)]` 标注，因为 `_tr`/`_minify_key` 是给生成代码用的内部宏。

```rust
#[doc(hidden)]
pub use rust_i18n_macro::{_minify_key, _tr, i18n};
```

[src/lib.rs:7-11](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L7-L11) — 再导出 support 的运行时类型与常量，不加 `doc(hidden)`，是面向用户的公开 API。

```rust
pub use rust_i18n_support::{
    AtomicStr, Backend, BackendExt, CowStr, MinifyKey, SimpleBackend,
    DEFAULT_MINIFY_KEY, DEFAULT_MINIFY_KEY_LEN, DEFAULT_MINIFY_KEY_PREFIX,
    DEFAULT_MINIFY_KEY_THRESH,
};
```

[src/lib.rs:12-13](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L12-L13) — 条件再导出：只有启用 `load-path` feature 时才暴露 `try_load_locales`。

```rust
#[cfg(feature = "load-path")]
pub use rust_i18n_support::try_load_locales;
```

再验证「生成代码引用根 crate 符号」这一点——在 macro 的 `generate_code` 里，生成的代码用的是 `rust_i18n::SimpleBackend`：

[crates/macro/src/lib.rs:290-296](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L290-L296) — 生成代码里写死 `rust_i18n::SimpleBackend::new()`，依赖根 crate 的再导出。

```rust
let all_translations = quote! {
    let mut backend  = rust_i18n::SimpleBackend::new();

    #(
        backend.add_translations(#all_translations);
    )*
};
```

同样，生成的 `__rust_i18n_t!` 内部宏会调用 `rust_i18n::_tr!(...)`：

[crates/macro/src/lib.rs:411-417](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L411-L417) — 生成代码引用 `rust_i18n::_tr!`，这个 `_tr` 正是上面第 6 行再导出的过程宏。

```rust
macro_rules! __rust_i18n_t {
    ($($all_tokens:tt)*) => {
        rust_i18n::_tr!($($all_tokens)*, _minify_key = #minify_key, ...)
    }
}
```

最后看 support crate 自己的入口，确认这些符号的「原始出生地」：

[crates/support/src/lib.rs:1-11](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L1-L11) — support 声明子模块并 `pub use` 出 `Backend`、`SimpleBackend`、`AtomicStr` 等，这些就是根 crate 再导出的来源。

```rust
mod atomic_str;
mod backend;
mod cow_str;
mod minify_key;
pub use atomic_str::AtomicStr;
pub use backend::{Backend, BackendExt, CombinedBackend, SimpleBackend};
pub use cow_str::CowStr;
pub use minify_key::{ minify_key, MinifyKey, DEFAULT_MINIFY_KEY, ... };
```

把这三段连起来，就形成了完整的「符号旅行」：

```text
support 的 backend.rs 定义 Backend/SimpleBackend
   → support/src/lib.rs:6  pub use（在 support crate 内暴露）
   → 根 src/lib.rs:7-11    pub use（再导出到 rust_i18n 名下）
   → 用户写 rust_i18n::Backend；生成代码写 rust_i18n::SimpleBackend::new()
```

#### 4.3.4 代码实践

1. **实践目标**：跟踪一个符号从「定义」到「用户可见」的完整再导出链路。
2. **操作步骤**：
   - 选定符号 `Backend`（trait）。
   - 第一步：在 `crates/support/src/backend.rs` 找到 `pub trait Backend` 的定义（这是源头）。
   - 第二步：在 `crates/support/src/lib.rs` 第 6 行确认 `pub use backend::{Backend, ...}`。
   - 第三步：在 `src/lib.rs` 第 7-11 行确认 `pub use rust_i18n_support::{ Backend, ... }`。
   - 第四步：得出结论——用户 `use rust_i18n::Backend;` 等价于一路追到 `support::backend::Backend`。
3. **需要观察的现象**：同一个 `Backend`，在三个文件里以「定义 → crate 内 pub use → 跨 crate pub use」三种姿态出现。
4. **预期结果**：你能画出 4.3.3 末尾那张「符号旅行」图，并解释为什么去掉 `src/lib.rs` 第 7-11 行的 `pub use` 后，用户代码 `rust_i18n::Backend` 会编译失败。
5. 这一实践纯源码阅读，不需要运行；若想验证，可在本地新建一个依赖 `rust-i18n` 的小 crate，写 `use rust_i18n::Backend;` 看 cargo 能否解析（标注「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_tr` 和 `_minify_key` 的再导出要加 `#[doc(hidden)]`，而 `Backend` 不用加？

**参考答案**：`_tr!` 和 `_minify_key!` 是给 `i18n!` 生成的内部代码调用的实现细节，不希望用户直接使用，加 `#[doc(hidden)]` 让它们不出现在 `docs.rs` 的 API 文档里。`Backend` 则是面向用户的公开 API（实现自定义后端要用），所以正常导出、出现在文档里。注意 `i18n` 虽然也在这行 `#[doc(hidden)]` 下，但它是用户要直接写的宏入口，文档里另有专门的宏说明页。

**练习 2**：如果根 crate 不再导出 `SimpleBackend`，`i18n!` 生成的代码 `rust_i18n::SimpleBackend::new()` 会怎样？

**参考答案**：会编译失败，提示找不到 `rust_i18n::SimpleBackend`。因为生成代码写死了 `rust_i18n::SimpleBackend` 这个全路径，它完全依赖根 crate 第 7-11 行的 `pub use` 才能解析到 `support::SimpleBackend`。这正体现了门面模式的「契约」：根 crate 必须再导出生成代码会用到的每一个符号。

**练习 3**：`try_load_locales` 为什么用 `#[cfg(feature = "load-path")]` 包起来，而不是无条件 `pub use`？

**参考答案**：`try_load_locales` 会在运行时读取、解析翻译文件，它依赖 support 的 `codegen` feature 里那一堆可选解析器（serde_yaml/toml/globwalk…）。默认情况下 rust-i18n 是「编译期 codegen、运行时零解析器」，所以这个函数默认不暴露；只有用户显式开启 `load-path` feature（它会连带开启 support 的 `codegen`），这个函数才有意义、才能编译，于是用 cfg 条件再导出。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「结构全景」任务：

**任务**：假设你要向团队新人介绍 rust-i18n 的工程结构，请产出一份「一页纸架构说明」，包含以下内容。

1. **成员清单**：列出 workspace 的全部成员（4 个 crate + 5 个 examples），标注每个 crate 的「中文职责」和「属于哪个阶段（编译期 / 运行时 / 开发期工具）」。
2. **依赖关系图**：画出 4.2.2 那张依赖图（可以手绘或用文字箭头），并在图上用不同标记区分：
   - 哪些边是「根 crate 运行时依赖」；
   - 哪些边带 `codegen` feature；
   - 哪些 crate 不被根 crate 依赖（即不进用户二进制）。
3. **符号追踪**：任选一个公开符号（建议 `SimpleBackend`），画出它从定义（`crates/support/src/backend.rs`）→ support 内 `pub use`（`crates/support/src/lib.rs`）→ 根 crate `pub use`（`src/lib.rs`）→ 生成代码引用（`crates/macro/src/lib.rs`）的四跳链路。
4. **原理问答**：用两三句话回答——为什么 `rust-i18n-macro` 必须 `proc-macro = true`？为什么 support 和 macro 不能合并？

**自检标准**：

- 你的依赖图里，`rust-i18n-cli` 应该是依赖图的「叶子」之一，且没有任何应用 crate 依赖它。
- 你的符号追踪里，`SimpleBackend` 应该最终在根 crate 名下可见，并且 `i18n!` 生成的代码能通过 `rust_i18n::SimpleBackend::new()` 找到它。
- 全程只读源码即可完成，无需运行；涉及运行验证的部分标注「待本地验证」。

> 提示：这份「一页纸」其实就是 u1-l2 的浓缩版。能独立产出它，说明你已经把 workspace 结构、crate 职责、依赖关系、门面再导出这四件事真正打通了。

## 6. 本讲小结

- rust-i18n 用「**根 package 兼任 workspace 根**」的结构，在一个 `Cargo.toml` 里同时管理对外发布的 `rust-i18n` 库和四个子 crate + 多个示例。
- 五个 crate 按「**阶段**」分工：`rust-i18n`（门面）、`rust-i18n-support`（运行时类型）、`rust-i18n-macro`（编译期过程宏）、`rust-i18n-extract`（提取器库）、`rust-i18n-cli`（`cargo i18n` 命令行）。
- 依赖关系上，根 crate 只依赖 support + macro；macro/extract/cli 都依赖 support 的 `codegen` feature；**extract 和 cli 不被根 crate 依赖**，不进用户应用二进制。
- `rust-i18n-macro` 必须设 `proc-macro = true`，因为它要在编译期介入；过程宏 crate 不能导出普通 item，这是它必须和 support 拆开的根本原因。
- 根 `src/lib.rs` 用三类 `pub use`（过程宏 `#[doc(hidden)]`、运行时类型公开、按 feature 条件）实现**门面模式**，让用户只依赖一个 crate 就拿到全部能力。
- 「符号旅行」链路：support 定义 → support `pub use` → 根 crate `pub use` → 用户代码 / `i18n!` 生成代码引用，四跳自洽。

## 7. 下一步学习建议

理解了 workspace 结构后，建议按这个顺序继续：

1. **u1-l3 快速上手：跑通最小示例**：亲手跑 `examples/app`，看 `i18n!("locales")`、`set_locale`、`t!` 三件套怎么用，把本讲的「门面」落到可运行代码上。
2. **u1-l4 本地化文件格式：v1 与 v2**：在进入编译期主链路（第二单元）之前，先认识 `support` 里 `codegen` feature 负责解析的 YAML/JSON/TOML 两种风格，为 u2-l2 的「加载与解析」打基础。
3. 进入第二单元后，重点读 `crates/macro/src/lib.rs` 的 `i18n` 过程宏和 `crates/support/src/lib.rs` 的 `try_load_locales`——你会把本讲看到的「macro 调 support 的 codegen 能力」这条边，展开成完整的编译期代码生成主链路。
