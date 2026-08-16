# CPU 仿真器内幕：内存模型与桩机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 CPU 仿真后端「模拟了什么、没模拟什么」：片上存储层级（UB/L1/L0A/L0B/L0C）如何用普通宿主内存模拟，GM 为什么反而不需要模拟。
2. 读懂 `NPUMemoryModel` 的三个核心动作：按架构分配容量、`TASSIGN` 偏移到指针的地址解析、指针到区域偏移的反查。
3. 理解 `cpu_stub.hpp` 的三类桩——关键字空宏、ACL 运行时函数的宿主机实现、同步原语空桩——以及 `dlsym` hook 注入的多核执行上下文机制。
4. 掌握 cpu_sim 调试三板斧：`assert` 定位、`GetXXXBase()/GetNPUAddr` 打印地址翻译、`ScopedExecutionContext` 模拟多核。

本讲是「测试体系与仿真器内幕」单元的第二讲，承接 u10-l1 的 ST 用例结构（你已经会用 `run_cpu.py` 跑一个用例），这次我们拆开 ST 可执行文件背后那台「假 NPU」。

## 2. 前置知识

- **后端路由（u2-l4）**：`__CPU_SIM` 宏把同一份 kernel 源码路由到 CPU 仿真实现；`add_definitions(-D__CPU_SIM)` 出现在 CPU ST 工程的顶层 CMake 里。
- **TASSIGN 与片上存储（u3-l2 / u2-l2）**：Manual 模式下 Tile 不自带存储，`TASSIGN` 把一个**片上偏移**绑给 Tile；TileType（Vec/Mat/Left/Right/Acc）决定它落在哪一级存储（UB/L1/L0A/L0B/L0C）。
- **TLOAD/TSTORE（u3-l1）**：搬运指令从 GlobalTensor（GM 视图）读写数据到 tile。
- **多核模型（u6-l1）**：`get_block_idx()` 提供核身份；真机多核 = 多个 AICore 并行执行同一 kernel。
- **一句术语澄清**：本讲的「GM」（Global Memory，HBM/DDR）在 CPU 仿真下**就是宿主机进程的普通堆内存**；「UB/L1/L0」是片上（on-chip）存储，才是 `NPUMemoryModel` 要模拟的对象。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pto/cpu/NPUMemoryModel.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp) | 仿真内存模型：8 个存储区域、按架构的容量表、每线程独立实例、地址解析 |
| [include/pto/common/cpu_stub.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp) | 桩机制总集：关键字空宏、ACL 函数宿主实现、同步空桩、执行上下文 hook |
| [docs/coding/cpu_sim.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/cpu_sim.md) | CPU_SIM 官方说明：限制、两种内存策略 |
| [include/pto/cpu/TAssign.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAssign.hpp) | CPU 版 TASSIGN：偏移 → 指针的解析入口，`NPU_MEMORY_INIT/CLEAR` |
| [include/pto/cpu/TLoad.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TLoad.hpp) | CPU 版 TLOAD：逐元素搬运的参考实现 |
| [include/pto/cpu/tile_offsets.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/tile_offsets.hpp) | 地址翻译函数：tile 逻辑坐标 → tile 存储偏移 / GM 偏移 |
| [include/pto/common/pto_tile.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp) | Tile 定义；CPU 仿真下 `data_` 退化为普通指针 |
| [include/pto/cpu/parallel.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/parallel.hpp) | CPU 仿真的元素级多线程工具 |
| [tests/cpu/st/testcase/tadd/tadd_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp) | 本讲实践标本：最小 TASSIGN+TLOAD+TADD+TSTORE 流水 |
| [tests/cpu/st/testcase/tpushpop/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop/main.cpp) | `ScopedExecutionContext` 模拟多核的范例 |
| [docs/coding/debug.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/debug.md) | 断言索引：SA-xxxx 编号与修复配方 |

## 4. 核心概念与源码讲解

### 4.1 内存模型：NPUMemoryModel 如何「假装」有片上存储

#### 4.1.1 概念说明

真机上，一个 AICore 拥有**物理独立**的多级片上存储：UB（Unified Buffer，向量单元的暂存）、L1（Mat tile 的落脚点）、L0A/L0B（Cube 单元左右操作数）、L0C（累加器），容量随架构（A2/A3、A5）不同。`TASSIGN(tile, offset)` 里的 offset 是**这些区域内部的偏移**，而不是进程地址。

CPU 仿真要回答一个问题：**没有这些硬件区域，offset 应该指向哪里？**

`NPUMemoryModel` 的答案朴素而有效：为每个区域预先 `resize` 一块按架构容量等大的 `std::vector<char>`，offset 就是这块 vector 内的字节偏移。这样「UB 放不下」「L0A 越界」这类真机约束在仿真里也以同样方式暴露（容量断言），Manual 模式的地址排布错误可以提前在 CPU 上发现。

而 **GM 不在 NPUMemoryModel 里**——CPU 仿真没有独立设备内存，`aclrtMalloc` 直接就是宿主 `calloc`（见 4.2.3），GlobalTensor 的指针就是普通宿主指针。所以本讲实践任务里「找 GM 的定义」，答案是：GM 没有 region，它就是进程堆。

#### 4.1.2 核心流程

`NPUMemoryModel` 的生命周期与三个地址动作：

