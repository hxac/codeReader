# Buffer 管理与双缓冲

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 ATVOSS 里「一个 UB 缓冲块」从被声明到被物理分配经历了哪两层抽象（**图级逻辑缓冲** vs **Block 级物理池**）。
2. 解释 `ParamBufIdMap` 如何为每个入参/出参/临时变量分配 ping/pong 两个物理槽位，以及 `GetBufferId` 在运行时如何按 `pingPong` 位选出其中一个。
3. 读懂 `GenerateBufferIdOrder` 如何按 `MemLevel`（LEVEL_0/1/2）从 MTE2/MTE3/TEMP 三个「空闲池」里借用/归还缓冲，并理解三级策略对缓冲复用激进程度的影响。
4. 推导 `UB_TILE_SIZE = Arch::UB_SIZE / MAX_BUFFER_COUNT` 的来历，知道 UB 为何被等分成若干槽位。
5. 描述 ping-pong 双缓冲如何让「第 i 次 Tile 的计算/回写」与「第 i+1 次 Tile 的 MTE2 搬入」在时间上重叠，从而隐藏 GM↔UB 的搬运延迟。

## 2. 前置知识

本讲是专家篇，默认你已读过以下前置讲义：

- **u2-l9（Block 层）**：那里讲过 UB 被划分为 IN/OUT/CALC 三段、`UB_TILE_SIZE = UB_SIZE / MAX_BUFFER_COUNT`、`BlockTensor` 的 CopyIn/CopyOut，以及求值器用 MTE2→V→MTE3 同步链配合双缓冲隐藏搬运延迟。本讲把其中的「缓冲如何被编号、分配、复用」彻底打开。
- **u3-l1（求值器）**：`Evaluator<OpAssign<...>>` 递归求值，`Param`/`LocalVar` 用编译期序号 `N` 从 `ContextData` 取张量。本讲会用到 `ContextData` 里的 `bufPools` 与 `pingPong` 两个字段。
- **u3-l3（DAG 与 Bind）**：`FullAutoDag` 把表达式变成有序算子序列，`DagNodeInfo` 做存活分析估算缓冲数。本讲正是 DAG 之后、求值器之前的「缓冲 ID 分配」环节。

下面几个术语本讲会反复用到，先对齐：

- **UB（Unified Buffer）**：AI Core 内部离 Vector 计算单元最近的高速 SRAM，DAV_3510 架构上固定为 240 KiB。所有 Tile 级计算都在 UB 里进行。
- **GM（Global Memory）**：Device 侧 HBM 显存，容量大但延迟高。算子的输入/输出最终都在 GM。
- **MTE2 / V / MTE3**：AI Core 的三条独立硬件流水线——MTE2 负责 GM→UB 搬入，V 负责向量计算，MTE3 负责 UB→GM 搬出。它们能并行工作，靠事件/Mutex 同步。
- **ping-pong（乒乓）双缓冲**：为同一份数据准备两块缓冲（ping 与 pong），让相邻两次迭代交替使用，使「搬运」与「计算」重叠。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/common/arch.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h) | 硬件常量：`DAV_3510` 的 `CORE_NUM=56`、`UB_SIZE=240*1024`。UB 大小由此决定。 |
| [include/utils/patterns.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h) | `MemMngPolicy`（AUTO/MANUAL）与 `MemLevel`（LEVEL_0/1/2）枚举，缓冲复用策略的开关。 |
| [include/elewise/graph/buffer.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h) | 本讲核心：缓冲类型/位置枚举、`ParamBufIdMap` ping/pong ID、`GenerateBufferIdOrder` 图级分配、`GetBufferId` 运行时查表。 |
| [include/elewise/graph/dag.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h) | `ChooseBufferLevel`（选 MemLevel）、`GetBufferIds`（构造三池并调用 `GenerateBufferIdOrder`）、`BufMap` 产物。 |
| [include/utils/buf_pool/block_buf_pool.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/buf_pool/block_buf_pool.h) | `BlockBufferEx`：物理 UB 池，预占整块 UB 并按 `bufferId` 切片下发 `LocalTensor`。 |
| [include/utils/buf_pool/loopbuf.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/buf_pool/loopbuf.h) | `LoopBufferEx`：基于 `TPipe` 事件 ID 的环形缓冲池（带同步），是另一套缓冲管理实现，供对比理解。 |
| [include/elewise/block/schedule.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h) | `MAX_BUFFER_COUNT`、`UB_TILE_SIZE` 的推导；`Process` 循环用 `pingPong=i&1` 驱动双缓冲。 |
| [include/elewise/tile/tensor_evaluator.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h) | OpCopyIn/OpCopyOut/OpAlloc/OpFree 求值器：调用 `GetBufferId` 取槽位 + `Mutex` 锁链同步。 |

## 4. 核心概念与源码讲解

本讲按数据流自上而下拆成五个最小模块：先看缓冲的「类型与位置」分类（4.1），再看「入参/临时变量 → 物理槽位」的 ping/pong 映射（4.2），接着是图级按 MemLevel 的池化分配（4.3），然后落到 Block 层的物理 UB 池（4.4），最后把双缓冲如何与流水重叠串起来（4.5）。

### 4.1 缓冲位置与类型：BufType / BufPosInList 与用途位图

#### 4.1.1 概念说明

一块 UB 缓冲，从「被谁用、用在哪个流水线」的角度需要打两个标签：

