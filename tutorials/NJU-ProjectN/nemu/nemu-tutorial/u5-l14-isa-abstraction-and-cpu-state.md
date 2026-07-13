# ISA 抽象与 CPU 状态定义

## 1. 本讲目标

本讲是「ISA 实现与指令」单元（U5）的第一篇，回答一个贯穿全框架的问题：**NEMU 用同一套框架源码模拟 x86 / mips32 / riscv / loongarch32r 四种 ISA，框架代码到底是怎么做到「不写死任何一种 ISA」的？**

学完本讲你应当能够：

- 把 `include/isa.h` 里声明的一组函数按子系统分类，并说出每一类对应框架里的哪个调用方。
- 说清 `concat(__GUEST_ISA__, _CPU_state)` 是如何与 `isa-def.h` 里的 `typedef` 名字精确对上、从而把框架与 ISA「缝合」起来的。
- 画出 riscv32 的 `CPU_state` 字段布局（`gpr` + `pc`），并解释 `word_t` 宽度由谁决定。
- 解释 `ISADecodeInfo` 的作用，并对比 riscv 与 x86 在这里的差异（定长 vs 变长指令的伏笔）。
- 说明 `CONFIG_RV64` / `CONFIG_RVE` 两个开关如何在不改一行源码的前提下改变 `CPU_state` 的大小，以及对差分测试寄存器布局的影响。

本讲只讲「抽象层与状态定义」，**不**讲具体指令的译码与执行——那是 u5-l16、u5-l17 的内容。

## 2. 前置知识

本讲承接 u1-l4（目录结构与 ISA 抽象层）与 u3-l10（取指与译码数据结构），需要你已了解：

- **`__GUEST_ISA__` 宏驱动类型拼接**：Makefile 把 `CONFIG_ISA` 的值（如 `riscv32`）注入为 `-D__GUEST_ISA__=riscv32`，框架头文件再用 `concat` 把它和后缀拼成 ISA 专属类型名。u1-l4 已讲过 `concat` 为什么要分两层（`concat_temp` 用 `##` 阻止参数展开，外层 `concat` 先展开参数再粘贴），本讲不再重复原理，只看它如何落地到具体类型。
- **`MUXDEF` 宏**：`MUXDEF(macro, X, Y)` 在 `macro` 已定义时取 `X`、未定义时取 `Y`，是 NEMU 的「编译期三目运算」。它由 `include/macro.h` 提供。
- **`Decode` 结构体**：u3-l10 讲过 `Decode` 是单条指令的「工作台」，含 `pc / snpc / dnpc` 三个 ISA 无关字段，以及一个 ISA 相关的 `isa` 字段——这个 `isa` 字段的类型就是本讲要讲的 `ISADecodeInfo`。
- **`word_t` / `vaddr_t` / `paddr_t`**：u1-l4 讲过这几个基本类型的宽度由 `MUXDEF(CONFIG_ISA64, ...)` 自适应，是系统的「宽度基因」。

下面用到的「永久链接」均指向当前 HEAD `8e7a0fe`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/isa.h` | ISA 抽象契约：声明框架与 ISA 之间的统一接口，并用 `concat` 拼出 `CPU_state` / `ISADecodeInfo` 类型名。框架代码只 include 它。 |
| `src/isa/riscv32/include/isa-def.h` | riscv 的 ISA 专属定义：`CPU_state`（寄存器堆+pc）、`ISADecodeInfo`（取出的指令字）、`isa_mmu_check` 宏。`riscv64` 是它的符号链接。 |
| `src/isa/x86/include/isa-def.h` | x86 的 ISA 专属定义，用于对比 `CPU_state` 与 `ISADecodeInfo` 的另一种写法。 |
| `include/common.h` | 定义 `word_t` / `sword_t` / `vaddr_t` / `paddr_t`，宽度由 `CONFIG_ISA64` 决定。 |
| `include/macro.h` | 提供 `concat` / `MUXDEF` / `IFDEF` 等宏基础设施。 |
| `include/difftest-def.h` | 定义 `DIFFTEST_REG_SIZE`，体现 RVE/RV64 对差分测试寄存器布局的影响。 |
| `include/cpu/decode.h` | `Decode` 结构体，其 `isa` 字段类型为 `ISADecodeInfo`（承接 u3-l10）。 |

## 4. 核心概念与源码讲解

### 4.1 isa.h 接口分类

#### 4.1.1 概念说明

`include/isa.h` 是框架与 ISA 之间的**抽象契约**。框架代码（`cpu-exec.c`、`vaddr.c`、`sdb.c`、`difftest` 等）只 `#include <isa.h>`，从不直接引用任何一种 ISA 的具体类型或函数名；每个 ISA 在 `src/isa/$(GUEST_ISA)/` 下提供与契约签名一致的具体实现。换 ISA 本质上是换一套实现文件，框架零改动。

