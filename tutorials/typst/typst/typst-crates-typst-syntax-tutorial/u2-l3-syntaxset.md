# SyntaxSet 位集与 syntax_set! 宏

## 1. 本讲目标

本讲聚焦 `src/set.rs` 这一个小而精的文件。学完后你应当能够：

- 说清 `SyntaxSet` 为什么用一个 `u128` 就能表示「一整套 `SyntaxKind`」，以及它为什么有「判别值必须 < 128」的硬限制。
- 读懂 `syntax_set!` 宏的展开过程，理解它如何借助 `const fn` 在编译期造出一个常量集合。
- 认识 `STMT`、`CODE_EXPR`、`ATOMIC_CODE_EXPR`、`PATTERN`、`ARG` 等预定义常量，并理解 parser 里 `p.at_set(...)` 这一行的真正含义。
- 明白 `SyntaxSet` 为何是 **crate 内部** 工具，而不在 `typst-syntax` 的公开 API 中。

本讲是 u2-l1（`SyntaxKind` 枚举全貌）的直接续篇：上一讲给的是「散落的单个 kind」，本讲给的是「把多个 kind 打包成一组的容器」。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，什么是位集（bitset）。** 假设你有 137 个种类（`SyntaxKind` 正好 137 个变体），想快速回答「当前这个 kind 是否属于某一组」。最朴素的办法是写一长串 `||`：

```rust
// 伪代码，仅示意
matches!(k, Plus | Minus | Star | Slash | And | Or)
```

当集合很大、且要在解析器的热路径上反复查询时，这种写法既啰嗦又慢。位集的思路是：给每个 kind 编一个号（就是它在枚举里的判别值 `0..136`），然后用一个整数的第 `i` 个比特位表示「编号为 `i` 的 kind 是否在集合里」。这样「是否包含」就退化成一次按位与 + 一次比较。

**第二，为什么用 `u128`。** Typst 的 `SyntaxKind` 有 137 个变体，需要 137 个比特位。`u64` 只有 64 位不够用，`u128` 有 128 位——但 128 < 137，所以仍然「差一点」。typst-syntax 的取舍是：**只允许判别值 < 128 的 kind 入集**，剩下的 9 个高位变体不允许加入（详见 4.1.3）。这是一个工程上「够用就好」的折中：高位那 9 个变体恰好都是 parser 产出的结构节点，不需要进任何预定义集合。

**第三，为什么强调「编译期」。** 解析器每读一个 token，都可能要问几十次「当前 token 在不在某个集合里」。如果集合能在编译期就构造好、存成只读常量，运行时就只剩零成本的一次位运算。本讲的两个主角——`const fn` 方法与 `syntax_set!` 宏——都是为了把构造开销挪到编译期。

## 3. 本讲源码地图

本讲只涉及两个文件，重点是前者：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `src/set.rs` | 定义 `SyntaxSet` 位集、`syntax_set!` 宏，以及一组预定义常量 | **唯一主角**，全讲围绕它 |
| `src/kind.rs` | `SyntaxKind` 枚举（u2-l1 已精读） | 提供「判别值」这一坐标轴 |
| `src/parser.rs` | 消费这些集合的解析器 | 提供「集合怎么被用」的真实场景 |
| `src/lib.rs` | crate 门面 | 解释 `SyntaxSet` 为何对 crate 外不可见 |

## 4. 核心概念与源码讲解

### 4.1 SyntaxSet 位集实现

#### 4.1.1 概念说明

`SyntaxSet` 回答的唯一问题是：**「给定的某个 `SyntaxKind`，是否属于这一组？」** 它是 parser 决策时最频繁的查询之一，例如「当前 token 能否开启一个表达式」「当前 token 是不是二元运算符」。

它的实现是一个「新类型（newtype）」：把一个 `u128` 包成结构体，所有操作都退化为对这个整数的按位运算。文件顶部注明它借鉴自 rust-analyzer 的 `TokenSet`：

