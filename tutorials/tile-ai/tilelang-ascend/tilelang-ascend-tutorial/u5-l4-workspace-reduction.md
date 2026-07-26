# Workspace 消除

## 1. 本讲目标

本讲解决一个在昇腾 NPU 上写算子时几乎一定会遇到的问题：**Cube 核算出来的结果（在 L0C 里）要交给 Vector 核继续处理（在 UB 里），这条「跨核」数据通路该怎么写？**

学完本讲，你应当能够：

1. 说清楚为什么 `L0C → UB`、`UB → L1` 这两类搬运在物理上必须经过 Global Memory（GM）两阶段中转，以及它带来的开销。
2. 理解 `AscendWorkspaceReduction` pass 如何自动把「一条 `T.copy`」改写成「写 workspace + 延迟回拷」两阶段，并自动分配 GM workspace。
3. 区分两套 workspace 机制：用户用 `workspace_idx` 显式声明的 workspace，与编译器通过 `auto_gm_indices` 自动分配的 workspace，理解「延迟回拷（deferred copy-back）」的含义。

本讲是 u5-l1（Cube/Vector 分离与 CombineCV）的延续：CV 分离让 Cube 和 Vector 各自跑在同一份 `.so` 的两段代码里，但二者之间交换数据仍需一条物理通路——这条通路正是本讲的主角。

## 2. 前置知识

在进入本讲前，请确保你已经理解下面几个来自前置讲义的概念：

- **片上存储层级与 scope**（u3-l1）：`shared.l1` 属 Cube 核，`shared.ub` 属 Vector 核，`wmma.accumulator`（L0C）属 Cube 核的寄存器级累加器。
- **T.copy 按 scope 派发**（u3-l2）：前端只声明「从哪块 buffer 搬到哪块 buffer」，走哪条 DMA 指令完全由 `src.scope()` 与 `dst.scope()` 决定。
- **Cube/Vector 分离**（u5-l1）：AIC（Cube）与 AIV（Vector）两核**并行**但**不能直连**，必须经 GM/L2 中转交换数据；编译器靠 `resource_scope` 属性把语句切成两段。
- **vid 消除**（u5-l3）：`threads=2` 时 UB 沿第 0 维对半切给两个 Vector 子核，workspace 则存放**完整**数据。

一句话复习：**Cube 写、Vector 读，中间隔着一层 GM**——这就是本讲要自动化的东西。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/transform/ascend_workspace_reduction.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc) | 本讲核心：`AscendWorkspaceReduction` pass 的全部 C++ 实现。 |
| [src/op/ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc) | `AscendCopy::Lower`：`T.copy` 在 lowering 时按 scope 生成 `copy_l0c_to_ub` / `copy_ub_to_l1` 模板调用，并打上 `virtual_channel` 标记。 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | 编译 pass 流水线，`AscendWorkspaceReduction` 被排在 `LowerAndLegalize` 阶段。 |
| [tilelang/jit/adapter/cython/cython_wrapper.pyx](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx) | 运行时按 `auto_gm_idx` 自动为编译器分配的 workspace 申请 torch 张量。 |
| [src/tl_templates/ascend/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) | 模板库：`copy_l0c_to_gm` / `copy_ub_to_gm` 等真实 AscendC 搬运原语。 |
| [examples/developer_mode/flash_attn_bshd_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py) | 实战样本：FlashAttention 中多处 `T.copy(l0c, ub)` 触发 workspace 消除。 |
| [docs/tutorials/workspace_reduction_tutorial.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/workspace_reduction_tutorial.md) | 官方教程：设计目标、用法与限制。 |
| [docs/tutorials/automatic_workspace_allocation.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/automatic_workspace_allocation.md) | 官方教程：`workspace_idx` 自动分配特性。 |

## 4. 核心概念与源码讲解

### 4.1 跨核搬运问题与 copy_l0c_to_ub / copy_ub_to_l1 触发点

#### 4.1.1 概念说明

在昇腾 AI Core 上，Cube 核的矩阵乘结果落在 **L0C**（`wmma.accumulator`），而 Vector 核的逐元素、reduce 计算都发生在 **UB**（`shared.ub`）。这两块存储分属两颗不同的核，**硬件上没有直接通路**。

因此，当一份算子逻辑是「Cube 算 QK 得到 attention score，Vector 做 softmax」时，数据必须走这样一条物理路径：

```
L0C (Cube) ──► GM (Global Memory) ──► UB (Vector)
```

这就是**两阶段 GM 中转**：先从片上写到片外 GM，再从 GM 读回另一块片上存储。同理，反向的 `UB → L1`（Vector 算完要喂回 Cube 做下一次矩阵乘）也要走 `UB → GM → L1`。

官方文档把这种「手写两阶段拷贝」的代价讲得很直白：用户必须自己管理 workspace 缓冲、计算偏移、声明生命周期，前端脚本 writing difficulty 大幅上升，可维护性下降。Workspace 消除的设计目标，就是**把这条两阶段通路完全自动化**，让用户只写一条 `T.copy`。

