# 集合通信：TReduce、TBroadCast、TGather/TScatter

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 PTO 四条集合通信指令（TGATHER/TSCATTER/TBROADCAST/TREDUCE）的**参与方模型**：谁执行、数据从哪来、结果落到哪。
2. 掌握 `ParallelGroup` 如何描述「一组跨卡 buffer」，以及四条指令各自的数据切分语义（逐元素折叠、扇出复制、沿 DIM_3 拼接/切分）。
3. 理解 A2/A3 上 `CollEngine::AIV` 引擎的实现路径：集合指令 = `TLOAD/TSTORE` + 普通计算指令 + 事件闭环的组合，并自动完成 2D 滑窗分块与可选的 ping-pong 双缓冲。
4. 面对实际需求（如 8 卡 AllReduce）能选择合适的集合通信原语并组合出指令序列，标出同步点。

## 2. 前置知识

本讲建立在前两讲（u7-l1 通信 ISA 总览、u7-l2 点对点通信）之上，先回顾几个关键认知：

- **集合通信 vs 点对点**：借用 MPI 的概念，点对点（TPUT/TGET）是「两张卡之间搬一份数据」；集合通信（Reduce/Broadcast/Gather/Scatter）是「一组卡共同参与的数学操作」，但 PTO 的实现方式与 MPI 有本质差异——见 4.1 节的「root 单方执行」模型。
- **远端地址从哪来**：u7-l2 讲过 HCCL 窗口机制与 `CommRemotePtr` 指针平移（对端窗口基址 + 本地偏移）。root 拿到 `parallelGroup[r]` 的地址后，对这个地址做 `TLOAD` 就能读到 r 号卡的 buffer——窗口机制让远端 GM 在 root 的地址空间里「直接可见」，这是集合指令能用普通 `TLOAD/TSTORE` 实现的前提。
- **staging tile**：跨卡数据都要经过 UB 上的 tile 中转，带宽受 UB 封顶。集合指令同样遵守这条通路。
- **事件同步**：`(srcPipe, dstPipe, eventId)` 三元组配对、set/wait 成对出现（u2-l3）。MTE2=搬入、V=向量计算、MTE3=搬出三条流水线并行执行。
- **5 维 GlobalTensor 与有效区**：DIM_3 是行、DIM_4 是列；tile 的 `ValidRow/ValidCol` 可以是静态常量也可以是 `DYNAMIC`（-1），动态时通过 `RowMaskInternal/ColMaskInternal` 在运行期填写（u2-l1/u2-l2）。
- **注意区分两种「多」**：u6-l1 的 `SyncAll` 是**同一张卡内多个核**之间的栅栏；本讲的集合通信是**多张 NPU 卡**之间的数据操作，两者层级不同。

本讲新术语：

| 术语 | 含义 |
|------|------|
| ParallelGroup | 一组参与集合通信的 GlobalTensor 视图（每张卡一个），零分配的轻量包装 |
| root | 集合操作的主导卡，由 `ParallelGroup::rootIdx` 指定，是「组内下标」而非全局卡号 |
| AIV 引擎 | 集合指令的默认实现路径：AIV 核用 TLOAD/TSTORE + 计算指令组合完成 |
| CCU 引擎 | A5 独有的集合通信硬件引擎，AIV 只负责「按门铃」触发 |
| 2D 滑窗分块 | 数据超过单个 UB tile 容量时，按「外层三维显式迭代 + 行/列滑窗」自动切块 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/pto/comm/comm_types.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp) | `ParallelGroup`、`ReduceOp`、`CollEngine`、`CcuTriggerContext` 等公共类型定义 |
| [include/pto/comm/pto_comm_inst.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp) | 用户侧 API 薄壳：TGATHER/TSCATTER/TBROADCAST/TREDUCE 的声明与 engine 分发 |
| [include/pto/comm/pto_comm_instr_impl.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_instr_impl.hpp) | 按「架构 × 后端」互斥引入 a2a3 / a5 / cpu 实现头 |
| [include/pto/comm/a2a3/TReduce.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp) | TREDUCE 的 A2/A3 NPU 实现（本讲主标本） |
| [include/pto/comm/a2a3/TBroadCast.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp) | TBROADCAST 的 A2/A3 NPU 实现 |
| [include/pto/comm/a2a3/TGather.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp) | TGATHER 的 A2/A3 NPU 实现 |
| [include/pto/comm/a2a3/TScatter.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp) | TSCATTER 的 A2/A3 NPU 实现 |
| [include/pto/cpu/comm/TReduce.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/comm/TReduce.hpp) | TREDUCE 的 CPU 仿真实现（用于对照「事件被抽掉后剩什么」） |
| [tests/cpu/st/testcase/treduce/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treduce/treduce_kernel.cpp) | TREDUCE 的 CPU ST 用例（kernel + main + gen_data 三件套） |
| [tests/cpu/st/testcase/tbroadcast/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tbroadcast/tbroadcast_kernel.cpp) | TBROADCAST 的 CPU ST 用例 |
| [docs/isa/comm/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/README.md) | 通信 ISA 文档索引与类型定义说明 |

## 4. 核心概念与源码讲解

### 4.1 公共骨架：ParallelGroup 与「root 单方执行」模型

#### 4.1.1 概念说明

四条集合指令共享同一套参与方描述——`ParallelGroup`。它回答三个问题：

1. **有谁**：一个 `GlobalData` 数组，第 r 个元素是 r 号卡参与数据的 GM 视图（经窗口机制映射到 root 可见的地址）。
2. **有多少**：`nranks`。
3. **谁是 root**：`rootIdx` 是组内下标（不是全局卡号），所有卡的 kernel 必须传同一个 rootIdx。