[src/set.rs:1-3](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L1-L3) —— 注明设计源自 rust-analyzer 的 `TokenSet`。

#### 4.1.2 核心流程

设集合内部整数为 \( S \)，某 kind 的判别值为 \( i \)。则「编号为 \( i \) 的比特位」对应掩码：

\[
\text{bit}(i) = 1 \ll i
\]

四个核心操作都可写成一行位运算：

| 操作 | 语义 | 公式 |
| --- | --- | --- |
| `add(k)` | 把 \( k \) 加入集合 | \( S \gets S \,\lor\, \text{bit}(i) \) |
| `remove(k)` | 把 \( k \) 移出集合 | \( S \gets S \,\land\, \lnot\text{bit}(i) \) |
| `union(other)` | 并集 | \( S \gets S \,\lor\, S_{\text{other}} \) |
| `contains(k)` | 是否包含 \( k \) | \( (S \,\land\, \text{bit}(i)) \neq 0 \) |

因为 `add`/`remove` 都是「按值消费 `self` 再返回新的 `Self`」，所以可以链式调用：

```rust
// 示例代码：链式构造，非项目原码
let s = SyntaxSet::new()
    .add(SyntaxKind::Plus)
    .add(SyntaxKind::Minus)
    .union(some_other_set);
```

#### 4.1.3 源码精读

结构体本身只有一个字段，且派生了 `Copy/Clone/Default`——这意味着 `SyntaxSet` 像 `u128` 一样可以随意按值复制，调用 `at_set(set)` 时是按值传进去，零开销：

[src/set.rs:7-9](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L7-L9) —— `pub struct SyntaxSet(u128);`，一个 `u128` 就是整个集合。

`add` 把对应比特位置 1。注意它带 `assert!((kind as u8) < BITS)`，且整个方法是 `const fn`：

[src/set.rs:20-23](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L20-L23) —— `add` 用按位或置位，`BITS` 常量定义在 [src/set.rs:44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L44) 为 `128`。

> **关键点：这个 assert 是编译期护栏。** 因为 `add` 是 `const fn`，而下面的预定义集合都是 `const`，所以「往 const 集合里加一个判别值 ≥ 128 的 kind」会在**编译期**就触发 panic，根本编不过。运行期若用普通函数调用加上越界 kind，则会在运行期 panic。无论哪种，越界都被严格禁止。

`remove` 用「按位与上掩码的非」清位，同样带 assert：

[src/set.rs:28-31](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L28-L31) —— `remove` 用 `self.0 & !bit(kind)` 清除对应位。

`contains` 是热路径上最常调用的方法。它先做越界判断（`>= 128` 直接返回 `false`，不会越界移位），再做按位与：

[src/set.rs:39-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L39-L41) —— `contains` 的两步：先判 `(kind as u8) < BITS`，再判 `(self.0 & bit(kind)) != 0`。

掩码函数 `bit` 把 kind 右移成「只有一位是 1」的 `u128`：

[src/set.rs:46-48](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L46-L48) —— `const fn bit(kind) -> u128 { 1 << (kind as usize) }`。

**关于「9 个高位变体不能入集」。** `SyntaxKind` 以 `#[repr(u8)]` 紧凑表示、共 137 个变体（u2-l1 已讲），判别值为 `0..=136`。`u128` 只能容纳 `0..=127`，因此判别值在 `128..=136` 的 9 个变体（`ImportItems`、`ImportItemPath`、`RenamedImportItem`、`ModuleInclude`、`LoopBreak`、`LoopContinue`、`FuncReturn`、`Destructuring`、`DestructAssignment`）无法进入任何 `SyntaxSet`。这是一个刻意接受的限制：这 9 个都是 parser 产出的结构节点，而集合里装的是「lexer 产出的 token / 可开启某构造的 token」，本来就用不到它们。

