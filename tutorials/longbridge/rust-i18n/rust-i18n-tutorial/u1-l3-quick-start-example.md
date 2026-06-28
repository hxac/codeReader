# 快速上手：跑通最小示例

## 1. 本讲目标

本讲是「边动手边理解」的第一课。读完本讲，你应该能够：

1. 在自己的项目里用 `i18n!("locales")` 完成一次性初始化，把翻译文件加载进程序。
2. 用 `set_locale` / `locale` 切换和读取全局语言。
3. 用 `t!` 取出翻译文本，并掌握两种最常见的调用方式：
   - 带变量插值：`t!("hello", name = "...")`
   - 临时指定语言：`t!("hello", locale = "...")`

我们会全程围绕仓库里真实存在、真实可运行的示例 `examples/app` 来讲解。它代码很短，但已经覆盖了「初始化 → 切语言 → 取值 → 插值」的完整闭环。

## 2. 前置知识

在开始前，请确认你已理解下面这些概念（它们在 u1-l1 已经建立）：

- **i18n / l10n**：i18n 是 internationalization（国际化）的缩写，指让程序「能」支持多种语言；l10n 是 localization（本地化），指把程序「真正翻译成」某一种语言。
- **编译期代码生成（codegen）**：rust-i18n 不是在程序运行时去读 yaml 文件，而是在 `cargo build` 阶段就把翻译文件解析好、塞进编译产物里。运行时没有文件 IO。
- **`t!` 是转发壳**：你写的 `t!("hello")` 会被转发到 `crate::_rust_i18n_t!(...)`，而后者是 `i18n!` 宏在编译期生成的。所以**没有调用 `i18n!`，`t!` 就无法编译**。

此外你需要一点最基本的 Rust 基础：会创建一个 Cargo 项目、会写 `#[test]` 测试。

> 本讲只讲「怎么用」，不展开「`i18n!` 内部怎么扫描和生成代码」。后者是第 2 单元（u2）的主题。现在你只需要知道：**写一行 `i18n!`，翻译就准备好了。**

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `examples/app/main.rs` | 最小可运行示例，含 `i18n!` 初始化与一组测试，是本讲的「主角」。 |
| `examples/app/locales/en.yml` | 英语翻译（只含 `hello` 键）。 |
| `examples/app/locales/fr.yml` | 法语翻译（只含 `hello` 键）。 |
| `examples/app/locales/view.en.yml` | 英语翻译的「嵌套键」部分（`view.buttons.*`、`view.datetime.*`）。 |
| `examples/app/locales/view.fr.yml` | 法语翻译的「嵌套键」部分。 |
| `src/lib.rs` | 根 crate，定义 `t!`、`set_locale`、`locale` 以及全局 `CURRENT_LOCALE`。 |
| `README.md` | 官方用法说明，可作为权威参考。 |

注意：同一个 `en` 语言的翻译被拆成了 `en.yml` 和 `view.en.yml` 两个文件，rust-i18n 会把它们合并到同一个 `en` locale 下。多文件合并的细节在 u2-l3 讲解，本讲你只要知道「合并这件事会发生」即可。

## 4. 核心概念与源码讲解

### 4.1 用 `i18n!()` 完成初始化

#### 4.1.1 概念说明

`i18n!` 是一个**过程宏（procedural macro）**，它必须在 `main.rs` 或 `lib.rs` 的顶层调用一次。它的职责是：

1. 在**编译期**读取你指定的目录（例如 `locales`）下的翻译文件。
2. 把这些文件解析、合并、拍平成一张「locale → (key → value)」的大表。
3. 用这张表生成一个静态后端（`SimpleBackend`），以及供 `t!` 使用的内部宏 `_rust_i18n_t!` 和函数 `_rust_i18n_available_locales`。

换句话说，`i18n!` 就是 rust-i18n 的「总开关」。不打开它，后面所有的 `t!` 调用都会因为找不到 `_rust_i18n_t!` 而编译失败。

#### 4.1.2 核心流程

一次 `i18n!("locales")` 在编译期做的事，可以用下面这条流水线概括：

