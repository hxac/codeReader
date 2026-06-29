# t! 宏的完整调用链

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚一次 `t!("hello")` 从「你在源码里敲下它」到「拿到翻译文本」中间到底经过了哪几跳。
2. 解释 `t!` 为什么要转发到 `crate::_rust_i18n_t!`，以及「每个 crate 拥有独立 backend」这个设计带来的约束。
3. 看懂 `i18n!` 生成的内部宏 `__rust_i18n_t!` 是如何把 `minify_key` 配置**偷偷注入**每一次 `_tr!` 调用的。
4. 画出 `_tr!` 过程宏最终生成的查找代码骨架，并指出 **backend 查表到底发生在哪一步**。

本讲是「运行时翻译机制」单元的入口，承接上一讲 u2-l4（`generate_code` 生成的运行时代码），重点回答一个问题：**生成的那些静态变量和函数，到底是被谁、以什么顺序调用起来的？**

## 2. 前置知识

在进入源码之前，先建立两个直觉。

### 2.1 声明宏（macro_rules!）与过程宏（proc-macro）的区别

- **声明宏** `macro_rules!`：靠「模式匹配 + 文本替换」展开，写法像 `match`。`t!`、`__rust_i18n_t!` 都是声明宏。它只能做 token 的搬运和重组，**不能在编译期跑 Rust 逻辑**。
- **过程宏** `#[proc_macro]`：是一段真正的 Rust 函数，输入是 token 流、输出也是 token 流，函数体里可以跑任意逻辑（解析、哈希、读文件）。`i18n!`、`_tr!`、`_minify_key!` 都是过程宏。

rust-i18n 的调用链之所以要「声明宏 → 声明宏 → 过程宏」三跳，正是因为每跳承担不同职责：第一跳定位到「当前 crate 的后端」，第二跳注入配置，第三跳才做真正的代码生成。

### 2.2 「转发壳」模式

一个 `#[macro_export]` 的声明宏，如果它的函数体只是把参数原样丢给另一个宏，就叫做**转发壳（forwarder）**。`t!` 就是最典型的转发壳：

```rust
macro_rules! t {
    ($($all:tt)*) => {
        crate::_rust_i18n_t!($($all)*)
    }
}
```

它**不做任何翻译**，只负责「把 `t!` 这个名字，路由到调用者自己 crate 里的 `_rust_i18n_t!`」。为什么不能直接在 `t!` 里查表？下一节会详细解释。

> 术语提示：`tt` 是 token tree（token 树），`$($all:tt)*` 表示「吃掉所有传入的 token，原样保留」。`crate::` 指向「当前 crate 的根」。这些会在 4.1 节展开。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) | 根 crate，导出所有公开符号 | `t!` 转发壳、`_tr` 过程宏的 re-export |
| [crates/macro/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) | 过程宏 crate：`i18n!`、`_tr!`、`_minify_key!` | `i18n!` 生成的 `__rust_i18n_t!`、`_rust_i18n_try_translate`、`_tr` 入口 |
| [crates/macro/src/tr.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs) | `_tr!` 的解析与代码生成 | `Tr` 结构、`into_token_stream` 的两条生成分支 |

一句话记住三个文件的分工：**根 crate 定义「名字」（`t!`、`_tr`），macro crate 的 `lib.rs` 定义「生成什么」（`__rust_i18n_t!`、`_rust_i18n_try_translate`），`tr.rs` 定义「`_tr!` 具体怎么展开」**。

## 4. 核心概念与源码讲解

整个调用链可以概括为**三跳**：

```
t!(...)                          ← 你写的代码
  │  Hop 1: macro_rules! t 转发
  ▼
crate::_rust_i18n_t!(...)        ← 由 i18n! 在你的 crate 里生成
  │  Hop 2: __rust_i18n_t 注入 minify_key 配置
  ▼
rust_i18n::_tr!(..., _minify_key = ...)   ← 真正的过程宏
  │  Hop 3: into_token_stream 生成查找代码
  ▼
crate::_rust_i18n_try_translate(locale, key)   ← backend 查表发生在这里
  │
  ▼
_RUST_I18N_BACKEND.translate(...)   ← 命中静态后端，拿到 Cow<str>
```

