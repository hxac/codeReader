# DSLX 类型系统与核心表达式

## 1. 本讲目标

学完本讲，你应当能够：

- 系统掌握 DSLX 的**全部类型**：位类型 `bits[n]` / `uN[n]` / `sN[n]` / `xN`、枚举、元组、结构体、数组、类型别名，以及类型转换 `as` 的扩展/截断规则。
- 系统掌握 DSLX 的**核心表达式语法**：字面量、各类运算符、`match`、`let`、`if`、`for`，并理解它们都是「表达式（有值）」而非「语句（产生副作用）」。
- 能区分**值（value）**与**类型注解（type annotation）**的写法，读懂运算符的类型推导规则。
- 能独立写出类型正确、可被 `interpreter_main` 验证的 DSLX 函数。

> 与上一讲的分工：[u1-l4](u1-l4-first-dslx-function.md) 教你「把一个函数写出来、跑通测试」；本讲是**系统化的语言参考**，回答「DSLX 到底有哪些类型和表达式、它们各自的语义边界在哪」。后续 [u2-l3](u2-l3-dslx-type-system.md) 会进入类型系统的**内部实现**（`type_info`、参数化推导），本讲只讲**语言层面**。

## 2. 前置知识

本讲默认你已经掌握 [u1-l4](u1-l4-first-dslx-function.md) 的内容，这里只做最小回顾，并解释几个关键概念。

回顾：DSLX 是**不可变、表达式式、定宽**的数据流语言；函数体本身就是一个表达式，没有 `return` 关键字，函数的返回值就是函数体最后那个表达式的值；`u8` / `uN[8]` / `bits[8]` 三种写法等价；参数化函数 `<N: u32>` 让位宽在调用时才确定；`for` 是带累加器的表达式。

需要先理解的几个术语：

- **类型系统（type system）**：给程序里每个值规定「它占多少 bit、按有符号还是无符号解释、是单个数还是组合结构」。DSLX 的类型系统专为硬件设计。
- **位宽（bit width）**：硬件里数据的物理宽度是固定的、必须在编译期就知道的——这是 DSLX 与 Python/C++ 等软件语言最大的区别。位宽直接决定生成的电路有多少根连线、多大面积。
- **表达式（expression）vs 语句（statement）**：表达式「求值得到一个结果」，语句「执行一个动作」。DSLX 里几乎一切都是表达式，连 `if`/`for` 都有返回值。
- **有符号 / 无符号（signed / unsigned）**：同一串 0/1 位模式，按无符号解释是一个值，按二进制补码有符号解释可能是另一个值。后文会详细讲。

## 3. 本讲源码地图

本讲围绕三个文件展开：

| 文件 | 作用 |
| --- | --- |
| [dslx_reference.md](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md) | DSLX 的**权威语言参考**，本讲的「教科书」。类型（L320 起）与表达式（L1197 起）两大节是直接依据。 |
| [xls/examples/gcd.x](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x) | 真实示例，综合运用了参数化类型、位切片、`for` 累加器、`match`、`#[test]`、`#[quickcheck]`。 |
| [intro_to_parametrics.md](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/intro_to_parametrics.md) | 参数化类型与函数的入门教程，解释「类型参数为什么会改变电路本身」。 |

---

## 4. 核心概念与源码讲解

本讲的两个最小模块：**4.1 基础类型与位宽**、**4.2 核心表达式**。

### 4.1 基础类型与位宽

#### 4.1.1 概念说明

DSLX 类型系统的核心思想：**每个值的位宽与符号性在编译期就完全确定**，因为它们要直接映射成硬件连线的根数和解释方式。

DSLX 的类型可以归为七大类：

