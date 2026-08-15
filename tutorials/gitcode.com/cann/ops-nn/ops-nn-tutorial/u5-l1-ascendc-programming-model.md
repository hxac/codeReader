# Ascend C 编程模型：矢量算子 Kernel 的结构与流水

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解 AI Core 上 GM（Global Memory）与 UB（Unified Buffer）两级存储的分工，以及一个矢量算子的数据在两级存储之间如何流动。
2. 掌握 Ascend C 中 `TPipe`、`TQue`、`GlobalTensor`、`LocalTensor` 四个核心抽象的职责与配合方式。
3. 读懂 `CopyIn → Compute → CopyOut` 三段式 kernel 结构，理解 `AllocTensor/EnQue/DeQue/FreeTensor` 的队列生命周期。
4. 理解 `BUFFER_NUM = 2`（双缓冲）为什么能带来「搬运与计算流水并行」，并通过实验观察单缓冲/三缓冲的行为差异。

本讲是第 5 单元「Kernel 实现」的第一讲：u4 系列讲的是 Host 侧怎么把任务切好（tiling），本讲开始进入 Device 侧——AI Core 上的 kernel 怎么消费这些切分结果。

## 2. 前置知识

阅读本讲前，你需要具备以下概念（前几讲已建立，这里简要回顾）：

- **Host 侧与 Device 侧**：Host 指 CPU 侧，负责 shape 推导、tiling 计算、任务下发；Device 指 NPU 的 AI Core，负责真正的并行计算。两侧通过 tiling data 这个字节契约传参（见 u4-l2）。
- **TilingData 三字段**：`AddExampleTilingData` 包含 `totalNum`（总元素数）、`blockFactor`（每个核处理的元素数，核切分粒度）、`ubFactor`（单次搬入 UB 的元素数，UB 切分粒度）。本讲 kernel 的所有循环边界都来自这三个字段。
- **AI Core 与 BlockIdx**：一个算子任务会被切成多份，由多个 AI Core（块，block）并行执行，`AscendC::GetBlockIdx()` 返回当前执行到的是第几块。
- **GM 与 UB**：
  - **GM（Global Memory）**：Device 上的大容量全局内存（类似 CPU 世界的内存/显存），容量大（GB 级）但访问慢，输入输出张量都放在这里。
  - **UB（Unified Buffer，统一缓冲区）**：每个 AI Core 内部的高速片上存储（类似 CPU 世界的 L1 Cache，可显式编程），容量小（百 KB 级）但访问快。AI Core 的矢量计算单元**只能对 UB 中的数据做计算**。
- **为什么需要搬运**：正因为计算单元只认 UB，所以任何算子的 kernel 都必然是「GM → UB → 计算 → UB → GM」的搬运-计算-搬运结构。这不是可有可无的工程包装，而是硬件约束的直接结果。

一个直观的类比：GM 是仓库，UB 是工作台，AI Core 是只在工作台上干活的工人。工人每次只能从仓库搬一批零件到工作台，加工完再搬回仓库。「每次搬多少」「搬几批」由 tiling 决定（u4-l1），「怎么搬、搬完怎么加工」是本讲 kernel 的内容。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/add_example/op_kernel/add_example.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h) | 本讲主角：`AddExample` Kernel 类，包含 TPipe/TQue 队列、三段式流水与双缓冲的完整实现 |
| [examples/add_example/op_kernel/add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp) | kernel 入口函数：读取 tiling data，按 tiling key 分发到模板实例 |
| [examples/add_example/op_kernel/add_example_tiling_data.h](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_data.h) | Host/Device 共享的 tiling data POD 结构体（u4-l2 已精读，本讲作为字段字典） |
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp) | Host 侧 tiling 实现。本讲重点看其中 `BUFFER_NUM = 6` 与 `ubFactor` 的计算——它决定了 kernel 侧改双缓冲时必须同步考虑什么 |
| [docs/zh/develop/aicore_develop_guide.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md) | 官方 AI Core 算子开发指南，给出了与本讲同构的 Kernel 类骨架和 CopyIn-Compute-CopyOut 流程图 |

## 4. 核心概念与源码讲解

### 4.1 两级存储与 Tensor 抽象：GlobalTensor 与 LocalTensor

#### 4.1.1 概念说明

