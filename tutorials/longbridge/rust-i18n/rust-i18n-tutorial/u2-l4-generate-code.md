# generate_code 生成的运行时代码

## 1. 本讲目标

本讲是「编译期代码生成主链路」的最后一环。前面三讲我们已经走完了：

- u2-l1：`i18n!` 宏把 token 流解析成 `Args`；
- u2-l2：编译期扫描并解析 yml/json/toml 文件；
- u2-l3：多文件合并、嵌套键拍平成点号键。

本讲要回答的问题是：**拍平之后的那张大表 `BTreeMap<locale, BTreeMap<key, value>>`，最终被 `i18n!` 宏变成了什么样的 Rust 代码？运行时 `t!` 又是靠这些代码里的哪一个函数取到译文的？**

学完本讲，你应该能够：

1. 看懂 `generate_code` 如何用 `quote!` 把翻译数据「总装」成一段完整的 Rust 代码。
2. 理解生成的静态后端 `_RUST_I18N_BACKEND` 为什么用 `LazyLock` 延迟初始化、为什么类型是 `Box<dyn Backend>`。
3. 掌握 `_rust_i18n_try_translate` 调用 `backend.translate` 的入口，以及它和外层 `_rust_i18n_translate` 的分工。
4. 能够用 `RUST_I18N_DEBUG=1` 亲自看到这段生成代码，并指出其中的静态后端、查找函数和内部宏。

---

## 2. 前置知识

- **过程宏的返回值是「代码」**：`i18n!` 是一个 `#[proc_macro]`，它在编译期运行，吃进 token 流，吐出**另一段 token 流**——也就是要插到调用点的 Rust 代码。本讲的主角 `generate_code` 就是「生产这段代码」的函数。它本身不在你的最终二进制里运行，它运行的结果（生成的代码）才会在运行时被执行。
- **`quote!` 宏**：来自 `quote` 库，用 `#变量` 插值，把一段「带占位符的 Rust 代码模板」变成 `proc_macro2::TokenStream`。可以把它理解成「Rust 代码的字符串模板」，但产物是结构化的 token 而不是文本。
- **`LazyLock`**：Rust 标准库提供的「延迟初始化 + 线程安全」容器。被它包裹的值只会在第一次被访问时构造一次，之后所有人共享同一个实例。这正是全局后端需要的样子。
- **`Cow<str>`（写时复制）**：一个「可能是借用、也可能是 owned」的字符串。命中翻译时返回 `Cow::Borrowed`（零拷贝，指向静态字符串字面量）；未命中需要拼接时返回 `Cow::Owned`。
- **trait object `Box<dyn Backend>`**：把不同具体后端（`SimpleBackend` 或 `CombinedBackend`）统一擦除成同一个静态变量的类型，方便后续 `.extend()` 组合。

> 关键定位：`generate_code` 生成的代码**注入到调用 `i18n!` 的那个 crate 里**（通常是你的应用 crate），而不是 rust-i18n 库自身。所以你会看到生成代码里到处用 `rust_i18n::...` 来引用「外部那个库」，而 `t!` 宏则转发到本 crate 内的 `crate::_rust_i18n_t!`。这一点是理解整段代码的关键。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/macro/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) | 本讲核心。`i18n!` 过程宏入口、`generate_code` 代码生成函数，以及生成的所有运行时 item 都在这里用 `quote!` 定义。 |
| [crates/support/src/backend.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs) | `Backend` trait、`SimpleBackend` 及 `add_translations` / `translate` 的实现——生成代码在运行时调用的就是它们。 |
| [crates/support/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs) | `is_debug()`、`load_locales()` 等编译期辅助函数所在文件。 |
| [src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs) | 根 crate 门面：`CURRENT_LOCALE`、`set_locale`、`locale()`、转发壳 `t!` / `available_locales!`。生成代码会引用这里的符号。 |

---

## 4. 核心概念与源码讲解

### 4.1 generate_code：编译期代码生成的总装车间

#### 4.1.1 概念说明

