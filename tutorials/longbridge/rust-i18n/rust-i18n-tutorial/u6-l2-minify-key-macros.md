# `_minify_key!` 与 `tkv!` 宏

## 1. 本讲目标

上一讲（u6-l1）我们拆解了 minify_key 的算法本身：`SipHasher13` 算 128 位哈希、`base62` 编码、按 `threshold` 决定是否压缩、按 `len`/`prefix` 控制输出形态。本讲回答两个紧接着的问题：

1. **这个算法在哪里、以什么方式被调用？** —— 它被包成一个过程宏 `_minify_key!`，在**编译期**就把字面量算成短键常量。
2. **用户怎么方便地拿到「短键 + 原文」这对值？** —— 通过声明宏 `tkv!`，它返回一个 `(key, msg)` 元组。

学完本讲，你应当能够：

- 看懂 `_minify_key!` 过程宏如何用 `syn` 解析四个**位置参数**，并在编译期调用 `minify_key` 直接产出常量 token；
- 掌握 `tkv!` 声明宏（`macro_rules!`）如何转发到 `i18n!` 编译期生成的内部宏 `__rust_i18n_tkv`，并返回 `(key, msg)` 元组；
- 理解 `__rust_i18n_tkv` 内部宏如何把 `i18n!` 接收到的 minify 配置**透传**给 `_minify_key!`，做到「用户无感生效」；
- 区分 `tkv!` 与 `t!` 在使用 minify_key 时的不同门控机制（阈值 vs. 总开关布尔）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 过程宏（proc-macro）能“在编译期跑代码”

普通函数在**运行期**执行，而过程宏在**编译期**（`cargo build` 展开 `i18n!`/`_tr!` 时）执行，它的返回值会被当成新的 Rust 源码塞回调用处。`_minify_key!` 正是利用这一点：它在编译期就把字符串喂给 `minify_key` 算法，算出的短键直接变成一个字符串字面量常量。这意味着**同一个输入字符串永远得到同一个短键**（因为 `SipHasher13` 是确定性哈希），且这个哈希计算**只发生在编译期一次**，运行期零开销。

### 2.2 声明宏（`macro_rules!`）只是文本替换的“转发壳”

`macro_rules! tkv` 自身不做任何计算，它只负责把调用 `tkv!("xxx")` 原样转发到 `crate::_rust_i18n_tkv!("xxx")`。而 `_rust_i18n_tkv!` 并不是 rust-i18n 库自带的，它是由你调用 `i18n!("locales", ...)` 时**编译期生成**出来的（见 u2-l4）。这个“壳 + 编译期生成内部宏”的套路，和 `t!` → `crate::_rust_i18n_t!` 完全一致（见 u3-l1）。

### 2.3 “配置透传”：把 `i18n!` 的参数埋进生成的宏

你在 `i18n!("locales", minify_key = true, minify_key_len = 24, ...)` 里写的配置，会被 `generate_code` 读出来，再**以字面量形式硬编码**进生成的 `__rust_i18n_tkv` 宏体里。所以后续每次写 `tkv!("...")`，都不必重复传 `len`/`prefix`/`thresh`——它们已经在编译期“焊”好了。

## 3. 本讲源码地图

本讲涉及三个核心源码文件（与 u6-l1 的 `crates/support/src/minify_key.rs` 算法实现相呼应）：

| 文件 | 作用 |
| --- | --- |
| [crates/macro/src/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/minify_key.rs) | `_minify_key!` 过程宏的解析结构 `MinifyKey`：解析四个位置参数、编译期调用 `minify_key` 产出常量。 |
| [src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) | 根 crate 门面：`pub use` 暴露 `_minify_key`；声明宏 `tkv!`（转发壳）。 |
| [crates/macro/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) | `_minify_key` 过程宏入口；`generate_code` 中生成的 `__rust_i18n_t` / `__rust_i18n_tkv` 内部宏及其改名导出。 |

辅助引用（用于实践与验证）：

| 文件 | 作用 |
| --- | --- |
| [crates/support/src/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs) | u6-l1 已讲：`minify_key` 函数与默认常量。 |
| [tests/i18n_minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/i18n_minify_key.rs) | minify_key 的集成测试，含 `test_tkv`。 |
| [examples/app-minify-key/src/main.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs) | 启用 minify_key 的可运行示例。 |