Ascend C 用两个类型把「数据在哪里」编码进类型系统：

- **`GlobalTensor<T>`**：指向 GM 中一段类型为 `T` 的数据。它是一个「远程视图」——持有起始地址和长度，不能直接参与计算。
- **`LocalTensor<T>`**：指向本核 UB 中一段类型为 `T` 的数据。它是「工作台上的零件」——只有这种 tensor 才能作为 `AscendC::Add` 等计算 API 的操作数。

把两者分成不同类型，是为了让「忘记搬运就直接计算」这类错误在**编译期**暴露，而不是运行时才发现数据在错误的存储里。

#### 4.1.2 核心流程

一个核处理自己那份数据（`blockLength_` 个元素）的全程：

```text
GM 中 x/y 的本核窗口 ──DataCopyPad──▶ UB 中的 xLocal/yLocal
                                        │
                                   AscendC::Add（UB 上计算）
                                        │
GM 中 z 的本核窗口 ◀──DataCopyPad── UB 中的 zLocal
```

#### 4.1.3 源码精读

Kernel 类中声明了三个 GM 视图成员（两输入一输出）：

[add_example.h:48-53](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L48-L53)：声明 `GlobalTensor<T>` 类型的 `inputGMX/inputGMY/outputGMZ`，以及本核处理长度 `blockLength_` 和单次搬运长度 `ubLength_`。

在 `Init` 中，`SetGlobalBuffer` 为每个 GM 视图设定「本核窗口」——基地址偏移到本核负责的起点，长度为本核的实际元素数：

[add_example.h:63-65](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L63-L65)：把 `(__gm__ T*)x` 偏移 `blockFactor * GetBlockIdx()` 个元素后交给 `inputGMX.SetGlobalBuffer`，并限定长度为 `blockLength_`。`__gm__` 是地址空间修饰符，标明该指针指向 GM；偏移量正是 u4-l1 中核切分算出的 `blockFactor × 核号`，tiling 的切分结果在这里被消费。

对应地，`LocalTensor` 只在搬运/计算的瞬间被创建出来（见 4.3 的 `AllocTensor`/`DeQue`），不做成员变量保存——它是队列流转的临时凭证，用完即还。

计算 API 只接受 LocalTensor：

[add_example.h:107](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L107)：`AscendC::Add(zLocal, xLocal, yLocal, currentNum)` 三个操作数全是 `LocalTensor<T>`，第 4 个参数是本次计算的元素个数。若试图把 `inputGMX` 直接传进来，编译期就会报类型错误——这正是两级 Tensor 抽象的价值。

> 阅读提示：`Init` 中第 59 行计算尾核长度用的是 `GetBlockIdx() - 1`，而第 63-65 行计算地址偏移用的是 `GetBlockIdx()`，两处下标基准疑似不一致（其中一处可能差 1）。本讲按整体流程理解即可，具体哪个为准**待确认**，建议结合本机运行结果或与仓库存量 issue 核对。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认「计算 API 只吃 LocalTensor」这一约束。
2. **操作步骤**：在本地把 [add_example.h:107](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L107) 中的 `xLocal` 临时改成 `inputGMX[0]`，执行编译，观察编译器报错信息；随后改回。
3. **需要观察的现象**：编译失败，错误信息会指出参数类型不匹配（GlobalTensor 不是 AscendC::Add 接受的操作数类型）。
4. **预期结果**：确认存储位置约束由类型系统静态保证。编译报错文案随 CANN 版本略有差异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `inputGMX` 可以作为成员变量长期持有，而 `xLocal` 不适合？

**参考答案**：`inputGMX` 是对 GM 稳定窗口的只读描述，GM 内容在算子执行期间有效；`xLocal` 对应的 UB 内存属于队列资源池，由 `AllocTensor/FreeTensor` 借还管理，跨迭代长期持有会破坏队列的缓冲复用（双缓冲正是靠「借一块、用一块」实现的），所以它只在一次 CopyIn/Compute 生命周期内存在。

**练习 2**：`ubLength_`（即 tiling 里的 `ubFactor`）变大，直接影响了什么资源？

