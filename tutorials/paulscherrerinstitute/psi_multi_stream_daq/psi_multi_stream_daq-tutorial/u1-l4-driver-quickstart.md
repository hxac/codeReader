# 软件驱动快速上手：从初始化到读取数据

## 1. 本讲目标

前三讲我们一直在看「硬件」：IP 核是什么、仓库怎么组织、顶层实体有哪些端口。但一块 FPGA IP 核如果只有硬件、没有软件去配置它、读它的数据，就只是一块死板的硅片。本讲把视角切换到「软件」一侧——**C 语言驱动 `driver/psi_ms_daq`**。

学完本讲，你应当能够：

- 说清楚驱动在整个系统里扮演的角色：它是运行在 CPU（如 Zynq 的 PS 侧）上的代码，负责通过 **AXI Slave 寄存器**配置 IP、通过 **内存拷贝**读取 DMA 写入 DDR 的数据。
- 写出一个最小化的使用骨架：`PsiMsDaq_Init` → `PsiMsDaq_GetStrHandle` → `PsiMsDaq_Str_Configure` → 注册中断回调 → `PsiMsDaq_Str_SetEnable`，并在回调里 `GetDataUnwrapped` + `MarkAsFree`。
- 理解 `PsiMsDaq_StrConfig_t` 八个字段各自的含义，以及它们最终被写进了哪些硬件寄存器。
- 理解驱动独有的「**访问函数注入**」机制：为什么驱动不直接读写指针，而是把「读寄存器 / 写寄存器 / 拷贝数据」三个动作做成可替换的函数指针。

> 本讲是面向软件使用的入门，**只讲怎么用、为什么这么设计**，不深入中断去抖、环形缓冲解包等算法细节——那些是后续 u4 单元（专家层）的内容。

## 2. 前置知识

本讲假设你已经读过前三讲，尤其是：

- **u1-l3 顶层 IP 核**：知道 IP 有 AXI Slave（CPU 配置寄存器）、AXI Master（写 DDR）、`Streams_g` 路数据流输入，以及每流可配置多个「窗口（window）」。
- 一些基本概念，如果你不熟悉，先看这里的通俗解释：

| 术语 | 通俗解释 |
|------|----------|
| **驱动（driver）** | 一组 C 函数，封装了对硬件寄存器的读写，让上层应用不用关心寄存器地址。 |
| **句柄（handle）** | 一个不透明的指针（`void*`），代表「某个对象」。你拿到它就能操作对象，但看不到对象内部细节。 |
| **MMIO / 寄存器访问** | CPU 把 IP 的寄存器映射到一段内存地址，向那段地址写 32 位整数等于配置硬件。 |
| **DMA** | Direct Memory Access，硬件自己把数据搬进 DDR，CPU 不参与每个字节。本 IP 用 AXI Master 把采集到的数据写进 DDR。 |
| **中断（IRQ）** | 硬件干完一件事（比如写满一个窗口）后，主动「打断」CPU 通知它，CPU 不必轮询。 |
| **Cache 一致性** | DDR 数据可能被 CPU 缓存。DMA 写了 DDR 后，CPU 若不先 invalidate cache，可能读到旧数据。 |
| **窗口（window）** | 每路流在 DDR 里划分成若干等大的环形/线性缓冲区，每个就是一个窗口。 |

一句话定位：**驱动 = 把「写寄存器配置流」+「等中断」+「从 DDR 把窗口数据拷出来」这套流程封装成好用 API 的 C 库。**

## 3. 本讲源码地图

本讲只涉及 `driver/` 目录下两个文件：

| 文件 | 行数 | 作用 |
|------|------|------|
| [driver/psi_ms_daq.h](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h) | 约 720 行 | 头文件：所有类型定义（句柄、配置结构体、回调、返回码）、寄存器地址宏、函数原型，以及顶部一段 `@mainpage` 文档和一个完整示例。 |
| [driver/psi_ms_daq.c](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c) | 约 820 行 | 实现：内部数据结构（IP 实例、流实例）、`PsiMsDaq_Init`、中断分发、流配置、窗口数据读取等全部函数体。 |

驱动不依赖任何第三方库，只用标准 C 的 `stdint.h`、`stdbool.h`、`string.h`、`stdlib.h`（见 [driver/psi_ms_daq.h:L137-L139](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L137-L139) 与 [driver/psi_ms_daq.c:L7-L8](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L7-L8)）。所以它能轻松移植到裸机（bare-metal）、FreeRTOS、Linux 用户态等各种环境。

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：

1. **4.1** 驱动的角色与「访问函数注入」机制
2. **4.2** 初始化与流句柄获取（`PsiMsDaq_Init` / `GetStrHandle`）
3. **4.3** 流配置 `PsiMsDaq_StrConfig_t` 与 `Str_Configure`
4. **4.4** 中断处理 `HandleIrq` 与窗口回调两种方案
5. **4.5** 数据读取与窗口释放（`GetDataUnwrapped` / `MarkAsFree`）

---

### 4.1 驱动的角色与「访问函数注入」机制

#### 4.1.1 概念说明

先想一个朴素的问题：**驱动要配置硬件，就得读写寄存器；读写寄存器就得知道寄存器映射在 CPU 地址空间的哪里。** 但「地址在哪」这件事，不同平台完全不同：

- 裸机 / RTOS：直接把寄存器地址当指针解引用（MMIO）。
- Linux 用户态：要走 `/dev/mem` 或专门的内核驱动，用 `ioctl`。
- 单元测试：根本没有硬件，要用一个软件模拟的寄存器数组。

如果把「直接解引用指针」写死在驱动里，它就只能用在裸机上。这个驱动用了更聪明的办法：**把「读寄存器 / 写寄存器 / 拷贝数据」这三个底层动作抽象成函数指针，由调用者在初始化时注入。** 这就是「访问函数注入（access function injection）」。

驱动内部还有两层句柄：

- `PsiMsDaq_IpHandle`：代表**整个 IP 核**的一个实例（一个 `void*`，见 [driver/psi_ms_daq.h:L190](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L190)）。
- `PsiMsDaq_StrHandle`：代表 IP 里**某一路流**（[driver/psi_ms_daq.h:L191](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L191)）。

之所以用 `void*` 而不是直接暴露结构体，是为了**信息隐藏**：调用者拿到的只是「一个指针」，不能（也不应该）去摸内部字段，这样将来内部结构改了也不破坏调用者代码。

#### 4.1.2 核心流程

注入机制的运作流程：

```text
调用者
  │  在 PsiMsDaq_Init 时传入 PsiMsDaq_AccessFct_t（或传 NULL 用默认实现）
  ▼
PsiMsDaq_Inst_t 内部保存三个函数指针:
  regWrFct  ──┐
  regRdFct  ──┼──► 所有后续 PsiMsDaq_RegWrite/RegRead/数据拷贝
  memcpyFct ──┘     都通过这三个指针间接调用
                       │
                       ▼
              实际的硬件访问（MMIO / ioctl / 模拟）
```

默认实现就是把地址强转成 `volatile uint32_t*` 解引用——这正是裸机 MMIO 的标准写法。

#### 4.1.3 源码精读

先看注入用的结构体定义：

```c
typedef struct {
    PsiMsDaq_DataCopy_f* dataCopy;  // 拷贝 DDR 数据到目标缓冲
    PsiMsDaq_RegWrite_f* regWrite;  // 写寄存器
    PsiMsDaq_RegRead_f*  regRead;   // 读寄存器
} PsiMsDaq_AccessFct_t;
```

