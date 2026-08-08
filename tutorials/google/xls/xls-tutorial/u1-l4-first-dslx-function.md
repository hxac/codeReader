# 用 DSLX 写第一个硬件函数

## 1. 本讲目标

本讲带你亲手写出第一段真正的硬件功能代码。我们以 XLS 仓库自带的 `xls/examples/gcd.x`（最大公约数）为范本，逐行读懂一个**完整的 DSLX 模块**——它包含函数定义、任意位宽类型、`for` 循环、`match` 匹配，以及 `#[test]` 单元测试和 `#[quickcheck]` 属性测试。

学完本讲你应该能够：

- 读懂 `gcd.x` 里的每一个语法元素，并能解释它们各自的硬件含义。
- 理解 DSLX 与传统软件语言的关键区别：**不可变、表达式式、定宽数据流**。
- 自己写一个 DSLX 函数，并用 `#[test]` 和 `#[quickcheck]` 为它编写测试，最后用 `interpreter_main` 跑通。

本讲是「入门单元」的第四篇。它承接 [u1-l2](u1-l2-build-and-run.md)（你已经能构建出 `interpreter_main`），并为 [u1-l5](u1-l5-full-toolchain-walkthrough.md)（把 `.x` 走完到 Verilog 的完整工具链）做语言层面的铺垫。

## 2. 前置知识

在开始之前，请确保你已经了解下面这些概念（前三篇讲义已建立）：

- **XLS 是什么**：一个高层综合（HLS）工具链，把高层描述翻译成可综合的 Verilog。详见 [u1-l1](u1-l1-project-overview.md)。
- **能构建并运行 `interpreter_main`**：这是本讲用来执行测试的二进制，形如 `./bazel-bin/xls/dslx/interpreter_main`。详见 [u1-l2](u1-l2-build-and-run.md)。
- **DSLX 是 XLS 主推的前端语言**：一种 Rust 风格的数据流 DSL，位于 `xls/dslx`。详见 [u1-l3](u1-l3-directory-structure.md)。

此外，有两个「软件思维 → 硬件思维」的转变，先在脑子里建立直觉：

1. **位宽是头等公民**。在 C/Python 里你写 `int`，编译器/解释器替你决定占几位；在硬件里，每一根线都对应确定的位数。所以 DSLX 的类型几乎总是「带位宽」的，例如 `u8`（8 位无符号）、`uN[32]`（32 位无符号）。
2. **没有「可变变量」，只有数据流**。DSLX 里 `let x = ...` 之后 `x` 不能再被赋值。一个 `for` 循环不是一个「反复改写某个变量」的过程，而是一个**带累加器（accumulator）的表达式**：每一轮把累加器演化成新值，循环结束时整个 `for` 表达式求值成「最后一次的累加器」。这一点会在 4.2 重点讲。

> 术语提示：
> - **DSLX**：XLS 的领域专用语言（DSL），文件后缀 `.x`。
> - **模块（module）**：一个 `.x` 文件就是一个模块，里面可以放多个函数、测试。
> - **综合（synthesis）**：把高层描述转成门级/RTL 网表的过程。DSLX 的设计目标之一是「可综合」。

## 3. 本讲源码地图

本讲主要围绕下面几个文件展开：

| 文件 | 作用 |
| --- | --- |
| [xls/examples/gcd.x](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x) | 本讲的主范本：用欧几里得算法和二进制 GCD 算法两种方式求最大公约数，并配有单元测试与 quickcheck。 |
| [docs_src/tutorials/hello_xls.md](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/hello_xls.md) | 官方最入门教程，演示如何创建一个 `.x` 模块并用 `interpreter_main` 跑测试。 |
| [docs_src/dslx_reference.md](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md) | DSLX 语言参考手册，本讲会引用其中类型、`for`、`match`、测试、quickcheck 的章节。 |
| [xls/dslx/stdlib/std.x](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/stdlib/std.x) | DSLX 标准库；`gcd.x` 里用到的 `iterative_div_mod`（同时返回商和余数）就定义在这里。 |
| [xls/dslx/interpreter_main.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/interpreter_main.cc) | `interpreter_main` 二进制的入口，定义了执行测试、quickcheck 时用到的命令行标志。 |

