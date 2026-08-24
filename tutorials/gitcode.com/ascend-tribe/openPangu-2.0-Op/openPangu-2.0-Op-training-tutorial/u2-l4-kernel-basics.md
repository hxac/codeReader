# op_kernel 入门：Ascend C 设备侧 Kernel 的结构

## 1. 本讲目标

上一讲（u2-l3）我们读懂了 Host 侧的 Tiling：它在 Kernel 启动前完成"作战规划"，产出 blockDim、tilingKey、TilingData、workspace 四项契约。本讲跨到设备侧，拆开 `op_kernel` 目录的三个文件，看完"士兵如何执行这份作战计划"。

学完本讲，你应该能够：

1. 读懂 kernel 入口 `extern "C" __global__ __aicore__` 函数的参数约定，解释 `TILING_KEY_IS` 与 `GET_TILING_DATA_WITH_STRUCT` 两个宏各自做什么。
2. 描述 `KernelAiInfraAggregateHidden<DTYPE>` 模板类的 Init / Process 两段式结构，说清 TPipe、TQue、TBuf 的角色。
3. 对照 tiling.h 里的 13 个 TilingData 字段，在 kernel 源码中逐一找到消费点。
4. 列出"给这个算子新增 fp32 支持"需要改动的 def / tiling / kernel 三处完整清单。

本讲继续以 mome 家族的 `ai_infra_aggregate_hidden`（hidden 层 token 间一维分组卷积）为解剖标本。

## 2. 前置知识

### 2.1 从"规划"到"执行"：本讲在四层模型中的位置

回顾 u1-l2 建立的四层模型：`_def.cpp`（原型注册）→ `_tiling.cpp`（Host 侧切分）→ kernel（设备侧计算）→ aclnn（对外接口）。本讲的 op_kernel 是真正跑在 AI Core 上的部分。

Host 侧与设备侧通过三样东西衔接（第四样 workspace 本算子未实际使用）：

| Host 侧（tiling.cpp）写 | 设备侧（kernel）读 |
|---|---|
| `SetBlockDim(blockDim)` 决定启动多少个核 | 每个核用 `GetBlockIdx()` 区分"我是谁" |
| `SetTilingKey(tilingKey)` 写入启动参数 | 入口用 `TILING_KEY_IS(k)` 判断走哪个分支 |
| `SaveToBuffer` 把 TilingData 序列化进 GM | 入口用 `GET_TILING_DATA_WITH_STRUCT` 解包成结构体 |

### 2.2 昇腾 AI Core 的存储与执行模型（新手术语）

- **GM（Global Memory）与 UB（Unified Buffer）**：GM 是 Device 上的大容量 DDR 显存，放输入输出张量；UB 是每个 AI Core 私有的高速缓存（百 KB 量级）。数据必须先搬到 UB 才能参与向量/矩阵计算。kernel 代码的本质就是"GM→UB→计算→UB→GM"的搬运与计算编排。
- **AIV 与 AIC**：向量核（AI Vector，执行 Mul/Add/Cast 等逐元素指令）与矩阵核（AI Cube，执行 Matmul）。本算子只有逐元素乘加，因此只用 AIV——入口的 `KERNEL_TYPE_AIV_ONLY` 和 tiling 只查询 `GetCoreNumAiv()` 互相印证。
- **SPMD 模型**：同一份 kernel 代码会被烧到 blockDim 个核上同时执行，每个核执行相同的代码、不同的数据。核内用 `GetBlockIdx()`（0 到 blockDim-1）反推自己负责哪一块数据。
- **TPipe / TQue / TBuf**：`TPipe` 是 UB 内存与队列同步的统一管理器；`TQue` 是带生产者-消费者同步语义的队列（`EnQue`/`DeQue` 会在搬运引擎 MTE 与向量引擎 V 之间插同步点）；`TBuf` 是不带队列语义的裸计算缓冲。
- **向量原语**：`Mul`、`Add`、`Duplicate`（填充常量）、`Cast`（类型转换）、`DataCopyPad`（GM↔UB 分块搬运）。这些都是 CANN 头文件 `kernel_operator.h` 提供的 Ascend C 原语。
- **`__aicore__` / `__gm__` / `__global__`**：编译器扩展修饰符。`__global__ __aicore__` 标记这是一个设备侧 AI Core 入口函数；`__gm__` 标记指针指向 GM 地址空间；`__aicore__ inline` 让成员函数在设备侧内联展开（设备侧不支持普通函数调用的开销模型）。
- **`PipeBarrier<PIPE_V>()`**：同一向量管线内前后指令有数据依赖时，插入屏障保证顺序执行。

### 2.3 本算子在算什么（承接 u2-l1 的公式）

对 [S,B,H] 的输入沿 S 维做窗口 W=3 的因果一维分组卷积，H 维各通道独立：

\[ out[s, h] = \sum_{k=0}^{2} x[s-k, h] \cdot w[k, h] \]

其中越界的 \( x[-1] = x[-2] = 0 \)（因果零填充）；若提供 [B,S] 的 bool mask，false 位置输出置 0。kernel 把这个三点卷积改写成**沿 S 的递推形式**，这是读懂本算子 kernel 的钥匙，见 4.2.2。

## 3. 本讲源码地图

