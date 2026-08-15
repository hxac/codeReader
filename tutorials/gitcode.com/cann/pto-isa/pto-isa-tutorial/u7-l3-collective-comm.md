# 集合通信：TReduce、TBroadCast、TGather/TScatter

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PTO 通信扩展 ISA 中四条集合通信指令（TREDUCE / TBROADCAST / TGATHER / TSCATTER）各自的语义、数学定义与参与方约束。
2. 理解「root 单边执行」模型：为什么集合通信只有 root 调用指令，非 root 只需保证缓冲区就绪。
3. 读懂 A2/A3 后端上集合指令的实现套路：TLOAD/TSTORE + 向量指令在 AIV 上的编排、2D 滑窗自动分块、ping-pong 双缓冲变体。
4. 掌握 ParallelGroup 这一参与方描述抽象，以及 ReduceOp / CollEngine 等配套枚举。
5. 针对一个 8 卡 AllReduce 需求，能在「TREDUCE+TBROADCAST 组合」与「原子累加（reduce-scatter 风格）」之间做出有依据的取舍，并写指令序列、标出同步点。

## 2. 前置知识

本讲建立在 u7-l1（通信 ISA 总览）与 u7-l2（TGet/TPut 与信号同步）之上，以下概念默认你已掌握，这里只做一句话回顾：

- **集合通信 vs 点对点**：类比 MPI。点对点（TPUT/TGET）是「我写/读你」，集合通信（本讲四条）是「一组卡协作完成一个整体数据搬运模式」——广播（一对多）、规约（多对一求和/最值）、收集（多对一拼接）、分发（一对多切分）。
- **HCCL 窗口机制**（u7-l2）：跨卡通信前，host 侧把各卡缓冲区注册进通信窗口，kernel 里拿到的远端地址是「对端窗口基址 + 本地偏移」平移出来的 GM 视图。本讲的 `parallelGroup[r]` 就是 root 眼中 rank r 的缓冲区地址。
- **staging tile**（u7-l2）：跨卡读写要经 UB 中转，数据通路是 GM→UB tile→GM。集合指令同样遵守这条通路。
- **事件同步**（u2-l3）：`set_flag/wait_flag(srcPipe, dstPipe, eventId)` 表达跨流水线依赖；TLOAD 挂 MTE2、TSTORE 挂 MTE3、向量计算挂 V。
- **ping-pong 双缓冲**（u5-l2/u6-l2）：双份等大缓冲 + 0/1 翻转 + 按槽配对的事件，用来重叠「搬运」与「计算/写回」。
- **DYNAMIC 掩码**（u2-l2）：Tile 的有效区可以编译期静态，也可以 `DYNAMIC(-1)` + 运行期写 `RowMaskInternal/ColMaskInternal`，这是尾块不越界的关键。

**一个必须先建立的直觉**：PTO 的集合通信不是黑盒运行时调用，而是**用普通 PTO 指令（TLOAD/TSTORE/TADD…）在 AIV 上编排出来的内联代码**。打开任何一个实现头文件，你会看到的都是你已经学过的指令循环。所谓「集合通信指令」，本质是**把经典集合通信协议固化成一段可复用的模板代码**，让算子开发者一行调用即可。这个设计正是 u7-l1 讲过的「通信指令与计算指令同级的 tile 抽象」的落地。

**命名冲突警告**：仓库里存在**两套同名指令**——本讲的集合通信 `comm::TGATHER(parallelGroup, dstGlobal, tile)` 与 u4-l3 讲过的 tile 内重排 `TGATHER(dstTile, srcTile[, idxTile])`。二者签名完全不同：集合版第一个参数是 `ParallelGroup`。`tests/cpu/st/testcase/` 下的 `tgather`、`tscatter` 用例测的是 **tile 级重排**，而 `treduce`、`tbroadcast` 用例测的才是**集合通信**。查文档时认准 `docs/isa/comm/` 目录。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/pto/comm/comm_types.hpp` | 参与方抽象 `ParallelGroup` 与 `ReduceOp`/`CollEngine` 等枚举，全部集合指令的公共类型底座 |
| `include/pto/comm/pto_comm_inst.hpp` | 通信指令 API 层：`TREDUCE/TBROADCAST/TGATHER/TSCATTER` 的薄壳声明与引擎分派 |
| `include/pto/comm/pto_comm_instr_impl.hpp` | 后端路由：按 `__CCE_AICORE__` + 架构宏 / `__CPU_SIM` 选择引入哪套 `*_IMPL` |
| `include/pto/comm/a2a3/TReduce.hpp` | TREDUCE 在 A2/A3 NPU 上的实现（本讲主标本之一，单缓冲 + ping-pong 两版） |
| `include/pto/comm/a2a3/TBroadCast.hpp` | TBROADCAST 的 A2/A3 实现（单缓冲 + ping-pong 两版） |
| `include/pto/comm/a2a3/TGather.hpp` | TGATHER 的 A2/A3 实现（沿 DIM_3 拼接收集） |
| `include/pto/comm/a2a3/TScatter.hpp` | TSCATTER 的 A2/A3 实现（沿 DIM_3 切分分发，TGATHER 的逆） |
| `include/pto/cpu/comm/TReduce.hpp` | TREDUCE 的 CPU 仿真实现（无事件、单线程按序），与 NPU 版签名一致 |
| `docs/isa/comm/TREDUCE.md`、`TGATHER.md`、`TSCATTER.md`、`TBROADCAST.md` | 四条指令的 ISA 文档（另有 `_zh` 中文版） |
| `tests/cpu/st/testcase/treduce/` | 集合 TREDUCE 的 CPU ST 用例（四件套：kernel / main / gen_data / CMakeLists） |
| `tests/cpu/st/testcase/tbroadcast/` | 集合 TBROADCAST 的 CPU ST 用例 |
| `kernels/manual/a2a3/gemm_ar/comm_kernel.cpp` | 生产级 GEMM+AllReduce 融合算子的通信 kernel，综合实践的对照物 |

## 4. 核心概念与源码讲解

### 4.1 集合通信公共协议：ParallelGroup 与 root 单边执行

#### 4.1.1 概念说明

四条集合指令共享同一套协议约定，先把公共部分吃透，后面三个模块就只剩差异点。

**参与方如何描述？** 用 `ParallelGroup<GlobalData>`——一个零分配的轻量视图：它只持有一个指向外部数组的指针、rank 数和 root 下标。数组里第 r 个元素是「rank r 的缓冲区在 root 眼中的 GlobalTensor 视图」（依赖 u7-l2 的窗口机制拿到远端地址）。

**谁执行？** **只有 root 单边执行**。root 即调用方 NPU，由 `parallelGroup.GetRootIdx()` 标识。非 root 卡**不需要调用**集合指令（调了是未定义行为），只需保证两件事：

- 它贡献的缓冲区（源或目的）在整个操作期间有效、可写；
- 与 root 之间的跨卡时序（谁先写完、谁后读）由信号指令（TNOTIFY/TWAIT，u7-l2）或上层调度保证。

这是典型的 one-sided / 单边通信模型：协议的复杂度从「所有卡协同调用」转移到「root 的编排代码 + 外部同步」。

**数据走哪条路？** A2/A3 上走 **AIV 引擎**（`CollEngine::AIV`，默认）：数据通路始终是「远端/本地 GM → UB staging tile → GM」，由 TLOAD/TSTORE 完成；规约计算由向量指令完成。A5 上另有 `CollEngine::CCU` 硬件引擎路径（AIV 只负责「敲铃」触发 CKE 门控，数据面由 CCU 硬件完成），A2/A3 没有 CCU，调用会直接编译失败。

#### 4.1.2 核心流程

四条集合指令的 `*_IMPL` 都遵循同一个三级分派骨架：

```text
T*_IMPL(parallelGroup, ...)
  ├─ 静态检查：元素类型三方一致（group / dst 或 src / tile）、布局一致
  ├─ 运行期断言：nranks > 0、rootIdx ∈ [0, nranks)、validRow/Col > 0
  ├─ 若 totalRows == 0 或 gShape4 == 0 → 直接返回（空数据短路）
  │
  ├─ 路径 A「simple」：数据装得进一个 tile
  │     totalRows ≤ tileValidRow 且 gShape4 ≤ tileValidCol
  │     → 对每个 rank 直接 TLOAD/TSTORE 全量数据
  │
  ├─ 路径 B「chunked 单缓冲」：装不下
  │     外三维 DIM_0..2 显式迭代
  │     DIM_3 按 tileValidRow 滑窗、DIM_4 按 tileValidCol 滑窗
  │     每 chunk：TLOAD → (计算) → TSTORE，逐块完成
  │     DYNAMIC 掩码的 tile 每 chunk 更新 RowMaskInternal/ColMaskInternal
  │
  └─ 路径 C「chunked + ping-pong」（双缓冲重载）：
        在 B 的基础上，用 ping/pong 两块 tile 重叠
        「下一块的 TLOAD(MTE2)」与「当前块的 TSTORE(MTE3)/计算(V)」
