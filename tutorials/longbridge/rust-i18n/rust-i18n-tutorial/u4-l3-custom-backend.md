# 自定义后端实战

## 1. 本讲目标

本讲承接 u4-l1（`Backend` trait 与 `SimpleBackend`）和 u4-l2（`BackendExt` 与 `CombinedBackend` 组合），把抽象的「翻译存储」落地为真实场景：**自己写一个后端**。

学完后你应该能够：

1. 为任意数据源（内存 `HashMap`、远程 API、数据库、配置中心）实现 `Backend` trait 的三个方法。
2. 用 `i18n!("locales", backend = MyBackend::new())` 把自定义后端接入 rust-i18n。
3. 准确解释「为什么自定义后端的同名键会优先于本地 YAML/JSON 文件」——并能从源码层面（`extend` → `CombinedBackend` → `self.1` 优先）讲清这条因果链。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（它们在前几讲已经建立）：

- **`Backend` trait**：rust-i18n 对「翻译存储」的抽象，只有三个方法：`available_locales`、`translate(locale, key) -> Option<Cow<str>>`、`messages_for_locale(locale)`。其中 `translate` 只做**精确命中**，**不负责回退**，回退链由外部 `_rust_i18n_try_translate` 编排（见 u4-l1、u3-l4）。
- **`SimpleBackend`**：默认实现，用两层嵌套 `HashMap` 存翻译，`i18n!` 编译期生成的 `_RUST_I18N_BACKEND` 本质就是它（见 u4-l1、u2-l4）。
- **`BackendExt::extend` 与 `CombinedBackend`**：`extend(self, other)` 按值消费两个后端，返回元组结构体 `CombinedBackend(self, other)`，即 `self.0 = 被扩展者`、`self.1 = 后加入者`；`translate` 是 `self.1` 优先、`self.0` 兜底（见 u4-l2）。
- **`Cow<str>`**：枚举类型，`Cow::Borrowed` 借用原始数据零拷贝、`Cow::Owned` 持有新分配的字符串。`Backend` 的返回值用它，能在「命中静态字面量」与「运行时动态生成」之间统一类型。

一句话复习关键结论：**「存储」与「回退策略」是解耦的**。自定义后端只需要老老实实实现「精确命中返回 `Some`、查不到返回 `None`」，回退、territory 削标签、显式 fallback 列表这些复杂逻辑全都不用你管。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `crates/support/src/backend.rs` | 定义 `Backend` trait、`BackendExt`、`CombinedBackend`、`SimpleBackend`，是本讲的「契约」与「组合器」所在。 |
| `tests/integration_tests.rs` | 用 `TestBackend` 演示最小可用的自定义后端，并用 `test_extend_backend` 断言它优先于本地文件。 |
| `examples/share-in-workspace/crates/i18n/src/lib.rs` | 用 `I18nBackend` 演示一种「包裹静态后端、再加 en 兜底」的自定义后端模式，是 workspace 多 crate 共享翻译的核心。 |
| `crates/macro/src/lib.rs` | `i18n!` 过程宏把 `backend = ...` 参数解析进 `args.extend`，并在 codegen 阶段生成 `.extend(#extend)` 调用。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，按「写后端 → 接后端 → 理解优先级」的顺序推进。

### 4.1 实现 Backend trait：自定义数据源

#### 4.1.1 概念说明

`Backend` trait 是 rust-i18n 与「翻译从哪来」之间的**接口契约**。默认情况下翻译来自编译期嵌入的本地文件（`SimpleBackend`）。但现实里翻译可能来自：

- 一个内存 `HashMap`（单元测试、动态配置）。
- 远程翻译平台 / 配置中心（运行时拉取）。
- 数据库（多租户、按用户存翻译）。
- 另一个已经初始化好的后端（workspace 共享，见 4.1.4）。

只要你能把「给定 locale 和 key，返回对应译文」这件事实现出来，就能把它接进 rust-i18n。trait 只规定了三个方法的签名，**对内部数据结构没有任何约束**——你可以用 `HashMap`、`BTreeMap`、`Vec`、甚至一个网络客户端。

注意 trait 上带的三个超职约束 `Send + Sync + 'static`（见 4.1.2），它要求你的后端必须能跨线程安全共享、且不持有非 `'static` 的借用。这把「远程 API 客户端」这类需要带连接池的对象也允许进来，只要连接池本身是线程安全的。

#### 4.1.2 核心流程

