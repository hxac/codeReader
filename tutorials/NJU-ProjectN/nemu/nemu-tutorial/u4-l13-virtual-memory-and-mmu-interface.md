# 虚拟内存 vaddr 与 MMU 接口

## 1. 本讲目标

上一讲 u4-l12 解决了「物理内存 `paddr` 怎么模拟」：一块字节数组 + 三分支路由（pmem/mmio/越界）。但客机 CPU 真正发出的地址并不是物理地址，而是**虚拟地址**（virtual address）。本讲就在 `paddr` 之上叠加一层 `vaddr`，并讲解 NEMU 为「分页翻译」预留的整套 MMU 接口。

学完本讲，你应当能够：

- 说清 **虚拟地址 `vaddr`** 与 **物理地址 `paddr`** 的关系，以及当前「未开启分页时 `vaddr == paddr`、`vaddr.c` 只是透传」这一现状。
- 解释为什么取指走 `vaddr_ifetch`、数据走 `vaddr_read`/`vaddr_write` 三个独立函数（而不是合一个）。
- 掌握 `isa.h` 定义的 MMU 三件套：`isa_mmu_check`（要不要翻译）、`isa_mmu_translate`（怎么翻译）、以及三组配套枚举。
- 区分两组易混枚举：`MMU_DIRECT/TRANSLATE/FAIL`（check 的输出）与 `MEM_RET_OK/FAIL/CROSS_PAGE`（translate 的输出）。
- 理解 `MEM_RET_CROSS_PAGE` 如何指导跨页访问的拆分实现，并能写出「check → translate → paddr_read」的调用链伪代码。
- 体会 NEMU「**接口先行、实现后补**」的工程风格——本讲涉及的 MMU 接口当前几乎全是 stub（占位），真正实现在 u7-l22。

本讲是内存系统单元（U4）的上层，也是高级单元 U7（中断异常与分页）的直接前置：分页机制（u7-l22）就建立在本讲描述的接缝之上。

## 2. 前置知识

在进入源码前，先用大白话把几个概念讲清楚。

**虚拟地址与物理地址。** 真实 CPU 里，程序发出的地址是虚拟地址，需要经过 MMU（Memory Management Unit，内存管理单元）翻译成物理地址才能访问 DRAM。引入这层间接的好处是：进程隔离、地址空间虚拟化、按需调页。NEMU 作为全系统模拟器，也要忠实呈现这层翻译——但当前阶段还没实现分页，于是虚拟地址在数值上就等于物理地址，翻译变成「不翻译」。

**分页与页表。** 分页（paging）是把地址空间切成固定大小的「页」（RISC-V/x86 通常是 4 KB），用一张「页表」记录「虚拟页 → 物理页」的映射。翻译一次地址就是查一次页表：取虚拟地址的高位作页表索引，查到物理页基址，再拼上虚拟地址的低位（页内偏移）得到物理地址。

**跨页问题。** 一次访存可能读写 1/2/4/8 字节。如果这次访问恰好横跨两个虚拟页的边界，那么它的字节落在两个不同的物理页上——一次页表翻译只能给出一个物理页基址，无法用一次连续的物理读写完成。这就需要把访问拆成两段，分别翻译、分别读写、再按字节序拼回去。这正是 `MEM_RET_CROSS_PAGE` 要表达的语义。

**「接口先行、实现后补」。** NEMU 的 PA 作业风格是：先把整套接口的「形状」用枚举、函数声明、占位实现定下来，让你在后续 PA 里只需填实现、不改框架。本讲的 MMU 接口就是典型——函数都声明好了、枚举都列好了、stub 都返回安全的失败值，但默认配置下它们根本不会被调用。