```

其中 `totalRows = D0 × D1 × D2 × D3`（外三维被折叠进行数）。2D 滑窗分块的四条硬约束（四条指令完全一致）：

1. 所有 rank 的源/目的张量**形状与步长假定相同**（不同则为未定义行为）；
2. tile 的 `ValidRow` 为静态时，`GetShape(DIM_3)` 必须被其整除（运行期 `PTO_ASSERT`），否则用 `DYNAMIC` ValidRow 的 tile 支持非整除尾块；
3. `ValidCol` 同理作用于 `GetShape(DIM_4)`；
4. tile 元素类型必须与 GM 张量元素类型逐字相同（编译期 `static_assert`）。

API 层则统一是「等待事件 → 转发 IMPL → 返回 RecordEvent」的薄壳，与本仓库所有指令一致（u2-l4 的 `XXX → XXX_IMPL` 粘合约定）。

#### 4.1.3 源码精读

先看参与方抽象 `ParallelGroup`：

- [include/pto/comm/comm_types.hpp:35-72](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L35-L72) —— `ParallelGroup` 结构体本体：只有 `tensors`（指向外部 GlobalTensor 数组的指针）、`nranks`、`rootIdx` 三个字段，注释明确说明这是避免设备侧动态分配的轻量视图包装；`operator[]` 带越界断言地取 rank r 的张量视图。
- [include/pto/comm/comm_types.hpp:46-56](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L46-L56) —— 构造函数与推荐的 `Create` 工厂：注意 `rootIdx` 是「root 在组内的下标」，**所有 rank 必须传同一个值**，而不是各自传自己的 rank。

两个配套枚举：

- [include/pto/comm/comm_types.hpp:111-115](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L111-L115) —— `ReduceOp`：Sum/Max/Min 三种规约算子，TREDUCE 的运行期参数。
- [include/pto/comm/comm_types.hpp:127-135](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L127-L135) —— `CollEngine`：AIV（默认 tile 通路）与 CCU（AIV 触发 CKE 门控、CCU 硬件执行集合通信）。

API 层以 TBROADCAST 为例（其余三条同构）：

- [include/pto/comm/pto_comm_inst.hpp:250-264](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L250-L264) —— `TBROADCAST` API 薄壳：编译期按 `engine` 模板参数 `if constexpr` 二选一——AIV 路径先 `WaitAllEvents(args...)` 再转发 `TBROADCAST_IMPL`；CCU 路径要求第一个可变参数是 `CcuTriggerContext`（CKE 槽位虚地址 + 触发掩码）。四种集合指令每条都有「单 staging tile」与「ping/pong 双 tile」两个重载。

后端路由在：

- [include/pto/comm/pto_comm_instr_impl.hpp:29-33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_instr_impl.hpp#L29-L33) —— NPU 编译（`__CCE_AICORE__` 且非仿真）+ `PTO_NPU_ARCH_A2A3` 时，引入 `a2a3/` 下四条集合指令头文件（A5 分支同理引入 `a5/` 一套）。
- [include/pto/comm/pto_comm_instr_impl.hpp:66-70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_instr_impl.hpp#L66-L70) —— `__CPU_SIM` 下改为引入 `pto/cpu/comm/` 一套仿真实现，同一份 kernel 源码零改动切换后端（u2-l4 的三宏路由在通信 ISA 上同样成立）。

A2/A3 上 CCU 不可用的拦截方式（以 TReduce 为例，四条指令同款）：

- [include/pto/comm/a2a3/TReduce.hpp:580-588](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L580-L588) —— `TREDUCE_CCU_IMPL` 桩模板：`static_assert` 依赖 `engine` 模板参数，只在真正实例化时报错「CCU requires A5 hardware」。A5 头文件会先定义 `PTO_COMM_A5_TREDUCE_PROVIDED` 宏再包含本文件，从而用真实现顶掉这个桩。

#### 4.1.4 代码实践

**实践目标**：验证「API 薄壳 → IMPL → 双后端实现」的三层结构在四条集合指令上完全一致，建立阅读后续实现前的结构地图。

**操作步骤**：

1. 在仓库根目录执行 `grep -n "WaitAllEvents(args...)" include/pto/comm/pto_comm_inst.hpp`，数一数命中行数——每条集合指令的 AIV 分支各一处。
2. 执行 `grep -c "CollEngine engine = CollEngine::AIV" include/pto/comm/pto_comm_inst.hpp`，与上一步对照，确认每条指令的引擎分派都是编译期 `if constexpr`。
3. 执行 `ls include/pto/comm/a2a3/ include/pto/cpu/comm/`，确认两套后端目录里集合指令文件一一对应（TReduce/TBroadCast/TGather/TScatter 四件）。
4. 打开 `include/pto/comm/a2a3/TGather.hpp` 与 `include/pto/cpu/comm/TGather.hpp`，只看两个文件里 `TGATHER_IMPL` 的**函数签名**，确认逐字相同（实现体一个带事件、一个不带，后面模块会讲原因）。

**需要观察的现象**：四条指令的 API 壳、路由文件、双后端文件名呈现出严格的镜像对称——这就是 u1-l2 所说「指令 × 后端 × 架构」三维坐标在通信指令族上的体现。

**预期结果**：步骤 1 命中 8 处左右（四条指令 × 单/双缓冲两个重载的 AIV 分支）；步骤 3 两侧各能看到 4 个集合指令头文件。若与你数出的不一致，回到 `pto_comm_inst.hpp` 逐条核对即可（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ParallelGroup` 用「裸指针 + 个数」而不是 `std::vector`？