完整定义见 [driver/psi_ms_daq.h:L276-L283](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L276-L283)。三个函数指针的类型分别在 [driver/psi_ms_daq.h:L201](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L201)（`PsiMsDaq_DataCopy_f`）、[L209](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L209)（`RegWrite_f`）、[L217](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L217)（`RegRead_f`）。注意 `DataCopy_f` 的 `src` 注释特意写明「exactly the way the IP sees the address space」——因为 DMA 写入的地址是 IP 视角的物理地址，可能与 CPU 视角不同，所以才需要让用户自己提供拷贝函数。

再看驱动内部如何保存这些指针，以及默认实现：

```c
// 默认写寄存器：把地址当指针，写入值（裸机 MMIO）
void PsiMsDaq_RegWrite_Standard(const uint32_t addr, const uint32_t value) {
    volatile uint32_t* addr_p = (volatile uint32_t *)(size_t)addr;
    *addr_p = value;
}
// 默认读寄存器
uint32_t PsiMsDaq_RegRead_Standard(const uint32_t addr) {
    volatile uint32_t* addr_p = (volatile uint32_t *)(size_t)addr;
    return *addr_p;
}
```

见 [driver/psi_ms_daq.c:L51-L66](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L51-L66)。`volatile` 关键字告诉编译器「别优化掉这次访问」，因为这是对硬件寄存器的读写，每次都必须真正发生。

驱动内部 IP 实例结构体（定义在 .c 文件里，对外不可见）保存了基址与这三个指针：

```c
typedef struct {
    uint32_t baseAddr;          // IP 寄存器基址
    uint8_t  maxStreams;
    uint8_t  maxWindows;
    uint32_t strAddrOffs;       // 流间窗口地址偏移（见 4.3）
    PsiMsDaq_StrInst_t* streams; // 每流一个子结构
    PsiMsDaq_DataCopy_f* memcpyFct;
    PsiMsDaq_RegWrite_f* regWrFct;
    PsiMsDaq_RegRead_f*  regRdFct;
} PsiMsDaq_Inst_t;
```

见 [driver/psi_ms_daq.c:L30-L39](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L30-L39)。所有寄存器访问最终都汇到两个函数 `PsiMsDaq_RegWrite` / `PsiMsDaq_RegRead`（[driver/psi_ms_daq.c:L730-L752](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L730-L752)），它们做的事就是「`baseAddr + addr` 然后调用注入的函数指针」：

```c
PsiMsDaq_RetCode_t PsiMsDaq_RegWrite(PsiMsDaq_IpHandle ipHandle, const uint32_t addr, const uint32_t value) {
    PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*)ipHandle;
    inst_p->regWrFct(inst_p->baseAddr + addr, value);  // 关键：基址 + 偏移
    return PsiMsDaq_RetCode_Success;
}
```

这就是整个驱动「**所有寄存器访问的唯一出口**」。理解了这一点，后续看任何 `PsiMsDaq_RegWrite(...)` 调用都等于「向 `baseAddr+addr` 写一个 32 位值」。

#### 4.1.4 代码实践

**实践目标**：亲手看清「注入」如何改变驱动的行为，而不需要任何真实硬件。

**操作步骤**（源码阅读型 + 可选运行）：

1. 打开 [driver/psi_ms_daq.c:L132-L181](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L132-L181) 的 `PsiMsDaq_Init`，找到 [L145-L154](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L145-L154) 这段 `if (NULL == accessFct_p)` 分支：传 `NULL` 就用三个 `_Standard` 默认函数，否则用用户传入的。
2. 想象你要写一个**单元测试**：声明一个全局数组 `uint32_t fakeRegs[0x8000/4]` 作为「模拟寄存器空间」，再写三个函数 `fakeRegWrite`/`fakeRegRead`/`fakeMemCopy` 操作这个数组，然后把它们装进 `PsiMsDaq_AccessFct_t` 传给 `PsiMsDaq_Init`。

**示例代码**（非项目原有代码，仅为说明注入机制）：

```c
/* 示例代码：演示访问函数注入，非仓库源码 */
#include "psi_ms_daq.h"
#include <stdio.h>

static uint32_t fakeRegs[0x2000];   /* 模拟寄存器空间（简化） */

static void myRegWrite(const uint32_t addr, const uint32_t value) {
    printf("[WR] addr=0x%05x value=0x%08x\n", addr, value);
    fakeRegs[addr / 4] = value;     /* 真实驱动里是 volatile 解引用 */
}
static uint32_t myRegRead(const uint32_t addr) {
    printf("[RD] addr=0x%05x -> 0x%08x\n", addr, fakeRegs[addr / 4]);
    return fakeRegs[addr / 4];
}
static void myMemCopy(void* dst, void* src, size_t n) {
    memcpy(dst, src, n);            /* 真实场景里可能要做地址翻译 */
}

int main(void) {
    PsiMsDaq_AccessFct_t fct = {
        .dataCopy = myMemCopy,
        .regWrite = myRegWrite,
        .regRead  = myRegRead,
    };
    /* 注入自定义访问函数；若传 NULL 则用默认 MMIO 实现 */
    PsiMsDaq_IpHandle h = PsiMsDaq_Init(0x40000000, 4, 8, &fct);
    /* 之后所有驱动内部寄存器访问都会打印日志 */
    return 0;
}
```

**需要观察的现象**：运行后你会看到 `PsiMsDaq_Init` 内部立刻打印出一串 `[WR]` 日志——因为它在初始化时会写 `GCFG`、`STRENA`、`IRQENA`、`IRQVEC` 等寄存器（见 4.2.3）。

**预期结果**：通过这种「可注入访问函数」的设计，同一份驱动既能在裸机上跑（默认实现），也能在测试环境里用模拟数组跑（自定义实现），**驱动源码一行都不用改**。

> 若你手头没有 C 编译环境，可以只做第 1 步的源码阅读，结论同样成立。**待本地验证**：具体打印的寄存器地址取决于 `PsiMsDaq_Init` 内部写寄存器的顺序。

#### 4.1.5 小练习与答案

**练习 1**：为什么默认的 `PsiMsDaq_RegRead_Standard` 要用 `volatile`，而去掉它可能出什么问题？

> **答案**：寄存器读取是「有副作用」的——每次读都可能改变硬件状态（例如读 IRQVEC 会清中断）。`volatile` 阻止编译器把多次读合并成一次或缓存到寄存器，保证每次访问都真正发给硬件。去掉它，编译器可能优化掉「看似冗余」的读，导致读不到最新值或漏清中断。

**练习 2**：`PsiMsDaq_DataCopy_f` 的 `src`（源地址）是「IP 视角的地址」，`dst`（目标地址）是「CPU 视角的地址」。为什么两者要分开？

> **答案**：DMA 把数据写进 DDR 时用的是 IP 视角的物理地址（可能与 CPU 看到的虚拟地址不同，尤其在带 MMU 的系统上）。拷贝时 `src` 必须按 IP 地址翻译到 CPU 可访问的地址，而 `dst` 是 CPU 自己的缓冲区。把这个翻译留给用户的 `dataCopy` 函数，驱动就保持平台无关。

---

### 4.2 初始化与流句柄获取

#### 4.2.1 概念说明

使用驱动的第一步永远是 `PsiMsDaq_Init`。它做三件事：

