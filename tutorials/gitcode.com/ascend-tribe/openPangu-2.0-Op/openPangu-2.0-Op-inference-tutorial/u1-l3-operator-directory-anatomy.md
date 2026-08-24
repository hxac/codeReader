# 解剖一个算子目录：以 ScatterBlockUpdate 为例

## 1. 本讲目标

上一讲（`u1-l2-build-and-environment.md`）我们打通了编译安装链路，知道了三个动态库（`cust_opapi.so`、`cust_opsproto_rt2.0.so`、`cust_opmaster_rt2.0.so`）是构建的终点。本讲反过来问：**这些库里的内容，在源码里长什么样？**

学完本讲，你应该能够：

1. 背出一个算子目录的固定五件套子目录（`docs` / `op_api` / `op_host` / `op_kernel` / `tests`）及各自的职责。
2. 拿到任何一个算子目录，能在一分钟内区分出 8 类典型文件：`aclnnXxx` 两段式接口文件（cpp/h 各一对）、`l0op` 封装文件（cpp/h 各一对）、`*_def.cpp` 原型注册文件、`*_tiling.h/.cpp` 切分文件、kernel 入口 cpp、kernel 类头文件。
3. 会使用 `docs/` 下的两类接口文档：`npu_*.md`（PyTorch 侧）与 aclnn 侧文档（C++ 侧），并知道文档与代码不一致时该信谁。
4. 说清楚一次算子调用在源码层面的行进方向：`op_api` → `op_host` → `op_kernel`，以及 `tests` 和 `CMakeLists.txt` 如何把这一切组织起来。

本讲选择的标本是 `ai_infra_scatter_block_update`——它是全仓库体量最小的算子之一（op_host + op_kernel 合计约 650 行），但五件套结构完整，是理想的「麻雀」。

## 2. 前置知识

本讲会把第 1 单元前两讲提到的名词落地，这里用通俗语言再解释一遍。

### 2.1 Host 侧与 Device（Kernel）侧

昇腾 NPU 编程和 GPU 类似，代码分两半：

- **Host 侧**：跑在服务器 CPU 上的代码。它能拿到完整的张量描述（形状、数据类型、stride）、能查询硬件规格（几个核、每核多大 UB 内存），负责「排计划」，但不做真正的数据计算。`op_api` 和 `op_host` 两个目录的代码都在 Host 侧。
- **Device 侧（Kernel 侧）**：最终被编译成 `.o`、在 NPU 计算核（AICore）上执行的代码。`op_kernel` 目录属于这一侧。

### 2.2 两级内存：GM 与 UB

- **GM（Global Memory）**：设备上的大容量 DDR 内存，输入输出张量都放在这里，容量大但访问慢。
- **UB（Unified Buffer）**：每个计算核内部的高速缓存，容量小（本算子默认按 192KB 处理）但访问快。kernel 的典型工作方式是「从 GM 搬一块到 UB → 计算 → 从 UB 搬回 GM」。

### 2.3 Tiling（切分）是什么

NPU 上一个算子的数据通常远大于一个核的 UB 容量，也远多于一个核能处理的量，所以要把数据**切成小块**，分给多个核、分多轮搬运。这个「怎么切」的计划就叫 **Tiling**。

Tiling 的计算发生在 Host 侧（`op_host` 目录），计算结果打包成一个叫 **TilingData** 的结构体（本质上是一串数字：总行数、每核处理多少行、每次搬多少行……），随任务下发到 Device 侧，kernel 照着这份「施工图」执行。**Host 侧只算计划，Device 侧只执行计划**——这是理解三层结构的钥匙。

### 2.4 aclnn 接口与两段式调用

`aclnn` 是 CANN 对外暴露的 C 语言算子接口命名前缀（Ascend Computing Language Neural Network）。它采用**两段式**设计：

- 第一段 `aclnnXxxGetWorkspaceSize`：做参数检查、算切分，告诉调用方「需要多大的 workspace（临时工作内存）」，并返回一个执行器 `aclOpExecutor`。
- 第二段 `aclnnXxx`：拿着申请好的 workspace 和执行器，把任务真正异步下发到 stream 上。

为什么要分两段？因为 Host 侧不知道 Device 侧需要多少临时内存，必须先问（第一段）再申请再执行（第二段）。

### 2.5 原地更新（in-place）

大部分算子「输入 → 输出」是两个张量；而 ScatterBlockUpdate 是**原地更新**：直接把 `update` 写进 `input` 自己的内存里，没有独立输出。这个特性会贯穿本讲：它的 OpDef 把输出也命名为 `input`，它的第二段接口没有输出参数，它的 kernel 入口参数里 `input` 既是输入也是输出。

## 3. 本讲源码地图

标本目录：`ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/`，全部 20 个文件如下（`find` 实测）：

```text
ai_infra_scatter_block_update/
├── CMakeLists.txt                     # 算子级构建脚本：把各层源码挂到全局目标
├── docs/                              # ① 接口文档（面向使用者，不参与编译）
│   ├── AiInfraScatterBlockUpdate.md           # aclnn C++ 接口文档
│   └── npu_ai_infra_scatter_block_update.md   # torch (npu) 接口文档
├── op_api/                            # ② 接口层：Host 侧对外的 aclnn 两段式接口
│   ├── aclnn_ai_infra_scatter_block_update.cpp  # aclnn 接口实现（检查+组装）
│   ├── aclnn_ai_infra_scatter_block_update.h    # aclnn 接口声明（会随 run 包安装）
│   ├── ai_infra_scatter_block_update.cpp        # L0 算子封装（挂启动列表）
│   └── ai_infra_scatter_block_update.h          # L0 算子声明（l0op 命名空间）
├── op_host/                           # ③ 原型与切分层：算子「身份证」+ Tiling
│   ├── ai_infra_scatter_block_update_def.cpp        # OpDef 原型注册
│   ├── ai_infra_scatter_block_update_tiling.h       # TilingData 字段 + Tiling 类声明
│   └── ai_infra_scatter_block_update_tiling.cpp     # Tiling 计算实现与注册
├── op_kernel/                         # ④ 计算层：Device 侧 AscendC kernel
│   ├── ai_infra_scatter_block_update.cpp   # kernel 入口函数
│   └── ai_infra_scatter_block_update.h     # Kernel 类（Init/Process/CopyIn/ScatterOut）
└── tests/                             # ⑤ 测试
    ├── CMakeLists.txt                         # 递归进入 ut/
    ├── st/test_ai_infra_scatter_block_update.py           # 系统测试（需真机）
    └── ut/
        ├── CMakeLists.txt
        ├── op_host/test_ai_infra_scatter_block_update_tiling.cpp  # Tiling 单测（无硬件）
        └── op_api/test_aclnn_ai_infra_scatter_block_update.cpp    # aclnn 接口单测
```

