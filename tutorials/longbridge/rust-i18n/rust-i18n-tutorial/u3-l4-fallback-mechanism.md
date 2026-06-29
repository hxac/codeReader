# 翻译回退机制

## 1. 本讲目标

本讲承接 [u3-l1](u3-l1-t-macro-call-chain.md)（`t!` 宏的完整调用链），专门讲清一件事：**当一次 `t!` 在目标 locale 里查不到某个键时，rust-i18n 会按什么顺序去「别处」再找一遍，直到命中或彻底放弃。**

这条「别处」就是**回退链（fallback chain）**。它由两段串联组成：

1. **territory 自动回退**：把 `zh-Hant-CN` 逐级削成 `zh-Hant`、`zh` 去试（按 RFC 4647 的语言标签查找思路）；
2. **显式 fallback 列表**：用户在 `i18n!` 里写死的 `fallback = ["zh", "en"]`，逐个去试。

学完后你应当能：

- 看懂 [`_rust_i18n_try_translate`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L385-L400) 如何把「精确命中 → territory 回退 → 显式列表」三段编排成一条链；
- 理解 [`_rust_i18n_lookup_fallback`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L363-L365) 如何靠 `rfind('-')` 逐级去掉语言标签的子标签；
- 说清 [`_RUST_I18N_FALLBACK_LOCALE`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L349) 这个静态量是怎么从 `i18n!(fallback = ...)` 生成的、又以什么优先级参与查找；
- 能根据 `tests/integration_tests.rs` 里的真实用例，**预测**一次 `t!` 的回退结果。

本讲的三个最小模块正好对应这条链的三个零件：总入口、territory 削标签器、显式列表。

## 2. 前置知识

在进入本讲前，请确认你已经了解（这些都在前置讲义里建立过）：

- **`t!` 是转发壳**：`t!` 自身不翻译，它转发到 `crate::_rust_i18n_t!`，再到全局的 `_tr!` 过程宏（见 [u3-l1](u3-l1-t-macro-call-chain.md)）。
- **查表入口是 `_rust_i18n_try_translate`**：`_tr!` 展开后的代码最终调用 `crate::_rust_i18n_try_translate(locale, key)`，它返回 `Option<Cow<str>>`——`Some` 表示命中、`None` 表示完全查不到（见 [u3-l2](u3-l2-tr-macro-codegen.md)、[u3-l3](u3-l3-interpolation-and-format.md)）。
- **静态后端 `_RUST_I18N_BACKEND`**：`i18n!` 在编译期把所有翻译灌进一个 `SimpleBackend`，包成 `Box<dyn Backend>` 存进 `LazyLock` 静态量，运行时 `.translate(locale, key)` 就是去这张表里查（见 [u2-l4](u2-l4-generate-code.md)）。
- **locale 是带层级的语言标签**：形如 `language-Script-REGION-Variant`，例如 `zh-Hant-CN` 表示「中文 - 繁体字 - 中国大陆地区」。`-` 切出来的一段叫一个**子标签（subtag）**。

一句话回顾位置关系：`_tr!` 只负责「拿到 locale 和 key 去查」，**查不到怎么办**完全是 `_rust_i18n_try_translate` 内部的事——本讲就把这个黑盒拆开。

> ⚠️ 一个常见误解：**`t!` 没有「临时指定 fallback」的能力**。`t!("key", fallback = "en")` 里的 `fallback = "en"` 并不会临时改回退语言——它会被当成一个名叫 `fallback` 的普通插值变量（见 4.3 节的剖析）。回退语言**只能在 `i18n!` 处整体配置**。本讲会让你彻底看清这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/macro/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) | `generate_code` 用 `quote!` 生成的运行时代码里，包含了本讲的全部三个零件：`_RUST_I18N_FALLBACK_LOCALE`（显式列表静态量）、`_rust_i18n_lookup_fallback`（territory 削标签）、`_rust_i18n_try_translate`（回退总入口）。 |
| [tests/integration_tests.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs) | 用多个子模块各自调用 `i18n!(fallback = ...)` 验证回退行为：单字符串 fallback、数组 fallback、territory 自动回退、缺失 locale 回退。 |