#### 4.1.4 代码实践

**实践目标：** 在 crate 内部亲手用 `new().add().union()` 构造一个集合，并验证 `contains` 行为。

**为什么必须「在 crate 内部」：** 见 4.3.1——`SyntaxSet` 与这些常量都是 **crate 私有**，外部程序拿不到。所以本实践要在 `src/set.rs` 已有的测试模块里加一个测试函数。

**操作步骤：**

1. 打开 `src/set.rs`，定位到文件末尾的 `#[cfg(test)] mod tests`（见 [src/set.rs:156-167](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L156-L167)）。
2. 仿照已有的 `test_set`，新增一个测试（示例代码，非项目原有）：

```rust
// 示例代码：添加到 src/set.rs 的 #[cfg(test)] mod tests 中
#[test]
fn test_custom_union() {
    // 自定义「可开启表达式的 token 集合」
    let mine = SyntaxSet::new()
        .add(SyntaxKind::Ident)
        .add(SyntaxKind::Int)
        .union(set::UNARY_OP); // 借用预定义集合
    assert!(mine.contains(SyntaxKind::Ident));
    assert!(mine.contains(SyntaxKind::Plus));   // 来自 UNARY_OP
    assert!(!mine.contains(SyntaxKind::Star));  // Star 是二元运算符，不在内
}
```

3. 运行：

```bash
cargo test -p typst-syntax --lib set::tests
```

**需要观察的现象：** 三个断言全部通过；`UNARY_OP` 里的 `Plus` 被合并进来，而二元运算符 `Star` 没有被合并。

**预期结果：** `test_custom_union ... ok`。若你故意把 `add(SyntaxKind::Destructuring)`（判别值 135）写进去，编译会因 `const` 上下文里的 assert 失败而报错——亲手验证「9 个高位变体不能入集」。

> 本实践会修改 `src/set.rs` 的测试模块。如果你不想改动源码，也可只阅读下文 4.3 的预定义集合，并手动追踪 `contains` 结果，作为「源码阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1：** `contains` 为什么要先判断 `(kind as u8) < BITS`，再做按位与？去掉这一步会怎样？

**答案：** 若不先判越界，当 `kind` 判别值 ≥ 128 时，`1 << 137` 这样的移位在 `u128` 上是未定义/溢出行为（debug 下 panic、release 下回绕），会得到错误结果甚至崩溃。先判 `< BITS` 保证越界 kind 安全返回 `false`。

**练习 2：** `SyntaxSet` 派生了 `Copy`。如果它不派生 `Copy`，parser 里 `fn at_set(&self, set: SyntaxSet)` 这种按值传参的写法还能成立吗？为什么要按值传？

**答案：** 不派生 `Copy` 则按值传入会移动所有权，调用方就没法再用同一个集合常量了。按值传是因为 `SyntaxSet` 就是一个 `u128`（16 字节），复制比借引用还便宜，且让调用点写 `p.at_set(set::CODE_EXPR)` 非常清爽。

### 4.2 syntax_set! 宏

#### 4.2.1 概念说明

虽然可以用 `SyntaxSet::new().add(A).add(B).add(C)` 手工构造集合，但当集合有二三十个成员时（比如下面的 `ATOMIC_CODE_EXPR` 有 26 个），这种写法又长又容易漏。`syntax_set!` 宏的作用就是把这些 `.add(...)` 调用自动生成出来，让你只写一列 kind 名字。

它和 4.1 的 `const fn` 是天生一对：宏生成的代码仍是 `const` 上下文里的 `add` 链，于是最终产物是一个**编译期常量**。

#### 4.2.2 核心流程

宏的展开规则可以等价描述成下面这段伪代码：

```
输入：  syntax_set!(A, B, C)
展开为：{
            const SET: SyntaxSet = SyntaxSet::new()
                .add(SyntaxKind::A)
                .add(SyntaxKind::B)
                .add(SyntaxKind::C);
            SET
        }
```

