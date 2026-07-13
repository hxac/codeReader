# 分页与 MMU 地址翻译

## 1. 本讲目标

u4-l13 在物理内存 `paddr` 之上叠了一层虚拟内存 `vaddr`，并铺好了两道「接缝」——`isa_mmu_check`（要不要翻译）与 `isa_mmu_translate`（怎么翻译），但当时只给了 stub：检查恒返回 `MMU_DIRECT`、翻译恒返回 `MEM_RET_FAIL`，`vaddr_read/write` 也只是原样透传给 `paddr_*`。本讲就把它从「接口先行」推进到「实现落地」：真正给 riscv32 装上一套 SV32 分页机制。

学完本讲你应该能够：

- 说清 `isa_mmu_check` 三级返回值 `MMU_DIRECT / MMU_TRANSLATE / MMU_FAIL` 各自的语义，以及当前为何用宏把它「编译期短路」成 `MMU_DIRECT`；
- 解释 `isa_mmu_translate` 复用 `paddr_t` 返回类型、用 `0/1/2` 作哨兵编码 `MEM_RET_OK/FAIL/CROSS_PAGE` 的设计，并说清它依赖「物理地址不会落在 0/1/2」这一假设；
- 设计并实现 SV32 两级页表遍历：从 `satp` 取根表基址，按 `VPN[1]/VPN[0]` 逐级读 PTE，做权限检查并合成物理地址；
- 处理跨 4KB 页的读写：识别 `MEM_RET_CROSS_PAGE`，在 `vaddr_read/write` 里把一次访问拆成两段、小端拼装；
- 理解可选的 TLB 加速思路，并知道它在教学版 NEMU 里不是必须的。

本讲是 PA3 的另一根支柱（与 u7-l21 中断异常并列）。它消费 u4-l13 的 MMU 接口、u5-l14 的 `CPU_state`/`ISADecodeInfo` 与 `word_t/vaddr_t/paddr_t` 宽度基因，并以 u7-l21 的 `isa_raise_intr` 作为缺页异常的出口。可以说：u4-l13 画了图纸，u7-l21 备好了「异常抛出」的通道，本讲负责把房子盖起来。

## 2. 前置知识

### 2.1 为什么需要分页——虚拟地址与物理地址的解耦

到目前为止，NEMU 里客机程序发出的地址（取指的 `pc`、`lw/sw` 的访存地址）都**直接等于**物理地址：`vaddr_ifetch/read/write` 透传给 `paddr_read/write`，而 `paddr_*` 再用 `guest_to_host` 把它平移成宿主机指针（见 u4-l12）。这意味着每个程序都必须知道自己被加载到哪段物理内存，两个程序不能共用同一套地址——没有隔离，也没有「各自拥有整段地址空间」的错觉。

分页（paging）打破这个限制：程序使用的是**虚拟地址（virtual address, vaddr）**，CPU 里的 **MMU（Memory Management Unit，内存管理单元）** 在每次访存时把它**翻译（translate）**成物理地址（paddr）。翻译依据是一张由操作系统维护的**页表（page table）**——它以「页（page）」为单位（RISC-V 一页 4KB）记录「哪段虚拟页 → 哪个物理页」的映射。于是：

- **隔离**：不同进程的页表不同，同一段虚拟地址可映射到不同物理页；
- **保护**：页表项里带权限位（可读 / 可写 / 可执行），MMU 在翻译时顺带做权限检查，越权即抛**缺页异常（page fault）**；
- **按需分配**：虚拟地址空间可以远大于物理内存，未映射的页触发异常后由 OS 按需装入。

> 一个关键直觉：**翻译是逐次访存发生的**，不是一次性把整段地址改写。每次 `lw`、每次取指，MMU 都要查一次页表。所以翻译函数 `isa_mmu_translate(vaddr, len, type)` 的参数里有 `len`（这次读几个字节，用来判断是否跨页）和 `type`（取指 / 读 / 写，用来做权限检查）。

### 2.2 RISC-V SV32 分页概览

NEMU 的 riscv32 只实现机器模式（M-mode）、不做保护（见 README）。它支持的分页模式是 **SV32**——RISC-V 32 位下的两级页表方案。涉及的核心数据有三件：控制翻译的 `satp` 寄存器、被翻译的虚拟地址、记录映射的页表项 PTE。

**`satp`（Supervisor Address Translation and Protection）** 是控制分页的 CSR（控制状态寄存器，见 u7-l21 对 CSR 的介绍）。RV32 下它 32 位，布局为：

\[
\text{satp} = \underbrace{\text{MODE}}_{\text{bit 31}} \;\Big|\; \underbrace{\text{ASID}}_{\text{bits 30..22}} \;\Big|\; \underbrace{\text{PPN}}_{\text{bits 21..0}}
\]

- `MODE`：0 = Bare（不翻译，虚拟地址直接当物理地址）；1 = SV32（启用两级页表）。
- `ASID`：地址空间标识，教学版可忽略。
- `PPN`：根页表的**物理页号**，根页表在物理内存里的基地址 = `PPN << 12`（左移 12 位是因为一页 4KB = 2¹² 字节）。

> 注意 `satp` 当前并不在 `CPU_state` 里——u7-l21 已经让你加过 `mepc/mcause/mtvec/mstatus` 四个 trap 相关 CSR，本讲需要再加一个 `satp`（综合实践第 1 步）。

**虚拟地址 VA**（32 位）被切成三段：

\[
\text{VA} = \underbrace{\text{VPN}[1]}_{\text{bits 31..22 (10 bit)}} \;\Big|\; \underbrace{\text{VPN}[0]}_{\text{bits 21..12 (10 bit)}} \;\Big|\; \underbrace{\text{offset}}_{\text{bits 11..0 (12 bit)}}
\]

`VPN`（Virtual Page Number）是页号，`offset` 是页内偏移。两级页表对应两级 `VPN`：`VPN[1]` 索引第一级（根）表，`VPN[0]` 索引第二级表。

