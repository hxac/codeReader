# newtype 封装与 SmolStr 内部机制

## 1. 本讲目标

上一讲（u1-l2）我们掌握了 `SharedString` 的「用法」：三种构造方式、`as_str()` 读取、`.into()` 惯用法，以及「克隆很便宜」这一结论。本讲往下钻一层，回答三个「为什么」：

1. 为什么源码写成 `pub struct SharedString(SmolStr)`——用一个**私有字段**把 `SmolStr` 包起来，而不是直接暴露 `SmolStr` 或干脆用 `Arc<str>`？这就是 **newtype 封装**的价值。
2. 为什么克隆一个 `SharedString` 不需要复制字符串内容？它内部到底怎么存数据？答案是 `SmolStr` 的**内联缓冲 / 堆共享双模式存储**。
3. 相比 `String` 的深拷贝克隆，O(1) 克隆在 Zed 这种每帧重建 UI 的场景里到底省了多少？我们用**成本对比**和一次真实的基准测试来量化。

学完本讲，你应该能读懂这一行代码背后的全部设计决策：

```rust
#[derive(Eq, PartialEq, PartialOrd, Ord, Hash, Clone)]
pub struct SharedString(SmolStr);
```

并且能独立复刻这种「最小 newtype + 廉价克隆」的模式。

## 2. 前置知识

### 2.1 元组结构体与 newtype 模式

Rust 的结构体有三种写法：命名字段结构体、元组结构体、单元结构体。**元组结构体**（tuple struct）形如 `struct Point(i32, i32)`，字段没有名字，用 `.0`、`.1` 访问。

**newtype 模式**是元组结构体的一个特例：只包一个字段，用来「基于现有类型造一个新类型」。本讲的 `SharedString(SmolStr)` 就是典型——`SharedString` 和 `SmolStr` 在数据布局上完全一样，但在类型系统里是两个不同类型。关键字段默认是**私有的**（没有 `pub`），外部代码无法直接访问 `.0`，这就是封装的起点。

### 2.2 derive：让编译器代写 trait 实现

`#[derive(...)]` 会让编译器为结构体自动生成所列 trait 的实现。对只有一个字段的元组结构体，生成的实现通常是**委托给内部字段**：`#[derive(PartialEq)]` 生成的 `eq` 就是拿 `self.0` 和 `other.0` 比较。理解「derive → 委托内层 → 内层按内容生效」这条委托链，是读懂本讲的前提。

### 2.3 栈内联与堆分配

- **栈**：函数调用帧上的内存，分配/释放几乎零成本（移动栈指针），但大小必须在编译期确定。
- **堆**：由分配器管理的内存，`String` 的字符数据就放在堆上；每次分配和释放都要走分配器逻辑，成本远高于栈，还可能造成内存碎片。

「把短字符串直接塞进结构体自身的那块固定内存」称为**内联（inline）存储**，是字符串优化的常见手段（C++ 的 SSO、Rust 各种 small-string 库都是这个思路）。

### 2.4 引用计数与共享所有权

`String` 拥有独占的所有权——克隆它必须深拷贝。而 `Arc<T>`（Atomically Reference Counted，原子引用计数指针）允许**多个所有者共享同一块堆内存**：克隆 `Arc` 只是复制指针并把计数加一（原子操作），不复制数据；当最后一个 `Arc` 被释放、计数归零时，内存才被回收。`Arc<str>` 就是「堆上一段被共享的字符串」。

### 2.5 复杂度记号