> 链接说明：本讲所有源码引用都用当前 HEAD（`e796ea8aeea3875362c7dbeb11a850f3854a9116`）生成永久链接，行号即链接里的 `#L...`。你在阅读时可点击直接跳转到 GitHub 上对应的那几行。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 模块、函数与类型**——DSLX 的结构骨架（`import`、`fn`、位宽类型、参数化泛型）。
- **4.2 表达式式数据流：`for` 与 `match`**——DSLX 之所以「像硬件」的核心：不可变累加器与模式匹配。
- **4.3 测试与 QuickCheck**——用 `#[test]` 与 `#[quickcheck]` 为函数写测试，并用 `interpreter_main` 执行。

### 4.1 模块、函数与类型

#### 4.1.1 概念说明

一个 `.x` 文件就是一个**模块（module）**，是 DSLX 的顶层编译单元。模块里可以放：

- `import` 语句：引入其他模块（例如标准库 `std`）。
- `fn` 函数：一段可被综合的纯计算。
- `const` 常量、类型别名等。
- 测试函数（`#[test]` / `#[quickcheck]`）：不会被综合，只供解释器执行。

DSLX 的函数有几个有别于 C/Python 的特点：

1. **函数体是表达式**，没有 `return` 关键字。函数返回「最后一个表达式的值」。
2. **每个参数都必须显式声明类型**，而且类型几乎总是带位宽。
3. **位宽类型**是 DSLX 最基础的类型：`bits[n]` 表示 n 位的位串。为了方便，`u8` 是 `bits[8]` 的别名、`u32` 是 `bits[32]` 的别名，`u*` / `s*` 系列定义到 64 位；`uN[n]` / `sN[n]` 则可以写任意位宽（如 `uN[5]`）。详见参考手册 [Bit Type](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L322) 一节。
4. **参数化（parametric）函数**：类似泛型，但泛型的是「位宽」这样的编译期常量，而不是类型本身。

#### 4.1.2 核心流程

一个 DSLX 模块从文本到可执行，在本讲里只关心前端这一层（后续讲义才进入 IR）：

1. 解释器读取 `.x` 文件，把它解析成一个模块（AST）。
2. 对模块做类型检查（确认每个表达式的位宽）。
3. 遇到测试函数时，直接在 DSLX 字节码解释器里执行它们（本讲）；非测试函数之后才会被转换成 IR、做优化、生成 Verilog（后续讲义）。

定义函数的标准骨架是：

```dslx
fn 函数名(参数名: 参数类型, ...) -> 返回类型 {
    最后一个表达式   // 它的值就是返回值
}
```

当函数需要「位宽可变」时，加上参数化参数：

```dslx
fn 函数名<N: u32>(a: uN[N]) -> uN[N] { ... }
//         ^ N 是一个 u32 类型的编译期常量，代表位宽
```

#### 4.1.3 源码精读

先看 `gcd.x` 的开头两行——模块属性与标准库导入：

[xls/examples/gcd.x:1](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L1) 是一个模块级属性 `#![feature(type_inference_v2)]`，开启较新的类型推导版本（属于实验性开关，初学时照抄即可）；[xls/examples/gcd.x:17](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L17) 的 `import std;` 引入了 DSLX 标准库，这样后面才能调用 `std::iterative_div_mod`。

接着是第一个函数 `gcd_euclidean`（欧几里得算法）的签名：

```dslx
fn gcd_euclidean<N: u32, DN: u32 = {N * u32:2}>(a: uN[N], b: uN[N]) -> uN[N] {
```

这段在 [xls/examples/gcd.x:20](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L20)。逐部分解读：

