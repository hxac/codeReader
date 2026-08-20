# From 转换矩阵：与 Rust 字符串生态互转

## 1. 本讲目标

上一讲我们看清了 `SharedString` 如何通过 `Deref` / `AsRef` / `Borrow` 把内部数据「借出去」。本讲换个方向，看数据如何「进来」和「出去」：`gpui_shared_string.rs` 里有整整 **12 个手写的 `From` impl**，是全文件数量最多的一类实现，它们共同构成一张「转换矩阵」，让 `SharedString` 和 Rust 字符串生态（`&str`、`String`、`Box<str>`、`Arc<str>`、`Cow`、`char`）无缝互转。

学完本讲，你应该能：

1. 背出转换矩阵的两个方向：**转入 10 个**（`&str`、`&mut str`、`&String`、`String`、`Box<str>`、`Arc<str>`、`&Arc<str>`、`Cow`、`char`、`&SharedString`）与**转出 2 个**（`Arc<str>`、`String`），再加上标准库的恒等转换，共 13 个可用方向。
2. 对每一个方向说出两件事：**是否转移所有权**（源值还能不能继续用）、**是否发生堆分配 / 数据拷贝**。
3. 解释为什么 `&mut str`、`&String` 这些「看起来和 `&str` 一样」的来源也必须各写一个 impl——答案是泛型边界处没有自动解引用强转。
4. 从 API 设计者视角读懂 `font_family(impl Into<SharedString>)` 这种签名为什么好用，以及 `const ELLIPSIS` 为什么必须用 `new_static`。

## 2. 前置知识

### 2.1 `From` 与 `Into` 的镜像关系

`From` 表示「从别的类型**构造**我」。标准库有一条反射性 blanket 实现：

```rust
// 标准库（简化示意）
impl<T, U> Into<U> for T
where
    U: From<T>,
```

意思是：**只要你为 `U` 实现了 `From<T>`，就自动获得了 `T: Into<U>`**。所以源码里写的是 `From`，而业务代码里到处用的 `.into()` 是 `Into::into`——每写一个 `From`，就同时点亮了一个 `.into()` 方向。此外还有 `impl<T> From<T> for T`（恒等转换：任何类型都能 `from`/`into` 出自己），它不属于本 crate 的 12 个手写 impl，但确实是第 13 个可用方向，4.4 节会在 zed 真实代码里遇到它。

### 2.2 转移所有权 vs 借用

- **借用型来源**（`&str`、`&String`……）：`.into()` 之后源值仍然归你，`SharedString` 必须把字符串字节**复制**进自己的存储（内联 24 字节或堆）。
- **所有权型来源**（`String`、`Box<str>`、`Arc<str>`、`Cow`……）：`.into()` 会**消费**源值，之后原变量不可再用。数据有三种命运：被接管为零拷贝共享（如 `Arc<str>`）、被转移为共享缓冲（如 `String`）、或仍需复制（内联情形）。

### 2.3 「分配」指什么：回顾 u2-l1 的结论

`SharedString` 内部是 `SmolStr`（0.3.6），固定 24 字节：

- **≤ 23 字节**（按 UTF-8 字节数计）：内联存储，**零堆分配**，克隆只复制结构体本身；
- **> 23 字节**：堆上 `Arc<str>` 共享，克隆只增加引用计数；
- **静态来源**（`new_static`）：指向二进制里的 `&'static str`，永不分配。

判断某个 `From` 「贵不贵」，本质上就是问：**这次转换把数据放进了哪种形态**。

### 2.4 本讲钥匙：泛型边界处没有自动强转

这是理解「为什么要写 12 个 impl」的关键。在日常代码里，`&String` 传给要 `&str` 的函数会自动强转（deref coercion）；但一旦函数签名是泛型：

```rust
fn font_family(self, family_name: impl Into<SharedString>) -> Self
```

