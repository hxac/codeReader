# i18n! 宏入口与参数解析

## 1. 本讲目标

学完本讲后，你应该能够：

1. 看懂 `i18n!` 过程宏在 [crates/macro/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) 中是如何被定义、入口函数 `i18n` 又是如何把 token 流交给 `Args` 解析的。
2. 读懂 `Args` 结构的字段语义——它就是 `i18n!` 所有参数的「内存模型」。
3. 理解 `syn::parse::Parse for Args` 实现的**三级优先级**：硬编码默认值 < `Cargo.toml` 中的 `[package.metadata.i18n]` < 宏调用时显式传入的参数。
4. 掌握 `consume_options` 如何靠**逗号递归**解析任意数量的命名参数，以及 `consume_fallback` 为什么能同时接受字符串 `"en"` 和字符串数组 `["en", "es"]` 两种形态。

本讲是第二单元「编译期代码生成主链路」的入口，承接 [u1-l2（工作区结构）](u1-l2-workspace-structure.md) 和 [u1-l3（快速上手）](u1-l3-quick-start-example.md)。本讲只讲「宏怎么解析参数」，不讲「解析完之后怎么加载文件、怎么生成代码」——那是 u2-l2、u2-l3、u2-l4 的任务。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：过程宏的本质是「编译期运行的函数」。**
普通的 Rust 函数在程序运行时执行；而 `#[proc_macro]` 标注的函数在 `cargo build` 阶段由编译器执行。它吃进去的是一段 token 流（`proc_macro::TokenStream`），吐出来的也是 token 流。`rust-i18n` 的 `i18n!` 宏就在编译期读取你的翻译文件、生成查找代码，最终把代码「织」进你的二进制。

**直觉二：`i18n!("locales", fallback = "en", minify_key = true)` 这种写法，本质是一个「带名字的可选参数列表」。**
Rust 过程宏不能像普通函数那样有真正的「命名参数」语法，所以 `rust-i18n` 用 `syn` 库手动解析这段 token 流：先认出第一个字符串字面量（locales 路径），再用逗号分隔出一个个 `名字 = 值` 的键值对。理解了这点，后面 `consume_options` 的递归就好懂了。

**几个本讲会用到的术语：**

- **过程宏（proc macro）**：编译期执行的宏，分三类：派生宏（`#[derive]`）、属性宏、函数式宏（`i18n!(...)` 属于这一类）。
- **token / token 流**：源码被切分后的最小语法单元序列，比如 `i18n ! ( "locales" , fallback = "en" )`。
- **`syn`**：Rust 生态里解析 Rust 源码语法的库，过程宏几乎都依赖它。
- **`ParseStream`**：`syn` 提供的「可以逐个 peek/parse token 的游标」，本讲大量出现。
- **metadata**：指 `Cargo.toml` 里的 `[package.metadata.i18n]` 配置段。

## 3. 本讲源码地图

本讲涉及的源码文件，按重要程度排列：

| 文件 | 作用 |
|------|------|
| [crates/macro/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) | **本讲主角**。定义 `Args` 结构、`Parse` 实现、`load_metadata`、`consume_options`/`consume_fallback`，以及 `i18n` 过程宏入口和 `generate_code`。 |
| [crates/support/src/config.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs) | 定义 `I18nConfig`，负责把 `Cargo.toml` 的 `[package.metadata.i18n]` 解析成结构体。`load_metadata` 调用它。 |
| [crates/support/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs) | 提供 `is_debug()`、`load_locales()` 和 `DEFAULT_MINIFY_KEY*` 常量。 |
| [examples/foo/Cargo.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/foo/Cargo.toml) | 一个真实的带 `[package.metadata.i18n]` 配置的示例项目，供实践参考。 |

## 4. 核心概念与源码讲解

### 4.1 i18n! 过程宏的入口与解析全景

#### 4.1.1 概念说明

在第二单元里，`i18n!` 是「编译期代码生成」的**总开关**。你在源码里写下 `i18n!("locales", fallback = "en")`，编译器会：

1. 调用 `rust_i18n_macro::i18n` 这个过程宏函数；
2. 这个函数把 token 流解析成 `Args` 结构；
3. 根据 `Args` 去加载翻译文件、生成运行时查找代码。

本讲只关注**第 1、2 步**：token 流是怎么进来的、又是怎么被拆成 `Args` 的。`generate_code`（第 3 步的代码生成）留给 [u2-l4](u2-l4-generate-code.md)。

入口函数本身非常薄，真正的活儿全在 `Args::parse` 里——这是一个值得记住的设计：**过程宏入口只做「解析 + 分发」，复杂的解析逻辑下沉到一个实现了 `Parse` trait 的结构体上**。

#### 4.1.2 核心流程