下面按这三跳拆成三个最小模块。

### 4.1 Hop 1：`macro_rules! t!` 转发壳

#### 4.1.1 概念说明

`t!` 是 rust-i18n 暴露给用户的「门面」。它本身**不做任何翻译**，只做一件事：把调用转发到调用者**自己 crate**里的内部宏 `_rust_i18n_t!`。

为什么非要绕这一层，而不是直接在 `t!` 里查 backend？因为 rust-i18n 的核心设计是：**每个调用了 `i18n!("locales")` 的 crate，都拥有自己独立的 `_RUST_I18N_BACKEND` 静态后端**（在 u2-l4 讲过）。于是在一个 workspace 里：

- crate A 调用了 `i18n!`，它有自己的 backend、自己的翻译表。
- crate B 也调用了 `i18n!`，它有另一个 backend、另一份翻译表。

当你在 crate A 里写 `t!("hello")`，必须查的是 **crate A 的 backend**；在 crate B 里写同样的 `t!("hello")`，必须查 **crate B 的 backend**。但 `t!` 这个宏是 `#[macro_export]` 的、全局唯一的，它怎么知道「该用谁的 backend」？

答案就是 `crate::` 这个前缀。`t!` 转发到 `crate::_rust_i18n_t!`，这里的 `crate::` 永远指向**写 `t!` 的那个 crate 的根**。而 `_rust_i18n_t!` 是由 `i18n!` 在**那个 crate 内部**生成的（通过 `pub(crate) use`）。于是「当前 crate 的 `t!`」自然就路由到「当前 crate 的 `_rust_i18n_t!`」，进而路由到「当前 crate 的 backend」。这就是「每个 crate 独立 backend」得以实现的关键机制。

这也解释了一个常见报错：**如果你忘了在某 crate 里调用 `i18n!`，那么该 crate 里就不存在 `_rust_i18n_t!`，于是 `t!` 展开会报「cannot find macro `_rust_i18n_t`」**。

#### 4.1.2 核心流程

```
用户代码: t!("messages.hello", name = "Jason")
        │  匹配 ($($all:tt)*)
        ▼
展开为:  crate::_rust_i18n_t!("messages.hello", name = "Jason")
        │  此处的 crate:: = 写 t! 的那个 crate 的根
        ▼
（交给 Hop 2）
```

要点：

- `t!` 用 `$($all:tt)*` 吃掉**所有**参数（包括 `locale = ...`、`name = ...` 等），不做任何拆分。
- `crate::_rust_i18n_t!` 必须存在，否则编译失败。

#### 4.1.3 源码精读

`t!` 的定义在根 crate 的 [src/lib.rs:L143-L147](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L143-L147)，整段只有一个 arms：

```rust
#[macro_export]
#[allow(clippy::crate_in_macro_def)]
macro_rules! t {
    ($($all:tt)*) => {
        crate::_rust_i18n_t!($($all)*)
    }
}
```

- `#[macro_export]`：把 `t!` 导出到 crate 根，使它对任何 `use rust_i18n::*` 或 `#[macro_use]` 的代码可见，并且**总是在调用者 crate 的根作用域展开**——这正是 `crate::` 能正确路由的前提。
- `#[allow(clippy::crate_in_macro_def)]`：在宏定义里写 `crate::` 通常会被 clippy 警告（因为宏可能被别的 crate 调用，`crate::` 的语义会「漂移」）。这里恰恰**故意利用**这种漂移——让 `crate::` 指向调用者自己的 crate，所以加了这个 allow。

注意 `_tr`（过程宏本体）的 re-export 在 [src/lib.rs:L5-L6](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L5-L6)：

```rust
#[doc(hidden)]
pub use rust_i18n_macro::{_minify_key, _tr, i18n};
```

`t!` 在 Hop 2 最终会调用 `rust_i18n::_tr!`，而 `_tr` 这个过程宏就是从这里被「搬到」`rust_i18n` 命名空间下的。`#[doc(hidden)]` 表示它是对外不可见的内部实现细节。

#### 4.1.4 代码实践