**参考答案**：因为这段代码最终要跑在 AICORE 设备侧，设备侧不支持标准库容器的动态内存分配；注释里明确写了「no dynamic memory allocation on device side」。所以它是一个指向**外部**GlobalTensor 数组的零分配视图，数组本身通常由 kernel 以局部数组形式构造（见 4.2.4 的测试用例）。

**练习 2**：非 root 卡在集合通信中要做什么？「不需要调用指令」意味着协议正确性靠什么保证？

**参考答案**：非 root 只需保证自己贡献的缓冲区在整个操作期间有效（gather/reduce 是源、scatter/broadcast 是目的）。由于 root 会直接读写远端缓冲（窗口机制），跨卡的「root 开始读之前源必须写完」「root 写完之前目的不能被读」这类时序，必须由信号同步（TNOTIFY/TWAIT，u7-l2）或上层（host 流同步）保证——这正是单边模型把复杂度外移的地方。

**练习 3**：`CollEngine::CCU` 在 A2/A3 上的调用为什么能「编译过、实例化才报错」？

**参考答案**：因为桩模板的 `static_assert` 条件依赖模板参数 `engine`，是依赖型断言——只有在某次调用真正实例化 `TREDUCE_CCU_IMPL<CollEngine::CCU>` 时才会求值并失败；未被使用的重载不报错。同时桩必须存在，否则 `pto_comm_inst.hpp` 里的受限模板名在 A2/A3 编译期根本无法解析。

---

### 4.2 TReduce：root 收集并逐元素规约

#### 4.2.1 概念说明

TREDUCE 是四条集合指令里唯一带「计算」的：root 从**所有** rank 的源缓冲取数，逐元素做同一种规约，结果写到 root 本地的目的张量。数学定义（对有效区内每个元素 \((i,j)\)）：

\[
\mathrm{dst}^{\mathrm{local}}_{i,j} = \bigoplus_{r=0}^{N-1} \mathrm{src}^{(r)}_{i,j}
\]

其中 \(N\) 是组内 rank 数，\(\oplus \in \{\text{Sum}, \text{Max}, \text{Min}\}\)。注意**这是 Reduce 而不是 AllReduce**——结果只落在 root 一份，其它卡拿不到；如何在这条指令上搭出 AllReduce 是本讲综合实践的主角。

「带计算」带来的实现差异：其它三条指令只需要 TLOAD/TSTORE 两条流水线（MTE2/MTE3），TREDUCE 还要把 Vector 流水线（V）卷进来，事件编排因此多出一对「搬运→计算」「计算→搬运」的依赖。

#### 4.2.2 核心流程

单缓冲 simple 路径（数据装进一个 tile）：

```text
TLOAD(accTile ← parallelGroup[root])          # 根数据做初值
set/wait_flag(MTE2 → V, ID0)                  # acc 就绪
for r ≠ root:                                 # 遍历远端 rank
    TLOAD(recvTile ← parallelGroup[r])
    set/wait_flag(MTE2 → V, ID1)              # recv 就绪
    Reduce(acc, recv, op)                     # TADD/TMAX/TMIN
    set/wait_flag(V → MTE2, ID0)              # recv tile 可复用
set/wait_flag(V → MTE3, ID0)                  # acc 最终值就绪
TSTORE(dstGlobal ← accTile)
set/wait_flag(MTE3 → MTE2, ID0)               # acc tile 可复用
```

远端 rank 的遍历顺序经过 `GetRemoteRank` 映射：给定「第 k 个远端」的序号，把它换算成真实组内下标（跳过 root 自己）。

数据超出一个 tile 时进入 2D 滑窗 chunked 路径：对每个行/列滑窗 chunk 完整执行一遍上面的「装载根→逐远端规约→写回」流水；若 tile 的 ValidRow/ValidCol 是 DYNAMIC，每个 chunk 先更新掩码再搬运，天然支持非整除尾块。ping-pong 变体则把「下一个远端 chunk 的 TLOAD」与「当前远端的规约」重叠：

```text
无乒乓： [TLOAD r0] → [Reduce r0] → [TLOAD r1] → [Reduce r1] → ...
有乒乓： [TLOAD r0] → [Reduce r0 ∥ TLOAD r1] → [Reduce r1 ∥ TLOAD r2] → ...
```

#### 4.2.3 源码精读

