# 序列号与循环算术：GenericSeqNumber

> 本讲属于第 4 单元「UDT 包的线上格式」，承接 u4-l1（`UdtPacket` 总入口）、u4-l2（数据包格式）、u4-l3（控制包格式）。前三讲告诉我们序列号在包头里占多少位，本讲回答：**这些序列号在 Rust 里是如何被表达和运算的，为什么它要自己实现一套「循环加减法」。**

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `GenericSeqNumber<T>` 为什么用一个「类型参数 `T` + `PhantomData`」来复用同一份循环算术，而不是为每种序列号各写一遍。
- 解释 `SeqConstants` trait 的作用，以及它如何让 `SeqNumber`（31 位）、`AckSeqNumber`（31 位）、`MsgNumber`（29 位）共用同一份代码却拥有不同的取值范围。
- 看懂 `Sub`（两个序列号相减）为什么返回 `i32`，而 `Add<i32>` / `Sub<i32>`（序列号加减一个整数）又如何用 `rem_euclid` 实现「回绕」。
- 区分两套截然不同的比较语义：派生出的 `Ord`（**原始 u32 比较**）和手写的 `Sub`（**带符号循环差**），并理解丢失链表 `LossList` 为什么同时需要这两套。
- 为 `SeqNumber` 写出验证循环加减（含 `MAX` 附近回绕）的单元测试，并说明 `Sub` 返回 `i32` 在 `rcv_loss_list` / `snd_loss_list` 里带来了什么便利。

## 2. 前置知识

### 2.1 为什么序列号会「回绕」

UDT 给每个发出的数据包编一个递增的序列号。连接生命周期很长、发送速率很高时，序列号会一直涨。但包头里留给序列号的位数是固定的（数据包序列号占 31 位，见 u4-l2），所以它不能无限增长——涨到最大值后必须**从 0 重新开始**。这就好比钟表的时针：12 点之后不是 13 点，而是回到 1 点。

设循环空间的容量为 \(N\)（对于 31 位序列号，\(N = 2^{31}\)），则合法取值是 \([0,\, N-1]\)。一个数 \(x\) 加 1 的结果是：

\[
(x + 1) \bmod N
\]

这就叫**循环算术（cyclic / modular arithmetic）**。难点在于「减法」：当两个数分别落在 0 和最大值附近时，它们的「真实距离」要从最短的那条弧算，而不是简单的数值相减。例如在 31 位空间里，`0` 和 `MAX` 其实只差 1（`MAX` 再加 1 就回绕到 `0`），但直接相减会得到一个接近 \(2^{31}\) 的巨大数字。

### 2.2 一个关键直觉：半区判定方向

要判断两个序列号谁在前、谁在后，且距离走的是「短弧」，UDT 用一个简单办法：把整个环对半切开，阈值

\[
H = \left\lfloor \tfrac{N-1}{2} \right\rfloor
\]

（代码里就是 `MAX_NUMBER / 2`）。当两个数的原始差不超过 \(H\) 时，可以肯定它们没跨过 0/MAX 边界，直接当普通有符号数相减即可；一旦原始差超过 \(H\)，就说明「短弧」必然穿过了边界，需要特殊处理。本讲的 `Sub` 实现就是围绕这个阈值展开的。

### 2.3 Rust 预备：`PhantomData` 与 trait 关联常量

- `PhantomData<T>` 是一个零大小的标记类型，本身不存任何数据，只用来在类型层面「声明」我与类型 `T` 有关联。本讲里它充当一个**纯编译期的「配置标签」**。
- trait 里可以用 `const MAX_NUMBER: u32;` 声明一个**关联常量**，让每种「标签类型」各自提供一个最大值。本讲的 `SeqConstants` 正是这么做的。

如果你对这两点陌生，只要记住一句话即可：**我们用不同的空类型当「标签」，让同一个泛型结构体在不同标签下表现为不同位宽的序列号，而运算逻辑只写一遍。**

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到的部分 |
| --- | --- | --- |
| `src/seq_number.rs` | **本讲主角**，定义循环序列号类型与算术 | 全文，仅 110 行 |
| `src/loss_list.rs` | 丢失链表，是 `Sub` 返回 `i32` 设计的最大受益者 | `insert` / `remove_all` 及其测试 |
| `src/socket.rs` | 真实调用点：丢包检测、流控窗口、ACK 水位 | `process_data` / `next_data_packets` 里的减法比较 |
| `src/state/socket_state.rs` | 初始化游标，用 `isn - 1` 表示「尚无数据」 | `SocketState::new` |
| `src/rate_control.rs` | 拥塞控制里也用到 `seq - 1` | `init` |