- `<N: u32, DN: u32 = {N * u32:2}>`：两个**参数化参数**。`N` 是位宽（由调用方决定）；`DN` 是一个**派生参数**，默认值是 `N * 2`，用 `{}` 包起来表示这是一个编译期常量表达式（类似 C++ 的 `constexpr`）。`DN` 在后面用作循环上界。
- `a: uN[N], b: uN[N]`：两个入参，都是 N 位无符号数。`uN[N]` 就是「位宽等于 N 的无符号类型」。
- `-> uN[N]`：返回值也是 N 位无符号数。

也就是说，**这一个函数定义就能同时表示 8 位、16 位、32 位等各种位宽的 GCD**，调用时再实例化，例如 `gcd_euclidean(u8:48, u8:18)` 会让 `N = 8`。

关于参数化的更多细节，可参考手册 [Parametric Functions](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L231) 一节；关于函数与参数写法，见 [Functions](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L181)。

#### 4.1.4 代码实践

**实践目标**：在 `gcd.x` 中把「类型」和「位宽」对应起来。

**操作步骤**：

1. 打开 [xls/examples/gcd.x](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x)。
2. 找到第二个函数 `gcd_binary_match`（[第 31–38 行](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L31-L38)），它的签名是 `fn gcd_binary_match<N: u32>(a: uN[N], b: uN[N], d: uN[N]) -> (uN[N], uN[N], uN[N])`。
3. 回答：它有几个参数化参数？返回类型是什么形状？（提示：返回类型是三个 `uN[N]` 组成的**元组**。）

**需要观察的现象**：你会注意到 DSLX 里「函数返回多个值」是通过返回一个**元组** `(uN[N], uN[N], uN[N])` 实现的，而不是像某些语言那样列出多个返回类型。

**预期结果**：`gcd_binary_match` 有 1 个参数化参数 `N`；返回一个三元组，三个元素都是 N 位无符号数。这正好是 4.2 里 `for` 循环要用的累加器形状。

#### 4.1.5 小练习与答案

**练习 1**：`u8`、`bits[8]`、`uN[8]` 三者是什么关系？

> **答案**：三者等价。`bits[n]` 是最基础的「n 位位串」类型；`u8` 是 `bits[8]` 的便捷别名；`uN[8]` 是用构造器写法表示的「8 位无符号类型」。无符号用 `u*`/`uN[*]`，有符号用 `s*`/`sN[*]`。

**练习 2**：`gcd_euclidean<N: u32, DN: u32 = {N * u32:2}>` 里，`{}` 的作用是什么？如果省略会怎样？

> **答案**：`{}` 表示里面的 `N * u32:2` 是一个**编译期常量表达式**（constexpr），用于推导派生参数 `DN`。按手册说明，除「简单字面量和常量引用」外的表达式必须用 `{}` 包裹以消歧，否则可能产生解析歧义或不可预期的错误（参见 [Expression ambiguity](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L277)）。

---

### 4.2 表达式式数据流：`for` 与 `match`

#### 4.2.1 概念说明

这是 DSLX 最「硬件味」的部分。两个关键观念：

**（1）`for` 是带累加器的表达式，不是可变循环。**

在 C 里你会写：

```c
int acc = 0;
for (int i = 0; i < 4; i++) { acc = acc + i; }   // 反复改写 acc
```

在 DSLX 里你写：

```dslx
for (i, acc) in u32:0..4 {
    acc + i          // 每轮的「新 acc 值」，就是这条表达式的结果
}(u32:0)             // 括号里是 acc 的初始值
```

整个 `for ... (...)(初始值)` 是一个**表达式**，它的值等于「最后一次迭代结束时累加器的值」。这里没有赋值，每轮只是**把累加器演化成一个新值**。累加器可以是元组，从而一次演化多个值。

为什么是这样设计的？因为 DSLX 要被综合成硬件：一个**计数循环**（迭代次数有上界）在生成流水线时会被**展开**成若干个流水级，每一级对应一轮迭代的组合逻辑。没有可变状态，硬件才好映射。参见手册 [for Expression](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1616)。

**（2）`match` 是模式匹配表达式。**

