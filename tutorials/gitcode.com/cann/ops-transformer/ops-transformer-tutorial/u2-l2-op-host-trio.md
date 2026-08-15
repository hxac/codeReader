# op_host 三件套：def、infershape 与 tiling

## 1. 本讲目标

上一讲（u2-l1）我们浏览了 add_example 的目录全景，知道了「每个文件放哪里、被谁编译」。本讲深入 op_host 目录，逐行精读宿主侧三个关键文件，学完后你应该能够：

1. 看懂 def 文件中算子原型（输入/输出/数据类型/format/SoC 配置）的注册方式。
2. 理解 infershape（shape 推导）做什么、它在 aclnn 两阶段 API 的哪个阶段被触发。
3. 理解 tiling 计算的输入（平台信息 + shape）与输出（tiling data / tiling key / blockDim），以及 tiling data 是如何从 host 传到 device 的。
4. 动手为本算子新增一个输出 shape 规则，并重新编译、跑 UT 验证。

## 2. 前置知识

在精读源码前，先建立三个直觉概念：

- **Host 侧与 Device 侧**：NPU 编程模型里，CPU 上运行的代码叫 host 侧，NPU 计算核心（AICore）上运行的代码叫 device 侧。host 侧不负责真正的计算，它负责「下单」：告诉系统这个算子长什么样（def）、输出 tensor 该分配多大（infershape）、数据该怎么切分给各个核（tiling）。
- **静态信息 vs 动态信息**：算子的输入输出个数、支持的数据类型，在写代码时就确定了，属于静态信息，由 def 文件注册；而输出的具体 shape、tiling 参数，要等运行时拿到真实输入才能算出来，属于动态信息，由 infershape 和 tiling 函数在每次调用时计算。
- **aclnn 两阶段 API**：aclnnXxx 接口分 GetWorkspaceSize 和 Run 两步。GetWorkspaceSize 阶段会触发 infershape（算出输出 shape）和 tiling（算出切分参数）；Run 阶段才真正启动 kernel。本讲的三件套正是在第一阶段工作的。这一机制将在 u3-l1 详细展开，这里只需记住「三件套跑在 host 侧、调用早期」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/add_example/op_host/add_example_def.cpp` | 算子原型定义：注册输入/输出、数据类型、format、多 SoC 配置，并挂接 kernel 入口文件名 |
| `examples/add_example/op_host/add_example_infershape.cpp` | shape 推导：根据输入 shape 计算输出 shape |
| `examples/add_example/op_host/add_example_tiling.cpp` | tiling 计算：读取平台信息与输入 shape，产出 tiling data、tiling key、blockDim、workspace 大小 |
| `examples/add_example/op_kernel/add_example_tiling_data.h` | tiling data 结构体定义，host 与 device 共同包含，是两侧之间的「数据合同」 |
| `examples/add_example/op_kernel/add_example_tiling_key.h` | tiling key 声明（本讲作为 tiling 模块的补充材料） |
| `examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp` | infershape 的单元测试，本讲代码实践要用到它 |

注意一个细节：tiling data 和 tiling key 的头文件放在 `op_kernel` 目录下，而不是 `op_host` 下。这是因为这两个结构体是 host 侧填充、device 侧消费的，放在 kernel 目录可以强调「它们描述的是 device 侧执行时需要的信息」。

## 4. 核心概念与源码讲解

### 4.1 算子定义（def 文件）

#### 4.1.1 概念说明

def 文件回答的问题是：**「这个算子叫什么、长什么样、在哪些芯片上可用？」** 它把算子的静态元信息注册进 CANN 的算子信息库（op registry）。后续无论是 aclnn Eager 调用还是 GE 图模式构图，框架都是先查这个信息库来认识算子的。

一个 def 文件的核心内容通常包括：

- 输入/输出列表：每个 tensor 的名字、是否必选、支持的数据类型、format。
- AICore 配置：动态 shape 支持、是否降精度等编译开关。
- `ExtendCfgInfo("opFile.value", ...)`：把 host 侧注册和 device 侧 kernel 入口文件「挂钩」。
- `AddConfig`：声明该算子支持哪些 SoC（如 ascend910b / ascend910_93 / ascend950）。

#### 4.1.2 核心流程

```text
OpDef 派生类构造
  ├── Input("x1") / Input("x2") / Output("y")   → 登记 tensor 元信息
  ├── OpAICoreConfig                             → 登记编译行为开关
  │     └── ExtendCfgInfo("opFile.value", "add_example")
  │            → 指向 op_kernel/add_example.cpp 这个 kernel 入口
  └── AICore().AddConfig("ascend910b"/"ascend910_93"/"ascend950")
        → 每代 SoC 各注册一份配置