本讲几乎全部围绕 `src/seq_number.rs` 这一个文件展开，其余文件只作为「这些算术在哪里被用上」的佐证。

## 4. 核心概念与源码讲解

### 4.1 GenericSeqNumber：一个带类型参数的循环数

#### 4.1.1 概念说明

UDT 里有三种用途不同的序列号：数据包序列号、ACK 序列号、消息号。它们的**线上位宽不同**（31 / 31 / 29 位，见 u4-l2、u4-l3），但**运算规则完全一样**：都是「在一个固定容量的环上做循环加减」。

如果为每种序列号各复制粘贴一份代码，会得到三段几乎完全相同的实现——既难维护，又容易改漏。tokio-udt 的做法是写一个泛型结构体 `GenericSeqNumber<T>`，把「容量」这个唯一变量抽到一个 trait `SeqConstants` 上，再用 `PhantomData<T>` 把「标签类型 `T`」挂到结构体里。最终：

- 运算逻辑（加减、比较）**只写一份**，写在 `impl<T: SeqConstants> ...` 里；
- 三种序列号只是 `type SeqNumber = GenericSeqNumber<SeqNumberConstants>;` 这样的别名，各自的 `T` 提供不同的 `MAX_NUMBER`。

`PhantomData<T>` 在这里**不占任何运行时空间**（结构体实际只存了一个 `u32`），它纯粹是给编译器看的「类型标签」，用来区分「这是 31 位的 SeqNumber」还是「29 位的 MsgNumber」，防止把它们混着相加。

#### 4.1.2 核心流程

一个 `GenericSeqNumber<T>` 的生命周期：

1. **构造**：通过 `From<u32>` 把一个原始 `u32` 包进来（注意：构造时**不做**取值范围校验，调用方需自己保证落在 `[0, MAX_NUMBER]`）。
2. **读取**：`.number()` 取回内部 `u32`，用于往包头里写字节。
3. **常用工厂**：`zero()`、`max()`、`random()`（握手初始序列号 ISN 就用 `random()`，见 `src/socket.rs` 的 `new`）。
4. **运算**：交给 `Sub`/`Add`/`Sub<i32>`（见 4.3）。
5. **比较**：交给派生的 `Ord`（原始 u32 顺序，见 4.3.1 的特别提醒）。

#### 4.1.3 源码精读

结构体定义只存一个 `u32`，外加一个零大小标记：

[src/seq_number.rs:12-21](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L12-L21) —— `GenericSeqNumber<T>` 结构体：唯一的真实字段是 `number: u32`，`phantom: PhantomData<T>` 只是编译期标签，运行时不占空间。

`From<u32>` 是最基础的构造途径，所有「从包头字节还原序列号」的地方都走它（例如 u4-l2 里 `seq_number.into()`）：

[src/seq_number.rs:23-30](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L23-L30) —— `From<u32>`：把原始数字包成序列号，注意它不校验上界。

几个常用工厂方法，注意 `MAX_NUMBER` 是从标签类型 `T` 拿来的关联常量：

[src/seq_number.rs:32-50](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L32-L50) —— `MAX_NUMBER`/`number`/`random`/`zero`/`max`：`random()` 用 `rand` 在 `0..=MAX_NUMBER` 范围取值，正是握手 ISN 的来源。

> 提示：`From<u32>` 不做范围检查，意味着 `999_999_999u32.into()` 对 `MsgNumber`（最大 `0x1fff_ffff ≈ 5.3 亿`）而言已经越界却仍能构造。库的其余部分默认调用方传入合法值。

#### 4.1.4 代码实践

**实践目标**：亲手构造几个序列号，验证「结构体只有 4 字节、`PhantomData` 不占空间」这件事。

**操作步骤**（示例代码，可放进一个临时 `examples/` 小程序或单元测试里运行）：