两个文件一「实现」一「验证」：前者是 `i18n!` 编译期生成、运行期执行的回退代码，后者是证明这些行为确实如此的真实测试。本讲会反复在两者间对照。

## 4. 核心概念与源码讲解

### 4.1 _rust_i18n_try_translate：编排三段回退的总入口

#### 4.1.1 概念说明

`_tr!` 展开后调用的是 `_rust_i18n_try_translate(locale, key)`，它是整个回退机制的**总入口**与**编排者**。它的职责不是「亲自查表」，而是把一次查找组织成**三段递进的尝试**：

1. **精确命中**：拿原始 `locale` 直接去后端查一次；
2. **territory 自动回退**：精确没中，就把 locale 逐级「削短」再查（`zh-Hant-CN` → `zh-Hant` → `zh`）；
3. **显式 fallback 列表**：territory 也耗尽了，再按用户配置的 `["zh", "en"]` 逐个查。

只要任何一段命中就立刻返回 `Some`；三段全空才返回 `None`。返回 `None` 时，外层 `t!` 会改用原始消息字符串（详见 4.1.3）。

> 名字里的 `try_` 暗示了语义：它**不会失败/不 panic**，只是「试着找」，找不到就给 `None`，把「用什么顶上」的决定权交给调用方。

#### 4.1.2 核心流程

```
_rust_i18n_try_translate(locale, key):
  1. 精确: v = backend.translate(locale, key)
     if v.is_some(): return v

  2. territory 回退循环 (current = locale):
     while let Some(parent) = lookup_fallback(current):
         v = backend.translate(parent, key)
         if v.is_some(): return v
         current = parent

  3. 显式列表:
     for fb in _RUST_I18N_FALLBACK_LOCALE:    # 例如 ["zh", "en"]
         v = backend.translate(fb, key)
         if v.is_some(): return v

  4. return None
```

三段的**优先级**是从上到下递减的：**精确 > territory > 显式列表**。一个重要推论：territory 回退会先于显式列表跑完，所以即便显式列表里写了 `zh`、而 territory 链已经路过 `zh` 没中，显式列表仍会**再把 `zh` 试一遍**（见第 5 节综合实践里 `zh` 被试两次的现象）。

#### 4.1.3 源码精读

总入口的完整实现就十几行，但信息量很大：

[crates/macro/src/lib.rs:L385-L400](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L385-L400) —— `_rust_i18n_try_translate`：先 `backend.translate(locale, key)` 精确查；miss 后进 `.or_else` 闭包，先跑 territory `while` 循环（不断调用 `_rust_i18n_lookup_fallback` 取父级 locale 再查），循环耗尽后再 `.and_then` 走 `_RUST_I18N_FALLBACK_LOCALE` 显式列表的 `find_map`。

几个关键点逐条点出来：

- 第一行 `_RUST_I18N_BACKEND.translate(locale, key.as_ref())` 是**唯一**真正查后端的地方；后面所有的 `.translate(...)` 调的都是这同一个静态后端，只是换 locale 参数。
- territory 循环用 `while let Some(fallback_locale) = _rust_i18n_lookup_fallback(current_locale)`：只要还能削出父级就继续，削不出（locale 里没 `-` 了）就退出。每轮把 `current_locale = fallback_locale` 推进，形成「逐级下钻」。
- 显式列表那段 `_RUST_I18N_FALLBACK_LOCALE.and_then(|fallback| fallback.iter().find_map(...))`：注意它套了 `.and_then`——只有当 `_RUST_I18N_FALLBACK_LOCALE` 是 `Some`（即用户配置了 fallback）才会执行；没配就是 `None`，整段直接跳过。`find_map` 会在列表里**按顺序**找到第一个命中的就停。

