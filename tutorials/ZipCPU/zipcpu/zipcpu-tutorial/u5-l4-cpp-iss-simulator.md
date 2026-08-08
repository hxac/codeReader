# C++ 指令级模拟器（ISS）

> 讲义 id：u5-l4 · 依赖：u1-l4（跑起来：模拟器与第一个程序）
> 关键源码：`sim/cpp/zsim.cpp`、`sim/cpp/zipelf.cpp`、`sim/cpp/twoc.cpp`

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「指令级模拟器（ISS）」和「基于 RTL 的 Verilator 模拟器」到底差在哪里——一个仿真**指令的语义**，一个仿真**硬件的时序**。
- 读懂 `sim/cpp` 下三个源文件如何拼出一台能跑 ELF 的「软件 ZipCPU」：`zipelf.cpp` 把程序装进内存、`zsim.cpp` 里的 `SIMBUS`/`SIMDEV` 搭出地址空间与设备、`ZIPMACHINE` 逐条解释指令。
- 跟踪一条指令从取指、译码、执行到写回 PC 与条件码（CC）的全过程，并指出 ISS 在哪一步「偷懒」省掉了真实硬件的流水线/缓存/总线时序。

## 2. 前置知识

本讲假设你已经读过 u1-l4，知道仓库里有**两套模拟器**：

| | C++ ISS（`sim/cpp`） | Verilator 模拟器（`sim/verilator`） |
|---|---|---|
| 仿真对象 | 指令的**语义**（一条指令做成什么事） | 真实 **RTL** 电路，逐时钟周期 |
| 速度 | 快（一个 C++ `switch` 一条指令） | 慢（每个时钟都要算全流水线） |
| 能抓的 bug | ISA 语义对不对 | 流水线冒险、缓存命中、总线时序对不对 |
| 代码量 | 三个 `.cpp`，约 1100 行 | 链接 Verilator 生成的 C++ 模型 |

几个本讲会用到的概念：

- **ISS（Instruction Set Simulator，指令集模拟器）**：不模拟硬件，只模拟「拿到一条指令该产出什么结果」。本质是一个把指令字翻译成 C++ 动作的大 `switch`。
- **ELF**：可执行文件格式。本讲的 ISS 不认识汇编，它直接吃 GCC/汇编器产出的 32 位 ZipCPU ELF。
- **load/store 架构**、**双寄存器组**、**条件码 CC**：这些在 u2-l1、u2-l5 已建立，本讲会用到「supervisor 组 = `m_r[0..15]`，user 组 = `m_r[16..31]`」「CC 低 4 位是 Z/C/N/V」的结论。
- **大端字节序（Big Endian）**：ZipCPU 是大端机，最高字节存在最低地址。这会直接影响 `MEMDEV` 怎么拼一个 32 位字。

> ⚠️ 一个诚实的提醒：`sim/cpp/README.md` 自己写着 "It is now pretty much **abandonware**"（基本是弃疗代码），原因是「外设和中断太难塞进去了，比往 Verilator 设计里塞还难」。所以本讲的重点是**读懂它的设计思想**，而不是把它当成主力模拟器——后者是 u5-l3 的 Verilator 体系。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|---|---|---|
| [sim/cpp/zipelf.cpp](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zipelf.cpp) | ~250 | 用 `libelf` 解析 ELF，校验机器码、取出入口地址、把每个程序段读进内存 |
| [sim/cpp/zipelf.h](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zipelf.h) | ~50 | 声明 `ELFSECTION` 结构与 `iself`/`elfread` 两个接口 |
| [sim/cpp/twoc.cpp](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/twoc.cpp) | ~55 | 两个补码小工具 `sbits`/`ubits`：从指令字里抠出指定位宽并做符号扩展 |
| [sim/cpp/zsim.cpp](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp) | ~1100 | 主体。包含 `SIMDEV`/`UARTDEV`/`MEMDEV`/`ROMDEV`/`SIMBUS`（设备与总线）和 `ZIPMACHINE`（CPU 核心）以及 `main` |

`zsim.cpp` 内部层次（自底向上）：

```
SIMDEV        ← 抽象设备接口（lw/sw/lb/lh/sh/sb + tick + load）
 ├─ MEMDEV    ← 一块大端 RAM（可读可写）
 ├─ ROMDEV    ← 只读 RAM（写操作被掏空）
 └─ UARTDEV   ← 串口：写到偏移 12 即 putchar 一个字符
SIMENTRY      ← 总线表项：{设备, 基址, 掩码, 权限标志, 名字}
SIMBUS        ← 用 (addr & mask)==base 做地址译码，把访问派发给对应 SIMDEV
ZIPMACHINE    ← 32 个寄存器 + CC + PC；execute() 解释一条指令
main()        ← 装配总线、装载 ELF、取指-执行主循环
```

