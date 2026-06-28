# Backend trait 与 SimpleBackend

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `Backend` trait 作为「翻译存储抽象」的三个方法各自返回什么、为什么都返回 `Cow`。
- 看懂 `SimpleBackend` 的嵌套 `HashMap` 数据结构，以及 `translate` 的两层查找过程。
- 理解 `add_translations` 为什么可以反复调用、同名键会发生什么。
- 明白一个关键结论：**`i18n!` 在编译期生成的那个「静态后端」，本质上就是一个 `SimpleBackend`**，只是被装进了 `Box<dyn Backend>`。
- 不依赖 `i18n!` 宏，也能独立构造一个 `SimpleBackend` 并用它做翻译查找。

本讲是「后端抽象与扩展」单元的起点。它把前面 u2-l4 看到的「生成代码里的 `_RUST_I18N_BACKEND`」往下拆一层，告诉你那个后端到底是什么类型、怎么存数据、怎么查。

## 2. 前置知识

阅读本讲前，建议你已经理解：

- **trait（特征）**：Rust 里定义一组方法签名的接口，类型通过 `impl Trait for Type` 来实现它。本讲的 `Backend` 就是一个 trait。
- **`Cow<'a, str>`（写时复制智能指针）**：一个既可以「借用」也可以「拥有」的字符串。命中已有字面量时返回 `Cow::Borrowed`（零拷贝），需要新生成时返回 `Cow::Owned`。这是 rust-i18n 在「翻译命中即零拷贝」上的关键手段。
- **`HashMap` 的两层嵌套**：`HashMap<K, HashMap<K2, V>>`，即「外层按一个键查到一张内层表，再在内层表里按第二个键查值」。
- **`Send + Sync + 'static`**：Rust 的线程安全约束。`Send` 表示可以在线程间转移所有权，`Sync` 表示可以在线程间共享引用，`'static` 表示不持有任何非静态引用。因为后端要被放进全局静态变量、被多线程并发访问，所以它必须满足这三条。
- u2-l4 已讲解的 `generate_code` 输出，特别是 `_RUST_I18N_BACKEND` 与 `_rust_i18n_try_translate`。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| [crates/support/src/backend.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs) | 定义 `Backend` trait、`BackendExt`、`CombinedBackend`、`SimpleBackend` 及其所有实现与单元测试，是本讲的核心。 |

为说明「生成的静态后端就是 SimpleBackend」，还会引用两处旁证：

| 文件 | 作用 |
| --- | --- |
| [crates/macro/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) | `generate_code` 中生成 `let mut backend = SimpleBackend::new()` 并调用 `add_translations` 的代码片段。 |
| [src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) | 根 crate 通过 `pub use` 把 `Backend`、`SimpleBackend` 等对外暴露。 |

> 关键事实：`Backend` / `SimpleBackend` 是**纯运行时类型**，定义在 `rust-i18n-support` 里，由根 crate `pub use` 暴露。它们**不依赖 `i18n!` 宏**，可以脱离宏独立使用——这一点是本讲代码实践的基础。

## 4. 核心概念与源码讲解

### 4.1 Backend trait：翻译存储的抽象

#### 4.1.1 概念说明

到目前为止，我们已经知道 `t!` 最终会去一个「静态后端」里查翻译。但「后端」到底是什么形状？谁来决定它能做什么？

`Backend` trait 就是回答这个问题的契约。它把「翻译存储」这件事抽象成三个最基本的能力：

1. **能列出我有哪些语言**（`available_locales`）。
2. **能按「语言 + 键」精确查一条译文**（`translate`）。
3. **能列出某种语言下的全部键值对**（`messages_for_locale`）。

只要一个类型实现了这三个方法，它就是一个合法的翻译后端。这意味着：你可以把翻译放在内存 `HashMap` 里（`SimpleBackend`），也可以放在远程数据库、Redis、或者任何自定义数据源——只要实现 `Backend` 即可（自定义后端实战见 u4-l3）。

一个容易混淆的点：**`Backend::translate` 只做「精确命中」的查找，它不负责回退（fallback）**。回退链（territory 回退、显式 fallback 列表）是由 `i18n!` 生成的 `_rust_i18n_try_translate` 在外部反复调用 `backend.translate` 来编排的（详见 u3-l4）。也就是说，trait 只暴露「一锤子精确查询」，回退是调用方的策略。把「存储」与「回退策略」解耦，是这个设计的核心动机。