- **它是谁的缓冲**：是外部入参/出参（`PARAM`），还是内部临时变量（`LOCAL_VAR`）。这决定了它要不要 ping/pong 双缓冲（见 4.2）。
- **它服务于哪条流水线**：MTE2（搬入）、MTE3（搬出）、还是 TEMP（计算用的中间量）。这决定了它能否被复用、复用得多激进。

ATVOSS 在 `buffer.h` 里用两套枚举 + 一组位图常量分别编码这两个维度。

#### 4.1.2 核心流程

```
BufType         → 区分 PARAM / LOCAL_VAR（决定 ping/pong）
BufPosInList    → 区分缓冲在“空闲池”里的位置（PERSIST_MTE2 / MTE2 / PERSIST_MTE3 / MTE3 / PERSIST_TEMP / TEMP / PONG_MTE3）
BUF_MTE2/MTE3/TEMP 位图 → 在一个 32 位整数里编码“这块缓冲属于哪条流水线”，便于 Combine（多缓冲合并编码）
```

`PERSIST_*` 表示「常驻缓冲」（比如被广播算子缓存、跨多个算子复用），普通 `MTE2/MTE3/TEMP` 是一次性缓冲。`PONG_MTE3` 是 LEVEL_0 策略下专门为 MTE3 预留的 pong 槽。

#### 4.1.3 源码精读

`BufType` 只有两个值，对应外部参数与内部临时变量两类缓冲宿主：

[include/elewise/graph/buffer.h:31-35](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L31-L35) — 定义 `BufType::PARAM` 与 `BufType::LOCAL_VAR`。

`BufPosInList` 把缓冲按「所属流水线 + 是否常驻」细分成 7 个位置槽，外加一个 `MAX_POS`（被复用为「已分配/待释放」哨兵下标）：

[include/elewise/graph/buffer.h:37-50](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L37-L50) — 7 档位置枚举与 `BUF_ALLOCATED_IDX`/`BUF_TO_RELEASE_IDX` 哨兵。

用途位图用一个 32 位整数的低 5 位编码缓冲身份，方便多个缓冲「打包」进一个整数（`CombineBufferWrapper` 会把它们移位拼接）：

[include/elewise/graph/buffer.h:52-66](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L52-L66) — `BUF_MTE2/MTE3/TEMP/PLACEHOLDER/SCALAR` 用途位，`BUF_PING/PONG` 选择位，以及合并编码的 `BUF_COMBINE_SHIFT=5`、`BUF_PING_PONG=2`、`BUF_MAX_COUNT=32`。

> 小贴士：`BUF_PLACEHOLDER`（值为 8，`0b01'000`）专门给 CopyOut 这类「不需要新缓冲」的节点占位，分配时会被填成 `-1`（见 4.3 的 OpCopyOut 分支）；`BUF_SCALAR` 给标量算子，标量不占 UB 空间。

#### 4.1.4 代码实践

**实践目标**：用「用途位图」手工解码一块缓冲的身份。

**操作步骤**：

1. 打开 [include/elewise/graph/buffer.h:52-66](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L52-L66)，记下 5 个用途位的值。
2. 假设某缓冲的 `bufferUsage = BUF_MTE2 | BUF_PLACEHOLDER`（即 `0b00'001 | 0b01'000 = 0b01'001 = 9`）。
3. 用掩码 `0b00'111`（低 3 位）取出真实用途，判断它是 MTE2 还是占位符。

**需要观察的现象**：用途位图让「占位」与「真实流水线」可以共存于一个整数——CopyOut 节点既被标记为占位（不分配新缓冲），又可能复用某条流水线的旧缓冲。

**预期结果**：`9 & 0b111 = 1 = BUF_MTE2`，说明这块缓冲本质上属于 MTE2 流水线，但当前节点只是「借用」它做占位。理解这一点对读懂 4.3 的 `CalcPongBufferId` 分支很关键。

#### 4.1.5 小练习与答案

**练习 1**：`BufPosInList` 为什么要把 MTE2 拆成 `PERSIST_MTE2` 和 `MTE2` 两档？

**参考答案**：`PERSIST_*` 表示常驻缓冲（生命周期跨越多个算子，如被广播算子缓存的输入），普通档是一次性缓冲。拆开是为了让分配器优先复用一次性缓冲、把常驻缓冲留到真正需要长期占用的地方，从而压低 UB 峰值占用。

**练习 2**：`BUF_COMBINE_SHIFT = 5` 为什么取 5？

**参考答案**：用途位最多用低 5 位（MTE2/MTE3/TEMP/PLACEHOLDER/SCALAR），所以每 5 位编码一块缓冲。注释里写明「At most 5（5×5=25 < 32，uint32 共 32 位）」，即一个 32 位整数最多打包 5 块缓冲的身份。

---

### 4.2 Param/LocalVar → 物理槽位：ParamBufIdMap 与 ping/pong ID

#### 4.2.1 概念说明

u3-l1 讲过，求值器靠 Param/LocalVar 的编译期序号 `N` 从 `ContextData` 取张量。但「序号 N」只是逻辑身份，真正要用 UB 里哪一块物理内存，需要一张映射表：**「序号 N + pingPong 位 → 物理 bufferId」**。这张表就是 `ParamBufIdMap`，表的构造由 `GenerateBufferId` 完成，运行时查表由 `GetBufferId` 完成。

关键设计：**入参/出参（PARAM）给两块缓冲（ping + pong），临时变量（LOCAL_VAR）只给一块**。原因是 PARAM 的数据来自/去往 GM，搬运与计算可以重叠，故双缓冲有收益；LOCAL_VAR 是纯 UB 内部中间量，单缓冲即可。