| 文件 | 所属层 | 编译去向（承接 u1-l2） |
|:---|:---|:---|
| `op_api/aclnn_*.cpp`、`op_api/ai_infra_*.cpp` | op_api | `cust_opapi.so`（aclnn 接口库） |
| `op_host/*_def.cpp` | op_host | `cust_opsproto_rt2.0.so`（算子原型库） |
| `op_host/*_tiling.cpp` | op_host | `cust_opmaster_rt2.0.so`（tiling 实现库） |
| `op_kernel/*` | op_kernel | kernel 二进制（随 run 包按 SOC 形态分发） |
| `docs/*`、`tests/*` | — | 文档不编译；测试仅在 `ENABLE_TEST` / `-u` 时参与 |

一句话记住行进方向：**使用者 → `op_api`（收参数、做检查）→ `op_host`（验原型、算切分）→ `op_kernel`（照计划搬数计算）**。

## 4. 核心概念与源码讲解

### 4.1 docs：算子的「说明书」——它在算什么

#### 4.1.1 概念说明

`docs/` 是全仓库唯一「不参与编译」的子目录，却是阅读任何算子的正确起点。本仓库的算子文档有两类，对应两类使用者：

| 文档 | 命名习惯 | 面向 | 内容侧重 |
|:---|:---|:---|:---|
| `npu_*.md` | 小写下划线，`npu_` 前缀 | PyTorch / torch_npu 用户 | `torch.ops.custom.*` 原型、Python 调用示例 |
| aclnn 文档 | 本算子叫 `AiInfraScatterBlockUpdate.md`（大驼峰） | C++ / AscendCL 开发者 | `aclnnXxx` C 原型、错误码、C++ 调用示例 |

ScatterBlockUpdate 的功能一句话：**按索引把更新值整行写进输入张量**。数学表达：

\[ \text{input}[\text{indices}[k,0],\ \text{indices}[k,1], :] = \text{update}[k, :], \quad k = 0, 1, \dots, T-1 \]

三个输入的形状是严格咬合的：

- `input`: \((b_n, b_s, D)\) —— 一本「b_n 页、每页 b_s 行、每行 D 个数」的账本；
- `indices`: \((T, 2)\) —— T 条「页码 + 行号」定位；
- `update`: \((T, D)\) —— T 行新数据，每行 D 个数，恰好填满被定位的一整行。

举个手工例子（示例代码，非项目代码）：`input` 形状 \((4, 3, 2)\)，`indices = [[2,1],[0,0]]`，`update = [[9,9],[7,7]]`，执行后 `input[2,1,:]` 变为 `[9,9]`、`input[0,0,:]` 变为 `[7,7]`，其余元素不变。

#### 4.1.2 核心流程

读一份算子文档的推荐顺序：

1. **产品支持表**：先确认你的硬件在不在支持列表（本算子支持 A2/A3 训练推理系列）。
2. **函数原型**：确认参数个数、类型、返回值。
3. **参数说明表**：每个参数的 shape 约定、dtype 列表、是否支持非连续。
4. **约束说明**：shape 取值范围（规格约束）与典型值——这是 tiling/测试用例设计的直接依据。
5. **调用示例**：抄一个能跑的最小例子。

#### 4.1.3 源码精读

torch 侧文档给出 Python 入口——注意函数名末尾的下划线 `_`，这是 PyTorch 惯例，表示**原地修改**（in-place）：