#### 4.1.2 核心流程

`Backend` trait 的调用关系（从 `t!` 的角度回顾）：

```
t!("hello")
  └─ _rust_i18n_try_translate(locale, key)        ← i18n! 生成的函数，负责回退编排
       ├─ _RUST_I18N_BACKEND.translate(locale, key)   ← 第 1 次：精确查
       │     （此处就是 Backend trait 的 translate 方法）
       ├─ 若 miss：territory 回退，逐级再调 backend.translate(...)
       └─ 若仍 miss：遍历显式 fallback 列表，逐个 backend.translate(...)
```

trait 自身的三个方法签名可以概括为下表：

| 方法 | 输入 | 输出 | 语义 |
| --- | --- | --- | --- |
| `available_locales` | 无 | `Vec<Cow<'_, str>>` | 列出所有可用语言 |
| `translate` | `locale`, `key` | `Option<Cow<'_, str>>` | 精确查一条；找不到返回 `None` |
| `messages_for_locale` | `locale` | `Option<Vec<(Cow, Cow)>>` | 该语言下全部 `(键, 值)`；无此语言返回 `None` |

为什么三个方法的返回值都用 `Cow<'_, str>`？因为这样实现者既能返回借用的静态字面量（`Cow::Borrowed`，零拷贝、零分配），也能返回运行时拼接的新字符串（`Cow::Owned`）。对 `SimpleBackend` 这种「值就是编译期字面量」的后端，`translate` 命中时走的就是零拷贝路径。

#### 4.1.3 源码精读

trait 的定义非常短：

[crates/support/src/backend.rs:4-12](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L4-L12) 定义了 `Backend` trait，要求实现者满足 `Send + Sync + 'static`，并声明三个方法。

其中两点值得展开：

- `pub trait Backend: Send + Sync + 'static`：这个超职约束（supertrait bound）保证任何 `Backend` 都能被放进全局静态变量 `_RUST_I18N_BACKEND: LazyLock<Box<dyn Backend>>` 并被多线程并发访问（线程安全机制详见 u8-l1）。如果你的自定义后端内部用了 `Rc`、`RefCell` 这类非线程安全类型，会在 `impl Backend` 时被编译器拒绝。
- 三个方法的返回类型都带生命周期 `'_`，指「借自 `&self`」。`SimpleBackend` 的实现里实际是克隆 `Cow`（克隆一个 `Cow::Borrowed` 只复制一个引用，不复制底层字符串），因此命中路径依然廉价。

#### 4.1.4 代码实践

**实践目标**：感受 trait 的「契约」本质——只引入 trait，不构造任何后端，确认三个方法的签名。

**操作步骤**：

1. 在一个依赖了 `rust-i18n` 的 crate 里写一个**永远 false 的编译期检查函数**，仅为了让编译器解析 trait 方法签名：

```rust
// 示例代码：仅用于阅读签名，不会被调用
use rust_i18n::Backend;

fn _signatures<B: Backend>(b: &B) {
    let _locales: Vec<_> = b.available_locales();
    let _one = b.translate("en", "hello");
    let _all = b.messages_for_locale("en");
}
```

2. `cargo check` 通过即说明你对三个方法签名的理解与源码一致。

**需要观察的现象**：把 `translate("en", "hello")` 赋值给变量时，IDE 推断出的类型是 `Option<Cow<'_, str>>`；把 `available_locales()` 推断为 `Vec<Cow<'_, str>>`。