也就是说，宏只是把「逗号分隔的标识符列表」翻译成「一连串 `.add(crate::SyntaxKind::标识符)`」，再返回这个临时常量 `SET`。它本身不做任何位运算——真正干活的是 `add`。

#### 4.2.3 源码精读

宏定义在 [src/set.rs:51-57](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L51-L57)：

```rust
macro_rules! syntax_set {
    ($($kind:ident),* $(,)?) => {{
        const SET: crate::set::SyntaxSet = crate::set::SyntaxSet::new()
            $(.add(crate::SyntaxKind:: $kind))*;
        SET
    }}
}
```

逐段解读：

- `$($kind:ident),*`：匹配「零个或多个标识符，用逗号分隔」。这就是你能写 `syntax_set!(Plus, Minus)` 或 `syntax_set!(End)` 甚至 `syntax_set!()`（空集）的原因。
- `$(,)?`：可选的尾逗号，方便多行书写。
- `$(.add(crate::SyntaxKind:: $kind))*`：对每个捕获的 `$kind`，生成一段 `.add(crate::SyntaxKind::<那个 kind>)`。
- 外层 `{{ ... }}`：这是一个**块表达式**，里面的 `const SET` 是块局部常量，最后把 `SET` 作为表达式的值返回。

宏导出行 [src/set.rs:59-60](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L59-L60) 用 `pub(crate) use syntax_set;` 把宏对 crate 内部可见。注意是 `pub(crate)`，不是 `pub`——外部 crate 用不到这个宏。

宏的真实使用场景在 parser.rs。三个解析入口都用它造「停止集合」`stop_set`，表示「遇到这些 token 就停止当前层级的解析」：

[src/parser.rs:15-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L15-L37) —— `parse` / `parse_code` / `parse_math` 分别用 `syntax_set!(End)` 作为最外层停止集合。

parser 里还有大量「就地构造、用一次即弃」的小集合，例如数学表达式里临时排除某个算符：

[src/parser.rs:353](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L353) —— `syntax_set!(Hat, Underscore).remove(op_kind)`，先造 `{Hat, Underscore}` 再用 `remove` 临时移除当前算符。这正是 4.1 里 `remove` 方法的用武之地。

#### 4.2.4 代码实践

**实践目标：** 亲手把宏「展开」一遍，确认它等价于一串 `.add()`。

**操作步骤：**

1. 阅读宏定义 [src/set.rs:51-57](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L51-L57)。
2. 找到 `UNARY_OP` 的定义 [src/set.rs:129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L129)：`syntax_set!(Plus, Minus, Not)`。
3. 在纸上把它展开成：

```rust
// 示例代码：手工展开结果
const UNARY_OP: SyntaxSet = {
    const SET: SyntaxSet = SyntaxSet::new()
        .add(SyntaxKind::Plus)
        .add(SyntaxKind::Minus)
        .add(SyntaxKind::Not);
    SET
};
```

4. （可选）用 `cargo expand` 查看 `typst-syntax` 的宏展开，核对与你手写的是否一致。

**需要观察的现象：** 展开后没有任何「魔法」，就是三个 `.add` 调用拼成的常量块。

**预期结果：** 手写展开与 `cargo expand` 输出一致；`UNARY_OP` 恰好包含 `Plus`、`Minus`、`Not` 三个一元运算符 token。

#### 4.2.5 小练习与答案

**练习 1：** 为什么宏里写成 `crate::SyntaxKind:: $kind`，而不是直接 `$kind`？

**答案：** 宏是在「调用点」展开的，调用点（比如 parser.rs）未必 `use crate::SyntaxKind::*`。写全路径 `crate::SyntaxKind::Plus` 能保证无论在哪里调用宏，都能正确解析到对应的 kind，避免命名冲突或未导入的问题。

**练习 2：** `syntax_set!()`（空参数）合法吗？它会展开成什么？