```
用户代码: i18n!("locales", fallback = "en", minify_key = true)
                      │
                      ▼  (proc_macro 把 token 流传进来)
   pub fn i18n(input: TokenStream)
                      │
                      │  parse_macro_input!(input as Args)
                      ▼  ── 触发 Args::parse
            ┌─────────────────────┐
            │  1. 硬编码默认值      │   ← 最低优先级
            │  2. load_metadata     │   ← 读 Cargo.toml，中等优先级
            │  3. consume_path /     │   ← 显式参数，最高优先级
            │     consume_options    │
            └─────────────────────┘
                      │
                      ▼
                 Args 结构体
                      │
                      ▼  (交给 load_locales + generate_code，本讲不展开)
              生成的运行时代码 token 流
```

入口函数 `i18n` 的执行顺序是：解析 `Args` → 拼 `locales_path` → `load_locales` 加载文件 → `generate_code` 生成代码 →（可选）调试打印 → 返回 token 流。

#### 4.1.3 源码精读

过程宏入口 `#[proc_macro] pub fn i18n`：[crates/macro/src/lib.rs:248-268](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L248-L268)。关键点：

- `parse_macro_input!(input as Args)`（第 250 行）这一句是「魔法」：它会调用 `Args` 的 `Parse::parse`，如果解析失败就自动把编译错误返回给用户。也就是说，`Args` 是怎样解析 token 流的，全靠它实现 `Parse` trait。
- 第 253-255 行：`i18n!` 拿到 `CARGO_MANIFEST_DIR`（当前正在编译的 crate 的根目录），把 `args.locales_path` 拼成绝对路径。
- 第 257 行 `load_locales(...)`、第 258 行 `generate_code(data, args)` 是下游主链路，**不在本讲范围**。
- 第 260-265 行：`is_debug()` 为真（即设置了环境变量 `RUST_I18N_DEBUG=1`）时，把生成的代码 `println!` 出来，方便调试。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `i18n!` 生成的代码长什么样，验证「过程宏在编译期运行」。

**操作步骤**：

1. 找到 `examples/app`（最小示例），进入它的视角编译：
   ```bash
   RUST_I18N_DEBUG=1 cargo build -p app
   ```
2. 观察终端输出：你应该能看到一段以 `-------------- code --------------` 包裹的 Rust 代码。这就是 `i18n!` 在编译期生成的（与本讲第 260-265 行的调试打印对应）。
3. 在生成的代码里找到 `static _RUST_I18N_BACKEND`、`fn _rust_i18n_translate` 等符号——它们在 [u2-l4](u2-l4-generate-code.md) 会详细讲解，本讲只需确认它们存在。

**预期结果**：终端打印出一段包含 `SimpleBackend::new()`、`add_translations(...)` 的代码块。

**待本地验证**：实际打印内容取决于 `examples/app` 的 locale 文件，请以本机输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`i18n` 函数第 253 行用了 `.expect("CARGO_MANIFEST_DIR is empty")`。为什么这里敢用 `expect` 而不是优雅处理错误？

**参考答案**：因为 `cargo build` 编译任何 crate 时，Cargo 都会把 `CARGO_MANIFEST_DIR` 环境变量设为该 crate 的 `Cargo.toml` 所在目录。这个环境变量在正常编译流程中**必然存在**，如果它真的为空，说明编译环境本身不正常，属于「不可恢复」的程序员级错误，用 `expect` 直接 panic 是合理的。

**练习 2**：如果把 `parse_macro_input!(input as Args)` 这一行删掉，会发生什么？

**参考答案**：`input` 这个 `TokenStream` 就不会被解析成 `Args`，`args` 变量也不存在，后续 `args.locales_path`、`generate_code(data, args)` 等全部编译失败。这正说明 `Args` 的解析是整个 `i18n!` 宏逻辑的起点。

---

### 4.2 Args 结构：宏参数的「内存模型」

#### 4.2.1 概念说明

`i18n!` 接收的所有参数，最终都被「装」进一个叫 `Args` 的结构体里。你可以把它理解成「宏参数的内存模型」——每一个字段对应一个可配置项。理解 `Args` 的字段，就等于理解了 `i18n!` 能配什么。

注意一个命名细节：宏调用时写的是 `backend = ...`，但 `Args` 里对应的字段叫 `extend`（因为底层是用 `BackendExt::extend` 把自定义后端「接」到本地后端上，详见 [u4-l2](u4-l2-backendext-combinedbackend.md)）。这种「对外名字」与「对内字段名」不一致，是读源码时容易踩的坑。

#### 4.2.2 核心流程

`Args` 的字段与 `i18n!` 参数的对应关系：

| `Args` 字段 | 类型 | 对应 `i18n!` 参数 | 是否能由宏参数覆盖 | 来源默认值 |
|-------------|------|-------------------|-------------------|-----------|
| `locales_path` | `String` | 第一个字符串字面量 `i18n!("locales")` | ✅ 能 | `"locales"` |
| `default_locale` | `Option<String>` | ❌ 无对应宏参数 | ❌ **只能来自 metadata** | `None` |
| `fallback` | `Option<Vec<String>>` | `fallback = "en"` 或 `fallback = ["en","es"]` | ✅ 能 | `None` |
| `extend` | `Option<Expr>` | `backend = MyBackend::new()` | ✅ 能 | `None` |
| `minify_key` | `bool` | `minify_key = true` | ✅ 能 | `false`（`DEFAULT_MINIFY_KEY`） |
| `minify_key_len` | `usize` | `minify_key_len = 12` | ✅ 能 | `24`（`DEFAULT_MINIFY_KEY_LEN`） |
| `minify_key_prefix` | `String` | `minify_key_prefix = "t_"` | ✅ 能 | `""`（`DEFAULT_MINIFY_KEY_PREFIX`） |
| `minify_key_thresh` | `usize` | `minify_key_thresh = 64` | ✅ 能 | `127`（`DEFAULT_MINIFY_KEY_THRESH`） |