1. **分配内存**：用 `malloc` 创建一个 IP 实例结构体和「每流一个」的流实例数组。
2. **注入访问函数**：按 4.1 的机制保存三个函数指针。
3. **把硬件复位到一个干净状态**：禁用所有流、所有中断，清掉最大电平，把所有窗口标记为空闲，最后只打开「全局使能」和「全局中断使能」两个总开关。

拿到 IP 句柄后，第二步是 `PsiMsDaq_GetStrHandle`，把「第 N 路流」的对象取出来——之后所有针对单路的操作（配置、使能、注册回调）都用这个流句柄。

> 重要：`maxStreams` 和 `maxWindows` 这两个参数**必须和 Vivado 里 IP 的生成参数（`Streams_g`、`MaxWindows_g`）一致**，否则驱动算出的窗口地址偏移会错位。函数原型的注释明确写了「must match setting in Vivado IPI」（[driver/psi_ms_daq.h:L311-L312](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L311-L312)）。

#### 4.2.2 核心流程

```text
PsiMsDaq_Init(baseAddr, maxStreams, maxWindows, accessFct)
  │
  ├─ malloc IP 实例 + maxStreams 个流实例
  ├─ 计算 strAddrOffs = 2^ceil(log2(maxWindows)) * 16   ← 窗口地址的「流间距」
  ├─ 注入（或选默认）访问函数
  ├─ 硬件复位：写 GCFG=0, STRENA=0, IRQENA=0, IRQVEC=0xFFFFFFFF
  ├─ for 每流: 清 MAXLVL, for 每窗口: 写 WINCNT=0 (释放), 初始化流结构体
  └─ 写 GCFG = ENA | IRQENA   ← 打开两个总开关（之后再不动）
        │
        ▼ 返回 IpHandle
PsiMsDaq_GetStrHandle(ipHandle, streamNr, &strHndl)
  └─ 校验 streamNr < maxStreams，返回 &streams[streamNr]
```

`strAddrOffs` 这个值很关键：窗口上下文寄存器在地址空间里按「流」分块，每块的步长就是把 `maxWindows` 向上取整到 2 的幂再乘 16 字节。这样硬件可以用低位直接译码窗口号。它的计算在 [driver/psi_ms_daq.c:L143](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L143)：

\[ \text{strAddrOffs} = 2^{\lceil \log_2(\text{maxWindows}) \rceil} \times 16 \]

辅助函数 `Log2` / `Log2Ceil` / `Pow` 见 [driver/psi_ms_daq.c:L99-L125](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L99-L125)。

#### 4.2.3 源码精读

`PsiMsDaq_Init` 的函数原型（返回 IP 句柄）见 [driver/psi_ms_daq.h:L307-L319](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L307-L319)。实现里硬件复位这一段最值得看：

```c
//Disable complete IP (all streams, IRQs, etc.)
PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_GCFG, 0);
PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_STRENA, 0);
PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_IRQENA, 0);
PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_IRQVEC, 0xFFFFFFFF);
//Reset values for all streams
for (int str = 0; str < maxStreams; str++) {
    PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_MAXLVL(str), 0);          // 清最大电平
    for (int win = 0; win < maxWindows; win++) {
        PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_WIN_WINCNT(str, win, inst_p->strAddrOffs), 0); // 释放窗口
    }
    /* ... 初始化流结构体字段 ... */
}
//Set general Enables (never touched later)
PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_GCFG, PSI_MS_DAQ_REG_GCFG_BIT_ENA | PSI_MS_DAQ_REG_GCFG_BIT_IRQENA);
```

见 [driver/psi_ms_daq.c:L155-L179](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L155-L179)。理解两点：

- **`WINCNT=0` 表示窗口空闲**。这是驱动与硬件之间的约定：一个窗口的 `WINCNT`（有效采样数）被写成 0，就等于告诉硬件「这个窗口我没在用，你可以往里写新数据」。后面 4.5 的 `MarkAsFree` 也是干这件事。
- **`IRQVEC=0xFFFFFFFF`** 是「写 1 清零（write-1-to-clear）」语义：向某位写 1 清掉该位中断。全写 1 等于清掉所有挂起的中断，避免初始化时误触发。注释里这行后面的 GCFG 写 `ENA|IRQENA` 标注「never touched later」——全局使能一旦打开就不再关闭，之后用每流 `IRQENA` 和 `STRENA` 做精细控制。

每路流的内部结构体 `PsiMsDaq_StrInst_t`（驱动对外隐藏）记录了这路流的所有软件状态：

```c
typedef struct {
    uint8_t  nr;             // 流号
    bool     isConfigured;   // 是否已配置
    uint8_t  widthBytes;     // 每样本字节数
    uint8_t  windows;        // 本流窗口数
    int8_t   lastProcWin;    // 上次处理到的窗口（中断循环用，见 4.4）
    uint32_t irqCalledWin;   // 位图：哪些窗口已回调过（防重复，见 4.4）
    PsiMsDaqn_WinIrq_f* irqFctWin;  // 窗口回调
    PsiMsDaqn_StrIrq_f* irqFctStr;  // 流回调
    void*    irqArg;
    PsiMsDaq_IpHandle ipHandle;
    uint32_t bufStart, winSize, postTrig;  // 缓存配置，供后续读取用
} PsiMsDaq_StrInst_t;
```

见 [driver/psi_ms_daq.c:L13-L27](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L13-L27)。注意 `lastProcWin` 初始化为 `-1`（[L175](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L175)）、`irqCalledWin` 初始化为 `0`（[L176](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L176)），这两个在中断处理里起决定性作用。

`PsiMsDaq_GetStrHandle` 的实现非常短，本质就是「校验 + 返回内部指针」（[driver/psi_ms_daq.c:L183-L195](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L183-L195)）：

```c
PsiMsDaq_RetCode_t PsiMsDaq_GetStrHandle(PsiMsDaq_IpHandle ipHandle, const uint8_t streamNr, PsiMsDaq_StrHandle* const strHndl_p) {
    PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*) ipHandle;
    SAFE_CALL(CheckStrNr(ipHandle, streamNr));         // streamNr 必须 < maxStreams
    *strHndl_p = (PsiMsDaq_StrHandle) &inst_p->streams[streamNr];
    return PsiMsDaq_RetCode_Success;
}
```

`SAFE_CALL` 是个小宏（[driver/psi_ms_daq.c:L44-L46](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L44-L46)）：调用一个返回 `RetCode_t` 的函数，若不成功就**立刻把错误码向上透传**。整个驱动大量用它做错误短路。

#### 4.2.4 代码实践

**实践目标**：通过阅读 `PsiMsDaq_Init`，画出初始化时寄存器被写的先后顺序，并理解为什么是这个顺序。

**操作步骤**：

1. 打开 [driver/psi_ms_daq.c:L132-L181](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L132-L181)。
2. 同时打开寄存器宏定义 [driver/psi_ms_daq.h:L145-L183](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L145-L183)，把每个 `PSI_MS_DAQ_REG_*` 展开成实际地址。
3. 列一张表：第几步 → 写哪个寄存器（地址）→ 写什么值 → 目的。

**需要观察的现象**：你会发现「先关所有东西、逐窗口释放、最后才打开总使能」这个顺序——这是典型的「安全上电」模式，确保打开使能瞬间硬件处于完全干净的状态，不会有残留的中断或半满窗口。

**预期结果**：例如你能解释为什么 `GCFG = ENA|IRQENA` 必须放在最后写：因为一旦使能，硬件就开始工作了，必须在此之前把所有流和窗口清干净。