与 MPI「全员调用同一集合接口」完全不同，PTO 的集合通信是 **root 单方执行**：只有 root 卡真正跑指令序列，非 root 卡在整个集合操作期间什么都不用做，只需保证自己的源/目的 buffer 已就绪且操作期间有效。文档明确：非 root 卡调用 TREDUCE 属于未定义行为。

由此可以写出四条指令的语义总表（这是本讲最重要的一张表）：

| 指令 | 谁执行 | 源 | 目的 | 数据关系 |
|------|--------|----|------|----------|
| TREDUCE | root | 每卡一块同形 tensor（含 root 自己） | root 本地 dst | 逐元素折叠（Sum/Max/Min） |
| TBROADCAST | root | root 本地 src | 每卡 buffer 各得一份（含 root 自己） | N 份相同拷贝 |
| TGATHER | root | 每卡一块同形 tensor | root 本地 dst | 沿 DIM_3 顺序拼接 |
| TSCATTER | root | root 本地 src（大 tensor） | 每卡 buffer | 沿 DIM_3 切分下发，TGATHER 的逆 |

#### 4.1.2 核心流程

四条指令的 `*_IMPL` 共享同一个骨架：

```text
API 薄壳（pto_comm_inst.hpp）
  ├─ WaitAllEvents(args...)          // 先等用户传入的前置事件
  └─ 按 CollEngine 分发
       ├─ AIV（默认）→ XXX_IMPL(...)        // 本讲主线
       └─ CCU      → XXX_CCU_IMPL<engine>(...)  // A5 硬件引擎

XXX_IMPL 骨架（AIV 引擎）
  ├─ static_assert：dtype / layout 一致性检查
  ├─ 从 root 的 tensor 读 5 维 shape 与 stride
  ├─ totalRows = D0*D1*D2*D3
  ├─ 若 totalRows ≤ tileValidRow 且 D4 ≤ tileValidCol
  │     → Simple 路径：整块进一个 tile，一蹴而就
  ├─ 否则
  │     → 整除断言（静态 ValidRow/ValidCol 时 D3/D4 必须整除）
  │     → 2D 滑窗分块：外层三维显式迭代 + 行/列滑窗
  └─ 每个块内执行「TLOAD → (计算) → TSTORE」+ 事件闭环
```

单 tile 与 ping-pong 双 tile 是两个重载：传一个 staging tile 走朴素路径，传 ping/pong 两个 tile 走搬运与计算（或搬运与写回）重叠的路径。

#### 4.1.3 源码精读

**ParallelGroup 是零分配视图**。见 [include/pto/comm/comm_types.hpp:L24-L72](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L24-L72)——`tensors` 只是指向外部 `GlobalData` 数组的指针，设备侧不做任何动态分配（CCE 不支持 `std::vector` 之类的容器）；`operator[]` 带越界断言返回第 r 张卡的视图。注释里特别说明：数组里每个元素通常是「该组内 rank 的 GlobalTensor 视图」，由 host 侧经窗口机制映射得到。

配套的 `ParallelGroupTraits` 见 [include/pto/comm/comm_types.hpp:L75-L83](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L75-L83)，实现层用它从 `ParallelGroup<GlobalData>` 中萃取 `GlobalDataType`——这就是 `*_IMPL` 里 `using GlobalSrcData = typename ParallelGroupTraits<...>::GlobalDataType;` 的来源。

三个关键枚举：

- `ReduceOp`（Sum/Max/Min）：[include/pto/comm/comm_types.hpp:L107-L115](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L107-L115)，TREDUCE 的折叠算子。
- `CollEngine`（AIV/CCU）：[include/pto/comm/comm_types.hpp:L126-L135](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L126-L135)——AIV 是默认的 tile 组合路径，CCU 是 A5 硬件集合引擎。
- `CcuTriggerContext`：[include/pto/comm/comm_types.hpp:L166-L171](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L166-L171)，CCU 路径由 host 填好 CKE 门铃槽位地址与掩码传给 AIV。