**与 `_rust_i18n_translate` 的区别（重要）。** 源码里还有一个长得像的兄弟函数：

[crates/macro/src/lib.rs:L371-L379](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L371-L379) —— `_rust_i18n_translate`：它内部调 `_rust_i18n_try_translate`，miss 时用 `unwrap_or_else` 兜底，返回 `"locale.key"`（locale 为空则返回 `key` 本身）。

这两个函数的兜底策略**不同**，别搞混：

| 函数 | miss 时返回 | 谁在用它 |
| --- | --- | --- |
| `_rust_i18n_try_translate` | `None`（交给调用方决定） | `t!` / `_tr!` 实际调用的是它 |
| `_rust_i18n_translate` | `"locale.key"` 字符串 | 集成测试里直接调用（如 `test1::_rust_i18n_translate("en", "missing.default")`） |

也就是说：**`t!` 在 miss 时既不会返回 `"locale.key"`、也不会 panic，而是返回你传给 `t!` 的原始消息字符串。** 这是因为 `_tr!` 生成的是这样的代码：

[crates/macro/src/tr.rs:L433-L445](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L433-L445) —— 无参数分支：`if let Some(translated) = _rust_i18n_try_translate(locale, &msg_key) { translated } else { 原始 msg_val }`。所以回退链全部 miss 后，`t!` 给你的是原文，而不是 `locale.key`。

#### 4.1.4 代码实践

**实践目标**：用一个真实存在的回退场景，肉眼确认「territory 命中就不再走显式列表」。

**操作步骤**：

1. 打开 [tests/integration_tests.rs:L84-L98](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L84-L98)（`test4` 模块），它配置了 `fallback = ["zh", "en"]`。
2. 阅读它的两条断言：
   - `_rust_i18n_translate("zh-CN", "messages.zero")` 期望 `"你没有消息。"`；
   - `_rust_i18n_translate("zh-CN", "messages.one")` 期望 `"You have one message."`。
3. 对照翻译数据：`zh-CN.yml` 里**没有** `messages.zero` 与 `messages.one`；`zh.yml`（即 locale `zh`）里有 `messages.zero: 你没有消息。` 但**没有** `messages.one`；`en.yml` 里有 `messages.one: You have one message.`。

**需要观察的现象**：

- `messages.zero`：`zh-CN` 精确 miss → territory 削到 `zh` 命中 → 返回中文。**territory 段就搞定了，根本没轮到显式列表。**
- `messages.one`：`zh-CN` 精确 miss → territory 削到 `zh` 仍 miss → territory 耗尽 → 走显式列表 `["zh","en"]`，`zh` 再 miss、`en` 命中 → 返回英文。

**预期结果**：两条断言成立（这是仓库自带的测试，`cargo test --test integration_tests test4` 应通过；如未本地运行，标注**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：`test4` 里查 `messages.zero` 时，显式列表 `["zh","en"]` 里的 `en` 会被尝试吗？

> **答案**：不会。territory 回退在 `zh` 就命中并 `return` 了，`while` 循环之后的显式列表 `find_map` 根本没机会执行。这印证了「命中即返回、后续不再尝试」。

**练习 2**：为什么 `_tr!` 选择调用 `_rust_i18n_try_translate`（返回 `Option`）而不是 `_rust_i18n_translate`（直接返回字符串）？

> **答案**：因为 `t!` 在 miss 时想返回**用户写的原始消息**（如 `t!("Hello")` miss 时返回 `"Hello"`），而不是 `"locale.key"` 这种调试用占位串。`try_translate` 用 `None` 把「没找到」这个信号交还给 `_tr!`，由 `_tr!` 决定拿原文顶上；`_translate` 的 `"locale.key"` 兜底只服务于测试等需要明确看到「哪个 locale 哪个 key 没中」的场景。

---

### 4.2 _rust_i18n_lookup_fallback：territory 自动逐级回退

