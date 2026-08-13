# Kernel 层：多核并行调度

## 1. 本讲目标

本讲打开五层架构（Device > Kernel > Block > Tile > Basic）中的 **Kernel 层** 黑盒。学完后你应当能够：

- 说清 `KernelBuilder` 的角色：它在 Host 上由 `MakeScheduleConfig` 算出「多核切分方案」，在 Device 上用 `GetBlockIdx()` 让每个核领走属于自己的那一份数据。
- 手工推导 `MakeScheduleConfig` 如何把总元素数分解成 `blockNum / unitNumPerCore / moreUnitCoreNum / tailNum` 四个量。
- 解释 `blockNum` 为什么会被 `Arch::CORE_NUM`（56）「裁剪」，以及裁剪之后 `unitNumPerCore` 是如何随之变大的。
- 读懂 `CalCurCoreEleCnt` 与 `CalGMOffset`：每个核如何计算自己要处理多少个元素、自己的数据在 GM（Global Memory）里的起始偏移。

本讲承接 u2-l7（Device 层把 tiling 算好、用 `<<<blockNum>>>` 启动 `KernelCustom`），向下交给 u2-l9（Block 层把单核任务再切成 Tile）。

## 2. 前置知识

阅读本讲前，请先确认你理解以下几个来自前序讲义的概念：

- **五层架构**（u1-l3）：Device 在 Host CPU 上跑，Kernel/Block/Tile/Basic 在 NPU 的 AI Core 内跑，`KernelCustom<<<blockNum>>>` 是跨越 Host↔Device 边界的唯一跳板。
- **三级 Builder 套娃**（u1-l4）：`BlockBuilder<Compute>` → `KernelBuilder<BlockOp>` → `DeviceAdapter<KernelOp>`，本讲主角是中间的 `KernelBuilder`。
- **DeviceAdapter::Run 三步**（u2-l7）：`PrepareParams` → `CalculateTiling`（Host 侧一次性算核间 + 核内两层 tiling）→ `LaunchKernelWithDataTuple`（用 `<<<blockNum>>>` 启动核函数）。本讲把 `CalculateTiling` 里属于 Kernel 的那一半彻底讲透。
- **TileShape**（u1-l4 / u2-l5）：用 `Atvoss::Shape<int...>` 把形状编码进类型，例如 `Shape<32>` 或 `Shape<1, 4096>`，它决定了「一次 Tile 处理多少数据」。

几个本讲会用到的昇腾术语：

- **核（Core / Block）**：AI Core 内的 Vector 计算单元实例。`KernelCustom<<<blockNum>>>` 会启动 `blockNum` 个核并发执行同一段代码，每个核用 `AscendC::GetBlockIdx()` 拿到自己的编号（从 0 起）。
- **GM（Global Memory）**：Device 上的全局显存，所有核共享；每个核各自从 GM 的不同偏移处读取自己那份数据。
- **Unit（对齐单元）**：Kernel 层把总元素数切成若干「对齐块」，一块 `unitNum` 个元素。这是本讲最关键的新概念，详见 4.3。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/elewise/kernel/builder.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h) | `KernelBuilder` 模板外壳、`DefaultKernelConfig`（切分结果容器）、`DefaultKernelPolicy` / `DefaultSegmentPolicy`（策略占位）。 |
| [include/elewise/kernel/schedule.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h) | `BaseKernelSchedule`：核心算法所在——`MakeScheduleConfig`（Host 侧切分）、`Run` / `CalCurCoreEleCnt` / `CalGMOffset`（Device 侧分发）。 |
| [include/common/arch.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h) | `Arch::DAV_3510` 硬件常量：`CORE_NUM = 56`、`UB_SIZE = 240KB`。 |
| [include/elewise/device/tiling.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/tiling.h) | `CalculateTiling`：Device 层调用点，先算 Kernel 的 `kernelParam`，再算 Block 的 `blockParam`（后者依赖前者）。 |

## 4. 核心概念与源码讲解

### 4.1 KernelBuilder 与 DefaultKernelConfig

#### 4.1.1 概念说明

Kernel 层要回答两个问题：

1. **总共要启动多少个核？**（`blockNum`）
2. **每个核各处理哪些数据？**

