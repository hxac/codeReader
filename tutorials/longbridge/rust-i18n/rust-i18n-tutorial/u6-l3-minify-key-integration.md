# 短键在 t! 与提取器中的协作

## 1. 本讲目标

本讲是 minify_key（短键）系列的第三篇，也是收尾篇。前两讲（u6-l1、u6-l2）已经讲清了短键「算什么」（SipHash13 + base62 算法）和「怎么单独调用」（`_minify_key!` / `tkv!` 宏）。本讲要回答的是：**当短键真正进入 `t!` 翻译主链路、并且和 `cargo i18n` 提取器一起工作时，会发生什么。**

学完本讲，你应当能够：

1. 说清 `_tr!` 过程宏在 `into_token_stream` 中，根据「消息值的形态」分出的**三条 minify 分支**（字面量字符串 / 元组 / 动态值）各自如何生成 `msg_key`，以及它们在「编译期 vs 运行期」上的根本差异。
2. 解释为什么**动态值只能在运行时调用** `MinifyKey::minify_key`，因而比字面量更耗 CPU。
3. 看懂 `cargo i18n` 提取器（extractor）如何用**同一套算法、同一套参数**为源码里的 `t!` 调用生成 key，从而保证「代码里查的 key」与「提取器写进 TODO.yml 的 key」完全一致。
4. 通过 `examples/app-minify-key` 这个真实示例，亲手验证「键一致性」原则，并理解配置参数（`len` / `prefix` / `threshold`）一旦在 `t!` 与翻译文件 / 提取器之间不一致会带来什么后果。

---

## 2. 前置知识

本讲默认你已经学过以下内容（若没有，建议先读对应讲义）：

- **u6-l1（minify_key 短键算法原理）**：`minify_key(value, len, prefix, threshold)` 的行为——当 `value.len() <= threshold` 时原样返回（`Cow::Borrowed`，零分配），否则对 UTF-8 字节做 SipHash13 得到 128 位哈希，base62 编码后截断到 `len` 长度并拼上 `prefix`。
- **u6-l2（`_minify_key!` 与 `tkv!` 宏）**：`_minify_key!` 是过程宏，在**编译期**就把字面量坍缩成常量 key；`tkv!` 返回 `(key, msg)` 元组。
- **u3-l2（`_tr!` 宏的解析与代码生成）**：`_tr!` 用 `Tr` / `Argument` / `Value` 解析宏调用，`into_token_stream` 按是否有插值参数生成查表代码，所有分支最终都收敛到 `crate::_rust_i18n_try_translate(locale, &msg_key)`。
- **u3-l1（`t!` 完整调用链）**：`t!` → `crate::_rust_i18n_t!`（即 `i18n!` 生成的 `__rust_i18n_t`）→ `rust_i18n::_tr!`，其中 `__rust_i18n_t!` 会把 `minify_key` 系列配置以 `_minify_key` / `_minify_key_len` / `_minify_key_prefix` / `_minify_key_thresh` 四个「系统参数」**自动注入**每次 `_tr!`。

如果上面这些术语你大致有印象，就可以继续。下面只复习一处最关键的衔接点。

### 一个关键复习：minify_key 是「受总开关门控」的

`_tr!` 里是否走短键，完全由一个**布尔总开关** `self.minify_key` 决定（它来自注入的 `_minify_key` 系统参数，最终回溯到 `i18n!(minify_key = true)` 或 `[package.metadata.i18n]` 的 `minify-key`）。开关关闭时，`msg_key` 就是消息本身（`&msg_val`），不做任何哈希。这与 `tkv!` 不同——`tkv!` **总是**调用 `_minify_key!`，不受总开关门控（见 u6-l2）。本讲讨论的所有分支，前提都是 `minify_key = true` 已经打开。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [crates/macro/src/tr.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs) | `_tr!` 过程宏的全部实现。本讲重点在 `into_token_stream` 中根据消息值形态分出的三条 minify 分支，以及辅助判断函数 `is_expr_lit_str` / `is_expr_tuple` / `to_string` / `to_tupled_token_streams`。 |
| [crates/extract/src/extractor.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs) | `cargo i18n` 提取器的核心。`take_message` 在识别到 `t!` / `tr!` 调用后，用与 `_tr!` **完全相同**的 `MinifyKey::minify_key` 算法为字面量生成 key。 |
| [examples/app-minify-key/src/main.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs) | 真实示例：用 `i18n!("locales", minify_key = true, ...)` 配置短键，演示字面量、带参数、运行时字符串等多种 `t!` 用法。 |
| [crates/support/src/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs) | 算法本体（u6-l1 已精读），本讲作为「两端共用同一函数」的证据引用。 |
| [examples/app-minify-key/locales/v2.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/locales/v2.yml) | 示例的翻译文件，key 已经是短键形态（`T_xxxxx`），用于观察「文件里的 key」与「运行时算出的 key」如何对齐。 |