最后 OP_ADD(AddExample) 把整个类实例化并注入全局算子信息库
```

`DataType({ge::DT_FLOAT, ge::DT_INT32})` 这种「花括号列表」写法表示按位置组合：列表里是 dtype 集合，format 列表与之对应展开。也就是说 x1、x2、y 三者都允许 FLOAT 或 INT32，且要求 ND（任意维）format。

#### 4.1.3 源码精读

先看输入定义。这段代码注册了输入 x1：必选（`REQUIRED`）、支持 FLOAT/INT32、ND format，并开启 `AutoContiguous()`——框架会在调用前自动把非连续内存整理成连续的，这解释了为什么这个教学算子不需要自己处理非连续 tensor：

[add_example_def.cpp:L22-L33](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L22-L33) —— 逐项配置输入 x1 与 x2 的参数类型、dtype、format 和自动连续化。

再看输出 y，写法与输入完全一致（输出也声明 dtype/format 是因为框架需要据此为输出分配内存）：

[add_example_def.cpp:L34-L39](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L34-L39) —— 注册输出 y，元信息与输入相同。

然后是 AICore 配置与 SoC 注册，这是 def 文件里最值得注意的一段：

[add_example_def.cpp:L41-L51](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L41-L51) —— `DynamicShapeSupportFlag(true)` 声明算子支持动态 shape（这是 tiling 存在的前提）；`ExtendCfgInfo("opFile.value", "add_example")` 把 host 注册与 kernel 入口文件 `op_kernel/add_example.cpp` 绑定；三次 `AddConfig` 分别注册到 ascend910b（A2）、ascend910_93（A3）、ascend950（A5）三代 SoC。

最后由一个宏完成注册：

[add_example_def.cpp:L54-L55](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L54-L55) —— `OP_ADD(AddExample)` 在全局构造期实例化该类，把算子信息写入信息库；没有这一行，前面所有配置都不会生效。

#### 4.1.4 代码实践

**实践目标**：直观感受「def 文件决定框架眼中的算子长相」。

1. 打开 [add_example_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp)。
2. 把输出 y 的 `DataType` 从 `{ge::DT_FLOAT, ge::DT_INT32}` 改成 `{ge::DT_FLOAT}`（只在本地改着观察，实验后还原）。
3. 重新编译宿主库：`bash build.sh --ophost --ops=add_example`（编译方法见 u1-l4）。
4. 观察编译是否通过；若你有 NPU 环境，再跑 eager 示例（见 u2-l4），预期 INT32 输入路径会因 dtype 不受支持而报错。

**需要观察的现象**：只改 host 侧声明、不改 kernel，dtype 受限后报错发生在框架的参数校验层，而不是 kernel 内部——说明 def 注册是框架校验的第一道关卡。若无法在本地运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果想让 add_example 支持第四代 SoC（假设名为 ascendxxx），def 文件需要加哪一行？

答案：在 [add_example_def.cpp:L49-L51](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L49-L51) 处追加一行 `this->AICore().AddConfig(" ascendxxx", aicoreConfig);`（去掉前导空格）。同时还需要在 op_host/config 下为新 SoC 补充 binary/ini 配置，kernel 侧也要有对应实现——def 只是入口之一。

**练习 2**：`AutoContiguous()` 解决什么问题？

答案：用户传入的 tensor 可能是转置、切片得到的非连续内存。开启后框架会自动做连续化拷贝，kernel 就可以假设数据在内存中按 shape 顺序紧密排列，简化 device 侧代码。代价是多一次潜在拷贝。

**练习 3**：`OP_ADD(AddExample)` 为什么不能漏掉？

答案：def 类只是 C++ 类定义，`OP_ADD` 宏负责在程序启动的全局对象构造阶段实例化它并把元信息登记进算子信息库。漏掉后编译不报错，但框架查不到该算子，运行时报「算子不存在」类错误。

### 4.2 shape 推导（infershape）

#### 4.2.1 概念说明

infershape 回答的问题是：**「给定输入的 shape，输出 tensor 的 shape 是什么？」** 对逐元素加法，输出 shape 等于输入 shape；但对 matmul、reshape 类算子，输出 shape 需要真正的推导逻辑。框架必须在分配输出内存之前知道输出多大，所以 infershape 是 aclnn GetWorkspaceSize 阶段最早执行的 host 逻辑之一。

另外要注意：infershape 面对的 shape 可能含 `-1`（未知维度），从下面 UT 用例中就能看到 `{1, -1, -1, 64}` 这样的输入——推导逻辑必须在这种「部分未知」的 shape 上也能工作。

#### 4.2.2 核心流程

```text
框架调用 InferShapeAddExample(context)
  ├── context->GetInputShape(0)     → 取输入 x1 的 shape
  ├── context->GetOutputShape(0)    → 取输出 y 的 shape 槽位（待填充）
  ├── 遍历输入每个维度
  └── yShape->SetDim(i, xShape->GetDim(i)) → 逐维复制