---

## 4. 核心概念与源码讲解

本讲拆三个最小模块，顺序按「程序怎么进来 → 装在哪些设备上 → CPU 怎么逐条执行」的数据流走：

1. **zipelf ELF 装载**（程序进入模拟器）
2. **SIMBUS / SIMDEV 设备模型**（地址空间与外设）
3. **ZIPMACHINE 指令解释循环**（取指-译码-执行，更新 PC 与 CC）

### 4.1 zipelf：把 ELF 装进模拟器

#### 4.1.1 概念说明

ISS 不像 Verilator 测试台那样靠调试端口把 ELF「喂」进 RAM（见 u5-l3），它直接用操作系统的 `libelf` 库把可执行文件**按段（segment）**读进一块内存，然后告诉 CPU「从入口地址 `entry` 开始跑」。

这里有两个关键设计：

- **机器码校验**：ZipCPU 在 ELF 头里的 `e_machine` 是自定义值 `0x0dad1`（在 binutils 补丁里定义为 `EM_ZIP`）。`elfread` 一上来就检查这个值，防止你拿一个 x86 或 ARM 的 ELF 来糊弄它。
- **段表 `ELFSECTION`**：每个程序段被打包成 `{起始物理地址 m_start, 长度 m_len, 虚拟地址 m_vaddr, 数据 m_data}`。注意 `m_data` 只声明了 `char m_data[4]`——这是「柔性数组」技巧，真正长度由 `m_len` 决定，靠 `malloc` 时多分配空间来容纳变长数据。

#### 4.1.2 核心流程

`elfread(fname, entry, sections)` 的执行过程：

1. 初始化 `libelf`，打开文件，用 `elf_begin` / `gelf_getehdr` 读 ELF 头。
2. 校验：必须是 32 位（`ELFCLASS32`）、必须是 ZipCPU 机器码（`e_machine == 0x0dad1`）。
3. 取出入口地址：`entry = ehdr.e_entry`。
4. 先遍历一遍程序头（`phdr`），**累计所有段需要的总字节数**。
5. `malloc` 一整块，把段指针数组 `sections[]` 和每个段的 `ELFSECTION`（含变长数据）紧凑摆在一起。
6. 再遍历一遍，对每个段：`lseek` 到文件偏移 `p_offset`，`read` 出 `p_filesz` 字节填进 `m_data`，并记录 `m_start = p_paddr`、`m_len = p_filesz`。
7. 末尾放一个 `m_len == 0` 的哨兵段，作为后续循环的终止条件。

> 注意第 6 步用的是**物理地址 `p_paddr`**作为装载地址，不是虚拟地址 `p_vaddr`——因为 ISS 没有真实 MMU，物理地址就是内存地址。

#### 4.1.3 源码精读

机器码校验与入口提取——这是「是不是 ZipCPU 程序」的第一道关：