**API 薄壳与 engine 分发**。以 TREDUCE 为例，见 [include/pto/comm/pto_comm_inst.hpp:L298-L336](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L298-L336)：模板参数 `CollEngine engine = CollEngine::AIV` 默认走 tile 路径，函数体先 `WaitAllEvents(args...)` 再调 `TREDUCE_IMPL`；若选 CCU，则编译期要求第一个可变参数是 `CcuTriggerContext`。注意有两个重载：`(acc, recv)` 双 tile 朴素版与 `(acc, ping, pong)` 三 tile 双缓冲版。TGATHER/TSCATTER/TBROADCAST 的声明结构完全同构，见 [include/pto/comm/pto_comm_inst.hpp:L153-L190](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L153-L190)（TGATHER）、[L200-L238](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L200-L238)（TSCATTER）、[L250-L287](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_inst.hpp#L250-L287)（TBROADCAST）。

**后端路由**。见 [include/pto/comm/pto_comm_instr_impl.hpp:L16-L71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_instr_impl.hpp#L16-L71)：`__CCE_AICORE__` 且非仿真时按 `PTO_NPU_ARCH_A2A3` / `PTO_NPU_ARCH_A5` 引入各自的实现头；`__CPU_SIM` 下引入 `pto/cpu/comm/` 下的仿真实现。注意**没有** `__COSTMODEL` 分支——CostModel 后端不提供通信指令，Kirin/A6 同样不可用（承接 u7-l1 的结论）。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 TREDUCE 的 CPU ST 用例，确认「kernel→IMPL→CPU 仿真」链路能通。

**操作步骤**：

1. 进入 `tests/` 目录，执行：
   ```bash
   python3 run_cpu.py -t treduce
   ```
   （`-t/--testcase` 参数见 [tests/run_cpu.py:L455](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L455)，用法示例见 [L444-L445](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L444-L445)。）
2. 打开 [tests/cpu/st/testcase/treduce/treduce_kernel.cpp:L26-L64](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treduce/treduce_kernel.cpp#L26-L64)，对照观察三处代码：
   - L46-L51：在栈上构造 `Global tensors[2]`（模拟两张卡的源 buffer），`ParallelGroup<Global> pg(tensors, 2, my_rank)`，root 是组内下标 0；
   - L57-L58：`TASSIGN` 给 acc/recv 两个 tile 手工摆放 UB 偏移（`0x0` 与 `0x10000`，Manual 模式开发者的排布责任）；
   - L61-L63：`if (my_rank == 0) TREDUCE(pg, outputG, accTile, recvTile, op);`——只有 root 调用集合指令。

**需要观察的现象**：脚本先跑 `gen_data.py` 生成 `input0.bin/input1.bin/golden.bin`，再编译并运行 gtest；`gen_data.py` 中 golden 就是 numpy 的 `np.add` / `np.maximum`（见 [tests/cpu/st/testcase/treduce/gen_data.py:L23-L28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treduce/gen_data.py#L23-L28)）。

**预期结果**：`TREDUCETest.case1`（Max）与 `case2`（Sum）两个用例全部 PASSED。具体输出**待本地验证**（本讲义编写环境未实际执行构建）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ParallelGroup` 用「指向外部数组的指针 + size」而不是内部持有数组？

**参考答案**：设备侧（CCE/AICORE）不支持 `std::vector` 等动态分配容器；`ParallelGroup` 只是零分配的轻量视图（头文件注释 L28-L30 明说），数组由 kernel 在栈上或静态构造，group 只负责包装指针与元数据。

**练习 2**：`parallelGroup.GetRootIdx()` 返回的 2 是什么意思？非 root 卡调用 `TREDUCE` 会怎样？

**参考答案**：rootIdx 是**组内下标**——组内第 2 张卡是 root，与全局卡号无关；所有卡的 kernel 必须传相同的 rootIdx。非 root 卡调用 TREDUCE 是未定义行为（ISA 文档明确），因为整套指令序列（远端读、折叠、写回）只站在 root 视角编写。

**练习 3**：`__COSTMODEL` 后端下能用 TREDUCE 吗？

**参考答案**：不能。[pto_comm_instr_impl.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/pto_comm_instr_impl.hpp#L16-L71) 只为 `__CCE_AICORE__`（分 a2a3/a5）和 `__CPU_SIM` 提供 IMPL，CostModel 没有通信分支。

---

### 4.2 TReduce：root 拉取式规约

#### 4.2.1 概念说明

TREDUCE 语义：root 把组内 N 张同形 tensor **逐元素折叠**成一份，写到 root 本地的 dst：

\[ \mathrm{dst}_{i,j} = \bigoplus_{r=0}^{N-1} \mathrm{src}^{(r)}_{i,j}, \qquad \oplus \in \{\text{Sum}, \text{Max}, \text{Min}\} \]

注意两点：

1. 这是 **reduce-to-root**，不是 AllReduce——结果只在 root 手里，其他卡拿不到。要做 AllReduce 需再补一次广播（见第 5 节综合实践）。
2. 实现是 **root 拉取式（pull）**：root 主动 `TLOAD` 每张卡的源数据，而不是各卡把数据推给 root。所以循环次数、带宽压力都集中在 root 一侧。

#### 4.2.2 核心流程

单 tile 朴素路径（`TreduceSimple`）：

```text
若 nranks == 1：TLOAD(root 源) → TSTORE(dst)，退化为本地拷贝
否则：
  TLOAD(accTile, parallelGroup[root])        # 先装 root 自己的数据当累加器初值
  事件：MTE2 → V（数据就绪）
  对每个远端卡 r（跳过 root）：
      TLOAD(recvTile, parallelGroup[r])      # 经窗口读远端
      事件：MTE2 → V（本块就绪）
      ReduceTiles(acc, recv, op)             # TADD/TMAX/TMIN，Vector 流水线
      事件：V → MTE2（recvTile 归还，下轮可复用）
  事件：V → MTE3（累加结果就绪）
  TSTORE(dst, acc)
  事件：MTE3 → MTE2（收尾）
```

数据超过一个 tile 时进入 **2D 滑窗分块**：外层三维（DIM_0/1/2）显式迭代，DIM_3 按 `tileValidRow`、DIM_4 按 `tileValidCol` 滑窗，每个窗口独立走一遍完整规约流水（块内 root 先装、逐远端折叠、写回）。

ping-pong 双缓冲版把「远端块的搬运」与「当前块的折叠」重叠：

```text
无 ping-pong： [TLOAD r0] → [Reduce r0] → [TLOAD r1] → [Reduce r1] → ...
有 ping-pong： [TLOAD r0] → [Reduce r0 | TLOAD r1] → [Reduce r1 | TLOAD r2] → ...
```

#### 4.2.3 源码精读

**规约算子就是普通计算指令**。见 [include/pto/comm/a2a3/TReduce.hpp:L27-L44](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L27-L44)——`ReduceTiles` 只是一个 switch，把 `ReduceOp::Sum/Max/Min` 映射到 `TADD/TMAX/TMIN`。这揭示了集合指令的本质：**跨卡通信 + 普通 Vector 指令的编排**，没有神秘的硬件集合原语（AIV 路径下）。

**远端卡号映射**。见 [include/pto/comm/a2a3/TReduce.hpp:L46-L49](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L46-L49)：`GetRemoteRank` 把「第 k 个远端」映射为真实组内下标——小于 rootIdx 的保持原值，否则加 1 跳过 root。

**朴素路径的事件编排**。见 [include/pto/comm/a2a3/TReduce.hpp:L54-L89](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L54-L89)：

- L59-L67：`nranks == 1` 的退化分支，`TLOAD → MTE2/MTE3 事件 → TSTORE` 纯本地拷贝；
- L69-L71：root 数据先入 acc，`set_flag(PIPE_MTE2, PIPE_V, EVENT_ID0)` + `wait_flag` 配对（u2-l3 的三元组规则）；
- L73-L82：远端循环。每轮 `TLOAD` 后用 **EVENT_ID1** 标记就绪、折叠后用 **EVENT_ID0** 让 V 把 recvTile 归还 MTE2——这样下一轮 `TLOAD` 复用同一 recvTile 时不会读到还没消费完的数据；
- L84-L88：最后 `V → MTE3` 事件交接后 `TSTORE`。

**IMPL 入口的分派与约束**。见 [include/pto/comm/a2a3/TReduce.hpp:L221-L283](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L221-L283)：

- L229-L232：`static_assert` 强制「组内 dtype == dst dtype == tile dtype」；
- L237-L238：运行期断言组大小与 rootIdx 范围；
- L240-L249：从 **root 的 tensor** 读 5 维 shape，`totalRows = D0*D1*D2*D3`，tile 有效区取自 acc tile；
- L258-L262：`totalRows <= chunkRows && gShape4 <= chunkCols` 时走 Simple 路径；
- L267-L278：否则若 tile 的 ValidRow/ValidCol 是**静态**的，断言 D3/D4 必须被整除；动态（`DYNAMIC`）tile 才支持非整除尾块。

**2D 滑窗与动态掩码**。分块策略的官方注释见 [include/pto/comm/a2a3/TReduce.hpp:L196-L219](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L196-L219)。`TreduceProcessChunkSingle` 里，动态 tile 在每块开始时写入运行期掩码，见 [include/pto/comm/a2a3/TReduce.hpp:L107-L114](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L107-L114)（`RowMaskInternal = currentRows`）；随后用 `SrcViewT/DstViewT` 在原始指针上加偏移构造**块视图**（[L116-L118](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L116-L118)、[L141-L142](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L141-L142)）——这正是 u2-l1「同一 GM 指针套多个视图」的典型应用。滑窗循环本体见 [L171-L193](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L171-L193)，`curRows/curCols` 按「剩余量与 tile 容量取小」计算（[L179](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L179)、[L181](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L181)）。

**ping-pong 版**。设计意图注释（含时间线对比）见 [include/pto/comm/a2a3/TReduce.hpp:L485-L503](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L485-L503)。`TreduceSimplePingPong` 见 [L290-L335](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L290-L335)：进入循环前先预取第一个远端块进 pingTile（L306-L307）；循环内**先发射下一个远端块的 TLOAD 再等当前块**（L317-L321），事件编号随奇偶轮换（`EVENT_ID1/EVENT_ID2`，L315-L316）——两套编号分别标记 ping 与 pong 的就绪，正是 u6-l2「按槽配对的双向事件」模式。三个 tile 的 IMPL 重载入口见 [L504-L568](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L504-L568)。

**CPU 仿真对照**。看 [include/pto/cpu/comm/TReduce.hpp:L121-L146](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/comm/TReduce.hpp#L121-L146)：同样的 TLOAD → 循环 TLOAD + ReduceTiles → TSTORE，但**一个 set_flag/wait_flag 都没有**——CPU 仿真单线程按序执行，事件全部是空桩（承接 u2-l3/u3-l4 的结论）。这印证：集合指令的功能正确性可在 CPU 验证，事件时序与真实带宽必须上真机。另外 CPU 版的 CCU 入口直接转发给 AIV 实现（[L474-L492](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/comm/TReduce.hpp#L474-L492)），而 A2/A3 真机版的 CCU 入口是一个「仅在被实例化时才报错」的静态断言桩（[include/pto/comm/a2a3/TReduce.hpp:L580-L588](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L580-L588)）——A2/A3 没有 CCU 硬件，真正的 CCU 实现在 A5 头文件中。

#### 4.2.4 代码实践

**实践目标**：给 TREDUCE 增加一个 `Min` 用例，体会「kernel 指令参数、host 测试、golden 脚本」三处必须同步修改的闭环。

**操作步骤**：

1. 修改 [tests/cpu/st/testcase/treduce/gen_data.py:L60-L62](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treduce/gen_data.py#L60-L62)：把 case2 的算子从 `np.add` 换成 `np.minimum`。
2. 修改 [tests/cpu/st/testcase/treduce/main.cpp:L98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treduce/main.cpp#L98)：把 `ReduceOp::Sum` 换成 `ReduceOp::Min`。
3. 重新运行：`cd tests && python3 run_cpu.py -t treduce`（gen_data 会自动重跑）。
4. 对照源码确认：`ReduceOp::Min` 在 [include/pto/comm/a2a3/TReduce.hpp:L37-L39](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L37-L39) 被映射到 `TMIN`。

**需要观察的现象**：只改第 2 步不改第 1 步时，golden 仍是「和」而输出是「最小值」，用例应失败；两处都改后恢复通过。

**预期结果**：case2 输出等于两个输入矩阵的逐元素最小值，测试 PASSED。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：8 卡 TREDUCE（root=0）的朴素路径中，root 总共发起几次远端 TLOAD？每次折叠用什么指令？

**参考答案**：7 次（N-1，跳过 root 自己，经 `GetRemoteRank` 映射到组内下标 1..7）；折叠用 TADD/TMAX/TMIN（由 `ReduceOp` 决定），跑在 Vector 流水线上。

**练习 2**：源 tensor 是 [1,1,1,130,256]，tile 是 `Tile<Vec, T, 64, 256, RowMajor, -1, -1>`（动态有效区）。会走哪条路径？若 tile 是静态 `ValidRow=64` 呢？

**参考答案**：totalRows=130 > 64，走 2D 滑窗分块，行方向分成 64+64+2 三块，动态掩码在第三块写入 `RowMaskInternal=2` 保护尾块；若 ValidRow 是静态 64，则 `130 % 64 != 0` 触发断言（[TReduce.hpp:L267-L272](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TReduce.hpp#L267-L272)），必须换动态 tile 或改 shape。

**练习 3**：ping-pong 版 TREDUCE 里为什么需要 `acc + ping + pong` 三个 tile，而朴素版只要两个？

**参考答案**：acc 贯穿整个块始终持有累加中间结果不能复用；朴素版 recv 一个 tile 串行地「装载-消费」；ping-pong 版要用两个接收 tile 轮换，才能在 Vector 消费当前块的同时让 MTE2 预取下一块，实现搬运与计算重叠。

---

### 4.3 TBroadCast：root 扇出式广播

#### 4.3.1 概念说明

TBROADCAST 语义：root 把本地一份 src 数据复制到组内**每一张卡**的 buffer（含 root 自己）。数学上就是：

\[ \mathrm{dst}^{(r)}_{i,j} = \mathrm{src}_{i,j}, \quad r = 0, 1, \ldots, N-1 \]

两个易错点：

1. **root 自己也会被写入**。`parallelGroup[rootIdx]` 也在写回循环里——`nranks == 1` 时它干脆退化为一次本地拷贝。
2. 与 TGATHER/TSCATTER 不同，广播**没有拼接/切分维度**，每份拷贝形状与 src 完全相同。

#### 4.3.2 核心流程

```text
若 nranks == 1：TLOAD(src) → TSTORE(root 的 buffer)，本地拷贝
若整块装得下：TLOAD(src) 一次 → 对 r = 0..N-1 依次 TSTORE(parallelGroup[r])
否则 2D 滑窗分块，每块：
    TLOAD 当前块 → 事件 MTE2→MTE3 → 对所有 rank TSTORE → 事件 MTE3→MTE2

ping-pong 版时间线：
  无重叠： [TLOAD c0] → [N×TSTORE c0] → [TLOAD c1] → [N×TSTORE c1] → ...
  有重叠： [TLOAD c0] → [N×TSTORE c0 | TLOAD c1] → [N×TSTORE c1 | TLOAD c2] → ...
```

注意广播是**纯搬运**（MTE2→MTE3），不经过 Vector 计算流水线，这和 TREDUCE（要过 V）不同。

#### 4.3.3 源码精读

**IMPL 入口**。见 [include/pto/comm/a2a3/TBroadCast.hpp:L172-L231](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L172-L231)：

- L179-L185：`static_assert` 除 dtype 外**还要求 src 与组内 tensor 的 layout 一致**（TREDUCE 没有这条布局断言，因为它的源/目的视图可以各自带布局）；
- L210-L216：`nranks == 1` 退化分支——`TLOAD` + `PtoSetWaitFlag<PIPE_MTE2, PIPE_MTE3>()`（set+wait 的便捷封装）+ `TSTORE`；
- L218-L226：单 tile 直通路径，一次 `TLOAD` 后循环对每张卡 `TSTORE`。

**分块搬运函数**。见 [include/pto/comm/a2a3/TBroadCast.hpp:L34-L51](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L34-L51)：`TbroadcastChunkTransfer` 是广播的最内层积木——`TLOAD(tile, srcView)` → `MTE2→MTE3` 事件 → 对每张卡用 `parallelGroup[r].data() + dstOffset` 构造目的视图依次 `TSTORE` → `MTE3→MTE2` 事件归还 tile。外层的 2D 滑窗循环（行/列滑窗 + 动态掩码）见 [L57-L82](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L57-L82)，结构与 TREDUCE 的分块完全同构。

**ping-pong 状态机**。广播的重叠对象是「上一块的 N 次 TSTORE」与「下一块的 TLOAD」，由于搬运跨越流水线两侧，实现用一个显式状态机记录「还有一块没写完」：`TbroadcastPingPongState` 见 [include/pto/comm/a2a3/TBroadCast.hpp:L26-L32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L26-L32)（`usePing/hasPending/pendingDstOffset/pendingRows/pendingCols`）。每块的处理见 [L234-L286](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L234-L286)：若上一块还挂着（`hasPending`），先 `wait` 上一块的 `MTE2→MTE3` 就绪事件、发射上一块的 N 次 `TSTORE`，同时 `TLOAD` 当前块（L260-L275）——读写分别打在不同 tile（ping/pong）与不同事件编号（`EVENT_ID0/EVENT_ID1`）上，互不干扰。循环结束后由 `TbroadcastPingPongEpilogue`（[L289-L313](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L289-L313)）把最后一块「排水」写完。整套时间线注释见 [L400-L413](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L400-L413)，ping-pong 版 IMPL 入口见 [L415-L474](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TBroadCast.hpp#L415-L474)。

#### 4.3.4 代码实践

**实践目标**：读懂 TBROADCAST 的 ST 用例如何「在一张卡上模拟 N 张卡」，并验证广播语义。

**操作步骤**：

1. 阅读 [tests/cpu/st/testcase/tbroadcast/tbroadcast_kernel.cpp:L21-L41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tbroadcast/tbroadcast_kernel.cpp#L21-L41)：L28-L32 把 `tensors[i] = GTf(out + i * sizePerCopy)`——N 张「卡」的 buffer 其实是同一个输出大 buffer 的第 i 段，root=0；L40 调用 `TBROADCAST(group, srcGlobal, tempTile)`。
2. 阅读 [tests/cpu/st/testcase/tbroadcast/gen_data.py:L23-L24](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tbroadcast/gen_data.py#L23-L24)：golden 用 `np.tile(input1, rank)` 生成——输入复制 N 份，正是广播的数学定义。
3. 运行：`cd tests && python3 run_cpu.py -t tbroadcast`。

**需要观察的现象**：输出 buffer 的 N 段内容完全相同，都等于输入。

**预期结果**：`TBroadCastTest` 各用例（float32×5 份、int32×3 份、int16×2 份、fp16×1 份等）全部 PASSED；KN=1 的用例退化为本地拷贝也通过。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CPU 仿真能把 N 张卡塞进一个连续 buffer？真机上这样写对吗？

**参考答案**：CPU 仿真没有真正的跨卡地址空间，「N 张卡」只是 N 个 GM 指针，落在同一 buffer 的不同段即可成立；真机上 `parallelGroup[r]` 必须是经 HCCL 窗口映射后的 r 号卡真实远端地址（u7-l2 的窗口机制），不能是本地偏移。

**练习 2**：TBROADCAST 的 ping-pong 为什么要用 `hasPending` 状态 + epilogue「排水」，而 TREDUCE 的 ping-pong 不用？

**参考答案**：TREDUCE 的重叠是「TLOAD 下一块 vs 折叠当前块」，折叠（Vector）与写回只在最后发生一次，循环天然收敛；TBROADCAST 的重叠是「TLOAD 下一块 vs N 次 TSTORE 上一块」，TSTORE 是延迟发射的（挂在 pending 状态上），循环结束时必有最后一块还没写，需要 epilogue 补一发。

**练习 3**：广播 8 张卡、单块需要 4 KB，一个 chunk 内 MTE3 流水线上会排几条 TSTORE？

**参考答案**：8 条——对组内每张卡各一条 `TSTORE`（root 自己也在列表里），它们共用同一 staging tile、由 `MTE2→MTE3` 事件统一保护。

---

### 4.4 TGather/TScatter：沿 DIM_3 拼接与切分

#### 4.4.1 概念说明

TGATHER 与 TSCATTER 互为逆操作，切分维度固定是 **DIM_3（行）**：

- **TGATHER**：每张卡贡献形状 \((D_0, D_1, D_2, H, W)\) 的数据，root 本地的 dst 形状为 \((D_0, D_1, D_2, N \cdot H, W)\)，第 r 张卡的数据落在行 \([rH, (r+1)H)\)：

\[ \mathrm{dst}^{(rH+i)}_{j} = \mathrm{src}^{(r)}_{i,j}, \quad i \in [0, H),\ r \in [0, N) \]

- **TSCATTER**：root 本地 src 形状 \((D_0, D_1, D_2, N \cdot H, W)\)，第 r 张卡收到行 \([rH, (r+1)H)\) 那一段。

两者都要求组内所有卡的 tensor 同形（gather 时是各卡源，scatter 时是各卡目的）。

#### 4.4.2 核心流程

TGATHER 朴素路径（每卡数据装得进一个 tile）：

```text
对 r = 0..N-1：
    TLOAD(stagingTile, parallelGroup[r])        # 读第 r 张卡
    事件 MTE2 → MTE3
    dstOffset = r * perRankRows * dstStride3    # 目的落点：行偏移
    TSTORE(dstView + dstOffset, stagingTile)    # 写进 root 本地 dst 的对应行段
    事件 MTE3 → MTE2
```

TSCATTER 反过来：

```text
对 r = 0..N-1：
    srcOffset = r * perRankRows * srcStride3    # 源起点：大 tensor 的第 r 段
    TLOAD(stagingTile, srcView + srcOffset)
    事件 MTE2 → MTE3
    TSTORE(parallelGroup[r], stagingTile)       # 写到第 r 张卡
    事件 MTE3 → MTE2
```

数据超 tile 时同样走 2D 滑窗；ping-pong 版同样是「TLOAD 下一块 vs TSTORE 上一块」的重叠，结构复用 TBROADCAST 的状态机。

#### 4.4.3 源码精读

**TGATHER 朴素路径**。见 [include/pto/comm/a2a3/TGather.hpp:L35-L63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L35-L63)：关键一行是 [L57](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L57)——`dstOffset = r * perRankRows * dstStride3`，即「第 r 张卡写到目的的第 r 个行段」，拼接语义的全部秘密就在这个偏移乘法。语义与约束的官方注释见 [L148-L168](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L148-L168)，IMPL 入口（含 layout 一致断言、单 tile 判定、整除断言）见 [L170-L234](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L170-L234)。

**TSCATTER 朴素路径**。见 [include/pto/comm/a2a3/TScatter.hpp:L36-L63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L36-L63)：[L54](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L54) 的 `srcOffset = r * perRankRows * srcStride3` 从**大 tensor** 里切出第 r 段，`TSTORE(parallelGroup[r], ...)` 写到第 r 张卡。一个值得注意的细节：TSCATTER 的 shape 取自 `parallelGroup[0]`（即**目的**卡 tensor 的每卡形状 H），见 [L186-L192](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L186-L192)；而 TGATHER 取自 `parallelGroup[0]`（各卡**源**的形状）——两者参考对象不同但都是「每卡形状」。语义注释见 [L145-L165](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L145-L165)，IMPL 入口见 [L167-L231](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L167-L231)。

**ping-pong 状态机的差异**。TGATHER 的状态见 [include/pto/comm/a2a3/TGather.hpp:L26-L32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L26-L32)；TSCATTER 的状态多一个 `pendingRank` 字段，见 [include/pto/comm/a2a3/TScatter.hpp:L26-L33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L26-L33)。原因：gather 的 pending 写回永远落在同一个本地 dst，而 scatter 的 pending 写回目标是**上一张卡**的 buffer（[L266](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L266) 用 `parallelGroup[state.pendingRank]` 取地址），必须把卡号也记进状态。两个 ping-pong IMPL 入口分别见 [TGather.hpp:L403-L466](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L403-L466) 与 [TScatter.hpp:L379-L442](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L379-L442)。

**CPU ST 用例**。`tests/cpu/st/testcase/` 下同时存在 tgather、tscatter（以及 tpairreducesum 等相关用例），目录结构与 treduce 相同（kernel + main + gen_data + CMakeLists 四件套）。

#### 4.4.4 代码实践

**实践目标**：手工推导一个 2 卡 TGATHER 的数据布局，再用用例验证。

**操作步骤**：

1. 假设每卡源是 [1,1,1,2,4]（2 行 4 列），卡 0 数据为 `a b c d / e f g h`，卡 1 为 `i j k l / m n o p`。写出 gather 后 root 本地 dst（形状 [1,1,1,4,4]）的 4×4 内容。
2. 阅读源码核对：落点由 [include/pto/comm/a2a3/TGather.hpp:L57](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TGather.hpp#L57) 的 `dstOffset = r * perRankRows * dstStride3` 决定（`perRankRows=2, dstStride3=4`，卡 1 的落点偏移为 `1*2*4=8` 个元素）。
3. 运行 CPU 用例：`cd tests && python3 run_cpu.py -t tgather`，再跑 `-t tscatter`。
4. 思考验证：把第 1 步的手工结果与 `gen_data.py` 的 golden 生成逻辑对照（gather 的 golden 即按行拼接两个输入）。

**需要观察的现象**：tgather 输出前 2 行来自卡 0、后 2 行来自卡 1；tscatter 输出反之被拆到两个 buffer。

**预期结果**：两个用例全部 PASSED。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：4 张卡做 TGATHER，每卡 [1,1,1,8,16]，DIM_3 的 stride 是 16。卡 2 的数据写到 dst 的哪个元素偏移？

**参考答案**：`dstOffset = 2 * 8 * 16 = 256`，即 dst 的第 16 行（行 16..23 归卡 2）。

**练习 2**：TSCATTER 与 TBROADCAST 都是「root 把数据发出去」，本质区别是什么？

**参考答案**：TBROADCAST 给每张卡发**同一份**完整数据（无切分语义）；TSCATTER 把大 tensor **沿 DIM_3 切成 N 段**，每张卡只拿到属于自己的那一段。

**练习 3**：想把切分维度从「行」换成「列」（沿 DIM_4 scatter），直接调 TSCATTER 行吗？

**参考答案**：不行。TSCATTER/TGATHER 的拼接维度硬编码为 DIM_3（[TScatter.hpp:L145-L153](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a2a3/TScatter.hpp#L145-L153) 注释明确）。想按列切分，常见做法是先在 host 侧或用 TTRANS/TRESHAPE 调整布局把「列」变成「行」维度，或者退回 TGET/TPUT 逐卡手动搬运。

---

## 5. 综合实践：设计 8 卡 AllReduce 方案

**任务**：8 张卡各持有一个 [1,1,1,128,128] 的 fp16 tensor，要求结束后每张卡都拿到 8 份数据的逐元素和（AllReduce-Sum）。给出指令选型、指令序列与同步点。

### 方案对比

**方案 A（推荐）：TREDUCE + TBROADCAST 两段组合**。

PTO 没有原生 AllReduce，但语义可精确分解为「规约到 root」+「root 扇出」：

```text`
===== 阶段 0：数据就绪同步（每张卡都参与）=====
for r in 1..7: 卡 r 执行 TNOTIFY(root 的就绪信号, 1, NotifyOp::AtomicAdd)
root 执行 TWAIT(就绪信号, 7, WaitCmp::EQ)          # 同步点 ①：root 确认 7 张卡数据就绪

===== 阶段 1：规约到 root（只有 root 执行）=====
root:
    构造 ParallelGroup(tensors[8], nranks=8, rootIdx=0)   # tensors[r] 经窗口映射
    TACC/TUB tile：accTile + pingTile + pongTile（ping-pong 版三 tile）
    TREDUCE(pg, dstG, accTile, pingTile, pongTile, ReduceOp::Sum)
    # 内部：TLOAD(root) → 预取远端 → [Reduce i | TLOAD i+1] ×7 → TSTORE(dst)

===== 阶段 2：结果扇出（只有 root 执行）=====
root:
    TBROADCAST(pg, dstG, bPingTile, bPongTile)
    # 内部：每块 TLOAD 一次 → 对 8 张卡各 TSTORE 一份（含 root 自己）

===== 阶段 3：结果就绪同步 ==========
for r in 1..7: root 执行 TNOTIFY(卡 r 的完成信号, epoch, NotifyOp::Set)
非 root: TWAIT(本卡完成信号, epoch, WaitCmp::EQ)      # 同步点 ②：各卡确认结果可读
```

同步点小结（这正是本讲要求的「说明同步点」）：

| 同步点 | 谁等谁 | 机制 | 为什么必须有 |
|--------|--------|------|--------------|
| ① 数据就绪 | root 等 7 张卡 | TNOTIFY(AtomicAdd) + TWAIT(EQ 7) | TREDUCE 是 pull 模型，root 读远端前必须保证源数据已写好 |
| root 内部 | 流水线之间 | set_flag/wait_flag（指令内部自动完成） | IMPL 已内建，开发者无需干预 |
| ② 结果就绪 | 各卡等 root | TNOTIFY(Set) + TWAIT | TBROADCAST 写的是各卡 buffer，写完前各卡不能读；事件系统只管卡内流水线，跨卡可见性必须走信号（u7-l2 结论） |

**方案 B：TPUT AtomicAdd 星型累加**。7 张卡各自 `TPUT<AtomicType::AtomicAdd>` 把数据加到 root 的 dst，再栅栏 + TBROADCAST。问题：浮点原子加的次序不确定导致结果逐位不可复现（数值一致性差）；且 7 个原子写集中在同一地址域，硬件串行化后未必更快。只有在整数累加或允许误差时才考虑。

**方案 C：TGET/TPUT 手写环形/树形协议**。控制力最强（可做 reduce-scatter + allgather 的层次化流水，避免单 root 瓶颈），但同步协议全部要自己用 TNOTIFY/TWAIT 搭，工程量大——这正是 u7-l5 gemm_ar 融合算子做的事情。

**选型结论**：数据量中等、追求简洁正确时选方案 A——两条指令 + 两个信号同步点即可；追求极限带宽（大模型梯度同步）时才值得投入方案 C。方案 A 的固有瓶颈也要清楚：所有远端读都压在 root 一张卡的 UB staging 通路上（u7-l2 讲过 UB 封顶约 4 GB/s 量级），8 卡大 tensor 时 root 是唯一带宽瓶颈。

### 动手验证（选做）

在 CPU 仿真下搭一个方案 A 的最小验证：参考 [tests/cpu/st/testcase/treduce/treduce_kernel.cpp:L46-L63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treduce/treduce_kernel.cpp#L46-L63) 的写法构造 8 个 tensor 的 `ParallelGroup`，先 `TREDUCE(..., ReduceOp::Sum)`，再以 root 的 dst 为 src 调 `TBROADCAST`，golden 用 `np.add` 逐卡累加再 `np.tile` 8 份。注意 CPU 仿真下 TWAIT 是带超时的空转桩（u7-l2），信号同步的正确性仍需真机验证。**待本地验证**。

## 6. 本讲小结

- PTO 集合通信四条指令（TGATHER/TSCATTER/TBROADCAST/TREDUCE）共用 `ParallelGroup` 描述参与方，采用**root 单方执行**模型：只有 root 跑指令序列，非 root 只保证 buffer 就绪，非 root 调用是未定义行为。
- A2/A3 的 `CollEngine::AIV` 路径下，集合指令没有专用硬件原语，本质是「TLOAD（经窗口读远端）+ 普通计算指令（TADD/TMAX/TMIN）+ TSTORE（写远端/本地）+ 事件闭环」的编排；A5 才有 CCU 硬件引擎路径。
- 四条指令的数据关系：TREDUCE 逐元素折叠到 root、TBROADCAST 扇出 N 份相同拷贝（纯搬运不过 Vector）、TGATHER/TSCATTER 沿 DIM_3 拼接/切分（落点偏移 `r * perRankRows * stride3`）。
- 数据超过单个 UB tile 时自动 2D 滑窗分块：外层三维显式迭代 + 行/列滑窗 + 动态掩码保护尾块；静态 ValidRow/ValidCol 要求 D3/D4 整除。
- 双缓冲重载用 ping/pong 两个 tile 重叠「搬运下一块」与「消费当前块」；TBROADCAST/TGATHER/TSCATTER 因 TSTORE 延迟发射需要 `hasPending` 状态机与 epilogue 排水，TSCATTER 还要额外记录 `pendingRank`。
- 集合指令的功能正确性可在 CPU 仿真验证（事件全为空桩），事件时序、跨卡可见性与带宽必须上真机；CostModel 后端不提供通信指令。

## 7. 下一步学习建议

1. **u7-l4（异步通信与通信引擎）**：TGET_ASYNC/TPUT_ASYNC 走 SDMA/URMA DMA 引擎，绕开 UB staging，是消除本讲 root 带宽瓶颈的钥匙；对照 `tget_bandwidth` 示例理解同步与异步的数据通路差异。
2. **u7-l5（gemm_ar 计算通信融合）**：看真实工程如何把 GEMM 计算与 AllReduce 通信在一条流水线里重叠，其中就用到了本讲的规约+广播组合思想。
3. 想深入 A5 CCU 硬件引擎的读者，可阅读 [include/pto/comm/a5/TReduce.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/a5/TReduce.hpp) 与 `CcuTriggerContext`/`CcuInputSource` 的定义（[include/pto/comm/comm_types.hpp:L137-L171](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/comm/comm_types.hpp#L137-L171)），对比 AIV 组合路径与硬件引擎路径的取舍。
4. 文档侧建议通读 [docs/isa/comm/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/comm/README.md) 与 TREDUCE/TGATHER/TSCATTER/TBROADCAST 各自的 md（含中文版），重点看 Constraints 一节与本讲源码断言的对应关系。