```rust
// 示例代码：演示 GenericSeqNumber 的构造与内存大小
use std::mem::size_of;
// 假设你能引用到内部类型（在 crate 内的测试模块中）：
// use crate::seq_number::{SeqNumber, MsgNumber};

fn main() {
    let a: SeqNumber = 10u32.into();
    let b: SeqNumber = SeqNumber::max();
    println!("a.number() = {}", a.number());
    println!("max.number() = {:#x}", b.number()); // 0x7fff_ffff
    println!("size = {}", size_of::<SeqNumber>()); // 期望 4（只有 u32）
}
```

**需要观察的现象**：`SeqNumber` 的 `size_of` 应为 **4**，说明 `PhantomData<T>` 没有带来任何运行时开销。

**预期结果**：`a.number() = 10`，`max.number() = 0x7fffffff`，`size = 4`。

**如果无法确定运行结果**：受公共 API 限制，`GenericSeqNumber` 与 `MsgNumber` 未在 `lib.rs` 重导出（只有 `SeqNumber` 是公开的，见 u1-l4）。若想验证 `MsgNumber`/`AckSeqNumber` 的大小，需在 crate 内的 `#[cfg(test)] mod tests` 中进行——标记为「待本地验证（需在 crate 内测试）」。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接用 `u32` 表示序列号，而要包一层 `GenericSeqNumber<T>`？

<details><summary>参考答案</summary>

有两点好处：(1) 类型安全——`SeqNumber` 和 `MsgNumber` 是不同的具体类型，编译器会阻止你把一个数据包序列号「加」到一个消息号上，而裸 `u32` 之间则可以任意混算；(2) 复用——把循环加减逻辑写在一处 `impl<T: SeqConstants>`，三种位宽的序列号自动获得正确实现，避免重复代码。
</details>

**练习 2**：`SeqNumber::max()` 和 `SeqNumber::from(0x7fff_ffff)` 是否相等？

<details><summary>参考答案</summary>

相等。`max()` 返回 `T::MAX_NUMBER.into()`，而 `SeqNumberConstants::MAX_NUMBER = 0x7fff_ffff`，二者内部 `number` 都是 `2147483647`，且结构体派生了 `PartialEq`，故相等。
</details>

### 4.2 SeqConstants 与三种序列号空间

#### 4.2.1 概念说明

`SeqConstants` 是那块「配置标签」的 trait，它只要求实现一个关联常量 `MAX_NUMBER`，并提供一个默认方法 `threshold()`（返回 `MAX_NUMBER / 2`，即 2.2 节里的半区阈值 \(H\)）。每个标签结构体（`SeqNumberConstants` 等）实现它，提供各自的位宽。

三个别名把泛型实例化成三种**真实存在、位宽不同**的序列号，正好对应 u4-l2 / u4-l3 讲过的包头字段：

| 别名 | 标签类型 | `MAX_NUMBER` | 位宽 | 对应包头字段 |
| --- | --- | --- | --- | --- |
| `SeqNumber` | `SeqNumberConstants` | `0x7fff_ffff` | 31 位 | 数据包 `seq_number`（u4-l2，掩码 `& 0x7fffffff`） |
| `AckSeqNumber` | `AckSeqNumberConstants` | `0x7fff_ffff` | 31 位 | 控制包 ACK 序号（`additional_info`，u4-l3） |
| `MsgNumber` | `MsgNumberConstants` | `0x1fff_ffff` | 29 位 | 数据包 `msg_number`（u4-l2，掩码 `& 0x1fffffff`）；也用于 MsgDropRequest |

这张表把「本讲的位宽」和「前两讲的包头位宽」对上了：包头里用掩码截出来的那几个数，回到 Rust 侧就是用这三种类型的 `From<u32>` 装回去的。

> 小细节：`SeqNumber` 和 `AckSeqNumber` 的 `MAX_NUMBER` 相同（都是 31 位），但它们是**不同的类型**，因此不能互相赋值或直接相加——这能在编译期挡住「把 ACK 序号当数据包序号用」的低级错误。这也是为什么 `MAX_NUMBER` 这个值要重复写两遍，而不是共享同一个标签类型。

#### 4.2.2 核心流程