trait 的定义本身就是契约的全部，三个方法的职责是：

```
available_locales()      -> Vec<Cow<str>>            枚举这个后端支持哪些语言
translate(locale, key)   -> Option<Cow<str>>         精确命中返回 Some，否则 None（不做回退）
messages_for_locale(loc) -> Option<Vec<(k, v)>>      返回某语言下的全部 (key, value) 列表
```

实现一个自定义后端的步骤可以归纳为：

1. 定义一个 struct，内部装你自己的数据源（如 `HashMap`）。
2. `impl Backend for YourStruct`，依次实现三个方法。
3. `translate` 里查不到就返回 `None`，**不要**自己写 fallback 逻辑。
4. 用 `Cow::from(...)` 把你的字符串包装成返回类型。

#### 4.1.3 源码精读

先看 trait 契约本身：

[crates/support/src/backend.rs:5-12](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L5-L12) — 这是 `Backend` trait 的全部定义。注意三个方法都返回带 `'static` 或 `'_` 生命周期的 `Cow`，trait 自身带 `Send + Sync + 'static` 约束，确保它最终能被装进 `Box<dyn Backend>` 放进全局静态量 `_RUST_I18N_BACKEND` 供多线程访问（u2-l4）。

再看集成测试里**最小可用的自定义后端** `TestBackend`：

[tests/integration_tests.rs:3-13](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L3-L13) — `TestBackend` 内部只是一个 `HashMap<String, String>`，`new()` 时写入一条 `"foo" => "pt-fake.foo"`。这证明自定义后端可以完全不依赖任何文件，数据全在内存里。

[tests/integration_tests.rs:15-39](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L15-L39) — `impl Backend for TestBackend`。重点观察三件事：

- `available_locales` 直接返回硬编码的 `["pt", "en"]`（即便数据里只有 `pt`，也可以声明自己支持哪些 locale）。
- `translate` 用 `if locale == "pt"` 做语言守卫，只对 `pt` 返回数据，其它 locale 一律 `None`。**它不做任何回退**——查不到就是 `None`。
- `messages_for_locale` 同样只对 `pt` 返回 `Some(...)`。注意它把 `HashMap` 的迭代结果重新包装成 `(Cow, Cow)` 元组 `Vec`。

这个例子里 `Cow::from(v.as_str())` 用的是 `as_str()` 借用，因此返回的是 `Cow::Borrowed`（零拷贝）。如果你的译文是运行时拼接出来的（如远程 API 返回的 `String`），则会得到 `Cow::Owned`，两种情况对调用方完全透明。

#### 4.1.4 进阶示例：包裹静态后端的 I18nBackend

`examples/share-in-workspace` 提供了另一种自定义后端写法：它**不存自己的数据，而是包裹另一个已经初始化好的后端**，并在其上叠加一层「en 兜底」逻辑。