经过 u2-l3 的拍平，`load_locales` 产出了一张大表：

```
BTreeMap<
  String,                       // locale，例如 "en"
  BTreeMap<String, String>      // 点号键 -> 译文
>
```

`generate_code` 的职责就是：**把这张「内存里的数据结构」翻译成「等价的 Rust 源代码」**，让翻译数据以代码（字符串字面量 + 静态变量）的形式编译进二进制。这样运行时就不再需要读文件、不再需要 yml/json 解析器，一次 `t!` 调用只是一次 HashMap 查找。

它是一个纯函数：吃进翻译表 + 已解析好的 `Args`，吐出一个 `proc_macro2::TokenStream`。

#### 4.1.2 核心流程

`generate_code` 采用「**分块拼装，最后合并**」的策略，先把几个易变的片段各自 `quote!` 成局部 token 流，再在一个大 `quote!` 里把它们插值到模板的对应位置：

```
generate_code(translations, args)
  │
  ├─ ① all_translations：把每个 locale 编译成一句
  │      backend.add_translations("en", { HashMap 内嵌字面量 })
  │
  ├─ ② default_locale（可选）：把 Cargo.toml 里的默认 locale 应用到全局
  ├─ ③ fallback（可选/None）：Some(&["en"]) 或 None
  ├─ ④ extend_code（可选）：let backend = backend.extend(自定义后端);
  ├─ ⑤ minify_key 四个常量
  │
  └─ ⑥ 大 quote! 模板：把上面片段插进
         _RUST_I18N_BACKEND = LazyLock::new(|| { ① ④ ② ; Box::new(backend) })
         _RUST_I18N_FALLBACK_LOCALE = ③
         _RUST_I18N_MINIFY_KEY* = ⑤
         _rust_i18n_lookup_fallback()
         _rust_i18n_translate()
         _rust_i18n_try_translate()
         _rust_i18n_available_locales()
         macro_rules! __rust_i18n_t / __rust_i18n_tkv
```

注意 ⑥ 这一步在 `i18n!` 入口里被打印（当 `RUST_I18N_DEBUG=1` 时），这就是我们实践任务要看的输出。

#### 4.1.3 源码精读

`i18n!` 入口很薄：解析参数 → 加载文件 → 调 `generate_code` →（可选）打印 → 返回 token 流。

[i18n! 入口与 generate_code 调用 — crates/macro/src/lib.rs:248-268](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L248-L268) — 第 257 行 `load_locales(...)` 拿到扁平表 `data`，第 258 行 `let code = generate_code(data, args)` 生成代码；第 260-265 行的 `if is_debug()` 块把生成代码原样 `println!`，这正是 `RUST_I18N_DEBUG=1` 能看到输出的原因。

`generate_code` 的函数签名与逐块拼装：

[generate_code 签名 — crates/macro/src/lib.rs:270-273](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L270-L273) — 入参 `translations: BTreeMap<String, BTreeMap<String, String>>` 就是 u2-l3 拍平后的产物。

[把每个 locale 编成 add_translations 调用 — crates/macro/src/lib.rs:274-296](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L274-L296) — 这里有两层 `quote!`。内层把每个 `(k, v)` 编成 `Cow::Borrowed(#k), Cow::Borrowed(#v)`，再用 `#( map.insert(#translation); )*` 展开成逐条 `insert`；外层套上 `backend.add_translations(#locale, { ... })`。注意 `#k`/`#v` 是字符串字面量插值，运行时它们是静态字符串，`Cow::Borrowed` 直接借用，零拷贝。

[default_locale / fallback / extend_code 三个可选片段 — crates/macro/src/lib.rs:298-327](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L298-L327) — `if let Some(...)` 控制片段是否存在。`fallback` 片段把 `Vec<String>` 展开成 `Some(&[#(#fallback),*])`（一个静态字符串切片数组）或 `None`；`extend_code` 把 `backend = ...` 的表达式（即宏参数 `backend = xxx` 的 AST）插进 `backend.extend(#extend)`。