| 文件 | 规模 | 角色 |
|---|---|---|
| [op_kernel/ai_infra_aggregate_hidden.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp) | 42 行 | kernel 入口：tilingKey 分支 + TilingData 解包 + 实例化模板类 |
| [op_kernel/ai_infra_aggregate_hidden.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h) | 249 行 | Kernel 模板类：Init / Process / CopyIn / Compute / CopyOut |
| [op_kernel/ai_infra_aggregate_hidden_common.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden_common.h) | 107 行 | 公共基类 CutHBS：核间索引分解、尾块处理、共享成员 |
| [op_host/ai_infra_aggregate_hidden_tiling.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h) | 159 行 | TilingData 结构定义（本讲只引用其字段，详解见 u2-l3） |
| [op_host/ai_infra_aggregate_hidden_tiling.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp) | 483 行 | Host 侧切分（本讲只引用关键行，详解见 u2-l3） |

下文链接文本中，路径前缀 `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/` 简写为 `…/`。

## 4. 核心概念与源码讲解

### 4.1 kernel 入口：extern "C" __global__ __aicore__ 函数

#### 4.1.1 概念说明

kernel 入口是 CANN 运行时按**算子名**查找并下发的设备函数，本算子的入口全名就是 `ai_infra_aggregate_hidden`——与 `_def.cpp` 注册的算子名、tiling 的 `IMPL_OP_OPTILING` 绑定名完全一致，这是四层对齐的最终落点。

三个关键约定：

1. **`extern "C"`**：关闭 C++ 的名字改编（name mangling），让运行时能用字符串符号名找到这个函数。
2. **六个 `GM_ADDR` 参数的顺序是死的**：先按 `_def.cpp` 中 `Input()`/`Output()` 的声明顺序排（input、weight、mask、output），再由框架追加 `workspace` 和 `tiling` 两个运行期参数。这直接印证了 u2-l2 的结论"Input 声明顺序即运行期索引"。
3. **mask 是可选输入**：不传时框架会传入空指针，所以 kernel 绝不能无条件绑定 mask 的 GM 地址——入口之后的 `Init()` 里用 `tilingData_->ifMask` 做了保护（4.2.3）。

入口本身不写计算逻辑，它只做三件事：选分支（按 tilingKey）、解包（TilingData）、把活交给模板类。

#### 4.1.2 核心流程

```text
运行时下发 kernel(blockDim 个核, 携带 tilingKey 与 tiling 字节流)
        │
        ▼
ai_infra_aggregate_hidden(input, weight, mask, output, workspace, tiling)
        │
        ├─ KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)   # 声明：纯向量核任务
        ├─ 栈上构造 TPipe pipe                                # UB 管理器
        │
        ├─ 若 TILING_KEY_IS(0)：   # BF16
        │     GET_TILING_DATA_WITH_STRUCT(...)  # GM 字节流 → 本地结构体
        │     KernelAiInfraAggregateHidden<bfloat16_t> op(&pipe, tilingData)
        │     op.Init(input, weight, mask, output); op.Process()
        │
        └─ 否则若 TILING_KEY_IS(1)： # FP16，同上但模板参数为 half
```

#### 4.1.3 源码精读

入口全文只有 42 行，逐段看：

**（1）tilingKey 常量的双侧定义。** [ai_infra_aggregate_hidden.cpp:21-22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L21-L22) 定义了 `AGGREGATE_HIDDEN_BF16=0`、`AGGREGATE_HIDDEN_HALF=1`；同样的两个宏在 […/op_host/ai_infra_aggregate_hidden_tiling.h:19-20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L19-L20) 也定义了一份。**Host 侧写 key、设备侧读 key，两份常量的数值必须永远一致**——这是隐式契约，没有编译期检查，改了一处忘了另一处就会静默出错（见 4.1.5 练习 1）。

**（2）函数签名与任务类型。** [ai_infra_aggregate_hidden.cpp:24-28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L24-L28)：六个 `GM_ADDR` 依次是 input、weight、mask、output、workspace、tiling；`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)` 声明本 kernel 只在向量核上运行，与 tiling 侧只按 AIV 核数规划 blockDim（[…/ai_infra_aggregate_hidden_tiling.cpp:85-87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L85-L87) 取 `GetCoreNumAiv()`）互为印证。`TPipe pipe` 在栈上构造，随后以指针传给 kernel 类。

**（3）BF16 分支。** [ai_infra_aggregate_hidden.cpp:29-34](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L29-L34)：

- `TILING_KEY_IS(AGGREGATE_HIDDEN_BF16)`：判断运行时带来的 tilingKey 是否等于 0。这个 key 正是 Host 侧 [TilingForAiInfraAggregateHidden 的 DoTiling](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L455-L457) 里 `context_->SetTilingKey(tilingInfo->tilingKey)` 写进去的，而 key 的取值由 [GetTilingKey](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L282-L289) 决定：默认 BF16(0)，输入是 `DT_FLOAT16` 则 FP16(1)。**Host 写、Device 读，至此闭环。**
- `GET_TILING_DATA_WITH_STRUCT(AiInfraAggregateHiddenTilingData, tiling_data_in, tiling)`：把 GM 上 `tiling` 指向的字节流解包成本地结构体实例 `tiling_data_in`。Host 侧的序列化在 [DoTiling 的 SaveToBuffer](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L452-L453)。设备侧能直接使用 `AiInfraAggregateHiddenTilingData` 这个类型名，靠的是第 16 行 `#include "kernel_tiling/kernel_tiling.h"`——**这个头文件不在仓库里**，它是构建期由 CANN 工具从 `_tiling.h` 的 `BEGIN_TILING_DATA_DEF` 块（[ai_infra_aggregate_hidden_tiling.h:43-57](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L43-L57)）生成的设备侧镜像（生成机制见 u1-l4 的 autogen 流程）。两个宏本身也由 CANN 工具链头文件提供，本仓库只使用不定义。
- 随后 `const AiInfraAggregateHiddenTilingData *__restrict tilingData = &tiling_data_in;` 取只读指针，构造 `KernelAiInfraAggregateHidden<bfloat16_t>` 并执行 `Init()` + `Process()`。