**参考答案**：UB 空间占用。每个队列要 `BUFFER_NUM × ubFactor × sizeof(T)` 字节的 UB（见 4.2.3 的 `InitBuffer`），`ubFactor` 越大单次搬运越多、循环次数越少，但 UB 容量有限，tiling 侧用 `FloorAlign` 向下对齐保证不越界（u4-l1）。

### 4.2 Kernel 类骨架：TPipe、TQue 与 Init 初始化

#### 4.2.1 概念说明

- **`TPipe`**：内存管道管理器。它负责把本核的 UB 空间划分给各条队列，是 UB 资源的「分配台」。
- **`TQue<position, depth>`**：队列，连接「搬运单元」和「计算单元」两类异步硬件部件。模板参数：
  - `QuePosition::VECIN`：输入队列，方向 GM → UB；
  - `QuePosition::VECOUT`：输出队列，方向 UB → GM；
  - `depth`（即 `BUFFER_NUM`）：队列深度，也就是缓冲块数。
- 队列的四个核心动作构成一个资源生命周期：
  - `AllocTensor()`：从队列的空闲缓冲中**借**一块 UB；
  - `EnQue(t)`：把填好数据的 tensor **入队**，交给下游硬件部件异步处理；
  - `DeQue()`：**取出**上游已就绪的 tensor；
  - `FreeTensor(t)`：**归还**缓冲，供下一轮复用。

这套「队列 + 借还」设计是 Ascend C 把底层异步硬件（DMA 搬运单元、矢量计算单元）包装成同步风格代码的关键。

#### 4.2.2 核心流程

Kernel 类的生命周期分两步：

```text
入口函数 ──▶ 构造 AddExample<T> 对象
        ──▶ Init(x, y, z, tilingData)   # 算窗口、设 GM 视图、给三条队列分配 UB
        ──▶ Process()                    # 三段式流水主循环（4.3）
```

#### 4.2.3 源码精读

类骨架与成员声明：

[add_example.h:27-46](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L27-L46)：第 27 行定义 `BUFFER_NUM = 2`（双缓冲）；第 43-46 行声明一个 `TPipe` 和三条 `TQue`——两条 `VECIN` 输入队列（x、y）加一条 `VECOUT` 输出队列（z），队列深度均为 `BUFFER_NUM`。这与官方开发指南中的说明一致：见 [aicore_develop_guide.md:318-326](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L318-L326)，其中注释明确写道「BUFFER_NUM 表示 buffer 数量，开启 double buff 达到流水并行，为 2」。

`Init` 完成三件事：

[add_example.h:57-70](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L57-L70)：

1. **算本核实际长度**（第 59-60 行）：`remainderLength = totalNum - blockFactor * (GetBlockIdx() - 1)`，再与 `blockFactor` 取小，即尾核可能不足一个整块（`blockLength_ ≤ blockFactor`）。
2. **设定 GM 窗口**（第 63-65 行）：见 4.1.3。
3. **给队列分 UB**（第 67-69 行）：`pipe.InitBuffer(queue, BUFFER_NUM, ubLength_ * sizeof(T))`——每条队列获得 `BUFFER_NUM` 块、每块 `ubLength_ × sizeof(T)` 字节的 UB。三条队列合计占用：

\[ \text{UB 占用} = 3 \times \text{BUFFER\_NUM} \times \text{ubFactor} \times \text{sizeof}(T) \]

这个式子解释了 Host 侧 tiling 的一个关键常量：

[add_example_tiling.cpp:41](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L41) 与 [add_example_tiling.cpp:218-222](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L218-L222)：Host 侧定义 `BUFFER_NUM = 6`，注释写明「2 个输入和 1 个输出，考虑使用双缓冲，共需要 6 块 UB tensor」——即 3 条队列 × 2 块缓冲。`ubFactor` 按

\[ \text{ubFactor} = \mathrm{FloorAlign}\left(\left\lfloor \frac{\text{ubSize} / \text{TYPE\_SIZE}}{6} \right\rfloor,\ \text{ubBlockSize}\right) \]