`match` 类似增强版的 `switch`：拿一个值去匹配若干「模式」，匹配到的那个分支的表达式就是整个 `match` 的值。它还能在模式里**绑定变量**。DSLX 目前要求每个 `match` 都要有一个「兜底（catch-all）」分支（如 `_ =>`）。参见 [match Expression](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1366)。

#### 4.2.2 核心流程

`gcd_euclidean` 用的是**欧几里得算法**，其数学递推关系为：

\[
\gcd(a, b) = \begin{cases} a & \text{若 } b = 0 \\ \gcd(b,\; a \bmod b) & \text{否则} \end{cases}
\]

把这条递推「拍平」成一个固定迭代次数的循环，就得到：每轮若 \(b=0\) 则保持 `(a, b)` 不变（已经收敛），否则把累加器从 `(a, b)` 演化成 `(b, a mod b)`。因为对 N 位数，欧几里得算法最多迭代约 \(2N\) 次必然收敛，所以 `gcd_euclidean` 把循环上界取成派生参数 `DN = N*2`，循环里用 `if (b == 0)` 提前「冻结」结果。

`for` 循环的标准写法是：

```
for (index, accumulator) in iterable {
    body_expression      // 求值出新 accumulator
}(initial-accumulator-value)
```

- `index`：当前迭代下标（可用 `_` 表示不用）。
- `accumulator`：累加器，循环体里演化它；可以是元组。
- `iterable`：可迭代对象，本讲里是 `u32:0..DN` 这样的范围。
- 末尾括号：累加器的**初始值**。

#### 4.2.3 源码精读

`gcd_euclidean` 的循环体在 [xls/examples/gcd.x:21-28](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L21-L28)：

```dslx
let (gcd, _) = for (_, (a, b)) in u32:0..DN {
    if (b == uN[N]:0) {
        (a, b)
    } else {
        (b, std::iterative_div_mod(a, b).1)
    }
}((a, b));
gcd
```

逐行解读：