> **待本地验证**：寄存器宏展开后的具体十六进制地址，需对照 [driver/psi_ms_daq.h:L147-L182](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L147-L182) 自行计算。

#### 4.2.5 小练习与答案

**练习 1**：如果调用 `PsiMsDaq_Init` 时把 `maxWindows` 传成了实际硬件值的两倍，会有什么后果？

> **答案**：`strAddrOffs` 会算错（因为它依赖 `maxWindows`），导致 `WIN_WINCNT(str, win, strAddrOffs)` 计算出的窗口寄存器地址与硬件实际布局不符。初始化时写的「释放窗口」会写错地方，后续 `GetDataUnwrapped` 读窗口计数也会读错地址，表现为数据错乱或读到全 0。

**练习 2**：`PsiMsDaq_Init` 为什么最后才写 `GCFG = ENA|IRQENA`，而不是一开始就写？

> **答案**：使能位一打开，硬件就开始接受数据并可能产生中断。如果先使能、再逐个清窗口，存在「硬件已经在写某个窗口、而软件又把它标记为空闲」的竞态。先复位干净、最后使能，保证使能瞬间所有流禁用、所有窗口空闲、无挂起中断，状态一致。

---

### 4.3 流配置 `PsiMsDaq_StrConfig_t` 与 `Str_Configure`

#### 4.3.1 概念说明

`PsiMsDaq_Str_Configure` 是配置一路流的核心函数。它把一个 `PsiMsDaq_StrConfig_t` 结构体里的 8 个字段翻译成一连串寄存器写操作。先看这个结构体（[driver/psi_ms_daq.h:L262-L274](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L262-L274)）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `postTrigSamples` | `uint32_t` | 触发后要记录的样本数（**含触发样本本身**）。决定一帧记录何时结束。 |
| `recMode` | `PsiMsDaq_RecMode_t` | 记录模式：Continuous / TriggerMask / SingleShot / Manual（见下表）。 |
| `winAsRingbuf` | `bool` | `true`=窗口当环形缓冲用（满了回绕覆盖最旧）；`false`=线性（写满即停/切窗）。 |
| `winOverwrite` | `bool` | `true`=即使窗口数据没被软件确认也允许覆盖；`false`=必须等软件 `MarkAsFree` 才能覆盖。 |
| `winCnt` | `uint8_t` | 本流使用的窗口个数。 |
| `bufStartAddr` | `uint32_t` | 本流缓冲区在 DDR 中的起始地址。 |
| `winSize` | `uint32_t` | 每个窗口的大小（**字节**）。 |
| `streamWidthBits` | `uint16_t` | 流的位宽（**比特**，必须是 8 的倍数，如 8/16/32/64）。 |

四种记录模式（枚举见 [driver/psi_ms_daq.h:L252-L260](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L252-L260)）：

| 模式 | 值 | 行为 |
|------|----|------|
| `Continuous` | 0 | 一直记录，不停。 |
| `TriggerMask` | 1 | 持续记录预触发数据，但只有 `Arm` 之后才检测触发；每次触发记录一帧后继续等下一次。 |
| `SingleShot` | 2 | `Arm` 后才记录预触发数据，检测到一个触发就记录该帧并**停止**。 |
| `Manual` | 3 | 完全由软件置/清 Arm 位手动控制起停。 |

> 关于触发、Arm、后触发的硬件细节是 u2-l3 的主题，本讲只需知道：`recMode` + `postTrigSamples` 决定了「记录什么、记录多久」。

#### 4.3.2 核心流程

`Str_Configure` 的执行流程：

```text
PsiMsDaq_Str_Configure(strHndl, &cfg)
  │
  ├─ 校验：
  │    ├─ streamWidthBits 必须是 8 的倍数  → 否则 IllegalStrWidth
  │    ├─ winCnt 不能超过硬件 maxWindows   → 否则 IllegalWinCnt
  │    ├─ winSize 必须是单样本字节的整数倍 → 否则 WinSizeMustBeMultipleOfSamples
  │    └─ 该流当前必须处于禁用状态         → 否则 StrNotDisabled
  ├─ 写硬件寄存器：
  │    ├─ POSTTRIG(strNr) = postTrigSamples
  │    ├─ MODE[strNr].RecMode 字段 = recMode
  │    ├─ SCFG.RINGBUF 位   = winAsRingbuf
  │    ├─ SCFG.OVERWRITE 位 = winOverwrite
  │    ├─ BUFSTART(strNr)   = bufStartAddr
  │    ├─ WINSIZE(strNr)    = winSize
  │    └─ SCFG.WINCNT 字段  = winCnt - 1     ← 注意减 1
  └─ 缓存到流结构体（widthBytes/windows/bufStart/postTrig/winSize）供后续读取用
```

**为什么 `WINCNT` 要写 `winCnt - 1`？** 因为硬件字段从 0 开始计数：写 0 表示「1 个窗口」，写 `winCnt-1` 表示「`winCnt` 个窗口」。这是硬件寄存器的常见约定。

#### 4.3.3 源码精读

函数原型与「只允许在禁用时配置」的说明见 [driver/psi_ms_daq.h:L343-L353](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L343-L353)。实现里校验段（[driver/psi_ms_daq.c:L273-L282](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L273-L282)）：

```c
if (0 != (config_p->streamWidthBits % 8)) return PsiMsDaq_RetCode_IllegalStrWidth;
if (config_p->winCnt > ipInst_p->maxWindows) return PsiMsDaq_RetCode_IllegalWinCnt;
if (0 != (config_p->winSize % (config_p->streamWidthBits/8))) return PsiMsDaq_RetCode_WinSizeMustBeMultipleOfSamples;
SAFE_CALL(CheckStrDisabled(ipHandle, strNr));   // 读 STRENA，确认本流未使能
```

`CheckStrDisabled` 读 `STRENA` 寄存器，看本流那一位是否为 1（[driver/psi_ms_daq.c:L68-L77](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L68-L77)）。这是「**配置前必须先禁用**」约定的代码体现。

寄存器写入段（[driver/psi_ms_daq.c:L284-L310](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L284-L310)）展示了三种不同的写法，值得对比：

```c
// ① 整寄存器写：直接写一个 32 位值
PsiMsDaq_RegWrite(ipHandle, PSI_MS_DAQ_REG_POSTTRIG(strNr), config_p->postTrigSamples);
// ② 字段写（读改写，RMW）：只改 RecMode 这两位，保留其他位
PsiMsDaq_RegSetField(ipHandle, PSI_MS_DAQ_REG_MODE(strNr), MODE_LSB_RECM, MODE_MSB_RECM, config_p->recMode);
// ③ 单位写：只改 RINGBUF 这一位
PsiMsDaq_RegSetBit(ipHandle, PSI_MS_DAQ_CTX_SCFG(strNr), PSI_MS_DAQ_CTX_SCFG_BIT_RINGBUF, config_p->winAsRingbuf);
// 窗口数写成 winCnt-1
PsiMsDaq_RegSetField(ipHandle, PSI_MS_DAQ_CTX_SCFG(strNr), WINCNT_LSB, WINCNT_MSB, config_p->winCnt - 1);
```