计算，保证 6 块缓冲塞得进 UB。**注意这两个 `BUFFER_NUM` 不是同一个东西**：kernel 侧是「每条队列的缓冲数」，Host 侧是「所有队列缓冲总数」——这正是本讲综合实践中改缓冲数时最容易踩的坑。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证 Host/Device 两侧缓冲数量约定的对应关系。
2. **操作步骤**：对照阅读 [add_example.h:27](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L27)（`BUFFER_NUM = 2`）与 [add_example_tiling.cpp:41](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L41)（`BUFFER_NUM = 6`），用上面的 UB 占用公式手算：若 kernel 侧改为 `BUFFER_NUM = 3`，Host 侧应改为多少才能保持「6 块刚好装满 UB」的约束不变？
3. **需要观察的现象**：纸面推导即可，无需运行。
4. **预期结果**：3 条队列 × 3 块 = 9，Host 侧应改为 9，否则按 6 块算出的 `ubFactor` 会让 9 块缓冲超出 UB 容量（详见第 5 节综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：`TQue<QuePosition::VECIN, 2>` 和 `TQue<QuePosition::VECOUT, 2>` 的区别是什么？

**参考答案**：位置（方向）不同。`VECIN` 用于 GM → UB 的输入搬运，`EnQue` 的是刚从 GM 拷入的数据；`VECOUT` 用于 UB → GM 的输出搬运，`EnQue` 的是刚算完待写回的结果。队列深度都为 2，即各有两块缓冲可轮换。

**练习 2**：如果两个输入 x、y 共用一条 `VECIN` 队列，会有什么问题？

**参考答案**：一次 `AllocTensor` 只从队列借一块缓冲，x 和 y 需要同时在场才能做 `Add`；共用一条队列意味着 x 的 tensor 入队后必须等它被消费归还才能借到 y 的缓冲，搬运无法成对进行，流水被打断。分成两条队列（`inputQueueX`/`inputQueueY`）让两路输入各自独立借还、并行搬运。（示例代码中 x/y 的搬运是顺序写的，见 4.3.3，但队列独立保证了正确性与优化空间。）

### 4.3 三段式流水与双缓冲：CopyIn → Compute → CopyOut

#### 4.3.1 概念说明

**三段式结构**把一次循环迭代拆成三个职责单一的函数：

| 阶段 | 方向 | 使用的 tensor | 队列动作 |
| --- | --- | --- | --- |
| `CopyIn` | GM → UB | `AllocTensor` 得到空 xLocal/yLocal，`DataCopyPad` 填入 | `EnQue` 两个输入 |
| `Compute` | UB → UB | `DeQue` 输入，`AllocTensor` 输出 | `EnQue` 输出、`FreeTensor` 输入 |
| `CopyOut` | UB → GM | `DeQue` 输出，`DataCopyPad` 写回 | `FreeTensor` 输出 |

**双缓冲**是这套结构发挥性能的机制：队列深度为 2 时，搬运单元在往「第 1 块缓冲」搬第 i+1 批数据的同时，计算单元可以在「第 2 块缓冲」上算第 i 批数据——搬运和计算两类硬件部件时间上重叠。没有双缓冲（深度 1）时，搬完才能算、算完才能搬，两者串行：

```text
单缓冲（串行）：  [搬运 i][计算 i][搬运 i+1][计算 i+1] ...
双缓冲（重叠）：  [搬运 i][搬运 i+1][搬运 i+2] ...     ← 搬运单元连续工作
                       [等待 i 到位][计算 i][计算 i+1] ... ← 计算单元几乎连续
```

若总耗时近似为 \( \max(T_{\text{copy}}, T_{\text{compute}}) \) 而非 \( T_{\text{copy}} + T_{\text{compute}} \)，则理想情况下执行时间缩短接近一半。深度增加到 3 以上通常收益递减（UB 占用却线性增长），所以工程默认值是 2。

#### 4.3.2 核心流程

`Process` 的主循环（伪代码）：

```text
loopCount = ⌈blockLength_ / ubFactor⌉           # 本核数据要分几批
for i in 0 .. loopCount-1:
    currentNum = 最后一批 ? blockLength_ - ubFactor*i : ubFactor   # 尾块可能不足整批
    CopyIn(i, currentNum)     # GM → UB
    Compute(currentNum)       # UB 上 z = x + y
    CopyOut(i, currentNum)    # UB → GM
```

队列动作的时序配合（以 x 路为例）：

```text
CopyIn:   AllocTensor ─ DataCopyPad ─ EnQue ─────▶ （缓冲进入"满"状态）
Compute:  DeQue ◀───────────────────── （等"满"）─ Add ─ FreeTensor（缓冲回到"空"）
```