[最后的总装模板 — crates/macro/src/lib.rs:334-433](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L334-L433) — 这就是最终生成代码的骨架：第 341-347 行定义 `_RUST_I18N_BACKEND` 静态后端，第 349-353 行定义 5 个配置静态量，第 363 行起是三个查找/枚举函数，第 413 行起是两个内部宏。本讲下面几个小节会逐一展开。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：在脑子里跑一遍「数据 → 代码」的映射，确认 `generate_code` 只是机械地把内存结构翻写成源码。
2. **操作步骤**：
   - 假设拍平后的表是 `{"en": {"hello": "Hello"}, "zh-CN": {"hello": "你好"}}`。
   - 对照 [第 274-296 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L274-L296)，手写出 `all_translations` 展开后大致的样子。
3. **预期结果**（示例代码，非项目原文）：

   ```rust
   // 示例代码：示意 generate_code 对两个 locale 的展开
   let mut backend = rust_i18n::SimpleBackend::new();
   backend.add_translations(
       "en",
       {
           let mut map = std::collections::HashMap::with_capacity(1);
           map.insert(::std::borrow::Cow::Borrowed("hello"), ::std::borrow::Cow::Borrowed("Hello"));
           map
       },
   );
   backend.add_translations(
       "zh-CN",
       {
           let mut map = std::collections::HashMap::with_capacity(1);
           map.insert(::std::borrow::Cow::Borrowed("hello"), ::std::borrow::Cow::Borrowed("你好"));
           map
       },
   );
   ```
4. **待本地验证**：片段中的 `with_capacity(1)` 对应 `translation.len()`，键值顺序因源表是 `BTreeMap` 而按键名字典序排列——这一点在真实输出里可以核对。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `generate_code` 要把翻译数据编成「代码里的字符串字面量」，而不是在运行时读 yml 文件？
  - **答案**：编成代码后，翻译数据随二进制分发，运行时零文件 IO；yml/json 解析器（`serde_yaml`/`serde_json`/`toml`）作为可选依赖可以不进最终二进制（见 u5-l3 的 feature 机制）。这也是 rust-i18n 与 gettext 类「运行时读文件」库的根本区别（u2-l2 已建立这一认知）。
- **练习 2**：`generate_code` 的返回类型是 `proc_macro2::TokenStream`，而 `i18n!` 过程宏对外返回 `proc_macro::TokenStream`，两者如何衔接？
  - **答案**：在 [i18n! 入口第 267 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L267) `code.into()`，通过 `From` 把 `proc_macro2::TokenStream` 转成 `proc_macro::TokenStream`。`proc_macro2` 是可在更多上下文（如测试、`quote!`）使用的「过程宏 token 流」的镜像类型。

---

### 4.2 SimpleBackend::add_translations：把扁平翻译表灌入静态后端

#### 4.2.1 概念说明

`generate_code` 生成的初始化代码，运行时会调用 `rust_i18n::SimpleBackend::new()` 建一个空后端，再对每个 locale 调一次 `add_translations` 把译文灌进去。`SimpleBackend` 就是 rust-i18n 内置的「键值存储后端」——它内部是一个嵌套 HashMap：`locale -> (key -> value)`。

理解这一节有两个要点：
1. **生成的静态后端本质上就是一个 `SimpleBackend`**（如果没传 `backend=` 参数的话）。我们在 u4-l1 会专门讲 `Backend` trait，这里只看它如何被「装填」。
2. **为什么用 `Cow<'static, str>`**：键和值在生成代码里都是字符串字面量，`Cow::Borrowed` 直接借用静态数据，不产生堆分配。

#### 4.2.2 核心流程

```
SimpleBackend::new()                      // 空 translations: HashMap<locale, HashMap<key, value>>
  └─ for each locale:
       add_translations(locale, data)
         ├─ translations.entry(locale).or_default()   // 拿到/新建该 locale 的内层 map
         └─ trs.extend(data)                           // 把本次 data 合并进去（同键覆盖）
```

因为合并其实已经在编译期 `merge_value`（u2-l3）阶段做完了，所以 codegen 阶段每个 locale 只对应**一次** `add_translations`，传入一张完整的扁平表。

