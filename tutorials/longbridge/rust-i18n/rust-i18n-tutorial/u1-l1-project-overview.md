# rust-i18n 是什么：定位与核心特性

## 1. 本讲目标

本讲是整本学习手册的起点。读完本讲，你应该能够：

- 说清楚 `rust-i18n` 到底解决了什么问题，它和「手写 `match` 做翻译」有什么本质区别。
- 理解它最核心的设计思想：**在编译期（compile time）把翻译文件代码生成进二进制**，运行时用一个全局的 `t!` 宏取文本。
- 列举 `rust-i18n` 的核心特性（多格式文件、fallback 回退、minify_key 短键等），并知道每个特性大致是哪个版本引入的。
- 看懂 [`t!`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) 和 `available_locales!` 这两个宏在 `src/lib.rs` 里是如何被导出和转发的。
- 了解它的 API 设计灵感来自 `ruby-i18n` 和 `Rails I18n`。

本讲**只讲「它是什么、为什么需要它」**，不深入编译期代码生成的实现细节——那是第二单元的内容。本讲的实践任务是本地新建一个 Cargo 项目并接入 `rust-i18n`。

## 2. 前置知识

在开始之前，最好对下面几个概念有一点了解。如果完全没接触过也没关系，本讲会用通俗的话再解释一遍。

- **国际化（Internationalization，常缩写为 i18n）**：因为首字母 `i` 和末字母 `n` 之间有 18 个字母，所以简称 i18n。它指的是让同一个程序能够根据用户的语言/地区显示不同文本的技术。例如同一个 `greeting` 键，英文环境显示 `Hello world`，中文环境显示 `你好世界`。
- **本地化（Localization，l10n）**：把程序「具体翻译成某一种语言」的过程。i18n 是「做好准备」，l10n 是「填入具体译文」。
- **locale（语言区域标识）**：用类似 `en`、`zh-CN`、`zh-Hant` 这样的字符串标识一种语言（有时还带地区）。`rust-i18n` 用它来区分不同语言的翻译。
- **Rust 的过程宏（proc-macro）与 `macro_rules!` 声明宏**：`rust-i18n` 既用到了过程宏（在编译期生成代码），也用到了声明宏（`t!` 这种写法）。本讲只需要知道 `t!` 是一个「宏」即可，具体机制后面讲义会拆开讲。
- **Cargo**：Rust 的包管理器与构建工具。本讲的实践任务需要你用 `cargo new` 建项目、用 `cargo build` 编译。

## 3. 本讲源码地图

本讲只涉及两个文件，它们是理解 `rust-i18n` 全貌的入口：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md) | 项目门面。用一段话讲清定位、列出全部特性（Features）、给出最小用法示例，是了解「它是什么」最快的入口。 |
| [src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) | 根 crate 的库入口。负责把子 crate 的符号「重新导出」给最终用户，并定义了 `t!`、`tkv!`、`available_locales!` 这几个用户最常用的宏，以及全局 locale 的读写函数。 |

> 提示：`src/lib.rs` 的第一行是 `#![doc = include_str!("../README.md")]`（[src/lib.rs:L1](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L1)），也就是说 **README 的内容会被直接当作这个 crate 的文档首页**。这也是为什么 README 写得这么详细——它既是仓库说明，也是 `docs.rs` 上的 API 文档。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

1. **README 功能列表** —— 理解 `rust-i18n` 的定位和「编译期代码生成」思想。
2. **`t!` / `available_locales!` 宏导出** —— 理解这两个最常用的宏是怎么在 `src/lib.rs` 里被定义和转发的。

### 4.1 README 功能列表：它解决了什么问题

#### 4.1.1 概念说明

假设我们要做一个支持中英文的程序。最朴素的做法是写一个 `match`：

```rust
// 示例代码：手写 match 做翻译的「笨办法」
fn greeting(locale: &str) -> &'static str {
    match locale {
        "en" => "Hello world",
        "zh-CN" => "你好世界",
        _ => "Hello world",
    }
}
```

这种写法在小项目里能跑，但很快会遇到一堆麻烦：