> 注意：`default_locale` 字段在 `consume_options` 的 match 里**没有对应分支**，意味着你无法通过 `i18n!("locales", default_locale = "zh")` 来设置它——它只能来自 `Cargo.toml` 的 metadata。这一点 4.3 节会再次确认。

四个 `DEFAULT_MINIFY_KEY*` 常量的真实取值在 [crates/support/src/minify_key.rs:6-15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L6-L15)：`false / 24 / "" / 127`。

#### 4.2.3 源码精读

`Args` 结构定义：[crates/macro/src/lib.rs:12-21](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L12-L21)。

```rust
struct Args {
    locales_path: String,
    default_locale: Option<String>,
    fallback: Option<Vec<String>>,
    extend: Option<Expr>,
    minify_key: bool,
    minify_key_len: usize,
    minify_key_prefix: String,
    minify_key_thresh: usize,
}
```

几个值得留意的类型选择：

- `fallback` 是 `Option<Vec<String>>` 而不是 `Option<String>`：这正是为了支持「多个回退 locale，按优先级排列」。`Vec` 的顺序就是回退顺序。
- `extend` 是 `Option<Expr>`（`syn::Expr`），不是字符串：因为 `backend = RemoteI18n::new()` 传入的是一段**任意 Rust 表达式**，必须原样保存它的 AST，等 `generate_code` 时再 `quote!` 出去。
- `default_locale` 是 `Option<String>`：用 `Option` 表示「用户/metadata 是否指定过」，`None` 时 `generate_code` 就不生成设置默认 locale 的代码（见 lib.rs:298-309）。

#### 4.2.4 代码实践

**实践目标**：建立「宏参数 ↔ Args 字段」的直觉。

**操作步骤（源码阅读型）**：

1. 打开 [crates/macro/src/lib.rs:12-21](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L12-L21)。
2. 对照下面的宏调用，逐个字段推断最终值：

   ```rust
   // 示例代码（非项目原有）
   i18n!("locales",
         fallback = ["en", "es"],
         backend = MyBackend::new(),
         minify_key = true,
         minify_key_len = 12,
         minify_key_prefix = "t_",
         minify_key_thresh = 64);
   ```

   推断：`locales_path = "locales"`，`fallback = Some(["en","es"])`，`extend = Some(<MyBackend::new() 的 AST>)`，`minify_key = true`，`minify_key_len = 12`，`minify_key_prefix = "t_"`，`minify_key_thresh = 64`，`default_locale = None`（因为没在宏里、也没在 Cargo.toml 里设——假设没有 metadata）。

3. 思考：如果上面这段调用写在**没有** `[package.metadata.i18n]` 的 crate 里，`default_locale` 会是什么？——答案是 `None`，因为它的唯一来源是 metadata。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `extend` 字段类型是 `Option<Expr>` 而不是 `Option<String>`？

**参考答案**：因为 `backend = ...` 后面跟的是任意 Rust 表达式（比如 `RemoteI18n::new()`、`Arc::new(...)` ），不是单纯的字符串。`syn::Expr` 保存的是这段表达式的语法树，`generate_code` 后续用 `quote! { backend.extend(#extend) }` 把这棵 AST 原样插回生成的代码里。如果存成 `String`，就得自己拼 Rust 源码再 parse，既麻烦又易错。

**练习 2**：`fallback` 用 `Vec<String>` 而不是单个 `String`，带来了什么能力？

**参考答案**：带来了「多级回退链」。你可以写 `fallback = ["en", "es"]`，查找某个 key 时会先在当前 locale 找，再依次在 `en`、`es` 找，直到命中。`Vec` 的元素顺序就是回退优先级顺序。

---

### 4.3 Parse impl：三级优先级解析

#### 4.3.1 概念说明

`syn` 要求自定义类型实现 `Parse` trait（只有一个方法 `parse`），这样 `parse_macro_input!(input as Args)` 才能把 token 流转换成 `Args`。`rust-i18n` 在 `parse` 里实现了一套非常清晰的**三级优先级**：

\[ \text{最终值} = (\text{硬编码默认}) \;\to\;\text{被 metadata 覆盖}\;\to\;\text{被显式宏参数覆盖} \]

用人话说：**写在宏参数里的最优先，其次是 `Cargo.toml` 的配置，最后才是代码里的硬编码默认值**。这个设计让你可以「在 Cargo.toml 里设一份团队统一的默认，再在个别 crate 的宏调用里临时覆盖」。

#### 4.3.2 核心流程