- `RegWrite`：整写，适合独占一个寄存器（如 `POSTTRIG`、`BUFSTART`、`WINSIZE`）。
- `RegSetField`：读改写一个多位字段，见 [driver/psi_ms_daq.c:L754-L771](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L754-L771)。它先读回旧值，清掉目标位段，再或上新值。**注意它不是原子的**——「读、改、写」之间若发生中断、中断里又改了同一寄存器，会丢更新。这也是头文件 [driver/psi_ms_daq.h:L25-L36](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L25-L36) 强调「驱动非线程安全」的原因。
- `RegSetBit`：单位特例，见 [driver/psi_ms_daq.c:L790-L805](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L790-L805)。

最后把配置缓存进流结构体（[driver/psi_ms_daq.c:L312-L317](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L312-L317)）：

```c
inst_p->widthBytes = config_p->streamWidthBits/8;  // 后续 GetDataUnwrapped 算字节用
inst_p->isConfigured = true;
inst_p->windows = config_p->winCnt;
inst_p->bufStart = config_p->bufStartAddr;
inst_p->postTrig = config_p->postTrigSamples;
inst_p->winSize = config_p->winSize;
```

这些缓存值在 4.5 读数据时直接用，免得每次都去读硬件寄存器。

#### 4.3.4 代码实践

**实践目标**：把 `PsiMsDaq_StrConfig_t` 的每个字段对应到它最终写入的寄存器位，理解「配置 = 一组寄存器写」。

**操作步骤**：

1. 准备一个具体场景：1 路 16 位流，DDR 缓冲起始 `0x10000000`，4 个窗口，每窗口 4096 字节，连续记录模式，环形缓冲，不允许覆盖，后触发 1000 个样本。
2. 打开 [driver/psi_ms_daq.c:L264-L320](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L264-L320) 与寄存器宏 [driver/psi_ms_daq.h:L154-L182](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L154-L182)。
3. 填下面这张「配置 → 寄存器」映射表。

**示例代码**（基于真实 API，参数按上面场景填）：

```c
/* 示例代码：演示一次完整的流配置 */
PsiMsDaq_StrConfig_t cfg = {
    .postTrigSamples = 1000,                          // 触发后 1000 样本（含触发样本）
    .recMode        = PsiMsDaqn_RecMode_Continuous,   // 连续记录
    .winAsRingbuf   = true,                           // 环形缓冲
    .winOverwrite   = false,                          // 不覆盖未确认窗口
    .winCnt         = 4,                              // 4 个窗口
    .bufStartAddr   = 0x10000000,                     // DDR 起始
    .winSize        = 4096,                           // 每窗口 4096 字节
    .streamWidthBits = 16,                            // 16 位/样本
};
PsiMsDaq_Str_Configure(daqStrHandle, &cfg);
```

**需要观察的现象 / 预期映射表**：

| 配置字段 | 写入的寄存器宏（n=0） | 写入值 | 寄存器地址 |
|----------|----------------------|--------|-----------|
| `postTrigSamples` | `PSI_MS_DAQ_REG_POSTTRIG(0)` | `1000` | `0x204` |
| `recMode` | `MODE(0)` 的 RecMode 字段 [0:1] | `0`（Continuous） | `0x208` |
| `winAsRingbuf` | `SCFG(0)` 的 RINGBUF 位 bit0 | `1` | `0x1000` |
| `winOverwrite` | `SCFG(0)` 的 OVERWRITE 位 bit8 | `0` | `0x1000` |
| `bufStartAddr` | `PSI_MS_DAQ_CTX_BUFSTART(0)` | `0x10000000` | `0x1004` |
| `winSize` | `PSI_MS_DAQ_CTX_WINSIZE(0)` | `4096` | `0x1008` |
| `winCnt` | `SCFG(0)` 的 WINCNT 字段 [16:20] | `4-1 = 3` | `0x1000` |

> **待本地验证**：上表地址由宏 `0x200+0x10*n`、`0x1000+0x20*n` 等公式算出，可在 [driver/psi_ms_daq.h:L155-L174](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L155-L174) 逐一核对。

#### 4.3.5 小练习与答案

**练习 1**：若 `winSize = 4097`、`streamWidthBits = 16`，`Str_Configure` 会返回什么错误？为什么？

> **答案**：返回 `PsiMsDaq_RetCode_WinSizeMustBeMultipleOfSamples`（-10）。因为 16 位 = 2 字节/样本，`4097 % 2 = 1 ≠ 0`，窗口大小不是样本的整数倍，硬件无法在一个窗口里整齐放下整数个样本。这个校验在 [driver/psi_ms_daq.c:L279-L281](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L279-L281)。

**练习 2**：为什么配置函数要求流必须先禁用（`CheckStrDisabled`）？如果没禁用就配置会怎样？

> **答案**：流使能时硬件正在按旧配置采集和写窗口，此时改寄存器（尤其 `winSize`、`bufStart`、`WINCNT`）会让硬件用到「一半新一半旧」的参数，导致写错地址或窗口计数混乱。所以驱动强制要求配置前禁用，返回 `StrNotDisabled`（-3）拒绝执行。

---

### 4.4 中断处理 `HandleIrq` 与窗口回调两种方案

#### 4.4.1 概念说明

硬件写满一个窗口（或一次触发记录完成）后，会拉高电平中断 `Irq`。CPU 进入中断服务程序（ISR）后，**必须调用 `PsiMsDaq_HandleIrq(ipHandle)`**——这是驱动与硬件中断的唯一入口（头文件 [driver/psi_ms_daq.h:L47-L48](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L47-L48) 明确了这个约定）。

驱动支持**两套互斥的中断处理方案**（详细说明见 [driver/psi_ms_daq.h:L38-L70](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L38-L70)）：

| 方案 | 注册函数 | 回调签名 | 适用场景 |
|------|----------|----------|----------|
| **窗口方案（Window based）** | `Str_SetIrqCallbackWin` | 每完成一个窗口调用**一次**，带 `WinInfo_t` | `winOverwrite=false`（每个窗口都要软件确认）的常规场景。**简单、推荐**。 |
| **流方案（Stream based）** | `Str_SetIrqCallbackStr` | 每次 IRQ 调用一次，只给流句柄 | `winOverwrite=true` 的特殊场景，用户自己决定怎么处理。 |

两者**不能对同一流同时使用**，否则注册第二个时会返回 `IrqSchemesWinAndStrAreExclusive`（-11），见 [driver/psi_ms_daq.c:L343-L345](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L343-L345)。

本讲重点讲**窗口方案**，因为它最常用、也最能体现驱动「替你管窗口」的设计。

#### 4.4.2 核心流程

`HandleIrq` 的窗口方案核心逻辑（伪代码）：

```text
PsiMsDaq_HandleIrq(ipHandle):
  strWithIrq = 读 IRQVEC              # 哪些流有中断
  写 IRQVEC = strWithIrq              # 写 1 清掉这些中断位
  for str in 0..maxStreams:
      if strWithIrq 第 str 位 == 0: continue
      if 注册了流回调 irqFctStr:  调用它（用户自己处理）
      if 注册了窗口回调 irqFctWin:
          lastWin = 读 LASTWIN(str)   # 硬件最新写完的窗口号
          win = lastProcWin            # 从上次处理到的窗口开始
          do:
              win = (win + 1) % windows        # 下一个窗口
              if irqCalledWin 的 win 位已置: break   # 这个窗口之前回调过、还没释放 → 停
              irqCalledWin |= (1 << win)       # 标记已回调
              构造 WinInfo_t{ipHandle, strHandle, win}
              调用用户回调 irqFctWin(winInfo, arg)
              lastProcWin = win
          while (win != lastWin)               # 直到追上硬件最新窗口
```

两个关键字段的作用：