返回 GRAPH_SUCCESS
```

本算子的推导规则用伪代码表达就是：\( y_i = x_i, \forall i \in [0, rank) \)，即输出与输入逐维相同。

#### 4.2.3 源码精读

推导函数主体如下：

[add_example_infershape.cpp:L23-L45](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_infershape.cpp#L23-L45) —— 先通过 `GetInputShape`/`GetOutputShape` 从 context 取 shape（每个指针都做了 `OP_CHECK_NULL_WITH_CONTEXT` 判空，这是 host 侧代码的标准防御式写法），再把输入 shape 的维数和每一维的值复制给输出。注意它复制的是「值」，包括 `-1` 这种未知维度也会原样复制。

函数写好后需要注册，把推导函数与 def 中注册的算子名关联起来：

[add_example_infershape.cpp:L47-L48](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_infershape.cpp#L47-L48) —— `IMPL_OP_INFERSHAPE(AddExample).InferShape(InferShapeAddExample)` 把该函数登记为 AddExample 的 shape 推导实现，作用类似于 def 里的 `OP_ADD`。

再看对应的 UT，理解「infershape 的行为是被怎么测出来的」：

[test_add_example_infershape.cpp:L29-L44](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp#L29-L44) —— UT 用 `InfershapeContextPara` 构造两个 shape 为 `{1, -1, -1, 64}` 的 fp16 输入，期望输出 shape 为 `{1, -1, -1, 64}`，然后由 `ExecuteTestCase` 驱动真实的推导函数并断言结果。注意：**UT 里用的 dtype 是 fp16，而 def 里注册的是 FLOAT/INT32**——infershape 单测只关注 shape 维度拷贝逻辑，不检查 dtype 合法性，dtype 校验发生在别处（例如 tiling 的 `GetShapeAttrsInfo`，见 4.3.3）。

#### 4.2.4 代码实践

**实践目标**：为本算子新增第二个输出 `y2`（与输入同 shape），完整走一遍「改 def → 改 infershape → 重编译 → 跑 UT」。

1. 修改 def：仿照 [add_example_def.cpp:L34-L39](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L34-L39) 的写法，追加 `this->Output("y2")...` 一段（元信息与 y 相同）。
2. 修改 infershape：在 [add_example_infershape.cpp:L32-L41](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_infershape.cpp#L32-L41) 后仿照一段，取 `GetOutputShape(1)` 并把 x 的 shape 复制进去。
3. 重新编译：`bash build.sh --ophost --ops=add_example`。
4. 跑 infershape UT：`bash build.sh --ophost_test --ops=add_example`（UT 体系详见 u7-l1；如环境不支持运行，至少确认 UT 可编译，标注「待本地验证」）。

**需要观察的现象 / 预期结果**：原 UT [test_add_example_infershape.cpp:L31-L43](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp#L31-L43) 中输出列表只有一个元素，改造后你会看到 UT 用例的输出描述与实际推导输出个数不一致导致失败——这正是练习的价值：**测试用例是算子「合同」的一部分，改接口必须同步改测试**。把 UT 的输出列表也补上第二个 `{{{},{}}}` 条目和期望 shape 后，测试恢复通过。

> 说明：本仓库当前的 infershape 实现在独立的 `add_example_infershape.cpp` 中（函数名 `InferShapeAddExample`），并没有写在 def 文件内部的 `DoInferShape` 成员函数里；两种写法在 CANN 生态中都存在，本仓库采用的是「分文件 + IMPL_OP_INFERSHAPE 注册」风格。

#### 4.2.5 小练习与答案

**练习 1**：如果输入 x1 是标量（shape 为空），这段推导代码行为如何？

答案：`GetDimNum()` 返回 0，循环体不执行，输出 shape 是 0 维——即输出也是标量。但在 tiling 侧有 `EnsureNotScalar` 把标量当作 `{1}` 处理（见 4.3.3），两侧对「标量」的约定并不相同，这是阅读真实代码时值得留意的细节。

**练习 2**：为什么输出 shape 的 `-1` 可以原样复制而不用算出真实值？

答案：`-1` 表示该维度在当前阶段未知（动态 shape）。框架允许输出暂时含 `-1`，等真实数据到达后（或在图编译的 shape 泛化阶段）再确定；推导函数只需保证「已知部分的映射关系正确」。

**练习 3**：infershape 和 tiling 都能拿到输入 shape，它们的分工是什么？

答案：infershape 只决定输出 tensor 的 shape（用于分配输出内存），不做任何切分计算，也通常不读平台信息；tiling 则结合平台资源（UB 大小、核数）与 shape 决定数据如何切分、启多少 block、走哪个 kernel 变体。前者面向内存分配，后者面向执行计划。

### 4.3 tiling 计算

#### 4.3.1 概念说明

tiling 回答的问题是：**「这个算子的数据怎么切给 NPU 上的多个核去算？」** NPU 一次 kernel 启动会拉起若干个 block（对应若干 AI Core/Vector 核），每个核只能先把一小块数据从全局内存（GM）搬进自己私有的 Unified Buffer（UB）里算，算完再搬回去。把大数据「切块」的方案就叫 tiling。

host 侧 tiling 函数的产出有四类：

| 产出 | 载体 | 消费方 |
| --- | --- | --- |
| tiling data（切分参数，如总长度、每块大小） | `context->GetTilingData<T>()` 写入的结构体 | device kernel 读取 |
| tiling key（变体选择号） | `context->SetTilingKey()` | 二进制/kernel 选择器，决定加载哪个预编译变体 |
| blockDim（启动多少个 block） | `context->SetBlockDim()` | kernel 启动器 |
| workspace 大小 | `context->GetWorkspaceSizes()` | 框架内存分配 |

其中 tiling data 的「合同」就是 `op_kernel/add_example_tiling_data.h` 中的结构体——host 填、device 读，两侧必须包含同一个头文件。

#### 4.3.2 核心流程

`AddExampleTilingFunc` 是 tiling 入口，流程分四步：

```text
AddExampleTilingFunc(context)
  ├── ① GetPlatformInfo   → 从平台信息取 ubSize、coreNumAiv（硬件资源上限）
  ├── ② GetShapeAttrsInfo → 取输入 shape（限定 4 维）、校验 dtype ∈ {FLOAT, INT32}
  │                          计算 totalIdx = N*C*H*W（元素总数）
  ├── ③ GetWorkspaceSize  → 固定申请 16MB 系统 workspace
  └── ④ 填充产出：
        ├── tiling->totalLength = totalIdx
        ├── tiling->tileNum     = 8        （常量）
        ├── context->SetBlockDim(8)        （常量）
        └── 按 dtype 设 tilingKey：
              FLOAT → MODE_0，INT32 → MODE_1