第一个问题在 **Host 上** 一次性算好（因为启动核数必须是个确定值，传进 `<<<blockNum>>>`）；第二个问题在 **Device 上、每个核内部** 用自己的编号 `GetBlockIdx()` 现算。这种「Host 算方案、Device 领任务」的分工，就是 Kernel 层的全部职责。

`KernelBuilder` 本身只是一个薄薄的外壳模板，它把真正的算法委托给 `Schedule` 模板参数（默认 `DefaultKernelSchedule`）。真正干活的两个字段容器是：

- `DefaultKernelConfig`：存放切分结果（`blockNum` 等），即 Host 算出来、要传进 Device 的「调度参数」。
- `OpParam`：把 Kernel 的 `kernelParam` 和 Block 的 `blockParam` 打包成一个结构体，作为 `KernelCustom` 的值参数整体送进 NPU（u2-l7 已讲过「tiling 作值参数进 NPU」）。

#### 4.1.2 核心流程

```text
DeviceAdapter::Run
   └─ CalculateTiling(args, opParam)            [Host]
        ├─ KernelSchedule::MakeScheduleConfig(args, opParam.kernelParam)   ← 本讲 4.3
        └─ BlockSchedule::MakeScheduleConfig(args, opParam.kernelParam, opParam.blockParam)
   └─ LaunchKernelWithDataTuple(opParam.kernelParam.blockNum, ...)         [Host]
        └─ KernelCustom<<<blockNum>>>(opParam, args)                       ← 跨进 NPU
              └─ KernelBuilder::Run(opParam, args...)                      [Device, 每个核各跑一份]
                   └─ schedule.Run(cfg, args...)                           ← 本讲 4.4
```

#### 4.1.3 源码精读

切分结果容器 `DefaultKernelConfig`，五个字段是本讲后续推导的主角：