这份契约做了两件事：

1. **拼出两个 ISA 专属类型**：`CPU_state`（CPU 的全部体系结构状态）和 `ISADecodeInfo`（译码时 ISA 需要的私有工作区）。
2. **声明一组统一签名的函数**，按子系统分成 6 组，每组对应 NEMU 的一个子系统。

#### 4.1.2 核心流程

`isa.h` 的声明按子系统分组如下：

| 组 | 声明 | 框架里的调用方 |
| --- | --- | --- |
| monitor | `init_isa()`、`isa_logo[]` | `init_monitor` 启动链 |
| reg | `cpu`（全局变量）、`isa_reg_display()`、`isa_reg_str2val()` | SDB 的 `info r`、表达式求值的寄存器解析 |
| exec | `isa_exec_once(struct Decode *)` | `exec_once` 单步执行 |
| memory | MMU 三组枚举、`isa_mmu_check`、`isa_mmu_translate` | `vaddr_read/write/ifetch` |
| interrupt | `isa_raise_intr`、`isa_query_intr`、`INTR_EMPTY` | `cpu_exec` 主循环每步查中断 |
| difftest | `isa_difftest_checkregs`、`isa_difftest_attach` | `difftest_step` |

注意 `isa.h` 只声明签名，**不提供实现**。每个 ISA 必须在自己的目录下实现这些符号，否则链接报错——这正是「契约」的含义。

#### 4.1.3 源码精读

先看头部的类型拼接与 isa-def.h 的引入：

[include/isa.h:L19-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L19-L25) —— 第 20 行 `#include <isa-def.h>` 把当前 ISA 的专属定义拉进来；第 24-25 行用 `concat` 把 `__GUEST_ISA__` 和 `_CPU_state` / `_ISADecodeInfo` 拼成最终类型名。

再看按子系统分组的函数声明（节选 reg / exec / memory / interrupt / difftest）：

[include/isa.h:L31-L56](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L31-L56) —— 这里能看到 `isa_reg_display`、`isa_exec_once`、`isa_mmu_check`、`isa_raise_intr`、`isa_difftest_checkregs` 等全部统一接口。注意第 44-46 行的 `#ifndef isa_mmu_check` 包裹：它允许 ISA 用宏把 `isa_mmu_check` 直接定义为常量（如 `MMU_DIRECT`），从而在编译期消除翻译分支——riscv 与 x86 都用了这个技巧（见 4.5）。

#### 4.1.4 代码实践

**实践目标**：建立「契约—实现」的对应关系。

**操作步骤**：

1. 打开 `include/isa.h`，把第 27-56 行的每个声明按上表归类。
2. 对 `isa_exec_once`、`isa_reg_display`、`isa_raise_intr` 三个函数，用 `Grep` 在 `src/isa/riscv32/` 下找到它们的具体实现文件。
3. 记录每个函数的「声明位置（isa.h 行号）」与「实现位置（文件:行号）」。

**需要观察的现象**：每个 isa.h 声明的函数，在 `src/isa/riscv32/` 下都能找到签名完全一致的定义；而 `src/isa/x86/` 下也能找到同名定义，只是实现不同。

**预期结果**：例如 `isa_exec_once` 声明在 isa.h 第 38 行，实现应在 `src/isa/riscv32/inst.c`；`isa_reg_display` 实现应在 `src/isa/riscv32/reg.c`。这验证了「一份契约，多套实现」。

#### 4.1.5 小练习与答案

**练习 1**：`isa.h` 第 32 行声明了 `extern CPU_state cpu;`，但 `CPU_state` 的具体定义不在 isa.h 里。它最终是在哪个文件里被定义成结构体的？

**答案**：在 `src/isa/riscv32/include/isa-def.h` 里。isa.h 只用 `concat` 把 `CPU_state` 定义成 `riscv32_CPU_state` 的别名，真正的 `struct {...} riscv32_CPU_state` 在 isa-def.h 中。