**页表项 PTE**（32 位）是页表里的一项，记录「下一级表 / 物理页」的位置与权限：

\[
\text{PTE} = \underbrace{\text{PPN}}_{\text{bits 31..10 (22 bit)}} \;\Big|\; \underbrace{\text{RSW}}_{\text{9..8}} \;\Big|\; \underbrace{\text{D A G U X W R V}}_{\text{bits 7..0}}
\]

低 8 位是权限标志：`V`（valid 有效）、`R`（read 可读）、`W`（write 可写）、`X`（execute 可执行）、`U`（user 用户态可访问）、`G`（global 全局）、`A`（accessed 已访问）、`D`（dirty 已写）。高 22 位 `PPN` 指向物理页。一次翻译就是顺着 `VPN[1] → VPN[0]` 两级读 PTE、最后用叶子 PTE 的 `PPN` 与 `offset` 合成物理地址（详见 4.4）。

> 因为 `riscv64` 在 NEMU 里是 `riscv32` 的符号链接（见 u1-l4、`src/isa/riscv64 -> riscv32`），同一份 `isa-def.h` 与 `mmu.c` 同时服务 riscv32/riscv64，故都按 SV32 处理。

### 2.3 前序接口回顾

把 u4-l13 铺好的接口再过一遍，本讲全部围绕它们展开：

- [`include/isa.h:L41`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L41) 的 `enum { MMU_DIRECT, MMU_TRANSLATE, MMU_FAIL }`——`isa_mmu_check` 的返回值；
- [`include/isa.h:L42`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L42) 的 `enum { MEM_TYPE_IFETCH, MEM_TYPE_READ, MEM_TYPE_WRITE }`——访存种类，翻译时据此查权限；
- [`include/isa.h:L43`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L43) 的 `enum { MEM_RET_OK, MEM_RET_FAIL, MEM_RET_CROSS_PAGE }`——`isa_mmu_translate` 的返回值哨兵；
- [`include/isa.h:L44-L46`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L44-L46) 的 `#ifndef isa_mmu_check` 守卫——这是本讲「把宏换成函数」的关键开关（4.1 详述）；
- [`src/memory/vaddr.c:L19-L29`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L19-L29) 的三个函数当前全部透传 `paddr_*`——这是本讲要改造的「主战场」。

另外回顾两个工具宏（[`include/macro.h:L86-L88`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L86-L88)）：`BITS(x, hi, lo)` 取 `x` 的 `[hi:lo]` 位段、`SEXT(x, len)` 做符号扩展——页表遍历里切 `VPN`、读 PTE 字段都靠它们，与 u5-l16 解码立即数用的是同一套。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/isa/riscv32/system/mmu.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/mmu.c) | `isa_mmu_translate`——**目前是 stub，恒返回 `MEM_RET_FAIL`**，本讲主战场之一 |
| [src/isa/riscv32/include/isa-def.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h) | `CPU_state`（待加 `satp`）、`ISADecodeInfo`、`isa_mmu_check` 宏（恒 `MMU_DIRECT`）——本讲要动这两处 |
| [include/isa.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h) | MMU/MEM_TYPE/MEM_RET 三组枚举与 `isa_mmu_check/translate` 接口声明、`#ifndef` 守卫 |
| [src/memory/vaddr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c) | `vaddr_ifetch/read/write`——**目前透传 paddr**，本讲要插入 check/translate/跨页拆分 |
| [include/memory/vaddr.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/vaddr.h) | `PAGE_SHIFT/PAGE_SIZE/PAGE_MASK`——跨页判定的依据 |
| [include/memory/paddr.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/paddr.h) | `PMEM_LEFT/RIGHT`、`in_pmem`——翻译出物理地址后仍走这套（见 u4-l12） |
| [include/macro.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h) | `BITS/SEXT/BITMASK/PG_ALIGN`——页表遍历的位操作工具 |
| [src/isa/riscv32/system/intr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c) | `isa_raise_intr`（u7-l21）——缺页异常经此抛出 |
| [src/isa/riscv32/inst.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c) | `Mr/Mw` 即 `vaddr_read/write`——分页启用后所有访存自动走翻译路径 |

## 4. 核心概念与源码讲解

本讲按「先判定要不要翻译、再翻译、再处理跨页、再讲页表怎么走、最后讲怎么加速」的顺序，拆成五个最小模块：

### 4.1 MMU 检查机制：isa_mmu_check 与 MMU_DIRECT/TRANSLATE/FAIL

#### 4.1.1 概念说明

每次访存到来时，框架要做的第一个决定是：**这次访问需要翻译吗？** 这正是 `isa_mmu_check(vaddr, len, type)` 的职责。它返回三个值之一（[`include/isa.h:L41`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L41)）：

| 返回值 | 语义 | 调用者动作 |
|--------|------|-----------|
| `MMU_DIRECT` | 直接访问，`vaddr == paddr`，无需翻译 | 直接 `paddr_read/write(addr, len)` |
| `MMU_TRANSLATE` | 需要翻译 | 调 `isa_mmu_translate`，再按结果处理 |
| `MMU_FAIL` | 翻译配置非法（如不支持的 `MODE`） | 抛异常 |

为什么要把「要不要翻译」单独抽成一步，而不是直接调 `isa_mmu_translate`？因为**绝大多数访存可能根本没开分页**（`satp.MODE == 0` 的 Bare 模式），此时每次都去走页表是巨大浪费。`isa_mmu_check` 让框架在「确定不用翻译」时走快路径直连物理内存，只有「确实开了分页」才进慢路径翻译。这是性能与结构的双重优化。

#### 4.1.2 核心流程

`isa_mmu_check` 的判定逻辑（以 SV32 为例）：

```text
读 satp.MODE
  MODE == 0 (Bare)  →  返回 MMU_DIRECT      # 虚拟地址即物理地址
  MODE == 1 (SV32)  →  返回 MMU_TRANSLATE   # 交给 isa_mmu_translate
  其它（如 SV39/SV48，RV32 不支持）→ 返回 MMU_FAIL
```