- trait 定义：`const MAX_NUMBER: u32;` + 默认 `fn threshold() -> u32 { Self::MAX_NUMBER / 2 }`。
- 三个常量结构体各自 `impl SeqConstants`，提供位宽。
- 三个 `pub type ... = GenericSeqNumber<XxxConstants>;` 把泛型固定下来。
- 对外（`lib.rs`）只 `pub use` 了 `SeqNumber` 一个；`AckSeqNumber` / `MsgNumber` 是 `pub(crate)` 内部使用。

#### 4.2.3 源码精读

trait 只有一个必需常量加一个默认方法：

[src/seq_number.rs:4-10](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L4-L10) —— `SeqConstants`：`MAX_NUMBER` 决定循环空间容量，`threshold()` 给 `Sub` 用作半区判定。

三个标签 + 别名，注意 `MsgNumber` 的位宽是 29：

[src/seq_number.rs:85-92](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L85-L92) —— `SeqNumberConstants` 与 `pub type SeqNumber`（31 位）。

[src/seq_number.rs:103-110](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L103-L110) —— `MsgNumberConstants` 与 `pub type MsgNumber`（29 位，`0x1fff_ffff`），与 u4-l2 数据包 `msg_number` 的掩码一致。

`MsgNumber` 被 `control_packet.rs` 用来从 `additional_info` 里取消息号，正好用上它的 `MAX_NUMBER` 当掩码：

[src/control_packet.rs:114-118](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L114-L118) —— `msg_seq_number()` 用 `& MsgNumber::MAX_NUMBER` 截取 29 位消息号，把本讲的常量与 u4-l3 的控制包解析连起来。

#### 4.2.4 代码实践

**实践目标**：确认三种序列号是三个互不兼容的类型。

**操作步骤**（示例代码，需放在 crate 内测试模块）：

```rust
// 示例代码：故意制造类型不匹配，观察编译器报错
fn _type_mismatch() {
    let s: SeqNumber = 5u32.into();
    let m: MsgNumber = 5u32.into();
    // let _ = s + m;          // 预期：编译失败，类型不匹配
    // let _: SeqNumber = m;   // 预期：编译失败，MsgNumber 不能当 SeqNumber
    let _ = (s, m);
}
```

**需要观察的现象**：把注释取消后，`cargo build` 应当因为类型不匹配而报错。

**预期结果**：编译失败，错误信息提示 `SeqNumber` 与 `MsgNumber` 是不同类型、`Add` 没有对应实现。这正是「类型标签」带来的编译期保护。

**如果无法确定运行结果**：标记为「待本地验证（需在 crate 内测试，因为 `MsgNumber` 未公开）」。

#### 4.2.5 小练习与答案

**练习 1**：`SeqNumber` 与 `AckSeqNumber` 的 `MAX_NUMBER` 完全相同，为什么还要写成两个不同的标签类型？

<details><summary>参考答案</summary>

为了**类型隔离**。二者虽然取值范围一样，但语义不同：一个是数据包序列号，一个是 ACK 序号。分成两个类型后，编译器能阻止你把 `AckSeqNumber` 误塞进需要 `SeqNumber` 的位置（例如丢包链表）。如果共用一个标签类型，这种混淆就只能靠人来检查了。
</details>

**练习 2**：`threshold()` 对 `MsgNumber` 返回多少？

<details><summary>参考答案</summary>

`MsgNumberConstants::MAX_NUMBER = 0x1fff_ffff = 536870911`，整数除以 2 得 `threshold() = 268435455`（即 `0x0fff_ffff`）。它就是 `MsgNumber` 循环减法里的半区阈值。
</details>

### 4.3 循环 Sub / Add：带符号差与非负回绕

这是本讲最核心、也最容易让人困惑的一节。请重点区分三件事：**两个序列号相减**（`Sub`，返回 `i32`）、**序列号加减一个整数**（`Add<i32>` / `Sub<i32>`，返回序列号）、以及**派生的 `Ord` 比较**（原始 u32 顺序）。

#### 4.3.1 概念说明

**① `Sub` 返回 `i32`：带符号的「循环差」。**

两个序列号 `a - b` 想表达的不是「另一个序列号」，而是「在环上，`a` 相对 `b` 偏了多少、偏向哪边」。它必须是个**带符号的整数**：正数表示一个方向，负数表示另一个方向，绝对值是环上的最短距离。这样调用方就能直接写 `(a - b) > 1` 来判断「二者之间是否隔了不止 1 个包」。