**练习 2**：为什么 `isa_mmu_check` 要用 `#ifndef ... #endif` 包起来，而 `isa_mmu_translate` 不用？

**答案**：因为 ISA 可能选择用宏把 `isa_mmu_check` 直接定义为常量返回值（如 riscv/x86 的 `(MMU_DIRECT)`），此时它不是一个函数，不能再声明函数原型，否则会冲突；`#ifndef` 让宏定义优先。而 `isa_mmu_translate` 始终是真正的函数（即便现在是 stub），所以直接声明即可。

### 4.2 concat 类型拼接

#### 4.2.1 概念说明

u1-l4 已讲过 `concat` 的两层宏原理。本讲聚焦它的**工程落点**：isa.h 里写 `concat(__GUEST_ISA__, _CPU_state)`，永远不会出现 `riscv32` 这个字面量；而 isa-def.h 里 `typedef struct {...} riscv32_CPU_state;` 又永远不会出现 `__GUEST_ISA__`。两边各写一半，由预处理器的 `##` 把它们缝成同一个 token `riscv32_CPU_state`。这条缝就是「框架（ISA 无关）」与「实现（ISA 专属）」的接缝。

#### 4.2.2 核心流程

让接缝对齐，需要**两条独立机制产出同一个 token**：

1. **Makefile 侧**：从 `CONFIG_ISA` 取出字符串 `riscv32`，注入 `-D__GUEST_ISA__=riscv32`。于是 isa.h 里的 `concat(__GUEST_ISA__, _CPU_state)` 展开成 `riscv32_CPU_state`。
2. **isa-def.h 侧**：`typedef struct {...} riscv32_CPU_state;` 直接给出这个 typedef 名字。

两边汇聚到 `riscv32_CPU_state`，框架代码就能用统一的 `CPU_state` 名字访问它。换 ISA 时，Makefile 改注入的值、isa-def.h 换一份文件，接缝自动重新对齐。

#### 4.2.3 源码精读

Makefile 如何取出 ISA 并注入宏：

[Makefile:L28-L30](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L28-L30) —— `GUEST_ISA` 从 `CONFIG_ISA` 去引号得到（如 `riscv32`），同时它还参与拼出二进制名 `NAME = $(GUEST_ISA)-nemu-$(ENGINE)`。

[Makefile:L52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L52) —— 把 `__GUEST_ISA__` 注入 CFLAGS。注意值没有引号，所以它是一个**标识符 token**（`riscv32`），可直接参与 `##` 粘贴。

`concat` 的定义（u1-l4 已详释，这里只确认位置）：

[include/macro.h:L31-L34](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L31-L34) —— `concat_temp` 用 `##` 粘贴，外层 `concat` 保证参数先展开再粘贴。

接缝的两半：

[include/isa.h:L24-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L24-L25) —— 框架侧，写 `__GUEST_ISA__`。

[src/isa/riscv32/include/isa-def.h:L21-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24) —— 实现侧，给出 `riscv32_CPU_state`（注意第 24 行 typedef 名字本身也是 `MUXDEF` 选出来的，见 4.5）。

#### 4.2.4 代码实践

**实践目标**：手工展开宏，亲眼看到「两半缝合」。

**操作步骤**：

1. 假设当前配置为默认的 riscv32，写出 `__GUEST_ISA__` 的值。
2. 把 `concat(__GUEST_ISA__, _CPU_state)` 逐步展开：先展开参数得 `concat(riscv32, _CPU_state)`，再粘贴得 `riscv32_CPU_state`。
3. 在 isa-def.h 中确认存在 `typedef struct {...} riscv32_CPU_state;`。
4. （可选）用 `make menuconfig` 切到 `x86`，重新展开，确认得到 `x86_CPU_state` 并在 `src/isa/x86/include/isa-def.h` 找到对应 typedef。

**需要观察的现象**：无论选哪种 ISA，isa.h 第 24 行展开后都得到「`<isa>_CPU_state`」，而对应 isa-def.h 里恰好定义了这个名字。

**预期结果**：riscv32 → `riscv32_CPU_state`；x86 → `x86_CPU_state`。接缝严丝合缝。

#### 4.2.5 小练习与答案

**练习 1**：如果把 Makefile 第 52 行的 `-D__GUEST_ISA__=$(GUEST_ISA)` 改成 `-D__GUEST_ISA__=\"$(GUEST_ISA)\"`（加引号），会发生什么？