- [include/pto/comm/a2a3/TReduce.hpp:27-44](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L27-L44) —— `ReduceTiles` 辅助函数：把 `ReduceOp` 运行期枚举翻译成 `TADD/TMAX/TMIN` 三条向量指令之一，未知 op 直接断言。这正是不带 CCU 的 AIV 集合路径的本质——**集合通信的算子就是普通 tile 指令**。
- [include/pto/comm/a2a3/TReduce.hpp:46-49](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L46-L49) —— `GetRemoteRank`：`remoteOrdinal < rootIdx ? remoteOrdinal : remoteOrdinal + 1`，一行完成「远端序号 → 组内下标」的跳根映射。
- [include/pto/comm/a2a3/TReduce.hpp:54-89](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L54-L89) —— `TreduceSimple`：上面流程图的逐行落地。注意 `nranks == 1` 时（59-67 行）退化为「TLOAD→TSTORE 纯拷贝」，规约循环整体跳过；主循环里 `set_flag(PIPE_V, PIPE_MTE2)` 是在告诉 MTE2「recvTile 我用完了，可以装下一家」——这就是 u6-l2 讲过的「防写覆盖」反向事件。
- [include/pto/comm/a2a3/TReduce.hpp:196-219](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L196-L219) —— TREDUCE_IMPL 的总注释块：写明 root 语义、2D 滑窗策略与全部 chunked 约束，是与 ISA 文档同步的权威描述。
- [include/pto/comm/a2a3/TReduce.hpp:221-283](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L221-L283) —— `TREDUCE_IMPL` 单缓冲版主入口：类型三方一致的 `static_assert`（229-232 行）、组/根的运行期断言（237-238 行）、空数据短路（254 行）、simple/chunked 分派（258-262 行）、静态 ValidRow/Col 的整除断言（264-278 行）。读任何一条 PTO 指令的 `*_IMPL`，这五段都是标准开头。
- [include/pto/comm/a2a3/TReduce.hpp:290-335](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L290-L335) —— `TreduceSimplePingPong`：simple 路径的乒乓版。304-308 行先把 root 和第一个远端**连续两个 TLOAD** 挂进流水线（分别用 EVENT_ID0/ID1 标记），循环体里 317-320 行提前装载下一远端、321 行才等当前就绪——「先发后等」正是重叠的写法；事件 ID0/1/2 按 ping/pong 槽位配对，是 u6-l2 双缓冲三要素的完整示范。
- [include/pto/comm/a2a3/TReduce.hpp:486-502](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L486-L502) —— 乒乓版 TREDUCE_IMPL 的注释块，直接画出了上面两组时间线，是理解「为什么要多花一块 UB 换重叠」的最短解释。
- [include/pto/comm/a2a3/TReduce.hpp:504-568](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L504-L568) —— 乒乓版主入口：结构与单缓冲版完全平行，仅把 recvTile 换成 ping/pong 双 tile、远端循环交给 `TreducePingPongLoop`（338-379 行）。

CPU 仿真版对照：