**预期结果**：编译通过，类型推断与上表一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Backend` trait 要加 `Send + Sync` 约束？去掉会怎样？

**参考答案**：因为生成的 `_RUST_I18N_BACKEND` 是 `LazyLock<Box<dyn Backend>>` 这样的全局静态变量，会被多个线程并发调用（`t!` 在任何线程都能用）。`Send + Sync` 是「能安全地跨线程共享」的编译期保证。去掉约束后，把 `SimpleBackend` 装进 `Box<dyn Backend>` 放进 `LazyLock` 时会编译报错，因为 `dyn Backend` 不再保证线程安全。

**练习 2**：`Backend::translate` 找不到键时返回什么？这个 `None` 最终会被谁处理？

**参考答案**：返回 `None`。这个 `None` 不会直接变成给用户的文本，而是被 `_rust_i18n_try_translate`（见 u3-l4）接住，去尝试 territory 回退和显式 fallback 列表；全部 miss 后，`t!` 的入口 `_rust_i18n_translate` 才会把兜底文本（key 本身或 `locale.key`）返回给用户。

---

### 4.2 SimpleBackend：嵌套 HashMap 的默认实现

#### 4.2.1 概念说明

`SimpleBackend` 是 `Backend` 的「参考实现」，也是 `i18n!` 默认生成的后端类型。它的存储思路极其朴素：用一个**两层嵌套的 `HashMap`** 把所有翻译都放在内存里。

数据结构是：

```
HashMap<locale, HashMap<key, value>>
   外层键: 语言代码，如 "en"、"zh-CN"
   内层表: 该语言下的 键 → 译文
```

这种「外层按语言、内层按键」的结构，恰好对应 `translate(locale, key)` 的两次查找：先定位语言，再定位键。由于 `HashMap` 查找的平均时间复杂度是常数级（哈希碰撞忽略不计时为 \(O(1)\)），`translate` 命中路径非常快。

注意三层都用了 `Cow<'static, str>`：

- `'static` 表示这些字符串要么是编译期字面量（`&'static str`），要么是被提升为拥有所有权的 `String`（同样可以当 `'static`）。
- 这正是 `i18n!` codegen 能做到「译文以字面量形式编入二进制、命中零拷贝」的根基——codegen 时插入的 `"Hello"` 是 `&'static str`，被包成 `Cow::Borrowed`。

#### 4.2.2 核心流程

`SimpleBackend` 实现 `Backend` 的三个方法，逻辑都很直接：

```
available_locales():
    取外层 HashMap 的所有 key → 克隆成 Vec → 排序后返回

translate(locale, key):
    1. translations.get(locale)        // 第 1 层：找语言，返回内层表
    2. 若找到内层表：内层表.get(key)    // 第 2 层：找键，返回译文
       并 .cloned() 复制 Cow
    3. 语言不存在 → 返回 None

messages_for_locale(locale):
    translations.get(locale)           // 找语言
    把内层表的所有 (k, v) 克隆进一个 Vec 返回
    语言不存在 → None
```

`translate` 的查找复杂度：

\[
\text{translate}(locale, key) \approx O(1) \text{（两次 HashMap 查找）}
\]

这也是为什么 rust-i18n 在运行时查表极快——它不解析文件、不遍历，只是两次哈希查找。

#### 4.2.3 源码精读

**结构体定义**：

[crates/support/src/backend.rs:69-72](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L69-L72) 声明 `SimpleBackend`，唯一字段 `translations` 是两层嵌套 HashMap，注意注释里强调「key 是 flatten key（点号扁平键，如 `en.hello.world`）」——这与 u2-l3 的扁平化产物对齐。

**`new` 构造空后端**：

[crates/support/src/backend.rs:96-102](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L96-L102) 提供 `SimpleBackend::new()`，初始一个空 `HashMap`。注意 `translations` 字段是私有的，外部只能通过 `new()` + `add_translations` 构造，无法直接写字段。

**`impl Backend for SimpleBackend`**：

[crates/support/src/backend.rs:125-145](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L125-L145) 是三个方法的实现，对应上文流程。重点看 `translate`（L132-L138）：先 `self.translations.get(locale)` 拿内层表，再用 `?`/`if let Some` 在内层表 `trs.get(key).cloned()`。`.cloned()` 在这里是克隆 `Cow`：若译文是 `Cow::Borrowed(&'static str)`，克隆只是复制一个指针，不复制字符串本体。

另外，[crates/support/src/backend.rs:147](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L147) 给 `SimpleBackend` 实现了空 `impl BackendExt for SimpleBackend {}`——这给本 backend 额外赠送了一个 `extend` 方法（用于组合后端，是 u4-l2 的主题），本讲暂不展开。

#### 4.2.4 代码实践

**实践目标**：手动构造一个 `SimpleBackend`，验证 `translate` 的两层查找与 miss 行为。

**操作步骤**：

在依赖 `rust-i18n` 的 crate 里新增一个测试（无需调用 `i18n!`）：