**② `Add<i32>` / `Sub<i32>`：在环上「向前/向后走若干步」，结果仍落在 `[0, MAX_NUMBER]`。**

关键工具是 `rem_euclid`（欧几里得取模，结果总是非负）。把序列号加任意整数（可为负）后对空间容量 \(N\) 取模，结果天然落在 \([0, N-1]\)，完美实现回绕。

**③ 派生的 `Ord` 是「原始 u32 比较」，不是循环比较。**

这点务必记住：`GenericSeqNumber` 上 `#[derive(PartialOrd, Ord)]`，比较的是内部的 `number: u32`（`PhantomData` 不影响比较）。所以 `a < b` 完全等价于 `a.number() < b.number()`，**与回绕无关**。这一点在丢失链表里被刻意利用（见 4.3.4）。

#### 4.3.2 核心流程

设 \(N = \text{MAX\_NUMBER} + 1\)（SeqNumber 的 \(N = 2^{31}\)），半区阈值 \(H = \lfloor \text{MAX\_NUMBER}/2 \rfloor\)。环上两数的最短距离为：

\[
d_{\min}(a, b) = \min\big((a - b) \bmod N,\; (b - a) \bmod N\big)
\]

**`Sub`（`self - other`，返回 `i32`）分三个分支**，对应「是否跨边界、`self` 落在哪个半区」：

1. 若原始差 `abs_diff(self, other) <= H`：二者在同一个半区内、没跨边界，直接返回 `self.number as i32 - other.number as i32`（自然符号）。**这是绝大多数实际场景**——因为真实序列号间距（几个、几千个包）远小于 \(H \approx 2^{30}\)。
2. 否则（原始差超过 \(H\)，说明短弧穿过边界）且 `self < H`（self 在低半区）：返回 `-(self.number + N - other.number)`，即带负号的短弧距离。
3. 否则（self 在高半区）：返回 `other.number + N - self.number`，即带正号的短弧距离。

> 关于符号约定：第 2、3 分支的符号由 **`self` 落在哪个半区** 决定。本讲的代码实践会请你用测试去实测 `MAX` 附近回绕时 `Sub` 的真实符号——这是个值得亲验的细节，**实际运行符号以本地测试为准（部分场景待本地验证）**。但有一点是确定的：三个分支给出的**绝对值**始终等于环上最短距离 \(d_{\min}\)。

**`Add<i32>`**：`((self.number as i64 + rhs as i64).rem_euclid(N)) as u32`。用 `i64` 是为了防止 `MAX + 正数` 暂时溢出 `i32`；`rem_euclid` 保证结果非负。

**`Sub<i32>`**：复用 `Add`，写成 `self + (rhs * -1)`（代码上有个 `#[allow(clippy::neg_multiply)]`，因为它等价于 `self + (-rhs)`）。

举个回绕例子：`SeqNumber::zero() - 1` 应当得到 `MAX`（0 往后退一步就是环的另一端 `MAX`）。这正好用在初始化里——把「游标」设在「第一个待收/待发序列号的前一格」，表示「还没有任何数据」：

[src/state/socket_state.rs:46-49](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L46-L49) —— `curr_rcv_seq_number: isn - 1`：用 `Sub<i32>` 把接收游标放到 ISN 的前一格，表示尚未收到任何包；当 `isn == 0` 时它回绕成 `MAX`。

[src/rate_control.rs:70-72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L70-L72) —— 拥塞控制里 `last_dec_seq = seq_number - 1` 同样依赖这个回绕减法。

#### 4.3.3 源码精读

`Sub` 的三分支实现，注意输出类型是 `i32`：

[src/seq_number.rs:52-65](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L52-L65) —— `impl Sub`：`type Output = i32`；以 `threshold()` 判定是否跨边界，跨边界时按 `self` 所在半区决定符号。

`Add<i32>` 用 `i64` + `rem_euclid` 实现非负回绕：

[src/seq_number.rs:67-74](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L67-L74) —— `impl Add<i32>`：先升到 `i64` 防溢出，再 `rem_euclid(MAX_NUMBER + 1)` 得到 \([0, N-1]\) 的结果。

`Sub<i32>` 直接复用 `Add`：

