# Workspace 多 crate 共享翻译模式

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「为什么要把翻译集中在一个独立的 `i18n` crate 里加载」，以及这样做如何节省内存、缩小最终二进制。
- 看懂 `examples/share-in-workspace` 里 `I18nBackend` 如何包裹编译期生成的 `_RUST_I18N_BACKEND` 并叠加 `en` 兜底。
- 解释 `init!` 宏为什么能让业务 crate「零配置」接入共享翻译，以及它展开后如何借助 `i18n!(backend = ...)` 形成组合后端。
- 区分 `pub use` 重导出的 `t!` 与原始 `rust_i18n::t!` 是同一个转发壳，并理解 `[workspace.dependencies]` 如何让多个 crate 复用同一份依赖。
- 动手设计一个最小 workspace：一个 `i18n` crate 负责加载与重导出，两个业务 crate 依赖它复用 `t!`。

## 2. 前置知识

本讲是「专家层」的综合应用篇，需要你已经掌握以下三块（对应前置讲义）：

1. **工作区结构**（[[u1-l2]]）：rust-i18n 的根 crate 兼任 workspace 根，子 crate 之间靠 `pub use` 形成门面；过程宏 crate 必须单独拆出。
2. **`t!` 宏的完整调用链**（[[u3-l1]]）：`t!` 是一个**转发壳**，它会把调用路由到 `crate::_rust_i18n_t!`——这里的 `crate::` 指向**写下 `t!` 的那个 crate**。这是理解「业务 crate 调 `t!` 为什么能查到共享翻译」的钥匙。
3. **自定义后端实战**（[[u4-l3]]）：实现 `Backend` trait 后，可用 `i18n!("locales", backend = MyBackend)` 接入；自定义后端会成为 `CombinedBackend` 的 `self.1`、优先级高于本地文件后端。

本讲所做的，本质是把 [[u3-l1]] 的「转发壳」与 [[u4-l3]] 的「自定义后端」两套机制，在一个真实 workspace 示例里拼装起来。所以如果你对这两块还不熟，建议先回去过一遍。

### 两种翻译组织方式对比

在进入源码前，先用一张表建立直觉：

| 维度 | 「每 crate 各自加载」（如 `examples/foo`） | 「workspace 共享加载」（如 `examples/share-in-workspace`） |
| --- | --- | --- |
| 谁调用 `i18n!` | 每个业务 crate 各自调用一次 | 只有一个 `i18n` crate 调用一次 |
| 翻译字符串嵌入次数 | 每个 crate 各嵌入一份 | 全局只嵌入一份 |
| 跨 crate 复用 | 不能，各自独立 | 业务 crate 通过 `I18nBackend` 复用 |
| 适用场景 | 单 crate、翻译互不相干 | 多 crate workspace、共用同一套文案 |

设翻译数据大小为 \( S \)、需要翻译的业务 crate 数为 \( N \)。两种方式的内存占用大致为：

\[
M_{\text{naive}} = N \times S \qquad M_{\text{shared}} = S
\]

