# BackendExt 与 CombinedBackend 组合

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 `BackendExt::extend` 为什么能把**任意两个**后端拼成一个组合后端，以及它「消费 `self`」的设计意图。
2. 画出 `CombinedBackend<A, B>` 的元组结构，并解释 `A`(`self.0`) 与 `B`(`self.1`) 在三方法里各自的优先级。
3. 复述 `translate`、`available_locales`、`messages_for_locale` 三个方法的合并语义，并解释它们**为什么优先级方向不一致**。
4. 把 `i18n!("locales", backend = MyBackend::new())` 的运行结果与 `CombinedBackend::translate` 的源码对应起来，讲清「自定义后端优先于本地文件」的根因。

本讲承接 [u4-l1](./u4-l1-backend-trait-simplebackend.md)：你已经知道 `Backend` 是翻译存储抽象、`SimpleBackend` 是默认实现。本讲要回答的下一个问题是——**当我手头有两份翻译来源（比如「编译期代码生成的本地文件」和「运行时从远程 API 拉取的翻译」），怎么把它们合并成一个后端？**

## 2. 前置知识

- **trait 的 supertrait（父 trait）**：当一个 trait 声明成 `trait BackendExt: Backend`，意思是「只有实现了 `Backend` 的类型才能实现 `BackendExt`」。本讲里 `BackendExt` 就是搭在 `Backend` 肩膀上的「增强补丁」。
- **泛型与所有权**：`fn extend<T: Backend>(self, other: T)` 接收的是**两个有具体类型、在编译期已知大小**的后端值，`self` 表示按值拿走所有权（不是 `&self` 借用）。
- **`Self: Sized` 约束**：Rust 的 trait 对象 `dyn Backend` 是「动态大小类型（DST）」，不能按值传递；`Self: Sized` 这个约束等于在类型层面声明「这个方法只能由具体类型调用，不能由 `dyn` 调用」。这是本讲一个关键伏笔。
- **`Cow<'_, str>` 与 `or_else`**：`translate` 返回 `Option<Cow<'_, str>>`，`or_else` 让「前一个返回 `None` 时才求值后一个」，正好用来表达「前者查不到再查后者」的回退。
- 建议先读完 [u4-l1](./u4-l1-backend-trait-simplebackend.md)，熟悉 `Backend` 三个方法和 `SimpleBackend` 的两层 `HashMap` 结构。

## 3. 本讲源码地图

本讲只围绕一个源码文件，外加宏侧的一小段配合代码：

| 文件 | 作用 |
| --- | --- |
| [`crates/support/src/backend.rs`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs) | 本讲主角：定义 `BackendExt`、`CombinedBackend`，以及 `SimpleBackend` 的空 `BackendExt` impl 和一个现成的 `test_combined_backend` 测试。 |
| [`crates/macro/src/lib.rs`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) | `generate_code` 里 `extend_code` 一段：把用户写的 `i18n!(backend = ...)` 翻译成 `backend.extend(#extend)` 调用，决定了本地文件与自定义后端谁是 `self.0`、谁是 `self.1`。 |

## 4. 核心概念与源码讲解

### 4.1 BackendExt：给所有后端装上「组合能力」

#### 4.1.1 概念说明

`Backend` trait 本身只规定「怎么存、怎么查」，并没有规定「怎么把两个后端拼起来」。组合能力是被单独抽到一个**扩展 trait `BackendExt`** 里的，它只多出一个方法 `extend`。

这样做有两个好处：

1. **职责分离**：`Backend` 回答「翻译从哪来」，`BackendExt` 回答「多个来源怎么叠加」。不实现组合的类型（理论上）依然可以只实现 `Backend`。
2. **零成本扩展现有实现**：因为 `BackendExt` 是 `Backend` 的子 trait，且 `extend` 有默认实现，所以任何已经实现 `Backend` 的类型只需写一行空的 `impl BackendExt for X {}` 就免费获得组合能力——`SimpleBackend` 正是这么做的。

#### 4.1.2 核心流程