#### 4.2.2 核心流程

`GenerateBufferId<paramCount, localVarCount>` 生成一张 `TypeList<ParamBufIdMap...>`：

```
pongOffset = paramCount + localVarCount          // pong 相对 ping 的偏移量

PARAM i (i = 1..paramCount):
    pingId = i - 1                                // 0, 1, ..., paramCount-1
    pongId = pingId + pongOffset                  // 跳过 LOCAL_VAR 区，放在高地址

LOCAL_VAR j (j = 1..localVarCount):
    pingId = pongId = paramCount - 1 + j          // LOCAL_VAR：ping == pong（单缓冲）
```

物理槽位布局（bufferId 从 0 起）：

```
[ PARAM ping 区: 0 .. paramCount-1 ]
[ LOCAL_VAR 区 : paramCount .. paramCount+localVarCount-1 ]   （单缓冲）
[ PARAM pong 区: paramCount+localVarCount .. 2*paramCount+localVarCount-1 ]
```

运行时 `GetBufferId<BuffMaps, N, bt>(pingPong)`：在表里找到 `paramNum==N && bufType==bt` 的那一项，`pingPong` 为真返回 `pingBufId`，为假返回 `pongBufId`。

#### 4.2.3 源码精读

`ParamBufIdMap` 是一条映射记录，存「序号 + 类型 + ping/pong 两个 ID」：

[include/elewise/graph/buffer.h:68-74](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L68-L74) — `ParamBufIdMap<N, bt, pingId, pongId>` 结构。

`ParamBufIdMapGenerator` 是编译期递归生成器。注意 `pongId` 的计算：只有 `PARAM` 才加 `pongOffset`，`LOCAL_VAR` 的 `pongId == pingId`：

[include/elewise/graph/buffer.h:76-91](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L76-L91) — 递归体里 `pongId = (bt==PARAM ? pingId+pongOffset : pingId)`；终止条件返回空表。

`GenerateBufferId` 把 PARAM 表与 LOCAL_VAR 表拼起来，PARAM 的 `bufIdOffset=0`、LOCAL_VAR 的 `bufIdOffset=paramCount`，二者共享同一个 `pongOffset`：

[include/elewise/graph/buffer.h:93-106](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L93-L106) — 拼接 PARAM 与 LOCAL_VAR 两段映射。

运行时查表函数 `GetBufferId`（注意它 `#if !defined(__ATVOSS_HOST_ONLY__)` 包裹，只在 Device 侧编译）：

[include/elewise/graph/buffer.h:509-522](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L509-L522) — 线性扫描表找到匹配项，按 `isPing` 返回 ping/pong ID；找不到则 `static_assert` 报「Param or LocalVar Id invalid」。

> 这张表由 DAG 构建时产出。`ManualDag` 直接用 `GenerateBufferId`（[dag.h:424](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L424)）；`FullAutoDag` 经 `GetBufferIds` + `Bind2OpAssign` 反填后产出 `BufMap`（[dag.h:585-595](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L585-L595)）。`schedule.h` 把它作为 `BufferMaps` 注入 `ContextData::BuffMaps`（[schedule.h:235](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L235)），求值器就能查到。

#### 4.2.4 代码实践

**实践目标**：为一个 2 入参 + 1 出参 + 1 临时变量的算子，手工推导整张 `BufMap`。

**操作步骤**：

1. 设 `paramCount = 3`（2 个 IN + 1 个 OUT）、`localVarCount = 1`，故 `pongOffset = 3 + 1 = 4`。
2. 按 4.2.2 的公式逐行写出 PARAM 1/2/3 与 LOCAL_VAR 1 的 `pingId`/`pongId`。

**需要观察的现象**：PARAM 的 pong 区被「跳过」LOCAL_VAR 区放在高地址；LOCAL_VAR 的 ping 与 pong 指向同一槽位。

**预期结果**（待本地验证你的推导与此一致）：

| 节点 | pingId | pongId |
| --- | --- | --- |
| PARAM 1 | 0 | 4 |
| PARAM 2 | 1 | 5 |
| PARAM 3 | 2 | 6 |
| LOCAL_VAR 1 | 3 | 3 |

物理槽位总数 = `2*paramCount + localVarCount = 2*3 + 1 = 7`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LOCAL_VAR 的 ping 与 pong 相等？这样做在运行时安全吗？

**参考答案**：LOCAL_VAR 是纯 UB 内部的中间量，只被 V 流水线读写，不涉及与 MTE2/MTE3 的跨流水线搬运，没有「搬运与计算重叠」的需求，故单缓冲即可。安全是因为同一 Tile 内 LocalVar 的 Alloc→使用→Free 严格顺序执行（求值器线性推进），不会出现两个迭代同时访问同一 LocalVar 的情况。

**练习 2**：`GetBufferId` 用线性扫描（`start` 递增）查表，性能上会有问题吗？

**参考答案**：不会。表长度 = `paramCount + localVarCount`，通常只有个位数；且整个函数是 `__aicore__` 内联 + `constexpr` 友好，编译期模板实例化后扫描循环大概率被展开或折叠，运行时开销可忽略。

---

### 4.3 图级缓冲分配：GenerateBufferIdOrder 与 MemLevel 三级

#### 4.3.1 概念说明