#### 4.2.1 概念说明

`_rust_i18n_lookup_fallback` 是 territory 回退段的核心零件，职责极其单一：**给定一个 locale，返回它「去掉最后一段子标签」后的父级 locale。** 例如：

- `"zh-Hant-CN"` → `"zh-Hant"`
- `"zh-Hant"` → `"zh"`
- `"zh"` → `None`（没有 `-`，削不动了）

这个「逐级削最后一段」的做法，对应 IETF **RFC 4647** 的 **Lookup（查找）** 算法思路：当请求的精确语言标签没有匹配资源时，按一定规则逐步去掉末尾子标签再试，直到命中或只剩主语言。源码注释直接引用了该规范：

[crates/macro/src/lib.rs:L355-L359](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L355-L359) —— 注释给出例子 `"zh-Hant-CN-x-private1-private2"` → ... → `"zh"`，并指向 RFC 4647 §3.4。

> 为什么需要它？现实里用户/浏览器常给出带地区的 locale（`zh-Hant-CN`、`en-GB`），但翻译文件往往只准备了语言级别（`zh`、`en`）。territory 回退让「只翻译了 `zh`」的项目也能服务 `zh-Hant-CN` 用户，而不必为每个地区变体都复制一份译文。

#### 4.2.2 核心流程

函数体只有一行，但干了两件事：

1. `locale.rfind('-')`：从右往左找第一个 `-` 的字节位置，得到 `Option<usize>`；
2. 若找到位置 `n`：取 `locale[..n]`（即删掉最后一段 `-xxx`），再 `trim_end_matches("-x")` 去掉可能残留的 `-x` 私有用途标记；
3. 若没找到 `-`：返回 `None`，表示已经削到主语言、无父级。

把 locale 看作由 `-` 连接的子标签序列 \( L = s_0\text{-}s_1\text{-}\cdots\text{-}s_n \)，则一次 `lookup_fallback` 的效果是：

\[
\text{lookup\_fallback}(s_0\text{-}\cdots\text{-}s_n) \;=\; s_0\text{-}\cdots\text{-}s_{n-1}
\]

特殊地，当 \(s_n\) 削掉后恰好让末尾露出 `-x`（私有用途子标签，RFC 5646 规定 `-x-...` 之后都是私有内容）时，`trim_end_matches("-x")` 会顺手把这段也去掉，避免留下一个无意义的 `-x` 尾巴。连续调用即得到完整的逐级序列：

\[
s_0\text{-}\cdots\text{-}s_n \;\longrightarrow\; s_0\text{-}\cdots\text{-}s_{n-1} \;\longrightarrow\; \cdots \;\longrightarrow\; s_0 \;\longrightarrow\; \text{None}
\]

这正是 `_rust_i18n_try_translate` 里 `while let Some(...)` 循环逐轮推进的结果。

#### 4.2.3 源码精读

[crates/macro/src/lib.rs:L363-L365](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L363-L365) —— `_rust_i18n_lookup_fallback`：`locale.rfind('-').map(|n| locale[..n].trim_end_matches("-x"))`。`rfind` 找最后一个 `-`，`map` 把位置转成切片，`trim_end_matches("-x")` 清理私有标记。

逐个例子手动验证（对着注释里的例子 `"zh-Hant-CN-x-private1-private2"`）：

| 输入 locale | `rfind('-')` 切出的前缀 | `trim_end_matches("-x")` 后 | 说明 |
| --- | --- | --- | --- |
| `zh-Hant-CN-x-private1-private2` | `zh-Hant-CN-x-private1` | `zh-Hant-CN-x-private1` | 末尾不是 `-x`，不变 |
| `zh-Hant-CN-x-private1` | `zh-Hant-CN-x` | `zh-Hant-CN` | 削掉 `private1` 后露出 `-x`，被 trim 掉 |
| `zh-Hant-CN` | `zh-Hant` | `zh-Hant` | 正常削最后一段 |
| `zh-Hant` | `zh` | `zh` | 继续削 |
| `zh` | — | `None` | 无 `-`，返回 `None` |

