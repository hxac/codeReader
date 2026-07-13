# 宏体系与条件编译工程化

## 1. 本讲目标

本讲是「测试、追踪与工程化」单元的收尾篇，专题讲解 NEMU 工程化的地基——一套用 C 预处理器搭起来的「宏基础设施」。

NEMU 要用**同一份源码**模拟四种 ISA（x86 / mips32 / riscv / loongarch32r）、两种运行模式（System / AM）、多种构建目标（Native ELF / Shared / AM），还要按需开关调试、追踪、设备、差分测试等数十个特性。能让这种「多形态」可行，靠的不是无数个分支，而是 `include/macro.h` 里几十个精心设计的宏。

学完本讲，你应该能够：

1. 理解 `MUXDEF` 如何在**不使用 `#ifdef`** 的前提下，根据「某个宏是否被定义」在编译期二选一，并清楚它的边界（定义为 0、1、还是完全未定义的区别）。
2. 掌握 `IFDEF` / `IFNDEF` / `IFONE` / `IFZERO` 四个条件编译简写宏的语义，看懂它们在源码里的贯穿使用。
3. 看懂 `concat` / `str` / `ARRLEN` / `BITS` / `SEXT` / `MAP` 等工具宏的原理与典型用法。
4. 从端到端走通一条 CONFIG 链路：`Kconfig` 描述选项 → `autoconf.h` 产出 `#define CONFIG_XXX` → `macro.h` 的宏在源码里条件编译，并能自己新增一个 CONFIG 开关。

## 2. 前置知识

本讲假设你已经读过 u1-l2（构建系统）与 u1-l4（ISA 抽象层），知道 `menuconfig` 会产出 `.config` / `auto.conf` / `autoconf.h` 三份文件，也知道 `__GUEST_ISA__` 这个宏驱动了类型拼接。本讲要回答的是：**那些「类型拼接」「按配置裁剪源码」的底层动作，到底是怎么用宏写出来的。**

需要回忆的几个 C 预处理器概念：

- **宏的字符串化（stringizing）**：`#x` 把宏参数 `x` 变成字符串字面量。
- **记号粘贴（token pasting）**：`x ## y` 把两个记号拼成一个，比如 `a` 与 `b` 拼成 `ab`。
- **`#define` 与「已定义/未定义」**：一个宏要么被定义（可能带替换体），要么未被定义；`#ifdef X` 只关心「是否定义」，不关心替换体是什么。
- **关键限制**：`#ifdef` 是**预处理指令**，不能出现在另一个宏的展开体里。也就是说，你**没法写**一个「内部用 `#ifdef` 来选择分支」的宏。这正是 NEMU 要发明 `MUXDEF` 的根本原因。

> 一个贯穿全讲的核心问题：**怎么在「宏展开」内部判断「某个宏是否被定义」？** `#ifdef` 用不了，NEMU 的答案是下面要讲的「占位符 + 逗号」技巧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/macro.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h) | 全部宏的定义集合，本讲的「主战场」。 |
| [include/common.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h) | 引入 `autoconf.h` 与 `macro.h`，并用 `MUXDEF` 定义 `word_t` / `paddr_t` 等核心类型。 |
| [Kconfig](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig) | 顶层配置描述，定义 ISA / 引擎 / 模式 / 目标四组开关。 |
| [src/memory/Kconfig](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig) | 定义 `CONFIG_MEM_RANDOM` 等，是本讲实践任务的参照样板。 |
| [src/filelist.mk](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/filelist.mk) | 用 `SRCS-$(CONFIG_*)` 在 Make 侧按配置收集/排除源文件。 |
| [src/memory/paddr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c) | 用 `IFDEF` 条件编译的典型现场（`CONFIG_MEM_RANDOM` / `CONFIG_DEVICE`）。 |
| [src/device/keyboard.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c) | `MAP` X-macro 的最佳示例：一份按键表驱动 enum 与映射表两处。 |
| [tools/kconfig/confdata.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/kconfig/confdata.c) | kconfig 生成 `autoconf.h` 的代码，用来验证「未设置的 bool 在头文件里不被定义」。 |

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：先讲最底层的「记号操作」（4.1），再讲「按是否定义做选择」的 `MUXDEF` 家族（4.2），接着把选择能力封装成更顺手的 `IFDEF`（4.3 属于 4.2 的延伸，合并讲解），然后是位操作 `BITS`/`SEXT`（4.3）与函数式 `MAP`（4.4），最后用一条完整的 CONFIG 链路把所有零件串起来（4.5）。

### 4.1 concat / str / ARRLEN：词法层的记号操作

#### 4.1.1 概念说明

这一组宏解决的是「编译期文本处理」问题：把宏参数变成字符串、把两个记号拼成一个、算出数组长度。它们是后面 `MUXDEF`、`isa.h` 类型拼接的底层零件。

要重点理解一个 C 预处理器的「坑」：**当宏参数本身又是一个宏时，`##` 会阻止它被展开。** 举例：若 `A` 是定义为 `foo` 的宏，直接写 `A ## B` 得到的是 `AB`，而不是 `fooB`，因为 `##` 两边的参数在粘贴前**不会**先展开。解决办法是「加一层间接」：先在一个中间宏里把参数展开，再传给真正做 `##` 的宏。这就是 `macro.h` 里 `concat_temp` / `concat` 分两层的原因。