**答案：** 合法。`$($kind:ident),*` 允许零次匹配，展开为 `{ const SET: SyntaxSet = SyntaxSet::new(); SET }`，即一个空集合。parser.rs 第 355 行就有 `syntax_set!()` 的真实用法（表示「空停止集」）。

### 4.3 预定义集合常量与 parser 中的用途

#### 4.3.1 概念说明：先搞清楚可见性

在罗列常量之前，必须先澄清一个容易踩坑的可见性问题，它直接决定你「能不能在自己的代码里用这些常量」。

- `set` 模块在 `lib.rs` 里声明为**私有**：`mod set;`（见 [src/lib.rs:14](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L14)）。
- `lib.rs` 只 `pub use self::kind::SyntaxKind;`（见 [src/lib.rs:19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L19)），**没有** `pub use` 任何 `SyntaxSet` 或集合常量。

因此，尽管 `set.rs` 内部把这些常量写成 `pub const`，它们对 **crate 外部仍然不可见**——`SyntaxSet` 整个类型是 `typst-syntax` 的**内部解析工具**，不进入公开 API。只有同 crate 的 parser 能通过 `use crate::set::{SyntaxSet, syntax_set};` 和 `set::CODE_EXPR` 这样的写法用到它们。这也是为什么 4.1.4 的实践要放进 crate 内部测试模块。

#### 4.3.2 核心流程：parser 怎么用这些集合

parser 里几乎所有「当前 token 是不是某某」的判断都收口到一个方法 `at_set`：

[src/parser.rs:1660-1663](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1660-L1663) —— `fn at_set(&self, set: SyntaxSet) -> bool { set.contains(self.token.kind) }`。

它把「当前 token 的 kind」丢给集合的 `contains`。于是 parser 的决策代码读起来非常接近语法规则本身，例如：

- `if !p.at_set(set::CODE_EXPR)` —— 当前 token 不能开启代码表达式吗？不能就报「意外 token」。
- `if p.at_set(set::STMT)` —— 当前 token 是不是语句起始关键字？是的话后续可能强制要求分号。
- `if p.at_set(set::UNARY_OP)` —— 当前 token 是不是一元运算符？是的话按一元表达式解析。

#### 4.3.3 源码精读：逐个常量

下表汇总所有预定义常量及其「回答的问题」：

| 常量 | 定义位置 | 回答的问题 |
| --- | --- | --- |
| `STMT` | [src/set.rs:63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L63) | 当前 token 能否开启一条**语句**？ |
| `MATH_EXPR` | [src/set.rs:66-89](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L66-L89) | 当前 token 能否开启一个**数学表达式**？ |
| `ATOMIC_CODE_EXPR` | [src/set.rs:99-126](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L99-L126) | 当前 token 能否开启一个**原子代码表达式**？ |
| `CODE_EXPR` | [src/set.rs:95-96](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L95-L96) | 当前 token 能否开启一个（一般的）**代码表达式**？ |
| `UNARY_OP` | [src/set.rs:129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L129) | 当前 token 是不是**一元运算符**？ |
| `BINARY_OP` | [src/set.rs:132-135](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L132-L135) | 当前 token 是不是**二元运算符**？ |
| `ARRAY_OR_DICT_ITEM` | [src/set.rs:138](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L138) | 当前 token 能否开启一个**数组/字典元素**？ |
| `ARG` | [src/set.rs:141](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L141) | 当前 token 能否开启一个**函数调用参数**？ |
| `PARAM` | [src/set.rs:144](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L144) | 当前 token 能否开启一个**函数参数声明**？ |
| `DESTRUCTURING_ITEM` | [src/set.rs:147](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L147) | 当前 token 能否开启一个**解构项**？ |
| `PATTERN` | [src/set.rs:150-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L150-L151) | 当前 token 能否开启一个**模式**？ |
| `PATTERN_LEAF` | [src/set.rs:154](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L154) | 当前 token 能否开启一个**模式叶子**？ |