`extend` 的语义可以概括为一句话：**把「我自己」和「另一个后端」打包成一个 `CombinedBackend`，我自己变成 `self.0`，另一个变成 `self.1`。**

```
self  (类型 A，比如 SimpleBackend，本地文件)
   │  extend(other)
   ▼
CombinedBackend(self, other)
   │            │
   │            └─ self.1 = other (类型 B)
   └─ self.0 = self (类型 A)
```

关键点：

- 按值消费 `self` 和 `other`，不保留对原始两个后端的独立引用。
- 返回类型是**具体类型** `CombinedBackend<Self, T>`，而不是 `Box<dyn Backend>`——因为组合发生在「装箱（boxing）」之前（见 4.1.4）。
- 约束 `Self: Sized`：阻止 `dyn Backend` 调用 `extend`。

#### 4.1.3 源码精读

扩展 trait 与 `extend` 方法：[crates/support/src/backend.rs:14-22](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L14-L22)

```rust
pub trait BackendExt: Backend {
    /// Extend backend to add more translations
    fn extend<T: Backend>(self, other: T) -> CombinedBackend<Self, T>
    where
        Self: Sized,
    {
        CombinedBackend(self, other)
    }
}
```

- 第 14 行 `trait BackendExt: Backend` 把 `BackendExt` 钉死在 `Backend` 之上。
- `fn extend<T: Backend>(self, other: T)`：`self` 按值传入（无 `&`），`other` 是任意实现了 `Backend` 的类型 `T`。
- `where Self: Sized`：保证调用方是具体类型。
- 方法体只有一行 `CombinedBackend(self, other)`：构造元组结构体，`self` 落到 `.0`，`other` 落到 `.1`。

`SimpleBackend` 的「免费」实现，整行只有一个空大括号：[crates/support/src/backend.rs:147](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L147)

```rust
impl BackendExt for SimpleBackend {}
```

因为 `extend` 在 trait 里有默认实现，这里什么都不写，`SimpleBackend` 就拿到了 `extend`。

#### 4.1.4 为什么 `extend` 必须发生在「装箱」之前

这是一个贯穿本讲的关键理解，先在这里铺垫：

- `Self: Sized` 排除了 `dyn Backend`，所以一个已经被 `Box::new` 成 `Box<dyn Backend>` 的后端**不能**再调用 `extend`。
- 因此 `i18n!` 生成的代码顺序必须是：先用具体类型 `SimpleBackend` 构造 → 调 `extend`（仍是具体类型 `CombinedBackend<…>`）→ 最后才 `Box::new` 成 `Box<dyn Backend>`。

这一点在 4.3 节用宏侧源码验证。

### 4.2 CombinedBackend：两个后端的元组组合体

#### 4.2.1 概念说明

`CombinedBackend<A, B>` 是一个只有两个字段的元组结构体，两个字段分别是一个后端。它本身也实现了 `Backend`，所以它可以**像单个后端一样被使用**，甚至可以被再次 `extend`（组合可以嵌套）。

```rust
pub struct CombinedBackend<A, B>(A, B);
```

命名约定（本讲全程遵守）：

- `A` = `self.0` = `extend` 调用时的「被扩展者」（第一个后端）。
- `B` = `self.1` = `extend` 调用时的 `other`（第二个、**后加入**的后端）。

> 术语提示：「先/后」是按 `extend` 的书写顺序，不是按翻译优先级。优先级在 4.3 讲。

#### 4.2.2 核心流程

`CombinedBackend` 实现 `Backend` 时，要求 `A: Backend, B: Backend`，这样三个方法的实现里就能分别委托给 `self.0` 和 `self.1`，再按各自规则合并结果：

```
CombinedBackend::translate(locale, key)
        │
        ├── 先问 self.1（B）  ── 命中即返回
        │
        └── 再问 self.0（A）  ── 作为兜底
```

由于 `CombinedBackend` 自己也是 `Backend`，组合是可链式的：

```
a.extend(b)            => CombinedBackend(a, b)
a.extend(b).extend(c)  => CombinedBackend<CombinedBackend<a,b>, c>
```

