# C 软件驱动：寄存器访问封装

## 1. 本讲目标

在 [u2-l1 寄存器地图](u2-l1-register-map.md) 里，我们已经把 IP 的软硬契约——32 个寄存器的地址、字段和枚举——梳理清楚了。但这些寄存器对软件来说只是一串裸地址（`0x00`、`0x0C`、`0x24` …）。本讲要解决的问题就是：

> **CPU（裸机程序）到底怎么读写这些寄存器，才能跑完一次完整的内存测试？**

`drivers/mem_test/` 这个 C 驱动就是答案。它把裸地址包装成一组人能读懂的函数（`MemTest_Start`、`MemTest_SetMode` …），让上层应用不必关心寄存器偏移和位拼接细节。

学完本讲你应该能够：

- 把 C 头文件里的 `*_OFFS` 宏与 VHDL `mem_test_pkg.vhd` 里的 `REG_*` 常量一一对应起来，并解释它们为何相等。
- 说清楚 `Xil_Out32` / `Xil_In32` 在 Xilinx 裸机（bare-metal）编程模型中扮演的角色。
- 独立调用驱动 API，写出「配置 → 启动 → 轮询 → 读结果」一次完整内存测试的代码。
- 理解 Vivado 在打包 IP 时，`data/` 目录下的 `.tcl` / `.mdd` 文件如何把驱动与硬件实例（`C_BASEADDR`）绑定在一起。

## 2. 前置知识

本讲假设你已经读过 [u2-l1](u2-l1-register-map.md)，知道寄存器地图长什么样。下面这些名词会反复出现，先做通俗解释：

- **寄存器（register）**：FPGA 里一个 32 位的存储单元，CPU 可以像读写内存一样读写它。区别于普通 RAM 的地方在于：某些寄存器写一下是为了「触发一个动作」（叫 strobe / 触发型），某些寄存器读一下是为了「查状态」（叫只读型）。
- **AXI-Lite**：ARM 提出的一种轻量总线协议，专门用来让 CPU 访问外设里的少量寄存器。本 IP 的控制面 `S00_AXI` 就是一组 AXI-Lite 接口（详见 u1-l1 的黑盒接口）。
- **裸机（bare-metal）程序**：不跑操作系统、直接在 CPU（如 Xilinx 的 MicroBlaze 或 ARM A9/R5）上运行的 C 程序。Xilinx 提供一套库函数（`xil_io.h` 里的 `Xil_Out32`/`Xil_In32`）让裸机程序访问内存映射的寄存器。
- **IP 打包（IP packaging）**：Vivado 把一组 RTL 包装成可复用的 IP 核（`.xci`/IP-XACT），其中「软件驱动」是 IP 的一部分，会随 IP 一起分发给用户。`drivers/mem_test/` 正是这个被一起打包的驱动。
- **`C_BASEADDR`**：Vivado 在 Block Design 里给每个 IP 实例分配的基地址。CPU 访问某个寄存器时，实际地址 = `C_BASEADDR + 寄存器偏移`。

> **承接说明**：u2-l1 已经讲过 `byte_address = 4 × index` 的换算、寄存器读写类型（strobe/配置/状态）和三组枚举。本讲不再重复这些定义，而是聚焦「C 驱动如何镜像这同一份契约」。

## 3. 本讲源码地图

本讲只看 `drivers/mem_test/` 下的 5 个文件，它们分属三个目录：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| `drivers/mem_test/src/mem_test.h` | 驱动头文件：寄存器偏移宏、枚举、API 声明 | 软硬契约的 C 镜像 |
| `drivers/mem_test/src/mem_test.c` | 驱动实现：用 `Xil_Out32/Xil_In32` 读写寄存器 | API 实现细节 |
| `drivers/mem_test/src/Makefile` | 把驱动编译进 `libxil.a` 的构建脚本 | 工程集成 |
| `drivers/mem_test/data/mem_test.tcl` | Vivado 生成 `xparameters.h` 的脚本 | 打包元数据 |
| `drivers/mem_test/data/mem_test.mdd` | 驱动定义文件（MDD），声明驱动支持的 IP | 打包元数据 |

一个直觉上的分层：

```
应用代码（你写的 main）
        │  调用
        ▼
mem_test.h / mem_test.c   ← 本讲主角：人话 API
        │  内部调用
        ▼
Xil_Out32 / Xil_In32      ← Xilinx 裸机库（内存映射 IO）
        │  经 AXI-Lite 总线
        ▼
   IP 里的 32 个寄存器     ← u2-l1 讲过的硬件契约
```

本讲的三个最小模块就是从上往下：**偏移宏（契约镜像）→ API 实现（IO 细节）→ 打包元数据（工程交付）**。

## 4. 核心概念与源码讲解