4.2 解决的是「序号 → 槽位」的固定映射。但 `FullAutoDag` 路径（默认 `MemMngPolicy::AUTO`）还要回答一个更细的问题：**有序算子序列里的每一个 Op，该把它的输入/输出落到哪一类缓冲（MTE2/MTE3/TEMP）？缓冲之间能不能互相借用、复用？**

这就是 `GenerateBufferIdOrder` 的职责——它是一个编译期的「寄存器分配器」：维护 MTE2、MTE3、TEMP 三个空闲缓冲池，顺序扫描算子，按节点类型从合适的池子里「借」一块缓冲，节点用完后归还，从而压低同时存活的缓冲数。`MemLevel`（LEVEL_0/1/2）控制借用激进度：级别越高，越允许跨池复用（比如把一块 TEMP 缓冲借给 MTE2 用）。

#### 4.3.2 核心流程

`GenerateBufferIdOrder` 对 `OpLst` 里每个算子分类处理：

```
for op in OpLst:
    if op 是标量算子:        → 分配 BUF_SCALAR 占位，scalarIdx++
    elif op 是 OpCopyIn:     → 从 MTE2 池借一块（AllocMte2），登记到 ToReleaseLst
    elif op 是 OpCopyOut:    → 归还相关缓冲（ReleaseAndUpdateLst），填 -1 占位
    elif op 连接到输出:       → 从 MTE3 池借一块（AllocMte3）
    else (中间计算):         → 从 TEMP 池借一块（AllocTempBuffer）
最后：ExtractBufferId 把 AllocLst 展开成 [ping 行, pong 行] 的 2×N 矩阵
```

三个分配器（`AllocMte2_t` / `AllocMte3_t` / `AllocTempBuffer_t`）内部都用 `PriorityGetFirst_t` 按 `MemLevel` 决定「能从哪些池子借」：

- LEVEL_2（最激进）：TEMP 池可以为空（即 MTE2/MTE3/TEMP 三类完全共享）。
- LEVEL_0（最保守）：各池基本独立，只有显式允许时才跨借。

最终 `Bind2OpAssign`（[dag.h:585](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L585)）把这 2×N 矩阵反填回每个 Param/LocalVar，产出 4.2 用的 `BufMap`。

#### 4.3.3 源码精读

`GenerateBufferIdOrder` 的签名与长长的模板参数列表，注释里写清了每个参数的含义（`BufLstLst` 是三个空闲池、`pongOffset` 是 pong 偏移、`memLvl` 是策略、`AllocLst` 是已分配列表、`ToReleaseLst` 是待释放映射）：

[include/elewise/graph/buffer.h:525-551](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L525-L551) — 函数文档注释与签名。

主递归里对 `OpCopyIn` 的处理：调用 `AllocMte2_t` 借一块 MTE2 缓冲，登记到 `ToReleaseLst` 以便后续归还：

[include/elewise/graph/buffer.h:589-599](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L589-L599) — OpCopyIn 分支：`AllocMte2_t<BufLstLst, memLvl, ...>` 借缓冲，`Mapping<Op, Mte2>` 登记。

对中间计算节点（不连输出）的处理：从 TEMP 池借一块，同时调用 `ReleaseAndUpdateLst` 归还上一批可释放的缓冲：

[include/elewise/graph/buffer.h:625-638](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L625-L638) — else 分支：`AllocTempBuffer_t` 借 + `ReleaseAndUpdateLst` 还。

`AllocMte2` 的池选择逻辑——`PriorityGetFirst_t` 按优先级从 PersistMte2/Mte2/Tmp/PongMte3 取第一块，而 `MemLevel` 通过 `std::conditional_t` 决定哪些池「可见」：

[include/elewise/graph/buffer.h:400-424](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L400-L424) — `memLvl == LEVEL_2 || cache` 时 `UsedTmpLst` 置空（即不借 TEMP），LEVEL_0 时才允许用 PongMte3，体现三级激进度差异。

`MemLevel` 枚举本身只有三档：

[include/utils/patterns.h:39-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L39-L44) — `LEVEL_0/1/2`。

`ChooseBufferLevel`（dag.h）是策略选择器：默认从最激进的 LEVEL_2 试起，若该级缓冲数 `> MAX_BUFFER_NUMBER` 就降级，保证不超限：

[include/elewise/graph/dag.h:462-476](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L462-L476) — 先试 LEVEL_2，再试 LEVEL_1，都不行才 LEVEL_0。

> 上限守卫：`GetBufferIds` 里有一条 `static_assert(totalCount <= BUF_MAX_COUNT)`，即图级缓冲总数不能超过 32：

[include/elewise/graph/dag.h:541-544](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L541-L544) — 超过 32 会提示「Please try to switch MemLevel to LEVEL_1 or LEVEL_0」。

#### 4.3.4 代码实践

**实践目标**：跟踪一条 `out = Sqrt(in)` 的算子序列，看 `GenerateBufferIdOrder` 如何为它分配缓冲。

**操作步骤**：

1. 假设线性化 + 插入 Alloc/Free 后的 `OpLst` 是：`[OpAlloc(in), OpCopyIn(in), OpAlloc(out), OpAssign(out, Sqrt(in)), OpCopyOut(out), OpFree(in), OpFree(out)]`（顺序为示意，真实顺序由 DAG 决定）。
2. 逐个算子套用 4.3.2 的分类规则，记录每步从哪个池借/还。
3. 注意 `out` 连接到最终输出（`ConnectToAny_v<OutLst, Op>` 为真），所以它的赋值节点会走 MTE3 池而非 TEMP 池。