编译器做类型推导时**不会**尝试「把 `&String` 强转成 `&str` 再套 `From<&str>`」这种链式操作。`&String: Into<SharedString>` 想成立，就必须存在一个**专门针对 `&String` 的 `From` impl**。这就是为什么源码里会出现 `From<&mut str>`、`From<&String>` 这些实现体与 `From<&str>` 一模一样的 impl——它们不是为了运行时行为，而是为了在泛型边界处「各占一个坑位」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/gpui_shared_string/gpui_shared_string.rs:110-192](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L110-L192) | 本讲主战场：12 个 `From` impl 全部集中在 L110-L192 这 83 行里 |
| [crates/gpui/src/styled.rs:13](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/styled.rs#L13) | `const ELLIPSIS: SharedString = SharedString::new_static("…")`：const 构造的典型 |
| [crates/gpui/src/styled.rs:87-108](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/styled.rs#L87-L108) | `text_ellipsis` 系列三个方法，反复克隆 `ELLIPSIS` |
| [crates/gpui/src/styled.rs:707-711](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/styled.rs#L707-L711) | `font_family(impl Into<SharedString>)`：`Into` 边界的教科书用法 |
| [crates/gpui/examples/text.rs:294-296](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/text.rs#L294-L296) | 真实用法：`SharedString` 本体直接传入 `font_family`（恒等转换方向） |
| [crates/gpui/examples/text.rs:311](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/text.rs#L311) | 真实用法：`SharedString::new(new_family)` 从 `&'static str` 构造 |

## 4. 核心概念与源码讲解

先用一张总表建立全局观（**所有权**指 `.into()` 后源值是否仍可用；**分配/拷贝**指构造过程的开销）：

| # | 方向 | 所有权 | 分配 / 拷贝 | 源码位置 |
| --- | --- | --- | --- | --- |
| 1 | `&str → SharedString` | 借用 | 短串内联零分配；长串堆分配 + 拷贝 | L117-L122 |
| 2 | `&mut str →` | 借用 | 同上 | L131-L136 |
| 3 | `&String →` | 借用 | 同上 | L138-L143 |
| 4 | `char →` | 转移 | 必然内联（1-4 字节），零堆分配 | L124-L129 |
| 5 | `String →` | 转移 | 短串内联拷贝；长串接管堆缓冲为共享，通常无整段重拷贝（实现细节，以 smol_str 0.3.6 为准） | L145-L150 |
| 6 | `Box<str> →` | 转移 | 长串零数据拷贝（Box 转共享）；短串内联拷贝 | L152-L157 |
| 7 | `Arc<str> →` | 转移 | **零拷贝零分配**：句柄直接入住 | L159-L164 |
| 8 | `&Arc<str> →` | 借用 | 零数据拷贝，引用计数 +1 | L166-L171 |
| 9 | `Cow<'a, str> →` | 转移 | 按分支：Borrowed 同 #1，Owned 同 #5 | L173-L178 |
| 10 | `&SharedString →` | 借用 | 克隆：24 字节复制或引用计数 +1 | L110-L115 |
| 11 | `SharedString → Arc<str>` | 转移 | 堆形态仅转移句柄；内联/静态形态需一次小分配 + 拷贝 | L180-L185 |
| 12 | `SharedString → String` | 转移 | **必然新分配 + 拷贝**（String 需独占可变缓冲） | L187-L192 |
| 13 | `SharedString → SharedString` | 转移 | 零开销（标准库恒等 `From<T> for T`） | 标准库 |

下面按「转入（借用型）→ 转入（所有权型）→ 转出 → API 设计视角」四块精读。

### 4.1 转入实现（一）：借用型来源

#### 4.1.1 概念说明

借用型来源的共同点：`.into()` 只**读取**源数据，不拿走它。因此 `SharedString` 必须给这些字节找一个新家——短字符串复制进 24 字节结构体内部（零堆分配），长字符串在堆上分配一次并逐字节拷贝。这一组包含 5 个 impl：`&str`、`&mut str`、`&String`、`&SharedString`、`&Arc<str>`。

其中 `&SharedString` 和 `&Arc<str>` 是「伪借用」：因为内部数据本来就可共享，它们不需要拷贝字节，只复制一个句柄/结构体。

#### 4.1.2 核心流程

以 `"hello world".into()` 为例：

```text
"hello world" (&str)
   │  From<&str> for SharedString
   ▼
SmolStr::from(&str)
   ├── len ≤ 23 ?  ──是──▶ 内联：字节复制进 24 字节结构体（零堆分配）
   └── len > 23 ?  ──否──▶ 堆：分配 Arc<str>，拷贝字节，计数 = 1
   ▼
SharedString(SmolStr)
```

#### 4.1.3 源码精读

先看最基础的 `From<&str>`：

[crates/gpui_shared_string/gpui_shared_string.rs:117-122](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L117-L122) —— 把 `&str` 交给 `SmolStr::from` 完成内联/堆的双模式构造，`#[inline]` 提示编译器把这一行转发内联进调用点，让 `.into()` 的机器码与直接调用 `SmolStr::from` 无异。

接着是两个「实现体相同、存在理由不同」的 impl：

[crates/gpui_shared_string/gpui_shared_string.rs:131-136](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L131-L136) —— `From<&mut str>`：函数体与 `From<&str>` 一字不差（`SmolStr::from(s)` 里 `&mut str` 自动降级为 `&str`，可变性根本没被用到）。它存在的唯一意义是让 `&mut str` 在 `impl Into<SharedString>` 泛型边界处也有专属坑位（见 2.4 节）。

[crates/gpui_shared_string/gpui_shared_string.rs:138-143](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L138-L143) —— `From<&String>`：同理。没有它，`fn f(x: impl Into<SharedString>)` 就无法接受 `&String`。

然后是「伪借用」的两个：

[crates/gpui_shared_string/gpui_shared_string.rs:110-115](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L110-L115) —— `From<&SharedString>`：直接 `s.clone()`。对照 u2-l1 讲过的克隆成本（内联 24 字节复制或引用计数 +1），这是一条廉价的「复制视图」路径；`#[inline]` 标注同样是消除转发开销。

[crates/gpui_shared_string/gpui_shared_string.rs:166-171](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L166-L171) —— `From<&Arc<str>>`：显式写出 `s.clone()`，先把 `Arc` 句柄克隆一份（引用计数 +1），再交给 `From<Arc<str>>`。全程零数据拷贝。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「泛型边界处没有自动强转」，从而理解 2.4 节的钥匙。

**操作步骤**（以下均为示例代码，待本地验证）：

1. 新建一个练习 crate（详见第 5 节的项目骨架），在 `src/lib.rs` 里写一个 40 行的迷你复刻版，只实现 `From<&str>`：

```rust
// 示例代码：迷你复刻，用于观察泛型边界的类型检查
pub struct MiniShared(String); // 故意用最简单的底层

impl From<&str> for MiniShared {
    fn from(s: &str) -> Self {
        MiniShared(s.to_string()) // 借用 → 必须拷贝
    }
}

fn accept(x: impl Into<MiniShared>) -> MiniShared {
    x.into()
}
```

2. 先调用 `accept("literal")`，编译通过。
3. 再加一行 `let s = String::from("owned"); accept(&s);`，重新编译。

**需要观察的现象**：第 3 步编译失败，报错大意是 `&String: Into<MiniShared>` 不满足，编译器列出的候选里没有 `From<&String>`——尽管 `From<&str>` 明明存在、`&String` 日常明明能当 `&str` 用。

**预期结果**：给 `MiniShared` 补上与 `From<&str>` 实现体相同的 `From<&String>` 后编译通过。这就是 `gpui_shared_string.rs` L131-L143 存在的全部理由。

#### 4.1.5 小练习与答案

**练习 1**：`From<&mut str>` 的函数体里用到了可变性吗？删掉这个 impl 会影响哪些代码？

**答案**：没有用到——`SmolStr::from(s)` 只做只读拷贝。删掉它影响的不是运行行为，而是类型检查：所有在 `impl Into<SharedString>` 边界处传入 `&mut str` 的调用点都会编译失败。

**练习 2**：`SharedString::from(&shared)` 与 `shared.clone()` 有何区别？

**答案**：运行时行为完全等价（前者内部就是调 `clone()`）；区别只在表达意图与适用场景——前者让 `&SharedString` 也能通过 `.into()` / `impl Into<SharedString>` 边界统一处理，后者是普通方法调用。

### 4.2 转入实现（二）：所有权型来源

#### 4.2.1 概念说明

这一组的 5 个 impl（`String`、`Box<str>`、`Arc<str>`、`Cow`、`char`）都会**消费**源值。设计者的核心诉求是：**能不拷贝就不拷贝**。

- `Arc<str>` 是最理想来源：它已经是共享堆数据，直接把句柄存进 `SmolStr`，零拷贝、零分配、连引用计数都不用动（所有权平移）。
- `String` / `Box<str>`：堆缓冲已在手上，长字符串时把它**接管**为共享数据（转成 `Arc<str>` 语义），避免再分配一次；短字符串则直接内联拷贝（拷 ≤ 23 字节比维护堆分配更便宜）。
- `char`：最多 4 字节，必然内联，零堆分配。
- `Cow<'a, str>`（Clone on Write 的缩写，枚举 `Borrowed(&'a str)` / `Owned(String)`）：消费时按分支分派，两条路各走各的语义。

#### 4.2.2 核心流程

```text
String::from("一整段较长的运行时文本..............")
   │  From<String>（转移所有权，源变量失效）
   ▼
SmolStr::from(String)
   ├── len ≤ 23 ──▶ 内联拷贝进结构体，原堆缓冲释放
   └── len > 23  ──▶ 接管堆缓冲为共享形态（长串通常无整段重拷贝）

Arc::from("...") 
   │  From<Arc<str>>（转移句柄）
   ▼
SmolStr 直接持有该 Arc：零拷贝、零分配、计数不变
```

#### 4.2.3 源码精读

[crates/gpui_shared_string/gpui_shared_string.rs:145-150](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L145-L150) —— `From<String>`：注意这里的标注升级成了 `#[inline(always)]`（本文件仅 3 处，另两处见 4.3），因为它是最热的路径——zed 代码里 `format!(...).into()` 随处可见，强制内联确保包装层零开销。

[crates/gpui_shared_string/gpui_shared_string.rs:152-157](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L152-L157) —— `From<Box<str>>`：`Box<str>` 是「大小固定的堆上 UTF-8」，转共享是纯句柄层面的转换。

[crates/gpui_shared_string/gpui_shared_string.rs:159-164](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L159-L164) —— `From<Arc<str>>`：全矩阵中**最便宜**的转入方向。调用方此前在别处拼好了一份共享文本（例如语言服务的响应缓存），这里原样接收。

[crates/gpui_shared_string/gpui_shared_string.rs:173-178](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L173-L178) —— `From<Cow<'a, str>>`：带生命周期参数 `'a`，因为 `Borrowed` 分支可能引用外借数据；消费枚举后按分支落位。

[crates/gpui_shared_string/gpui_shared_string.rs:124-129](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L124-L129) —— `From<char>`：`smol_str` 没有提供 `From<char>`，所以这里用 `SmolStr::from_iter(iter::once(c))`（`FromIterator<char>`）凑出单字符字符串。`char` 的 UTF-8 编码最长 4 字节，永远内联。

#### 4.2.4 代码实践

**实践目标**：验证 `Cow` 两个分支行为一致，以及所有权型来源被消费的事实。

**操作步骤**（示例代码，待本地验证；项目骨架见第 5 节）：

```rust
use std::borrow::Cow;
use gpui_shared_string::SharedString;

#[test]
fn cow_both_variants() {
    // Borrowed 分支：等同 From<&str>，借用拷贝
    let borrowed: Cow<str> = Cow::Borrowed("borrowed branch");
    let s1 = SharedString::from(borrowed);
    assert_eq!(s1.as_str(), "borrowed branch");

    // Owned 分支：转移 String 的所有权
    let owned: Cow<str> = Cow::Owned(String::from("owned branch"));
    let s2 = SharedString::from(owned);
    assert_eq!(s2.as_str(), "owned branch");
    // assert_eq!(owned, ...); // 取消注释会编译错误：owned 已被 move
}
```

**需要观察的现象**：两个分支产出的 `SharedString` 用起来毫无差别——转换语义差异（拷贝 vs 转移）完全被类型封装吸收了。

**预期结果**：测试通过；取消最后一行注释后出现 `borrow of moved value` 编译错误，直观证明「所有权型来源被消费」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `From<char>` 不写成 `SmolStr::from(c)`？

**答案**：`smol_str` 只为 `char` 提供了 `FromIterator<char>`，没有 `From<char>`，所以必须经 `from_iter(iter::once(c))`。这也是 newtype 转发层的现实：底层的 API 缺口会原样透传到上层。

**练习 2**：把一段 10 字节的 `String` 转成 `SharedString`，数据放在哪里？100 字节呢？

**答案**：10 字节 ≤ 23，走内联，字节复制进 24 字节结构体，原 `String` 堆缓冲被释放；100 字节 > 23，走堆形态，缓冲被接管为共享数据。两种情况下源 `String` 变量都不可再用。

**练习 3**：`From<&Arc<str>>` 与 `From<Arc<str>>` 转换后，原 `Arc` 的引用计数分别是多少变化？

**答案**：`From<Arc<str>>` 平移所有权，计数不变；`From<&Arc<str>>` 内部 `s.clone()` 使计数 +1，源 `Arc` 仍可用。

### 4.3 转出实现：`From<SharedString> for Arc<str>` / `String`

#### 4.3.1 概念说明

转出方向只有两个，都是**消费整个 `SharedString`**。为什么恰恰是这两个？

- **`Arc<str>`**：当文本要长期保存、跨线程传递或存入缓存时，`Arc<str>` 是 Rust 生态的「通用货币」。`SharedString` 内部本就可能持有一个 `Arc`，转换常常是零成本的。
- **`String`**：当文本要传给需要**可变、独占**字符串的 API（如某些第三方库）时必须转出。这一步无法共享——`String` 的语义决定了必须独占缓冲，因此必然分配 + 拷贝。

两个 impl 都写 `text.0.into()`：先由 `.0` 解开 newtype 拿到 `SmolStr`（消费结构体），再走 `SmolStr → Arc<str>` / `SmolStr → String` 的转换链——**newtype 的转发热就是这么一行**。

#### 4.3.2 核心流程

```text
SharedString ──解开 .0──▶ SmolStr ──into()──▶ 目标类型

SmolStr → Arc<str>：
  ├── 堆形态  ──▶ 转移/克隆 Arc 句柄，数据不动（零拷贝）
  └── 内联/静态形态 ──▶ 一次性堆分配，拷贝 ≤ 23 字节（静态串按实际长度）

SmolStr → String：
  └── 任何形态 ──▶ 分配 String 缓冲 + 拷贝全部字节（独占语义，无共享可言）
```

#### 4.3.3 源码精读

[crates/gpui_shared_string/gpui_shared_string.rs:180-185](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L180-L185) —— 转出为 `Arc<str>`：`text.0.into()` 一行完成「解包 + 转发」；`#[inline(always)]` 保证这层转发在机器码层面消失。

[crates/gpui_shared_string/gpui_shared_string.rs:187-192](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L187-L192) —— 转出为 `String`：同样的转发写法。转出后原 `SharedString` 已被消费，这正是「独占缓冲」的代价。

真实用例可参考 text 示例里的状态更新：

[crates/gpui/examples/text.rs:311](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/text.rs#L311) —— `this.font_family = SharedString::new(new_family)`：从 `&'static str` 重新构造视图状态。注意此处源是运行时从 `FONT_FAMILIES` 数组中挑选的 `&'static str`（见 [crates/gpui/examples/text.rs:304-309](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/text.rs#L304-L309)），旧的 `SharedString` 被整体替换后由引用计数自动回收。

#### 4.3.4 代码实践

**实践目标**：用 `Arc::strong_count` 和 `Arc::ptr_eq` 证明「`Arc<str>` 转入再转出，数据一个字节都没动」。

**操作步骤**（示例代码，待本地验证）：

```rust
use std::sync::Arc;
use gpui_shared_string::SharedString;

#[test]
fn arc_roundtrip_is_zero_copy() {
    // 故意用 > 23 字节的字符串，确保走堆形态（Arc 被保留）
    let original: Arc<str> = Arc::from("a sufficiently long string to live on the heap");

    assert_eq!(Arc::strong_count(&original), 1);
    let shared: SharedString = original.clone().into(); // 计数 1 → 2，句柄平移
    assert_eq!(Arc::strong_count(&original), 2);

    let roundtrip: Arc<str> = shared.into(); // 消费 shared，句柄回到 Arc 形态
    assert_eq!(Arc::strong_count(&original), 2); // original + roundtrip
    assert!(Arc::ptr_eq(&original, &roundtrip)); // 数据未移动、未拷贝
}
```

**需要观察的现象**：三处断言全部通过——计数如注释所示变化，且往返后的 `Arc` 与原始 `Arc` 指向同一块内存。

**预期结果**：`ptr_eq` 成立说明 `Arc<str> → SharedString → Arc<str>` 的往返在堆形态下是纯句柄操作。作为对照，可再试一段 ≤ 23 字节的短串：往返后 `ptr_eq` 将**不成立**（内联形态转出 `Arc<str>` 必须新分配）。

#### 4.3.5 小练习与答案

**练习 1**：`From<SharedString> for String` 为什么不可能零拷贝？

**答案**：`String` 拥有独占的可增长堆缓冲，且必须保证后续 `push_str` 等修改不被其他所有者观察到；共享数据（引用计数 > 1 或静态区）无法直接充当，只能新建缓冲并拷贝。

**练习 2**：`let s: String = shared.into();` 之后还能使用 `shared` 吗？想保留原值怎么办？

**答案**：不能，`shared` 已被 move。保留方案：`String::from(shared.as_str())`（借用 + 新分配）或先 `shared.clone()` 再转换（克隆本身 O(1)）。

### 4.4 API 设计视角：`impl Into<SharedString>` 边界与 `const ELLIPSIS`

#### 4.4.1 概念说明

前面 12 个 `From` 是「弹药」，这一节看 gpui 如何「开枪」。两个代表性设计：

1. **`font_family(impl Into<SharedString>)`**：把参数边界定为 `Into<SharedString>` 而不是具体类型，等于向调用方声明「字符串怎么给都行」——字面量 `&str`、动态 `String`、现成的 `SharedString`、甚至 `&SharedString` 和 `char`，13 个方向全部直接可用，调用侧零适配代码。
2. **`const ELLIPSIS: SharedString = SharedString::new_static("…")`**：在 `const` 上下文构造 `SharedString`。只有 `new_static` 是 `const fn`（u1-l2 讲过），`"...".into()` 不是 const，写不了——这就是 `new_static` 存在的第二个理由（第一个是零分配）。

#### 4.4.2 核心流程

```text
调用 font_family("Zed Plex Mono")
   │ "Zed Plex Mono" : &str
   │ &str 满足 Into<SharedString>（由 From<&str> 反射得到）
   ▼
family_name.into()  ──▶  SharedString（内联，13 字节）
   ▼
self.text_style().font_family = Some(...)
```

#### 4.4.3 源码精读

[crates/gpui/src/styled.rs:707-711](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/styled.rs#L707-L711) —— `font_family` 的完整签名与实现：参数 `family_name: impl Into<SharedString>`，函数体第一句就 `.into()` 归一化。这是 zed 里接收字符串的 UI API 的标准姿势。

[crates/gpui/src/styled.rs:13](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/styled.rs#L13) —— `const ELLIPSIS` 的定义：省略号字符 `"…"`（3 字节 UTF-8，内联形态），编译期就住进二进制的只读数据段。

[crates/gpui/src/styled.rs:87-108](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/styled.rs#L87-L108) —— `text_ellipsis` / `text_ellipsis_start` / `text_ellipsis_middle` 三个方法每次调用都克隆一次 `ELLIPSIS` 塞进 `TextOverflow`。因为克隆是 O(1)（内联 24 字节复制），这里完全不需要担心重复构造的开销——这正是「克隆便宜」红利在 API 内部的体现。

[crates/gpui/examples/text.rs:294-296](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/text.rs#L294-L296) —— 真实调用：`.font_family(self.font_family.clone())` 传入的就是 `SharedString` 本体，走的是表格第 13 行的**恒等转换**（标准库 `impl<T> From<T> for T`）——`.clone()` 产生 O(1) 副本，`Into` 边界原样放行。这一个调用点同时用到了「克隆便宜」和「恒等 From」两件事。

#### 4.4.4 代码实践

**实践目标**：模仿 `font_family` 写一个 `impl Into<SharedString>` 边界的函数，验证多种来源都能直接传入。

**操作步骤**（示例代码，待本地验证）：

```rust
use gpui_shared_string::SharedString;

struct Title {
    text: SharedString,
}

impl Title {
    // 模仿 styled.rs 的 font_family：边界用 Into，进来立刻归一化
    fn set_text(&mut self, new_text: impl Into<SharedString>) {
        self.text = new_text.into();
    }
}

#[test]
fn title_accepts_many_sources() {
    let mut title = Title { text: SharedString::default() };

    title.set_text("静态字面量");                        // &str   → 方向 1
    title.set_text(format!("计数: {}", 42));              // String → 方向 5（转移）
    title.set_text('★');                                 // char   → 方向 4
    let owned = SharedString::new("现成的");
    title.set_text(owned.clone());                        // SharedString → 方向 13（恒等）
    title.set_text(&owned);                               // &SharedString → 方向 10

    assert_eq!(title.text.as_str(), "现成的");
}
```

**需要观察的现象**：五种来源、五种不同类型，同一行 `new_text.into()` 全部通过编译且行为正确。

**预期结果**：测试通过。再试着传入 `&String`、`Box<str>`、`Cow`，依然直接可用——都是同一张转换矩阵里的方向。

#### 4.4.5 小练习与答案

**练习 1**：把 L13 改成 `const ELLIPSIS: SharedString = "...".into();` 能编译吗？为什么？

**答案**：不能。`into` 是运行时方法调用，不是 `const fn`；`const` 上下文只允许 const fn 与编译期常量操作。这正是 `new_static` 被设计成 `const fn` 的动机之一。

**练习 2**：`font_family` 为什么不直接收 `&str`，让调用方自己 `.into()`？

**答案**：收 `impl Into<SharedString>` 把转换成本放在**最合适的一方**：持有 `String` 的调用方可以直接转移所有权（省一次拷贝），持有字面量的一方零成本内联。若收 `&str`，所有 `String` 调用方都被迫先借用再被内部拷贝一次，多出一次分配。

**练习 3**：`styled.rs` 里三个 `text_ellipsis*` 方法每调用一次就克隆一次 `ELLIPSIS`，要不要优化成 `&'static SharedString` 或每次 `new_static`？

**答案**：不需要。克隆 `ELLIPSIS` 只是复制 24 字节结构体（内联形态、无堆参与），与引用一个全局值的开销同数量级；而 `new_static` 每次构造同样廉价但让类型变成 `&'static` 后反而增加解引用层级。现有写法在简单与性能之间已是最优平衡。

## 5. 综合实践

**任务**：为转换矩阵的 **13 个方向**（12 个手写 `From` + 1 个恒等转换）各写一个单元测试，构成一份「语义备忘录」；每条断言旁用注释标注该转换的所有权与分配语义。这正是规格中要求的完整实践。

**项目骨架**（示例代码；`gpui_shared_string` 的传递依赖 smol_str / serde / schemars 需从 crates.io 拉取，离线环境需先行 vendor）：

```text
shared-string-lab/
├── Cargo.toml
└── src/
    └── lib.rs
```

```toml
# Cargo.toml（示例代码）
[package]
name = "shared-string-lab"
version = "0.1.0"
edition = "2021"

[dependencies]
gpui_shared_string = { path = "../zed/crates/gpui_shared_string" }
```

```rust
// src/lib.rs（示例代码，待本地验证）
use std::{borrow::Cow, sync::Arc};
use gpui_shared_string::SharedString;

// ---------- 方向 10：&SharedString → SharedString ----------
#[test]
fn t10_from_ref_shared_string() {
    let source = SharedString::new("clone me");
    let copied = SharedString::from(&source); // 借用源；内联复制，无堆分配
    assert_eq!(copied, source);               // 源仍可用：所有权未转移
}

// ---------- 方向 1：&str ----------
#[test]
fn t01_from_str() {
    let s = SharedString::from("literal");    // 8 字节：内联，零堆分配
    assert_eq!(s.as_str(), "literal");        // 借用构造，源仍可用
}

// ---------- 方向 2：&mut str ----------
#[test]
fn t02_from_mut_str() {
    let mut buf = String::from("mutable");
    let s = SharedString::from(buf.as_mut_str()); // 实现同 &str；仅服务泛型边界
    assert_eq!(s, "mutable");
}

// ---------- 方向 3：&String ----------
#[test]
fn t03_from_ref_string() {
    let owned = String::from("borrowed only");
    let s = SharedString::from(&owned);       // 借用 + 拷贝，owned 未 move
    assert_eq!(s.as_str(), owned.as_str());
}

// ---------- 方向 5：String ----------
#[test]
fn t05_from_string() {
    let s = SharedString::from(String::from("consumed"));
    // 短串内联拷贝；长串接管缓冲。源 String 已被 move，不可再用
    assert_eq!(s, "consumed");
}

// ---------- 方向 4：char ----------
#[test]
fn t04_from_char() {
    let s = SharedString::from('中');          // 3 字节 UTF-8，必然内联零分配
    assert_eq!(s.as_str(), "中");
}

// ---------- 方向 6：Box<str> ----------
#[test]
fn t06_from_box_str() {
    let boxed: Box<str> = String::from("boxed").into_boxed_str();
    let s = SharedString::from(boxed);         // 转移：堆缓冲被接管为共享
    assert_eq!(s, "boxed");
}

// ---------- 方向 7：Arc<str> ----------
#[test]
fn t07_from_arc_str() {
    let arc: Arc<str> = Arc::from("a long enough arc-backed string ...");
    let before = Arc::strong_count(&arc);
    let s = SharedString::from(arc);           // 句柄平移：零拷贝，计数不变
    let _ = before;
    assert_eq!(s.as_str(), "a long enough arc-backed string ...");
}

// ---------- 方向 8：&Arc<str> ----------
#[test]
fn t08_from_ref_arc_str() {
    let arc: Arc<str> = Arc::from("shared with others .........");
    let s = SharedString::from(&arc);          // clone 句柄：计数 +1，零数据拷贝
    assert_eq!(Arc::strong_count(&arc), 2);    // arc 仍可用（借用）
    assert_eq!(s.as_str(), &*arc);
}

// ---------- 方向 9：Cow<'a, str> ----------
#[test]
fn t09_from_cow() {
    let b = SharedString::from(Cow::Borrowed("cow borrowed")); // 同方向 1
    let o = SharedString::from(Cow::<str>::Owned(String::from("cow owned"))); // 同方向 5
    assert_eq!((b.as_str(), o.as_str()), ("cow borrowed", "cow owned"));
}

// ---------- 方向 11：SharedString → Arc<str> ----------
#[test]
fn t11_into_arc_str() {
    let source: Arc<str> = Arc::from("heap roundtrip payload ......");
    let shared: SharedString = source.clone().into();
    let out: Arc<str> = shared.into();         // 堆形态：转移句柄，数据不动
    assert!(Arc::ptr_eq(&source, &out));       // 零拷贝的直接证据
}

// ---------- 方向 12：SharedString → String ----------
#[test]
fn t12_into_string() {
    let shared = SharedString::new("exclusive copy");
    let text: String = shared.into();          // 消费源；必然新分配 + 拷贝
    assert_eq!(text, "exclusive copy");
}

// ---------- 方向 13：SharedString → SharedString（标准库恒等 From）----------
#[test]
fn t13_identity() {
    let original = SharedString::new("same value");
    let moved = SharedString::from(original);  // blanket impl<T> From<T> for T：纯 move
    assert_eq!(moved, "same value");
}
```

**运行方式**：在 `shared-string-lab` 目录执行 `cargo test`。

**预期结果**：13 个测试全部通过（待本地验证）。完成后建议做两个延伸动作加深记忆：

1. 把方向 7 的断言改为 `assert_eq!(Arc::strong_count(&arc), before);` 验证「转入不动计数」。
2. 对照第 4 节的总表逐条核对注释，确认每个方向你都能脱口说出「转移还是借用、分配还是零拷贝」。

## 6. 本讲小结

- `gpui_shared_string.rs` L110-L192 集中了 **12 个手写 `From`**：转入 10 个（借用型 `&str`/`&mut str`/`&String`/`&SharedString`/`&Arc<str>`，所有权型 `String`/`Box<str>`/`Arc<str>`/`Cow`/`char`），转出 2 个（`Arc<str>`/`String`）；加上标准库恒等 `From<T> for T` 共 13 个可用方向。
- 所有实现都是「解包 + 转发给 `SmolStr`」的一行式转发，配合 `#[inline]` / `#[inline(always)]` 让包装层在机器码层面消失。
- 借用型来源必然复制字节（短串内联零分配、长串堆分配）；所有权型来源尽量接管（`Arc<str>` 零拷贝、`String`/`Box<str>` 接管缓冲）；转出 `String` 必然分配 + 拷贝，转出 `Arc<str>` 在堆形态下零拷贝（可用 `Arc::ptr_eq` 证明）。
- `&mut str`、`&String` 这些「与 `&str` 实现体相同」的 impl 存在的唯一理由：**泛型边界 `impl Into<SharedString>` 处没有自动解引用强转**，每种来源都要专属坑位。
- `font_family(impl Into<SharedString>)` 是这张矩阵的受益者：调用方随便给什么字符串类型都能直接传入；`const ELLIPSIS` 则展示了 `new_static` 作为 `const fn` 的不可替代性。

## 7. 下一步学习建议

转换矩阵回答了「数据怎么进出」。下一讲 **u2-l4《相等、排序与哈希：跨类型比较》** 将回答「进来的数据如何比较」：精读 L86-L108 的四个手写跨类型 `PartialEq`（与 `String`、`str`、`&str` 的多方向相等）以及 derive 出的 `Eq` / `Ord` / `Hash`，并理解它们与上一讲 `Borrow<str>` 契约的配合——那正是 `HashMap` 跨类型查键能正确工作的另一半前提。建议先自己读一遍 L86-L108，带着「为什么 `PartialEq` 要写四个方向、`Eq` 却只 derive 一次」这个问题进入下一讲。