[examples/share-in-workspace/crates/i18n/src/lib.rs:4](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/src/lib.rs#L4) — 先调用 `i18n!("../../locales")`，让这个 i18n crate 自己编译期加载本地文件、生成静态后端 `_RUST_I18N_BACKEND`。

[examples/share-in-workspace/crates/i18n/src/lib.rs:6-25](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/src/lib.rs#L6-L25) — `I18nBackend` 是个单元结构体（无字段），它的 `translate` 先查 `_RUST_I18N_BACKEND.translate(locale, key)`，如果 miss，再查一次 `_RUST_I18N_BACKEND.translate("en", key)` 做 en 兜底。这是一种**把回退逻辑塞进后端本身**的写法，与 trait「不负责回退」的原则并不冲突——这里只是后端内部的实现细节，对外仍是一个普通的 `Backend`。这种「包裹 + 叠加」的思路是 workspace 共享翻译的关键，将在 u8-l3 展开。

> 提示：4.1.4 这种「自定义后端内部包裹静态后端」的写法，配合 `init!` 宏（见 4.2.3）能让多个 crate 共用同一份翻译。本讲只需理解「`Backend` 也可以这样组合实现」即可。

#### 4.1.5 代码实践

**实践目标**：脱离 `i18n!` 宏，直接手写一个自定义后端并验证它的 `translate` 行为。

**操作步骤**（示例代码，非项目原有代码）：

```rust
// 示例代码：独立验证自定义后端的 translate 行为
use rust_i18n::Backend;
use std::borrow::Cow;
use std::collections::HashMap;

struct DictBackend {
    trs: HashMap<String, String>,
}

impl DictBackend {
    fn new() -> Self {
        let mut trs = HashMap::new();
        trs.insert("greeting".into(), "你好".into());
        Self { trs }
    }
}

impl Backend for DictBackend {
    fn available_locales(&self) -> Vec<Cow<'_, str>> {
        vec![Cow::from("zh-CN")]
    }

    fn translate(&self, locale: &str, key: &str) -> Option<Cow<'_, str>> {
        if locale != "zh-CN" {
            return None; // 精确语言守卫，不命中就 None
        }
        self.trs.get(key).map(|v| Cow::from(v.as_str()))
    }

    fn messages_for_locale(&self, locale: &str) -> Option<Vec<(Cow<'_, str>, Cow<'_, str>)>> {
        if locale != "zh-CN" {
            return None;
        }
        Some(
            self.trs
                .iter()
                .map(|(k, v)| (Cow::from(k.as_str()), Cow::from(v.as_str())))
                .collect(),
        )
    }
}

fn main() {
    let b = DictBackend::new();
    assert_eq!(b.translate("zh-CN", "greeting"), Some(Cow::from("你好")));
    assert_eq!(b.translate("en", "greeting"), None); // 非 zh-CN 直接 None
    assert_eq!(b.translate("zh-CN", "missing"), None); // key 不存在也 None
}
```

**需要观察的现象**：

- `translate("zh-CN", "greeting")` 命中，返回 `Some(Cow::Borrowed("你好"))`。
- 语言不匹配（`"en"`）或 key 不存在时都返回 `None`，**没有任何回退**。

**预期结果**：三条 `assert_eq!` 全部通过。注意：这里的 `None` 是「我没这条翻译」的诚实信号，回退是由调用方（`_rust_i18n_try_translate`）负责的，不要在后端里偷偷加回退。

> 待本地验证：上述示例依赖 `rust_i18n` crate 的 `Backend` 在根 crate 被 re-export。若你直接放在 examples 里运行，请确认 `Backend` 已通过 `use rust_i18n::Backend;` 正确引入。

#### 4.1.6 小练习与答案

**练习 1**：如果想让 `DictBackend` 在「key 不存在」时返回一个固定的占位文案（比如 `"???"`），应该改 `translate` 让它返回 `Some(Cow::from("???"))` 吗？

**参考答案**：**不应该**。`translate` 返回 `Some` 意味着「这是一条真实命中」，调用方的回退链（territory 回退、显式 fallback）就不会再尝试其它来源了。正确的做法是返回 `None`，把「补一个占位文案」交给最外层的 `_rust_i18n_translate`（它会在全 miss 时返回原始 key）。混淆「命中」与「兜底」是自定义后端最常见的坑。

**练习 2**：为什么 `Backend` trait 要带 `Send + Sync + 'static` 约束？

**参考答案**：因为后端最终会被装进 `Box<dyn Backend>` 放进全局静态量 `_RUST_I18N_BACKEND`（`LazyLock`），在多线程下被任意线程通过 `t!` 并发查询（见 u8-l1）。`Send + Sync` 保证跨线程安全共享，`'static` 保证它不持有任何临时借用，可以存活到程序结束。

---

### 4.2 用 i18n!(backend = ...) 接入

#### 4.2.1 概念说明

实现了 `Backend` 只是第一步，还要让它**生效**。rust-i18n 提供了 `i18n!` 宏的 `backend = ...` 命名参数，让你把一个自定义后端表达式注入到生成的全局后端里。

这里有一个关键的「命名错位」需要提前点破，否则读源码会很困惑：**宏参数叫 `backend`，但 `i18n!` 内部对应的字段叫 `extend`**。原因正是 u4-l2 讲的——接入自定义后端的底层机制就是 `extend`（组合成 `CombinedBackend`）。从用户视角是「设置 backend」，从实现视角是「extend 现有的本地文件后端」。

#### 4.2.2 核心流程

`backend = ...` 从宏参数到生成代码的流程：

```
i18n!("locales", backend = MyBackend::new())
        │
        │  ① Args::parse 解析命名参数
        ▼
   args.extend = Some(<MyBackend::new() 的 AST>)
        │
        │  ② generate_code 里判断 args.extend
        ▼
   生成:  let backend = backend.extend(MyBackend::new());
        │
        │  ③ backend 此时是 CombinedBackend(SimpleBackend_local, MyBackend)
        ▼
   Box::new(backend)  装进 _RUST_I18N_BACKEND
```

第 ② 步生成的 `backend.extend(...)`，会让 `MyBackend` 成为 `CombinedBackend` 的 `self.1`（后加入者），而本地文件加载出来的 `SimpleBackend` 是 `self.0`。这直接决定了 4.3 节要讲的优先级。

#### 4.2.3 源码精读

**① 字段定义与参数解析**

[crates/macro/src/lib.rs:16](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L16) — `Args` 里承载 `backend=` 的字段就叫 `extend`，类型是 `Option<syn::Expr>`，保存的是用户写的表达式的 AST（而不是求值结果，因为过程宏在编译期只操作 token / AST）。

[crates/macro/src/lib.rs:97-100](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L97-L100) — `consume_options` 里 `"backend"` 分支：把 `=` 右边的表达式 `parse::<Expr>()` 后存进 `self.extend`。注意它用 `Expr` 接收，意味着 `backend = ` 后面可以是任意表达式：`MyBackend::new()`、`MyBackend::from_env()`、甚至一个返回 `impl Backend` 的函数调用。

**② codegen 阶段生成 extend 调用**

[crates/macro/src/lib.rs:321-327](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L321-L327) — 这是连接 `args.extend` 与运行时的关键代码。如果用户传了 `backend=`，就生成 `let backend = backend.extend(#extend);`，其中 `#extend` 替换成用户的表达式；否则生成空块，`backend` 保持为本地文件的 `SimpleBackend`。

**③ 装进全局静态量**

[crates/macro/src/lib.rs:334-347](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L334-L347) — 生成的 `_RUST_I18N_BACKEND` 是个 `LazyLock<Box<dyn rust_i18n::Backend>>`。注意代码块里的顺序：

- 先 `#all_translations`：构造本地文件的 `SimpleBackend`（变量名 `backend`）。
- 再 `#extend_code`：如果有 `backend=`，执行 `backend = backend.extend(用户后端)`，`backend` 变成 `CombinedBackend`。
- 最后 `Box::new(backend)` 装箱。

[crates/macro/src/lib.rs:290-296](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L290-L296) — 对应的本地文件后端构造：`let mut backend = rust_i18n::SimpleBackend::new();` 再逐语言 `add_translations`。这就是 `extend` 的 `self.0`。

**真实用例：集成测试接入 TestBackend**

[tests/integration_tests.rs:41-45](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L41-L45) — `rust_i18n::i18n!("./tests/locales", fallback = "en", backend = TestBackend::new())`，同时演示了 `backend=` 与 `fallback=` 可以一起用。

**真实用例：share-in-workspace 的 init! 宏**

[examples/share-in-workspace/crates/i18n/src/lib.rs:27-32](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/share-in-workspace/crates/i18n/src/lib.rs#L27-L32) — 这里 `init!` 宏展开成 `rust_i18n::i18n!(backend = i18n::I18nBackend)`，**注意它没有写 locales 路径**。`i18n!` 的默认路径是 `"locales"`（见 [crates/macro/src/lib.rs:180-181](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L180-L181)），业务 crate 下没有这个目录，所以加载出空数据的 `SimpleBackend`，再 `.extend(I18nBackend)`——所有翻译实际来自 `I18nBackend`（它内部包裹了 i18n crate 那份真正的静态后端）。这是 workspace 共享翻译的精髓，详见 u8-l3。

#### 4.2.4 代码实践

**实践目标**：用 `RUST_I18N_DEBUG=1` 直接观察 `i18n!(backend = ...)` 生成的代码，确认其中真的有 `.extend(...)` 调用。

**操作步骤**：

1. 在 `examples/app/main.rs`（或你自己的最小项目）的 `i18n!("locales")` 调用上，临时加一个 `backend = ...`（可复用上面 4.1.5 的 `DictBackend`，先 `include!` 或把它放进同一 crate）。
2. 用 `RUST_I18N_DEBUG=1 cargo build` 编译。
3. 在编译输出里搜索 `let backend = backend.extend(` 这一行。

**需要观察的现象**：调试输出会打印出生成的 Rust 源码，里面应能看到先 `SimpleBackend::new()` 灌数据、再 `backend.extend(你的后端)`、最后 `Box::new(backend)` 的三段结构。

**预期结果**：能定位到 `backend.extend(...)` 语句，验证「`backend=` 参数在 codegen 里就是一次 extend 调用」。

> 待本地验证：不同 rust-i18n 版本调试输出的格式可能略有差异；若 `RUST_I18N_DEBUG=1` 无输出，请确认环境变量在 `cargo build` 命令行前缀生效（而非写在程序里）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `backend = ` 后面接的是表达式（`MyBackend::new()`），而不是一个类型名？

**参考答案**：因为过程宏在编译期只能拿到 token / AST，无法执行 `MyBackend::new()`。它把这个表达式原样嵌入生成代码，真正的求值发生在程序运行期 `_RUST_I18N_BACKEND` 首次被访问（`LazyLock` 初始化）时。所以 `backend=` 接受的是「能产生一个 `impl Backend` 的表达式」。

**练习 2**：`backend=` 对应的内部字段为什么不叫 `backend` 而叫 `extend`？

**参考答案**：因为底层机制就是 u4-l2 的 `BackendExt::extend`——把自定义后端与本地文件后端组合成 `CombinedBackend`。命名 `extend` 直接反映了「在现有后端之上叠加」的实现本质，而对外暴露的参数名 `backend` 则贴合用户「我要设置后端」的心智模型。

---

### 4.3 extend 优先级：为什么自定义后端覆盖本地文件

#### 4.3.1 概念说明

这是本讲最核心的结论：**当自定义后端与本地文件存在同名键时，自定义后端的值优先**。这条结论不是魔法，而是 `CombinedBackend::translate` 的查找顺序决定的。

回顾 u4-l2：`backend.extend(other)` 返回 `CombinedBackend(self, other)`，即 `self.0 = 本地文件后端`、`self.1 = 自定义后端`。`translate` 的实现是「先查 `self.1`，miss 再查 `self.0`」——也就是**后加入者优先**。

这个设计很实用：你可以用自定义后端做**覆盖**（比如线上热修某条文案、A/B 测试某段文案），而不必去改编译期嵌入的本地文件。

#### 4.3.2 核心流程

`CombinedBackend::translate` 的查找顺序可以用一个简单的逻辑式表达。设 \( T_B(\ell, k) \) 表示后端 \( B \) 对 locale \( \ell \)、key \( k \) 的查找结果，\( \oplus \) 表示「取第一个 `Some`、否则取第二个」（即 `Option::or_else` 的语义）：

\[
T_{\text{combined}}(\ell, k) \;=\; T_{B_{\text{custom}}}(\ell, k) \;\oplus\; T_{B_{\text{local}}}(\ell, k)
\]

其中 \( B_{\text{custom}} = \text{self.1} \)（自定义后端），\( B_{\text{local}} = \text{self.0} \)（本地文件后端）。因为 \( B_{\text{custom}} \) 在左侧、`or_else` 优先取左侧的非空值，所以自定义后端的命中会「挡住」本地文件的同名键。

三个方法的优先级方向需要分别记清楚：

| 方法 | 优先级方向 | 实现 |
| --- | --- | --- |
| `translate` | `self.1` 优先，`self.0` 兜底 | `or_else` |
| `messages_for_locale` | `self.1` 胜，按 key 去重 | `filter` 掉 `self.1` 已有的 key |
| `available_locales` | 取并集，以 `self.0` 为基底 | `contains` 显式去重 |

注意 `available_locales` 的方向与 `translate` 不同：它取的是**并集**（两边都算），先后只影响列表顺序，不影响谁优先；而 `translate` 和 `messages_for_locale` 都是 `self.1` 胜。

#### 4.3.3 源码精读

[crates/support/src/backend.rs:41-46](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L41-L46) — `CombinedBackend::translate` 的全部实现：`self.1.translate(locale, key).or_else(|| self.0.translate(locale, key))`。这一行就是「自定义后端优先」的**根因**。注意它带 `#[inline]`，因为 `t!` 每次调用都会走到这里，是热路径。

[crates/support/src/backend.rs:48-65](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L48-L65) — `messages_for_locale` 的去重合并：以 `self.1`（自定义后端）的列表为基底，再把 `self.0` 中「`self.1` 没有的 key」补进来（`filter(|(k, _)| self.1.translate(locale, k).is_none())`）。所以同名 key 的值取 `self.1` 的版本。

[crates/support/src/backend.rs:31-39](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L31-L39) — `available_locales` 以 `self.0`（本地）为基底，遍历 `self.1`（自定义）的 locale，用 `contains` 去重后追加。这是「取并集」，**不**是「自定义优先」。

[crates/support/src/backend.rs:14-22](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L14-L22) — 回顾 `extend` 的定义：`CombinedBackend(self, other)`，`self`（本地）进 `.0`、`other`（自定义）进 `.1`。结合上面 `translate` 的 `.1` 优先，整条因果链闭合。

**用断言验证优先级**

[tests/integration_tests.rs:285-288](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L285-L288) — `test_extend_backend` 断言 `t!("foo", locale = "pt") == "pt-fake.foo"`。注意 `tests/locales/` 目录下**根本没有 `pt.yml`**（可用 `ls tests/locales/` 验证只有 `en.json/en.yml/zh-CN.yml/zh.yml/v2.yml/...`），所以 `pt` 这个 locale 完全来自 `TestBackend`。这条测试同时验证了两件事：① 自定义后端能引入本地文件里没有的新 locale；② 自定义后端的值能被 `t!` 正确取到。

如果想看「同名键覆盖」的直接证据，可以参考 `backend.rs` 自带的单元测试：

[crates/support/src/backend.rs:187-217](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L187-L217) — `test_combined_backend` 里，`backend`（`.0`）的 `en.hello = "Hello"`、`backend2`（`.1`）的 `en.hello = "Hello2"`，组合后 `combined.translate("en", "hello")` 断言为 `"Hello2"`——即 `.1`（后加入者）的值覆盖了 `.0`。这就是「自定义后端优先」的最小验证。

#### 4.3.4 代码实践

**实践目标**：亲手验证「自定义后端的同名键优先于本地文件」。

**操作步骤**：

1. 复制 `examples/app` 为一个新项目（或直接在 `examples/app` 上改）。
2. 在 `examples/app/locales/en.yml` 里确认有 `hello` 这类键（例如值 `"Hello"`）。
3. 新增一个自定义后端（示例代码，非项目原有代码）：

   ```rust
   // 示例代码：覆盖本地文件的 hello 键
   use rust_i18n::Backend;
   use std::borrow::Cow;
   use std::collections::HashMap;

   struct OverrideBackend { trs: HashMap<String, String> }
   impl OverrideBackend {
       fn new() -> Self {
           let mut trs = HashMap::new();
           // 故意与本地 en.yml 里的 hello 同名
           trs.insert("hello".into(), "Hello from CUSTOM backend".into());
           Self { trs }
       }
   }
   impl Backend for OverrideBackend {
       fn available_locales(&self) -> Vec<Cow<'_, str>> { vec![Cow::from("en")] }
       fn translate(&self, locale: &str, key: &str) -> Option<Cow<'_, str>> {
           if locale != "en" { return None; }
           self.trs.get(key).map(|v| Cow::from(v.as_str()))
       }
       fn messages_for_locale(&self, _: &str) -> Option<Vec<(Cow<'_, str>, Cow<'_, str>)>> { None }
   }
   ```

4. 把 `i18n!("locales")` 改成 `i18n!("locales", backend = OverrideBackend::new())`。
5. 在 `main` 里打印 `t!("hello", locale = "en")`。

**需要观察的现象**：输出是 `"Hello from CUSTOM backend"`，而不是本地 `en.yml` 里的 `"Hello"`。

**预期结果**：自定义后端的值覆盖了本地文件——这就是 4.3.3 那行 `self.1.translate(...).or_else(|| self.0.translate(...))` 的运行时体现。

**若想反向验证**：把 `OverrideBackend` 里那条 `hello` 删掉（或改成别的 key），重新编译运行，此时 `t!("hello", locale = "en")` 应该回退到本地文件的原值——因为自定义后端 miss 后，`or_else` 会去查 `self.0`（本地文件）。

> 待本地验证：实际输出值取决于你 `en.yml` 里 `hello` 的真实取值；重点是「同名时自定义胜、自定义 miss 时本地胜」这两条对比观察。

#### 4.3.5 小练习与答案

**练习 1**：假设本地 `en.yml` 有 `a`、`b` 两个键，自定义后端有 `b`、`c` 两个键（都是 `en`）。组合后 `t!("a")`、`t!("b")`、`t!("c")` 分别取哪个来源的值？

**参考答案**：`a` 只在本地 → 取本地值；`b` 两边都有 → 取**自定义后端**的值（`self.1` 优先）；`c` 只在自定义 → 取自定义值。这正是 `self.1.translate(...).or_else(|| self.0.translate(...))` 的行为：自定义命中就挡住本地，自定义 miss 才轮到本地。

**练习 2**：`available_locales()` 在上述场景下会返回什么？顺序如何？

**参考答案**：返回本地与自定义 locale 的**并集**去重。若本地是 `["en"]`、自定义声明 `["en"]`，结果是 `["en"]`（去重）。顺序以 `self.0`（本地）为基底、再追加 `self.1`（自定义）里没有的 locale，所以「自定义优先」**不适用于** `available_locales`——它只是取并集，别和 `translate` 的优先级搞混。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**用自定义后端给一个已有项目「热补」一条翻译，并验证它优先于本地文件**。

任务要求：

1. 选一个最小项目（推荐基于 `examples/app`）。
2. 在本地 `en.yml` 中确认存在某键 `messages.hello`（或任选一个真实存在的键），记下它的本地值。
3. 实现一个 `PatchBackend`，内部用 `HashMap` 存放：① 一条与本地**同名**的键（值改成 `[PATCHED] ...`）以演示「覆盖」；② 一条本地**不存在**的新键（如 `dynamic.feature_flag_msg`）以演示「新增」。
4. 用 `i18n!("locales", backend = PatchBackend::new())` 接入。
5. 运行程序，分三种情况打印并解释：
   - 同名键 → 应显示 `[PATCHED] ...`（自定义优先）。
   - 新键 → 应显示 `PatchBackend` 的值（本地没有，靠自定义提供）。
   - 既不在本地也不在自定义的键 → 应触发回退链（territory / 显式 fallback），最终 miss 时返回原始 key（见 u3-l4）。
6. （可选）用 `RUST_I18N_DEBUG=1 cargo build` 找到生成代码里的 `.extend(...)`，把你看到的「三段结构」（`SimpleBackend` 灌数据 → `extend` → `Box::new`）贴进学习笔记。

通过这个任务，你会同时用到：实现 `Backend`（4.1）、`i18n!(backend=...)` 接入（4.2）、`extend` 优先级与 miss 回退（4.3 + u3-l4）。

## 6. 本讲小结

- 实现 `Backend` 只需定义一个 struct 并实现三个方法（`available_locales` / `translate` / `messages_for_locale`），数据源完全自由（内存 `HashMap`、远程 API、甚至包裹另一个后端），trait 带 `Send + Sync + 'static` 是为了能装进全局静态量供多线程访问。
- `translate` 只做**精确命中**、查不到返回 `None`，**不要**在后端里写回退逻辑；回退是 `_rust_i18n_try_translate` 的职责。
- `i18n!("locales", backend = ...)` 是接入入口；宏参数叫 `backend`、内部字段叫 `extend`，codegen 时生成 `backend.extend(你的后端)`。
- 接入后，本地文件后端是 `CombinedBackend` 的 `self.0`、自定义后端是 `self.1`。
- `translate` 用 `self.1.or_else(self.0)`，**故自定义后端的同名键优先于本地文件**；`messages_for_locale` 同向（`self.1` 胜）；`available_locales` 则是取并集去重，方向不同。
- 「包裹静态后端再叠加逻辑」（如 `I18nBackend` 的 en 兜底）是 workspace 共享翻译的基础，将在 u8-l3 展开。

## 7. 下一步学习建议

- **u5-l1（Cargo.toml 配置）**：学完自定义后端后，可以去看 `[package.metadata.i18n]` 如何配置 `fallback`、`minify-key` 等参数，理解「metadata 中等优先级 < 宏显式参数」如何与 `backend=` 协同。
- **u8-l3（Workspace 多 crate 共享翻译）**：本讲 4.1.4 / 4.2.3 已经预告了 `I18nBackend` + `init!` 宏的共享模式，u8-l3 会完整拆解「一个 i18n crate 集中初始化、多个业务 crate 复用」的工程实践，强烈建议紧接着学。
- **u8-l1（线程安全）**：如果你好奇 `Box<dyn Backend>` 如何在多线程下被并发查询、`LazyLock` 如何保证只初始化一次，去读 `CURRENT_LOCALE` 与 `AtomicStr` 的实现。
- **动手扩展**：尝试实现一个「从 JSON 文件运行时加载」的后端（需要启用 `load-path` feature，见 u5-l3），对比它与编译期 `SimpleBackend` 在二进制体积和依赖上的差异。