#### 4.2.3 源码精读

[SimpleBackend 的存储结构 — crates/support/src/backend.rs:69-72](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L69-L72) — `translations: HashMap<Cow<'static,str>, HashMap<Cow<'static,str>, Cow<'static,str>>>`，注释明确「All translations key is flatten key, like `en.hello.world`」——注意这里说的是**逻辑形态**，实际 key 已经是 `hello.world` 这种点号键，locale 单独存在外层。

[SimpleBackend::new — crates/support/src/backend.rs:96-102](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L96-L102) — 建一个空 `HashMap`，对应生成代码里的 `rust_i18n::SimpleBackend::new()`。

[add_translations — crates/support/src/backend.rs:104-122](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L104-L122) — 第 120 行 `entry(locale).or_default()` 拿到该 locale 的内层 map（不存在则新建），第 121 行 `trs.extend(data)` 把传入的键值合并进去；`extend` 对重复键是「后写覆盖」，正好满足「一次灌入完整表」的语义。

[生成代码里调用 add_translations 的位置 — crates/macro/src/lib.rs:290-296](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L290-L296) — `let mut backend = rust_i18n::SimpleBackend::new();` 后跟 `#( backend.add_translations(#all_translations); )*`，对每个 locale 展开成一次调用。

最终这个 `backend` 在 [第 346 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L341-L347) 被 `Box::new(backend)` 装进 `LazyLock`，类型擦除成 `Box<dyn rust_i18n::Backend>`。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：不依赖 `i18n!` 宏，手动用 `SimpleBackend` 复现生成代码做的事。
2. **操作步骤**：阅读 [backend.rs 内联测试 test_simple_backend — crates/support/src/backend.rs:163-185](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L163-L185)。
3. **需要观察的现象**：测试里 `SimpleBackend::new()` 后分别用 `add_translations("en", ...)` 和 `add_translations("zh-CN", ...)` 灌入两种语言，再用 `translate("en", "hello")` 取值。
4. **预期结果**：这正是 `generate_code` 生成代码在运行时的等价行为——只不过生成代码把「数据」变成了字符串字面量，而这个单元测试用的是运行时构造的 `HashMap`。

#### 4.2.5 小练习与答案

- **练习 1**：`add_translations` 里用的是 `trs.extend(data)` 而不是「先清空再写入」，这样设计有什么好处？
  - **答案**：支持对同一 locale **多次** `add_translations` 并增量合并（后写覆盖同键）。在自定义后端场景（u4-l3）里，用户可以分批灌入翻译而不必一次性构造完整表。
- **练习 2**：生成代码里 `HashMap::with_capacity(#translation_length)` 的 `translation_length` 来自哪里？
  - **答案**：来自 [第 275 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L274-L278) 的 `translation.len()`，即该 locale 的键数量。预分配容量是为了避免逐条 `insert` 时 HashMap 反复扩容/rehash。

---

### 4.3 _rust_i18n_try_translate：后端查找与回退的真正入口

#### 4.3.1 概念说明

运行时 `t!` 经过层层转发（详见 u3-l1），最终调用的是生成代码里的 `_rust_i18n_try_translate`。它是「真正去后端里查表」的函数，返回 `Option<Cow>`：查到返回 `Some`，查不到返回 `None`。

它实现了 rust-i18n 的两层回退策略：
1. **territory 回退**（自动）：`zh-Hant-CN` → `zh-Hant` → `zh`，逐级去掉 `-` 子标签。
2. **显式 fallback 列表**（用户在 `i18n!(fallback=[...])` 配置的）：territory 回退也 miss 后，再按列表逐个试。

> 本节只讲「入口与骨架」，回退策略的完整剖析（含 RFC 4647 细节、显式列表优先级）放在 u3-l4，避免重复。

#### 4.3.2 核心流程