**答案**：`__GUEST_ISA__` 会变成字符串字面量 `"riscv32"` 而非标识符 `riscv32`，`concat` 粘贴会得到 `"riscv32"_CPU_state` 这种非法 token，编译报错。这就是为什么注入时不加引号。

**练习 2**：新增一种 ISA（比如 `riscv128`）需要修改 isa.h 吗？

**答案**：不需要。isa.h 里没有任何 ISA 字面量，只要新建 `src/isa/riscv128/include/isa-def.h` 并定义 `riscv128_CPU_state` / `riscv128_ISADecodeInfo`，再在 Kconfig 加一个选项即可。这是「抽象层」带来的可扩展性。

### 4.3 riscv32 CPU_state

#### 4.3.1 概念说明

`CPU_state` 是 CPU 的**全部体系结构状态**——对 riscv 而言就是 32 个通用寄存器（GPR，x0~x31）加上一个 `pc`。它是整个模拟器最核心的数据结构：

- `exec_once` 每步读写它（取 pc、改寄存器、写回新 pc）。
- SDB 的 `info r` 显示它（`isa_reg_display`）。
- 差分测试每步比对它（`isa_difftest_checkregs`）。

它是一个全局变量 `cpu`，定义在 `src/cpu/cpu-exec.c`。

#### 4.3.2 核心流程

riscv 的 `CPU_state` 极简：

```
struct CPU_state {
  word_t gpr[32];   // x0..x31，word_t 宽度由 CONFIG_ISA64 决定
  vaddr_t pc;       // 程序计数器
};
```

几个关键点：

- **`gpr` 是数组而非 32 个独立字段**：因为 riscv 寄存器在指令编码里用 5 位索引（0~31），数组下标 `gpr[i]` 直接对应编码，译码极简。对比 x86 用 `union` + 命名字段（见 4.4 综合实践）。
- **`x0` 恒为 0 是软件约定**：硬件上 `gpr[0]` 仍是普通内存单元，riscv 实现里在每条指令执行后用 `R(0) = 0` 强制复位（u5-l16 会讲）。
- **`word_t` 是宽度基因**：riscv32 下 `word_t = uint32_t`，riscv64 下 `word_t = uint64_t`，所以同一个结构体定义自动适配两种位宽。

`CPU_state` 的大小（默认 riscv32 配置）：

\[
\text{sizeof}(CPU\_state) = 32 \times 4 + 4 = 132 \text{ 字节}
\]

#### 4.3.3 源码精读

riscv 的 `CPU_state` 定义：

[src/isa/riscv32/include/isa-def.h:L21-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24) —— `gpr` 数组长度由 `MUXDEF(CONFIG_RVE, 16, 32)` 决定（4.5 详述），`word_t` 是寄存器宽度，typedef 名字由 `MUXDEF(CONFIG_RV64, ...)` 决定。

`word_t` 宽度从哪来：

[include/common.h:L38-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L38-L42) —— `word_t = MUXDEF(CONFIG_ISA64, uint64_t, uint32_t)`，而 `vaddr_t` 就是 `word_t` 的别名，所以 `pc` 的宽度也随之变化。`CONFIG_ISA64` 在 riscv 下由 `RV64` 自动派生（见 4.5）。

全局实例 `cpu`：

[src/cpu/cpu-exec.c:L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L28) —— `CPU_state cpu = {};`，零初始化。整个模拟器通过这个全局变量访问寄存器堆与 pc。

#### 4.3.4 代码实践

**实践目标**：验证 `CPU_state` 的大小与字段布局。

**操作步骤**：

1. 在 `src/isa/riscv32/init.c` 的 `restart()` 函数里（或任意一处启动期代码）临时加一行：
   ```c
   printf("sizeof(CPU_state) = %zu, gpr=%zu pc=%zu\n",
          sizeof(CPU_state), sizeof(cpu.gpr), sizeof(cpu.pc));
   ```
   （示例代码，验证完请删除。）
2. 重新 `make` 并运行，观察输出。
3. 用输出验证上面 132 字节的计算。

**需要观察的现象**：打印出 `sizeof(CPU_state) = 132`，且 `gpr=128 pc=4`。

**预期结果**：与手算一致。若开启了 `CONFIG_RVE`（见 4.5），`gpr` 会变成 64 字节、总数 68 字节。

> 说明：本实践需修改源码临时加打印，验证后请还原，不要把调试打印提交。