#### 4.3.3 源码精读

**Process 主循环**：

[add_example.h:114-123](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L114-L123)：`loopCount` 用向上取整 `(blockLength_ + ubLength_ - 1) / ubLength_` 计算（等价于 u4-l1 讲过的 `CeilDiv`，这里手写展开）；`currentNum` 只在最后一批取剩余量 `blockLength_ - ubLength_ * i`，其余批次等于 `ubLength_`——尾块处理由此贯穿三个阶段。官方指南对这一结构的说明见 [aicore_develop_guide.md:359-370](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L359-L370)，其流程图（[aicore_develop_guide.md:243-252](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L243-L252)）同样画出了 `CopyIn → Compute → CopyOut` 的串联关系。

**CopyIn（借缓冲 → 搬入 → 入队）**：

[add_example.h:73-86](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L73-L86)：第 75-76 行 `AllocTensor` 分别从两条输入队列借出 `xLocal/yLocal`；第 77-81 行组装 `DataCopyParams`（`blockLen` 按**字节**计，为 `currentNum * sizeof(T)`，所以尾块不足 32 字节对齐时用 `DataCopyPad` 而不是 `DataCopy`，细节在 u5-l2 展开）；第 82-83 行以 `inputGMX[progress * ubLength_]` 为源（第 i 批在本核窗口内的起点）执行 `DataCopyPad` 搬入；第 84-85 行 `EnQue` 把两个 tensor 入队交给下游。

**Compute（取输入 → 计算 → 入队输出 → 还输入）**：

[add_example.h:102-111](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L102-L111)：第 104-105 行 `DeQue` 取出就绪的输入（`DeQue` 内含同步语义：缓冲没填满前会等待）；第 106 行从输出队列借 `zLocal`；第 107 行 `AscendC::Add(zLocal, xLocal, yLocal, currentNum)` 完成逐元素相加——注意矢量指令「输出在前、输入在后、元素个数收尾」的参数约定（u1-l4 已验证过改成 `Mul` 即换语义）；第 108 行把结果入输出队列；第 109-110 行归还两个输入缓冲。

**CopyOut（取输出 → 搬出 → 还缓冲）**：

[add_example.h:89-99](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L89-L99)：第 91 行 `DeQue` 取出已算完的 `zLocal`；第 97 行 `DataCopyPad` 写回 GM 的 `outputGMZ[progress * ubLength_]` 处；第 98 行 `FreeTensor` 归还输出缓冲，完成一个完整循环。

把三个函数放在一起看，每块缓冲都严格走完 `Alloc → 填/算 → EnQue → DeQue → 用/搬 → Free` 的闭环，队列深度 2 提供的「多一块」就是搬运与计算可以错峰使用的活动空间。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：用「打标签」法人工跟踪一次缓冲流转。
2. **操作步骤**：设 `loopCount = 4`、`BUFFER_NUM = 2`。画一张 4 行 × 3 列的表（行 = 迭代 i，列 = CopyIn/Compute/CopyOut），在每个格子里标注本次迭代 x 路用的是 A/B 哪块缓冲（提示：迭代 0 借 A，迭代 1 借 B，迭代 2 时 A 已在迭代 0 的 Compute 里归还、可复用）。
3. **需要观察的现象**：缓冲编号呈现 A、B、A、B 交替。
4. **预期结果**：能直观看到「迭代 1 的 CopyIn 用 B 块搬数据时，迭代 0 的 A 块数据正在被 Compute 计算」——这就是双缓冲的重叠。纯纸面推演，无需设备。

#### 4.3.5 小练习与答案

**练习 1**：`Compute` 里 `DeQue` 和 `AllocTensor` 的调用顺序能互换吗（先给输出借缓冲再取输入）？

**参考答案**：在本算子这种 UB 充足的场景下功能上通常可行（两块缓冲分属不同队列，互不阻塞），但保持「先取输入、后借输出」的顺序是更稳妥的习惯：若 UB 紧张，先借输出可能占掉输入队列还来不及归还的空间。生产算子一般遵循固定的取用顺序以避免资源死锁。

**练习 2**：如果把 `BUFFER_NUM` 从 2 改成 1，第 4.3.4 的表格会变成什么样？