**实践目标**：亲手验证「没有 `i18n!` 就没有 `_rust_i18n_t!`」。

**操作步骤**：

1. 新建一个最小 Cargo 项目，把 `rust-i18n` 加进依赖。
2. 在 `src/main.rs` 里写：
   ```rust
   #[macro_use]
   extern crate rust_i18n;

   fn main() {
       println!("{}", t!("hello"));
   }
   ```
   **故意不写 `i18n!("locales");`**。
3. 运行 `cargo build`。

**需要观察的现象**：编译器报错，提示找不到 `_rust_i18n_t` 之类的未解析宏。

**预期结果**：报错信息印证了 Hop 1 的依赖关系——`t!` 转发依赖 `i18n!` 生成的 `_rust_i18n_t!`。补上 `i18n!("locales");` 并建好 `locales/en.yml` 后即可编译通过。

> 若本地无法运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `t!` 里的 `crate::_rust_i18n_t!` 改成 `rust_i18n::_rust_i18n_t!`，会发生什么？

**参考答案**：`rust_i18n`（库）本身并不会调用 `i18n!`，所以 `rust_i18n` crate 内部不存在 `_rust_i18n_t!`，会导致编译失败。这也说明「`crate::` 路由到调用者自身」是刻意设计。

**练习 2**：`$($all:tt)*` 中的 `*` 和外层 `$()` 各起什么作用？

**参考答案**：外层 `$()` 圈定「要重复的模式」，`$all:tt` 是每次重复捕获的 token 树，`*` 表示「重复零次或多次」。三者合起来表示「贪婪地吃掉所有剩余 token，统称 `all`」。

### 4.2 Hop 2：`__rust_i18n_t!` 把 `minify_key` 配置透传给 `_tr!`

#### 4.2.1 概念说明

Hop 1 之后，调用来到了 `crate::_rust_i18n_t!`。这个宏**不是手写的**，而是上一讲 u2-l4 里 `i18n!` 在编译期生成的。它的真正名字是 `__rust_i18n_t`，然后通过一行 `pub(crate) use __rust_i18n_t as _rust_i18n_t;` 暴露成 `_rust_i18n_t`。

这个生成宏的核心职责只有一个：**把 `i18n!` 接收到的 `minify_key` 系列配置，以「额外命名参数」的形式，偷偷追加到每一次 `_tr!` 调用里**。

为什么要这么做？因为 `_tr!`（过程宏，下一跳）需要知道 `minify_key` 是否开启、长度多少、前缀是什么、阈值多少，才能决定 key 是「原样使用」还是「哈希成短键」。但这些配置是**整个 crate 级别**的（写在 `i18n!("locales", minify_key = true)` 或 `Cargo.toml` 里），用户每次写 `t!("hello")` 不可能重复传一遍。于是 rust-i18n 用一个生成宏做「自动注入」：用户无感，但每次 `t!` 都带上了正确的 minify 配置。

这是「用户无感生效」的关键设计——回顾 u2-l4 提到的「内部宏将 minify_key 隐式注入每次 `_tr!`」，本模块就是把这句话落到源码上。

#### 4.2.2 核心流程

`i18n!(...)` 在编译期算出四个配置值（设为 `MK`、`MK_LEN`、`MK_PREFIX`、`MK_THRESH`），然后生成：

```
crate::_rust_i18n_t!("messages.hello", name = "Jason")
        │  展开为（生成宏的固定模板）
        ▼
rust_i18n::_tr!(
    "messages.hello", name = "Jason",
    _minify_key = MK, _minify_key_len = MK_LEN,
    _minify_key_prefix = MK_PREFIX, _minify_key_thresh = MK_THRESH
)
        │  （交给 Hop 3）
        ▼
```

注意 `_minify_key`、`_minify_key_len` 等参数名带下划线前缀，是有意和用户参数（如 `name`、`locale`）区分开的「系统参数」。

#### 4.2.3 源码精读

生成宏的定义在 [crates/macro/src/lib.rs:L411-L417](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L411-L417)：