**重点一：常量之间会相互组合。** 这是 `union`/`add` 最主要的用途。`CODE_EXPR` 不是用 `syntax_set!` 重新列一遍，而是直接复用 `ATOMIC_CODE_EXPR`：

[src/set.rs:95-96](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L95-L96) —— `CODE_EXPR = ATOMIC_CODE_EXPR.union(UNARY_OP).add(SyntaxKind::Underscore);` 含义：一般代码表达式 = 原子表达式 + 一元运算符前缀 + 单独的下划线（用于 `_ => {}` 箭头函数或 `_ = x` 赋值）。

类似地，参数集合都建立在 `CODE_EXPR`/`PATTERN` 之上，再 `add(Dots)` 表示「可以用 `..` 展开sink」：

[src/set.rs:138-147](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L138-L147) —— `ARG`/`ARRAY_OR_DICT_ITEM` 都是 `CODE_EXPR.add(Dots)`；`PARAM`/`DESTRUCTURING_ITEM` 都是 `PATTERN.add(Dots)`。

而 `PATTERN` 又建立在 `PATTERN_LEAF` 之上，`PATTERN_LEAF` 直接复用 `ATOMIC_CODE_EXPR`：

[src/set.rs:150-154](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L150-L154) —— `PATTERN = PATTERN_LEAF.add(LeftParen).add(Underscore)`，`PATTERN_LEAF = ATOMIC_CODE_EXPR`。

于是这些常量层层复用，构成一张小小的依赖图：

```
ATOMIC_CODE_EXPR ──┬──> CODE_EXPR ──┬──> ARG
                   │                 └──> ARRAY_OR_DICT_ITEM
                   └──> PATTERN_LEAF ──> PATTERN ──┬──> PARAM
                                                 └──> DESTRUCTURING_ITEM
UNARY_OP ──────────────> CODE_EXPR
```

**重点二：`STMT` 集合装的是「关键字 token」，不是「语句节点」。** 这是一个容易和 u2-l2 混淆的点。`STMT = syntax_set!(Let, Set, Show, Import, Include, Return)` 装的是 `Let`/`Set`/.../`Return` 这些**关键字 token**（lexer 产出），而 u2-l2 讲的 `is_stmt()` 判定的是 `LetBinding`/`SetRule`/... 这些**已解析出的语句节点**（parser 产出）。两者作用于解析的不同阶段：

- parser 在 `#` 之后用 `at_set(set::STMT)` 判断「下一个 token 是不是语句关键字」，从而决定是否强制要求分号：

[src/parser.rs:587](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L587) —— `let stmt = p.at_set(set::STMT);` 记下「这是不是一条语句」，供后续分号检查使用。

- 而 `is_stmt()` 是 CST 已经建好后，对节点本身的归类。

换句话说：**集合口径面向「未来的 token」，谓词口径面向「已有的节点」**。这也是为什么 `STMT` 里有 `Return` token，而 `is_stmt()` 里没有 `FuncReturn` 节点——它们关注的是不同阶段的同一语法概念。

#### 4.3.4 代码实践

**实践目标：** 用 `at_set` 的视角读懂一段 parser 代码，并把自定义集合与 `ATOMIC_CODE_EXPR` 做对比。

**操作步骤：**

1. 打开 parser.rs 的 `code_exprs`，阅读这段循环：

[src/parser.rs:556-576](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L556-L576) —— `while !p.at_set(stop_set)` 内部先 `if !p.at_set(set::CODE_EXPR) { p.unexpected(); return; }`。

2. 解释它的行为：循环不断解析代码表达式，只要当前 token 还没到停止集；如果当前 token 既不在停止集、又不能开启代码表达式（不在 `CODE_EXPR` 里），就报「意外 token」并跳过。