```
_rust_i18n_try_translate(locale, key)
  │
  1. _RUST_I18N_BACKEND.translate(locale, key)        // 精确 locale 直接查
  │     命中 → return Some(value)
  │
  2. 否则进入 or_else 回退：
     │
     ├─ territory 回退循环：
     │    current = locale
     │    while let Some(parent) = _rust_i18n_lookup_fallback(current):
     │        命中 backend.translate(parent, key) → return Some(value)
     │        current = parent
     │
     └─ 显式 fallback 列表（_RUST_I18N_FALLBACK_LOCALE）：
          对列表里每个 locale 调 backend.translate，命中即返回
  │
  全 miss → return None
```

`_RUST_I18N_BACKEND.translate` 本身是 trait 方法，最终落到 `SimpleBackend::translate`：

[SimpleBackend::translate — crates/support/src/backend.rs:132-138](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L132-L138) — 先 `translations.get(locale)` 拿到该 locale 的内层 map，再 `trs.get(key).cloned()` 取值；两层 `HashMap::get`，命中即 `Some`，否则 `None`。这就是「一次 `t!` = 一次 HashMap 查找」的底层实现。

#### 4.3.3 源码精读

[_rust_i18n_try_translate — crates/macro/src/lib.rs:381-400](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L381-L400) — 第 386 行 `_RUST_I18N_BACKEND.translate(locale, key.as_ref())` 是精确查找入口；第 387-399 行的 `.or_else(...)` 是回退闭包：第 388-394 行是 territory 回退循环（`current_locale` 不断被 `_rust_i18n_lookup_fallback` 缩短），第 396-398 行是显式 fallback 列表的 `find_map`。注意 `key: impl AsRef<str>`，内部统一用 `key.as_ref()`。

[_rust_i18n_lookup_fallback — crates/macro/src/lib.rs:355-365](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L355-L365) — 用 `rfind('-')` 找到最后一个 `-` 的位置，返回前半段；`trim_end_matches("-x")` 顺便去掉 IETF 语言标签里的 `-x-private` 私有扩展。函数文档给的例子很直观：`zh-Hant-CN-x-private1-private2 → zh-Hant-CN-x-private1 → zh-Hant-CN → zh-Hant → zh`。

[_RUST_I18N_FALLBACK_LOCALE 静态量 — crates/macro/src/lib.rs:349](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L349) — 类型 `Option<&[&'static str]>`，由 4.1 节的 `fallback` 片段填充；没配 `fallback=` 时就是 `None`，第 396 行的 `.and_then(...)` 会直接短路返回 `None`。

#### 4.3.4 代码实践（跟踪型）

1. **实践目标**：跟踪一次 miss 的查找，看回退链如何逐级尝试。
2. **操作步骤**：假设后端里只有 `en` 和 `zh` 两个 locale，且配置了 `i18n!("locales", fallback = ["en"])`。跟踪 `t!("only_in_fr", locale = "zh-Hant-CN")` 的查找过程。
3. **需要观察的现象**：按 [第 386-398 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L381-L400) 顺序列出每一步尝试的 locale。
4. **预期结果**（示例分析）：
   - 精确查 `zh-Hant-CN` → miss；
   - territory 回退：`zh-Hant` → miss；`zh` → **命中或 miss 视后端内容而定**；
   - 若 `zh` 也 miss，进入显式列表：`en` → 命中或最终 `None`。
5. **待本地验证**：精确的「命中/miss」取决于后端里实际有没有该 key，请用一个最小 yml 文件本地复现。

#### 4.3.5 小练习与答案

- **练习 1**：`_rust_i18n_try_translate` 的 `key` 参数类型是 `impl AsRef<str>` 而不是 `&str`，为什么？
  - **答案**：为了同时接受 `&str`、`String`、`Cow<str>` 等多种字符串类型（调用方 `_tr!` 生成的代码可能传入不同形态），用 `AsRef<str>` 统一化，内部 `.as_ref()` 取 `&str`。
- **练习 2**：territory 回退和显式 fallback 列表，谁先执行？
  - **答案**：territory 回退**先**执行（第 388-394 行的 `while` 循环），全部 miss 后**才**轮到显式 fallback 列表（第 396-398 行）。这与 u3-l4 将给出的优先级结论一致。