#### 4.1.2 核心流程

三个宏的展开流程：

- **`str(x)`**：两层。外层 `str(x)` → 调 `str_temp(x)`；此时 `x` 已被展开（比如 `__GUEST_ISA__` 展开成 `riscv32`），再由内层 `str_temp` 用 `#` 字符串化，得到 `"riscv32"`。同样必须两层，否则字符串化发生在展开之前。
- **`concat(x, y)`**：两层。`concat(x, y)` → `concat_temp(x, y)` 让 `x`/`y` 先展开，再由 `concat_temp` 做 `x ## y`。还有 `concat3`/`concat4`/`concat5` 做更长拼接。
- **`ARRLEN(arr)`**：编译期求静态数组元素个数，公式为 \(\text{ARRLEN} = \lfloor \text{sizeof(arr)} / \text{sizeof(arr[0])} \rfloor\)，转型为 `int`。配合表驱动设计，加表项不用改长度常量。

#### 4.1.3 源码精读

[include/macro.h:L21-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L21-L29) 定义了字符串化、字符串常量长度与数组长度三个工具宏。注意 `str_temp` 与 `str`、`concat_temp` 与 `concat` 都是「内层做实际动作、外层先展开参数」的两层结构。

[include/macro.h:L31-L36](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L31-L36) 定义了 `concat` 家族，`concat3/4/5` 都建立在 `concat` 之上逐级拼接。

`concat` 最经典的应用在 ISA 抽象层：框架侧只有宏 `__GUEST_ISA__`（被 Makefile 注入为 `riscv32` 等），实现侧有 `riscv32_CPU_state` 这样的具体类型，二者用 `concat` 缝合成同一个 token：

[include/isa.h:L24-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L24-L25) 用 `concat(__GUEST_ISA__, _CPU_state)` 在预处理期拼出 `riscv32_CPU_state`，于是框架代码里写 `CPU_state`、实现侧提供 `riscv32_CPU_state`，二者自动对接（详见 u1-l4、u5-l14）。

`ARRLEN` 的典型应用是命令表与正则规则表的「条目计数」：

[src/monitor/sdb/sdb.c:L70](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L70) 与 [src/monitor/sdb/expr.c:L44](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L44) 分别用 `ARRLEN(cmd_table)` 和 `ARRLEN(rules)` 自动得到表长，新增命令/规则时无需维护单独的计数常量。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `concat` 的两层展开与「不加层会出错」的差异。

**操作步骤**：

1. 在任意目录写一个最小测试程序 `t.c`（**示例代码，非项目源码**）：

   ```c
   #define concat_temp(x, y) x ## y
   #define concat(x, y) concat_temp(x, y)
   #define ISA riscv32
   typedef int ISA ## _CPU_state;        /* 直接 ##：得到 ISA_CPU_state，错误 */
   typedef int concat(ISA, _CPU_state);  /* 两层：得到 riscv32_CPU_state，正确 */
   ```

2. 用 `gcc -E t.c` 只做预处理，查看输出。

**需要观察的现象**：第一行展开成 `typedef int ISA_CPU_state;`（`ISA` 没展开就被粘贴了），第二行展开成 `typedef int riscv32_CPU_state;`。

**预期结果**：第二行才正确，证明 `##` 会阻止参数展开，必须加一层间接宏。这正是 `macro.h` 采用两层的原因。

#### 4.1.5 小练习与答案

**练习 1**：`str(__GUEST_ISA__)` 与 `#__GUEST_ISA__` 的结果有何不同？

**答案**：`str(__GUEST_ISA__)` 先把 `__GUEST_ISA__` 展开成 `riscv32`，再字符串化得到 `"riscv32"`；而直接写 `#__GUEST_ISA__` 会立刻字符串化得到 `"__GUEST_ISA__"`，宏不会被展开。所以字符串化也必须两层。

**练习 2**：`ARRLEN` 能否用于指针指向的数组？为什么？

**答案**：不能。`sizeof(指针)` 得到的是指针本身大小（如 8），而非数组总大小。`ARRLEN` 只对「可见完整定义的静态数组」有效，传退化后的指针参数会得到错误结果。

---

### 4.2 MUXDEF 家族与 IFDEF / IFNDEF：编译期的「多路选择」

这是本讲最重要的模块。它解决的核心问题是：**如何在宏展开内部判断「某个宏是否被定义」，从而在编译期二选一——而 `#ifdef` 用不了。**

#### 4.2.1 概念说明

NEMU 提供了一组「多路选择」宏：

| 宏 | 选择 X 的条件 | 选择 Y 的条件 |
| --- | --- | --- |
| `MUXDEF(macro, X, Y)` | `macro` 已定义（含定义为 0） | `macro` 未定义 |
| `MUXNDEF(macro, X, Y)` | `macro` 未定义 | `macro` 已定义（与上相反） |
| `MUXONE(macro, X, Y)` | `macro` 被定义为 1 | 其它 |
| `MUXZERO(macro, X, Y)` | `macro` 被定义为 0 | 其它 |