**（4）FP16 分支。** [ai_infra_aggregate_hidden.cpp:35-41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L35-L41) 与 BF16 分支逐行对称，仅模板参数换成 `half`。**注意 if / else-if 链没有 else 兜底**：若 tilingKey 不在 {0, 1}，kernel 什么都不做直接返回——因此"tilling 侧可能写的每一个 key 值，入口必须有对应分支"是硬纪律。

#### 4.1.4 代码实践：跟踪一帧 fp16 调用的跨侧链路（纯源码阅读型）

1. **实践目标**：把"用户传入 fp16 张量"到"kernel 走 `half` 分支"的完整链路用行号串起来，体会 tilingKey 的跨侧闭环。
2. **操作步骤**：
   - 从 [ai_infra_aggregate_hidden_def.cpp:24-29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L24-L29) 确认 input 允许 `DT_FLOAT16`；
   - 在 [CheckInputValid](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L117-L120) 找到读取该类型并记入 `inputType_` 的行；
   - 在 [GetTilingKey](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L284-L288) 找到 `DT_FLOAT16 → tilingKey_=1` 的赋值；
   - 在 [DoTiling](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L457) 找到 `SetTilingKey`；
   - 在 kernel 入口 [L29 与 L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L29-L41) 找到消费该值的 `TILING_KEY_IS`。
3. **需要观察的现象**：每个环节在源码中的具体行号。
4. **预期结果**：得到一条 5 步链路（def 声明类型 → tiling 读类型 → GetTilingKey 置 1 → SetTilingKey 写入 → kernel TILING_KEY_IS(1) 命中 `half` 分支）。本实践无需 NPU，纯阅读即可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果把 tiling 侧 `GetTilingKey` 的 `if (inputType_ == ge::DT_FLOAT16)` 分支删掉（恒返回 0），用 fp16 输入调用会发生什么？
**答案**：tiling 永远写 key=0，kernel 永远走 BF16 分支，`half` 数据被当作 `bfloat16_t` 解释，位宽相同（都是 2 字节）所以不会有任何报错，但数值解释完全错误——输出是静默的乱码。这正是"tillingKey 双侧一致"必须靠纪律维护的原因。

**练习 2**：入口六个参数的顺序由什么决定？mask 不传时这个参数是什么？
**答案**：前四个由 `_def.cpp` 中 Input/Output 的声明顺序决定（input、weight、mask、output），后两个（workspace、tiling）由框架固定追加。mask 缺省时框架传空指针，kernel 靠 `tilingData_->ifMask`（tiling 侧由 [CheckMaskValid](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L198-L203) 写入）决定是否绑定，绝不解引用空指针。

**练习 3**：`GET_TILING_DATA_WITH_STRUCT` 用到的类型 `AiInfraAggregateHiddenTilingData` 定义在哪里？
**答案**：Host 侧定义在 `_tiling.h` 的 `BEGIN_TILING_DATA_DEF`/`END_TILING_DATA_DEF` 宏块（L43-57）中；设备侧使用的是构建期生成的 `kernel_tiling/kernel_tiling.h` 中的镜像定义，该文件由 CANN 工具链生成、不在本仓库内。

### 4.2 Kernel 模板类：Init / Process 两段式与 fp32 中间状态

#### 4.2.1 概念说明

`ai_infra_aggregate_hidden.h` 定义模板类 `KernelAiInfraAggregateHidden<DTYPE>`，继承公共基类 `KernelAiInfraAggregateHiddenCutHBS<DTYPE>`（4.3 详讲）。它解决三件事：

1. **一份代码、两种精度**：bf16/fp16 的差异被收敛为模板参数 `DTYPE`，入口两个分支实例化同一份逻辑，避免维护两份 kernel。
2. **Init / Process 两段式**：`Init()` 做"一次性"工作——绑定 GM 地址、按 tiling 结果划分 UB；`Process()` 做"每帧"工作——数据搬运与计算主循环。这是 Ascend C kernel 最普遍的组织范式。
3. **递推状态与混合精度**：输出沿 S 有依赖（每个 token 依赖前两个 token），kernel 在 UB 里维护三个 fp32 中间状态 y0/y1/y2 跨越内层循环复用；GM 进出用低精度（bf16/fp16），UB 计算一律升到 fp32——存储省一半带宽，累加不吃精度损失。

`TPipe` 的角色在 Init 里看得最清楚：所有 `pipe_->InitBuffer(...)` 调用把 UB 划分给各队列/缓冲，之后一切 Alloc/EnQue/DeQue 都由它统一管理。

#### 4.2.2 核心流程

三点卷积写成沿 S 的递推。设 \( x_s \) 为当前 token 的一个 H 切片（向量），进入第 \( s \) 轮迭代时 UB 中保有：

\[ y_0 = x_{s-1} \cdot w_0, \qquad y_1 = x_{s-1} \cdot w_1 + x_{s-2} \cdot w_0 \]

每轮做三步更新：

\[ y_2 = x_s \cdot w_2 + y_1 \;\;(\text{即 } out[s]), \qquad y_1 \leftarrow x_s \cdot w_1 + y_0, \qquad y_0 \leftarrow x_s \cdot w_0 \]