链式 `extend` 时，**最后 `extend` 进来的（最外层 `self.1`）优先级最高**，逐层向内回退。

#### 4.2.3 源码精读

结构体定义：[crates/support/src/backend.rs:24](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L24)

```rust
pub struct CombinedBackend<A, B>(A, B);
```

trait 实现的边界约束：[crates/support/src/backend.rs:26-30](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L26-L30)

```rust
impl<A, B> Backend for CombinedBackend<A, B>
where
    A: Backend,
    B: Backend,
{
    // 三个方法的合并实现见 4.3
}
```

`A` 和 `B` 都被要求实现 `Backend`，这正是「组合可嵌套」的类型基础——`CombinedBackend` 本身满足 `Backend`，于是它可以作为外层 `extend` 的 `A` 或 `B`。

### 4.3 三方法的优先级合并语义

这是本讲的重点。三个方法对 `self.0` 与 `self.1` 的处理**方向并不一致**，需要分别记清。

#### 4.3.1 概念说明

| 方法 | 谁优先 | 合并方式 | 去重？ |
| --- | --- | --- | --- |
| `translate` | `self.1`（B）优先 | 短路回退：B miss 才查 A | 隐式（命中即返回，天然只取一个） |
| `available_locales` | 无所谓先后（要的是集合） | 把 B 的 locale 追加到 A 之后 | **显式去重**（`contains` 判重） |
| `messages_for_locale` | `self.1`（B）优先 | 全量拼接，但 A 中「B 也有的键」被过滤掉 | 显式（按 key 去重，B 胜） |

一句话总结：**凡是「按 key 取一个值」的（translate、messages_for_locale），都是 `self.1` 赢；而 `available_locales` 只是收集一个 locale 名单，靠 `contains` 去重保证不重复，先后顺序只影响列表顺序，不影响集合内容。**

#### 4.3.2 核心流程

**`translate`（核心）：**

```
self.1.translate(locale, key)  ── Some(v) ──▶ 返回 Some(v)
        │
        └── None ──▶ self.0.translate(locale, key) ──▶ 原样返回（Some 或 None）
```

用 `Option::or_else` 表达：「前面是 `None` 才求值后面」。这正好对应「B 查不到再查 A」。

**`available_locales`：**

```
result = self.0.available_locales()           // 先放 A 的全部
for loc in self.1.available_locales():
    if loc not in result:                      // contains 判重
        result.push(loc)
return result
```

注意起点是 `self.0`，与 `translate`「`self.1` 优先」相反。但因为它最终要的是**去重后的并集**，谁先放进去不影响集合内容，只影响列表里 locale 的排列顺序。

**`messages_for_locale`：**

```
(b, a) = (self.1.messages_for_locale(), self.0.messages_for_locale())
若都为 None            -> None
若只有一个为 None      -> 返回另一个
若都为 Some            -> b ++ (a 中「self.1 没有该 key」的项)
                          └── 用 self.1.translate(locale, k).is_none() 判定
```

`(Some(b), Some(a))` 分支里，B 的全部消息原样保留，A 的消息只保留那些「B 里查不到该 key」的——也就是**重复 key 以 B 为准**，与 `translate` 的优先级完全一致。

#### 4.3.3 源码精读

**`translate`——`self.1` 优先：**[crates/support/src/backend.rs:41-46](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L41-L46)

```rust
#[inline]
fn translate(&self, locale: &str, key: &str) -> Option<Cow<'_, str>> {
    self.1
        .translate(locale, key)
        .or_else(|| self.0.translate(locale, key))
}
```

先 `self.1.translate(...)`（B，后加入者），命中（`Some`）就直接返回；只有 `None` 时才求值 `self.0.translate(...)`（A）。`#[inline]` 提示这是个会被高频调用的小函数，建议内联以消去 `or_else` 闭包开销。

**`available_locales`——`contains` 显式去重：**[crates/support/src/backend.rs:31-39](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L31-L39)