---

## 4. 核心概念与源码讲解

本讲三个最小模块构成一条清晰的调用链：

```
用户写 tkv!("一句长文案")
   │  src/lib.rs 的 macro_rules! tkv（转发壳）
   ▼
crate::_rust_i18n_tkv!("一句长文案")
   │  这是 i18n! 编译期生成的 __rust_i18n_tkv（已改名）
   ▼
rust_i18n::_minify_key!("一句长文案", len, prefix, thresh)   ← 编译期调用 minify_key
   │  返回常量 key；同时 val = 原文
   ▼
(key, msg) 元组
```

### 4.1 `_minify_key!` 过程宏：编译期算出常量 key

#### 4.1.1 概念说明

`_minify_key!` 是一个**函数式过程宏**（`#[proc_macro]`）。它的职责极其单一：接收一个字符串字面量和三个数值/字符串配置，在编译期调用 u6-l1 讲过的 `minify_key` 函数，把算出的短键**直接变成一个字符串字面量 token**返回。

要点：

- **参数是位置参数，不是命名参数**。调用形如 `_minify_key!("This is message", 24, "t_", 4)`，严格按 `(msg, len, prefix, threshold)` 顺序排列，用逗号分隔。这与 `i18n!`、`_tr!` 采用命名参数（`key = value`）的风格不同。
- **计算发生在编译期**。`minify_key` 的哈希、base62 编码、截断、加前缀，全部在 `cargo build` 时完成一次，运行期看到的就是一个写死的常量字符串。
- **确定性**：相同输入恒得相同输出（`SipHasher13` 无随机种子），所以无论你在代码里写几处 `_minify_key!("Hello, world!", ...)`，编译期算出的 key 都一样。

#### 4.1.2 核心流程

`_minify_key!` 从“宏调用”到“常量 token”的流程：

1. **入口**（`crates/macro/src/lib.rs` 的 `_minify_key`）：用 `parse_macro_input!(input as minify_key::MinifyKey)` 把 token 流交给 `MinifyKey::parse`。
2. **解析**（`MinifyKey::parse`）：按固定顺序依次解析 `LitStr`（msg）、`,`、`LitInt`（len）、`,`、`LitStr`（prefix）、`,`、`LitInt`（threshold），填入 `MinifyKey` 结构体。
3. **生成**（`MinifyKey::into_token_stream`）：调用 `rust_i18n_support::minify_key(&self.msg, self.len, &self.prefix, self.threshold)`，把返回的 `Cow<str>` 用 `quote! { #key }` 包成一个字符串字面量 token 流。

由于 `quote!` 会把 `&str` 自动转成带引号的字符串字面量，所以 `_minify_key!("Hello, world!", 24, "", 0)` 在编译期就坍缩成等价于 `"1LokVzuiIrh1xByyZG4wjZ"` 的字面量。

#### 4.1.3 源码精读

**解析结构 `MinifyKey`** 承载四个字段，与 `minify_key` 函数的四个入参一一对应：