对应的「条件编译简写」是 `IFDEF` / `IFNDEF` / `IFONE` / `IFZERO`，它们把 `MUXDEF` 的结果再用 `__KEEP` / `__IGNORE` 包一层，直接决定「代码留还是不留」。

它们共享同一个巧妙机制：**用「占位符宏是否带逗号」来编码「目标宏是否定义」，再用变参宏 `CHOOSE2nd` 取第 2 个参数。** 这绕开了 `#ifdef` 不能用在宏展开内部的限制。

#### 4.2.2 核心流程

以 `MUXDEF(CONFIG_MEM_RANDOM, X, Y)` 为例，假设 `CONFIG_MEM_RANDOM` 被定义为 `1`（即 `#define CONFIG_MEM_RANDOM 1`）。展开链路如下：

1. `MUXDEF(1, X, Y)` → `MUX_MACRO_PROPERTY(__P_DEF_, 1, X, Y)`
2. → `MUX_WITH_COMMA(concat(__P_DEF_, 1), X, Y)`，其中 `concat(__P_DEF_, 1)` 拼成 `__P_DEF_1`
3. `__P_DEF_1` 被定义为 `X,`（**末尾带一个逗号**），所以 `MUX_WITH_COMMA(__P_DEF_1, X, Y)` → `CHOOSE2nd(__P_DEF_1 X, Y)` → `CHOOSE2nd(X, X, Y)`（占位符的逗号把实参「撑开」成三个）
4. `CHOOSE2nd(a, b, ...) = b`，于是取到第 2 个参数 `X`。**结果：X。**

反过来，若 `CONFIG_MEM_RANDOM` 完全未定义：

1. `concat(__P_DEF_, CONFIG_MEM_RANDOM)` 拼成 `__P_DEF_CONFIG_MEM_RANDOM`，这是个**未定义**的宏，展开为空。
2. `MUX_WITH_COMMA(空, X, Y)` → `CHOOSE2nd( X, Y)` → `CHOOSE2nd(X, Y)`，只有两个实参，取第 2 个 `Y`。**结果：Y。**

**关键洞察**：决定结果的不是 `#ifdef`，而是「占位符 `__P_DEF_xxx` 这个 token 是否恰好是一个已定义宏」。定义了就带逗号、撑开成 3 个参数取 X；没定义就是空、只有 2 个参数取 Y。这套机制完全在「记号粘贴 + 变参选择」里完成，合法地出现在任何宏展开内部。

`__P_DEF_0` 与 `__P_DEF_1` 都被定义（都等于 `X,`），所以 `MUXDEF` 把「定义为 0」和「定义为 1」都算作「已定义」。这与 kconfig 的行为配合后会出现一个微妙点，见 4.5。

> `IFDEF(macro, code)` 就是 `MUXDEF(macro, __KEEP, __IGNORE)(code)`：已定义时挑 `__KEEP`（保留 `code`），未定义时挑 `__IGNORE`（吞掉 `code`）。

#### 4.2.3 源码精读

[include/macro.h:L40-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L40-L42) 是机制的骨架：`CHOOSE2nd` 永远取第 2 个参数，`MUX_WITH_COMMA` 把「占位符」与候选 `a` 拼在一起，`MUX_MACRO_PROPERTY` 用 `concat` 把属性前缀 `__P_DEF_` 接到目标宏名上。

[include/macro.h:L43-L52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L43-L52) 定义了四类占位符与四个选择宏。注意 `__P_DEF_0`/`__P_DEF_1` 都在（MUXDEF 认为 0 和 1 都算「已定义」），而 `__P_ONE_` 只有 `_1`（MUXONE 只认值为 1）、`__P_ZERO_` 只有 `_0`（MUXZERO 只认值为 0）。

[include/macro.h:L67-L77](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L67-L77) 把多路选择封装成条件编译简写：`__KEEP(...)` 原样保留，`__IGNORE(...)` 吞掉一切，于是 `IFDEF`/`IFNDEF`/`IFONE`/`IFZERO` 就是「按宏状态保留或丢弃一段代码」。

`MUXDEF` 在 `common.h` 里被用来定义系统最底层的「宽度基因」类型：

[include/common.h:L38-L44](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L38-L44) 用 `MUXDEF(CONFIG_ISA64, uint64_t, uint32_t)` 让 `word_t` 在 64 位 ISA 下是 `uint64_t`、否则 `uint32_t`；`FMT_WORD` 同理选出 `printf` 的格式串。这是「一套源码适配多宽度」的关键。

`IFDEF` 在设备初始化里被高频用于「按配置装配外设」：

[src/device/device.c:L76-L89](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L76-L89) 的 `init_device()` 用一连串 `IFDEF(CONFIG_HAS_xxx, init_xxx())`：只有某设备在 menuconfig 里被开启（`CONFIG_HAS_SERIAL` 等）才调用它的初始化函数，关掉的设备对应的调用在预处理阶段就被整行删掉，零运行时开销。

`MUXDEF` 还能在「字符串拼接」里用，比如构造 ISA 名：