这张表与源码注释承诺的序列完全一致。

> 关于 `trim_end_matches("-x")` 的边界：它只删末尾**恰好是 `-x`** 的情况，不会误伤正常的子标签。例如 `zh-Hant` 切出 `zh` 后并不以 `-x` 结尾，trim 啥也不做。这是一处很小但很关键的兼容性处理，让私有用途标签（`-x-...`）能被正确「整段」跳过。

#### 4.2.4 代码实践

**实践目标**：用纯 Rust 片段复刻 `lookup_fallback` 的行为，验证逐级削标签序列。

**操作步骤**：

1. 在任意一个临时 Rust 文件（或 `rustc` 直接编译）里写：
   ```rust
   // 示例代码：复刻 _rust_i18n_lookup_fallback 的行为
   fn lookup_fallback(locale: &str) -> Option<&str> {
       locale.rfind('-').map(|n| locale[..n].trim_end_matches("-x"))
   }

   fn main() {
       let mut cur = "zh-Hant-CN-x-private1-private2";
       while let Some(parent) = lookup_fallback(cur) {
           println!("{}", parent);
           cur = parent;
       }
   }
   ```
2. 编译运行（`rustc demo.rs && ./demo`，或放进一个 Cargo 项目的 `main.rs`）。

**需要观察的现象**：输出应当依次是 `zh-Hant-CN-x-private1` → `zh-Hant-CN` → `zh-Hant` → `zh`，然后停止。

**预期结果**：与 4.2.3 表格一致。这是对源码逻辑的等价复刻，可放心对照；若你选择直接运行，结果即为上述序列（**待本地验证**仅指你是否亲自跑了它）。

#### 4.2.5 小练习与答案

**练习 1**：`lookup_fallback("en-GB")` 返回什么？`lookup_fallback("en")` 呢？

> **答案**：`"en-GB"` 的 `rfind('-')` 定位到 `GB` 前的 `-`，切出 `"en"`，不以 `-x` 结尾，返回 `Some("en")`。`"en"` 没有 `-`，`rfind` 返回 `None`，整个函数返回 `None`。

**练习 2**：如果把 `trim_end_matches("-x")` 去掉，对 `zh-Hant-CN-x-private1` 这个输入会产生什么异常结果？

> **答案**：削掉 `private1` 后会得到 `zh-Hant-CN-x`，这个 locale 几乎不可能在翻译文件里存在，于是多一次注定 miss 的查找；下一轮再从 `zh-Hant-CN-x` 削成 `zh-Hant-CN`。功能上最终仍能命中（只是多绕一圈、多一次无效查表），但 `trim_end_matches("-x")` 让它一步到位、更干净。

---

### 4.3 _RUST_I18N_FALLBACK_LOCALE：显式 fallback 列表

#### 4.3.1 概念说明

territory 回退是「按 locale 自身结构自动削」的机械规则，但有时你想**主动指定**一批兜底语言，顺序无关 locale 的层级。例如一个中文产品希望「任何查不到的情况都先试英文」，这就需要**显式 fallback 列表**。

rust-i18n 用一个编译期生成的静态量来承载它：

[crates/macro/src/lib.rs:L349](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L349) —— `static _RUST_I18N_FALLBACK_LOCALE: Option<&[&'static str]>`：`Some` 时是字符串切片数组（如 `Some(&["zh", "en"])`），未配置则是 `None`。

它的值来自 `i18n!(fallback = ...)` 参数，支持两种写法（见 [u2-l1](u2-l1-i18n-macro-arg-parsing.md) 的 `consume_fallback`）：

- 单字符串：`fallback = "en"` → `Some(&["en"])`；
- 字符串数组：`fallback = ["zh", "en"]` → `Some(&["zh", "en"])`。