展开即还原三点卷积：\( y_2 = x_s w_2 + x_{s-1} w_1 + x_{s-2} w_0 \)。初值：序列开头 \( y_0 = y_1 = 0 \)（等价零填充）；S 维被切分时从 GM 回读前两个 token 重建初值（`InitY`）。

主流程（文字图）：

```text
Init()
 ├─ InitSharedData()            # 基类：反解 blockIdx → (baseHIdx, baseBIdx, baseSIdx)，处理尾块
 ├─ SetGlobalBuffer ×4          # 绑 input/weight/output；mask 仅 ifMask=1 时绑
 └─ pipe_->InitBuffer ×7        # 划分 UB（预算表见 4.2.3）

Process()
 ├─ 权重预热：AllocTensor(inQueueW)
 │    → DataCopyPad 搬 3 行权重（DTYPE 视图）
 │    → EnQue/DeQue（搬运↔计算同步点）
 │    → Cast ×3：DTYPE 视图 → 紧凑 fp32 三段
 │    → EnQue/DeQue（再同步）
 ├─ for bIdx in [0, baseB):
 │    ├─ InitY()：S 首块？零填充(Duplicate) ：回读 input[s-2]/input[s-1] 重建 y0/y1
 │    └─ for sIdx in [0, baseS):
 │         ├─ CopyIn()： AllocTensor(inQueueX) → DataCopyPad(GM→UB)
 │         ├─ EnQue/DeQue → Cast(x → fp32) → FreeTensor
 │         ├─ Compute()： y2 = x·w2 + y1（mask 为 false 时 Duplicate 0）；更新 y1、y0
 │         └─ CopyOut()： AllocTensor(outQueue) → Cast(fp32→DTYPE) → EnQue/DeQue
 │                        → DataCopyPad(UB→GM) → FreeTensor
 └─ FreeTensor(weightFp32)
```

#### 4.2.3 源码精读

**（1）Init：绑 GM + 划 UB。** [ai_infra_aggregate_hidden.h:30-52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L30-L52)：

- 第 33-38 行把入口收到的 4 个 `GM_ADDR` 绑成 `GlobalTensor`：input [S,B,H]、weight [3,H]、output [S,B,H]；[第 35-37 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L35-L37) 只有 `tilingData_->ifMask` 为真才绑 `maskGm`——可选输入的空指针保护就在这里。
- [第 41-51 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L41-L51) 七次 `InitBuffer` 划分 UB。以 `alignBaseH=4096`（H 维全载上限，即 tiling 侧 `H_SIZE_FULL`）、DTYPE 为 2 字节估算：

| 缓冲 | 分配表达式 | 大小（注释中的估算） | 用途 |
|---|---|---|---|
| inQueueX | 2 × alignBaseH×sizeof(DTYPE) | 16K | 输入 x 双缓冲队列 |
| y2 / y1 / y0 | 各 alignBaseH×4B | 各 16K，共 48K | 递推状态（fp32，TBuf） |
| inQueueW | 2 × 3×alignBaseH×4B | 96K | 权重队列（按 fp32 尺寸分配） |
| inputFp32 | alignBaseH×4B | 16K | x 的 fp32 暂存（TBuf） |
| outQueue | 2 × alignBaseH×sizeof(DTYPE) | 16K | 输出双缓冲队列 |

合计约 192K——**这就是 tiling 侧 `H_SIZE_FULL = 4096`（[…/ai_infra_aggregate_hidden_tiling.cpp:63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L63) 注释"UB 全载时 H 最大是 4096"）在 kernel 侧的对应物**：Host 的切分上限由 Device 的 UB 预算倒推决定，两侧必须匹配。（具体芯片 UB 总容量请以硬件手册为准，待本地验证。）

**（2）Process 权重预热：一块 UB 的两种视图。** [ai_infra_aggregate_hidden.h:56-82](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L56-L82)。`inQueueW` 按 **fp32 尺寸**（3×alignBaseH×4B）分配，却先用 **DTYPE 视图**接收数据：第 57-60 行 `AllocTensor<float>()` 后立刻 `ReinterpretCast<DTYPE>()` 切出 localW0/localW1/localW2 三个切片；第 61-69 行用 `DataCopyPad` 一次搬 3 行（`blockCount=3`，每行 `blockLen = baseH * sizeof(DTYPE)` 字节，源端跳过 `(hSize - baseH)` 的列间隙）。随后第 70-71 行 `EnQue/DeQue` 插入第一个同步点（确保搬运完成），第 75-80 行把三行 `Cast` 成紧凑排列的 fp32 三段（`weightFp32[0]`、`[alignBaseH]`、`[2*alignBaseH]`），第 81-82 行再 `EnQue/DeQue` 一次。此后整个 S 循环里权重直接以 fp32 参与计算，省掉每 token 的三次 Cast。

两个读码提示：其一，Cast 的源（DTYPE 视图切片）与目的（fp32 视图切片）刻意落在同一块 UB 上，是空间复用写法，改动时须小心区间关系；其二，[第 66 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L66) `dstStride` 显式除以了 `BLOCK_SIZE(=32)` 而 `blockLen` 以字节计——`DataCopyExtParams` 各字段单位以所用 CANN 版本的 Ascend C API 参考为准（待确认），进阶练习可查证。

**（3）主循环与三次阶段函数。** [ai_infra_aggregate_hidden.h:87-100](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L87-L100)：外层 `bIdx`、内层 `sIdx` 双循环，每 token 依次 `CopyIn → Cast(fp32) → Compute → CopyOut`，一读一算一写，正是 4.2.2 文字图的内层展开。