[sim/cpp/zipelf.cpp:L138-L144](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zipelf.cpp#L138-L144) —— 校验 `e_machine` 必须为 `0x0dad1`（否则报 "not a ZipCPU/8 ELF file" 并退出），随后把 `e_entry` 赋给输出参数 `entry`。

`ELFSECTION` 结构（柔性数组）：

[zipelf.h:L44-L48](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zipelf.h#L44-L48) —— `m_data[4]` 只是占位，真实数据长度由 `m_len` 决定，靠 `malloc` 多分内存实现变长。

把一个段读进内存的核心循环（每段记录 `p_paddr`→`m_start`、`p_filesz`→`m_len`，再 `read` 数据）：

[zipelf.cpp:L217-L234](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zipelf.cpp#L217-L234) —— `r[i]->m_start = phdr.p_paddr`、`m_len = phdr.p_filesz`，然后 `lseek`+`read` 把段内容填进 `m_data`。代码里有一段被注释掉的字节序交换，说明作者曾纠结大/小端问题（最终保留原始大端字节序，由 `MEMDEV` 自己处理拼装）。

`iself` 只看前 4 字节魔数 `0x7f 'E' 'L' 'F'`，是更便宜的「快速判断」：

[zipelf.cpp:L55-L69](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zipelf.cpp#L55-L69)

#### 4.1.4 代码实践

**实践目标**：亲手验证一个 ZipCPU ELF 的机器码与段布局，对照 `elfread` 理解它会被装到哪里。

**操作步骤**：

1. 进入 `bench/asm`，编译专为 ISS 写的 `hellosim`（详见 `bench/asm/Makefile`，目标 `hellosim` 依赖 `simscript.ld`）：
   ```bash
   cd bench/asm && make hellosim      # 产物是 ELF 可执行文件 hellosim
   ```
2. 用系统的 `readelf` 观察 ELF 头与段：
   ```bash
   readelf -h hellosim | grep -i machine   # 期望看到 0xdad1（readelf 可能显示为十六进制或 "Advanced ..."，因为不是标准机器码）
   readelf -l hellosim                      # 看 LOAD 段的 PhysAddr / FileSiz
   ```

**需要观察的现象**：`e_machine` 字段是 `0xdad1`；至少有一个 `LOAD` 段，其 `PhysAddr` 对应 `simscript.ld` 里 `.start` 段的地址。

**预期结果**：对照 `elfread` 的第 6 步，这个 `PhysAddr` 就是 `m_start`，`FileSiz` 就是 `m_len`，段内容会被 `memcpy` 进 `SIMBUS` 上对应地址的设备。

> 「待本地验证」：若环境没装 `zip-gcc` 或 `libelf`，本实践可退化为纯阅读——直接读 `bench/asm/simscript.ld` 找到 `.start` 的链接地址，再对照下面 4.2 的设备表确认它落在哪个设备里。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ELFSECTION::m_data` 只声明 4 字节却装得下整个段？
**答案**：这是 C 风格柔性数组（struct hack）。`elfread` 在第 4 步先累加所有段大小，第 5 步 `malloc(total_octets + ...)` 一次性分配足够大的块，`m_data[4]` 只是占位首地址，越界部分由这块大内存承载。

**练习 2**：如果拿一个 64 位的 x86 ELF 喂给 `zsim`，会在哪一行、为什么失败？
**答案**：在 [zipelf.cpp:L117-L120](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zipelf.cpp#L117-L120) 先因 `ELFCLASS64`（非 `ELFCLASS32`）报 "64-bit ELF file"；即便绕过，也会在 L138 因 `e_machine != 0x0dad1` 报 "not a ZipCPU/8 ELF file"。

---

### 4.2 SIMBUS / SIMDEV：地址空间与设备模型

#### 4.2.1 概念说明

CPU 要访问内存和外设，在真实硬件里走的是 Wishbone/AXI 总线（见 u4 单元）。ISS 不模拟总线协议握手，而是用一个简单的**地址译码表**模拟「给一个地址，找到对应设备，调它的读写方法」。

这套模型由三层组成：

- **`SIMDEV`**：抽象基类，定义 `lw/sw/lb/lh/sh/sb`（字/半字/字节 读写）以及 `tick()`（时钟推进）、`load()`（批量装载）、`interrupt()`（是否有中断）。
- **具体设备**：`MEMDEV`（大端 RAM）、`ROMDEV`（只读）、`UARTDEV`（写到特定偏移就 `putchar`）。
- **`SIMBUS`**：持有一张 `SIMENTRY` 表，每个表项是 `{设备, 基址, 掩码, 权限, 名字}`；收到地址就用 `(addr & mask) == base` 找设备，再按权限（R/W/X/L）放行。

这套设计的好处是**加外设只需 `bus->add(...)`**，坏处（也是 README 抱怨的）是它没有真实的中断/时序语义，很难表达「定时器第 N 个周期触发中断」这类行为。

#### 4.2.2 核心流程

地址译码的核心是一行位运算：

\[
\text{devid} = \text{找到第一个满足 } (\text{addr}\ \&\ \text{mask}) == \text{base} \text{ 的表项}
\]

掩码里为 1 的位参与比较（通常是高位地址位），为 0 的位是**设备内偏移**。例如 UART 基址 `0x150`、掩码 `0xfffffff0`，意味着 `0x150..0x15f` 都命中 UART，低 4 位（0/4/8/12）是它的 4 个寄存器。

读写时，`SIMBUS` 会先把地址里的「设备内偏移」抠出来再传给设备：

```
SIMBUS::lw(addr)
  → devid = getdev(addr)            // (addr & mask)==base 找设备
  → dev->lw(addr & ((~mask) & -4))  // 只保留偏移位并对齐到字
```

权限检查分三种：`getdev`（只要命中即可读）、`getwrdev`（额外要求 `W_OK`）、`getexdev`（额外要求 `X_OK`，用于**取指**——只有标记可执行的段才能拿来当指令）。找不到设备或权限不符就置 `m_buserr = true`，CPU 下一拍会因此触发总线错误异常。

`MEMDEV` 的大端拼装（这是大端机的精髓）：

```
lw(addr): v = (m_mem[addr]<<24) | (m_mem[addr+1]<<16)
            | (m_mem[addr+2]<<8) | (m_mem[addr+3])
```

即**最低地址的字节放在最高 8 位**。

#### 4.2.3 源码精读

`SIMDEV` 抽象接口与半字/字节的默认实现：

[sim/cpp/zsim.cpp:L69-L113](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L69-L113) —— 纯虚 `lw/sw` 必须由子类实现；`lb/lh/sh/sb` 给了默认实现（用 `lw` 读出整字再移位），子类（如 `MEMDEV`）可以覆盖得更高效。

`MEMDEV::lw` 的大端字拼装：

[zsim.cpp:L153-L168](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L153-L168) —— `(a<<24)|(b<<16)|(c<<8)|d`，最低地址字节 `a` 在最高位，正是大端序。

`UARTDEV`：写到偏移 12 就 `putchar`（这就是 printf 输出的最终落点）：

[zsim.cpp:L130-L139](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L130-L139) —— `case 12: putchar(vl & 0x0ff);`。

`SIMBUS` 的地址译码 `getdev` 与字读 `lw`：

[zsim.cpp:L236-L252](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L236-L252) —— 遍历设备表，返回第一个 `(addr & m_mask)==m_addr` 的下标，找不到返回 -1。

[zsim.cpp:L309-L322](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L309-L322) —— `lw` 用 `getdev`、`lx`（取指用）用 `getexdev`（要求 `X_OK`），二者都会在 miss 时置 `m_buserr`。

`add()` 如何把权限字符串解析成标志位：

[zsim.cpp:L275-L293](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L275-L293) —— 字符串里的 `r/w/x/l` 分别置 `R_OK/W_OK/X_OK/L_OK`。其中 `R_OK/W_OK/X_OK` 复用了 `<unistd.h>` 里 `access()` 的常量（4/2/1），`L_OK=8` 是本文件 [L221-L223](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L221-L223) 自定义的第 4 个 bit（表示「可装载」，用于 `load()`）。

#### 4.2.4 代码实践

**实践目标**：搞清楚 `main()` 里那张设备表，并跟踪一次内存读的完整路径。

**操作步骤**：

1. 阅读 `main()` 里的设备装配（这是整个地址空间的定义）：

   [zsim.cpp:L1040-L1047](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L1040-L1047) —— 四个设备：UART@`0x00000150`、BlockRAM(128KiB)@`0x0020000`、Flash(16MiB,ROM)@`0x01000000`、SDRAM(256MiB)@`0x10000000`。把这张表抄下来。

2. 跟踪一条 `LW (R1),R0`（假设 R1=`0x10000004`）的调用链：
   ```
   ZIPMACHINE::fullinsn case 18 → m_bus->lw(0x10000004)
     → SIMBUS::lw: getdev(0x10000004) 命中 SDRAM（base=0x10000000,mask=0xf0000000）
     → MEMDEV::lw(0x10000004 & ~mask = 0x4)  // 设备内偏移 4
     → 大端拼 m_mem[4..7] 返回 32 位字
   ```

**需要观察的现象**：地址的高位（被掩码选中的位）决定命中哪个设备；低位（掩码外）变成设备内偏移。

**预期结果**：你能解释「为什么 BlockRAM 用 `0x07fe0000` 这个奇怪的掩码」——它选中的是地址的高位段，留下约 17 位（128KiB）作为 RAM 内偏移。

> 「待本地验证」：可加一行 `bus->add(new MEMDEV(10), 0x00020000, 0xfffc0000, "RW", "MyDev");`（示例代码，非项目原有），重新 `make zsim` 后用一个往 `0x20000` 写再读的小程序观察是否命中——但这需要能编译 ZipCPU 汇编，环境不全时退化为阅读练习。

#### 4.2.5 小练习与答案

**练习 1**：`SIMBUS::lw` 和 `SIMBUS::lx` 有什么区别？为什么取指必须用 `lx`？
**答案**：`lw` 用 `getdev`（只要地址命中即可），`lx` 用 `getexdev`（额外要求设备有 `X_OK` 可执行权限）。取指用 `lx` 是为了**禁止从不可执行的段（如纯数据区）执行代码**，对应真实硬件里取指与访存的权限分离。

**练习 2**：`SIMDEV::lb`（默认实现）为什么先 `lw(addr&-4)` 再移位？
**答案**：因为很多设备（如未来的寄存器外设）只支持按字读。默认实现先按字对齐读出 4 字节，再根据 `addr&3` 把目标字节移到最低位并掩码到 `0xff`，让所有设备「免费」获得字节读能力。`MEMDEV` 因为有字节寻址的 RAM，覆盖了更直接的版本。

---

### 4.3 ZIPMACHINE：指令解释循环

#### 4.3.1 概念说明

`ZIPMACHINE` 是 ISS 的 CPU 核心。它不模拟流水线、不模拟缓存、不模拟总线时序——它只做一件事：**给我一条 32 位指令字，我告诉你寄存器和 CC 怎么变**。这是 ISS「快」的根本原因：一条指令 = 一次 C++ 函数调用 + 一个 `switch` 分支，没有时钟周期的概念。

它的状态极简：

- `m_r[32]`：32 个 32 位寄存器。下标 `0..15` 是 supervisor 组，`16..31` 是 user 组（与 u2-l1 的双寄存器组完全对应）。其中 `m_r[14]`=sCC、`m_r[15]`=sPC、`m_r[30]`=uCC、`m_r[31]`=uPC。
- `m_gie`：当前是否在 user 模式（GIE 位）。它决定 `rbase()` 返回 0 还是 16，从而选 supervisor 组还是 user 组。
- `m_jumped` / `m_advance_pc`：本次指令是否改写了 PC（跳转）、PC 是否该正常 +4。
- `m_icount`：已执行指令计数（仅用于调试日志）。

`gie()`（读）与 `gie(bool)`（写）是模式切换的钥匙：写 `gie(false)` 会顺带把 `m_jumped=true`，表示「发生了模式切换，PC 别再 +4 了」——这正是 u2-l5 讲的「中断/异常靠切寄存器组响应，零内存开销」在软件层面的实现。

#### 4.3.2 核心流程

一条指令的生命周期（`main` 主循环 → `execute` → `fullinsn`）：

```
main 循环:
  insn = bus->lx(pc())                 // 1. 取指（必须可执行段）
  若 bus->error() → 触发总线错误异常
  若 sleeping() → 跳过执行
  否则 execute(insn)                   // 2. 解释
  若 halted() → 打印 "CPU HALT"，退出
  bus->tick()                          // 3. 推进设备时钟
  若 interrupt() && gie() && !locked() → gie(false)   // 4. 响应中断

execute(insn):
  igie = gie()
  if (insn & 0x80000000):              // 最高位=1 → 压缩指令 CIS
      先执行高半字 cisinsn(insn>>16)，置 CC_PHASE
      再执行低半字 cisinsn(insn&0xffff)，清 CC_PHASE
      pc_advance(igie)
  else:                                // 普通全字指令
      fullinsn(insn)
      if (m_advance_pc) pc_advance(igie)
      if (gie() && CC_STEP) gie(false) // 单步模式：执行一条就掉回 supervisor
```

`fullinsn` 内部按 ISA 编码（u2-l2）切字段：

- `opc  = (insn>>22)&0x1f`（5 位操作码，bit 26..22）
- `arg  = (insn>>27)&0x0f`（目的寄存器 DR，bit 30..27，同时是源操作数 A）
- `brg  = (insn>>14)&0x0f`（源寄存器 BR，bit 17..14）
- `cnd  = (insn>>19)&7`（3 位条件码，bit 21..19；LDI 无条件码）
- 立即数位宽由指令类别决定（LDI 取 23 位、MOV 取 13 位、寄存器型取 14 位偏移、立即数型取 18 位），统一用 `twoc.cpp` 的 `sbits` 做符号扩展。

然后按 `opc` 查那个巨大的 `switch`：`case 0/16` 是 SUB/CMP、`case 2` 是 ADD、`case 13` 是 MOV、`case 18..23` 是 LW/SW/LH/SH/LB/SB、`case 24/25` 是 LDI、`case 26..31` 是 FPU……每个分支算出 `result` 和标志 `f`（Z/C/N/V）。

PC 与 CC 的更新发生在末尾：

- **PC**：`pc_advance()` 给当前组的 `m_r[15+rbase()] += 4` 并对齐到字。但若指令写回了 PC（`arg==15+rbase()`），则 `m_advance_pc=false`、`m_jumped=true`，PC 用写入值、不再 +4。
- **CC**：`if (wf) ccodes(f)` 把低 4 位标志 Z/C/N/V 写进当前组的 CC；写 CC 本身（`arg==14+rbase()`）还能触发模式切换（清/置 GIE 位 = RTU/返回用户态）。

#### 4.3.3 源码精读

取指-执行主循环（ISS 的「心跳」）：

[zsim.cpp:L1072-L1105](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L1072-L1105) —— `insn = bus->lx(zipm->pc())` 取指；`bus->error()` 触发异常；`!sleeping()` 才 `execute`；`halted()` 退出；`bus->tick()` 推进设备；最后检查中断。

`ZIPMACHINE` 的状态字段与 `gie` 切组逻辑：

[zsim.cpp:L374-L380](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L374-L380) —— `m_r[32]` 两组合一、`m_gie`、`m_jumped`、`m_advance_pc`。

[zsim.cpp:L445-L463](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L445-L463) —— `rbase()` 按 `m_gie` 返回 0 或 16 选组；`cc()` 把 GIE 位或进去（sCC.GIE 恒 0、uCC.GIE 恒 1，与 u2-l1 结论一致）。

[zsim.cpp:L469-L478](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L469-L478) —— `gie(bool v)` 在模式变化时置 `m_jumped=true`；`pc_advance` 给当前组 PC 加 4 并对齐。

字段切分（对应 u2-l2 的编码表）：

[zsim.cpp:L605-L622](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L605-L622) —— `opc/arg/brg` 切位；立即数按 LDI(23)/MOV(13)/寄存器(14)/立即数(18) 四档用 `sbits` 符号扩展。

条件求值（对应 u2-l4 的 8 种条件码）：

[zsim.cpp:L655-L670](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L655-L670) —— `cnd` 0=无条件、1..4=Z/N/C/V、5=NZ、6=GE(NN)、7=NC；条件不满足则 `execinsn=false`，整条指令「执行但不写回」。

ADD 执行 + 标志位生成（Z/N/C/V 同时算出）：

[zsim.cpp:L752-L758](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L752-L758) —— `result = bv + av`；进位 CC_C 来自无符号溢出、CC_V 来自有符号溢出（正/负号翻转）。末尾 [L854-L857](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L854-L857) 统一由 `result==0` 置 CC_Z、由最高位置 CC_N。

`twoc.cpp` 的 `sbits`——抠位宽并符号扩展（被上面 L618-L622 反复调用）：

[twoc.cpp:L43-L50](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/twoc.cpp#L43-L50) —— 先掩码到 `bits` 位，若最高位为 1 则把高位全置 1（符号扩展）。

写回 CC 与 PC（本次指令的「落槌」）：

[zsim.cpp:L896-L940](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L896-L940) —— `if (wf) ccodes(f)` 写低 4 位标志；`if (wb)` 写寄存器，其中写 PC(`arg==15+rbase()`) 置 `m_jumped`、写 CC(`arg==14+rbase()`) 时按 GIE 位变化触发模式切换（RTU/返回用户态）；最后强制 `sCC.GIE=0`、`uCC.GIE=1`、PC 字对齐。

`execute` 顶层分发（区分 CIS 压缩指令与全字指令）：

[zsim.cpp:L988-L1012](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L988-L1012) —— bit31=1 时拆成两个 16 位半字分别 `cisinsn`（中间用 CC_PHASE 标记「在执行 CIS 的第二拍」），否则 `fullinsn`；执行后按 `m_advance_pc` 推进 PC，单步模式（CC_STEP）执行一条即掉回 supervisor。

#### 4.3.4 代码实践

**实践目标**（本讲核心实践）：跟踪一条 `ADD 1,R0`（编码 `0x10800001`，opc=2、arg=0、立即数 1）从取指到写回的全过程，描述 PC 与 CC 如何更新；并说明 ISS 比起 Verilator 模拟器快在哪、又丢失了什么。

**操作步骤（源码阅读型）**：

1. **取指**：在 [L1074](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L1074) `insn = bus->lx(pc())` 假设取回 `0x10800001`，无 bus error，`execute(insn)` 被调用。
2. **分发**：[L991](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L991) bit31=0 → 走 `fullinsn`。
3. **切字段**：[L605-L607](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L605-L607) `opc=2`(ADD)、`arg=0`(R0)、`brg=0`；[L620-L622](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L620-L622) bit18=0 → `imm = sbits(insn,18) = 1`。
4. **取操作数**：[L682-L696](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L682-L696) `bv=imm=1`、`av=m_r[0]`（旧 R0）。
5. **执行**：[L752-L758](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L752-L758) `result = bv+av`，按是否进位/溢出置 `f` 的 CC_C/CC_V；[L854-L857](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L854-L857) 补 CC_Z/CC_N。
6. **写回**：[L896-L897](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L896-L897) `ccodes(f)` 更新 CC 低 4 位；[L925](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L925) `m_r[0]=result` 更新 R0。
7. **PC 推进**：回到 `execute` [L1006-L1007](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L1006-L1007)，因没写 PC，`m_advance_pc=true` → `pc_advance()` 给 PC +4。

**PC 与 CC 更新总结**：PC 在当前组（supervisor 组 `m_r[15]` 或 user 组 `m_r[31]`）+4；CC 的低 4 位（Z/C/N/V）被 `ccodes(f)` 覆盖为本次 ADD 的结果标志，高位（SLEEP/GIE/异常位）保持不变。

**ISS vs Verilator**：

| | ISS（`zsim`） | Verilator（`zipcpu_tb`） |
|---|---|---|
| **快在哪** | 一条指令 = 一次函数调用 + 一个 `switch`，没有时钟概念，每秒可执行数百万条指令 | 每个时钟都要算完整五级流水线、缓存、总线握手，慢 2~3 个数量级 |
| **丢失了什么** | ❌ 没有流水线冒险/停顿（所以 `simtest.s` 里那些测 pipeline 的用例在 ISS 上意义有限）❌ 没有缓存命中/缺失 ❌ 没有真实总线时序（`SIMBUS` 是瞬时函数调用，不是周期握手）❌ 中断/外设时序极难表达（这正是 README 说它 "abandonware" 的原因）❌ 没有周期计数，算不出 IPC | ✅ 全都有，能抓硬件 bug |

一句话：**ISS 验证「ISA 语义对不对」，Verilator 验证「硬件实现对不对」**。

> 「待本地验证」：若已 `make zsim` 并编译了 `bench/asm/hellosim`，可 `./zsim hellosim` 观察 `Hello World!` 经 `SOUT` → `siminsn` → `putchar` 输出，最后 `SEXIT 0` 触发 `exit(0)`。环境不全时，本实践完全可作为纯源码跟踪完成。

#### 4.3.5 小练习与答案

**练习 1**：`ADD 1,R0` 执行后，如果旧 R0=`0xFFFFFFFF`，CC 的 Z/C/N/V 各是几位、哪些会被置 1？
**答案**：result = `0xFFFFFFFF + 1 = 0x00000000`（回绕）。CC_Z(bit0)=1（结果为 0）；CC_C(bit1)=1（无符号进位：`result < (uint64_t)bv+(uint64_t)av`）；CC_N(bit2)=0（最高位 0）；CC_V(bit3)=0（有符号：0xFFFFFFFF=-1 与 1 异号相加，不会溢出）。注意 CC 的位序是 `{V,N,C,Z}` 对应 bit3/2/1/0（见 [zsim.cpp:L64-L67](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L64-L67)）。

**练习 2**：为什么「写 PC」和「模式切换」都要把 `m_jumped=true`？
**答案**：二者都意味着「下一条要执行的指令不在 PC+4 处」。写 PC 是显式跳转；模式切换（`gie` 变化）会把活动寄存器组从 user 换到 supervisor（或反之），此时「当前 PC」的语义已经切换到另一组的 PC，不能再给原来的 PC +4，否则会跑错地方。置 `m_jumped=true` 让 `execute` 跳过 `pc_advance`。

**练习 3**：`SEXIT 0`（`bench/asm/hellosim.s` 里的退出指令）在 ISS 里走哪条路径？
**答案**：它是一条 NOOP 编码的 SIM 伪指令。`fullinsn` 里 `noop` 分支 [L722-L724](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L722-L724) 检测到 `insn != 0x7fc00000` 就调 `siminsn(insn)`；`siminsn` [L499-L501](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L499-L501) 匹配 `(insn&0x0fffff)==0x00100`（SIM Exit(0)）直接 `exit(0)`——这是 ISS 专属的「带外退出通道」，真实硬件上这条指令是普通 NOOP（这也是 `hellosim.s` 末尾跟一个 `HALT` 兜底的原因）。

---

## 5. 综合实践

**任务**：把三个模块串起来，讲清楚「`Hello` 这串字符是怎么从 ELF 文件跑到终端上的」。

**背景**：`bench/asm/hellosim.s` 是专为 ISS 写的最小测试——它只用 SIM 伪指令（`SOUT` 输出字符、`SEXIT` 退出），不依赖任何真实外设时序，是验证 ISS 是否正常工作的「冒烟测试」。

**请按以下链路梳理（源码阅读 + 可选运行）**：

1. **装载**（4.1）：`./zsim hellosim` → `main` 调 `elfread` → 校验 `e_machine==0x0dad1` → 取 `entry=e_entry` → 把 `.start` 段读进 `m_data`，记录 `m_start`。
2. **入内存**（4.2）：[zsim.cpp:L1049-L1059](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L1049-L1059) 的段装载循环调 `bus->load(m_start, m_data, m_len)`，由 `SIMBUS` 译码找到对应 `MEMDEV`/`ROMDEV` 并 `memcpy` 进去。
3. **取指执行**（4.3）：`zipm->m_r[15] = entry` 设 PC；主循环 `bus->lx(pc)` 取指 → `execute` → 因 `hellosim` 全用 NOOP 编码的 SIM 指令，走 `fullinsn` 的 `noop` 分支 → `siminsn`。
4. **输出**：每个 `SOUT 'x'` 匹配 `siminsn` 的 `(insn&0x0fff00)==0x00400`（SOUT[Imm]，[L537-L539](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L537-L539)），`fprintf(stderr,"%c", insn&0xff)` 把字符打到 stderr——这就是 `Hello` 的由来（**注意：SIM 指令走 stderr，而真走 UART 的 `simuart.s` 才用 `UARTDEV`→`putchar`→stdout，两者路径不同**）。
5. **退出**：`SEXIT 0` 匹配 `(insn&0x0fffff)==0x00100` → `exit(0)`，进程结束。

**交付物**：画一张数据流图，标注每一步对应的源码行号；并写一段话回答——**如果改用 Verilator 模拟器跑 `hellosim`，第 4 步会变成什么？**（提示：SIM 伪指令在真实 RTL 上是 NOOP，所以 Verilator 跑 `hellosim` 不会输出任何字符，这也正是为什么 u1-l4 的 hello 程序走的是真 UART 路径而非 SIM 指令。）

> 「待本地验证」：能跑的话 `cd bench/asm && make hellosim && cd ../cpp && make zsim && ./zsim ../asm/hellosim`，应在 stderr 看到 `Hello World!`。这条命令能否成功取决于是否装好了 `zip-gcc`、`libelf-dev` 以及 `simscript.ld` 的链接地址是否落在 ISS 设备表（4.2）的某段里。

## 6. 本讲小结

- **ISS 仿真语义，不仿真时序**：`zsim` 用一个大 `switch` 把指令字翻译成 C++ 动作，没有时钟周期概念，因此快但抓不到流水线/缓存/总线类的硬件 bug。
- **三文件分工**：`zipelf.cpp` 用 `libelf` 装载 ELF（校验机器码 `0x0dad1`、取入口、按段 `p_paddr` 读入）；`zsim.cpp` 的 `SIMBUS`/`SIMDEV` 用 `(addr&mask)==base` 做地址译码、模拟 RAM/ROM/UART；`ZIPMACHINE` 逐条解释指令。
- **双寄存器组在软件里就是一个 `m_r[32]`**：`rbase()` 按 `m_gie` 在 0/16 间切换，supervisor 组占 `0..15`、user 组占 `16..31`；模式切换 = 改 `m_gie` + 置 `m_jumped`，对应 u2-l5 的零开销中断响应。
- **PC 与 CC 的更新是「写回阶段」才落槌**：`ccodes(f)` 写低 4 位标志，写 PC/CC 会触发跳转或模式切换；普通指令末尾 `pc_advance()` 给 PC +4。
- **SIM 伪指令是 ISS 的带外通道**：`SOUT`/`SEXIT` 等 NOOP 编码指令只在 `siminsn` 里有意义，在真实 RTL 上是普通 NOOP——这就是 `hellosim.s` 末尾要跟 `HALT` 兜底、且 Verilator 测试改走真 UART 的原因。
- **它是 abandonware**：README 自己承认外设/中断太难集成，所以 ZipCPU 的主力验证已转向 Verilator（u5-l3）和形式化（u5-l2）；ISS 如今主要用于快速冒烟测试 ISA 语义。

## 7. 下一步学习建议

- **对照真实硬件**：读本讲的 ISS 后，强烈建议接着读 **u5-l3（Verilator 测试框架）**，看同一份测试程序在「逐周期仿真」下要额外处理调试端口、`tick()`、HALT/BUSY 判定——你会立刻体会到本讲说的「丢失了什么」。
- **ISA 语义的权威**：本讲 `fullinsn` 的 `switch` 是 ISA 的 C++ 表达，**u2 单元（spec.tex）**是同一份 ISA 的文字表达。若发现某条指令的 ISS 行为和 spec 不一致，以 spec 为准（spec 是 RTL 实现的依据，ISS 反而是「可能过时」的那一个）。
- **指令编码**：想彻底看懂 [L605-L622](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L605-L622) 的字段切分，复习 **u2-l2（指令格式与编码）**；想看硬件怎么译码，读 **u3-l3（idecode）**——你会发现在 ISS 里用 `switch case` 做的事，硬件里用组合逻辑 + `generate if` 做。
- **外设建模的差距**：本讲的 `UARTDEV`/`MEMDEV` 是「瞬时函数调用」，而真实外设是 **u4-l5** 那些有状态、按时钟节拍工作的 Wishbone 从设备。比较二者能深刻理解为什么 README 说「外设难塞进 ISS」。