[src/seq_number.rs:76-83](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L76-L83) —— `impl Sub<i32>`：`self + (rhs * -1)`，即「减去 n 等于加上 −n」。

**两个真实调用点**，体会「返回 `i32` 带来的便利」：

[src/socket.rs:306-310](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L306-L310) —— 发送限流：`(curr_snd_seq_number - last_ack_received) > window_size`。这里 `Sub` 返回的 `i32` 直接和窗口大小比，得到「在途未确认包数是否超过窗口」。

[src/socket.rs:719-726](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L719-L726) —— 丢包检测：`(seq_number - curr_rcv_seq_number) > 1` 表示「收到的包比期望的跳了不止一格」→ 中间有丢包，立即把缺失区间 `[curr+1, seq-1]` 插入 `rcv_loss_list` 并发 NAK。这里 `curr_rcv_seq_number + 1` 与 `seq_number - 1` 同时用到了循环加减。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：为 `SeqNumber` 补单元测试，验证循环加减；并解释 `Sub` 返回 `i32` 在丢失链表里的便利。

**操作步骤**：

1. 在 `src/seq_number.rs` 末尾追加一个测试模块（模仿 `src/loss_list.rs:163` 起的 `#[test]` 风格），加入下面的用例（**示例代码**）：

   ```rust
   #[cfg(test)]
   mod tests {
       use super::*;

       #[test]
       fn sub_within_threshold_is_natural() {
           // 分支 1：没跨边界，自然符号
           assert_eq!(SeqNumber::from(10) - SeqNumber::from(5), 5);
           assert_eq!(SeqNumber::from(5) - SeqNumber::from(10), -5);
           assert_eq!(SeqNumber::from(7) - SeqNumber::from(7), 0);
       }

       #[test]
       fn add_wraps_at_max() {
           // Add<i32>：MAX + 1 回绕到 0
           assert_eq!((SeqNumber::max() + 1).number(), 0);
           assert_eq!((SeqNumber::max() + 2).number(), 1);
       }

       #[test]
       fn sub_i32_wraps_below_zero() {
           // Sub<i32>：0 - 1 回绕成 MAX
           assert_eq!((SeqNumber::zero() - 1).number(), SeqNumber::max().number());
       }

       #[test]
       fn sub_across_boundary_magnitude() {
           // 跨边界（MAX 附近）：绝对值应为最短距离，符号以本地测试为准
           let d1 = SeqNumber::from(1) - SeqNumber::max(); // self=1 在低半区
           let d2 = SeqNumber::max() - SeqNumber::from(1); // self=MAX 在高半区
           // 绝对值确定等于最短距离 2
           assert_eq!(d1.abs(), 2);
           assert_eq!(d2.abs(), 2);
           // 符号：请按你读到的三分支逻辑填入预期值后取消注释验证
           // assert_eq!(d1, ___);
           // assert_eq!(d2, ___);
       }
   }
   ```

2. 运行 `cargo test seq_number`。
3. 打开 `src/loss_list.rs`，对照 `insert`（第 16–46 行）与 `remove_all`（第 67–76 行）观察它如何**同时**使用原始比较和循环加减。

**需要观察的现象**：

- 前三个测试应全部通过：分支 1 的自然符号、`Add` 在 `MAX` 处回绕、`Sub<i32>` 在 `0` 下方回绕。
- 第四个测试：`d1.abs()` 与 `d2.abs()` 都等于 2（`1` 与 `MAX` 在环上只差 2）。至于 `d1`、`d2` 的**正负号**，请你根据 [src/seq_number.rs:52-65](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L52-L65) 的三分支亲手判定后填入 `assert_eq!` 验证——这正是「threshold 判定方向」要你搞清的地方。

**预期结果**：分支 1 的符号为自然符号；回绕加减正确；跨边界场景的**绝对值**正确为最短距离。跨边界场景的**符号**以你本地实测为准（部分符号约定待本地验证）。

**关于「`Sub` 返回 `i32` 在丢失链表里的便利」的参考答案**（这是本实践要你回答的问题）：

`LossList` 把丢失区间存进 `BTreeMap<SeqNumber, (SeqNumber, SeqNumber)>`，它**同时依赖两套比较语义**，缺一不可：