### 4.1 寄存器偏移宏：C 与 VHDL 的一份镜像契约

#### 4.1.1 概念说明

软件和硬件是两个独立编译的世界——硬件用 VHDL 综合，软件用 C 编译。两边都要知道「START 寄存器在 0x00、MODE 在 0x0C」。如果各写各的、互不通气，一旦有人改了硬件地址，软件没跟上，系统就坏了。

这个 IP 用最朴素也最稳妥的办法解决：**让 C 头文件和 VHDL package 各自独立地声明同一套常量，靠人（和测试）保证它们一致**。`mem_test.h` 里的 `MEM_TEST_*_OFFS` 宏就是 VHDL `mem_test_pkg.vhd` 里 `REG_*` 常量的 C 端镜像。

#### 4.1.2 核心流程

换算规则在 u2-l1 已确立：每个寄存器 32 位 = 4 字节，所以

\[
\text{byte\_offset} = 4 \times \text{register\_index}
\]

C 头文件直接把最终的**字节偏移**写成宏，省去运行时乘法。CPU 真正发出的地址是：

\[
\text{access\_address} = \text{C\_BASEADDR} + \text{offset}
\]

例如读状态：`Xil_In32(C_BASEADDR + 0x24)`。

#### 4.1.3 源码精读

先看 C 头文件里的偏移宏定义：

