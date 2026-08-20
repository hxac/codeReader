# SharedString 快速上手：构造、读取与克隆

## 1. 本讲目标

上一讲（u1-l1）我们从整体上认识了 `gpui_shared_string` 这个 crate：它只有 212 行源码，为 GPUI 提供一个可廉价克隆的不可变字符串类型 `SharedString`。本讲开始动手，学完后你应该能够：

1. 用三种方式构造一个 `SharedString`：`new_static`（编译期常量）、`new`（运行时借用）、`.into()`（`From` 转换惯用法），并说清楚各自的适用场景。
2. 用两种方式读取它的内容：显式调用 `as_str()`，以及依赖 `Deref` 自动解引用直接调用 `str` 的方法。
3. 解释为什么克隆一个 `SharedString` 是便宜的操作，而克隆 `String` 通常要复制全部字节。
4. 在 zed 的真实代码（如 `hello_world` 示例）中一眼认出这些惯用法。

## 2. 前置知识

本讲只需要少量 Rust 基础概念，先用两三句话把它们讲清楚：

- **`String` 与 `&str` 的区别**：`String` 是拥有所有权的、可增长的堆分配字符串；`&str` 是对字符串数据的只读「借用」，本身不拥有数据。`"hello"` 这样的字面量类型是 `&'static str`，其中的 `'static` 表示它在整个程序运行期间都有效。
- **`Arc<T>` 是什么**：Atomic Reference Counted，带原子引用计数的共享所有权智能指针。`Arc<T>::clone()` 只是让计数加一，不复制内部数据，代价近似 O(1)。上一讲说过，`SharedString` 在概念上就是 `Arc<str>` 与 `&'static str` 的统一抽象。
- **`const fn` 是什么**：能在编译期求值的函数。只有 `const fn` 才能出现在 `const ITEM: T = ...` 这种常量定义的等号右边——这一点直接决定了 `new_static` 与 `new` 的分工。
- **`Deref` 与 `From` 两个 trait**：`Deref` 定义「类型可以被自动解引用成什么目标类型」，编译器会在方法调用时自动插入解引用；`From<A> for B` 定义「从 A 到 B 的转换」，配合 `.into()` 可以在目标类型已知的上下文里省去写具体构造函数名。
- **克隆成本**：`String` 的 `clone` 要把字符串逐字节复制一遍，成本 O(n)；`SharedString` 的 `clone`（由 `#[derive(Clone)]` 透传给内部的 `SmolStr`）要么复制固定大小的内联缓冲，要么给 `Arc` 计数加一，都是近似 O(1)。本讲只需建立这个直觉，内部机制留到下一讲（u2-l1）精读。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [crates/gpui_shared_string/gpui_shared_string.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L1-L212) | 本 crate 唯一源码文件 | `new_static`/`new`/`as_str`、`Deref`、`From<&str>`、`From<String>`、`Display`、`Default` |
| [crates/gpui/examples/hello_world.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/hello_world.rs#L1-L122) | gpui 的最小 GUI 示例 | `HelloWorld` 结构体的 `text: SharedString` 字段、`"World".into()` 惯用法 |
| [crates/gpui/src/styled.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/styled.rs#L13-L13) | gpui 的样式 trait 定义 | 第 13 行的 `const ELLIPSIS`：`new_static` 的教科书用法 |
| [crates/gpui/src/window.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L6714-L6718) | gpui 窗口与元素 ID 定义 | `From<&'static str> for ElementId`：`new_static` 在框架层的批量使用 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**构造**（怎么创建）、**读取**（怎么用）、**真实用法**（zed 代码里长什么样）。

### 4.1 构造 API：`new_static` / `new` / `From` 三条路径

#### 4.1.1 概念说明

`SharedString` 是**不可变**字符串——创建之后没有任何修改内容的方法（你可以提前做一个实验：在整个 212 行源码里找不到 `push_str`、`insert` 之类的接口）。所以「构造」就是拿到 `SharedString` 的唯一入口，crate 为此提供了三条路径，分别对应三种典型场景：

| 路径 | 函数 | 能否用于 `const` 常量 | 输入的所有权 | 典型场景 |
| --- | --- | --- | --- | --- |
| 静态构造 | `SharedString::new_static(&'static str)` | ✅（它是 `const fn`） | 借用一个活满全程的字面量 | 常量、框架内置文案 |
| 运行时构造 | `SharedString::new(impl AsRef<str>)` | ❌ | 借用任意字符串 | 从运行时数据（如 `&String`）构造 |
| 转换构造 | `"..."` / `String` 等 `.into()` | ❌ | 可能转移所有权 | 结构体字段初始化等目标类型已知的场合 |

为什么需要区分？关键在 `const fn`：Rust 常量定义（`const ITEM: T = ...`）要求等号右边的表达式能在**编译期**求值，而泛型函数 `new` 接受 `impl AsRef<str>`，只能在运行时调用。于是 `new_static` 作为一个 `const fn` 单独存在，专供编译期使用；它要求参数是 `&'static str`，因为编译期常量的引用必须保证在整个程序生命周期内有效。

#### 4.1.2 核心流程

三条路径的执行过程可以用下面的伪代码概括：

```text
new_static("…")        编译期求值：
                          把 &'static str 包装成 SmolStr 的静态形态
                          → 存入常量，程序运行期间零额外开销

new(&runtime_string)   运行时求值：
                          &String → (AsRef<str>) → &str → SmolStr::new
                          → 短字符串内联存储，长字符串 Arc 堆分配（细节见 u2-l1）

"lit".into()           运行时求值，目标类型由上下文推断：
                          上下文需要 SharedString
                          → 编译器选中 impl From<&str> for SharedString
                          → 等价于 SharedString::from("lit")
```

注意 `.into()` 的方向：`From<&str> for SharedString` 意味着 `&str` 可以 `into()` 成 `SharedString`。目标类型不需要写在 `into()` 旁边，而是由**使用位置的类型注解**（变量标注、函数参数、结构体字段类型）决定——这正是 4.3 节 `text: "World".into()` 能工作的原因。

#### 4.1.3 源码精读

先看固有方法（inherent method）块，三条路径中的两条都在这里：

[crates/gpui_shared_string/gpui_shared_string.rs:L25-L40](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L25-L40)

```rust
impl SharedString {
    /// Creates a static [`SharedString`] from a `&'static str`.
    pub const fn new_static(str: &'static str) -> Self {
        Self(SmolStr::new_static(str))
    }

    /// Creates a [`SharedString`].
    pub fn new(str: impl AsRef<str>) -> Self {
        SharedString(SmolStr::new(str))
    }

    /// Get a &str from the underlying string.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}
```

这段代码做了三件事：

1. `new_static` 带有 `const` 修饰符（`pub const fn`），参数限定 `&'static str`，内部把工作转交给 `SmolStr::new_static`——后者同样是 `const fn`，这是整条链能在编译期跑通的前提。
2. `new` 是泛型函数，接受任何满足 `AsRef<str>` 的类型。`&String`、`&str`、`String` 的引用都能直接传入。
3. `as_str` 返回对内部数据的借用 `&str`，不发生拷贝（读取 API，详见 4.2）。

再看第三条路径中最常用的两个 `From` 实现：

[crates/gpui_shared_string/gpui_shared_string.rs:L117-L122](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L117-L122)

```rust
impl From<&str> for SharedString {
    #[inline]
    fn from(s: &str) -> SharedString {
        SharedString(SmolStr::from(s))
    }
}
```

这是 `text: "World".into()` 背后实际被选中的实现：`From<&str>` 让任何字符串字面量都能一键转换成 `SharedString`。

[crates/gpui_shared_string/gpui_shared_string.rs:L145-L150](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L145-L150)

```rust
impl From<String> for SharedString {
    #[inline(always)]
    fn from(text: String) -> Self {
        SharedString(SmolStr::from(text))
    }
}
```

`From<String>` 接收**按值**的 `String`，也就是把所有权交进来，避免多余的借用生命周期标注。完整的转换矩阵（`Box<str>`、`Arc<str>`、`Cow`、`char` 等 13 个方向）留到 u2-l3 逐一精读，本讲先掌握 `&str` 与 `String` 这两条最高频路径。

最后看两个「边界上的设计」，它们能帮你确认自己真的理解了 `const fn` 的约束：

[crates/gpui_shared_string/gpui_shared_string.rs:L56-L60](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L56-L60)

```rust
impl Default for SharedString {
    fn default() -> Self {
        Self::new_static("")
    }
}
```

`Default`（默认值）直接复用 `new_static("")`——空字符串连堆分配都不需要，且这里刻意选了静态构造路径。

框架侧的真实用法首推 gpui 样式模块里的省略号常量：

[crates/gpui/src/styled.rs:L13-L13](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/styled.rs#L13-L13)

```rust
const ELLIPSIS: SharedString = SharedString::new_static("…");
```

这一行同时用到了本讲的两个知识点：常量定义要求编译期求值（所以必须用 `new_static` 而不能是 `new`），而文本溢出时显示的省略号是固定不变的内置文案（所以适合静态构造）。

元素 ID 的转换也在批量使用 `new_static`：

[crates/gpui/src/window.rs:L6714-L6718](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L6714-L6718)

```rust
impl From<&'static str> for ElementId {
    fn from(name: &'static str) -> Self {
        ElementId::Name(SharedString::new_static(name))
    }
}
```

`ElementId` 是 gpui 给 UI 元素编号的类型，名称部分用 `SharedString` 承载；由于传入的 `name` 本身已是 `&'static str`，这里自然选择零开销的静态构造路径（同文件 L6726-L6758 还有四个针对 `(&'static str, 整数)` 组合的类似实现，模式完全一致）。

#### 4.1.4 代码实践

**实践目标**：在 zed 仓库中统计 `new_static` 的真实使用方式，验证「常量定义与 `'static` 输入」这条规律。

**操作步骤**（源码阅读型实践，不需要编译）：

1. 在 zed 仓库根目录执行 `rg 'SharedString::new_static' crates/gpui/src -n`。
2. 把命中结果分成两类：A 类出现在 `const XXX: SharedString = ...` 的等号右侧（如 `styled.rs:13`）；B 类出现在普通函数体内（如 `window.rs:6716`、`text_system.rs:1202` 的字体名映射）。
3. 检查 B 类每一处的输入参数类型，确认它们都声明为 `&'static str`（`window.rs` 的 `From<&'static str>` 就是证据）。

**需要观察的现象**：没有任何一处 `new_static` 的输入来自运行时生成的 `String` 或 `&str`（不带 `'static` 的借用）。

**预期结果**：所有命中要么在常量定义里，要么函数签名已保证输入 `'static`。如果你找到一处输入是运行时字符串还用了 `new_static`，那它应该根本无法通过编译——这反过来验证了类型系统的约束。

**待本地验证**：本实践只需 `rg`（ripgrep）与阅读，无需运行程序。

#### 4.1.5 小练习与答案

**练习 1**：把 `styled.rs:13` 改写成 `const ELLIPSIS: SharedString = SharedString::new("…");` 会发生什么？为什么？

> **答案**：无法通过编译。`new` 不是 `const fn`（它接受 `impl AsRef<str>` 泛型参数，只能在运行时调用），而常量定义要求等号右侧在编译期求值。只有 `new_static` 这样的 `const fn` 才能出现在 `const` 上下文中。

**练习 2**：`SharedString::new(&runtime_string)` 中，`runtime_string` 是 `String`，传入的是 `&String`。为什么 `&String` 能满足 `impl AsRef<str>`？

> **答案**：标准库为 `String` 实现了 `AsRef<str>`，并且为引用提供了转发实现（`T: AsRef<U>` 蕴含 `&T: AsRef<U>`），所以 `&String: AsRef<str>` 成立。这也是 `new` 用 `impl AsRef<str>` 而不是写死 `&str` 的好处：调用侧不必先解引用。

**练习 3**：`SharedString::default()` 返回什么？它是怎么构造出来的？

> **答案**：返回内容为空字符串的 `SharedString`，实现是 `Self::new_static("")`，走编译期静态路径，零堆分配。

### 4.2 读取 API：`as_str()` 与 `Deref` 自动解引用

#### 4.2.1 概念说明

既然 `SharedString` 不可变，使用它的过程就是不断「读取」。crate 提供两种读取姿势：

1. **显式借用**：调用 `as_str()` 拿到一个 `&str`。适合需要把字符串传给其他接受 `&str` 的函数、或希望类型一目了然的场合。
2. **自动解引用**：`SharedString` 实现了 `Deref<Target = str>`，于是 `str` 的全部方法（`len`、`split_whitespace`、`starts_with`、`as_bytes`……）可以直接在 `SharedString` 值上调用，编译器自动插入解引用。这让 `SharedString` 用起来几乎和 `str` 一模一样，学习成本趋近于零。

两者背后是同份数据的两种视图，都只是借用，都没有拷贝。

#### 4.2.2 核心流程

```text
显式：   s.as_str()  ──────────────►  &str   （借用内部数据）
自动：   s.len()  ──编译器改写为──►  Deref::deref(&s).len()
                            │
                            ▼
                    &str 再调用 str::len
```

打印输出则是第三条路：`SharedString` 实现了 `Display`，所以 `println!("{}", s)` 与 `format!("{}", s)` 直接可用，无需任何转换。

#### 4.2.3 源码精读

[crates/gpui_shared_string/gpui_shared_string.rs:L17-L23](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L17-L23)

```rust
impl std::ops::Deref for SharedString {
    type Target = str;

    fn deref(&self) -> &Self::Target {
        self.0.as_str()
    }
}
```

这段代码声明了「`SharedString` 可以被解引用成 `str`」，实现只有一行：把内部的 `SmolStr` 转成 `&str` 返回。有了它，`s.len()`、`s.split_whitespace()`、`s.chars()` 都自动可用。

[crates/gpui_shared_string/gpui_shared_string.rs:L36-L39](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L36-L39)

```rust
    /// Get a &str from the underlying string.
    pub fn as_str(&self) -> &str {
        &self.0
    }
```

`as_str` 返回的 `&str` 借用自 `self`：只要 `SharedString` 本体还活着，这个 `&str` 就有效；反过来，`&str` 存在期间也不能以别的方式移动或销毁本体（普通借用规则，没有特别之处）。

顺带看 `Display`，它是 4.3 节 `format!` 能直接格式化的原因：

[crates/gpui_shared_string/gpui_shared_string.rs:L80-L84](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L80-L84)

```rust
impl std::fmt::Display for SharedString {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0.as_str())
    }
}
```

`Display` 的实现同样只是一次转发：把内部字符串按原样写进格式化输出。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `Deref` 自动解引用生效、且 `SharedString` 确实没有修改内容的方法。

**操作步骤**（可追加到第 5 节综合实践的同一项目中；单独验证也可用任何引入了本 crate 的工程）：

1. 构造 `let s = SharedString::new("immutable");`。
2. **不写** `.as_str()`，直接调用 `assert_eq!(s.len(), 9);` 与 `assert!(s.starts_with("immut"));`。
3. 再补一行被注释掉的代码 `// s.push_str("!");`，取消注释后执行 `cargo build`。

**需要观察的现象**：步骤 2 的两行直接编译通过（自动解引用生效）；步骤 3 报编译错误，错误信息形如 `no method named 'push_str' found for struct 'SharedString'`。

**预期结果**：`str` 的只读方法全部可用，而 `push_str` 这类可变方法不存在——因为 `Deref` 的目标是 `str`（本身就是不可变切片），`SharedString` 也没有实现 `DerefMut`。「不可变」不是口头约定，而是类型系统保证。

**待本地验证**：具体错误措辞随编译器版本略有差异，请以本地 `cargo build` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`as_str(&self) -> &str` 返回的 `&str`，它的生命周期绑定在谁身上？

> **答案**：绑定在 `&self` 上（省略生命周期形式即 `fn as_str<'a>(&'a self) -> &'a str`）。`SharedString` 本体存活期间 `&str` 一直有效；这也是为什么把 `as_str()` 的结果存成长期变量时，本体会一直被借用。

**练习 2**：`s.len()` 为什么能编译通过？请写出编译器概念上的改写过程。

> **答案**：方法查找先在 `SharedString` 的固有方法里找 `len`，找不到，于是沿着 `Deref` 链找——`Deref::deref(&s)` 得到 `&str`，`str` 有 `len`，改写为 `Deref::deref(&s).len()`。这称为自动解引用（auto-deref）。

**练习 3**：`println!("{}", s)` 与 `println!("{:?}", s)` 分别走哪个 trait？

> **答案**：`{}` 走 `Display`（L80-L84），`{:?}` 走 `Debug`（L74-L78，同样是转发到内部 `SmolStr`）。两者本 crate 都实现了。

### 4.3 真实用法：`hello_world` 示例与 `.into()` 惯用法

#### 4.3.1 概念说明

掌握了构造与读取，我们看 zed 仓库里真实的使用方式。gpui 的 `hello_world` 是最小可运行的 GUI 示例：一个窗口里显示 `Hello, World!` 和一排彩色方块。它的状态结构体只有**一个**字段，而这个字段的类型正是 `SharedString`——这不是巧合，而是「视图状态中的文本载荷用 `SharedString` 承载」这一惯例的最小体现。

为什么 UI 状态字段选 `SharedString` 而不是 `String`？回顾上一讲的定位：GPUI 的实体（entity）状态会在渲染、元素树构建、异步任务之间被框架反复读取和克隆。`String` 每次克隆都 O(n)，而 `SharedString` 近似 O(1)，高频路径上差距会被放大。文本一旦确定也不需要原地修改——改文案通常是整体替换字段值，这正好匹配不可变语义。

#### 4.3.2 核心流程

`hello_world` 中一个 `SharedString` 的完整生命周期：

```text
初始化          渲染（每帧）                     输出
────────────    ────────────────────────────    ────────────────
"World"         render() 被调用                  窗口里出现
   │            format!("Hello, {}!", self.text) "Hello, World!"
   ▼                │
From<&str>          ▼ Display
   │            self.text 被借用（不克隆、不移动）
   ▼
text: SharedString
（字段类型注解决定 .into() 的目标）
```

#### 4.3.3 源码精读

第一步，`use` 里从 `gpui` 导入 `SharedString`（上一讲说过，gpui 对本 crate 做了整体再导出，所以应用代码统一走 `gpui::SharedString`）：

[crates/gpui/examples/hello_world.rs:L3-L6](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/hello_world.rs#L3-L6)

```rust
use gpui::{
    App, Bounds, Context, SharedString, Window, WindowBounds, WindowOptions, div, prelude::*, px,
    rgb, size,
};
```

第二步，状态结构体的字段类型：

[crates/gpui/examples/hello_world.rs:L9-L11](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/hello_world.rs#L9-L11)

```rust
struct HelloWorld {
    text: SharedString,
}
```

第三步，构造实体时的 `.into()` 惯用法——本讲最重要的三行：

[crates/gpui/examples/hello_world.rs:L100-L104](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/hello_world.rs#L100-L104)

```rust
                cx.new(|_| HelloWorld {
                    text: "World".into(),
                })
```

`"World"` 的类型是 `&'static str`，`.into()` 本身不指明目标类型，目标由**字段类型** `text: SharedString` 锚定，编译器据此选中 4.1.3 精读过的 `impl From<&str> for SharedString`。这就是 zed 代码库里最常见的 `SharedString` 构造写法：不写 `SharedString::from("World")` 也不写 `SharedString::new("World")`，而是让类型推断完成收尾。

第四步，渲染时直接把字段塞进 `format!`：

[crates/gpui/examples/hello_world.rs:L28-L28](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/hello_world.rs#L28-L28)

```rust
            .child(format!("Hello, {}!", self.text))
```

这里没有 `.as_str()` 也没有 `.to_string()`——`format!` 的 `{}` 占位符调用的是 4.2.3 精读过的 `Display` 实现，直接借用字段输出文本。整个渲染路径上，字符串数据一次都没有被复制。

#### 4.3.4 代码实践

**实践目标**：运行 `hello_world` 示例，并把 `text` 字段从静态字面量换成运行时构造的字符串，观察渲染内容随之变化。

**操作步骤**：

1. 在本地克隆的 zed 仓库根目录执行 `cargo run -p gpui --example hello_world`。
2. 确认窗口显示 `Hello, World!` 与一排彩色方块。
3. 把 `hello_world.rs` 第 102 行的 `text: "World".into()` 临时改为（示例代码）：
   ```rust
   text: format!("Zed {}", 2026).into(),
   ```
4. 重新运行同一条命令。

**需要观察的现象**：窗口文案变为 `Hello, Zed 2026!`；同时注意 `format!` 产出的是**运行时** `String`，走的是 `From<String>` 而非 `From<&str>`，但 `.into()` 的写法完全一致——两条转换路径在调用侧看不出差别。

**预期结果**：文案更新，其余 UI 不变。验证后请把改动还原（`git checkout -- crates/gpui/examples/hello_world.rs`），保持工作区干净。

**待本地验证**：本实践需要本地图形环境（Linux 上依赖窗口系统与 GPU 渲染，CI/无头环境无法显示窗口）；编译本身可通过 `cargo build -p gpui --example hello_world` 验证。若运行报缺平台依赖，可只完成编译验证并阅读代码。

#### 4.3.5 小练习与答案

**练习 1**：`text: "World".into()` 中，`.into()` 要转换成什么类型是由谁决定的？如果删掉结构体定义里的类型注解还能这么写吗？

> **答案**：由使用位置的类型锚定——这里是结构体字面量的字段类型 `SharedString`。`Into::into` 的目标类型永远由上下文推断；如果上下文没有类型信息（例如 `let x = "World".into();` 且无标注），编译器会报「type annotations needed」错误。

**练习 2**：把 `text` 字段类型改成 `String`，示例还能编译通过吗？改动前后丢失了什么性质？

> **答案**：仍能编译（`format!` 对 `String` 也有 `Display`），但字段克隆从近似 O(1) 变成 O(n) 逐字节复制。在本示例中差异无感，但在框架高频克隆文本的路径上（元素树、状态分发），`SharedString` 的廉价克隆正是它存在的理由。

**练习 3**：`format!("Hello, {}!", self.text)` 里 `self.text` 是 `SharedString`，为什么不需要先转成 `&str`？

> **答案**：`format!` 的 `{}` 占位符对实现了 `Display` 的类型直接可用，`SharedString` 实现了 `Display`（内部转发到字符串本体），因此无需任何显式转换。

## 5. 综合实践

把本讲三条构造路径、两种读取方式与克隆体验串成一个独立的小项目。

**实践目标**：新建一个独立的 cargo 项目，以 path 依赖引入 zed 仓库的 `gpui_shared_string`，用三种方式各构造一个 `SharedString`，验证读取与克隆。

**操作步骤**：

1. 在 zed 仓库之外新建目录 `shared-string-lab`，写入如下 `Cargo.toml`（示例代码；`path` 按你本地 zed 仓库的实际位置调整）：

   ```toml
   [package]
   name = "shared-string-lab"
   version = "0.1.0"
   edition = "2021"

   [dependencies]
   gpui_shared_string = { path = "../zed/crates/gpui_shared_string" }
   ```

2. 写入 `src/main.rs`（示例代码）：

   ```rust
   use gpui_shared_string::SharedString;

   // 路径一：new_static 是 const fn，因此可以定义常量（运行时构造的 new 做不到这一点）
   const GREETING: SharedString = SharedString::new_static("hello");

   fn main() {
       // 路径二：new 接受 impl AsRef<str>，这里传入 &String
       let runtime_string = String::from("world");
       let from_new = SharedString::new(&runtime_string);

       // 路径三：From<&str> + .into()，目标类型由 let 标注锚定
       let from_into: SharedString = "lit".into();

       // 读取方式一：显式 as_str()
       println!("greeting as_str = {}", GREETING.as_str());
       println!("from_new as_str = {}", from_new.as_str());
       println!("from_into as_str = {}", from_into.as_str());

       // 读取方式二：Deref 自动解引用，直接调用 str 的方法
       println!("greeting len = {}", GREETING.len());
       println!("words = {:?}", GREETING.split_whitespace().collect::<Vec<&str>>());

       // 克隆：不深拷贝，原值依旧可用
       let cloned = GREETING.clone();
       println!("cloned = {}", cloned);
       assert_eq!(cloned, GREETING);

       // 再体会一条 From 路径：String 按值转移所有权进来
       let from_string = SharedString::from(runtime_string);
       println!("from_string as_str = {}", from_string.as_str());
   }
   ```

3. 运行 `cargo run`（首次会编译 smol_str、serde、schemars 三个传递依赖）。

**需要观察的现象**：

- 程序正常打印四组 `as_str` 结果、`len` 与 `words`；
- `cloned` 与 `GREETING` 内容一致，且克隆之后 `GREETING` 仍能继续使用（第 20 行还在用它）；
- 全程没有任何「复制了字符串内容」的报错或警告——克隆就是合法且廉价的常规操作。

**预期结果**（大致输出，待本地验证）：

```text
greeting as_str = hello
from_new as_str = world
from_into as_str = lit
greeting len = 5
words = ["hello"]
cloned = hello
from_string as_str = world
```

**注意事项与兜底方案**（待本地验证）：`gpui_shared_string` 自身的依赖通过 zed workspace 的 `.workspace = true` 继承版本，path 依赖引入时 Cargo 会从其物理位置向上解析 zed 仓库的 workspace 定义，通常可直接工作；若你的本地环境报 workspace 解析错误，可改在 zed 仓库内验证等价代码——例如给第 5 节代码新建一个 `examples/` 目录外的临时 bin 不被允许时，直接以 4.3.4 的 `hello_world` 改造实践作为替代。

## 6. 本讲小结

- `SharedString` 的构造有三条路径：`new_static` 是 `const fn`、专供编译期常量（如 `styled.rs` 的 `ELLIPSIS`）；`new` 接受 `impl AsRef<str>`、适合运行时借用构造；`From<&str>`/`From<String>` 配合 `.into()` 是 zed 代码里最常见的惯用法，目标类型由上下文锚定。
- 读取有两种姿势：显式 `as_str()` 拿 `&str` 借用，或依赖 `Deref<Target = str>` 直接调用 `str` 的全部只读方法；打印输出则由 `Display`/`Debug` 转发实现兜底。
- `Default` 实现为 `new_static("")`，空串走静态路径，零分配。
- 不可变性由类型系统保证：没有 `DerefMut`、没有修改方法，`s.push_str(...)` 直接编译失败。
- 克隆是廉价操作（内联按位复制或引用计数加一），克隆后原值依旧可用；这也是 `hello_world` 等视图状态用 `SharedString` 而非 `String` 承载文本的原因。

## 7. 下一步学习建议

下一讲（u2-l1《newtype 封装与 SmolStr 内部机制》）将打开本讲一直黑盒对待的内部：`pub struct SharedString(SmolStr)` 这层 newtype 封装到底带来什么好处，`SmolStr` 的「短字符串内联、长字符串 `Arc` 堆分配」双模式存储如何让两种长度的克隆都保持 O(1)。在那之前，建议你先自己带着两个问题重读一遍 [gpui_shared_string.rs 的 L11-L40](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L11-L40)：`#[derive(...)]` 列表里的 `Clone` 是转发给谁的？如果哪天要换掉 `SmolStr`，哪些代码完全不用动？这两个问题正是 newtype 模式的价值所在。
