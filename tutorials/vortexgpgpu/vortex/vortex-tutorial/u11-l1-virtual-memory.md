# 虚拟内存子系统

## 1. 本讲目标

Vortex 是一台 RISC-V GPGPU，它的内核程序、命令处理器 DMA、缓存层次都用「地址」去访问内存。当 `VX_CFG_VM_ENABLE` 打开后，这些地址就不再是物理地址（PA），而是虚拟地址（VA），需要一套**虚拟内存子系统**把 VA 翻译成 PA。

本讲学完后，你应该能够：

1. 说清 Vortex 虚拟内存的 **v3 模型**：翻译发生在哪两个位置、主机在传输时是否参与翻译。
2. 读懂 RISC-V **Sv32 / Sv39 页表格式**，手算一次页表遍历（PTW）。
3. 理解 SimX 中 **TLB**（全相联 CAM + MRU 替换）与 **PTW 状态机**（层级计数器 FSM）的实现。
4. 理解主机运行时 **VMManager** 如何分配物理页、铸造虚拟地址、在主机侧维护「影子页表」并批量 flush 到设备。
5. 知道 RTL 当前**仍硬编码 Sv32**这一待确认点，以及 SimX 与 RTL 在 VM 上的差异。

本讲承接 u8-l3（访存合并、本地内存与 DRAM），向下打开 MMU/TLB/PTW 这一层；同时呼应 u3-l2（设备、缓冲区与内存管理）中提到的 VMManager，把它从黑盒变成白盒。

---

## 2. 前置知识

### 2.1 为什么要虚拟内存

在真实的 GPU/操作系统中，「程序看到的地址」和「内存芯片上的地址」往往不同：

- **隔离与保护**：每个进程有自己的 VA 空间，互不干扰。
- **地址随机化**：把缓冲区映射到随机的 VA，提升安全性（Vortex 用 `VORTEX_RANDOMIZE_VA` 支持）。
- **碎片整理**：VA 连续、PA 可以离散。

翻译由一张**页表（page table）**描述：把 VA 按页（Vortex 固定 4 KB）切成段，每一段记录它对应的物理页号（PPN）和权限标志。MMU（内存管理单元）每次访问都查页表，把 VA 翻成 PA。

### 2.2 RISC-V 的两级（或多级）页表

直接用一张大表存「每个页的映射」会占太多内存，所以 RISC-V 把页表做成**多级树**：

- **Sv32**（RV32）：2 级，VA 32 位，每张表 1024 项，每项 4 字节。
- **Sv39**（RV64）：3 级，VA 39 位（带符号扩展到 64 位），每张表 512 项，每项 8 字节。

查找一次 VA 要逐级读「页表项」（PTE，Page Table Entry）：先读根表里的某项，如果它指向下一级表就继续往下；如果它是「叶子」（leaf，带 R/W/X 权限位）就直接得到 PPN。

### 2.3 TLB：页表遍历的缓存

多级遍历意味着一次访存要引发多次内存读（Sv32 要 2 次，Sv39 要 3 次）。为避免每次都走一遍，MMU 内部有一个**TLB**（Translation Lookaside Buffer），缓存「最近用过的 VPN→PPN」。命中直接出 PA，未命中才触发 PTW。

### 2.4 Vortex 的关键背景

- **SATP**（Supervisor Address Translation and Protection）是 RISC-V 的一个 CSR，指向页表根、并声明地址模式（BARE/Sv32/Sv39）。Vortex 在内核启动时为每个核写一次 `satp`。
- Vortex 的执行模型与缓存层次已在 u8 系列讲过：LSU→L1→L2→L3→DRAM，本讲插入的 MMU 就在 **LSU/coalescer 之后、dcache 之前**。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [docs/designs/virtual_memory_subsystem.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/virtual_memory_subsystem.md) | 架构参考文档，定义 v3 模型、ISA/CSR、RTL/SimX 组件与「有意简化」清单。 |
| [sw/common/vm_types.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/vm_types.h) | 纯主机侧算术：`SATP_t` / `PTE_t` / `vAddr_t` 值类，Sv32/Sv39 分支，被 SimX MMU 与主机运行时共享。 |
| [sim/simx/mem/mmu_tlb.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu_tlb.cpp) | SimX 的 TLB：全相联线性扫描 CAM、MRU 替换、4 个性能计数器。 |
| [sim/simx/mem/mmu.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp) | SimX 的每核 MMU SimObject：旁路、TLB 查询、内嵌 PTW 状态机（PTE fetch 经由同一缓存端口）。 |
| [sw/runtime/common/vm.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp) | 主机运行时 `VMManager`：PA 分配 + VA 分配 + 主机影子页表 + 批量 flush。 |
| [sw/runtime/common/vm.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.h) | `VMManager` 与 `DeviceMemIO` 接口定义，解释影子页表设计动机。 |
| [sim/common/cmd_processor.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp) | CP DMA 的软件页表遍历 `cp_translate()`，与 `VMManager::page_table_walk` 镜像。 |
| [sw/kernel/src/vx_start.S](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S) | 设备侧启动序列：为每个核 `csrw satp` 指向页表根。 |

> 约定：本讲的永久链接均指向当前 HEAD `d76b7f24e`。RTL MMU（`hw/rtl/mem/VX_mmu*.sv`）会在文中作为对照出现，但不展开精读。

---

## 4. 核心概念与源码讲解

### 4.1 Vortex v3 虚拟内存模型

#### 4.1.1 概念说明

Vortex 的虚拟内存并非「一个集中式 MMU 翻译所有流量」。设计文档把它明确拆成**两个翻译点**：