#### 4.3.5 小练习与答案

**练习 1**：riscv 的 `CPU_state` 为什么把 `gpr` 设计成数组，而 x86 却用 `union` + 命名字段（见 4.4）？

**答案**：riscv 是定长指令、寄存器在编码里用统一的 5 位索引寻址，数组下标直接对应编码，最自然；x86 寄存器编码与位宽（32/16/8 位）耦合（如 `al/ah/ax/eax` 共用同一物理寄存器的不同部分），需要 `union` 让不同宽度的访问叠加在同一存储上。

**练习 2**：`cpu` 是全局变量且零初始化，那 `x0` 恒为 0 是靠初始化保证的吗？

**答案**：不是。零初始化只是上电时恰好为 0；运行中若有指令写 `gpr[0]`，存储单元会被改写。riscv 实现靠在每条指令执行后执行 `R(0) = 0` 来强制维持 `x0` 恒 0 的体系结构约定（u5-l16 详述）。

### 4.4 ISADecodeInfo

#### 4.4.1 概念说明

`ISADecodeInfo` 是 `Decode` 结构体里**留给 ISA 的私有工作区**。u3-l10 讲过 `Decode` 有 `pc / snpc / dnpc` 三个 ISA 无关字段，外加一个 `isa` 字段——这个 `isa` 字段的类型就是 `ISADecodeInfo`。框架不知道也不关心它里面装什么，由各 ISA 自行定义。

为什么需要它？因为译码过程中 ISA 要暂存一些中间信息（比如刚取出的指令字节），而这些信息对框架是不可见的。把这部分塞进 `Decode.isa`，既复用了 `Decode` 这个栈上工作台，又保持了框架的 ISA 无关性。

#### 4.4.2 核心流程

- riscv：定长指令，一条指令恒为 32 位，所以 `ISADecodeInfo` 只需一个 `uint32_t inst` 存取出的指令字。
- x86：变长指令（1~15 字节），需要边取边解析，所以 `ISADecodeInfo` 是一个 16 字节缓冲区加一个游标指针。

这种差异是定长 ISA 与变长 ISA 译码复杂度差异的根源，u5-l16（riscv）和 u5-l17（x86）会展开讲，本讲先记住「同一个字段，两种截然不同的内容」。

#### 4.4.3 源码精读

`Decode` 里 `isa` 字段的位置（承接 u3-l10）：

[include/cpu/decode.h:L21-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L21-L27) —— 第 25 行 `ISADecodeInfo isa;` 是框架与 ISA 在「单条指令工作台」上的接缝。`ISADecodeInfo` 本身又由 isa.h 的 `concat` 拼成 ISA 专属类型。

riscv 的 `ISADecodeInfo`：

[src/isa/riscv32/include/isa-def.h:L27-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L27-L29) —— 只有一个 `uint32_t inst`。译码时 `s->isa.inst` 就是刚 `inst_fetch` 取出的 32 位指令字，后续 INSTPAT 模式匹配（u3-l11）直接对它做位运算。

x86 的 `ISADecodeInfo`：