3. 回到 `src/set.rs` 的测试模块，新增一个对比测试（示例代码，非项目原有）：

```rust
// 示例代码：添加到 src/set.rs 的 #[cfg(test)] mod tests 中
#[test]
fn test_compare_with_atomic() {
    // 自定义「可开启表达式的 token 集合」
    let mine = SyntaxSet::new()
        .add(SyntaxKind::Ident)
        .add(SyntaxKind::Int)
        .add(SyntaxKind::Str)
        .union(set::UNARY_OP);

    // mine 是 ATOMIC_CODE_EXPR 的一个真子集吗？
    for k in [SyntaxKind::Ident, SyntaxKind::Int, SyntaxKind::Str,
             SyntaxKind::Plus, SyntaxKind::Minus, SyntaxKind::Not] {
        // 这几个 mine 和 ATOMIC_CODE_EXPR 都应包含
        assert!(mine.contains(k));
        assert!(set::ATOMIC_CODE_EXPR.contains(k));
    }
    // mine 没有收录 LeftBrace，但 ATOMIC_CODE_EXPR 收录了
    assert!(!mine.contains(SyntaxKind::LeftBrace));
    assert!(set::ATOMIC_CODE_EXPR.contains(SyntaxKind::LeftBrace));
}
```

4. 运行 `cargo test -p typst-syntax --lib set::tests`。

**需要观察的现象：** 自定义集合 `mine` 在「标识符/整数/字符串/一元运算符」上与 `ATOMIC_CODE_EXPR` 行为一致，但 `ATOMIC_CODE_EXPR` 还额外包含 `LeftBrace`、`LeftBracket`、`Dollar`、各种关键字等——它才是「完整」的表达式起始集。

**预期结果：** 两个断言集合全部通过。如果你把 `mine` 拿到 parser 里替换 `set::CODE_EXPR`，解析器会因为漏掉 `LeftBrace` 而把 `{ ... }` 代码块开头的 `{` 当成意外 token——这能直观感受「预定义集合必须完备」的重要性。

#### 4.3.5 小练习与答案

**练习 1：** `ARG` 和 `ARRAY_OR_DICT_ITEM` 的定义完全相同（都是 `CODE_EXPR.add(Dots)`）。为什么 typst 要给同一个集合起两个名字？

**答案：** 它们的字面内容相同，但语义角色不同：`ARG` 用于「函数调用参数列表」（如 `f(a, b, ..rest)`），`ARRAY_OR_DICT_ITEM` 用于「数组/字典元素列表」（如 `(1, 2, ..xs)`）。起两个名字是为了让 parser 代码读起来贴合语法语义——读 `at_set(set::ARG)` 时立刻知道在解析函数参数，而不用去想「这个集合还顺便管数组」。这是「同值不同义」的常见命名技巧。

**练习 2：** 假设要给 Typst 新增一个关键字 `await`（判别值假设会排到 137），它能否被加入某个 `SyntaxSet`？为什么？

**答案：** 不能。判别值 137 ≥ 128，超出了 `u128` 能表示的范围，`add` 里的 `assert!((kind as u8) < BITS)` 会在编译期触发 panic。这也解释了为什么 typst 把所有「可能进集合的 token」都尽量排在枚举前 128 位，把结构节点放到末尾——是一种有意识的枚举排序策略。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「迷你语法分析」任务。

**背景：** 假设你想写一个极简的「Typst 代码表达式起始符检测器」：给定一个 token 的 `SyntaxKind`，判断它能不能作为代码表达式的开头，并进一步细分它是「原子起点」「一元运算符前缀」还是「都不是」。

**任务：**