注意它**逐次访存**判定——因为 `satp` 可能被客机程序在运行中改写（如 `csrw satp, x0` 关闭分页），所以检查必须读「当下」的 `satp`，不能缓存编译期结论。

#### 4.1.3 源码精读

当前 riscv32 把 `isa_mmu_check` 定义成一个**宏**，恒返回 `MMU_DIRECT`（[`src/isa/riscv32/include/isa-def.h:L31`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L31)）：

```c
#define isa_mmu_check(vaddr, len, type) (MMU_DIRECT)
```

四套 ISA 都这么写（mips32 / x86 / loongarch32r 的 `isa-def.h` 同样是这行），等于宣告「本 ISA 当前不分页」。这个宏之所以能成立，靠的是 [`include/isa.h:L44-L46`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L44-L46) 的守卫：

```c
#ifndef isa_mmu_check
int isa_mmu_check(vaddr_t vaddr, int len, int type);
#endif
```

机制是这样的：`isa.h` 第 20 行先 `#include <isa-def.h>`，于是 `isa_mmu_check` 这个宏已经被定义；等到第 44 行 `#ifndef isa_mmu_check` 时，预处理器看到它**已定义**，就跳过下面的函数声明。结果在所有 `.c` 文件里，`isa_mmu_check(a,b,c)` 被展开成常量 `(MMU_DIRECT)`，**整个翻译分支在编译期就被消除**——这正是 u4-l13 说的「编译期短路」。

> 一个易错点：宏体里引用了 `MMU_DIRECT`，但 `MMU_DIRECT` 的 `enum` 定义在 [`include/isa.h:L41`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L41)，**晚于** `isa-def.h` 的包含。这没问题——宏体只在**展开时**才求值，而展开发生在 `.c` 文件里，那时整个 `isa.h`（含 enum）都已可见。

要把检查改成「读 `satp` 的运行时判定」，关键是**把宏换成函数**：删掉 `isa-def.h` 里那行 `#define`，`#ifndef` 守卫随即放行函数声明，再在 `mmu.c` 里实现这个函数即可（见 4.1.4）。

#### 4.1.4 代码实践

**实践目标**：把 `isa_mmu_check` 从「编译期常量」改成「读 `satp` 的运行时判定」。

**操作步骤**（示例代码，需你动手落到源码）：

1. 在 [`src/isa/riscv32/include/isa-def.h:L21-L24`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24) 的 `CPU_state` 里加一个 `satp` 字段（与 u7-l21 加 trap CSR 同理）：

   ```c
   typedef struct {
     word_t gpr[MUXDEF(CONFIG_RVE, 16, 32)];
     vaddr_t pc;
     word_t satp;   // 新增：分页控制寄存器
   } riscv32_CPU_state;
   ```

2. 删掉 [`src/isa/riscv32/include/isa-def.h:L31`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L31) 的 `#define isa_mmu_check ...`，让 `#ifndef` 守卫放行函数声明。

3. 在 `mmu.c` 里实现：

   ```c
   int isa_mmu_check(vaddr_t vaddr, int len, int type) {
     word_t mode = BITS(cpu.satp, 31, 31);   // SV32: 1 bit MODE
     if (mode == 0) return MMU_DIRECT;       // Bare
     if (mode == 1) return MMU_TRANSLATE;    // SV32
     return MMU_FAIL;
   }
   ```

**需要观察的现象**：编译应通过（`#ifndef` 放行后函数声明与定义匹配）。在未写 `satp` 时 `cpu.satp` 初值为 0，`MODE=0`，行为与原先的 `MMU_DIRECT` 完全一致——即旧程序不受影响。

**预期结果**：`make` 成功；运行内置镜像仍 `HIT GOOD TRAP`（因为 Bare 模式下走快路径，与改动前等价）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接删掉 `isa_mmu_check`、让 `vaddr_read` 每次都调 `isa_mmu_translate`？

**答案**：性能与语义双重原因。性能上，Bare 模式（未开分页）下每次访存都走页表是无谓开销；语义上，`MMU_FAIL` 表达「分页配置非法」（如不支持的 `MODE`），与「需要翻译」是两件事，合并会丢失这层诊断。

**练习 2**：把 `#define isa_mmu_check(...) (MMU_DIRECT)` 留着、同时又在 `mmu.c` 里定义同名函数，会发生什么？

**答案**：编译报错或行为错乱。宏在预处理阶段把所有 `isa_mmu_check(...)` 调用替换成 `(MMU_DIRECT)`，`mmu.c` 里的函数定义头部也会被替换成 `int (MMU_DIRECT)(vaddr_t vaddr, int len, int type) { ... }`，导致语法错误。所以「换函数」必须先删宏。

### 4.2 MMU 翻译机制：isa_mmu_translate 与 MEM_RET 返回值编码

#### 4.2.1 概念说明

当 `isa_mmu_check` 返回 `MMU_TRANSLATE` 时，框架调用 `isa_mmu_translate(vaddr, len, type)` 真正走页表，把虚拟地址翻成物理地址。它的返回类型是 `paddr_t`（物理地址类型，见 [`include/common.h:L43`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L43)），但要表达的不止「成功给出物理地址」一种结果——还可能「翻译失败（缺页）」或「跨页」。NEMU 用一个巧妙的编码同时承载这三种结果（[`include/isa.h:L43`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L43)）：

```c
enum { MEM_RET_OK, MEM_RET_FAIL, MEM_RET_CROSS_PAGE };
```

即 `0/1/2` 三个小整数既是枚举值，又复用为 `paddr_t` 返回值的**哨兵（sentinel）**：返回 `0/1/2` 表示对应的失败/跨页语义，返回**其它值**就是真实的物理地址。这依赖一个假设：**真实的物理地址永远不会是 0、1、2**——在 NEMU 的内存布局里，物理内存从 `CONFIG_MBASE`（默认 `0x80000000`）开始，0/1/2 落在内存洞里，确实不会出现，假设成立（见 u4-l12 的 `PMEM_LEFT`）。

