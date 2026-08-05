# 内核运行时启动与入口模型

## 1. 本讲目标

本讲打开「设备侧内核运行时」的黑盒，回答一个核心问题：**当一个 kernel 被 launch 之后，设备上的第一个线程究竟从哪一条指令开始执行、又如何进入你写的 C/C++ kernel 函数？**

学完后你应当能够：

- 说清 `__vx_cta_entry` 这段统一 prologue（前置代码）依次做了哪些寄存器与段设置（SATP / gp / sp / tp+TLS / 全局构造函数），以及它如何派发到真正的 kernel。
- 解释为何 dispatch 的 `jalr` 必须用 `.option norvc` 强制成 4 字节指令，这和调度器的「CTA rewind」有什么关系。
- 掌握多入口 `.vxbin` 的 `VXSYMTAB` 符号表磁盘布局，理解 `__vx_kentry_<name>` 别名与 `vxbin.py` 的协作。
- 理解 `kernel_main` / `main` 命名约定，以及无 footer 的旧式单入口二进制为何能与新机制逐字节兼容。

本讲承接 u3-l4（主机侧 `.vxbin` 加载与 KMU 启动），把视角从主机推进到设备侧的入口代码。

## 2. 前置知识

阅读本讲前，你需要具备以下认知（均已在前面讲义建立）：

- **SIMT 与 warp 模型**（u1-l1）：Vortex 每周期发射一个 warp，warp 内线程共享 PC，靠 thread mask 控制写回。本讲里的「每个 hart / 每个线程」就是 warp 内的 lane。
- **CTA 概念**：CTA（Cooperative Thread Array）即一个线程块（block）。一次 kernel launch 会被 KMU 拆成若干 CTA，分派到各 warp 上执行。详见 u6-l1 与 `docs/designs/cta_clustering_and_dispatch.md`。
- **KMU**（Kernel Management Unit）：命令处理器的一部分，负责把主机发来的 launch 请求拆成 CTA 并派发。主机侧如何向 KMU 写 `VX_DCR_KMU_*` 寄存器、如何触发 `CMD_LAUNCH`，见 u3-l4。
- **CSR（Control and Status Register）**：RISC-V 的控制状态寄存器。Vortex 扩展了一批自定义 CSR，如 `VX_CSR_MHARTID`（hart id）、`VX_CSR_MSCRATCH`（通用暂存）、`VX_CSR_CTA_ENTRY`（kernel 入口 PC）。
- **RVC（RISC-V Compressed）**：RISC-V 的 2 字节压缩指令编码。一条 `jalr` 在允许 RVC 时可能被压缩成 2 字节的 `c.jalr`，本讲的 `.option norvc` 正是为了禁止这种压缩。
- **`.vxbin` 文件格式**（u3-l4）：磁盘布局为 `[min_vma][max_vma][image]`，可能再带一个 `VXSYMTAB` 尾部。

补充两个 RISC-V/ELF 术语：

- **prologue（前置代码）**：在调用真正的函数（如 `main` 或 kernel）之前，由运行时执行的一段初始化代码，负责设置栈指针、全局指针、TLS 等，把 CPU「打扮成」可以安全调用 C 函数的状态。
- **weak 符号（弱符号）**：一种可以被强定义覆盖的符号。如果没有任何地方强定义它，弱符号解析为 0 而不报链接错误。本讲会看到 `kernel_main` 的命名约定与弱符号思想的关系。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [sw/kernel/src/vx_start.S](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S) | 设备侧运行时入口。定义 `_start` 与统一 prologue `__vx_cta_entry`，是本讲的绝对主角。同时包含旧式非 KMU 入口路径。 |
| [sw/kernel/scripts/vxbin.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/scripts/vxbin.py) | 把链接好的 ELF 转成 `.vxbin` 的工具。扫描 `__vx_kentry_*` 符号，生成 `VXSYMTAB` 符号表尾部。 |
| [sw/kernel/include/vx_spawn2.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn2.h) | 定义 `__kernel` 宏，用 `annotate("vortex.kernel")` 驱动后端为每个 kernel 生成入口别名。 |
| [sw/kernel/scripts/link64.ld](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/scripts/link64.ld) | 64 位链接脚本。`KEEP (*(.vx_entry ...))` 保留多入口 stub，并 `PROVIDE(__tls_block_size)` 给 TLS 步长。 |
| [sw/kernel/src/vx_syscalls.c](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c) | 提供 `__init_tls` 与 `__libc_init_array`，prologue 会调用它们。 |
| [sw/kernel/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/Makefile) | 把同一份 `vx_start.S` 编成 `libvortex`（旧式）与 `libvortex2`（KMU）两个库。 |
| [tests/regression/multikernel/](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/kernel.cpp) | 仓库内不依赖 PoCL 的多入口回归测试，含 `add_k`/`mul_k`/`acc_k` 三个 kernel。 |
| [docs/designs/kernel_entry_and_dispatch.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/kernel_entry_and_dispatch.md) | 入口与多入口机制的设计文档。 |

## 4. 核心概念与源码讲解

### 4.1 设备侧入口模型：为什么需要统一 prologue

#### 4.1.1 概念说明

在传统 CPU 程序里，`main` 之前有一段 crt（C runtime）启动代码，负责设栈、清 BSS、跑全局构造函数。GPU 内核启动面临一个更复杂的问题：**同一个程序里可能有多个 kernel，每个 kernel 的入口地址不同；而且它们由硬件调度器（KMU）在任意时刻、在任意 warp 上派发，CPU 风格的「从 `_start` 一路跑到 `main`」并不适用。**

Vortex 的解法是：**让所有 kernel 共享同一段 prologue `__vx_cta_entry`**。这段 prologue 把一个「裸」的 warp lane 提升到「可以安全调用 C 函数」的状态——设好页表基址（SATP）、全局指针（gp）、栈指针（sp）、线程本地存储指针（tp），并运行全局构造函数（`__libc_init_array`）和 TLS 初始化（`__init_tls`）。做完这些一次性准备后，它再**派发**到真正要执行的那个 kernel。

关键设计：**「要执行哪个 kernel」不是一个写死的地址，而是由 KMU 在每个 CTA 启动时，通过 CSR `VX_CSR_CTA_ENTRY` 现场告知的。** prologue 只负责把环境准备好，然后从这个 CSR 读出入口 PC，跳过去。