1. **位类型（Bit Type）**：最基础的类型。`bits[n]` 表示 n 位；衍生出无符号 `uN[n]` / 简写 `u8..u64`、有符号 `sN[n]` / 简写 `s8..s64`，以及符号性也可参数化的 `xN`。
2. **枚举（Enum）**：一组有名字的常量，底层绑定到某个位类型。
3. **元组（Tuple）**：定长、异构（元素类型可不同）的组合类型，按位置访问。
4. **结构体（Struct）**：带**命名字段**的组合类型，按名字访问。
5. **数组（Array）**：同类型元素的定长序列，如 `u32[8]`。
6. **类型别名（Type Alias）**：给已有类型起一个更可读的名字。
7. **类型转换（`as`）**：严格说不算独立类型，而是跨位宽/跨符号性的运算，但理解它离不开对类型的理解，故一并讲清。

**为什么位宽要显式、且要尽量小？** 因为硬件的面积、速度、功耗都与位宽正相关。参数化教程里有一句点睛之笔：改普通函数参数只是「往电路里灌不同的数据」，而改类型参数（位宽）则是「**改变了电路本身**」：

> changing regular function parameters pumps different values through the circuit, while changing parametric values changes the circuit itself.
> —— [intro_to_parametrics.md:L22-L27](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/intro_to_parametrics.md#L22-L27)

这句话是理解整个 DSLX 类型设计的钥匙。

#### 4.1.2 核心流程

**位类型的取值范围。** 一个 n 位无符号数能表示的范围是：

\[
[0,\; 2^{n}-1]
\]

一个 n 位**二进制补码**有符号数能表示的范围是：

\[
[-2^{n-1},\; 2^{n-1}-1]
\]

例如 `u8` 的范围是 \([0,255]\)，`s8` 的范围是 \([-128,127]\)。这正是为什么后文写 `u8:256` 会触发编译期错误——它装不下。

**位类型家族写法对照表：**

| 写法 | 含义 | 示例 |
| --- | --- | --- |
| `bits[n]` | n 位（默认无符号） | `bits[256]` |
| `uN[n]` | n 位无符号（显式写法） | `uN[16]` |
| `u8` … `u64` | `bits[8]`…`bits[64]` 的简写 | `u32` |
| `sN[n]` | n 位有符号 | `sN[9]` |
| `s8` … `s64` | 有符号简写 | `s64` |
| `xN[S][n]` | 符号性 `S`（一个 `bool`）也可参数化 | `xN[true][8]` 等价于 `s8` |

`u*` / `uN[*]` / `bits[*]` 一律按无符号解释；`s*` / `sN[*]` 按有符号解释。两者主要在**比较、（变宽）乘法、除法**以及右移语义上行为不同。

**位类型的编译期属性。** 任何位类型 `T` 都带三个类属性，类似 C++ 的 `std::numeric_limits`：

- `T::MAX` —— 全 1（无符号）或最大有符号值；
- `T::ZERO` —— 全 0；
- `T::MIN` —— 最小值（无符号即 0，有符号如 `s3::MIN == s3:-4`）。

**有符号 vs 无符号的实质差异**体现在右移：`>>` 对无符号左操作数做**逻辑右移**（高位补 0），对有符号左操作数做**算术右移**（高位补符号位）。

#### 4.1.3 源码精读

**位类型的基础定义。** 参考文档明确：最基础的类型是变长位类型 `bits[n]`，并给出 `bits[1]` / `uN[1]` / `u1` 三种等价写法，以及 `bits[0]` 合法但无意义的提醒：

> 见 [dslx_reference.md:L320-L339](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L320-L339) —— 定义 `bits[n]` 与 `u1`/`u8`/`u32` 等简写。

紧接着说明：常用类型有别名（`u8`、`u32`），简写定义到 `u64`；有符号用 `s*`/`sN[*]`，简写定义到 `s64`：

> 见 [dslx_reference.md:L341-L363](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L341-L363) —— 别名上限与「有符号数在比较、乘法、除法上行为不同」。

**符号性也可参数化：`xN`。** 当一个函数需要对「有符号/无符号」两种情况都适用时，用 `xN[S][N]`，第一个参数 `S` 是 `bool`：

```dslx
fn p<S: bool, N: u32>() -> xN[S][N] { xN[S][N]:0 }
// xN[false][32] 等价于 u32，xN[true][64] 等价于 s64
```

> 见 [dslx_reference.md:L364-L388](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L364-L388)。

**位类型属性与字符常量。** `u3::MAX`/`s3::MIN` 等属性见 [dslx_reference.md:L390-L404](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L390-L404)；字符常量（如 `'\0'`、`'a'`）被隐式当作 `u8`，见 [dslx_reference.md:L406-L423](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L406-L423)。

**枚举：命名常量 + 底层位类型。** 枚举把一组常量绑定到一个位类型上，超出范围会在编译期报错，且枚举本身不允许直接做算术（需先 `as` 转成数值）：

```dslx
enum Opcode : u3 { NOP = 0, ADD = 1, SUB = 2, MUL = 3 }
```

> 见 [dslx_reference.md:L425-L470](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L425-L470) —— 枚举定义、越界编译期报错、禁止算术、按底层类型符号扩展。

**元组：定长异构，可解构。** 元组按位置访问（`t.1`），可以用 `let (a, b) = t` 解构；`_` 丢弃**恰好一个**元素，`..` 丢弃**零或多个**连续元素：

```dslx
let t = (u32:2, u8:3, true);
let (.., v) = t;   // v == true，.. 吞掉前面所有元素
```

> 见 [dslx_reference.md:L498-L582](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L498-L582) —— 元组定义、索引访问、解构、`_` 与 `..`。

**结构体：命名字段、名义类型、不可原地修改。** 结构体字段按名访问（`p.x`），构造时字段顺序可任意；不能「原地改」，要改只能构造一个新值。有便捷的更新语法 `Point3 { y: 42, ..p }`：

```dslx
struct Point3 { x: u32, y: u32, z: u32 }
fn update_y(p: Point3) -> Point3 { Point3 { y: 42, ..p } }
```

> 结构体定义与构造见 [dslx_reference.md:L584-L658](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L584-L658)；更新语法见 [dslx_reference.md:L660-L671](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L660-L671)；**名义类型**（名字不同即不同类型）见 [dslx_reference.md:L693-L718](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L693-L718)。

> ⚠️ **元组 vs 结构体的关键区别**：元组是**结构化类型（structural）**——只要元素类型一致就算同类型；结构体是**名义类型（nominal）**——字段完全相同但名字不同，也是不同类型。这是后文练习的考点。

**数组：同类型定长序列。** 元素类型必须一致，用 `a[i]` 索引；多维数组「声明由内到外、索引由外到内」；可用 `...` 用最后一个元素填满剩余位置（此时必须显式标注类型）：

```dslx
fn make_array(x: u32) -> u32[3] { [42, x, ...] }   // = [42, x, x]
```

> 见 [dslx_reference.md:L720-L776](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L720-L776) —— 数组构造、多维声明/索引、`...` 填充。

**类型别名。** 给已有类型起名，常用于给匿名元组一个可读名字（但官方更推荐用结构体，因为元组顺序脆弱）：

```dslx
type F32 = (u1, u8, u23);   // 之后 F32 与 (u1, u8, u23) 可互换使用
```

> 见 [dslx_reference.md:L814-L856](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L814-L856)。

**类型转换 `as` 与数值转换规则。** `as` 可在任意位宽、任意符号性之间转换，遵循 Rust 规则：

- **宽→窄（截断）**：保留最低位，丢弃高位（截断有符号数**不保留**原符号位的值）；
- **窄→宽（扩展）**：源无符号则零扩展，源有符号则符号扩展；
- **同宽、仅改符号性**：no-op（位模式不变）。

```dslx
let s8_m2 = s8:-2;
assert_eq(s8_m2 as u32, 0xfffffffe);   // 有符号→宽：符号扩展
assert_eq(u8:0xfe as u32, 0xfe);        // 无符号→宽：零扩展
```

> 转换语法与示例见 [dslx_reference.md:L858-L902](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L858-L902)；三条规则原文见 [dslx_reference.md:L1702-L1716](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1702-L1716)。

**真实示例：`gcd.x` 的参数化类型与派生参数。** `gcd_euclidean` 用 `<N: u32, DN: u32 = {N * u32:2}>` 声明位宽参数 `N`，并派生出一个编译期常量 `DN = 2N` 作为循环上界；参数和返回值都是 `uN[N]`：

```dslx
fn gcd_euclidean<N: u32, DN: u32 = {N * u32:2}>(a: uN[N], b: uN[N]) -> uN[N] {
    ...
}
```

> 见 [gcd.x:L20-L29](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L20-L29)。派生参数（用 `{}` 包裹编译期表达式）的详细讲解见 [intro_to_parametrics.md:L73-L111](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/intro_to_parametrics.md#L73-L111)；参数化结构体见 [intro_to_parametrics.md:L164-L198](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/intro_to_parametrics.md#L164-L198)。

> 🔗 **类型推导的内部机制**（参数化实例化、`type_info` 如何记录每个表达式的类型）属于 [u2-l3](u2-l3-dslx-type-system.md) 的内容，本讲不展开。

#### 4.1.4 代码实践

**实践目标**：亲手验证「类型转换 `as` 的扩展/截断规则」，并体会位宽是编译期属性。

**操作步骤**：

1. 新建 `/tmp/types_practice.x`，写入下面这段**示例代码**（仿照参考文档的转换测试）：

   ```dslx
   #[test]
   fn test_cast_rules() {
       // 截断：u4 取低 2 位
       assert_eq((u4:0b1100) as u2, 0);
       // 零扩展：无符号 u2 -> u4
       assert_eq((u2:0b11) as u4, 3);
       // 符号扩展：有符号 s2(=-1) -> s4
       assert_eq((s2:0b11) as s4, -1);
       // 有符号 -> 宽无符号：符号扩展
       assert_eq((s2:0b11) as u3, 0b111);
   }
   ```

2. 用解释器运行该测试（构建方式见 [u1-l2](u1-l2-build-and-run.md)）：

   ```bash
   ./bazel-bin/xls/dslx/interpreter_main /tmp/types_practice.x
   ```

**需要观察的现象**：四个断言全部通过；如果把某行改成会溢出的字面量（例如把 `u4:0b1100` 改成 `u2:5`），会得到 `TypeInferenceError`，且该错误发生在**编译期**而非运行期。

**预期结果**：测试通过，输出形如 `OK ... test_cast_rules`。若尚未构建 `interpreter_main`，则**待本地验证**（不要假装已经运行）。

#### 4.1.5 小练习与答案

**练习 1**：`u8` 与 `s8` 的取值范围分别是什么？

> **答案**：`u8`（无符号 8 位）范围 \([0, 255]\)；`s8`（有符号 8 位补码）范围 \([-128, 127]\)。

**练习 2**：`bits[12]`、`u12`、`uN[12]` 是不是同一个类型？

> **答案**：是。三者都表示 12 位无符号整数，仅写法不同（`u12` 是 `bits[12]` 的简写，`uN[12]` 是显式写法）。

**练习 3**：结构体（struct）是名义类型还是结构化类型？元组（tuple）呢？

> **答案**：结构体是**名义类型（nominal）**——名字不同即不同类型，即便字段完全相同；元组是**结构化类型（structural）**——只要元素类型序列一致就算同类型。

---

### 4.2 核心表达式

#### 4.2.1 概念说明

DSLX 最重要的一条语法事实：**函数体是「一个表达式」**。`let` / `if` / `match` / `for` 全都是表达式，都产生一个值；没有赋值语句、没有可变变量、没有 `return`。函数的返回值就是函数体最后那个表达式的值。

DSLX 字面量的写法是 `类型:值`（`Type:Value`），例如 `u16:1`、`u8:0x0c`、`s8:12`。**每个字面量都显式带类型**——这是 DSLX 与多数软件语言的一个鲜明区别，也是它能精确控制硬件位宽的基础。

#### 4.2.2 核心流程

**运算符分类与类型签名。** DSLX 的运算符按「操作数类型约束」分类，理解这些类型签名是写对表达式的前提：

| 类别 | 运算符 | 类型签名 |
| --- | --- | --- |
| 一元 | `!`（按位非）、`-`（取负，二进制补码） | `(xN[N]) -> xN[N]` |
| 同类型算术/位运算 | `\|` `&` `^` `+` `-` `*` | `(xN[N], xN[N]) -> xN[N]` |
| 逻辑 | `\|\|` `&&` | `(bool, bool) -> bool`（`bool` 即 `u1`） |
| 移位 | `<<` `>>` | `(xN[M], uN[N]) -> xN[M]`（右操作数必须无符号，宽度可不同） |
| 比较 | `==` `!=` `>=` `>` `<=` `<` | `(T, T) -> bits[1]`（两操作数同类型，结果是 1 位） |
| 拼接 | `++` | 无符号拼接，左操作数成为最高位 |

**关键推导规则**：加法 `+` 的类型规则是 `(T, T) -> T`——两操作数**必须同类型**，结果**也是同类型**，**不会自动变宽**。也就是说 `u8 + u8` 的结果仍是 `u8`，溢出会回绕；想要进位必须先手动扩宽（如标准库 `std::uadd_with_overflow`）。

**右移 `>>` 的符号性依赖**：取决于左操作数——无符号做逻辑右移（补 0），有符号做算术右移（补符号位）。

**四大控制表达式：**

- **`let`**：词法作用域的绑定，本身是表达式。`let a = e1; e2` 的值就是 `e2`，`a` 只在 `e1; e2` 这段范围内有效。
- **`if`**：表达式而非语句，等价于 C 的三目 `?:`。`else` 可省略，省略时等价于 `else { () }`（返回 unit），此时 `then` 分支也必须返回 `()`。
- **`match`**：模式匹配。可匹配字面量、命名常量、元组解构、范围（`1..3` 半开、`4..=5` 闭区间）、多模式（`1 | 3`）。⚠️ **目前所有 `match` 都必须有一个兜底分支 `_ => ...`**，即便其他分支看似已穷尽。
- **`for`**：可综合的「计数循环」，带一个**累加器（accumulator）**。每轮把累加器演化成新值，整个 `for` 表达式的值就是**最后一轮的累加器**。流水线生成时它会被展开（unroll）成各级。

**`for` 的语法骨架**：

```
for (index, accumulator) in iterable {
    body-expression        // 必须产出「新的累加器」
}(initial-accumulator-value)
```

累加器可以是任意类型，尤其是**元组**——这样能在一轮里同时演化多个值（这正是 `gcd.x` 的做法）。

#### 4.2.3 源码精读

**字面量 `Type:Value`。** 字面量必须能装进所标注的位宽，否则编译期报错（`u8:256` 会得到 `TypeInferenceError`）：

> 见 [dslx_reference.md:L1199-L1227](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1199-L1227)。

**一元、二元、逻辑、移位、比较、拼接运算符。** 各类的类型签名与约束：

> 一元见 [dslx_reference.md:L1236-L1243](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1236-L1243)；同类型二元算术/位运算（`(T,T)->T`，想进位要先扩宽）见 [dslx_reference.md:L1251-L1271](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1251-L1271)；逻辑见 [dslx_reference.md:L1272-L1278](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1272-L1278)；移位与「右移符号性依赖左操作数」见 [dslx_reference.md:L1280-L1301](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1280-L1301)；比较见 [dslx_reference.md:L1303-L1313](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1303-L1313)；拼接 `++`（左为最高位）见 [dslx_reference.md:L1315-L1334](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1315-L1334)。

**块表达式。** 用 `{ ... }` 限定作用域，其值是其中最后一个表达式（缺省为 `()`）；DSLX 无生命周期概念、名字可重新绑定：

> 见 [dslx_reference.md:L1336-L1364](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1336-L1364)。

**`match` 表达式。** 基本用法（匹配值并绑定变量）见 [dslx_reference.md:L1366-L1396](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1366-L1396)。三个要点各有出处：

- **必须兜底**：即便其他分支看似穷尽，也**当前要求**有一个 `_ =>` 不可反驳分支，见 [dslx_reference.md:L1416-L1421](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1416-L1421)；
- **范围模式** `1..3`（半开）/ `4..=5`（闭），见 [dslx_reference.md:L1423-L1445](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1423-L1445)；
- **多模式** `1 | 3` 与**冗余模式检测**（语法相同的重复分支会报错），见 [dslx_reference.md:L1447-L1496](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1447-L1496)。

**`let` 表达式。** 词法作用域绑定，与 ML 家族一致：

> 见 [dslx_reference.md:L1498-L1523](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1498-L1523)。

**`if` 表达式。** 是表达式（非语句），等价三目；`else` 可省略（缺省返回 `()`）：

> 见 [dslx_reference.md:L1525-L1556](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1525-L1556)；省略 `else` 的等价语义见 [dslx_reference.md:L1540-L1551](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1540-L1551)。

**可迭代表达式与 `for`。** 可迭代对象有两种：范围 `m..n`（半开）/ `m..=n`（闭），以及 `enumerate`（产出 `(index, value)` 对）。`for` 是带累加器的表达式，累加器可为元组：

> 可迭代表达式见 [dslx_reference.md:L1582-L1614](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1582-L1614)；`for` 语法骨架与「演化累加器」语义见 [dslx_reference.md:L1616-L1660](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1616-L1660)。

**类型推导规则示例。** 参考文档以 `+` 为例说明推导规则 `(T, T) -> T`：

> 见 [dslx_reference.md:L955-L963](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L955-L963)。完整运算符优先级表见 [dslx_reference.md:L1962](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1962)（Operator Precedence 节）。

**真实示例 1：`gcd.x` 的 `match` 位切片。** `gcd_binary_match` 对两个数的最低位做模式匹配——`a[0:1]` 是半开位切片 `[0:1)`，取出 1 个最低位（`u1`），然后对 `(u1, u1)` 的四种组合分别给出返回元组：

```dslx
match (a[0:1], b[0:1]) {
  (u1:0, u1:1) => (b, a >> 1, d),
  (u1:1, u1:0) => (a, b >> 1, d),
  (u1:0, u1:0) => (a >> 1, b >> 1, d+uN[N]:1),
  (u1:1, u1:1) => ((a - b) >> 1, b, d),
}
```

> 见 [gcd.x:L31-L38](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L31-L38)。注意此处四个分支虽看似穷尽，但 DSLX 仍允许这种写法；位切片的完整语义（含 `[:-2]`、`[start+:uN]` 等形式）见 [dslx_reference.md:L1790-L1930](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md#L1790-L1930)。

**真实示例 2：`gcd.x` 的 `for` 三元组累加器。** `gcd_binary` 用 `for` 同时演化三个值 `(a, b, d)`，初始累加器是 `((a, b, uN[N]:0))`，整个 `for` 的结果是最后一轮的三元组，再解构取用：

```dslx
let (a, _, d) = for (_, (a, b, d)) in u32:0..DN {
    if (a == b) { (a, b, d) }
    else if (a < b) { gcd_binary_match(b, a, d) }
    else { gcd_binary_match(a, b, d) }
}((a, b, uN[N]:0));
(a << d)
```

> 见 [gcd.x:L41-L52](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L41-L52)。这里 `let (a, _, d) = ...` 同时展示了元组解构与 `_` 丢弃元素；`if/else if/else` 链展示了 `if` 作为表达式返回不同三元组。

#### 4.2.4 代码实践

**实践目标**：通过阅读 `gcd.x` 透彻理解 `match` 的位切片匹配，并亲手写一个带范围与多模式的 `match`。

**操作步骤**：

1. 打开 [gcd.x:L31-L38](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/gcd.x#L31-L38)，按下表填出 `gcd_binary_match` 四个分支的匹配条件（`a`、`b` 各自的最低位）：

   | `a` 最低位 | `b` 最低位 | 返回的 `(a', b', d')` |
   | --- | --- | --- |
   | 0 | 1 | `(b, a>>1, d)` |
   | 1 | 0 | ? |
   | 0 | 0 | ? |
   | 1 | 1 | ? |

2. 在 `/tmp/match_practice.x` 写一个**示例代码**，用范围模式与多模式 `|` 给 `u8` 分档，并配 `#[test]`：

   ```dslx
   fn bucket(x: u8) -> u2 {
       match x {
           0 | 1 => u2:0,        // 多模式
           2..=10 => u2:1,       // 闭区间范围
           11..100 => u2:2,      // 半开范围
           _ => u2:3,            // 兜底（必需）
       }
   }

   #[test]
   fn test_bucket() {
       assert_eq(bucket(0), u2:0);
       assert_eq(bucket(7), u2:1);
       assert_eq(bucket(50), u2:2);
       assert_eq(bucket(200), u2:3);
   }
   ```

3. 运行：`./bazel-bin/xls/dslx/interpreter_main /tmp/match_practice.x`。

**需要观察的现象**：测试通过；若删掉 `_ => u2:3` 兜底分支，会得到「match 必须有不可反驳分支」的编译期错误。

**预期结果**：四个断言通过。若尚未构建 `interpreter_main`，则**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`if cond { x }` 省略了 `else` 分支时，整个 `if` 表达式的类型/值是什么？

> **答案**：等价于 `if cond { x } else { () }`，整个 `if` 的值在不满足条件时为 unit `()`；因此 `x` 所在的 `then` 分支也必须返回 `()`，否则两个分支类型不一致会报类型错误。

**练习 2**：表达式 `u8:200 + u8:100` 的结果类型和数值是什么？

> **答案**：类型仍是 `u8`（加法规则 `(T,T)->T`，不自动扩宽）；数值为 `(200+100) mod 256 = 300 mod 256 = 44`，发生回绕。要捕获进位需先扩宽或用 `std::uadd_with_overflow`。

**练习 3**：`for` 循环的「累加器」可以是哪些类型？为什么 `gcd.x` 要用元组当累加器？

> **答案**：累加器可以是任意合法类型（位类型、数组、元组、结构体等）。用元组当累加器可以在一轮迭代里**同时演化多个相关变量**（如 `gcd_binary` 的 `(a, b, d)`），并在循环结束后一次性解构取出，契合 DSLX「不可变、表达式式数据流」的风格。

---

## 5. 综合实践

把本讲的类型与表达式串起来，完成规格要求的任务：**写一个 DSLX 函数，输入 `uN[16]`，返回其高/低字节组成的元组；再写一个 `match` 表达式按最高位（视作「符号位」）分支；用 `#[test]` 验证。**

> 说明：`uN[16]` 是无符号类型，本身没有真正的符号位。这里我们把它的**最高有效位（bit 15）当作符号位**来分类，作为 `match` + 位切片的练习。

**操作步骤**：

1. 新建 `/tmp/split_classify.x`，写入下面的**示例代码**（待本地验证）：

   ```dslx
   // 把 16 位数拆成 (高字节, 低字节)
   fn split_bytes(x: uN[16]) -> (u8, u8) {
       let hi = (x >> uN[16]:8) as u8;
       let lo = (x & uN[16]:0xff) as u8;
       (hi, lo)
   }

   // 把最高位当符号位：0 -> "非负"，1 -> "负"
   fn classify_by_msb(x: uN[16]) -> u2 {
       match x[-1:] {           // 位切片 [-1:] 取最高 1 位
           u1:0 => u2:0,        // 最高位为 0
           _ => u2:1,           // 最高位为 1（兜底，必需）
       }
   }

   #[test]
   fn test_split_bytes() {
       // 0xABCD: 高字节 0xAB=171，低字节 0xCD=205
       assert_eq(split_bytes(uN[16]:0xABCD), (u8:0xAB, u8:0xCD));
       assert_eq(split_bytes(uN[16]:0x0102), (u8:0x01, u8:0x02));
   }

   #[test]
   fn test_classify_by_msb() {
       assert_eq(classify_by_msb(uN[16]:0x7FFF), u2:0);  // 最高位 0
       assert_eq(classify_by_msb(uN[16]:0x8000), u2:1);  // 最高位 1
       assert_eq(classify_by_msb(uN[16]:0xFFFF), u2:1);
       assert_eq(classify_by_msb(uN[16]:0x0000), u2:0);
   }
   ```

2. 运行验证：

   ```bash
   ./bazel-bin/xls/dslx/interpreter_main /tmp/split_classify.x
   ```

**这段代码综合运用了本讲的哪些知识点**：

- 类型：`uN[16]`、`u8`、`u2`、元组 `(u8, u8)`、字面量 `uN[16]:0xABCD`；
- 运算符：移位 `>>`、按位与 `&`、类型转换 `as`（`uN[16]` 截断为 `u8`）；
- 表达式：`let` 绑定、元组字面量与构造、`match` + 位切片 `[-1:]` + 兜底分支；
- 测试：`#[test]` + `assert_eq`。

**需要观察的现象**：两个测试各 4 条断言全部通过；若把 `match` 的 `_ =>` 兜底去掉会编译失败；若把 `x & uN[16]:0xff` 误写成 `x & uN[8]:0xff` 会得到类型错误（`&` 要求两边同类型）。

**预期结果**：全部通过，输出 `Result: OK`。若尚未构建 `interpreter_main`，则**待本地验证**。

> 进阶自测：把 `classify_by_msb` 改成**真正的有符号判断**——先用 `x as sN[16]` 把位模式重解释成有符号数，再用 `match s < sN[16]:0 { true => .., false => .. }` 或对 `s[-1:]` 切片来分类。注意位切片官方限定在**无符号**位类型上，所以应切片原始的 `x` 而非 `s`。

---

## 6. 本讲小结

- DSLX 的类型为硬件而生：**每个值的位宽与符号性在编译期完全确定**，直接决定电路连线的宽度与解释方式；改类型参数（位宽）等于改变电路本身。
- 七大类类型：**位类型**（`bits[n]`/`uN`/`sN`/`xN` + `u8..u64` 简写 + `::MAX/::ZERO/::MIN` 属性）、**枚举**、**元组**（结构化）、**结构体**（名义）、**数组**、**类型别名**；跨类型用 **`as`** 转换，遵循「窄→宽按符号性扩展、宽→窄截断」。
- DSLX 函数体是**一个表达式**：`let` / `if` / `match` / `for` 都有值，没有语句式赋值与 `return`；字面量写作 `类型:值`。
- 运算符按类型约束分类，核心规则 `+ : (T,T)->T` 意味着**算术不自动扩宽**，溢出回绕；`>>` 的语义依赖左操作数的符号性。
- `match` 支持**值/常量/元组解构/范围/多模式**匹配，但目前**必须有 `_ =>` 兜底**；`for` 是带**累加器**的可综合循环，累加器常为元组以同时演化多个值。
- 真实代码 `gcd.x` 把上述要素（`uN[N]` 派生参数、位切片 `match`、三元组 `for` 累加器、解构）融为一炉，是最好的对照阅读样本。

## 7. 下一步学习建议

- **想看清这些类型/表达式在编译器里如何表示** → 下一讲 [u2-l2 DSLX 前端：扫描、解析与 AST](u2-l2-dslx-frontend-parser-ast.md)，你会看到本讲的每一个类型和表达式都对应 `ast.h` 里的一个 AST 节点。
- **想搞懂类型推导与参数化实例化的内部机制** → [u2-l3 DSLX 类型推导与检查](u2-l3-dslx-type-system.md)，讲解 `type` / `type_info` / `parametric_env`。
- **想理解 `for` 累加器、`let` 等如何被翻译成 IR** → [u3-l4 从 DSLX 到 IR 的转换](u3-l4-dslx-to-ir-conversion.md)。
- **随时可查的权威手册** → [docs_src/dslx_reference.md](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/dslx_reference.md)，本讲所有结论均出自此文档。