> 设计动机：避免给函数加一个额外的 `paddr_t *out` 出参或错误码枚举，用「偷物理地址低端几个值当哨兵」换来一个极简的 `paddr_t isa_mmu_translate(...)` 签名。这是「接口先行」风格的典型取舍。

#### 4.2.2 核心流程

`isa_mmu_translate` 的总体逻辑（伪代码，4.4 才填进真正的页表遍历）：

```text
function isa_mmu_translate(vaddr, len, type):
    if (vaddr & PAGE_MASK) + len > PAGE_SIZE:      # 跨页检测
        return MEM_RET_CROSS_PAGE                   # 交给调用者拆分
    paddr = walk_sv32(vaddr, type)                  # 走页表（4.4）
    if paddr 是缺页:
        return MEM_RET_FAIL
    return paddr                                     # 真实物理地址（>= 3）
```

调用者（`vaddr_read/write/ifetch`）据此三分支处理（详见 4.3）：成功就用返回值当物理地址、失败就抛缺页异常、跨页就拆成两段递归。

#### 4.2.3 源码精读

当前 [`src/isa/riscv32/system/mmu.c:L20-L22`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/mmu.c#L20-L22) 是 stub：

```c
paddr_t isa_mmu_translate(vaddr_t vaddr, int len, int type) {
  return MEM_RET_FAIL;
}
```

它声明在 [`include/isa.h:L47`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L47)：

```c
paddr_t isa_mmu_translate(vaddr_t vaddr, int len, int type);
```

注意它**不是** `isa_mmu_check` 那种可选宏——`isa.h` 对它直接声明函数、没有 `#ifndef` 守卫，所以每套 ISA 都必须在 `system/mmu.c` 里给出实现（四套 ISA 当前都返回 `MEM_RET_FAIL`）。这个 stub 之所以「自洽」：因为 `isa_mmu_check` 恒返回 `MMU_DIRECT`，框架根本不会走到 `isa_mmu_translate`，于是它返回什么都不影响行为——典型的「接口先行、实现后补」。

#### 4.2.4 代码实践

**实践目标**：先不写页表遍历，只把「跨页检测 + 成功/失败」骨架搭起来，验证返回值编码通路。

**操作步骤**（示例代码）：

```c
paddr_t isa_mmu_translate(vaddr_t vaddr, int len, int type) {
  // 跨页：起始地址在页内，但延伸到下一页
  if ((vaddr & PAGE_MASK) + len > PAGE_SIZE) {
    return MEM_RET_CROSS_PAGE;
  }
  // TODO: walk_sv32（4.4 实现）；暂先假装全是缺页
  return MEM_RET_FAIL;
}
```

**需要观察的现象**：由于 `isa_mmu_check` 在 4.1.4 已改成读 `satp`，而内置镜像不写 `satp`（仍 Bare），翻译函数仍不会被调用，行为不变。这一步的意义是「骨架就位」，等你写一个会 `csrw satp` 开分页的测试程序时，这个函数才真正被触发。

**预期结果**：编译通过；内置镜像仍 `HIT GOOD TRAP`。真正的页表遍历留到 4.4 与综合实践。

> 待本地验证：跨页判定的边界——`vaddr=0xFFC, len=4` 时 `(0xFFC & 0xFFF)+4 = 0xFFC+4 = 0x1000 > 0x1000` 为真（跨页）；`vaddr=0xFF8, len=4` 时 `0xFF8+4=0xFFC ≤ 0x1000` 为假（不跨页）。

#### 4.2.5 小练习与答案

**练习 1**：为什么哨兵选 `0/1/2` 而不是选 `0xFFFFFFFF` 这种大数？

**答案**：因为 `paddr_t` 在 RV32 下是 `uint32_t`，`MEM_RET_OK/FAIL/CROSS_PAGE` 是从 0 开始递增的 `enum`，天然就是 `0/1/2`；选大数反而要额外偏移。前提是物理地址不落在 `0/1/2`，而 NEMU 物理内存基址 `CONFIG_MBASE=0x80000000`，满足。

**练习 2**：`MEM_RET_OK == 0`，但「翻译成功」时函数返回的是真实物理地址而非 `MEM_RET_OK`。这二者如何区分？

**答案**：成功时返回的物理地址**不等于 0/1/2**（落在物理内存里，≥ `0x80000000`），调用者用「返回值是否 ∈ {0,1,2}」区分成功与哨兵。`MEM_RET_OK` 这个枚举名其实更多是「概念占位」，真正表示「成功」的是「返回了一个非哨兵的物理地址」。

### 4.3 跨页访问处理：MEM_RET_CROSS_PAGE 与 vaddr 读写拆分

#### 4.3.1 概念说明

一次访存可能跨越**两个页**。例如在页末尾 `vaddr=0xFFC` 处读 4 字节，前 4 字节其实落在「本页末尾 4 字节 + 下一页开头」——而这两段虚拟地址分属不同页，它们的物理地址没有连续保证，必须**各自翻译、各自读写**，再按字节序拼回。

判定跨页的数学条件（`PAGE_MASK = PAGE_SIZE - 1 = 0xFFF`，见 [`include/memory/vaddr.h:L25-L27`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/vaddr.h#L25-L27)）：

\[
\text{cross} \iff (vaddr \mathbin{\&} \text{PAGE\_MASK}) + len > \text{PAGE\_SIZE}
\]

直觉：`vaddr & PAGE_MASK` 是「页内偏移」，加上 `len` 超过页大小 `PAGE_SIZE`（=4096），就说明这次访问伸出了页边界。`isa_mmu_translate` 检测到此情况就返回 `MEM_RET_CROSS_PAGE`，把拆分责任交回给 `vaddr_read/write`——因为只有 `vaddr_*` 知道如何把一次 `len` 字节读写拆成两次并拼装。

#### 4.3.2 核心流程

`vaddr_read` 处理翻译结果的三分支（伪代码）：

```text
function vaddr_read(addr, len):
    mmu = isa_mmu_check(addr, len, MEM_TYPE_READ)
    if mmu == MMU_DIRECT:
        return paddr_read(addr, len)
    # MMU_TRANSLATE
    paddr = isa_mmu_translate(addr, len, MEM_TYPE_READ)
    if paddr == MEM_RET_FAIL:
        raise_page_fault(addr, MEM_TYPE_READ)     # 经 isa_raise_intr（u7-l21）
    if paddr == MEM_RET_CROSS_PAGE:
        len1 = PAGE_SIZE - (addr & PAGE_MASK)     # 本页内的剩余字节
        len2 = len - len1                          # 下一页的字节
        lo = vaddr_read(addr, len1)                # 递归：此时已在页内
        hi = vaddr_read(addr + len1, len2)
        return lo | (hi << (len1 * 8))             # 小端：低字节在低地址
    return paddr_read(paddr, len)                  # 成功，paddr 是真实物理地址
```

关键点有三：

1. **拆分后递归**：拆出的两段各自 `vaddr_read`，每段都在单页内，递归调用时 `isa_mmu_translate` 不再返回 `CROSS_PAGE`，而是真正走页表给出物理地址。
2. **小端拼装**：NEMU 宿主机是小端（见 u4-l12 的 `host_read`），低地址存低字节，故 `lo`（本页）放低位、`hi`（下一页）左移后放高位。
3. **缺页出口**：`MEM_RET_FAIL` 不在 `vaddr_*` 里直接终止，而是经 `isa_raise_intr` 抛缺页异常（接 u7-l21），由客机 trap 处理程序决定后续（如换页、重执行）。

`vaddr_write` 同理，只是把「读后拼」换成「拆后写」：先算 `len1/len2`，把 `data` 的低 `len1` 字节写到本页、高 `len2` 字节写到下一页。

#### 4.3.3 源码精读

当前 [`src/memory/vaddr.c:L23-L25`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L23-L25) 的 `vaddr_read` 只有一行透传：

```c
word_t vaddr_read(vaddr_t addr, int len) {
  return paddr_read(addr, len);
}
```

`vaddr_write`（[`L27-L29`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L27-L29)）、`vaddr_ifetch`（[`L19-L21`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L19-L21)）同样透传。本节要做的就是把上面三分支逻辑插进去。注意 `vaddr_ifetch` 也要改：取指同样可能落在未映射 / 无执行权限的页上，应走 `MEM_TYPE_IFETCH` 检查。不过 RV32 指令 4 字节且按 4 对齐，取指不会跨页（4 字节对齐落在 4KB 页内），所以 `ifetch` 不会触发 `CROSS_PAGE`，但仍需 `DIRECT/TRANSLATE/FAIL` 三分支。

> `MEM_RET_CROSS_PAGE` 这个枚举值当前在全仓库**只声明、未使用**（grep 全仓只命中 [`include/isa.h:L43`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L43) 一处），是「为分页实现预留」的典型痕迹——本节正是把它用起来。

#### 4.3.4 代码实践

**实践目标**：把 `vaddr_read` 改造为支持 check/translate/跨页三分支（页表遍历仍用 4.4 的实现，这里先把调度骨架写好）。

**操作步骤**（示例代码，写入 `src/memory/vaddr.c`）：

```c
word_t vaddr_read(vaddr_t addr, int len) {
  int mmu = isa_mmu_check(addr, len, MEM_TYPE_READ);
  if (mmu == MMU_DIRECT) return paddr_read(addr, len);

  paddr_t paddr = isa_mmu_translate(addr, len, MEM_TYPE_READ);
  if (paddr == MEM_RET_FAIL) {
    isa_raise_intr(/* load page fault */ 13, /* epc */ cpu.pc);
    return 0;
  }
  if (paddr == MEM_RET_CROSS_PAGE) {
    int len1 = PAGE_SIZE - (addr & PAGE_MASK);
    int len2 = len - len1;
    word_t lo = vaddr_read(addr, len1);
    word_t hi = vaddr_read(addr + len1, len2);
    return lo | (hi << (len1 * 8));
  }
  return paddr_read(paddr, len);
}
```

**需要观察的现象**：在 Bare 模式（`satp.MODE=0`）下，`isa_mmu_check` 返回 `MMU_DIRECT`，第一行即返回，与改造前完全等价——旧程序不受影响。

**预期结果**：`make` 通过；内置镜像仍 `HIT GOOD TRAP`。`vaddr_write` / `vaddr_ifetch` 同理改造（store page fault 编号为 15、instruction page fault 为 12）。

> 待本地验证：写一个故意跨页读的小程序（在页边界 `0x...FFC` 读 4 字节）开分页跑，确认拆分后读到正确的 4 字节。无分页测试程序时此步可暂缓，待 4.4 完成后统一验证。

#### 4.3.5 小练习与答案

**练习 1**：跨页拆分时，为什么是 `len1 = PAGE_SIZE - (addr & PAGE_MASK)` 而不是 `len1 = len / 2`？

**答案**：因为「本页内的剩余字节」由起始地址的页内偏移决定——`addr & PAGE_MASK` 是页内偏移，`PAGE_SIZE` 减它就是本页还剩多少字节能读。`len/2` 与页边界无关，会把本可在本页读完的字节错误地拆到下一页。

**练习 2**：为什么跨页拼装用 `lo | (hi << (len1*8))` 而不是 `hi | (lo << (len2*8))`？

**答案**：小端字节序下，低地址存低位。本页（`addr`）是低地址，放低字节 `lo`；下一页（`addr+len1`）是高地址，放高字节 `hi`，故 `hi` 左移到高位。若写成后者则高低位颠倒，读出的值错。

### 4.4 SV32 页表遍历设计

#### 4.4.1 概念说明

本模块是分页的「心脏」：给定一个虚拟地址，沿着 `satp` 指向的根页表，逐级读 PTE，最终合成物理地址。SV32 是**两级**页表：

- 第一级（根表）由 `satp.PPN` 指向，用 `VPN[1]`（VA 的 bits 31..22）索引；
- 第二级表由第一级叶子或非叶 PTE 的 `PPN` 指向，用 `VPN[0]`（bits 21..12）索引；
- 叶子 PTE 的 `PPN` 与 VA 的 `offset`（bits 11..0）合成最终物理地址。

每级 PTE 的低 8 位是权限标志。**非叶 PTE**（`R=W=0` 且 `X` 任意但通常 0）指向下一级表；**叶子 PTE**（`R` 或 `X` 为 1）直接给出物理页映射，并在 `R/W/X` 上标注该页权限。翻译时还要按 `type` 查权限：取指要 `X`、读要 `R`、写要 `W`，不符即缺页。

#### 4.4.2 核心流程

SV32 页表遍历（伪代码，参考 RISC-V 特权规范）：

```text
function walk_sv32(vaddr, type):
    base = (satp.PPN) << 12                 # 根表物理基址
    vpn1 = BITS(vaddr, 31, 22)              # 第一级索引
    vpn0 = BITS(vaddr, 21, 12)              # 第二级索引
    offset = vaddr & PAGE_MASK

    # 第一级
    pte1 = paddr_read(base + vpn1 * 4, 4)   # 每个 PTE 4 字节
    if (pte1 & V) == 0: return MEM_RET_FAIL # 无效 → 缺页
    if (pte1 & R) or (pte1 & X):            # 叶子（superpage，本讲可暂不处理对齐检查）
        ...                                 # 超级页，简化版可不支持
    # 非叶 → 进第二级
    l2base = BITS(pte1, 31, 10) << 12       # PTE.PPN << 12
    pte2 = paddr_read(l2base + vpn0 * 4, 4)
    if (pte2 & V) == 0: return MEM_RET_FAIL
    if not is_leaf(pte2):     return MEM_RET_FAIL  # SV32 两级到底还非叶 → 非法
    # 权限检查
    if type == MEM_TYPE_IFETCH and (pte2 & X) == 0: return MEM_RET_FAIL
    if type == MEM_TYPE_READ   and (pte2 & R) == 0: return MEM_RET_FAIL
    if type == MEM_TYPE_WRITE  and (pte2 & W) == 0: return MEM_RET_FAIL
    # 合成物理地址
    paddr = (BITS(pte2, 31, 10) << 12) | offset
    return paddr
```

物理地址合成公式（叶子在第二级，即 4KB 页）：

\[
\text{paddr} = (\text{PTE.PPN} \ll 12) \mathbin{|} \text{offset}, \quad \text{其中 PTE.PPN} = \text{BITS}(\text{PTE}, 31, 10)
\]

注意遍历里**读 PTE 用的是 `paddr_read`** 而非 `vaddr_read`——页表本身存在物理内存里，访问它不能再走翻译（否则无限递归）。这是「翻译的根基落在物理内存上」的体现，也再次说明 u4-l12 的 `paddr_*` 是整座大厦的地基。

> 关于超级页（superpage）：SV32 允许第一级 PTE 直接是叶子，映射 4MB 大页。教学版可先不支持（遇到第一级叶子即视为非法或缺页），只支持 4KB 页，待基础跑通再扩展。

#### 4.4.3 源码精读

遍历要用到的位操作工具在 [`include/macro.h:L86-L88`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L86-L88)：

```c
#define BITMASK(bits) ((1ull << (bits)) - 1)
#define BITS(x, hi, lo) (((x) >> (lo)) & BITMASK((hi) - (lo) + 1)) // 类似 Verilog 的 x[hi:lo]
#define SEXT(x, len) ({ ... })                                       // 符号扩展
```

`BITS` 与 u5-l16 解码 RV32 立即数（`immI = SEXT(BITS(i,31,20),12)`，见 [`src/isa/riscv32/inst.c:L32-L34`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L32-L34)）用的是同一个宏——切 `VPN`、读 PTE 字段同理。另外 [`include/macro.h:L93`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L93) 的 `PG_ALIGN`（`__attribute((aligned(4096)))`）可用于客机程序里把页表按 4KB 对齐。

PTE 的权限位定义（`V/R/W/X/U/G/A/D`，bits 0..7）与 2.2 的公式一致；本讲按位与即可取用，例如 `pte & 0x1` 取 `V`、`pte & 0x2` 取 `R`、`pte & 0x4` 取 `W`、`pte & 0x8` 取 `X`。

当前 [`src/isa/riscv32/system/mmu.c:L20-L22`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/mmu.c#L20-L22) 仍是 stub，遍历逻辑需你写入此处（见 4.4.4）。

#### 4.4.4 代码实践

**实践目标**：实现 `isa_mmu_translate` 的 SV32 两级页表遍历。

**操作步骤**（示例代码，写入 `src/isa/riscv32/system/mmu.c`）：

```c
static paddr_t walk_sv32(vaddr_t vaddr, int type) {
  word_t satp = cpu.satp;
  paddr_t base = BITS(satp, 21, 0) << 12;          // satp.PPN << 12
  uint32_t vpn1 = BITS(vaddr, 31, 22);
  uint32_t vpn0 = BITS(vaddr, 21, 12);

  // 第一级
  uint32_t pte1 = paddr_read(base + vpn1 * 4, 4);
  if ((pte1 & 0x1) == 0) return MEM_RET_FAIL;       // V=0
  // 简化：不支持超级页，第一级必须是非叶（R=W=X=0）
  if ((pte1 & 0xA) != 0) return MEM_RET_FAIL;       // R 或 X 置位视为超级页，暂不支持

  // 第二级
  paddr_t l2base = BITS(pte1, 31, 10) << 12;
  uint32_t pte2 = paddr_read(l2base + vpn0 * 4, 4);
  if ((pte2 & 0x1) == 0) return MEM_RET_FAIL;       // V=0
  if ((pte2 & 0xA) == 0) return MEM_RET_FAIL;       // 非叶（R=X=0）→ 非法

  // 权限检查
  if (type == MEM_TYPE_IFETCH && (pte2 & 0x8) == 0) return MEM_RET_FAIL; // X
  if (type == MEM_TYPE_READ   && (pte2 & 0x2) == 0) return MEM_RET_FAIL; // R
  if (type == MEM_TYPE_WRITE  && (pte2 & 0x4) == 0) return MEM_RET_FAIL; // W

  return (BITS(pte2, 31, 10) << 12) | (vaddr & PAGE_MASK);
}

paddr_t isa_mmu_translate(vaddr_t vaddr, int len, int type) {
  if ((vaddr & PAGE_MASK) + len > PAGE_SIZE) return MEM_RET_CROSS_PAGE;
  return walk_sv32(vaddr, type);
}
```

**需要观察的现象**：你需要一个会开启分页的客机测试程序——它分配并对齐两张页表（根表 + 二级表）、用 `csrw satp` 设 `MODE=1` 与根表 `PPN`、然后做受控访存。开启后，`vaddr_read` 走 `MMU_TRANSLATE` 分支进入本函数。

**预期结果**：合法映射的访存得到正确数据；访问未映射页或越权（如对只读页写）触发 `MEM_RET_FAIL` → `isa_raise_intr` 抛缺页异常（接 u7-l21）。

> 待本地验证：由于内置镜像不开启分页，本实践需自备测试程序或使用 PA3 提供的分页测试用例；若无现成用例，可先只验证 Bare 模式不回归，遍历逻辑留待接入测试程序后确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么读 PTE 用 `paddr_read` 而不是 `vaddr_read`？

**答案**：页表存在物理内存里，其地址（`satp.PPN<<12`、PTE.PPN<<12）已是物理地址。若用 `vaddr_read` 会再次触发翻译，而翻译又要读页表，形成无限递归。物理地址访问必须直连物理内存。

**练习 2**：若第一级 PTE 是叶子（超级页），按本讲的简化实现会怎样？

**答案**：示例代码用 `(pte1 & 0xA) != 0`（R 或 X 置位）识别叶子并返回 `MEM_RET_FAIL`，即把超级页当作「不支持」处理。这是教学取舍：先支持 4KB 页跑通主链路，超级页可作为后续扩展。

### 4.5 TLB 加速设计（可选）

#### 4.5.1 概念说明

上面 `walk_sv32` 每次翻译要读**两次**物理内存（第一级 PTE + 第二级 PTE）。一次 `lw` 本只读 4 字节，却额外付出 8 字节的页表访问——访存放大了 3 倍。真实 CPU 用 **TLB（Translation Lookaside Buffer，旁路翻译缓冲）** 缓解：它是一个缓存，记录「近期用过的 VPN → 物理页号」映射，命中时直接给出物理地址、跳过页表遍历。

TLB 是「正确性之外的加速器」——没有它程序也能跑（每次都走页表），只是慢。所以教学版 NEMU 把它列为可选项：先把功能跑通，再考虑加 TLB。这也是 u4-l13 接口里 `isa_mmu_translate` 留出的扩展空间。

#### 4.5.2 核心流程

带 TLB 的翻译流程（伪代码）：

```text
function isa_mmu_translate(vaddr, len, type):
    if 跨页: return MEM_RET_CROSS_PAGE
    vpn = vaddr >> 12
    entry = tlb_lookup(vpn)
    if entry 命中 且 权限满足 type:
        return (entry.ppn << 12) | (vaddr & PAGE_MASK)   # 快路径
    # 未命中：走页表
    paddr = walk_sv32(vaddr, type)
    if paddr 不是缺页:
        tlb_fill(vpn, paddr >> 12, type)                 # 回填
    return paddr
```

关键设计点有二：其一，**TLB 以页为粒度**，键是 `VPN = vaddr >> 12`，值是物理页号 `PPN`，页内偏移不进 TLB；其二，**一致性**——当 `satp` 被改写（切换地址空间）或页表被改写时，TLB 里旧映射会失效，必须冲刷（flush）。教学版可简化为：`csrw satp` 时整体清空 TLB。

#### 4.5.3 源码精读

NEMU 当前没有 TLB 相关源码——`isa_mmu_translate`（[`src/isa/riscv32/system/mmu.c:L20-L22`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/mmu.c#L20-L22)）直接是 stub，框架也未预留 TLB 结构。这印证了「TLB 是可选扩展」：接口签名 `paddr_t isa_mmu_translate(vaddr, len, type)` 不变，TLB 完全是函数内部实现细节，加不加都不影响调用者（`vaddr_read/write` 的三分支处理照旧）。

可以借用 [`include/macro.h:L86`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L86) 的 `BITMASK` 做一个简单的直接映射 TLB（如 256 项、`vpn & 0xFF` 索引、每项存「有效位 + vpn + ppn + 权限」）。

#### 4.5.4 代码实践

**实践目标**：在 4.4 的遍历之上叠加一个最小直接映射 TLB，对比开关前后的访存次数。

**操作步骤**（示例代码，思路）：

1. 在 `mmu.c` 顶部定义 TLB 表：

   ```c
   #define TLB_SIZE 256
   static struct { bool valid; vaddr_t vpn; paddr_t ppn; } tlb[TLB_SIZE];
   ```

2. 在 `isa_mmu_translate` 开头先查 TLB，命中且权限满足则直接合成返回；未命中则调 `walk_sv32`，成功后回填。
3. 在 `csrw satp` 的处理路径（写 `satp` 的指令实现里）整体清空 `tlb[]` 的 `valid`。

**需要观察的现象**：对一段密集访存的程序，开启 TLB 后 `g_nr_guest_inst` 不变但宿主耗时下降；关 TLB 则每次访存多两次 `paddr_read`（读两级 PTE）。

**预期结果**：功能等价（程序结果不变），模拟频率（`statistic` 打印的 inst/s，见 u3-l9）提升。

> 待本地验证：实际加速比取决于测试程序的访存模式；教学版 TLB 不做权限缓存细节时，需保证权限检查仍走 `walk_sv32` 或在 TLB 项里存权限位，否则会漏掉缺页。

#### 4.5.5 小练习与答案

**练习 1**：TLB 为什么以页（`vpn = vaddr >> 12`）为粒度，而不是缓存「完整虚拟地址 → 物理地址」？

**答案**：同一页内所有地址共享同一个页表项、同一个物理页号，只有页内偏移不同。以页为粒度，一项 TLB 条目覆盖 4KB 内所有地址，命中率远高于逐地址缓存；且偏移部分不需翻译，直接拼回即可。

**练习 2**：如果客机程序改写了页表但没冲刷 TLB，会发生什么？

**答案**：TLB 里残留旧映射，后续访存会用过期的「VPN → PPN」翻译，读到/写错物理页，造成数据错乱。这就是为什么改写页表或 `csrw satp` 后必须 flush TLB——一致性是 TLB 设计绕不开的责任。

## 5. 综合实践

把五个模块串起来，完成 riscv32 SV32 分页的端到端实现。目标：让一个开启分页的客机程序能正确访存，并对越权访问抛出缺页异常。

建议步骤：

1. **加 `satp` 字段**：在 [`src/isa/riscv32/include/isa-def.h:L21-L24`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24) 的 `CPU_state` 里加 `word_t satp`，并实现 `csrrw/csrrs` 对 `satp` 的读写（若 u7-l21 未做 CSR 指令则一并补上）。注意 `satp` 写入后应冲刷 TLB（若你做了 4.5）。

2. **改 `isa_mmu_check` 为函数**：删 [`src/isa/riscv32/include/isa-def.h:L31`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L31) 的宏，在 `mmu.c` 实现按 `satp.MODE` 返回 `MMU_DIRECT/TRANSLATE/FAIL`（4.1.4）。

3. **实现 `isa_mmu_translate`**：SV32 两级遍历 + 跨页检测（4.4.4 + 4.2.4）。

4. **改造 `vaddr_*`**：在 [`src/memory/vaddr.c:L19-L29`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L19-L29) 的三个函数里插入 `DIRECT/TRANSLATE/FAIL` + `CROSS_PAGE` 三分支（4.3.4）。

5. **接缺页异常**：`MEM_RET_FAIL` 时按 `type` 选 `mcause`（load=13 / store=15 / instruction=12）调 `isa_raise_intr`（u7-l21），`epc` 传引发访问的指令 `pc`。

6. **验证**：用 PA3 提供的分页测试用例（或自写一个：分配对齐页表、`csrw satp` 开 SV32、做受控读写）。合法访问应得到正确数据；对未映射页访问应触发缺页、客机 trap 处理程序能捕获；运行结束 `HIT GOOD TRAP`。

7. **回归**：确认 Bare 模式（不写 `satp`）下内置镜像与已有 PA1/PA2 测试全部不回归——这是「分页是可选叠加层」的底线。

> 待本地验证：第 6 步依赖具体的分页测试程序，若无现成用例，至少保证第 7 步的 Bare 模式回归通过，并把分页功能的端到端验证标注为「待接入测试用例后确认」。

## 6. 本讲小结

- `isa_mmu_check` 返回 `MMU_DIRECT/TRANSLATE/FAIL`，决定每次访存「要不要翻译」；当前 riscv32 用宏把它短路成 `MMU_DIRECT`，靠 [`include/isa.h:L44`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L44) 的 `#ifndef` 守卫实现「ISA 用宏覆盖、否则走函数声明」的可选机制——要开分页，先删宏换函数。
- `isa_mmu_translate` 返回 `paddr_t`，复用 `0/1/2` 作哨兵编码 `MEM_RET_OK/FAIL/CROSS_PAGE`，依赖物理地址不落在 0/1/2；当前是 stub（[`mmu.c:L20-L22`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/mmu.c#L20-L22)），因 `check` 恒 `DIRECT` 而不会被调用，自洽。
- 跨页判定为 `(vaddr & PAGE_MASK) + len > PAGE_SIZE`，返回 `MEM_RET_CROSS_PAGE` 后由 `vaddr_read/write` 拆成两段递归翻译、小端拼装；该枚举此前全仓未用，本讲把它接通。
- SV32 是两级页表：`satp.PPN<<12` 是根表基址，按 `VPN[1]`、`VPN[0]` 逐级读 4 字节 PTE，叶子 PTE 的 `PPN<<12 | offset` 合成物理地址，并按 `type` 查 `R/W/X` 权限，不符即缺页；读 PTE 必须用 `paddr_read` 以免递归。
- `vaddr_ifetch/read/write` 当前透传 `paddr_*`（[`vaddr.c:L19-L29`](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L19-L29)），是本讲改造主战场；缺页经 `isa_raise_intr` 出口，与 u7-l21 的异常机制合流。
- TLB 是可选加速器：以页为粒度缓存「VPN→PPN」，命中跳过页表遍历；改 `satp` 或页表后须 flush 以保一致性。教学版可不做，功能不受影响。

## 7. 下一步学习建议

- **跑通分页测试**：接入 PA3 的分页测试用例，验证 SV32 端到端正确性；若开了差分测试（u8-l24），可对比 spike 作为 REF 的翻译结果，缺页行为对齐是差分测试的重点难点。
- **读 u8-l24 差分测试**：分页与缺页会让 DUT/REF 的指令边界出现差异（如缺页异常使某条指令在 REF 侧被「打包」），`difftest_skip_ref` 与 `ref_difftest_raise_intr` 的协作在分页场景下尤其关键，建议结合本讲的缺页出口一起读。
- **读 u8-l25 指令追踪**：分页下若翻译出错，`itrace`（`logbuf` 组装的 pc/指令字节/disassemble）是定位「哪条指令、哪个虚拟地址」的第一现场，把本讲的 `MEM_RET_FAIL` 与 `logbuf` 一起看排错效率最高。
- **扩展超级页与权限细节**：在 4KB 页跑通后，可尝试支持 SV32 4MB 超级页（第一级叶子），并细化 `U` 位在 M/U 模式下的行为，体会真实 MMU 的完整语义。