```text
i18n!("locales")
   │  1. 把 "locales" 解析成加载路径（相对于本 crate 的 Cargo.toml 所在目录）
   ▼
   扫描 locales/ 下所有 *.yml / *.yaml / *.json / *.toml
   │  2. 逐个解析、按语言分组
   ▼
   合并同名语言的多文件 + 把嵌套键拍平成点号键
   │  3. 例如 view.buttons.ok 来自嵌套结构
   ▼
   生成代码：静态 SimpleBackend + _rust_i18n_t! + _rust_i18n_available_locales
   │  4. 这些符号留在当前 crate 里
   ▼
   运行时：t!("...") 转发到 _rust_i18n_t!(...) → 在静态后端里查表
```

关键点：**步骤 1~4 都发生在编译期**，所以运行时 `t!` 只是在一张已经生成好的静态表里查找，速度很快，也不需要把 yaml 解析器带进最终二进制。

#### 4.1.3 源码精读

先看示例里的初始化那一行：

[examples/app/main.rs:1-2](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/main.rs#L1-L2) —— 在文件顶层调用 `rust_i18n::i18n!("examples/app/locales");`，完成当前 crate 的翻译初始化。

这里有个**新手容易踩的坑**值得专门说一下：为什么路径是 `examples/app/locales` 而不是 `locales`？

因为这个 `app` 示例是声明在根 `Cargo.toml` 里的（通过 `[[example]] name = "app"`），它属于根 package。`i18n!` 解析加载路径时，是**相对于该 package 的 `Cargo.toml` 所在目录**（即仓库根目录）来定位的。所以从仓库根出发，要写成 `examples/app/locales`。

> 在你自己的项目里，`locales` 文件夹一般就紧挨着 `Cargo.toml`，所以你通常会直接写 `i18n!("locales")`。路径解析的完整规则在 u5-l2（构建脚本与增量重编译）里讲。

再看 `i18n!` 这个宏是怎么来到你面前的。根 crate 把它从 `rust_i18n_macro` 子 crate 里 re-export 出来：

[src/lib.rs:5-6](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L5-L6) —— `pub use rust_i18n_macro::{_minify_key, _tr, i18n};`，其中 `i18n` 就是你调用的 `i18n!` 宏。它带有 `#[doc(hidden)]`，表示这是内部实现细节，文档里通常不直接展示。

官方 README 给出的标准初始化写法（与你自己的项目对齐时参考）：

[README.md:36-46](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L36-L46) —— 说明 `i18n!("locales")` 应放在 `lib.rs` 或 `main.rs`，并会读取 `Cargo.toml` 里的 `[package.metadata.i18n]` 配置（如果有的话）。

#### 4.1.4 代码实践

**目标**：亲手写一个最小的 `i18n!` 初始化，验证「不调用 `i18n!` 时 `t!` 无法编译」。

**步骤**：

1. 新建一个 Cargo 项目（u1-l1 已经建好的项目可以直接用）：
   ```bash
   cargo new my-i18n-app
   cd my-i18n-app
   ```
2. 在 `Cargo.toml` 加入依赖：
   ```toml
   [dependencies]
   rust-i18n = "3"
   ```
3. 在项目根目录新建 `locales/en.yml`，内容：
   ```yaml
   hello: Hello, world!
   ```
4. 把 `src/main.rs` 改成：
   ```rust
   rust_i18n::i18n!("locales");

   fn main() {
       println!("{}", t!("hello"));
   }
   ```
   注意：这里直接用 `t!` 而没有 `use`，能编译是因为……见下面第 5 步。
5. `cargo run`，应输出 `Hello, world!`。
6. **观察失败现象**：把第 4 步里的 `rust_i18n::i18n!("locales");` 这一行注释掉，再 `cargo build`。

**需要观察的现象**：

- 第 5 步：程序正常打印翻译文本，说明初始化成功。
- 第 6 步：编译报错，提示找不到 `_rust_i18n_t!` 之类的符号。这正是「`t!` 是转发壳、依赖 `i18n!` 生成内部宏」的直接证据。

> 关于第 4 步能直接用 `t!`：`t!` 通过 `#[macro_export]` 导出，所以在同 crate 内全局可用；跨文件时一般用 `use rust_i18n::t;`（示例 `examples/app` 就是这样做的）。两种写法等价，任选其一。

**预期结果**：加上 `i18n!` 能跑通；去掉 `i18n!` 编译失败。如果第 6 步居然能编译通过，说明你的 `t!` 没有真正连上后端，需要回头检查。

#### 4.1.5 小练习与答案

**练习 1**：`i18n!("locales")` 是在程序运行时执行，还是在 `cargo build` 时执行？

> **答案**：在 `cargo build` 时（编译期）执行。它是一个过程宏，会在编译阶段扫描文件、生成代码。运行时 `t!` 只是查表。

**练习 2**：为什么示例里要写 `i18n!("examples/app/locales")` 而不是 `i18n!("locales")`？

> **答案**：因为 `app` 示例属于根 package，加载路径相对于根 `Cargo.toml` 所在目录解析；从仓库根出发，locales 目录就在 `examples/app/locales`。在你自己的项目里，locales 一般紧挨 `Cargo.toml`，所以写 `i18n!("locales")` 即可。

### 4.2 用 `set_locale` / `locale` 管理全局语言

#### 4.2.1 概念说明

程序里有一个**全局当前语言**。`t!` 在没有显式指定 `locale` 时，就使用这个全局值。你用两个函数来操作它：

- `set_locale("fr")`：把全局语言设成法语。
- `locale()`：读取当前全局语言。

这样设计的好处是：你不必在每一处 `t!` 都写明语言，只要在程序启动或用户切换语言时调用一次 `set_locale`，全程序的 `t!` 就自动跟上。

#### 4.2.2 核心流程

```text
程序启动
  │  CURRENT_LOCALE 被惰性初始化为 "en"（默认英语）
  ▼
调用 set_locale("fr")
  │  把 CURRENT_LOCALE 内部的值原子地替换成 "fr"
  ▼
之后任何 t!("hello")（不带 locale 参数）
  │  读取 CURRENT_LOCALE 得到 "fr"
  ▼
在 "fr" 翻译表里查找 "hello"
```

两个要点：

1. 默认值是 `"en"`，即使你从不调用 `set_locale`，全局语言也是英语。
2. `set_locale` 是**原子替换**（基于 `arc-swap`），所以多线程下并发调用是安全的。线程安全细节在 u8-l1 讲，本讲你只要记住「它是线程安全的」。

#### 4.2.3 源码精读

全局语言存在一个静态变量里：

[src/lib.rs:15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L15) —— `static CURRENT_LOCALE: LazyLock<AtomicStr> = LazyLock::new(|| AtomicStr::from("en"));`。`LazyLock` 表示「第一次用到时才初始化」，初始值是 `"en"`。`AtomicStr` 是 rust-i18n 自己封装的无锁字符串类型。

写全局语言：

[src/lib.rs:17-20](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L17-L20) —— `set_locale` 调用 `CURRENT_LOCALE.replace(locale)`，原子地把当前语言换成传入值。

读全局语言：

[src/lib.rs:22-25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L22-L25) —— `locale()` 返回 `impl Deref<Target = str>`，也就是一个「守卫（Guard）」，你用它就像用 `&str`。返回守卫而不是裸引用，是为了避免在并发替换时拿到悬空指针（细节见 u8-l1）。

在示例里，测试通过两次 `set_locale` 来切换语言：

[examples/app/main.rs:12](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/main.rs#L12) —— `rust_i18n::set_locale("en");` 把全局语言设为英语。

[examples/app/main.rs:25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/main.rs#L25) —— `rust_i18n::set_locale("fr");` 切到法语。注意这之后第 26 行的 `t!("hello", ...)` 就返回法语文案了。

README 里也强调了这套用法：

[README.md:230-235](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L230-L235) —— 说明可以在运行时用 `set_locale` 设置全局语言，免去每次 `t!` 都指定语言。

#### 4.2.4 代码实践

**目标**：验证默认语言是 `en`，并能用 `set_locale` 切换、用 `locale` 读回。

**步骤**：

1. 在 4.1 实践的项目里，把 `src/main.rs` 的 `main` 改成：
   ```rust
   rust_i18n::i18n!("locales");

   fn main() {
       println!("default locale = {}", &*rust_i18n::locale());
       rust_i18n::set_locale("zh-CN");
       println!("after set    = {}", &*rust_i18n::locale());
   }
   ```
   （`locale()` 返回守卫，用 `&*` 解出 `&str` 才能 `{}` 打印。）
2. `cargo run`。

**需要观察的现象**：

- 第一行打印 `default locale = en`，验证默认值。
- 第二行打印 `after set    = zh-CN`，验证 `set_locale` 生效。

**预期结果**：两行分别输出 `en` 和 `zh-CN`。

> 注意：即使你的 `locales/` 里没有 `zh-CN.yml`，`set_locale("zh-CN")` 本身也不会报错——它只是改了全局标记。至于查不到翻译时会怎样（回退），是 u3-l4 的内容。本讲先不展开。

#### 4.2.5 小练习与答案

**练习 1**：如果不调用 `set_locale`，`locale()` 返回什么？

> **答案**：返回 `"en"`，因为 `CURRENT_LOCALE` 的惰性初始值就是 `"en"`。

**练习 2**：为什么 `locale()` 返回的是 `impl Deref<Target = str>`（守卫），而不是直接返回 `&str`？

> **答案**：因为全局语言可能在另一个线程被 `set_locale` 原子替换。返回守卫（底层是 `arc-swap` 的 Guard）可以保证你读到的字符串在这段使用期内不会被释放，避免悬空引用。完整原理在 u8-l1。

### 4.3 用 `t!` 取翻译文本

#### 4.3.1 概念说明

`t!` 是你日常用得最多的宏。它能做三件事，本讲聚焦前两件：

1. **按 key 取文本**：`t!("view.buttons.ok")`，key 用点号表示嵌套。
2. **变量插值**：翻译文案里的 `%{name}` 占位符，由 `t!("hello", name = "Jason")` 里的 `name` 参数填充。
3. **临时指定语言**：`t!("hello", locale = "fr")` 只对这一次调用用法语，**不会**改变全局语言。

第三单元（u3）会深入讲解 `t!` 的展开和插值原理，本讲只讲怎么用。

#### 4.3.2 核心流程

一次 `t!("hello", name = "Longbridge")` 的查找过程：

```text
t!("hello", name = "Longbridge")
   │  转发到 crate::_rust_i18n_t!(...)（由 i18n! 生成）
   ▼
确定 locale：
   │  本次没有 locale = 参数 → 用全局 CURRENT_LOCALE（这里设成了 "en"）
   ▼
在 "en" 翻译表里查 key "hello" → 得到模板 "Hello, %{name}!"
   ▼
插值：用 name = "Longbridge" 替换 %{name}
   ▼
返回 "Hello, Longbridge!"
```

两种「指定语言」方式的区别要分清：

| 写法 | 影响范围 | 是否改变全局语言 |
| --- | --- | --- |
| `set_locale("fr")` 后再 `t!(...)` | 之后所有不带 locale 的 `t!` | 是 |
| `t!(..., locale = "fr")` | 仅这一次调用 | 否 |

#### 4.3.3 源码精读

`t!` 的定义极简，它只是个转发壳：

[src/lib.rs:143-147](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L143-L147) —— `macro_rules! t` 把全部参数原样转发给 `crate::_rust_i18n_t!`。后者是 `i18n!` 在编译期生成的，负责真正去后端查表。

参数含义在文档里说得很清楚：

[src/lib.rs:93-110](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L93-L110) —— 文档说明：`expr` 是 key（如 `"foo.bar.baz"`）；变量名要包在 `%{}` 里（如 `"Hello, %{name}!"`）；可选的 `locale` 指定本次语言；`args` 用 `key = value` 传插值变量。

现在看示例里的真实用法。**英语场景**（全局已 `set_locale("en")`）：

[examples/app/main.rs:13-19](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/main.rs#L13-L19) —— 三种用法齐活：
- `t!("hello", name = "Longbridge")` → `"Hello, Longbridge!"`（带变量插值）。
- `t!("view.buttons.ok")` → `"Ok"`（点号嵌套键）。
- `t!("view.datetime.about_x_hours", count = "10")` → `"about 10 hours"`（嵌套键 + `%{count}` 插值）。

**临时指定法语（不改全局）**：

[examples/app/main.rs:21-24](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/main.rs#L21-L24) —— `t!("hello", locale = "fr", name = "Longbridge")` → `"Bonjour, Longbridge!"`。注意此时全局语言仍是 `en`，这一行只是临时用法语。

**切换全局语言到法语后**：

[examples/app/main.rs:25-30](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/main.rs#L25-L30) —— `set_locale("fr")` 之后，不带 locale 的 `t!` 自动返回法语：`t!("hello", name = "Longbridge")` → `"Bonjour, Longbridge!"`，`t!("view.datetime.about_x_hours", count = "10")` → `"environ 10 heures"`。

这些返回值来自哪里？看翻译文件：

[examples/app/locales/en.yml:1](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/en.yml#L1) —— `hello: Hello, %{name}!`，这就是英语 `hello` 的模板，`%{name}` 是占位符。

[examples/app/locales/fr.yml:1](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/fr.yml#L1) —— `hello: Bonjour, %{name}!`，法语对应模板。

[examples/app/locales/view.en.yml:1-6](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/view.en.yml#L1-L6) —— 嵌套结构 `view.buttons.ok/cancel` 和 `view.datetime.about_x_hours`。rust-i18n 在编译期把这种嵌套拍平成点号键 `view.buttons.ok`，所以你才能用 `t!("view.buttons.ok")` 取到。

[examples/app/locales/view.fr.yml:1-6](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/view.fr.yml#L1-L6) —— 法语对应的嵌套结构，`about_x_hours: environ %{count} heures`。

把源码和断言对照起来看，整条链路就清楚了：**翻译文件里的 `%{name}`/`%{count}` 占位符 ↔ `t!` 调用里的同名参数**，名字必须一致才能正确替换。

#### 4.3.4 代码实践

**目标**：亲手体验「临时 locale」与「全局 locale」的差别。

**步骤**：

1. 在前面项目里新增 `locales/fr.yml`：
   ```yaml
   hello: Bonjour, %{name}!
   ```
2. 把 `src/main.rs` 改成：
   ```rust
   rust_i18n::i18n!("locales");

   fn main() {
       rust_i18n::set_locale("en");
       println!("{}", t!("hello", name = "World"));          // 全局 en
       println!("{}", t!("hello", locale = "fr", name = "Monde")); // 临时 fr
       println!("{}", t!("hello", name = "World"));          // 仍是 en，全局没被改
   }
   ```
3. `cargo run`。

**需要观察的现象**：

- 第 1 行：`Hello, World!`
- 第 2 行：`Bonjour, Monde!`（临时法语）
- 第 3 行：又是 `Hello, World!`，证明第 2 行的 `locale = "fr"` 没有污染全局。

**预期结果**：三行依次为 `Hello, World!` / `Bonjour, Monde!` / `Hello, World!`。如果第 3 行变成了法语，说明你对「临时 locale」的理解有误——它绝不改变全局状态。

#### 4.3.5 小练习与答案

**练习 1**：翻译文件里写成 `hello: Hello, %{name}!`，但调用时写成 `t!("hello", username = "Jason")`，结果会是什么？为什么？

> **答案**：结果会是 `Hello, %{name}!`（占位符原样保留）。因为占位符名 `name` 和参数名 `username` 不一致，`%{name}` 找不到匹配的参数，就不会被替换。**占位符名和参数名必须完全一致。**

**练习 2**：`t!("view.buttons.ok")` 里的 `view.buttons.ok` 这个点号 key，对应翻译文件里的什么结构？

> **答案**：对应嵌套结构 `view: buttons: ok: Ok`。rust-i18n 在编译期把嵌套 map 拍平成点号分隔的扁平 key，所以 `view.buttons.ok` 能取到 `Ok`。拍平细节在 u2-l3。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性任务（这正是本讲规格指定的实践）：

**任务**：复制 `examples/app`，新增一个 `goodbye` 翻译键，给出英语和法语两个值，并用 `cargo test` 验证 `t!("goodbye")` 在两种 locale 下都返回正确文本。

**操作步骤**：

1. 这个示例已经声明在根 `Cargo.toml` 里：

   [Cargo.toml:71-73](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml#L71-L73) —— `[[example]] name = "app"` 且 `test = true`，表示可以用 `cargo test --example app` 运行其中的 `#[test]`。

2. 在 `examples/app/locales/en.yml` 末尾追加一行（key 名任选，这里用 `goodbye`）：
   ```yaml
   hello: Hello, %{name}!
   goodbye: Goodbye, %{name}!
   ```
3. 在 `examples/app/locales/fr.yml` 末尾追加对应法语：
   ```yaml
   hello: Bonjour, %{name}!
   goodbye: Au revoir, %{name}!
   ```
4. 在 `examples/app/main.rs` 的 `tests` 模块里补两条断言（参考已有的 `hello` 断言写法）：
   ```rust
   // 在 set_locale("en") 之后
   assert_eq!(t!("goodbye", name = "Longbridge"), "Goodbye, Longbridge!");
   // 在 set_locale("fr") 之后
   assert_eq!(t!("goodbye", name = "Longbridge"), "Au revoir, Longbridge!");
   ```
5. 在仓库根目录运行：
   ```bash
   cargo test --example app
   ```

**需要观察的现象**：

- 测试编译通过（说明 `i18n!` 重新加载了改动后的 yml）。
- `test_example_app` 通过，说明 `goodbye` 在 `en` 和 `fr` 两个 locale 下都返回了你写的值。

**预期结果**：`cargo test --example app` 输出 `test test_example_app ... ok`，全部断言通过。

> 进阶观察：修改 yml 后无需任何额外操作，直接 `cargo test` 就会重新编译——这是因为 `build.rs` 对 locale 文件发出了 `cargo:rerun-if-changed`（详见 u5-l2）。如果你想确认 `i18n!` 到底生成了什么代码，可以用 `RUST_I18N_DEBUG=1 cargo build --example app`，把生成的代码打印出来看（详见 u2-l4）。

## 6. 本讲小结

- `i18n!("locales")` 是一次性初始化的总开关，在**编译期**扫描翻译文件并生成静态后端；不调用它，`t!` 无法编译。
- 加载路径相对于本 crate 的 `Cargo.toml` 所在目录解析；示例 `app` 属于根 package，所以路径写成 `examples/app/locales`。
- 全局语言存在 `CURRENT_LOCALE`，默认 `"en"`；`set_locale` 改、`locale()` 读，且是线程安全的原子操作。
- `t!` 是转发壳，转发到 `i18n!` 生成的 `_rust_i18n_t!`；翻译文件里的 `%{name}` 占位符由 `t!(..., name = ...)` 的同名参数填充，名字必须一致。
- 两种指定语言的方式要分清：`set_locale` 改全局、影响后续所有 `t!`；`t!(..., locale = "fr")` 只影响当次调用，不改全局。
- 嵌套 YAML 会被拍平成点号键，所以 `t!("view.buttons.ok")` 能取到嵌套结构里的值。

## 7. 下一步学习建议

你已经会「用」rust-i18n 了。接下来建议按这个方向深入：

1. **u1-l4（本地化文件格式）**：本讲只用了最简单的 yml，下一讲会讲 `_version: 1`（按语言拆文件）和 `_version: 2`（所有语言塞进一个文件）两种风格的差异，帮你决定项目该用哪种。
2. **u2（编译期代码生成主链路）**：想知道 `i18n!` 内部到底怎么扫描、合并、拍平、生成代码，就进入第 2 单元。建议先读 u2-l1（`i18n!` 宏入口与参数解析）。
3. **u3（运行时翻译机制）**：想知道 `t!` 的完整展开链路、`%{}` 插值的字节级实现、以及查不到翻译时的回退策略，进入第 3 单元。

如果想立刻看到 `i18n!` 生成的代码长什么样，可以先用 `RUST_I18N_DEBUG=1` 编译本讲的 `app` 示例，把生成的代码贴出来对照阅读——这是衔接 u2 的最好热身。