[tools/spike-diff/difftest.cc:L104](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/spike-diff/difftest.cc#L104) 用 `"RV" MUXDEF(CONFIG_RV64, "64", "32") MUXDEF(CONFIG_RVE, "E", "I") "MAFDC"` 拼出 `"RV32IMAFC"` 这样的 ISA 字符串，开关直接体现在字面量里。

#### 4.2.4 代码实践

**实践目标**：用 `gcc -E` 观察 `MUXDEF` 与 `IFDEF` 的真实展开，亲眼确认「已定义选 X、未定义选 Y」。

**操作步骤**：

1. 写一个最小头 `mt.c`（**示例代码**），include 真实的 `macro.h`：

   ```c
   #include <macro.h>
   /* 故意只定义 A，不定义 B */
   #define A 1
   int x = MUXDEF(A, 111, 222);
   int y = MUXDEF(B, 111, 222);
   void f(void){ IFDEF(A, int kept = 1;) IFDEF(B, int removed = 1;) }
   ```

   编译时需要让预处理器找得到头文件，例如 `gcc -E -I include mt.c`（在项目根目录执行）。

**需要观察的现象**：`x` 被替换成 `111`、`y` 被替换成 `222`；`f` 的函数体里只剩 `int kept = 1;`，`int removed = 1;` 整句消失。

**预期结果**：与现象一致，证明选择完全发生在编译期，没有运行时分支。若结果不符，多半是没 include 到正确的 `macro.h` 或 `A`/`B` 定义位置不对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MUXDEF` 必须用「占位符 + 逗号 + `CHOOSE2nd`」这一整套，而不能直接写 `#ifdef`？

**答案**：因为 `#ifdef` 是预处理**指令**，只能出现在源文件顶层、不能写在某个宏的替换体里再被展开。`MUXDEF` 要在「表达式中」「类型定义中」「函数调用中」随处使用，必须用纯记号操作（拼接 + 变参选择）来模拟判断。

**练习 2**：`MUXDEF(CONFIG_X, A, B)` 与 `MUXONE(CONFIG_X, A, B)` 的区别是什么？给定 `#define CONFIG_X 0`，两者各选谁？

**答案**：`MUXDEF` 看「是否定义」，`MUXONE` 看「是否定义为 1」。当 `CONFIG_X` 定义为 0 时，`MUXDEF` 选 A（0 也算已定义），`MUXONE` 选 B。`__P_DEF_0` 存在而 `__P_ONE_0` 不存在正是这个区别的体现。

---

### 4.3 BITS / SEXT：编译期的位切片与符号扩展

#### 4.3.1 概念说明

译码指令时（u3-l11、u5-l16），经常要从一条 32 位指令字里「切出某几位」当作 opcode、寄存器号或立即数。`macro.h` 提供了两个位操作宏，写法刻意模仿 Verilog 的 `x[hi:lo]`，让硬件背景的读者一眼看懂。

- `BITS(x, hi, lo)`：取 `x` 的第 `lo` 到 `hi` 位（含两端），等价于 Verilog 的 `x[hi:lo]`。
- `SEXT(x, len)`：把 `x` 的低 `len` 位**符号扩展**成 64 位，用于把「立即数字段」还原成正确的（可能为负的）整数。
- `BITMASK(bits)`：低 `bits` 位全 1 的掩码。

#### 4.3.2 核心流程

`BITS` 的数学定义：

\[
\texttt{BITS}(x, hi, lo) = (x \gg lo)\ \&\ ((2^{hi-lo+1}-1))
\]

即先右移把目标字段移到最低位，再用「`hi-lo+1` 个 1」的掩码截取。

`SEXT(x, len)` 的原理更巧妙：它利用 C 的**位域（bit-field）**。声明一个结构体，里面放一个 `int64_t n : len` 的位域（宽度恰好是 `len`），把 `x` 赋给它。C 标准规定**有符号位域赋值会发生符号扩展**，于是读回 `(uint64_t)n` 时，第 `len-1` 位（符号位）被复制填充到高位。这比手写 `((x ^ sign) - sign)` 之类的算术更简洁、可读性更好。

需要注意 `SEXT` 用了语句表达式 `({ ... })`（GCC 扩展），所以它是「值」，能直接用在表达式里。

#### 4.3.3 源码精读

[include/macro.h:L86-L88](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L86-L88) 定义了三个位操作宏。`SEXT` 那行声明了匿名结构体位域 `{ int64_t n : len; }`，靠有符号位域赋值自动完成符号扩展。

最典型的应用在 RISC-V 指令译码。RV32I 指令是定长 32 位，立即数散布在不同字段：

[src/isa/riscv32/inst.c:L32-L40](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L32-L40) 用 `BITS` 从指令字 `i` 里切出 `rs1`（19:15）、`rs2`（24:20）、`rd`（11:7）等字段；`immI/immU/immS` 三个宏用 `BITS` 取出立即数位段后，再用 `SEXT` 符号扩展（例如 `immI` 取 31:20 共 12 位后 `SEXT(..., 12)`）。没有 `SEXT`，负立即数（如 `addi a0, a0, -1`）就会算错。

> MIPS、LoongArch、x86 的 `inst.c` 都在用同一组 `BITS`/`SEXT`（见 u5-l16、u5-l17），说明这套位操作宏是跨 ISA 共享的工具。

#### 4.3.4 代码实践

**实践目标**：验证 `SEXT` 对负立即数的符号扩展是否正确。

**操作步骤**：写一个最小程序（**示例代码**）：

```c
#include <macro.h>
#include <stdio.h>
int main(void){
  /* 12 位全 1 = 0xFFF，符号位(第11位)为 1，应扩展成 -1 */
  printf("%ld\n", (long)SEXT(0xFFF, 12));
  /* 12 位最大正数 0x7FF，符号位为 0，应保持 0x7FF = 2047 */
  printf("%ld\n", (long)SEXT(0x7FF, 12));
  printf("%d\n", BITS(0b10101100, 5, 3));  /* 取 bit5..3 = 101 = 5 */
  return 0;
}
```

在项目根目录 `gcc -I include t.c && ./a.out` 运行。

**需要观察的现象**：三行输出分别是 `-1`、`2047`、`5`。

**预期结果**：与现象一致。若把 `SEXT` 换成直接 `(uint64_t)0xFFF`，第一行会变成 `4095` 而非 `-1`，对比即可体会符号扩展的必要性。

#### 4.3.5 小练习与答案

**练习 1**：`BITS(x, 31, 0)` 的结果是什么？掩码是多少？

**答案**：取出全部 32 位，等于 `x` 本身（截断到 32 位）。掩码是 \(2^{32}-1\)，即 `0xFFFFFFFF`。

**练习 2**：为什么 `SEXT` 要用「有符号位域」而不是直接 `x | 0xFFFFF000` 这样的写法？

**答案**：硬编码掩码要把 `len` 写死，且要区分正负两种情况手动处理，容易错。位域把 `len` 作为宽度参数、由 C 编译器按符号位自动复制，既通用又不容易出错。

---

### 4.4 MAP：函数式宏（X-macro 模式）

#### 4.4.1 概念说明

`MAP` 是 C 里实现「代码生成」的经典手法，俗称 **X-macro**。它的思想是：把一组数据写成一个「列表宏」，列表里每个元素都写成 `f(元素)` 的形式；然后提供不同的 `f`（变换函数），就能从同一份数据生成不同的代码（比如 enum 常量、初始化语句、字符串表）。

好处是**单一数据源**：新增一个元素，只需在列表里加一行，所有由它派生的代码自动同步，不会漏改。

#### 4.4.2 核心流程

`MAP(c, f)` 的定义极其简单：`c(f)`——即「把 `f` 当参数传给容器 `c`」。因为 `c` 本身写成 `f(a0) f(a1) f(a2) ...`，代入后每个 `f` 都被展开一次，等价于对每个元素应用 `f`。

要点（来自源码注释）：
- 容器 `c` 必须写成 `f(a0) f(a1) ...` 的列表形式。
- 每个元素可以是单个值，也可以是元组（多字段）。

#### 4.4.3 源码精读

[include/macro.h:L79-L84](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L79-L84) 给出 `MAP` 的定义与注释，本体就一句 `#define MAP(c, f) c(f)`。

最漂亮的应用在键盘设备：一份按键列表 `NEMU_KEYS`，被 `MAP` 分别「喂」给两个不同的函数，生成 enum 和映射表：

[src/device/keyboard.c:L25-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L25-L46) 是完整的样板。

- [src/device/keyboard.c:L25-L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L25-L32) 定义容器 `NEMU_KEYS(f)`，把所有按键名（ESCAPE、F1…PAGEDOWN）都写成 `f(名字)`。
- [src/device/keyboard.c:L34-L39](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L34-L39) 第一次 `MAP(NEMU_KEYS, NEMU_KEY_NAME)`，`NEMU_KEY_NAME(k)` 展开成 `NEMU_KEY_k,`，于是在 enum 里生成一连串枚举常量 `NEMU_KEY_ESCAPE, NEMU_KEY_F1, ...`。
- [src/device/keyboard.c:L41-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L41-L46) 第二次 `MAP(NEMU_KEYS, SDL_KEYMAP)`，`SDL_KEYMAP(k)` 展开成 `keymap[SDL_SCANCODE_k] = NEMU_KEY_k;`，于是在 `init_keymap()` 里生成一连串赋值语句，把 SDL 扫描码映射到 NEMU 按键号。

**同一份按键表，派生出 enum（编号）和映射表（SDL→编号）两套代码。** 新增一个按键只需在 `NEMU_KEYS` 里加一个 `f(新键)`，enum 自动有新常量、映射表自动有新条目，两边永远同步。

> `INSTPAT` 译码也用了类似 X-macro 思想：`decode_exec` 体里一长串 `INSTPAT(...)` 宏调用就是「指令表」，框架宏按统一格式展开每一条，详见 u3-l11。

#### 4.4.4 代码实践

**实践目标**：体会 X-macro「一处增加、多处同步」的好处。

**操作步骤**：

1. 阅读 [src/device/keyboard.c:L25-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L25-L46)，确认 `NEMU_KEYS` 这个容器被 `MAP` 用了两次。
2. 思考：如果要在键盘里支持一个新按键 `F13`，需要改几处？

**需要观察的现象**（源码阅读型）：理论上只需在 `NEMU_KEYS(f)` 列表里 `f(F12)` 后面加一个 `f(F13)`，enum 会自动多出 `NEMU_KEY_F13`，`init_keymap` 会自动多出 `keymap[SDL_SCANCODE_F13] = NEMU_KEY_F13;`。

**预期结果**：只改一行，派生代码全部同步——这就是 X-macro 相对「手写两份表」的核心优势。

> 这是源码阅读型实践。若要真正运行验证，可在编译设备后用 `objdump` 或在 `init_keymap` 加打印观察 `F13` 是否被赋值（**待本地验证**，因为需要 SDL 环境且 F13 是否被 SDL 支持取决于版本）。

#### 4.4.5 小练习与答案

**练习 1**：`MAP(c, f)` 为什么能写成 `c(f)` 而不是更复杂的形式？

**答案**：因为容器 `c` 的定义本身就是 `f(a0) f(a1) ...`，把 `f` 作为参数代入 `c` 后，预处理器自然会把每个 `f(ai)` 展开。`MAP` 只是触发这次代入的「开关」，不需要自己写循环（预处理器也没有循环）。

**练习 2**：如果 `NEMU_KEYS` 里两个地方（enum 和映射表）需要的元素格式不同，X-macro 还能复用同一份列表吗？

**答案**：能。让元素带「元组」字段（如 `f(name, scancode)`），两个不同的 `f` 各取自己需要的字段即可。本例因为按键名同时用于构造 `NEMU_KEY_名` 和 `SDL_SCANCODE_名`，单字段就够，所以更简单。

---

### 4.5 CONFIG 条件编译贯穿：从 Kconfig 到源码的一条完整链路

前四个模块讲了「工具怎么用」，本模块把它们串成一条真实链路：**你在 menuconfig 里勾选一个选项，最终是怎么变成源码里某段代码的「编译/不编译」的。**

#### 4.5.1 概念说明

这条链路有四站：

1. **Kconfig**：用 `config XXX` / `bool` / `depends on` 描述「有哪些选项、默认值、依赖关系」。
2. **menuconfig → syncconfig**：交互选择后，由 `tools/kconfig` 的 `conf --syncconfig` 产出 `include/generated/autoconf.h`，里面是一堆 `#define CONFIG_XXX ...`。
3. **源码 include**：`include/common.h` 引入 `autoconf.h`，于是所有 `CONFIG_XXX` 在 C 源码里都成了普通宏。
4. **macro.h 的宏消费**：用 `IFDEF` / `MUXDEF` / `#ifdef` 消费这些宏，决定代码去留；同时 `src/filelist.mk` 用 `SRCS-$(CONFIG_XXX)` 在 Make 侧决定源文件去留。

一个必须弄准的关键事实：**kconfig 对 bool 选项的处理是「开 = 定义为 1，关 = 完全不定义」。** 这直接决定了 `MUXDEF`/`IFDEF` 的语义。

#### 4.5.2 核心流程

以 `CONFIG_MEM_RANDOM`（随机初始化内存）为例，完整链路：

```
src/memory/Kconfig: config MEM_RANDOM (bool, depends on MODE_SYSTEM && !DIFFTEST && !TARGET_AM)
        │  menuconfig 勾选 → CONFIG_MEM_RANDOM=y
        ▼
autoconf.h: #define CONFIG_MEM_RANDOM 1   （选中时）
            （未选中时：这一行根本不存在）
        │  common.h #include <generated/autoconf.h>
        ▼
src/memory/paddr.c: IFDEF(CONFIG_MEM_RANDOM, memset(pmem, rand(), CONFIG_MSIZE));
        │  选中 → 保留 memset 调用；未选中 → 整句消失
        ▼
编译产物：内存是否被随机填充
```

由于未选中的 bool 在 `autoconf.h` 里**不存在**（不是定义为 0），所以 `IFDEF(CONFIG_MEM_RANDOM, ...)` 的行为就是朴素的「开了就编、没开就不编」，符合直觉。

> 注意一个**微妙点**：`MUXDEF` 把「定义为 0」也算作「已定义」（因为 `__P_DEF_0` 存在）。所以在 kconfig 管道里这点不会出问题（kconfig 从不把 bool 定义成 0）；但如果你**自己手写** `#define CONFIG_X 0`，`IFDEF(CONFIG_X, code)` 会保留 `code`——这与 `#ifdef` 的「定义即真」一致，但可能和「值为 0 应该算关」的直觉冲突。遇到「值为 0 算关」的需求要用 `IFONE`/`MUXONE`。

#### 4.5.3 源码精读

**第一站：Kconfig 描述选项。** [src/memory/Kconfig:L27-L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig#L27-L32) 定义 `CONFIG_MEM_RANDOM`：`bool` 类型，默认 `y`，带 `depends on MODE_SYSTEM && !DIFFTEST && !TARGET_AM`（差分测试与 AM 模式下自动消失，因为随机内存会让 REF 比对失败、AM 不需要）。顶层的 [Kconfig:L213-L215](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L213-L215) 的 `CONFIG_RT_CHECK` 是另一个简单 bool 例子。

**第二站：kconfig 生成 autoconf.h。** [tools/kconfig/confdata.c:L644-L683](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/kconfig/confdata.c#L644-L683) 是生成 `autoconf.h` 的「Header printer」。关键在 [tools/kconfig/confdata.c:L649-L663](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/kconfig/confdata.c#L649-L663)：对 `S_BOOLEAN`，值是 `'n'` 时 `break;`（**什么都不写**），是 `'y'` 时才 `fprintf("#define %s%s 1\n", ...)`。这从代码层面证明了「关 = 未定义，开 = 定义为 1」。

**第三站：源码引入 autoconf.h。** [include/common.h:L24-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L24-L25) 引入 `<generated/autoconf.h>` 与 `<macro.h>`，把「配置宏」与「工具宏」都带到所有 include `common.h` 的源文件里。

**第四站（C 侧）：宏消费配置。** [src/memory/paddr.c:L49](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L49) 的 `IFDEF(CONFIG_MEM_RANDOM, memset(pmem, rand(), CONFIG_MSIZE));` 是教科书式的一行：配置开则随机填内存（暴露「读未初始化内存」UB），配置关则这行不存在。[src/memory/paddr.c:L53-L63](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L53-L63) 还用 `IFDEF(CONFIG_DEVICE, return mmio_read(...))` 让 `paddr_read/write` 在未启用设备时直接走 `out_of_bound`，连 `mmio_read` 符号都不引用。

**第四站（Make 侧）：filelist 按配置收源文件。** [src/filelist.mk:L16-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/filelist.mk#L16-L19) 用 `DIRS-$(CONFIG_MODE_SYSTEM) += src/memory` 让内存目录只在 System 模式下编译，用 `DIRS-BLACKLIST-$(CONFIG_TARGET_AM) += src/monitor/sdb` 在 AM 模式下排除 SDB。设备侧 [src/device/filelist.mk:L17](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/filelist.mk#L17) 用 `SRCS-$(CONFIG_DEVICE)` 收设备源文件，[src/device/filelist.mk:L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/filelist.mk#L26) 再用 `SRCS-BLACKLIST-$(CONFIG_TARGET_AM)` 把 `alarm.c` 排除（AM 自带设备模型，详见 u6-l20）。

**两层裁剪的分工**：Make 侧的 `filelist` 决定「哪些 `.c` 文件参与编译」（粗粒度，整文件级），C 侧的 `IFDEF` 决定「文件内部的哪些片段参与编译」（细粒度，语句级）。两者都由同一组 `CONFIG_*` 驱动，互为补充。

#### 4.5.4 代码实践

**实践目标**：照着 `CONFIG_MEM_RANDOM` 的样板，从零新增一个 CONFIG 调试开关，并跑通「Kconfig → autoconf.h → IFDEF」全链路。这一步是本讲综合实践（§5）的热身，先做静态阅读。

**操作步骤（阅读型）**：

1. 读 [src/memory/Kconfig:L27-L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig#L27-L32)，记住一个 bool 选项的最小写法：`config NAME` + `bool "提示语"` + `default y/n` + 可选 `depends on` + 可选 `help`。
2. 读 [src/memory/paddr.c:L44-L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L44-L51)，确认 `init_mem` 里 `IFDEF(CONFIG_MEM_RANDOM, ...)` 的用法。
3. 读 [src/device/device.c:L76-L89](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L76-L89)，对照一连串 `IFDEF(CONFIG_HAS_xxx, init_xxx())`。

**需要观察的现象**：理解「定义一个 bool、在某处 `IFDEF` 引用它」就是最小闭环；选项的 `depends on` 会决定它在哪些配置组合下存在。

**预期结果**：能口头复述 `CONFIG_MEM_RANDOM` 从 Kconfig 到 `paddr.c` 的完整路径，并知道改哪里能新增一个类似开关。具体的「新增开关」实操放在 §5 综合实践。

#### 4.5.5 小练习与答案

**练习 1**：`CONFIG_MEM_RANDOM` 的 `depends on !DIFFTEST` 是为什么？

**答案**：差分测试要求 NEMU（DUT）与参考实现（REF）在每一步状态完全一致。若 NEMU 把内存随机填充而 REF 不这么做，两者初始内存就不同，`difftest_step` 立刻误报不一致。所以在差分测试模式下必须关掉随机内存。

**练习 2**：为什么 `alarm.c` 同时出现在 `SRCS-$(CONFIG_DEVICE)` 和 `SRCS-BLACKLIST-$(CONFIG_TARGET_AM)` 里？

**答案**：第一行表示「启用设备时编译 `alarm.c`」，第二行表示「AM 模式下把它排除」。两条规则取交集：只有在「启用设备 且 非 AM 模式」时 `alarm.c` 才真正参与编译。因为 AM 自身通过 `ioe_init`/`io_read` 提供设备与时钟，Native 的 `SIGVTALRM` 信号方案不适用（详见 u6-l20）。

**练习 3**：如果你想让某个开关「值为 0 时也算关闭」，该用 `IFDEF` 还是 `IFONE`？

**答案**：用 `IFONE`。`IFDEF` 只看「是否定义」，定义为 0 也会保留代码；`IFONE` 只在「定义为 1」时保留，更符合「0 = 关」的直觉。不过 kconfig 管道里关掉的 bool 是未定义而非 0，所以两者在 CONFIG 场景下效果相同，差异只在「自己手写 `#define X 0`」时才显现。

---

## 5. 综合实践

**任务**：参考 `CONFIG_MEM_RANDOM` 的模式，新增一个名为 `CONFIG_DEBUG_BOOT` 的调试开关，作用是「在 `init_mem` 里打印一句启动日志」。要求跑通 Kconfig → autoconf.h → 源码条件编译的完整链路，并在 menuconfig 里切换它验证生效。

**操作步骤**：

1. **在 Kconfig 里定义选项**。在 [src/memory/Kconfig](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig) 的 `MEM_RANDOM` 附近仿写（**示例代码**，注意不要破坏原文件结构，这是教学建议而非必须提交的改动）：

   ```kconfig
   config DEBUG_BOOT
     bool "Print a boot message in init_mem"
     default n
     help
       Print a log line when physical memory is initialized.
   ```

   注意：`src/memory/Kconfig` 只在 `MODE_SYSTEM` 下被 source（见顶层 [Kconfig:L196-L199](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L196-L199)），所以这个开关天然只在 System 模式可见。

2. **在源码里用 IFDEF 消费它**。在 `init_mem()`（[src/memory/paddr.c:L44-L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L44-L51)）里加一行：

   ```c
   IFDEF(CONFIG_DEBUG_BOOT, Log("init_mem: pmem ready, size=0x%x", CONFIG_MSIZE));
   ```

   `Log` 是项目自带的日志宏（见 [include/debug.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/debug.h)），开关关闭时这行连同 `Log` 调用一起在预处理期消失。

3. **menuconfig 选择并编译**。在项目根目录运行 `make menuconfig`，进入 Memory Configuration 找到新选项，分别设为 `n` 和 `y` 各编译一次（`make`）。

4. **验证链路**：
   - `y` 时：检查 `include/generated/autoconf.h` 里应出现 `#define CONFIG_DEBUG_BOOT 1`；运行内置镜像应看到那条 boot 日志。
   - `n` 时：`autoconf.h` 里**没有**这一行；运行时也无该日志。

**需要观察的现象**：开关切换时，`autoconf.h` 的内容变化、运行日志的有无，三者一致。

**预期结果**：你能完整解释「menuconfig 勾选 → autoconf.h 多一行 define → IFDEF 保留 Log → 运行看到日志」这条因果链，证明你已经掌握本讲全部内容。

> 想再进一步：把这个开关从「C 侧 IFDEF」升级成「Make 侧 filelist」——例如新增 `src/utils/debug-boot.c`，在 `src/filelist.mk` 里用 `SRCS-$(CONFIG_DEBUG_BOOT) += src/utils/debug-boot.c` 让它只在开关开启时参与编译，体会「整文件级」与「语句级」两种裁剪的差异。（**待本地验证**完整编译流程。）

## 6. 本讲小结

- NEMU 用 `include/macro.h` 里的一套预处理器宏，支撑了「一份源码、多种 ISA / 模式 / 目标」的工程化能力。
- `concat` / `str` 必须分两层（内层做动作、外层先展开参数），否则 `##` 与 `#` 会阻止宏参数被展开；`ARRLEN` 提供编译期数组长度。
- `MUXDEF` 家族用「占位符 + 逗号 + `CHOOSE2nd`」绕开了「`#ifdef` 不能用在宏展开内部」的限制，能在任意位置做编译期二选一；`MUXDEF` 把「定义为 0/1」都算已定义，`MUXONE` 只认值为 1。
- `IFDEF` / `IFNDEF` 是 `MUXDEF` + `__KEEP` / `__IGNORE` 的封装，用于语句级条件编译；`BITS`/`SEXT` 用 Verilog 风格的位切片与「有符号位域」符号扩展支撑指令译码；`MAP` 用 X-macro 模式让一份列表驱动多处代码生成（如键盘的 enum + 映射表）。
- CONFIG 全链路：`Kconfig` 描述选项 → kconfig 的 `header_print_symbol` 生成 `autoconf.h`（bool 开 = `#define X 1`，关 = 不定义）→ `common.h` 引入 → 源码用 `IFDEF`/`MUXDEF` 在 C 侧裁剪、`filelist.mk` 用 `SRCS-$(CONFIG_*)` 在 Make 侧裁剪，两层互补。

## 7. 下一步学习建议

- 本讲是 U8（测试、追踪与工程化）的收尾。如果想再看「宏 + 条件编译」在更复杂场景的应用，可回顾 u8-l24（差分测试的 `DIFFTEST_REG_SIZE` 与 `ref_difftest_*` 如何靠 `MUXDEF(CONFIG_RV64/CONFIG_RVE,...)` 适配寄存器布局）与 u8-l25（`ITRACE_COND` 如何作为字符串注入条件）。
- 想深入「类型拼接」的下游，回到 u5-l14（ISA 抽象与 `CPU_state`）、u5-l17（x86 变长译码里 `concat(TYPE_, type)` 的用法）。
- 若准备做 PA 的二次开发，建议动手完成 §5 综合实践，并尝试用 `MAP` 给某个表（如 SDB 命令表、键盘表）扩展一项，体会「单一数据源」的维护便利。
