# u8-l2 MoE 通信算子：dispatch/combine 与 mega 融合

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清一个 MoE（Mixture of Experts）前向中 token 的完整旅行：router 打分 → dispatch 重排跨卡搬运 → 专家计算 → combine 归还 → 加权恢复原序。
2. 读懂三个真实算子的分工：`moe_dispatch`（去程）、`moe_combine`（回程）、`dispatch_mega_combine`（七段融合的单 kernel 全流程）。
3. 掌握 token 重排的核心数据结构——路由表 `tokenPerExpert / cumsumMM / preSumBeforeRank / expandedRowIdx`——并能手工推演它们如何换算出每一段拷贝的源地址与目的地址。
4. 理解「pending-credit 一类跨 dispatch 状态同步问题」：稀疏同步留下的账目（pending chunk、TPipe free credit、count-as-flag 计数）必须在生命周期边界被精确结算，否则下一次 dispatch 会读到残留状态而死锁或错算。
5. 会把这三类状态问题各自在源码中的「结算点」（drain / 析构 drain / epoch 重置）定位出来。

## 2. 前置知识

本讲是 advanced 层的「复杂算子实战」第二讲，默认你已完成单元七与 u8-l1，以下概念直接沿用，不再重新展开：

- **HCCL 窗口与远端指针**（u7-l2）：跨卡可见的 GM 缓冲叫窗口（window）；设备侧把「对端窗口基址 + 本地偏移」翻译成远端地址，例如 `moe_dispatch` 里的 `CommRemotePtr`（本讲 4.2.3 精读）。
- **数据/信号两段式握手**（u7-l2）：数据用 `TPUT`/`TGET` 搬，完成通知用 `TNOTIFY`（远端 int32 信号量，AtomicAdd 计数）+ `TWAIT`（自旋等待本地信号）。本讲的 combine 归还阶段就是这套原语的完整落地。
- **事件驱动的 ping-pong 双缓冲**（u2-l3、u6-l2）：`(srcPipe, dstPipe, eventId)` 三元组配对，`set_flag`/`wait_flag` 让 MTE2（TLOAD）与 MTE3（TSTORE）重叠。本讲会看到它被推广到「跨 remote rank 连续流水」。
- **TPipe 环形 FIFO**（u3-l2）：`TALLOC → TSTORE → TPUSH` 与 `TPOP → TLOAD → TFREE` 的生产者-消费者协议，同步按 `SyncPeriod` 稀疏进行。本讲 4.3 正是从它的 credit 账本讲起。
- **GM 环形 FIFO 衔接 Cube/Vector**（u8-l1）：Flash Attention 用三条 GM FIFO 串起四阶段跨核流水；mega 融合算子把同样的思想推广到跨卡。

本讲新增的领域概念：

- **MoE / EP / topK / gate**：混合专家模型把 FFN 拆成多个专家，router 给每个 token 打分选出 `topK` 个专家（`probs` 是归一化的门控权重，`expertId` 是专家编号）。EP（Expert Parallelism）把专家分片到多张卡，于是 token 必须离开本卡去专家所在的卡，算完再回来——这就是 dispatch/combine 存在的原因。
- **token 重排（reorder / permute）**：为让专家做连续的批量矩阵乘，散落的 token 要按「目标专家」分组重排成 expert-major 的连续行；combine 之后还要按 `expandedRowIdx` 逆映射恢复原始 token 顺序。
- **量化 token 的交织行格式**：`moe_dispatch` 搬运的是 per-token 量化的行 `[int8 × K][32B padding，scale 在 offset 0]`，dispatch 拆包成紧凑的 `gmA`（int8 token）与 `gmPerTokenScale`（float scale）两块输出。

> 运行环境说明：本讲三个算子都是**真机算子**（A2/A3 + HCCL RDMA 窗口 + MPI 多进程），仓库的 CPU 仿真路径不包含它们。因此本讲的实践以「源码阅读 + 纸面推演」为主，涉及 `run.sh` 的运行步骤均标注「待本地真机验证」。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [kernels/manual/a2a3/moe_dispatch/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md) | 去程算子总览：三条路径（Direct/ViaGM/WithSync）的数据流与算法伪码 |
| [kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp) | 去程 kernel 主体：跨 rank ping-pong 拉取 + 设备侧路由表计算 |
| [kernels/manual/a2a3/moe_dispatch/moe_dispatch_config.h](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_config.h) | 形状常量、`DispatchTraits` 自适应分块、路由表 workspace 布局、`DATA_AS_FLAG_OFFSET` |
| [kernels/manual/a2a3/moe_combine/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/README.md) | 回程算子总览：`routeMeta` 显式路由契约、`peerWindow` 布局、三阶段流程 |
| [kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp) | 回程 kernel 主体：TPUT 归还 + TNOTIFY/TWAIT + TAXPY 加权恢复 |
| [kernels/manual/a2a3/dispatch_mega_combine/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/README.md) | 融合算子总览：七段流水、专家级 overlap、count-as-flag、HCCL 窗口布局 |
| [kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h) | 融合算子设备侧总调度：按核角色与 stageNum 分派各段 |
| [kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch.h](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch.h) | 融合版 dispatch：远端拉取拆包 + pending chunk 结算 + GMM1 就绪通知 |
| [kernels/manual/a2a3/dispatch_mega_combine/op_kernel/combine.h](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/combine.h) | 融合版 combine：反量化远端写回 + 专家进度发布 + count-as-flag 重置 |
| [include/pto/npu/a2a3/TPush.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TPush.hpp) | TPipe 实现：`countPendingFreeCredits` credit 账本与析构 drain（HEAD 修复点） |
| [tests/npu/a2a3/src/st/testcase/tpushpop_cv_nosplit/tpushpop_cv_nosplit_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/tpushpop_cv_nosplit/tpushpop_cv_nosplit_kernel.cpp) | pending-credit 修复的回归用例：编译期断言 drain 数量 |

## 4. 核心概念与源码讲解

### 4.1 MoE 流程：token 的一次完整旅行

#### 4.1.1 概念说明

一个 EP 域内有 \( R \) 张卡（rank），每卡持有 \( E \) 个本地专家，全局专家数为 \( R \times E \)。每个 token 经 router 得到 `topK` 个 `(expertId, prob)` 路由。因为专家分散在各卡，MoE 前向被切成五段：