**承接前两讲。** 本讲建立在 u4-l12 与 u3-l10 之上：
- u4-l12 讲过 `paddr_read`/`paddr_write` 的 pmem→mmio→越界三分支（[src/memory/paddr.c:L53-L64](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L53-L64)），本讲的 `vaddr_*` 最终都落到它们上面。
- u3-l10 讲过取指走 `inst_fetch` → `vaddr_ifetch` 的专用路径（[include/cpu/ifetch.h:L20-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/ifetch.h#L20-L24)），本讲会解释它为何与数据读 `vaddr_read` 分开。
- `vaddr_t`/`paddr_t` 的类型宽度来自 [include/common.h:L38-L44](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L38-L44)，u1-l4 已讲过。

## 3. 本讲源码地图

本讲围绕四个核心文件，并涉及若干周边文件：

| 文件 | 作用 |
| --- | --- |
| [src/memory/vaddr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c) | 虚拟内存层实现：`vaddr_ifetch`/`vaddr_read`/`vaddr_write`，当前直接转发 `paddr_*`。 |
| [include/memory/vaddr.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/vaddr.h) | 虚拟内存对外接口与页大小常量 `PAGE_SHIFT`/`PAGE_SIZE`/`PAGE_MASK`。 |
| [include/isa.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h) | MMU 抽象契约：`isa_mmu_check`/`isa_mmu_translate` 声明，以及三组枚举（MMU 模式、MEM_TYPE、MEM_RET）。 |
| [src/isa/riscv32/system/mmu.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/mmu.c) | riscv32 的 `isa_mmu_translate` 占位实现（stub）。 |
| [src/isa/riscv32/include/isa-def.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h) | riscv32 把 `isa_mmu_check` 用宏定义为永远返回 `MMU_DIRECT`。 |
| [src/isa/x86/include/isa-def.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/include/isa-def.h) | x86 同样用宏把 `isa_mmu_check` 定义为 `MMU_DIRECT`，可作对比。 |
| [include/cpu/ifetch.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/ifetch.h) | `inst_fetch` 调用 `vaddr_ifetch`，是取指路径与虚拟内存的接合点。 |
| [src/memory/paddr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c) | `paddr_read`/`paddr_write`，`vaddr_*` 转发的终点（u4-l12 已精读）。 |

## 4. 核心概念与源码讲解

### 4.1 vaddr 读写转发：虚拟地址层当前为何是「透传」

#### 4.1.1 概念说明

NEMU 的内存被分成两层：

- **物理内存层（paddr）**：u4-l12 讲的那块 `pmem` 字节数组 + mmio 路由，是「真正存字节」的地方。
- **虚拟内存层（vaddr）**：客机 CPU 看到的地址入口。CPU 取指、load/store 都先到这里。

为什么要多一层？因为一旦开启分页，`vaddr` 不能直接当 `paddr` 用，要先经 MMU 翻译。所以框架预先把这一层放进来，让所有访存入口统一走 `vaddr_*`，将来在 `vaddr_*` 里插入翻译逻辑即可，不必改动取指与译码代码。

当前阶段**没有实现分页**，虚拟地址在数值上就等于物理地址，于是 `vaddr_*` 三个函数的全部工作就是原样转发给 `paddr_*`。这一层暂时是个「空壳」，但它的存在是分页的立足点。

`vaddr` 层提供三个入口而不是一个，对应三种访存语义：

| 函数 | 用途 | 调用者 |
| --- | --- | --- |
| `vaddr_ifetch` | 取指令 | `inst_fetch`（取指专用） |
| `vaddr_read` | 读数据 | load 类指令、表达式求值的内存解引用 |
| `vaddr_write` | 写数据 | store 类指令 |

分开的原因是：将来开启分页后，取指与数据读写的**权限检查不同**（一段内存可读不一定可执行），需要用 `type` 参数区分。即使现在三者实现一样，接口也要先分开占位。

#### 4.1.2 核心流程

当前数据流是一条直通的转发链：

```
取指:  inst_fetch(pc) → vaddr_ifetch(pc)  → paddr_read(pc)  → pmem/mmio
读:    load 指令        → vaddr_read(addr) → paddr_read(addr) → pmem/mmio
写:    store 指令       → vaddr_write(addr,data) → paddr_write(addr,data) → pmem/mmio
```

三条链当前都**没有任何翻译**，`vaddr` 层是透明的。翻译逻辑要等 4.2~4.5 的 MMU 接口被真正接通（u7-l22）才会插入。

#### 4.1.3 源码精读

虚拟内存层目前只有这 30 行：

[src/memory/vaddr.c:L19-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L19-L29) —— `vaddr_ifetch`/`vaddr_read`/`vaddr_write` 三个函数，函数体各自只有一行 `return paddr_read(...)` 或 `paddr_write(...)`，是纯粹的透传。注意三者都把 `addr` 原样传给 `paddr_*`——因为当前 `vaddr == paddr`。

接口声明与页大小常量在头文件：

[include/memory/vaddr.h:L21-L23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/vaddr.h#L21-L23) —— 三个函数的声明，签名一致：地址 `vaddr_t addr`、宽度 `int len`、（写还有）数据 `word_t data`。返回 `word_t`（读出的值）。

取指路径如何衔接到 `vaddr_ifetch`：

[include/cpu/ifetch.h:L20-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/ifetch.h#L20-L24) —— `inst_fetch` 调 `vaddr_ifetch(*pc, len)` 取指令字节，再把 `*pc += len` 推进 PC（u3-l10 讲过 `snpc` 的推进就发生在这里）。注意它走的是 `vaddr_ifetch` 而非 `vaddr_read`——这正是「取指与数据读分开」的体现。

`vaddr_*` 转发的终点是 u4-l12 讲过的物理总线：

[src/memory/paddr.c:L53-L58](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L53-L58) —— `paddr_read` 的 pmem→mmio→越界三分支，是 `vaddr_read`/`vaddr_ifetch` 的最终落点。

类型上，`vaddr_t` 与 `paddr_t` 在 32 位客机下都是 `uint32_t`，但语义不同——一个是客机 CPU 的虚拟地址，一个是物理内存地址，只是当前恰好相等：

[include/common.h:L42-L43](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L42-L43) —— `typedef word_t vaddr_t;` 与 `typedef MUXDEF(PMEM64, uint64_t, uint32_t) paddr_t;`。`vaddr_t` 直接复用 `word_t`，`paddr_t` 则可能更宽（当 `CONFIG_MBASE + CONFIG_MSIZE > 4GB` 时启用 `PMEM64`，u4-l12 讲过）。

#### 4.1.4 代码实践

**目标：** 直观确认「`vaddr` 层目前是透传」，并观察每次访存的地址与宽度。

**步骤：**

1. 阅读 [src/memory/vaddr.c:L19-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L19-L29)，确认三个函数体都只是一行转发。
2. 在 `vaddr_read` 里临时加一行打印（**示例代码，仅供观察，验证后请还原**）：

   ```c
   word_t vaddr_read(vaddr_t addr, int len) {
     printf("[vaddr] read addr=" FMT_WORD " len=%d\n", (word_t)addr, len);  /* 示例代码 */
     return paddr_read(addr, len);
   }
   ```

3. 重新 `make` 并运行内置镜像（先确保已按 u1-l3 删除 `welcome()` 里的 `assert(0)`），观察打印。

**需要观察的现象：** 每条 load 指令执行时打印一组 `addr`/`len`，`len` 通常为 1/2/4；取指不会经过这里（取指走 `vaddr_ifetch`）。

**预期结果：** 内置镜像的 `lbu a0, 16(t0)`（u4-l12 提到）会触发一次 `vaddr_read(0x80000010, 1)`，打印里能看到这条。**待本地验证**：具体地址取决于 `CONFIG_MBASE`。

> 本地验证提示：验证完务必删除调试打印，避免污染后续讲义的行为与 itrace 输出。

#### 4.1.5 小练习与答案

**练习 1：** 既然当前 `vaddr_ifetch` 与 `vaddr_read` 实现完全一样，为什么不合并成一个函数？

**参考答案：** 因为它们的**语义不同**，将来分页时要分别做权限检查：取指要求「可执行」，数据读要求「可读」。接口预先分开，译码代码里 `inst_fetch` 调 `vaddr_ifetch`、load 调 `vaddr_read` 就不用在分页实现时再改调用点。这是「接口形状先定、实现后补」的体现。

**练习 2：** `vaddr_write(addr, len, data)` 把 `data` 类型定为 `word_t`。如果客机是 32 位、`len=1`，`data` 的高 24 位会被写进内存吗？

**参考答案：** 不会。`vaddr_write` → `paddr_write` → `pmem_write` → `host_write`，`host_write` 按 `len` 选 `uint8_t`/`uint16_t`/`uint32_t` 解引用（u4-l12 的 [include/memory/host.h:L31-L39](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/host.h#L31-L39)），`len=1` 时只写最低字节，高位被类型转换截断。`word_t` 只是承载容器，实际写入宽度由 `len` 决定。

---

### 4.2 isa_mmu_check 接口：先问「要不要翻译」

#### 4.2.1 概念说明

在调用 `isa_mmu_translate` 走页表之前，框架需要先快速判定**这次访问到底需不需要翻译**。这就是 `isa_mmu_check` 的职责：它是一个 per-access（每次访存都调）的轻量判定，返回三种结果（见 4.3）：

- **不需要翻译**（`MMU_DIRECT`）：分页关闭，或该地址段不参与翻译，`vaddr` 直接当 `paddr` 用。
- **需要翻译**（`MMU_TRANSLATE`）：分页开启且该地址要走页表，接下来调 `isa_mmu_translate`。
- **不可翻译**（`MMU_FAIL`）：访问本身在翻译前就非法（如模式不匹配），应抛异常，不访问内存。

为什么要把「判定」和「翻译」拆成两个函数？因为判定逻辑通常是 O(1) 的（看一个状态位，如 RISC-V 的 `satp` 模式位、x86 的 `CR0.PG`），而翻译逻辑要遍历多级页表、开销大。把 O(1) 的判定单拎出来，可以让「分页关闭」这一最常见情况用一次比较就跳过整套页表遍历。

#### 4.2.2 核心流程

```
isa_mmu_check(vaddr, len, type) ──┬── MMU_DIRECT    → vaddr 直接当 paddr，走 paddr_*
                                  ├── MMU_TRANSLATE → 调 isa_mmu_translate(...) 翻译
                                  └── MMU_FAIL      → 抛异常，不访存
```

`type` 参数取自 4.5 讲的 `MEM_TYPE_IFETCH/READ/WRITE`，让 check 能按访存种类做不同判定（例如某段内存可读但不可执行）。

#### 4.2.3 源码精读

`isa.h` 用一个巧妙的 `#ifndef` 守卫声明 `isa_mmu_check`：

[include/isa.h:L44-L47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L44-L47) —— `#ifndef isa_mmu_check` 包住函数声明：如果某个 ISA 在自己的 `isa-def.h` 里把 `isa_mmu_check` **定义成了宏**，那么这里的函数声明就被跳过，调用处直接展开成宏体；如果 ISA 没定义这个宏，则保留这个真实函数声明，由 ISA 提供函数实现。这是「宏覆盖函数」的可选优化接缝。

riscv32 选择了「宏」这条路：

[src/isa/riscv32/include/isa-def.h:L31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L31) —— `#define isa_mmu_check(vaddr, len, type) (MMU_DIRECT)`：riscv32 当前未实现分页，于是把 check 直接定义为常量 `MMU_DIRECT`。任何调用 `isa_mmu_check(...)` 的地方在编译期就被替换成 `(MMU_DIRECT)`，编译器还能据此做常量传播与死代码消除——`switch` 里 `case MMU_TRANSLATE`/`case MMU_FAIL` 的分支会被整段优化掉，零运行时开销。

x86 也一样：

[src/isa/x86/include/isa-def.h:L52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/include/isa-def.h#L52) —— x86 的 `isa_mmu_check` 同样宏定义为 `MMU_DIRECT`。事实上全仓库四个 ISA（riscv32/x86/mips32/loongarch32r）当前都这么写。

注意对比：`isa_mmu_translate` **没有** `#ifndef` 守卫（见 4.4.3 的 [include/isa.h:L47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L47)），它始终是一个真实函数。这说明框架的设计取舍：check 在热路径、当前又是常量，用宏最划算；translate 逻辑复杂、只在需要时才调，用函数更自然。

#### 4.2.4 代码实践

**目标：** 读通 `isa_mmu_check` 宏返回 `MMU_DIRECT` 的含义，并理解「宏覆盖函数」接缝。

**步骤：**

1. 阅读 [src/isa/riscv32/include/isa-def.h:L31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L31)，确认 riscv32 把 `isa_mmu_check` 定义为恒返回 `MMU_DIRECT`。
2. 对照 [include/isa.h:L44-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L44-L46) 的 `#ifndef`，理解：因为 riscv32 定义了宏，`isa.h` 里的函数声明被跳过，全仓库**不存在** `isa_mmu_check` 的函数定义（可以 `grep` 验证）。
3. **思考实验**：将来要按 `satp` 寄存器判定分页是否开启（u7-l22），应如何改？答案是：删掉 `isa-def.h:L31` 的宏定义，转而在 `src/isa/riscv32/system/mmu.c` 里写一个真实的 `isa_mmu_check` 函数，根据 `cpu.satp` 的模式位返回 `MMU_DIRECT`（bare 模式）或 `MMU_TRANSLATE`（SV32 模式）。

**需要观察的现象：** `grep -rn "isa_mmu_check" src/` 只在 `isa-def.h` 里命中宏定义，没有 `.c` 文件提供函数体。

**预期结果：** 确认当前 riscv32 下 `isa_mmu_check` 是编译期常量 `MMU_DIRECT`，因此 `vaddr_*` 里的翻译分支永远不会被执行——这与 4.1「透传」现状自洽。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `isa_mmu_check` 用「宏」实现，而 `isa_mmu_translate` 用「函数」实现？

**参考答案：** `isa_mmu_check` 在每次访存的热路径上都要调，且 riscv32 当前判定结果是常量（恒为 `MMU_DIRECT`），用宏让编译器把它内联成常量、消除翻译分支，零开销。`isa_mmu_translate` 只在 check 返回 `MMU_TRANSLATE` 时才调，且要做多级页表遍历、逻辑复杂，用函数更清晰，也没有热路径内联的压力。两者职责与调用频率不同，取舍也不同。

**练习 2：** 如果一个 ISA 既不定义 `isa_mmu_check` 宏、也不提供函数实现，会怎样？

**参考答案：** `isa.h` 的 `#ifndef` 守卫会保留函数声明 `int isa_mmu_check(...)`，于是链接期需要找到它的定义。若没有任何 `.c` 提供实现，链接器报「未定义引用」（undefined reference），编译失败。也就是说，ISA 必须「二选一」地提供 check：要么宏、要么函数。

---

### 4.3 MMU 模式枚举：DIRECT / TRANSLATE / FAIL 三种语义

#### 4.3.1 概念说明

`isa_mmu_check` 的返回值是下面这个枚举：

```c
enum { MMU_DIRECT, MMU_TRANSLATE, MMU_FAIL };
```

三个值描述「这次访问与 MMU 的关系」，语义如下：

| 枚举值 | 数值 | 语义 | 典型触发场景 |
| --- | --- | --- | --- |
| `MMU_DIRECT` | 0 | 不翻译，`vaddr` 直接当 `paddr` | 分页关闭（RISC-V bare 模式 / x86 `CR0.PG=0`）；或访问不参与翻译的地址段 |
| `MMU_TRANSLATE` | 1 | 需要查页表翻译 | 分页开启（RISC-V SV32，`satp.mode=1`） |
| `MMU_FAIL` | 2 | 翻译前就判定非法 | 访问模式与当前特权级/分页配置冲突，应抛异常 |

需要特别注意：当前所有 ISA 的 `isa_mmu_check` 恒返回 `MMU_DIRECT`，所以 `MMU_TRANSLATE` 与 `MMU_FAIL` 在现有代码里**从未被实际产生**——它们是为分页实现（u7-l22）预留的语义。

#### 4.3.2 核心流程

`vaddr_read` 在接通 MMU 后，会按 check 的返回值走三分支（伪代码，**示例代码，非项目原有**）：

```
switch (isa_mmu_check(addr, len, MEM_TYPE_READ)) {
  case MMU_DIRECT:    return paddr_read(addr, len);            // 直读
  case MMU_TRANSLATE: paddr = isa_mmu_translate(addr, ...);    // 翻译后再读
                       return paddr_read(paddr, len);
  case MMU_FAIL:      raise_exception(addr); return 0;          // 抛异常
}
```

当前因为 check 恒为 `MMU_DIRECT`，只有第一个分支会执行，后两个分支即便写出来也会被编译器消除。

#### 4.3.3 源码精读

[include/isa.h:L41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L41) —— `enum { MMU_DIRECT, MMU_TRANSLATE, MMU_FAIL };`：注意这是匿名枚举，三个常量直接进入外层作用域，可在 `switch` 里直接当标签用，无需 `MMU::DIRECT` 之类的限定。

这组枚举与 4.5 的 `MEM_RET_*` 是**两组不同语义**的值，初学极易混淆——一个是 check 的输出（要不要翻译），一个是 translate 的输出（翻译结果如何）。务必分清。

#### 4.3.4 代码实践

**目标：** 把 `MMU_*` 三值与 `MEM_RET_*` 三值分清，并理解它们各自由谁产生、由谁消费。

**步骤：**

1. 阅读 [include/isa.h:L41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L41) 与 [include/isa.h:L43](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L43) 两组枚举。
2. 画一张两列对照表：左列「`isa_mmu_check` 产生 `MMU_*`」，右列「`isa_mmu_translate` 产生 `MEM_RET_*`」，分别写出消费者（前者由 `vaddr_*` 消费决定走哪个分支；后者由 `vaddr_*` 在翻译后消费决定是否报错/拆页）。

**需要观察的现象：** 两组枚举数值都是 0/1/2，但分属不同函数的返回域，不能混用。

**预期结果：** 能口头说出「`MMU_FAIL` 是 check 说『别翻译、直接报错』，`MEM_RET_FAIL` 是 translate 说『我翻了、但页表项无效、缺页』」——两者都引发异常，但发生在翻译的不同阶段。

#### 4.3.5 小练习与答案

**练习 1：** `MMU_FAIL` 与 `MEM_RET_FAIL` 有什么区别？

**参考答案：** 阶段不同。`MMU_FAIL` 由 `isa_mmu_check` 返回，表示在**翻译前**就判定这次访问非法（如分页配置与访问模式冲突），根本不该尝试翻译；`MEM_RET_FAIL` 由 `isa_mmu_translate` 返回，表示**翻译过程中**页表项无效或不存在（non-present），即缺页（page fault）。两者最终都会引发异常，但触发点和语义不同。

**练习 2：** 当前 riscv32 配置下，`MMU_TRANSLATE` 这个值会在运行时出现吗？

**参考答案：** 不会。因为 [src/isa/riscv32/include/isa-def.h:L31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L31) 把 `isa_mmu_check` 宏定义为恒返回 `MMU_DIRECT`，运行时永远拿不到 `MMU_TRANSLATE`。它要等 u7-l22 实现 SV32 分页、把 check 改成按 `satp` 返回后才会出现。

---

### 4.4 isa_mmu_translate：把虚拟地址翻译成物理地址

#### 4.4.1 概念说明

当 `isa_mmu_check` 返回 `MMU_TRANSLATE` 时，框架调用 `isa_mmu_translate` 做真正的页表遍历，把一个虚拟地址翻译成物理地址。它的签名是：

```c
paddr_t isa_mmu_translate(vaddr_t vaddr, int len, int type);
```

注意返回类型是 `paddr_t`，但它**承载多重语义**：

- 翻译成功：返回翻译后的物理地址（一个很大的数，如 `0x80000000` 起）。
- 翻译失败：返回 `MEM_RET_FAIL`（=1，表示页表项无效、缺页）。
- 跨页：见 4.5，由 `MEM_RET_CROSS_PAGE` 或调用方拆分处理。

**为什么 `paddr_t` 能同时返回「地址」和「错误码」？** 因为真实物理地址通常 `≥ 0x80000000`（由 `CONFIG_MBASE` 决定），绝不会落在 `0/1/2` 这几个小值上，于是 `0/1/2`（即 `MEM_RET_OK/FAIL/CROSS_PAGE`）可以安全地当哨兵值复用 `paddr_t` 的返回通道。这是 C 里「用值域分离来复用返回类型」的常见技巧，省去额外的出参。

当前 riscv32 的 `isa_mmu_translate` 是一个 **stub（占位实现）**：直接返回 `MEM_RET_FAIL`。这看起来很奇怪——既然翻译总是失败，程序怎么还能跑？答案是：因为 `isa_mmu_check` 恒返回 `MMU_DIRECT`，`isa_mmu_translate` **根本不会被调用到**，stub 返回什么都无所谓。选 `MEM_RET_FAIL` 是「安全默认」：万一将来 check 被改坏、误调到 translate，也不会用一个错误的物理地址去访存，而是报失败暴露问题。

#### 4.4.2 核心流程

将来实现 SV32 分页时（u7-l22），`isa_mmu_translate` 的概念流程如下（**示例伪代码，非项目原有**）：

```
paddr_t isa_mmu_translate(vaddr_t vaddr, int len, int type):
  base = cpu.satp.ppn << 12          # 页表基址（第一级）
  for 每一级页表 (SV32 有两级):
    idx   = (vaddr >> shift) & mask   # 取本级索引
    pte   = paddr_read(base + idx*4, 4)  # 读页表项
    if not (pte & V): return MEM_RET_FAIL   # 页表项无效 → 缺页
    base  = (pte >> 10) << 12         # 下一级页表基址
  return base | (vaddr & PAGE_MASK)   # 物理页基址 + 页内偏移
```

真实 SV32 的细节（两级页表、页表项位域、大页等）留待 u7-l22，本讲只关心接口契约：**输入虚拟地址，输出物理地址或 `MEM_RET_FAIL`**。

#### 4.4.3 源码精读

`isa_mmu_translate` 的声明没有 `#ifndef` 守卫，始终是真实函数：

[include/isa.h:L47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L47) —— `paddr_t isa_mmu_translate(vaddr_t vaddr, int len, int type);`：与 `isa_mmu_check` 不同，这里没有宏覆盖机制，每个 ISA 必须在 `system/mmu.c` 里提供真实函数定义。

riscv32 的 stub 实现：

[src/isa/riscv32/system/mmu.c:L20-L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/mmu.c#L20-L22) —— `isa_mmu_translate` 当前只 `return MEM_RET_FAIL;`，是占位实现。它包含了 `isa.h`、`memory/vaddr.h`、`memory/paddr.h` 三个头文件，正是为将来在函数体内调用 `paddr_read` 读页表项预先准备好的依赖。x86/mips32/loongarch32r 的 `mmu.c` 也是同样的 stub。

#### 4.4.4 代码实践

**目标：** 理解 stub 为何选 `MEM_RET_FAIL`，并确认它当前不会被调用。

**步骤：**

1. 阅读 [src/isa/riscv32/system/mmu.c:L20-L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/mmu.c#L20-L22)，确认 stub 返回 `MEM_RET_FAIL`。
2. 在 stub 里临时加一行打印（**示例代码，仅供观察，验证后请还原**）：

   ```c
   paddr_t isa_mmu_translate(vaddr_t vaddr, int len, int type) {
     printf("[mmu] translate called! vaddr=" FMT_WORD "\n", (word_t)vaddr);  /* 示例代码 */
     return MEM_RET_FAIL;
   }
   ```

3. 重新编译运行内置镜像。

**需要观察的现象：** 打印**永远不会出现**——因为 `isa_mmu_check` 恒返回 `MMU_DIRECT`，`vaddr_*` 根本不会调到 `isa_mmu_translate`。

**预期结果：** 运行全程看不到 `[mmu] translate called!`，内置镜像照常 `HIT GOOD TRAP`。这反向印证了「check 永远 DIRECT」与「translate 是 stub」是自洽的：stub 之所以能是 stub，正因为它不会被触发。

> 本地验证提示：若你已按 4.5.4 或综合实践把 `vaddr_read` 改成显式调用 `isa_mmu_check`，在 check 仍返回 `MMU_DIRECT` 的情况下，translate 依旧不会被调到。验证后请还原打印。

#### 4.4.5 小练习与答案

**练习 1：** 既然 `isa_mmu_translate` 当前永远不被调用，为什么还要提供 stub？

**参考答案：** 两个原因。其一，`isa.h` 声明了 `isa_mmu_translate` 且无宏覆盖，链接器需要一个符号定义，否则链接失败——stub 满足符号需求。其二，stub 是为将来分页实现预留的「修改入口」与「安全默认」：一旦 check 改为返回 `MMU_TRANSLATE`，开发者直接在这个已有函数里填页表遍历，且 stub 返回 `MEM_RET_FAIL` 保证填之前误调也不会用错地址访存。

**练习 2：** `isa_mmu_translate` 返回类型是 `paddr_t`，但可能返回 `MEM_RET_FAIL`（枚举 int 值 1）。在 32 位客机下这两者类型一致吗？

**参考答案：** `paddr_t` 在 32 位客机下是 `uint32_t`，`MEM_RET_FAIL` 是 `int`（枚举值），返回时发生 `int → uint32_t` 的隐式转换，值 `1` 被原样保留。调用方再把这个 `paddr_t` 与 `MEM_RET_FAIL`（同样转 `uint32_t`）比较，数值相等，判定成立。这是 C 枚举与无符号整数混用的常规行为，依赖「物理地址不会是 0/1/2」这一前提。

---

### 4.5 MEM_RET 返回值与跨页处理

#### 4.5.1 概念说明

`isa_mmu_translate` 的翻译结果由下面这组枚举编码：

```c
enum { MEM_RET_OK, MEM_RET_FAIL, MEM_RET_CROSS_PAGE };
```

| 枚举值 | 数值 | 语义 |
| --- | --- | --- |
| `MEM_RET_OK` | 0 | 翻译成功（实际实现里通常直接返回物理地址，而非这个 0） |
| `MEM_RET_FAIL` | 1 | 翻译失败：页表项无效/不存在，即缺页（page fault） |
| `MEM_RET_CROSS_PAGE` | 2 | 访问跨越两个页边界，需拆成两次翻译 |

本节重点是 **`MEM_RET_CROSS_PAGE`**。当一次访存的字节范围 `[vaddr, vaddr+len)` 横跨两个虚拟页时，单次页表翻译只能给出一个物理页基址，无法用一次连续的物理读写服务。于是需要把访问拆成两段：分别翻译两个虚拟页、各自做物理读写、再按字节序拼装成最终结果。

`type` 参数对应的另一组枚举描述访存种类，供 check/translate 做权限区分：

[include/isa.h:L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L42) —— `enum { MEM_TYPE_IFETCH, MEM_TYPE_READ, MEM_TYPE_WRITE };`：取指/读/写三种，与 `vaddr_ifetch`/`vaddr_read`/`vaddr_write` 一一对应。这就是 4.1 说「三个函数要分开」的根因——`type` 要随调用者不同而不同。

**重要现状：** `MEM_RET_CROSS_PAGE` 在全仓库**从未被使用**（可以 `grep` 验证，仅有 `isa.h` 的枚举定义这一处）。它是为分页实现预留的哨兵，体现 NEMU「接口先行」风格——先把跨页的语义编码定下来，实现时（u7-l22）再填。

#### 4.5.2 核心流程

**跨页判定**的数学条件。一次 `len` 字节的访问跨越页边界，当且仅当起始地址在页内的偏移加上长度超过页大小：

\[
\text{cross}(vaddr, len) \iff \big(vaddr \,\&\, \text{PAGE\_MASK}\big) + len > \text{PAGE\_SIZE}
\]

其中 `PAGE_SIZE = 1 << 12 = 4096`，`PAGE_MASK = PAGE_SIZE - 1 = 0xfff`。`vaddr & PAGE_MASK` 就是「页内偏移」。

举几个例子（`PAGE_SIZE = 0x1000`）：

| `vaddr` | `len` | `vaddr & 0xfff` | `+ len` | `> 0x1000`? | 跨页? |
| --- | --- | --- | --- | --- | --- |
| `0x...ffc` | 4 | `0xffc` | `0x1000` | 否（等于） | 否（4 字节都在本页 `0xffc..0xfff`） |
| `0x...ffd` | 4 | `0xffd` | `0x1001` | 是 | 是（3 字节在本页、1 字节下页） |
| `0x...1000` | 4 | `0x000` | `0x004` | 否 | 否（页起始，全在本页） |
| `0x...ffe` | 8 | `0xffe` | `0x1006` | 是 | 是（2 字节本页、6 字节下页） |

**跨页拆分**：若判定跨页，把访问拆成两段。

\[
len_1 = \text{PAGE\_SIZE} - (vaddr \,\&\, \text{PAGE\_MASK})
\]

\[
len_2 = len - len_1
\]

第一段读 `vaddr` 起 `len_1` 字节（在本页），第二段读 `vaddr + len_1` 起 `len_2` 字节（在下一页），最后按小端序拼装：

\[
\text{result} = lo \;\big|\; \big(hi \ll (8 \cdot len_1)\big)
\]

#### 4.5.3 源码精读

三组关键枚举集中在一起：

[include/isa.h:L41-L43](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L41-L43) —— `MMU_DIRECT/TRANSLATE/FAIL`、`MEM_TYPE_IFETCH/READ/WRITE`、`MEM_RET_OK/FAIL/CROSS_PAGE` 三组枚举连续定义。注意 `MEM_RET_*` 的数值是 `0/1/2`，与物理地址的值域（`≥ 0x80000000`）不冲突，这正是 4.4 说「`paddr_t` 返回值能复用当哨兵」的基础。

页大小常量在 `vaddr.h`：

[include/memory/vaddr.h:L25-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/vaddr.h#L25-L27) —— `PAGE_SHIFT = 12`、`PAGE_SIZE = 1ul << 12 = 4096`、`PAGE_MASK = PAGE_SIZE - 1 = 0xfff`。这是跨页判定与拆分要用到的全部常量。`1ul` 用 `unsigned long` 避免 32 位下移位溢出。

`isa_mmu_translate` stub 返回的就是 `MEM_RET_FAIL`：

[src/isa/riscv32/system/mmu.c:L20-L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/mmu.c#L20-L22) —— stub 返回 `MEM_RET_FAIL`（=1），即「翻译失败」。由于当前不会被调到，这个返回值只是占位。

#### 4.5.4 代码实践（本讲主实践）

**目标：** 设计「启用分页时 `vaddr_read` 先 check 再 translate 再 `paddr_read`」的完整调用链伪代码，并处理跨页。

**步骤：**

1. 先阅读 [include/isa.h:L41-L47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L41-L47)，把三组枚举与两个函数声明的签名记牢。
2. 写出分页就绪版 `vaddr_read`（**示例伪代码，非项目原有，仅用于理解调用链**）：

   ```c
   /* 示例伪代码：分页就绪后的 vaddr_read 调用链 */
   word_t vaddr_read(vaddr_t addr, int len) {
     int mmu = isa_mmu_check(addr, len, MEM_TYPE_READ);
     switch (mmu) {
       case MMU_DIRECT:
         return paddr_read(addr, len);                 // 不翻译，直读

       case MMU_TRANSLATE: {
         paddr_t paddr = isa_mmu_translate(addr, len, MEM_TYPE_READ);
         if (paddr == MEM_RET_FAIL) { raise_page_fault(addr); return 0; }
         /* 跨页判定 */
         if ((addr & PAGE_MASK) + len > PAGE_SIZE) {
           int len1 = PAGE_SIZE - (addr & PAGE_MASK);  // 本页字节数
           int len2 = len - len1;                      // 下页字节数
           word_t lo = paddr_read(paddr, len1);        // 本页已翻译，直接读
           paddr_t paddr2 = isa_mmu_translate(addr + len1, len2, MEM_TYPE_READ);
           if (paddr2 == MEM_RET_FAIL) { raise_page_fault(addr + len1); return 0; }
           word_t hi = paddr_read(paddr2, len2);
           return lo | (hi << (8 * len1));             // 小端拼装
         }
         return paddr_read(paddr, len);                // 单页，翻译后读
       }

       case MMU_FAIL:
         raise_exception(addr); return 0;              // 翻译前非法
     }
     return 0;
   }
   ```

3. **手算一个跨页例子**：`addr = 0x...ffd`，`len = 4`。
   - `addr & PAGE_MASK = 0xffd`，`0xffd + 4 = 0x1001 > 0x1000` → 跨页。
   - `len1 = 0x1000 - 0xffd = 3`，`len2 = 4 - 3 = 1`。
   - 先读本页 3 字节（`0xffd,0xffe,0xfff`），再翻译下一页读 1 字节（`0x1000`），拼装时 `hi` 左移 `8*3=24` 位。

**需要观察的现象：** 伪代码覆盖了 `DIRECT`/`TRANSLATE`/`FAIL`/跨页四分支；跨页时对两个页分别 translate、分别 `paddr_read`，再小端拼装。

**预期结果：** 你能口头解释「check 决定走哪条路、translate 给出物理地址或失败、跨页时拆两段分别翻译」这条调用链，并说清 `len1`/`len2` 的算法。**待本地验证**：本实践为设计型，无需运行；若想验证调用链通畅，可看下面的综合实践（把 `vaddr_read` 真的改成调 `isa_mmu_check`，在 check 仍返回 `MMU_DIRECT` 时行为不变）。

#### 4.5.5 小练习与答案

**练习 1：** `addr = 0x...ffe`，`len = 8`，是否跨页？若跨，`len1`/`len2` 各是多少？

**参考答案：** `0xffe & 0xfff = 0xffe`，`0xffe + 8 = 0x1006 > 0x1000` → 跨页。`len1 = 0x1000 - 0xffe = 2`，`len2 = 8 - 2 = 6`。即本页读 2 字节、下页读 6 字节，拼装时 `hi << 16`。

**练习 2：** 跨页拆分时，两段是分别调 `isa_mmu_translate`，而不是只翻译一次。为什么不能只翻译一次？

**参考答案：** 因为两个虚拟页可能映射到**完全不相邻**的两个物理页。一次翻译只能给出第一页的物理基址，第二页的物理基址必须靠对 `addr + len1` 再做一次翻译得到。虚拟地址连续 ≠ 物理地址连续，这正是分页的特性，也是 `MEM_RET_CROSS_PAGE` 存在的根本原因。

**练习 3：** `MEM_RET_CROSS_PAGE` 这个枚举值当前在源码里被用到吗？

**参考答案：** 没有。全仓库 `grep` 只有 [include/isa.h:L43](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L43) 这一处定义，无任何使用点。它是为分页实现预留的哨兵——本讲的跨页拆分用的是调用方（`vaddr_read`）自己判定 `(addr & PAGE_MASK) + len > PAGE_SIZE`，也可选择让 `isa_mmu_translate` 返回 `MEM_RET_CROSS_PAGE` 来通知调用方拆分。两种设计都合法，具体取舍留给 u7-l22。

---

## 5. 综合实践

把本讲五个最小模块串起来，完成一个「分页就绪的 `vaddr` 层」设计任务，并验证接缝接通后默认行为不变。

**任务：** 给 `vaddr.c` 写一份「分页就绪」改造方案，让虚拟内存层在不开分页时行为完全不变，但为开分页留好接缝。**以下为示例代码，仅供理解与可选验证，勿提交。**

1. **读懂现状**：先确认 [src/memory/vaddr.c:L19-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L19-L29) 三个函数当前都是透传，且 [src/isa/riscv32/include/isa-def.h:L31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L31) 的 `isa_mmu_check` 恒返回 `MMU_DIRECT`。

2. **改造 `vaddr_read`**（**示例代码，可选验证后请还原**）：

   ```c
   /* 示例代码：接通 MMU 接缝（check 仍返回 MMU_DIRECT，行为不变） */
   word_t vaddr_read(vaddr_t addr, int len) {
     int mmu = isa_mmu_check(addr, len, MEM_TYPE_READ);
     Assert(mmu == MMU_DIRECT, "vaddr_read: paging not supported yet, mmu=%d", mmu);
     return paddr_read(addr, len);
   }
   ```

   - 这里用 `Assert` 守卫：当前 check 恒为 `MMU_DIRECT`，断言必过，行为与原透传一致；将来若误开分页而 `vaddr_read` 还没实现翻译，断言会立刻暴露而不是静默用错地址。
   - `vaddr_write` 同理改（`MEM_TYPE_WRITE`），`vaddr_ifetch` 同理改（`MEM_TYPE_IFETCH`）。

3. **验证接缝接通**：重新 `make` 并运行内置镜像，应照常 `HIT GOOD TRAP`——说明在 check 仍返回 `MMU_DIRECT` 时，接通 MMU 接缝不改变任何可观察行为。

4. **设计跨页**：参照 4.5.4 的伪代码，把 `case MMU_TRANSLATE` 分支补全（纸上设计即可，**不必真的实现 SV32**，那是 u7-l22 的任务）。重点检查：
   - 跨页判定 `(addr & PAGE_MASK) + len > PAGE_SIZE` 是否覆盖 4.5.2 表格中的四个例子。
   - `len1 = PAGE_SIZE - (addr & PAGE_MASK)` 在 `addr` 恰为页起始时给出 `PAGE_SIZE`（即不跨页，不会进入此分支），逻辑自洽。
   - 小端拼装 `lo | (hi << (8*len1))` 的位移量是否正确。

5. **解释 `ifetch` 为何用 `MEM_TYPE_IFETCH`**：取指与数据读虽然都「读内存」，但分页后权限位不同（可执行 vs 可读）。在 `vaddr_ifetch` 的改造里传 `MEM_TYPE_IFETCH`，让将来的 `isa_mmu_translate` 能据此检查执行权限。

6. **还原**：删除所有示例改动，把 `vaddr.c` 恢复成原始透传，确保不污染后续讲义。

**通过标准：** 你能口头讲清一条 `vaddr_read(addr, len)` 在「当前」与「开分页后」两条路径上的完整调用链：
- 当前：`vaddr_read → (check=MMU_DIRECT) → paddr_read → pmem_read/mmio`。
- 开分页后：`vaddr_read → (check=MMU_TRANSLATE) → isa_mmu_translate → paddr_read`，跨页时拆两段、小端拼装。
并说清 `MMU_*` 与 `MEM_RET_*` 两组枚举分别由谁产生、谁消费。

## 6. 本讲小结

- `vaddr` 是客机 CPU 看到的虚拟地址，`paddr` 是物理内存地址；当前未实现分页，`vaddr == paddr`，`vaddr.c` 的 `vaddr_ifetch`/`vaddr_read`/`vaddr_write` 三个函数都只是 `paddr_*` 的透传。
- 取指走 `vaddr_ifetch`、数据走 `vaddr_read`/`vaddr_write`，三者分开是为分页后的权限区分（执行/读/写），对应 `MEM_TYPE_IFETCH/READ/WRITE` 三个 `type` 值。
- `isa_mmu_check` 是 per-access 的「要不要翻译」判定，返回 `MMU_DIRECT`/`MMU_TRANSLATE`/`MMU_FAIL`；riscv32 等四 ISA 用宏把它定义为恒返回 `MMU_DIRECT`，编译期消除翻译分支。
- `isa.h` 用 `#ifndef isa_mmu_check` 守卫函数声明，让 ISA 可用宏覆盖它（零开销内联）；`isa_mmu_translate` 无此守卫，始终是真实函数，由各 ISA 的 `system/mmu.c` 提供。
- `isa_mmu_translate` 当前是 stub，返回 `MEM_RET_FAIL`；返回类型 `paddr_t` 复用 `0/1/2` 作哨兵编码 `MEM_RET_OK/FAIL/CROSS_PAGE`，依赖「物理地址不会是 0/1/2」这一前提。
- `MEM_RET_CROSS_PAGE` 处理跨页：当 `(addr & PAGE_MASK) + len > PAGE_SIZE` 时，访问横跨两页，需拆成两段分别翻译、分别读写、再小端拼装 `lo | (hi << (8*len1))`。该枚举当前全仓库未使用，为分页预留。
- 整个 MMU 接口（check/translate + 三组枚举）当前几乎全是 stub，是 NEMU「接口先行、实现后补」风格的典型——真正实现在 u7-l22。

## 7. 下一步学习建议

本讲解构了虚拟内存层与 MMU 接口的「形状」，但翻译逻辑仍是空壳。接下来的进阶单元 U7 会把这套接口填实：

- **u7-l22 分页与 MMU 地址翻译**：实现 riscv32 SV32 页表遍历——把 `isa_mmu_check` 改成按 `satp` 模式位返回 `MMU_DIRECT`/`MMU_TRANSLATE`，把 `isa_mmu_translate` 的 stub 替换成真实的多级页表遍历，并落地本讲 4.5.4 设计的跨页拆分。
- **u7-l21 中断与异常机制**：当 `isa_mmu_translate` 返回 `MEM_RET_FAIL`（缺页）或 `isa_mmu_check` 返回 `MMU_FAIL` 时，要经 `isa_raise_intr` 抛出页错误异常。本讲的失败返回值正是异常的触发源。
- **u7-l23 NEMU trap 与执行状态机**：翻译失败引发的异常如何与 NEMU 的 `NEMU_ABORT`/`NEMU_STOP` 状态交互。

建议阅读顺序：

1. 重读本讲 4.5.4 的 `vaddr_read` 伪代码，确认你理解 DIRECT/TRANSLATE/FAIL/跨页四分支。
2. 读 [src/isa/riscv32/system/mmu.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/mmu.c) 与 [src/isa/riscv32/include/isa-def.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h)，记住这两个文件就是 u7-l22 的主战场。
3. 可选横向跳到 **u6-l18 设备框架与 IOMap**：理解 MMIO 地址（如 `0xa00003f8`）为何**不经过** MMU 翻译——它们在 `paddr_read` 的 mmio 分支就被设备回调截走，根本到不了 `vaddr` 层的翻译逻辑。