---

### 4.4 _rust_i18n_translate 与其余生成物：守卫包装、枚举与内部宏

#### 4.4.1 概念说明

除了 `try_translate`，`generate_code` 还生成了几个配套 item，它们共同构成 `t!` 能正常工作的「运行时支撑面」：

- `_rust_i18n_translate`：`try_translate` 的「带默认值」包装。查不到时返回 key 本身（或 `locale.key`）而不是 `None`，所以它的返回类型是无 `Option` 的 `Cow<'r, str>`。
- `_rust_i18n_available_locales`：列出所有可用 locale，供 `available_locales!` 宏使用。
- 5 个配置静态量：`_RUST_I18N_FALLBACK_LOCALE`、`_RUST_I18N_MINIFY_KEY` 等。
- 两个内部宏 `__rust_i18n_t` / `__rust_i18n_tkv`：把 `i18n!` 解析出的 minify_key 配置「烤」进每一次 `_tr!` / `_minify_key!` 调用，再以 `_rust_i18n_t` / `_rust_i18n_tkv` 之名 `pub(crate) use` 暴露给本 crate 的 `t!` / `tkv!` 转发壳。

#### 4.4.2 核心流程

```
t!("hello")                                  // 用户调用
  └─ crate::_rust_i18n_t!("hello")            // t! 转发壳（src/lib.rs）
       └─ rust_i18n::_tr!("hello", _minify_key=..., ...)   // __rust_i18n_t 注入 minify 配置
            └─（_tr! 过程宏生成）
                 _rust_i18n_try_translate(locale, key)     // 真正查表
                   或
                 _rust_i18n_translate(locale, key)         // 带默认值的包装
```

关键点：`__rust_i18n_t` 宏把 `i18n!` 在编译期读到的四个 minify_key 参数**附加**到每一次 `_tr!` 调用末尾。这样 `t!` 的使用者完全感知不到 minify_key 配置，但它确实生效了。这也是为什么每个 crate 各自调 `i18n!` 后，`t!` 都能用本 crate 的配置——配置被「烤」进了本 crate 的 `_rust_i18n_t!`。

#### 4.4.3 源码精读

[_rust_i18n_translate — crates/macro/src/lib.rs:367-379](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L367-L379) — 第 372 行调用 `_rust_i18n_try_translate`，第 372-378 行的 `unwrap_or_else` 处理 miss：`locale` 为空时返回 `key` 本身（`key.into()` → `Cow::Borrowed`），否则返回 `format!("{}.{}", locale, key)`（`Cow::Owned`）。这就是「查不到就把 key 当译文」的兜底行为。

[_rust_i18n_available_locales — crates/macro/src/lib.rs:402-409](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L402-L409) — 调 `_RUST_I18N_BACKEND.available_locales()` 后 `locales.sort()` 排序。它对应 `SimpleBackend` 的实现 [backend.rs:126-130](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L126-L130)（取出 keys 并排序），最终被根 crate 的 `available_locales!` 宏转发调用——见 [src/lib.rs:191-197](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L191-L197)。

[5 个配置静态量 — crates/macro/src/lib.rs:349-353](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L349-L353) — `_RUST_I18N_FALLBACK_LOCALE`（回退列表）、`_RUST_I18N_MINIFY_KEY`（是否启用）、`_RUST_I18N_MINIFY_KEY_LEN/_PREFIX/_THRESH`（短键长度/前缀/阈值，u6 详讲）。它们都是 `static`，值在编译期由 4.1 节的对应片段填入。

[__rust_i18n_t 内部宏与 pub(crate) use — crates/macro/src/lib.rs:411-432](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L411-L432) — 第 413-417 行定义 `__rust_i18n_t`，它把所有 token 透传给 `rust_i18n::_tr!`，**并在末尾追加** `_minify_key = #minify_key, _minify_key_len = ..., _minify_key_prefix = ..., _minify_key_thresh = ...` 四个配置参数；第 421-429 行的 `__rust_i18n_tkv` 同理服务于 `tkv!`；第 431-432 行用 `pub(crate) use __rust_i18n_t as _rust_i18n_t` 把它以 `_rust_i18n_t` 之名暴露——这正是 `src/lib.rs` 里 `t!` 宏 [第 143-147 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L143-L147) 转发的目标。