#### 4.3.2 核心流程

显式列表的「生命周期」分三步：

1. **编译期生成**：`generate_code` 根据 `Args.fallback`（`Option<Vec<String>>`）用 `quote!` 拼出 `Some(&[#(#fallback),*])` 或 `None`，注入调用方 crate；
2. **静态存放**：编译后它是一个进程级静态量，整个程序只此一份、不可变；
3. **运行期消费**：`_rust_i18n_try_translate` 在 territory 回退耗尽后，用 `fallback.iter().find_map(...)` 按数组顺序逐个查后端，命中即返回。

优先级上，**显式列表是三段里最低的**：只有精确命中和全部 territory 父级都没中时才轮到它。这点务必记住——它不是「首选语言」，而是「最后的兜底」。

> 再次提醒 2 节末尾的误解点：`fallback` **只能**在 `i18n!` 处配置。`t!("key", fallback = "en")` 里的 `fallback = "en"` **不会**进到这个静态量，而是被 `filter_arguments` 当成普通插值变量保留下来（见 [crates/macro/src/tr.rs:L342-L376](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L342-L376)：`filter_arguments` 只分流 `locale` 与 `_minify_key*` 五个特殊名，`fallback` 落入 `_ => {}` 被当普通参数）。所以那个写法对回退**没有任何作用**，顶多在译文里有 `%{fallback}` 占位符时被替换掉。

#### 4.3.3 源码精读

**生成处**——把 `Args.fallback` 翻译成 `quote!` token：

[crates/macro/src/lib.rs:L311-L319](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L311-L319) —— `if let Some(fallback) = args.fallback { Some(&[#(#fallback),*]) } else { None }`。`#(#fallback),*` 是 `quote!` 的重复展开语法，把 `Vec<String>` 逐个铺成 `"zh", "en"` 这样的字面量序列。

**存放处**——见上文 L349 的静态量声明。

**消费处**——在 `_rust_i18n_try_translate` 末尾：

[crates/macro/src/lib.rs:L396-L398](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L396-L398) —— `_RUST_I18N_FALLBACK_LOCALE.and_then(|fallback| fallback.iter().find_map(|locale| _RUST_I18N_BACKEND.translate(locale, key.as_ref())))`：外层 `.and_then` 保证没配 fallback 时整段为 `None` 直接跳过；`find_map` 在列表里**按声明顺序**找首个命中。

**测试佐证**——单字符串 fallback 的最小用例：

[tests/integration_tests.rs:L56-L66](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L56-L66) —— `test1` 配 `fallback = "en"`，断言 `_rust_i18n_translate("en", "missing.default")` 命中英文译文。这里 locale 本就是 `en`、精确命中，主要验证 fallback 链路能正常工作。

更能说明「显式列表兜底」的是这条用例（注意它发生在 crate 顶层 `i18n!(... fallback = "en")` 之下）：

[tests/integration_tests.rs:L252-L262](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L252-L262) —— `test_fallback_missing_locale`：`t!("missing.default", locale = "zh-CN")` 与 `locale = "foo"` 都期望返回英文 `"This is missing key fallbacked to en."`。

逐条拆解这两个断言（顶层 fallback 为 `["en"]`，`missing.default` 只在 `en.yml` 里有）：

- `locale = "zh-CN"`：精确 miss → territory 削到 `zh` 仍 miss → 显式列表 `en` 命中 → 英文。
- `locale = "foo"`：精确 miss → `lookup_fallback("foo")` 直接 `None`（没有 `-`，territory 段一步都不走）→ 显式列表 `en` 命中 → 英文。

后者特别能说明问题：**一个不含 `-` 的非法/未知 locale，territory 回退帮不上忙，全靠显式列表兜底。**

#### 4.3.4 代码实践

**实践目标**：验证「显式列表按声明顺序、首个命中即停」。

**操作步骤**：