**参考答案**：只有一块缓冲 A。迭代 0：CopyIn 用 A 搬入，Compute 取 A 计算，CopyOut 取 A 搬出并归还 A；迭代 1 的 CopyIn 必须等迭代 0 的 CopyOut 归还缓冲后才能开始——搬运与计算完全串行，性能下降（正确性不受影响）。这正是综合实践要观察的现象。

**练习 3**：`currentNum` 为什么不直接用 `ubLength_`，而要在最后一批特殊处理？

**参考答案**：`blockLength_`（本核长度）不一定是 `ubLength_` 的整数倍（尾核、尾块），最后一批实际元素数是剩余量。`DataCopyPad` 的 `blockLen`、`AscendC::Add` 的元素个数都用 `currentNum`，搬多算多会越界读写，搬少算少会漏数据。

### 4.4 kernel 入口：从 tiling data 到 Kernel 类实例

#### 4.4.1 概念说明

Kernel 类本身不会自己跑起来，需要一个符合 Ascend C 约定的**入口函数**：它以 GM 地址（输入输出、workspace、tiling data）为参数，被框架在 AI Core 上调用。入口函数的职责只有三件：读 tiling data、按 tiling key 选分支、构造并驱动 Kernel 类。

#### 4.4.2 核心流程

```text
框架下发 ──▶ add_example<schMode>(x, y, z, workspace, tiling)
              ├─ REGISTER_TILING_DEFAULT / GET_TILING_DATA_WITH_STRUCT 读取 tiling data
              ├─ if constexpr (schMode == 0) → AddExample<float>
              └─ if constexpr (schMode == 1) → AddExample<int32_t>
                     每个分支：op.Init(...) → op.Process()
```

#### 4.4.3 源码精读

[add_example.cpp:36-57](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L36-L57)：入口函数模板 `add_example<schMode>` 用 `__global__ __aicore__` 修饰（表示可在 AI Core 上执行的核函数）；第 40-42 行注册并读取 `AddExampleTilingData`（`GET_TILING_DATA_WITH_STRUCT` 从最后一个 GM 参数按字节还原出结构体——u4-l2 讲过的契约在 Device 侧的消费端）；第 45-56 行用 `if constexpr` 按 tiling key 分发：0 实例化 `AddExample<float>`，1 实例化 `AddExample<int32_t>`。由于是编译期分支，两个实例各自生成一份专用二进制，互不增加运行时开销。入口与 DataCopy/对齐的更多细节是下一讲（u5-l2）的主题，这里只需理解「Kernel 类被谁、以什么参数驱动」。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：建立「tiling key → 模板实例」的映射直觉。
2. **操作步骤**：对照阅读 [add_example.cpp:24-27](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L24-L27) 的枚举（FLOAT=0、INT32=1）与 [add_example_tiling.cpp:227-240](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L227-L240) 中 Host 侧设置 key 的分支，写出「输入 dtype → tiling key → kernel 模板实参」三段映射表。
3. **需要观察的现象**：三处取值一一对应。
4. **预期结果**：`DT_FLOAT → 0 → AddExample<float>`，`DT_INT32 → 1 → AddExample<int32_t>`。纸面作业，无需设备。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `if constexpr` 而不是运行时 `if`？

**参考答案**：`AddExample<float>` 和 `AddExample<int32_t>` 是两个不同类型，运行时 `if` 的两个分支需要在同一份代码里同时构造两种对象并保留运行时判断开销；`if constexpr` 在编译期裁剪分支，每个 tiling key 只编译出自己需要的实例化代码，类型安全且零运行时开销。

**练习 2**：入口函数的五个参数中，kernel 类的 `Init` 只用了四个，哪个没被 `AddExample` 类消费？

**参考答案**：`workspace`。Add 是纯 elementwise 算子不需要额外工作内存，Host 侧 tiling 也把 workspace 大小设为 0（[add_example_tiling.cpp:30](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L30) `WS_SYS_SIZE = 0`）；需要中间缓存（如分块矩阵乘的转置缓冲）的算子才会使用它。

## 5. 综合实践

**任务：把 AddExample 的双缓冲改成单缓冲与三缓冲，对比行为与性能。**

1. **实践目标**：亲手验证 `BUFFER_NUM` 对（a）功能正确性、（b）UB 资源占用、（c）执行性能的影响，建立对双缓冲意义的量化直觉。