- **InitY**（[L104-160](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L104-L160)）：`baseSIdx == 0`（本核从序列头开始）时 `Duplicate` 把 y0/y1 清零，等价零填充；否则回读 GM 上前两个 token（`preSIdx = baseS*baseSIdx - 2`），按递推式重建 y0/y1（第 141-157 行的 Mul/Add 序列）。特别地，[第 129-131 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L129-L131)处理 `startSIdx==1` 时 s-2 越界的情形（填零）。这解释了一个 tiling 取舍：**默认不切 S（baseSCnt=1）正是为了让递推留在单核内**，只有 B 切完后还剩核才切 S（见 u2-l3 CoreSplit）。
- **CopyIn**（[L162-177](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L162-L177)）：计算 GM 偏移 `seqIdx*(bSize*hSize) + batchIdx*hSize + baseHIdx*baseH`，把当前 token 的 H 切片搬进 UB。注意 [第 173 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L173) 块长用**本核修正后**的 `this->baseH`（尾块核取尾块大小），而 [第 168 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L168) 列偏移用 **TilingData 里的原始 `baseH`**（前面的块都是整块）——权重搬运处第 67-68 行的注释专门强调了这一区分。
- **Compute**（[L179-217](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L179-L217)）：无 mask 时三步纯向量运算（L205-215）；有 mask 时先算 `tokenIdx = batchIdx*sSize + seqIdx`，用 `this->maskGm(tokenIdx)` **标量读** GM 上的一个 bool，再经 `SetWaitFlag<HardEvent::S_V>`（标量单元→向量单元的依赖同步，实现在 [common.h:31-37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden_common.h#L31-L37)）保证读回后再发向量指令；mask 为 false 时 `Duplicate` 清零输出。**注意第 210-215 行的 y1/y0 更新在 mask 分支之外**——mask 只掩当前输出，不打断因果递推。
- **CopyOut**（[L219-243](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L219-L243)）：fp32 结果 Cast 回 DTYPE 再搬回 GM。[第 224-227 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L224-L227) 用 `if constexpr` 按编译期类型选舍入模式：bf16 用 `CAST_RINT`（就近偶舍入），fp16 用 `CAST_NONE`。**这个 if constexpr 链没有 float 分支**——这正是综合实践中"新增 fp32"必须动 kernel 的一处。

#### 4.2.4 代码实践：画 Init→Process 调用图并标注 Ascend C 原语

1. **实践目标**：把 4.2.2 的文字图落实为一张带原语标注的调用图，检验对 kernel 结构的掌握。
2. **操作步骤**：以 `ai_infra_aggregate_hidden.h` 为准（不看本讲答案先自己画），为每个阶段标注用到的 Ascend C 原语/设施；完成后与下方参考对照。
3. **参考答案**（调用图 + 原语分类）：

   ```text
   KernelAiInfraAggregateHidden<DTYPE>
   ├─ Init                          [L30-52]
   │   ├─ InitSharedData            （基类，无原语，纯整数运算）
   │   ├─ SetGlobalBuffer ×4        ← GlobalTensor 绑定
   │   └─ pipe_->InitBuffer ×7      ← TPipe UB 划分（TQue×3、TBuf×4）
   └─ Process                       [L54-102]
       ├─ 权重预热 [L56-82]
       │   ├─ AllocTensor / EnQue / DeQue / FreeTensor   ← TQue 生命周期 + 同步
       │   ├─ ReinterpretCast                          ← LocalTensor 视图切换
       │   ├─ DataCopyPad + DataCopyExtParams/DataCopyPadExtParams ← GM→UB 分块搬运
       │   └─ Cast ×3 (CAST_NONE)                       ← DTYPE→fp32
       ├─ for bIdx: InitY [L104-160]
       │   ├─ Duplicate + PipeBarrier<PIPE_V>           ← 首块清零
       │   └─ DataCopyPad / Cast / Mul / Add            ← 回读重建 y0/y1
       └─ for sIdx:
           ├─ CopyIn   [L162-177]  DataCopyPad（GM→UB）
           ├─ Cast     [L95]       x: DTYPE→fp32
           ├─ Compute  [L179-217]  Mul / Add / Duplicate / PipeBarrier
           │                       SetWaitFlag<S_V>（读 mask 标量后同步）
           └─ CopyOut [L219-243]  Cast（RINT/NONE）+ DataCopyPad（UB→GM）
   ```

   原语按职能分四类记忆：**搬运**（DataCopyPad）、**计算**（Mul/Add/Duplicate/Cast）、**同步**（EnQue/DeQue、PipeBarrier、SetWaitFlag）、**内存管理**（SetGlobalBuffer/InitBuffer/AllocTensor/FreeTensor/ReinterpretCast）。
4. **需要观察的现象**：自己画的图与参考答案的差异点（多数初学者会漏掉权重预热的同步点和 SetWaitFlag）。
5. **预期结果**：能不看源码复述"一帧 token 的完整生命周期：AllocTensor→DataCopyPad→EnQue→DeQue→Cast→Compute→Cast→EnQue→DeQue→DataCopyPad→FreeTensor"。无需 NPU，纯阅读完成。

#### 4.2.5 小练习与答案

**练习 1**：GM 上进出都是 bf16/fp16，为什么 Compute 内部全程用 fp32 LocalTensor？
**答案**：混合精度策略。三点卷积沿 S 累加，低精度直接累加会放大舍入误差；因此搬入 UB 后立即 `Cast` 升到 fp32（L95），y0/y1/y2 状态也按 fp32 存放（InitBuffer 按 `sizeof(float)` 分配，L43-45），只在 CopyOut 出口降回低精度。存储（GM）省带宽，计算（UB）保精度。

**练习 2**：mask 为 false 的位置，y0/y1 递推状态会被清零吗？
**答案**：不会。mask 分支（L195-203）只决定 y2 是正常累加还是 `Duplicate` 清零，L210-215 的 y1/y0 更新在分支之外照常执行——即 mask 只屏蔽输出，不打断窗口递推。这决定了"被 mask 的 token 仍会贡献到后继 token 的输出"。

**练习 3**：为什么权重要在每个核的 Process 开头各搬一次并转成 fp32，而不是放在 Init 里做一次？
**答案**：SPMD 模型下各核的 UB 相互隔离，权重必须每核一份；放在 Process 开头（而非 Init）搬，可以借队列机制先让搬运引擎与后续向量指令并行起来。代价是每核 96K 的 UB 占用与每核一次的冗余搬运（3 行 × alignBaseH），相对于 S 循环内每 token 省掉三次 Cast 是划算的。

### 4.3 公共基类 CutHBS：核间索引分解、尾块与共享成员

#### 4.3.1 概念说明

`ai_infra_aggregate_hidden_common.h` 的 `KernelAiInfraAggregateHiddenCutHBS` 是 kernel 侧的"共享底座"（CutHBS = 按 H/B/S 三个维度切分）。它解决 SPMD 的根本问题：**TilingData 是全核共享的同一份字节流，每个核必须自行反解"我负责哪个 tile"**。此外它还集中放置了三类共享内容：数值常量、标量-向量同步小工具、以及 GM/UB 成员变量——把"与算法无关的机制代码"从算法类里剥离，这是该仓库 kernel 代码的通用组织手法（`_common.h` 命名约定）。

#### 4.3.2 核心流程

Host 侧 [CoreSplit 的收尾三行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L348-L350)：

\[ blockDim = baseHCnt \times baseBCnt \times baseSCnt \]

设备侧 `InitSharedData` 用三级取模把 `GetBlockIdx()` 分解回三维坐标（H 变化最快、S 最慢，与乘法因子的排列一一对应）：

```text
baseHIdx = blockIdx % baseHCnt
baseBIdx = (blockIdx / baseHCnt) % baseBCnt
baseSIdx = (blockIdx / (baseHCnt * baseBCnt)) % baseSCnt
```

再按"我是不是某一维的最后一个块"用 Tail 字段覆盖块大小：`baseH/baseHTail`、`baseB/baseBTail`、`baseS/baseSTail` 六个字段两两配对（整块大小 / 末块大小）。这套分解与 u2-l3 讲过的 H→B→S 三级级联切分严格互逆。

#### 4.3.3 源码精读

**（1）常量与同步工具。** [ai_infra_aggregate_hidden_common.h:24-29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden_common.h#L24-L29) 定义 NUM_ZERO~NUM_THREE、`BLOCK_SIZE=32`（32 字节块，搬运转换单位）、`ALIGN_SIZE=16`（f16/bf16 每元素 2 字节，16 个元素恰为 32 字节对齐，见第 29 行注释）。[第 31-37 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden_common.h#L31-L37) 的 `SetWaitFlag<event>` 是 FetchEventID + SetFlag + WaitFlag 三连，服务于 Compute 里"标量读 mask 后再做向量运算"的跨单元依赖。

**（2）AlignData。** [ai_infra_aggregate_hidden_common.h:46-49](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden_common.h#L46-L49)：向上取整到 16 的倍数 \( \lceil a/16 \rceil \times 16 \)。H 约束本身是 192 的倍数，但核数调整（u2-l3 的 48/5→6、40/6→8 特判）会把 baseH 切成非 192 倍数，此时靠它兜底对齐。

**（3）InitSharedData：身份反解 + 尾块覆盖。** [ai_infra_aggregate_hidden_common.h:51-76](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden_common.h#L51-L76)：第 53-55 行先取整块大小；第 56-59 行按 4.3.2 的公式分解 `GetBlockIdx()`；第 62-63 行对 H 计算 32 字节对齐后的 `alignBaseH`；第 66-75 行三段 if 分别在"我是该维最后一个块"时用 Tail 值覆盖 baseH/baseS/baseB。此后算法类使用的 `this->baseH` 等即已是"本核真实块大小"，而 GM 偏移计算仍用 `tilingData_->baseH`（整块大小）——4.2.3（3）已经见过这对区分。

**（4）成员区。** [ai_infra_aggregate_hidden_common.h:78-103](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden_common.h#L78-L103)：整数下标（L80-87）、TPipe 指针与 TilingData 只读指针（L90-91）、四个 `GlobalTensor`（L93-94）、三个 `TQue`（VECIN×2 + VECOUT，L96-98）、四个 `TBuf`（y0/y1/y2/inputFp32，L99-102）。对照 4.2.3 的 UB 预算表，每个成员一一对应一块 UB。

**（5）TilingData 13 个字段的设备侧消费总表。**（本讲学习目标 3 的完整答案，路径列 `K:` = op_kernel 目录）

| TilingData 字段（tiling.h L43-57） | kernel 侧消费位置 | 用途 |
|---|---|---|
| ifMask | K/ai_infra_aggregate_hidden.h L35、L189 | 是否绑 maskGm；Compute 是否走 mask 分支 |
| hSize | 同文件 L65、L117-118、L119-120、L167-168、L234-235 | GM 偏移步长（行与行之间隔 bSize×hSize 等） |
| bSize | 同文件 L117、L119、L165、L167、L190、L233、L234 | GM 偏移步长、batchIdx 还原 |
| sSize | 同文件 L192 | mask 一维下标 \( batchIdx \times sSize + seqIdx \) |
| baseH | 同文件 L64、L67-68、L126、L173、L238 | 搬运块长与 H 列偏移（列偏移用整块值） |
| baseB | 同文件 L87、L116、L165、L190、L233 | 本核 B 循环次数、batchIdx 还原 |
| baseS | 同文件 L89、L114、L166、L191、L232 | 本核 S 循环次数、seqIdx 还原 |
| baseHTail | K/ai_infra_aggregate_hidden_common.h L63、L67 | H 末块覆盖 |
| baseBTail | 同文件 L74 | B 末块覆盖 |
| baseSTail | 同文件 L71 | S 末块覆盖 |
| baseHCnt | 同文件 L56-L59、L66 | blockIdx 三级分解、末块判定 |
| baseBCnt | 同文件 L57-L59、L73 | 同上 |
| baseSCnt | 同文件 L59、L70 | 同上 |

13 个字段全部被消费，无一冗余——TilingData 的字段集就是 Host 与 Device 之间的全部接口。

#### 4.3.4 代码实践：手推一个 48 核算例（纯计算，无需 NPU）

1. **实践目标**：给定 shape 与核数，完整推一遍"tiling 切分 → 每核身份 → 每核任务"，验证对两侧机制的理解。
2. **操作步骤**：
   - 设输入 [S,B,H] = [1024, 8, 8192]，`aivNum=48`。
   - 按 [CoreSplit](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L291-L353) 推导 13 个字段与 blockDim；
   - 再按 InitSharedData 分解 blockIdx=47，求该核的 (baseHIdx, baseBIdx, baseSIdx) 与修正后的 (baseH, baseB, baseS)，写出它负责的 token 范围与 H 列范围；
   - 有环境时可用 [DumpTilingInfo](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L379-L396) 的 OP_LOGD 日志核对（待本地验证）。
3. **参考推导**：baseHCnt = ⌈8192/4096⌉ = 2（不命中 48&5 特判）；baseH = baseHTail = 4096；coreNumH = 48/2 = 24；baseB = ⌈8/24⌉ = 1，baseBCnt = 8，baseBTail = 1；baseB==1 所以切 S：baseSCnt = 24/8 = 3，baseS = ⌈1024/3⌉ = 342，baseSTail = 1024 − 342×2 = 340；blockDim = 2×8×3 = 48（AIV 满载）。
   核 47：baseHIdx = 47%2 = 1；baseBIdx = (47/2)%8 = 7；baseSIdx = (47/16)%3 = 2。它是 S 维末块（baseS 修正为 340）与 B 维末块（baseB=1），H 维"末块" baseHTail=4096 与整块相同。该核负责：B 序号 7，S 第 684~1023 号 token（共 340 个），H 第 4096~8191 列。
4. **需要观察的现象**：手推结果与 DumpTilingInfo 日志逐字段一致。
5. **预期结果**：任给一个 blockIdx ∈ [0,48)，都能说出该核的 tile；体会到 blockDim 恰为 48 不是巧合，而是 CoreSplit 的设计目标（核数整除特判服务于它）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TilingData 不直接为每个核写好各自的 (baseH, baseB, baseS)，而要每核自己分解？
**答案**：TilingData 是全核共享的同一份字节流（入口的 `tiling` 参数对所有核可见且相同），按核写会导致体积随核数膨胀；SPMD 下标准做法是传"切分方案"，各核用 `GetBlockIdx()` 自行反解。三级取模的顺序（H 最快、S 最慢）与 blockDim 乘法因子排列一一对应。

**练习 2**：`ALIGN_SIZE=16` 的注释写"f16/bf16 对齐到 32 字节"，16 和 32 是什么关系？
**答案**：16 是**元素个数**，f16/bf16 每元素 2 字节，16×2=32 字节，正好满足搬运引擎对 32 字节块（`BLOCK_SIZE=32`）的对齐要求。若未来支持 fp32（每元素 4 字节），对齐到 32 字节只需 8 个元素，该常量语义要重新审视（见综合实践）。

**练习 3**：基类为什么叫 CutHBS？把它从算法类拆出来有什么好处？
**答案**：它封装"按 H/B/S 三维切分"这一与本算子算法无关的机制（身份反解、尾块覆盖、共享成员）。拆出来后 `ai_infra_aggregate_hidden.h` 只剩纯算法（CopyIn/Compute/CopyOut），同族算子（如反向 `ai_infra_aggregate_hidden_grad`）可复用同样的底座结构，降低阅读与维护成本。

## 5. 综合实践：新增 fp32 支持需要改哪三处

**任务**：算子当前只支持 bf16/fp16。假设产品要求增加 fp32（DT_FLOAT）输入输出，请列出完整改动清单。这是对"四层联动"的终极检验——答案不止三处编辑，但按文件归组恰好是 def / tiling / kernel 三层（kernel 层含两个文件）。

**参考改动清单**（行号基于当前 HEAD）：

**第一层：原型注册 —— `_def.cpp`**

1. [ai_infra_aggregate_hidden_def.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L24-L71) 中共有 **6 处** `DataType({ge::DT_BF16, ge::DT_FLOAT16})` 列表（外层 Input×3/Output + `aicore_config` Input×3/Output，即 L26、L32、L44、L51、L57、L69），需在**所有列表的同一位置**追加 `ge::DT_FLOAT`。注意仓库约定：所有张量的类型列表长度一致、按位组成"类型组合"（mask 处是 `{DT_BOOL, DT_BOOL}` 与之相容），因此必须 6 处同步追加，漏一处就会导致类型组合不匹配。

**第二层：Tiling —— `_tiling.h` + `_tiling.cpp`**

2. [ai_infra_aggregate_hidden_tiling.h:19-20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h#L19-L20)：新增 `#define AGGREGATE_HIDDEN_FLOAT 2`。
3. [CheckInputValid 的类型检查](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L117-L120)（L118-120 只放行 float16/bfloat16）：增加 `DT_FLOAT` 分支，同时确认 weight/output 的同型校验逻辑无需改动（它们只断言"与 input 相同"）。
4. [GetTilingKey](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L282-L289)：新增 `DT_FLOAT → AGGREGATE_HIDDEN_FLOAT` 分支。

**第三层：Kernel —— `op_kernel/` 两个文件**

5. [ai_infra_aggregate_hidden.cpp:21-22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L21-L22)：新增与 tiling.h **同值**的 `#define AGGREGATE_HIDDEN_FLOAT 2`；在 [L35-41 的 else-if 链](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L35-L41) 后追加 `TILING_KEY_IS(AGGREGATE_HIDDEN_FLOAT)` 分支，实例化 `KernelAiInfraAggregateHidden<float>`。
6. [ai_infra_aggregate_hidden.h 的 CopyOut](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L224-L227)：`if constexpr` 链没有 `float` 分支（fp32 出口无需 Cast），需补 `else if constexpr (std::is_same<T, float>::value)` 直通分支。`Init` 里的 InitBuffer 尺寸全部乘了 `sizeof(DTYPE)`，fp32 会自动放大一倍——**需要重新核算 UB 预算**（按 4.2.3 的表重算：inQueueX 与 outQueue 各从 16K 涨到 32K，y0/y1/y2、inQueueW、inputFp32 本就按 fp32 分配不变，合计从约 192K 涨到约 224K），可能超出 UB 容量，届时应下调 tiling 侧 `H_SIZE_FULL`（[_tiling.cpp:63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L63)），并同步检查 `_common.h` 的 `ALIGN_SIZE` 语义（练习 2 已提示）。此外 [CopyIn 的 Cast](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L95)（DTYPE→fp32）在 T=float 时是同型转换，多数版本可保留但需确认编译器接受。

**验证方式**：在已安装 CANN 与毕昇编译器的容器中执行 `bash build.sh -n ai_infra_aggregate_hidden -c ascend910_93`（用法见 u1-l4），先保证编译通过；再按 u2-l3 的 UT 模板为 DT_FLOAT 补 tiling 用例、按 u8 的 ST 流程补精度用例。本综合实践的编译与精度结果**待本地验证**。

## 6. 本讲小结

- kernel 入口 `extern "C" __global__ __aicore__` 只做三件事：`TILING_KEY_IS` 按 Host 写入的 tilingKey 选 dtype 分支、`GET_TILING_DATA_WITH_STRUCT` 把 GM 上的 TilingData 字节流解包成本地结构体、实例化模板类并执行 `Init()+Process()`。
- `KernelAiInfraAggregateHidden<DTYPE>` 采用 Init（绑 GM、按 `alignBaseH` 划分约 192K 的 UB）/ Process（权重 fp32 预热 + B/S 双循环 CopyIn→Compute→CopyOut）两段式；TPipe 统一管理 UB 与队列同步。
- 算法核心是把 W=3 因果卷积改写为沿 S 的递推：UB 中的 fp32 状态 y0/y1/y2 跨 token 复用，mask 只掩输出不打断递推，S 被切分时用 InitY 回读前两个 token 暖启动。
- 公共基类 CutHBS 用三级取模把 `GetBlockIdx()` 反解为 (H,B,S) 三维 tile 身份并用 Tail 字段处理末块——与 tiling 的 `blockDim = baseHCnt×baseBCnt×baseSCnt` 严格互逆；TilingData 的 13 个字段在 kernel 侧全部被消费。
- Host 与 Device 的契约靠三处约定维持：入口参数顺序 = def 声明顺序、tilingKey 常量双侧同值、TilingData 字段镜像生成——任何一处失配都是静默错误。

## 7. 下一步学习建议

1. **对照读反向算子**：[ai_infra_aggregate_hidden_grad/op_kernel/](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden_grad/op_kernel/ai_infra_aggregate_hidden_grad.cpp) 与前向同款结构（同样使用 TILING_KEY_IS），试着独立走读，检验本讲方法可否复用。
2. **进入 u2-l5（aclnn 两段式接口）**：补齐四层模型的最后一层，看 op_api 如何在 Host 侧把张量、属性打包成入口的六个参数。
3. **预习 u3 单元**：kernel 里反复出现的 `PipeBarrier`、`SetWaitFlag`、队列同步属于 Ascend C 并发原语，u3-l1/u3-l2 将系统走读 utils 错误日志与 common 公共组件。
4. **查阅 CANN 官方文档**：`Ascend C API 参考`中 `DataCopyPad`/`DataCopyExtParams` 的字段单位、`Cast` 的 RoundMode 语义，本讲标注"待确认"的两处均可在其中查证。