[drivers/mem_test/src/mem_test.h:23-35](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.h#L23-L35) —— 定义了 13 个 `MEM_TEST_*_OFFS` 字节偏移宏。

对照 VHDL package 里的寄存器编号：

[hdl/mem_test_pkg.vhd:30-69](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L30-L69) —— 用 `REG_*` 整数编号 + 注释里的字节地址声明寄存器。

把两边逐项对齐（这是本模块最重要的一张表）：

| 寄存器 | C 宏 | C 偏移 | VHDL 常量 | index | index×4 |
|--------|------|--------|-----------|-------|---------|
| START | `MEM_TEST_START_OFFS` | 0x00 | `REG_START` | 0 | 0x00 |
| STOP | `MEM_TEST_STOP_OFFS` | 0x04 | `REG_STOP` | 1 | 0x04 |
| MODE | `MEM_TEST_MODE_OFFS` | 0x0C | `REG_MODE` | 3 | 0x0C |
| SIZE_LO | `MEM_TEST_SIZE_LO_OFFS` | 0x10 | `REG_SIZE_LO` | 4 | 0x10 |
| SIZE_HI | `MEM_TEST_SIZE_HI_OFFS` | 0x14 | `REG_SIZE_HI` | 5 | 0x14 |
| ADDR_LO | `MEM_TEST_ADDR_LO_OFFS` | 0x18 | `REG_ADDR_LO` | 6 | 0x18 |
| ADDR_HI | `MEM_TEST_ADDR_HI_OFFS` | 0x1C | `REG_ADDR_HI` | 7 | 0x1C |
| PATTERN | `MEM_TEST_PATTERN_OFFS` | 0x20 | `REG_PATTERN_SEL` | 8 | 0x20 |
| STATUS | `MEM_TEST_STATUS_OFFS` | 0x24 | `REG_STATUS` | 9 | 0x24 |
| ERRORS | `MEM_TEST_ERRORS_OFFS` | 0x28 | `REG_ERRORS` | 10 | 0x28 |
| FIRSTERR_LO | `MEM_TEST_FIRSTERR_LO_OFFS` | 0x2C | `REG_FERR_ADDR_LO` | 11 | 0x2C |
| FIRSTERR_HI | `MEM_TEST_FIRSTERR_HI_OFFS` | 0x30 | `REG_FERR_ADDR_HI` | 12 | 0x30 |
| ITER | `MEM_TEST_ITER_OFFS` | 0x34 | `REG_ITER` | 13 | 0x34 |

**两张表完全吻合**。两个值得注意的细节：

- **0x08 是空的**：C 宏从 `0x04`（STOP）直接跳到 `0x0C`（MODE），中间没有 `0x08`。VHDL 里同样没有 index=2 的寄存器（u2-l1 已指出 32 个寄存器空间实际只用 14 个）。两边都老实地「留空」。
- **64 位量拆成两个 32 位寄存器**：`SIZE`（测试长度）和 `ADDR`（起始地址）都是 64 位，各占 `LO`/`HI` 两个相邻寄存器。这是因为 AXI-Lite 数据宽度固定 32 位，一次只能传 32 位。拆/拼逻辑在 4.2 节讲。

再看枚举。C 头文件把三组枚举也完整镜像了：

[drivers/mem_test/src/mem_test.h:42-63](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.h#L42-L63) —— `MemTest_Mode`、`MemTest_Pattern`、`MemTest_Status` 三个枚举。

与 VHDL 对照（`C_MODE_*` / `C_PATTERN_SEL_*` / `C_STATUS_*`）：

| C 枚举值 | 数值 | VHDL 常量 |
|----------|------|-----------|
| `MemTest_Mode_Single` | 0 | `C_MODE_SINGLE` |
| `MemTest_Mode_Continuous` | 1 | `C_MODE_CONTINUOUS` |
| `MemTest_Mode_WriteOnly` | 2 | `C_MODE_WRITEONLY` |
| `MemTest_Mode_ReadOnly` | 3 | `C_MODE_READONLY` |
| `MemTest_Pattern_Count` | 0 | `C_PATTERN_SEL_COUNT` |
| `MemTest_Pattern_Walk1` | 1 | `C_PATTERN_SEL_WALK1` |
| `MemTest_Pattern_OwnAddr` | 2 | `C_PATTERN_SEL_OWNADD` |
| `MemTest_Pattern_Prbn` | 3 | `C_PATTERN_SEL_PRBN` |
| `MemTest_Status_Idle` | 0 | `C_STATUS_IDLE` |
| `MemTest_Status_Writing` | 1 | `C_STATUS_WRITING` |
| `MemTest_Status_Reading` | 2 | `C_STATUS_READING` |
| `MemTest_Status_AxiErr` | 3 | `C_STATUS_AXIERR` |
| `MemTest_Status_IntErr` | 6 | `C_STATUS_INTERR` |
| `MemTest_Status_Unknown` | 7 | `C_STATUS_UNKNOWN` |

注意状态码在 3 之后**跳到 6**——这把「总线错误」与「内部错误」分组，是 u2-l1 讲过的有意设计，C 端也原样保留。

> 小提示：第 40 行还有一个 `typedef enum {RANDOM, IMMEDIATE, SEARCH} strategy;`，但全文件（乃至整个驱动）都没用到它，是个遗留定义。阅读时可以忽略，但不要被它误导以为驱动里有「策略选择」功能。

[drivers/mem_test/src/mem_test.h:40](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.h#L40) —— 未被使用的遗留枚举 `strategy`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「C 宏 = 4 × VHDL index」这条契约。

**操作步骤**：

1. 打开 [hdl/mem_test_pkg.vhd:30-69](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L30-L69)，记下 `REG_PATTERN_SEL` 的 index。
2. 打开 [drivers/mem_test/src/mem_test.h:30](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.h#L30)，读出 `MEM_TEST_PATTERN_OFFS` 的十六进制值。
3. 心算 `4 × index`，看是否等于该十六进制值。

**预期结果**：`REG_PATTERN_SEL = 8`，`4 × 8 = 32 = 0x20`，与 `MEM_TEST_PATTERN_OFFS = 0x20` 完全相等。如果哪一行对不上，说明软件和硬件的契约失配——这正是回归仿真（u1-l3）会替你守住的底线。

#### 4.1.5 小练习与答案

**练习 1**：为什么 C 头文件直接写最终字节偏移（`0x0C`），而不是像 VHDL 那样写 index（`3`）再让代码乘 4？

> **答案**：C 宏最终要拼进 `Xil_In32(base + offset)` 的地址运算，直接用字节偏移最直观、运行时零开销。VHDL 端写 index 是因为寄存器译码（`axi_slave_ipif`）按编号工作，index 更贴近硬件结构。两边各自选择对自己最自然的表达。

**练习 2**：假如硬件把 `REG_MODE` 的 index 从 3 改成 2（填上 0x08 那个空位），软件需要改什么？如果忘了改会怎样？

> **答案**：必须同步把 `MEM_TEST_MODE_OFFS` 从 `0x0C` 改成 `0x08`。忘了改的话，软件写「MODE」实际写进了 0x0C（硬件现在的空位），IP 收不到模式配置，测试行为错误。这就是软硬双份声明的一致性风险。

---

### 4.2 API 函数实现：用 Xil_Out32 / Xil_In32 访问寄存器

#### 4.2.1 概念说明

有了偏移宏，下一步就是用它们读写寄存器。Xilinx 裸机库里有两个最基础的内存映射 IO 函数：

- `Xil_Out32(addr, value)`：向地址 `addr` 写入 32 位 `value`。
- `Xil_In32(addr)`：从地址 `addr` 读出 32 位值。

它们底层就是 CPU 的一条 store/load 指令——因为 AXI-Lite 寄存器被映射进了 CPU 的地址空间，CPU「写内存」等价于「写寄存器」。

`mem_test.c` 里每个 API 函数都是对这两个函数的薄封装：把「人话动作」翻译成「一次或几次寄存器读写」。

#### 4.2.2 核心流程

把 9 个 API 按功能分三类：

```
① 配置类（仅在停止时调用，顺序无严格要求）
   SetMode    → 写 MODE 寄存器（32 位枚举值）
   SetPattern → 写 PATTERN 寄存器（32 位枚举值）
   SetRange   → 写 ADDR(64) + SIZE(64)，共 4 次写

② 触发类（strobe）
   Start → 写 1 到 START 寄存器（清除错误计数、启动 FSM）
   Stop  → 写 1 到 STOP 寄存器（仅 Continuous 模式用，优雅停止）

③ 查询类（只读）
   GetStatus        → 读 STATUS（32 位枚举）
   GetErrors        → 读 ERRORS（32 位计数）
   GetIterations    → 读 ITER（32 位计数）
   GetFirstErrorAddr → 读 FIRSTERR_LO + FIRSTERR_HI，拼成 64 位
```

**64 位拆/拼**是本模块的技术核心。一次 AXI-Lite 写只能传 32 位，所以 64 位量要拆成 LO/HI 两次：

- 写入（`SetRange`）：先写 `size` 低 32 位，再写 `size >> 32`（高 32 位）；地址同理。
- 读出（`GetFirstErrorAddr`）：先读高位，再读低位，用 `(addrHigh << 32) | addrLow` 拼回 64 位。

读时**先读 HI 后读 LO** 是个小优化：LO 寄存器里硬件可以快照当时的值，但本驱动实现简单，两次读之间硬件值不变，所以顺序不影响正确性——这点留作练习。

一次完整测试的调用序列：

```
SetMode(base, Single);             // 1. 选模式
SetPattern(base, OwnAddr);         // 2. 选 pattern
SetRange(base, 0x10000000, 4096);  // 3. 设地址范围与大小
Start(base);                       // 4. 启动（清零错误计数）
while (GetStatus(base) != Idle) {} // 5. 轮询直到完成
err  = GetErrors(base);            // 6. 读错误数
addr = GetFirstErrorAddr(base);    // 7. 读首个错误地址
```

#### 4.2.3 源码精读

先看文件头：驱动实现只 include 了自家头文件和 Xilinx 的 IO 库。

[drivers/mem_test/src/mem_test.c:7-8](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L7-L8) —— `#include "mem_test.h"` 和 `#include <xil_io.h>`，后者提供 `Xil_Out32/Xil_In32`。

**触发类（最简单）**：`Start` 和 `Stop` 都是写常量 `1` 到对应 strobe 寄存器。

[drivers/mem_test/src/mem_test.c:11-14](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L11-L14) —— `MemTest_Start` 向 `START_OFFS` 写 1。

```c
void MemTest_Start(const uint32_t baseAddr) {
    Xil_Out32(baseAddr + MEM_TEST_START_OFFS, 1);
}
```

为什么是写 `1`？因为 START 是 strobe 型寄存器，硬件只关心「有没有写动作」，不关心写了什么值（u2-l1 讲过触发型寄存器语义）。驱动固定写 1 是约定俗成。

[drivers/mem_test/src/mem_test.c:17-20](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L17-L20) —— `MemTest_Stop` 同理向 `STOP_OFFS` 写 1。

**配置类**：`SetMode` / `SetPattern` 直接把枚举强转 `uint32_t` 写入对应寄存器。

[drivers/mem_test/src/mem_test.c:23-27](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L23-L27) —— `MemTest_SetMode` 写 MODE 寄存器。

[drivers/mem_test/src/mem_test.c:30-34](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L30-L34) —— `MemTest_SetPattern` 写 PATTERN 寄存器。

**64 位拆分写入**（本模块最值得读的一段）：`SetRange` 把 64 位 `startAddr` 和 `size` 各拆成低/高 32 位，共 4 次写。

[drivers/mem_test/src/mem_test.c:37-45](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L37-L45) —— `MemTest_SetRange`：先写 SIZE_LO/HI，再写 ADDR_LO/HI。

```c
void MemTest_SetRange(const uint32_t baseAddr, const uint64_t startAddr, const uint64_t size) {
    Xil_Out32(baseAddr+MEM_TEST_SIZE_LO_OFFS, (uint32_t)size);            // 低 32 位
    Xil_Out32(baseAddr+MEM_TEST_SIZE_HI_OFFS, (uint32_t)(size >> 32));    // 高 32 位
    Xil_Out32(baseAddr+MEM_TEST_ADDR_LO_OFFS, (uint32_t)startAddr);
    Xil_Out32(baseAddr+MEM_TEST_ADDR_HI_OFFS, (uint32_t)(startAddr >> 32));
}
```

`(uint32_t)(size >> 32)` 就是把 64 位数右移 32 位、丢掉高位、取低 32 位，等价于「取高 32 位」。两次写合起来在硬件里拼回完整 64 位值。

**查询类**：`GetStatus` / `GetErrors` / `GetIterations` 各一次 `Xil_In32`，外加强转回枚举。

[drivers/mem_test/src/mem_test.c:48-51](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L48-L51) —— `MemTest_GetStatus` 读 STATUS 寄存器并强转成 `MemTest_Status`。

[drivers/mem_test/src/mem_test.c:54-57](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L54-L57) —— `MemTest_GetErrors` 读 ERRORS 计数。

[drivers/mem_test/src/mem_test.c:60-63](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L60-L63) —— `MemTest_GetIterations` 读 ITER 计数。

**64 位拼接读出**：`GetFirstErrorAddr` 读两个半字再拼回 64 位。

[drivers/mem_test/src/mem_test.c:66-71](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L66-L71) —— `MemTest_GetFirstErrorAddr`：先读 HI，再读 LO，用移位或运算拼接。

```c
uint64_t MemTest_GetFirstErrorAddr(const uint32_t baseAddr) {
    const uint64_t addrHigh = Xil_In32(baseAddr+MEM_TEST_FIRSTERR_HI_OFFS);
    const uint64_t addrLow  = Xil_In32(baseAddr+MEM_TEST_FIRSTERR_LO_OFFS);
    return (addrHigh << 32) | addrLow;
}
```

注意两次读出的结果都先存进 `uint64_t` 局部变量——这很关键。如果直接写 `(Xil_In32(...) << 32)`，`Xil_In32` 返回的是 32 位 `u32`，左移 32 位在 32 位类型上属于**未定义行为**（shift count >= type width），结果会是 0 或编译警告。先转成 `uint64_t` 再移位才安全。这是嵌入式 C 里一个经典的位运算坑。

#### 4.2.4 代码实践

**实践目标**：跟踪「写 START 寄存器」从 C 函数到硬件地址的完整路径。

**操作步骤**：

1. 从 [mem_test.c:13](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L13) `MemTest_Start` 出发，记下它调用的偏移宏 `MEM_TEST_START_OFFS`。
2. 回到 [mem_test.h:23](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.h#L23)，确认该宏 = `0x00`。
3. 假设 Block Design 给 IP 分配的 `C_BASEADDR = 0x4000_0000`，写出 CPU 实际发出的写地址与写值。

**需要观察的现象**：CPU 对 `0x40000000` 发起一次 32 位写、值为 1，该写经 AXI-Lite 到达 IP 的 `S00_AXI`，最终被译码成对 START 寄存器的 strobe（u4-l1 会讲译码细节）。

**预期结果**：地址 = `0x40000000 + 0x00 = 0x40000000`，值 = `1`，IP 内部 FSM 脱离 Idle 开始写存储器（可由随后 `GetStatus` 返回 `Writing` 验证）。

> 待本地验证：以上行为依赖真实硬件或带 AXI-Lite 从机模型的仿真平台。纯软件编译无法观察到寄存器副作用。

#### 4.2.5 小练习与答案

**练习 1**：`SetRange` 先写 SIZE 后写 ADDR，`GetFirstErrorAddr` 先读 HI 后读 LO。这些顺序是必须的吗？颠倒会出错吗？

> **答案**：对当前实现**不会出错**。`SetRange` 四次都是普通配置寄存器写，硬件在 START 之前不会「消费」它们，顺序无所谓。`GetFirstErrorAddr` 两次读之间，FIRSTERR 在测试完成后就固定不变，先读谁都不影响拼出来的 64 位值。这些顺序只是作者的书写习惯，并非协议要求。

**练习 2**：把 `GetFirstErrorAddr` 的实现改成一行 `return (Xil_In32(HI_OFFS) << 32) | Xil_In32(LO_OFFS);`，会有什么隐患？

> **答案**：`Xil_In32` 返回 `u32`（32 位），`u32 << 32` 是移位位数 ≥ 类型宽度的**未定义行为**，在许多编译器/平台上结果为 0，于是高 32 位丢失，读出的首个错误地址永远是 0~0xFFFFFFFF 的低地址。原实现先存入 `uint64_t` 再移位，正是为了规避这个坑。

**练习 3**：为什么 `Start` 写的是常量 `1`，而 `SetMode` 写的是参数 `mode`？

> **答案**：START 是 strobe 触发型寄存器，硬件只检测「写动作发生」，数据值无意义，所以驱动写一个固定非零值（1）。MODE 是配置型寄存器，硬件要把写入的数据当作模式编号保存，所以必须把调用方传入的 `mode` 真正写进去。

---

### 4.3 驱动打包元数据：Vivado 怎么把驱动挂到 IP 上

#### 4.3.1 概念说明

写完 `mem_test.c/.h` 只是第一步。Vivado 打包 IP 时，会把整个 `drivers/mem_test/` 目录连同 RTL 一起塞进 IP 包。用户在 Block Design 里例化这个 IP、生成比特流后，Vivado 还会自动「生成 BSP（Board Support Package）」，把驱动源码拷进裸机工程、生成一张「IP 实例地址表」——就是著名的 `xparameters.h`。

`data/` 目录下的两个文件就是告诉 Vivado **怎么生成这张表、驱动支持哪个 IP**。它们本身不是 C 代码，而是 Vivado 工具链的描述文件（TCL 脚本 + MDD 定义）。

#### 4.3.2 核心流程

```
Vivado 打包 IP (scripts/package.tcl, 见 u5-l2)
        │  把 drivers/mem_test/ 一并打包
        ▼
用户在 Block Design 例化 mem_test，分配 C_BASEADDR
        │
        ▼
生成比特流后 → Vivado/SDK 生成 BSP
        │  读 mem_test.mdd（这是个驱动，支持 mem_test 外设）
        │  执行 mem_test.tcl 的 generate 过程
        ▼
产物：xparameters.h 里出现
      #define XPAR_MEM_TEST_0_BASEADDR  0x40000000
      #define XPAR_MEM_TEST_0_DEVICE_ID ...
      #define XPAR_MEM_TEST_NUM_INSTANCES 1
        │
        ▼
应用代码 #include "xparameters.h"，把 baseAddr 传给 MemTest_* 函数
```

关键点：**应用代码里的 `baseAddr` 不是凭空写的，而是来自 `xparameters.h` 的 `XPAR_*_BASEADDR`**，而这个宏由 `mem_test.tcl` 脚本根据 Block Design 里实际分配的地址自动生成。这样改了地址不用改代码，重新生成 BSP 即可。

#### 4.3.3 源码精读

**MDD（Microprocessor Driver Definition）**：声明「这是一个驱动、它支持 `mem_test` 外设、版本 1.0」。

[drivers/mem_test/data/mem_test.mdd:5-9](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/data/mem_test.mdd#L5-L9) —— `DRIVER mem_test` 块，`supported_peripherals = (mem_test)`、`copyfiles = all`。

```
OPTION psf_version = 2.1;
BEGIN DRIVER mem_test
    OPTION supported_peripherals = (mem_test);   ← 绑定到同名外设
    OPTION copyfiles = all;                       ← 所有源文件拷进 BSP
    OPTION VERSION = 1.0;
    OPTION NAME = mem_test;
END DRIVER
```

`sapplied_peripherals = (mem_test)` 是核心：它告诉工具「当 Block Design 里出现一个叫 `mem_test` 的 IP 实例时，用本驱动」。`copyfiles = all` 表示把 `src/` 下所有 `.c/.h` 都拷给用户工程。

**TCL 生成脚本**：定义 BSP 生成时要往 `xparameters.h` 里写哪些宏。

[drivers/mem_test/data/mem_test.tcl:3-5](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/data/mem_test.tcl#L3-L5) —— `generate` 过程调用 `xdefine_include_file` 声明 4 个宏。

```tcl
proc generate {drv_handle} {
    xdefine_include_file $drv_handle "xparameters.h" mem_test \
        "NUM_INSTANCES" "DEVICE_ID" "C_BASEADDR" "C_HIGHADDR"
}
```

`xdefine_include_file` 是 Xilinx BSP 工具提供的辅助过程。它的作用是：对 Block Design 里每个 `mem_test` 实例，从 IP 的参数表里读取 `C_BASEADDR`、`C_HIGHADDR`、`DEVICE_ID`，加上实例计数 `NUM_INSTANCES`，生成形如：

```c
#define XPAR_MEM_TEST_NUM_INSTANCES   1
#define XPAR_MEM_TEST_0_DEVICE_ID     XPAR_MEM_TEST_0_DEVICE_ID
#define XPAR_MEM_TEST_0_BASEADDR      0x40000000
#define XPAR_MEM_TEST_0_HIGHADDR      0x4000FFFF
```

注意这里**没有**列 `C_M00_AXI_DATA_WIDTH` 之类的 generic——因为驱动对所有数据宽度的行为完全相同（都是 32 位寄存器访问），不需要按参数特化。

**Makefile**：让驱动源码编译进 BSP 的静态库 `libxil.a`。

[drivers/mem_test/src/Makefile:14](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/Makefile#L14) —— 用 `$(wildcard *.c)` 自动收集所有 C 源文件生成对象列表。

[drivers/mem_test/src/Makefile:17-21](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/Makefile#L17-L21) —— `libs` 目标：编译、归档进 `libxil.a`、清理。

`COMPILER`、`ARCHIVER` 等变量留空，是因为它们由 Vitis/SDK 的 BSP 构建系统在调用时通过命令行注入（如 `make COMPILER=arm-none-eabi-gcc ...`）。`OBJECTS = $(addsuffix .o, $(basename $(wildcard *.c)))` 这行用 `wildcard` 自动枚举当前目录所有 `.c`，去掉后缀加上 `.o`，得到对象文件列表——这是最近一次 BUGFIX（提交 `c731a8f`，修复 Vitis/Windows 下 `*.o` 通配在某些情况失效的问题）改进的写法，把 `OUTS = *.o` 换成了显式展开的对象列表，并顺带支持了汇编文件 `.S`。

#### 4.3.4 代码实践

**实践目标**：理解 `xparameters.h` 里的 `BASEADDR` 是怎么从 Block Design 流到 C 代码的。

**操作步骤（源码阅读型，无需硬件）**：

1. 读 [mem_test.tcl:4](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/data/mem_test.tcl#L4)，记下 `xdefine_include_file` 列出的 4 个字符串参数。
2. 设想 Block Design 里有一个 `mem_test_0` 实例，地址区间 `0x40000000 ~ 0x4000FFFF`。
3. 手写 BSP 生成后会产出的 4 行 `#define`。

**预期结果**：

```c
#define XPAR_MEM_TEST_NUM_INSTANCES   1
#define XPAR_MEM_TEST_0_DEVICE_ID     XPAR_MEM_TEST_0_DEVICE_ID
#define XPAR_MEM_TEST_0_BASEADDR      0x40000000
#define XPAR_MEM_TEST_0_HIGHADDR      0x4000FFFF
```

应用代码这样取基地址：

```c
#include "xparameters.h"
#include "mem_test.h"

MemTest_Start(XPAR_MEM_TEST_0_BASEADDR);   // 示例代码
```

#### 4.3.5 小练习与答案

**练习 1**：如果用户在 Block Design 里例化了**两个** `mem_test` IP（地址分别是 `0x40000000` 和 `0x40010000`），`xparameters.h` 里会多出什么？应用代码怎么分别访问它们？

> **答案**：`NUM_INSTANCES` 变成 2，并多出一组 `XPAR_MEM_TEST_1_BASEADDR / HIGHADDR`。应用代码对两个实例分别调用 `MemTest_Start(XPAR_MEM_TEST_0_BASEADDR)` 和 `MemTest_Start(XPAR_MEM_TEST_1_BASEADDR)`——驱动所有 API 第一个参数都是 `baseAddr`，天生支持多实例。

**练习 2**：为什么 `mem_test.tcl` 的 `generate` 过程里没有提到 `MEM_TEST_*_OFFS` 这些偏移宏？

> **答案**：偏移宏是**驱动内部**的常量，已经硬编码在 `mem_test.h` 里，对所有实例都一样，不需要 BSP 按实例生成。`generate` 只负责生成「每个实例各不相同」的信息（基地址、设备号、实例数），二者职责分明。

**练习 3**：最近的提交 `c731a8f` 把 Makefile 里 `OUTS = *.o` 改成了 `OBJECTS = $(addsuffix .o, $(basename $(wildcard *.c)))`，这解决了什么问题？

> **答案**：`OUTS = *.o` 依赖 make 的变量展开阶段就匹配磁盘上的 `.o` 文件，但在 Vitis（Windows）的某些流程里，编译产物名匹配不到通配，导致归档步骤找不到对象文件。新写法在 make 解析时**显式枚举所有 `.c` 源文件**并推算出对应的 `.o` 名字，不依赖 `.o` 文件已存在，更稳健；同时新增 `ASSEMBLY_OBJECTS` 以兼容 `.S` 汇编源。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格里要求的那个任务：

> 用驱动 API 写一小段 C 伪代码：设置 OwnAddr pattern + Single 模式、测试 `0x10000000` 起 4KB 区域、启动后轮询状态、完成后打印错误数与首个错误地址。

下面是可直接放进裸机 `main.c` 的参考实现（**示例代码**，非项目原有文件）：

```c
/* 示例代码：完整的内存测试一次执行 */
#include <stdio.h>
#include "xparameters.h"
#include "mem_test.h"

#define MEM_TEST_BASE   XPAR_MEM_TEST_0_BASEADDR   /* 来自 xparameters.h */

void RunOneTest(void) {
    /* 1. 配置：必须在前一次测试结束（Idle）后才能改配置 */
    MemTest_SetMode   (MEM_TEST_BASE, MemTest_Mode_Single);
    MemTest_SetPattern(MEM_TEST_BASE, MemTest_Pattern_OwnAddr);
    MemTest_SetRange  (MEM_TEST_BASE,
                       0x10000000ULL,   /* 起始地址 */
                       4096ULL);        /* 测试 4 KB */

    /* 2. 启动：Start() 会清零 ERRORS/ITER 计数并触发 FSM */
    MemTest_Start(MEM_TEST_BASE);

    /* 3. 轮询：Single 模式下写完读完后自动回 Idle */
    while (MemTest_GetStatus(MEM_TEST_BASE) != MemTest_Status_Idle) {
        /* 真实工程里建议加超时退出，避免总线错误时死循环 */
    }

    /* 4. 读结果 */
    uint32_t  err  = MemTest_GetErrors(MEM_TEST_BASE);
    uint64_t  faddr= MemTest_GetFirstErrorAddr(MEM_TEST_BASE);
    uint32_t  iter = MemTest_GetIterations(MEM_TEST_BASE);

    if (err == 0) {
        printf("PASS: %u iterations, no errors.\n", iter);
    } else {
        printf("FAIL: %u errors, first at 0x%08llX\n",
               (unsigned)err, (unsigned long long)faddr);
    }
}
```

**配套实践任务**：

1. **阅读核对**：对照 [mem_test.c:37-45](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/drivers/mem_test/src/mem_test.c#L37-L45) 确认 `MemTest_SetRange(base, 0x10000000, 4096)` 实际向哪 4 个寄存器写入了什么值（写出 4 行「地址+数据」）。
2. **故障推演**：如果存储器在第 `0x10000080` 处有一位数据线粘连，用 OwnAddr pattern 时，`GetFirstErrorAddr` 应该返回什么？`GetErrors` 至少会返回多少？（提示：OwnAddr pattern 让每个字节等于自己的地址，地址不同则数据不同，粘连位会让一段连续地址都比对失败。）
3. **超时改造**：给上面的 `while` 循环加一个超时计数器，超过 N 次仍非 Idle 就主动读一次 `GetStatus`，若为 `MemTest_Status_AxiErr`/`IntErr` 则打印「总线/内部错误」并退出。这能避免硬件挂死时软件跟着死循环。

> 待本地验证：第 2、3 题的行为依赖真实硬件或带 AXI 从机模型的仿真。可在 [u5-l1](u5-l1-testbench-and-axi-emulation.md) 讲到的 `tb/top_tb.vhd` 里用类似方式注入错误来观察这些值。

## 6. 本讲小结

- `mem_test.h` 的 `MEM_TEST_*_OFFS` 宏是 VHDL `mem_test_pkg.vhd` 的 `REG_*` 寄存器地图的 C 端镜像，全部满足 `byte_offset = 4 × index`，两边逐项吻合。
- 三组 C 枚举（`MemTest_Mode` / `MemTest_Pattern` / `MemTest_Status`）的数值与 VHDL 的 `C_MODE_*` / `C_PATTERN_SEL_*` / `C_STATUS_*` 完全一致，包括状态码 3 之后跳到 6 的分组设计。
- 驱动实现极其薄：每个 API 就是对 `Xil_Out32` / `Xil_In32` 的一两次封装——strobe 写 1，配置写参数值，状态/计数直接读。
- 64 位量（ADDR/SIZE/FIRSTERR）通过 LO/HI 两个 32 位寄存器拆传；读拼接时先把半字存入 `uint64_t` 再移位，规避了 `u32 << 32` 的未定义行为。
- `data/mem_test.mdd` 声明驱动支持的 IP，`data/mem_test.tcl` 在 BSP 生成时产出 `xparameters.h` 的 `C_BASEADDR` 等宏——应用代码因此不必硬编码地址。
- Makefile 用 `$(wildcard *.c)` 显式枚举源文件（`c731a8f` 的 BUGFIX），让驱动稳健地编译进 `libxil.a`。

## 7. 下一步学习建议

到这里，你已经从软件视角把「控制一个内存测试器」的完整链路打通了：寄存器地图（u2-l1）→ 模式与 pattern 语义（u2-l2）→ C 驱动封装（本讲）。接下来有两个方向：

- **向下看硬件实现**：进入第三单元。建议先读 [u3-l1 顶层 wrapper 架构](u3-l1-wrapper-architecture.md)，看 AXI-Lite 从机怎么把 `Xil_Out32` 发来的写事务译码成 `Reg_Wr/Reg_WData` 信号交给核心逻辑；再到 [u3-l3 主状态机](u3-l3-main-fsm.md) 看 `MemTest_Start` 写下的那一下「1」如何驱动 FSM 跑完写→读→比对。
- **向验证方向看**：如果想亲眼看到 `GetErrors` / `GetFirstErrorAddr` 在出错时的真实取值，直接跳到 [u5-l1 testbench 与 AXI 仿真](u5-l1-testbench-and-axi-emulation.md)，那里用 `psi_tb` 的 AXI 辅助过程在仿真里注入错误、校验这些寄存器的值——相当于本讲「待本地验证」部分的官方答案。