```

tiling key 的选取逻辑可以用一个简单映射表达：

\[
\text{tilingKey} = \begin{cases} \text{MODE\_0} & \text{dtype} = \text{DT\_FLOAT} \\ \text{MODE\_1} & \text{dtype} = \text{DT\_INT32} \end{cases}
\]

教学算子的 tiling 是「写死」的（blockDim=8、tileNum=8），目的是让读者先看清骨架；工业算子的 tiling 会用 ubSize/coreNum 做真正的资源规划，这在 u4-l3 精读 flash_attention_score 时会看到。

#### 4.3.3 源码精读

先看平台信息获取——tiling 与 infershape 最大的不同就是它关心硬件：

[add_example_tiling.cpp:L40-L51](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L40-L51) —— 通过 `context->GetPlatformInfo()` 构造 `PlatformAscendC` 适配器，取出 AI Vector 核数（`GetCoreNumAiv`）和 UB 内存大小；两者为 0 都直接报错返回。

然后是 shape/属性收集与校验：

[add_example_tiling.cpp:L54-L91](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L54-L91) —— 用 `EnsureNotScalar` 把标量 shape 归一为 `{1}`；用 `OP_CHECK_IF` 校验输入输出都必须是 4 维，否则打日志返回失败；随后取出 N/C/H/W 计算 `totalIdx`（元素总数）；最后校验 dtype 必须是 FLOAT 或 INT32——这就是 4.2.3 中提到的「dtype 校验在 tiling 侧」的实锤位置。

workspace 大小声明：

[add_example_tiling.cpp:L93-L99](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L93-L99) —— `GetWorkspaceSizes(1)` 取出 workspace 尺寸数组并固定写 16MB；本算子其实用不到 workspace，这里是为了演示接口用法。

tiling 主流程与产出填充：

[add_example_tiling.cpp:L102-L141](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L102-L141) —— 四步依次调用后，`GetTilingData<AddExampleTilingData>()` 拿到框架分配的 tiling data 指针，`memset_s` 清零，写入 `totalLength` 与 `tileNum`；`SetBlockDim(8)` 声明启动 8 个 block；最后按 dtype 二选一设置 tilingKey（`GET_TPL_TILING_KEY` 宏来自 [add_example_tiling_key.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_key.h)，其中 `ASCENDC_TPL_ARGS_DECL` 声明了一个名为 schMode 的模板参数，取值 0/1，与 kernel 侧的模板分支一一对应）。

tiling 注册入口：

[add_example_tiling.cpp:L143-L150](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L143-L150) —— `IMPL_OP_OPTILING(AddExample).Tiling(...).TilingParse<...>(...)` 把 tiling 函数注册给 AddExample；`TilingParseForAddExample` 目前是空实现（配合空的 `AddExampleCompileInfo` 结构体），预留了「从编译产物反解信息」的扩展点。

最后是 host/device 共享的数据合同：

[add_example_tiling_data.h:L19-L23](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L23) —— `AddExampleTilingData` 只有 `totalLength` 和 `tileNum` 两个 int64 字段，host 侧在 tiling 函数里填充，device 侧 kernel 通过同一结构体解析。顺带一提，这个文件的 include guard 是 `_ROTARY_POSITION_EMBEDDING_GRAD_TILING_DATA_H_`——从别的算子（rotary_position_embedding_grad）复制模板时忘了改名。这不影响编译（guard 只需本文件内唯一），但提醒我们：仓库里大量算子是「复制-修改」出来的，阅读时要警惕这类复制残留，自己写算子时应改成与文件匹配的名字。

#### 4.3.4 代码实践

**实践目标**：观察 tiling data 从「结构体定义」到「运行时消费」的链路，理解 host/device 的数据合同。

1. 阅读 [add_example_tiling_data.h:L19-L23](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L23) 和 [add_example_tiling.cpp:L119-L127](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L119-L127)，确认 host 侧写入了哪些字段。
2. 用 Grep 在 `examples/add_example/op_kernel/add_example.cpp` 中搜索 `AddExampleTilingData`、`GetTilingData`、`totalLength`，找到 device 侧读取 tiling data 的代码（kernel 入口函数会通过框架 API 拿到同一块内存并按结构体解析）。
3. 做一个「破坏性实验」（本地临时改，实验后还原）：把 `tiling->totalLength = totalIdx;` 改成 `tiling->totalLength = totalIdx / 2;`，重新编译 `bash build.sh --ophost --ops=add_example`。
4. 有 NPU/simulator 环境则运行 eager 示例（u2-l4 的 `--run_example`），观察输出只计算了前一半元素（或长度校验报错）；无环境则写出你的预测并标注「待本地验证」。

**预期结果**：totalLength 是 host 告诉 device 「一共有多少个元素要算」的唯一凭据，改小后 device 只处理一半数据，后半段输出是未初始化内存——这说明 tiling data 没有冗余校验，合同两端必须一致。

#### 4.3.5 小练习与答案

**练习 1**：教学算子把 `BLOCK_DIM` 写死为 8，真实算子应该怎么取值？

答案：应以 `GetCoreNumAiv()` 取到的核数为基础，结合数据量做映射（如 `min(coreNum, ceil(totalIdx / 每核最小工作量))`）：数据小的时候少占核，数据大的时候占满核。代码里 [add_example_tiling.cpp:L46](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L46) 已经把 coreNum 取出来了，只是没用于 blockDim，这正是预留给读者的改进点。

**练习 2**：`TILING_KEY` 的 MODE_0/MODE_1 与 def 里注册的 dtype 集合是什么关系？

答案：def 层面声明「本算子接受 FLOAT 和 INT32」（合法性）；tiling key 层面则根据实际 dtype 选择不同的 kernel 二进制变体（路由）。FLOAT 走 MODE_0、INT32 走 MODE_1，device 侧的 kernel 用模板参数 schMode 区分两个变体分别编译，使每种 dtype 都有针对性的指令优化。

**练习 3**：为什么 tiling data 结构体放在 op_kernel 目录、而填充它的代码在 op_host 目录？

答案：tiling data 描述的是 device 执行所需信息，本质上是 kernel 的「参数表」，由 kernel 作者定义；host 侧 tiling 函数只是填表方。放在 op_kernel 目录使 kernel 代码自包含（kernel 及其参数定义在一起），host 通过相对路径包含（[add_example_tiling.cpp:L20](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L20) 的 `#include "../op_kernel/add_example_tiling_data.h"`），保证两端编译时看到的是同一份定义。