[src/isa/x86/include/isa-def.h:L43-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/include/isa-def.h#L43-L46) —— `uint8_t inst[16]` 是指令字节缓冲区（16 ≥ x86 最大指令长度 15），`uint8_t *p_inst` 是游标指针，指向「下一次要取的字节」，支持变长指令的增量取指。

#### 4.4.4 代码实践

**实践目标**：理解同一字段在两种 ISA 下的差异。

**操作步骤**：

1. 阅读 riscv 的 `isa_exec_once`（`src/isa/riscv32/inst.c`），找到它如何把取出的指令存入 `s->isa.inst`。
2. 切换到 x86（`make menuconfig` 选 x86），阅读 x86 的 `isa_exec_once`，找到它如何用 `s->isa.p_inst` 增量取字节。
3. 对比两者取指后的「指针推进方式」：riscv 一次推进固定 4 字节，x86 按需推进若干字节。

**需要观察的现象**：riscv 取指一步到位（一条 `inst_fetch(&s->snpc, 4)`），x86 取指是「边解析边取」的循环。

**预期结果**：能口述「`inst` 是定长指令的完整快照，`inst[16]+p_inst` 是变长指令的流水线式缓冲区」。

**待本地验证**：x86 `isa_exec_once` 的具体取指循环结构，建议本地阅读 `src/isa/x86/inst.c` 确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么 x86 的 `inst` 缓冲区取 16 字节而不是更大？

**答案**：x86 指令最大长度为 15 字节（体系结构规定），16 字节刚好能装下任意一条完整指令并留 1 字节余量，既够用又省空间。

**练习 2**：`ISADecodeInfo` 里存的是「指令本身」还是「指令的译码结果」？

**答案**：主要是「取出的指令原始字节/字」，是译码的**输入**而非结果。译码结果（操作数、立即数等）通常存在 `Decode` 的其它字段或 INSTPAT 的局部变量里。`ISADecodeInfo` 是 ISA 在译码过程中需要的私有暂存区。

### 4.5 RV64/RVE 条件

#### 4.5.1 概念说明

riscv 的 `CPU_state` 有两个编译期开关，能在不改源码的前提下改变结构体形态：

- **`CONFIG_RV64`**：是否为 64 位 RISC-V。它同时改变寄存器宽度（`word_t` 32→64）和结构体 typedef 名字（`riscv32_CPU_state`→`riscv64_CPU_state`）。
- **`CONFIG_RVE`**：是否使用 E 扩展（嵌入式基础整数指令集）。E 扩展只有 16 个通用寄存器（x0~x15），所以 `gpr` 数组从 32 缩为 16。

这两个开关都通过 `MUXDEF` 作用于同一份 `isa-def.h`，再加上 `riscv64` 是 `riscv32` 的符号链接（同一份文件），实现了「一份源码，多种形态」。

#### 4.5.2 核心流程

配置如何层层传导到 `CPU_state`：

```
make menuconfig
   ├─ RV64=y/n   (src/isa/riscv32/Kconfig)
   │     └─ ISA64 自动派生为 y（仅当 RV64=y）   (Kconfig)
   │           └─ word_t = MUXDEF(CONFIG_ISA64, uint64_t, uint32_t)   (common.h)
   │           └─ typedef 名 = MUXDEF(CONFIG_RV64, riscv64_CPU_state, riscv32_CPU_state) (isa-def.h)
   └─ RVE=y/n   (src/isa/riscv32/Kconfig)
         └─ gpr 长度 = MUXDEF(CONFIG_RVE, 16, 32)   (isa-def.h)
```

`CPU_state` 大小随配置变化：

| 配置 | `word_t` | `gpr` 长度 | `sizeof(CPU_state)` |
| --- | --- | --- | --- |
| riscv32（默认） | uint32_t | 32 | \(32\times4+4=132\) |
| riscv32 + RVE | uint32_t | 16 | \(16\times4+4=68\) |
| riscv64（RV64） | uint64_t | 32 | \(32\times8+8=264\) |
| riscv64 + RVE | uint64_t | 16 | \(16\times8+8=136\) |

#### 4.5.3 源码精读

riscv 专属的两个开关：

[src/isa/riscv32/Kconfig:L1-L10](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/Kconfig#L1-L10) —— `RV64` 与 `RVE` 两个 bool 选项，默认均为 `n`。

`ISA64` 如何由 `RV64` 派生：

[Kconfig:L16-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L16-L28) —— 第 20-21 行 `CONFIG_ISA` 在 `RV64` 开启时取 `riscv64`，否则 `riscv32`；第 25-28 行 `ISA64` 仅在 `ISA_riscv && RV64` 时自动置 `y`。注意 `riscv64` 是 `riscv32` 的符号链接，故两者共用同一份 `isa-def.h`，差别仅在 `CONFIG_RV64` / `CONFIG_ISA64` 这两个宏。

`CPU_state` 里两个 `MUXDEF` 同时作用：

[src/isa/riscv32/include/isa-def.h:L21-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24) —— 第 22 行 `MUXDEF(CONFIG_RVE, 16, 32)` 控制 `gpr` 数组长度；第 24 行 `MUXDEF(CONFIG_RV64, riscv64_CPU_state, riscv32_CPU_state)` 控制 typedef 名字，与 isa.h 的 `concat(__GUEST_ISA__, _CPU_state)` 对齐（RV64 时 `__GUEST_ISA__=riscv64`，正好拼出 `riscv64_CPU_state`）。

RVE/RV64 对差分测试寄存器布局的影响：

[include/difftest-def.h:L30-L33](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/difftest-def.h#L30-L33) —— `RISCV_GPR_TYPE` 跟随 `CONFIG_RV64` 选 `uint64_t`/`uint32_t`，`RISCV_GPR_NUM` 跟随 `CONFIG_RVE` 选 `16`/`32`，`DIFFTEST_REG_SIZE = sizeof(RISCV_GPR_TYPE) * (RISCV_GPR_NUM + 1)`（GPRs + pc）。这与 `CPU_state` 的大小完全一致——差分测试传输的寄存器块就是 `CPU_state` 的「GPRs + pc」部分。

#### 4.5.4 代码实践

**实践目标**：观察 RVE 开关如何改变 `gpr` 大小与差分测试寄存器布局。

**操作步骤**：

1. 默认配置（RVE=n）下，在 `src/isa/riscv32/init.c` 的 `restart()` 里临时加打印 `sizeof(cpu.gpr)` 和 `DIFFTEST_REG_SIZE`（需 `#include <difftest-def.h>`），记录数值。
2. `make menuconfig`，进入「ISA-dependent Options for riscv」开启 `Use E extension`（RVE=y），重新编译运行，再次记录。
3. 对比两次数值。

**需要观察的现象**：RVE=n 时 `sizeof(cpu.gpr)=128`、`DIFFTEST_REG_SIZE=132`；RVE=y 时分别变为 `64`、`68`。

**预期结果**：`gpr` 从 32 项缩为 16 项，`DIFFTEST_REG_SIZE` 同步从 132 降为 68 字节，证明差分测试的寄存器传输大小与 `CPU_state` 的 GPR 数量直接挂钩。

> 说明：本实践需临时加打印，验证后请还原。

#### 4.5.5 小练习与答案

**练习 1**：开启 `RV64` 后，`__GUEST_ISA__` 的值是什么？isa.h 的 `concat(__GUEST_ISA__, _CPU_state)` 展开成什么？它和 isa-def.h 里的 typedef 名字一致吗？

**答案**：`__GUEST_ISA__=riscv64`（因 `CONFIG_ISA` 取 `riscv64`），展开成 `riscv64_CPU_state`；isa-def.h 第 24 行 `MUXDEF(CONFIG_RV64, riscv64_CPU_state, riscv32_CPU_state)` 在 RV64=y 时取 `riscv64_CPU_state`，两者一致。这验证了「Makefile 注入的 ISA 字符串」与「isa-def.h 的 MUXDEF 选择」是两条独立却始终对齐的机制。

**练习 2**：为什么 `riscv64` 用符号链接指向 `riscv32` 而不是复制一份目录？

**答案**：因为两者共用同一份 `isa-def.h` 和实现源码，差别全在 `CONFIG_RV64`/`CONFIG_ISA64` 这两个宏上，由 `MUXDEF` 在编译期区分。符号链接避免代码重复，改 riscv 实现时 32/64 同步生效。

**练习 3**：`DIFFTEST_REG_SIZE` 对 riscv 的计算是 `sizeof(RISCV_GPR_TYPE) * (RISCV_GPR_NUM + 1)`，那个 `+1` 是什么？

**答案**：是 `pc`。差分测试每步比对的寄存器块 = 全部 GPR + pc，与 `CPU_state` 的字段一致。

## 5. 综合实践

把本讲内容串起来，完成下面这个综合任务（即本讲指定的实践任务）。

**任务**：对比 riscv32 与 x86 的 `isa-def.h`，列出各自 `CPU_state` 字段差异；并解释 `CONFIG_RVE` 如何使 `gpr` 从 32 缩为 16，以及对差分测试寄存器大小的影响。

**操作步骤**：

1. **字段对比**。填出下表（先自己填，再对照源码核对）：

   | 维度 | riscv32 `CPU_state` | x86 `CPU_state` |
   | --- | --- | --- |
   | GPR 组织方式 | `word_t gpr[32]` 数组 | `struct{ _32; _16; _8[2]; } gpr[8]` + 命名 `eax..edi` |
   | GPR 数量 | 32（RVE 时 16） | 8 |
   | 寄存器宽度 | `word_t`（32/64 自适应） | 固定 `uint32_t` 为主，含 16/8 位子寄存器 |
   | `pc` 字段 | `vaddr_t pc` | `vaddr_t pc` |
   | 是否用 union | 否（数组即可） | 当前未用，但有 TODO 要求改用 union |
   | `ISADecodeInfo` | `uint32_t inst` | `uint8_t inst[16] + uint8_t *p_inst` |

   核对依据：[src/isa/riscv32/include/isa-def.h:L21-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L29) 与 [src/isa/x86/include/isa-def.h:L29-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/include/isa-def.h#L29-L46)。

2. **解释 RVE 的影响**。回答以下三点（用源码行号佐证）：
   - `gpr` 数组长度由哪个宏决定？（[isa-def.h:L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L22) 的 `MUXDEF(CONFIG_RVE, 16, 32)`）
   - `RVE=y` 时 `gpr` 从 32 项变 16 项，`sizeof(CPU_state)` 从 132 降到 68 字节。
   - 对差分测试的影响：[difftest-def.h:L32-L33](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/difftest-def.h#L32-L33) 中 `RISCV_GPR_NUM` 同样跟随 `CONFIG_RVE` 取 16/32，`DIFFTEST_REG_SIZE` 因此从 132 降为 68 字节——DUT 与 REF 必须用相同的 RVE 配置，否则寄存器块大小不匹配，差分测试会在第一步比对时越界或错位。

3. **验证**。开启 `RVE` 重新编译，若已开启 `DIFFTEST`，观察 REF 侧（如 spike）是否也需相应配置为 E 扩展，否则比对失败。

**预期结果**：能口述「riscv 用数组 + 自适应宽度，x86 用 union 思路 + 命名字段适配多宽度寄存器；RVE 通过 `MUXDEF` 在编译期把 `gpr` 从 32 缩为 16，并连带缩小 `DIFFTEST_REG_SIZE`」。

## 6. 本讲小结

- `include/isa.h` 是框架与 ISA 的抽象契约：用 `concat` 拼出 `CPU_state`/`ISADecodeInfo` 两个类型，并按 monitor/reg/exec/memory/interrupt/difftest 六组声明统一接口，框架只依赖这份契约。
- `concat(__GUEST_ISA__, _CPU_state)` 是「接缝」：框架侧写 `__GUEST_ISA__`，实现侧写 `riscv32_CPU_state`，由预处理器缝合成同一个 token；Makefile 用 `-D__GUEST_ISA__=$(GUEST_ISA)` 注入值，不带引号以保持标识符属性。
- riscv 的 `CPU_state` = `word_t gpr[32]` + `vaddr_t pc`；`word_t` 宽度由 `MUXDEF(CONFIG_ISA64, uint64_t, uint32_t)` 决定，是系统的「宽度基因」；全局实例 `cpu` 定义在 `cpu-exec.c`。
- `ISADecodeInfo` 是 `Decode.isa` 字段的类型，是 ISA 在译码工作台上的私有暂存区：riscv 仅 `uint32_t inst`（定长），x86 是 `uint8_t inst[16] + p_inst`（变长）。
- `CONFIG_RV64` 同时改寄存器宽度与 typedef 名字（且与 Makefile 注入的 `__GUEST_ISA__` 对齐），`CONFIG_RVE` 把 `gpr` 从 32 缩为 16；二者经 `MUXDEF` 作用于同一份 isa-def.h（riscv64 是 riscv32 的符号链接）。
- `DIFFTEST_REG_SIZE` 的计算与 `CPU_state` 大小一致（GPRs + pc），故 RVE/RV64 会直接改变差分测试的寄存器传输大小，DUT 与 REF 必须配置一致。

## 7. 下一步学习建议

本讲定义了 `CPU_state` 与 `ISADecodeInfo` 的「形状」，但还没看它们如何被使用。建议：

- **u5-l15 寄存器实现**：看 `reg.c` 如何基于 `CPU_state` 实现 `isa_reg_display` / `isa_reg_str2val`，以及 `restart()` 如何初始化 `pc` 与 `$0`。
- **u5-l16 RISC-V 指令实现**：看 `inst.c` 如何把取出的 `s->isa.inst` 经 INSTPAT 匹配后读写 `cpu.gpr`、改写 `cpu.pc`，并理解 `R(0)=0` 的复位。
- **u5-l17 x86 变长指令实现对比**：看 x86 如何用 `ISADecodeInfo` 的 `inst[16]+p_inst` 做增量取指与 ModR/M 解码，与本讲 4.4 的伏笔呼应。
- 复习 **u3-l11 INSTPAT 模式匹配**：`INSTPAT_INST(s)` 宏读取的就是本讲的 `s->isa.inst`，理解这一接缝能帮你把「数据结构」与「译码机制」连成一条线。