当 workspace 里多个 crate 被链接进同一个最终二进制时，这个差距就会真实体现在二进制体积与运行时内存上——这正是 [README](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/README.md#L1-L13) 强调的「save memory and reduce the size of the final binary」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `examples/share-in-workspace/crates/i18n/src/lib.rs` | 共享层核心：加载一次翻译、定义 `I18nBackend`、定义 `init!` 宏、重导出 `t!`/`set_locale` |
| `examples/share-in-workspace/Cargo.toml` | workspace 根，声明 `[workspace] members` 与 `[workspace.dependencies]` |
| `examples/share-in-workspace/crates/i18n/Cargo.toml` | `i18n` crate 依赖 `rust-i18n`（workspace 继承） |
| `examples/share-in-workspace/crates/my-app1/src/lib.rs` | 业务 crate 示例：调用 `i18n::init!()` 并用 `t!` 取值 |
| `examples/share-in-workspace/crates/my-app2/src/lib.rs` | 另一个业务 crate 示例，演示另一种 `use` 写法 |
| `examples/foo/src/lib.rs` | 对比示例：「每 crate 各自加载」的写法 |
| `crates/macro/src/lib.rs` | `generate_code`，重点看 `extend_code` 与 `_RUST_I18N_BACKEND` 的生成 |
| `crates/support/src/config.rs` | `I18nConfig` 默认 `load_path`，解释「业务 crate 不嵌入翻译」的原因 |

## 4. 核心概念与源码讲解

### 4.1 共享 i18n crate：集中加载一次翻译

#### 4.1.1 概念说明

问题背景：假设你的 workspace 有 `my-app1`、`my-app2` 两个业务 crate，它们都要显示同一套多语言文案。如果照搬 [[u1-l3]] 的写法，让每个 crate 各自 `i18n!("locales")`，那么同一份翻译 YAML 会被**编译期代码生成两次**，分别嵌进两个 crate 的二进制。当二者最终链接进同一个可执行文件时，这些字符串字面量在 `.rodata` 段里就会出现两份——这就是「内存与体积浪费」的来源。

解决思路：把「加载翻译、生成静态后端」这件事**收拢到一个专职的 `i18n` crate**，让它成为整条翻译链路的**单一数据源（single source of truth）**。业务 crate 不再各自加载文件，而是通过一个薄薄的自定义后端 `I18nBackend` 去**引用** `i18n` crate 里已经生成好的 `_RUST_I18N_BACKEND`。

这恰好是 [[u4-l3]]「自定义后端」的一种特例：自定义后端不读数据库、不调远程 API，而是**包裹另一个已经初始化好的静态后端**，并在此基础上叠加一点逻辑（这里叠加的是 `en` 兜底）。

#### 4.1.2 核心流程

`i18n` crate 的 `lib.rs` 做三件事，顺序如下：

1. **加载一次**：调用 `rust_i18n::i18n!("../../locales")`，在编译期扫描上级 `locales/` 目录、生成 `i18n` crate 自己的 `_RUST_I18N_BACKEND`（一个装满译文的 `SimpleBackend`）。
2. **包裹 + 兜底**：定义空结构体 `I18nBackend`，为它实现 `Backend`，三个方法全部转发给上一步生成的 `_RUST_I18N_BACKEND`，并在 `translate` 里追加一条「查不到就回退 `en`」的规则。
3. **对外暴露**：用 `pub use` 重导出 `t!`、`set_locale`，并用 `init!` 宏（见 4.2）把接入方式封装好。

伪代码：

```
i18n crate:
    i18n!("../../locales")          # 生成 i18n::_RUST_I18N_BACKEND（装满译文）
    struct I18nBackend              # 空壳，只是个类型标记
    impl Backend for I18nBackend:
        translate(locale, key):
            _RUST_I18N_BACKEND.translate(locale, key)
                .or( _RUST_I18N_BACKEND.translate("en", key) )   # en 兜底
```

注意一个关键点：`I18nBackend::translate` 里引用的 `_RUST_I18N_BACKEND`，是**`i18n` crate 命名空间下**的那个静态量（由本文件第 4 行的 `i18n!` 生成）。这与业务 crate 里生成的同名静态量是**两个不同的变量**，只是名字一样——下文 4.2 会展开。

#### 4.1.3 源码精读

加载一次翻译，这是整个共享模式的「源头」：

[examples/share-in-workspace/crates/i18n/src/lib.rs:1-4](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/src/lib.rs#L1-L4) —— 导入 `Backend` 与 `Cow` 后，用相对路径 `"../../locales"` 指向 workspace 根下的 `locales/app.yml`，编译期生成 `i18n` crate 的静态后端。

接着看 `I18nBackend` 如何包裹这个后端并加 `en` 兜底：

[examples/share-in-workspace/crates/i18n/src/lib.rs:6-25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/src/lib.rs#L6-L25) —— `I18nBackend` 是无字段的单元结构体（unit struct），它本身不存任何数据，纯粹是一个「类型标签」+「行为载体」。三个 trait 方法全部委托给 `_RUST_I18N_BACKEND`。

重点是 `translate` 的兜底分支：

[examples/share-in-workspace/crates/i18n/src/lib.rs:13-20](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/src/lib.rs#L13-L20) —— 先按请求的 `locale` 查一次；若返回 `None`，再用硬编码的 `"en"` 查一次。这条逻辑把 `en` 提升成了「最终兜底语言」，和 [[u3-l4]] 讲的 `_rust_i18n_try_translate` 编排的回退链是**两层不同**的兜底：外层是 `I18nBackend` 内的 `en` 兜底（存储层自己加的），内层是 `_rust_i18n_try_translate` 的 territory + 显式 fallback（回退策略层编排的）。两者叠加，互不冲突。

被加载的翻译文件采用 v2 格式（多语言合入一文件，见 [[u1-l4]]）：

[examples/share-in-workspace/locales/app.yml:1-5](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/locales/app.yml#L1-L5) —— `_version: 2`，`welcome` 键下挂 `en` / `zh-CN` / `zh-HK` 三种语言，文件名 `app` 不影响 locale（v2 风格）。

> 对比一下「每 crate 各自加载」的写法，来自 `examples/foo`：
> [examples/foo/src/lib.rs:5-5](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/foo/src/lib.rs#L5-L5) —— `foo` 是单 crate，直接 `rust_i18n::i18n!("locales", fallback = "en")` 加载自己 `locales/` 下的 `en.yml` / `fr.yml`。它没有跨 crate 共享需求，所以走最朴素的单 crate 路线，回退由 `i18n!` 的 `fallback = "en"` 参数提供，而不是自定义后端。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认「翻译字符串只嵌入一次」。
2. **操作步骤**：
   - 打开 [examples/share-in-workspace/crates/i18n/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/src/lib.rs#L1-L36)，找到唯一的 `i18n!` 调用（第 4 行）。
   - 用 `grep -rn 'i18n!(' examples/share-in-workspace/` 检查 `my-app1`、`my-app2` 是否各自直接调用了 `i18n!("...")`。
3. **需要观察的现象**：业务 crate 里**没有**直接加载文件的 `i18n!("locales")` 调用，只有 `i18n::init!()`（它不传路径，见 4.2）。这说明真正读 YAML、嵌入字面量的只有 `i18n` crate 一次。
4. **预期结果**：除 `i18n/src/lib.rs:4` 外，业务 crate 不出现带路径参数的 `i18n!` 调用。
5. 待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`I18nBackend` 为什么设计成无字段的单元结构体？它能存数据吗？

> **答案**：它不需要存数据——真正的译文已经存在 `i18n` crate 的 `_RUST_I18N_BACKEND` 里。`I18nBackend` 只是一个「行为载体」，通过实现 `Backend` trait 提供委托逻辑（外加 `en` 兜底）。单元结构体零开销，是最轻量的自定义后端形态。

**练习 2**：如果把 `translate` 里的 `en` 兜底删掉，只保留 `_RUST_I18N_BACKEND.translate(locale, key)`，行为会有什么变化？

> **答案**：当某个 locale 下某 key 缺译时，会直接返回 `None`，再由外层 `_rust_i18n_try_translate` 的回退链处理。`en` 兜底相当于在「存储层」额外加了一道保险，即使调用方没配 fallback、也没触发 territory 回退，也能拿到英文译文。

---

### 4.2 init! 宏：业务 crate 的零配置接入

#### 4.2.1 概念说明

`I18nBackend` 定义在 `i18n` crate 里，但要让业务 crate 真正用上它，还差一步：业务 crate 自己也得调用一次 `i18n!(backend = I18nBackend)`，才能在自己的命名空间里生成 `_RUST_I18N_BACKEND` 和转发宏 `_rust_i18n_t!`（回忆 [[u3-l1]]：`t!` 转发壳依赖 `crate::_rust_i18n_t!` 存在）。

可是这一步对所有业务 crate 都是一模一样的样板代码。于是 `i18n` crate 把它封装成一个**声明宏 `init!`**，业务 crate 只要写一行 `i18n::init!();` 就完成接入——这就是「零配置接入」。

这里的精妙之处在于：`init!` 是 `macro_rules!`（声明宏），它**在被调用的 crate 里展开**。也就是说，`i18n::init!()` 展开后的 `i18n!(backend = I18nBackend)` 代码，落在了**业务 crate** 的命名空间，于是生成的 `_RUST_I18N_BACKEND` 属于业务 crate。这与 [[u3-l1]] 讲的「转发壳在调用方 crate 展开」是同一种机制。

#### 4.2.2 核心流程

完整接入链路（以 `my-app1` 为例）：

```
my-app1 写:  i18n::init!();
    ↓ macro_rules 展开（在 my-app1 命名空间）
my-app1 得到: rust_i18n::i18n!(backend = i18n::I18nBackend);
    ↓ 过程宏 generate_code
my-app1 生成:
    static _RUST_I18N_BACKEND = LazyLock(|| {
        let backend = SimpleBackend::new();     // 业务 crate 无 locales → 空
        let backend = backend.extend(I18nBackend);  // 组合：CombinedBackend(空, I18nBackend)
        Box::new(backend)
    });
my-app1 调 t!("welcome"):
    → crate::_rust_i18n_t! → _RUST_I18N_BACKEND.translate
    → CombinedBackend.translate: 先查 I18nBackend(self.1)
    → I18nBackend 委托 → i18n crate 的 _RUST_I18N_BACKEND（装满译文）
```

关键结论有三条：

1. **业务 crate 的 `SimpleBackend` 是空的**：因为 `init!()` 展开出的 `i18n!(backend = ...)` 没有传路径参数，`generate_code` 里 `add_translations` 一次都不执行，所以 `SimpleBackend::new()` 里没有任何译文。
2. **译文来自 `I18nBackend`**：它是 `CombinedBackend` 的 `self.1`，优先级高于空的 `SimpleBackend`（[[u4-l2]] 讲过 `translate` 用 `self.1.or_else(self.0)`）。
3. **最终数据源是 `i18n` crate 的静态后端**：`I18nBackend` 把查询委托回 `i18n::_RUST_I18N_BACKEND`，那里才有真正嵌入的字面量译文。

#### 4.2.3 源码精读

`init!` 宏的定义——整个共享接入的核心就这一句展开：

[examples/share-in-workspace/crates/i18n/src/lib.rs:27-32](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/src/lib.rs#L27-L32) —— `macro_rules! init` 不接收任何参数，固定展开成 `rust_i18n::i18n!(backend = i18n::I18nBackend)`。`#[macro_export]` 让它能被业务 crate 以 `i18n::init!()` 调用。

过程宏侧，`backend = ...` 是怎么被消费的：在 `consume_options` 里，`"backend"` 分支把等号右边的表达式存进 `args.extend` 字段（注意：宏参数叫 `backend`，内部字段叫 `extend`，[[u2-l1]] 讲过这个命名不一致）。

[crates/macro/src/lib.rs:97-100](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L97-L100) —— `"backend" => self.extend = Some(val)`，`val` 是 `I18nBackend` 这个表达式的 AST。

到了 `generate_code`，`extend` 字段被翻译成 `backend.extend(...)` 调用：

[crates/macro/src/lib.rs:321-327](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L321-L327) —— 若传了 `backend`，就生成 `let backend = backend.extend(#extend);`，即把空 `SimpleBackend` 和用户后端组合成 `CombinedBackend`。

最终生成的静态后端结构：

[crates/macro/src/lib.rs:334-347](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L334-L347) —— `_RUST_I18N_BACKEND` 是 `LazyLock<Box<dyn rust_i18n::Backend>>`，闭包内依次执行 `#all_translations`（构造 `SimpleBackend` 并 `add_translations`）、`#extend_code`（组合自定义后端）、`#default_locale`，最后 `Box::new(backend)`。业务 crate 的 `#all_translations` 为空，所以只剩 `extend(I18nBackend)`。

那为什么业务 crate 的 `#all_translations` 一定是空的？因为 `init!()` 不传路径，`Args::parse` 走的是 `else if lookahead.peek(Ident)` 分支（不消费路径）：

[crates/macro/src/lib.rs:199-201](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L199-L201) —— 没有路径字面量时，直接进 `consume_options`，`locales_path` 维持 `load_metadata` 注入的值。

而 `load_metadata` 在业务 crate 没有 `[package.metadata.i18n]` 配置时，会拿到默认值 `"./locales"`：

[crates/support/src/config.rs:39-39](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L39-L39) —— `I18nConfig::default()` 的 `load_path` 是 `"./locales"`。

于是 `try_load_locales("./locales")` 在业务 crate 目录下（`crates/my-app1/`）找不到任何文件，`all_translations` 为空——这正是「业务 crate 不嵌入翻译」的根因，也是共享模式能省内存的机制保证。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：跟踪一次 `i18n::init!()` 的展开与代码生成。
2. **操作步骤**：
   - 在 `examples/share-in-workspace/` 目录下执行 `RUST_I18N_DEBUG=1 cargo build`（关于 `RUST_I18N_DEBUG=1` 见 [[u5-l2]]）。
   - 在编译输出里查找 `my-app1` 生成的 `_RUST_I18N_BACKEND` 片段。
3. **需要观察的现象**：生成的闭包里应能看到 `backend.extend(i18n::I18nBackend)`，且**没有**任何 `add_translations(...)` 调用。
4. **预期结果**：确认业务 crate 后端 = 空的 `SimpleBackend` `extend` 上 `I18nBackend`。
5. 待本地验证（不同工具链版本下 debug 输出格式可能略有差异）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `init!` 用 `macro_rules!`（声明宏），而不是直接在 `i18n` crate 里写一个普通函数？

> **答案**：因为 `i18n!(backend = ...)` 是**过程宏**，它生成的 `_RUST_I18N_BACKEND`、`_rust_i18n_t!` 等符号必须**出现在业务 crate 的命名空间**（这样 `t!` 转发壳的 `crate::_rust_i18n_t!` 才解析得到）。普通函数调用不会把符号注入调用方 crate，只有宏展开能做到。声明宏正好在被调用处展开文本，是封装「样板过程宏调用」的正确工具。

**练习 2**：如果某个业务 crate 想在共享翻译之外，**再加几个自己的私有译文**，该怎么做？

> **答案**：在该业务 crate 下建一个 `locales/` 目录放自己的 YAML，并把 `i18n::init!();` 改成 `rust_i18n::i18n!("locales", backend = i18n::I18nBackend);`。此时 `add_translations` 会把私有译文灌进 `SimpleBackend`（成为 `self.0`），`I18nBackend` 仍是 `self.1`——但要注意优先级：`CombinedBackend.translate` 是 `self.1`（共享）优先，私有同名键反而会被共享译文覆盖。若想让私有优先，需要调换 `extend` 两侧或自定义后端顺序。

---

### 4.3 re-export 与 workspace 依赖复用

#### 4.3.1 概念说明

共享层除了「加载翻译」和「提供 `init!`」，还做了第三件事：**重导出（re-export）`t!` 与 `set_locale`**，让业务 crate 可以用 `use i18n::t;` 而不必直接依赖 `rust_i18n`（虽然这里两者都依赖了，但概念上业务 crate 只需认识 `i18n` 这一个门面）。

这里有一个容易混淆的点要澄清：**`i18n::t!` 和 `rust_i18n::t!` 是同一个宏**。因为 `t!` 在根 crate 里是 `#[macro_export]` 的转发壳（[[u3-l1]]），它永远位于 `rust_i18n` 的 crate 根；`pub use rust_i18n::t` 只是给它起了一个 `i18n::t` 的别名。无论你写 `use i18n::t;` 还是 `use rust_i18n::t;`，调用的都是同一个 `t!`，展开后都路由到 `crate::_rust_i18n_t!`——而 `crate` 是**业务 crate 自己**（因为宏在业务 crate 里被调用）。所以 `my-app1` 和 `my-app2` 分别用两种 `use` 写法，行为完全一致。

另一块是 **`[workspace.dependencies]`**：它把 `rust-i18n` 和 `i18n` 两个依赖在 workspace 根声明一次，成员 crate 用 `xxx.workspace = true` 继承，保证整条依赖树用的是同一份 path、同一个版本，避免重复解析。

#### 4.3.2 核心流程

依赖与重导出的装配顺序：

```
workspace 根 Cargo.toml:
    [workspace.dependencies]
        rust-i18n = { path = "../../" }     # 指向仓库根的 rust-i18n
        i18n      = { path = "crates/i18n" }

i18n crate:
    [dependencies] rust-i18n.workspace = true   # 继承 workspace 依赖
    lib.rs: pub use rust_i18n::{t, set_locale};  # 重导出门面

业务 crate (my-app1 / my-app2):
    [dependencies]
        rust-i18n.workspace = true
        i18n.workspace = true
    lib.rs:
        i18n::init!();                 # 接入共享后端
        use i18n::t;   # 或 use rust_i18n::t;  —— 两者等价
        t!("welcome", locale = "en");  # 取值
```

#### 4.3.3 源码精读

重导出两行：

[examples/share-in-workspace/crates/i18n/src/lib.rs:34-35](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/src/lib.rs#L34-L35) —— `pub use rust_i18n::set_locale;` 和 `pub use rust_i18n::t;`。注意 `t` 是宏，`pub use` 对宏同样有效（因为它被 `#[macro_export]` 暴露到了 `rust_i18n` 的 crate 根）。

workspace 根的依赖声明：

[examples/share-in-workspace/Cargo.toml:4-13](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/Cargo.toml#L4-L13) —— `[workspace] members` 列出三个 crate；`[workspace.dependencies]` 把 `rust-i18n`（指向仓库根 `../../`）和 `i18n`（指向 `crates/i18n`）集中声明。注意根 package 名 `sare-locales-in-workspace`（示例里的小笔误，不影响功能）。

成员 crate 用 `.workspace = true` 继承：

[examples/share-in-workspace/crates/i18n/Cargo.toml:6-7](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/Cargo.toml#L6-L7) —— `i18n` crate 只继承 `rust-i18n`。

[examples/share-in-workspace/crates/my-app1/Cargo.toml:6-8](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/my-app1/Cargo.toml#L6-L8) —— `my-app1` 同时继承 `rust-i18n` 和 `i18n`。`my-app2` 的 [Cargo.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/my-app2/Cargo.toml#L6-L8) 完全一样。

两种等价的 `use` 写法——同一个 `t!`：

[examples/share-in-workspace/crates/my-app1/src/lib.rs:1-3](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/my-app1/src/lib.rs#L1-L3) —— `my-app1` 用 `use i18n::t;`（走重导出门面），再 `i18n::init!();`。

[examples/share-in-workspace/crates/my-app2/src/lib.rs:1-1](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/my-app2/src/lib.rs#L1-L1) —— `my-app2` 同样调 `i18n::init!();`，但在测试里用 `use rust_i18n::t;`（直接用原始门面）。两种写法行为一致，都路由到业务 crate 自己的 `_rust_i18n_t!`。

最后看断言，验证共享翻译确实生效：

[examples/share-in-workspace/crates/my-app1/src/lib.rs:6-15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/my-app1/src/lib.rs#L6-L15) —— `my-app1` 用 `t!("welcome", locale = "en")` 和 `locale = "zh-CN"` 拿到的，正是 `locales/app.yml` 里那份**只在 `i18n` crate 加载过一次**的译文。

#### 4.3.4 代码实践（动手型）

1. **实践目标**：亲手验证 `i18n::t!` 与 `rust_i18n::t!` 等价。
2. **操作步骤**：
   - 进入 `examples/share-in-workspace`，运行 `cargo test -p my-app1` 和 `cargo test -p my-app2`。
   - 在 `my-app1/src/lib.rs` 的 `assert_messages` 里临时再加一行 `assert_eq!(rust_i18n::t!("welcome", locale = "en"), t!("welcome", locale = "en"));`。
3. **需要观察的现象**：两种导入路径下 `t!` 返回值完全相同；测试通过。
4. **预期结果**：两个 crate 的测试都绿，证明 `t!` 是同一个转发壳。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`[workspace.dependencies]` 里写 `rust-i18n = { path = "../../" }`，这个 `../../` 相对的是哪个目录？

> **答案**：相对**workspace 根 Cargo.toml 所在目录**，即 `examples/share-in-workspace/`。`../../` 指向仓库根，正好是发布的 `rust-i18n` 库 crate。成员 crate 用 `.workspace = true` 继承时，path 也由 workspace 根统一解析。

**练习 2**：如果 `i18n` crate 不写 `pub use rust_i18n::t;`，业务 crate 还能用 `t!` 吗？

> **答案**：能。因为业务 crate 的 `[dependencies]` 里也有 `rust-i18n.workspace = true`，可以直接 `use rust_i18n::t;`（`my-app2` 就是这么做的）。`i18n` crate 的 `pub use t` 只是提供了一个更短的 `i18n::t` 别名，让业务 crate 可以「只认识 `i18n` 一个门面」，并非功能必需。

## 5. 综合实践

**任务**：参照 `examples/share-in-workspace`，从零设计一个最小 workspace，让两个业务 crate 共享同一份翻译。

要求：

1. 新建一个 workspace，结构如下（路径仅作参考）：

   ```
   my-workspace/
   ├── Cargo.toml              # workspace 根
   ├── locales/
   │   └── app.yml             # _version: 2，含 "greeting" 的 en / zh-CN 译文
   └── crates/
       ├── i18n/
       │   ├── Cargo.toml
       │   └── src/lib.rs      # 加载翻译、I18nBackend、init!、重导出
       ├── app-a/
       │   ├── Cargo.toml
       │   └── src/lib.rs      # i18n::init!() + 一个返回 t!("greeting") 的函数
       └── app-b/
           ├── Cargo.toml
           └── src/lib.rs      # 同上，但用 use rust_i18n::t; 写法
   ```

2. **workspace 根 `Cargo.toml`** 关键内容（对照 [share-in-workspace/Cargo.toml:4-13](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/Cargo.toml#L4-L13)）：

   ```toml
   # 示例代码
   [workspace]
   members = ["crates/i18n", "crates/app-a", "crates/app-b"]

   [workspace.dependencies]
   rust-i18n = "4"                 # 实际项目中替换为需要的版本
   i18n = { path = "crates/i18n" }
   ```

3. **`crates/i18n/src/lib.rs`** 直接照搬 [share-in-workspace/crates/i18n/src/lib.rs:1-35](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/src/lib.rs#L1-L35) 的结构（注意 `i18n!("../../locales")` 的相对路径要指向你的 `locales/` 目录）。

4. **`app-a` / `app-b` 的 `Cargo.toml`** 里 `rust-i18n.workspace = true`、`i18n.workspace = true`（对照 [my-app1/Cargo.toml:6-8](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/my-app1/Cargo.toml#L6-L8)）。

5. 在两个业务 crate 的 `lib.rs` 顶部写 `i18n::init!();`，并各写一个测试断言 `t!("greeting", locale = "zh-CN")` 返回正确的中文译文（对照 [my-app1/src/lib.rs:6-15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/my-app1/src/lib.rs#L6-L15)）。

6. 运行 `cargo test`，确认两个业务 crate 都能取到**同一份**译文。

**验收点**：

- `locales/app.yml` 只被 `i18n` crate 加载一次（用 `grep -rn 'i18n!("' crates/` 确认业务 crate 不直接加载文件）。
- 两个业务 crate 的测试都通过，证明共享成功。
- 能口头解释：为什么改了 `locales/app.yml` 后，重新 `cargo build` 会让两个业务 crate 的译文同步更新（提示：`build.rs` 的 `rerun-if-changed`，见 [[u5-l2]]）。

## 6. 本讲小结

- **共享 i18n crate** 把「加载翻译 + 生成静态后端」收拢到一个专职 crate，翻译字面量全局只嵌入一次，多 crate 链接进同一二进制时不重复占用 `.rodata`。
- **`I18nBackend`** 是无字段的单元结构体，实现 `Backend` 后把三个方法委托给 `i18n` crate 的 `_RUST_I18N_BACKEND`，并在 `translate` 里追加 `en` 兜底——它是 [[u4-l3]]「包裹静态后端再叠加逻辑」的典型用法。
- **`init!` 宏**是声明宏，在被调用的业务 crate 里展开成 `i18n!(backend = I18nBackend)`；`generate_code` 把它变成 `SimpleBackend::new().extend(I18nBackend)`，即 `CombinedBackend(空, I18nBackend)`。
- **业务 crate 的 `SimpleBackend` 是空的**：因为 `init!()` 不传路径、业务 crate 又没有 `locales/` 目录，`try_load_locales` 找不到文件，译文全部由 `I18nBackend` 委托回共享层。
- **`pub use t / set_locale` 与 `[workspace.dependencies]`** 共同构成门面：`i18n::t!` 与 `rust_i18n::t!` 是同一个转发壳，业务 crate 无论用哪种 `use`，都路由到自己 crate 的 `_rust_i18n_t!`；workspace 依赖继承保证整条依赖树版本一致。
- 本模式是 [[u3-l1]] 转发壳 + [[u4-l3]] 自定义后端两套机制的真实工程拼装，适合多 crate workspace 共用同一套文案的场景。

## 7. 下一步学习建议

- 阅读 [[u8-l4]]（测试体系与质量保障），理解为什么涉及全局 `locale` 的集成测试必须在 `RUST_TEST_THREADS=1` 下运行——本讲的 `cargo test` 在多 crate 并行时也可能踩到这个约束。
- 回看 [[u4-l2]]（`CombinedBackend` 组合），把本讲 `extend(I18nBackend)` 的优先级（`self.1` 优先）和它对「私有译文想覆盖共享译文」的影响彻底弄清。
- 进阶尝试：把 `I18nBackend` 改造成「先查运行时 `HashMap`、miss 再委托共享后端」的二级缓存后端，体会自定义后端在「远程/动态译文 + 编译期静态译文」混合场景下的扩展能力。
- 继续阅读源码：把 `examples/share-in-workspace` 与 `examples/foo` 放在一起对照，巩固「共享加载 vs 每 crate 各自加载」的选型判断。