**需要观察的现象**：CopyIn 借 MTE2、Sqrt 借 TEMP（或 MTE3，因 out 连输出）、CopyOut 触发归还。整条序列的峰值存活缓冲数应远小于「每个算子各占一块」。

**预期结果**：待本地验证。重点不是具体 ID，而是理解「借—用—还」让缓冲被循环复用，峰值存活数 ≈ 同时在用的输入+输出+临时量。

#### 4.3.5 小练习与答案

**练习 1**：`MemLevel::LEVEL_2` 比 `LEVEL_0` 更激进，激进体现在哪里？代价是什么？

**参考答案**：LEVEL_2 允许 MTE2/MTE3/TEMP 三类缓冲跨池复用（`AllocMte2` 里 `UsedTmpLst` 在 LEVEL_2 时置空，即不从 TEMP 借），从而用最少的物理缓冲跑完整条序列。代价是缓冲被不同流水线分时复用，需要更频繁的同步（PipeBarrier/Mutex）来保证正确性；而 LEVEL_0 各池独立、几乎不跨借，缓冲多但同步少、流水更顺。

**练习 2**：为什么 `GenerateBufferIdOrder` 最后返回的是「2×N 矩阵」而不是一维数组？

**参考答案**：第 0 行是 ping 缓冲 ID、第 1 行是 pong 缓冲 ID（见 `ExtractBufferId`，[buffer.h:215-221](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L215-L221)）。每个算子的输出都要同时记下 ping/pong 两个候选槽，运行时由 `pingPong` 位二选一，这是双缓冲的物质基础。

---

### 4.4 Block 层物理池：BlockBufferEx、TPipe 与 UB_TILE_SIZE

#### 4.4.1 概念说明

4.2/4.3 都在「逻辑 bufferId」层面工作——bufferId 只是个整数编号。真正把它变成一块可读写的 UB `LocalTensor`，是 Block 层 `BlockBufferEx` 的职责：它在构造时一次性向 Ascend C 的 `TPipe` 申请整块 UB，再按 `bufferId` 把这块大内存切成等长的小片下发。

这块「每片多大」就是 `UB_TILE_SIZE`，它的推导是本讲最常被问到的一个公式。

#### 4.4.2 核心流程

```
IN_PARAMS_COUNT   = InParams * 2          # 每个输入 ping+pong 两槽
OUT_PARAMS_COUNT  = OutParams * 2         # 每个输出 ping+pong 两槽
LOCAL_VAR_COUNT   = LocalVars             # 每个临时变量单槽
MAX_BUFFER_COUNT  = IN_PARAMS_COUNT + OUT_PARAMS_COUNT + LOCAL_VAR_COUNT
UB_TILE_SIZE      = Arch::UB_SIZE / MAX_BUFFER_COUNT   # 等分 UB，向下对齐到 KB
```

即：把整块 UB（240 KiB）平均分给「所有需要的缓冲槽」，每槽一份。`BlockBufferEx<MAX_BUFFER_COUNT, UB_TILE_SIZE>` 预占 `MAX_BUFFER_COUNT * UB_TILE_SIZE` 字节（≈ 整块 UB），`AllocTensor(bufferId)` 返回第 `bufferId` 片。

#### 4.4.3 源码精读

`DAV_3510` 的 UB 大小是硬约束的源头：