## 5. 综合实践

把三件串起来做一个小改造：**给 add_example 增加一个布尔属性 `half_output`，当属性为 true 时输出 shape 保持不变、但意义改为「只算前一半元素」**。

1. **def 侧**：在 [add_example_def.cpp:L41](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L41) 之前仿照 Input/Output 的链式写法追加 `this->Attr("half_output", ...)` 布尔属性（可参考仓库其他算子 def 中 `Attr` 的用法，用 Grep 搜索 `\.Attr\(` 找一个布尔属性样例）。
2. **tiling 侧**：在 [add_example_tiling.cpp:L54-L91](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L54-L91) 的 `GetShapeAttrsInfo` 中用 `context->GetAttrs()->GetAttrPointer<bool>(0)` 读出属性，为 true 时把 `totalIdx` 减半（对应 totalLength 减半），并加一行 `OP_LOGI` 日志。
3. **编译验证**：`bash build.sh --ophost --ops=add_example`；若有环境，再跑 tiling UT（`tests/ut/op_host/test_add_example_tiling.cpp`，运行方式见 u7-l1）对比改造前后用例输出中 totalLength 的变化。
4. **思考题（选做）**：infershape 要不要跟着改？为什么？（答案：不需要——输出 shape 语义没变，仍与输入相同；变的是「计算多少元素」，属于 tiling 职责。这个练习正好划清了三件套的职责边界。）