#### 4.1.2 核心流程

那么，编译器怎么知道「这条 `T.copy` 需要跨核」？答案是 **scope 派发**（u3-l2 已讲）。在 lowering 阶段，`AscendCopy::Lower` 检查 `src.scope()` 与 `dst.scope()`，当发现源是 L0C、目标是 UB（或源是 UB、目标是 L1）时，就生成专门的模板调用，并打上一个 `virtual_channel = true` 标记，表示「这是一条虚拟的跨核搬运，物理上还没有真正实现」。

```
T.copy(acc_s_l0c, acc_s_ub)
   │  src.scope() = "wmma.accumulator" (L0C, Cube)
   │  dst.scope() = "shared.ub"        (UB,   Vector)
   ▼
AscendCopy::Lower 生成:  copy_l0c_to_ub<...>(src, dst, src_N, src_M, dst_M, dst_N)
                         并置 config.virtual_channel = true
```

随后，`AscendWorkspaceReduction` pass 会识别这两类带 `virtual_channel` 标记的搬运语句（`copy_l0c_to_ub` 与 `copy_ub_to_l1`），把它们改写成真实的两阶段 GM 拷贝。改写规则如下表（来自官方编程指南）：

| 原始搬运（虚拟） | 第一阶段（写 workspace） | 第二阶段（从 workspace 读） |
| --- | --- | --- |
| `copy_ub_to_l1` | `copy_ub_to_gm` | `copy_gm_to_l1` |
| `copy_l0c_to_ub` | `copy_l0c_to_gm` | `copy_gm_to_ub` |

#### 4.1.3 源码精读

**触发点的生成** 在 `src/op/ascend.cc` 的 `AscendCopy::Lower` 中。当目标是 L1、源是 UB，或源是 L0C、目标是 UB 时，分别生成两条模板调用，并把 `config.virtual_channel` 置真：

[src/op/ascend.cc:287-304](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L287-L304) —— 当 `dst.scope() == "shared.l1"`（UB→L1）生成 `copy_ub_to_l1`；当 `src.scope() == "wmma.accumulator"`（L0C→UB）生成 `copy_l0c_to_ub`，两者都置 `config.virtual_channel = true`。

`virtual_channel` 标记后续会驱动 lowering 把源/目标的运行时尺寸（`src_N`、`src_M`、`dst_M`、`dst_N`）作为额外参数追加到这条 call 里，供 workspace 消除 pass 读取：

[src/op/ascend.cc:579-583](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L579-L583) —— `virtual_channel` 分支追加 `src_N`、`src_M` 等运行时维度参数。

**真实搬运模板** 在模板库里，`copy_l0c_to_gm` 与 `copy_ub_to_gm` 就是改写后的目标：

[src/tl_templates/ascend/common.h:171-173](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L171-L173) —— `copy_l0c_to_gm`：把 L0C LocalTensor 写到 GM GlobalTensor。

[src/tl_templates/ascend/common.h:251-255](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L251-L255) —— `copy_ub_to_gm`：把 UB LocalTensor 写到 GM。

**官方文档对「两阶段开销」的描述** 直接点明了设计动机：