[crates/macro/src/minify_key.rs:5-12](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/minify_key.rs#L5-L12) —— `MinifyKey` 结构体，字段 `msg`/`len`/`prefix`/`threshold`，对应算法的四个参数。

**`Parse` 实现**严格按位置解析四个参数（注意每个参数之间都硬性要求一个逗号）：

[crates/macro/src/minify_key.rs:21-38](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/minify_key.rs#L21-L38) —— 这是“位置参数解析”的全部实现：先取字符串字面量 `msg`，再依次取整型 `len`、字符串 `prefix`、整型 `threshold`，没有命名匹配、没有逗号递归，写法比 `_tr!`/`i18n!` 简单得多。

**`into_token_stream` 在编译期调用 `minify_key`**：

[crates/macro/src/minify_key.rs:14-19](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/minify_key.rs#L14-L19) —— 关键两行：`let key = minify_key(...)` 在**编译期**执行算法，`quote! { #key }` 把结果包成字面量 token。这就是“key 在编译期确定”的物理来源。

**过程宏入口**：

[crates/macro/src/lib.rs:436-441](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L436-L441) —— `pub fn _minify_key` 标了 `#[proc_macro]` 与 `#[doc(hidden)]`，仅做 `parse_macro_input!` 分发。

**根 crate 的再导出**（让用户写 `rust_i18n::_minify_key!` 即可）：

[src/lib.rs:5-6](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L5-L6) —— `#[doc(hidden)] pub use rust_i18n_macro::{_minify_key, _tr, i18n};`，过程宏被标为“文档隐藏”，意味着它是内部宏，普通用户通常不直接调用，而是经 `tkv!`/`t!` 间接使用。

#### 4.1.4 代码实践

**实践目标**：验证 `_minify_key!` 在编译期把字面量算成常量，且与运行期调用 `minify_key` 结果一致。

**操作步骤**：

1. 新建一个 crate（或复用 `examples/app-minify-key`），依赖 `rust-i18n`。
2. 在 `main.rs` 顶部写一个最小 `i18n!` 初始化（让 `_minify_key` 符号可用，且复用其 minify 配置默认值）：

```rust
use rust_i18n_support::minify_key;

rust_i18n::i18n!("locales");

fn main() {
    // 编译期：_minify_key! 把 "Hello, world!" 在 build 阶段算成常量
    let compile_time_key = rust_i18n::_minify_key!("Hello, world!", 24, "", 0);
    // 运行期：直接调用 support 的同一个函数
    let runtime_key = minify_key("Hello, world!", 24, "", 0);
    println!("compile-time key = {}", compile_time_key);
    println!("runtime     key = {}", runtime_key);
    assert_eq!(compile_time_key, runtime_key);
}
```

3. 运行 `cargo run`。

**需要观察的现象**：两行打印输出**完全相同**。

**预期结果**：两条都打印 `1LokVzuiIrh1xByyZG4wjZ`（该值来自 u6-l1 的测试断言 [crates/support/src/minify_key.rs:113-122](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L113-L122)）。这说明 `_minify_key!` 只是把运行期函数 `minify_key` 搬到了编译期，算法完全一致。若你对一条中文字符串（如 `"一句足够长的文案"`）做同样验证，两条也会一致——但因为无法手算 `SipHash13`，具体短键值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`_minify_key!("msg", 24, "p_", 4)` 的四个参数分别对应 `minify_key` 函数签名的哪个入参？为什么这里用位置参数而不是命名参数？

> **答案**：依次对应 `value`、`len`、`prefix`、`threshold`。`_minify_key!` 是内部辅助宏，调用点单一（只由 `__rust_i18n_tkv` 调用），位置参数实现最短、解析最简单，不需要 `i18n!`/`_tr!` 那种命名参数 + 逗号递归的复杂度。

**练习 2**：如果把 `_minify_key!` 的实现从“编译期调用 `minify_key`”改成“生成 `rust_i18n::MinifyKey::minify_key(...)` 的运行期调用代码”，会有什么后果？

> **答案**：短键的哈希计算会从编译期推迟到**每次运行期执行**，`t!` 每次查找、`tkv!` 每次取值都要重算一次 `SipHash13` + base62，徒增 CPU 开销；这正是 u6-l3 会讲到的“动态值只能运行期计算”的代价被人为放大了。当前设计把字面量的哈希**前置到编译期**，是关键优化。

---

### 4.2 `tkv!` 声明宏：返回 `(key, msg)` 元组

#### 4.2.1 概念说明

`tkv!`（**t**ranslation **k**ey/**v**alue）是一个面向用户的 `macro_rules!` 声明宏。它解决一个具体痛点：在「文案即 key」模式下，一条长文案既要当 key 去查表、又要当 value 兜底显示，**手写两遍又冗长又易错**。`tkv!` 让你只写一遍，一次拿到 `(key, msg)`：

```rust
let (key, msg) = tkv!("一句长文案");
// key = minify 后的短键（或原文，取决于阈值）
// msg = 原文字面量
```

要点：

- **只接受单个字面量** `$msg:literal`，不接受 `t!` 那样的 `name = value` 插值参数。
- **不翻译**：它返回的 `msg` 就是原始输入字符串，**不是**翻译后的文本（这一点上源码行为与注释描述略有出入，以源码为准）。
- **是转发壳**：自身不计算，转发到 `crate::_rust_i18n_tkv!`（由 `i18n!` 编译期生成）。

#### 4.2.2 核心流程

`tkv!("Hello, world!")` 的展开：

1. `macro_rules! tkv` 匹配 `$msg:literal`，转发为 `crate::_rust_i18n_tkv!("Hello, world!")`。
2. `crate::_rust_i18n_tkv` 实为 `i18n!` 生成的 `__rust_i18n_tkv`（经 `pub(crate) use` 改名），它展开为：

```
{
    let val = "Hello, world!";
    let key = rust_i18n::_minify_key!("Hello, world!", <len>, <prefix>, <thresh>);
    (key, val)
}
```

3. 其中 `rust_i18n::_minify_key!(...)` 在编译期把短键算成常量（见 4.1）；`val` 仍是原文。最终元组 `(key, msg)` 中：**key 是编译期常量、msg 是原文字面量**。

#### 4.2.3 源码精读

**`tkv!` 声明宏（转发壳）**：

[src/lib.rs:173-179](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L173-L179) —— `macro_rules! tkv` 只有一条规则：匹配 `$msg:literal`，转发到 `crate::_rust_i18n_tkv!($msg)`。`crate::` 路由保证它指向调用者所在 crate 的生成宏，与 `t!` 同构（对比 [src/lib.rs:143-147](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L143-L147) 的 `t!`）。

`tkv!` 的文档注释说明了它的用途与返回值约定：

[src/lib.rs:149-172](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L149-L172) —— 注意注释里写 “msg is the translated message” 其实不够准确；按源码 `val = $msg`，msg 就是输入字面量本身（详见 4.3 的生成代码与测试断言）。

**生成的内部宏 `__rust_i18n_tkv`**（重点看它如何产出 `(key, val)`）：

[crates/macro/src/lib.rs:419-432](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L419-L432) —— `macro_rules! __rust_i18n_tkv` 把 `$msg` 绑给 `val`，再调用 `rust_i18n::_minify_key!($msg, #minify_key_len, #minify_key_prefix, #minify_key_thresh)` 算出 `key`，最后返回元组 `(key, val)`。其中 `#minify_key_len` 等是 `quote!` 在 `generate_code` 阶段插值进来的配置字面量（4.3 详述）。末尾 `pub(crate) use __rust_i18n_tkv as _rust_i18n_tkv;` 完成改名，让 `crate::_rust_i18n_tkv!` 能解析到它。

**测试佐证行为**：集成测试 `test_tkv` 给出了可直接对照的期望值：

[tests/i18n_minify_key.rs:52-63](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/i18n_minify_key.rs#L52-L63) —— 该测试在 `i18n!` 配置 `prefix = "t_"`、`len = 24`、`thresh = 4` 下：`tkv!("Hello, world!")` 返回 `("t_1LokVzuiIrh1xByyZG4wjZ", "Hello, world!")`；而 `tkv!("Hey")`（3 字符 ≤ 阈值 4）返回 `("Hey", "Hey")`，`tkv!("")` 返回 `("", "")`。这清楚说明：**msg 恒为原文，key 是否被压缩由阈值决定**。

#### 4.2.4 代码实践

**实践目标**：用 `tkv!("一句足够长的文案")` 生成并打印 `(key, msg)`，并解释为什么 key 能在编译期确定、msg 仍是原文。

**操作步骤**：

1. 复用 `examples/app-minify-key`（或新建 crate），其 `i18n!` 已配置 `minify_key = true, minify_key_len = 24, minify_key_prefix = "mytr_", minify_key_thresh = 4`（见 [examples/app-minify-key/src/main.rs:3-9](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs#L3-L9)）。
2. 在 `main` 中加入：

```rust
use rust_i18n::tkv;

// 注意：中文字符串长度按字节计，"一句足够长的文案" 远超阈值 4，会被压缩
let (key, msg) = tkv!("一句足够长的文案");
println!("key = {}", key);   // 以 mytr_ 为前缀的 24 位短键
println!("msg = {}", msg);   // 一句足够长的文案
```

3. 运行 `cargo run -p app-minify-key`（或对应包名）。

**需要观察的现象**：

- `msg` 一行打印**原文**「一句足够长的文案」；
- `key` 一行打印一个以 `mytr_` 开头的 24 位字符串（含前缀共 29 字符）。

**预期结果**：

- `msg == "一句足够长的文案"`（由 `let val = $msg` 决定，恒为原文）。
- `key` 形如 `mytr_xxxxxxxxxxxxxxxxxxxxxxxx`。因无法手算 `SipHash13`，具体字符**待本地验证**。

**为什么 key 在编译期确定、msg 仍是原文？**

- key 在编译期确定：`tkv!` → `_rust_i18n_tkv!` → `rust_i18n::_minify_key!(...)`，而 `_minify_key!` 是过程宏，它在编译期执行 `minify_key`（4.1.3 的 `into_token_stream`），把哈希结果变成字面量常量。`"一句足够长的文案"` 是编译期已知的字面量，故整个 `minify_key` 计算可在编译期完成。
- msg 仍是原文：生成代码里 `let val = $msg;` 只是把字面量原样绑定，没有任何计算或翻译（4.2.3 的生成宏体）。`tkv!` 本身不查表、不翻译，它的定位是“帮你同时拿到 key 和原文”，方便你把这对值写进翻译文件或用于按 key 查找。

#### 4.2.5 小练习与答案

**练习 1**：在 `minify_key = false`（默认）的 `i18n!` 配置下，`tkv!("Hello, world!")` 的 `key` 会是什么？为什么？

> **答案**：取决于阈值。`tkv!` 调用链里 `__rust_i18n_tkv` **总是**调用 `_minify_key!`，并不检查 `minify_key` 这个布尔总开关；是否压缩由 `_minify_key!` → `minify_key` 内部的 `value.len() <= threshold` 判定。默认 `threshold = 127`（[crates/support/src/minify_key.rs:15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L15)），`"Hello, world!"`（13 字节）≤ 127，故 `minify_key` 短路返回原文，`key = "Hello, world!"`。**这是 `tkv!` 与 `t!` 的关键差异**：`t!` 受布尔总开关门控（见 4.3 末尾），`tkv!` 只受阈值门控。

**练习 2**：为什么 `tkv!` 的规则是 `($msg:literal)` 而不是像 `t!` 那样 `($($all:tt)*)`？

> **答案**：`tkv!` 只接收一个要算 key 的字面量，不支持插值参数；`_minify_key!` 也只接受字面量（见 4.1）。限定 `literal` 能在宏层面就拒绝 `tkv!(format!(...))` 这类动态值——因为动态值无法在编译期算 key。`t!` 需要支持插值和动态消息，故用 `tt` 通配。

---

### 4.3 `__rust_i18n_tkv` 内部宏：把 `i18n!` 的 minify 配置透传给 `_minify_key!`

#### 4.3.1 概念说明

`__rust_i18n_tkv` 与 `__rust_i18n_t` 一样，**不是源码里写死的宏，而是 `i18n!` 在编译期生成的**。它的核心使命是“配置透传”：你在 `i18n!(..., minify_key_len = 24, minify_key_prefix = "mytr_", minify_key_thresh = 4)` 写的三个参数，会被 `generate_code` 读出，再**以字面量硬编码**进宏体，使后续每次 `tkv!`/`t!` 调用都自动带上这套配置——用户无需重复传递。

要点：

- 配置来源是 `i18n!` 的 `Args`（经三级优先级：硬编码默认 < `Cargo.toml` metadata < 显式宏参数，见 u2-l1/u5-l1）。
- 透传方式是 `quote!` **插值**：在生成宏体时把配置值当字面量嵌进去。
- 生成后通过 `pub(crate) use __rust_i18n_tkv as _rust_i18n_tkv;` 改名，使 `crate::_rust_i18n_tkv!` 能解析。

#### 4.3.2 核心流程

`generate_code` 中与 minify_key 配置透传相关的步骤：

1. 从 `args`（`i18n!` 解析出的参数结构）取出 `minify_key_len`/`minify_key_prefix`/`minify_key_thresh`（以及布尔 `minify_key`）。
2. 把这些值绑定到本地同名变量，供 `quote!` 插值。
3. 在生成的 `__rust_i18n_t` 宏里，把 `#minify_key`/`#minify_key_len`/`#minify_key_prefix`/`#minify_key_thresh` 作为**命名系统参数** `_minify_key = ...` 注入 `_tr!` 调用。
4. 在生成的 `__rust_i18n_tkv` 宏里，把 `#minify_key_len`/`#minify_key_prefix`/`#minify_key_thresh` 作为**位置参数**直接传给 `_minify_key!`。
5. 同时生成四个静态量 `_RUST_I18N_MINIFY_KEY(_LEN/_PREFIX/_THRESH)`，记录这套配置（测试可断言）。
6. 改名导出 `__rust_i18n_t` → `_rust_i18n_t`、`__rust_i18n_tkv` → `_rust_i18n_tkv`。

两条透传路径对比：

| 维度 | `__rust_i18n_t`（服务 `t!`） | `__rust_i18n_tkv`（服务 `tkv!`） |
| --- | --- | --- |
| 下游宏 | `rust_i18n::_tr!` | `rust_i18n::_minify_key!` |
| 传参方式 | 命名系统参数 `_minify_key = ...` | 位置参数 `(msg, len, prefix, thresh)` |
| 是否传布尔总开关 | 传（`_minify_key = #minify_key`） | **不传**（靠阈值门控） |
| 门控机制 | `t!` 在 `_tr!` 内先查 `self.minify_key` 布尔 | `tkv!` 由 `minify_key` 函数的阈值判定 |

#### 4.3.3 源码精读

**第一步：从 `args` 取出配置并绑定**：

[crates/macro/src/lib.rs:329-333](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L329-L333) —— `generate_code` 把 `args.minify_key(_len/_prefix/_thresh)` 绑到本地变量，这些变量随后被 `quote!` 插值进生成的代码。

**第二步：生成配置静态量**（供测试与运行期反射）：

[crates/macro/src/lib.rs:350-353](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L350-L353) —— 生成 `static _RUST_I18N_MINIFY_KEY: bool`、`_RUST_I18N_MINIFY_KEY_LEN: usize`、`_RUST_I18N_MINIFY_KEY_PREFIX: &str`、`_RUST_I18N_MINIFY_KEY_THRESH: usize`，其值即 `i18n!` 配置。集成测试正是断言它们（[tests/i18n_minify_key.rs:15-20](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/i18n_minify_key.rs#L15-L20)）。

**第三步：`__rust_i18n_t` 透传（命名系统参数）**：

[crates/macro/src/lib.rs:411-417](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L411-L417) —— 生成的 `__rust_i18n_t` 在转发到 `_tr!` 时，**自动追加**四个下划线前缀的系统参数 `_minify_key = #minify_key, _minify_key_len = ..., _minify_key_prefix = ..., _minify_key_thresh = ...`。这就是 `t!` 调用者“无感生效”的来源（u3-l1 已讲）。

**第四步：`__rust_i18n_tkv` 透传（位置参数，注意不传布尔）**：

[crates/macro/src/lib.rs:419-429](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L419-L429) —— 生成的 `__rust_i18n_tkv` 把 `$msg` 同时绑给 `val` 与传给 `_minify_key!`，并**以位置参数**填入 `#minify_key_len`、`#minify_key_prefix`、`#minify_key_thresh`。注意它**没有**把布尔 `minify_key` 传进去——`_minify_key!` 也只接受 4 个参数。是否压缩完全交给 `minify_key` 函数的阈值判定（[crates/support/src/minify_key.rs:39-46](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L39-L46)）。这也是 4.2.5 练习 1 的根因。

**第五步：改名导出**：

[crates/macro/src/lib.rs:431-432](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L431-L432) —— `pub(crate) use __rust_i18n_t as _rust_i18n_t;` 和 `pub(crate) use __rust_i18n_tkv as _rust_i18n_tkv;`。这一步把内部宏名改成 `t!`/`tkv!` 转发壳里写定的 `crate::_rust_i18n_t!` / `crate::_rust_i18n_tkv!`，闭合整条转发链。

**对照 `t!` 侧的布尔门控**：`_tr!` 的 `into_token_stream` 中，minify 分支以 `if self.minify_key && ...` 开头：

[crates/macro/src/tr.rs:390-413](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L390-L413) —— `t!`（经 `_tr!`）先检查 `self.minify_key` 布尔总开关，只有为真才进入 minify 分支；这与 `tkv!` 的阈值门控形成鲜明对比。布尔总开关经 [tr.rs:348-359](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L348-L359) 的 `filter_arguments` 从 `_minify_key` 系统参数解析得到。

#### 4.3.4 代码实践

**实践目标**：用 `RUST_I18N_DEBUG=1` 直接“看见” `i18n!` 生成的 `__rust_i18n_tkv` 内部宏，验证配置透传。

**操作步骤**：

1. 进入 `examples/app-minify-key`（它的 `i18n!` 配置了 `len=24, prefix="mytr_", thresh=4`）。
2. 用调试环境变量编译：

```bash
RUST_I18N_DEBUG=1 cargo build -p app-minify-key 2>&1 | tee /tmp/i18n-debug.txt
```

3. 在 `/tmp/i18n-debug.txt` 中搜索 `__rust_i18n_tkv` 与 `__rust_i18n_t` 两段生成代码。

**需要观察的现象**：`-------------- code --------------` 分隔块内会打印 `generate_code` 的产物，其中应能看到：

- `static _RUST_I18N_MINIFY_KEY_PREFIX: &str = "mytr_";`（与配置一致）；
- `__rust_i18n_tkv` 宏体里 `_minify_key!( ... , 24, "mytr_", 4)` —— 三个配置已被**插值成字面量**；
- `__rust_i18n_t` 宏体里 `_tr!( ... , _minify_key = true, _minify_key_len = 24, _minify_key_prefix = "mytr_", _minify_key_thresh = 4)`。

**预期结果**：生成代码中，`mytr_`、`24`、`4` 这三个配置值都已被“焊”进两个内部宏，证明透传成功。若改 `i18n!` 里的 `minify_key_prefix = "abc_"` 后重新编译，生成代码里的前缀字面量应随之变为 `"abc_"`。

> 提示：`RUST_I18N_DEBUG` 只有精确等于 `"1"` 时才生效（见 u5-l2 的 `is_debug`）。该变量由 `generate_code` 末尾的 `if is_debug() { println!(...) }` 触发（[crates/macro/src/lib.rs:260-265](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L260-L265)）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `i18n!` 里的 `minify_key_prefix` 改成新值，但不重新编译，`tkv!` 会用新前缀还是旧前缀？

> **答案**：用**旧前缀**。`__rust_i18n_tkv` 的前缀是 `i18n!` 在**上一次编译期**插值生成的字面量，已固化进二进制。改 `Cargo.toml` 或 `i18n!` 参数后必须重新 `cargo build`（修改 `i18n!` 本身会触发重编译；若是改 `Cargo.toml` 的 metadata，配合 build.rs 的 rerun 机制，见 u5-l2）。这也再次印证“配置在编译期焊死”。

**练习 2**：为什么 `__rust_i18n_tkv` 不像 `__rust_i18n_t` 那样把布尔 `_minify_key` 也传下去？

> **答案**：`_minify_key!` 的签名只接受 4 个位置参数（4.1），没有布尔开关的位置；且 `minify_key` 函数本身就用阈值判定是否压缩（短串原样返回）。`tkv!` 的语义是“给我这对 key/msg”，是否压缩由阈值决定即可，不需要额外布尔门。`t!` 则不同——它要决定“是否把消息当 minify key 去查表”，这是个独立于阈值的策略选择，故需要布尔总开关。

---

## 5. 综合实践

设计一个把本讲三个模块串起来的小任务：**验证 `tkv!` 与 `t!` 对同一字面量产出相同的 key，从而能用 `tkv!` 的 key 给 `t!` 准备翻译**。

**背景**：在「文案即 key + minify」模式下，`t!("一句长文案")` 实际查表用的 key 是 minify 后的短键。你若想手动往翻译文件里加这条译文，必须知道这个短键是什么——`tkv!` 正好能告诉你。

**步骤**：

1. 复用 `examples/app-minify-key`（`prefix="mytr_", len=24, thresh=4`），在 `main.rs` 加入：

```rust
use rust_i18n::{t, tkv};

fn main() {
    // ... 原有代码 ...

    let long_msg = "This is a sufficiently long message for minify key demo!";
    // (1) tkv! 同时拿到 key 和原文
    let (key, msg) = tkv!(long_msg);
    println!("tkv!  => key = {:?}, msg = {:?}", key, msg);

    // (2) t! 对同一字面量内部也用相同配置算 key 去查表
    let translated = t!(long_msg, locale = "en");
    println!("t!    => {}", translated);

    // (3) 关键断言：t! 查表用的 key 与 tkv! 给出的 key 一致
    //     （即：若你把译文以这个 key 写进 locales，t! 就能命中）
    assert_eq!(key, /* t! 内部算出的 key */);
}
```

2. 因为无法直接拿到 `t!` 内部的 key 变量，改用**间接验证**：往 `examples/app-minify-key/locales/en.yml` 里，用 `tkv!` 打印出的 `key` 作为键，写入一条译文，例如：

```yaml
en:
  mytr_xxxxxxxxxxxxxxxxxxxxxxxx: "这是手动补的译文"
```

   （具体短键以第 1 步 `tkv!` 打印值为准，**待本地验证**填入。）

3. 重新运行 `cargo run -p app-minify-key`，观察 `t!(long_msg, locale = "en")` 是否返回你写入的译文。

**预期结果**：

- 第 1 步：`msg` 为原文，`key` 为 `mytr_` 开头的 24 位短键。
- 第 3 步：`t!(long_msg)` 命中你手填的译文，证明 **`tkv!` 的 key 与 `t!` 的查表 key 一致**——这正是下一讲（u6-l3）要展开的核心命题：**`t!`、`tkv!`、CLI 提取器三者使用同一套 minify_key 参数，才能保证 key 全局一致**。

**如果运行失败**：最常见原因是译文键填错（手算不出 `SipHash13`）。务必以 `tkv!` 实际打印的 key 为准，不要自己猜。

---

## 6. 本讲小结

- `_minify_key!` 是函数式过程宏，用 `syn` 按**位置**解析 `(msg, len, prefix, threshold)` 四个参数，并在**编译期**调用 u6-l1 的 `minify_key` 函数，把短键变成字面量常量 token；输入恒定则输出恒定（确定性哈希）。
- `tkv!` 是 `macro_rules!` 转发壳，只接受单个 `$msg:literal`，转发到 `i18n!` 编译期生成的 `crate::_rust_i18n_tkv!`，返回 `(key, msg)` 元组：**key 在编译期确定（经 `_minify_key!`），msg 恒为原文字面量**，用于“长文案只写一次”的场景。
- `__rust_i18n_tkv` 由 `generate_code` 生成，通过 `quote!` 插值把 `i18n!` 的 `minify_key_len/prefix/thresh` **以位置参数字面量透传**给 `_minify_key!`，再经 `pub(crate) use` 改名为 `_rust_i18n_tkv`，闭合 `tkv!` 的转发链。
- **`tkv!` 与 `t!` 的门控差异**：`t!`（经 `_tr!`）受布尔总开关 `minify_key` 门控（`if self.minify_key && ...`）；`tkv!` 总是调用 `_minify_key!`，是否压缩仅由 `minify_key` 函数的**阈值**判定（`value.len() <= threshold` 短路返回原文）。
- 配置在**编译期焊死**：`__rust_i18n_tkv`/`__rust_i18n_t` 里的参数字面量是上一次 build 生成的，改配置必须重新编译；可用 `RUST_I18N_DEBUG=1` 直接查看生成代码验证透传。
- 三条调用链共享同一套 minify_key 配置：`tkv!` → `__rust_i18n_tkv` → `_minify_key!`；`t!` → `__rust_i18n_t` → `_tr!`（内部对字面量分支调 `MinifyKey::minify_key`）；二者对同一字面量产出相同 key——这是与提取器协作（u6-l3）的基础。

## 7. 下一步学习建议

- **下一讲 u6-l3《短键在 `t!` 与提取器中的协作》** 将把本讲的 `tkv!`/`_minify_key!` 与 `t!` 的三条 minify 分支、以及 `cargo i18n` 提取器联系起来，讲清“为什么 `t!`、`tkv!`、提取器三者必须用同一套参数，否则 key 对不上”。建议重点阅读 [crates/macro/src/tr.rs:390-413](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L390-L413) 的字面量/元组/动态值三分支，以及 `crates/extract/src/extractor.rs` 中提取器调用 `minify_key` 的位置。
- 若想加深对“编译期 vs 运行期”开销的理解，可对照本讲 4.1（字面量编译期算）与 u6-l3 将讲的“动态值只能运行期算”，体会 rust-i18n 为何把字面量哈希前置到编译期。
- 推荐继续阅读源码：`generate_code` 全貌（[crates/macro/src/lib.rs:270-434](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L270-L434)）把本讲三个模块置于完整的代码生成上下文中；以及集成测试 [tests/i18n_minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/i18n_minify_key.rs) 作为可运行的参考用例。