---

## 4. 核心概念与源码讲解

### 4.1 `_tr!` 中三条 minify 分支：字面量 / 元组 / 动态值

#### 4.1.1 概念说明

当 `minify_key` 总开关打开后，`_tr!` 并不是「无脑对消息哈希」。它会先看**消息值（`msg.val`）的形态**，再决定怎么得到 `msg_key`。这是因为短键算法需要「看到字符串内容」才能算哈希，而过程宏在编译期能看到的程度，取决于你写的是字面量还是表达式：

- **字面量字符串**（`t!("Hello")`）：宏在编译期就能读到 `"Hello"` 这个确定内容，于是可以在**编译期**就把哈希算好，把结果坍缩成一个常量 token。运行期零开销。
- **元组**（`t!(("my_key", "Hello"))`）：调用者**显式提供**了 key，宏不需要哈希，直接把元组的第一个元素当 key、第二个当消息值。这常见于「长文案想用自定义短 key」或与 `tkv!` 返回的 `(key, msg)` 配合的场景。
- **动态值**（`t!(some_var)`、`t!(*src)`）：消息是一个**运行时才有值**的变量或表达式。过程宏在编译期看不到它的内容，无法算哈希，只能在生成的代码里**写一句运行期调用** `rust_i18n::MinifyKey::minify_key(...)`，等程序跑起来才算。

这条「能不能在编译期算」的分界线，是本节的核心。

#### 4.1.2 核心流程

`into_token_stream` 用一个 `if / else if / else if / else` 链，按优先级依次判断消息形态。决策流程如下（伪代码）：

```
若 minify_key 打开 且 消息是字符串字面量:
    编译期算好 key（调 MinifyKey::minify_key，结果当常量）
    msg_key = 常量 token,  msg_val = 字面量
否则若 minify_key 打开 且 消息是二元组:
    拆元组：(first, last)
    msg_key = first,  msg_val = last
否则若 minify_key 打开（即消息是非字面量、非元组的动态值）:
    生成运行期调用：msg_key = rust_i18n::MinifyKey::minify_key(&msg_val, len, prefix, thresh)
    msg_val = 原表达式
否则（minify_key 关闭）:
    msg_key = &msg_val   （不哈希）
```

用一句话概括短键的生成公式（当 `value.len() > threshold` 时）：

\[
\text{key} = \text{prefix} \,\|\, \text{base62}\big(\text{SipHash13}(\text{value})\big)\big[\,0\,..\,\text{len}\,\big]
\]

当 `value.len() \le \text{threshold}` 时，key 直接等于 value 本身（短路返回，零分配）。这条公式在三条分支里是同一个，区别只在于**在哪一刻求值**：分支一在编译期，分支三在运行期。

#### 4.1.3 源码精读

决策的主体在 `into_token_stream` 开头：

[crates/macro/src/tr.rs:390-413](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L390-L413) — 按「字面量 → 元组 → 动态值 → 关闭」四级匹配，产出 `(msg_key, msg_val)`。

下面逐条拆开看。

**分支一：字面量字符串（编译期求值）**

```rust
let (msg_key, msg_val) = if self.minify_key && self.msg.val.is_expr_lit_str() {
    let msg_val = self.msg.val.to_string().unwrap();
    let msg_key = MinifyKey::minify_key(
        &msg_val,
        self.minify_key_len,
        self.minify_key_prefix.as_str(),
        self.minify_key_thresh,
    );
    (quote! { #msg_key }, quote! { #msg_val })
}
```