- [include/pto/cpu/comm/TReduce.hpp:121-146](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/comm/TReduce.hpp#L121-L146) —— CPU 版的 simple 路径：算法骨架（TLOAD 根 → 循远端 TLOAD + `ReduceTiles` → TSTORE）与 NPU 版逐行对应，但**一条 set_flag/wait_flag 都没有**——CPU 仿真单线程按序执行，同步全是空桩（u2-l3 的结论在这里再次兑现）。
- [include/pto/cpu/comm/TReduce.hpp:474-492](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/comm/TReduce.hpp#L474-L492) —— CPU 版的 `TREDUCE_CCU_IMPL` 直接转发到 AIV 实现：CPU 仿真没有 CCU 硬件，语义上用 tile 通路模拟，方便在无硬件环境验证调用方代码的形状/参数逻辑。

#### 4.2.4 代码实践

**实践目标**：跑通集合 TREDUCE 的 CPU ST 用例，并通过「改规约算子」体会 kernel 侧与 golden 侧必须同步修改的测试闭环。

**操作步骤**：

1. 阅读 kernel 侧 [tests/cpu/st/testcase/treduce/treduce_kernel.cpp:26-64](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treduce/treduce_kernel.cpp#L26-L64)：注意 45-51 行用两个 GM 指针构造 `Global tensors[2]`，再包成 `ParallelGroup`（模拟 2 卡、root=0）；60-63 行 `my_rank == 0` 才调用 TREDUCE，是「仅 root 执行」的活样本。
2. 运行用例（CPU 仿真路径，u1-l3 的统一入口）：
   ```bash
   python3 tests/run_cpu.py --testcase treduce
   ```
3. 查看 golden 生成 [tests/cpu/st/testcase/treduce/gen_data.py:59-61](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treduce/gen_data.py#L59-L61)：case1 用 `np.maximum`（对应 `ReduceOp::Max`），case2 用 `np.add`（对应 `ReduceOp::Sum`）——golden 就是 numpy 对两个输入做的同款规约。
4. 仿照 case1 新增 case3：`gen_data.py` 里把算子换成 `np.minimum`、参数列表加一行；`main.cpp` 里对应新增 `TEST_F(TREDUCETest, case3)` 并把 `ReduceOp::Max` 换成 `ReduceOp::Min`（kernel 模板会透传 `op`）。
5. 重新运行 `python3 tests/run_cpu.py --testcase treduce`，观察 case3 通过；随后做一个反证——只改 `main.cpp` 的 `ReduceOp` 而不改 `gen_data.py`，重跑，观察用例以数值不匹配失败。

**需要观察的现象**：改动前 case1/case2 全绿；第 5 步的反证中 gtest 报告 golden 与实际输出的逐元素差异（Sum 与 Max 在随机数据上几乎必然不同）。

**预期结果**：case3（Min）通过、反证实验失败——这说明「指令语义 = kernel 的 op 参数」与「期望 = golden 的 numpy 算子」是同一契约的两端。本实践涉及重新编译，具体输出以本地为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：TREDUCE 的 simple 循环里，`set_flag(PIPE_V, PIPE_MTE2, EVENT_ID0)` 每轮都执行一次，它保护什么？

**参考答案**：它向 MTE2 流水线声明「recvTile 上的向量计算已完成」，防止下一轮 `TLOAD(recvTile, ...)` 在 V 还在读旧数据时就覆写同一块 UB——即 u6-l2 说的「防写覆盖」方向的事件。没有它，真机上可能出现下一轮搬运与当前规约的读写竞争。

**练习 2**：8 卡场景下 root 用单缓冲 TREDUCE 规约一个大张量，瓶颈在哪？乒乓版改善了什么、没改善什么？

**参考答案**：单缓冲下每个远端 chunk 是「装载→规约」纯串行，MTE2 与 V 轮流空转，瓶颈在两段流水无法重叠。乒乓版把 TLOAD[k+1] 与 Reduce[k] 重叠，消除了这段空转；但它没有消除「root 一家做全部 7 份远端搬运」的串行结构——这是协议层的串行，只有换原子累加方案（见综合实践）或 CCU 引擎才能根治。

**练习 3**：为什么 TREDUCE 要求 tile 元素类型与 GM 张量**完全相同**，而不允许边规约边升精度？

**参考答案**：实现里 `ReduceTiles` 是 `TADD(acc, acc, recv)` 这类同型 tile 的逐元素指令，acc 与 recv 共用同一个 TileData 模板参数，编译期 `static_assert` 直接锁死三方同型（TReduce.hpp 229-232 行）。若要升精度（如 fp16 求和用 fp32 累加），需要调用方在调用前自行 TLOAD+TCVT 构造 fp32 缓冲，集合指令本身不提供隐式转换。

---

### 4.3 TBroadCast：root 一对多分发

#### 4.3.1 概念说明

TBROADCAST 把 root 本地的一个源张量写到**组内每一个 rank 的缓冲区**（注意：包括 root 自己那份——目的地址就是 `parallelGroup[r]`）。数学上就是复制：

\[
\mathrm{dst}^{(r)}_{i,j} = \mathrm{src}^{\mathrm{local}}_{i,j} \quad \forall\, r \in [0, N)
\]

与 TREDUCE 的「多对一 + 计算」相对，它是纯「一对多搬运」，实现里因此**完全不涉及 Vector 流水线**——只有 TLOAD(MTE2) 与 TSTORE(MTE3) 两段，事件只需表达「装好了才能写」与「写完才能复用 tile」。

#### 4.3.2 核心流程

```text
simple（装得进一个 tile）:
  TLOAD(stagingTile ← srcGlobal)
  set/wait_flag(MTE2 → MTE3)           # tile 就绪
  for r in [0, nranks):                 # 注意包含 root 自己
      TSTORE(parallelGroup[r] ← stagingTile)
      set/wait_flag(MTE3 → MTE2)        # 本 tile 写完，防止下一轮装载覆写

nranks == 1: 退化为 TLOAD → TSTORE 本地拷贝

chunked（装不下）: 对每个 2D 滑窗 chunk 执行 TbroadcastChunkTransfer：
  TLOAD(chunk) → N 次 TSTORE(到每个 rank 的同位置偏移)

ping-pong: 用状态机把「下一块 TLOAD」与「当前块 N 次 TSTORE」重叠，
           最后一块由 Epilogue 补写
```

乒乓版的特别之处：因为一个 chunk 要写 N 份，pending 状态只需要记「上一块写到哪个目的偏移、多大」，不用记 rank（所有 rank 都要写）。

#### 4.3.3 源码精读

- [include/pto/comm/a2a3/TBroadCast.hpp:172-231](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L172-L231) —— `TBROADCAST_IMPL` 单缓冲主入口。结构仍是 4.1.2 的五段式；注意 179-185 行的三条 `static_assert` 里多了一条**布局相等**（`src layout == dst layout`），ND/NZ 等布局混用会编译失败。
- [include/pto/comm/a2a3/TBroadCast.hpp:210-226](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L210-L226) —— 两个快速路径：`nranks == 1`（210-216 行）退化为一次本地 TLOAD→TSTORE；装得进 tile 时（218-226 行）一次 TLOAD 后对**所有 rank（含 root）**循环 TSTORE，每份写完立刻 `MTE3→MTE2` 回牌。
- [include/pto/comm/a2a3/TBroadCast.hpp:35-51](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L35-L51) —— `TbroadcastChunkTransfer`：chunked 路径的原子单元，45-48 行对每个 rank 用「`parallelGroup[r].data() + dstOffset` + 同一 chunk 视图」构造目的视图再 TSTORE——一次装载、N 次写回，这是广播能把 GM 读放大压到 1/N 的关键。
- [include/pto/comm/a2a3/TBroadCast.hpp:234-286](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L234-L286) —— `TbroadcastPingPongProcessChunk`：乒乓状态机核心。260-279 行是重叠段——先 `wait_flag(MTE2→MTE3, prevEvent)` 等上一块装好，然后**先 TSTORE 上一块、再 TLOAD 当前块**（271-274 行两条装载指令紧挨着发射），最后两个方向各回一张牌。281-285 行把当前块记入 pending，翻转 ping/pong。
- [include/pto/comm/a2a3/TBroadCast.hpp:289-313](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L289-L313) —— `TbroadcastPingPongEpilogue`：循环走完后最后一块还悬在 pending 里，这里补上它的 N 次 TSTORE 并做收尾同步——「延迟一块写回」是所有流水浒板式的标准收尾，与 u6-l2 的 drain 阶段同构。
- [include/pto/comm/a2a3/TBroadCast.hpp:400-413](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L400-L413) —— 乒乓版注释块：`[N×TSTORE chunk0 | TLOAD chunk1]` 的时间线写明了重叠的对象是「当前块的写回组」与「下一块的装载」。

#### 4.3.4 代码实践

**实践目标**：跑通集合 TBROADCAST 的 CPU ST 用例，并亲手验证「广播输出 = 输入的 N 份拷贝」以及 chunked 分块条件的判定。

**操作步骤**：

1. 阅读 kernel [tests/cpu/st/testcase/tbroadcast/tbroadcast_kernel.cpp:22-41](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tbroadcast/tbroadcast_kernel.cpp#L22-L41)：28-33 行把**同一段输出缓冲按 `sizePerCopy` 等分**成 KN 个视图构造 ParallelGroup——用一块 GM 模拟 KN 张卡的窗口缓冲，这是单进程验证集合通信的常用手法；37-38 行声明了 `ValidRow/ValidCol = DYNAMIC(-1)`、容量 D3×D4 的 tile。
2. 运行：
   ```bash
   python3 tests/run_cpu.py --testcase tbroadcast
   ```
3. 阅读 golden 生成 [tests/cpu/st/testcase/tbroadcast/gen_data.py:15-29](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tbroadcast/gen_data.py#L15-L29)：`golden = np.tile(input1, rank)`——期望输出就是输入平铺 rank 份，与广播语义严格对齐。
4. 对 case1（`<float, 1, 2, 4, 64, 64, 5>`）手工判定分派路径：`totalRows = 1×2×4×64 = 512 > tileValidRow(64)`，故走 chunked 路径，行方向滑 `512/64 = 8` 个 chunk、列方向 1 个，共 8 轮，每轮向 5 个「rank」各写一份。
5. 把 case1 的 KN 从 5 改成 3（kernel 显式实例化与 main.cpp 的调用都要改），重跑，观察输出份数随之变化。

**需要观察的现象**：步骤 2 全部用例通过；步骤 5 输出缓冲中输入块重复 3 次而非 5 次（golden 也需同步用 rank=3 重新生成，`run_cpu.py` 会在运行前自动执行 gen_data）。

**预期结果**：用例全绿；手工判定的 8 个行 chunk 与实现行为一致（可在 CPU 版 `TBroadcast.hpp` 的 chunked 循环里临时加打印验证，验完删掉）。步骤 5 的具体输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：TBROADCAST 为什么把 root 自己也包括在 TSTORE 循环里？

**参考答案**：因为目的地址统一由 `parallelGroup[r]` 描述——组内每个 rank（含 root）在窗口里注册的缓冲都会收到一份，这样「广播」对组内所有成员语义一致，调用方不需要为 root 单独写一个本地拷贝分支。同时这也意味着 root 的窗口缓冲会被重写，若 root 还需要保留原值应另行备份。

**练习 2**：广播的乒乓版把「下一块 TLOAD」与「当前块 N 次 TSTORE」重叠。为什么不像 TREDUCE 那样与「计算」重叠？

**参考答案**：广播根本没有计算——它是纯搬运指令，数据通路只有 MTE2（装）与 MTE3（写）两条流水线，能重叠的只有这两段；TREDUCE 才有 V 流水线可与之重叠。这印证了「乒乓的对象是相邻两段都会空转的流水线」这一通用原则。

**练习 3**：若把 `parallelGroup` 里两个 rank 的张量声明成不同 layout（一个 ND、一个 NZ），会发生什么？

**参考答案**：编译失败。`TBROADCAST_IMPL` 有 `static_assert(GlobalSrcData::layout == GlobalDstData::layout)`（TBroadCast.hpp 185 行），src 与组内张量的布局必须一致；四条集合指令都有同款断言，因为 chunk 视图类型是用同一个 layout 模板参数实例化的。

---

### 4.4 TGather/TScatter：按行拼接收集与切分分发

#### 4.4.1 概念说明

这两条互为逆操作，都沿 **DIM_3（行维）** 切：

- **TGATHER（收集）**：root 从每个 rank 收一份 \((D_0,D_1,D_2,H,W)\)，在本地拼成 \((D_0,D_1,D_2,N \cdot H,W)\)，rank r 的数据落在结果的第 \(r \cdot H\) 到 \((r+1)\cdot H\) 行：
  \[
  \mathrm{dst}_{d_0,d_1,d_2,\;r\cdot H+i,\;j} = \mathrm{src}^{(r)}_{d_0,d_1,d_2,\;i,\;j}
  \]
- **TSCATTER（分发）**：root 把本地 \((D_0,D_1,D_2,N\cdot H,W)\) 的源按行等分，第 r 段发给 rank r：
  \[
  \mathrm{dst}^{(r)}_{d_0,d_1,d_2,\;i,\;j} = \mathrm{src}^{\mathrm{local}}_{d_0,d_1,d_2,\;r\cdot H+i,\;j}
  \]

注意两个「宽松约定」：目的（gather）或源（scatter）张量的 DIM_3 **大于** \(N \cdot H\) 时，多出的行不读不写；两指令都假定各 rank 张量同形同步长。

一个容易踩的实现细节：**TSCATTER 的形状基准取自 `parallelGroup[0]`（每个 rank 的目的形状），而不是源张量**——因为「每份多大」由接收方缓冲决定，源只要足够长即可；TGATHER 则相反，以 rank 侧（源）形状为基准推目的偏移。

#### 4.4.2 核心流程

```text
TGATHER simple:
  for r in [0, nranks):
      TLOAD(stagingTile ← parallelGroup[r])       # 取 rank r 整份
      set/wait(MTE2 → MTE3)
      dstOffset = r × H × dstStride(DIM_3)         # 行拼接种子偏移
      TSTORE(dstGlobal + dstOffset ← stagingTile)  # 写进第 r 段
      set/wait(MTE3 → MTE2)

TSCATTER simple:
  for r in [0, nranks):
      srcOffset = r × H × srcStride(DIM_3)         # 从源的第 r 段读
      TLOAD(stagingTile ← srcGlobal + srcOffset)
      set/wait(MTE2 → MTE3)
      TSTORE(parallelGroup[r] ← stagingTile)       # 写到 rank r
      set/wait(MTE3 → MTE2)

chunked：外三维迭代 + 行/列滑窗，每 chunk 走一遍"取一段→写一段"
ping-pong：与 TBROADCAST 同构（pending 状态 + Epilogue 补尾块），
           TSCATTER 的 pending 额外记录 pendingRank（每块去向不同 rank）
```

偏移种子是这两条指令的灵魂：`rankDstBase = r × perRankRows × stride(DIM_3)`，行方向天然按 rank 分段，列方向共用 stride(DIM_4)。

#### 4.4.3 源码精读

- [include/pto/comm/a2a3/TGather.hpp:148-168](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L148-L168) —— TGATHER_IMPL 注释块：写明「沿 DIM_3 拼接、rank r 落在 \([r\cdot H,(r+1)\cdot H)\) 行」的数学语义与全部约束。
- [include/pto/comm/a2a3/TGather.hpp:35-63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L35-L63) —— `TgatherSimple`：53-62 行的循环体就是流程图的逐行实现——57 行 `dstOffset = r × perRankRows × dstStride3` 是行拼接种子，58 行用「目的基址 + 偏移 + 每 rank 形状 + 目的步长」构造视图后 TSTORE。
- [include/pto/comm/a2a3/TGather.hpp:170-234](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L170-L234) —— `TGATHER_IMPL` 主入口：183-186 行取组大小与 root；195 行 `perRankRows = gShape3`（以 rank 侧形状为基准）；207-213 行判 simple；216-229 行静态 ValidRow/Col 的整除断言。
- [include/pto/comm/a2a3/TGather.hpp:237-290](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L237-L290) —— `TgatherPingPongProcessChunk`：结构同 TBROADCAST 的版本——`hasPending` 时先写上一块再装当前块（264-279 行）；差别是 pending 只记偏移不记 rank（gather 的目的地永远在 root 本地）。
- [include/pto/comm/a2a3/TScatter.hpp:145-165](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L145-L165) —— TSCATTER_IMPL 注释块：明确「源形如 \((D_0,D_1,D_2,N\times H,W)\)、rank r 收第 r 段」及 TGATHER 逆操作的定位。
- [include/pto/comm/a2a3/TScatter.hpp:167-231](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L167-L231) —— `TSCATTER_IMPL` 主入口：186-192 行的形状全部读自 `parallelGroup[0]`（每个 rank 的**目的**缓冲），`perRankRows = gShape3` 由此而来——这就是「形状基准取接收方」的实现落点。
- [include/pto/comm/a2a3/TScatter.hpp:36-63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L36-L63) —— `TscatterSimple`：与 `TgatherSimple` 严格镜像——gather 的 `dstOffset` 换成 54 行的 `srcOffset = r × perRankRows × srcStride3`，读远端换成写远端。
- [include/pto/comm/a2a3/TScatter.hpp:25-33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L25-L33) —— `TscatterPingPongState`：比 gather 的状态多一个 `pendingRank`——scatter 每块的写回目标 rank 不同，Epilogue 补尾块时必须知道最后一块要去谁家。

#### 4.4.4 代码实践

**实践目标**：为集合 TGATHER/TSCATTER 做一次「手工仿真 + 用例骨架设计」。由于 `tests/cpu/st/testcase/` 下的 `tgather`、`tscatter` 是 tile 级重排指令的用例（见 2 节的命名冲突警告），集合版没有现成 CPU 用例，本实践带你以 treduce 用例为模板搭一个可运行的骨架，并先在纸上算清偏移。

**操作步骤**：

1. **手工仿真**：设 4 个 rank，每 rank 持有 \((1,1,1,2,8)\) 的 fp32 数据（2 行 8 列），行主序（stride3 = 8，stride4 = 1）。写出 TGATHER 后目的张量的形状与每个 rank 的数据落点；再写 TSCATTER 把 \((1,1,1,8,8)\) 的源分回 4 个 rank，验证两次操作的偏移种子都是 \(r \times 2 \times 8\) 个元素。
2. **抄骨架**：把 `tests/cpu/st/testcase/tbroadcast/` 四件套复制为 `tests/cpu/st/testcase/tbroadcast_gather/`（先只做阅读，不改仓库源码；如需真正编译请复制到自己的实验分支）。kernel 侧参照 [tests/cpu/st/testcase/tbroadcast/tbroadcast_kernel.cpp:22-41](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tbroadcast/tbroadcast_kernel.cpp#L22-L41) 的写法：一块 GM 按 rank 等分成 KN 段构造 `ParallelGroup`，再声明一个 \(KN \times H\) 行的目的张量，调用 `TGATHER(group, dstG, stagingTile)`。
3. **golden 对照**：gen_data.py 里 golden 用 numpy 沿行拼接：`golden = np.concatenate([input_r for r in ranks], axis=0)`（对 5-D 张量沿 axis=3 拼接），与实现里的 `dstOffset = r × H × stride3` 对齐。
4. **互逆性检查**：再写一个 scatter→gather 串联调用：先把拼接好的大张量 TSCATTER 回各段缓冲，再 TGATHER 拼回，golden 就是原大张量——若两条指令实现正确，往返必须无损。

**需要观察的现象**：步骤 1 的手算偏移与 `TgatherSimple`/`TscatterSimple` 里的 `r × perRankRows × stride3` 表达式一致；步骤 4 的往返结果与原数据逐元素相等。

**预期结果**：偏移种子全部吻合、往返无损。步骤 2-4 的编译与运行结果待本地验证（本实践为设计型，未在本环境执行）。

#### 4.4.5 小练习与答案

**练习 1**：TSCATTER 的形状为什么从 `parallelGroup[0]`（目的）读，而不是从源张量读？

**参考答案**：scatter 的语义是「每 rank 收 H 行」，H 由接收方缓冲的 DIM_3 定义；源只被约束为「至少 N×H 行」（多出的行被忽略）。若以源形状为基准，就无法知道每份该切多大——除非约定源 DIM_3 恰好整除 N，而实现选择了更宽松的「接收方定尺寸」约定。

**练习 2**：用 TGATHER + TBROADCAST 组合能搭出什么经典集合通信？

**参考答案**：AllGather——先 TGATHER 把各 rank 数据拼到 root，再 TBROADCAST 把拼接结果发回所有 rank。每个 rank 最终都持有 \((D_0,D_1,D_2,N\cdot H,W)\) 的完整张量。注意中间需要一次跨卡同步（root 拼完之前不能开始广播非 root 侧的读）。

**练习 3**：`TgatherPingPongState` 与 `TscatterPingPongState` 的字段差异说明了什么设计点？

**参考答案**：后者多了 `pendingRank`。gather 的每块写回目的地固定在 root 本地，状态只需偏移与尺寸；scatter 的每块去向是不同 rank，Epilogue 补最后一块时必须知道「最后一块要发给谁」。状态结构体精确反映了各指令数据通路的拓扑差异。

---

## 5. 综合实践

**任务：设计一个 8 卡 AllReduce 方案（\(\mathrm{out} = \sum_{r=0}^{7} \mathrm{in}^{(r)}\)），在 TREDUCE+TBROADCAST 组合与 TGET/TPUT 原子累加之间做取舍，写出指令序列并标出同步点。**

### 方案 A：TREDUCE（root=0）→ 跨卡同步 → TBROADCAST（root=0）

指令序列（以 rank 0 为 root，`buf[r]` 为窗口里 rank r 的缓冲）：

```text
# 阶段 0：各 rank 的输入就绪（各自前序 kernel 产出 in[r]）
# 同步点 ①（输入可见性）：7 个非 root 用 TNOTIFY(AtomicAdd) 给 rank0 的
#   到达计数信号 +1；rank0 TWAIT(GE 7) 后才可开跑 TREDUCE，
#   保证 root TLOAD 远端缓冲时数据已写完（必要时配合 dcci/dsb，见 u7-l2）。

# 阶段 1：仅 rank 0 执行
TREDUCE(group, out0, accTile, pingTile, pongTile, ReduceOp::Sum)

# 同步点 ②（结果可见性）：rank0 完成 TBROADCAST 后 TNOTIFY 各非 root；
#   非 root TWAIT 后再读自己的 buf[r]。
#   （rank0 侧 TREDUCE 与 TBROADCAST 之间的本地依赖已由指令内部的
#    MTE3→MTE2 事件闭环保证，无需额外同步。）

# 阶段 2：仅 rank 0 执行，把结果发回所有 rank（含自己）
TBROADCAST(group, out0, pingTile, pongTile)
```

**特性**：指令最少、逻辑最直白；但两阶段都由 root 一家执行——root 要做 7 次远端装载 + 规约 + 8 次写回，是**串行瓶颈**；且中间有一次全卡同步点，两阶段无法重叠。

### 方案 B：原子累加（reduce-scatter 风格），无 root 瓶颈

每 rank **并发**把自己的数据原子加到所有 rank 的缓冲：

```text
# 各 rank 初始化：把自己的 in[r] 写入自己的 buf[r]（本地 TLOAD→TSTORE 拷贝）
for j ≠ my_rank:                                   # 7 个对端
    TPUT<AtomicType::AtomicAdd>(buf[j], in[my], stagingTile)
    TNOTIFY(sig[j], 1, NotifyOp::AtomicAdd)        # 告诉 j「我加完了」

# 同步点（唯一）：每个 rank TWAIT(sig[my], GE 7)，
#   收齐 7 个对端的原子加完成信号后，buf[my] 即全量和。
#   信号本身用 AtomicAdd 计数，天然支持多生产者（u7-l2 的纪元式信号）。
```

**特性**：8 张卡同时干活，无 root 串行、无两阶段屏障；代价是 \(N(N-1) = 56\) 份全量传输（每份数据被复制 8 次），互联流量放大 8 倍——数据大时应先把数据按行切成 N 段、每段只发给它的 owner（**ReduceScatter**：流量降为 \(N-1\) 份），再配一次 AllGather 拿全量。

### 生产实践的对照与结论

仓库里的 GEMM+AllReduce 融合算子选的就是方案 B 的改良版：

- [kernels/manual/a2a3/gemm_ar/comm_kernel.cpp:11-16](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/comm_kernel.cpp#L11-L16) —— 注释写明策略：「RS 用 TPUT<AtomicAdd> 直接在 owner 的 reduced_output 上累加，然后发布 owner 本地的就绪计数器，让 AG（AllGather）无需设备级 RS→AG 全局屏障就能排空已完成的子块」——即 **ReduceScatter + AllGather 分解 + 原子累加 + 细粒度信号**，比朴素方案 A/B 都更精细。

**结论建议**：8 卡、中小数据量、快速验证逻辑 → 方案 A（两行指令，正确性最容易保证，CPU 仿真即可验证语义）；追求吞吐、与计算重叠 → 方案 B 的 RS+AG 变体；A5 平台且 host 侧能配合 `HcclCcuKernelRegister/HcclCcuKernelLaunch` 时，评估 `CollEngine::CCU` 硬件路径。无论哪种方案，同步点只有两类：**搬运前的「源就绪」**与**搬运后的「结果可读」**，全部用信号三件套（TNOTIFY/TWAIT/TTEST）表达——这正是 u7-l2 与本讲的衔接点。

## 6. 本讲小结

- 四条集合指令共享一套协议：`ParallelGroup` 描述参与方（root 视角下各 rank 的缓冲视图），**仅 root 单边执行**，非 root 只保证缓冲有效，跨卡时序靠信号指令外置。
- A2/A3 上的集合通信是 **AIV 引擎上的 TLOAD/TSTORE（+向量指令）编排**，不是黑盒：TREDUCE 的算子就是 `ReduceTiles` 里的 TADD/TMAX/TMIN。
- 所有指令统一三级分派：装得进 tile 的 simple 路径 → 2D 滑窗 chunked 路径（外三维迭代 + 行列滑窗 + DYNAMIC 掩码尾块）→ ping-pong 双缓冲变体（重叠下一块装载与当前块写回/规约），静态 ValidRow/Col 有整除断言约束。
- TReduce 是 **Reduce 而非 AllReduce**（结果只在 root）；Broadcast 写回**包含 root 自己**在内的所有组缓冲；Gather/Scatter 沿 DIM_3 以 `r × H × stride3` 为偏移种子互逆切拼，Scatter 的形状基准取接收方 `parallelGroup[0]`。
- 引擎选择是编译期的：`CollEngine::AIV` 默认可用，`CollEngine::CCU` 仅 A5 有真实现，A2/A3 上是实例化才报错的 `static_assert` 桩；CPU 仿真后端同签名、去事件、单线程按序。
- **命名冲突**：`comm::TGATHER/TSCATTER`（集合，首参 ParallelGroup）与 tile 级重排指令同名不同签名，`tests/cpu` 里的 tgather/tscatter 用例测的是后者。

## 7. 下一步学习建议

1. **u7-l4（异步通信与通信引擎）**：集合指令的搬运全部走 staging tile 受 UB 带宽封顶；下一讲对比 SDMA/URMA 直通 GM 的异步通路（TPUT_ASYNC/TGET_ASYNC），你会看到「tile 中转」与「DMA 直通」两条路线的取舍。
2. **u7-l5（GEMM+AllReduce 融合算子）**：精读 `kernels/manual/a2a3/gemm_ar/comm_kernel.cpp`，看生产代码如何把本讲综合实践的 RS+AG 方案落成「子块粒度就绪计数 + 免全局屏障」的流水线。
3. **动手延伸**：把 5 节综合实践的方案 A 在 CPU 仿真下真的搭出来（参照 treduce/tbroadcast 用例的单进程多窗口缓冲手法），再读 `include/pto/comm/a5/TReduce.hpp` 对比 CCU 真实现与 A2/A3 tile 编排在签名与约束上的差异。
4. **文档对照**：通读 `docs/isa/comm/` 下四条指令的 `*_zh.md`，特别注意 CCU 路径「所有 rank 都要经 host 注册/启动」与 AIV 路径「仅 root 执行」的参与方差异。