```text
初始化（每线程一次）
  Initialize(arch) ──► 从 kA2A3MemorySizes / kA5MemorySizes 拷贝容量表
                    ──► 8 个 buffers_[region].resize(size, 0)
  （未显式调用时，首次使用经 EnsureInitialized() 按 defaultArch_=A2A3 自动初始化）

正向解析：TASSIGN(tile, offset)
  ResolveAssignedAddress<TileDef>(addr)
    ├─ addr 已落在某个 region 缓冲内？──► 它本来就是宿主指针，直接返回（别名视图场景）
    └─ 否则按 offset 解释 ──► GetPointer<TileDef>(offset)
         按 TileDef::Loc 编译期选择 region：Vec→UB, Mat→L1, Left→L0A, Right→L0B, Acc→L0C,
                                      ScaleLeft→L0A_MX, ScaleRight→L0B_MX
         assert(offset + numel*sizeof(T) <= sizes_[region])  ← 容量硬检查
         返回 buffers_[region].data() + offset

反向查询：GetNPUAddr(ptr)
  遍历 8 个 region，找到 ptr 落在哪一个 [base, base+size) 区间，返回 ptr-base
  ── 用于调试打印「这个 tile 现在住在哪个区域、区域内部偏移是多少」
```

任意 tile 元素的最终宿主地址由两级偏移相加决定：

\[ \text{host\_addr}(r, c) = \underbrace{\text{region\_base} + \text{TASSIGN\_offset}}_{\text{TASSIGN 决定}} + \underbrace{\text{GetTileElementOffset}(r,c) \times \text{sizeof}(T)}_{\text{布局决定}} \]

而 GM 侧源地址则由 `MapTileIndicesToGlobalOffset` 按布局反解出的五维下标乘 stride 求和：

\[ \text{gm\_offset} = \sum_{i=0}^{4} \text{idx}_i \times \text{stride}_i \]

#### 4.1.3 源码精读

**① 存储区域枚举与「按架构容量表」**

[NPUMemoryModel.hpp:L43-L53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L43-L53) 定义 8 个区域：REG（模拟 NPU 寄存器，仅 128 字节）、UB、L1、L0A、L0B、L0C、L0A_MX/L0B_MX（A5 的 MX 缩放因子专用小缓冲）。

[NPUMemoryModel.hpp:L63-L83](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L63-L83) 给出两套容量（注释里还附了昇腾官方文档链接）：

| 区域 | A2/A3 | A5 | 服务的 TileType |
| --- | --- | --- | --- |
| REG | 16×8 B | 16×8 B | 量化配置寄存器（见 ④） |
| UB | 192 KiB | 256 KiB | Vec |
| L1 | 512 KiB | 512 KiB | Mat |
| L0A | 64 KiB | 64 KiB | Left |
| L0B | 64 KiB | 64 KiB | Right |
| L0C | 128 KiB | 256 KiB | Acc |
| L0A_MX / L0B_MX | 4 KiB | 4 KiB | ScaleLeft / ScaleRight（MX） |

**② 每线程一个实例 = 每核一套片上存储**

[NPUMemoryModel.hpp:L88-L92](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L88-L92) 把单例挂在 `thread_local` 上，注释明说这是在建模「每个 AICore 物理独立的 UB/L0」。这一行是整个「租户隔离」的支点：两个线程各自 `TASSIGN(tile, 0)` 得到的是**不同**的宿主内存，正如两个核各自的 UB[0]。

**③ 初始化与默认架构**