- [crates/macro/src/tr.rs:391-399](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L391-L399)：`is_expr_lit_str()` 为真时进入。注意这里 `MinifyKey::minify_key(...)` 是**过程宏自己在编译期执行**的（`MinifyKey` 是 support crate 导出的 trait，`&msg_val` 是个 `&String`，UFCS 调用其 trait 方法），返回的 `Cow<str>` 被 `quote! { #msg_key }` 直接变成一个字符串字面量 token。等价于 u6-l2 讲过的 `_minify_key!`：**输入恒定则输出恒定，运行期零开销**。
- 判断函数 `is_expr_lit_str`：[crates/macro/src/tr.rs:16-23](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L16-L23)，只认 `Expr::Lit` 且字面量是 `Lit::Str`；`to_string`：[crates/macro/src/tr.rs:32-39](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L32-L39) 同样只对字符串字面量返回 `Some`——这正是「编译期短键只对字面量生效」的根因（u3-l2 已提及）。

**分支二：二元组（显式提供 key）**

```rust
} else if self.minify_key && self.msg.val.is_expr_tuple() {
    self.msg.val.to_tupled_token_streams().unwrap()
}
```

- [crates/macro/src/tr.rs:400-401](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L400-L401)：消息是一个二元组表达式时进入。
- `to_tupled_token_streams`：[crates/macro/src/tr.rs:41-55](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L41-L55)，要求元组恰好两个元素，返回 `(first, last)` 两个 token 流，于是 `msg_key = first`、`msg_val = last`。宏**不哈希**，调用者说什么 key 就用什么 key。

**分支三：动态值（运行期求值）**

```rust
} else if self.minify_key {
    let minify_key_len = self.minify_key_len;
    let minify_key_prefix = self.minify_key_prefix;
    let minify_key_thresh = self.minify_key_thresh;
    let msg_val = self.msg.val.to_token_stream();
    let msg_key = quote! { rust_i18n::MinifyKey::minify_key(&msg_val, #minify_key_len, #minify_key_prefix, #minify_key_thresh) };
    (msg_key, msg_val)
}
```

- [crates/macro/src/tr.rs:402-408](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L402-L408)：这是最值得注意的一条。此处 `msg_key` 不再是一个常量，而是一段**会出现在最终二进制里的代码**：`rust_i18n::MinifyKey::minify_key(&msg_val, ...)`。
- 也就是说，对于 `t!(some_var)` 这种调用，每次执行 `t!` 都会在运行时做一次 SipHash13 + base62。相比分支一的「编译期算好、运行期查表」，分支三每次调用都多付一份哈希计算的 CPU 开销。

**分支四：minify_key 关闭（不哈希）**

```rust
} else {
    let msg_val = self.msg.val.to_token_stream();
    let msg_key = quote! { &msg_val };
    (msg_key, msg_val)
}
```

- [crates/macro/src/tr.rs:409-413](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L409-L413)：`msg_key` 直接是 `&msg_val`，消息原文即 key。这是默认行为（`DEFAULT_MINIFY_KEY = false`）。

四条分支产出 `(msg_key, msg_val)` 后，后续代码（有无插值参数的两条路径，见 u3-l2）统一用 `crate::_rust_i18n_try_translate(locale, &msg_key)` 查表——查表逻辑与是否 minify 无关。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「字面量走编译期、动态值走运行期」的差别。

**操作步骤**：

1. 进入 `examples/app-minify-key`，用 `RUST_I18N_DEBUG=1` 展开宏查看生成代码：

   ```bash
   cd examples/app-minify-key
   RUST_I18N_DEBUG=1 cargo build 2>&1 | tee /tmp/i18n_debug.log
   ```