`parse` 的执行步骤（[crates/macro/src/lib.rs:176-204](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L176-L204)）：

```
1. lookahead = input.lookahead1()           // 先"偷看"第一个 token 是什么类型，但不消费它
2. 构造 result，填入硬编码默认值              // 第 180-189 行（最低优先级）
3. result.load_metadata(input)              // 第 191 行：读 Cargo.toml 覆盖默认值（中等优先级）
4. 分两种情况消费 token：
   - 如果第一个 token 是字符串字面量(LitStr)：   // 形如 i18n!("locales", ...)
        consume_path()                      // 消费路径
        如果后面还有逗号 → consume_options()  // 消费后续命名参数
   - 如果第一个 token 是标识符(Ident)：         // 形如 i18n!(fallback = "en")，省略路径
        consume_options()                   // 直接消费命名参数
5. 返回 result
```

这里有个 `syn` 的关键技巧：`lookahead1()`。它在**不移动游标**的前提下，提前判断下一个 token 的类型，从而决定走哪条解析分支。注意第 191 行 `load_metadata` **无条件执行**（先于判断 token 类型），这就保证了「metadata 永远先被读进来，成为后续被覆盖的基线」。

> **一个容易忽略的细节**：`load_metadata` 会无条件把 `locales_path` 覆盖成 `cfg.load_path`（metadata 的默认值是 `"./locales"`，见 config.rs）。所以硬编码默认值 `"locales"` 实际上几乎总会被覆盖——它只在 `CARGO_MANIFEST_DIR` 取不到时才生效。

#### 4.3.3 源码精读

`parse` 实现：[crates/macro/src/lib.rs:176-204](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L176-L204)。

硬编码默认值构造（注意 fallback/minify 系列都用 `DEFAULT_*` 常量兜底）：

```rust
let mut result = Self {
    locales_path: String::from("locales"),
    default_locale: None,
    fallback: None,
    extend: None,
    minify_key: DEFAULT_MINIFY_KEY,            // false
    minify_key_len: DEFAULT_MINIFY_KEY_LEN,    // 24
    minify_key_prefix: DEFAULT_MINIFY_KEY_PREFIX.to_owned(), // ""
    minify_key_thresh: DEFAULT_MINIFY_KEY_THRESH, // 127
};
```

然后 `result.load_metadata(input)?;`（第 191 行）——这是「中等优先级」注入点。最后是分支消费（第 193-201 行）——这是「最高优先级」注入点。

`parse` 上方的文档注释（[crates/macro/src/lib.rs:152-172](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L152-L172)）用 5 段 `no_run` 代码列举了合法调用形态，正好对应 `v1()` 到 `v5()` 五种写法，是理解合法语法的最佳速查表。

#### 4.3.4 代码实践

**实践目标**：验证三级优先级——宏参数 > metadata > 硬编码默认。

**操作步骤（源码阅读 + 本地验证）**：

1. 假设某 crate 的 `Cargo.toml` 写了：
   ```toml
   [package.metadata.i18n]
   fallback = ["zh"]
   minify-key-thresh = 50
   ```
2. 同一 crate 的源码里写：`i18n!("locales", fallback = "en");`
3. 推断 `args.fallback` 的最终值。按优先级，显式宏参数 `fallback = "en"` 会覆盖 metadata 的 `["zh"]`，所以 **`args.fallback == Some(["en"])`**。
4. 推断 `args.minify_key_thresh`：宏参数没传它，所以保留 metadata 的 `50`。
5. （可选）用 `RUST_I18N_DEBUG=1` 编译，在生成的代码里找到 `_RUST_I18N_MINIFY_KEY_THRESH: usize = 50;` 验证。

**需要观察的现象**：生成的 `_RUST_I18N_FALLBACK_LOCALE` 静态变量对应 `["en"]`（宏参数），而 `_RUST_I18N_MINIFY_KEY_THRESH` 对应 `50`（metadata）。

**预期结果**：宏参数覆盖了 metadata 的 fallback；未被宏参数覆盖的 minify_key_thresh 保留了 metadata 值。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `parse` 用 `input.lookahead1()` 而不是直接 `input.parse::<LitStr>()`？

**参考答案**：因为 `i18n!` 的第一个 token 可能是字符串字面量（`i18n!("locales", ...)`），也可能直接是标识符（`i18n!(fallback = "en")`，省略了路径）。`lookahead1()` 可以在不消费 token 的前提下「偷看」它的类型，从而选择正确的分支。如果直接 `parse::<LitStr>()`，遇到 `i18n!(fallback = ...)` 这种省略路径的写法就会报错，无法回退到 `consume_options` 分支。

**练习 2**：如果用户写 `i18n!("locales", default_locale = "zh")`，会发生什么？

**参考答案**：`consume_options` 的 match（见 4.5 节）里**没有** `default_locale` 分支，会落到 `_ => {}` 被静默忽略。也就是说 `default_locale` 不会被设置成 `"zh"`——它仍然由 metadata 决定。这是一个容易踩的「写了不生效」的坑：`default_locale` 只能在 `Cargo.toml` 里配，不能在宏参数里配。