- 译文和逻辑代码混在一起，每次改文案都要改 `.rs` 源码、重新编译。
- 翻译人员（往往不是程序员）没法直接编辑译文。
- 没有统一的「回退（fallback）」机制——某个 locale 缺翻译时怎么办？
- 无法方便地做变量插值（`Hello, %{name}`）。

`rust-i18n` 的解决方案是把译文抽到独立的 **YAML / JSON / TOML 文件**里，然后在**编译期**把这些文件读取、解析、转换成 Rust 程序可用的数据，并**代码生成（codegen）进最终二进制**。运行时，你只需要调用一个全局的 `t!` 宏就能取到对应语言的文本。

> README 开头一句话就把这个定位讲清楚了（[README.md:L7](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L7)）：
> 「Rust I18n is a crate for loading localized text from a set of (YAML, JSON or TOML) mapping files. The mappings are converted into data readable by Rust programs **at compile time**, and then localized text can be loaded by simply calling the provided `t!` macro.」

这句话里有三个关键词，请记住：

1. **localized text**（本地化文本）：翻译后的文案。
2. **at compile time**（编译期）：解析翻译文件这件事，发生在 `cargo build` 阶段，不是程序运行时。
3. **`t!` macro**：运行时取文本的唯一入口。

另外，README 明确说明了它的 API 风格来自哪里（[README.md:L11](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L11)）：