1. **FrontReorder（重排打包）**：源卡把本卡 token 按「目标全局专家」分组排序，量化后写成 expert-major 的连续行（交织格式 `[int8×K][32B scale]`），放进自己的 HCCL 窗口。
2. **Dispatch（分发）**：目的卡按路由表从所有源卡窗口**拉取**（pull）属于自己的行，拆包成 `gmA`（int8）+ `perTokenScale`（float）喂给本地专家。
3. **专家计算**：分组 GEMM（GMM1 → SwiGLU 激活+动态量化 → GMM2）。
4. **Combine（归还）**：专家输出行经 `TPUT` 远端写回 token 属主卡的窗口 `ptrD`，用 `TNOTIFY/TWAIT` 做完成同步。
5. **Unpermute / Restore（恢复）**：属主卡按 `expandedRowIdx` 找回每条路由对应的行，加权求和恢复 `outputC[token] = Σ prob × 行`。

为什么 dispatch 选择 **pull（目的卡拉）** 而不是 push？因为每张卡最清楚自己要哪些行、以及这些行在自己 `gmA` 里的落点（由路由表算出），拉取方天然掌握目的地址；push 模式则要求源卡知道全网的布局细节。注意 `moe_combine` 的归还阶段用的是 **push（`TPUT` 远写）**——此时行归属由 `expandedRowIdx` 显式给出，且写回目的地是「属主卡的 ptrD」，push 更直接。方向的选择由「谁掌握寻址信息」决定。

#### 4.1.2 核心流程

三个算子覆盖的链路（PTO 指令类别标注在右侧）：

```text
                    ┌──────────────── 本卡（源 rank） ───────────────┐
 x[M,K] ─router─▶ expertId[M,topK], probs[M,topK]
    │
    ├─ FrontReorder ──▶ offsetA[行, K+32]（HCCL 窗口，交织行）      【排序: TMRGSORT/TSORT32；量化: TQUANT 风格】
    │                   + tokenPerExpert / cumsumMM / preSumBeforeRank
    │                   + expandedRowIdx（路由→行 逆映射）
    ▼
 ══════════ 跨卡边界（对端窗口 = 对端窗口基址 + 本地偏移） ══════════
    │
    ├─ Dispatch（目的卡 pull）                                        【TLOAD / TGET + TSTORE 拆包】
    │     for 本地专家 g, for 源卡 s:
    │        rows      = tokenPerExpert[s, 全局专家(g)]
    │        srcRowBase = preSumBeforeRank[s, g]        （源侧偏移）
    │        dstRowBase = groupBase + cumsumMM[s-1, g]  （目的侧偏移）
    │        远端 offsetA[srcRowBase : +rows] → 本地 gmA[dstRowBase : +rows] + perTokenScale
    ▼
    ├─ GMM1 → SwiGLU(动态量化) → GMM2                                 【TMATMUL / TAXPY / TCVT / TQUANT】
    ▼
    ├─ Combine（push 归还）                                            【TPUT 远写 + TNOTIFY/TWAIT】
    │     专家输出行 → 属主卡 remoteWindow.ptrD[dstRow]
    ▼
    └─ Unpermute / Restore（属主卡）                                  【TLOAD + TAXPY(prob) + TSTORE】
          outputC[t,:] = Σ_valid slot  probs[t,slot] × ptrD[expandedRowIdx[t*topK+slot], :]
```

关键认识：**dispatch 与 combine 都不是一条集合通信指令，而是「路由表驱动的变长分段拷贝 + 信号握手」**。`moe_dispatch` 与 `moe_combine` 分别是去程、回程的独立验证算子；`dispatch_mega_combine` 把整条链（含专家计算）融合进一个 kernel。

#### 4.1.3 源码精读

**路由表：整个 MoE 通信的寻址核心。** 去、回两个 README 都围绕它定义契约。回程算子的 README 给出五张表的完整语义：