```rust
// 示例代码：独立使用 SimpleBackend，不经过 i18n! 宏
use std::borrow::Cow;
use std::collections::HashMap;
use rust_i18n::{Backend, SimpleBackend};

#[test]
fn simple_backend_manual_lookup() {
    let mut backend = SimpleBackend::new();

    let mut en = HashMap::new();
    en.insert(Cow::from("hello"), Cow::from("Hello"));
    en.insert(Cow::from("bye"), Cow::from("Goodbye"));
    backend.add_translations("en".into(), en);

    // 命中：两次 HashMap 查找
    assert_eq!(backend.translate("en", "hello"), Some(Cow::from("Hello")));
    // 语言存在但键不存在 → None
    assert_eq!(backend.translate("en", "missing"), None);
    // 语言不存在 → None
    assert_eq!(backend.translate("fr", "hello"), None);
    // 可用语言按字典序排列
    assert_eq!(backend.available_locales(), vec![Cow::from("en")]);
}
```

**需要观察的现象**：注意「语言存在但键不存在」和「语言不存在」两种 miss，`translate` 都返回 `None`——从 trait 层面看不出区别，回退策略要在外部判断。

**预期结果**：`cargo test simple_backend_manual_lookup` 通过。

> 提示：本测试直接脱胎于 [backend.rs:163-185](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L163-L185) 的官方单元测试 `test_simple_backend`，你可以对照阅读。

#### 4.2.5 小练习与答案

**练习 1**：`translate` 命中时调用的 `.cloned()`，复制的是整个译文字符串，还是只复制一个指针？

**参考答案**：只复制一个指针（前提是译文以 `Cow::Borrowed(&'static str)` 形式存储）。`Cow::cloned` 在内部是 `Borrowed` 时返回一个新的 `Borrowed`，共享底层字符串；只有 `Owned` 时才会克隆 `String`。因此 codegen 注入的字面量译文在命中路径上是零拷贝的。

**练习 2**：`available_locales` 为什么要 `sort()`？

**参考答案**：因为 `HashMap` 的迭代顺序不确定（取决于哈希），不排序的话每次调用返回的语言顺序会变，既不利于测试断言，也不利于对外呈现稳定的列表。排序后顺序确定，[backend.rs:184](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L184) 的断言 `vec!["en", "zh-CN"]` 才能稳定成立。

---

### 4.3 add_translations 与 i18n! 生成的静态后端

#### 4.3.1 概念说明

光有 `new()` 只能得到一个空后端。真正把翻译「灌」进去的方法是 `add_translations(locale, data)`。它的关键特性是：**可以反复调用，且对同一语言会「合并」而非「覆盖整张语言表」**。

这听起来是个小细节，但它是 `i18n!` codegen 能优雅工作的前提。回顾 u2-l3 的产物：编译期得到的是一张 `BTreeMap<locale, BTreeMap<key, value>>`，按语言分组。codegen 要把这张表灌进后端，最自然的写法就是「遍历每种语言，调用一次 `add_translations`」。如果 `add_translations` 是「整表覆盖」，多次调用同一语言就会丢数据；正因为它是「合并」，这种逐语言灌入的写法才安全。

而合并时遇到同名键怎么办？答案是 **last-write-wins（后写覆盖）**——这正是用户期望的：后 `add_translations` 进来的同名键覆盖先来的。

#### 4.3.2 核心流程

`add_translations` 的合并逻辑用伪代码表示：

```
add_translations(locale, data):
    trs = translations.entry(locale)       // 找到或新建该语言的内层表
               .or_default()               // 不存在则插入空 HashMap
    trs.extend(data)                       // 把 data 的所有 (k,v) 并入
```

`HashMap::extend` 的语义就是「逐个 insert，遇到已有键则覆盖」——天然实现 last-write-wins。

`i18n!` 生成的静态后端则把这些串起来（这是 u2-l4 的回顾）：

```
// 由 generate_code 生成（macro/src/lib.rs）
static _RUST_I18N_BACKEND: LazyLock<Box<dyn Backend>> = LazyLock::new(|| {
    let mut backend = SimpleBackend::new();          // 空 SimpleBackend
    backend.add_translations(/* en 的键值表 */);      // 逐语言灌入
    backend.add_translations(/* zh-CN 的键值表 */);
    ...
    Box::new(backend)                                // 装箱为 dyn Backend
});
```