---

### 4.4 load_metadata：从 Cargo.toml 读取低优先级配置

#### 4.4.1 概念说明

`load_metadata` 是三级优先级里的「中间层」。它做的事很直白：**找到当前 crate 的 `Cargo.toml`，把里面的 `[package.metadata.i18n]`（或 `[workspace.metadata.i18n]`）读成 `I18nConfig`，然后填进 `Args`**。

这一层的设计意图是「团队级默认」：把团队约定好的 fallback、minify_key 等写进 `Cargo.toml`，所有开发者无需在每个 `i18n!` 调用里重复写，又允许个别调用临时覆盖。

`Cargo.toml` 用 `metadata` 表是一个 Cargo 官方支持的扩展机制——第三方工具可以往 `[package.metadata.xxx]` 里塞自己的配置，Cargo 会忽略它们但不会报错。

#### 4.4.2 核心流程

`load_metadata`（[crates/macro/src/lib.rs:125-146](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L125-L146)）的流程：

```
1. 读 CARGO_MANIFEST_DIR 环境变量（当前 crate 根目录）
   ├─ 取到 → 继续
   └─ 取不到 → 若 is_debug()，报错；否则什么都不做（返回 Ok）
2. cfg = I18nConfig::load(cargo_dir)     // 打开并解析 Cargo.toml
3. 把 cfg 的字段逐个写进 self：
     self.locales_path       = cfg.load_path
     self.default_locale     = Some(cfg.default_locale)        // 总是设
     if !cfg.fallback.is_empty() { self.fallback = Some(cfg.fallback) }  // 只在有内容时设
     self.minify_key / _len / _prefix / _thresh = cfg 对应字段
```

注意第 134 行的 `if !cfg.fallback.is_empty()`：只有 `Cargo.toml` 里**确实写了** fallback 时才覆盖。否则保留 `Args` 里原本的 `None`，把决定权让给后续的宏参数。这是个很谨慎的写法——避免用「空数组」去冲掉用户在别处设的值。

`I18nConfig::parse`（[crates/support/src/config.rs:65-95](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L65-L95)）则用了一个巧妙的字符串替换技巧：它先检测 `Cargo.toml` 里有没有 `[i18n]`、`[package.metadata.i18n]`、`[workspace.metadata.i18n]` 三种节；如果有后两者，就用 `contents.replace("[package.metadata.i18n]", "[i18n]")` 把表头**原地改名**成 `[i18n]`，再交给 `toml` 反序列化成一个 `MainConfig { i18n: I18nConfig }`。这样就不必自己写解析器，直接复用 serde + toml。

#### 4.4.3 源码精读

`load_metadata` 本体：[crates/macro/src/lib.rs:125-146](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L125-L146)。核心就是把 `cfg` 字段灌进 `self`：

```rust
let cfg = I18nConfig::load(&current_dir)
    .map_err(|_| input.error("Failed to load config from Cargo.toml for `metadata`"))?;
self.locales_path = cfg.load_path;
self.default_locale = Some(cfg.default_locale.clone());
if !cfg.fallback.is_empty() {
    self.fallback = Some(cfg.fallback);
}
self.minify_key = cfg.minify_key;
// ... 其余 minify 字段同理
```

`I18nConfig` 结构：[crates/support/src/config.rs:13-32](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L13-L32)。注意两点：

1. `#[serde(rename_all = "kebab-case")]`——所以 `Cargo.toml` 里要写 `default-locale`、`available-locales`、`minify-key-thresh` 这种短横线风格（kebab-case），而不是 Rust 的 `snake_case`。
2. 每个字段都带 `#[serde(default = "...")]`——缺哪个字段就用哪个默认函数，因此 `Cargo.toml` 里只写需要的几项即可。

`parse` 把 metadata 表头改名再反序列化的技巧：[crates/support/src/config.rs:65-95](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L65-L95)。第 84-92 行还会把 `default_locale` 自动插入 `available_locales` 列表头部并去重，保证默认 locale 一定在可用列表里。

#### 4.4.4 代码实践

**实践目标**：用一个真实示例 crate，确认 metadata 被正确读进 `Args`。

**操作步骤（源码阅读型）**：

1. 打开 [examples/foo/Cargo.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/foo/Cargo.toml)，它只有：
   ```toml
   [package.metadata.i18n]
   available-locales = ["en", "zh-CN"]
   default-locale = "en"
   ```
2. 对照 config.rs 的 `test_load`（[config.rs:213-220](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L213-L220)）：断言 `available_locales == ["en", "zh-CN"]`、`default_locale == "en"`。注意 `available-locales` 只写了 `["zh-CN"]` 也能得到 `["en","zh-CN"]`，因为 `default_locale` 被自动插入了头部。
3. 推断：若该 crate 调用 `i18n!()`（不带任何参数），`args.default_locale` 应为 `Some("en")`，`args.locales_path` 应为 `"./locales"`（来自 `cfg.load_path` 默认值）。

#### 4.4.5 小练习与答案