- **原始 `Ord`（派生）** 用来**组织区间结构**：`BTreeMap` 按原始 `u32` 排序键；[src/loss_list.rs:19-23](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L19-L23) 用 `n1.number() > n2.number()` 判定「这个区间是否跨过了 0/MAX 边界」，一旦跨边界就**拆成两段**不跨边界的子区间分别处理；[src/loss_list.rs:67-76](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L67-L76) 的 `remove_all` 同样用 `n1 <= n2`（原始比较）决定要不要拆。拆分之后，每段子区间内部都在同一半区内，原始比较与循环比较**等价**，于是 `BTreeMap` 的 `range` 查询能正确工作。
- **循环 `Sub`/`Add`** 用来**在环上导航**：求前驱/后继、计算缺口大小。[src/socket.rs:719-726](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L719-L726) 里 `(seq_number - curr_rcv_seq_number) > 1` 判断「是否跳格」，`curr_rcv_seq_number + 1` 与 `seq_number - 1` 算出缺失区间的两个端点（含跨边界时的回绕）。

**为什么是 `i32` 而不是 `Self`？** 因为 `(a - b) > 1`、`(curr_snd - last_ack) > window_size`、`to_ack = seq - last_sent_ack` 这些用法都需要一个**可正可负、能和普通整数比大小**的「带符号缺口」。若 `Sub` 返回 `Self`（另一个序列号），结果就只是个原始 `u32`，既丢失了符号（无法判断方向），也无法直接表达「缺口大小」（跨边界时原始值巨大），上述判断统统写不出来。**返回 `i32` = 把「环上的带符号最短距离」直接交给调用方去和阈值比较**，这就是它的便利所在。

**如果无法确定运行结果**：第四个测试的符号断言请以本地 `cargo test` 实测为准；本讲义不预先断言跨边界分支的符号正负，以免误导。

#### 4.3.5 小练习与答案

**练习 1**：`(SeqNumber::max() + 1)` 和 `(SeqNumber::zero() - 1)` 的 `.number()` 各是多少？

<details><summary>参考答案</summary>

- `max() + 1`：`(MAX + 1).rem_euclid(N)`。因 \(N = \text{MAX}+1\)，故 `MAX + 1 ≡ 0 (mod N)`，结果为 **0**。
- `zero() - 1`：等价于 `zero() + (-1)`，`(0 - 1).rem_euclid(N) = N - 1 = MAX`，结果为 **`0x7fff_ffff`（`MAX`）**。

两者互为「反向邻居」：`max() + 1 == 0`（往前一步跨过边界回到 0），`zero() - 1 == MAX`（往后一步跨过边界回到 MAX），正好体现环的对称结构。建议你用测试确认。
</details>

**练习 2**：为什么 `Add<i32>` 里要先把操作数升成 `i64` 再 `rem_euclid`，而不是直接用 `i32`？

<details><summary>参考答案</summary>

因为 `self.number` 可能接近 `MAX_NUMBER ≈ 2.1e9`，再加一个（哪怕不大的）正 `i32`，其和可能超过 `i32::MAX ≈ 2.15e9`，从而在 `i32` 域里溢出。先升到 `i64`（范围远大于 \(2 \times 2^{31}\)）做加法和取模，就不会溢出；最后再 `as u32` 截回。这是处理「接近上界」的常规防御性写法。
</details>

**练习 3**：`loss_list.rs` 的 `insert` 在发现 `n1.number() > n2.number()` 时为什么要把区间拆成 `[n1, MAX]` 和 `[0, n2]` 两段？

<details><summary>参考答案</summary>

因为 `n1 > n2`（原始比较）只能说明这个「逻辑区间」跨过了 0/MAX 边界（在环上它其实是一条连续弧）。而 `BTreeMap` 的键是按原始 `u32` 排序的，无法直接表示一条跨越边界的弧。把它拆成「到 `MAX` 为止」和「从 `0` 开始」两段后，每段都不跨边界、内部原始序与循环序一致，就能正确地存进 `BTreeMap` 并参与 `range` 查询与合并。这正是「原始 `Ord` + 循环 `Sub`」两套语义协同的体现。
</details>

## 5. 综合实践

把本讲的三块知识（泛型复用、三种位宽、循环加减）串起来，完成下面这个小任务：

**任务**：模拟一个「在 31 位环上滑动窗口」的场景，亲手走一遍序列号的演进。