```rust
#[doc(hidden)]
#[allow(unused_macros)]
macro_rules! __rust_i18n_t {
    ($($all_tokens:tt)*) => {
        rust_i18n::_tr!($($all_tokens)*, _minify_key = #minify_key, _minify_key_len = #minify_key_len, _minify_key_prefix = #minify_key_prefix, _minify_key_thresh = #minify_key_thresh)
    }
}
```

- 这段代码出现在 `generate_code` 的 `quote! { ... }` 里，所以 `#minify_key`、`#minify_key_len` 等是**编译期插值**——它们的值就是 `i18n!` 解析出来的配置（来源见 u2-l1 的三级优先级：默认值 < `Cargo.toml` < 宏显式参数）。比如默认情况下 `#minify_key` 会被替换成 `false`（`DEFAULT_MINIFY_KEY = false`）。
- `rust_i18n::_tr!(...)`：注意这里用的是 `rust_i18n::` 而不是 `crate::`，因为 `_tr` 是 `rust_i18n` 库全局导出的过程宏，所有 crate 共用同一份。

紧接着在 [crates/macro/src/lib.rs:L431](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L431) 把它改名暴露：

```rust
pub(crate) use __rust_i18n_t as _rust_i18n_t;
```

`pub(crate)` 保证它只在「调用了 `i18n!` 的那个 crate」内部可见，正好和 Hop 1 里 `crate::_rust_i18n_t!` 对上。

`_tr` 过程宏的入口在 [crates/macro/src/lib.rs:L443-L452](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L443-L452)：

```rust
#[proc_macro]
#[doc(hidden)]
pub fn _tr(input: proc_macro::TokenStream) -> proc_macro::TokenStream {
    parse_macro_input!(input as tr::Tr).into()
}
```

入口同样很薄：把 token 流解析成 `tr::Tr` 结构，再通过 `Into<TokenStream>` 转回 token 流（实际调用 `into_token_stream`，见 4.3 节）。

`_tr!` 收到的参数里混了「用户参数」和「系统参数」，`Tr` 的解析会把它们分开。负责剥离系统参数的是 `filter_arguments`，见 [crates/macro/src/tr.rs:L342-L376](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L342-L376)：

```rust
fn filter_arguments(&mut self) -> syn::parse::Result<()> {
    for arg in self.args.iter() {
        match arg.name.as_str() {
            "locale" => { self.locale = Some(arg.value.clone()); }
            "_minify_key" => { self.minify_key = Self::parse_minify_key(&arg.value)?; }
            "_minify_key_len" => { self.minify_key_len = Self::parse_minify_key_len(&arg.value)?; }
            "_minify_key_prefix" => { self.minify_key_prefix = Self::parse_minify_key_prefix(&arg.value)?; }
            "_minify_key_thresh" => { self.minify_key_thresh = Self::parse_minify_key_thresh(&arg.value)?; }
            _ => {}
        }
    }
    // 把系统参数从「普通参数列表」里移除，避免它们被当成插值变量
    self.args.as_mut().retain(|v| ![...].contains(&v.name.as_str()));
    Ok(())
}
```

这段做两件事：① 把 `_minify_key*` 和 `locale` 这些系统参数从 `args` 里「摘出来」存进 `Tr` 的字段；② 用 `retain` 把它们从普通参数列表里删掉，保证它们**不会**被当成 `%{name}` 那样的插值变量去替换。`Tr` 结构本身的字段见 [crates/macro/src/tr.rs:L262-L270](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L262-L270)，其中 `minify_key`、`minify_key_len` 等就是从这里来的。

#### 4.2.4 代码实践

**实践目标**：用官方调试开关，亲眼看到 Hop 2 注入的 `_minify_key` 参数。

**操作步骤**：

1. 在 `examples/app`（或你自己的项目）里，把 `i18n!` 调用改成显式开启 minify：
   ```rust
   i18n!("locales", minify_key = true);
   ```
2. 用调试环境变量编译：
   ```bash
   RUST_I18N_DEBUG=1 cargo build 2>&1 | sed -n '/-------------- code/,/--------------/p'
   ```
   （`RUST_I18N_DEBUG=1` 会让 `i18n!` 把生成的代码 `println!` 出来，见 [crates/macro/src/lib.rs:L260-L265](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L260-L265)。）