[npu_ai_infra_scatter_block_update.md:29](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/docs/npu_ai_infra_scatter_block_update.md#L29)

> 这一行声明了 torch 侧原型：三个 Tensor 入参、无返回值（原地更新 `input`）。

参数表逐项列出三个输入的 dtype/格式/shape/非连续支持：

[npu_ai_infra_scatter_block_update.md:L32-L36](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/docs/npu_ai_infra_scatter_block_update.md#L32-L36)

> `input` 支持 FLOAT16/BF16/FLOAT/INT64/BOOL/INT8，`indices` 支持 INT32/INT64 且不许重复，`update` 与 `input` 同 dtype。

规格约束表（后面读 tiling 代码时会再次遇到这些数字）：

[npu_ai_infra_scatter_block_update.md:L55-L62](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/docs/npu_ai_infra_scatter_block_update.md#L55-L62)

> 四个维度各有取值区间，例如 `T` 支持 1~262144。这些边界值正是第 6 单元测试用例的素材来源。

aclnn 侧文档则给出两段式 C 原型：

[AiInfraScatterBlockUpdate.md:L32-L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/docs/AiInfraScatterBlockUpdate.md#L32-L37)

> 第一段返回 `workspaceSize` 与 `executor`；第二段带 `workspace` 和 `stream` 执行。这是所有 aclnn 算子的统一模板。

两份文档的参数表存在**不一致**（例如 aclnn 文档的参数表把 `update` 写成仅 FLOAT32，见 [AiInfraScatterBlockUpdate.md:L44-L46](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/docs/AiInfraScatterBlockUpdate.md#L44-L46)，而代码实际支持四种 dtype；C++ 示例里的 `indices` 数据 `{5,0,1,5}` 与 `inputShape={2,3,7}` 甚至自相矛盾）。**结论：文档是地图，代码是地形；两者冲突时以代码为准。** 这也是本手册坚持「结合源码学习」的原因。

#### 4.1.4 代码实践

**实践目标**：不动任何代码，仅凭两份文档还原出算子的完整「用户契约」。

**操作步骤**：

1. 打开上面两份文档，各通读一遍。
2. 在笔记本上回答四个问题：
   - 这个算子支持哪几款硬件？不支持哪几款？
   - Python 怎么调用（写出完整函数名）？为什么有下划线后缀？
   - `indices` 有哪三条附加约束（提示：空、重复、取值范围）？
   - 四个 shape 字母 \(b_n, b_s, D, T\) 各自的范围是多少？
3. 对比两份文档的 `update` dtype 描述，找出不一致处，然后打开下一节的代码验证谁对（答案在 4.2.3 的 dtype 支持列表里）。

**需要观察的现象**：文档之间、文档与代码之间存在事实上的出入（dtype 描述、示例数据）。

**预期结果**：能列出全部 4 个问题的答案，并指出至少 1 处文档不一致；dtype 之争以代码为准（FLOAT/FLOAT16/BF16/INT8）。

#### 4.1.5 小练习与答案

**练习 1**：为什么这个算子的 torch 原型没有返回值，而 aclnn 原型的第一段接口有 `workspaceSize` 和 `executor` 两个「输出」？

**答案**：torch 侧是原地更新语义，结果直接写在 `input` 张量的内存里，无需返回；aclnn 第一段的「输出」不是计算结果，而是执行计划信息——需要多大的临时内存、以及封装好计算流程的执行器，供第二段使用。

**练习 2**：如果调用方传入的 `indices` 里出现两个相同的 `[3, 5]`，会发生什么？

**答案**：文档约束「indices 不能存在重复元素」，但这是**语义约束**而非代码强校验——4.2.3 节会看到 op_api 层只查空指针/空张量/dtype，不查重复。两个核同时写同一行属于数据竞争，结果不确定；正确性责任在使用者。这也解释了 ST 测试为什么用 `torch.randperm`（天然无重复）构造索引（见 4.5.3）。

**练习 3**：`docs/` 下两个文件分别服务于哪两类调用路径（提示：回顾 u1-l1 的双包结构）？

**答案**：`npu_*.md` 服务于 wheel 包路径——`torch.ops.custom.npu_ai_infra_scatter_block_update_`（csrc 适配层最终也调 aclnn）；aclnn 文档服务于 run 包路径——C++ 开发者通过 AscendCL 直接调 `aclnnAiInfraScatterBlockUpdate` 两段式接口。

### 4.2 op_api：接口层——收参数、做检查、组装执行

#### 4.2.1 概念说明

`op_api` 是算子对外的**门面**。它包含两对四个文件，分工不同：

| 文件对 | 命名 | 职责 |
|:---|:---|:---|
| 第一对 | `aclnn_ai_infra_scatter_block_update.cpp/.h` | aclnn 两段式接口：参数检查、非连续处理、创建执行器 |
| 第二对 | `ai_infra_scatter_block_update.cpp/.h`（无 aclnn 前缀） | **L0 算子封装**：把算子挂到 AICore 启动列表，供 aclnn 层（以及未来的其他 aclnn 算子）复用 |

什么是 L0？可以把 aclnn 层理解为「营业厅」（面对用户、验票、开单），L0 层是「后厨入口」（真正把这道菜登记到出菜单上）。`l0op` 命名空间里的函数完成「登记」，真正的计算要等 op_host 的 tiling 与 op_kernel 的实现就位后才执行。

#### 4.2.2 核心流程

`aclnnXxxGetWorkspaceSize`（第一段）的执行流程：

```text
入口 aclnnXxxGetWorkspaceSize
  ├─ L2_DFX_PHASE_1            # 打点（性能诊断埋点）
  ├─ CREATE_EXECUTOR           # 创建 aclOpExecutor（执行器）
  ├─ CommonProcess
  │    ├─ ① CheckXxxParams
  │    │     ├─ NotNull       检查空指针
  │    │     ├─ EmptyTensor   检查空张量
  │    │     └─ DtypeValid    检查 dtype 支持列表
  │    ├─ ② 非连续处理：input 用 CreateView 保留 stride；
  │    │      indices/update 用 l0op::Contiguous 拷贝成连续
  │    └─ ③ l0op::AiInfraScatterBlockUpdate(...)   # 调 L0 算子
  └─ 返回 workspaceSize + executor

入口 aclnnXxx（第二段）
  └─ CommonOpExecutorRun(workspace, workspaceSize, executor, stream)  # 异步下发
```

「参数三步检查」是全仓库 aclnn 文件的统一套路，记住顺序：**空指针 → 空张量 → dtype**。

#### 4.2.3 源码精读

dtype 支持列表定义在匿名命名空间里，注释直接写明「根据API定义，需要列出所能支持的所有dtype」：

[aclnn_ai_infra_scatter_block_update.cpp:33-L39](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L33-L39)

> `input`/`update` 支持 FLOAT/FLOAT16/BF16/INT8，`indices` 支持 INT64/INT32。4.1 练习 3 的 dtype 之争在此一锤定音：代码支持四种，aclnn 文档参数表只写 FLOAT32 是文档滞后。

三步检查的第三步（dtype 一致性）：

[aclnn_ai_infra_scatter_block_update.cpp:76-L103](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L76-L103)

> `OP_CHECK_DTYPE_NOT_SUPPORT` 宏逐个对照支持列表，不命中即用 `OP_LOGE` 打错误日志并返回 false；最后还要求 `update` 与 `input` dtype 严格一致。`CheckAiInfraScatterBlockUpdateParams` 把三个检查按 1→2→3 串起来，分别映射错误码 `ACLNN_ERR_PARAM_NULLPTR`(161001) 与 `ACLNN_ERR_PARAM_INVALID`(161002)。

最有教学价值的是非连续输入的处理——`input` 和另外两个输入走了不同分支：

[aclnn_ai_infra_scatter_block_update.cpp:111-L134](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L111-L134)

> 因为是**原地更新**，`input` 不能被 `Contiguous`（那会拷贝到新内存，写完就丢）；所以连续时直接过，非连续时用 `executor->CreateView` 造一个保留 stride/storageShape/offset 的视图，让 kernel 自己按 stride 算地址。而 `indices`/`update` 只是只读输入，`l0op::Contiguous` 拷成连续最省事。最后调 `l0op::AiInfraScatterBlockUpdate` 登记计算。

两段式的骨架非常短：

[aclnn_ai_infra_scatter_block_update.cpp:139-L161](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L139-L161)

> 第一段：`CREATE_EXECUTOR` → `CommonProcess` → 取 `GetWorkspaceSize()` → `ReleaseTo(executor)` 把执行器交还给调用方。第二段只有一句 `CommonOpExecutorRun`，把任务异步投递到 stream。

再看 L0 封装这对文件——整个文件的核心只有 15 行：

[ai_infra_scatter_block_update.cpp:22-L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/ai_infra_scatter_block_update.cpp#L22-L37)

> `OP_TYPE_REGISTER(AiInfraScatterBlockUpdate)` 注册算子类型；`ADD_TO_LAUNCHER_LIST_AICORE(..., OP_INPUT(input, indices, update), OP_OUTPUT(input))` 把「输入三件、输出一件（还是 input 自己）」登记进执行器的 AICore 启动列表。返回值就是 `input` 本身。

对应的头文件只暴露一个 `l0op` 命名空间函数，这是其他代码复用本算子的唯一入口：

[ai_infra_scatter_block_update.h:17-L21](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/ai_infra_scatter_block_update.h#L17-L21)

> 注意 `aclnn_*.h` 会被安装进 run 包给用户 include（见 4.5.3 的 install 规则），而 L0 头文件只在仓库内部使用。

#### 4.2.4 代码实践

**实践目标**：追踪「一次非法输入的旅程」，把参数检查的代码路径走通。

**操作步骤**：

1. 假想调用方执行 `aclnnAiInfraScatterBlockUpdateGetWorkspaceSize(input, indices, update, &ws, &exec)`，其中 `indices` 被误建成 INT16。
2. 在源码上用手指 tracing：`GetWorkspaceSize` → `CommonProcess`（[L148](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L148)）→ `CheckAiInfraScatterBlockUpdateParams` → `CheckAiInfraScatterBlockUpdateDtypeValid` → `OP_CHECK_DTYPE_NOT_SUPPORT(indices, ...)`。
3. 记下每一层返回什么：内层 `return false` → `CHECK_RET` 展开 → `CheckAiInfraScatterBlockUpdateParams` 返回 `ACLNN_ERR_PARAM_INVALID` → 外层 `CHECK_RET(ret == ACLNN_SUCCESS, ret)` → 第一段接口整体返回 161002。
4. （可选，需真机）在 ST 脚本里故意把 `indices_dtype` 改成 `torch.int16` 跑一次，观察报错日志。**待本地验证**。

**需要观察的现象**：错误不会在 kernel 里才爆，而是在第一段接口的 Host 检查处就被拦截，且日志带有 `OP_LOGE` 打出的具体字段名。

**预期结果**：整条返回链路能口述出来，最终错误码为 `ACLNN_ERR_PARAM_INVALID`（161002）；若真机验证，日志中应出现 indices dtype 相关的 error 信息。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `input` 非连续时用 `CreateView` 而不是像 `indices`/`update` 一样用 `Contiguous`？

**答案**：本算子原地更新 `input`。`Contiguous` 会把数据拷贝到一块新的连续内存，kernel 若写这块新内存，调用方手里的原张量不会被修改，原地语义就破了。`CreateView` 只是新建一个描述（保留 stride/offset），底层还是原来那块内存，kernel 按 stride 偏移定位写入，修改直接落在原张量上。`indices`/`update` 是只读输入，拷贝无副作用，统一连续化能让 kernel 搬运逻辑更简单。

**练习 2**：`aclnn_*.h` 和无前缀的 `ai_infra_scatter_block_update.h` 都在 `op_api/` 下，它们的消费者有何不同？

**答案**：前者声明 C 接口，随 run 包安装到 `ACLNN_INC_INSTALL_DIR`，供外部 C++ 用户 `#include "aclnnop/aclnn_ai_infra_scatter_block_update.h"` 使用（docs 的 C++ 示例正是这么包含的）；后者是仓库内部的 L0 封装声明，供本仓库其他 host 代码在 `l0op` 命名空间下复用，不对外安装。

**练习 3**：第二段接口 `aclnnAiInfraScatterBlockUpdate` 里没有任何输入张量参数，它怎么知道要算什么？

**答案**：所有计算要素（算子类型、输入输出描述、tiling 结果）在第一段就被打包进 `aclOpExecutor`；第二段只负责拿 `workspace + executor + stream` 把任务异步下发，所以参数里看不到业务张量。

### 4.3 op_host：原型注册与 Tiling——算子的「身份证」和「施工图」

#### 4.3.1 概念说明

`op_host` 三个文件回答两个问题：

1. **「你是谁」**——`*_def.cpp` 里的 **OpDef**。它向 CANN 图引擎注册算子的原型：有几个输入输出、每个支持哪些 dtype/format 组合、跑在哪类芯片上。没有 OpDef，图引擎不认识这个节点，aclnn 层的登记也无法落到具体实现。
2. **「怎么切」**——`*_tiling.h/.cpp` 里的 **TilingData** 与 **Tiling 类**。Host 侧根据输入 shape 和硬件规格，计算多核分配与搬运粒度，把结果填进 TilingData 下发。

`*_tiling.h` 与 `*_tiling.cpp` 的分工是「声明 / 实现」：`.h` 定义 TilingData 有哪些字段（kernel 要读的「施工图」栏目）、Tiling 类覆写了哪些步骤；`.cpp` 实现每个步骤的具体计算。

#### 4.3.2 核心流程

Tiling 类继承公共基类 `TilingBaseClass`（位于 `common/include/tiling_base/`，第 5 单元会深入），按七个虚函数步骤执行（`*_tiling.h` 中的注释编号即执行顺序）：

```text
DoTiling(context)
  ├─ 1 GetPlatformInfo   # 查硬件：AIV 核数 aivNum、UB 大小 ubSize
  ├─ 2 GetShapeAttrsInfo # 取输入输出/属性信息（本算子为空实现）
  ├─ (IsCapable)         # 本 tiling 类是否胜任当前输入
  ├─ 3 DoOpTiling        # 核心切分计算（见下方）
  ├─ 4 DoLibApiTiling    # 高阶 API 的 tiling（本算子为空实现）
  ├─ 5 GetTilingKey      # 产出 TilingKey（本算子恒为 1000）
  ├─ 6 GetWorkspaceSize  # workspace 大小（本算子为空实现）
  └─ 7 PostTiling        # 填充并保存 TilingData、设置 BlockDim
```

`DoOpTiling` 内部的切分算法（按 indices 行数均分到各 AIV 核）：

\[ \text{eachCore} = \lceil T / N_{core} \rceil, \quad \text{usedCore} = \lceil T / \text{eachCore} \rceil, \quad \text{tail} = T - \text{eachCore} \times (\text{usedCore} - 1) \]

UB 容量校验决定单次搬运行数（double buffer，UB 对半分）：

\[ \text{indicesPerLoad} = \left\lfloor \frac{(ubSize - 4096)\,/\,2}{2 \times \text{idxBytes} + \text{AlignUp}(D \times updBytes,\ 32)} \right\rfloor \]

#### 4.3.3 源码精读

先看「身份证」。OpDef 用链式写法声明输入 `input`：

[ai_infra_scatter_block_update_def.cpp:22-L29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L22-L29)

> `ParamType(REQUIRED)` 表示必选；`DataType({...})` 与 `Format({...})`、`UnknownShapeFormat({...})` 三个列表**按位置一一对应**——第 i 种 dtype 搭配第 i 种格式（这里 8 个条目实际是 4 种 dtype 的 ND 组合重复了两遍）。`indices`/`update` 同理声明。

输出也叫 `input`——原地更新的身份证明：

[ai_infra_scatter_block_update_def.cpp:47-L54](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L47-L54)

> `Output("input")` 与 `Input("input")` 同名，正是 op_api 层 `OP_OUTPUT(input)` 的注册依据。

芯片适配与能力开关：

[ai_infra_scatter_block_update_def.cpp:56-L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L56-L63)

> `OpAICoreConfig` 打开动态编译/动态格式/动态 rank/动态 shape 四个开关（推理场景 shape 多变，必须开）；`AddConfig("ascend910b")` 与 `AddConfig("ascend910_93")` 声明支持的两类 SOC——这正对应 u1-l2 讲过的 `build.sh -c` 可选值。

顺带一个「带怀疑精神读源码」的例子：该文件头部的文件名注释写的是 `lower_triangular_inverse_def.cpp.cpp`（[第 12 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L12)）——复制粘贴的残留，与实际文件名不符。注释会撒谎，代码不会。

再看「施工图」。TilingData 的字段清单用宏定义，每个字段 kernel 侧都能按名读取：

[ai_infra_scatter_block_update_tiling.h:25-L43](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L25-L43)

> `BEGIN_TILING_DATA_DEF`/`TILING_DATA_FIELD_DEF(类型, 名字)`/`END_TILING_DATA_DEF` 定义 14 个字段；`REGISTER_TILING_DATA_CLASS(算子名, TilingData类)` 把它与算子绑定，编译期会生成 host↔kernel 两侧一致的布局代码。

Tiling 类声明：继承 `TilingBaseClass`，按注释编号覆写七步：

[ai_infra_scatter_block_update_tiling.h:50-L84](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L50-L84)

> 本算子只认真实现 `GetPlatformInfo`/`DoOpTiling`/`GetTilingKey`/`PostTiling` 四步，`GetShapeAttrsInfo` 等返回 `GRAPH_SUCCESS` 的空实现。私有成员区（[L94-L127](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L94-L127)）缓存了 shape/stride/dtype 的中间变量，注释标明了每个输入的形状约定。

实现侧，第 1 步查平台信息（带缺省兜底）：

[ai_infra_scatter_block_update_tiling.cpp:81-L101](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L81-L101)

> 优先从 `context_->GetPlatformInfo()` 实时查询 AIV 核数与 UB 大小；查不到时退回编译期 `TilingPrepare` 存入 CompileInfo 的快照（默认 40 核 / 192KB）。

第 3 步是切分核心，分核、UB 预算、搬运上限三段式：

[ai_infra_scatter_block_update_tiling.cpp:307-L341](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L307-L341)

> 步骤 2 按行均分（`CeilDiv` 两次得到 `eachCoreIndexCount_`/`usedCoreNum_`，尾核单独算 `tailCoreIndexCount_`）；步骤 3 扣除 4KB 栈预留后把 UB 对半（double buffer），算出每半能装多少行；步骤 4 用「每核处理量、UB 装载量、单次搬运硬上限 4064」三者取最小作为 `maxIndicesPerLoad_`。每段都有 `OP_CHECK_IF` 保护，越界即报错返回。

第 7 步把计算结果落盘为 TilingData 并设置核数：

[ai_infra_scatter_block_update_tiling.cpp:376-L416](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L376-L416)

> `set_xxx` 逐字段填充（与 4.3.3 开头的字段清单一一对应）→ `SaveToBuffer` 序列化进 context → `SetBlockDim(usedCoreNum_)` 告诉 runtime 启动多少个核。末尾校验 TilingData 必须 8 字节对齐，并预留 workspace（本算子为 0）。

最后是注册三件套——tiling 模板、TilingFunc、编译期信息解析：

[ai_infra_scatter_block_update_tiling.cpp:452-L454](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L452-L454)

> `IMPL_OP_OPTILING(AiInfraScatterBlockUpdate)` 把 `TilingFunc4ScatterBlockUpdate`（内部转交给模板注册表 `TilingRegistry::DoTilingImpl`，见 [L418-L425](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L418-L425)）注册为该算子的 tiling 入口，并用 `TilingParse<AiInfraScatterBlockUpdateCompileInfo>` 在编译期缓存平台快照。而 [L78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L78) 的 `REGISTER_TILING_TEMPLATE(..., 1000)` 把 tiling 类以 key=1000 挂进模板注册表。

#### 4.3.4 代码实践

**实践目标**：不开电脑编译，纯手算复现一次 DoOpTiling，验证你能读懂切分算法。

**操作步骤**：

设 `T = 100`（indices 行数），`aivNum = 40`，`ubSize = 192 × 1024`，`D = 128`，数据类型 BF16（`updateTypeSize = 2`），`indicesTypeSize = 4`（INT32）。

1. 按 [L303-L305](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L303-L305) 计算：`oneIndexSize = 2 × 4 = 8` 字节；`oneUpdateAlignSize = AlignUp(128 × 2, 32) = 256` 字节。
2. 按 [L309-L313](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L309-L313) 计算：`eachCoreIndexCount = ⌈100/40⌉ = 3`；`usedCoreNum = ⌈100/3⌉ = 34`；`tailCoreIndexCount = 100 − 3 × 33 = 1`。
3. 按 [L317-L332](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L317-L332) 计算：`availableUb = 196608 − 4096 = 188416`；`halfUb = 94208`；`perLoadSize = 8 + 256 = 264`；`indicesPerLoad = ⌊94208/264⌋ = 356`。
4. 按 [L340-L341](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L340-L341) 计算：`maxIndicesPerLoad = min(3, 356, 4064) = 3`。
5. （可选，需环境）对照 UT 用例 `test_ai_infra_scatter_block_update_tiling.cpp` 的写法，把本组参数做成新用例跑 `bash build.sh -u`。**待本地验证**。

**需要观察的现象**：均分不是「40 核各 2.5 行」而是「33 个核各 3 行 + 尾核 1 行」；`usedCoreNum` 常常小于物理核数。

**预期结果**：手算结果为 `eachCoreIndexCount=3, usedCoreNum=34, tailCoreIndexCount=1, maxIndicesPerLoad=3`；步骤 5 若真机跑通，UT 断言的 TilingKey 应为 1000。

#### 4.3.5 小练习与答案

**练习 1**：TilingData 里为什么要存 `inputStride0_`/`inputStride1_` 这两个字段？删掉它们行不行？

**答案**：不行。op_api 层为了保住原地更新语义，对非连续 `input` 用 `CreateView` 保留 stride（见 4.2.3），所以 kernel 收到的 `input` 第 0 维 stride 可能不等于 \(b_s \times D\)。kernel 写入时必须按 \(\text{offset} = idx_0 \times \text{stride0} + idx_1 \times \text{stride1}\) 定位（见 4.4.3 的 ScatterOut），这两个 stride 只能由 Host 侧查出来经 TilingData 带下去。

**练习 2**：`GetPlatformInfo` 里为什么默认值是 `aivNum = 40`、`ubSize = 192KB`，而 `*_tiling.h` 成员初始化却是 `aivNum_ = 20`？

**答案**：这是防御性兜底的两层：局部默认值（40/192K，[L83-L84](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L83-L84)）用于平台信息与编译缓存都拿不到的极端情况；成员初始值 20（[tiling.h L96](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L96)）只在没有走 `GetPlatformInfo` 时短暂生效。正常路径都会被 `PlatformAscendC` 的真实查询覆盖，数值本身不影响正确性，只影响极端场景下的保守程度。

**练习 3**：`REGISTER_TILING_TEMPLATE("AiInfraScatterBlockUpdate", ..., 1000)` 里的 1000 是什么？它和 kernel 里的 `FULL_LOAD_TILING_KEY` 是什么关系？

**答案**：1000 是 TilingKey，即 tiling 模板的编号。Host 侧 `GetTilingKey()` 返回它（[L371-L374](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L371-L374)），随任务下发；Device 侧 kernel 入口用 `TILING_KEY_IS(1000)` 分支选择对应实现（见 4.4.3）。当一个算子有多套切分策略（如第 4 单元的注意力算子）时，不同 key 对应不同 kernel 分支——本算子只有一套「满载」策略，所以恒为 1000。

### 4.4 op_kernel：计算层——照施工图在 NPU 上搬数

#### 4.4.1 概念说明

`op_kernel` 是 Device 侧代码，用 **AscendC** 语言编写（昇腾的 C++ 方言，`__aicore__` 等关键字标注设备函数）。两个文件分工：

- `ai_infra_scatter_block_update.cpp`：**kernel 入口**。函数名即算子在二进制里的符号名，参数布局（若干 `GM_ADDR` + workspace + tiling）由框架约定，它负责取出 TilingData 并按 TilingKey 分发。
- `ai_infra_scatter_block_update.h`：**Kernel 类**。模板类 `ScatterBlockUpdateKernel<T, IndexT>`，`Init` 绑定内存与队列、`Process` 做多轮「搬入-搬出」流水。

两个在 2.2 节埋下的概念在此落地：**TPipe** 管理 UB 内存，向 kernel 提供「队列」抽象；**TQue double buffer** 让「搬入（MTE2）」和「搬出（MTE3）」两件工作交替使用两块缓冲，天然重叠。

#### 4.4.2 核心流程

单个核上一轮工作的数据流：

```text
            (tiling 指挥)
GM(indices) ──DataCopyPad──► UB(indicesQue_)  ┐
GM(update)  ──DataCopyPad──► UB(updateQue_)   ├─ CopyIn：AllocTensor→拷贝→EnQue
                                               │
UB(indLocal/updLocal) ──读索引、算stride偏移──  │
UB(updLocal[i]) ──DataCopyPad──► GM(input + offset)  ← ScatterOut：DeQue→写回→FreeTensor
                       （两队列交替，搬入搬出重叠）
```

每个核的职责划分（`Process` 内）：

- 核 `blockIdx` 负责 `[blockIdx × eachCoreIndexCount, ...)` 这一段行；
- 最后一个核（`blockIdx == usedCoreNum - 1`）只处理 `tailCoreIndexCount` 行；
- 超出 `usedCoreNum` 的核直接返回（tiling 只启动了 `usedCoreNum` 个核，这是双保险）。

#### 4.4.3 源码精读

kernel 入口只有 10 行，但每一行都是信息：

[ai_infra_scatter_block_update.cpp:25-L35](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L25-L35)

> `extern "C" __global__ __aicore__` 是 AscendC kernel 入口的固定签名；六个 `GM_ADDR` 参数按框架约定排列——三个输入、一个输出（原地更新所以 `input_out` 与 `input` 指向同一块内存）、workspace、tiling。`GET_TILING_DATA(tilingData, tiling)` 把 tiling 指针反序列化成结构体（字段名与 4.3.3 的 TilingData 定义完全一致）；`TILING_KEY_IS(1000)` 对应 4.3 练习 3 的 key 分支；`DTYPE_INPUT`/`DTYPE_INDICES` 是**编译期宏**，由构建系统按 OpDef 声明的 dtype 组合实例化出多个 kernel 版本（文件头注释 [L14-L15](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L14-L15) 有说明）。

Kernel 类的成员一览——两类成员正好对应「对外的三块 GM 内存」与「对内的两条 UB 队列」：

[ai_infra_scatter_block_update.h:29-L58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L29-L58)

> `GlobalTensor<T>` 是 GM 上的视图；`TQue<TPosition::VECIN, 2>` 是 double buffer 的输入队列（本算子只搬入不搬出队列，写回直接对 GM 用 `DataCopyPad`）；余下成员全部来自 TilingData（`eachCoreIndexCount_`、`maxIndicesPerLoad_`、`inputStride0_`……与 4.3.3 字段清单对得上号）。

`Init` 做绑定与按 tiling 分配 UB：

[ai_infra_scatter_block_update.h:64-L94](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L64-L94)

> 三步：`SetGlobalBuffer` 绑 GM；逐字段抄 tiling；`pipe_->InitBuffer` 按 `maxIndicesPerLoad × 行字节数` 给两条队列各分配 double buffer 的 UB 空间——Host 侧算好的「每次最多搬 3 行」，Device 侧照单全收。

`Process` 是多核调度的 device 版实现：

[ai_infra_scatter_block_update.h:97-L124](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L97-L124)

> `GetBlockIdx()` 拿到自己的核号，与 `usedCoreNum_`/`tailCoreIndexCount_`（都来自 TilingData）比对出本核的 `[coreStart, coreStart+coreCount)` 区间；while 循环按 `maxIndicesPerLoad_` 分批。注释（[L113-L116](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L113-L116)）清楚解释了 double buffer 如何让搬入搬出自然重叠。

`ScatterOut` 中最关键的三行——用 stride 折算 GM 目标地址：

[ai_infra_scatter_block_update.h:168-L188](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L168-L188)

> 从 UB 用 `GetValue` 读出索引对 \((idx_0, idx_1)\)（负索引直接跳过）；按 \(\text{gmOffset} = idx_0 \times \text{inputStride0}\_ + idx_1 \times \text{inputStride1}\_\) 计算元素偏移——这正是 4.2 练习 1 里「CreateView 保留 stride」的终点；最后 `DataCopyPad` 把 UB 中对齐后的一行整段写回 `inputGm_[gmOffset]`。`MTE2_S` 事件（[L165-L167](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L165-L167)）保证「搬运完成」先于「标量读索引」发生。

#### 4.4.4 代码实践

**实践目标**：画出一轮 `CopyIn → ScatterOut` 的完整数据流图，打通 GM↔UB 的空间想象。

**操作步骤**：

1. 白纸上画三个长条代表 GM：`input`（\(b_n \times b_s \times D\) 个元素）、`indices`（\(T \times 2\)）、`update`（\(T \times D\)）。
2. 画两个小方块代表 UB 队列 `indicesQue_`、`updateQue_`，各标上 double buffer（2 个 slot）。
3. 对照 [CopyIn（L127-L156）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L127-L156) 画箭头：`indicesGm_[startIdx*2] → indLocal`、`updateGm_[startIdx*D] → updLocal`，标注「按行搬运、行内 padding 到 32B 对齐」。
4. 对照 [ScatterOut（L159-L191）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L159-L191) 画回程箭头：`updLocal[i*updateRowElements_] → inputGm_[idx0*stride0 + idx1*stride1]`。
5. 在图上标出 4.3.4 手算场景（`maxIndicesPerLoad = 3`）时一轮循环搬运的字节数：indices 3×8=24B，update 3×256=768B。

**需要观察的现象**：本算子没有 VECOUT 队列、没有向量计算，是纯粹的「搬入-改地址-搬出」结构；所有搬运粒度都由 TilingData 字段控制。

**预期结果**：图中每条箭头都能对应到一句 `DataCopyPad` 调用；能说清 `updateRowElements_ = oneUpdateAlignSize / sizeof(T)` 为什么存在（UB 中行被 padding，取第 i 行视图要按对齐后行距偏移）。

#### 4.4.5 小练习与答案

**练习 1**：kernel 入口有 6 个参数，但算子只有 3 个输入，多出来的 `input_out`、`workspace`、`tiling` 分别是什么？

**答案**：`input_out` 是框架约定的输出槽位——本算子原地更新，它与 `input` 指向同一块内存（OpDef 的 `Output("input")` 决定）；`workspace` 是第一段接口申请的临时内存（本算子 tiling 里 `workspaces[0] = 0`，用不到但仍占参数位）；`tiling` 指向序列化的 TilingData，由 `GET_TILING_DATA` 展开。

**练习 2**：`Process` 里 `if (blockIdx >= usedCoreNum_) return;` 是不是多余的——既然 tiling 已经 `SetBlockDim(usedCoreNum_)` 只启动了这么多核？

**答案**：逻辑上是双保险。BlockDim 决定实际启动核数，正常情况下不会有核号越界；但防御性检查让 kernel 的正确性不依赖 host 侧配置（例如未来接入别的 tiling 模板或调度变化时），代价只有一条标量比较，值得保留。

**练习 3**：`CopyIn` 里 `update` 每行明明只有 `D × sizeof(T)` 字节，为什么 UB 里要按 `AlignUp(D × sizeof(T), 32)` 对齐存放？

**答案**：昇腾 DMA 搬运按 32 字节块对齐效率最高（`ALIGN_BYTES = 32` 常量即为此）。把 UB 中每行 padding 到 32B 的整数倍后，`ScatterOut` 取第 i 行视图 `updLocal[i * updateRowElements_]` 的偏移也天然 32B 对齐，单行写回 GM 可以用一次整块 `DataCopyPad`，避免非对齐访问的性能惩罚。GM 侧数据本身没变，只是 UB 内部布局做了对齐。

### 4.5 CMakeLists 与 tests：把三层粘起来，并证明它们是对的

#### 4.5.1 概念说明

- **算子级 `CMakeLists.txt`**：五件套的「粘合剂」。它回答三个问题：本算子有哪些编译选项；各层源码分别挂到哪个全局目标（承接 u1-l2 的 `opapi`/`optiling`/`op_host_aclnnInner`）；tests 目录何时参与构建。
- **tests** 分两类：
  - **UT（单元测试，C++）**：`tests/ut/op_host`（tiling 逻辑）与 `tests/ut/op_api`（接口逻辑），借助 **faker** 伪造 `TilingContext`，**不需要 NPU 硬件**即可跑。
  - **ST（系统测试，Python）**：`tests/st`，真机执行 `torch.ops.custom.*`，与 CPU 参考实现（golden）做逐位比对。

#### 4.5.2 核心流程

算子 CMakeLists 的挂接逻辑：

```text
CMakeLists.txt
  ├─ add_ops_compile_options(OP_NAME ...)          # 编译选项（-Werror 等）
  ├─ target_sources(op_host_aclnnInner  ← def.cpp)     # 原型 → cust_opsproto
  ├─ target_sources(optiling            ← tiling.cpp)  # 切分 → cust_opmaster
  ├─ target_sources(opapi               ← op_api/*.cpp)# 接口 → cust_opapi
  ├─ install(FILES aclnn_*.h → ACLNN_INC_INSTALL_DIR)  # 头文件随包安装
  ├─ 递归子目录（tests 仅在 ENABLE_TEST 时保留）
  └─ BUILD_OPS_RTY_KERNEL 分支：另一套 host 形态的挂接
```

UT 与 ST 的互补关系：UT 验证「切分计算对不对」（快、无硬件），ST 验证「整条链路端到端算得对不对」（慢、需真机）。

#### 4.5.3 源码精读

文件头注释直接给出两条路线的选择标准：

[CMakeLists.txt:9-L16](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt#L9-L16)

> `op_host_aclnnInner` 用于自己实现了 aclnn 接口的算子（本仓库全部如此）；`op_host_aclnn` 用于直接用自动生成接口的算子。`add_ops_compile_options` 绑定算子名与 `-Werror` 等选项。

三层源码的挂接点：

[CMakeLists.txt:19-L45](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt#L19-L45)

> `def.cpp → op_host_aclnnInner`、`tiling.cpp → optiling`（`BUILD_OPEN_PROJECT` 时再挂一份到 `opmaster_ct`）、`op_api/` 两个 cpp → `opapi`；`install(... OPTIONAL)` 把 aclnn 头文件装进 run 包的 include 目录。**读到这里你应该能反推出 u1-l2 的三个动态库各装了本算子的哪些文件。**

tests 的按需参与：

[CMakeLists.txt:47-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt#L47-L55)

> 先 `file(GLOB)` 收集子目录，`if(NOT ENABLE_TEST)` 时把 `tests` 从列表里剔除，再对剩余有 CMakeLists 的子目录 `add_subdirectory`——所以平时构建完全不带测试，`build.sh -u` 才会打开开关。

UT 用例的典型写法——用 `TilingContextPara` 描述输入输出，再交给执行器断言：

[test_ai_infra_scatter_block_update_tiling.cpp:59-L89](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp#L59-L89)

> 每个用例 = 一组 shape/dtype 描述 + CompileInfo + 期望值（`expectTilingKey=1000`、期望 workspace `{0}`），`ExecuteTestCase(...)` 驱动真实 tiling 代码执行并比对。用例上方的块注释（[L50-L58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp#L50-L58)）是全仓库 UT 的规范写法：输入/约束/预期三段式。第二个用例的注释（[L91-L102](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp#L91-L102)）坦诚说明 faker 框架暂不支持 stride，非连续路径只能靠 op_api UT / ST 覆盖——测试也有边界，读注释能省去无效尝试。

UT 的构建开关与目录挂接：

[tests/ut/op_host/CMakeLists.txt:10-L17](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/CMakeLists.txt#L10-L17)

> `UT_TEST_ALL OR OP_HOST_UT` 时把本目录源码挂进 `${OP_TILING_MODULE_NAME}` 单测目标——对应 u1-l2 讲过的 `bash build.sh -u --ophost` 触发方式。

ST 的写法——golden 参考实现 + 真机调用 + 逐位比对：

[test_ai_infra_scatter_block_update.py:16-L33](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L16-L33)

> `golden_scatter_block_update` 在 CPU 上按公式逐行赋值（正是 4.1 的数学定义直译）；`make_noncontig_dim0` 用 `torch.as_strided` 构造第 0 维非连续输入——专门覆盖 4.2/4.4 讲的 stride 路径。

真机调用与二进制一致断言：

[test_ai_infra_scatter_block_update.py:63-L83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L63-L83)

> 第 71 行 `torch.ops.custom.npu_ai_infra_scatter_block_update_(...)` 正是 docs `npu_` 文档原型的落地（u1-l4 将打通这条调用链的注册细节）；比对用 `torch.equal` 要求**逐位一致**（原地拷贝类算子无浮点误差，允许这么严），失败时打印 mismatch 统计。资源声明见 [L88-L92](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L88-L92) 的 `@pytest.mark.resources(device="npu:*", npus_per_node=1)`。

#### 4.5.4 代码实践

**实践目标**：在不接触硬件的情况下，通过「读测试」验证你对本算子的理解。

**操作步骤**：

1. 打开 `tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp`，找到 4.3.4 手算的等价用例：用例 `(2048,128,128), T=16384` 断言 `expectTilingKey=1000`、`expectWorkspaces={0}`。
2. 用 4.3.4 的算法手算该用例的 `usedCoreNum`（设 40 核）：`eachCore = ⌈16384/40⌉ = 410`，`usedCore = ⌈16384/410⌉ = 40`，`tail = 16384 − 410×39 = 274`。
3. 打开 `tests/st/test_ai_infra_scatter_block_update.py`，数一数有多少个 `test_*` 方法覆盖了哪些 dtype/非连续组合。
4. （需昇腾环境 + 已安装 run 包与 wheel 包）任选一个 ST 用例执行：`pytest tests/st/test_ai_infra_scatter_block_update.py -k base`。**待本地验证**。

**需要观察的现象**：UT 只断言 tiling 元数据（key/workspace/成功与否），不算数值；ST 才算数值且要求二进制一致；两者覆盖面互补且都有覆盖不到的角落（UT 不支持 stride）。

**预期结果**：步骤 2 手算结果 `usedCoreNum=40, tailCoreIndexCount=274`（若与实际不符请重查手算，算法在 4.3.3 的 L309-L313）；步骤 4 通过则输出全绿。

#### 4.5.5 小练习与答案

**练习 1**：`CMakeLists.txt` 里 `target_sources(optiling PRIVATE op_host/ai_infra_scatter_block_update_tiling.cpp)` 出现了两次（第二次在 `if (NOT BUILD_OPEN_PROJECT)` 里挂给 `opmaster_ct`），为什么同一份源码要挂两个目标？

**答案**：两个全局目标对应两种交付形态：`optiling` 服务于标准 run 包（`cust_opmaster_rt2.0.so`），`opmaster_ct` 服务于 `BUILD_OPEN_PROJECT` 开源工程形态的构建。tiling 逻辑只有一份源码，通过重复挂接让两种形态都包含它——这也是 u1-l2 讲的「一份源码多种打包」在算子目录级别的体现。

**练习 2**：为什么 ST 敢用 `torch.equal`（逐位一致）而不是常见的 `allclose`（带容差）？

**答案**：本算子只做数据搬运不改数值（copy 语义），NPU 搬运不产生浮点舍入误差，理论上必须与 CPU golden 完全一致；一旦不一致说明链路有 bug（漏行、错位、越界）。带浮点计算的算子（如第 4 单元的注意力）则必须用带容差的比对。

**练习 3**：如果只想验证「tiling 计算逻辑」，不装 run 包、没有 NPU，能做到吗？

**答案**：能。UT 的 faker 框架（`tiling_context_faker.h`）在纯 Host 侧伪造了 `TilingContext`，只需在构建时打开 `UT_TEST_ALL`/`OP_HOST_UT` 开关（`bash build.sh -u --ophost`），编译出的单测二进制在 x86 服务器上即可运行——第 6 单元将深入这套框架。

## 5. 综合实践

**任务：以 ScatterBlockUpdate 为模板，给 `ai_infra_mhc_sandwich_norm_post_preonly` 制作一张「文件清单表」。** 这是把本讲知识迁移到任意算子的能力检验。

**操作步骤**：

1. 列出 `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/` 下全部文件（约 24 个），逐个填入下表模板：

| 文件 | 所属子目录/层 | 文件类型（八类之一或「其他」） | 职责一句话 | 与模板算子的差异点 |
|:---|:---|:---|:---|:---|
| `op_host/ai_infra_mhc_sandwich_norm_post_preonly_def.cpp` | op_host | `*_def.cpp` | OpDef 原型注册 | 120 行 vs 模板 70 行，输出更多 |
| `…`（自行补全） | | | | |

2. 重点核对并记录五处可预期的差异（先自己找，再对照下面的提示）：
   - docs 目录的 aclnn 文档文件名前缀是 `aclnn`（`aclnnAiInfraMhcSandwichNormPostPreonly.md`），与模板的大驼峰无前缀命名不同——文档命名不统一是仓库现状；
   - `op_api/` 下多出一个 `CMakeLists.txt`（模板的 op_api 挂接写在算子根 CMakeLists 里）；
   - `op_kernel/` 下有 **7 个**文件（`common/kernel/kernel_compute/kernel_io/singlecore/dualcore/dualcore_mt` 系列 .h + 入口 .cpp），而模板只有 2 个——因为该算子按单核/双核/多 tile 分支组织 kernel，一个文件一类分支；
   - `tests/ut/op_host/` 下除 `.cpp` 外还有一个 `test_*_tiling.h`（用例的公共头）；
   - 该算子**不是原地更新**：docs 中的 torch 原型有独立输出参数，OpDef 的 Output 与 Input 不同名。
3. 从表中挑出「def → tiling.h/.cpp → kernel 入口」各一个文件，各读 30 行，验证类型名、TilingData 字段、kernel 入口参数三者同名同源（如 `AiInfraMhcSandwichNormPostPreonly` / `MhcSandwichNormPostPreonlyTilingData`）。

**预期结果**：一张完整填写的清单表；能对每一个文件回答「它在三层结构（或文档/测试/构建）中站在哪个位置」；能说出至少 3 条结构性差异及原因（kernel 分支多 ⇨ 计算形态多；非原地 ⇨ 有独立输出）。

**待本地验证**部分：若想进一步确认差异，可对两个算子分别执行 `bash build.sh -n '<算子目录名>' -c ascend910_93` 对比产物清单（需昇腾环境）。

## 6. 本讲小结

- 算子目录五件套各司其职：`docs` 讲给人听、`op_api` 收参数做检查、`op_host` 注册原型并算切分、`op_kernel` 在 NPU 上执行、`tests` 双轨验证（UT 无硬件验逻辑、ST 真机验精度）。
- **op_api 两对文件**：`aclnn_*` 两段式接口（第一段检查+组执行器+返回 workspaceSize，第二段异步执行）与无前缀的 L0 封装（`ADD_TO_LAUNCHER_LIST_AICORE` 登记计算）。
- **op_host 三个文件**：`*_def.cpp` 是算子身份证（dtype/format 按位对应、`AddConfig` 声明 SOC）；`*_tiling.h/.cpp` 定义 TilingData 字段并实现七步 Tiling 流程，核心是「按行均分到 AIV 核 + UB 对半的 double buffer 预算」。
- **op_kernel 两个文件**：入口宏取 TilingData、按 TilingKey 分发；Kernel 类 `Init/Process/CopyIn/ScatterOut` 完成按 stride 定位的 GM↔UB 搬运，原地更新靠 Host 侧 `CreateView` 保留 stride + Device 侧 `offset = idx0×stride0 + idx1×stride1` 配合实现。
- **CMakeLists 是粘合剂**：`def→op_host_aclnnInner`、`tiling→optiling`、`op_api→opapi`、aclnn 头文件随包安装、tests 仅在 `ENABLE_TEST` 时参与——源码组织与 u1-l2 的三个动态库一一对应。
- 文档、注释都可能滞后（dtype 不一致、文件名注释残留），**以代码为准**是读仓库的默认姿态。

## 7. 下一步学习建议

本讲建立了「一个算子长什么样」的静态认知，下一讲 `u1-l4-torch-extension-quickstart.md` 将打通动态一环：安装 `omni_custom_ops` wheel 包，在 PyTorch 里亲手调用 `torch.ops.custom.*`，让 ST 测试里的那行调用在你自己的脚本里跑起来。

进入第 2 单元后，本讲的每个目录都会被放大成独立一讲精读：

- `u2-l1`：OpDef 的 dtype/format/SOC 配置逐字段精读；
- `u2-l2`：aclnn 两段式与参数三步检查的完整套路（本讲 4.2 的加深版）；
- `u2-l3`：`TilingBaseClass` 七步框架的公共基类源码（本讲 4.3 的加深版）；
- `u2-l4`：AscendC kernel 的入口宏、TPipe、队列机制（本讲 4.4 的加深版）。

建议带着本讲综合实践产出的那张 mhc 清单表进入第 2 单元——遇到新概念时回到这张表问自己：「这个机制在 mhc 算子里对应哪个文件？」