[include/common/arch.h:22-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h#L22-L25) — `CORE_NUM=56`、`UB_SIZE=240*1024`。

`MAX_BUFFER_COUNT` 与 `UB_TILE_SIZE` 的推导（注意 IN/OUT 都乘 2，LOCAL_VAR 不乘）：

[include/elewise/block/schedule.h:134-142](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L134-L142) — `MAX_BUFFER_COUNT` 求和，`UB_TILE_SIZE = UB_SIZE / MAX_BUFFER_COUNT / 1024 * 1024`（`/1024*1024` 是向下对齐到 1 KiB），以及 `UB_ADDR_IN/OUT/CALC` 三段逻辑基址。

`BlockBufferEx` 的物理分配——构造时调 `TPipe::InitBuffer` 一次性预占，`AllocTensor` 按下标切片：

[include/utils/buf_pool/block_buf_pool.h:15-43](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/buf_pool/block_buf_pool.h#L15-L43) — `Init()` 里 `GetTPipePtr()->InitBuffer(tbuf_, TILE_SIZE*TILE_NUM)` 预占整块；`AllocTensor` 里 `tensorPool_[bufferId * BLOCK_LEN].ReinterpretCast<T>()` 切片。两条 `static_assert` 保证 `TILE_SIZE` 是 32 的倍数、`TILE_NUM>0`。

`BaseBlockSchedule` 构造函数里初始化池子，并做 TileShape 上限检查：

[include/elewise/block/schedule.h:207-214](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L207-L214) — 若用户 `BASIC_BLOCK`（TileShape 决定的每 Tile 元素数 × 类型大小）超过 `UB_TILE_SIZE`，触发 `TileCheckAssert` 报错；随后 `bufPools_.Init()`。

成员声明——一个 `TPipe` + 一个 `BlockBufferEx`：

[include/elewise/block/schedule.h:301-304](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L301-L304) — `AscendC::TPipe pipe_;` 与 `Atvoss::BlockBufferEx<MAX_BUFFER_COUNT, UB_TILE_SIZE> bufPools_;`。

> `TileCheckAssert` 的定义见 [include/elewise/block/schedule.h:74-77](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L74-L77)：`static_assert(USER_TILE_SIZE <= UB_TILE_SIZE, ...)`，即用户声明的 TileShape 不能超过单槽容量。
>
> 另一套实现 `LoopBufferEx`（[loopbuf.h:40-212](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/buf_pool/loopbuf.h#L40-L212)）走的是 `TPipe` 事件 ID（`AllocEventID`/`ReleaseEventID`）+ 环形 `header_` 指针的路线，自带 `set_flag/wait_flag` 同步，是 arch35 之外另一种缓冲管理选择。当前 Block 层默认用更简单的 `BlockBufferEx`，本讲以它为准。

#### 4.4.4 代码实践

**实践目标**：解释 `UB_TILE_SIZE` 为何按 `Arch::UB_SIZE / MAX_BUFFER_COUNT` 计算，并为 abs 算出具体值。

**操作步骤**：

1. 打开 [include/elewise/block/schedule.h:134-142](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L134-L142) 与 [include/common/arch.h:22-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h#L22-L25)。
2. 对 abs 样例（1 个 IN、1 个 OUT、0 个 LOCAL_VAR）：`IN_PARAMS_COUNT=2`、`OUT_PARAMS_COUNT=2`、`LOCAL_VAR_COUNT=0`、`MAX_BUFFER_COUNT=4`。
3. 代入公式：

\[
\text{UB\_TILE\_SIZE} = \left\lfloor \frac{240\times1024}{4}\middle/1024 \right\rfloor \times 1024 = 60\times1024 = 60\,\text{KiB}
\]

4. 验证：`BlockBufferEx<4, 61440>` 预占 `4 × 60 KiB = 240 KiB`，正好等于整块 UB。

**需要观察的现象**：UB 被等分成 `MAX_BUFFER_COUNT` 片，每片 `UB_TILE_SIZE`。`MAX_BUFFER_COUNT` 越大（算子入参/出参/临时变量越多），每片越小，用户 TileShape 上限随之收紧（`TileCheckAssert`）。

**预期结果**：abs 的单槽容量 60 KiB，能容纳 `60 KiB / sizeof(float) = 15360` 个 float；而 abs 的默认 `TileShape=Shape<32>`，远小于上限，安全。

#### 4.4.5 小练习与答案

**练习 1**：`UB_TILE_SIZE` 公式里的 `/1024*1024` 起什么作用？去掉会怎样？

**参考答案**：它是把每槽字节数向下对齐到 1 KiB 边界（先除以 1024 取整 KB 数，再乘回 1024）。Ascend C 的 `InitBuffer` 通常要求缓冲大小按一定粒度对齐；去掉后每槽可能是非整 KB，可能触发 `InitBuffer` 对齐断言或浪费尾部空间。

**练习 2**：如果一个算子有 3 个输入、2 个输出、4 个临时变量，`MAX_BUFFER_COUNT` 是多少？

**参考答案**：`IN_PARAMS_COUNT = 3*2 = 6`，`OUT_PARAMS_COUNT = 2*2 = 4`，`LOCAL_VAR_COUNT = 4`，合计 `MAX_BUFFER_COUNT = 6+4+4 = 14`。每槽 `240 KiB / 14 ≈ 17 KiB`（对齐后），用户的 TileShape 上限会被压得更紧。

---

### 4.5 ping-pong 双缓冲流水：Process 循环与 Mutex 锁链

#### 4.5.1 概念说明

前面四节铺好了所有材料：物理池有 ping/pong 两槽（4.2/4.4），求值器按 `pingPong` 位选槽（4.2），同步用 MTE2→V→MTE3 锁链（u3-l2）。本节把它们组装成**双缓冲流水**：让相邻两次 Tile 迭代分别落在 ping 与 pong 两块缓冲上，使「第 i 次的计算/回写」与「第 i+1 次的搬入」在三条流水线上并行执行，从而把 GM↔UB 的搬运延迟藏在计算背后。

#### 4.5.2 核心流程

`Process` 循环驱动双缓冲：

```
for i in [0, wholeLoop):                       # 整 Tile
    context.pingPong = i & 1                   # 偶数次 → pong 槽，奇数次 → ping 槽
    Evaluate<ExprTile>(context)                # CopyIn → 计算 → CopyOut，全部用本轮选中的槽
# 尾 Tile（若有）同理，pingPong = wholeLoop & 1
```

求值器内部（以 OpCopyIn 为例）：

```
bufferId = GetBufferId(BuffMaps, in.number, PARAM, pingPong)   # 选 ping 或 pong 槽
Mutex::Lock<PIPE_MTE2>(bufferId)     # 占住这块缓冲的 MTE2 锁
CopyIn(...)                          # GM → 该缓冲
Mutex::Unlock<PIPE_MTE2>(bufferId)   # 释放 MTE2 锁
Mutex::Lock<PIPE_V>(bufferId)        # 占住 V 锁 → 计算必须等本块搬完
```

关键点：**锁是按 `bufferId` 粒度的**。第 i 次迭代锁的是 pong 槽，第 i+1 次迭代锁的是 ping 槽，两个 bufferId 不同，锁互不冲突——于是第 i+1 次的 MTE2 搬入不必等第 i 次的 V 计算完成，二者并行。这就是「搬运延迟被计算隐藏」的原理。

#### 4.5.3 源码精读

`Process` 循环——`pingPong = i & 1` 是双缓冲的总开关，每次迭代构造新的 `ContextData` 下传：

[include/elewise/block/schedule.h:239-248](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L239-L248) — 整 Tile 循环 `i&1`、尾 Tile 用 `cfg.tileCnt` 与同一 `i&1`，`ContextData` 含 `gmOffset=i*BASIC_BLOCK`、`elementNum`、`pingPong`。

`ContextData` 结构——把 `bufPools`、`gmOffset`、`elementNum`、`pingPong` 与 `BuffMaps` 一起下传给求值器：

[include/common/type_def.h:15-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/type_def.h#L15-L25) — 六字段上下文，`BuffMaps` 作为类型别名供 `GetBufferId` 查表。

OpCopyIn 求值器——先按 `pingPong` 选槽，再用 Mutex 锁链串起 MTE2→V：

[include/elewise/tile/tensor_evaluator.h:60-85](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L60-L85) — `GetBufferId(..., context.pingPong)` 选槽；arch35 走 `Mutex::Lock/Unlock<PIPE_MTE2/PIPE_V>(bufferId)`，默认 ascend950 走粗粒度 `PipeBarrier<PIPE_ALL>()`。

OpCopyOut 求值器——完成 V→MTE3 的锁移交：

[include/elewise/tile/tensor_evaluator.h:87-113](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L87-L113) — `Mutex::Unlock<PIPE_V>(bufferId); Mutex::Lock<PIPE_MTE3>(bufferId)` 后 CopyOut，再 `Unlock<PIPE_MTE3>`。

OpAlloc 对 OUT 参数也加 V 锁，保证计算前该槽就绪：

[include/elewise/tile/tensor_evaluator.h:164-173](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L164-L173) — OUT 参数 Alloc 后 `Mutex::Lock<PIPE_V>(outId)`。

> 完整锁链（单块缓冲的生命周期）：**MTE2 锁（搬入）→ V 锁（计算）→ MTE3 锁（搬出）**。每个 bufferId 独立维护这条链，所以 ping 与 pong 两块的链互不阻塞，得以并行。arch35 用 `Mutex`（细粒度、bufferId 级），ascend950 默认用 `PipeBarrier<PIPE_ALL>`（粗粒度、全流水线屏障，正确性更保险但重叠度低）。

#### 4.5.4 代码实践

**实践目标**：说明双缓冲下第 i 次与第 i+1 次 Tile 如何重叠，并实地观察 `bufferId`/`pingPong` 的交替。

**操作步骤（源码阅读 + 轻量修改型实践）**：

1. 读 [include/elewise/block/schedule.h:239-248](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L239-L248)，确认 `pingPong = i & 1`。
2. 读 [include/elewise/tile/tensor_evaluator.h:68-84](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L68-L84)，注意 `GetBufferId(..., context.pingPong)` 与被注释掉的 `AscendC::printf`。
3. **可选修改（仅用于观察，勿提交）**：把 tensor_evaluator.h 中 OpCopyIn/OpCopyOut/OpAlloc/OpFree 里被注释的 `AscendC::printf` 取消注释（如 [tensor_evaluator.h:71](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L71)、[181](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L181) 等），重编 abs 样例。
4. 运行（真机或 `cannsim` 仿真），观察打印里 `bufferId` 与 `pingPong` 的取值。

**需要观察的现象**：对同一个入参（如 `in.number=1`），相邻 Tile 迭代的 `bufferId` 在两个值之间交替（如 pong→ping→pong→ping），证明偶/奇迭代落到不同物理槽。

**重叠解释（文字推导）**：

- 第 i 次迭代（偶，`pingPong=0` → pong 槽）：MTE2 搬入 pong → V 计算 pong → MTE3 回写 pong。
- 第 i+1 次迭代（奇，`pingPong=1` → ping 槽）：MTE2 搬入 ping → V 计算 ping → MTE3 回写 ping。
- 因 pong 与 ping 的 `bufferId` 不同，各自的 `Mutex` 锁链独立。于是第 i+1 次的 **MTE2 搬入 ping** 不必等第 i 次的 **V 计算 pong** 结束——二者在不同槽、不同流水线上并行。MTE2 的搬运延迟就此被 V 的计算时间「盖住」。

预期结果：在 NPU Profiling 里能看到 MTE2 与 V 流水线在时间轴上交叠（ping-pong 波形）；若关闭双缓冲（假设 ping/pong 指向同一槽），MTE2 与 V 将被迫串行，耗时显著增加。

#### 4.5.5 小练习与答案

**练习 1**：如果某个算子的某个入参只在一个 Tile 里用一次（无复用），双缓冲还有意义吗？

**参考答案**：对「只搬一次、用一次」的边界 Tile 意义不大（没有下一个迭代可重叠）。双缓冲的收益来自循环主体里相邻迭代的重叠；边界 Tile 只付出「多占一倍 UB」的成本而无重叠收益。这也是为什么 UB 紧张时框架会降级到 LEVEL_0/单缓冲策略。

**练习 2**：arch35 用 `Mutex::Lock<PIPE>(bufferId)`，ascend950 默认用 `PipeBarrier<PIPE_ALL>()`，两者在双缓冲效果上有什么差别？

**参考答案**：`Mutex` 是 bufferId 粒度的细粒度锁，只阻塞访问同一块缓冲的流水线，ping/pong 两块完全独立、重叠度最高；`PipeBarrier<PIPE_ALL>` 是全流水线粗粒度屏障，会把所有流水线都排空，重叠度低但实现简单、正确性保险。因此 arch35 的双缓冲流水效率通常优于 ascend950 默认路径。

---

## 5. 综合实践

**任务**：为一个 `out = Exp(in1) + Sqrt(in2)` 的双输入算子（1 个 IN `in1`、1 个 IN `in2`、1 个 OUT `out`，无临时变量——假设 Exp/Sqrt 的中间结果可原地或与输出共用）完整走一遍缓冲管理，把五节的知识串起来。

请按以下步骤完成（以源码阅读与手工推导为主，辅以可选编译验证）：

1. **确定缓冲槽位数**（4.4）：算出 `IN_PARAMS_COUNT`、`OUT_PARAMS_COUNT`、`LOCAL_VAR_COUNT`、`MAX_BUFFER_COUNT`，并据此推出 `UB_TILE_SIZE`（按 DAV_3510 的 240 KiB）。
2. **推导 BufMap**（4.2）：用 `GenerateBufferId` 的公式写出 `in1`/`in2`/`out` 三个 PARAM 的 `pingId`/`pongId`（`pongOffset = paramCount + localVarCount`）。
3. **跟踪图级分配**（4.3）：写出线性化后大致的算子序列（`OpCopyIn(in1)`、`OpCopyIn(in2)`、`Exp`、`Sqrt`、`Add`→`out`、`OpCopyOut(out)`…），说明 `GenerateBufferIdOrder` 会为 CopyIn 借 MTE2、为中间计算借 TEMP、为输出借 MTE3。
4. **解释双缓冲重叠**（4.5）：说明第 i 次迭代搬入 `in1`/`in2` 到 pong 槽、做计算时，第 i+1 次迭代可以同时搬入 ping 槽，从而隐藏 MTE2 延迟。
5. **（可选）编译验证**：仿照 abs 样例写一个含两个输入的 Config，用 `bash scripts/build.sh -DSOC=ascend950 <your_example>` 编译，观察是否通过 `TileCheckAssert` 与 `static_assert(totalCount <= BUF_MAX_COUNT)` 两道关卡。

**参考推导要点**：
- `MAX_BUFFER_COUNT = 2*2 + 2*1 + 0 = 6`；`UB_TILE_SIZE = 240 KiB / 6 = 40 KiB`（对齐后）。
- `pongOffset = 3 + 0 = 3`；BufMap：`in1` ping=0/pong=3，`in2` ping=1/pong=4，`out` ping=2/pong=5。
- 双缓冲下，`in1`/`in2` 的搬入（MTE2）与上一轮的计算（V）因 ping/pong 槽分离而并行。

## 6. 本讲小结

- ATVOSS 的缓冲管理是**两层抽象**：图级（`buffer.h`/`dag.h`）负责「算子 → 逻辑 bufferId」的池化分配与 ping/pong 编码；Block 级（`block_buf_pool.h`/`schedule.h`）负责把 bufferId 变成物理 UB 切片。
- **入参/出参双缓冲、临时变量单缓冲**：`ParamBufIdMap` 给 PARAM 分 ping/pong 两槽（pong 跳过 LOCAL_VAR 区放在高地址），LOCAL_VAR 的 ping==pong。
- `GenerateBufferIdOrder` 是编译期「寄存器分配器」：维护 MTE2/MTE3/TEMP 三个池，按算子类型借/还缓冲；`MemLevel`（LEVEL_0/1/2）控制跨池复用的激进程度，`ChooseBufferLevel` 自动选能装下的最激进档。
- `UB_TILE_SIZE = Arch::UB_SIZE / MAX_BUFFER_COUNT`：把 240 KiB UB 等分给所有缓冲槽；`MAX_BUFFER_COUNT = 2*InParams + 2*OutParams + LocalVars`。用户 TileShape 受 `TileCheckAssert` 约束不得超过单槽容量。
- **ping-pong 双缓冲**靠 `pingPong = i&1` 让相邻迭代落到不同物理槽，配合 bufferId 粒度的 `Mutex` 锁链（MTE2→V→MTE3）实现「搬入与计算并行」，隐藏 GM↔UB 延迟；arch35 走细粒度 Mutex，ascend950 默认走粗粒度 PipeBarrier。
- 安全网有三道：`static_assert(totalCount <= BUF_MAX_COUNT=32)`（图级缓冲总数上限）、`TileCheckAssert`（TileShape ≤ 单槽）、`BlockBufferEx` 的 `static_assert`（TILE_SIZE 32 对齐、TILE_NUM>0）。

## 7. 下一步学习建议

- **u3-l6（Reduce 模块）**：带 ReduceSum/Broadcast 的表达式在归约边界要额外插 CopyIn/Copy，缓冲复用判定也更复杂（`IsNodeUsedAfter`）。学完本节再看 Reduce 专用 `buffer.h` 会非常自然。
- **u3-l8（策略与配置定制）**：本讲的 `MemLevel` 与 `MemMngPolicy` 正是调优的核心旋钮。建议接着读 `platform_info.h`/`arch.h`，理解如何按硬件（`vectorCoreNum`/`ubSize`）切换缓冲策略。
- **深读 LoopBufferEx**：若对 arch35 的细粒度流水同步感兴趣，可对照读 [loopbuf.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/buf_pool/loopbuf.h)，理解 `AllocEventID`/`set_flag`/`wait_flag` 如何替代 Mutex 实现等价的双缓冲。
- **测试验证**：`tests/st` 下 `test_compute_*_with_manupolicy` 与 `test_compute_*_with_autopolicy` 两组用例能帮你观察 `MemMngPolicy` 对缓冲分配的实际影响（承接 u3-l10）。