**需要观察的现象**：在打印出的生成代码里，找到 `macro_rules! __rust_i18n_t`，确认它展开出的 `_tr!(..., _minify_key = true, _minify_key_len = 24, _minify_key_prefix = "", _minify_key_thresh = 127)`。

**预期结果**：可见 `_minify_key = true` 被编译期插值进去，其余三个用了默认常量值（`24`、`""`、`127`，见 `minify_key.rs` 的 `DEFAULT_*` 常量）。

> 待本地验证：实际打印内容以本地 `cargo build` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_minify_key` 系列参数要带下划线前缀？

**参考答案**：为了和用户的插值变量名（如 `name`、`count`）区分开，避免冲突。`filter_arguments` 正是靠这些带前缀的固定名字来识别并剥离系统参数，下划线前缀相当于「保留命名空间」。

**练习 2**：`__rust_i18n_t` 用 `rust_i18n::_tr!` 而不是 `crate::_tr!`，说明什么？

**参考答案**：`_tr` 是 `rust_i18n` 库全局导出的**同一份**过程宏，所有 crate 共用，不需要每个 crate 各自生成一份。而 `_rust_i18n_t!` 必须每个 crate 各有一份（因为要注入该 crate 自己的 minify 配置），所以用 `crate::` 路由。

### 4.3 Hop 3：`_tr!` 展开为查找代码，`_rust_i18n_try_translate` 真正查表

#### 4.3.1 概念说明

前两跳都是「搬运 + 注入」，没有产生任何查找逻辑。真正的代码生成发生在 `_tr!` 过程宏的 `into_token_stream` 里：它根据「是否启用 minify_key」「有没有插值参数」两个维度，生成一段**等价的 Rust 表达式**，这段表达式里调用 `crate::_rust_i18n_try_translate(locale, key)`。

而 `_rust_i18n_try_translate` 是 `i18n!` 在你的 crate 里生成的**函数**（u2-l4 已生成，本讲讲它如何被 `_tr!` 调用）。它内部访问 `_RUST_I18N_BACKEND` 静态后端做查找——**backend 查表就发生在这里**。

所以本模块回答本讲的核心问题：**backend 查找发生在 Hop 3 生成的 `_rust_i18n_try_translate` 调用里，而不是更早。**

#### 4.3.2 核心流程

`into_token_stream` 先决定两件事，再据此选分支：

1. **key 怎么算**（受 `minify_key` 影响，共 4 个分支）：
   - `minify_key` 开 + 字面量字符串 → 编译期算出短键常量（最快）。
   - `minify_key` 开 + 元组 `(key, msg)` → 直接取元组两元素。
   - `minify_key` 开 + 其它动态值 → 生成**运行时**调用 `MinifyKey::minify_key(...)`（最慢，每次查找都哈希）。
   - `minify_key` 关（默认）→ `msg_key = &msg_val`，原样用字面量当 key。
2. **有没有插值参数**（`args.is_empty()`）：
   - 无参数 → 命中就直接返回译文；miss 则返回原值。
   - 有参数 → 命中后还要用 `replace_patterns` 把 `%{name}` 占位符替换掉；miss 则对原值做替换。

无论哪条分支，**查找入口都是同一个**：`crate::_rust_i18n_try_translate(locale, &msg_key)`。

`_rust_i18n_try_translate` 的查找顺序（来自 u2-l4 的生成代码）：

```
1. 精确查 _RUST_I18N_BACKEND.translate(locale, key)
2. miss → territory 回退：zh-Hant-CN → zh-Hant → zh（逐级去 "-"）
3. 仍 miss → 显式 fallback 列表 _RUST_I18N_FALLBACK_LOCALE
4. 全 miss → 返回 None（由 _tr! 生成的代码决定回退成 key 还是原值）
```

#### 4.3.3 源码精读

`into_token_stream` 的全貌在 [crates/macro/src/tr.rs:L390-L466](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L390-L466)。先看「key 怎么算」的 4 分支（默认 `minify_key = false`，所以日常使用走最后那个 `else`）：

```rust
let (msg_key, msg_val) = if self.minify_key && self.msg.val.is_expr_lit_str() {
    // 编译期算短键
    ...
} else if self.minify_key && self.msg.val.is_expr_tuple() {
    self.msg.val.to_tupled_token_streams().unwrap()
} else if self.minify_key {
    // 运行时算短键（动态值）
    let msg_key = quote! { rust_i18n::MinifyKey::minify_key(&msg_val, ...) };
    (msg_key, msg_val)
} else {
    // 默认：原样用字面量当 key
    let msg_val = self.msg.val.to_token_stream();
    let msg_key = quote! { &msg_val };
    (msg_key, msg_val)
};
```

接着决定 locale（没传 `locale=` 就用全局当前 locale），见 [crates/macro/src/tr.rs:L414-L417](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L414-L417)：

```rust
let locale = self.locale.map_or_else(
    || quote! { &rust_i18n::locale() },
    |locale| quote! { #locale },
);
```

> 这里 `&rust_i18n::locale()` 利用了 `locale()` 返回 `impl Deref<Target=str>` 的守卫（见 u1-l3），取 `&` 后经 Deref 强制转换成 `&str`。

**无参数分支**（命中即返回译文，miss 返回原值）在 [crates/macro/src/tr.rs:L433-L445](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L433-L445)：

```rust
if self.args.is_empty() {
    quote! {
        {
            let msg_val = #msg_val;
            let msg_key = #msg_key;
            if let Some(translated) = crate::_rust_i18n_try_translate(#locale, &msg_key) {
                translated.into()
            } else {
                #logging
                rust_i18n::CowStr::from(msg_val).into_inner()
            }
        }
    }
}
```

**有参数分支**（命中后多一步 `replace_patterns` 替换占位符）在 [crates/macro/src/tr.rs:L446-L465](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L446-L465)：

```rust
} else {
    quote! {
        {
            let msg_val = #msg_val;
            let msg_key = #msg_key;
            let keys = &[#(#keys),*];
            let values = &[#(#values),*];
            {
                if let Some(translated) = crate::_rust_i18n_try_translate(#locale, &msg_key) {
                    let replaced = rust_i18n::replace_patterns(&translated, keys, values);
                    std::borrow::Cow::from(replaced)
                } else {
                    #logging
                    let replaced = rust_i18n::replace_patterns(rust_i18n::CowStr::from(msg_val).as_str(), keys, values);
                    std::borrow::Cow::from(replaced)
                }
            }
        }
    }
}
```

两条分支里 `crate::_rust_i18n_try_translate(...)` 的 `crate::` 同样指向调用者 crate 的根——那里有 `i18n!` 生成的同名函数（u2-l4）。该函数定义在 [crates/macro/src/lib.rs:L381-L400](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L381-L400)：

```rust
pub fn _rust_i18n_try_translate<'r>(locale: &str, key: impl AsRef<str>) -> Option<std::borrow::Cow<'r, str>> {
    _RUST_I18N_BACKEND.translate(locale, key.as_ref())   // ← backend 查表就在这一行
        .or_else(|| {
            // territory 回退
            let mut current_locale = locale;
            while let Some(fallback_locale) = _rust_i18n_lookup_fallback(current_locale) {
                if let Some(value) = _RUST_I18N_BACKEND.translate(fallback_locale, key.as_ref()) {
                    return Some(value);
                }
                current_locale = fallback_locale;
            }
            // 显式 fallback 列表
            _RUST_I18N_FALLBACK_LOCALE.and_then(|fallback| {
                fallback.iter().find_map(|locale| _RUST_I18N_BACKEND.translate(locale, key.as_ref()))
            })
        })
}
```

`_RUST_I18N_BACKEND` 是 u2-l4 生成的静态后端（`LazyLock<Box<dyn Backend>>`），见 [crates/macro/src/lib.rs:L341-L347](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L341-L347)；territory 回退靠 `_rust_i18n_lookup_fallback` 逐级去掉 `-`，见 [crates/macro/src/lib.rs:L355-L365](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L355-L365)。回退策略的细节留待 u3-l4 详讲，本讲只需记住：**第一次 `.translate()` 调用就是 backend 查表的入口**。

#### 4.3.4 代码实践

**实践目标**：对两种典型调用，手写出 `_tr!` 展开后的伪代码骨架，并标出 backend 查找发生在哪一步（即本讲指定的实践任务）。

**操作步骤**：参照 4.3.3 的两条分支，假设默认未开 minify_key、未传 `locale=`，分别推导。

**① `t!("hello")` 的展开骨架**：

```rust
// Hop 1
crate::_rust_i18n_t!("hello")
// Hop 2
rust_i18n::_tr!("hello", _minify_key = false, _minify_key_len = 24,
                _minify_key_prefix = "", _minify_key_thresh = 127)
// Hop 3（无参数分支，msg_key = &msg_val）
{
    let msg_val = "hello";
    let msg_key = &msg_val;
    if let Some(translated) = crate::_rust_i18n_try_translate(&rust_i18n::locale(), &msg_key) {
        // ★★★ backend 查找发生在 _rust_i18n_try_translate 内部的 _RUST_I18N_BACKEND.translate() ★★★
        translated.into()
    } else {
        rust_i18n::CowStr::from(msg_val).into_inner()   // miss：返回原值 "hello"
    }
}
```

**② `t!("messages.hello", name = "x")` 的展开骨架**：

```rust
// Hop 1
crate::_rust_i18n_t!("messages.hello", name = "x")
// Hop 2
rust_i18n::_tr!("messages.hello", name = "x", _minify_key = false, ...)
//           ← filter_arguments 会把 name 留下，把 _minify_key* 剥掉
// Hop 3（有参数分支）
{
    let msg_val = "messages.hello";
    let msg_key = &msg_val;
    let keys = &["name"];
    let values = &[format!("{}", "x")];        // 每个 value 都被 format! 包一层
    {
        if let Some(translated) = crate::_rust_i18n_try_translate(&rust_i18n::locale(), &msg_key) {
            // ★★★ backend 查找发生在这一步 ★★★
            let replaced = rust_i18n::replace_patterns(&translated, keys, values);  // 替换 %{name}
            std::borrow::Cow::from(replaced)
        } else {
            let replaced = rust_i18n::replace_patterns(rust_i18n::CowStr::from(msg_val).as_str(), keys, values);
            std::borrow::Cow::from(replaced)
        }
    }
}
```

**需要观察的现象 / 预期结果**：

- 两种调用里，**backend 查找都只发生在 `_rust_i18n_try_translate(...)` 这一行**，前两跳不查表。
- ① 走无参数分支，命中直接返回；② 走有参数分支，命中后多一次 `replace_patterns` 占位符替换。
- `locale` 没传时统一用 `&rust_i18n::locale()`（全局当前 locale）。

> 待本地验证：可用 `RUST_I18N_DEBUG=1 cargo build` 把真实生成代码打印出来，和上面的骨架逐行对照。

#### 4.3.5 小练习与答案

**练习 1**：如果开了 `minify_key`，且写的是 `t!("一句很长的文案")`（字面量），`msg_key` 会变成什么？

**参考答案**：走 4.3.3 的第一个分支，`minify_key` 在**编译期**就把字面量哈希成短键常量，`msg_key` 是一个编译期确定的短字符串 token（如 `""` 前缀 + base62 哈希），运行时不再计算。这正是 minify_key「省内存、加速查找」的关键（算法细节见 u6-l1）。

**练习 2**：`t!("hi")` 命中翻译时，返回的是 `Cow::Borrowed` 还是 `Cow::Owned`？为什么？

**参考答案**：无参数分支命中时，`translated.into()` 直接返回 `_rust_i18n_try_translate` 给的 `Cow`。由于译文以字面量灌进 `SimpleBackend`（u2-l4 用 `Cow::Borrowed` 存），所以命中的是 `Cow::Borrowed`，**零拷贝**。只有 miss 后 `CowStr::from(msg_val).into_inner()` 才会产生 `Owned`。

**练习 3**：为什么 `_rust_i18n_try_translate` 用 `crate::` 而不是 `rust_i18n::`？

**参考答案**：因为它要访问的是**当前 crate** 的 `_RUST_I18N_BACKEND` 静态后端（每个 crate 独立一份）。用 `crate::` 让它指向调用者 crate 根，从而查到正确的 backend。这和 Hop 1 用 `crate::` 的理由一致。

## 5. 综合实践

**任务**：把三跳串起来，画一张完整的「调用链 + 数据流」图，并用 `RUST_I18N_DEBUG=1` 验证。

**步骤**：

1. 在 `examples/app` 里加两个调用：
   ```rust
   i18n!("locales");
   fn main() {
       println!("{}", t!("hello"));
       println!("{}", t!("messages.hello", name = "Jason"));
   }
   ```
   （确保 `locales/en.yml` 里有 `hello` 和 `messages.hello` 两个键。）
2. `RUST_I18N_DEBUG=1 cargo build`，截取打印出的生成代码。
3. 在生成代码里依次找出并标注：
   - `macro_rules! __rust_i18n_t`（Hop 2 的注入点）。
   - `pub fn _rust_i18n_try_translate`（Hop 3 的查找入口）。
   - `static _RUST_I18N_BACKEND`（被查找的静态后端）。
4. 用 4.3.4 的骨架，对照生成代码，用箭头标出：`t!` → `_rust_i18n_t!` → `_tr!`（注入 minify_key）→ 生成的表达式 → `_rust_i18n_try_translate` → `_RUST_I18N_BACKEND.translate`。

**验收标准**：能指出「backend 查表发生在 `_RUST_I18N_BACKEND.translate()`，且只发生在最后一跳」；能解释前两跳分别承担「路由到当前 crate」「注入 minify 配置」的职责。

> 待本地验证。

## 6. 本讲小结

- `t!` 是一个 `#[macro_export]` 的**转发壳**，靠 `crate::_rust_i18n_t!` 把调用路由到**调用者自己 crate** 的内部宏——这是「每个 crate 拥有独立 backend」得以实现的关键。
- `_rust_i18n_t!` 不是手写的，而是 `i18n!` 在编译期生成的 `__rust_i18n_t`（经 `pub(crate) use` 改名），它把 `minify_key` 系列配置**自动注入**每一次 `_tr!` 调用，实现「用户无感生效」。
- `_tr!` 是真正的过程宏，在 `into_token_stream` 里按「是否 minify」「有无参数」生成不同的查找表达式，但**所有分支的查表入口都是同一个** `crate::_rust_i18n_try_translate(locale, key)`。
- **backend 查找发生在最后一跳**：`_rust_i18n_try_translate` 内部的 `_RUST_I18N_BACKEND.translate()`，前面两跳只做路由和配置注入，不碰 backend。
- 整条链靠两处 `crate::`（`t!` 里、`_tr!` 生成代码里）实现「路由到当前 crate 的 backend」，这正是 workspace 中多 crate 各自独立翻译的基础。
- 可以用 `RUST_I18N_DEBUG=1` 把 `i18n!` 生成的全部代码打印出来，逐行验证三跳。

## 7. 下一步学习建议

本讲搞清楚了「调用链的骨架」，但刻意回避了三个深入话题，建议按顺序继续：

1. **`_tr!` 的解析细节** → 下一讲 **u3-l2（`_tr!` 宏的解析与代码生成）**：深入 `Tr`/`Argument`/`Value` 的 syn 解析，以及 `filter_arguments` 如何区分 `locale`、`_minify_key*` 与普通插值参数。
2. **占位符替换与格式化** → **u3-l3（变量插值与格式化说明符）**：精读 `replace_patterns` 的字节级状态机和 `key = value : {:08}` 这种格式说明符怎么拼进 `format!`。
3. **回退策略** → **u3-l4（翻译回退机制）**：本讲只点到 `_rust_i18n_lookup_fallback` 和 `_RUST_I18N_FALLBACK_LOCALE`，回退链的完整顺序和 territory 回退细节在那里详讲。

阅读源码时，建议把本讲的 [crates/macro/src/lib.rs:L411-L432](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L411-L432)（生成宏 + 改名导出）和 [crates/macro/src/tr.rs:L390-L466](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L390-L466)（`into_token_stream`）对照着看，它们共同回答了「`t!` 到底变成了什么代码」。