[NPUMemoryModel.hpp:L99-L128](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L99-L128)：`Initialize(arch)` 逐项拷贝容量表并 `buffers_[i].resize(sizes_[i], 0)`（零填充）；`EnsureInitialized()` 保证未显式初始化的线程在首次使用时按 `defaultArch_`（默认 A2A3，[L274](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L274)）自动初始化。这就是为什么普通 ST 用例（如 tadd）从不调用初始化也能跑——它们默默用了 A2A3 的 192 KiB UB。需要 A5 语义的用例则显式初始化，例如 [tests/cpu/st/testcase/tmatmul_mx/main.cpp:L61](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul_mx/main.cpp#L61)：`pto::NPUMemoryModel::Instance().Initialize(pto::NPUArch::A5);`。

**④ TASSIGN 的地址解析**

[TAssign.hpp:L22-L37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAssign.hpp#L22-L37)：tile 分支调用 `ResolveAssignedAddress<T>(addr)`，GlobalTensor 分支只是 `SetAddr(addr)` 存指针。而 [NPUMemoryModel.hpp:L178-L188](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L178-L188) 的解析分两步：先 `TryResolveExistingPointer`（[L246-L258](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L246-L258)）判断传入的整数是否本来就落在某个 region 缓冲内——PTOAS 生成的 kernel 可能直接传「已物化的宿主指针」来构造同一块存储上的另一个 tile 视图；不是指针就当 offset 用 `GetPointer<TileDef>` 解析。

[NPUMemoryModel.hpp:L145-L172](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L145-L172)：`GetPointer<TileDef>` 用 `if constexpr` 按 `TileDef::Loc` 在**编译期**选 region，Vec 及未知类型兜底到 UB。真正的容量断言在 [L260-L267](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L260-L267)：`assert(byteOffset + numel * sizeof(T) <= sizes_[region])`——TASSIGN 排布越界时，CPU 仿真会在这里直接 abort，这是 Manual 模式最常用的一道保险。

**⑤ 调试后门与 REG 区域的妙用**

[NPUMemoryModel.hpp:L191-L224](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L191-L224) 暴露 `GetREGBase/GetUBBase/GetL1Base/GetL0ABase/GetL0BBase/GetL0CBase` 与 `GetSizes()/GetArch()/IsInitialized()`，注释直言「for debugging/direct access」。REG 区域的消费者是量化配置指令：[include/pto/cpu/SetQuantScalar.hpp:L25](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/SetQuantScalar.hpp#L25) 把 scale/offset 写进 `GetREGBase()` 起始处，偏移常量 `QUANT_SCALAR_REG_OFFSET=0 / QUANT_VECTOR_REG_OFFSET=1` 定义在 [cpu_stub.hpp:L158-L159](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L158-L159)——真机写硬件配置寄存器，仿真写这块 128 字节的假寄存器，`GetQuantScalar` 再从同一位置读回。

**⑥ MX 缩放地址编码也依赖本模型**

[NPUMemoryModel.hpp:L284-L290](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L284-L290)：`GetScaleAddr` 用 `GetNPUAddr` 反查出 tile 在 region 内的偏移再右移 4 位，复现真机「scale 地址 = 物理地址 / 16」的编码（u5-l5 讲过的 MX 布局），说明反查接口不只是调试后门，也参与语义实现。

#### 4.1.4 代码实践：画出地址空间示意图（含租户隔离对照）

1. **实践目标**：亲手确认「GM 在堆上、UB/L1/L0 在 NPUMemoryModel 里、每线程一套」，产出一张地址空间示意图。
2. **操作步骤**：
   - 通读 [NPUMemoryModel.hpp:L63-L83](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L63-L83)，把 A2/A3 的 8 个区域容量抄成表。
   - 参考下面的「示例代码」写一个独立小工程（或临时加进某个 ST 用例的 main 里，做完删除）：打印各 region 基址、容量，以及一个 `aclrtMalloc` 出来的「GM」指针，观察 GM 指针是否落在任何 region 区间内。

   ```cpp
   // 示例代码：非项目原有代码，仅用于观察仿真内存布局
   #include <pto/pto-inst.hpp>
   #include <cstdio>
   int main()
   {
       auto& m = pto::NPUMemoryModel::Instance();
       m.Initialize(pto::NPUArch::A2A3);          // 显式初始化，避免默认值干扰观察
       auto* sizes = m.GetSizes();
       std::printf("REG base=%p size=0x%zx\n", (void*)m.GetREGBase(), sizes[0]);
       std::printf("UB  base=%p size=0x%zx\n", (void*)m.GetUBBase(),  sizes[1]);
       std::printf("L1  base=%p size=0x%zx\n", (void*)m.GetL1Base(),  sizes[2]);
       void* gm = nullptr;
       aclrtMalloc(&gm, 1024, ACL_MEM_MALLOC_HUGE_FIRST);   // CPU 下即 calloc
       std::printf("GM  ptr=%p (heap, 不属于任何 region)\n", gm);
       return 0;
   }
   ```

   - 画图。参考答案（区域间宿主地址实际不相邻，示意图按逻辑画）：

```text
「隔离前」假想：全局共享一个 NPUMemoryModel（单例不按线程隔离）
┌────────────────── 共享的一套 buffers_ ──────────────────┐
│ REG 128B │ UB 192KiB │ L1 512KiB │ L0A/B/C │ ...        │
└──────────────────────────────────────────────────────────┘
   thread A 的 TASSIGN(t, 0) ─┐
   thread B 的 TASSIGN(t, 0) ─┴──► 同一块内存，数据互踩（错误！）

「隔离后」实际实现：thread_local NPUMemoryModel（NPUMemoryModel.hpp:L88-L92）
┌──────── thread A（模拟 AICore 0）────────┐   ┌──────── thread B（模拟 AICore 1）────────┐
│ REG │ UB │ L1 │ L0A │ L0B │ L0C │ MX    │   │ REG │ UB │ L1 │ L0A │ L0B │ L0C │ MX    │
└───────────────────────────────────────────┘   └───────────────────────────────────────────┘
        │ TASSIGN(t,0) → 本线程 UB[0]                  │ TASSIGN(t,0) → 本线程 UB[0]
        └──────────── 偏移语义相同，宿主内存不同 ────────┘

GM：不在 NPUMemoryModel 中。aclrtMalloc → calloc，即进程堆，
    所有「核」（线程）共享 —— 与真机 HBM 被全体核共享的行为一致。
```

3. **需要观察的现象**：各 region 基址互不相同且彼此不相邻（它们是独立的 vector）；GM 指针不在任何 `[base, base+size)` 区间内。
4. **预期结果**：A2/A3 下打印出的 size 依次为 0x80（REG 128B）、0x30000（192KiB）、0x80000（512KiB）、0x10000、0x10000、0x20000、0x1000、0x1000。
5. `Initialize` 后同一进程再次调用会重新 resize，观察请只初始化一次；若在你的机器上跑不出上述数值，请核对 `NPUArch` 枚举传参——其余现象**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：tadd 用例里 `TASSIGN(src0Tile, 0x0); TASSIGN(src1Tile, 0x4000); TASSIGN(dstTile, 0x8000);`（[tadd_kernel.cpp:L26-L28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L26-L28)），三个 tile 都是 64×64 的 float。这三个偏移为什么不会越界、不会重叠？

**答案**：64×64×4B = 16384B = 0x4000，所以 0x0/0x4000/0x8000 恰好首尾相接，总占 0xC000 = 48KiB < UB 的 192KiB。任何一个 tile 再大一点，`GetPointer` 的 `assert(byteOffset + numel*sizeof(T) <= sizes_[UB])`（[NPUMemoryModel.hpp:L265](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L265)）就会 abort。

**练习 2**：`ResolveAssignedAddress` 为什么要先 `TryResolveExistingPointer` 再按 offset 解释？直接当 offset 用不行吗？

**答案**：PTOAS 生成的 CPU kernel 可能传入「已经物化的宿主指针」（比如在同一个 L1 缓冲上再开一个 tile 视图），此时整数的值是真实地址而非偏移；先反查区间能把两类实参区分开。若一律当 offset，指针值（通常是个巨大的数）会立刻触发容量断言或越界。

**练习 3**：A5 的 L0C 是 256 KiB 而 A2/A3 是 128 KiB。一个 256×256 的 float `TileAcc`（256KiB）在两种架构下的 TASSIGN 行为有何差别？

**答案**：初始化为 A5 时恰好装满 L0C 可通过；默认 A2A3 时 `assert(0 + 65536×4 ≤ 131072)` 失败、进程 abort——容量约束随 `Initialize(NPUArch::...)` 的选择而变，这正是仿真「按架构建模」的意义。

### 4.2 桩机制：cpu_stub.hpp 如何把 NPU 程序骗过 CPU 编译器

#### 4.2.1 概念说明

一份 PTO kernel 源码里混着大量「Ascend 专属语法」：`__gm__`/`__ubuf__` 地址空间关键字、`AICORE` 函数标注、`aclrtMalloc` 等 ACL 运行时调用、`set_flag/wait_flag` 流水线原语、`dcci/dsb` 缓存维护指令。这些东西标准 C++ 编译器既不认识也不需要。

`cpu_stub.hpp` 的策略是**三类替换**：

1. **关键字 → 空宏**：让源码「能编译」，语义上视同不存在。
2. **ACL 运行时函数 → 宿主机实现**：`aclrtMalloc` 变 `calloc`、`aclrtMemcpy` 变 `memcpy`，设备内存管理坍缩成进程堆管理。
3. **同步/缓存原语 → 空操作**：`set_flag/wait_flag/pipe_barrier/dcci/dsb` 全部空桩——因为 CPU 仿真单线程按序执行，同步在功能上恒成立。

此外还有第四类「半桩」：`get_block_idx()` 这类多核身份查询保留真实语义，但数据源可被外部通过 `dlsym` 注入的 hook 接管，用于更逼真的多核模拟。

#### 4.2.2 核心流程

一份 kernel 从 NPU 源码到 CPU 可执行的替换链：

```text
源码记号            cpu_stub.hpp 中的替身                     效果
─────────────────  ─────────────────────────────────────  ─────────────────────
__gm__/__ubuf__... #define 空宏（L28-L40）                   关键字消失
AICORE/__aicore__  #define 空宏                              函数变普通函数
aclrtMalloc        → aclrtMallocHost → calloc（L69-L76）     设备内存=堆内存
aclrtMemcpy        → std::memcpy(min(szDst,szSrc))（L78）    H2D/D2H 全成进程内拷贝
aclrtSynchronizeStream → return 0（L99）                     流同步恒完成
set_flag/wait_flag inline 空函数（L118-L119）                事件同步恒成立
pipe_barrier       inline 空函数（L53）                       流水线屏障恒成立
dcci/dsb           inline 空函数（L133-L135）                 缓存维护恒成立
set_ctrl/get_ctrl  读写 thread_local uint64_t（L136-L143）    硬件 CTRL 寄存器的宿主化身
get_block_idx()    先查 dlsym hook，无 hook 走 thread_local   多核身份可注入
                       execution_context（L256-L266）
```

配套的宏观事实（[docs/coding/cpu_sim.md:L4-L6](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/cpu_sim.md#L4-L6)）：CPU_SIM 所有操作同步执行、同步原语为空实现、多线程支持不完整（tile 内存跨线程不共享）。

#### 4.2.3 源码精读

**① 关键字空宏与流水线常量**

[cpu_stub.hpp:L28-L40](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L28-L40) 一口气把 `__global__`、`AICORE`、`__aicore__`、`__gm__`、`__out__`、`__in__`、`__ubuf__`、`__cbuf__`、`__ca__`、`__cb__`、`__cc__`、`__fbuf__`、`__tf__` 全部定义为空。注意 u1-l4 讲过的 `__gm__ T __out__* out` 在 CPU 下就坍缩成 `T* out`。[L42-L52](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L42-L52) 用 `typedef int pipe_t` 和常量复原 `PIPE_S/V/MTE1/MTE2/MTE3/M/FIX` 编号——事件 API 的参数类型还得「长得像」真的，[L53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L53) 的 `pipe_barrier` 则什么都不做。

**② ACL 运行时的宿主机实现（GM 的真正下落）**

[cpu_stub.hpp:L69-L76](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L69-L76)：`aclrtMallocHost` 就是 `calloc`（且 `sz==0` 会触发断言，报错文案直接指向 [docs/coding/debug.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/debug.md)）；`aclrtMalloc` 转发到同一实现。这是「GM=堆」的代码级证据。[L78-L82](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L78-L82) 的 `aclrtMemcpy` 取 `min(szDst, szSrc)` 做 `memcpy`，`ACL_MEMCPY_HOST_TO_DEVICE` 等枚举（[L62-L67](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L62-L67)）全为 0/1/2 的摆设值——方向参数在进程内拷贝中无意义。整段包在 `#if !defined(__COSTMODEL)` 里：CostModel 后端另有自己的桩，不复用这一套。

**③ 同步与缓存原语空桩 + CTRL 寄存器化身**

[cpu_stub.hpp:L118-L146](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L118-L146)：`set_flag/wait_flag` 空、`dcci/dsb` 空、`cache_line_t` 的常量全为 0。值得细看的是 [L136-L143](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L136-L143) 的 `cpu_ctrl_register()`：u4-l4 讲过 SaturationMode 走硬件 CTRL 寄存器第 59 位，仿真下这个 `thread_local uint64_t` 就是那枚寄存器——`set_ctrl/sbitset1` 真实生效，所以 CPU 仿真能复现饱和/截断行为差异。再往下 [L147-L149](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L147-L149)：`__cce_get_tile_ptr(x)` 定义为 `x` 本身、`set_mask_norm/set_vector_mask` 为空——NPU 上 tile 变量要经编译器内建函数取指针，CPU 上 `data_` 本来就是指针（见 4.3.3）。

**④ 通信桩**

[cpu_stub.hpp:L151-L173](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L151-L173)：`CommDeviceContext` 保留了 `rankId/rankNum/windowsIn[64]/windowsOut[64]` 的窗口结构（u7-l2 的跨卡窗口机制），供 CPU 仿真下的单进程多 rank 通信指令（tget/tput 等 ST 用例）寻址使用。

**⑤ 执行上下文：thread_local 兜底 + dlsym hook 接管**

这是桩机制里最有设计感的一段。[cpu_stub.hpp:L186-L210](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L186-L210) 用 `dlsym(RTLD_DEFAULT, ...)` 惰性解析四个可选符号：`pto_cpu_sim_set_execution_context` / `pto_cpu_sim_get_execution_context` / `pto_cpu_sim_get_shared_storage` / `pto_cpu_sim_get_task_cookie`。谁导出这些符号，谁就成为「多核调度器」：外部 runner（或 CostModel 的 perf_sim 启动器，它同样使用 `set_execution_context`，见 [include/pto/costmodel/perf_sim/launch.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/launch.hpp)）可以在真实线程里切换 `block_idx`。ST 用例进程没有导出它们，`dlsym` 返回 `nullptr`，于是 [L218-L225](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L218-L225) 的 `thread_local ExecutionContext execution_context` 兜底。[L256-L266](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L256-L266) 的 `get_block_idx()` 由此恒有定义：先问 hook、再读 thread_local——kernel 源码一行不改就能在两种「世界观」下运行。

**⑥ SYNCALL 空桩**

[cpu_stub.hpp:L306-L336](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L306-L336)：`__CPU_SIM` 下 `SYNCALL_IMPL/SYNCALL_SOFT_IMPL/...` 全为空——u6-l1 讲过 CPU 仿真单线程按序执行，栅栏天然满足，Soft 版所需的 GM 工作区也不会被触碰。

#### 4.2.4 代码实践：亲手数一遍桩

1. **实践目标**：建立「一份 kernel 在 CPU 下到底被替换掉了什么」的量化直觉。
2. **操作步骤**：
   - 用 Grep 在 [include/pto/common/cpu_stub.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp) 中统计 `#define` 空宏数量与 `inline.*\{\s*\}` 形态的空函数数量（可在仓库根目录执行 `grep -c '^#define' include/pto/common/cpu_stub.hpp` 与 `grep -cE 'inline [a-zA-Z_]+ [a-z_]+\([^)]*\) \{ \}' include/pto/common/cpu_stub.hpp`）。
   - 打开 [tests/cpu/st/testcase/tadd/main.cpp:L44-L68](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L44-L68)，逐行标注：哪些调用在 NPU 上是真实 runtime API、在 CPU 上分别变成了什么（`aclInit`→空宏、`aclrtMallocHost`→`calloc`、`aclrtMemcpy`→`memcpy`、`aclrtSynchronizeStream`→`return 0`）。
   - 用 `nm` 或 `ldd` 观察 ST 可执行文件链接了 `dl`（[tests/cpu/st/testcase/CMakeLists.txt:L33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L33) 为用例链接 `dl` 库正是为 `dlsym`），确认它**没有**导出 `pto_cpu_sim_set_execution_context`。
3. **需要观察的现象**：空宏约十几个；空函数十处左右；tadd 的 main 中全部设备管理代码在 CPU 下退化为堆内存操作。
4. **预期结果**：统计数字与你的标注表能一一对应源码行；`nm` 输出中找不到 hook 符号（`T pto_cpu_sim_set_execution_context`）。
5. grep 的精确计数值依赖正则写法，以你自己执行的结果为准；`nm` 需要先构建出可执行文件（`python3 tests/run_cpu.py -t tadd`），**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`aclrtMemcpy(dst, szDst, src, szSrc, kind)` 为什么取 `min(szDst, szSrc)`？这在什么情况下会掩盖 bug？

**答案**：CPU 下没有独立的源/目的设备内存，`kind` 无意义；取 min 是防御式截断。若调用者本该传相同大小却传错了（如把元素个数当字节数），仿真会静默少拷而非崩溃，比对 golden 时才暴露——这是「CPU 仿真只保证功能正确」的一个具体体现。

**练习 2**：`set_flag/wait_flag` 都是空桩，为什么 ST 用例里还保留这些调用（如 [tadd_kernel.cpp:L36-L40](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L36-L40)）？

**答案**：为了与真机 kernel 源码逐字一致（u2-l4 的「kernel 源码一行不改」原则）。同一份 kernel 在 NPU 上这些调用是真实的流水线依赖表达；在 CPU 上编译为空，功能等价于顺序执行。删掉它们 CPU 结果不变，但真机行为会错。

**练习 3**：如果不 include `cpu_stub.hpp`（或所在工程没定义 `__CPU_SIM`），一份含 `aclInit` 的 host 代码还能在 CPU 上编译吗？

**答案**：不能，链接器找不到 `aclInit` 等符号。[docs/coding/cpu_sim.md:L11](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/cpu_sim.md#L11) 明确说：不 include 它也可以，但那样你必须自己删掉/替换所有 `aclInit/aclrtSetDevice` 类调用。`pto-inst.hpp` 在 `__CPU_SIM` 下已经替你把这份桩 include 进来。

### 4.3 仿真调试：从 Tile 指针化到地址翻译验证

#### 4.3.1 概念说明

调试 CPU 仿真的前提是知道三个「变形」：

1. **Tile 数据成员指针化**：NPU 上 `Tile::data_` 的类型是 `__ubuf__ half` 这类带地址空间的类型（编译器魔法）；CPU 仿真下它就是 `DType*`。所以仿真代码可以直接 `tile.data()[i]` 读改数据——这就是调试后门。
2. **懒分配（`__PTO_AUTO__`）**：Auto 模式的 CPU 仿真不预分配，首次 `data()` 时才从 `internalBuffer` 落地。这解释了 [docs/coding/cpu_sim.md:L22-L28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/cpu_sim.md#L22-L28) 的警告：既不 TASSIGN 也不开 `__PTO_AUTO__`，`data_` 就是野指针，程序 segfault。
3. **多核的两种模拟法**：ST 用例默认单线程、`get_block_idx()` 恒 0；需要表达多核语义时用 `ScopedExecutionContext` 在同一线程内「扮演」不同核依次执行；真正想并行时开线程——但此时 `thread_local` 内存模型保证每线程一套片上存储，tile 数据本身不跨线程共享。

#### 4.3.2 核心流程

一次 `TLOAD` 在 CPU 仿真里的完整地址翻译链（以 tadd 的 `TLOAD(src0Tile, src0Global)` 为例）：

```text
TLOAD(src0Tile, src0Global)                    公共 API 薄壳（TSYNC 空桩 + 转发）
  └─ TLOAD_IMPL ──► TLOAD_TILE_IMPL            include/pto/cpu/TLoad.hpp:L126-L152
       ├─ CheckTileData                        编译期/运行期形状契约检查
       ├─ std::fill(dst.data(), ..., pad)      先把整个 tile 填成 PadValue
       └─ for r in [0,validRow) for c in [0,validCol)
            gm_off  = MapTileIndicesToGlobalOffset(r, c, shapes, strides)
                    │  ND 布局：反解 i3=r%shape3, i2=(r/shape3)%shape2, ...
                    │  再 gm_off = Σ idx_i * stride_i        （tile_offsets.hpp:L169）
            src     = src0Global.data()[gm_off]             ← GM 侧：宿主堆指针
            tile_off = GetTileElementOffset(r, c)           （tile_offsets.hpp:L64）
                     │  RowMajor 无分形：r*Cols + c
            dst.SetElement(r, c, src)
                    └─ data()[GetTileElementOffset(r,c)]     ← UB 侧：TASSIGN 决定的基址
```

GM 侧与 tile 侧是**两套独立的坐标→偏移映射**：前者由 GlobalTensor 的布局（ND/DN/NZ/MX_*…）决定，后者由 Tile 的 BLayout/SLayout 决定。调试搬运类 bug 的黄金手段就是同时打印这两侧的偏移，核对它们是否与你手算一致。

#### 4.3.3 源码精读

**① Tile 的 CPU 形态**

[pto_tile.hpp:L1539-L1554](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1539-L1554)：`#if defined(__CPU_SIM) || defined(__COSTMODEL)` 分支里 `TileDType = Tile::DType*`；NPU 分支则是 `MemoryQualifier<Loc, DType>::type`（如 `__ubuf__` 限定类型），`__PTO_AUTO__` 下更进一步是 `tile_size(Rows*Cols)` 的向量类型（u9-l1 讲过）。[L1693-L1702](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1693-L1702)：`assignData` 是私有 setter（只允许 `TASSIGN_IMPL` 这个 friend 改地址），`data_` 在 CPU+Auto 组合下由 `internalBuffer` 撑腰。[L1556-L1568](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1556-L1568) 的懒分配 `data()`：`if (!data_) { internalBuffer.resize(...); data_ = internalBuffer.data(); }`。

**② TLOAD 的逐元素翻译**

[TLoad.hpp:L126-L152](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TLoad.hpp#L126-L152)：先 `CheckTileData`（静态断言 dtype/布局一致 + 运行时 assert 有效区匹配），再 `std::fill` 填充 pad（u3-l1 讲过的 PadValue 语义在此落地），然后双层循环调 `MapTileIndicesToGlobalOffset` + `dst.SetElement`。注意它是**布局感知的逐元素读写**——CPU 仿真从不模拟 burst DMA，只求功能正确。

**③ 两个坐标映射函数**

[tile_offsets.hpp:L54-L75](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/tile_offsets.hpp#L54-L75)：`GetTileElementOffset`——无分形布局走 `r*Cols+c`（行主序）或 `c*Rows+r`（列主序），分形布局走子块+内块两级寻址（NZ/ZN/ZZ 摆放）。[tile_offsets.hpp:L169-L259](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/tile_offsets.hpp#L169-L259)：`MapTileIndicesToGlobalOffset`——按 `GlobalData::layout` 用 `if constexpr` 分派十余种布局的反解公式，最后统一 \(\sum_i \text{idx}_i \cdot \text{stride}_i\)。这两个函数是你在仿真里验证「数据到底从哪来到哪去」的真相之源。

**④ 多核扮演：ScopedExecutionContext**

[cpu_stub.hpp:L241-L253](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L241-L253)：RAII 对象，构造时保存旧上下文并 `set_execution_context(block_idx, subblock_id, subblock_dim)`，析构时还原。真实用例见 [tpushpop/main.cpp:L73-L84](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop/main.cpp#L73-L84)：先后以 `(0,0,2)`、`(0,1,2)` 两个身份 `TPUSH`，再以消费者身份 `TPOP`——TPipe 的跨核协议（u3-l2）在单线程里就被这样「串行演完」。

**⑤ 元素级并行的边界**

[parallel.hpp:L20-L26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/parallel.hpp#L20-L26) 定义 `PTO_CPU_PARALLEL_THRESHOLD_ELEMS=16384` 阈值与 `PTO_CPU_MAX_THREADS` 上限；[L53-L95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/parallel.hpp#L53-L95) 的 `parallel_for_1d` 在元素少时直接串行、多时按 chunk 开 `std::thread`。这是 u3-l4 提过的行级多线程——它只加速单个 tile 内的循环，不改变指令语义，也解释了为什么 `docs/coding/cpu_sim.md` 要警告 tile 跨线程共享不安全。

**⑥ 断言体系与排障入口**

[docs/coding/debug.md:L5-L9](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/debug.md#L5-L9) 把 PTO 的检查分三层：`static_assert`（编译期）、`PTO_ASSERT`（真机 `_DEBUG` 下）、CPU 仿真的 `assert`（直接 abort 进程）。CPU 仿真排障的固定动作：拿 abort 信息里的文件行号/SA 编号到 debug.md 索引里搜，按 FIX-Ann 配方修。例如 TASSIGN 容量类问题对应 `FIX-A12`（[debug.md:L33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/debug.md#L33)，索引条目见 [debug.md:L394-L397](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/debug.md#L394-L397) 的 SA-0351～0354）。

#### 4.3.4 代码实践：用 debug 打印验证一次 TLOAD 的地址翻译

1. **实践目标**：对 tadd 用例的 float 64×64 case，亲眼确认 (a) 三个 tile 落在 UB 的 0x0/0x4000/0x8000；(b) GM 侧源偏移与 tile 侧落点偏移都等于 `r*64+c`。
2. **操作步骤**（这是你本地实验性的临时修改，验证后请还原，不要提交）：
   - 构建并确认基线通过：`python3 tests/run_cpu.py -t tadd -g "case_float_64x64_64x64_64x64"`。
   - 在本地工作副本 [tests/cpu/st/testcase/tadd/tadd_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp) 的 `TASSIGN(dstTile, 0x8000);` 之后、`TLOAD(src0Tile, src0Global);` 之前插入（示例代码，非项目原有代码）：

   ```cpp
   // ---- 临时调试打印：验证 TLOAD 地址翻译，验证后删除 ----
   auto& mem = pto::NPUMemoryModel::Instance();
   printf("[sim] arch=%d UB size=0x%zx\n", (int)mem.GetArch(), mem.GetSizes()[1]);
   printf("[sim] src0Tile UB off=0x%zx\n", mem.GetNPUAddr(src0Tile.data()));
   printf("[sim] src1Tile UB off=0x%zx\n", mem.GetNPUAddr(src1Tile.data()));
   printf("[sim] dstTile  UB off=0x%zx\n", mem.GetNPUAddr(dstTile.data()));
   printf("[sim] GM src0 base=%p (heap, outside any region)\n", (void*)src0);
   {   // 抽查三个元素的两侧偏移
       const std::vector<int64_t> shapes = {1, 1, 1, kGRows_, kGCols_};
       const std::vector<int64_t> strides = {kGCols_, kGCols_, kGCols_, kGCols_, 1};
       for (auto [r, c] : std::vector<std::pair<int,int>>{{0,0},{1,0},{63,63}}) {
           size_t gmOff  = pto::MapTileIndicesToGlobalOffset<GlobalData>(r, c, shapes, strides);
           size_t ubOff  = pto::GetTileElementOffset<TileData>(r, c);
           printf("[sim] (r=%d,c=%d) gm_off=%zu ub_off=%zu\n", r, c, gmOff, ubOff);
       }
   }
   // ---- 临时调试打印结束 ----
   ```

   - 重新运行同一条 `run_cpu.py` 命令，记录输出。
   - （可选）把 `TASSIGN(src1Tile, 0x4000)` 改成 `0x2000`，观察与 src0Tile 重叠后输出比对是否仍通过——这验证「仿真不查重叠，错排布会静默互踩」（u3-l2 的结论在仿真下的表现）。
3. **需要观察的现象**：三个 UB 偏移恰为 0x0、0x4000、0x8000；GM 指针是普通堆地址；`(0,0)/(1,0)/(63,63)` 三个采样点的 gm_off 与 ub_off 均为 0、64、4095；重叠实验中 src1Tile 与 src0Tile 同住 UB+0x2000 起的区间，`TLOAD(src1Tile, ...)` 会把 src0Tile 已装入的数据覆盖掉一部分。
4. **预期结果**：与手算完全一致——因为该 tile 是 RowMajor 无分形 64×64，GlobalTensor 是 ND 布局 stride=<kGCols_,1> 最内维，两个映射都退化为 `r*64+c`；用例最终 `ResultCmp` 通过（重叠实验则可能失败，取决于覆盖区间是否影响有效数据）。
5. `MapTileIndicesToGlobalOffset` 的 strides 参数需要传全五维：示例里前三维给 `kGCols_` 只是占位（shape=1 时不参与求和）。若你的编译器对结构化绑定 `auto [r,c]` 报错（C++20 下正常），改用 `std::pair<int,int> pts[] = ...` 遍历。打印的具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MapTileIndicesToGlobalOffset` 用 `if constexpr` 逐布局分派，而不是运行时 switch？

**答案**：`GlobalData::layout` 是编译期常量（模板参数），`if constexpr` 能在编译期只实例化命中的分支——十余种布局的反解公式互不相容，运行期分支既浪费又会实例化不合法的组合。这也再次体现 PTO「布局进类型」的设计。

**练习 2**：一个 `Tile<TileType::Vec, half, 16, 256, ...>`（RowMajor）经 `TASSIGN(t, 0x1000)` 绑定后，元素 (5, 100) 的宿主地址表达式是什么？

**答案**：`UB_base + 0x1000 + GetTileElementOffset(5,100)*sizeof(half) = UB_base + 0x1000 + (5*256+100)*2 = UB_base + 0x1000 + 2720`。其中 `UB_base` 是本线程 `buffers_[UB].data()`，容量检查 `0x1000 + 16*256*2 ≤ 192KiB` 通过。

**练习 3**：`ScopedExecutionContext` 切换的是 `block_idx`，但它并不真正并发。那 tpushpop 用例为什么还能验证 TPush/TPop 的正确性？

**答案**：因为它验证的是**协议的数据面**——生产者写入的槽位、消费者读出的槽位、槽位路由（TILE_UP_DOWN 切分时 r/VecTile::Rows 决定 lane）这些逻辑与并发时序无关；而「生产者是否领先过多」「等牌是否死锁」这类时序问题单线程天然无法暴露，必须真机验证。这正是 u6-l1「CPU 只验功能、同步须真机」结论的又一实例。

## 5. 综合实践

**任务：给你的仿真器画一张「体检报告」并验证一条完整数据通路。**

1. 复用 4.1.4 的示例程序，但这次在**两个线程**里各跑一遍打印（`std::thread` 包起来）：对比两线程打印的 UB 基址与 `GetNPUAddr` 结果，用实际输出佐证「thread_local 租户隔离」——两个线程对同一个逻辑偏移 0 的 TASSIGN 解析到不同宿主地址。
2. 在 tadd 用例里按 4.3.4 插桩，拿到三级证据链：
   - **区域层**：tile 落在 UB 区域内的偏移（`GetNPUAddr`）；
   - **通路层**：GM 堆指针 → `MapTileIndicesToGlobalOffset` → tile 内偏移的两侧数值；
   - **数据层**：在 `TADD` 之后打印 `dstTile.data()[0]`、`[1]`，与 gen_data.py 生成的 `input1.bin + input2.bin` 前两个元素手算对比。
3. 产出一张综合示意图：把 4.1.4 的「隔离后」地址空间图扩充，在 thread A 的 UB 区间里标出 0x0/0x4000/0x8000 三个 tile，在 GM 区间里标出 src0/src1/dst 三个缓冲，用箭头画出 `TLOAD→TADD→TSTORE` 的数据流。
4. 验收标准：三层证据数值互相咬合（区域偏移差 = tile 字节数、两侧坐标映射 = 手算值、数据 = golden 手算），并能用一句话向同伴解释「为什么 CPU 仿真下改 tile 重叠排布不报错但结果可能错」。

## 6. 本讲小结

- `NPUMemoryModel` 用 8 个按架构容量等大的 `std::vector<char>` 模拟 REG/UB/L1/L0A/L0B/L0C(+MX) 片上存储；`thread_local` 单例让每个线程（仿真核）拥有独立一套，天然实现核间隔离；**GM 不在其中**——CPU 仿真下 `aclrtMalloc` 即 `calloc`，GM 就是宿主堆。
- `TASSIGN` 的地址解析是「先反查指针、再按偏移」两步（`ResolveAssignedAddress`），容量越界会被 `assert` 当场拦下；`GetNPUAddr` 反查区域偏移，既是调试后门也支撑 MX scale 地址编码。
- `cpu_stub.hpp` 三类桩：关键字空宏让 NPU 语法可编译；ACL 运行时函数坍缩为堆内存操作；`set_flag/wait_flag/dcci/dsb/pipe_barrier` 全部空操作，CTRL 寄存器由 `thread_local uint64_t` 化身，饱和模式等行为仍可复现。
- 多核身份 `get_block_idx()` 采用「dlsym hook 优先、thread_local 兜底」双层设计：外部 runner 可注入调度器接管，ST 用例则靠 `ScopedExecutionContext` 在单线程内扮演不同核。
- CPU 仿真的 TLOAD 是布局感知的逐元素翻译：GM 侧 `MapTileIndicesToGlobalOffset`（按 GlobalTensor 布局反解五维下标乘 stride）+ tile 侧 `GetTileElementOffset`（按 Tile 布局求区域内偏移），两套映射独立，是调试搬运 bug 的核心观测点。
- 排障入口固定三层：`static_assert`/`PTO_ASSERT`/CPU `assert`，abort 信息对照 [docs/coding/debug.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/debug.md) 的 SA 编号与 FIX 配方；仿真不查地址重叠，错排布以数据互踩形式静默出错。

## 7. 下一步学习建议

- **下一讲 u10-l3（CostModel 性能模拟）**：本讲的 `#if !defined(__COSTMODEL)` 分支多处出现——CostModel 后端既复用 CPU 仿真的内存模型（`pto_tile.hpp` 中两者共用 `TileDType = DType*` 与懒分配），又有独立的桩与流水线建模。带着「哪些机制被 CostModel 继承、哪些被替换」的问题去读 `include/pto/costmodel/lightweight_costmodel.hpp` 与 `perf_sim/pipe_model.hpp`。
- 若你想亲手加深本讲内容：给 4.3.4 的插桩扩展到 NZ 分形布局的 tile（参考 `GetTileElementOffsetSubfractals` 的四分支），验证分形摆放下 tile 内偏移不再是 `r*Cols+c`。
- 延伸阅读：[docs/coding/cpu_sim.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/cpu_sim.md)（两种内存策略与 segfault 警告）、[include/pto/costmodel/perf_sim/launch.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/launch.hpp)（看 hook 的另一个消费者如何切换执行上下文）。