- \( O(1) \)：常数时间，**与输入规模无关**（不代表「快」，只代表「不随 n 增长」）。
- \( \Theta(n) \)：与输入规模成正比。克隆一个 n 字节的 `String` 要逐字节复制 n 次，就是 \( \Theta(n) \)。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [gpui_shared_string.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs) | crate 全部源码，共 212 行 | 第 11–15 行的文档注释、derive 与结构体定义；第 25–40 行的三个固有方法 |
| [Cargo.toml](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/Cargo.toml) | crate 清单 | `smol_str = "0.3.6"`——唯一一个直接写版本的依赖 |
| [smol_str 0.3.6 官方文档（docs.rs，外部链接）](https://docs.rs/smol_str/0.3.6/smol_str/struct.SmolStr.html) | 底层库的行为依据 | 24 字节固定大小、23 字节内联阈值、`Clone` 为 O(1) |

> 说明：`smol_str` 是 crates.io 上的第三方 crate，不在 zed 仓库内，所以本讲引用它的官方文档而不是仓库永久链接。涉及它的一切行为描述均以该文档为准。

## 4. 核心概念与源码讲解

本讲的三个最小模块依次回答三个问题：**封装了什么**（4.1）、**内部怎么存**（4.2）、**克隆为什么便宜**（4.3）。

### 4.1 结构体定义与六个 derive 的逐个含义

#### 4.1.1 概念说明

先看这行定义：

```rust
pub struct SharedString(SmolStr);
```

这行代码做了三件事：

1. **造了一个新类型**：`SharedString` 与 `SmolStr` 数据布局相同、但类型不同，不能互换——调用方永远要显式地「进入」这个抽象。
2. **把字段藏起来**：`(SmolStr)` 前没有 `pub`，字段 `.0` 对 crate 外是私有的。外部唯一的内容出口是 `as_str()`、`Deref` 等受控接口。
3. **把实现细节钉在文档里，又留了退路**：文档注释里那句 "currently backed by a [`SmolStr`]"（当前由 SmolStr 支撑）是关键信号——**"currently" 意味着底层表示将来可能换掉**，而只要字段私有、API 面受控，更换实现就不会破坏任何下游代码。

**为什么不直接用 `SmolStr`？** 如果 `type SharedString = SmolStr;`（类型别名）或把字段设为 `pub`，那么 `SmolStr` 的全部公开方法（`new_inline`、`is_heap_allocated`、`SmolStrBuilder` 等）都会成为 `SharedString` 公共 API 的一部分。一旦下游代码用了它们，未来想换底层实现就被锁死了。封装让公共 API 只有三件事：**构造、读取、克隆**。

**为什么不直接用 `Arc<str>`？** 对比一下：

| 维度 | `Arc<str>` | `SmolStr`（SharedString 底层） |
|---|---|---|
| 短字符串 | 也要堆分配（构造 `Arc<str>` 会把数据拷到堆上） | ≤23 字节内联，零堆分配 |
| 静态字面量 | 无法在编译期构造（`Arc::new` 不是 `const fn`） | `new_static` 是 `const fn` |
| 克隆 | 原子计数 +1（要碰指针指向的内存） | 内联变体只复制结构体自身的 24 字节 |

`SmolStr` 相当于「`&'static str`（静态）、内联缓冲（短）、`Arc<str>`（长）」三种表示的合体——这正是 SharedString 文档注释所说的 "an abstraction over an `Arc<str>` and `&'static str`"。

#### 4.1.2 核心流程

六个 derive 各自带来的能力一览：

| derive | 生成的 trait | 带来的能力 | 委托链终点 |
|---|---|---|---|
| `Clone` | `Clone` | `.clone()`，成本由内层决定 → O(1) | `SmolStr::clone`（24 字节复制或计数 +1） |
| `PartialEq` | `PartialEq<SharedString>` | 同类型 `==` / `!=` | `SmolStr` 的相等 → 按 `str` 内容比较 |
| `Eq` | `Eq` | 标记相等关系是全等的（自反、对称、传递） | 空标记 trait，无方法 |
| `PartialOrd` | `PartialOrd` | `<`、`>` 等部分排序 | `SmolStr` 的比较 → 字节字典序 |
| `Ord` | `Ord` | 全序，提供 `cmp`、`max`、`clamp` 等 | 同上 |
| `Hash` | `Hash` | 可被哈希（HashMap/HashSet 键的前提之一） | `SmolStr` 的哈希 → 按内容 |

derive 在单字段元组结构体上的展开，可以用下面这段伪代码理解（**示例代码**，仅示意编译器生成物的形状）：

```rust
// #[derive(PartialEq)] 大致展开为：
impl PartialEq for SharedString {
    fn eq(&self, other: &Self) -> bool {
        self.0 == other.0   // 委托给 SmolStr 的 PartialEq
    }
}
// 而 SmolStr 的 PartialEq 又按 str 内容比较——
// 于是整条委托链的终点是「字符串内容」。
```

三个要点：

- **委托链**：`SharedString 的 derive → SmolStr 的实现 → 按 str 内容/字典序生效`。所以相等、排序、哈希都只看文本内容，与字符串是内联存的还是堆存的无关（第 u2-l4 讲会专门验证这一点）。
- **`Eq` 与 `PartialEq` 的分工**：`PartialEq` 提供运算符；`Eq` 是无方法的标记 trait，向容器承诺「相等是全等关系」。`HashMap` 的键要求 `Eq + Hash` 同时成立。
- **故意不 derive 的东西**：没有 `Copy`（见练习 3）；`Default` 是手写的（`new_static("")`，明确「空串零分配」语义，而不是笼统地调用内层 default）；`Debug`/`Display` 也是手写转发（第 74–84 行）。

#### 4.1.3 源码精读

**① 文档注释——设计意图的第一手材料**

[gpui_shared_string.rs:L11-L13](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L11-L13)：这三行文档注释说明了 SharedString 的定位——「不可变、可在 GPUI 任务中廉价克隆的字符串，本质上是 `Arc<str>` 与 `&'static str` 的抽象，**当前**由 `SmolStr` 支撑」。注意 "currently" 这个词：它是封装可替换性的书面承诺。

**② 结构体定义与六个 derive**

[gpui_shared_string.rs:L14-L15](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L14-L15)：整行就是 4.1.1 分析的对象——`#[derive(Eq, PartialEq, PartialOrd, Ord, Hash, Clone)]` 加上私有字段 `(SmolStr)`。字段私有意味着 `self.0` 只有本 crate 内部能碰。

**③ 固有方法——封装内部如何访问 `.0`**

[gpui_shared_string.rs:L25-L40](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L25-L40)：三个固有方法是 crate 内部访问私有字段的唯一位置——构造时 `Self(SmolStr::...)` 包装（[L26-L29](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L26-L29) 的 `new_static` 与 [L31-L34](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L31-L34) 的 `new`），读取时 `&self.0` 借用（[L36-L39](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L36-L39) 的 `as_str`）。所有对外能力都是「薄薄一层转发」——这是 newtype 的典型形态：类型即全部创新，逻辑几乎为零。

**④ 手写的 Default**

[gpui_shared_string.rs:L56-L60](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L56-L60)：`Default` 实现为 `new_static("")`——空字符串走静态路径，**零分配**。若直接 derive `Default`，会调用 `SmolStr::default()`，结果也是空串，但手写版本把「空值不碰堆」的意图显式化。

**⑤ 依赖清单——封装的边界也体现在 Cargo.toml**

[Cargo.toml:L10-L13](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/Cargo.toml#L10-L13)：三个依赖中，`schemars` 与 `serde` 用 `.workspace = true` 继承版本，只有 `smol_str = "0.3.6"` 是唯一直接写版本的依赖——它就是本讲的主角。另外 [Cargo.toml:L7-L8](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/Cargo.toml#L7-L8) 的 `[lib] path = "gpui_shared_string.rs"` 让库根文件与 crate 同名（这是 zed 仓库的编码规范，u1-l1 讲过）。

#### 4.1.4 代码实践：亲眼看见「字段是私有的」

**实践目标**：验证 newtype 的封装真的存在——外部代码拿不到 `.0`；同时确认 `SharedString` 与 `String` 一样大。

**操作步骤**：

1. 复用上一讲（u1-l2）创建的练习项目（或新建一个），确保 `Cargo.toml` 里有：

   ```toml
   [dependencies]
   gpui_shared_string = { path = "/path/to/zed/crates/gpui_shared_string" }
   ```

2. 在 `src/main.rs` 写入（**示例代码**）：

   ```rust
   use gpui_shared_string::SharedString;
   use std::mem::size_of;

   fn main() {
       let s = SharedString::new("hello");

       // 第一处：尝试把私有字段抠出来
       let inner = s.0;

       // 第二处：比较大小（64 位平台上两者都应是 24 字节）
       assert_eq!(size_of::<SharedString>(), size_of::<String>());
       println!("size_of::<SharedString>() = {}", size_of::<SharedString>());
       println!("{:?}", inner);
   }
   ```

3. 运行 `cargo build`，观察编译错误。
4. **删除 `let inner = s.0;` 和 `println!("{:?}", inner);` 两行**，再 `cargo run`。

**需要观察的现象**：

- 第一次编译失败，错误形如 `error[E0616]: field \`0\` of struct \`SharedString\` is private`（具体措辞待本地验证）。
- 删除后运行成功，打印出 `size_of::<SharedString>() = 24`（在 64 位平台上；这与 smol_str 文档承诺的 `size_of::<SmolStr>() == 24 == size_of::<String>()` 一致——newtype 不增加任何体积）。

**预期结果**：封装由编译器强制执行，而不是靠约定；newtype 是真正的「零成本抽象」——类型多了一层，内存一字节不多。

#### 4.1.5 小练习与答案

**练习 1**：如果把定义改成 `pub struct SharedString(pub SmolStr)`，会立刻失去哪两个好处？

**答案**：① 失去 API 面控制——`SmolStr` 的全部公开方法（`new_inline`、`is_heap_allocated`、builder 等）自动成为公共 API，文档承诺的「字符串抽象」名存实亡；② 失去更换底层实现的自由——一旦下游直接依赖 `SmolStr` 类型本身（例如把它存进自己的结构体），将来把底层换成别的实现就是破坏性变更。

**练习 2**：六个 derive 中，哪几个让 `SharedString` 能作为 `HashMap` 的键？`BTreeMap` 呢？

**答案**：`HashMap` 的键需要 `Eq + Hash`（`Eq` 又要求 `PartialEq`），所以是 `Hash`、`Eq`、`PartialEq` 三个；`BTreeMap` 的键需要 `Ord`（其排序比较依赖 `PartialOrd` 提供的运算符语义），所以是 `Ord`、`PartialOrd`。

**练习 3**：克隆这么便宜，为什么**不**顺手 `derive(Copy)`？

**答案**：`Copy` 的前提是「按位复制即安全」且所有字段均为 `Copy`，而 `SmolStr` 不是 `Copy`：堆变体含引用计数，若允许隐式按位复制，多个副本会各自认为自己是最后一个所有者，导致双重释放。即便内联变体理论上可 `Copy`，类型统一实现 `Copy` 也会让复制无处不在、成本不可见（隐式发生在赋值和传参中）。保持 `Clone` 让每次复制都是显式的 `.clone()` 调用。

### 4.2 SmolStr 的双模式存储：内联缓冲 vs 堆共享

#### 4.2.1 概念说明

封装的内层是 `SmolStr`。要理解克隆为什么便宜，得先知道它怎么存字符串。以下事实全部来自 [smol_str 0.3.6 官方文档](https://docs.rs/smol_str/0.3.6/smol_str/struct.SmolStr.html)：

1. **固定大小**：\( size\_of::<SmolStr>() = 24 \) 字节（64 位平台），恰好等于 `size_of::<String>()`——`String` 在栈上是「指针 + 容量 + 长度」三个机器字，字符数据在堆上；`SmolStr` 的 24 字节则**本身就是存储**。
2. **内联条件**：满足以下任一条件的字符串存在结构体自身内部（栈内联，零堆分配）：
   - 长度 **≤ 23 字节**；
   - 长度超过 23 字节，但是 `WS` 的子串——`WS` 指「32 个换行符后跟 128 个空格」组成的字符串，即**形如“若干连续换行 + 若干连续空格”**的文本。
3. **堆共享**：不满足上述条件的字符串在堆上分配，多个克隆通过**引用计数**共享同一块内存（smol_str 提供与 `Arc<str>` 的双向 `From` 转换；SharedString 的文档注释也自述为 "an abstraction over an `Arc<str>` and `&'static str`"）。
4. **静态**：`SmolStr::new_static(&'static str)` 永不分配（它是 `const fn`）。
5. **不可变**：与 `String` 不同，`SmolStr` 没有任何修改方法——不可变性正是廉价克隆的前提（大家共享同一份数据，谁都不能改）。

为什么有那个奇怪的 `WS` 特例？smol_str 的作者（rust-analyzer 的作者 matklad）在文档里写明：这个库的首要场景是**存放编程语言的词法 token**，而源代码里「连续换行后跟连续空格」正是**缩进**的形态，出现频率极高，值得特判免堆分配。Zed 是编辑器，文本里到处是缩进，这个特例对它同样受益。

#### 4.2.2 核心流程

构造 `SmolStr::new(s)` 时的存储决策（伪代码）：

```text
输入 s
├── s.len() ≤ 23 字节 ──────────→ 内联：把字节写进结构体自带的 24 字节缓冲
├── s 是 "连续换行+连续空格" ────→ 内联（WS 特例，为源码缩进设计）
└── 其他 ──────────────────────→ 堆：分配一块内存存内容，结构体内放指针，
                                    克隆共享、引用计数管理生命周期
```

克隆时的成本（独立公式）：

\[
T_{clone} =
\begin{cases}
\text{复制固定 } 24 \text{ 字节缓冲（与内容长度无关）} & \text{内联变体} \\\\
\text{原子引用计数 } +1 & \text{堆变体}
\end{cases}
\quad\Rightarrow\quad T_{clone} = O(1)
\]

两种变体的克隆成本都与字符串长度 \( n \) 无关，因此是 \( O(1) \)。注意 \( O(1) \) 只说「不随 n 增长」：内联克隆是一条 24 字节 memcpy，堆克隆是一次原子加——两者都是常数，通常内联克隆更快（原子操作要对缓存行做独占访问）。

#### 4.2.3 源码精读

**① 构造路径如何选择存储模式**

[gpui_shared_string.rs:L31-L34](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L31-L34)：`new` 直接调用 `SmolStr::new(str)`，**内联还是进堆的判断完全交给 SmolStr**——SharedString 自己不关心存储细节，这正是封装分工清晰的体现。

**② 静态路径：编译期构造、永不分配**

[gpui_shared_string.rs:L26-L29](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L26-L29)：`new_static` 是 `const fn`（上一讲讲过它的用法），内部调用 `SmolStr::new_static`——后者按文档「never allocates」。配合 `Default`（[L56-L60](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L56-L60)）实现空串零分配，构成了「常量与空值完全不碰堆」的静态路径。

**③ 读取路径：借用，不拷贝**

[gpui_shared_string.rs:L36-L39](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L36-L39)：`as_str` 返回 `&self.0`——无论内联还是堆，都只是返回一个指向既有数据的 `&str` 借用，**零拷贝、零分配**。内联变体借用结构体自身，堆变体借用堆块。

**④ 外部依据**

存储策略的三条关键事实（24 字节、23 字节阈值、`Clone` 为 `O(1)`、`WS` 特例）见 [docs.rs：SmolStr in smol_str 0.3.6](https://docs.rs/smol_str/0.3.6/smol_str/struct.SmolStr.html)。SharedString 未暴露 `is_heap_allocated()`——你无法从 `SharedString` 上直接问「我在不在堆上」，这是 4.1 讲的封装在起作用：存储模式被视为实现细节。

#### 4.2.4 代码实践：用 is_heap_allocated 探明内联边界

`SharedString` 问不到存储模式，但我们可以在练习项目里**直接依赖 smol_str**，用它的 `is_heap_allocated()` 做边界实验（这是你自己的练习项目，不违反「不改源码」的约束）。

**实践目标**：亲手验证 23/24 字节边界与 `WS` 缩进特例。

**操作步骤**：

1. 在练习项目的 `Cargo.toml` 中加入（与 zed 同一版本系列）：

   ```toml
   [dependencies]
   smol_str = "0.3"
   ```

2. 写入 `src/bin/storage.rs`（**示例代码**）：

   ```rust
   use smol_str::SmolStr;

   fn main() {
       // 先写下你的预测，再看输出
       let cases: Vec<(&str, String)> = vec![
           ("10 字节字母", "x".repeat(10)),
           ("23 字节字母（边界内侧）", "x".repeat(23)),
           ("24 字节字母（边界外侧）", "x".repeat(24)),
           // 64 字节：32 个换行 + 32 个空格，是 WS（32 换行+128 空格）的子串
           ("缩进形态：换行+空格 64 字节", format!("{}{}", "\n".repeat(32), " ".repeat(32))),
           ("64 字节字母（对照）", "x".repeat(64)),
           // UTF-8 下每个汉字 3 字节：5 个汉字 = 15 字节
           ("中文 5 字", "你好世界呀".to_string()),
       ];

       for (name, text) in cases {
           let s = SmolStr::new(&text);
           println!(
               "{:<28} len={:>3} 字节  heap={}",
               name,
               text.len(),
               s.is_heap_allocated()
           );
       }
   }
   ```

3. 运行 `cargo run --bin storage`。

**需要观察的现象**：每行打印字符串长度与 `heap` 布尔值。

**预期结果**（待本地验证）：

| 用例 | 长度 | heap |
|---|---|---|
| 10 字节字母 | 10 | `false`（内联） |
| 23 字节字母 | 23 | `false`（边界内） |
| 24 字节字母 | 24 | `true`（过界进堆） |
| 缩进形态（换行+空格） | 64 | `false`（WS 特例，超过 23 仍内联） |
| 64 字节字母 | 64 | `true`（对照） |
| 中文 5 字 | 15 | `false`（按**字节**而非字符计数） |

最后一行特别值得注意：内联阈值按 **UTF-8 字节数**判断，与「几个字符」无关。

#### 4.2.5 小练习与答案

**练习 1**：`"你好，世界！"`（含中文标点共 6 个字符）会内联吗？

**答案**：会。UTF-8 中这 6 个字符每个占 3 字节，共 15 字节 ≤ 23，走内联。判断依据永远是字节数，不是字符数——Rust 字符串一律按字节度量长度。

**练习 2**：24 字节的结构体为什么最多内联 23 字节，那 1 字节去哪了？

**答案**：内联表示必须额外记录「有效数据有多长」以及区分内联/堆变体，至少要占掉 1 字节，所以纯数据至多 23 字节。具体如何编码（长度放哪、判别怎么区分）属于 smol_str 的内部实现细节，文档只承诺 23 字节阈值与 24 字节总大小。

**练习 3**：为什么值得为「换行+空格」这种形态专门设计 WS 特例？

**答案**：因为 smol_str 的设计场景是编程语言 token 存储，而源代码的缩进恰好就是「若干连续换行后跟若干连续空格」，长度常常超过 23 字节却没有其他信息量。特判让这类高频字符串免于堆分配。Zed 是代码编辑器，缓冲区、行内容、填充文本里充满缩进，天然受益。

### 4.3 String 深拷贝克隆与 SharedString O(1) 克隆的成本对比

#### 4.3.1 概念说明

现在把两边摆上台面对比。`String` 是**独占所有权、可变**的字符串：栈上 24 字节存「指针 + 容量 + 长度」，字符数据独占一块堆内存。因为独占，`clone` 别无选择——分配一块新堆内存，把 n 字节逐个复制过去：

\[
T_{String::clone}(n) = \Theta(n) \quad \text{（分配 + 逐字节复制）}
\]

`SharedString` 因为不可变，克隆只需「多登记一个所有者」：内联变体复制固定 24 字节，堆变体做一次原子计数加一：

\[
T_{SharedString::clone} \le 24\ \text{字节复制} \;\text{或}\; \text{一次原子加} \;=\; O(1)
\]

量级感受：把一个 100 字节的字符串克隆一百万次，`String` 要完成约 \( 10^6 \times 100\,\text{B} = 100\,\text{MB} \) 的堆分配加字节搬运；`SharedString` 总共只动 24 MB 的栈数据（内联情形）或一百万次原子加（堆情形）。

**为什么 Zed 特别在乎这件事？** GPUI 的渲染模型是：视图状态变化 → `cx.notify()` → 下一帧重新执行 `render()` → **整棵元素树从头构建**。文本是 UI 里最常见的数据，意味着字符串克隆发生在「每帧 × 每个文本元素」的量级上；此外实体间事件、异步任务回传结果也都在复制文本。选 `String` 的话，这些复制全是 \( \Theta(n) \) 加堆分配；选 `SharedString`，全部降到 \( O(1) \) 且不碰分配器。这就是 SharedString 文档注释第一句 "can be cheaply cloned in GPUI tasks" 的现实背景。

#### 4.3.2 核心流程

三种字符串一次克隆的完整流程对比：

```text
String::clone()
  1. 向分配器申请 n 字节堆内存        ← 分配器开销
  2. 逐字节复制源数据                  ← Θ(n)
  3. 栈上写入新的 指针/容量/长度
  4. Drop 时再走一次分配器释放          ← 分配器开销

SharedString::clone()（内联变体）
  1. 把自身 24 字节按位复制到新值      ← 一次定长 memcpy，无堆交互

SharedString::clone()（堆变体）
  1. 复制指针/长度等 24 字节头部
  2. 原子引用计数 +1                  ← 一次原子操作
  （Drop 时计数 −1，归零才真正释放堆内存）
```

能力与成本总表：

| 维度 | `String` | `SharedString`（内联） | `SharedString`（堆） |
|---|---|---|---|
| 克隆成本 | \( \Theta(n) \) + 两次分配器交互 | 24 字节复制 | 原子计数 +1 |
| 克隆是否碰堆 | 是 | 否 | 否（仅计数） |
| 可变性 | 可（`push_str` 等） | 否 | 否 |
| 栈上大小 | 24 字节 | 24 字节 | 24 字节 |
| 额外语义 | — | 数据就在值里 | 多个克隆共享同一堆块 |

#### 4.3.3 源码精读

**① 克隆能力的来源就是 derive**

[gpui_shared_string.rs:L14-L15](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L14-L15)：derive 列表里的 `Clone` 让 `SharedString` 获得 `.clone()`，实现完全委托给 `SmolStr::clone`——而后者在官方文档中被明确承诺为 **O(1)**。也就是说，SharedString「克隆便宜」这一性质不是自己写出来的，而是通过 newtype **继承**自底层库的保证；如果哪天换了底层（只要它同样 O(1)），derive 一行都不用改。

**② 文档注释把成本承诺写在第一行**

[gpui_shared_string.rs:L11-L13](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L11-L13)：第一句就是 "can be cheaply cloned in GPUI tasks"——克隆成本不是附带效果，而是这个类型的**核心设计目标**，所以放在文档最前面。

**③ 一个「克隆即转移」的惯用法**

[gpui_shared_string.rs:L110-L115](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L110-L115)：`impl From<&SharedString> for SharedString` 的实现就是 `s.clone()`。既然克隆是 O(1)，「从引用得到拥有值」和「复制」便合二为一——这种写法在 O(n) 克隆的类型上是反模式，在 SharedString 上却是惯用法。它也让泛型 API（`impl Into<SharedString>`）可以同时接受 `&SharedString` 和 `SharedString` 而无需两套代码。

#### 4.3.4 代码实践：百万次克隆基准测试（本讲核心实践）

**实践目标**：用真实数据量化 `String` 与 `SharedString` 的克隆差距，并验证「无论短字符串（内联）还是长字符串（堆），SharedString 克隆都便宜」。

**操作步骤**：

1. 在练习项目（依赖 `gpui_shared_string` 的 path 依赖，见 4.1.4）中新建 `tests/clone_bench.rs`（**示例代码**）：

   ```rust
   use std::hint::black_box;
   use std::time::{Duration, Instant};

   use gpui_shared_string::SharedString;

   /// 把 value 克隆 times 次，返回总耗时。
   /// black_box 防止编译器把克隆优化掉。
   fn bench_clone<T: Clone>(value: &T, times: usize) -> Duration {
       let start = Instant::now();
       for _ in 0..times {
           black_box(value.clone());
       }
       start.elapsed()
   }

   #[test]
   fn shared_string_clone_beats_string_clone() {
       const N: usize = 1_000_000;

       // 短字符串：10 字节。
       // - SharedString：≤23 字节走内联，克隆 = 复制固定 24 字节缓冲，
       //   与内容长度无关，也不碰堆。
       // - String：克隆 = 堆分配 + 逐字节复制 10 字节。
       let short_shared = SharedString::new("0123456789");
       let short_string = "0123456789".to_string();

       // 长字符串：100 字节。
       // - SharedString：>23 字节走堆共享，克隆 = 原子引用计数 +1，
       //   同样与内容长度无关。
       // - String：克隆 = 堆分配 + 逐字节复制 100 字节，
       //   一百万次共约 100MB 的分配与搬运。
       let long_text = "x".repeat(100);
       let long_shared = SharedString::new(&long_text);
       let long_string = long_text.clone();

       let t1 = bench_clone(&short_shared, N);
       let t2 = bench_clone(&short_string, N);
       let t3 = bench_clone(&long_shared, N);
       let t4 = bench_clone(&long_string, N);

       println!("短(10B)   SharedString: {:>12?}  String: {:>12?}", t1, t2);
       println!("长(100B)  SharedString: {:>12?}  String: {:>12?}", t3, t4);
       println!(
           "加速比  短: {:.1}x  长: {:.1}x",
           t2.as_secs_f64() / t1.as_secs_f64(),
           t4.as_secs_f64() / t3.as_secs_f64()
       );

       // 断言「明显更快」：取宽松的 2 倍阈值，避免计时抖动导致偶发失败。
       // 若在低速机器上仍偶发失败，把 N 增大一个数量级后重跑。
       assert!(t1 * 2 < t2, "短字符串克隆：SharedString 应明显快于 String");
       assert!(t3 * 2 < t4, "长字符串克隆：SharedString 应明显快于 String");
   }
   ```

   代码中的注释就是实践任务要求的解释：**两种长度的 SharedString 克隆都便宜，是因为成本封顶在「24 字节复制」（内联）或「一次原子加」（堆），都不随字符串长度增长；而 String 的克隆必须分配加逐字节复制，长度越长差距越大。**

2. 运行 `cargo test -- --nocapture`（debug 模式，差距最悬殊）。
3. 再运行 `cargo test --release -- --nocapture`（release 模式，看优化后差距是否依然存在）。

**需要观察的现象**：两组耗时与加速比打印；测试通过。

**预期结果**：SharedString 在两种长度下都明显快于 String（常见量级为几倍到几十倍，具体数值随机器、模式与分配器不同而异，**待本地验证**）。两个值得留意的观察点：

- 短字符串场景中 `String` 克隆只有 10 字节的复制量，但它**每次都要堆分配**——差距主要来自分配器，而非字节数；
- 长字符串场景中 `String` 的字节数放大到 100 字节，而 SharedString 的堆变体克隆成本**没有随之变化**（仍是一次原子加）——这正是 O(1) 的含义。

#### 4.3.5 小练习与答案

**练习 1**：克隆一个 100 字节的 `String` 一百万次，总共要复制多少字节的数据？`SharedString`（堆变体）呢？

**答案**：`String`：\( 10^6 \times 100\,\text{B} \approx 100\,\text{MB} \)，外加一百万次堆分配与释放；`SharedString` 堆变体：数据零复制，只有约 \( 10^6 \times 24\,\text{B} = 24\,\text{MB} \) 的头部复制加一百万次原子计数加一（实际被立即 Drop 后又减一，堆块始终只有一块）。

**练习 2**：为什么 `String::clone` 必须深拷贝，没有别的选择？

**答案**：`String` 拥有数据的独占所有权，且承诺可变。如果克隆共享底层内存，那么修改其中一个克隆就会影响另一个，违反独占语义；若改成写时复制又会让每次修改背负检查成本。深拷贝是「独占 + 可变」这两个承诺的必然代价。`SharedString` 之所以能逃开，是因为它**放弃了可变性**——不可变数据天然可以安全共享。

**练习 3**：内联克隆（24 字节 memcpy）和堆克隆（原子加）哪个更贵？为什么？

**答案**：通常原子加更贵。原子操作要对缓存行取得独占权（x86 上是 `lock` 前缀指令），在多核间协调；而 24 字节 memcpy 只是普通的内存写入。不过两者都是与 n 无关的常数，都属于 O(1)。极端高频克隆场景下，短的、适合内联的字符串是最理想的情况——这也是 SmolStr 把 23 字节以内的字符串全部内联的动机之一。

## 5. 综合实践

把本讲三个模块串成一个「SharedString 存储与成本全景实验」。在练习项目中新建 `tests/full_picture.rs`（**示例代码**），完成以下三步：

1. **边界扫描**：对长度为 0、10、23、24、100、1000 的字符串（再补一个「换行+空格」缩进形态）分别构造 `SmolStr`，打印 `is_heap_allocated()`，画出一张「长度 → 存储」的表格，标注 23/24 这条分界线。
2. **成本曲线**：用 4.3.4 的 `bench_clone` 对上述每个长度各克隆一百万次，分别记录 `String` 与 `SharedString` 的耗时，输出一张「长度 → 两者耗时 → 加速比」的表格。观察：`String` 的耗时应随长度近似线性增长，`SharedString` 的耗时应基本平坦（内联段与堆段可能在 23/24 边界处有细微台阶——堆克隆的原子加比内联 memcpy 略贵）。
3. **写下结论**：在文件末尾用注释回答——「如果我要给 GPUI 的某个高频重建的视图选一个文本字段类型，依据这张表我会怎么选？内联上限、堆共享、不可变性各扮演什么角色？」

通过标准：两张表与三条注释能自圆其说，断言（SharedString 各长度均不慢于 String 的 2 倍以上）全部通过。具体数值**待本地验证**。

## 6. 本讲小结

- `pub struct SharedString(SmolStr)` 是标准 newtype：私有字段 + 受控的三个固有方法，让「底层 currently 是 SmolStr」成为一个可以随时更换的实现细节，而公共 API（构造/读取/克隆）保持稳定。
- 六个 derive（`Eq`、`PartialEq`、`PartialOrd`、`Ord`、`Hash`、`Clone`）通过「derive → 委托 SmolStr → 按 str 内容生效」的委托链工作：相等、排序、哈希只看文本内容，`Clone` 则继承了 SmolStr 的 O(1) 保证。
- SmolStr 是 24 字节固定大小的双模式容器：≤23 字节（以及「连续换行+连续空格」的缩进特例）内联存储零堆分配，更长的字符串堆分配并由引用计数共享；`new_static` 永不分配。
- 克隆成本：`String` 是 \( \Theta(n) \) 深拷贝加堆分配；`SharedString` 封顶在 24 字节复制或一次原子加，即 \( O(1) \)，且两种变体都与长度无关——基准测试可以量化这一差距。
- 这不是微优化：GPUI 每帧重建元素树、实体与任务间高频传递文本，「每帧 × 每个文本元素」量级的克隆决定了 O(1) 是刚需；而廉价克隆的代价是**放弃可变性**——SharedString 没有任何修改方法。

## 7. 下一步学习建议

下一讲（u2-l2）《Deref、AsRef、Borrow：像 str 一样使用》将精读本讲反复提到的读取出口：`Deref<Target = str>` 如何让 `SharedString` 免写 `.as_str()` 就能调用 `str` 的全部方法、`AsRef<str>` 如何让它进入泛型字符串 API、`Borrow<str>` 如何配合本讲的 `Hash + Eq` 让 `HashMap<SharedString, V>` 支持 `&str` 跨类型查键。建议先自行重读 [gpui_shared_string.rs:L17-L23](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L17-L23)（Deref 实现，只有 7 行）和 [L62-L72](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L62-L72)（AsRef 与 Borrow），带着「这三个 trait 分别解决什么问题」去读效果最好。