[include/elewise/kernel/builder.h:16-22](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h#L16-L22) — 定义 `blockNum`（启动核数）、`unitNumPerCore`（每核基础单元数）、`moreUnitCoreNum`（多处理一个单元的核数）、`tailNum`（尾数）、`unitNum`（每个单元的元素数）。

`KernelBuilder` 的模板签名与 `OpParam`：

[include/elewise/kernel/builder.h:39-49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h#L39-L49) — `KernelBuilder<BlockOp, Policy, ScheduleCfg, Schedule>`，其中 `OpParam` 把 `kernelParam`（Kernel 切分）与 `blockParam`（Block 切分）同框承载。

`KernelBuilder::Run` 只是把调用转发给 `ScheduleClz`：

[include/elewise/kernel/builder.h:58-66](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h#L58-L66) — 注意它被 `#if !defined(__ATVOSS_HOST_ONLY__)` 包住，说明 `Run` 只在 Device 侧编译；Host 侧只编译 `MakeScheduleConfig`。

#### 4.1.4 代码实践

**实践目标**：确认「Host 算方案 / Device 跑分发」的代码分区。

**操作步骤**：

1. 打开 `include/elewise/kernel/schedule.h`。
2. 观察 `MakeScheduleConfig`（约 57 行起）**没有**被 `#if !defined(__ATVOSS_HOST_ONLY__)` 包住 → 它在 Host 编译。
3. 观察 `Run`、`CalCurCoreEleCnt`、`CalGMOffset`（约 105 行起的 `#if` 块内）→ 它们只在 Device 编译。

**需要观察的现象**：同一份 `schedule.h` 在 Host 编译时「砍掉」了所有用到 `AscendC::GetBlockIdx()` 的代码（那是 NPU 专有指令），反过来 Device 编译时也不需要 `MakeScheduleConfig` 的 `printf` 调试逻辑混入。

**预期结果**：你能指出 schedule.h 里「Host 段」与「Device 段」的分界线就是第 105 行附近的 `#if !defined(__ATVOSS_HOST_ONLY__)`。

#### 4.1.5 小练习与答案

**练习 1**：`OpParam` 为什么要同时装 `kernelParam` 和 `blockParam` 两个东西，而不是只装核间切分？

**参考答案**：因为 Block 层的切分依赖 Kernel 层的结果——`CalculateTiling` 先算 `kernelParam`，再把 `kernelParam` 作为输入传给 Block 的 `MakeScheduleConfig`（见 tiling.h 第 25 行）。两者必须在 Host 上一起算好，再作为一个值参数整体送进 NPU，Device 侧每个核才能同时拿到「我属于哪个核间划分」和「我这个核内部怎么切 Tile」两套信息。

---

### 4.2 UniformSegment 均匀分段

#### 4.2.1 概念说明

`DefaultSegmentPolicy` 是一个枚举，目前只有一个值 `UniformSegment`（均匀切分）。它的语义是：**把全部「对齐单元」尽可能均匀地分给各个核**——大部分核处理 `unitNumPerCore` 个单元，剩下 `moreUnitCoreNum` 个核各多处理 1 个单元，最后那个核再把除不尽的「尾数」`tailNum` 也吃掉。

需要诚实地说明一点：当前 `MakeScheduleConfig` 的代码**并没有**根据 `Policy.segmentPolicy` 做分支，而是直接实现了 UniformSegment 这一种算法。也就是说，`DefaultSegmentPolicy` 枚举更像是一个**为未来扩展预留的策略占位**（例如日后可能新增按负载不均匀切分的策略），现阶段你只需理解 UniformSegment 这一种行为。

#### 4.2.2 核心流程

均匀分段的目标可以用一句话概括：

\[ \text{每个核的工作量} \in \{U,\ U{+}1\} \text{ 个单元} \quad (\text{尾核再额外加上 } tailNum) \]

其中 \(U\) = `unitNumPerCore`。设总单元数为 \(T\)、核数为 \(B\)，则：

\[ U = \left\lfloor T / B \right\rfloor, \qquad \text{moreUnitCoreNum} = T \bmod B \]

即前 `moreUnitCoreNum` 个核各分 \(U{+}1\) 个单元，其余核各分 \(U\) 个单元。这正是「均匀」的含义：任意两个核的单元数之差不超过 1。

#### 4.2.3 源码精读

策略枚举与默认策略对象：

[include/elewise/kernel/builder.h:24-33](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h#L24-L33) — `DefaultSegmentPolicy::UniformSegment` 是目前唯一取值；`defaultKernelPolicy` 是传给 `KernelBuilder` 的默认策略实参。

`KernelBuilder` 的模板形参里预留了 `Policy` 和 `Schedule` 两个扩展点：

[include/elewise/kernel/builder.h:39-41](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h#L39-L41) — 第 41 行 `template <...> class Schedule = DefaultKernelSchedule` 允许用户替换调度实现，这是 u3-l8「自定义 Schedule」的入口。

#### 4.2.4 代码实践

**实践目标**：验证「均匀」语义——任意两核的单元数之差不超过 1。

**操作步骤**：阅读 4.3 的手工推导结果（100000 元素、ACTUAL_N=32 的情形），数一数「分到 56 个单元的核」与「分到 55 个单元的核」各有多少个。

**预期结果**：45 个核各得 56 个单元，11 个核各得 55 个单元，差值恰为 1，符合「均匀分段」定义。（具体推导见 4.3.4）

#### 4.2.5 小练习与答案

**练习 1**：如果有 \(T = 100\) 个单元要分给 \(B = 7\) 个核，`unitNumPerCore` 和 `moreUnitCoreNum` 各是多少？

**参考答案**：\(U = \lfloor 100/7 \rfloor = 14\)，`moreUnitCoreNum` = \(100 \bmod 7 = 2\)。即前 2 个核各处理 15 个单元，后 5 个核各处理 14 个单元；合计 \(2{\times}15 + 5{\times}14 = 100\)。

---

### 4.3 MakeScheduleConfig：核数切分（Host 侧）

#### 4.3.1 概念说明

这是 Kernel 层最核心的算法，跑在 Host 上。它的输入是用户通过 `ArgumentsBuilder` 传进来的张量形状，输出是填好的 `DefaultKernelConfig`。整段逻辑围绕一个关键抽象——**Unit（对齐单元）**：

- 把总元素数 `totalEleNum` 按 `ACTUAL_N`（单元大小，单位是元素个数）切成「整数个完整单元 + 一个不足单元的尾数」。
- `totalUnitCnt = totalEleNum / ACTUAL_N`（完整单元数）。
- `tailNum = totalEleNum % ACTUAL_N`（尾数，最后一个核单独处理）。
- 再把 `totalUnitCnt` 个单元均匀分给若干个核。

`ACTUAL_N` 的取值由 `TileShape` 决定：若 TileShape 是一维（只有一个数，如 abs 的 `Shape<32>`），`ACTUAL_N` 固定为 32（框架的最小对齐粒度，与底层 Vector 数据搬运的对齐要求一致）；若是二维（如 `Shape<1, 4096>`），则取最后一维的大小。

#### 4.3.2 核心流程

```text
输入：totalEleNum（总元素数，由输入张量形状累乘得到）

1. 若 totalEleNum <= BASIC_CORE_ELE_NUM：            # 数据量很小
     blockNum = 1, tailNum = totalEleNum              # 只用 1 个核，全给它
     return

2. basicCoreUnitNum = BASIC_CORE_ELE_NUM / ACTUAL_N   # 「参考单核」能处理的单元数
3. totalUnitCnt     = totalEleNum / ACTUAL_N          # 总完整单元数
4. blockNum         = ceil(totalUnitCnt / basicCoreUnitNum)
5. 若 blockNum > CORE_NUM(=56): blockNum = CORE_NUM    # ★ 不能超过物理核数
6. unitNumPerCore   = totalUnitCnt / blockNum          # 每核基础单元数
7. moreUnitCoreNum  = totalUnitCnt % blockNum          # 多分一个单元的核数
8. tailNum          = totalEleNum % ACTUAL_N           # 尾数
```

第 5 步是「裁剪」：算出来的 `blockNum` 不能超过芯片实际的 `CORE_NUM`。裁剪之后，第 6 步的 `unitNumPerCore` 自然变大——因为同样的总单元数被分给更少的核，每核分到的单元数变多。

#### 4.3.3 源码精读

`ACTUAL_N_ASSIGN` 与 `BASIC_CORE_ELE_NUM` 的编译期推导：

[include/elewise/kernel/schedule.h:46-49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L46-L49) — `ACTUAL_N_ASSIGN`：一维 TileShape 取 32，否则取最后一维；`BASIC_CORE_ELE_NUM` 把 `BASIC_BLOCK` 向上对齐到 `ACTUAL_N` 的整数倍。

`MakeScheduleConfig` 主体——小数据短路、单元计数、核数裁剪、均匀分配：

[include/elewise/kernel/schedule.h:71-92](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L71-L92) — 注意第 85-87 行的裁剪：`if (blockNum > ArchTag::CORE_NUM) blockNum = ArchTag::CORE_NUM;`，随后第 88-89 行基于裁剪后的 `blockNum` 重算 `unitNumPerCore` 与 `moreUnitCoreNum`。

Device 层如何串联调用 Kernel 与 Block 两层 `MakeScheduleConfig`：

[include/elewise/device/tiling.h:20-28](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/tiling.h#L20-L28) — 第 20 行先算 `kernelParam`，第 25 行把 `kernelParam` 作为输入再算 `blockParam`，印证 4.1.5 所说的依赖关系。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：给定 `totalEleNum = 100000`，用 abs 样例的真实配置（`Shape<32>`，故 `ACTUAL_N = 32`、`BASIC_CORE_ELE_NUM = 32`）手工推导 `blockNum / unitNumPerCore / moreUnitCoreNum / tailNum`，并解释裁剪。

**已知量**（来自 abs 的 `Shape<32>`）：

- `ACTUAL_N` = `ACTUAL_N_ASSIGN` = 32（因为 `TILE_SHAPE_SIZE == 1`，走 32 分支）
- `BASIC_CORE_ELE_NUM` = `(32 + 32 - 1) / 32 * 32` = `32`
- `unitNum` 在 `MakeScheduleConfig` 第 71 行被赋为 `ACTUAL_N` = 32

**推导步骤**：

1. `totalEleNum = 100000`，明显大于 `BASIC_CORE_ELE_NUM = 32`，不走单核短路。
2. `basicCoreUnitNum = 32 / 32 = 1`。
3. `totalUnitCnt = 100000 / 32 = 3125`（`3125 × 32 = 100000`，整除）。
4. `blockNum = ceil(3125 / 1) = 3125`。
5. **裁剪**：`3125 > CORE_NUM(56)` → `blockNum = 56`。★ 这就是裁剪发生处。
6. `unitNumPerCore = 3125 / 56 = 55`（`55 × 56 = 3080`）。
7. `moreUnitCoreNum = 3125 % 56 = 45`（`3080 + 45 = 3125` ✓）。
8. `tailNum = 100000 % 32 = 0`。

**最终结果**：

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `blockNum` | **56**（从 3125 裁到 56） | 实际启动 56 个核 |
| `unitNumPerCore` | 55 | 每核基础分 55 个单元 |
| `moreUnitCoreNum` | 45 | 前 45 个核各多分 1 个单元（共 56 个） |
| `tailNum` | 0 | 没有尾数（100000 恰被 32 整除） |

**核间分配核对**：45 个核各得 \(55+1=56\) 个单元，11 个核各得 55 个单元；总单元 \(45{\times}56 + 11{\times}55 = 2520 + 605 = 3125\) ✓，折合元素 \(3125{\times}32 = 100000\) ✓。

**为什么会被 `CORE_NUM` 裁剪**：`<<<blockNum>>>` 启动的核数不能超过芯片物理 Vector 核数。`DAV_3510` 只有 56 个核（见 4.5），如果按 `basicCoreUnitNum=1` 的「理想」分配会要求 3125 个核，物理上不存在。裁剪到 56 后，算法自动把多余的单元摊到每个核上（`unitNumPerCore` 从 1 涨到 55），保证总工作量不丢。

**待本地验证**：以上数值是据源码公式手算的结果。你可以在 abs 样例里把输入元素数设为 100000，在 `MakeScheduleConfig` 末尾临时加一行 `printf` 打印这四个字段（仅 Host 侧生效），上板或仿真运行后核对是否一致。

#### 4.3.5 小练习与答案

**练习 1**：如果把 abs 的输入元素数改成 `100`（其它不变），`blockNum` 是多少？还会被裁剪吗？

**参考答案**：`totalUnitCnt = 100/32 = 3`，`blockNum = ceil(3/1) = 3`。`3 < 56`，不触发裁剪；`unitNumPerCore = 3/3 = 1`，`moreUnitCoreNum = 0`，`tailNum = 100 % 32 = 4`。即 3 个核各处理 1 个单元（32 元素），最后一个核额外处理 4 个尾元素。

**练习 2**：为什么第 6 步「重算 `unitNumPerCore`」必须放在裁剪之后，而不能放在裁剪之前？

**参考答案**：`unitNumPerCore = totalUnitCnt / blockNum` 依赖 `blockNum`。若在裁剪前算，用的是 3125，得到 `3125/3125 = 1`；裁剪后 `blockNum` 变成 56，必须用 56 重算才能得到正确的 55。顺序错了会导致每个核只领 1 个单元、大量数据无人处理。

---

### 4.4 CalCurCoreEleCnt 与 CalGMOffset：多核分发（Device 侧）

#### 4.4.1 概念说明

`blockNum` 等四个量算好、随 `OpParam` 送进 NPU 后，每个核并发执行同一段 `KernelBuilder::Run`。此时每个核都要回答两个仅属于自己的问题：

1. **我要处理多少个元素？** → `CalCurCoreEleCnt`
2. **我的数据在 GM 的哪里开始？** → `CalGMOffset`

这两个函数都用 `AscendC::GetBlockIdx()`（当前核编号）从 `kernelParam` 里「领」出自己的那份。领到的元素数会被写进 `blockParam.totalElemCnt`，再交给 Block 层去切成 Tile（u2-l9）。

#### 4.4.2 核心流程

```text
每个核 (blockIdx = i) 执行 Run:
  1. configBlock.totalElemCnt = CalCurCoreEleCnt(kernelParam)   # 算「我处理多少元素」
  2. PrepareParams / ConvertArgs                                 # 给每个张量参数加上 GM 偏移
       └─ ConstructParam 内部调用 CalGMOffset(kernelParam)      # 算「我的数据从哪开始」
  3. blockOp.Run(configBlock, convertArgs)                       # 下沉给 Block 层
```

`CalCurCoreEleCnt` 的逻辑（设 `i = GetBlockIdx()`）：

\[ \text{eleCnt}_i = U \cdot \text{unitNum} + \begin{cases} \text{unitNum}, & i < \text{moreUnitCoreNum} \\ 0, & \text{否则} \end{cases} + \begin{cases} \text{tailNum}, & i = \text{blockNum}-1 \\ 0, & \text{否则} \end{cases} \]

即「基础单元 + 可能的额外一个单元（前 moreUnitCoreNum 个核）+ 可能的尾数（最后一个核）」。

`CalGMOffset` 给出当前核在 GM 中的起始元素偏移：前 `moreUnitCoreNum` 个核每个占地 \((\text{unitNumPerCore}{\cdot}\text{unitNum} + \text{unitNum})\)，其后的核每个占地 \(\text{unitNumPerCore}{\cdot}\text{unitNum}\)。

#### 4.4.3 源码精读

`Run` 把每核元素数塞进 `blockParam.totalElemCnt`，再下沉 Block 层：

[include/elewise/kernel/schedule.h:117-130](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L117-L130) — 第 122 行 `CalCurCoreEleCnt` 的结果成为 Block 层的工作量输入。

`CalCurCoreEleCnt` 三段式累加：

[include/elewise/kernel/schedule.h:140-151](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L140-L151) — 基础 `unitNum*unitNumPerCore`，前 `moreUnitCoreNum` 个核再加一个 `unitNum`，最后一个核再加 `tailNum`。

`CalGMOffset` 计算每核 GM 起始偏移（单位：元素）：

[include/elewise/kernel/schedule.h:203-210](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L203-L210) — 前 `moreUnitCoreNum` 个核与之后核用不同公式，区别就是前者多算了一个 `unitNum`。

偏移如何作用到张量指针上——`ConstructParam` 把裸 GM 指针前移 `sizeof(元素) * offset` 字节：

[include/elewise/kernel/schedule.h:159-176](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L159-L176) — 第 165-167 行：`ptr = (uint64_t)arg + sizeof(DTypeTmp) * offset`，让每个核只看到 GM 里属于自己的那段；标量参数则原样透传（第 170-174 行）。

#### 4.4.4 代码实践

**实践目标**：用 4.3.4 推出的结果（`blockNum=56, unitNumPerCore=55, moreUnitCoreNum=45, tailNum=0, unitNum=32`），逐核验证 `CalCurCoreEleCnt` 与 `CalGMOffset` 是否自洽。

**操作步骤**：代入公式，分别计算核 0、核 44、核 45、核 55 的元素数与 GM 偏移。

**推导结果**（`eleCnt_i` 与 `offset_i`，单位均为元素）：

| 核编号 i | 类别 | `CalCurCoreEleCnt` | `CalGMOffset` |
| --- | --- | --- | --- |
| 0 | \(i < 45\) | \(32{\times}55 + 32 = 1792\) | \(0\) |
| 44 | \(i < 45\)（最后一个多单元核） | 1792 | \(44{\times}(55{\times}32+32) = 44{\times}1792 = 78848\) |
| 45 | \(i \geq 45\)，非尾核 | \(32{\times}55 = 1760\) | \(55{\times}32{\times}45 + 45{\times}32 = 79200 + 1440 = 80640\) |
| 55 | 尾核（\(i = 56-1\)） | \(1760 + 0 = 1760\) | \(55{\times}32{\times}55 + 45{\times}32 = 96800 + 1440 = 98240\) |

**自洽性核对**：

- 核 45 的偏移应等于「前 45 个核处理总量」：\(45{\times}1792 = 80640\) ✓
- 核 55 处理完应到 100000：\(98240 + 1760 = 100000\) ✓
- 总量：\(45{\times}1792 + 11{\times}1760 = 80640 + 19360 = 100000\) ✓

**预期结果**：每个核的 `offset + eleCnt` 恰好等于下一个核的 `offset`，最后一个核的 `offset + eleCnt` 恰好等于总元素数 100000——说明 56 个核无重叠、无遗漏地瓜分了整段 GM。

#### 4.4.5 小练习与答案

**练习 1**：核 44 与核 45 的 `CalCurCoreEleCnt` 差了多少？为什么？

**参考答案**：差 \(1792 - 1760 = 32\)，正好一个 `unitNum`。因为核 44 满足 \(i < \text{moreUnitCoreNum}(45)\) 多分了一个单元，核 45 不满足，所以二者相差恰好一个单元（32 元素）。

**练习 2**：为什么 `CalGMOffset` 对「前 moreUnitCoreNum 个核」和「其余核」要用两套不同公式，而不能统一？

**参考答案**：因为前 `moreUnitCoreNum` 个核每个占地 \((\text{unitNumPerCore}{+}1){\cdot}\text{unitNum}\)（多一个单元），其余核每个占地 \(\text{unitNumPerCore}{\cdot}\text{unitNum}\)。若统一用一套公式，算出来的偏移会与前面核实际占用的区间对不上，导致数据错位。所以第 i 个核（\(i \geq \text{moreUnitCoreNum}\)）的偏移 = 前 `moreUnitCoreNum` 个核的总占地 + 第 `moreUnitCoreNum` 到 `i-1` 个核的均匀占地，这正是第 208-209 行两个分支表达的含义。

---

### 4.5 Arch::DAV_3510：核数与 UB 约束

#### 4.5.1 概念说明

Kernel 层的切分不是「想分几个核就分几个核」，它受芯片物理资源约束。这些常量集中在 `arch.h` 的 `Arch::DAV_3510` 结构体里：

- `CORE_NUM = 56`：物理 Vector 核数上限，直接决定 `blockNum` 的裁剪阈值（4.3 的第 5 步）。
- `UB_SIZE = 240 * 1024`：Unified Buffer 大小（240KB），它不直接约束 Kernel 层，但约束下游 Block 层的 Tile 大小——一次 Tile 搬进 UB 的数据不能超过这个量（详见 u2-l9 / u3-l5）。

这两个常量通过模板参数 `ArchTag` 一路传到 `BaseKernelSchedule`（`using ArchTag = typename BlockOp::ScheduleClz::ArchTag;`），让算法「硬件感知」。

#### 4.5.2 核心流程

```text
用户在 Config 里指定 ArchTag（默认 DAV_3510）
   → BlockBuilder 把 ArchTag 存进 BlockSchedule
   → KernelBuilder 通过 BlockOp::ScheduleClz::ArchTag 取回
   → MakeScheduleConfig 用 ArchTag::CORE_NUM 裁剪 blockNum
   → BlockSchedule 用 ArchTag::UB_SIZE 推算 UB_TILE_SIZE（约束 Tile 大小）
```

#### 4.5.3 源码精读

硬件常量定义：

[include/common/arch.h:22-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h#L22-L25) — `DAV_3510`：`CORE_NUM = 56`、`UB_SIZE = 240KB`。

`BaseKernelSchedule` 如何拿到 `ArchTag`：

[include/elewise/kernel/schedule.h:42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L42) — `using ArchTag = typename BlockOp::ScheduleClz::ArchTag;`，ArchTag 由 Block 层透传上来。

裁剪处实际引用的就是这个 `ArchTag::CORE_NUM`：

[include/elewise/kernel/schedule.h:85-87](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L85-L87) — `if (kernelParam.blockNum > ArchTag::CORE_NUM) kernelParam.blockNum = ArchTag::CORE_NUM;`。

> 说明：`UB_SIZE` 对 Kernel 层是「间接约束」。它直接影响的是 Block 层 `UB_TILE_SIZE = ArchTag::UB_SIZE / MAX_BUFFER_COUNT / 1024 * 1024`（见 block/schedule.h），从而限制 `BASIC_BLOCK`（一次 Tile 元素数）的选值；而 `BASIC_BLOCK` 又通过 `BASIC_CORE_ELE_NUM` 回到 Kernel 层影响切分。这条链路在 u2-l9 详讲，本讲只需知道 `UB_SIZE` 是它的物理源头。

#### 4.5.4 代码实践

**实践目标**：理解「换一颗芯片，切分结果会变」。

**操作步骤**：假设未来新增一个 `Arch::DAV_X` 结构体，`CORE_NUM = 28`（减半）、`UB_SIZE` 不变。在脑海中重跑 4.3.4 的推导（`totalEleNum = 100000`、`ACTUAL_N = 32`）。

**需要观察的现象**：`totalUnitCnt` 仍是 3125，但裁剪阈值从 56 变成 28。

**预期结果**：`blockNum` 从 56 变为 28；`unitNumPerCore = 3125/28 = 111`（`111*28 = 3108`）；`moreUnitCoreNum = 3125 % 28 = 17`；`tailNum = 0`。核数减半后，每个核分到的单元数大致翻倍——这正是硬件感知切分的意义。

#### 4.5.5 小练习与答案

**练习 1**：`CORE_NUM` 和 `UB_SIZE` 分别约束的是哪一层？

**参考答案**：`CORE_NUM` 直接约束 Kernel 层——它是 `blockNum` 的裁剪上限（启动核数不能超过物理核数）。`UB_SIZE` 直接约束 Block/Tile 层——它决定一次 Tile 能搬进 Unified Buffer 的最大数据量（`UB_TILE_SIZE`），进而限制 `BASIC_BLOCK` 的选值；对 Kernel 层只是通过 `BASIC_CORE_ELE_NUM` 间接影响。

---

## 5. 综合实践

**任务**：把本讲的四段知识（切分公式、裁剪、每核工作量、每核偏移）串起来，完整推演一遍 abs 样例在 **`totalEleNum = 100000`** 下的多核调度全过程，并画出核间数据布局。

**要求**：

1. 写出 Host 侧 `MakeScheduleConfig` 的全部输出字段（`blockNum / unitNumPerCore / moreUnitCoreNum / tailNum / unitNum`）——参考 4.3.4。
2. 画出 56 个核的 GM 偏移示意：标出核 0、核 44、核 45、核 55 的起止元素区间。
3. 用一句话解释：为什么 100000 个元素在没有裁剪时本应需要 3125 个核，而实际只用了 56 个核却没丢任何数据？
4. 进阶：如果把输入元素数改成 `100032`（即 `100000 + 32`），`tailNum` 会变成多少？最后一个核 `CalCurCoreEleCnt` 的结果会比原来多多少？

**参考要点**：

1. `blockNum=56, unitNumPerCore=55, moreUnitCoreNum=45, tailNum=0, unitNum=32`。
2. 核 0：`[0, 1792)`；核 44：`[78848, 80640)`；核 45：`[80640, 82400)`；核 55：`[98240, 100000)`。
3. 因为裁剪后算法在第 6-7 步基于 56 重新做了「均匀分配」（`unitNumPerCore` 从 1 涨到 55），把原本要分给 3125 个核的单元重新摊到 56 个核上，总单元数守恒（\(3125 = 45{\times}56 + 11{\times}55\)）。
4. `100032 % 32 = 0`，`tailNum` 仍为 0（因为 100032 仍能被 32 整除）；若改成 `100033`，`tailNum = 1`，最后一个核 `CalCurCoreEleCnt` 增加 1。

## 6. 本讲小结

- Kernel 层的职责是「Host 算切分方案、Device 每核领任务」：`MakeScheduleConfig` 在 Host 算 `blockNum` 等，`CalCurCoreEleCnt` / `CalGMOffset` 在 Device 用 `GetBlockIdx()` 领取各自那份。
- 核心抽象是 **Unit（对齐单元）**：总元素数被切成「完整单元 + 尾数」，单元大小 `ACTUAL_N` 由 TileShape 决定（一维取 32，二维取最后一维）。
- `MakeScheduleConfig` 先估理想核数 `ceil(totalUnitCnt / basicCoreUnitNum)`，再被 `ArchTag::CORE_NUM(=56)` **裁剪**，最后基于裁剪后的核数重算 `unitNumPerCore` 与 `moreUnitCoreNum`，保证总工作量不丢。
- `CalCurCoreEleCnt` 用「基础单元 + 前 moreUnitCoreNum 个核各加一个单元 + 尾核加尾数」三段式给每核定量；`CalGMOffset` 用两套公式给每核定位，二者配合实现 GM 的无重叠、无遗漏瓜分。
- `DefaultSegmentPolicy::UniformSegment` 是当前唯一实现的多核策略，策略枚举与 `Schedule` 模板形参共同构成未来的扩展点（详见 u3-l8）。
- `Arch::DAV_3510` 的 `CORE_NUM=56` 直接裁剪核数，`UB_SIZE=240KB` 经 Block 层 `UB_TILE_SIZE` 间接回压 `BASIC_BLOCK`，是硬件感知切分的两个源头。

## 7. 下一步学习建议

- **紧接 u2-l9（Block 层）**：本讲每个核领到的 `totalElemCnt` 会交给 Block 层的 `DefaultBlockSchedule::Run`，被切成 `wholeLoop` 个完整 Tile + 1 个尾 Tile。建议带着「单核 1792 或 1760 个元素会怎么被切」的问题去读。
- **回顾 u2-l7（Device 层）**：现在你已能填上 `CalculateTiling` 与 `<<<blockNum>>>` 的内部细节，可以重读 Device 层，把三步流程彻底闭环。
- **进阶 u3-l5（Buffer 管理）与 u3-l8（自定义策略）**：若想深究 `UB_SIZE` 如何决定 `UB_TILE_SIZE` 与双缓冲，或想替换 `Schedule` 模板形参实现非均匀切分，这两篇是后续落点。