完成后记得用 `git checkout -- examples/` 还原现场，保持仓库干净。

## 6. 本讲小结

- **def 是静态户口**：注册输入/输出元信息、编译开关、kernel 入口挂钩（`ExtendCfgInfo`）和多 SoC 支持（`AddConfig`），经 `OP_ADD` 注入算子信息库，是框架校验的第一道关卡。
- **infershape 是动态量尺**：在 aclnn GetWorkspaceSize 阶段早期执行，从输入 shape 推导输出 shape 供框架分配内存，能处理含 `-1` 的动态 shape，通过 `IMPL_OP_INFERSHAPE` 注册。
- **tiling 是执行计划**：综合平台信息（UB/核数）与输入 shape，产出 tiling data（切分参数）、tiling key（kernel 变体路由）、blockDim 和 workspace 大小四类信息。
- **tiling data 是 host/device 合同**：结构体定义在 op_kernel 目录、host 填充、device 解析，两端共享同一头文件，没有运行时校验。
- **教学算子的 tiling 参数是写死的**（blockDim=8、tileNum=8），工业级 tiling 策略将在 u4-l3 的 flash_attention_score 中展开。
- 阅读复制出来的算子代码时要警惕复制残留（如 add_example_tiling_data.h 里错误的 include guard 名）。

## 7. 下一步学习建议

下一讲（u2-l3）将进入 device 侧，精读 `op_kernel/add_example.cpp`——本讲准备的 tiling data 和 tiling key 会在 kernel 入口被消费，你会看到 `AddExampleTilingData` 的另一半故事。建议先自行浏览该文件，重点找三样东西：kernel 入口宏、`GetTilingData` 的 device 侧调用、按 `totalLength/tileNum` 组织的循环。之后再进入 u2-l4 学习如何把算子跑起来（Eager 与 Graph 两种方式）。若想提前理解 tiling key 与二进制变体的深层关系，可以回头读 u2-l1 提到的 `op_host/config` 下 binary.json 与 ini 配置。