```rust
fn available_locales(&self) -> Vec<Cow<'_, str>> {
    let mut available_locales = self.0.available_locales();
    for locale in self.1.available_locales() {
        if !available_locales.contains(&locale) {
            available_locales.push(locale);
        }
    }
    available_locales
}
```

第 32 行先把 `self.0`（A）的 locale 列表作为基底；第 33-37 行遍历 `self.1`（B）的 locale，用 `contains` 判重，**只追加尚未出现的**。这正是本讲实践题要验证的「避免重复 locale」机制。复杂度是 \(O(n \cdot m)\)（两层 `Vec` 线性扫描），但 locale 总数通常是个位数，可忽略。

**`messages_for_locale`——按 key 去重，B 胜：**[crates/support/src/backend.rs:48-65](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L48-L65)

```rust
fn messages_for_locale(&self, locale: &str) -> Option<Vec<(Cow<'_, str>, Cow<'_, str>)>> {
    match (
        self.1.messages_for_locale(locale),
        self.0.messages_for_locale(locale),
    ) {
        (None, None) => None,
        (None, a) => a,
        (b, None) => b,
        (Some(b), Some(a)) => Some(
            b.into_iter()
                .chain(
                    a.into_iter()
                        .filter(|(k, _)| self.1.translate(locale, k).is_none()),
                )
                .collect(),
        ),
    }
}
```

前三个分支好理解。重点在 `(Some(b), Some(a))`：

- `b`（来自 `self.1`，B）的消息**原样**放进结果。
- `a`（来自 `self.0`，A）的消息经 `filter` 过滤：只保留满足 `self.1.translate(locale, k).is_none()` 的 key，也就是「B 里查不到的 key」。
- 所以对同一个 key，B 有就用 B 的，A 的那份被丢弃。优先级与 `translate` 完全一致。
- 这里复用了 `self.1.translate(...)` 来判重，而不是再 `messages_for_locale` 一次，避免构造两个 `Vec` 再比对，实现更省。

#### 4.3.4 与 `i18n!(backend = ...)` 的对应关系

这是把本讲连回实际使用场景的关键一环。当用户写：

```rust
i18n!("locales", backend = MyBackend::new());
```

`i18n!` 生成的运行时代码顺序如下（来自 `generate_code`）：

1. 先用本地文件构造一个 `SimpleBackend`：[crates/macro/src/lib.rs:290-296](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L290-L296)
   ```rust
   let mut backend = rust_i18n::SimpleBackend::new();
   #( backend.add_translations(...); )*
   ```
   此时 `backend` 的静态类型是 `SimpleBackend`，对应将来 `CombinedBackend` 里的 `self.0`（A）。

2. 若提供了 `backend = ...`，生成 `extend` 调用：[crates/macro/src/lib.rs:321-327](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L321-L327)
   ```rust
   let backend = backend.extend(#extend);
   ```
   `#extend` 就是用户传入的 `MyBackend::new()`，它落到 `CombinedBackend` 的 `self.1`（B）。

3. 最后装箱：[crates/macro/src/lib.rs:341-347](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L341-L347)
   ```rust
   static _RUST_I18N_BACKEND: std::sync::LazyLock<Box<dyn rust_i18n::Backend>> =
       std::sync::LazyLock::new(|| {
           // ... all_translations ...
           // ... extend_code ...
           Box::new(backend)
       });
   ```

把第 1、2 步连起来看：**本地文件是 `self.0`，用户后端是 `self.1`**；又因为 `translate` 里 `self.1` 优先（4.3.3），所以——

> **结论：`i18n!(backend = ...)` 里传入的自定义后端，其翻译优先于本地文件里的同名键。** 这正是 [u4-l3](./u4-l3-custom-backend.md)「自定义后端优先于本地翻译」的根因，根就在本讲的 `CombinedBackend::translate`。

同时也能验证 4.1.4 的论断：`extend`（第 2 步）发生在 `Box::new`（第 3 步）**之前**，此时 `backend` 仍是具体类型，满足 `Self: Sized`；如果顺序反过来，`extend` 就无法编译。

#### 4.3.5 代码实践