**练习 1**：`Cargo.toml` 里写 `minify-key-thresh = 16`，但 Rust 结构体字段叫 `minify_key_thresh`，两者怎么对得上？

**参考答案**：靠 `#[serde(rename_all = "kebab-case")]`。它告诉 serde：把结构体里的 `snake_case` 字段名（`minify_key_thresh`）在反序列化时当作 `kebab-case`（`minify-key-thresh`）来匹配。所以 `Cargo.toml` 用短横线、源码用下划线，serde 帮你自动转换。

**练习 2**：为什么 `load_metadata` 对 fallback 要加 `if !cfg.fallback.is_empty()` 判断，而 `minify_key` 等字段却直接赋值不加判断？

**参考答案**：因为 `Args.fallback` 是 `Option`，语义是「有没有设过 fallback」。如果 metadata 没写 fallback（得到空 `Vec`），直接赋值会把「空列表」当成「设过」，从而干扰后续的优先级判断（比如可能覆盖掉硬编码的 `None` 语义）。加判断后，空 `Vec` 被视为「没设」，保留 `Args` 原值。而 `minify_key` 是 `bool`/`usize` 这类「总有合法值」的标量，metadata 即便没写也有 `DEFAULT_*` 兜底，直接赋值安全。

---

### 4.5 consume_options 递归与 fallback 双形态分支

#### 4.5.1 概念说明

这一节是本讲的**核心难点**，也是学习目标里点名要求掌握的两点：`consume_options` 的**逗号递归**，以及 `consume_fallback` 的**字符串/数组双形态**。

- **`consume_options`**：负责解析 `fallback = "en", minify_key = true, backend = X` 这种「一串逗号分隔的键值对」。它解析完一个，就**递归调用自己**去解析下一个，直到没有逗号为止。这种「靠逗号递归」的模式是手写 `Parse` 时处理可变长度参数的常见手法。
- **`consume_fallback`**：负责解析 `fallback` 的值。它的特别之处在于用「先试 A，失败再试 B」的 `if let Ok(...)` 模式，让 fallback 既能写成单个字符串 `fallback = "en"`，也能写成数组 `fallback = ["en", "es"]`。

#### 4.5.2 核心流程

**`consume_options` 的递归结构**（[crates/macro/src/lib.rs:88-122](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L88-L122)）：

```
consume_options(self, input):
    1. ident   = input.parse::<Ident>()       // 读名字，如 "fallback"
    2. input.parse::<Token![=]>()?            // 读等号
    3. match ident.as_str():                  // 按名字分发
         "fallback"          -> consume_fallback(input)
         "backend"           -> self.extend = parse::<Expr>()
         "minify_key"        -> consume_minify_key(input)
         "minify_key_len"    -> consume_minify_key_len(input)
         "minify_key_prefix" -> consume_minify_key_prefix(input)
         "minify_key_thresh" -> consume_minify_key_thresh(input)
         _                   -> {}            // 未知名字：静默忽略
    4. if input.parse::<Token![,]>().is_ok(): // 如果下一个 token 是逗号
           self.consume_options(input)?       //   就递归解析下一个键值对
    5. Ok(())
```

关键在第 4 步：`input.parse::<Token![,]>().is_ok()` 是**试探性解析**——成功说明还有更多参数（吃掉逗号，递归）；失败说明参数到此结束（不报错，直接返回）。这就实现了「任意数量的命名参数」。

**`consume_fallback` 的双形态分支**（[crates/macro/src/lib.rs:31-56](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L31-L56)）：

```
consume_fallback(self, input):
    1. 先试 input.parse::<LitStr>()           // 形如 "en"
         成功 -> self.fallback = Some(vec![那一个字符串]);  返回
    2. 失败 -> input.parse::<syn::ExprArray>()  // 形如 ["en", "es"]
         逐个元素断言是字符串字面量，收集成 Vec<String>
         self.fallback = Some(收集到的 Vec)
```

第 1 步用 `if let Ok(val) = ...` 而不是 `?`，这是「优先尝试单字符串，失败回退到数组」的关键——单字符串失败**不算错误**，而是触发「试另一种形态」。

#### 4.5.3 源码精读

`consume_options`：[crates/macro/src/lib.rs:88-122](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L88-L122)。两个关键片段：

```rust
// 按名字分发（第 93-114 行）
match ident.as_str() {
    "fallback"   => { self.consume_fallback(input)?; }
    "backend"    => { let val = input.parse::<Expr>()?; self.extend = Some(val); }
    "minify_key" => { self.consume_minify_key(input)?; }
    // ... 其余 minify_key_* 同理
    _ => {}   // 未知键静默忽略
}

// 逗号递归（第 117-119 行）
if input.parse::<Token![,]>().is_ok() {
    self.consume_options(input)?;
}
```

`consume_fallback`：[crates/macro/src/lib.rs:31-56](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L31-L56)。双形态的核心：