运行时 `_rust_i18n_try_translate` 每次都调用 `_RUST_I18N_BACKEND.translate(locale, key)`，也就是走到上面这个 `SimpleBackend` 的 `translate`。

#### 4.3.3 源码精读

**`add_translations` 方法**：

[crates/support/src/backend.rs:115-122](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L115-L122) 是核心：`entry(locale).or_default()` 拿到（或新建）内层表，`extend(data)` 合并。其上方的文档注释（[L106-L114](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L106-L114)）就给了一个最小用法示例，正是本讲实践的范本。

**codegen 如何调用它**：

[crates/macro/src/lib.rs:290-296](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L290-L296) 生成 `let mut backend = rust_i18n::SimpleBackend::new();`，随后用 `#( backend.add_translations(#all_translations); )*` 对每种语言展开一次 `add_translations`。这直接印证了「生成的静态后端就是 `SimpleBackend`」。

**静态后端的类型与装箱**：

[crates/macro/src/lib.rs:341-347](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L341-L347) 把上面构造好的 `backend` 装进 `static _RUST_I18N_BACKEND: LazyLock<Box<dyn rust_i18n::Backend>>`，`LazyLock` 保证首次访问时才初始化（把所有 `add_translations` 跑一遍），之后 `Box<dyn Backend>` 让这个静态量可以容纳任意后端类型——这正是后续 u4-l3「自定义后端」能接入的入口。

**查表入口**：

[crates/macro/src/lib.rs:385-386](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L385-L386) 中 `_rust_i18n_try_translate` 的第一行 `_RUST_I18N_BACKEND.translate(locale, key.as_ref())`，就是走到 `SimpleBackend::translate`（即 4.2 讲的两层查找）。

**根 crate 的导出**：

[src/lib.rs:7-9](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L7-L9) 把 `Backend`、`SimpleBackend` 等从 `rust_i18n_support` 重新导出，所以我们才能写 `use rust_i18n::{Backend, SimpleBackend};`。

#### 4.3.4 代码实践

**实践目标**：验证 `add_translations` 的「合并 + last-write-wins」行为，并亲手复现「i18n! 生成的静态后端」的构造过程。

**操作步骤**：

```rust
// 示例代码：复现 codegen 的构造方式，并验证合并语义
use std::borrow::Cow;
use std::collections::HashMap;
use rust_i18n::{Backend, SimpleBackend};

#[test]
fn add_translations_merges_and_overrides() {
    let mut backend = SimpleBackend::new();

    // 第一次灌入 en，含 hello / bye
    let mut en1 = HashMap::new();
    en1.insert(Cow::from("hello"), Cow::from("Hello"));
    en1.insert(Cow::from("bye"), Cow::from("Bye"));
    backend.add_translations("en".into(), en1);

    // 第二次再灌入 en，只含 hello（覆盖）+ 新键 thanks
    let mut en2 = HashMap::new();
    en2.insert(Cow::from("hello"), Cow::from("Hi!"));   // 覆盖旧值
    en2.insert(Cow::from("thanks"), Cow::from("Thanks")); // 新增
    backend.add_translations("en".into(), en2);

    // bye 来自第一次，没被丢掉 → 合并生效
    assert_eq!(backend.translate("en", "bye"), Some(Cow::from("Bye")));
    // hello 被第二次覆盖 → last-write-wins
    assert_eq!(backend.translate("en", "hello"), Some(Cow::from("Hi!")));
    // thanks 来自第二次
    assert_eq!(backend.translate("en", "thanks"), Some(Cow::from("Thanks")));

    // 复现 codegen：把后端装箱为 dyn Backend，模拟 _RUST_I18N_BACKEND
    let _boxed: Box<dyn Backend> = Box::new(backend);
}
```

**需要观察的现象**：
- 第二次 `add_translations("en", …)` 没有清空 `bye`，证明是「合并」而非「覆盖整表」。
- `hello` 的值从 `"Hello"` 变成 `"Hi!"`，证明同名键后写覆盖。
- 最后 `Box::new(backend)` 能成功转为 `Box<dyn Backend>`，正是 codegen 里 `_RUST_I18N_BACKEND` 的形态。

**预期结果**：`cargo test add_translations_merges_and_overrides` 通过。这就是 `i18n!` 在编译期为你自动做、而在本实践中你手动复现的事情。

#### 4.3.5 小练习与答案