> 「The API of this crate is inspired by [ruby-i18n](https://github.com/ruby-i18n/i18n) and [Rails I18n](https://guides.rubyonrails.org/i18n.html)。」

所以如果你写过 Rails，会发现 `t!("hello")`、`t!("messages.hello", name: "...")` 这种用法非常眼熟——这正是 `rust-i18n` 刻意对齐的设计。

#### 4.1.2 核心流程

从一个用户的视角，使用 `rust-i18n` 的整体流程可以概括为三步：

```text
1. 写翻译文件              2. 编译期代码生成              3. 运行时取文本
┌───────────────┐         ┌────────────────────┐        ┌──────────────────┐
│ locales/en.yml│  cargo  │ i18n! 宏读取文件   │ 生成   │ 程序调用 t! 宏   │
│ hello: Hello  │ ──build▶│ 解析 + 合并 + 扁平化│──────▶ │ 在静态后端里查找 │
└───────────────┘         │ → 生成 Rust 静态代码│  代码  │ → 返回当前语言文本│
                          └────────────────────┘        └──────────────────┘
```

用伪代码描述：

```
# 步骤 1：用户写一个 YAML 文件
locales/en.yml:
  _version: 1
  hello: "Hello world"

# 步骤 2：在 main.rs 里用 i18n! 宏初始化（这个宏在编译期执行）
i18n!("locales");

# 步骤 3：运行时用 t! 取值
println!("{}", t!("hello"));   // => "Hello world"
```

注意：**第 2 步是编译期发生的事**。`i18n!("locales")` 不是普通函数调用，而是一个过程宏——`cargo build` 时它会去读 `locales/` 目录下的文件，把翻译内容变成 Rust 代码编译进二进制。等程序真正运行时（第 3 步），翻译数据已经是二进制里的静态变量了，`t!` 只是在这些静态数据里做查找。

这个「编译期加载」带来的直接好处是：**程序运行时不需要再读磁盘上的 YAML 文件，也不需要把 YAML 解析器打包进二进制**（这是后面 `load-path` feature 相关的内容）。

#### 4.1.3 源码精读

下面是 README 里完整的 **Features（特性列表）**，建议你对照源码逐条理解。这是 `rust-i18n` 区别于其他 i18n 库的核心卖点（[README.md:L13-L25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L13-L25)）：

```text
- Codegen on compile time for includes translations into binary.
- Global `t!` macro for loading localized text in everywhere.
- Use YAML (default), JSON or TOML format for mapping localized text,
  and support mutiple files merging.
- `cargo i18n` Command line tool for checking and extract untranslated
  texts into YAML files.
- Support all localized texts in one file, or split into difference
  files by locale.
- Supports specifying a chain of fallback locales for missing translations.
- Supports automatic lookup of language territory for fallback locale.
  (Since v2.4.0)
- Support short hashed keys for optimize memory usage and lookup speed.
  (Since v3.1.0)
- Support format variables in `t!`, and support format variables with
  std::fmt syntax. (Since v3.1.0)
- Support for log missing translations at the warning level with
  `log-miss-tr` feature. (Since v3.1.0)
- `load-path` feature for runtime locale file loading via
  `try_load_locales`. By default, YAML/TOML parsing deps are
  compile-time only and not included in the binary.
```

把这 11 条翻译并归纳成一张表，方便你建立整体印象：

| 特性 | 一句话解释 | 引入版本 |
| --- | --- | --- |
| Codegen on compile time | 编译期把译文生成进二进制 | 初始 |
| Global `t!` macro | 全局可用的 `t!` 宏取文本 | 初始 |
| YAML / JSON / TOML + 多文件合并 | 三种格式都支持，且可拆分多文件 | 初始 |
| `cargo i18n` CLI | 命令行工具，从源码提取未翻译文案 | 初始 |
| 一文件 / 按语言拆分 | v1 拆分、v2 合并两种文件风格 | 初始 |
| fallback 回退链 | 缺翻译时按设定的语言链回退 | 初始 |
| territory 自动回退 | `zh-Hant-CN` → `zh-Hant` → `zh` | v2.4.0 |
| minify_key 短键 | 长文案用哈希短键，省内存、加速查找 | v3.1.0 |
| 变量插值 + `std::fmt` 格式化 | `%{name}` 占位符 + `{:08}` 格式说明符 | v3.1.0 |
| `log-miss-tr` | 命中失败时按 warning 级别记日志 | v3.1.0 |
| `load-path` feature | 运行时通过 `try_load_locales` 加载，解析器默认不进二进制 | 较新 |

这张表里大多数特性在后面对应单元都有专门讲义深入。本讲你只需要**记住这张表的整体轮廓**，知道 `rust-i18n` 不只是「一个翻译字典」那么简单。

> 小贴士：表中带「Since vX.X.0」的标注很重要——它告诉你某些特性是后来才加的。如果你看的是老版本的资料（比如网上博客），可能没有 minify_key、territory 回退这些功能。

#### 4.1.4 代码实践（阅读型）

**实践目标**：通过精读 README 的 Features 列表，建立对 `rust-i18n` 能力边界的整体认知，并搞清「编译期」三个字的含义。

**操作步骤**：

1. 打开 [README.md:L13-L25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L13-L25)，对照上面的归纳表，逐条读一遍英文原文。
2. 再读 [README.md:L7](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L7) 和 [README.md:L9](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L9) 这两句定位描述。
3. 在自己脑子里画一遍 4.1.2 节那张「写文件 → 编译期代码生成 → 运行时取文本」的三步流程图。

**需要观察的现象**：

- 注意 Features 里**没有任何一条**说「运行时从磁盘读 YAML」——这是默认行为不做的，只有开了 `load-path` feature 才会做。这印证了「编译期加载」是默认设计。

**预期结果**：

- 能用自己的话向别人解释：为什么 `rust-i18n` 把翻译文件解析放在编译期，而不是运行时。参考答案要点：① 二进制运行时零文件 IO；② YAML/TOML 解析器默认不进二进制，体积更小；② 翻译数据是静态的，`t!` 查找极快（README 基准测试显示单次 `t!` 约 33ns，见 [README.md:L437-L449](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L437-L449)）。

> 「待本地验证」：基准数据随机器不同而变化，你不必复现这个绝对数值。

#### 4.1.5 小练习与答案

**练习 1**：Features 列表里有 `load-path` feature 一条说「YAML/TOML parsing deps are compile-time only and not included in the binary」。这句话和「编译期代码生成」思想有什么关系？

**参考答案**：正因为翻译文件是在编译期（`cargo build` 时）解析并生成成 Rust 代码的，所以解析 YAML/TOML 的那些第三方库（如 `serde_yaml`、`toml`）只在编译期被需要。默认情况下它们不会被打包进最终二进制——这就是「compile-time only」的含义。只有当你开启 `load-path` feature，想在**运行时**也读文件，才需要把这些解析器编进二进制。

**练习 2**：README 提到 `_version: 1` 和 `_version: 2` 两种文件风格，它们和这里的 Features 哪一条对应？

**参考答案**：对应「Support all localized texts in one file, or split into difference files by locale」这条。`_version: 1` 是「按语言拆分成不同文件」，`_version: 2` 是「所有语言放进同一个文件」。（本讲不展开文件格式细节，那是 [u1-l4](u1-l4-locale-file-formats.md) 的主题。）

### 4.2 `t!` / `available_locales!` 宏导出

#### 4.2.1 概念说明

`rust-i18n` 的使用者最常打交道的，是两个宏：

- **`t!`**：取一条翻译文本。例如 `t!("hello")`、`t!("messages.hello", name = "world")`。它是**全局宏**，在程序的任何地方都能直接用。
- **`available_locales!`**：返回当前所有可用的 locale 列表，例如 `["en", "zh-CN"]`。

这两个宏都定义在根 crate 的 [src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) 里。本讲我们只看「它们长什么样、被导出成什么样」，不展开它们内部展开成什么代码（那是 [u3-l1](u3-l1-t-macro-call-chain.md) 的内容）。

这里有一个**关键设计点**值得先知道：`t!` 并不是一个「自己直接去查翻译」的宏。它只是一个**转发壳（forwarding shell）**，会把调用原样转发给 `crate::_rust_i18n_t!`。而 `_rust_i18n_t!` 这个内部宏，是由 `i18n!("locales")` 在编译期**自动生成**出来的，每个调用 `i18n!` 的 crate 都会生成属于自己的一份。

> 为什么要这么设计？因为一个大的 Rust workspace 里可能有多个 crate，每个 crate 都可能各自 `i18n!()` 初始化自己的翻译。让 `t!` 转发到「当前 crate 自己的」`_rust_i18n_t!`，就能保证每个 crate 的 `t!` 查的是它自己的翻译后端。本讲你只需记住这个「转发」的事实，原因会在 [u3-l1](u3-l1-t-macro-call-chain.md) 详讲。

#### 4.2.2 核心流程

`t!` 一次调用的「外壳」流程是这样的：

```text
你写：t!("hello")
   │
   ▼  （macro_rules! 声明宏，逐 token 转发）
展开为：crate::_rust_i18n_t!("hello")
   │
   ▼  （_rust_i18n_t! 是 i18n! 在编译期生成的内部宏）
最终：在当前 crate 的静态后端里查找 "hello" 对应文本
```

`available_locales!` 同理，它转发到一个由 `i18n!` 生成的函数 `crate::_rust_i18n_available_locales()`：

```text
你写：available_locales!()
   │
   ▼
展开为：crate::_rust_i18n_available_locales()
   │
   ▼
返回：Vec<&'static str>，例如 ["en", "zh-CN"]
```

注意这两个宏都引用了 `crate::...`，意味着它们查的是「当前 crate」里 `i18n!` 生成的符号。这也是为什么你必须在某个源文件里先调用过 `i18n!`，`t!` 才能用——否则 `crate::_rust_i18n_t!` 根本不存在，会编译报错。

#### 4.2.3 源码精读

先看 `src/lib.rs` 顶部的**重新导出（re-export）**部分。根 crate 把过程宏 crate `rust_i18n_macro` 里的符号重新导出给用户（[src/lib.rs:L5-L13](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L5-L13)）：

```rust
#[doc(hidden)]
pub use rust_i18n_macro::{_minify_key, _tr, i18n};
pub use rust_i18n_support::{
    AtomicStr, Backend, BackendExt, CowStr, MinifyKey, SimpleBackend,
    DEFAULT_MINIFY_KEY, DEFAULT_MINIFY_KEY_LEN, DEFAULT_MINIFY_KEY_PREFIX,
    DEFAULT_MINIFY_KEY_THRESH,
};
#[cfg(feature = "load-path")]
pub use rust_i18n_support::try_load_locales;
```

要点：

- `i18n`、`_tr`、`_minify_key` 这三个**过程宏**来自 `rust_i18n_macro` 子 crate。其中 `i18n` 就是你写的 `i18n!("locales")`；`_tr` 是 `t!` 最终会展开调用的核心宏。
- `Backend`、`SimpleBackend` 等**运行时类型**来自 `rust_i18n_support` 子 crate。
- `try_load_locales` 只有在开启 `load-path` feature 时才会被导出——这呼应了 4.1 节讲的「解析器默认不进二进制」。

> 你可能注意到：这里**没有直接 re-export `t` 这个宏**。`t!` 不是过程宏，而是下面用 `macro_rules!` 定义的声明宏。这种「过程宏走 `pub use`、声明宏走 `macro_rules!` + `#[macro_export]`」的混合方式是 Rust 常见模式。

接下来看 **`t!` 宏的定义**（[src/lib.rs:L141-L147](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L141-L147)）：

```rust
#[macro_export]
#[allow(clippy::crate_in_macro_def)]
macro_rules! t {
    ($($all:tt)*) => {
        crate::_rust_i18n_t!($($all)*)
    }
}
```

逐行解读：

- `#[macro_export]`：把这个宏导出到 crate 根，这样用户写 `rust_i18n::t!` 或 `use rust_i18n::t;` 后就能用 `t!`。
- `#[allow(clippy::crate_in_macro_def)]`：允许宏定义里出现 `crate::`（默认 clippy 会警告，因为宏可能在别的 crate 里展开，`crate::` 指向会变。这里正是要它指向「调用者所在 crate」，所以主动放行）。
- `($($all:tt)*) =>`：捕获**所有传入的 token**，原封不动地……
- `crate::_rust_i18n_t!($($all)*)`：转发给当前 crate 内部的 `_rust_i18n_t!`。

所以 `t!` 的实现体只有一行：**原样转发**。真正的查找逻辑在 `_rust_i18n_t!`（由 `i18n!` 生成）和它进一步调用的 `_tr!` 里。

再看 **`available_locales!` 宏**（[src/lib.rs:L191-L197](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L191-L197)）：

```rust
#[macro_export(local_inner_macros)]
#[allow(clippy::crate_in_macro_def)]
macro_rules! available_locales {
    () => {
        crate::_rust_i18n_available_locales()
    }
}
```

它比 `t!` 更简单——不接受参数，直接转发到 `crate::_rust_i18n_available_locales()` 这个函数调用（注意是函数，不是宏，结尾有 `()`）。

- `#[macro_export(local_inner_macros)]`：`local_inner_macros` 表示这个宏内部引用的其他宏（如果有的话）优先在**本 crate** 里找。这里虽然只调用函数，但保持这个属性是安全写法。

最后顺带看一眼**全局 locale 的读写**，因为 `t!` 在不指定 `locale = ...` 时就会用到当前全局 locale。它由一个静态变量 `CURRENT_LOCALE` 承载（[src/lib.rs:L15-L25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L15-L25)）：

```rust
static CURRENT_LOCALE: LazyLock<AtomicStr> = LazyLock::new(|| AtomicStr::from("en"));

/// Set current locale
pub fn set_locale(locale: &str) {
    CURRENT_LOCALE.replace(locale);
}

/// Get current locale
pub fn locale() -> impl Deref<Target = str> {
    CURRENT_LOCALE.as_str()
}
```

这说明：默认 locale 是 `"en"`；`set_locale("zh-CN")` 可以在运行时切换全局语言；之后所有不带 `locale =` 参数的 `t!` 调用都会自动用这个全局 locale 查找。关于这里的 `AtomicStr`、`LazyLock` 如何保证多线程安全，是 [u8-l1](u8-l1-atomicstr-thread-safety.md) 的主题，本讲不展开。

#### 4.2.4 代码实践（阅读型 + 验证型）

**实践目标**：亲手验证 `t!` 的「转发壳」本质，并理解 `available_locales!` 的来源。

**操作步骤**：

1. 打开 [src/lib.rs:L141-L147](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L141-L147)，确认 `t!` 的宏体只有一行转发。
2. 在 [src/lib.rs:L191-L197](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L191-L197) 确认 `available_locales!` 转发到的是函数 `_rust_i18n_available_locales()`（结尾有括号）。
3. 用 `git grep` 在仓库里搜索 `_rust_i18n_t` 这个符号，看看它在哪里被「定义」。

**需要观察的现象**：

- 第 3 步你会发现：在源码里**搜不到 `_rust_i18n_t!` 的 `macro_rules!` 定义**。这正是因为它是 `i18n!` 过程宏在编译期**生成**出来的，仓库源码里看不到它的字面定义。

**预期结果**：

- 理解为什么「必须在某个源文件先调用 `i18n!`，`t!` 才能编译通过」——因为 `_rust_i18n_t!` 是 `i18n!` 生成的，没有 `i18n!` 就没有这个内部宏，`t!` 转发时会找不到目标。

> 「待本地验证」：第 3 步的具体 grep 命令和返回行数依赖你的本地环境，你只需确认「搜不到宏定义」这一现象即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `t!` 宏体里写的是 `crate::_rust_i18n_t!(...)` 而不是 `rust_i18n::_rust_i18n_t!(...)`？

**参考答案**：因为 `t!` 设计成「查当前 crate 自己的翻译」。每个调用了 `i18n!` 的 crate 都会在自己内部生成一份 `_rust_i18n_t!`。用 `crate::` 就能保证宏在哪个 crate 里展开，就指向哪个 crate 自己生成的那份内部宏，从而实现「每个 crate 独立后端」。（`#[allow(clippy::crate_in_macro_def)]` 就是为此特意放行的。）

**练习 2**：`available_locales!` 和 `t!` 转发到的目标有什么不同？

**参考答案**：`t!` 转发到一个内部**宏** `_rust_i18n_t!`（名字结尾没有括号，因为它本身是宏调用）；`available_locales!` 转发到一个内部**函数** `_rust_i18n_available_locales()`（结尾有 `()`，是一次函数调用）。前者展开成更多代码，后者直接得到一个 `Vec`。

## 5. 综合实践

把本讲两个模块串起来，完成下面这个贯穿任务：

> **阅读 README，归纳出 `rust-i18n` 相比「手写 `match` 做翻译」的 3 个优势；并在本地新建一个 Cargo 项目，把 `rust-i18n` 加入依赖。**

**目标**：① 用本讲学到的特性列表，形成对 `rust-i18n` 价值的自我表达；② 在本地搭好一个最小可编译的「接入 `rust-i18n`」的项目骨架（本任务**不要求**真的调用 `t!` 成功取值，那需要先写翻译文件并调用 `i18n!`，是 [u1-l3](u1-l3-quick-start-example.md) 的内容；本讲只要求把依赖接上、能编译）。

**操作步骤**：

1. **归纳优势**：重读 [README.md:L13-L25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L13-L25)，挑出 3 条你认为最有说服力的特性，用自己的话写成 3 个优势点。参考方向（不要照抄，自己组织语言）：
   - 译文与代码分离 + 编译期加载（改文案不必改源码逻辑）。
   - 内建 fallback 回退链（缺翻译自动降级，不用自己写 `_ =>`）。
   - 变量插值与格式化（`%{name}`、`{:08}` 开箱即用，不必自己拼字符串）。
2. **新建项目**：

   ```bash
   cargo new my-i18n-app
   cd my-i18n-app
   ```

3. **加依赖**：编辑 `my-i18n-app/Cargo.toml`，在 `[dependencies]` 下加入（当前仓库版本为 `4.1.0`，参见 [Cargo.toml:L19](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L19)）：

   ```toml
   [dependencies]
   rust-i18n = "4"
   ```

   > 注意：README 的示例里写的是 `rust-i18n = "3"`（见 [README.md:L31-L34](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L31-L34)），那是文档尚未更新的旧版本。本仓库 `Cargo.toml` 实际是 `4.1.0`，所以新项目请用 `"4"`。

4. **最小可编译骨架**：把 `src/main.rs` 改成下面这样（**示例代码**，只验证依赖能编译，暂不调用 `t!`）：

   ```rust
   // 示例代码：仅验证 rust-i18n 依赖能被正确引入并编译
   // 注意：这里还没有 i18n!() 初始化，所以 t!() 还不能用，
   // 我们只是确认 `use rust_i18n::t;` 这一行能编译通过。
   #[allow(unused_imports)]
   use rust_i18n::t;

   fn main() {
       println!("rust-i18n 已接入，下一步将在 u1-l3 用 i18n! 初始化并调用 t!");
   }
   ```

5. **编译**：运行 `cargo build`。

**需要观察的现象**：

- `cargo build` 应当成功，并从 crates.io 拉取 `rust-i18n`（及其 `rust-i18n-macro`、`rust-i18n-support` 等子 crate）。
- 此时若你**强行**在 `main.rs` 里写 `t!("hello")` 再编译，应当会报类似 `cannot find macro _rust_i18n_t in this scope` 的错误——这正好印证了 4.2 节讲的：「没有 `i18n!` 就没有 `_rust_i18n_t!`」。

**预期结果**：

- 本地有一个能编译、已接入 `rust-i18n` 4.x 依赖的最小项目，作为后续讲义（[u1-l3](u1-l3-quick-start-example.md)）的起点。
- 一份用自己话写的「3 个优势」笔记。

> 「待本地验证」：`cargo build` 是否能拉到依赖、报错信息原文，取决于你的网络与本地 cargo 缓存。若离线，可改用 `path` 依赖指向本仓库根目录验证。

## 6. 本讲小结

- `rust-i18n` 解决的核心问题是：把翻译文件（YAML/JSON/TOML）在**编译期**解析、合并，并**代码生成进二进制**，运行时用一个全局 `t!` 宏取文本。
- 它的 API 灵感来自 `ruby-i18n` 和 `Rails I18n`，所以 `t!("hello", name = "...")` 这种写法和 Rails 很像。
- 核心特性包括：编译期 codegen、全局 `t!` 宏、多格式多文件合并、fallback 回退链（含 v2.4.0 的 territory 自动回退）、minify_key 短键（v3.1.0）、变量插值与 `std::fmt` 格式化、`log-miss-tr`、`load-path` 等。
- 在 [src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) 里，`t!` 和 `available_locales!` 都是**声明宏（macro_rules!）**，且都是**转发壳**：`t!` 转发到 `crate::_rust_i18n_t!`（内部宏），`available_locales!` 转发到 `crate::_rust_i18n_available_locales()`（内部函数）。
- 全局 locale 由 `CURRENT_LOCALE`（`LazyLock<AtomicStr>`，默认 `"en"`）承载，`set_locale` / `locale` 读写它；`t!` 不带 `locale =` 时就用这个全局值。
- 根 crate 通过 `pub use` 把 `rust_i18n_macro`（过程宏）和 `rust_i18n_support`（运行时类型）的符号对外暴露，`t!` 用的 `_tr`、初始化用的 `i18n` 都来自 `rust_i18n_macro`。

## 7. 下一步学习建议

本讲只看了「门面」。要继续深入，建议按下面顺序：

1. **下一讲 [u1-l2](u1-l2-workspace-structure.md)：工作区结构**。搞清楚根 crate 和 `rust-i18n-support`、`rust-i18n-macro`、`rust-i18n-extract`、`rust-i18n-cli` 四个子 crate 各自的职责，以及它们是怎么协作的。这会帮你理解本讲里反复出现的 `rust_i18n_macro`、`rust_i18n_support` 到底是什么。
2. **[u1-l3](u1-l3-quick-start-example.md)：快速上手**。在综合实践搭好的项目里，真正写出 `i18n!("locales")`、`t!("hello")`，跑通一个能切换语言的完整例子。
3. **第二单元（编译期代码生成主链路）**：如果你已经迫不及待想知道 `_rust_i18n_t!` 到底是怎么由 `i18n!` 生成出来的，可以跳到 [u2-l4](u2-l4-generate-code.md) 看 `generate_code` 的源码。但建议先按顺序学完第一单元，打好基础。

继续阅读建议：把本讲的 [src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) 整个通读一遍——它只有 200 多行，是理解整个项目骨架最好的起点。
