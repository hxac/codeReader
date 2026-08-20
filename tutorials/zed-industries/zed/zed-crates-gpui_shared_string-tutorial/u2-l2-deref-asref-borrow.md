# Deref、AsRef、Borrow：像 str 一样使用

## 1. 本讲目标

上一讲我们拆开了 `SharedString` 的外壳（newtype 封装 + `SmolStr` 双模式存储），知道了它的克隆为什么便宜。但它还有一个同样重要的侧面：**它用起来几乎和 `&str` / `String` 一样顺手**。这不是巧合，而是三个精心实现的 trait 换来的：

- `Deref<Target = str>`：让 `shared.len()`、`shared.split_whitespace()` 直接可用，不必写 `.as_str()`。
- `AsRef<str>`：让 `SharedString` 能传入任何接受 `impl AsRef<str>` 的泛型 API。
- `Borrow<str>`：让 `HashMap<SharedString, V>` 能直接用 `&str` 查键。

学完本讲，你应该能：

1. 说出这三个 trait 各自解决什么问题、编译器/标准库分别在哪个环节使用它们。
2. 逐行读懂这三个 impl 在 `gpui_shared_string.rs` 中的实现，并解释它们为什么都只是「把内部 `SmolStr` 以 `&str` 视角交出去」。
3. 理解 `Borrow` 的隐含契约（`Eq` / `Ord` / `Hash` 与被借类型一致）为什么是 `HashMap` 跨类型查键的前提。
4. 在 zed 真实代码中辨认出「用 `&str` 在 `HashMap<SharedString, _>` 上查键」的惯用法。

## 2. 前置知识

本讲会用到四个 Rust 基础概念。已熟悉的读者可以快速扫过，但第 4 条（`HashMap::get` 的签名）是本讲后半部分的钥匙，建议至少精读它。

### 2.1 trait 与手写 impl 块

前几讲我们见到的多是 `#[derive(Eq, PartialEq, ...)]` 这种「编译器替你生成实现」的写法。本讲的主角是**手写的 `impl Trait for Type` 块**，例如：

```rust
impl AsRef<str> for SharedString {
    fn as_ref(&self) -> &str {
        &self.0
    }
}
```

它告诉编译器：`SharedString` 实现了 `AsRef<str>` 这个「能力」。之后任何要求这个能力的泛型代码，都能接受 `SharedString`。

### 2.2 `str`、`&str` 与胖指针

`str` 是「一段 UTF-8 字节序列」的类型，但它在编译期**大小未知**（DST，动态大小类型），所以几乎不能直接按值使用；我们平时操作的都是 `&str`——一个「指针 + 长度」的胖指针。`String` 则是一个持有堆缓冲区、可增长的字符串。本讲所有 impl 的返回值都是 `&str`：它们不复制任何字节，只是把 `SharedString` 内部的数据以「只读借用」的视角交出来。

### 2.3 方法解析与自动解引用

当你写 `value.method()` 时，编译器并不是只在该类型自己的方法里找，而是沿一条「解引用链」逐层查找：先在 `SharedString` 的固有方法和它实现的 trait 方法里找，找不到，就对其解引用得到 `str`，再找一遍。这条链由 `Deref` trait 描述——这就是为什么实现了 `Deref<Target = str>` 之后，`str` 上几十个方法（`len`、`trim`、`split`……）全都「免费」挂到了 `SharedString` 上。

### 2.4 `HashMap::get` 的泛型签名（本讲钥匙）

标准库中 `HashMap::get` 的签名（简化排版）是：

```rust
pub fn get<Q>(&self, k: &Q) -> Option<&V>
where
    K: Borrow<Q>,
    Q: Hash + Eq + ?Sized,
```

读法：`HashMap<SharedString, V>` 的键类型 `K = SharedString`。只要 `SharedString: Borrow<Q>`，你就能拿一个 `&Q` 来查。最常用的两个 `Q`：

- `Q = SharedString`：靠标准库的反射实现 `impl Borrow<T> for T`，用 `&SharedString` 查——人人都行。
- `Q = str`：靠本讲要精读的 `impl Borrow<str> for SharedString`，用 `&str` 查——**这才需要专门实现**。