**练习 1**：如果 codegen 改成「先 `add_translations` 整个文件 A，再 `add_translations` 整个文件 B」，且 A、B 都含 `en.hello`，最终 `translate("en","hello")` 返回谁的值？

**参考答案**：返回 B 的值。因为 `add_translations` 用 `HashMap::extend` 合并，同名键 last-write-wins，后灌入的 B 覆盖 A。这也解释了为何「同名语言多文件合并」时，文件处理顺序会影响最终结果（与 u2-l3 的合并语义一致）。

**练习 2**：`_RUST_I18N_BACKEND` 为什么用 `Box<dyn Backend>` 而不是直接 `SimpleBackend`？

**参考答案**：为了允许「自定义后端」接入。当用户传 `i18n!("locales", backend = MyBackend)` 时，codegen 会用 `backend.extend(MyBackend)` 把自定义后端和 `SimpleBackend` 组合成 `CombinedBackend`（u4-l2 主题）。`Box<dyn Backend>` 让静态量能统一容纳 `SimpleBackend`、`CombinedBackend` 或任意用户后端类型，无需为每种后端生成不同的静态量类型。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「迷你后端」任务：

1. 新建（或在已有）一个依赖 `rust-i18n` 的 crate，**不要**调用 `i18n!` 宏。
2. 写一个函数 `build_demo_backend() -> Box<dyn rust_i18n::Backend>`，内部用 `SimpleBackend::new()` 灌入 `en` 和 `zh-CN` 两种语言，每种至少含 `greeting` 和 `farewell` 两个键。
3. 写三个测试：
   - 断言 `translate("en", "greeting")` 与 `translate("zh-CN", "greeting")` 返回各自语言的译文。
   - 断言 `available_locales()` 返回 `["en", "zh-CN"]`（字典序）。
   - 用 `messages_for_locale("en")` 拿到全部键值对，断言其长度等于你灌入的键数。
4. 反思：你的这个 `Box<dyn Backend>` 与 `i18n!` 自动生成的 `_RUST_I18N_BACKEND` 在**类型、数据结构、查表方式**上完全相同——区别只在于「谁负责灌数据」（你手动 vs. codegen 自动）。把这一点写成一句话注释。

> 进阶思考（为 u4-l3 铺垫）：既然 `Box<dyn Backend>` 能容纳任意后端，你能否把上面的 `SimpleBackend` 换成一个「从 `HashMap` 里读、且能给某些键返回运行时拼接字符串」的自定义类型？需要实现 trait 的哪几个方法？答案在 u4-l3。

## 6. 本讲小结

- `Backend` trait 把「翻译存储」抽象成三个方法：`available_locales`、`translate`、`messages_for_locale`，并要求 `Send + Sync + 'static` 以便放进全局静态量供多线程访问。
- **`Backend::translate` 只做精确命中，不负责回退**；回退链由外部 `_rust_i18n_try_translate` 反复调用它来编排（trait 与回退策略解耦）。
- `SimpleBackend` 用两层嵌套 `HashMap<locale, HashMap<key, value>>` 存翻译，`translate` 是两次哈希查找，命中路径通过 `Cow::Borrowed` 实现零拷贝。
- `add_translations` 用 `entry().or_default()` + `extend` 实现「合并而非覆盖整表」，同名键 last-write-wins，这使 codegen 能逐语言安全灌入。
- **关键结论：`i18n!` 生成的 `_RUST_I18N_BACKEND` 本质上就是一个被装进 `Box<dyn Backend>` 的 `SimpleBackend`**，由 `LazyLock` 延迟初始化。
- `Backend` / `SimpleBackend` 是纯运行时类型，可脱离 `i18n!` 宏独立使用。

## 7. 下一步学习建议

- 下一篇 **u4-l2 BackendExt 与 CombinedBackend 组合**：本讲提到的 `extend` 方法（[backend.rs:14-22](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L14-L22)）会把两个后端组合起来，重点看 `CombinedBackend::translate` 里 `self.1` 优先于 `self.0` 的优先级合并。
- 之后 **u4-l3 自定义后端实战**：动手实现自己的 `Backend`，并通过 `i18n!(backend = ...)` 接入，理解自定义后端为何优先于本地文件。
- 若想回看「这个 `SimpleBackend` 是怎么被 codegen 调用的」，重温 **u2-l4 generate_code 生成的运行时代码**。