#### 4.4.4 代码实践（阅读型）

1. **实践目标**：理清 `t!` 的一次调用如何拿到 minify_key 配置。
2. **操作步骤**：
   - 阅读 [src/lib.rs 的 t! 转发壳 — src/lib.rs:141-147](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L141-L147)，确认它转发到 `crate::_rust_i18n_t!`。
   - 回到 [__rust_i18n_t 宏定义 — crates/macro/src/lib.rs:413-417](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L413-L417)，看它如何把 `_minify_key=*` 追加到 `_tr!`。
3. **需要观察的现象**：用户写的 `t!("hello")` 里**完全没有** minify_key 参数，但展开后 `_tr!` 调用却带上了四个 `_minify_key*` 参数。
4. **预期结果**：得出结论——minify_key 配置由 `i18n!` 在编译期读取，再由 `__rust_i18n_t` 宏「隐式注入」到每次 `t!`，用户无需关心。

#### 4.4.5 小练习与答案

- **练习 1**：`_rust_i18n_translate` 和 `_rust_i18n_try_translate` 都返回 `Cow<'r, str>`，这个生命周期 `'r` 绑定在谁身上？为什么 miss 时能返回 `Cow::Borrowed`？
  - **答案**：`'r` 绑定在 `key: &'r str` 参数上（见 [第 371 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L367-L379)）。miss 时 `_rust_i18n_translate` 返回 `key.into()`，即借用调用者传入的 key 字符串，所以是 `Cow::Borrowed`、零分配；只有 `format!("{}.{}", ...)` 那条路径才是 `Cow::Owned`。
- **练习 2**：为什么 `__rust_i18n_t` 要先定义成 `__rust_i18n_t` 再 `pub(crate) use ... as _rust_i18n_t`，而不是直接定义成 `_rust_i18n_t`？
  - **答案**：Rust 的 `macro_rules!` 导出/引用规则下，用「先定义带前导下划线的私有名，再 `use as` 重命名导出」是一种常见的、能稳定地把内部宏以目标名字暴露给本 crate（这里是给 `t!` 转发壳用）的写法，便于控制可见性边界（`pub(crate)`），避免宏名污染。

---

## 5. 综合实践

本讲的核心实践（也是任务书指定的代码实践）是：**用 `RUST_I18N_DEBUG=1` 亲自看到 `generate_code` 的产物，并按本讲学到的结构给出生成代码做注释**。

1. **实践目标**：把第 4 节的「纸面理解」对照到真实的编译期输出，验证你对静态后端、查找函数、内部宏的认识。
2. **操作步骤**：
   - 进入示例目录（示例代码，路径以本地为准）：
     ```bash
     cd examples/app
     RUST_I18N_DEBUG=1 cargo build 2>&1 | tee /tmp/i18n_build.log
     ```
   - 在日志里查找 `-------------- code --------------` 这一行（对应 [i18n! 入口第 261-264 行](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L260-L265) 的 `println!`），它下方直到 `----------------------------------` 之间就是完整生成代码。
3. **需要观察的现象**：在贴出的生成代码里逐项标注：
   - **静态后端**：`static _RUST_I18N_BACKEND: std::sync::LazyLock<Box<dyn rust_i18n::Backend>> = ...`，其闭包体内的 `SimpleBackend::new()` + 若干 `backend.add_translations(...)`。
   - **配置静态量**：`_RUST_I18N_FALLBACK_LOCALE`、`_RUST_I18N_MINIFY_KEY*` 五行。
   - **回退查找函数**：`_rust_i18n_lookup_fallback`、`_rust_i18n_try_translate`（含 territory 回退循环与显式 fallback）、`_rust_i18n_translate`（带默认值包装）。
   - **locale 枚举函数**：`_rust_i18n_available_locales`。
   - **内部宏**：`macro_rules! __rust_i18n_t` 与 `__rust_i18n_tkv`，以及末尾的 `pub(crate) use __rust_i18n_t as _rust_i18n_t`。