1. 设初始发送序列号 `snd = SeqNumber::random()`（模拟握手 ISN）。
2. 用 `snd = snd + 1` 模拟「发出一个新包」，连续加一个较小的数（例如 100），观察 `.number()` 单调递增。
3. 构造一个 ACK：`ack = snd - 5`（表示「最近 5 个包还没被确认」），计算「在途未确认包数」`in_flight = snd - ack`（应为 `5`，类型 `i32`）。
4. 构造一个「在窗口边缘」的场景：把 `snd` 强行设到 `SeqNumber::max() - 2`，再 `snd = snd + 5`，观察它如何回绕过 `MAX` 回到小数值；然后计算 `ack = SeqNumber::max() - 1`，求 `in_flight = snd - ack`，验证循环减法在跨边界时给出的**绝对值**是否等于你直觉上的在途数量。
5. 把上述每一步的 `.number()` 打印出来，画出这条「环上的滑动轨迹」。

**验收**：

- 步骤 2、3 在不跨边界时，`in_flight` 与普通算术一致；
- 步骤 4 跨边界时，`in_flight` 的**绝对值**应等于环上最短距离（即真实在途数）；其符号请你结合三分支逻辑解释，并以本地实测为准（待本地验证）。
- 用一句话回答：如果 `Sub` 返回的是 `Self` 而不是 `i32`，步骤 3、4 里「在途包数」还能用 `snd - ack` 这样直接算出来吗？为什么？

**提示**：这正是 [src/socket.rs:306](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L306) 那行 `(curr_snd_seq_number - last_ack_received) > window_size` 每天在做的事——把「带符号循环差」直接拿来和窗口比大小。

## 6. 本讲小结

- `GenericSeqNumber<T>` 用「`PhantomData<T>` 标签 + `SeqConstants` 关联常量」让 **31/31/29 位三种序列号共用同一份循环算术**，结构体运行时只占 4 字节。
- `SeqConstants` 只规定 `MAX_NUMBER`，并提供半区阈值 `threshold() = MAX_NUMBER / 2`；`SeqNumber` / `AckSeqNumber` / `MsgNumber` 是三个互不兼容的具体类型，位宽分别匹配 u4-l2 / u4-l3 的包头字段。
- `Sub`（两序列号相减）**返回 `i32`**，给出「环上带符号最短距离」，用 `threshold()` 三分支判定是否跨边界；`Add<i32>` / `Sub<i32>` 用 `i64 + rem_euclid` 实现非负回绕，结果始终落在 `[0, MAX_NUMBER]`。
- 务必区分两套比较：**派生 `Ord` = 原始 u32 比较**（用于 `BTreeMap` 结构与跨边界拆分），**`Sub` = 循环带符号差**（用于缺口/窗口判断）。`LossList` 同时依赖这两套。
- `Sub` 返回 `i32`（而非 `Self`）是关键设计：它让 `(a - b) > 1`、`(snd - ack) > window` 这类「缺口比阈值」的判断能直接写出来，这正是 `rcv_loss_list` / `snd_loss_list` 比较的便利所在。
- 跨边界（`MAX` 附近回绕）时 `Sub` 的符号由 `self` 所在半区决定，绝对值恒为最短距离；真实符号建议用本讲的单元测试本地实测确认。

## 7. 下一步学习建议

- **横向闭环**：回到 u4-l2 / u4-l3，对照包头里的 `& 0x7fffffff`、`& 0x1fffffff` 等掩码，确认它们取出的数正是本讲 `SeqNumber` / `MsgNumber` 装回的类型，把「线上位宽 → Rust 类型」这条链彻底打通。
- **纵向深入（推荐下一站）**：进入第 5 单元 u5-l1（发送队列与发送缓冲），看 `SndBuffer` 如何用 `seq_number = seq_number + 1`（[src/queue/snd_buffer.rs:156](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L156)）为每个分片编号；再到 u6-l3（丢包检测与 NAK）看 `LossList` 的区间合并/拆分如何密集使用本讲的两套比较。
- **进阶阅读**：若你对「循环序」的数学背景感兴趣，可搜索 *serial number arithmetic*（RFC 1982 给出了经典的半区判定规则，本讲的 `threshold()` 与之一脉相承）。