这就回答了本讲的根本问题：设备上的第一个线程从 `_start`（image 基址）开始，跑完统一 prologue，再根据 KMU 注入的 CSR 跳到具体 kernel。

#### 4.1.2 核心流程

一次 CTA 在设备侧的启动可以概括为：

```text
KMU 派发一个 CTA 到某 warp
  │  ① 把该 CTA 的 kernel 入口 PC 写入 VX_CSR_CTA_ENTRY
  │  ② 把 kernel 参数指针写入 VX_CSR_MSCRATCH
  │  ③ 把 warp 的 PC 重置到 image 基址（_start / __vx_cta_entry）
  ▼
__vx_cta_entry（统一 prologue）
  ├─ (若开启 VM) 写 SATP：页表基址 PPN + 寻址模式
  ├─ (若 NEED_GP) 设 gp = __global_pointer
  ├─ 设 sp = STACK_BASE - hartid * STACK_SIZE   （每 hart 独立栈）
  ├─ (若 NEED_TLS) 设 tp = _end + hartid * tls_block_size；调用 __init_tls
  ├─ (若 NEED_INITFINI) 调用 __libc_init_array（跑全局构造函数）
  ├─ s11 = VX_CSR_CTA_ENTRY    （读 kernel 入口 PC）
  ├─ a0  = VX_CSR_MSCRATCH     （读 kernel 参数指针）
  ├─ jalr ra, s11              （派发到 kernel；a0 作为第一个参数）
  │        ▲ kernel 返回后回到这里 ▲
  ├─ wsync                    （排空本 warp 所有挂起指令）
  └─ tmc x0                   （关闭本 warp，退休）
```

注意一个重要细节：prologue 的「一次性准备」（SATP/gp/sp/tp/构造函数）与「每个 CTA 都要重做的派发」是分开的。调度器在新 CTA 进驻同一个 warp 槽位时，并不回到 prologue 最开头，而是**回卷（rewind）到最后那 5 条指令的派发窗口**——这正是 4.4 节要解释的「CTA rewind」优化，也是 `.option norvc` 存在的原因。

#### 4.1.3 源码精读：两个库、一个 `_start`、一个 `__vx_cta_entry`