- **`lastProcWin`**：记录「上次回调到哪个窗口」。下一次中断从这里 +1 开始往前追，避免重复回调旧窗口。
- **`irqCalledWin`**（位图）：记录「哪些窗口已回调过但用户还没 `MarkAsFree`」。一旦某窗口的位已置，循环就 `break`——**保证每个窗口的回调只发生一次**，即使中断来得比用户处理还快也不会重复回调。

这两个机制合起来实现了头文件承诺的「driver ensures that the user callback gets called exactly once for every window」（[driver/psi_ms_daq.h:L54](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L54)）。具体的「防丢、防重」细节是 u4-l2 的主题，本讲先建立直觉。

#### 4.4.3 源码精读

`HandleIrq` 的函数原型只有一个参数（[driver/psi_ms_daq.h:L335](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L335)），完整实现见 [driver/psi_ms_daq.c:L197-L258](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L197-L258)。读中断与清中断：

```c
uint32_t strWithIrq;
PsiMsDaq_RegRead(ipHandle, PSI_MS_DAQ_REG_IRQVEC, &strWithIrq);   // 哪些流有中断
PsiMsDaq_RegWrite(ipHandle, PSI_MS_DAQ_REG_IRQVEC, strWithIrq);    // 写回原值 = 清这些位
```

见 [driver/psi_ms_daq.c:L203-L205](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L203-L205)。这是「读出哪几位为 1，再写回 1 清零」的标准 write-1-to-clear 用法。

流方案的分支很短（[driver/psi_ms_daq.c:L218-L221](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L218-L221)）：直接调一次用户回调就完事，驱动不插手窗口。窗口方案的核心循环（[driver/psi_ms_daq.c:L225-L253](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L225-L253)）：

```c
uint8_t lastWin;
PsiMsDaq_Str_GetLastWrittenWin(strHandle, &lastWin);   // 硬件最新写完的窗口
int8_t win = str_p->lastProcWin;                        // 从上次处理到的位置开始
do {
    PsiMsDaq_RegWrite(ipHandle, PSI_MS_DAQ_REG_IRQVEC, (1 << str));  // 清本流中断
    PsiMsDaq_Str_GetLastWrittenWin(strHandle, &lastWin);             // 重读（防止竞态）
    win = (win + 1) % str_p->windows;                                // 下一个窗口
    if (str_p->irqCalledWin & (1 << win)) { break; }                 // 已回调未释放 → 停
    str_p->irqCalledWin |= (1 << win);                               // 标记已回调
    PsiMsDaq_WinInfo_t winInfo = { .ipHandle=ipHandle, .strHandle=strHandle, .winNr=win };
    if (str_p->irqFctWin != NULL) { str_p->irqFctWin(winInfo, str_p->irqArg); }
    str_p->lastProcWin = win;                                        // 推进游标
} while (win != lastWin);                                            // 追上硬件为止
```

注意循环里**每次都重读 `lastWin` 并清一次本流 IRQ**（[driver/psi_ms_daq.c:L233-L235](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L233-L235)）：因为回调执行期间硬件可能又写完了下一个窗口，重读保证不漏。

回调函数类型定义见 [driver/psi_ms_daq.h:L230-L239](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L230-L239)（窗口回调 `PsiMsDaqn_WinIrq_f`）和 [L241-L250](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L241-L250)（流回调 `PsiMsDaqn_StrIrq_f`）。注意一个细节：注释特意提醒 `WinInfo_t` 是**分配在栈上的**（[driver/psi_ms_daq.h:L221-L222](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L221-L222)），所以回调返回后它就失效了——**不能把 `WinInfo_t` 存起来以后再用**，必须在回调内完成所有处理。

注册窗口回调的函数原型见 [driver/psi_ms_daq.h:L365-L380](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L365-L380)，实现见 [driver/psi_ms_daq.c:L336-L351](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L336-L351)（就是存指针 + 互斥校验）。使能中断 `Str_SetIrqEnable` 见 [driver/psi_ms_daq.c:L370-L382](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L370-L382)，它写 `IRQENA` 寄存器对应位。

#### 4.4.4 代码实践

**实践目标**：用一个具体的多窗口场景，手工追踪 `lastProcWin` 和 `irqCalledWin` 的变化，理解「每窗口恰好回调一次」是怎么做到的。

**操作步骤**：

1. 场景：某流 `windows = 4`，初始 `lastProcWin = -1`，`irqCalledWin = 0`。
2. 假设硬件连续写完窗口 0、1（`LASTWIN` 读到 1），但用户回调很慢。第一次 `HandleIrq` 被调用。
3. 打开 [driver/psi_ms_daq.c:L225-L253](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L225-L253)，逐步填表。
4. 再假设：用户在窗口 0 回调里**没有**调用 `MarkAsFree`（即没清 `irqCalledWin` 的位 0）。紧接着第二次 `HandleIrq` 到来，`LASTWIN` 仍为 1。追踪这次会怎样。

**需要观察的现象 / 预期结果**（用一张表追踪）：

| 时刻 | `lastProcWin` | `irqCalledWin` | 动作 |
|------|---------------|----------------|------|
| 初始 | -1 | `0000` | — |
| 第 1 次 IRQ，迭代 1 | 0 | `0001` | win=(−1+1)%4=0，位 0 未置 → 置位、回调窗口 0 |
| 第 1 次 IRQ，迭代 2 | 1 | `0011` | win=1，位 1 未置 → 置位、回调窗口 1，win==lastWin(1) 退出 |
| 第 2 次 IRQ，迭代 1 | — | — | win=(1+1)%4=2，但 `LASTWIN` 仍是 1…… |

**关键观察**：第 2 次 IRQ 时，循环从 `lastProcWin=1` 开始，`win` 先变成 2，但 `2 != lastWin(1)` 不会立刻退出——它会把 win 推进到 2、3、0、1……不过注意：循环条件 `win != lastWin` 在 `lastWin=1`、从 `win=2` 出发时，第一次 `win` 变 2 就 `!=1`，会检查 `irqCalledWin` 位 2（未置）→ 置位并回调窗口 2。**这其实会「超前」回调尚未被硬件报告的窗口**——这正是为什么实际使用中 `winOverwrite` 必须为 `false`：硬件保证不会写一个还没被 `MarkAsFree` 的窗口，所以超前回调到的窗口里数据一定是完整的。

> ⚠️ 上面这个「超前」追踪较微妙，**结论与边界条件请以 u4-l2 为准**，本讲只要求你理解「`irqCalledWin` 位图 + `lastProcWin` 游标」两个机制的存在与作用。**待本地验证**：可在模拟环境里实际跑一遍确认。

#### 4.4.5 小练习与答案

**练习 1**：`HandleIrq` 开头为什么要「读 IRQVEC 再写回原值」？

> **答案**：IRQVEC 是 write-1-to-clear 寄存器。读出哪些位为 1（=哪些流有中断），把原值写回去就清掉了这些位，防止退出 ISR 后中断仍挂着、立刻再次进入。见 [driver/psi_ms_daq.c:L204-L205](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L204-L205)。

**练习 2**：为什么 `WinInfo_t` 被明确标注「栈上分配、返回即失效」？如果用户把回调收到的 `WinInfo_t` 存到全局变量里延后处理，会出什么问题？

> **答案**：`HandleIrq` 循环里每个窗口都用同一个栈变量 `winInfo` 装填（[driver/psi_ms_daq.c:L244-L247](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L244-L247)），下一轮就被覆盖；函数返回后栈帧回收。存到全局延后用，读到的 `winNr` 等字段是错的或被覆盖的值。正确做法是在回调**内**完成 `GetDataUnwrapped` 等所有操作。