```rust
// 形态一：单个字符串
if let Ok(val) = input.parse::<LitStr>() {
    self.fallback = Some(vec![val.value()]);
    return Ok(());
}
// 形态二：字符串数组
let val = input.parse::<syn::ExprArray>()?;
let fallback = val.elems.into_iter().map(|expr| {
    // 逐个断言数组元素是字符串字面量，否则报错
    if let syn::Expr::Lit(syn::ExprLit { lit: syn::Lit::Str(lit_str), .. }) = expr {
        Ok(lit_str.value())
    } else {
        Err(input.error("`fallback` must be a string literal or an array of string literals"))
    }
}).collect::<syn::parse::Result<Vec<String>>>()?;
self.fallback = Some(fallback);
```

> 注意：`consume_fallback` 在数组形态里对**每个元素**都做了类型检查（必须是 `Lit::Str`）。所以 `fallback = ["en", 42]` 会因为 `42` 不是字符串而报出清晰的错误信息。

#### 4.5.4 代码实践

**实践目标**：定位 fallback 双形态分支，并用文字复述 `consume_options` 的递归机制。这是本讲指定的实践任务。

**操作步骤（源码阅读型）**：

1. **定位 fallback 双形态分支**：打开 [crates/macro/src/lib.rs:31-56](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L31-L56)。
   - 第 32-35 行：`if let Ok(val) = input.parse::<LitStr>()` 是「单字符串分支」。成功时把这一个字符串包成 `vec![val.value()]`。
   - 第 36-54 行：`input.parse::<syn::ExprArray>()` 是「数组分支」。把数组里每个字符串字面量收集成 `Vec<String>`。
   - 两支最终都把结果放进 `self.fallback = Some(...)`，所以无论用户写哪种形态，`Args.fallback` 的类型都是统一的 `Option<Vec<String>>`。

2. **写一段说明 `consume_options` 如何靠逗号递归解析任意数量的命名参数**。请你自己用一段话（不超过 5 句）描述下面这段调用：
   ```rust
   // 示例代码（非项目原有）
   i18n!("locales", fallback = ["en","es"], minify_key = true, minify_key_prefix = "t_");
   ```
   你的描述应覆盖：
   - `consume_path` 先消费 `"locales"`；
   - 遇到第一个逗号后进入 `consume_options`；
   - 第一次调用解析 `fallback = [...]`（内部走 `consume_fallback` 的数组分支）；
   - 第 117 行发现还有逗号 → **递归**调用 `consume_options`，解析 `minify_key = true`；
   - 再次发现逗号 → 再递归，解析 `minify_key_prefix = "t_"`；
   - 这次之后没有逗号，`parse::<Token![,]>().is_ok()` 为 false，递归终止。

3. **可选验证**：在第 117 行附近设想：如果把 `if input.parse::<Token![,]>().is_ok() { self.consume_options(input)?; }` 改成 `if input.parse::<Token![,]>().is_ok() {}`（删掉递归调用），会发生什么？——答案：只会解析**第一个**命名参数，后面的 `, minify_key = true` 会因为没有被消费而留在 token 流里，导致 `parse_macro_input!` 报「还有多余 token」的编译错误。这反向证明了递归的必要性。

**预期结果**：你能准确指出双形态分支的行号，并说清递归是如何让 `consume_options` 支持任意数量键值对的。

#### 4.5.5 小练习与答案

**练习 1**：`consume_fallback` 里 `if let Ok(val) = input.parse::<LitStr>()` 用的是 `if let Ok`，而 `consume_minify_key_len` 里用的是 `input.parse::<syn::LitInt>()?`（带 `?`）。为什么这里要用 `if let Ok` 而不是 `?`？

**参考答案**：因为 `consume_fallback` 需要支持**两种**形态。如果第一个 `parse::<LitStr>()` 失败，那不代表「整体出错」，只代表「这不是单字符串形态」，需要**继续尝试**数组形态。用 `?` 会在单字符串解析失败时直接返回错误，无法回退到数组分支；而 `if let Ok` 允许「失败就跳到下一个尝试」。相比之下，`minify_key_len` 只接受整数字面量一种形态，失败就是真失败，所以用 `?`。

**练习 2**：如果用户写了 `i18n!("locales", unknown = 123)`，会发生什么？

**参考答案**：`consume_options` 的 match 里没有 `"unknown"` 分支，会落到 `_ => {}`，静默忽略整个 `unknown = 123`。**不会报错**。这意味着拼错参数名（比如把 `minify_key` 写成 `minifykey`）会被悄悄吞掉，是一个值得注意的「静默失败」陷阱。

**练习 3**：`consume_options` 是「尾递归」（递归调用在函数最后）。如果参数非常多（几十个命名参数），会有栈溢出风险吗？

**参考答案**：在 Rust 的**安全**调用模型里，普通递归不会自动做尾调用优化，理论上极深的递归有栈溢出风险。但实际场景里，`i18n!` 的命名参数最多就 6 个，递归深度极浅，不会有任何问题。这种「靠递归处理可变长度参数」的写法在过程宏里很常见且安全。

## 5. 综合实践

**任务**：给一个虚拟 crate 设计完整的 `i18n!` 调用与 `Cargo.toml` 配置，并完整追踪参数从 token 流到 `Args` 的解析过程。