2. **操作步骤**：

   a. **基线**：确认本地已按 u1-l2/u1-l4 流程装好配套 CANN 环境，编译安装原版算子并跑通 `bash build.sh --run_example add_example eager cust --vendor_name=custom`，记录输出与（可选）耗时。

   b. **单缓冲**：把 [add_example.h:27](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L27) 的 `BUFFER_NUM` 从 2 改为 1。**同步**把 [add_example_tiling.cpp:41](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L41) 的 `BUFFER_NUM` 从 6 改为 3（3 条队列 × 1 块），保持「tiling 预留 = kernel 实际占用」的约定。重新 `bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16` 并安装 run 包，重跑样例。

   c. **三缓冲**：kernel 侧改回 3、Host 侧改为 9，同样重编译安装并重跑。

   d. **（可选）性能对比**：参考 u8-l2 会详细讲解的 `msprof op`，对三个版本各采集一次，对比 Task Duration。

3. **需要观察的现象**：

   - 三个版本的**数值输出应完全一致**（加法结果不依赖缓冲深度——缓冲只影响性能不影响语义，这是 u4-l1 「切分只影响性能」结论在缓冲维度的对应）。
   - 单缓冲版本 Task Duration 变长（搬运与计算串行）；三缓冲版本与双缓冲接近或略好（收益递减）。
   - 若某版本只改 kernel 侧不改 Host 侧的 6：三缓冲场景 9 块缓冲按 6 块的 `ubFactor` 分配会超出 UB 容量，可能出现初始化失败或运行错误——这个「故意踩坑」对照实验能加深对两侧约定的理解（建议在 c 步之后单独试一次）。

4. **预期结果**：功能一致、性能呈「单缓冲明显慢、双缓冲显著提速、三缓冲边际收益小」的形态。具体耗时数字与是否触发 UB 超限报错依赖芯片型号与 CANN 版本，**待本地验证**。

5. 完成后把观察记录成三行表格（版本 / 输出是否一致 / Task Duration），留作 u8-l2 性能调优实践的基线数据。

## 6. 本讲小结

- AI Core 的矢量计算单元只能访问 UB，因此任何 kernel 天然是 **GM → UB → 计算 → UB → GM** 的搬运-计算-搬运结构；`GlobalTensor`（远程视图）与 `LocalTensor`（可计算）的类型区分把存储约束编码进编译期。
- **`TPipe` 是 UB 分配台，`TQue` 是连接搬运与计算两类异步硬件部件的队列**；`AllocTensor → EnQue → DeQue → FreeTensor` 构成缓冲的借还闭环。
- **`CopyIn-Compute-CopyOut` 三段式**是矢量算子的标准骨架：CopyIn 借缓冲搬入并 EnQue，Compute 取输入算输出并归还输入，CopyOut 取输出搬回并归还缓冲；尾块用 `currentNum` 贯穿三阶段保证不越界不漏数据。
- **双缓冲（`BUFFER_NUM = 2`）让搬运与计算时间重叠**，理想情况下耗时从两者之和降为两者取大；正确性不受缓冲深度影响。
- kernel 侧 `BUFFER_NUM`（每队列块数）与 Host 侧 tiling 的 `BUFFER_NUM = 6`（全部队列块数之和）是**两个必须同步修改的量**，`ubFactor` 的计算依赖后者。
- 入口函数经 `GET_TILING_DATA_WITH_STRUCT` 读 tiling data，`if constexpr` 按 tiling key 分发模板实例，再以 `Init → Process` 驱动 Kernel 类。

## 7. 下一步学习建议

下一讲 **u5-l2「Kernel 入口与数据搬运」**将深入本讲一笔带过的部分：`extern "C"`/`__global__` 入口的完整约定、`DataCopy` 与 `DataCopyPad` 的参数语义、32 字节对齐要求以及尾块不对齐场景的处理。之后 u5-l3 会带你把本讲的教学样例与生产算子 `activation/gelu` 的 kernel 对比，观察真实算子在多架构目录（arch35）、分支与模板上的工程化差异。若想巩固本讲概念，建议先重读 [aicore_develop_guide.md](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md) 中 Kernel 实现一节的流程图与代码骨架，再进入下一讲。