4. **预期结果**：你能把生成代码的每一块都对应回本讲第 4 节的某个小节，说明它由 `generate_code` 的哪个 `quote!` 片段产生。
5. **待本地验证**：不同 `i18n!` 参数（是否传 `fallback=`、`backend=`、`minify_key=`）会让生成代码里的对应片段出现/消失；可以改 [examples/app/main.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/main.rs) 的 `i18n!("locales")` 参数后重新编译，观察差异。

> 若本地无法编译（缺工具链/网络），可退化为「源码阅读型实践」：对照 [第 334-433 行的总装模板](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L334-L433)，在脑中把 `#all_translations`/`#fallback`/`#extend_code` 等占位符替换成具体内容，画出一张「生成代码结构图」。

---

## 6. 本讲小结

- `generate_code` 是编译期代码生成的「总装车间」：它用一组「分块 `quote!` + 最后大 `quote!` 插值」的策略，把拍平的翻译表 `BTreeMap<locale, BTreeMap<key, value>>` 翻写成等价的 Rust 源码。
- 生成的 `_RUST_I18N_BACKEND` 是 `LazyLock<Box<dyn rust_i18n::Backend>>`：首次访问时才构造，闭包体内 `SimpleBackend::new()` + 逐 locale `add_translations(...)` 把译文灌进去，最后 `Box::new(backend)`。
- `SimpleBackend::add_translations` 用 `entry(locale).or_default()` + `extend(data)` 增量合并；生成代码里每个 locale 只调一次，传入预分配容量的、由 `Cow::Borrowed(字面量)` 组成的 HashMap，运行时零拷贝。
- `_rust_i18n_try_translate` 是真正查表的入口：先精确 locale 查 `backend.translate`，miss 后走 territory 回退循环，最后才轮到显式 fallback 列表；`SimpleBackend::translate` 底层就是两层 `HashMap::get`。
- `_rust_i18n_translate` 是 `try_translate` 的带默认值包装（miss 返回 key 或 `locale.key`）；`_rust_i18n_available_locales` 供 `available_locales!` 用；5 个 `static` 配置量承载 fallback / minify_key 设置。
- 两个内部宏 `__rust_i18n_t` / `__rust_i18n_tkv` 把 `i18n!` 读到的 minify_key 配置「隐式注入」到每次 `_tr!` / `_minify_key!` 调用，再以 `_rust_i18n_t` 之名暴露给 `t!` 转发壳——这正是「用户无感、配置生效」的关键。

---

## 7. 下一步学习建议

- **进入第三单元运行时翻译**：本讲只讲了「生成代码长什么样、查表入口是哪个函数」，而 `t!` 从用户书写到最终调到 `_rust_i18n_try_translate` 的完整转发链（`t!` → `crate::_rust_i18n_t!` → `rust_i18n::_tr!`）请接着读 **u3-l1（t! 宏的完整调用链）**。
- **回退策略的深度剖析**：本讲对 territory 回退 + 显式 fallback 只讲了骨架，RFC 4647 细节与用例预测见 **u3-l4（翻译回退机制）**。
- **后端抽象**：本讲出现的 `Box<dyn Backend>`、`SimpleBackend` 只是后端体系的冰山一角；想理解 `Backend` trait 的三个方法、`CombinedBackend` 组合优先级、自定义后端如何通过 `backend=` 接入（即 `extend_code` 片段的来历），请读 **第四单元（u4-l1 / u4-l2 / u4-l3）**。
- **建议动手**：把综合实践里的 `RUST_I18N_DEBUG=1` 输出保存下来，后续学到 `_tr!`（u3-l2）、minify_key（u6）时，回来对照这份输出会非常有帮助——它就是整条主链路最直观的「实物证据」。