`get` 内部会先用 `Q` 的 `Hash` 算哈希定位桶，再在桶内用 `==` 比较「借出来的键」与「存进去的键」。所以跨类型查键能正确工作，有一个隐含前提：**两种类型的哈希与相等语义必须一致**——这正是 `Borrow` 的契约，4.3 节展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/gpui_shared_string/gpui_shared_string.rs:1-212](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L1-L212) | 本讲主文件：`Deref`（L17-L23）、`AsRef`（L62-L66）、`Borrow`（L68-L72）三个 impl 都在这 212 行里 |
| [crates/theme/src/registry.rs:60-61](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/theme/src/registry.rs#L60-L61) | 真实用法：主题注册表用 `HashMap<SharedString, Arc<Theme>>` 存主题 |
| [crates/theme/src/registry.rs:200-208](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/theme/src/registry.rs#L200-L208) | 真实用法：`ThemeRegistry::get(&self, name: &str)` 直接用 `&str` 查上面的 map |
| [crates/project/src/lsp_store/semantic_tokens.rs:537](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/project/src/lsp_store/semantic_tokens.rs#L537) | 真实用法：`modifier_mask: HashMap<SharedString, u32>` |
| [crates/project/src/lsp_store/semantic_tokens.rs:607-612](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/project/src/lsp_store/semantic_tokens.rs#L607-L612) | 真实用法：`has_modifier(&self, ..., modifier: &str)` 用 `&str` 查 `modifier_mask` |

## 4. 核心概念与源码讲解

先用一张表建立三个 trait 的整体分工，随后逐个精读：

| | `Deref` | `AsRef` | `Borrow` |
| --- | --- | --- | --- |
| 回答的问题 | 「编译器解引用时把我当什么」 | 「我能被看作什么」 | 「我作为容器键时，能用什么来查我」 |
| 谁在用它 | 编译器（隐式：方法解析、强制转换） | 泛型 API 作者（显式：`impl AsRef<str>` 边界） | 标准库容器（`HashMap` / `BTreeMap` 的 `get` 等） |
| 能实现几个方向 | 每个类型只能有一个 `Target` | 任意多个（如 `String` 实现了 `AsRef<str>`、`AsRef<[u8]>`、`AsRef<Path>`……） | 任意多个，但带一致性契约 |
| 隐含契约 | 无 | 无（只是「便宜视图」） | **`Eq` / `Ord` / `Hash` 必须与被借类型一致** |
| 在本 crate 的行为 | 交出 `&str` | 交出 `&str` | 交出 `&str` |

三个 impl 殊途同归——都只是把内部的 `SmolStr` 以 `&str` 的视角交出去——但**服务对象完全不同**。

### 4.1 `Deref<Target = str>` 与自动解引用

#### 4.1.1 概念说明

`std::ops::Deref` 是编译器的「解引用说明书」：

```rust
pub trait Deref {
    type Target: ?Sized;
    fn deref(&self) -> &Self::Target;
}
```

它声明：对 `SharedString` 解引用（`*s`），你得到的是 `str`；`deref` 负责完成这次转换。编译器在两个场合会**自动**使用它，不需要你写 `*` 或 `.as_str()`：

1. **方法解析的自动解引用**：`shared.len()` 中 `len` 不是 `SharedString` 的方法，编译器沿解引用链找到 `str::len`。
2. **解引用强制转换（deref coercion）**：当某处期待 `&U` 而你给出 `&T`，且 `T: Deref<Target = U>` 时，编译器自动插入转换。典型场景是函数调用：`fn f(s: &str)` 可以直接传 `&shared`。

注意一个关键限制：强制转换发生在**引用之间**。`f(shared)`（不加 `&`）不行——`shared` 是 `SharedString` 类型的值，不会自动变成 `&str`；必须是 `f(&shared)`。

另外，本 crate **故意没有实现 `DerefMut`**（没有 `deref_mut`），所以你拿不到 `&mut str`，也就无法透过 `SharedString` 改内容——不可变性在类型系统层面焊死，这与前两讲「不可变、可廉价克隆」的定位一致。

#### 4.1.2 核心流程

`shared.split_whitespace()` 从源码到执行的路径：

```text
写法：shared.split_whitespace()
  │
  ├─ ① 编译器在 SharedString 的固有方法中找 split_whitespace
  │      （只有 new_static / new / as_str 三个，没有）
  ├─ ② 再在 SharedString 实现的 trait 方法中找（也没有）
  ├─ ③ 对 shared 解引用：*shared ──调用──> Deref::deref(&shared)
  │      返回 &str（内部即 self.0.as_str()，一次指针交接，零复制）
  └─ ④ 在 str 的方法中找到 split_whitespace，调用之
        返回值 SplitWhitespace<'_> 借用着这个 &str，
        因而其生命周期与 shared 本体绑定
```

而 `f(&shared)`（`f` 接受 `&str`）的路径是编译期类型转换：

```text
&SharedString --deref coercion--> &str --deref coercion--> （若目标还是 &str 就到此为止）
```

强制转换还可以**多跳**：`&Rc<SharedString>` → `&SharedString` → `&str` 也能一步到位，因为 `Rc<T>` 同样实现了 `Deref<Target = T>`。

#### 4.1.3 源码精读

先看结构体定义回顾上下文：

[crates/gpui_shared_string/gpui_shared_string.rs:14-23](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L14-L23) —— 定义 newtype 并实现 `Deref`：

```rust
#[derive(Eq, PartialEq, PartialOrd, Ord, Hash, Clone)]
pub struct SharedString(SmolStr);

impl std::ops::Deref for SharedString {
    type Target = str;

    fn deref(&self) -> &Self::Target {
        self.0.as_str()
    }
}
```

逐行说明：

- `type Target = str;`：声明解引用目标是 `str`。整个类型只能有这一个方向——这决定了 `SharedString` 的「代理身份」只有字符串一种。
- `self.0.as_str()`：`self.0` 是私有的 `SmolStr`，这里调用 `SmolStr` 自己的固有方法 `as_str()` 拿到 `&str`。整个 `deref` 只是一次指针与长度的交接，不分配、不复制。

再对照固有方法 `as_str`：

[crates/gpui_shared_string/gpui_shared_string.rs:36-39](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L36-L39) —— 显式的读取接口：

```rust
    /// Get a &str from the underlying string.
    pub fn as_str(&self) -> &str {
        &self.0
    }
```

注意 `&self.0` 的类型本来是 `&SmolStr`，它能出现在返回类型 `&str` 的位置，是因为 `SmolStr` 自身实现了 `Deref<Target = str>`，在返回处发生了一次解引用强制转换。也就是说：`Deref::deref` 走「`SmolStr::as_str()` 固有方法」，固有 `as_str` 走「`&SmolStr` 的强制转换」——**两条等价的路径，都通向同一份字节**。既然有 `Deref`，固有 `as_str` 的价值在于显式、可读（读者一眼看出「这里在取借用」），以及在需要方法调消歧的场合可用。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `Deref` 带来的两类便利——免写 `.as_str()` 调用 `str` 方法，以及 `&SharedString` 直接传入 `&str` 参数。

**操作步骤**（以下为示例代码，承接 u1-l2 建立的独立 cargo 项目，`Cargo.toml` 中以 path 依赖引入 zed 仓库的 `gpui_shared_string`，路径按你的实际布局调整）：

```toml
[dependencies]
gpui_shared_string = { path = "../../zed/crates/gpui_shared_string" }
```

```rust
use gpui_shared_string::SharedString;

#[test]
fn deref_gives_str_methods_for_free() {
    let text = SharedString::new("hello gpui world");

    // ① 直接调用 str 的方法，全程没有写 .as_str()
    assert_eq!(text.len(), 16); // str::len，按字节计
    let words: Vec<&str> = text.split_whitespace().collect();
    assert_eq!(words, vec!["hello", "gpui", "world"]);

    // ② 解引用强制转换：&SharedString -> &str
    fn char_count(s: &str) -> usize {
        s.chars().count()
    }
    assert_eq!(char_count(&text), 16); // 注意要加 &

    // ③ 反例对照（取消注释应编译失败）：
    // char_count(text); // 期待 &str，给的是 SharedString 值，coercion 只发生在引用之间
}
```

**需要观察的现象**：测试编译通过且断言全绿；取消最后一行注释后 `cargo test` 报类型不匹配错误，错误信息里通常能看到 "expected `&str`, found `SharedString`" 以及编译器提示可以考虑 deref。

**预期结果**：`len()` / `split_whitespace()` / `char_count(&text)` 三处都验证 `Deref` 生效。本实践未在本环境运行，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`fn f(s: &str)` 能接受 `f(&shared)`，为什么不能定义 `fn g(s: str)` 并传值？

**答案**：`str` 是动态大小类型（DST），编译期不知道占多少字节，不能按值放在参数里；而强制转换只发生在「`&T` 到 `&U`」的引用之间。所以字符串视图永远以 `&str` 传递，`Deref` 恰好描述了 `&SharedString → &str` 这一跳。

**练习 2**：`let v: Vec<&str> = shared.split_whitespace().collect();` 得到的 `Vec` 借用的是谁？若 `shared` 随后被移动或销毁，再用 `v` 会怎样？

**答案**：`split_whitespace` 是 `str` 的方法，经由 `Deref::deref` 借到了 `&str`，其生命周期与 `shared` 本体绑定，所以 `v` 借用的是 `shared`。之后销毁 `shared` 再用 `v`，借用检查器会直接拒绝编译（报「借用了被移动的值」），这正是零成本借用在类型层面的保护。

**练习 3**：`fn f(s: &str)`，有 `let r = std::rc::Rc::new(SharedString::new("x"));`，`f(&r)` 能编译吗？

**答案**：能。解引用强制转换可以链式多跳：`&Rc<SharedString>` →（`Rc<T>: Deref<Target = T>`）→ `&SharedString` →（本 crate 的 `Deref`）→ `&str`。这是标准库明确保证的传递行为。

### 4.2 `AsRef<str>` 与泛型字符串 API 的互操作

#### 4.2.1 概念说明

`AsRef` 的定义同样极简：

```rust
pub trait AsRef<T: ?Sized> {
    fn as_ref(&self) -> &T;
}
```

语义是「我可以用很低的代价被看作 `T`」。与 `Deref` 的三点关键差异：

1. **服务对象不同**：`Deref` 是给编译器的（隐式触发）；`AsRef` 是给泛型 API 作者的——函数签名写 `fn f(s: impl AsRef<str>)`，就声明「任何能当作 `str` 看的东西都收」。标准库大量 API 采用这种写法，例如 `std::fs::read_to_string(path: impl AsRef<Path>)`。
2. **方向数量不同**：`Deref` 每类型只有一个 `Target`；`AsRef` 可以实现任意多个方向。标准库的 `String` 就同时实现了 `AsRef<str>`、`AsRef<[u8]>`、`AsRef<OsStr>`、`AsRef<Path>` 等。
3. **不被隐式链式使用**：编译器不会因为 `SharedString: AsRef<str>` 且 `str: AsRef<OsStr>` 就允许把 `&SharedString` 当 `AsRef<OsStr>` 用——泛型匹配的是**精确的 trait 实现**。

本 crate 内部就有最贴切的例子：构造函数 `new` 的参数是 `impl AsRef<str>`。因为这个签名，`SharedString::new("lit")`、`SharedString::new(some_string)`、甚至 `SharedString::new(&other_shared)` 都能直接编译（`&T` 有 `T: AsRef<U> => &T: AsRef<U>` 的覆盖实现）。

#### 4.2.2 核心流程

一个泛型函数 `fn describe(s: impl AsRef<str>)` 接到调用 `describe(&shared)` 时：

```text
调用：describe(&shared)
  │
  ├─ ① 单态化：参数类型 S = &SharedString
  ├─ ② 检查边界：&SharedString: AsRef<str> 是否成立？
  │      由覆盖实现 impl<T: AsRef<U>> AsRef<U> for &T
  │      归约到 SharedString: AsRef<str> —— 本 crate 手写实现，成立
  └─ ③ 函数体内 s.as_ref() 调用 AsRef::as_ref(&shared)
         -> &self.0 即 &SmolStr，经强制转换成为 &str，零复制
```

#### 4.2.3 源码精读

[crates/gpui_shared_string/gpui_shared_string.rs:31-34](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L31-L34) —— `AsRef` 的「消费端」用法，构造函数直接用泛型边界：

```rust
    /// Creates a [`SharedString`].
    pub fn new(str: impl AsRef<str>) -> Self {
        SharedString(SmolStr::new(str))
    }
```

`SmolStr::new` 同样接受 `impl AsRef<str>`，参数一路透传。得益于这个边界加覆盖实现，调用方传 `&str`、`String`、`&String`、`Cow<'_, str>`、`&SharedString` 都无需先做任何转换。

[crates/gpui_shared_string/gpui_shared_string.rs:62-66](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L62-L66) —— `AsRef` 的「生产端」实现：

```rust
impl AsRef<str> for SharedString {
    fn as_ref(&self) -> &str {
        &self.0
    }
}
```

与 `Deref::deref` 对照看：`deref` 写的是 `self.0.as_str()`（走 `SmolStr` 固有方法），这里写的是 `&self.0`（走 `&SmolStr` 到 `&str` 的强制转换）。两条写法生成的行为一致——把内部数据的只读视图交出去。这也再次体现了 newtype 的透明代理风格：**所有 trait 实现都只做「转发」，不添加任何自有逻辑**。

#### 4.2.4 代码实践

**实践目标**：写一个接受 `impl AsRef<str>` 的函数，验证 `SharedString` 能与 `&str`、`String` 混用，体会「泛型字符串 API」的互操作能力。

**操作步骤**（示例代码，加入上面的独立项目）：

```rust
use gpui_shared_string::SharedString;

fn describe(s: impl AsRef<str>) -> String {
    format!("len = {}", s.as_ref().len())
}

#[test]
fn as_ref_interops_with_string_ecosystem() {
    assert_eq!(describe("literal"), "len = 7");           // &str: AsRef<str>
    assert_eq!(describe(String::from("owned")), "len = 5"); // String: AsRef<str>

    let shared = SharedString::new("shared");
    assert_eq!(describe(&shared), "len = 6"); // &SharedString 经覆盖实现归约
    assert_eq!(describe(shared), "len = 6");  // 按值传入同样成立（move）
}
```

**需要观察的现象**：四类参数（`&str`、`String`、`&SharedString`、`SharedString`）全部编译通过且结果一致。

**预期结果**：断言全绿。`&shared` 之所以可行，是因为标准库的覆盖实现 `impl<T: AsRef<U>> AsRef<U> for &T` 把它归约到了我们手写的 `SharedString: AsRef<str>`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：标准库 `Path::new` 的签名是 `pub fn new<S: AsRef<OsStr> + ?Sized>(s: &S) -> &Path`。`Path::new(&shared)` 能编译吗？为什么？

**答案**：不能。此时 `S = SharedString`，需要 `SharedString: AsRef<OsStr>`，但本 crate 只实现了 `AsRef<str>`。泛型边界做的是精确匹配，编译器不会替你先 `as_ref()` 再转——`AsRef` 不具备 `Deref` 那样的隐式链式能力。正确写法是 `Path::new(shared.as_str())`（利用 `str: AsRef<OsStr>`）。

**练习 2**：`String` 实现了四个方向的 `AsRef`，却只能有一个 `Deref` 目标。这两者的分工对 API 设计意味着什么？

**答案**：`Deref` 唯一且被编译器隐式使用，适合表达「本类型的本体就是它」（`String` 的本体是 `str`，`SharedString` 的本体也是 `str`）；`AsRef` 多方向且需显式/泛型使用，适合表达「本类型可以顺便被看作哪些东西」（字符串顺便可以当字节切片、路径……）。`SharedString` 选择与 `String` 相同的分工：`Deref` 只指向 `str`，`AsRef` 也只做 `str`——保持最小、精准的互操作面。

**练习 3**：把 `SharedString::new` 的参数从 `impl AsRef<str>` 改成 `&str`（仅作思想实验），会失去哪些调用方式？

**答案**：`SharedString::new(some_string)`、`SharedString::new(&other_shared)`、传 `Cow` 等写法都要先改写成 `&x[..]` / `x.as_str()`。`AsRef` 边界把「调用方到字符串的距离」从「必须恰好是 `&str`」放宽为「任何能廉价视图成 `str` 的类型」，这正是它成为标准库通用字符串参数惯用法的原因。

### 4.3 `Borrow<str>` 与 `HashMap<SharedString, V>` 用 `&str` 查键实验

#### 4.3.1 概念说明

`Borrow` 的方法签名和 `AsRef` 一模一样：

```rust
pub trait Borrow<Borrowed: ?Sized> {
    fn borrow(&self) -> &Borrowed;
}
```

区别在**文档契约**：标准库要求 `Borrow` 的实现保证 `Owner` 与 `Borrowed` 之间的 `Eq`、`Ord`、`Hash`（以及比较序）**完全一致**——即「借出来的视图」和「本体」在任何以内容判等/分桶的场合不可区分。`AsRef` 没有这条契约，它只是「便宜视图」。

为什么容器只认 `Borrow`？回看 2.4 节 `get<Q>` 的边界：`K: Borrow<Q>, Q: Hash + Eq`。`insert` 时用 `K` 自己的 `Hash`/`Eq` 定位；`get` 时用 `Q` 的 `Hash` 算桶、用 `K::borrow` 出来的视图做相等比较。**若两种类型的哈希不一致，同一个文本在插入和查找时会落进不同的桶**，查找将从「概率性失败」退化成「必然找不到」——这正是违反 `Borrow` 契约的后果（标准库称其为逻辑错误，容器行为不再有保证）。

`SharedString` 满足契约的依据是一条完整的委托链（承接 u2-l1 的结论）：

```text
SharedString 的 derive(Eq, PartialEq, Ord, PartialOrd, Hash)
  --委托--> SmolStr 的实现（按字符串内容比较/哈希）
  --语义等价于--> str 的 Eq/Ord/Hash（同样按内容）
```

所以「`SharedString` 本体的哈希」与「`borrow()` 交出的 `&str` 的哈希」对同一文本永远相同，跨类型查键是安全的。注意 `Borrow` 一致性是对**内容**而言的，与存储方式无关：`new_static` 存进内联、`new(&long_string)` 存到堆上，只要文本相同，哈希与相等就相同。

顺带一提，`PartialEq<String>`、`PartialEq<str>`、`PartialEq<&str>` 这批手写实现（[crates/gpui_shared_string/gpui_shared_string.rs:86-108](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L86-L108)）解决的是「直接用 `==` 比较」的场景，是同一「内容语义」哲学的延伸，下一讲（u2-l4）会逐行精读。

#### 4.3.2 核心流程

`map.get("rust")`（`map: HashMap<SharedString, i32>`）的完整旅程：

```text
get("rust")
  │
  ├─ ① 确定查找类型 Q = str（"rust" 的类型是 &str）
  ├─ ② 检查边界：SharedString: Borrow<str>？—— 手写实现，成立
  ├─ ③ 用 str 的 Hash 对 "rust" 计算哈希，定位桶
  │      （insert 时用的是 SharedString 的 derive(Hash)
  │        -> SmolStr -> 按内容；两者对同一文本必然同值 —— 契约成立）
  ├─ ④ 桶内对每个键 k 调用 Borrow::borrow(k) 得到 &str
  │      （内部即 self.as_ref() -> &self.0）
  └─ ⑤ 用 str 的 == 比较该视图与 "rust"，命中则返回 Some(&value)
```

#### 4.3.3 源码精读

先看 `Borrow` 实现本身：

[crates/gpui_shared_string/gpui_shared_string.rs:68-72](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L68-L72) —— 三行实现，直接委托给上一节的 `AsRef::as_ref`：

```rust
impl Borrow<str> for SharedString {
    fn borrow(&self) -> &str {
        self.as_ref()
    }
}
```

配套的导入在文件开头，`Borrow` 是从 `std::borrow` 显式引入的：

[crates/gpui_shared_string/gpui_shared_string.rs:1-5](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L1-L5)

```rust
use std::{
    borrow::{Borrow, Cow},
    iter,
    sync::Arc,
};
```

再看契约的另一半——保证哈希/相等一致的 derive：

[crates/gpui_shared_string/gpui_shared_string.rs:14-15](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L14-L15) —— `Hash`/`Eq`/`Ord` 全部按内容委托给 `SmolStr`：

```rust
#[derive(Eq, PartialEq, PartialOrd, Ord, Hash, Clone)]
pub struct SharedString(SmolStr);
```

`borrow()` 的三行 + `derive(Hash, Eq, ...)` 的一行，合起来才是「可以用 `&str` 查键」的完整条件——**前者提供视图，后者保证语义一致**，缺一不可。

最后看 zed 真实代码里的两个惯用法。

**例一：主题注册表。** [crates/theme/src/registry.rs:60-61](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/theme/src/registry.rs#L60-L61) 中主题表以 `SharedString` 为键：

```rust
    themes: HashMap<SharedString, Arc<Theme>>,
    icon_themes: HashMap<SharedString, Arc<IconTheme>>,
```

[crates/theme/src/registry.rs:200-208](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/theme/src/registry.rs#L200-L208) 中按名字取主题时，直接用 `&str` 查这张表：

```rust
    /// Returns the theme with the given name.
    pub fn get(&self, name: &str) -> Result<Arc<Theme>, ThemeNotFoundError> {
        self.state
            .read()
            .themes
            .get(name)
            .ok_or_else(|| ThemeNotFoundError(name.to_string().into()))
            .cloned()
    }
```

`themes.get(name)` 能编译，靠的正是 `impl Borrow<str> for SharedString`。调用方（设置解析、命令面板）手里只有 `&str`，完全不需要先构造一个 `SharedString` 再来查。

**例二：LSP 语义token修饰符。** [crates/project/src/lsp_store/semantic_tokens.rs:537](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/project/src/lsp_store/semantic_tokens.rs#L537) 用 `HashMap<SharedString, u32>` 记录每个修饰符的位掩码：

```rust
    modifier_mask: HashMap<SharedString, u32>,
```

[crates/project/src/lsp_store/semantic_tokens.rs:607-612](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/project/src/lsp_store/semantic_tokens.rs#L607-L612) 查询时同样直接传 `&str`：

```rust
    pub fn has_modifier(&self, token_modifiers: u32, modifier: &str) -> bool {
        let Some(mask) = self.modifier_mask.get(modifier) else {
            return false;
        };
        (token_modifiers & mask) != 0
    }
```

两处真实代码的模式一致：**键用 `SharedString`（廉价克隆、跨任务共享），查询用 `&str`（调用方现成的字符串）**，中间的桥梁就是本讲的 `Borrow<str>`。

#### 4.3.4 代码实践

**实践目标**：亲手完成「`HashMap<SharedString, V>` 双路查键」实验，并用一个反例理解契约的意义。

**操作步骤**（示例代码，加入前两节的独立项目）：

```rust
use std::collections::HashMap;
use gpui_shared_string::SharedString;

#[test]
fn borrow_enables_cross_type_lookup() {
    let mut scores: HashMap<SharedString, i32> = HashMap::new();
    scores.insert(SharedString::new("rust"), 1);

    // 路线一：用 &str 查键 —— 依赖 impl Borrow<str> for SharedString
    assert_eq!(scores.get("rust"), Some(&1));

    // 路线二：用 &SharedString 查键 —— 依赖反射实现 impl Borrow<T> for T
    let key = SharedString::new("rust"); // 与插入的键文本相同
    assert_eq!(scores.get(&key), Some(&1));

    // 两种查法都应命中同一桶、判等一致
    assert_eq!(scores.get("rust"), scores.get(&key));

    // 不存在的键
    assert_eq!(scores.get("python"), None);
}
```

**需要观察的现象**：`&str` 与 `&SharedString` 两条查找路线都返回 `Some(&1)`；`get("python")` 返回 `None`。把插入的键换成 `SharedString::new_static("rust")`（静态构造）再 `get("rust")`，仍然命中——验证「一致性只看内容，与构造路径/存储方式无关」。

**预期结果**：全部断言通过。若把 `scores.get("rust")` 错写成 `scores.get(&"rust".to_string())`，注意它能编译吗？——能，但那是 `Q = String` 的查法，靠的是 `SharedString: Borrow<String>` 吗？并不存在，所以实际会编译失败，报缺少 `Borrow<String>`；这条报错本身就是理解 `get` 边界的好材料。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：假设（思想实验）把 `borrow()` 的实现改成恒返回 `"x"`，且仍能满足编译。`map.get("rust")` 会发生什么？

**答案**：`insert` 时用 `SharedString` 自己的 `Hash`（按内容，"rust" 的哈希）定位桶；`get("rust")` 用 `str` 的哈希定位同一个桶，但桶内比较时 `borrow()` 恒给出 `"x"`，与 `"rust"` 不相等，于是永远返回 `None`。更糟的是 `get("x")` 可能「意外命中」错误的键。违反 `Borrow` 契约（视图与本体 `Eq`/`Hash` 不一致）后，容器行为在逻辑上失去保证——这就是标准库把它写成契约而非编译器检查的原因。

**练习 2**：为什么 `HashMap::get` 的边界是 `K: Borrow<Q>` 而不是 `K: AsRef<Q>`？

**答案**：因为 `get` 的正确性依赖「插入时用的哈希/相等」与「查找时用的哈希/相等」对同一内容给出相同结果。`AsRef` 没有这条一致性承诺（它只是「能给出视图」），而 `Borrow` 的文档契约恰好承诺了 `Eq`/`Ord`/`Hash` 一致。标准库把「语义保证」编码进选择哪个 trait。

**练习 3**：`map: HashMap<SharedString, i32>`，以下哪些调用能编译？`map.contains_key("rust")`、`map.remove("rust")`、`map.get(&String::from("rust"))`。

**答案**：前两个能：`contains_key` 与 `remove` 的泛型边界和 `get` 一样是 `K: Borrow<Q>, Q: Hash + Eq`，`Q = str` 成立。第三个不能：`Q = String` 需要 `SharedString: Borrow<String>`，本 crate 没有实现（也无必要）——想用 `String` 查，写成 `map.get(string.as_str())` 即可。

## 5. 综合实践

把本讲三个 trait 串成一个完整小程序：**词频统计器**。输入端用 `AsRef` 接收任意字符串，统计中经 `Deref` 调用 `str` 的方法切词，键存为 `SharedString`，查询端用 `Borrow` 以 `&str` 直查（示例代码）：

```rust
use std::collections::HashMap;
use gpui_shared_string::SharedString;

// AsRef：接受任何"能看作 str"的输入（&str / String / &SharedString ...）
fn word_counts(text: impl AsRef<str>) -> HashMap<SharedString, usize> {
    let mut counts: HashMap<SharedString, usize> = HashMap::new();
    // text.as_ref() 得到 &str 后，split_whitespace 是 str 的方法；
    // 也可以借助 Deref 直接对 SharedString 调用（见 main 末尾）
    for word in text.as_ref().split_whitespace() {
        // From<&str>：&str -> SharedString 键（u2-l3 将精读 From 家族）
        *counts.entry(word.into()).or_insert(0) += 1;
    }
    counts
}

// Borrow：用 &str 在 HashMap<SharedString, _> 上查键
fn count_of(counts: &HashMap<SharedString, usize>, word: &str) -> usize {
    counts.get(word).copied().unwrap_or(0)
}

fn main() {
    let counts = word_counts("the quick brown fox jumps over the lazy dog the end");

    // 查询端：&str 直查（Borrow<str>）
    println!("the = {}", count_of(&counts, "the")); // 期望输出 3
    println!("fox  = {}", count_of(&counts, "fox")); // 期望输出 1

    // 遍历端：直接在 &SharedString 上调 str 方法（Deref），无需 .as_str()
    for (word, n) in &counts {
        println!("{} -> {} ({} 字节)", word.to_uppercase(), n, word.len());
    }
}
```

验证清单（在独立项目中运行 `cargo run`）：

1. `the = 3`、`fox = 1` —— 验证 `Borrow` 查键。
2. 遍历输出每个词的大写形式与字节长度 —— 验证 `Deref` 免写 `.as_str()`。
3. 把 `main` 里的调用改成 `word_counts(SharedString::new("a b a"))` 再 `count_of(&counts, "a")`（期望 2）—— 验证 `AsRef` 输入端互操作。
4. 思考题（对照 4.3 练习 1）：为什么统计键用 `SharedString` 而不是 `String`？—— 提示回看 u2-l1 的克隆成本对比：词频表若要在 GPUI 任务间共享/克隆，`O(1)` 克隆会体现价值。

本综合实践未在本环境运行，待本地验证。

## 6. 本讲小结

- `Deref<Target = str>`（[L17-L23](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L17-L23)）让编译器在方法解析与引用强制转换两个场合自动把 `SharedString` 当 `str` 用；没有 `DerefMut`，不可变性焊死在类型上。
- `AsRef<str>`（[L62-L66](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L62-L66)）服务于泛型 API：`new(impl AsRef<str>)` 因此能收 `&str`、`String`、`&SharedString` 等；`AsRef` 可多方向但不被编译器隐式链式使用。
- `Borrow<str>`（[L68-L72](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L68-L72)）是容器查键专用，契约要求与 `str` 的 `Eq`/`Ord`/`Hash` 一致；这一致性由 `derive(Eq, PartialEq, PartialOrd, Ord, Hash)` 委托 `SmolStr` 按内容生效而成立。
- 三个 impl 的实现体都只是「把内部 `SmolStr` 以 `&str` 视角交出去」（分别走 `self.0.as_str()`、`&self.0`、`self.as_ref()` 三条等价路径），是 newtype 透明代理模式的教科书示范。
- zed 真实代码中，`ThemeRegistry::get`（[registry.rs:200-208](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/theme/src/registry.rs#L200-L208)）与 `has_modifier`（[semantic_tokens.rs:607-612](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/project/src/lsp_store/semantic_tokens.rs#L607-L612)）都是「键为 `SharedString`、查询用 `&str`」的惯用法。

## 7. 下一步学习建议

本讲搞定了「像 `str` 一样**使用**」；下一讲 u2-l3（From 转换矩阵）解决「像 `str` 一样**转换**」：系统梳理 `SharedString` 的全部 13 个 `From`/`Into` 方向（从 `&str`、`String`、`Box<str>`、`Arc<str>`、`Cow`、`char` 转入，转出为 `Arc<str>` 与 `String`），并分析每个转换背后的所有权与内存语义。建议先精读 [crates/gpui_shared_string/gpui_shared_string.rs:110-192](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L110-L192) 的 `From` 家族，再带着「哪些转换零拷贝、哪些要分配」的问题去看 [crates/gpui/src/styled.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/styled.rs) 中 `font_family(impl Into<SharedString>)` 这类 API 的设计。若对 `Borrow` 契约意犹未尽，标准库 `std::borrow::Borrow` 的文档注释中关于 `Eq`/`Ord`/`Hash` 一致性的段落是权威补充读物。