1. 打开 [tests/integration_tests.rs:L84-L98](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L84-L98)（`test4`，`fallback = ["zh", "en"]`）。
2. 关注 `messages.one` 那条断言：`zh-CN` → territory `zh` miss → 显式列表先试 `zh`（再 miss）、再试 `en`（命中）。
3. 想象把 `fallback` 改成 `["en", "zh"]`（顺序对调）。

**需要观察的现象**：`messages.one` 仍返回英文——因为 `zh` 里根本没有 `messages.one`，无论 `zh` 排在列表第几位都会 miss，最终还是 `en` 命中。但如果某个键**同时**存在于 `zh` 和 `en`，那么列表顺序就会决定返回哪一种语言。

**预期结果**：当前 `["zh","en"]` 顺序下，`messages.one` 走到 `en` 命中；这印证了 `find_map` 是「顺序短路」的。修改顺序后对 `messages.one` 无影响（因为 `zh` 没有该键），但对「两语言都有」的键会有影响。如自行改测试运行，请用 `RUST_TEST_THREADS=1 cargo test`（见第 6 节与 [u8-l4](u8-l4-test-suite.md) 对单线程约束的说明）。

#### 4.3.5 小练习与答案

**练习 1**：如果 `i18n!` 完全不写 `fallback`，`_rust_i18n_try_translate` 在精确 miss 且 territory 也 miss 后会发生什么？

> **答案**：`_RUST_I18N_FALLBACK_LOCALE` 为 `None`，`.and_then(...)` 直接得到 `None`，函数整体返回 `None`；外层 `t!` 随即返回原始消息字符串。也就是说没有显式列表时，回退链只有「精确 + territory」两段。

**练习 2**：`t!("missing.default", locale = "zh-CN", fallback = "en")`（见 `test_lookup_fallback`）里那个 `fallback = "en"` 真的改变了回退语言吗？为什么测试还能拿到英文？

> **答案**：没有改变。`fallback = "en"` 被当成普通插值变量，不进 `_RUST_I18N_FALLBACK_LOCALE`。测试之所以拿到英文，是因为**crate 顶层** `i18n!(... fallback = "en")` 配置了显式列表 `["en"]`，`zh-CN` 经 territory `zh` miss 后由这个顶层列表兜底命中。`missing.default` 译文里没有 `%{fallback}` 占位符，所以那个多余参数也没产生可见副作用——这是一个「看起来像临时改 fallback、其实不是」的迷惑写法。

---

## 5. 综合实践

把三段回退串起来做一次完整的手动推演。这是本讲的核心练习。

**任务**：给定 `i18n!("locales", fallback = ["zh", "en"])`，跟踪 `t!("missing", locale = "zh-Hant-CN")` 的查找顺序，按顺序列出每一步尝试的 locale，直到命中或耗尽。

**前提数据**（沿用本仓库 `tests/locales` 的真实结构）：`missing` 在所有 locale 文件里都是**对象**（含 `missing.default`、`missing.lookup-fallback` 等子键），并没有一个叶子值直接挂在 `missing` 这个键上——因此它是一个「必然 miss」的键，正好能把整条链走到耗尽。

**推演步骤**（对照 [`_rust_i18n_try_translate`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L385-L400) 与 [`_rust_i18n_lookup_fallback`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L363-L365)）：

**第 1 段 · 精确命中**

| # | 尝试的 locale | backend.translate 结果 |
| --- | --- | --- |
| 1 | `zh-Hant-CN` | None（无此 locale） |

**第 2 段 · territory 自动回退**（`current_locale` 从 `zh-Hant-CN` 开始，每轮 `lookup_fallback` 推进）

| # | lookup_fallback(current) 得到 | 尝试的 locale | backend.translate 结果 |
| --- | --- | --- | --- |
| 2 | `zh-Hant-CN` → `zh-Hant` | `zh-Hant` | None |
| 3 | `zh-Hant` → `zh` | `zh` | None |
| — | `zh` → `None`（无 `-`） | （循环退出） | — |