---

### 4.5 数据读取与窗口释放

#### 4.5.1 概念说明

中断回调里拿到 `WinInfo_t` 后，典型动作是两步：

1. **`PsiMsDaq_StrWin_GetDataUnwrapped`**：把该窗口里指定「前/后触发样本数」的数据，**解环形**地拷贝到用户缓冲区。
2. **`PsiMsDaq_StrWin_MarkAsFree`**：把这个窗口标记为空闲，告诉硬件「我处理完了，可以覆盖」。

为什么需要「解环形（unwrap）」？因为窗口可配置成环形缓冲（`winAsRingbuf=true`）：触发可能发生在窗口中间，记录会绕着窗口写一圈。所以「最后一个样本」可能在窗口地址的高端，而它前面的样本（前触发数据）反而回绕到了窗口低端。要把数据按时间顺序读成一段连续数组，驱动需要做**两段拼接**。

本讲只讲用法和直觉，「环形解包的地址数学」是 u4-l3 的主题。

#### 4.5.2 核心流程

```text
回调内：
  ┌─ GetDataUnwrapped(winInfo, preTrigSamples, postTrigSamples, buffer, bufferSize)
  │     ├─ samples = pre + post;  bytes = samples * widthBytes
  │     ├─ 校验: bufferSize 够不够、post 不超配置、pre 不超可用
  │     ├─ 算窗口起止地址 winStart/winLast
  │     ├─ 读末样地址 lastSplAddr → 推出触发字节、末字节地址（含环形回绕修正）
  │     ├─ 若整段不跨窗口边界: 一次 memcpy（快路径）
  │     └─ 否则: 两段 memcpy 拼接（环形回绕路径）
  └─ MarkAsFree(winInfo)
        ├─ irqCalledWin 清掉本窗口位（允许下次再回调）
        └─ 写 WINCNT=0（告诉硬件：窗口空闲，可覆盖）
```

#### 4.5.3 源码精读

`GetDataUnwrapped` 原型见 [driver/psi_ms_daq.h:L540-L556](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L540-L556)，实现见 [driver/psi_ms_daq.c:L579-L639](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L579-L639)。校验段（[driver/psi_ms_daq.c:L596-L604](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L596-L604)）给出三种典型错误码，对应头文件 [driver/psi_ms_daq.h:L288-L301](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L288-L301)：

```c
if (bufferSize < bytes) return PsiMsDaq_RetCode_BufferTooSmall;             // -7
if (postTrigSamples > str_p->postTrig) return ...MorePostTrigThanConfigured; // -8
if (preTrigSamples > preTrig) return ...MorePreTrigThanAvailable;            // -9
```

拷贝用的是注入的 `memcpyFct`（不是标准 `memcpy`），见快路径 [driver/psi_ms_daq.c:L624-L627](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L624-L627) 与两段拼接路径 [L629-L635](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L629-L635)：

```c
// 快路径：整段不跨边界，一次拷贝
const int64_t firstByteLinear = (int64_t)lastByteAddr - bytes + 1;
if (firstByteLinear >= winStart) {
    ip_p->memcpyFct(buffer_p, (void*)(size_t)firstByteLinear, bytes);
}
// 回绕路径：两段拼接
else {
    const uint32_t secondChunkSize = lastByteAddr - winStart + 1;
    const uint32_t firstChunkSize  = bytes - secondChunkSize;
    const int64_t  firstChunkStartAddr = winLast - firstChunkSize + 1;
    ip_p->memcpyFct(buffer_p, (void*)(size_t)firstChunkStartAddr, firstChunkSize);
    ip_p->memcpyFct((void*)((uint32_t)buffer_p + firstChunkSize), (void*)(size_t)winStart, secondChunkSize);
}
```

注意一个**重要的头文件约定**（[driver/psi_ms_daq.h:L549-L551](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L549-L551)）：`GetDataUnwrapped` **不会**自动确认窗口。必须再调 `MarkAsFree` 才释放。这把「读数据」和「释放窗口」解耦，允许同一窗口被读多次。

`MarkAsFree` 原型见 [driver/psi_ms_daq.h:L558-L564](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L558-L564)，实现 [driver/psi_ms_daq.c:L641-L653](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L641-L653)，两件事：

```c
str_p->irqCalledWin &= ~(1 << winInfo.winNr);   // 清位图，允许下次再回调本窗口
PsiMsDaq_RegWrite(winInfo.ipHandle, PSI_MS_DAQ_WIN_WINCNT(strNr, winInfo.winNr, ip_p->strAddrOffs), 0);  // WINCNT=0 → 空闲
```

`WINCNT=0` 就等于「窗口空闲」。这和 4.2.3 里 `PsiMsDaq_Init` 释放所有窗口用的是**同一个寄存器、同一个约定**——首尾呼应。

#### 4.5.4 代码实践

**实践目标**：写一个完整的最小用户回调，把 cache invalidate、读数据、释放串起来。这是头文件示例的简化版。

**操作步骤**：

1. 读头文件示例代码 [driver/psi_ms_daq.h:L76-L131](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L76-L131)，重点看 `UserDaqIsr` 部分（[L92-L100](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L92-L100)）。
2. 注意示例里 `Xil_DCacheInvalidateRange` 是 Xilinx 平台特有的 cache 操作——因为 DMA 写了 DDR，CPU cache 里可能是旧数据，必须先 invalidate。
3. 自己写一个不依赖具体平台的回调骨架。

**示例代码**（基于头文件示例 L92-L100 简化，省略平台相关宏）：

```c
/* 示例代码：最小用户窗口回调 */
static uint8_t g_buffer[8192];   /* 用户接收缓冲，需足够大 */

void UserDaqIsr(PsiMsDaq_WinInfo_t winInfo, void* arg) {
    /* 1. 算本次要读的前/后触发样本数 */
    uint32_t preTrig, postTrig = 1000;   /* postTrig 与配置一致 */
    PsiMsDaq_StrWin_GetPreTrigSamples(winInfo, &preTrig);

    /* 2. 平台相关：让 CPU cache 丢弃该窗口所在 DDR 区域的旧副本
          （Xilinx 用 Xil_DCacheInvalidateRange；其他平台用对应 API） */

    /* 3. 把窗口数据按时间顺序解环形拷到 g_buffer */
    PsiMsDaq_RetCode_t r = PsiMsDaq_StrWin_GetDataUnwrapped(
        winInfo, preTrig, postTrig, g_buffer, sizeof(g_buffer));
    if (r != PsiMsDaq_RetCode_Success) {
        /* 处理错误：buffer 太小、pre/post 越界等 */
        return;
    }

    /* 4. 处理数据（这里只是占位） */
    /* ... 使用 g_buffer 里的 preTrig+postTrig 个样本 ... */

    /* 5. 释放窗口，允许硬件覆盖 */
    PsiMsDaq_StrWin_MarkAsFree(winInfo);
}
```

**需要观察的现象**：如果漏掉第 5 步 `MarkAsFree`，且 `winOverwrite=false`，那么所有窗口最终都会被写满且不被释放，`HandleIrq` 里的 `irqCalledWin` 位全置 1，循环一进去就 `break`，回调再也不会被调用——**数据流停住**。这就是 `winOverwrite=false` 模式下「必须 MarkAsFree」的强制性的体现。