2. 在 `src/main.rs` 里有两类典型调用（[examples/app-minify-key/src/main.rs:34](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs#L34) 与 [examples/app-minify-key/src/main.rs:73](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs#L73)）：

   ```rust
   t!("Hello", locale = locale)   // 字面量 → 分支一
   ...
   let translated = t!(*src, locale = locale);  // 动态值 → 分支三
   ```

3. 在调试输出里搜索 `MinifyKey::minify_key` 字样，对比两类调用展开后的 `msg_key`。

**需要观察的现象**：

- `t!("Hello", ...)` 展开后，`msg_key` 应是一个**字符串常量**（编译期已算好的短键），看不到运行期哈希调用。
- `t!(*src, ...)` 展开后，`msg_key` 应包含 `rust_i18n::MinifyKey::minify_key(&msg_val, 24, "mytr_", 4)` 这样的**运行期调用**。

**预期结果**：字面量调用零运行期哈希开销；动态值调用每次执行都要算一次哈希。

**待本地验证**：`RUST_I18N_DEBUG=1` 的确切打印格式与具体短键值（依赖本机编译），请以实际输出为准。如果调试输出里两类调用的 `msg_key` 形态如上所述，即验证了三分支的差别。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `t!("Hello")` 能在编译期算出短键，而 `t!(user_input)` 不行？

> **参考答案**：过程宏在编译期只能「看到」token。`"Hello"` 是字符串字面量，内容已知，宏可直接调用 `MinifyKey::minify_key` 算出常量（分支一）；`user_input` 是个变量，编译期不知道它的运行时值，宏无法求哈希，只能在生成代码里写一句运行期调用（分支三）。

**练习 2**：`t!(("greet", "Hello"))` 这种写法走哪条分支？`msg_key` 是什么？

> **参考答案**：走分支二（元组）。`to_tupled_token_streams` 把二元组拆成 `(first, last)`，`msg_key = "greet"`、`msg_val = "Hello"`。宏不做任何哈希，key 由调用者显式指定。

**练习 3**：把 `i18n!(minify_key = true, ...)` 改成 `minify_key = false` 后，`t!("Hello")` 走哪条分支？

> **参考答案**：走分支四。总开关关闭后，前三条 `minify_key && ...` 条件全为假，`msg_key = &msg_val`，即 key 就是 `"Hello"` 本身，不哈希。

---

### 4.2 extractor 用同一套算法生成 key

#### 4.2.1 概念说明

短键方案有一个**天然的协作难题**：运行时 `t!("很长的文案……")` 会把这句长文案算成一个短 key 去查表；可是翻译文件里的 key 也必须是同一个短 key，否则查不到。那翻译文件里的短 key 谁来填？

答案就是 `cargo i18n` 提取器（extractor）。它**遍历你的源码**，找到所有 `t!(...)` / `tr!(...)` 调用，取出第一个字面量参数（即那句长文案），然后**用和 `_tr!` 完全相同的 `minify_key` 算法、完全相同的参数**把它算成短 key，写进 `TODO.yml`。这样，提取器写出的 key 与 `t!` 运行时查的 key 天然一致——前提是**两端用了同一套 `len` / `prefix` / `threshold`**。

#### 4.2.2 核心流程

提取器的主流程（详见 u7-l2）是：用 `ignore` 库遍历 `.rs` 文件 → `Extractor::invoke` 递归扫描 token 流 → 用 `METHOD_NAMES = ["t", "tr"]` 加 `!` 识别宏调用 → `take_message` 取出首个字面量并记录行号。本节只聚焦 `take_message` 里**与 minify_key 有关的那段**：

```
take_message(stream):
    取第一个 token，必须是 Literal（字符串字面量），否则放弃
    把 Literal 还原成字符串 key
    若 cfg.minify_key 为真:
        hashed = MinifyKey::minify_key(key, len, prefix, thresh)   ← 与 _tr! 同一函数
        message_key  = hashed
        message_content = 原文 key        ← 写进 YAML 的「待翻译值」
    否则:
        message_key  = format_message_key(key)   ← 折叠空白当 key
        message_content = message_key
    以 message_key 为键插入 results，并记录 Location(行号)
```

关键点：**`message_key`（写到文件里的 key）与 `message_content`（待翻译的原文）分离**。开启 minify 时，文件里的 key 是短键，值是原文；这正好对应 `t!` 运行时「用短键查表、查到原文译文」的行为。

#### 4.2.3 源码精读

识别哪些宏名算「翻译调用」：

[crates/extract/src/extractor.rs:35](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L35) — `static METHOD_NAMES: &[&str] = &["t", "tr"];`，只有 `t!` / `tr!` 会被提取。

`take_message` 里 minify 与非 minify 的分流：

```rust
let (message_key, message_content) = if *minify_key {
    let hashed_key = rust_i18n_support::MinifyKey::minify_key(
        &key,
        *minify_key_len,
        minify_key_prefix,
        *minify_key_thresh,
    );
    (hashed_key.to_string(), key.clone())
} else {
    let message_key = format_message_key(&key);
    (message_key.clone(), message_key)
};
```

- [crates/extract/src/extractor.rs:109-120](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L109-L120)：注意这里调用的是 `rust_i18n_support::MinifyKey::minify_key`——与 `_tr!` 分支一里那个 `MinifyKey::minify_key` 是**同一个 trait 的同一个方法**（定义见 [crates/support/src/minify_key.rs:49-52](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L49-L52)，底层函数 [crates/support/src/minify_key.rs:39-46](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L39-L46)）。
- 参数 `*minify_key_len` / `minify_key_prefix` / `*minify_key_thresh` 全部来自 `self.cfg: I18nConfig`（[crates/extract/src/extractor.rs:98-104](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L98-L104) 解构而来），即 `cargo i18n` 从 `[package.metadata.i18n]` 读到的配置。

**这正是「键一致性」的工程保证**：`_tr!`（在用户的 `i18n!` 配置下）和 extractor（在同一份 `[package.metadata.i18n]` 配置下）调用的是**同一个函数、同一套参数**，所以对同一个字面量必然算出同一个 key。

> 注意一个边界：extractor 只提取**字面量**首参（[crates/extract/src/extractor.rs:92-96](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L92-L96) 要求首 token 是 `Literal`，再经 [crates/extract/src/extractor.rs:140-145](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L140-L145) 的 `literal_to_string` 仅认 `LitStr`）。因此 `t!(some_var)` 这种动态值调用**不会被提取**——这也合理，因为动态值的内容编译期未知，提取器无从生成 key。换句话说，分支三（动态值）的翻译需要你**手动**维护，提取器帮不上忙。

#### 4.2.4 代码实践

**实践目标**：验证提取器对字面量算出的 key，与 `t!` 运行时查的 key 一致。

**操作步骤**：

1. 阅读提取器的内置测试 [crates/extract/src/example.test.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/example.test.rs)，里面是若干 `t!("...")` 调用。
2. 跑该测试（默认 `cfg = I18nConfig::default()`，即 `minify_key = false`，走 `format_message_key` 分支）：

   ```bash
   cargo test -p rust-i18n-extract test_extract
   ```

3. 想观察 minify 分支，可临时写一个最小用例（**示例代码**，非项目原有）：

   ```rust
   // 仅作阅读理解用，不必真的加进仓库
   use rust_i18n_support::{I18nConfig, MinifyKey};
   // 取一句长文案，用与 app-minify-key 相同的参数算 key
   let key = MinifyKey::minify_key("Apple", 24, "mytr_", 4);
   println!("{:?}", key); // 预期: 形如 "mytr_xxxxxx..."（len 5 > thresh 4，会哈希）
   ```

**需要观察的现象**：

- `test_extract` 默认配置下，提取出的 `Message.key` 是折叠空白后的原文（如 `"hello"`、`"views.message.title"`），与 [crates/extract/src/extractor.rs:217-257](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L217-L257) 测试断言一致。
- 若把 `cfg.minify_key` 置为 `true` 并配上 `len/prefix/thresh`，同一句 `"Apple"` 会变成 `prefix + base62(hash)` 形态的短键。

**预期结果**：只要 `_tr!` 与 extractor 用同一组 `(len, prefix, threshold)`，对同一字面量产出的 key 必然相同。

**待本地验证**：`MinifyKey::minify_key("Apple", 24, "mytr_", 4)` 的确切字符串值请在本机打印确认（u6-l1 已展示 `"Hello, world!"` → `"1LokVzuiIrh1xByyZG4wjZ"` 的算法确定性）。

#### 4.2.5 小练习与答案

**练习 1**：extractor 调用的 `MinifyKey::minify_key` 和 `_tr!` 分支一里调用的是不是同一个函数？为什么这很重要？

> **参考答案**：是同一个（都是 `rust_i18n_support::MinifyKey` trait 的 `minify_key` 方法）。重要性在于：只要两端参数一致，对同一字面量必然算出同一 key，从而「提取器写进文件的 key」与「`t!` 运行时查的 key」天然对齐，不会错位。

**练习 2**：为什么 `t!(user_input)` 这种动态值调用无法被 extractor 提取？

> **参考答案**：`take_message` 要求宏调用的首 token 是字符串 `Literal`（[crates/extract/src/extractor.rs:92](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs#L92)），变量名是 `Ident` 不是 `Literal`，会被跳过。动态值的内容编译期未知，提取器无从生成确定的 key。

**练习 3**：开启 minify 后，extractor 写进 `TODO.yml` 的「键」和「值」分别是什么？

> **参考答案**：键是短键（`hashed_key.to_string()`），值是原文（`key.clone()`）。即 `T_xxxxxx: { en: "Apple", ... }` 这种形态——短键当 key、原文当待翻译值。

---

### 4.3 `app-minify-key` 配置与「键一致性」原则

#### 4.3.1 概念说明

minify_key 有一条**不可违反的工程铁律**：

> **`t!` 运行时计算 key 所用的 `(len, prefix, threshold)`，必须与翻译文件 / 提取器所用的参数完全一致。**

这条铁律的根源是：短键 key 是个**普通字符串**，`t!` 拿它去 `SimpleBackend` 里做 `HashMap` 精确查找（见 u4-l1）。任何一个参数（尤其 `prefix`）不一致，算出的字符串就不同，查找就 miss——表现为「翻译没生效，返回了原文」。

`examples/app-minify-key` 这个示例恰好提供了一个**观察这条铁律的活样本**：它的 `i18n!` 配置和内置翻译文件之间存在一个前缀不一致，正好可以用来理解「不一致会怎样」。本节就借着它把配置项和铁律讲透。

#### 4.3.2 核心流程

示例的初始化配置在 [examples/app-minify-key/src/main.rs:3-9](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs#L3-L9)：

```rust
rust_i18n::i18n!(
    "locales",
    minify_key = true,
    minify_key_len = 24,
    minify_key_prefix = "mytr_",
    minify_key_thresh = 4
);
```

四个参数的含义与作用点：

| 参数 | 值 | 作用 |
| --- | --- | --- |
| `minify_key` | `true` | 总开关，打开后 `_tr!` 才会走 4.1 的分支一/二/三。 |
| `minify_key_len` | `24` | 短键目标长度；128 位 base62 最多 22 字符，故 24 几乎总被钳到 22（不损信息）。 |
| `minify_key_prefix` | `"mytr_"` | 短键前缀，做命名空间隔离。**最敏感的参数**：前缀错一个字符，key 就全错。 |
| `minify_key_thresh` | `4` | 阈值：`value.len() <= 4` 时原样返回不哈希；`> 4` 才哈希。 |

配置注入路径（回顾 u3-l1 / u6-l2）：`i18n!` 把这四个值生成进内部宏 `__rust_i18n_t`，后者在每次 `_tr!` 调用前以 `_minify_key` / `_minify_key_len` / `_minify_key_prefix` / `_minify_key_thresh` 系统参数注入；`_tr!` 经 `filter_arguments`（[crates/macro/src/tr.rs:342-376](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L342-L376)）把它们从普通插值参数里剥离，填进 `Tr` 的对应字段，最终驱动 4.1 的分支选择。

**threshold=4 对长短字符串的影响**（本讲实践任务的核心）：

- 短字符串（`len <= 4`，如 `"Hi"`）：`minify_key` 短路返回原文，key 就是 `"Hi"` 本身，**不哈希**。
- 长字符串（`len > 4`，如 `"Apple"`、`"Hello"`）：走哈希，key = `prefix + base62(hash)`。

示例里几乎所有文案（`"Apple"`、`"Hello"`、`"Hello, %{name}!"` …）长度都大于 4，因此都会被哈希，key 都会带上 `prefix`——这正是前缀敏感性会集中爆发的场景。

#### 4.3.3 源码精读

先看翻译文件 `v2.yml` 的 key 形态：

[examples/app-minify-key/locales/v2.yml:1-12](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/locales/v2.yml#L1-L12) — 这是 v2 格式（`_version: 2`，多语言合一文件，见 u1-l4），key 形如 `T_29xGXAUPAkgvVzCf9ES3q8`，**前缀是 `T_`**。

而 `main.rs` 里 `minify_key_prefix = "mytr_"`（[examples/app-minify-key/src/main.rs:7](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs#L7)）。

于是出现了一个**真实的前缀不一致**：运行时 `t!("Apple")` 会算出 `mytr_<hash>`，而文件里的 key 是 `T_<hash>`。两者的哈希部分（`<hash>`）相同（同一算法、同一 `len`、同一输入 `"Apple"`），**只有前缀不同**。而 `SimpleBackend::translate` 是精确字符串匹配（u4-l1），前缀不同 → 不匹配 → miss。

> 这个不一致有据可查：`git log` 显示 `main.rs` 的前缀在提交 `dbc68b9`（"Refactor code for #73"）中由 `"T."` 改成了 `"mytr_"`，而 `v2.yml` 的 key 仍是更早的 `T_` 前缀、未同步重新生成。这正是「改了配置却忘了重新提取/同步翻译文件」的典型现场，也是本讲最好的反面教材。

再看示例里几类典型 `t!` 调用，分别命中 4.1 的哪条分支：

- [examples/app-minify-key/src/main.rs:34](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs#L34) `t!("Hello", locale = locale)` —— 字面量，**分支一**（编译期求 key）。
- [examples/app-minify-key/src/main.rs:44](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs#L44) `t!("Hello, %{name}!", name = "World", locale = locale)` —— 字面量 + 插值参数，仍走**分支一**算 key，再用 `replace_patterns` 填占位符（u3-l3）。
- [examples/app-minify-key/src/main.rs:73](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs#L73) `t!(*src, locale = locale)`，其中 `src` 来自 `["Apple", "Banana", "Orange"]` —— **动态值，分支三**（运行期求 key）。

注意第 73 行的动态值案例最能暴露前缀不一致：因为它每次都在运行时算 `mytr_<hash>`，而文件里只有 `T_<hash>`，必然 miss。

#### 4.3.4 代码实践

**实践目标**：用 `minify_key_thresh = 4` 区分长短字符串的处理方式；并通过示例里的前缀不一致，亲手验证「键一致性」铁律。

**操作步骤**：

1. 运行示例，重点看「Translation of runtime strings」一节（动态值，分支三）：

   ```bash
   cargo run -p app-minify-key
   ```

2. 阅读输出里 `Apple / Banana / Orange` 那几行。对照 [examples/app-minify-key/locales/v2.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/locales/v2.yml)：文件里这三条文案的 key 是 `T_29xGXAUPAkgvVzCf9ES3q8` / `T_7MQVq9vgi0h6pLE47CdWXH` / `T_2RohljPx99sA18L8E5oTD4`（前缀 `T_`）。

3. 推理：运行时 prefix 是 `mytr_`，所以 `t!(*src)` 算出的是 `mytr_29xGXAUPAkgvVzCf9ES3q8`（哈希部分相同、前缀不同），与文件里的 `T_29xGXAUPAkgvVzCf9ES3q8` 不相等 → 查不到 → 返回原文。

4. 修复验证（二选一）：
   - 把 [examples/app-minify-key/src/main.rs:7](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/src/main.rs#L7) 的前缀改回 `"T_"`，重新运行，观察译文是否生效；或
   - 保持 `mytr_`，用 `cargo i18n`（参见 u7 系列）按当前配置重新提取，生成带 `mytr_` 前缀的新 `TODO.yml`，再把译文补进去。

**需要观察的现象**：

- 修复前：`Apple / Banana / Orange` 以及 `Hello` 等行的译文大概率**与原文相同**（miss 后 `_tr!` 返回原始消息，见 [crates/macro/src/tr.rs:438-444](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L438-L444) 的兜底分支）。
- 修复后（前缀对齐）：译文应正确返回，如 `Apple` 在 `zh` 下显示 `苹果`。

**预期结果**：只要 `t!` 的 `prefix` 与翻译文件 key 的前缀一致，查找即命中；不一致则全部 miss。这直接印证了「`t!`、翻译文件、extractor 三者必须共用同一组 `minify_key` 参数」的铁律。

**待本地验证**：示例的实际 stdout 取决于本机编译与运行，请以实际输出为准。若观察到「修复前 miss、修复后命中」的现象，即验证了前缀敏感性。

#### 4.3.5 小练习与答案

**练习 1**：在 `minify_key_thresh = 4` 下，`t!("Hi")` 和 `t!("Hello")` 的 key 分别是什么形态？

> **参考答案**：`"Hi"` 长度 2 ≤ 4，短路返回原文，key = `"Hi"`（不哈希）；`"Hello"` 长度 5 > 4，走哈希，key = `"mytr_" + base62(hash("Hello"))`。

**练习 2**：如果把 `minify_key_prefix` 从 `"mytr_"` 改成 `"T_"`，但翻译文件 `v2.yml` 不变，`t!("Apple")` 能查到吗？

> **参考答案**：能。改成 `"T_"` 后运行时算出的 key 变成 `T_<hash>`，与 `v2.yml` 里的 `T_29xGXAUPAkgvVzCf9ES3q8` 前缀一致、哈希部分相同，精确匹配命中。反过来说明了前缀必须两端对齐。

**练习 3**：为什么说「修改了 `minify_key_prefix` 之后，必须重新跑 extractor 或同步翻译文件」？

> **参考答案**：因为 prefix 是 key 字符串的一部分。运行时 `t!` 用新 prefix 算 key，而旧翻译文件里是旧 prefix 的 key，二者不等 → miss。extractor 用同一函数、同一参数生成 key，重新跑一遍就能让文件里的 key 与 `t!` 重新对齐。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「端到端验证键一致性」的小任务。

**场景**：你要在一个最小项目里启用 minify_key，并确保 `cargo i18n` 提取出的 key 与 `t!` 实际查找的 key 完全一致。

**步骤**：

1. 新建一个 crate，依赖 `rust-i18n`，并写初始化（**示例代码**）：

   ```rust
   rust_i18n::i18n!(
       "locales",
       minify_key = true,
       minify_key_len = 24,
       minify_key_prefix = "t_",
       minify_key_thresh = 10
   );
   ```

2. 在代码里写两条调用，分别命中分支一与分支三：

   ```rust
   // 分支一：字面量，编译期算 key
   println!("{}", t!("This is a long user-facing message."));
   // 分支三：动态值，运行期算 key
   let s = String::from("This is a long user-facing message.");
   println!("{}", t!(&s));
   ```

3. 用 `RUST_I18N_DEBUG=1 cargo build` 展开，确认：
   - 分支一的 `msg_key` 是常量字面量；
   - 分支三的 `msg_key` 含 `rust_i18n::MinifyKey::minify_key(...)` 运行期调用；
   - 两者算出的 key **字符串相同**（因为算法与参数一致）。

4. 在 `[package.metadata.i18n]` 里写**同样的** `minify-key / minify-key-len / minify-key-prefix / minify-key-thresh`（注意 kebab-case，见 u5-l1），运行 `cargo i18n` 提取，检查生成的 `TODO.yml` 里那条文案的 key，是否与第 3 步看到的 key 一致。

5. 给该 key 补上一种语言的译文，运行程序，确认 `t!` 返回译文而非原文。

**验收标准**：

- 能说清分支一与分支三在生成代码上的差别；
- 能指出 `cargo i18n` 提取的 key 与 `t!` 查的 key 相同的**根本原因**（同一函数、同一参数）；
- 一旦故意把 `[package.metadata.i18n]` 的 `minify-key-prefix` 改成与 `i18n!` 不同，能预测到「提取的 key 与 `t!` 查的 key 错位、译文 miss」并解释。

---

## 6. 本讲小结

- `_tr!` 的 `into_token_stream` 按「消息值形态」分四条分支：**字面量（分支一，编译期求 key）/ 元组（分支二，显式给 key）/ 动态值（分支三，运行期求 key）/ 关闭（分支四，不哈希）**。
- 字面量能在编译期算好短键并坍缩成常量（零运行期开销）；动态值因编译期未知内容，只能生成 `rust_i18n::MinifyKey::minify_key(...)` 运行期调用，每次 `t!` 都多付一次哈希计算。
- extractor（`cargo i18n`）的 `take_message` 调用的是**同一个 `MinifyKey::minify_key`、同一套参数**，因此对同一字面量产出的 key 与 `t!` 完全一致——这是「提取器写入文件的 key」与「运行时查的 key」天然对齐的工程保证。
- extractor 只提取**字面量**首参，动态值调用不会被提取，其翻译需手动维护。
- **键一致性铁律**：`t!`、翻译文件、extractor 三者必须共用同一组 `(minify_key, len, prefix, threshold)`，任一参数（尤其 `prefix`）错位都会导致精确查找 miss、译文失效。
- `examples/app-minify-key` 的 `minify_key_thresh = 4` 演示了「短字符串原样返回、长字符串才哈希」的阈值门控；其配置前缀（`mytr_`）与翻译文件前缀（`T_`）的历史不一致，正好是理解前缀敏感性的活样本。

---

## 7. 下一步学习建议

- **u7-l1 / u7-l2 / u7-l3（cargo i18n CLI 提取器）**：本讲只用到 extractor 的 `take_message`，下一单元会完整讲清 CLI 入口、源码遍历（`ignore` walker）和 `TODO.yml` 生成与去重，把「提取→翻译→回填」的闭环补齐。
- **u8-l2（性能基准与内存优化）**：本讲指出分支三（动态值）每次调用都要哈希，更耗 CPU；u8-l2 的 criterion 基准会给出量化对比，并讲解 `Cow` / `SmallVec` 等内存优化如何与短键配合。
- **动手延伸**：阅读 [crates/support/src/minify_key.rs:108-161](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L108-L161) 的单元测试，验证同一字面量在 `&str` / `String` / `Cow` / `&String` 各类型上产出相同 key，加深「确定性哈希」的直觉。