Vortex 用同一份 `vx_start.S` 编出两个设备侧运行时库：旧式的 `libvortex` 和 KMU 模型的 `libvortex2`，靠 `KMU_ENABLE` 宏区分（[sw/kernel/Makefile:36-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/Makefile#L36-L64)）。其中 `.2.o` 目标带 `-DKMU_ENABLE`，对应 `libvortex2`：

```makefile
# sw/kernel/Makefile
PROJECT  := libvortex
PROJECT2 := libvortex2
...
%.S.2.o: $(SRC_DIR)/%.S
	$(CC) $(CFLAGS) $(STARTUP_FLAGS) -DKMU_ENABLE -c $< -o $@
```

多入口测试明确选用 KMU 库（[tests/regression/multikernel/Makefile:12-13](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/Makefile#L12-L13)）：

```makefile
# vortex2 = the KMU kernel runtime, providing _start and __vx_cta_entry.
KERNEL_LIB := vortex2
```

在 KMU 路径里，ELF 入口 `_start` 与统一 prologue `__vx_cta_entry` **落在同一个地址**（image 基址）。源码里 `_start:` 之后到 `__vx_cta_entry:` 之间只有注释，没有指令，所以二者互为别名（[sw/kernel/src/vx_start.S:30-67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L30-L67)）：

```asm
  .global _start
  .type   _start, @function
_start:

#ifdef KMU_ENABLE
  ...（一大段说明性注释）...
  # _start is the shared per-CTA entry ... _start aliases __vx_cta_entry.
  .global __vx_cta_entry
  .type   __vx_cta_entry, @function
__vx_cta_entry:
```

> **关于源码注释与实现的一个提示**：`vx_start.S` 顶部的注释块（[L34-L60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L34-L60)）描述了一种「每个 kernel 各有一个 `lla s11, <kernel>; j __vx_cta_entry` 的 stub」的概念模型，以及 `kernel_main` 为 weak 的设想。但**当前实现的派发是 CSR 驱动的**（见 4.2.3 与 4.4），prologue 从 `VX_CSR_CTA_ENTRY` 读入口，而不是从某个 stub 加载 `s11`。阅读源码时以代码本身为准；那个注释块描述的是设计意图/历史路径，二者最终都汇聚到同一段 `__vx_cta_entry` prologue。

#### 4.1.4 代码实践：确认两个库与别名

1. **目标**：验证「同一份 `vx_start.S` 编出两个库」与「`_start`/`__vx_cta_entry` 同址」。
2. **操作步骤**：
   - 打开 [sw/kernel/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/Makefile)，找到 `PROJECT`/`PROJECT2` 与 `%.S.2.o` 规则，确认 `-DKMU_ENABLE` 只加在 `.2.o` 上。
   - 阅读 [sw/kernel/src/vx_start.S:30-67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L30-L67)，数一下 `_start:` 与 `__vx_cta_entry:` 之间有几条真实指令（答案应为 0，全是注释与 `#ifdef`）。
3. **需要观察的现象**：两个标号之间没有任何机器指令。
4. **预期结果**：`_start` 与 `__vx_cta_entry` 在反汇编里地址相同；`libvortex2`（`.2.o`）的 `_start` 进入 KMU 路径，`libvortex`（`.o`）进入 4.1.5 提到的旧式 `wspawn` 路径。
5. 若你已 configure 出 build 目录，可对 `build/hw/tests/multikernel` 的 ELF 跑 `riscv64-objdump -d`，定位 `_start` 与 `__vx_cta_entry` 的地址是否一致（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Vortex 要维护 `libvortex` 与 `libvortex2` 两个设备侧运行时库，而不是只留一个？

> **参考答案**：`libvortex` 是旧式的非 KMU 模型，`_start` 自己用 `wspawn` 派生 warp、用 `tmc` 激活线程，然后直接 `call main`（见 [vx_start.S:137-172](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L137-L172)）；`libvortex2` 是新的 KMU 模型，依赖硬件 KMU 来派发 CTA、靠 CSR 传入口。保留两者是为了让旧式单 kernel 程序与新多 kernel 程序（以及 PoCL）各取所需，同一份源码用 `KMU_ENABLE` 切换。

**练习 2**：`_start` 与 `__vx_cta_entry` 是两个不同的全局符号，为什么说它们「同址」？

> **参考答案**：在 KMU 路径中，源码在 `_start:` 之后、`__vx_cta_entry:` 之前没有插入任何指令（只有注释和条件编译守卫），所以汇编器把两个标号放在同一个程序地址上。它们是同一处入口的两个名字。

### 4.2 `__vx_cta_entry` prologue 精读：从 SATP 到派发

#### 4.2.1 概念说明

`__vx_cta_entry` 的职责是把 warp lane 从「刚被 KMU 唤醒、寄存器状态未知」提升到「可以安全执行 C/C++ kernel」。这需要依次解决五件事：

1. **页表**（可选）：若启用虚拟内存（`VX_CFG_VM_ENABLE`），写 `satp` CSR 指向运行时预先建好的页表，并设置寻址模式（Sv32/Sv39）。
2. **全局指针 gp**（可选）：C 编译器用 gp 做小数据段（.sdata/.sbss）的相对寻址，必须先设好。
3. **栈指针 sp**：每个 hart 需要独立的栈区，按 hart id 错开。
4. **TLS 指针 tp**（可选）：线程本地存储（`thread_local` 变量）需要每个 hart 一份独立映像。
5. **全局构造函数**（可选）：`__attribute__((constructor))` 注册的函数要在这里跑（如填充查表）。

完成这些后，prologue 才读 CSR、派发到 kernel。其中 1–5 是否执行由编译期宏（`VX_CFG_VM_ENABLE`、`NEED_GP`、`NEED_TLS`、`NEED_INITFINI`）决定——这是一个「按需裁剪」的 prologue。

#### 4.2.2 核心流程

prologue 内部的数据流可以画成：

```text
                  ┌──────────────── VM? ──→ satp = PT_BASE>>page_log2 | mode_bit
                  │
VX_CSR_MHARTID ──→┼──→ t0 = hartid ──┬──→ sp  = STACK_BASE - t0<<STACK_LOG2   (独立栈)
                  │                  ├──→ tp  = _end + t0*tls_block_size        (独立 TLS)
                  │                  └──→ (gp 来自 __global_pointer 链接符号)
                  │
                  └──(TLS)──→ __init_tls(复制 .tdata 模板、清零 .tbss)
                             └──(INITFINI)──→ __libc_init_array(跑 .init_array)
                                                └──→ s11=CTA_ENTRY; a0=MSCRATCH; jalr s11
```

每个 hart 的栈与 TLS 都用 `VX_CSR_MHARTID`（hart id，[VX_types.toml:504](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L504) 定义为 `0xF14`）做错位，公式为：

\[ \text{sp} = \text{STACK\_BASE} - \text{hartid} \times 2^{\text{STACK\_LOG2\_SIZE}} \]

\[ \text{tp} = \text{\_end} + \text{hartid} \times \text{tls\_block\_size} \]

其中 `tls_block_size` 不是 `.tbss` 一段的大小，而是 `.tdata + .tbss` 合起来的步长（详见 4.2.3）。

#### 4.2.3 源码精读

**(a) 写 SATP（仅 VM 开启时）** — [sw/kernel/src/vx_start.S:68-89](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L68-L89)

```asm
#ifdef VX_CFG_VM_ENABLE
  # 运行时已把页表放在 VX_MEM_PAGE_TABLE_BASE_ADDR，这里只把每核 MMU 指过去。
#if VX_VM_ADDR_MODE == SV39
  li    t0, VX_MEM_PAGE_TABLE_BASE_ADDR
  srli  t0, t0, VX_VM_PAGE_LOG2_SIZE      # 右移成 PPN
  li    t1, 1
  slli  t1, t1, 63                         # Sv39 模式位在第 63 位
  or    t0, t0, t1
  csrw  satp, t0
#elif VX_VM_ADDR_MODE == SV32
  ...                                     # Sv32 模式位在第 31 位
#endif
#endif
```

这段把页表基址右移 `VX_VM_PAGE_LOG2_SIZE` 位得到物理页号（PPN），再或在最高模式位（Sv39 用第 63 位、Sv32 用第 31 位），写进 `satp`。注意：栈（STACK）与页表（PT）区在 `VMManager::need_trans` 里是 MMU 旁路的，所以**在设好栈之前先写 satp 是安全的**（见注释 L72-L74）。虚拟内存子系统的完整细节见 u11-l1。

**(b) 设 gp（仅 NEED_GP 时）与 sp** — [sw/kernel/src/vx_start.S:90-98](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L90-L98)

```asm
#ifdef NEED_GP
  la    gp, __global_pointer               # 全局指针，由链接脚本计算
#endif
  LOAD_IMMEDIATE64(sp, VX_MEM_STACK_BASE_ADDR)   # sp = 栈基址
  csrr  t0, VX_CSR_MHARTID                       # t0 = hartid
  sll   t1, t0, VX_MEM_STACK_LOG2_SIZE           # t1 = hartid << STACK_LOG2
  sub   sp, sp, t1                               # sp = 栈基址 - hartid*栈大小
```

`LOAD_IMMEDIATE64` 在 RV64 上用 `li/slli/or` 三步装入 64 位立即数，在 RV32 上退化为单条 `li`（定义在 [sw/kernel/src/common.h:16-25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/common.h#L16-L25)）。这样每个 hart 拿到不重叠的栈区。

**(c) 设 tp + 初始化 TLS（仅 NEED_TLS 时）** — [sw/kernel/src/vx_start.S:99-114](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L99-L114)

```asm
#ifdef NEED_TLS
  # 每 hart 的 TLS 步长是 __tls_block_size（= __tbss_offset + __tbss_size），
  # 不能只用 __tbss_size，否则相邻 hart 的 TLS 映像会重叠 __tbss_offset 字节。
  lui   t1, %hi(__tls_block_size)
  addi  t1, t1, %lo(__tls_block_size)
  mul   t0, t0, t1                          # t0 = hartid * tls_block_size
  la    tp, _end
  add   tp, tp, t0                          # tp = _end + hartid*tls_block_size
  call  __init_tls                          # 复制 .tdata 模板、清零 .tbss
#endif
```

这里有一个**关键的「坑」与修复**：每个 hart 的 TLS 映像横跨 `.tdata`（已初始化）与 `.tbss`（零初始化）两段，所以步长必须是 `__tbss_offset + __tbss_size`，而不是 `__tbss_size`。如果只用 `.tbss` 大小，相邻 hart 的映像会重叠 `__tbss_offset` 字节，导致一个 hart 写自己的 `.tbss` 累加器时踩到邻居的 `.tdata` 标记。链接脚本显式提供这个步长（[sw/kernel/scripts/link64.ld:115-120](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/scripts/link64.ld#L115-L120)）：

```ld
PROVIDE(__tbss_size = ALIGN(SIZEOF(.tbss), 8));
/* 每 hart 的 TLS 步长，横跨 .tdata + .tbss。 */
PROVIDE(__tls_block_size = __tbss_offset + __tbss_size);
```

> 注意源码用 `lui/addi`（绝对寻址）而非 `la`（PC 相对）读取 `__tls_block_size`，因为它是很小的绝对值，`la` 的 medany PC 相对展开在 RV64 上会溢出（注释 L102-L106）。`__init_tls` 的实现见 [sw/kernel/src/vx_syscalls.c:70-78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L70-L78)：它用 `tp` 把 `.tdata` 模板 memcpy 进来，再把 `.tbss` 区 memset 清零。

**(d) 跑全局构造函数（仅 NEED_INITFINI 时）** — [sw/kernel/src/vx_start.S:115-118](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L115-L118)

```asm
#ifdef NEED_INITFINI
  call  __libc_init_array
#endif
```

`__libc_init_array` 遍历链接器提供的 `.preinit_array` 与 `.init_array` 段，逐个调用其中的构造函数（[sw/kernel/src/vx_syscalls.c:93-108](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L93-L108)）。多入口测试里的 `g_cubes[]` 查表就靠一个 `__attribute__((constructor))` 函数填充（[tests/regression/multikernel/kernel.cpp:39-43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/kernel.cpp#L39-L43)）——如果跳过这一步，表保持全零，所有 `add_k`/`mul_k` 结果都会错。

> 这里有一条值得注意的 callee-saved 约定：prologue 在 (d) 之后才读 `s11`，而 `__init_tls`/`__libc_init_array` 是普通 C 调用。源码顶部注释（L41-L43）强调 `s11` 是被调用者保存（callee-saved）寄存器，所以即使将来 prologue 提前把入口放进 `s11`，这些调用也不会破坏它。当前实现则是直接从 CSR 读 `s11`，进一步避免了依赖。

**(e) 派发到 kernel** — [sw/kernel/src/vx_start.S:119-134](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L119-L134)

```asm
  # 每个 CTA 的派发窗口：调度器为新 CTA 回卷到这整整 5 条（20 字节）。
  csrr  s11, VX_CSR_CTA_ENTRY              # 入口 PC（KMU 每个 CTA 重新注入）
  csrr  a0,  VX_CSR_MSCRATCH               # kernel 参数指针
  .option push
  .option norvc
  jalr  ra, s11                            # 派发；a0 作为 kernel 第一个参数
  .option pop
  .insn r RISCV_CUSTOM0, 7, 0, x0, x0, x0  # wsync：排空本 warp 挂起指令
  .insn r RISCV_CUSTOM0, 0, 0, x0, x0, x0  # tmc x0：关闭本 warp，退休
```

- `VX_CSR_CTA_ENTRY`（`0xCE1`，[VX_types.toml:562](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L562)）由 KMU 在每个 CTA 启动时写入「该 kernel 的入口 PC」。
- `VX_CSR_MSCRATCH`（`0x340`，[VX_types.toml:514](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L514)）由 KMU 写入「kernel 参数指针」，prologue 把它放进 `a0`，于是 kernel 的第一个形参就是参数指针——这就是为什么 kernel 形如 `void add_k(kernel_arg_t* __UNIFORM__ arg)`。
- `jalr ra, s11` 跳到 kernel，并把返回地址 `ra` 指向后面的 `wsync`。kernel 执行完毕 `ret` 回到 `wsync`，排空挂起指令后 `tmc x0` 关闭 warp。
- `.option norvc` 的作用是 4.4 节的核心。

#### 4.2.4 代码实践：跟踪寄存器/段设置

1. **目标**：写出 `__vx_cta_entry` 依次做了哪些寄存器/段设置。
2. **操作步骤**：逐行阅读 [sw/kernel/src/vx_start.S:67-135](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L67-L135)，按下表填写每个条件编译守卫对应的设置：

   | 守卫宏 | 设置的寄存器/动作 | 来源 |
   |--------|------------------|------|
   | `VX_CFG_VM_ENABLE` | `satp`（页表 PPN + 模式位） | ? |
   | `NEED_GP` | `gp` | ? |
   | （恒做） | `sp`（按 hartid 错开） | ? |
   | `NEED_TLS` | `tp` + `__init_tls` | ? |
   | `NEED_INITFINI` | `__libc_init_array` | ? |
   | （恒做） | `s11`/`a0` + `jalr` + `wsync` + `tmc` | ? |

3. **需要观察的现象**：哪些步骤是「每个 hart 不同」（用 `VX_CSR_MHARTID` 错位），哪些是「所有 hart 相同」（如 `satp`、`gp`）。
4. **预期结果**：`satp`/`gp` 在每个 hart 上写同样的值（幂等）；`sp`/`tp` 因 `hartid` 而异；`__libc_init_array` 会被每个 hart 各跑一遍（注释 [vx_start.S:189-193](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L189-L193) 在旧式路径里解释了这种对称性）。
5. 若想验证「参数指针真的经 `a0` 传入」，可在 [tests/regression/multikernel/kernel.cpp:63](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/kernel.cpp#L63) 看到 kernel 第一个形参就是 `kernel_arg_t* __UNIFORM__ arg`，与 prologue 把 `MSCRATCH` 放进 `a0` 一一对应（待本地运行确认）。

#### 4.2.5 小练习与答案

**练习 1**：为什么栈指针用 `STACK_BASE - hartid<<STACK_LOG2` 而不是 `STACK_BASE + hartid<<STACK_LOG2`？

> **参考答案**：RISC-V（以及绝大多数架构）的栈是「向下生长」的——压栈使 `sp` 减小。把基址放在最高地址，每个 hart 向下分配一段，既能保证栈生长方向正确，又让各 hart 栈区互不重叠。

**练习 2**：如果把 prologue 里 `call __libc_init_array` 注释掉，多入口测试的 `add_k` 会怎样？

> **参考答案**：`g_cubes[]` 的填充构造函数不会运行，表保持 `.bss` 全零，`add_k` 会算出 `src[i] + g_cubes[i&15] = src[i] + 0`，结果系统性出错（不是崩溃）。这正是 [kernel.cpp:5-10](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/kernel.cpp#L5-L10) 注释强调的「跳过这一步，所有结果都会错」。

### 4.3 多入口 `.vxbin`：`__vx_kentry_*` 与 VXSYMTAB 符号表

#### 4.3.1 概念说明

一个 GPU 程序里常常有多个 kernel（如 PoCL 编译出的多函数程序）。Vortex 要让**一个 `.vxbin` 同时承载多个命名 kernel**，主机能按名字解析出各自的入口 PC，再分别 launch。

为此需要三件事配合：

1. **设备侧标注**：用 `__kernel` 宏（背后是 `annotate("vortex.kernel")`）标记每个 kernel 函数，编译器后端为它生成一个 `__vx_kentry_<name>` 入口别名符号。
2. **打包工具收集**：`vxbin.py` 在链接后的 ELF 里扫描所有 `__vx_kentry_*` 符号，把「名字 → 入口地址」写进 `.vxbin` 末尾的 `VXSYMTAB` 尾部。
3. **主机侧解析**：运行时加载器（`module.cpp`，u3-l4 已讲）嗅探尾部 magic，解析出名字→PC 表，`vx_module_get_kernel(name)` 查表返回句柄，launch 时把该 PC 程序进 KMU。

一个精妙的设计是**向后兼容**：如果一个程序没有 `__vx_kentry_*` 符号（旧式单 kernel 程序），`vxbin.py` 就**不追加**任何尾部，生成的 `.vxbin` 与旧格式逐字节相同。

#### 4.3.2 核心流程

```text
源码：__kernel void add_k(...)  ──clang 后端──▶  符号 __vx_kentry_add_k = <PC>
                                                        │
              ELF 里所有 __vx_kentry_*  ──vxbin.py──▶  VXSYMTAB 尾部
                                                        │
              .vxbin = [min_vma][max_vma][image][VXSYMTAB?]
                                                        │
              主机 module.cpp 嗅探尾部 magic ──▶  名字→PC 表
                                                        │
              vx_module_get_kernel("add_k") ──▶  vx_kernel_h（PC 已缓存）
                                                        │
              vx_enqueue_launch ──▶ 把 PC 写入 KMU ──▶ VX_CSR_CTA_ENTRY
```

#### 4.3.3 源码精读

**(a) `__kernel` 宏驱动入口别名** — [sw/kernel/include/vx_spawn2.h:20-24](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn2.h#L20-L24)

```cpp
// `annotate("vortex.kernel")` 驱动 kernel 调用约定，以及后端为 launch
// 发射的 `__vx_kentry_<name>` 别名。`used`/`retain` 保留函数体：设备按地址
// 派发，没有任何静态引用，否则会被 --gc-sections 删掉。
#define __kernel extern "C" __attribute__((annotate("vortex.kernel"), used, retain))
```

要点：

- `annotate("vortex.kernel")` 是给 Vortex 定制 clang 后端的信号，后端据此决定 kernel 调用约定并发射 `__vx_kentry_<name>` 别名。
- `used`/`retain` 防止链接器 `--gc-sections` 把「没有被静态调用」的 kernel 函数体删掉——因为设备是**按地址派发**的，普通引用分析看不到它。
- `extern "C"` 保证符号名不被 C++ name-mangle。

测试里的三个 kernel 就是这样声明（[tests/regression/multikernel/kernel.cpp:63-87](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/kernel.cpp#L63-L87)）：

```cpp
__kernel void add_k(kernel_arg_t* __UNIFORM__ arg) { ... }
__kernel void mul_k(kernel_arg_t* __UNIFORM__ arg) { ... }
__kernel void acc_k(kernel_arg_t* __UNIFORM__ arg) { ... }
```

**(b) `vxbin.py` 扫描入口符号** — [sw/kernel/scripts/vxbin.py:67-89](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/scripts/vxbin.py#L67-L89)

```python
def get_kernel_entries(elf_file):
    # 工具链为每个 vortex.kernel 函数发射一个 "__vx_kentry_<kernel>" 别名；
    # 运行时 vx_module_get_kernel(<kernel>) 解析到它的地址。
    # 约定的单 kernel 入口 "kernel_main" 以公共名 "main" 暴露。
    cmd = ['readelf', '-s', '-W', elf_file]
    output = subprocess.check_output(cmd, universal_newlines=True)
    regex = re.compile(
        r'\s*\d+:\s+([0-9a-fA-F]+)\s+\d+\s+\S+\s+\S+\s+\S+\s+\S+\s+'
        r'__vx_kentry_(\S+)$')
    entries = []
    seen = set()
    for line in output.splitlines():
        match = regex.match(line)
        if match:
            name = match.group(2)
            if name == 'kernel_main':
                name = 'main'              # 旧式单入口的公共名
            if name in seen:
                continue
            seen.add(name)
            entries.append((name, int(match.group(1), 16)))
    return entries
```

注意 `kernel_main` 被重命名为公共名 `main`——这就是「`kernel_main` 命名约定」的实际落点。它**不是** `vx_start.S` 里的一条指令，而是 `vxbin.py` 里的一个字符串映射。

**(c) VXSYMTAB 尾部的磁盘布局** — [sw/kernel/scripts/vxbin.py:91-107](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/scripts/vxbin.py#L91-L107)

```python
def build_symtab_footer(entries):
    # VXSYMTAB 尾部布局（被 module.cpp 的 Module::load_bytes 消费）：
    #   [字符串团：名字背靠背拼接]
    #   [条目：N x { name_off:u32, name_len:u16, _pad:u16, pc:u64 }]
    #   [n_symbols : u32]
    #   [magic     : 8 字节 'VXSYMTAB']
    string_blob = b''
    offsets = []
    for name, _pc in entries:
        offsets.append(len(string_blob))
        string_blob += name.encode('utf-8')
    footer = bytearray(string_blob)
    for (name, pc), off in zip(entries, offsets):
        footer += struct.pack('<IHHQ', off, len(name.encode('utf-8')), 0, pc)
    footer += struct.pack('<I', len(entries))
    footer += b'VXSYMTAB'
    return bytes(footer)
```

每个条目结构（小端）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `name_off` | u32 | 名字在字符串团里的偏移 |
| `name_len` | u16 | 名字字节长度 |
| `_pad` | u16 | 对齐填充 |
| `pc` | u64 | kernel 入口地址 |

尾部最后是 `n_symbols`（u32）和 8 字节 magic `'VXSYMTAB'`。主机加载器从文件末尾嗅探这个 magic，再向前解析——这正是 u3-l4 讲过的「从尾部嗅探」机制。

**(d) 拼装整个 `.vxbin`** — [sw/kernel/scripts/vxbin.py:109-144](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/scripts/vxbin.py#L109-L144)

```python
def create_vxbin_binary(input_elf, output_bin, objcopy_path):
    min_vma, max_vma = get_vma_size(input_elf)
    ...
    # 符号表尾部：把每个 kernel 名字映射到入口地址。
    footer = b''
    entries = get_kernel_entries(input_elf)
    if entries:                            # 没有 __vx_kentry_* 就不追加尾部
        footer = build_symtab_footer(entries)

    with open(output_bin, 'wb') as bin_file:
        bin_file.write(min_vma_bytes)      # [min_vma : u64]
        bin_file.write(max_vma_bytes)      # [max_vma : u64]
        bin_file.write(binary_data)        # [image]
        bin_file.write(footer)             # [VXSYMTAB?]  可选
```

于是磁盘布局为：

\[ \text{.vxbin} = \underbrace{\text{min\_vma}}_{8B}\,\underbrace{\text{max\_vma}}_{8B}\,\text{image}\,[\,\text{VXSYMTAB}\,]^{?} \]

当 `entries` 为空（无 `__vx_kentry_*`）时，`footer` 是空串，二进制与旧式单入口格式**逐字节相同**。主机侧对无 footer 的二进制会合成一个 `"main" → min_vma` 的单入口（见 u3-l4 与设计文档 [kernel_entry_and_dispatch.md:40-45](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/kernel_entry_and_dispatch.md#L40-L45)）。

**(e) 链接脚本保留 `.vx_entry`** — [sw/kernel/scripts/link64.ld:56-61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/scripts/link64.ld#L56-L61)

```ld
.text :
{
  /* 多入口 .vxbin 的 per-kernel KMU 入口 stub。只经 VXSYMTAB 尾部可达，
     链接器看不到——KEEP 让 --gc-sections 不删它们。 */
  KEEP (*(.vx_entry .vx_entry.*))
  ...
```

`.vx_entry` 段是后端可能为每个 kernel 发射的入口 stub 所在处。因为它们只通过 `VXSYMTAB` 尾部（运行时解析）被引用，链接器的静态可达性分析看不到，所以必须 `KEEP` 防 `--gc-sections` 删除。`link32.ld` 与 `link64.ld` 都保留了它（[link64.ld:61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/scripts/link64.ld#L61)）。

#### 4.3.4 代码实践：观察多入口解析

1. **目标**：理解主机如何按名字从同一个 `.vxbin` 解析出三个不同 kernel。
2. **操作步骤**：阅读 [tests/regression/multikernel/main.cpp:88-102](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/main.cpp#L88-L102)，关注三处 `vx_module_get_kernel` 与一处「故意查不到」的负向断言。
3. **需要观察的现象**：
   - `add_k`/`mul_k`/`acc_k` 三个名字分别解析到三个**不同**的句柄（L95-L96 断言它们互不相同）。
   - 查一个不存在的名字 `no_such_kernel` 必须失败（L100-L102）——这证明是真的 footer 查表，而不是「任何名字都退化成 `main`」的回退。
4. **预期结果**：三个 kernel 各自 launch、各自写回独立缓冲，最后全部校验通过打印 `PASSED!`。
5. 想进一步看符号表，可在 build 出的 `kernel.vxbin` 上用 `xxd` 查看文件末尾 8 字节是否为 `VXSYMTAB`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：如果一个 `.vxbin` 没有任何 `__vx_kentry_*` 符号，`vxbin.py` 会生成什么样的文件？主机侧又如何处理？

> **参考答案**：`get_kernel_entries` 返回空列表，`footer` 保持空串，`vxbin.py` 不追加任何尾部——文件就是 `[min_vma][max_vma][image]`，与旧式单入口格式逐字节相同。主机加载器嗅探不到 `VXSYMTAB` magic，于是合成单个 `"main" → min_vma` 入口，旧程序无需改动即可工作（见 [vxbin.py:132-136](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/scripts/vxbin.py#L132-L136) 与设计文档 §2）。

**练习 2**：为什么 `__kernel` 宏要带 `used` 和 `retain`？

> **参考答案**：kernel 是按地址（经 KMU→CSR）派发的，源码里没有任何地方静态「调用」它，普通编译流程会认为它不可达，开启 `--gc-sections`（Makefile 里 `-ffunction-sections`）时会把它删掉。`used`/`retain` 告诉编译器/链接器强制保留函数体（见 [vx_spawn2.h:20-24](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn2.h#L20-L24)）。

### 4.4 派发窗口、CTA rewind 与 `.option norvc`

#### 4.4.1 概念说明

本模块解决本讲实践任务的核心问题：**为什么 `jalr` 必须用 `.option norvc` 强制成 4 字节？**

答案藏在调度器的一个优化里：prologue 最开头那些设置（SATP/gp/sp/tp/构造函数）对一个 warp 槽位而言只需做一次；但**同一个 warp 槽位会先后承载多个 CTA**（一个 CTA 跑完退休，下一个 CTA 进驻）。调度器为新 CTA 重新派发时，不需要回到 prologue 最开头重做所有设置，而是**把 PC 回卷（rewind）到最后那 5 条指令的「派发窗口」**——因为只有「入口 PC + 参数指针」是每个 CTA 不同的，其余环境可以复用。

这个优化成立的前提是：**派发窗口必须是固定长度，调度器才能用固定偏移回卷。** 源码把这个窗口设计成 5 条指令、恰好 20 字节：

| # | 指令 | 编码长度 |
|---|------|---------|
| 1 | `csrr s11, VX_CSR_CTA_ENTRY` | 4 字节（CSR 指令不可压缩） |
| 2 | `csrr a0, VX_CSR_MSCRATCH` | 4 字节 |
| 3 | `jalr ra, s11` | **可被 RVC 压成 2 字节** → 必须强制 4 字节 |
| 4 | `wsync`（自定义 `.insn`） | 4 字节 |
| 5 | `tmc x0`（自定义 `.insn`） | 4 字节 |

`jalr` 是这 5 条里**唯一**可能被 RVC 压缩成 2 字节（`c.jalr`）的指令。如果不禁止，窗口就变成 18 字节，调度器的固定回卷偏移就会指错地方。所以源码用 `.option norvc` 临时关掉 RVC，强制 `jalr` 占 4 字节，保证窗口恒为 20 字节。

#### 4.4.2 核心流程

```text
warp 槽位的生命周期：

  CTA#0 进驻 ──▶ 从 _start/__vx_cta_entry 跑完整 prologue
                   （SATP/gp/sp/tp/init_array 一次性完成）
                ──▶ 进入派发窗口：csrr; csrr; jalr──▶ kernel#0 ──ret──▶ wsync; tmc(退休)

  CTA#1 进驻 ──▶ 调度器把 PC 回卷 20 字节，回到派发窗口开头
                ──▶ csrr(新入口); csrr(新参数); jalr──▶ kernel#1 ──ret──▶ wsync; tmc(退休)
                   （不重做 SATP/gp/sp/tp/init_array）

  ↑ 固定 20 字节窗口 = 5 × 4 字节，依赖 jalr 不被 RVC 压缩
```

#### 4.4.3 源码精读

派发窗口与 rewind 的注释与代码在 [sw/kernel/src/vx_start.S:119-134](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L119-L134)：

```asm
  # Per-CTA dispatch window: 调度器为新 CTA 把 PC 回卷到这整整 5 条（20 字节）。
  # 从 CSR 读 kernel 入口与 kargs 指针（都由 KMU 每个 CTA 重新注入），再调用。
  csrr  s11, VX_CSR_CTA_ENTRY
  csrr  a0, VX_CSR_MSCRATCH
  # 临时关掉 RVC，强制一条 4 字节调用——这是窗口里唯一可压缩的指令；
  # csrr/wsync/tmc 恒为 4 字节。
  .option push
  .option norvc
  jalr  ra, s11
  .option pop
  .insn r RISCV_CUSTOM0, 7, 0, x0, x0, x0  # wsync
  .insn r RISCV_CUSTOM0, 0, 0, x0, x0, x0  # tmc x0
```

几个要点：

- `.option push`/`.option pop` 把 `norvc` 的作用域**严格限制在这条 `jalr`**，不影响程序其他部分的 RVC 优化——后续的 `wsync`/`tmc` 是自定义 `.insn`，本就是 4 字节。
- CSR 指令（`csrr`）的编码格式决定了它**不在 RVC 压缩集**里，恒为 4 字节，所以无需额外处理。
- `wsync`（排空本 warp 挂起指令）与 `tmc x0`（关闭 warp、退休）是 Vortex 自定义指令，用 `.insn r RISCV_CUSTOM0, ...` 显式编码（`RISCV_CUSTOM0 = 0x0B`，定义在 [common.h:14](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/common.h#L14)）。这两条是 warp 退休前的收尾，保证 kernel 发出的所有访存/计算都落地后再关 warp。

> **待确认**：调度器回卷的精确机制（回卷偏移如何硬编码、rewind 触发条件）属于调度器侧（u6-l1），本讲从设备入口源码确认的是「窗口必须 20 字节」这一约束；调度器实现细节建议结合 u6-l1 的 `scheduler.cpp` 一起读。

#### 4.4.4 代码实践（对应本讲主实践任务）

1. **目标**：写出 `__vx_cta_entry` 依次做的寄存器/段设置，并解释为何 dispatch 的 `jalr` 要 `.option norvc`。
2. **操作步骤**：
   - 通读 [sw/kernel/src/vx_start.S:67-135](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L67-L135)。
   - 按执行顺序列出设置：①（VM）`satp`；②（NEED_GP）`gp`；③ `sp`；④（NEED_TLS）`tp`+`__init_tls`；⑤（NEED_INITFINI）`__libc_init_array`；⑥ `s11`=`CTA_ENTRY`、`a0`=`MSCRATCH`、`jalr`、`wsync`、`tmc`。
   - 数派发窗口的指令与字节数：`csrr`(4) + `csrr`(4) + `jalr`(4，因 norvc) + `wsync`(4) + `tmc`(4) = 20 字节。
3. **需要观察的现象**：除 `jalr` 外，其余 4 条都不可被 RVC 压缩；只有 `jalr` 需要显式 `norvc`。
4. **预期结果（对「为何 norvc」的回答）**：调度器把同一 warp 槽位上新 CTA 的 PC 回卷到派发窗口开头，依赖窗口为固定 20 字节；`jalr` 是窗口里唯一可被 RVC 压成 2 字节的指令，若不强制成 4 字节，窗口会缩成 18 字节，回卷偏移就会错位，导致新 CTA 从错误地址取指。`.option norvc` 以 `push/pop` 精确限定在这条 `jalr`，既保住窗口长度，又不影响程序其余部分的 RVC 优化。
5. 进阶验证（待本地验证）：对 build 出的 ELF 跑 `riscv64-objdump -d`，定位 `__vx_cta_entry`，确认派发窗口 5 条指令恰好占 20 字节，且 `jalr` 编码是 4 字节（操作码最低两位为 `11`，而非 RVC 的压缩前缀）。

#### 4.4.5 小练习与答案

**练习 1**：如果有人不小心把 `.option norvc` 删掉，且编译器恰好把 `jalr ra, s11` 压成了 `c.jalr`，会发生什么？

> **参考答案**：派发窗口从 20 字节缩成 18 字节。调度器为新 CTA 回卷固定 20 字节偏移时会落在错误位置——可能落到 `wsync`/`tmc` 中间或下一条指令的半截，导致取指错位、kernel 入口读不到正确的 `VX_CSR_CTA_ENTRY`，行为未定义。这是一个「在单 CTA 测试里可能恰好不暴露、在多 CTA 回卷时才爆」的隐蔽 bug。

**练习 2**：为什么 `wsync` 和 `tmc` 不需要 `.option norvc`？

> **参考答案**：它们用 `.insn r RISCV_CUSTOM0, ...` 显式给出 32 位自定义指令编码，本就是 4 字节，且不在 RVC 压缩集中；`csrr` 同理（CSR 指令恒 4 字节）。窗口里只有标准 `jalr` 属于 RVC 可压缩范围，所以只有它需要 `norvc`。

## 5. 综合实践

把本讲三块知识串起来：**追踪「一个命名 kernel 从主机 launch 到设备 `__vx_cta_entry` 派发」的完整入口链**。

1. **准备**：阅读 [tests/regression/multikernel/](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/kernel.cpp) 的设备侧与主机侧。
2. **画三张图**：
   - **打包图**：`__kernel void add_k` →（clang 后端）→ `__vx_kentry_add_k` →（`vxbin.py`）→ `VXSYMTAB` 尾部条目 `(name="add_k", pc=<入口>)`。
   - **加载图**：主机 `vx_module_load_file` → 嗅探 `VXSYMTAB` magic → 解析名字→PC 表 → `vx_module_get_kernel("add_k")` 返回句柄 → `vx_enqueue_launch` 把 PC 写入 KMU（u3-l4）。
   - **设备入口图**：KMU 把 PC 写入 `VX_CSR_CTA_ENTRY`、参数指针写入 `VX_CSR_MSCRATCH`，warp PC 重置到 `_start`/`__vx_cta_entry` → prologue 设 SATP/gp/sp/tp/init_array → `csrr s11, CTA_ENTRY; csrr a0, MSCRATCH; jalr ra, s11` → `add_k(arg)` → ret → `wsync; tmc`。
3. **标注三个「坑」**：① TLS 步长必须用 `__tls_block_size`（`.tdata+.tbss`），不能只用 `.tbss`；② 跳过 `__libc_init_array` 会让 `g_cubes[]` 全零；③ 派发窗口必须 20 字节（`jalr` 用 `norvc`）。
4. **运行验证**：在 build 目录用 `./ci/blackbox.sh --driver=simx --app=multikernel`（或对应回归入口）跑测，确认打印 `PASSED!`（待本地验证具体命令名）。
5. **反思**：如果把 `add_k` 改名成 `add_kernel`，你需要改哪些地方？（答案：源码改 `__kernel void add_kernel`，主机侧 `vx_module_get_kernel` 的名字串改掉；`vxbin.py` 与 prologue 都无需改——它们靠符号名自动收集与 CSR 派发。）

## 6. 本讲小结

- Vortex 用同一份 `vx_start.S` 编出 `libvortex`（旧式）与 `libvortex2`（KMU）两个设备侧运行时库，靠 `KMU_ENABLE` 切换；多入口程序用 `libvortex2`。
- KMU 模型下，ELF 入口 `_start` 与统一 prologue `__vx_cta_entry` 同址（image 基址）；prologue 把 warp lane 提升到 C 可调用状态：设 `satp`/`gp`/`sp`/`tp`+TLS、跑 `__libc_init_array`。
- 派发是 **CSR 驱动**的：kernel 入口 PC 来自 `VX_CSR_CTA_ENTRY`、参数指针来自 `VX_CSR_MSCRATCH`（均由 KMU 每个 CTA 注入），prologue 把参数放进 `a0` 后 `jalr ra, s11` 跳到 kernel。
- 多入口 `.vxbin` 靠 `__kernel` 宏（`annotate("vortex.kernel")`）→ 后端发射 `__vx_kentry_<name>` → `vxbin.py` 收集进 `VXSYMTAB` 尾部；无入口符号时不追加尾部，与旧格式逐字节兼容。`kernel_main` 只是 `vxbin.py` 里重命名为 `main` 的命名约定，不是 `vx_start.S` 里的指令。
- 派发窗口是 5 条指令、固定 20 字节；`jalr` 是窗口里唯一可被 RVC 压缩的指令，故用 `.option norvc`（`push/pop` 精确限定）强制 4 字节，以维持调度器的 CTA rewind 偏移。
- kernel 返回后经 `wsync`（排空挂起指令）与 `tmc x0`（关闭 warp、退休）收尾。

## 7. 下一步学习建议

- **u4-l2（SIMT 控制指令与 warp 调度 API）**：本讲的 `tmc`/`wsync` 只是冰山一角。接下来系统学习 `vx_intrinsics.h`/`vx_spawn.h` 暴露的 TMC/WSPAWN/SPLIT/JOIN/PRED 等 warp 控制内联函数，理解 warp 派生与分支发散。
- **u6-l1（Warp 调度器、CTA 派发与屏障）**：从调度器侧看「CTA rewind」的另一端——`scheduler.cpp`/`cta_dispatcher.cpp` 如何把 CTA 拆成 warp、如何回卷 PC，与本讲的派发窗口对齐。
- **u3-l4（主机→设备启动流程与 .vxbin 加载）**：若你对主机侧 `module.cpp` 如何解析 `VXSYMTAB` 尾部、如何把 PC 写入 KMU 还想加深，可重读该讲。
- **延伸阅读**：[docs/designs/kernel_entry_and_dispatch.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/kernel_entry_and_dispatch.md)（注意其 §1 描述的 stub 模型与当前 CSR 实现的差异）、[docs/designs/cta_clustering_and_dispatch.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/cta_clustering_and_dispatch.md)（KMU 的 CTA 派发细节）。