**预期结果**：正确流程下，每个窗口被回调一次、读一次、释放一次，硬件得以持续写入新窗口。

> **待本地验证**：cache invalidate 的具体调用因平台而异；`postTrig` 应与 `Str_Configure` 时设的 `postTrigSamples` 一致，否则 `GetDataUnwrapped` 返回 `MorePostTrigThanConfigured`。

#### 4.5.5 小练习与答案

**练习 1**：`GetDataUnwrapped` 为什么要区分「快路径（一次拷贝）」和「回绕路径（两段拷贝）」？

> **答案**：环形缓冲窗口里，触发点可能让数据绕窗口边界写了一圈。如果请求的数据段整体落在窗口的一段连续地址内（不跨边界），一次 `memcpy` 最快；如果跨越了窗口末尾→开头的回绕点，就必须拆成「窗口尾部一段 + 窗口头部一段」两段拷贝，再拼到用户缓冲里，才能得到时间顺序正确的连续数据。判定依据是 `firstByteLinear >= winStart`（[driver/psi_ms_daq.c:L624-L625](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L624-L625)）。

**练习 2**：如果不调用 `MarkAsFree`，在 `winOverwrite=false` 时系统会怎样？在 `winOverwrite=true` 时又会怎样？

> **答案**：`winOverwrite=false`：窗口不会被释放，`irqCalledWin` 位图逐渐填满，`HandleIrq` 循环一进去就 break，回调停止，硬件因所有窗口都被占用而无法写入新数据，采集停顿。`winOverwrite=true`：硬件忽略「未释放」状态直接覆盖旧窗口，数据流不会停，但会丢失未被及时处理的旧窗口数据——这正是 `winOverwrite` 两种语义的本质区别。

---

## 5. 综合实践

把本讲 5 个模块串起来，完成下面这个**端到端最小工程**（纯源码阅读 + 伪代码编写，无需硬件）。

**场景**：一块 Zynq SoC，FPGA 侧实例化了 `psi_ms_daq_axi`，参数 `Streams_g=2`、`MaxWindows_g=8`。流 0 是 16 位 ADC，连续记录模式，DDR 缓冲 `0x10000000`，4 个窗口各 8 KiB，环形缓冲，不允许覆盖，后触发 2000 样本。

**任务**：

1. **写出完整的初始化 + 配置 + 中断注册序列**（参照头文件示例 [driver/psi_ms_daq.h:L102-L130](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L102-L130)）。要求：
   - `PsiMsDaq_Init` 的参数与 `Streams_g`/`MaxWindows_g` 匹配。
   - 填一个完整的 `PsiMsDaq_StrConfig_t`。
   - 注册**窗口方案**回调并使能中断、使能流。
2. **写出系统 ISR**（[driver/psi_ms_daq.h:L82-L89](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L82-L89)）和**用户回调**（[L92-L100](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L92-L100)）。
3. **画一张数据流时序图**（文字版即可）：硬件写满窗口 → 拉 IRQ → CPU 进 ISR → `HandleIrq` → 用户回调 → `GetDataUnwrapped` → `MarkAsFree` → 窗口空闲 → 硬件写下一个窗口。
4. **回答**：如果用户回调处理太慢，4 个窗口全被写满且都没 `MarkAsFree`，第 5 次想写窗口时会发生什么？（用 4.4、4.5 的机制解释）

**参考要点（自行对照）**：

- 初始化参数必须是 `PsiMsDaq_Init(baseAddr, 2, 8, NULL)`——`maxWindows=8` 对应硬件 `MaxWindows_g`，**不是**配置里的 `winCnt=4`（`winCnt` 只是本流实际启用的窗口数，受 `maxWindows` 上限约束）。
- `winSize = 8192` 字节，`streamWidthBits = 16`，`winSize % (16/8) == 0` 通过校验。
- ISR 里必须调 `PsiMsDaq_HandleIrq(ipHandle)`，回调里必须 `GetDataUnwrapped` 后 `MarkAsFree`。
- 第 4 问：`irqCalledWin` 全置位 → `HandleIrq` 循环 break → 不再回调；硬件侧因 `winOverwrite=false`，所有窗口都被「占用」，状态机的窗口保护逻辑（u4-l5）会阻止继续写入，数据积压在输入 FIFO 直至溢出。

> 这个综合实践覆盖了本讲全部 5 个最小模块。如果你能独立完成，说明你已经掌握了驱动的基本用法。

---

## 6. 本讲小结

- **驱动的角色**：运行在 CPU 上的 C 库，通过 AXI Slave 寄存器配置 IP、通过内存拷贝读取 DMA 写入 DDR 的窗口数据，是「软件使用硬件」的桥梁。
- **访问函数注入**：`PsiMsDaq_AccessFct_t` 把「读/写寄存器、拷贝数据」抽象成三个可注入函数指针，使驱动平台无关、可测试。所有寄存器访问最终经 `PsiMsDaq_RegWrite/RegRead`（`baseAddr+addr`）出口。
- **典型调用序列**：`Init` → `GetStrHandle` → `Str_Configure` → `Str_SetIrqCallbackWin` → `Str_SetIrqEnable` → `Str_SetEnable`，回调内 `GetDataUnwrapped` + `MarkAsFree`。
- **配置即寄存器写**：`PsiMsDaq_StrConfig_t` 八个字段被翻译成 `POSTTRIG`、`MODE`、`SCFG`、`BUFSTART`、`WINSIZE` 等寄存器写；`WINCNT` 字段写 `winCnt-1`；配置前流必须禁用。
- **两种中断方案**：窗口方案（每窗口回调一次，需 `winOverwrite=false`，推荐）与流方案（每次 IRQ 回调一次，特殊场景），二者对同一流互斥。
- **窗口释放约定**：`MarkAsFree` 清 `irqCalledWin` 位并把 `WINCNT` 写 0；`WINCNT=0` 即「窗口空闲」是驱动与硬件的核心约定，贯穿初始化与释放。

## 7. 下一步学习建议

本讲让你「会用」驱动，但很多机制只是建立了直觉。建议接下来的学习路径：

- **进入 u2 单元（数据通路）**：理解 `postTrigSamples`、`recMode`、Arm、触发这些配置在**硬件侧**是如何实现的——看 `hdl/psi_ms_daq_input.vhd`（u2-l2、u2-l3）。这会让你明白「为什么配置要这些字段」。
- **u3 单元（控制状态机）**：理解窗口、环形缓冲、`WINCNT`、`bufStart`/`winSize` 这些在硬件寄存器接口 `hdl/psi_ms_daq_reg_axi.vhd` 里的真实布局（u3-l4、u3-l5），把驱动的寄存器宏与硬件地址译码对上号。
- **u4 单元（专家层）**：当你需要深入时，u4-l2 讲中断去抖的完整细节（`irqCalledWin`/`lastProcWin` 的边界），u4-l3 讲环形解包的地址数学（`GetDataUnwrapped` 两段拼接的完整推导）。
- **动手实验建议**：结合 u5 单元的 testbench（尤其顶层 `psi_ms_daq_axi_tb`），可以看到「软件写寄存器 → 硬件采集 → 内存校验」的端到端参考实现，是验证你对驱动用法理解的最佳参照。

> 阅读源码顺序推荐：先把本讲的 [driver/psi_ms_daq.h](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h) 顶部 `@mainpage` 示例（L16-L132）完整读一遍，再带着示例里的疑问去翻 `.c` 的实现，这样最高效。