[docs/tutorials/workspace_reduction_tutorial.md:5-11](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/workspace_reduction_tutorial.md#L5-L11) —— Cube 与 Vector 不能直连，传统方式需 Cube 写 GM、Vector 读 GM，引入编程复杂度。

#### 4.1.4 代码实践

**实践目标**：在真实算子里找到一条 `copy_l0c_to_ub`，确认它确实是「跨核搬运」的触发点。

**操作步骤**：

1. 打开 [examples/developer_mode/flash_attn_bshd_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py)。
2. 定位到 `T.copy(acc_s_l0c, acc_s_ub_)`：

   [examples/developer_mode/flash_attn_bshd_developer.py:81-82](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py#L81-L82) —— `T.gemm_v0` 把 QK 结果写到 `acc_s_l0c`（L0C，Cube），紧接着 `T.copy(acc_s_l0c, acc_s_ub_)` 把它搬给 UB（Vector），这就是一处典型的跨核搬运。

3. 在该文件目录下，给脚本末尾加上一行打印（**示例代码**，非项目原有内容）：

   ```python
   print(func.get_kernel_source())
   ```

4. 在装有昇腾环境的机器上运行 `python flash_attn_bshd_developer.py`（若无 NPU 环境，则跳过运行，标注「待本地验证」）。

**需要观察的现象**：打印出的 Ascend C 源码里，`copy_l0c_to_ub` 这条模板调用应当**不存在**于最终设备函数里——它已经被改写。你会看到它的位置被 `copy_l0c_to_gm`（写 workspace）与稍后的 `copy_gm_to_ub`（从 workspace 读回 UB）取代。

**预期结果**：源码里出现形如 `copy_l0c_to_gm<...>(workspace_1_handle[...], acc_s_l0c, ...)` 与 `copy_gm_to_ub<...>(acc_s_ub_, workspace_1_handle[...], ...)` 的两条语句。若环境不具备，记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `copy_gm_to_ub`（GM→UB）和 `copy_l1_to_l0a`（L1→L0A）**不需要** workspace 消除，而 `copy_l0c_to_ub` 需要？

**参考答案**：`copy_gm_to_ub` 的源 GM 与目标 UB 都在「能被同一侧访问」的范围内（GM 是全局可见的，UB 属 Vector，Vector 直接从 GM 读 UB 是硬件支持的单阶段 DMA）；`copy_l1_to_l0a` 源和目标都属 Cube 核内部。只有「源属 Cube、目标属 Vector」（或反之）的搬运才物理上跨核，必须经 GM 中转，才需要 workspace 消除。

**练习 2**：在 `AscendCopy::Lower` 里，`copy_l0c_to_ub` 与 `copy_ub_to_l1` 都把 `config.virtual_channel` 设为 `true`。这个标记对后续 pass 意味着什么？

**参考答案**：它表示「这条搬运在物理上还没真正落地，只是一条虚拟的跨核意图」。它会驱动 lowering 追加运行时尺寸参数，并提示 `AscendWorkspaceReduction` pass：这条语句需要被改写成两阶段 GM 拷贝。

---

### 4.2 AscendWorkspaceReduction pass：自动两阶段转换

#### 4.2.1 概念说明

`AscendWorkspaceReduction`（注册名 `tl.AscendWorkspaceReduction`）是本讲的主角。它做三件事：

1. **收集**：扫描整个 kernel，找到所有 `copy_l0c_to_ub` / `copy_ub_to_l1` 语句，为每一条建立一个 `WorkspaceInfo`（记录 dtype、形状、对应的源/目标 buffer 名）。
2. **分配**：为每条跨核搬运分配一块 GM workspace buffer，形状是 `[total_core_nums, M, N]`——每个核拥有独立的 workspace 区域，靠 `cid` 偏移隔离。
3. **改写**：把原来的单条搬运原地改写成「第一阶段：写 workspace」；并在**后续真正消费该数据的语句之前**，插入「第二阶段：从 workspace 读回」。

这就是「workspace 消除」名字的由来：它**消除的是用户手写 workspace 的负担**，而非消除 workspace 本身——workspace 依然存在，只是改由编译器自动分配和管理。

> 名字辨析：pass 名里的「Reduction」是「减少 / 消除（用户负担）」之意，与 `T.reduce_sum` 里的 reduce（收缩）无关。

#### 4.2.2 核心流程

pass 的执行分两遍遍历 IR：

**第一遍（CopyInfoCollector，只读访问）** —— 收集信息：

```
对每条 Evaluate(Call) 语句:
  若是 copy_l0c_to_ub / copy_ub_to_l1:
    1. 解析模板参数，得到 dtype 与 [M, N]
    2. WorkspaceInfoCollector:
       - workspace_name = "workspace_<序号>"
       - offset  = cid * (M * N)          # 多核隔离
       - extent  = total_core_nums * M*N - offset
       - decl_buffer([total_core_nums, M, N], dtype, "global")
    3. 记录 src→workspace、dst→workspace、dst→scope 的映射
    4. 记录 dst 的 access_ptr（供第二阶段回拷用）
同时从 AttrStmt(thread_extent) 抓取 cid_var / vid_var / vector_cnt
```

**第二遍（AscendWorkspaceReductionPass，改写）** —— 重写语句：

```
对每条 Evaluate(Call) 语句:
  (A) 若是 copy_l0c_to_ub / copy_ub_to_l1（第一阶段改写）:
      把函数名 copy_l0c_to_ub → copy_l0c_to_gm
      把 dst access_ptr 替换为 workspace access_ptr（offset = cid*M*N [+ vid 偏移]）
  (B) 否则，检查该语句是否「读」了一个已跟踪的 dst buffer（延迟回拷检测）:
      若是，在该语句之前插入 copy_gm_to_ub / copy_gm_to_l1（第二阶段回拷）
最后：把所有 workspace buffer 追加为 PrimFunc 新参数，
     并用 auto_gm_indices 属性记录它们在参数列表中的下标。
```

关键是 **(B) 的「延迟回拷」**：第二阶段并不紧跟在第一阶段之后插入，而是推迟到**真正有人读这块 UB/L1 buffer 的那条语句之前**。这样可以减少搬运与计算的交错，避免过早把数据搬回片上又闲置。延迟回拷的检测靠 `rw_mask`：只有当某条语句以「读」（`rw_mask & 1`）方式访问被跟踪的 dst buffer 时，才在它前面插回拷。

**多核与双向量核的偏移** 是 workspace 寻址的核心。每个核的 workspace 区域起点为：

\[
\text{offset} = \text{cid} \times (M \times N)
\]

当 `threads=2`（双向量核，u5-l3）且 workspace 的对侧是 UB 时，还要叠加 vid 偏移（每个 vid 读自己的那一半）：

\[
\text{offset} = \text{cid} \times (M \times N) \;+\; \frac{\text{vid} \times (M \times N)}{\text{vector\_cnt}}
\]

注意 workspace 存的是**完整**的 \(M \times N\) 数据（由 Cube 一次写满），而 UB 已被 VidReduction 对半切，所以每个 vid 只读 \(M \times N / 2\)。

#### 4.2.3 源码精读

**pass 在流水线中的位置** —— 它排在 `LowerAndLegalize` 阶段，紧跟在 `LowerTileOp` 与 `AscendTailMaskPropagation` 之后、`LegalizeVectorizedLoop` 之前：

[tilelang/engine/phase.py:78-82](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L78-L82) —— 注释 `# Erase manual workspace allocations for virtual CV copy in Ascend` 一行即本 pass，处在 lowering 主体内，此时 CV 尚未分离（CombineCV 在下一个阶段 `OptimizeForTarget` 才执行）。

**识别目标搬运语句** —— `CopyInfoCollector` 用两张表锁定要处理的搬运：

[src/transform/ascend_workspace_reduction.cc:114-118](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L114-L118) —— `target_copy_stmts_ = {"copy_ub_to_l1", "copy_l0c_to_ub"}`，并建立搬运名→目标 scope（L1 或 UB）的映射 `scope_table_`。

**workspace 分配（以 L0C→UB 为例）** —— `WorkspaceInfoCollector` 的 `L0cToUb` 分支计算 offset、extent，并 `decl_buffer` 一块 global buffer：

[src/transform/ascend_workspace_reduction.cc:403-418](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L403-L418) —— `offset = cid_var * per_block_ele_nums`，`extent = total_core_nums * per_block_ele_nums - offset`，即每个核独占 \(M \times N\) 元素的连续区段。

[src/transform/ascend_workspace_reduction.cc:421-438](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L421-L438) —— 把 `[total_core_nums, M, N]` 作为 real_shapes，`decl_buffer(..., "global")` 声明 GM workspace。

**改写表** —— 第一阶段原地改名 + 换 dst 的核心是一张替换表：

[src/transform/ascend_workspace_reduction.cc:1003-1004](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L1003-L1004) —— `{{"copy_ub_to_l1", "copy_ub_to_gm"}, {"copy_l0c_to_ub", "copy_l0c_to_gm"}}`：直接把虚拟搬运名替换成「写 GM」的第一阶段名。

**workspace access_ptr 的偏移构造** —— `CreateWorkspaceAccessPtr` 同时处理 cid 基址与 vid 偏移：

[src/transform/ascend_workspace_reduction.cc:1192-1205](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L1192-L1205) —— 当对侧是 UB 且 `vector_cnt > 1`（threads=2）时，叠加 `vid * per_block_ele_nums / vector_cnt` 偏移，让每个 vid 只读自己那一半。

**第一阶段（原地改写）与第二阶段（延迟回拷插入）** 都在同一个 `VisitStmt_(EvaluateNode)` 里：

[src/transform/ascend_workspace_reduction.cc:1314-1402](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L1314-L1402) —— 若是目标搬运，执行第一阶段改名换 dst（`copy_l0c_to_ub` → `copy_l0c_to_gm`，dst 换成 workspace）。

[src/transform/ascend_workspace_reduction.cc:1404-1447](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L1404-L1447) —— 否则用 `IsTargetAccessNode` 检测当前语句是否读了某个被跟踪的 dst buffer；若是，在原语句之前（`SeqStmt`）插入 `copy_gm_to_ub` / `copy_gm_to_l1` 第二阶段回拷。这就是「延迟回拷」。

**延迟回拷的判定** —— `IsTargetAccessNode` 靠 `rw_mask` 区分读写，只对「读」触发：

[src/transform/ascend_workspace_reduction.cc:1151-1162](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L1151-L1162) —— 注释明言「Only consider buffer as a "consumer" if rw_mask has read bit set. Skip write-only access since no copy-back needed before a write」，即只在真正消费数据前插回拷，纯写访问不触发。

**workspace 参数与 auto_gm_indices 属性** —— pass 末尾把 workspace buffer 追加为 PrimFunc 新参数，并记录下标：

[src/transform/ascend_workspace_reduction.cc:932-950](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L932-L950) —— 为每个 workspace 创建 `workspace_N_handle` 参数加入 `buffer_map` 与 `params`，把下标收集进 `auto_gm_idx`，最后用 `WithAttr(..., "auto_gm_indices", auto_gm_idx)` 贴到 PrimFunc 上——这就是运行时自动分配 workspace 的契约（见 4.3）。

**pass 注册** —— 暴露为 `tl.transform.AscendWorkspaceReduction`，并按 `target.model`（ascendc/pto）与 `npu_platform`（A3/A5）分流：

[src/transform/ascend_workspace_reduction.cc:1455-1478](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L1455-L1478) —— `AscendWorkspaceReduction()` 读 target 与 platform，对 PTO 后端走 pipe 路线（`pto_use_pipe`），对 AscendC 走 GM 两阶段路线。

**官方文档的转换表** —— 编程指南给出了与源码一致的两阶段映射：

[docs/TileLang-Ascend Programming Guide.md:1254-1266](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L1254-L1266) —— 原始 `copy_ub_to_l1`/`copy_l0c_to_ub` → 第一阶段写 GM、第二阶段从 GM 读，并列举多核 `cid` 偏移、threads=2 的 vid 分割、延迟回拷、skip UB 四项关键特性。

#### 4.2.4 代码实践

**实践目标**：写一个最小的「Cube 产出 L0C、Vector 消费」kernel，用单条 `T.copy(acc_s_l0c, acc_s_ub)` 触发 workspace 消除，并在生成代码中确认两阶段拷贝被自动插入。

**操作步骤**：

1. 新建一个脚本 `matmul_act.py`（**示例代码**，仿照 `flash_attn_bshd_developer.py` 的结构精简而来，非项目原有文件），内容如下：

   ```python
   # 示例代码：最小化 Cube→Vector 跨核 kernel
   import tilelang
   from tilelang import language as T
   import torch

   torch.set_default_device("npu")
   tilelang.disable_cache()

   M = N = K = 128
   block_M = block_N = K_L1 = 64
   dtype = "float16"
   accum_dtype = "float"

   pass_configs = {
       tilelang.PassConfigKey.TL_ASCEND_AUTO_CV_COMBINE: True,
       tilelang.PassConfigKey.TL_ASCEND_AUTO_SYNC: True,
       tilelang.PassConfigKey.TL_ASCEND_AUTO_CV_SYNC: True,
   }


   @tilelang.jit(out_idx=[2], pass_configs=pass_configs)
   def matmul_relu():
       @T.prim_func
       def main(
           A: T.Tensor([M, K], dtype),
           B: T.Tensor([K, N], dtype),
           C: T.Tensor([M, N], dtype),
       ):
           with T.Kernel(M // block_M, N // block_N, threads=2, is_npu=True) as (cid):
               bx = cid % (N // block_N)
               by = cid // (N // block_N)

               a_l1 = T.alloc_shared([block_M, K_L1], dtype)
               b_l1 = T.alloc_shared([K_L1, block_N], dtype)
               acc_l0c = T.alloc_fragment([block_M, block_N], accum_dtype)
               acc_ub = T.alloc_shared([block_M, block_N], accum_dtype)

               T.copy(A[by * block_M:(by + 1) * block_M, :], a_l1)
               for k in T.serial(T.ceildiv(K, K_L1)):
                   T.copy(B[k * K_L1:(k + 1) * K_L1, bx * block_N:(bx + 1) * block_N], b_l1)
                   T.gemm_v0(a_l1, b_l1, acc_l0c, init=(k == 0))

               # —— 这一行触发 Workspace 消除：L0C(Cube) → UB(Vector) ——
               T.copy(acc_l0c, acc_ub)
               T.tile.mul(acc_ub, acc_ub, acc_ub)  # Vector 侧逐元素计算

               T.copy(acc_ub, C[by * block_M:(by + 1) * block_M, bx * block_N:(bx + 1) * block_N])
       return main


   func = matmul_relu()
   print(func.get_kernel_source())   # 打印生成的 Ascend C 源码
   ```

   > 说明：`acc_l0c` 是 `alloc_fragment`（L0C，Cube），`acc_ub` 是 `alloc_shared`（由 `AscendInferBufferScope` 推断为 UB，Vector）。两者之间的 `T.copy` 即跨核搬运。`threads=2` 配合三个 CV 相关 pass，让编译器自动做 CV 分离与同步。

2. 在昇腾环境运行 `python matmul_act.py`（无 NPU 则标注「待本地验证」）。
3. 在打印出的源码里搜索 `copy_l0c_to_gm`、`copy_gm_to_ub` 与 `workspace_1_handle`。

**需要观察的现象**：

- 源码里**找不到** `copy_l0c_to_ub`（已被改写）。
- 出现 `copy_l0c_to_gm<...>(workspace_1_handle[...], acc_l0c, ...)`：Cube 把 L0C 写入 workspace（第一阶段）。
- 在 `acc_ub` 被 `mul` 消费之前，出现 `copy_gm_to_ub<...>(acc_ub, workspace_1_handle[...], ...)`：从 workspace 读回 UB（第二阶段，延迟回拷）。
- `workspace_1_handle` 作为 kernel 的一个额外参数出现，且无需用户在调用时传入（由运行时自动分配，见 4.3）。

**预期结果**：两阶段拷贝均出现在生成代码中，且第二阶段紧贴在 `mul`（消费 `acc_ub` 的语句）之前而非紧跟第一阶段之后。若运行环境不具备，记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：workspace 的形状为什么是 `[total_core_nums, M, N]` 而不是单份 `[M, N]`？

**参考答案**：因为多个 AI Core 并行执行同一份 kernel，每个核都会产生自己的 \(M \times N\) 跨核数据。若只用单份 `[M, N]`，各核会互相覆盖。所以按 `cid` 给每核划出独立区段：第 `cid` 核落在偏移 `cid * M * N` 处，总长度 `total_core_nums * M * N`。`[total_core_nums, M, N]` 只是这片连续 GM 区间的多维视图。

**练习 2**：为什么第二阶段回拷是「延迟」插入，而不是紧跟第一阶段？

**参考答案**：第一阶段写 workspace 后，数据已在 GM；若立即搬回 UB，UB 会被这段数据占据直到被消费，期间无法他用，且搬运与计算无法重叠。延迟到「真正读取该 buffer 的语句之前」才回拷，缩短了数据在片上的驻留时间，也减少了搬运与计算的交错。判定依据是 `rw_mask`：只有「读」访问才触发回拷，纯写不触发。

**练习 3**：`threads=2` 时，workspace 存完整 \(M \times N\)，但每个 vid 只读一半。这个「一半」是怎么体现的？

**参考答案**：在 `CreateWorkspaceAccessPtr` 里，当对侧是 UB 且 `vector_cnt > 1` 时，access_ptr 的 offset 在 `cid * M * N` 基础上再叠加 `vid * M * N / vector_cnt`；同时第二阶段 `copy_gm_to_ub` 的模板维（`maskShapeM`）也被除以 `vector_cnt`。于是 vid=0 读前半、vid=1 读后半，正好对应 VidReduction 把 UB 对半切后的两份。

---

### 4.3 workspace_idx 与自动分配、延迟回拷

#### 4.3.1 概念说明

到这里你可能会问：编译器自动分配的 workspace，运行时谁负责真正在 GM 上申请内存？答案是运行时适配器（`CythonKernelAdapter`）。它靠两个机制识别「这块参数是 workspace，需要我自动分配」：

1. **`workspace_idx`（用户显式声明）**：用户在 `@tilelang.jit(...)` 里用 `workspace_idx=[4,5,6,7]` 声明「函数签名的第 4~7 个参数是 workspace」。典型场景是 Sparse Flash Attention（SFA）这类**用户自己写出 workspace 切片**（如 `workspace_1[cid, vid*block_M//2 : ..., :]`）的算子——workspace 作为普通张量出现在 kernel 体里，用户必须显式声明它的位置。

2. **`auto_gm_indices`（编译器自动产生）**：本讲的 `AscendWorkspaceReduction` pass 在改写完后，把新加的 workspace 参数下标写进 PrimFunc 的 `auto_gm_indices` 属性。运行时读到这个属性，就知道「这些参数是编译器偷偷加的 workspace，调用者不会传，我来分配」。

两者最终都汇入 `CythonKernelWrapper` 的 `workspace_idx` / `auto_gm_idx` 两个列表，在 `forward` 时统一用 `torch.empty(...)` 申请。**对调用者完全透明**：用户只传输入张量，workspace 由框架全权管理。

> 关键区分：`workspace_idx` 是「用户在源码里写了 workspace、告诉框架它的位置」；`auto_gm_indices` 是「用户根本没写 workspace、编译器自动加了并自报位置」。前者服务于手写 workspace 的复杂算子（SFA），后者服务于本讲的自动两阶段拷贝。

#### 4.3.2 核心流程

```
编译期:
  AscendWorkspaceReduction pass 改写完毕
    → PrimFunc 多出 workspace_N_handle 参数
    → PrimFunc 属性 auto_gm_indices = [这些参数的下标]
  JITKernel._compile_and_create_adapter
    → 从 device_mod 读出 auto_gm_indices，存入 self._auto_gm_idx
    → 连同用户声明的 workspace_idx 一起传给 CythonKernelAdapter

运行期 (每次 forward):
  CythonKernelWrapper.forward 遍历 params:
    i in result_idx 或 workspace_idx → torch.empty 申请输出/workspace
    i in auto_gm_idx                → torch.empty 申请编译器自动 workspace
    其他                            → 取调用者传入的输入张量
  打包所有张量指针 → 调用 lib.call 启动 kernel
```

注意 `auto_gm_idx` 分支有一个硬限制：**不支持动态 shape**——因为 workspace 形状必须是编译期常量（pass 在 ICHECK 里强制 `virtual_channel` 的尺寸为 `IntImm`）。这也是官方教程列出的首要限制。

#### 4.3.3 源码精读

**编译期写入 auto_gm_indices** —— 见 4.2.3 已引用的：

[src/transform/ascend_workspace_reduction.cc:947-950](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L947-L950) —— `WithAttr(..., "auto_gm_indices", auto_gm_idx)` 把 workspace 参数下标贴回 PrimFunc。

**JITKernel 读出该属性** —— 编译完成后从目标 PrimFunc 的 attrs 里取回下标列表：

[tilelang/jit/kernel.py:248-250](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L248-L250) —— 若 PrimFunc 含 `auto_gm_indices` 属性，则 `[int(item.value) for item in ...]` 解析为整数下标列表，存入 `self._auto_gm_idx`，随后传给 `CythonKernelAdapter`。

**运行时按 auto_gm_idx 自动申请** —— `CythonKernelWrapper.forward` 遍历参数，对 `auto_gm_idx` 内的下标用 `torch.empty` 申请：

[tilelang/jit/adapter/cython/cython_wrapper.pyx:130-137](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L130-L137) —— `elif i in self.auto_gm_idx:` 分支按 `param_shapes[i]` 申请 torch 张量，注释明确「Auto GM: Dynamic shape scenarios are not supported currently」。对比上一分支（`result_idx` / `workspace_idx`），可见三者都走 `torch.empty`，只是来源标记不同。

**用户声明的 workspace_idx** —— 当用户**自己**写出 workspace 切片（典型如 SFA）时，需在装饰器声明其位置：

[docs/tutorials/automatic_workspace_allocation.md:12-16](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/automatic_workspace_allocation.md#L12-L16) —— `@tilelang.jit(out_idx=[3], workspace_idx=[4, 5, 6, 7])` 声明第 4~7 个参数为 workspace，框架据此自动管理其生命周期，调用者无需传入。

**后端限制** —— 该自动管理特性目前仅 Cython 后端支持：

[docs/tutorials/automatic_workspace_allocation.md:62-65](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/automatic_workspace_allocation.md#L62-L65) —— 明确「Currently ... only available in Cython Backend」，ctypes 等后端暂不支持。

**当前限制汇总** —— 官方教程列出 workspace 消除的三条约束：

[docs/tutorials/workspace_reduction_tutorial.md:65-74](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/workspace_reduction_tutorial.md#L65-L74) —— 静态 shape 要求、必须配合 vid 消除（threads=1/2）、目前仅支持 2D `[M,N]`、skip UB 场景目前仅用于 Sparse Flash Attention 的索引数组搬运。

#### 4.3.4 代码实践

**实践目标**：观察「调用者无需传 workspace」这一透明性，并理解 `workspace_idx` 与 `auto_gm_indices` 的区别。

**操作步骤**：

1. 复用 4.2.4 的 `matmul_act.py`（或直接用 `flash_attn_bshd_developer.py`）。
2. 在 `func = matmul_relu()` 之后，**不传**任何 workspace，直接调用：

   ```python
   # 示例代码
   a = torch.randn(M, K, dtype=torch.float16)
   b = torch.randn(K, N, dtype=torch.float16)
   c = func(a, b)        # 只传输入，workspace 由框架自动分配
   print(c.shape)        # torch.Size([128, 128])
   print(func.auto_gm_idx)   # 打印编译器自动加的 workspace 参数下标
   ```

3. 若手头有 Sparse Flash Attention 样例（`examples/sparse_flash_attention/`），对比它的 `@tilelang.jit(..., workspace_idx=[...])` 写法。

**需要观察的现象**：

- `func(a, b)` 正常返回，无需调用者提供 workspace——它由 `CythonKernelWrapper` 在 `auto_gm_idx` 分支自动 `torch.empty` 申请。
- `func.auto_gm_idx` 给出一个非空列表（如 `[3]`），即编译器自动加的 `workspace_1_handle` 在参数列表中的下标。
- SFA 样例则相反：workspace 是用户在 kernel 体里显式索引的（`workspace_1[cid, ...]`），故必须在装饰器里用 `workspace_idx` 声明，二者下标来源不同。

**预期结果**：自动 workspace 对调用者完全透明；`auto_gm_idx` 与用户声明的 `workspace_idx` 是两条独立的「这是 workspace」通道，最终都汇入运行时的自动分配逻辑。无 NPU 环境则记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果一个 kernel 既用了 `AscendWorkspaceReduction` 自动加的 workspace，又用了用户手写的 workspace（SFA 风格），运行时怎么区分二者？

**参考答案**：靠两个独立列表。用户手写的 workspace 由装饰器的 `workspace_idx` 指定，编译器自动加的由 PrimFunc 的 `auto_gm_indices` 属性指定。`CythonKernelWrapper` 同时持有 `workspace_idx` 与 `auto_gm_idx`，`forward` 时分别命中各自的 `torch.empty` 分支，互不干扰。

**练习 2**：为什么 `auto_gm_idx` 分支不支持动态 shape？

**参考答案**：workspace 的形状与偏移在 `AscendWorkspaceReduction` pass 里被固化为编译期常量（`ICHECK` 强制 `virtual_channel` 的尺寸为 `IntImm`），多核 `cid` 偏移、vid 对半切都依赖确定的 \(M \times N\)。若允许动态 shape，workspace 形状、每核区段、vid 偏移都无法在编译期算出，整套寻址会失效。所以动态 shape 目前不支持。

**练习 3**：延迟回拷检测为什么用 `rw_mask & 1`（读位）而不是 `& 2`（写位）？

**参考答案**：回拷的目的是「在数据被消费前把它从 workspace 搬回片上」。消费即「读」。若某条语句只是「写」该 buffer（例如重新赋值覆盖），那它不需要旧数据，前面就不必插回拷——插了也是白搬。所以只有读访问才触发延迟回拷，写访问被显式跳过。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读 + 改 + 验」的小任务：

1. **读**：打开 [examples/developer_mode/flash_attn_bshd_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py)，找出**所有**触发 workspace 消除的 `T.copy`。

   - 提示：找源是 `acc_s_l0c` / `acc_o_l0c`（L0C）、目标是某个 UB buffer 的 `T.copy`。
   - 参考：[examples/developer_mode/flash_attn_bshd_developer.py:82](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py#L82) 的 `T.copy(acc_s_l0c, acc_s_ub_)` 与 [examples/developer_mode/flash_attn_bshd_developer.py:110](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py#L110) 的 `T.copy(acc_o_l0c, acc_o_ub)`。

2. **改**：用 `func.get_kernel_source()` 打印生成代码，在其中定位每处 `copy_l0c_to_gm`（第一阶段）与对应的 `copy_gm_to_ub`（第二阶段），并画出两者的 `workspace_N_handle` 偏移关系（cid 基址 + 可选 vid 偏移）。

3. **验**：回答三个问题——
   - 这处 `T.copy` 改写后，第二阶段回拷插在了**哪条消费语句之前**？为什么不是紧跟第一阶段？
   - 该 kernel 用的是 `auto_gm_indices`（编译器自动）还是 `workspace_idx`（用户声明）？为什么？（提示：看调用处 `func(q, k, v)` 是否传了 workspace，以及 `flash_attn_bshd_developer.py` 是否在装饰器里写了 `workspace_idx`。）
   - 该 kernel 用了 `threads=2`，这对 workspace 寻址意味着什么？

**预期产出**：一张「前端 `T.copy` → 第一阶段写 workspace →（延迟）第二阶段读 workspace」的对应表，以及对上述三个问题的书面回答。运行部分若无 NPU 环境，统一标注「待本地验证」。

## 6. 本讲小结

- **跨核搬运是物理现实**：Cube（L0C）与 Vector（UB/L1）不能直连，`copy_l0c_to_ub` / `copy_ub_to_l1` 在 lowering 时被打上 `virtual_channel` 标记，表示「虚拟意图，尚未落地」。
- **AscendWorkspaceReduction 自动改写**：它把每条虚拟搬运改成两阶段——第一阶段 `copy_l0c_to_gm`/`copy_ub_to_gm` 写 workspace，第二阶段 `copy_gm_to_ub`/`copy_gm_to_l1` 读回；并在 `LowerAndLegalize` 阶段执行。
- **多核与双向量核寻址**：workspace 形状 `[total_core_nums, M, N]`，每核偏移 `cid * M * N`；`threads=2` 时叠加 `vid * M * N / vector_cnt`，workspace 存完整数据、每个 vid 读一半。
- **延迟回拷**：第二阶段不紧跟第一阶段，而是插在「真正读该 buffer 的语句之前」，靠 `rw_mask` 读位判定，减少片上驻留与搬运/计算交错。
- **两种 workspace 通道**：用户用 `workspace_idx` 显式声明（SFA 等手写 workspace 场景）；编译器用 `auto_gm_indices` 属性自报（本讲自动两阶段拷贝）。两者最终都由 `CythonKernelWrapper` 在 `forward` 时 `torch.empty` 自动申请，对调用者透明。
- **限制**：需静态 shape、需配合 vid 消除（threads=1/2）、目前仅 2D、仅 Cython 后端支持自动分配。

## 7. 下一步学习建议

- **u7-l1 FlashAttention 实现案例**：本讲的 `flash_attn_bshd_developer.py` 会在那一讲作为综合案例完整拆解，把 Cube 算 QK、Vector 算 softmax/PV、跨核 workspace 中转串成一条完整数据流，是检验你是否真懂本讲的最佳读物。
- **u5-l2 跨核流水**：本讲只解决了「单次跨核搬运怎么自动落地」，当跨核搬运发生在**循环**里、且要让 Cube 写领先于 Vector 读以掩盖 GM 往返延迟时，就需要 `CrossCorePipeline` pass 把它做成流水——这是「把单条搬运变快」到「把循环里的搬运重叠起来」的下一步。
- **阅读 `skip UB` 分支**（[src/transform/ascend_workspace_reduction.cc:317-388](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_workspace_reduction.cc#L317-L388)）：SFA 的索引数组搬运走的是这条特殊路径，理解它能帮你把 workspace 消除与 vid 消除（u5-l3）的契约彻底打通。