1. 在 `src/set.rs` 的 `#[cfg(test)] mod tests` 中新增一个测试函数 `test_expr_classifier`（示例代码）。
2. 在测试里，先用 `SyntaxSet::new().add(...).union(...)` 自定义一个集合 `MY_EXPR`，它至少等于 `ATOMIC_CODE_EXPR.union(UNARY_OP)`。
3. 写一个闭包（或函数），输入一个 `SyntaxKind`，返回字符串 `"atomic"` / `"unary"` / `"none"`，规则为：
   - 在 `ATOMIC_CODE_EXPR` 里 → `"atomic"`；
   - 否则在 `UNARY_OP` 里 → `"unary"`；
   - 否则 → `"none"`。
4. 对 `Ident`、`Plus`、`Star`、`LeftBrace`、`Semicolon` 五个 kind 调用并断言结果依次为 `"atomic"`、`"unary"`、`"none"`、`"atomic"`、`"none"`。
5. 运行 `cargo test -p typst-syntax --lib set::tests::test_expr_classifier`。

**预期结果：** 测试通过。这个任务把「位集实现」「宏构造的常量」「parser 的 `at_set` 决策视角」三者连了起来——你既复用了预定义常量，又亲手用 `add`/`union` 拼装了新集合，还用 `contains` 实现了一个迷你分类器，正是 parser 内部 `code_expr_prec` 的核心决策逻辑的简化版（对照 [src/parser.rs:610](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L610) 的 `if p.at_set(set::UNARY_OP)`）。

> 提示：若不想改动源码，可把上述逻辑写在一张纸上，只追踪每个 kind 走到哪个分支，作为纯阅读型实践。

## 6. 本讲小结

- `SyntaxSet` 是一个基于 `u128` 的位集 newtype，`add`/`remove`/`union`/`contains` 全是 `const fn`，可在编译期构造常量集合，运行期查询只剩一次按位与。
- 它有硬限制：只能容纳判别值 `< 128` 的 kind；`SyntaxKind` 末尾 9 个变体（`ImportItems`..`DestructAssignment`，判别值 128–136）无法入集，且越界会在 `const` 上下文里触发**编译期** assert。
- `syntax_set!` 宏只是把「一列 kind 标识符」展开成「一串 `.add(SyntaxKind::...)`」的常量块，本身不做位运算；它是 `pub(crate)`，仅 crate 内部可用。
- 预定义常量（`STMT`/`CODE_EXPR`/`ATOMIC_CODE_EXPR`/`UNARY_OP`/`BINARY_OP`/`ARG`/`PARAM`/`PATTERN` 等）层层 `union`/`add` 复用，构成一张小依赖图，parser 通过 `at_set(set::NAME)` 在「当前 token 是否属于某类」上做决策。
- **可见性关键点：** `set` 模块在 `lib.rs` 是私有 `mod`，`SyntaxSet` 既未 `pub use`，故整个类型与所有常量都是 **crate 内部** 工具，不在 `typst-syntax` 公开 API 中；想动手实践只能进 crate 内的测试模块。
- 集合口径（`STMT` 装关键字 token）与 u2-l2 的谓词口径（`is_stmt()` 判语句节点）分别面向「未来的 token」与「已有的节点」，是同一语法概念在两个阶段的不同表达。

## 7. 下一步学习建议

下一讲进入 **U3 词法分析 Lexer**。届时你会看到 lexer 如何逐字符产出本讲反复提到的那些 token（`Plus`、`Ident`、`LeftBrace`...），从而真正理解「`SyntaxSet` 里装的 token 是怎么来的」。建议：

- 先读 `src/lexer.rs` 的 `Lexer` 结构与 `next()` 分发，对照本讲的 `SyntaxKind` 词表，看每个字符分派到哪个 kind。
- 带着「lexer 产 token → parser 用 `at_set` 消费 token」的视角进入 u3-l1，会更容易理解 lexer 为何要区分 Markup / Code / Math 三模式。
- 如果你对 parser 如何使用本讲的集合更感兴趣，也可以提前跳到 U4（`src/parser.rs`）的 `code_exprs` / `code_expr_prec`，看 `at_set` 如何驱动优先级爬升。