**第 3 段 · 显式列表 `["zh", "en"]`**（`find_map` 按序）

| # | 尝试的 locale | backend.translate 结果 |
| --- | --- | --- |
| 4 | `zh` | None（第二次尝试 `zh`，仍 miss） |
| 5 | `en` | None（`missing` 是对象，无叶子值） |

**结论**：

- 完整查找顺序为：`zh-Hant-CN` → `zh-Hant` → `zh` → `zh`（显式）→ `en`（显式），全部 miss。
- `_rust_i18n_try_translate` 返回 `None`，`t!` 进而返回**原始消息 `"missing"`**（不是 `"zh-Hant-CN.missing"`，因为 `t!` 走的是 `try_translate` 而非 `_translate`，见 4.1.3）。
- 注意第 3 步与第 4 步**都试了 `zh`**：territory 段已经路过 `zh` 没中，显式列表里又写了一次 `zh`，于是被重复尝试——这正是 4.1.2 提到的「显式列表不去重 territory 结果」的体现。

**进阶验证（可选）**：在 `tests/locales` 下新增一个 `zh-Hant-CN.yml`（或 `zh-Hant.yml`）并在其中放一个 `missing: <某字符串>`，重复上述推演，观察命中会在第几步提前发生。这一步需要你新建文件，**待本地验证**。

## 6. 本讲小结

- 回退由 `_rust_i18n_try_translate` 编排成**三段递进**：精确命中 → territory 自动回退 → 显式 fallback 列表，**优先级从高到低**，任一段命中即返回。
- `_rust_i18n_lookup_fallback` 用 `locale.rfind('-')` 逐级削掉最后一个子标签（RFC 4647 Lookup 思路），并用 `trim_end_matches("-x")` 清理私有用途标记；locale 无 `-` 时返回 `None` 终止循环。
- `_RUST_I18N_FALLBACK_LOCALE` 是编译期由 `i18n!(fallback = ...)` 生成的 `Option<&[&str]>` 静态量，运行期用 `find_map` 按声明顺序逐个查；未配 fallback 时整段跳过。
- `t!` 实际调用的是 `_rust_i18n_try_translate`（返回 `Option`），全 miss 时返回**原始消息**；而 `_rust_i18n_translate`（返回 `"locale.key"`）只是测试用的便利包装，二者兜底策略不同，勿混淆。
- **fallback 只能在 `i18n!` 配置**：`t!(..., fallback = "en")` 不会临时改回退语言，那个参数会被当成普通插值变量（`test_lookup_fallback` 里就是这么个「迷惑写法」）。
- 不含 `-` 的未知 locale（如 `"foo"`）territory 段一步都不走，完全靠显式列表兜底。

## 7. 下一步学习建议

- **回到调用链全貌**：本讲把 `t!` → `_tr!` → `_rust_i18n_try_translate` 里「查不到」的分支讲透了，建议重读 [u3-l1](u3-l1-t-macro-call-chain.md) 把三跳链路与回退段在脑子里拼成一张完整时序图。
- **去看后端如何 `.translate`**：本讲里 `_RUST_I18N_BACKEND.translate(locale, key)` 是个黑盒，下一单元 [u4-l1](u4-l1-backend-trait-simplebackend.md) 会拆开 `Backend` trait 与 `SimpleBackend` 的 `HashMap` 查找实现，并解释「组合后端」如何改变回退段的查表源头（[u4-l2](u4-l2-backendext-combinedbackend.md)）。
- **并发与测试**：回退链依赖全局静态后端与全局 locale，多线程下的安全性留到 [u8-l1](u8-l1-atomicstr-thread-safety.md)；而「为何集成测试必须 `RUST_TEST_THREADS=1`」会在 [u8-l4](u8-l4-test-suite.md) 系统讲解——这正好解释了本讲多次出现的单线程运行提示。