**背景**：假设你的团队约定——默认 locale 是 `en`，所有未命中的翻译回退到 `en` 和 `es`；对于长文案启用 minify_key，短键长度 12、前缀 `t_`、阈值 64。但某个特定 crate 想临时把 fallback 改成只回退 `en`，并使用一个自定义后端。

**步骤**：

1. 在该 crate 的 `Cargo.toml` 写下团队默认配置：
   ```toml
   # 示例配置（非项目原有文件）
   [package.metadata.i18n]
   default-locale = "en"
   fallback = ["en", "es"]
   minify-key = true
   minify-key-len = 12
   minify-key-prefix = "t_"
   minify-key-thresh = 64
   ```
2. 在该 crate 源码里写覆盖调用：
   ```rust
   // 示例代码（非项目原有）
   rust_i18n::i18n!("locales", fallback = "en", backend = MyBackend::new());
   ```
3. **完整追踪**（画出每一步 `Args` 各字段的值）：
   - 第一步（硬编码默认）：`fallback=None, minify_key=false, minify_key_thresh=127, locales_path="locales", default_locale=None`。
   - 第二步（`load_metadata` 读 Cargo.toml）：`fallback=Some(["en","es"]), minify_key=true, minify_key_thresh=64, locales_path="./locales", default_locale=Some("en")`。
   - 第三步（`consume_path` + `consume_options`）：`locales_path="locales"`（覆盖 `"./locales"`）、`fallback=Some(["en"])`（覆盖 `["en","es"]`，走 `consume_fallback` 单字符串分支）、`extend=Some(MyBackend::new() 的 AST)`。未被宏参数触及的 `minify_key=true, minify_key_thresh=64, default_locale=Some("en")` 保留 metadata 值。
4. 用 `RUST_I18N_DEBUG=1 cargo build`，在打印的生成代码里核对：
   - `_RUST_I18N_FALLBACK_LOCALE` 应对应 `Some(&["en"])`（宏参数赢了）；
   - `_RUST_I18N_MINIFY_KEY_THRESH: usize = 64`（metadata 赢，因为宏参数没传它）。

**这个任务串起了本讲全部知识点**：`Args` 字段映射（4.2）、三级优先级（4.3）、metadata 加载（4.4）、`consume_options`/`consume_fallback` 的递归与双形态（4.5）。

## 6. 本讲小结

- `i18n!` 是一个**函数式过程宏**，入口 `pub fn i18n` 用 `parse_macro_input!(input as Args)` 把 token 流交给 `Args::parse`，解析完再 `load_locales` + `generate_code`（后两者本讲不展开）。
- `Args` 是宏参数的「内存模型」；其中 `backend = ...` 对应字段叫 `extend`（用 `Option<syn::Expr>` 保存表达式 AST），`default_locale` **只能来自 metadata、不能在宏参数里设**。
- 参数优先级是**三级**：硬编码默认值（如 `minify_key_thresh=127`）< `Cargo.toml` 的 `[package.metadata.i18n]` < 宏调用时的显式参数。
- `load_metadata` 无条件先跑，用 `I18nConfig::load` 读 `Cargo.toml`；`I18nConfig` 靠 `#[serde(rename_all="kebab-case")]` 把 `Cargo.toml` 的 `minify-key-thresh` 映射到 `minify_key_thresh`。
- `consume_options` 靠**逗号递归**（`if input.parse::<Token![,]>().is_ok() { self.consume_options(input)? }`）支持任意数量的命名参数；未知键名落到 `_ => {}` 被**静默忽略**（写错参数名不会报错，是易踩的坑）。
- `consume_fallback` 用「`if let Ok` 先试单字符串，失败再试数组」的模式，让 `fallback` 同时支持 `"en"` 和 `["en","es"]` 两种写法，统一收进 `Option<Vec<String>>`。

## 7. 下一步学习建议

参数解析完之后，`i18n` 入口函数会拿 `args.locales_path` 去加载翻译文件。这就是下一讲的内容：

- **u2-l2 编译期加载与解析本地化文件**：跟着 `load_locales` / `try_load_locales` 看它如何用 `globwalk` 扫描 `**/*.{yml,yaml,json,toml}`、按 `_version` 走 v1/v2 两条解析路径。建议先复习 [u1-l4（本地化文件格式）](u1-l4-locale-file-formats.md)，因为 `_version` 字段的语义在那里已建立。
- **进阶补充**：如果你想立刻看到 `Args` 如何影响生成代码，可以先跳到 [u2-l4 generate_code 生成的运行时代码](u2-l4-generate-code.md)，看 `args.fallback`、`args.extend` 怎么被 `quote!` 成 `_RUST_I18N_FALLBACK_LOCALE` 和 `backend.extend(...)`。
- **关于 minify_key 系列参数的去向**：本讲只讲了它们怎么被解析进 `Args`，它们在 `generate_code` 里被 `quote!` 成四个 `_RUST_I18N_MINIFY_KEY*` 静态变量，并透传给 `__rust_i18n_t!` 宏。完整的 minify_key 机制见第六单元 [u6-l1](u6-l1-minify-key-algorithm.md)。