1. **计算核 MMU**（RTL + SimX）：翻译内核 LSU/fetch 的 VA，是典型的硬件 MMU。
2. **CP DMA 软件遍历器**：命令处理器（CP）在搬运 `CMD_MEM_*`（主机↔设备的 DMA 命令）时，用软件遍历页表把操作数 VA 翻译成 PA。

关键的工程取舍：**主机运行时 API 只认 VA，主机在传输时刻不做翻译**——翻译都交给 CP。这一点与 u3-l2 讲过的「copy_to_dev 经 CP 的 DMA 通路」完全吻合。

VM 由 `VX_CFG_VM_ENABLE` 开关控制，**默认关闭**（[docs/designs/virtual_memory_subsystem.md:18-19](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/virtual_memory_subsystem.md#L18-L19)）。关掉时整个子系统被 `#ifdef` 掉，地址直达缓存。

#### 4.1.2 核心流程

设计文档用一张图概括了 v3 模型的两条翻译路径：

```
  kernel LSU/fetch VA                         runtime (host)
        │                                     ──────────────
        ▼                                     VMManager: 分配 PA, 铸造 VA,
   per-core MMU (coalescer 之后)               建立主机影子页表,
        │  satp[31]? BARE → 旁路              批量 flush 脏 PT 页
        ▼                                            │
   TLB CAM ── 命中 ─► PA ─► cache               CP_SATP_LO/HI ──► CP cp_translate()
        │ 未命中                                 (Sv32/Sv39 软件遍历设备页表)
        ▼
   PTW 遍历 (PTE 读经同一缓存端口) ─► 填充 TLB ─► 以 PA 重放
```

端到端的一次访存（对应文档第 6 节）：

1. LSU lane → coalescer/adapter → 每核 dcache MMU。若 `satp` 模式为 BARE（或 SATP 未设置）则**旁路**翻译；否则 VA 进 TLB。
2. TLB 命中 → 以 PA 进缓存；TLB 未命中 → 启动 PTW。
3. PTW 逐级读 PTE，**PTE 取数走的和普通 load 同一个下游缓存端口**（RTL 用 `VX_mem_arb` 合并；SimX 用 `ReqOut[0]` + 一个 PTW tag 标记）。
4. 填充 TLB 后，原请求以 PA 重放到缓存。
5. SATP 在启动时由 `vx_start.S` 写一次；CP 的 `CP_SATP` 由主机在 `cp_init` 时写。

#### 4.1.3 源码精读

**(1) 每核 MMU 的接线位置** —— MMU 被实例化在每个核里，位于 LSU 适配器与核的 dcache/icache 端口之间，且**一个核一个 MMU（不是每个 LSU slice 一个）**：

[sim/simx/core.cpp:192-216](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L192-L216) 中文说明：在 `#ifdef VX_CFG_VM_ENABLE` 下创建 dcache MMU（多端口）与 icache MMU（单端口），把「LSU 内存侧 → dcache MMU → 核 dcache 端口」串成链路；icache MMU 的下游绑到核 icache 端口。

**(2) 设备侧 SATP 编程** —— 每个核在 CTA 入口 `__vx_cta_entry` 处把页表基址写进 `satp`：

[sw/kernel/src/vx_start.S:68-89](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L68-L89) 中文说明：取 `VX_MEM_PAGE_TABLE_BASE_ADDR`，右移 `VX_VM_PAGE_LOG2_SIZE`（12）得到 PPN，再把模式位（Sv32 是 bit31，Sv39 是 bit63）或上去，最后 `csrw satp, t0`。注释解释此时页表已被主机运行时填好、栈/PT 区按 `need_trans` 旁路，所以先写 SATP 再设栈是安全的。

**(3) CP 的软件遍历镜像主机侧** —— CP DMA 翻译与 `VMManager::page_table_walk` 完全一致：

[sim/common/cmd_processor.cpp:157-201](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cmd_processor.cpp#L157-L201) 中文说明：`cp_translate(vaddr, physical)`——`physical` 为真或 SATP=0/BARE 直接返回 VA；否则按 `VX_VM_PT_LEVEL` 逐级读 PTE（通过 `hooks_.dram_read`），找到叶子后重建 PA，对超页（megapage/gigapage）用 `off_mask` 把低位偏移从 VA 取回。

#### 4.1.4 代码实践

**实践目标**：在 SimX 上亲手打开 VM，跑一个回归用例，并对比「开启翻译」与「BARE 模式」的行为差异。

**操作步骤**：

1. 用 `ci/blackbox.sh` 之外更直接的方式：仓库在 [ci/testcases/vm.yaml](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/vm.yaml) 里声明了 `vm` 测试类别，默认 `configs: -DVX_CFG_VM_ENABLE`，驱动**仅 simx**，跑 `sgemm / diverge / dogfood / raycast / gfx_draw3d`，xlen 覆盖 32 与 64。
2. 手动等价命令（在 build 目录）：
   ```bash
   make -C tests/regression/sgemm run-simx CONFIGS="-DVX_CFG_VM_ENABLE"
   ```
3. 再跑一次 BARE 模式（vm.yaml 的 `isa-6..isa-10` 用 `configs+: -DVX_CFG_VM_ADDR_MODE=BARE`）：
   ```bash
   make -C tests/regression/sgemm run-simx CONFIGS="-DVX_CFG_VM_ENABLE -DVX_CFG_VM_ADDR_MODE=BARE"
   ```

**需要观察的现象**：

- 开启 VM 时，SimX 启动会打印 `VMManager Initialization...`（见 [vm.cpp:77](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L77)）。
- 用 `--perf=1` 运行时，VM/MMU 性能计数器（`VX_CSR_MPM_TLB_*` / `VX_CSR_MPM_PTW_*`）会有非零值；BARE 模式下这些计数器应为 0（因为 `needs_translation` 恒假，TLB 不被查询）。

**预期结果**：两种模式下程序都应打印 `PASSED!`、退出码 0——这验证了「VA 翻译」与「不翻译」对外可见的行为一致。RTL 行为**待本地验证**（设计文档第 8 节指出 RTL VM 的 rtlsim/xrt CI 行当前被注释，等待 RTL PTW 完成）。

#### 4.1.5 小练习与答案

**练习 1**：为什么主机运行时在 `vx_copy_to_dev` 时不做 VA→PA 翻译？
**答案**：因为主机 API 是 VA-only，翻译职责在 CP 的 DMA 软件遍历器（`cp_translate`）。主机只负责把 VA 写进命令环，CP 读到命令后用 `CP_SATP` 走页表翻成 PA，再发起真正的 DRAM 搬运。

**练习 2**：如果 `VX_CFG_VM_ENABLE` 关闭，本节提到的 MMU SimObject 还存在吗？
**答案**：不存在。`mmu.cpp` / `mmu.h` / `mmu_tlb.cpp` 整体被 `#ifdef VX_CFG_VM_ENABLE` 包裹，core.cpp 的 MMU 实例化也在同一宏下；关闭时走 `#else` 的直连旁路（[core.cpp:217-219](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/core.cpp#L217-L219)）。

---

### 4.2 Sv32 / Sv39 页表格式与数据结构

#### 4.2.1 概念说明

页表格式由 RISC-V 特权规范定义，Vortex 通过 `VX_types.toml` 把它固化成编译期常量（[VX_types.toml:48-54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L48-L54)）：

| 参数 | Sv32（XLEN=32） | Sv39（XLEN=64） |
| --- | --- | --- |
| `VX_VM_ADDR_MODE` | SV32 (0x1) | SV39 (0x8) |
| `VX_VM_PT_LEVEL` | 2 | 3 |
| `VX_VM_PTE_SIZE` | 4 字节 | 8 字节 |
| `VX_VM_PAGE_LOG2_SIZE` | 12（页 4 KB） | 12 |
| 每级 VPN 位数 | 10 | 9 |

每级 VPN 位数由「一张表里有多少项」取 log2 得到。设一张表大小为 `VX_VM_PT_SIZE`（等于页大小 4096）、每项 `VX_VM_PTE_SIZE` 字节，则：

\[
\text{VPN\_BITS} = \log_2\!\left(\frac{\text{VX\_VM\_PT\_SIZE}}{\text{VX\_VM\_PTE\_SIZE}}\right) = \log_2\!\left(\frac{4096}{\text{PTE\_SIZE}}\right)
\]

代入：Sv32 = \(\log_2(1024) = 10\)，Sv39 = \(\log_2(512) = 9\)。这与 [vm.cpp:40-41](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L40-L41) 注释一致。

#### 4.2.2 核心流程

三个值类分别建模 SATP、PTE、VA：

**SATP（页表根指针 + 模式）**：

- Sv32：`mode=bit[31]`，`asid=bits[30:22]`，`ppn=bits[21:0]`（22 位）。
- Sv39：`mode=bits[63:60]`，`asid=bits[59:44]`，`ppn=bits[43:0]`（44 位）。
- 页表根物理地址 = `ppn << 12`。

**PTE（页表项）**：低 8 位是权限标志，其中：

- `V`（bit0）有效；`R/W/X`（bit1/2/3）读/写/执行；`U`（bit4）用户态；`G/A/D`（bit5/6/7）全局/已访问/已脏。
- `R=W=X=0` 表示**非叶子**，其 PPN 指向下一级表；R/W/X 任一非零表示**叶子**。
- Sv32：`ppn=bits[31:10]`（22 位）；Sv39：`ppn=bits[53:10]`（44 位）。

**VA 切片**：

- Sv32：`vpn[1]=bits[31:22]`，`vpn[0]=bits[21:12]`，`pgoff=bits[11:0]`。
- Sv39：`vpn[2]=bits[38:30]`，`vpn[1]=bits[29:21]`，`vpn[0]=bits[20:12]`，`pgoff=bits[11:0]`。

RISC-V 规定的「**叶子判定与无效判定**」是 PTW 的语义核心（[mmu.cpp:84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L84) 与 [vm.cpp:370-381](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L370-L381) 都实现了同一逻辑）：

- 无效：`V=0`，或 `R=0 且 W=1`（「写了但不可读」是规范禁止的组合）→ 触发页错误。
- 叶子：`R/W/X` 任一为 1。
- 非叶子：`R=W=X=0`，PPN 指向下一级表。

#### 4.2.3 源码精读

**(1) 模式编码常量** —— 注意 SV39 的模式值是 0x8（不是 0x2）：

[sw/common/vm_types.h:27-29](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/vm_types.h#L27-L29) 中文说明：`#define BARE 0x0 / SV32 0x1 / SV39 0x8`，这三个值会同时被 SimX 的 `needs_translation` / `cp_translate` 与设备侧 `vx_start.S` 的 SATP 模式位使用。

**(2) SATP_t 的双构造函数** —— 一个从「原始 SATP 值」解析（读硬件/CP 返回值时用），一个从「基址+asid」打包（主机铸造 SATP 时用）：

[sw/common/vm_types.h:53-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/vm_types.h#L53-L64) 中文说明：按 `VX_VM_ADDR_MODE` 在编译期二选一解析；`address = ppn << VX_VM_PAGE_LOG2_SIZE`。

**(3) PTE_t 与 vAddr_t 的位切片**：

[sw/common/vm_types.h:146-161](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/vm_types.h#L146-L161) 中文说明：从原始 PTE 字节解析——Sv39 取 `ppn=bits[10:53]` 并额外解析 `N`/`PBMT`，Sv32 取 `ppn=bits[10:31]` 并断言高 32 位为 0；低 8 位经 `set_flags` 拆成 `v/r/w/x/u/g/a/d`。

[sw/common/vm_types.h:176-192](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/vm_types.h#L176-L192) 中文说明：把 VA 切成各级 VPN 与页内偏移 `pgoff`，`level` 记录级数（Sv32=2，Sv39=3）。

#### 4.2.4 代码实践

**实践目标**：手算一个 Sv32 地址翻译，验证你对位域的理解。

**操作步骤**：取 VA = `0x00401234`，假设 SATP 指向页表根 PPN = `0xF0000`（即根表位于 `0xF0000000`），且这是一个「恒等映射」（identity map，PA = VA）。

1. 切片：`vpn[1] = bits[31:22]`。`0x00401234` 的 bit22 在 `0x4`（bit23..20=0100）那组里为 1，故 `vpn[1] = 1`；`vpn[0] = bits[21:12] = 1`；`pgoff = 0x234`。
2. 第 1 级 PTE 地址 = `0xF0000 × 4096 + vpn[1] × 4 = 0xF0000000 + 4 = 0xF0000004`。
3. 读 L1 PTE，假设它是非叶子，PPN = `0xF0001`（指向下一级表 `0xF0001000`）。
4. 第 0 级 PTE 地址 = `0xF0001 × 4096 + vpn[0] × 4 = 0xF0001004`。
5. 读 L0 PTE，它是叶子，PPN = `0x401`，权限 `V|R|W|X = 0xF`。
6. PA = `0x401 × 4096 + pgoff(0x234) = 0x401234`，恰等于 VA（恒等映射成立）。

**需要观察的现象**：手动算出的 PTE 原始字节应为 `(ppn << 10) | flags = (0x401 << 10) | 0xF = 0x10040F`。

**预期结果**：与 `VMManager::page_table_walk(0x00401234)` 在恒等映射下的返回值一致。如果你手写一个小程序调用 `PTE_t(0x401000, 0xF)` 构造再读 `.pte_bytes`，应得到 `0x10040F`。

> 说明：以上是「示例代码 / 手算」，不是仓库里现成的测试。仓库里实际的恒等映射由 `install_identity_map` 自动选择超页粒度，PTE 内容由它生成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Sv39 的模式值是 `0x8` 而 Sv32 是 `0x1`？
**答案**：这是 RISC-V 特权规范规定的 SATP 模式编码（Sv32 占 1 位 mode 字段所以是 1；Sv39 的 mode 字段在 64 位 SATP 的高 4 位，规范赋值为 8）。设备侧 `vx_start.S` 对 Sv39 用 `1<<63`、对 Sv32 用 `1<<31` 就是把对应模式值放到正确位置。

**练习 2**：一个 PTE 的 `V=1, R=0, W=1, X=0` 是否合法？
**答案**：不合法。`R=0 且 W=1` 是规范禁止的组合，会被判为无效并触发页错误（[mmu.cpp:84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L84)）。

---

### 4.3 SimX 的 TLB：全相联 CAM 与 MRU 替换

#### 4.3.1 概念说明

TLB 是「最近用过的 VPN→PPN」的高速缓存。Vortex 的 TLB 配置很朴素（设计文档第 2 节）：

- **单层平铺**：一个 `VX_CFG_TLB_SIZE`（默认 32）项的全相联 CAM，**没有 L2/L3 TLB**。
- 每个核有**两个** TLB：一个在 dcache MMU、一个在 icache MMU（指令 fetch 用）。
- 每项记录 `{valid, mru, vpn, ppn, flags}`。

SimX 用一个小的 `std::vector<Entry>` 线性扫描来「模拟」CAM 的并行查找行为（[mmu_tlb.h:50-52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu_tlb.h#L50-L52)），项数小（32）所以每周期一次线性扫描是可接受的。

#### 4.3.2 核心流程

三个操作：

1. **lookup(vpn)**：遍历所有项，找到 `valid && vpn 匹配` 的项，置 `mru=true`，返回 `{hit, ppn}`；没找到返回 `{false, 0}`。每次调用 `reads_++`，命中 `hits_++`，未命中 `misses_++`。
2. **fill(vpn, ppn, flags)**：安装一个新翻译。替换策略是**伪 MRU**：
   - 优先选无效槽；
   - 否则选第一个 `mru=false` 的槽；
   - 若所有项都 `valid && mru=true`，清空所有 MRU 位并踢掉第 0 槽。
   - 被踢的槽原来有效则 `evictions_++`。
3. **flush()**：全部置 `valid=false`，等价于 `sfence.vma`。`set_satp()` 会调用它。

#### 4.3.3 源码精读

**(1) lookup**：

[sim/simx/mem/mmu_tlb.cpp:21-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu_tlb.cpp#L21-L32) 中文说明：线性扫描，命中置 MRU 并返回 PPN，统计读/命中/未命中。

**(2) fill 的三级回退替换**：

[sim/simx/mem/mmu_tlb.cpp:34-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu_tlb.cpp#L34-L60) 中文说明：先找空槽 → 再找非 MRU 槽 → 都没有则清 MRU 位并牺牲 0 号槽；注释强调这是一种近似 MRU 的策略。

**(3) flush**：

[sim/simx/mem/mmu_tlb.cpp:62-67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu_tlb.cpp#L62-L67) 中文说明：所有项失有效，对应 `sfence.vma`。

**(4) flush 的触发点**：

[sim/simx/mem/mmu.cpp:39-42](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L39-L42) 中文说明：`set_satp()` 把传入值解析成 `SATP_t` 并 `tlb_.flush()`，因为换了页表根，旧翻译全部失效。

#### 4.3.4 代码实践

**实践目标**：用 VM 性能计数器量化 TLB 行为。

**操作步骤**：开启 VM 跑 sgemm，加上 `--perf=1`，运行结束后查看报告里的 VM/MMU 计数器。它们定义在 [VX_types.toml:738-749](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L738-L749)：`TLB_READS / TLB_HITS / TLB_MISSES / TLB_EVICTS / PTW_WALKS / PTW_LATENCY`。

**需要观察的现象**：TLB 命中率 = `TLB_HITS / TLB_READS`；`TLB_MISSES` 应大致等于 `PTW_WALKS`（每次未命中触发一次 PTW）；访问的数据页越多，`TLB_EVICTS` 越大。

**预期结果**：在小规模 sgemm 上命中率应很高（工作集远小于 32 页），`PTW_WALKS` 很小；把矩阵增大到远超 `32 × 4 KB` 后未命中与遍历次数会显著上升。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果所有 32 项都有效且都被标记 MRU，下一次 `fill` 会发生什么？
**答案**：清空全部 MRU 位，然后牺牲 0 号槽（[mmu_tlb.cpp:46-50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu_tlb.cpp#L46-L50)）。

**练习 2**：为什么 `set_satp` 后必须 flush TLB？
**答案**：SATP 指向页表根，换根意味着旧 VPN→PPN 翻译可能已失效（指向不同的页表树）。不 flush 会读到陈旧翻译，这是 RISC-V `sfence.vma` 的语义。

---

### 4.4 SimX 的 PTW：层级计数器状态机

#### 4.4.1 概念说明

TLB 未命中时，MMU 内嵌的 **PTW**（Page Table Walker）接管，逐级读 PTE 直到找到叶子。SimX 的 PTW 有两个关键设计：

1. **不是「每级一个状态」，而是用一个「层级计数器 FSM」**：状态只有 `PTW_IDLE / PTW_REQ / PTW_WAIT / PTW_FILL` 四个，靠 `ptw_level_`（从 `VX_VM_PT_LEVEL-1` 递减到 0）控制当前在哪一级。这使得**同一份代码同时建模 Sv32（2 级）与 Sv39（3 级）**。
2. **PTE 取数走同一缓存端口**：PTW 发出的 PTE 读请求与普通 LSU load 走同一条 `ReqOut`，靠 tag 上的 `PTW_TAG_MARKER`（bit24）区分响应归属。

> 与 RTL 的差异（设计文档第 4、8 节）：RTL 的 `VX_mmu_ptw.sv` 是 **Sv32-only、硬编码 2 级**（状态 `PTW_L1_REQ/L1_RESP/L0_REQ/L0_RESP`），且**不检查 V/R/W/X 标志**（页错误未交付）。SimX 则做真实页错误检查并在出错时 `std::abort()`。

#### 4.4.2 核心流程

PTW 的状态机（[mmu.h:103-108](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.h#L103-L108)）：

```
PTW_IDLE --(TLB miss, PTW 空闲)--> start_ptw() --> PTW_REQ
PTW_REQ : 算出当前级 PTE 地址, 发 LD(PTW_TAG_MARKER) --> PTW_WAIT
PTW_WAIT: 收到响应 --> on_ptw_response()
          叶子? --> PTW_FILL
          非叶子? ptw_level_--, --> PTW_REQ (再发下一级)
          level==0 仍非叶子 / V=0 / R=0&W=1 --> 页错误 abort
PTW_FILL: 合成 PA, tlb_.fill(), 把原请求以 PA 重发 --> PTW_IDLE
```

`on_tick()` 每周期分三步（[mmu.cpp:169-226](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L169-L226)）：

1. **排空响应**：带 `PTW_TAG_MARKER` 的响应交给 PTW FSM，其余原样转发上游。
2. **驱动 PTW FSM**：发出待发的 PTE 读 / 填充。
3. **转发上游请求**：需翻译才查 TLB——命中就地翻译转发，未命中且 PTW 空闲则 `start_ptw` 并把请求暂存（`ptw_orig_req_`）。

注意 `PTW_TAG_MARKER = 1u << 24` 的选位很有讲究（[mmu.h:42-52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.h#L42-L52)）：dcache 在 non-cacheable 旁路打包时会把请求者端口左移进 tag 低位，bit31 会溢出丢失、导致 PTE 响应被误送上游；bit24 既留出 16M 真实 tag 余量，又留出 7 位左移余量。

#### 4.4.3 源码精读

**(1) 是否需要翻译** —— 关键结论：**没有地址区间旁路**：

[sim/simx/mem/mmu.cpp:44-54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L44-L54) 中文说明：因为运行时在启动时已为所有 PA 寻址区（IO MMIO、内核镜像、页表、栈）安装了恒等 PTE，所以 SATP 设置后**一切访问都走页表**；唯一不翻译的路径是 SATP 编程前（BARE）那几条取指。注释明确说「不再需要地址区间旁路」。

**(2) 启动一次遍历**：

[sim/simx/mem/mmu.cpp:56-68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L56-L68) 中文说明：从根表（`ptw_level_ = VX_VM_PT_LEVEL-1`，`ptw_cur_ppn_ = SATP.ppn`）开始，记录原始请求与端口，并累加 `walks_` 计数。

**(3) 处理 PTE 响应（叶子判定 + 页错误）**：

[sim/simx/mem/mmu.cpp:70-115](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L70-L115) 中文说明：从 cache line 载荷里按字节偏移取出 `VX_VM_PTE_SIZE` 字节当 PTE；先做有效性检查（`V=0 | (R=0 & W=1)` → 页错误 abort）；再判叶子（R/W/X 任一非零）——叶子记录 `ptw_leaf_level_` 进入 FILL，非叶子则降级继续；level=0 仍非叶子也是页错误。

**(4) drive_ptw：发请求与合成 PA**：

[sim/simx/mem/mmu.cpp:117-167](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L117-L167) 中文说明：`PTW_REQ` 按当前级 VPN 切片算 PTE 地址 `pte_addr(ptw_cur_ppn_, vpn)`，发一条带 `PTW_TAG_MARKER` 的 LD；`PTW_FILL` 合成 PA——对 level-L 叶子，低 `12 + L×VPN_BITS` 位是超页内偏移、来自 VA 而非 PPN；随后 `tlb_.fill(vpn, ppn_4kb, flags)` 把**每个 4 KB 子页**单独缓存（megapage 也只服务触发未命中的那 4 KB，后续同 megapage 的 VA 会重新遍历）。

**(5) RTL 对照（待确认点）**：

[hw/rtl/mem/VX_mmu_ptw.sv:106-119](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/mem/VX_mmu_ptw.sv#L106-L119) 中文说明：RTL 解析了 `pte_valid / pte_invalid_combo / pte_is_leaf`，但用 `` `UNUSED_VAR `` 显式丢弃——标志被解析却**未被使用**，页错误处理尚未实现。

[hw/rtl/mem/VX_mmu_ptw.sv:136-148](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/mem/VX_mmu_ptw.sv#L136-L148) 中文说明：RTL 状态机是写死的两级 `PTW_L1_REQ/L1_RESP → PTW_L0_REQ/L0_RESP`，PTE 32 位、PPN 取 `pte_data[29:10]`——这是 Sv32 专用的实现，不通用。

#### 4.4.4 代码实践

**实践目标**：跟踪一条指令的 VA 从未命中到重放的全过程。

**操作步骤**：

1. 开 VM 跑 sgemm，加 `--debug=4` 生成 trace。
2. 在 trace 里搜索 `ptw L` 关键字（来自 [mmu.cpp:133-134](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L133-L134) 的 `DT(4, ...)` 打印）。
3. 对一条未命中，观察它的 `L1-req → L1 响应 → L0-req → L0 响应 → fill` 序列。

**需要观察的现象**：Sv32 一次未命中会发出 2 条 `ptw L?-req`（L1 和 L0 各一条）；Sv39 会发出 3 条。每次 PTE 读都是一次真实的 cache 访问，可能进一步 miss 到下层。

**预期结果**：trace 中能看到 `walk_start_cyc_` 到 `PTW_FILL` 的周期差，对应 `PTW_LATENCY` 计数。具体周期数**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：SimX PTW 用「层级计数器 + 4 状态」而非「每级一对状态」有什么好处？
**答案**：同一份 FSM 同时描述 Sv32 与 Sv39，无需为不同级数写不同代码；级数由 `VX_VM_PT_LEVEL` 编译期决定，循环深度也随之变化（[mmu.cpp:60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L60)）。

**练习 2**：为什么 SimX 对 megapage 也只缓存「触发未命中的那 4 KB」？
**答案**：`PTW_FILL` 用 `vpn = ptw_vaddr_ >> 12`、`ppn_4kb = pa >> 12` 填 TLB（[mmu.cpp:153-155](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L153-L155)），粒度是 4 KB。同一 megapage 的其他 4 KB VA 会再次未命中并重走 PTW——正确但非最优，对恒等映射的系统区影响可忽略。

**练习 3**：PTW 同时只能走一次遍历，第二个未命中到来时怎么处理？
**答案**：若 PTW 非 IDLE，第二个请求停留在 `ReqIn[p]` 队头等待（[mmu.cpp:218-223](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/mem/mmu.cpp#L218-L223) 只在 `PTW_IDLE` 时 `start_ptw`），直到当前遍历完成释放 PTW。

---

### 4.5 主机运行时 VMManager：影子页表与批量 flush

#### 4.5.1 概念说明

页表是**谁来填写**的？答案是主机运行时的 `VMManager`（[sw/runtime/common/vm.h:71-127](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.h#L71-L127)）。它是 `vx::Device` 拥有的页表构造器 + VA 分配器，核心思想有三：

1. **VA-only API**：`vx_mem_alloc` 返回的是 VA。主机先从全局 PA 池分配物理页，再「铸造」一个 VA，并安装 PTE 把 VA 映射到 PA。
2. **主机影子页表**：所有 PTE 读写都打在主机内存里的 `shadow_pt_`（host memcpy，无设备往返）；改动只标记「脏 PT 页」，`flush()` 时**每个脏 PT 页用一次 DMA**批量推到设备。这模仿了 CUDA/ROCm/Level Zero 等主流 GPU 驱动的做法，让 FPGA 的 DMA 路径高效（1 MB 分配 ≈ 每 512 个 PTE 一次 DMA，而非 256 次单 PTE 写）。
3. **运行时发现 VM**：`vm.{h,cpp}` 与 `vm_types.h` **没有 `#ifdef VM_ENABLE`**——VMManager 总是被编译进 `libvortex.so`。运行时从 CP 的 `DEV_CAPS.VM_ENABLED`（bit24）发现设备是否有 MMU（[device.cpp:274](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L274)），无 MMU 时它就是惰性的、从不构造。

#### 4.5.2 核心流程

**初始化（`init`）**：

1. 在 `VX_MEM_PAGE_TABLE_BASE_ADDR` 处开一个 PA 分配器（页表区）。
2. 在 `ALLOC_BASE_ADDR` 处开一个 VA 分配器（虚拟区）；Sv32 时把虚拟区裁到 32 位地址空间内。
3. 分配根页表，记下 SATP（仅主机侧记账，硬件 SATP 由 `vx_start.S` 写）。
4. 为系统 PA 区安装**恒等映射**（IO 区、页表/栈高区），用超页（megapage/gigapage）尽量减少 PTE 数。
5. `flush()` 把脏页推到设备。

**分配设备内存（`phy_to_virt_map`，由 `vx_mem_alloc` 触发）**：

1. 已分配 PA → 查 `addr_mapping` 表复用旧 VA；否则从 VA 分配器申请新 VA。
2. 对每一页调 `update_page_table(ppn, vpn, flags)` 安装叶子 PTE。
3. **断言往返一致**：`assert(page_table_walk(init_vAddr) == init_pAddr)`——用主机侧遍历验证刚装的映射能翻译回原 PA。
4. 把 VA 写回给调用者，`flush()` 推脏页。

**主机侧遍历（`page_table_walk`）**：与 SimX PTW / CP `cp_translate` 完全同构，用于断言验证。

#### 4.5.3 源码精读

**(1) init：分配器 + 恒等映射**：

[sw/runtime/common/vm.cpp:75-119](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L75-L119) 中文说明：构造页表分配器与 VA 分配器，分配根表，对 `[0, USER_BASE_ADDR)` 与 `[PAGE_TABLE_BASE, GLOBAL_MEM)` 两段系统区调 `install_identity_map`，最后 `flush()`。

**(2) phy_to_virt_map：铸造 VA + 断言往返**：

[sw/runtime/common/vm.cpp:181-255](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L181-L255) 中文说明：按页向上取整（注意 `size < 4KB` 也要一页，否则 `size>>12` 截断成 0 导致漏映射）；支持随机化 VA（`VORTEX_RANDOMIZE_VA`）；逐页 `update_page_table`；末尾 `assert(page_table_walk(init_vAddr) == init_pAddr)` 验证。

**(3) update_page_table：遍历并安装 PTE**：

[sw/runtime/common/vm.cpp:266-316](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L266-L316) 中文说明：从根逐级走，遇已存在叶子（超页）则按 `span` 判断是否幂等覆盖（允许 `install_identity_map` 在粗超页内再细化）；遇非叶子则下钻；遇无效则在目标 `leaf_level` 写叶子 PTE（`PTE_V` + 权限），中间级则 `alloc_page_table` 新建表并写「`PTE_V` only」的指针项。

**(4) install_identity_map：超页优化**：

[sw/runtime/common/vm.cpp:318-352](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L318-L352) 中文说明：按对齐与剩余大小，尽量用最高级叶子（Sv32 L1=4 MB，Sv39 L1=2 MB、L2=1 GB）覆盖恒等区，权限 `V|R|W|X`，把 `leaf_level` 传给 `update_page_table`。

**(5) page_table_walk：主机侧遍历（与 PTW 同构）**：

[sw/runtime/common/vm.cpp:354-394](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L354-L394) 中文说明：逐级 `read_pte`，做同样的无效/叶子判定，叶子在 level-i 时用 `off_mask` 把超页内偏移从 VA 取回重建 PA。

**(6) 影子页表 + 批量 flush**：

[sw/runtime/common/vm.cpp:398-455](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L398-L455) 中文说明：`touch_pt_page` 懒分配 PT 页大小的影子缓冲并标脏；`write_pte/read_pte` 在影子里按小端序序列化 `VX_VM_PTE_SIZE` 字节；`flush()` 对每个脏 PT 页发**一次** `dev_io_->write`，然后清脏集——这就是「一页一 DMA」的批量化。

**(7) DeviceMemIO 抽象**：

[sw/runtime/common/vm.h:46-51](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.h#L46-L51) 中文说明：驱动适配器实现的 PA 寻址裸 read/write；simx/rtlsim 是 memcpy 进仿真 RAM，opae/xrt 是 DMA 到 FPGA 的 PT 区。

#### 4.5.4 代码实践

**实践目标**：跟踪一次 `vx_mem_alloc` 到设备页表被填充的完整链路。

**操作步骤**：

1. 在 [tests/regression/demo](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo) 的主机程序里定位 `vx_mem_alloc` 调用（见 u1-l4、u3-l1）。
2. 开 VM 跑该程序，在控制台确认出现 `VMManager Initialization...`。
3. 阅读调用链：`vx_mem_alloc`（u3-l2 的 device.cpp）→ `VMManager::phy_to_virt_map` → `update_page_table` → `write_pte`（写影子）→ `flush`（DMA 推设备）。
4. 在 `phy_to_virt_map` 的断言行（[vm.cpp:251](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L251)）旁加一行临时日志（**示例代码**，仅为观察，勿提交）：
   ```cpp
   std::cout << "[trace] VA=" << std::hex << init_vAddr
             << " PA=" << init_pAddr << std::dec << std::endl;
   ```

**需要观察的现象**：每个 `vx_mem_alloc` 触发一对 VA/PA；`flush()` 的次数远少于 PTE 数（批量）；若设了 `VORTEX_RANDOMIZE_VA=1`，VA 不再等于 PA 而是随机的。

**预期结果**：程序仍 `PASSED!`；日志显示 VA 与 PA 的映射关系，且对同一 PA 的二次分配会复用旧 VA（`addr_mapping` 命中）。带日志的运行**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `VMManager` 要维护主机影子页表，而不是直接写设备内存？
**答案**：读 PTE（`read_pte`，用于遍历与断言）和写 PTE 都打在 host memcpy 上，避免每次 PTE 操作一次设备往返；改动按 PT 页批量 flush，把多次小写合并成一次 DMA，这对 FPGA 后端尤其关键（[vm.h:53-66](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.h#L53-L66)）。

**练习 2**：`page_table_walk` 与 SimX 的 PTW、CP 的 `cp_translate` 三者是什么关系？
**答案**：三者是**同一套 Sv32/Sv39 遍历算法的三处实现**——VMManager 用它在主机侧验证映射、SimX 用它做硬件 MMU 的功能模型、CP 用它在 DMA 时翻译操作数。三处语义一致才能保证「主机装的映射、SimX 翻的 PA、CP 搬的地址」三者对齐。

**练习 3**：`install_identity_map` 对系统区为什么用超页？
**答案**：系统区（IO、页表、栈）通常很大且按超页边界对齐，用 megapage/gigapage 一项就能覆盖 MB/GB 级区域，大幅减少 PTE 数与 PTW 深度。

---

## 5. 综合实践

**任务**：把本讲四条主线串起来——「主机装页表 → 设备写 SATP → 访存时 TLB/PTW 翻译 → CP DMA 也翻译」，并指出 RTL 的 Sv32 待确认点。

请按以下步骤完成一份「Vortex 虚拟内存全链路说明书」：

1. **Sv32 页表遍历过程（4.2 + 4.4）**：用你自己的话写清一次 Sv32 VA→PA 翻译的完整步骤，包括根表地址怎么从 SATP 得到、两级 PTE 地址怎么算、叶子与非叶子的判定、以及 megapage 叶子在 PA 重建时的偏移来源。给出一个具体 VA 的手算例子。

2. **主机运行时如何把 VA 映射到 PA（4.5）**：画一张时序图，描述 `vx_mem_alloc(size)` 调用后，`VMManager` 内部发生了什么——PA 分配、VA 铸造、`update_page_table` 逐级填 PTE、影子页表标记脏、`flush` 批量 DMA。指出断言 `page_table_walk(VA) == PA` 在哪一行起作用。

3. **设备侧启动（4.1）**：说明 `vx_start.S` 在何时、由谁、把什么值写进 `satp`；解释为什么这之后「一切访问都走页表」（提示：`needs_translation` 与恒等映射）。

4. **RTL Sv32 待确认点（4.4.3）**：阅读 [VX_mmu_ptw.sv:113-119](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/mem/VX_mmu_ptw.sv#L113-L119) 与状态机，明确写出：当前 RTL PTW 只支持 Sv32 两级、不检查权限标志（页错误未交付），因此 RV64/Sv39 的 VM 在 FPGA 上尚未真正可用。对照 SimX 的通用层级计数器 FSM，说明「SimX 已通用化、RTL 待补齐」这一差异。

5. **验证**：用 `make -C tests/regression/sgemm run-simx CONFIGS="-DVX_CFG_VM_ENABLE"` 跑通 SimX VM，确认 `PASSED!`；用 `--perf=1` 记录 TLB/PTW 计数器，说明它们在 BARE 模式下应为 0。RTL 的 rtlsim VM 运行**待本地验证**（CI 中相关行被注释）。

**预期产出**：一份包含 1 张时序图、1 个手算例、1 段 RTL 差异说明的文档，能向另一位读者讲清「Vortex 的一次带 VM 的访存，从主机 `vx_mem_alloc` 到设备缓存看到 PA，到底经过了哪些步骤」。

---

## 6. 本讲小结

- **v3 模型**：VM 有两个翻译点——计算核 MMU（硬件）与 CP DMA 软件遍历器；主机 API 是 VA-only，传输时不翻译。
- **页表格式**：Sv32（2 级、4B PTE、每级 10 位 VPN）/ Sv39（3 级、8B PTE、每级 9 位 VPN），由 `VX_VM_ADDR_MODE` 编译期二选一；叶子判定看 R/W/X，无效组合 `V=0 | (R=0 & W=1)` 触发页错误。
- **TLB**：32 项全相联 CAM，每核一个 dcache TLB + 一个 icache TLB；MRU 替换；`set_satp` 触发 flush。
- **SimX PTW**：4 状态 + 层级计数器 FSM，通用支持 Sv32/Sv39；PTE 读经同一缓存端口，靠 `PTW_TAG_MARKER`(bit24) 区分响应；做真实页错误检查并 abort。
- **VMManager**：主机侧 PA+VA 双分配器、影子页表、按 PT 页批量 flush；运行时从 `DEV_CAPS` bit24 发现 VM，无 `#ifdef`。
- **RTL 待确认点**：RTL PTW 当前**硬编码 Sv32 两级、不检查权限标志**（页错误未交付），故 FPGA 上仅 Sv32/RV32 VM 可用；Sv39 与页错误交付是设计文档第 8 节列出的待办。

---

## 7. 下一步学习建议

- **u11-l2 原子内存操作与多缓存一致性**：AMO 操作要在缓存层次中做 read-modify-write，理解本讲的 PA 翻译后再看 AMO 如何被路由到特定缓存点会非常自然。
- **u11-l3 命令处理器与 KMU**：本讲提到的 `cp_translate`、`CP_SATP` 属于 CP 的职责，下一讲会完整打开 CP 从 DCR 写入到 KMU 派发 CTA 的控制流。
- **继续阅读源码**：
  - [docs/designs/virtual_memory_subsystem.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/virtual_memory_subsystem.md) 第 7、8 节——「有意简化」与「未实现提案」，看清当前模型的边界与未来路线（如多级 TLB 层次提案）。
  - [hw/rtl/mem/VX_mmu.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/mem/VX_mmu.sv) 与 `VX_mmu_ptw.sv`——对照 SimX 看 RTL 如何用 `VX_mem_arb` 合并 TLB 路径、旁路与 PTW。