- [moe_combine/README.md:150-160](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/README.md#L150-L160) 定义 `routeMeta` 布局：`peerTokenPerExpert[ep, expertNumPadded]`（源卡 s 发给全局专家 e 的行数）、`cumsumPerExpert`（按全局专家的包含前缀和）、`dispatchOffset[expertPerRank]`（本地专家在本卡 expertOutput 中的起始行）、`prevSumBeforeRank[ep, expertPerRank]`（源卡 s 在本地专家 g 行内的偏移）、`expandedRowIdx[M*topK]`（路由 → `ptrD` 行的逆映射，-1 表示无效路由）。
- [moe_dispatch/README.md:46-54](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md#L46-L54) 的算法伪码写出目的卡侧同构的三张表：`cumsumMM`（源卡维度的前缀和）、`tokenPerExpert`、`preSumBeforeRank`——README 明确说明这组参数「与 MegaMoE 完全匹配」（[moe_dispatch/README.md:165-169](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md#L165-L169)）。

**三张表如何换算出一次拷贝？** 这正是融合算子 `dispatch.h` 里 `FetchRankGroupRows` 干的事（详见 4.2.3），公式与 `moe_dispatch` kernel 中完全一致：

\[ \text{rows} = \text{TPE}[s,\; rE+g], \quad \text{srcRowBase} = \text{PSBR}[s, g], \quad \text{dstRowBase} = G + \text{cumsumMM}[s-1, g] \]

其中 \( G \) 是此前本地专家累计的 `prevGroupSum`。**对称性**：把公式里的「源/目的」角色互换，就是 combine 归还段（`moe_combine` 的 `LoadReturnSegment`，见 4.2.3）的地址计算——两张路由表是同一份元数据在去程/回程两个方向的投影。

**恢复公式**（combine 的最终输出）：[moe_combine/README.md:62-70](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/README.md#L62-L70) 给出逐 token 的加权恢复伪码：`outputC[t,c]` 初始化为 0，对每条有效路由 `slot` 累加 `probs[t*topK+slot] * ptrD[expandedRowIdx[t*topK+slot], c]`。这就是一个以 `prob` 为系数的 AXPY 序列。

**融合算子的七段流水**：[dispatch_mega_combine/README.md:7-13](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/README.md#L7-L13) 概括主链路 `FrontReorder -> Dispatch -> GMM1 -> SwiGLU -> GMM2 -> Combine -> Unpermute`，功能上等价于：

\[ \text{out}[t] = \sum_{k=0}^{\text{topK}-1} \text{probs}[t,k] \cdot \text{FFN}_{e(t,k)}(x[t]) \]

其中 FFN 为 int8 GMM1 + SwiGLU + int8 GMM2（[dispatch_mega_combine/README.md:74-81](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/README.md#L74-L81)）。

#### 4.1.4 代码实践（源码阅读 + 纸面推演）

1. **实践目标**：用一组最小数字手工验证路由表的地址换算，确认你真的理解了三张表的语义。
2. **操作步骤**：
   - 阅读两份 README 的 Data Flow / Algorithm 小节（[moe_dispatch/README.md:20-72](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md#L20-L72)、[moe_combine/README.md:50-74](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/README.md#L50-L74)）。
   - 设定 \( R=2 \) 卡、\( E=1 \) 个本地专家/卡（全局专家 0、1）、`topK=2`。假设 `tokenPerExpert`（按 `[srcRank, globalExpert]`）为：卡 0 有 3 行发给专家 0、1 行发给专家 1；卡 1 有 0 行发给专家 0、2 行发给专家 1。
   - 站在 **卡 0（rank=0，本地专家 g=0 即全局专家 0）** 的视角，按 4.1.3 的公式推演：它要从卡 0 本地拉几行？从卡 1 拉几行？`cumsumMM[0, 0]` 应是多少？（提示：`cumsumMM` 是**目的卡**对自己本地专家、按源卡维度累加的前缀和。）
3. **需要观察的现象**：推演得到的 `rows` 序列应与 `tokenPerExpert` 第 0 列一致（3 行、0 行），`dstRowBase` 依次为 0、3——即前缀和把变长段拼成紧凑输出。
4. **预期结果**：`rows = (3, 0)`，`cumsumMM[0,0] = 3`（卡 0 贡献的前缀），`preSumBeforeRank[0,0] = 0`（卡 0 之前没有任何卡发给专家 0，它的行排最前）。若你换成站在卡 1 的视角（全局专家 1），对应答案应为 `rows = (1, 2)`、`cumsumMM[0,0] = 1`——同一张表在不同目的卡眼里投影不同，这正是路由表「按目的侧解释」的含义。
5. 本实践为纸面推演，无需真机；若想实际运行对照，可用 `bash run.sh all --ep 2 --mode sync`（moe_dispatch 目录，见 [README.md:111-138](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md#L111-L138)），**待本地真机验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 dispatch 用 pull、combine 用 push？如果反过来各会付出什么代价？

答案：dispatch 时目的卡掌握「我要哪些行、放到我 gmA 的哪一行」（路由表在目的侧换算目的地址），pull 天然匹配；若改 push，源卡需要理解全网每张卡的目的布局，路由表的对称投影变单向广播。combine 时行归属由 `expandedRowIdx` 直接给出、目的地是属主卡固定的 `ptrD` 窗口，push（`TPUT` 远写）一步到位；若改 pull，属主卡要按专家分组反向遍历路由表，寻址逻辑更绕且难以整行连续搬运。

**练习 2**：`expandedRowIdx[t*topK+slot] = -1` 表示什么？为什么恢复循环必须跳过它？

答案：表示 token t 的第 slot 条路由无效（例如该路由被丢弃或去重）。恢复是加权和，若把 -1 当行号会读到 `ptrD` 前一个位置的内存（未定义行为），因此 `LoadRestoreRoute` 返回 false 后 `continue` 跳过该条（见 4.2.3 的 `AccumulateRestoreTile`）。

**练习 3**：`gmA` 的行数上限 `maxOutputSize` 由什么决定？dispatch 循环里哪里体现这个上限？

答案：由 workspace 容量决定（`moe_dispatch` 默认 512；mega 算子典型用例固定 81940）。体现为 kernel 中 `if (rowStart + rows > maxOutputSize) rows = maxOutputSize - rowStart;` 的截断（[moe_dispatch_kernel.cpp:126-128](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L126-L128)），以及 `rowStart >= maxOutputSize` 时整段跳过。

### 4.2 token 重排：dispatch 拉取拆包与 combine 归还恢复

#### 4.2.1 概念说明

token 重排要解决两个具体问题：

1. **交织行的拆包（split）**：源卡窗口里的行是 `[int8 token × K][32B padding，float scale 在 offset 0]` 的交织格式（行跨度 `copyInNum = K + 32`，见 [moe_dispatch_kernel.cpp:73](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L73)），因为它是 FrontReorder 一次写出的。但专家计算要的是**紧凑**的 int8 矩阵 `gmA[rows, K]` 加一列 scale——GMM 的 A 操作数不能容忍每行拖着 32B 垃圾。dispatch 在搬运途中顺手完成拆包：一个交织 tile 进 UB，用两个视图（`tokenView` 取前 K 列、`scaleView` 取第 K 列起的 32B）分别 TSTORE 到两块 GM。
2. **变长段的地址换算与乒乓流水**：每个 `(源卡, 本地专家)` 段长度 `rows` 都不一样，段内按 `MOVE_NUM` 行一块切tile，块与块之间用 ping/pong 双 UB 缓冲把「装下一块」（MTE2）与「存上一块」（MTE3）重叠起来。

`MOVE_NUM` 不是拍脑袋定的：[moe_dispatch_config.h:57-62](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_config.h#L57-L62) 的 `DispatchTraits<TILE_COLS>` 按「UB 半区 96 KiB / 每行字节数」自动收缩行数、上限 16——`hiddenSize` 越大，单块行数越少，保证乒乓两区装得下。

combine 侧的重排是逆过程：专家输出按 `[src_rank × local_expert]` 段布局在 `expertOutput`，要归还到属主卡 `ptrD` 中**按 token 路由展开**的行位（`dstStart` 来自 `cumsumPerExpert`），最后再用 `expandedRowIdx` 把 `topK` 条路由行加权折叠回 token 序。

#### 4.2.2 核心流程

**dispatch 的三条路径**（[moe_dispatch/README.md:9-13](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md#L9-L13)）：

```text
Direct（2 步，快路径）:
  远端 GM ──TLOAD──▶ UB(ping/pong) ──TSTORE──▶ gmA + gmPerTokenScale
  事件乒乓：TLOAD(N+1) 与 TSTORE(N) 重叠；乒乓状态跨源卡延续

ViaGM（4 步，MegaMoE 兼容路径）:
  远端 GM ──TGET──▶ 本地临时 GM ──TLOAD──▶ UB ──TSTORE──▶ gmA + scale
  （TGET 自带 ping/pong staging，先落一次本地 GM）

WithSync（自包含路径，三阶段）:
  Phase A  TPE AllGather：本卡 tokenPerExpert + DataAsFlag 偏移，TSTORE 远写所有对端
  Phase B  TWAIT 等全部对端数据到达 → core0 设备端算路由表
           B.1 TADDS 剥掉 flag 偏移   B.2 TLOAD/TADD/TSTORE 向量化前缀和得 cumsumMM
           B.3 标量循环算 preSumBeforeRank（带 dcci 刷缓存）
  Phase C  SYNCALL(Soft) 后按 Direct 路径分发
```

DataAsFlag 是 Phase A 的巧思：给计数统一加上 `0x800000` 偏移（[moe_dispatch_config.h:71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_config.h#L71)），于是「首元素非 0」就同时意味着「数据已到达」，`TWAIT(≠0)` 一条指令既等数据又不用额外信号区——数据本身即 flag。

**combine 的三阶段**（[moe_combine_kernel.cpp:605-630](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp#L605-L630)）：

```text
1. ReturnExpertRowsToOwners   遍历 (src_rank × local_expert) 段，行块按 (全局块号 % AIV块数) 分片
   ├─ src == myRank：本地拷贝 TLOAD→TSTORE 逐行逐 1024 列 tile 进本卡 ptrD
   └─ src != myRank：TPUT(远端 ptrD 段, 本地 expertOutput 段, ping, pong) 远写
   SoftSyncAiv 栅栏 → TNOTIFY(对端 combineDoneSignal[myRank], 1, AtomicAdd) 逐个通知属主
2. WaitCombinePhase   TWAIT(本卡 combineDoneSignal[peer] >= 1) 逐个等所有属主卡写完
   SoftSyncAiv → 3
3. RestoreOutputRows  token 按 AIV 分片；逐 token:
   dcci 逐行 + 一次 dsb(DSB_DDR) 刷新 ptrD → 逐 1024 列 tile:
      TEXPANDS(outTile, 0) → [TLOAD(ptrTile, ptrD 行) → TAXPY(outTile, ptrTile, prob)]×有效路由 → TSTORE(outputC)
```

#### 4.2.3 源码精读

**(a) 远端指针翻译**——一切跨卡寻址的起点：[moe_dispatch_kernel.cpp:46-52](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L46-L52)。`CommRemotePtr` 取本卡窗口基址 `windowsIn[myRank]`，算出本地指针相对偏移，再加到对端基址 `windowsIn[pe]` 上。**同一个偏移在两张卡上语义一致**，这就是 u7-l2 讲过的窗口机制。`moe_combine` 侧的同名函数在 [moe_combine_kernel.cpp:161-167](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp#L161-L167)。

**(b) Direct 路径的乒乓骨架**：[moe_dispatch_kernel.cpp:85-97](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L85-L97) 在 UB 上摆两个乒乓区，每个区用三个视图共享同一块物理 tile：`interleavedPing`（整行 K+32）、`tokenViewPing`（前 K 列）、`scaleViewPing`（偏移 K 处 32B）。注意 `TASSIGN` 都以 `PING_OFFSET`/`PONG_OFFSET` 为基——**拆包不搬额外数据，只是同一 UB 区域的三套 shape 视图**。

**(c) 跨 rank 连续流水**——本讲最精彩的软件流水：[moe_dispatch_kernel.cpp:142-205](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L142-L205) 是内层块循环：

- `curPP = globalChunkIdx & 1`：乒乓序号来自**全局块计数器**，不随源卡切换归零——最后一个 rank 的尾块与下一个 rank 的首块依然错开半拍，流水不断流（README 称之为 *cross-rank continuous pipeline*，[moe_dispatch/README.md:82](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md#L82)）。
- `hasPending` 分支（[L163-191](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L163-L191)）是标准乒乓三步：① `wait_flag(MTE2→MTE3, prevEvent)` 等上一块**装完**；② 发出 `TSTORE`（拆包写 token 视图 + scale 视图）与下一块 `TLOAD`；③ `set_flag(MTE3→MTE2, prevEvent)` 挂「上一块已存完」的牌，再 `wait_flag(MTE3→MTE2, prevEvent)` 等它生效——保证同一 UB 区在 MTE3 读走数据前不被下一次 MTE2 复写。
- 但这个循环**故意留下一个尾巴不存**：只把 `pendingPP / pendingRows / pendTokenDstPtr` 记进状态（[L197-204](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L197-L204)），本块 TLOAD 完成牌挂上后立刻进入下一块——这样下一轮迭代才能「先 TLOAD 再 TSTORE」维持重叠。
- **结算点**：外层每个 `groupIdx`（本地专家）结束时，[L208-234](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L208-L234) 把最后一块 pending TSTORE 掉并等 MTE3 完成。**如果漏掉这段 drain，最后一块数据静静躺在 UB 里永远不会写回 GM**。这正是 4.3 要展开的「pending 结算」模式的第一实例。

**(d) ViaGM 与 TGET**：[moe_dispatch_kernel.cpp:313-321](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L313-L321) 先用 `pto::comm::TGET(tempDstG, remoteSrcG, tgetPing, tgetPong)` 把整段交织行原样搬到本地临时 GM，再在其上跑与 Direct 相同的拆包乒乓。多一次 GM 中转、多一倍 GM 流量，但接口与 MegaMoE 的 `DispatchCopyPerToken` 对齐（[moe_dispatch/README.md:167-169](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md#L167-L169)）。

**(e) 设备侧路由表计算**：WithSync 的 Phase A/B 在 [moe_dispatch_kernel.cpp:486-655](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L486-L655)。三处值得细读：

- Phase A 的事件链（[L502-525](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L502-L525)）：`TLOAD` 本卡 TPE 行 → `TADDS(+DATA_AS_FLAG_OFFSET)` 加 flag → 按核跨步 TSTORE 到每个对端窗口的 `row[myRank]`，每次 TSTORE 前后都有 MTE3↔MTE2 事件保 UB tile 复用安全。
- Phase B 等待（[L541-547](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L541-L547)）：每核负责 `TWAIT(对端行首元素 ≠ 0)`，DataAsFlag 在这里兑现。
- Phase B.2 的向量化前缀和（[L611-633](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L611-L633)）：`accumTile` 累加器常驻 UB，逐源卡 `TLOAD` 一行 → `TADD(accum, tmp)` → `TSTORE` 出一行 cumsumMM。注意整行按 8 对齐的 `paddedExpNum` 宽度搬运（DMA 对齐，[moe_dispatch_config.h:73-78](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_config.h#L73-L78)）。

**(f) combine 的段装载与远写**：[moe_combine_kernel.cpp:341-360](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp#L341-L360) `LoadReturnSegment` 是 4.1.3 公式的回程版：`srcStart = dispatchOffset[localExpert] + prevSumBeforeRank[src, localExpert]`（本地 expertOutput 侧），`dstStart = cumsumPerExpert[src, globalExpert-1]`（属主卡 ptrD 侧，全局专家 0 时为 0）。远写走 [moe_combine_kernel.cpp:407-427](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp#L407-L427) 的 `TPUT(remoteDst, localSrc, ping, pong)`——u7-l3 精读过的集合通信原语，这里是它作为点对点变长归还的用法。行块按 `(全局块序 % blockNum)` 分给 AIV 核（[L471-483](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp#L471-L483)），负载天然均衡。

**(g) 信号收尾与加权恢复**：归还完成后 [moe_combine_kernel.cpp:438-446](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp#L438-L446) `TNOTIFY(sig, 1, AtomicAdd)` 对每个属主卡的 `combineDoneSignal[myRank]` 槽原子 +1；[L263-273](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp#L263-L273) `WaitCombinePhase` 用 `TWAIT(≥1)` 等齐。恢复段两条防线值得抄下来：

- **缓存一致性防线**：远端 `TPUT` 写入本卡窗口后，本核 cache 里可能是旧值。[moe_combine_kernel.cpp:494-520](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp#L494-L520) `PrepareRestoreRouteReads` 对每条路由的 ptrD 行逐 cache line `dcci`，一批路由只做一次 `dsb(DSB_DDR)`——即 README 所述 *DCCI batched acquire*。
- **TLOAD→TAXPY 事件链**：[moe_combine_kernel.cpp:533-558](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/moe_combine_kernel.cpp#L533-L558) 用两个 `pto::Event` 对象（`loadToAxpy` 与 `axpyToNextLoad`）把 topK 条路由的 `TLOAD`/`TAXPY` 串成软件流水：上一条 TAXPY 没算完前，下一条 TLOAD 已经可以发起（不同 UB tile）。u2-l3 讲的 `Event<Op,Op>` 对象风格在这里派上正式用场。

#### 4.2.4 代码实践（源码阅读型：数事件、验配对）

1. **实践目标**：验证 Direct 路径乒乓循环中的事件配对闭环，确认无泄漏、无缺等待。
2. **操作步骤**：
   - 精读 [moe_dispatch_kernel.cpp:142-234](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L142-L234)，建一张四列表格：`set_flag(源流,目的流,事件号)`、对应 `wait_flag`、保护的 UB 区（PING/PONG）、语义（装完/存完）。
   - 追踪一条具体执行：某核负责 2 个源卡、共 5 个块（3+2）。按 `globalChunkIdx = 0..4` 写出每轮发起的 TLOAD/TSTORE 与事件号，标出第 5 块何时被 drain。
3. **需要观察的现象**：每个事件号在每个「半区」上，set 与 wait 严格一一对应；最后一个块的 TSTORE 只出现在 group 结尾的 drain 段；乒乓序号在两个源卡之间连续（0,1,2,3,4 而非每卡重新从 0 开始）。
4. **预期结果**：5 块共 5 次 TLOAD；拆包写（每轮 token 视图 + scale 视图共两条 TSTORE）只发生在 4 个块上——循环内的 `hasPending` 分支写第 0~3 块、group 末尾的 drain 段补写第 4 块；装载侧 EVENT_ID0 用于第 0/2/4 块、EVENT_ID1 用于第 1/3 块，各自的 set/wait 配对完整。CPU 仿真下这些事件均为空桩、无法验证时序，**同步正确性属真机命题，待本地真机验证**。
5. 若有真机，可用 `bash run.sh all --ep 2 --mode direct --debug`（[moe_dispatch/README.md:130-137](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md#L130-L137)）跑对照，**待本地真机验证**。

#### 4.2.5 小练习与答案

**练习 1**：Direct 与 ViaGM 各多一次什么？为什么 Direct 更快？

答案：ViaGM 在「远端 GM → UB → 本地 GM」之外还要再「本地 GM → UB → 拆包写 GM」，即数据两次穿过 UB、GM 流量翻倍；Direct 用「远端 GM → UB（乒乓）→ 拆包直写」一步到位，省掉中间 GM 缓冲的读写（README：*PTO-ISA optimization that bypasses intermediate GM buffer*，[moe_dispatch/README.md:169](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md#L169)）。

**练习 2**：`tokenViewPing` 与 `scaleViewPing` 为什么能 `TASSIGN` 到同一 UB 偏移（前者 `PING_OFFSET`、后者 `PING_OFFSET + HIDDEN_SIZE`）而互不干扰？

答案：它们是对**同一块物理 UB 数据**的两套 tile 视图：TLOAD 把整行 `K+32` 字节装进 `interleavedPing`；`tokenViewPing` 以列 `[0, K)` 解释前段，`scaleViewPing` 以列 `[K, K+32)` 解释后段。TSTORE 按各自有效区读走，本质是「装载一次、按两个窗口分别写回」，没有额外拷贝（[moe_dispatch_kernel.cpp:85-97](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/moe_dispatch_kernel.cpp#L85-L97)）。

**练习 3**：`RestoreOutputRows` 为什么不直接 `TLOAD` 整行 ptrD 后相加，而要维护 `RestoreRouteCache` 并配合 dcci/dsb？

答案：两个原因。一是正确性：ptrD 是远端 TPUT 刚写入的窗口内存，必须 `dcci` 失效本地 cache 行、`dsb(DSB_DDR)` 等内存序生效后才能读到新值（u7-l5 讲过的「数据先于信号、读前先取」纪律）。二是效率：`topK ≤ 16` 时把行号与概率缓存进核内数组，内层逐列 tile 循环不再反复读 `routeMeta`（README：*route cache for restore*，[moe_combine/README.md:174-177](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/README.md#L174-L177)）。

### 4.3 融合与同步加固：dispatch_mega_combine 与 pending-credit

#### 4.3.1 概念说明

**为什么要融合？** 把 `moe_dispatch`、专家 GEMM、`moe_combine` 做成三个独立 kernel，每次 launch 之间数据要落 GM、整卡要栅栏，通信与计算只能串行。`dispatch_mega_combine` 把七段装进**一个 kernel**：AIV 核跑通信类段（Dispatch/SwiGLU/Combine），AIC 核跑计算段（GMM1/GMM2），两族核按**本地专家粒度**互推进度——第 i 个专家的行一凑齐，GMM1 就能开算，GMM2 一出结果 Combine 就能写回。这就是 u7-l5「把同步粒度从矩阵级细化到 tile/subtile 级」思想的专家级版本（README：*Expert-level overlap*，[dispatch_mega_combine/README.md:96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/README.md#L96)）。

**什么是「pending-credit 一类跨 dispatch 状态同步问题」？** 三个算子里反复出现同一类难题：为了让流水不断，同步都是**稀疏**的（每隔 SyncPeriod 一次、每块挂一次牌、计数带 flag 发布）；稀疏就意味着总有一笔「已经产生、但还没被消费」的账挂在那里。这笔账跨越了逻辑段的边界——跨过源卡边界、跨过 kernel 生命周期边界、跨过一次 dispatch 进入下一次 dispatch——如果边界处的**结算（drain/结算/重置）不精确**，轻则丢尾块数据，重则残留 stale 状态让下一次 dispatch 死锁。本讲给出三个实例：

| 实例 | 账本 | 结算点 | 结算错误后果 |
| --- | --- | --- | --- |
| dispatch 尾块 | `hasPending / pendingPP / pendingRows / pendDst` 指针 | 每个 `groupIdx` 末尾 drain（4.2.3(c)） | 最后一块永不写回 GM |
| TPipe free credit | `TFREE` 已通知但 `TPUSH` 尚未消费的 free credit 数 | TPipe 析构 `countPendingFreeCredits` 精确 drain | 连续多次 dispatch 残留 stale flag → `507018` / `S1:running-stalled` 死锁 |
| count-as-flag 计数 | `tokenPerExpert` 行里的 `+0x800000` 标记值 | Combine 边界 `ResetTokenPerExpertByOwner` 清零 + epoch | 下一轮 dispatch 读到旧计数，等待条件永假 |

#### 4.3.2 核心流程

**融合算子的角色分派**（stageNum 是 launch 时随 tiling 下发的段号闸门）：

```text
stageNum:   9        10~12            11         12~13         13        14
AIV(48核)  Dispatch  (SwiGLU 16)      SwiGLU     Combine 8  →  Unpermute 两波(32→48)
AIC(24核)            GMM1 24→16 释放  —          GMM2 8→24 扩  —
同步:      dispatchArrival→gmm1Ready  gmm1Done(epoch)  gmm2Arrival→combineReady  ExpertProgress→DataReady
```

每条边界都是「生产者发布 arrival、协调者聚合后发布 ready」的 epoch 协议；跨卡的 Combine 进度则进 HCCL 窗口的 `ExpertProgress / DataReady` 槽，Unpermute 据此分两波开工（先处理已就绪路由的 token，Combine 齐了再全量 48 核）。

**TPipe 的 credit 账本**（修复后的语义）：设槽深 `SlotNum = N`，同步周期 `SyncPeriod S = N/2`（N>2 时），共传输 `T` 次：

\[ \text{notified} = \left\lfloor \frac{T}{S} \right\rfloor \quad(\text{TFREE 每 } S \text{ 次通知一次}) \]

\[ \text{waited} = \frac{\text{lastWait} - \text{firstWait}}{S} + 1, \quad \text{firstWait} = N,\ \text{lastWait} = \left\lfloor \frac{T-1}{S} \right\rfloor \cdot S \]

析构时应 drain 的 credit 数即两者之差。**关键在「精确」**：修复前的实现固定 drain `S` 次——传得少时等一个永远不会来的通知（卡死），传得多时残留 stale flag 给下一次 dispatch 埋雷。

#### 4.3.3 源码精读

**(a) 总调度：一个函数分派七段**。[dispatch_mega_combine.h:125-165](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L125-L165) `ProcessFixedGroups` 按 `FixedCoreRole(tilingData)` 返回的核角色（Dispatch/Gmm1/Gmm2/Swiglu/Combine…）与 `stageNum` 闸门各段入场：`stageNum >= 9` 跑 Dispatch（[L132-136](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L132-L136)），`>= 10` GMM1、`>= 12` GMM2 动态加入（[L137-155](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L137-L155)），`>= 11` SwiGLU、`>= 13` Combine。GMM1 组收尾时 `pipe_barrier + dsb` 后发布 `gmm1Done` 的 epoch 标记（[L140-146](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L140-L146)）——先数据后信号的发布纪律与 u7-l5 一致。

**(b) 融合版 dispatch：pending chunk 的「批量结算」变体**。[dispatch.h:262-301](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch.h#L262-L301) `FetchRemoteDispatchedRows` 与 4.2.3(c) 同构，但粒度更大：每块 2 行交织行（tile 静态容量 8192B/行，运行期按 `RowMaskInternal`/构造参数裁到实际行宽 `K+32`），两个 UB 区分别起始于 0 与 96 KiB（[dispatch.h:30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch.h#L30)），事件用 `EVENT_ID2/ID3`。它把「装完等牌 + 拆包存 + 挂存完牌」封装进 [dispatch.h:251-260](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch.h#L251-L260) `StorePendingDispatchGatherChunk`；拆包也有优化——`K` 是 4 的倍数且行跨度匹配时，payload 直接按 `uint32` 宽 tile 一条 TSTORE 整行写走（[L193-210](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch.h#L193-L210)）。结算点在 [dispatch.h:303-331](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/dispatch.h#L303-L331) `ProcessRankSplitCopy`：每个本地专家的拉取循环结束后 `WaitDispatchGatherCopyEvents()` 把两个缓冲区的存完牌都等齐，随后调用 `NotifyGroupConsumersMte` 发布该专家的 arrival 并聚合发布 GMM1 消费者的 ready——**结算与「通知下游」被绑在同一个边界上**，这就是专家级 overlap 的胶水。

**(c) 融合版 combine：单行五步流水 + 进度发布**。[combine.h:353-407](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/combine.h#L353-L407) `ProcessDirectLargeSegmentRows` 逐行执行 `TLOAD(gmm2Output 行) → TCVT 转 fp32 → TMULS 乘 perTokenScale2 反量化 → TCVT 转半精度 → TSTORE 到远端 offsetD`，五步之间用按 bufferId 的 Load/Store 事件乒乓。段完成后经 [combine.h:276-285](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/combine.h#L276-L285) 按属主 rank 发布 `PublishExpertProgress`，全部专家可见后发布 `PublishDataReady`（[L287-314](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/combine.h#L287-L314)）。

**(d) count-as-flag 的结算与 epoch**。FrontReorder 发布计数行时带标记值，对端可直接等数据到达而无需额外计数交换栅栏（README：*Count-as-flag*，[dispatch_mega_combine/README.md:98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/README.md#L98)）。但计数行是**复用**的——下一轮 dispatch 还要写同一块窗口。结算在 [combine.h:250-274](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/combine.h#L250-L274)：`ProcessFixedFinalBoundary` 里最后一个 AIV 执行 `ResetTokenPerExpertByOwner`——UB 填 0 → `pipe_barrier` → TSTORE 覆写计数行 → `pipe_barrier` → `dsb(DSB_DDR)` 生效。同时跨卡信号改用 **epoch（纪元）算术**：`dataReadyEpoch_` 区分轮次，等待条件形如「值 ≥ 本轮期望」，旧值不会误触发（对照 u6-l1 Soft SyncAll 的 `target=(⌊before/N⌋+1)·N`）。对照 `moe_combine` 的做法——由 **host 在每次迭代前清零** `combineDoneSignal`（[moe_combine/README.md:261-262](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/README.md#L261-L262)）——融合算子把重置也搬进了 kernel，代价换收益：少一次 host 干预、多一份设备侧自洽。

**(e) TPipe pending-credit 的精确结算（HEAD 修复点）**。[TPush.hpp:37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TPush.hpp#L37) 定义 `SyncPeriod = SlotNum/2`；[L56-75](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TPush.hpp#L56-L75) 的 `shouldWaitFree/shouldNotifyFree` 说明同步为何稀疏：生产者只在 `tileIndex % S == 0` 时等空间，消费者只在 `(tileIndex+1) % S == 0` 时通知释放。于是账本函数 [TPush.hpp:77-95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TPush.hpp#L77-L95) `countPendingFreeCredits(tileCount)` 按上节公式计算「已通知 − 已等待」，析构函数 [TPush.hpp:511-519](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TPush.hpp#L511-L519) 只 drain 这么多次 `prod.allocate()`。提交信息（HEAD `8aacb8e0`）记录了这次加固的动机：旧实现「固定 drain SyncPeriod 次」在 TPipe 被反复构造/析构（连续 80 次 dispatch）时残留 stale credit，设备症状为 `507018`、`S1:running-stalled`。回归防线是编译期断言 [tpushpop_cv_nosplit_kernel.cpp:79-83](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/tpushpop_cv_nosplit/tpushpop_cv_nosplit_kernel.cpp#L79-L83)：深度 8、40 次传输时 drain 必须恰为 2。**注意这是 A2/A3 NPU 路径的 TPipe**（u8-l1 的 Flash Attention 正是靠 TPipe 串 Cube/Vector），它解释了为什么「每次 kernel 都对、连跑多次才挂」的 bug 会盯上 MoE 这种高频重复 launch 的算子。

#### 4.3.4 代码实践（源码阅读型：手算 credit 账本）

1. **实践目标**：亲手复算 `countPendingFreeCredits(40)`，与回归用例的编译期断言对上，确认理解稀疏同步账本。
2. **操作步骤**：
   - 读 [TPush.hpp:37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TPush.hpp#L37) 与 [TPush.hpp:77-95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TPush.hpp#L77-L95)。
   - 取 `SlotNum = 8`（故 \( S = 4 \)）、`tileCount = 40`，代入 4.3.2 的公式分步计算 `notified`、`firstWait`、`lastWait`、`waited`、drain 数。
   - 打开 [tpushpop_cv_nosplit_kernel.cpp:79-83](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/npu/a2a3/src/st/testcase/tpushpop_cv_nosplit/tpushpop_cv_nosplit_kernel.cpp#L79-L83) 核对断言值，并读该用例 `gen_data.py`（同目录）看连续 dispatch 的用例参数如何组织。
3. **需要观察的现象**：手算 drain = 2，与断言 `countPendingFreeCredits(NUM_M_TILES) == 2` 一致；再取 `tileCount = 20` 重算一遍（应得 drain = 2），体会「固定 drain S 次」在哪种数量下必然出错。
4. **预期结果**：\( \text{notified} = \lfloor 40/4 \rfloor = 10 \)，\( \text{firstWait} = 8 \)，\( \text{lastWait} = 9 \times 4 = 36 \)，\( \text{waited} = (36-8)/4 + 1 = 8 \)，**drain = 10 − 8 = 2**。固定 drain 4 次时多等的 2 次通知永远不会到来——这正是死锁点。
5. 该用例属 NPU ST（`tests/npu/a2a3`），CPU 仿真不含此路径；如需运行验证用真机 ST 流程，**待本地真机验证**。

#### 4.3.5 小练习与答案

**练习 1**：融合算子里 GMM1 为什么「先 24 核后 16 核」、GMM2 反过来「先 8 核后 24 核」？

答案：启动时 Dispatch 还在产数据、GMM2 还没有输入，AIC 应尽量先灌 GMM1（24 核）；随着第一个专家算完、SwiGLU 开始产出，GMM1 剩余专家的负载下降，释放的核加入 GMM2（8→24），让两段 GMM 的总完成时间更均衡（[dispatch_mega_combine/README.md:100](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/README.md#L100)）。加入点在专家边界处对齐（`gmm2JoinSlot` epoch 判定，见 [combine.h:316-331](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/combine.h#L316-L331) 的 `Gmm2ProducerCount`）。

**练习 2**：`moe_combine` 靠 host 清信号、`dispatch_mega_combine` 靠设备端清零 + epoch，两种方案各有什么代价？

答案：host 清零多一次 host 参与、kernel 之间要同步（且要求 host 确实知道每次迭代），但实现直白；设备端清零把生命周期闭环在 kernel 内（最后完成的 AIV 执行），配合 epoch 后无需真的清零旧信号也能区分轮次，代价是发布方必须遵守「数据先行 + pipe_barrier/dsb + 再发信号」的内存序纪律，代码更复杂（[combine.h:250-274](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/op_kernel/combine.h#L250-L274)）。

**练习 3**：为什么 pending-credit 类 bug 的典型症状是「单次运行正确、连续多次 dispatch 才挂」？

答案：单次运行内，账本虽然没结清，但 flag/credit 的残留不影响本次已经完成的传输；只有 TPipe 析构后下一次 dispatch 在同一 flag 编号上重新配对时，残留的 stale flag（或多 drain 掉的未来通知）才让新一次的 `wait_flag`/`allocate` 语义错位——表现为卡死（`S1:running-stalled`）。这类「状态跨生命周期泄漏」在 u2-l3 讲过的事件「记录一次、等待一次」纪律里是同一根源：稀疏同步的账本必须在边界精确归零。

## 5. 综合实践

**任务：画出 token 从输入到专家再回来的完整数据流图，并标注每一步的 PTO 指令类别。**

1. **准备**：通读三份 README 的数据流章节——[moe_dispatch/README.md:20-42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_dispatch/README.md#L20-L42)、[moe_combine/README.md:13-18](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/README.md#L13-L18) 与 [moe_combine/README.md:224-232](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/README.md#L224-L232)、[dispatch_mega_combine/README.md:144-165](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/dispatch_mega_combine/README.md#L144-L165)。
2. **画图**：以 2 卡为例，一张图覆盖全链路，节点用缓冲名（`x / offsetA / gmA / gmC / gmPermutedToken / gmm2Output / offsetD / outputC`），边上标指令类别，至少覆盖：重排排序类、量化类、TLOAD/TSTORE 搬运类、TGET/TPUT 跨卡类、TNOTIFY/TWAIT 信号类、TCVT/TAXPY/TMULS 计算类、SYNCALL/SoftSync 栅栏类、dcci/dsb 内存序类。
3. **标注三个结算点**：在图上用醒目记号标出 dispatch 尾块 drain、count-as-flag 重置、（若画到 TPipe 场景）析构 drain，各配一句话说明「这笔账不结会怎样」。
4. **对照检查**：把图与 4.1.2 的骨架图互查，确认没有漏掉「数据先于信号」的箭头方向；确认 `expandedRowIdx` 同时连着 FrontReorder（写）与 Unpermute（读）两个方向。
5. **可选上机**（有真机时）：分别运行 `moe_dispatch` 的 `--mode sync` 与 `moe_combine` 的小形状快验 `bash run.sh -pes 2 -M 8 -K 64 -topK 2 -expertPerPe 1 --aiv-blocks 2`（[moe_combine/README.md:310-315](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/moe_combine/README.md#L310-L315)），对照 `verify=PASS` 与图中节点一一对应。**待本地真机验证**。

## 6. 本讲小结

- MoE 通信 = **路由表驱动的变长分段拷贝 + 信号握手**：`tokenPerExpert/cumsumMM/preSumBeforeRank` 换算每段源/目的地址，`expandedRowIdx` 承担去程重排与回程恢复的逆映射。
- dispatch 选 **pull**（目的卡 TLOAD/TGET 拉取并拆包 token/scale），combine 选 **push**（TPUT 远写属主 ptrD + TNOTIFY/TWAIT 完成同步），方向由「谁掌握寻址信息」决定。
- 乒乓流水可以**跨源卡延续**（`globalChunkIdx` 不归零），代价是必须显式维护 `hasPending` 状态并在段边界 drain——尾块结算错误是静默丢数据。
- `dispatch_mega_combine` 用一个 kernel + 专家级 arrival/ready epoch 协议把七段重叠起来，AIC(24)/AIV(48) 动态分组；count-as-flag 让「数据到达」免掉一轮计数交换栅栏。
- **pending-credit 一类跨 dispatch 状态同步问题**的共同解法是「稀疏同步的账本在生命周期边界精确结算」：kernel 内 drain 尾块、TPipe 析构按 `countPendingFreeCredits` 精确 drain、count-as-flag 设备端清零 + epoch 区分轮次。
- 跨卡读回前的 `dcci + dsb(DSB_DDR)` 与「数据先行、信号在后」的发布顺序，是窗口内存正确性的两条铁律。

## 7. 下一步学习建议

本讲完成了单元八「复杂算子实战」。建议：

1. **横向对照三个融合算子**：把 u7-l5 的 `gemm_ar`（计算-通信重叠）、u8-l1 的 `flash_atten`（TPipe 串 Cube/Vector）与本讲的 mega 融合放在同一张表里比较：各自的同步粒度（tile/subtile/专家）、信号机制（GM 原子计数/TPipe credit/epoch 槽）、以及结算点位置。
2. **深挖 mega 的未读文件**：`op_kernel/front_reorder.h`（三条排序路径 FullLoad/OneCore/MultiCore 的选择逻辑）、`op_kernel/unpermute.h`（两波 Unpermute 的就绪判定）、`op_kernel/utils/mega_expert_sync.hpp`（`NotifyGroupConsumersMte/CoordinateGroupConsumersMte` 的 arrival/ready 聚合实现）。
3. **回到指令层补课**：若对 `TPUT/TGET/TNOTIFY/TWAIT` 的参数细节生疏，重读 u7-l2/u7-l3；对 `TPUSH/TPOP/TFREE` 想再确认，重读 u3-l2 与本讲 4.3 的 `TPush.hpp`。
4. **性能视角**：mega 算子的性能讨论在 `kernels/manual/a2a3/dispatch_mega_combine/overview.md`，可结合 u6-l3 的 Bound 判定方法阅读——README 提示 2048 用例 AIC 已近饱和、后续优化方向是削减 AIC/AIV 的 HBM 争用。