- `for (_, (a, b))`：下标用 `_`（丢弃），累加器是一个二元组 `(a, b)`——注意这里的 `a`、`b` 是**循环体内绑定的累加器名字**，会「遮蔽」外层函数参数 `a`、`b`。
- `in u32:0..DN`：迭代范围 `0..DN`（上界由派生参数给出）。
- 循环体是一个 `if` 表达式：若 `b == 0`，返回 `(a, b)` 不变（收敛后保持）；否则返回 `(b, a mod b)`，实现欧几里得一步。
- `std::iterative_div_mod(a, b)` 同时返回商和余数组成的元组 `(商, 余数)`，`.1` 是元组的**第二个元素**（余数），即 `a mod b`。这个函数定义在标准库 [xls/dslx/stdlib/std.x:332](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/stdlib/std.x#L332)。
- `}((a, b))`：累加器的初始值是函数入参组成的元组 `(a, b)`。
- 整个 `for` 表达式的值是最终累加器（一个二元组）。外层 `let (gcd, _) = ...` 用**元组解构**把第一个元素取出命名为 `gcd`，第二个用 `_` 丢弃。函数最后一行 `gcd` 就是返回值。

再来看 `match` 的用法，在 [xls/examples/gcd.x:31-38](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L31-L38)：

```dslx
fn gcd_binary_match<N: u32>(a: uN[N], b: uN[N], d: uN[N]) -> (uN[N], uN[N], uN[N]) {
    match (a[0:1], b[0:1]) {
      (u1:0, u1:1) => (b, a >> 1, d),
      (u1:1, u1:0) => (a, b >> 1, d),
      (u1:0, u1:0) => (a >> 1, b >> 1, d+uN[N]:1),
      (u1:1, u1:1) => ((a - b) >> 1, b, d),
    }
}
```

这是**二进制 GCD 算法**的一步。关键点：

- `a[0:1]` 是**位切片（bit slice）**，取 `a` 的第 0 位（`[起:止)` 半开区间），得到一个 `u1`。
- `match (a[0:1], b[0:1])` 匹配的是「a 的最低位、b 的最低位」组成的元组，穷举了 `u1 × u1` 的四种组合（`(0,0)/(0,1)/(1,0)/(1,1)`）。
- 每个分支形如 `模式 => 表达式`，表达式就是匹配后该返回的元组。

> 注意：这里四个分支恰好穷尽了两位的所有组合，但 DSLX 仍建议/在通用场景下要求保留兜底分支（手册 [match](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1416) 处的 WARNING 说明：当前实现要求所有 `match` 都含一个不可反驳（irrefutable）的兜底分支）。`gcd.x` 这个例子依赖了较新的穷尽匹配能力；你自己写 `match` 时，稳妥做法是加一条 `_ => ...`。

#### 4.2.4 代码实践

**实践目标**：用纸笔「手动展开」一次循环，验证累加器的演化符合欧几里得算法。

**操作步骤**：

1. 取 `a = 48, b = 18`，`N = 8`，则 `DN = 16`。
2. 仿照下表，逐轮写出累加器 `(a, b)` 的值，其中 `a mod b` 可手算：

| 轮次 | 进入时 (a, b) | b==0? | 新 (a, b) |
| --- | --- | --- | --- |
| 0 | (48, 18) | 否 | (18, 48 mod 18 = 12) |
| 1 | (18, 12) | 否 | (12, 18 mod 12 = 6) |
| 2 | (12, 6) | 否 | (6, 12 mod 6 = 0) |
| 3 | (6, 0) | 是 | (6, 0)（冻结） |
| 4…15 | (6, 0) | 是 | (6, 0) |

3. 最后一行累加器第一个元素是 `6`，即 `gcd(48, 18) = 6`。

**需要观察的现象**：一旦 `b` 变成 0，累加器被 `if` 冻结，此后所有剩余迭代都不再改变结果——这正是用「固定上界循环 + 收敛判断」来表达「不定长算法」的套路。

**预期结果**：手算得到 `gcd_euclidean(48, 18) = 6`，与 4.3 里测试 `gcd_euclidean_test` 的断言一致（[xls/examples/gcd.x:56](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L56)）。

> 若你对某一步的取模结果不确定，可标注「待本地验证」并用 `interpreter_main` 实际跑一遍对照。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `for (_, (a, b))` 写成 `for (i, (a, b))`，循环体里却没用 `i`，会发生什么？

> **答案**：DSLX 会对未使用的绑定给出告警（可用 `_` 显式表示「丢弃」来消除告警，正如 `gcd.x` 里把下标写成 `_`）。这不会改变计算结果，但属于代码风格问题。

**练习 2**：`std::iterative_div_mod(a, b).1` 里的 `.1` 是什么意思？`.0` 又会得到什么？

> **答案**：`iterative_div_mod` 返回元组 `(商, 余数)`。`.1` 取第二个元素（余数），`.0` 取第一个元素（商）。所以 `.1` 在这里等价于 `a mod b`。函数定义见 [xls/dslx/stdlib/std.x:332](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/stdlib/std.x#L332)。

**练习 3**：`a[0:1]` 取的是哪一位？`a[1:3]` 又会取几位？

> **答案**：位切片是半开区间 `[起:止)`。`a[0:1]` 取第 0 位（1 个 bit）。`a[1:3]` 取第 1、2 位（2 个 bit）。位切片详见手册 [Bit Slice Expressions](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1790)。

---

### 4.3 测试与 QuickCheck

#### 4.3.1 概念说明

写完函数，怎么知道它对？DSLX 提供两种写在**同一个 `.x` 文件里**的测试手段：

- **`#[test]` 单元测试**：和 Rust 类似。被 `#[test]` 标注的函数必须**无参、非参数化、返回 unit**（即不返回值），里面用 `assert_eq(期望, 实际)` 等断言检查。它不会被综合，只由解释器执行。见手册 [Unit Tests](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1992)。
- **`#[quickcheck]` 属性测试**：基于「性质（property）」的测试。你不写具体输入输出，而是声明一个「对任意输入都应成立的性质」，框架会**自动生成大量随机输入**来尝试打破它。被标注的函数必须**非参数化、返回 `bool`**（性质成立为 `true`）。默认跑 1000 组随机输入，可用 `test_count=...` 改。见手册 [QuickCheck](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L2039)。

这两种测试都用同一个二进制执行：`interpreter_main`。

#### 4.3.2 核心流程

用 `interpreter_main` 跑测试的整体流程：

1. 解析并类型检查 `.x` 模块。
2. 找出所有 `#[test]` / `#[quickcheck]` 函数。
3. 在 DSLX 字节码解释器里依次执行它们（默认行为）。
4. 任一断言失败或 quickcheck 返回 `false`，则该测试失败；否则通过。

几个会用到命令行标志（定义在 [xls/dslx/interpreter_main.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/interpreter_main.cc)）：

- `--execute`：默认 `true`，即默认会执行测试；设为 `false` 则只做解析与类型检查。
- `--test_filter=正则`：只跑名字能被正则**全匹配**上的测试。
- `--seed=N`：控制 quickcheck 随机输入的种子；`0` 表示非确定。
- `--evaluator=...`：执行引擎，默认 `dslx-interpreter`（DSLX 字节码解释器），也可选 `ir-jit` / `ir-interpreter`。

> 小贴士：默认 `evaluator` 是 DSLX 字节码解释器，quickcheck 会正常执行；当切到较慢的 `ir-interpreter` 时，需要额外加 `--run_quickcheck_when_interpreting` 才会跑 quickcheck（见 [interpreter_main.cc:112](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/interpreter_main.cc#L112)）。本讲用默认引擎即可。

#### 4.3.3 源码精读

`gcd.x` 里有两个单元测试和一个 quickcheck。第一个单元测试在 [xls/examples/gcd.x:54-58](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L54-L58)：

```dslx
#[test]
fn gcd_euclidean_test() {
    assert_eq(u8:6, gcd_euclidean(u8:48, u8:18));
    assert_eq(u8:6, gcd_euclidean(u8:18, u8:48));
}
```

要点：

- `#[test]` 标注紧跟其后的 `fn`；该函数无参、无返回类型。
- `assert_eq(期望, 实际)`：两边相等才通过。注意 `u8:6` 这种「类型:值」的字面量写法——DSLX 里**每个字面量都要带类型前缀**，这样位宽无歧义。
- `gcd_euclidean(u8:48, u8:18)` 通过传入 `u8` 实参让参数化参数 `N` 推导为 `8`。

quickcheck 在 [xls/examples/gcd.x:66-69](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L66-L69)：

```dslx
#[quickcheck(test_count=50000)]
fn prop_gcd_equal(a: u32, b: u32) -> bool {
    gcd_euclidean<u32:32>(a, b) == gcd_binary<u32:32>(a, b)
}
```

要点：

- `#[quickcheck(test_count=50000)]`：把这个 quickcheck 的随机输入数从默认 1000 提到 50000。
- 函数带参数 `a: u32, b: u32`：框架会**根据参数类型自动生成随机 `u32`** 作为输入。
- 返回 `bool`：性质是「两种 GCD 算法（欧几里得 vs 二进制）对任意 `a, b` 结果相等」。如果框架找到任何一组输入让两者不等，quickcheck 失败。
- `gcd_euclidean<u32:32>(a, b)` 用了**显式参数化实例化** `<u32:32>`，明确把 `N` 指定为 32。

把测试跑起来的命令（来自官方入门教程 [docs_src/tutorials/hello_xls.md](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/hello_xls.md)）形如：

```bash
$ ./bazel-bin/xls/dslx/interpreter_main gcd.x --alsologtostderr
```

成功时的输出形如：

```
[ RUN UNITTEST  ] gcd_euclidean_test
[            OK ]
...
[===============] N test(s) ran; 0 failed; 0 skipped.
```

#### 4.3.4 代码实践（本讲必做的核心实践）

**实践目标**：仿照 `gcd.x`，自己写一个 DSLX 函数判断 `u8` 是否为偶数，并为它配一个 `#[test]` 和一个 `#[quickcheck]`，最后用 `interpreter_main` 跑通。

**操作步骤**：

1. 在仓库根目录新建一个文件 `even.x`，填入下面的**示例代码**（这不是仓库原有代码，是本讲为你写的练习用代码）：

   ```dslx
   // 示例代码：判断 u8 是否为偶数
   fn is_even(x: u8) -> bool {
       // 取最低位：偶数的最低位是 0
       (x & u8:1) == u8:0
   }

   #[test]
   fn is_even_test() {
       assert_eq(true, is_even(u8:4));
       assert_eq(false, is_even(u8:7));
       assert_eq(true, is_even(u8:0));
   }

   #[quickcheck]
   fn prop_even_parity_periodic(x: u8) -> bool {
       // 性质：任意 x 与 x+2 奇偶性相同
       // （即便 u8 在 255 处回绕到 1，相邻 2 的奇偶性仍一致）
       is_even(x) == is_even(x + u8:2)
   }
   ```

2. 在仓库根目录执行（确认你已按 [u1-l2](u1-l2-build-and-run.md) 构建过 `interpreter_main`）：

   ```bash
   $ ./bazel-bin/xls/dslx/interpreter_main even.x --alsologtostderr
   ```

3. 如果只想跑某一个测试，用 `--test_filter`：

   ```bash
   $ ./bazel-bin/xls/dslx/interpreter_main even.x --test_filter=is_even_test
   ```

**需要观察的现象**：

- 终端应打印每个测试的 `[ RUN UNITTEST  ]` / `[ OK ]`，最后是 `... test(s) ran; 0 failed; 0 skipped.`。
- quickcheck `prop_even_parity_periodic` 会用随机 `u8` 跑默认 1000 次。

**预期结果**：两个测试全部通过（0 failed）。`is_even(4)=true`、`is_even(7)=false`、`is_even(0)=true`。

**如果无法本地构建/运行**：明确标注「待本地验证」。你也可以退而做**源码阅读型实践**——对照 `even.x` 与 `gcd.x` 的 `#[test]` / `#[quickcheck]`，逐行解释每条 `assert_eq` 与性质，并指出 `is_even` 用了位运算 `&` 和比较 `==`。

> 故障排查：若提示找不到 `interpreter_main`，说明还没构建，回到 [u1-l2](u1-l2-build-and-run.md) 执行 `bazel build -c opt //xls/dslx:interpreter_main`。若 quickcheck 报某个输入失败，检查你的性质是否对所有 `u8`（含 0 和 255）都成立。

#### 4.3.5 小练习与答案

**练习 1**：`#[test]` 函数和 `#[quickcheck]` 函数在**签名**上各有什么要求？

> **答案**：`#[test]` 函数必须无参、非参数化、返回 unit（无返回类型）。`#[quickcheck]` 函数可以有参数（框架按参数类型生成随机输入）、必须非参数化、必须返回 `bool`。两者都不会被综合，只由解释器执行。（见手册 [Unit Tests](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1992) 与 [QuickCheck](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L2039)。）

**练习 2**：`gcd_euclidean(u8:48, u8:18)` 里，参数化参数 `N` 是怎么确定的？如果把实参换成 `u16:48`，`N` 会变成多少？

> **答案**：`N` 由实参类型推导——传入 `u8` 实参，`N=8`；传入 `u16` 实参，`N=16`，同时派生参数 `DN = N*2 = 32`。

**练习 3**：把 4.3.4 里 quickcheck 的 `test_count` 从默认改成 50000 该怎么写？默认是多少次？

> **答案**：写成 `#[quickcheck(test_count=50000)]`（正如 `gcd.x` 的 `prop_gcd_equal`）。默认是 1000 次。

---

## 5. 综合实践

把本讲的「函数 + 类型 + `for` 累加器 + `match` + 测试 + quickcheck」串起来，完成下面这个小任务。

**任务**：实现一个 `popcount4(x: u4) -> u4` 函数，统计一个 4 位数里有多少个比特是 1。要求：

1. 用一个**计数循环**：`for (i, acc) in u32:0..4` 遍历 4 个比特位。
2. 用**元组累加器**（至少把「原始输入」和「当前计数」一起带着演化，避免在循环体里读不到输入），例如累加器形状 `(u4, u4)`（输入副本 + 计数）。
3. 循环体里用 **`if` 或 `match`** 判断第 `i` 位是否为 1（提示：位切片 `x[i+:1]` 或 `x[i:i+1]`，二者之一可用即可，请以本地解释器报错为准；不确定时标注「待确认」）。
4. 配一个 `#[test]`（例如 `popcount4(u4:0b1011) == u4:3`）和一个 `#[quickcheck]`（性质可取「`popcount4(x)` 等于一个朴素的逐位相加定义」，二者结果相等）。
5. 用 `interpreter_main` 跑通，确认 0 failed。

**参考答案草图**（这是**示例代码**，不是仓库原有代码；位切片写法可能因版本略有差异，请以本地为准）：

```dslx
// 示例代码：4 位置位计数
fn popcount4(x: u4) -> u4 {
    // 累加器：(输入副本, 计数)
    let (_, count) = for (i, (x, acc)) in u32:0..4 {
        let bit = x[i:i+1];          // 取第 i 位（半开区间），待本地确认位切片写法
        match bit {
            u1:1 => (x, acc + u4:1),
            _    => (x, acc),
        }
    }((x, u4:0));
    count
}

#[test]
fn popcount4_test() {
    assert_eq(u4:3, popcount4(u4:0b1011));
    assert_eq(u4:0, popcount4(u4:0));
    assert_eq(u4:4, popcount4(u4:0b1111));
}
```

> quickcheck 的性质可以参考 `gcd.x` 的写法：另写一个朴素的逐位 `+` 定义 `popcount4_naive`，然后 `#[quickcheck] fn prop_eq(x: u4) -> bool { popcount4(x) == popcount4_naive(x) }`。

**验收标准**：`interpreter_main` 报告所有测试通过；你能口头解释「累加器每一轮是怎么演化的」「`match` 在这里起什么作用」。

## 6. 本讲小结

- DSLX 模块（`.x` 文件）由 `import`、`fn`、`#[test]` / `#[quickcheck]` 等组成；函数体是表达式，没有 `return`，返回最后一个表达式的值。
- 类型几乎总是**带位宽**的：`bits[n]` / `uN[n]` / `u8` 等是等价写法；**参数化（parametric）**函数可以让位宽在调用时才确定（如 `gcd_euclidean<N: u32>`）。
- `for` 是**带累加器的表达式**，不是可变循环：每轮把累加器（可以是元组）演化成新值，整个 `for` 的值是最终累加器；这让它能被展开成流水线。
- `match` 是**模式匹配表达式**，可按值/位切片匹配并绑定变量；写 `match` 时稳妥起见保留 `_ =>` 兜底分支。
- 用 `#[test]`（无参、返回 unit、`assert_eq` 断言）写单元测试，用 `#[quickcheck]`（带参、返回 `bool`）写属性测试；二者都用 `interpreter_main` 执行。

## 7. 下一步学习建议

现在你已经能读懂并写出带测试的 DSLX 模块了。接下来：

- **[u1-l5 完整工具链走一遍](u1-l5-full-toolchain-walkthrough.md)**：把一个 `.x` 文件依次走过 `interpreter_main → ir_converter_main → opt_main → codegen_main`，亲眼看到它变成 Verilog。这是入门单元的收尾。
- 若想更系统了解 DSLX 的类型与表达式（数组、元组、结构体、类型转换、位切片全集），直接通读 [docs_src/dslx_reference.md](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md)，这是后续 [u2-l1](u2-l1-dslx-types-and-expressions.md) 的主要素材。
- 想看更多带测试的范本，可以浏览 `xls/examples/` 与 `xls/dslx/stdlib/` 下的 `.x` 文件，它们大多都带 `#[test]` 和 `#[quickcheck]`，是很好的模仿对象。