**实践目标**：亲手用两个 `SimpleBackend` 调 `extend`，验证「后加入者（`self.1`）translate 结果优先」，并观察 `available_locales` 的去重行为。

**操作步骤**：

1. 在 `crates/support` 下找到现成的单元测试 [`test_combined_backend`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L187-L217)（位于 `backend.rs` 文件末尾的 `#[cfg(test)] mod tests`）。它的核心片段是：
   ```rust
   let mut backend = SimpleBackend::new();
   // ... 灌入 en: hello=Hello, zh-CN: hello=你好 ...

   let mut backend2 = SimpleBackend::new();
   // ... 灌入 en: hello=Hello2, zh-CN: hello=你好2 ...

   let combined = backend.extend(backend2);
   assert_eq!(combined.translate("en", "hello"), Some(Cow::from("Hello2")));
   assert_eq!(combined.translate("zh-CN", "hello"), Some(Cow::from("你好2")));
   assert_eq!(combined.available_locales(), vec!["en", "zh-CN"]);
   ```
2. 直接运行这条测试：
   ```bash
   cargo test -p rust-i18n-support test_combined_backend
   ```
3. 在仓库里（或在本地复刻一份）追加一个你自己的测试，故意让两个后端**都含 `en` 和 `zh-CN`**，并在 `backend2` 里只覆盖部分 key，例如：
   ```rust
   // backend（self.0）：en 有 hello、foo
   // backend2（self.1）：en 只有 hello（Hello2），没有 foo
   let combined = backend.extend(backend2);
   // 预期：hello 走 backend2（Hello2），foo 走 backend（Foo bar）
   ```

**需要观察的现象**：

- `combined.translate("en", "hello")` 返回 `"Hello2"`（来自 `backend2`，即 `self.1`），证明后加入者优先。
- 上一步新加的 `foo` 键，`combined.translate("en", "foo")` 应返回 `backend` 里的值，证明 `self.1` miss 后会回退到 `self.0`。
- `combined.available_locales()` 仍是 `["en", "zh-CN"]`，**没有**变成 `["en", "zh-CN", "en", "zh-CN"]`，证明 `contains` 去重生效。

**预期结果**：测试通过；`translate` 体现 `self.1 > self.0` 的优先级；`available_locales` 体现并集去重。

> 待本地验证：如果你没有本机 Rust 工具链，可以只完成「阅读源码 + 手动推演」部分——上述断言完全由 4.3.3 的源码逻辑保证，无需运行也能得出确定结论。

#### 4.3.6 小练习与答案

**练习 1**：如果把 `backend.extend(backend2)` 改成 `backend2.extend(backend)`，`combined.translate("en", "hello")` 会变成什么？为什么？

> **答案**：会变成 `"Hello"`（来自原来的 `backend`）。因为调换顺序后，原来的 `backend` 成了 `self.1`（优先），`backend2` 成了 `self.0`（兜底）。这说明 `extend` 的书写顺序直接决定优先级，并非「先创建者优先」。

**练习 2**：`available_locales` 的起点是 `self.0.available_locales()`（A 在前），而 `translate` 是 `self.1` 优先（B 在前）。这两个方向相反，是否矛盾？

> **答案**：不矛盾。`translate` 要的是「按 key 取一个值」，必须明确谁优先，所以让 `self.1` 先查；`available_locales` 要的是「locale 名单的并集」，谁先放进去都一样，关键在 `contains` 去重，起点选 `self.0` 只是实现选择，不影响最终集合。两者的「目标不同」导致「实现方向不同」，并非语义冲突。

**练习 3**：为什么不能写 `let b: Box<dyn Backend> = ...; b.extend(another);`？

> **答案**：`extend` 带 `where Self: Sized` 约束，而 `dyn Backend` 是 unsized（DST），不满足 `Sized`，因此编译期就会被拒绝。组合必须在「装箱」之前、类型还是具体类型时完成（参见 4.1.4 与 4.3.4 的 `generate_code` 顺序）。

## 5. 综合实践

把本讲的三个最小模块串起来，完成一个「双源翻译合并」的小任务：

**场景**：模拟一个应用，其本地文件提供「基础翻译」，运行时从某个配置（这里用第二个 `SimpleBackend` 代替）提供「覆盖翻译」。你要：

1. 构造 `base`（`SimpleBackend`）：`en` 下有 `greeting = "Hello"`、`app.title = "App"`。
2. 构造 `override_be`（`SimpleBackend`）：`en` 下只有 `greeting = "Hi (overridden)"`，外加一个 `en` 没有的 `app.banner = "Sale"`（注意：`override_be` 也要 `add_translations("en", ...)` 才会和 `base` 同属一个 locale）。
3. 用 `let combined = base.extend(override_be);` 组合。
4. 编写断言验证：
   - `combined.translate("en", "greeting")` == `"Hi (overridden)"`（覆盖生效，`self.1` 胜）。
   - `combined.translate("en", "app.title")` == `"App"`（`self.1` 没有，回退 `self.0`）。
   - `combined.translate("en", "app.banner")` == `"Sale"`（`self.0` 没有，由 `self.1` 提供）。
   - `combined.available_locales()` == `["en"]`（两边都只有 `en`，去重后仍是一个）。
5. 用一句话总结：`combined` 在行为上等价于「`override_be` 优先、`base` 兜底」的单个后端——而这正是 `i18n!(backend = ...)` 让自定义后端覆盖本地文件的内部机制。

> 提示：可参考仓库自带的 [`test_combined_backend`](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L187-L217) 作为脚手架；它已经覆盖了「同名键 `self.1` 胜」与「`available_locales` 去重」两点，你只需补上「`self.1` miss 回退 `self.0`」和「`self.0` miss 取 `self.1`」两个方向的断言。

## 6. 本讲小结

- `BackendExt` 是 `Backend` 的子 trait，只多一个 `extend` 方法；任何 `Backend` 实现加一行空 `impl BackendExt for X {}`（如 `SimpleBackend` 在 [backend.rs:147](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/backend.rs#L147)）即可免费获得组合能力。
- `extend(self, other)` 按值消费两个后端，返回元组结构体 `CombinedBackend<A, B>`，其中 `A=self.0`（被扩展者）、`B=self.1`（后加入者）；`Self: Sized` 约束排除了 `dyn Backend`，故组合必须发生在「装箱」之前。
- `CombinedBackend::translate` 用 `self.1.translate(...).or_else(|| self.0.translate(...))` 实现 **`self.1`（B）优先、`self.0`（A）兜底**。
- `available_locales` 以 `self.0` 为基底、用 `contains` 对 `self.1` 的 locale **显式去重**，保证并集无重复。
- `messages_for_locale` 在双 `Some` 时保留 `self.1` 全部消息，并用 `self.1.translate(...).is_none()` 过滤 `self.0` 的重复键，优先级与 `translate` 一致。
- `i18n!("locales", backend = ...)` 把本地文件的 `SimpleBackend` 作 `self.0`、用户后端作 `self.1`，因此**自定义后端的同名键覆盖本地文件**——这一行为的根就在本讲的 `CombinedBackend::translate`。

## 7. 下一步学习建议

- 下一讲 [u4-l3 自定义后端实战](./u4-l3-custom-backend.md) 会把本讲的 `extend` 用到真实场景：实现一个自己的 `Backend`（比如从内存 `HashMap` 或远程 API 读取），通过 `i18n!(backend = ...)` 接入，并验证自定义值优先于本地文件。建议先确保你能默写本讲 4.3.4 的「`self.0`=本地、`self.1`=自定义」对应关系。
- 如果想再巩固「组合」本身，可以阅读 `examples/share-in-workspace` 里用 `CombinedBackend` 思想把全局 `_RUST_I18N_BACKEND` 包一层兜底后端的写法（u8-l3 会详讲）。
- 想深入 `SimpleBackend` 的存储细节，可回到 [u4-l1](./u4-l1-backend-trait-simplebackend.md) 复习 `add_translations` 的 `entry().or_default()` 合并语义。
