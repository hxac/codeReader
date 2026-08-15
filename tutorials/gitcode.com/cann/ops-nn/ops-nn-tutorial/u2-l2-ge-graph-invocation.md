# u2-l2 GE 图模式调用算子：从图构建到算子执行

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 **GE 图模式（GEIR）调用** 与上一讲 **aclnn eager 调用** 的本质区别：一个是"先把计算画成一张图再整体执行"，一个是"每次调用立即下发一个算子"。
2. 理解 `op_graph` 交付件的作用：`*_proto.h` 中的 `REG_OP` 宏如何把算子"身份证"注册进 Graph Engine，让 GE 在构图和图优化阶段能识别这个算子。
3. 读懂 `test_geir_add_example.cpp` 的完整骨架：初始化 GE → 构图（Data 节点 + 算子节点）→ 建会话 → AddGraph → RunGraph → 取回输出。
4. 能独立运行 GE 图模式样例，并在图中串接第二个算子节点。

## 2. 前置知识

### 2.1 什么是"图模式"

在上一讲（u2-l1）中，我们用 aclnn 两段式 API **直接**调用算子：`GetWorkspaceSize` 登记一次执行，`aclnnXxx` 立即把这一个算子异步提交到 stream。这种方式叫 **eager（即时）模式**，特点是"一算子一调用"，适合拼装零散计算。

**图模式**则是另一种思路：先把要做的一系列计算抽象成一张 **计算图（Graph）**——图里的节点（Operator）是算子，边代表张量的生产/消费关系——然后把整张图交给 **Graph Engine（GE，图引擎）**。GE 会对图做编译优化（算子融合、内存规划、切分调度），最后一次性下发执行。

用一个比喻：

- eager 模式像"点菜一道做一道"：灵活性高，但每道菜之间有沟通开销。
- 图模式像"把整桌菜单交给厨房统一排菜"：厨房可以合并工序（融合算子）、提前备料（内存复用），整桌效率更高。

GE 图模式调用在本仓库文档中也叫 **GEIR 调用**（GE Intermediate Representation，GE 中间表示），对应的样例文件统一命名为 `test_geir_<op_name>.cpp`。

### 2.2 图模式需要的两个额外推导能力

图在编译时只知道每个节点的输入描述，输出必须"推"出来，因此算子入图必须额外提供两个推导函数：

- **InferShape**：根据输入 shape 推导输出 shape（决定输出张量占多少内存）。
- **InferDataType**：根据输入 dtype 推导输出 dtype（决定输出按什么类型解释）。

eager 模式下这两步由 aclnn 适配层在 Host 侧完成；图模式下则由 GE 在图编译阶段调用这两个注册函数完成。

### 2.3 相关术语

| 术语 | 含义 |
| --- | --- |
| Graph | 一张计算图，由若干 Operator 节点和它们的连接关系构成 |
| Operator | 图中的算子节点，通过 `op::Xxx()` 工厂函数创建 |
| `op::Data` | 图的"数据入口"节点，代表一个外部喂进来的输入张量 |
| Session | GE 的执行会话，负责把图编译并下发到设备执行 |
| `REG_OP` | 在 `*_proto.h` 中注册算子原型（输入/输出/属性）的宏 |
| proto | prototype（原型）的缩写，即算子对外暴露的"接口说明书" |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/add_example/op_graph/add_example_proto.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_graph/add_example_proto.h) | 算子原型定义：用 `REG_OP` 声明 AddExample 的两个输入、一个输出及支持的 dtype，供 GE 识别 |
| [examples/add_example/op_graph/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_graph/CMakeLists.txt) | op_graph 侧构建脚本，调用 `add_graph_plugin_sources()` 把本目录编入图插件 |
| [examples/add_example/op_host/add_example_infershape.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_infershape.cpp) | InferShape 与 InferDataType 的实现及注册（add_example 把两者合并在一个文件里） |
| [examples/add_example/examples/test_geir_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_geir_add_example.cpp) | GE 图模式调用样例：构图、建会话、执行、落盘输出 |
| [build.sh](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh) | `--run_example ... graph` 分支：编译并执行 `test_geir_*.cpp` |
| [docs/zh/develop/graph_develop_guide.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/graph_develop_guide.md) | 官方图模式适配指南：讲清入图需要哪些交付件 |
| [docs/zh/invocation/quick_op_invocation.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md) | 官方算子调用指南，其中"GE图模式"一节是本讲实践的命令依据 |

## 4. 核心概念与源码讲解

### 4.1 图模式与 eager 模式的差异

#### 4.1.1 概念说明

两种调用方式的本质差异在于 **"谁决定执行"**：

- eager：用户代码自己控制每次算子调用，aclnn 适配层逐个把算子塞进 stream。
- 图模式：用户只负责 **描述** 计算（构图），执行计划完全交给 GE——GE 拿到图后做编译优化，再把整张图（可能已被改写、融合）下发执行。

对照官方文档给出的调用方式清单，ops-nn 支持 PyTorch API、aclnn API、GE 图模式三种调用方式，其中 GE 图模式的说明为"通过算子 IR（Intermediate Representation）定义，以构图方式实现算子调用"（见 [quick_op_invocation.md:L22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md#L22)）。

#### 4.1.2 核心流程

两种模式的调用流程对照（伪代码）：

```text
eager 模式（上一讲）:
  aclInit → 构造 aclTensor → aclnnXxxGetWorkspaceSize（登记）
          → 申请 workspace → aclnnXxx（下发） → 同步 → 取结果
  特点：一个算子一轮，算子之间无全局优化

图模式（本讲）:
  GEInitialize → new Graph → 图里放 op::Data 输入节点 + op::Xxx 算子节点
              → 连边（算子的输入接 Data 或上游算子的输出）
              → graph.SetInputs/SetOutputs
              → new Session → session->AddGraph（编译）
              → session->RunGraph（执行，喂输入 Tensor，取输出 Tensor）
              → GEFinalize
  特点：一次描述整张图，GE 统一优化并执行
```

#### 4.1.3 源码精读

两种模式在构建系统层面也有差异。`build.sh` 的样例执行函数按模式分两条编译路径：eager 模式链接 `-lopapi_nn -lopapi_math -lascendcl` 等 aclnn 相关库，而图模式链接的是 GE 全家桶：

[build.sh:L1576-L1580](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1576-L1580) —— 图模式样例用 g++ 编译时链接 `-lgraph -lge_runner -lgraph_base -lge_compiler`，这四个库分别提供构图 API、会话执行、图基础数据和图编译能力。

[build.sh:L1599-L1600](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1599-L1600) —— 图模式的样例文件按 `test_geir_` 前缀查找，这正是样例文件命名约定的来源。

另外，运行命令上二者也不同：`--run_example` 的第二个参数为 mode，取 `eager` 或 `graph`；文档明确说明 **mode 为 graph 时不指定 pkg_mode 和 vendor_name**（见 [quick_op_invocation.md:L47-L55](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md#L47-L55)），因为图引擎会根据环境变量自动加载已安装的算子包，无需像 eager 那样手动指定自定义包路径。

#### 4.1.4 代码实践

**实践目标**：直观感受同一算子在两种模式下的调用命令与输出形式差异。

**操作步骤**：

1. 前置：按 u1-l2 完成编译并安装了 add_example 的自定义算子包（`bash build.sh --pkg --soc=${soc_version} --ops=add_example`）。
2. 运行 eager 样例：`bash build.sh --run_example add_example eager cust --vendor_name=custom`
3. 运行图模式样例：`bash build.sh --run_example add_example graph`

**需要观察的现象**：两条命令各自动找到 `examples/` 下的 `test_aclnn_add_example.cpp` 和 `test_geir_add_example.cpp`，g++ 编译后执行；日志前缀不同（eager 走 aclnn 日志，graph 走 `[XIR]` 前缀的 GE 日志）。

**预期结果**：两条命令都以 `Run xxx success.` 结尾；图模式额外在当前目录生成 `dump` 目录（图结构文本）和若干 `tc_ge_irrun_test_*_input/output_*.bin` 文件。具体输出内容**待本地验证**（本讲义编写环境无 NPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么图模式不需要像 eager 模式那样在命令里指定 `cust --vendor_name=custom`？

**答案**：图引擎（GE）根据配置好的环境变量自动发现并加载已安装的算子包（无论自定义包还是内置包），算子识别依赖的是注册进 GE 的算子原型，而不是 eager 路径下按 vendor 目录查找 aclnn 动态库的机制，所以无需区分。文档在 [quick_op_invocation.md:L55](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md#L55) 明确写了这条规则。

**练习 2**：如果你要把 10 个算子串成一条流水线，且整条流水线会反复执行，选 eager 还是图模式更合适？为什么？

**答案**：图模式更合适。图模式下 10 个算子构成一张图，GE 只需编译一次，之后每次 `RunGraph` 复用编译结果，还能在编译期做算子融合与内存复用优化；eager 模式每次调用都要走一遍 Host 侧登记与下发，算子间的调度开销无法消除。

### 4.2 op_graph 交付件：REG_OP 算子原型注册

#### 4.2.1 概念说明

GE 要管理一张图，就必须"认识"图里的每个算子：它有几个输入、几个输出、各是什么类型、有哪些属性。这些信息由算子**原型（proto）** 描述，通过 `REG_OP` 宏注册。这就是 `op_graph` 交付件存在的意义——它是算子进入图世界的"报关单"。

官方图模式适配指南给出了入图所需的交付件结构（见 [graph_develop_guide.md:L7-L15](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/graph_develop_guide.md#L7-L15)）：`op_host` 下要有 InferShape 实现，`op_graph` 下要有原型定义头文件；并且明确指出图模式**不需要 aclnn 适配**。指南还给出了标准目录里应包含 `${op_name}_graph_infer.cpp`（InferDataType 交付件）——注意 add_example 这个教学样例实际把 InferDataType 合并写进了 `op_host/add_example_infershape.cpp`，这是样例的简化，不是与文档冲突。

#### 4.2.2 核心流程

算子原型注册到 GE 可被图模式使用的数据流：

```text
*.proto.h 编译为图插件库（opgraph_nn.so 的一部分）
        ↓  REG_OP(AddExample)...OP_END_FACTORY_REG(AddExample)
注册进 GE 的算子原型注册表（输入/输出/属性清单）
        ↓
用户样例 #include "../op_graph/add_example_proto.h"
        ↓
op::AddExample("add1") 工厂函数可用 → 图中可创建该类型节点
        ↓
GE 图编译阶段查注册表 + 调 InferShape/InferDataType 推导输出
```

#### 4.2.3 源码精读

[examples/add_example/op_graph/add_example_proto.h:L35-L39](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_graph/add_example_proto.h#L35-L39) —— AddExample 的算子原型注册：声明两个必选输入 `x1`、`x2` 和一个输出 `y`，三者的 TensorType 都允许 `DT_FLOAT` 或 `DT_INT32`。这四行就是 GE 识别 AddExample 所需的全部"身份证信息"。

```cpp
REG_OP(AddExample)
    .INPUT(x1, TensorType({DT_FLOAT, DT_INT32}))
    .INPUT(x2, TensorType({DT_FLOAT, DT_INT32}))
    .OUTPUT(y, TensorType({DT_FLOAT, DT_INT32}))
    .OP_END_FACTORY_REG(AddExample)
```

`REG_OP` 之上还有大段注释（[add_example_proto.h:L23-L34](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_graph/add_example_proto.h#L23-L34)），用 Doxygen 风格说明算子功能、输入输出约束和框架兼容性——这份注释会被文档系统抓取，是算子约束的第一手材料。

关于 `REG_OP` 的语法要素，官方指南整理成了速查表（[graph_develop_guide.md:L100-L106](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/graph_develop_guide.md#L100-L106)）：

| 输入/输出 | 关键字 | 写法示例 |
| --- | --- | --- |
| 必选输入 | INPUT | `.INPUT(x1, TensorType({DT_FLOAT}))` |
| 可选输入 | OPTIONAL_INPUT | `.OPTIONAL_INPUT(...)` |
| 必选属性 | REQUIRED_ATTR | `.REQUIRED_ATTR(name, Int)` |
| 可选属性 | ATTR | `.ATTR(name, Int, 默认值)` |
| 输出 | OUTPUT | `.OUTPUT(y, TensorType({DT_FLOAT}))` |

[examples/add_example/op_graph/CMakeLists.txt:L11](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_graph/CMakeLists.txt#L11) —— 整个 op_graph 目录的构建只有一行 `add_graph_plugin_sources()`：把本目录源文件收进图插件库。目录下还有一个空的 `fusion_pass/` 子目录（只含 `.gitkeep`），那是图融合 pass 的预留位置，本讲不展开。

注册了原型之后，输出描述还差"推导"能力。[examples/add_example/op_host/add_example_infershape.cpp:L37-L61](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_infershape.cpp#L37-L61) 是 InferShape 实现：取出输入 x1 的 shape，把维数和每一维依次复制给输出 y（加法的输出 shape 与输入一致）。[L72-L83](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_infershape.cpp#L72-L83) 是 InferDataType 实现：输出 dtype 直接取输入 dtype。

最后，[L87](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_infershape.cpp#L87) 一行把两个推导函数挂到 AddExample 名下：

```cpp
IMPL_OP_INFERSHAPE(AddExample).InferShape(InferShapeAddExample).InferDataType(InferDataTypeAddExample);
```

#### 4.2.4 代码实践

**实践目标**：验证"改 proto 的 dtype 声明会影响图模式可用的类型"。

**操作步骤**：

1. 打开 `examples/add_example/op_graph/add_example_proto.h`，把三处 `TensorType({DT_FLOAT, DT_INT32})` 中的 `DT_INT32` 删掉（只留 `DT_FLOAT`）。
2. 重新编译安装：`bash build.sh --pkg --soc=${soc_version} --ops=add_example`，并安装 run 包。
3. 再跑一次 `bash build.sh --run_example add_example graph`。

**需要观察的现象**：样例默认用 `DT_FLOAT`（见下一节 `main` 中的 `DataType inDtype = DT_FLOAT;`），所以 float 路径仍然能跑通；但如果你把样例里的 `inDtype` 改成 `DT_INT32` 再编译运行，GE 在图编译阶段就会因原型不支持该 dtype 而报错。

**预期结果**：float 样例正常；改 `DT_INT32` 后报"算子原型不匹配/不支持该类型"一类的 GE 错误。报错的具体文案**待本地验证**。实验完成后记得把 `DT_INT32` 加回去。

#### 4.2.5 小练习与答案

**练习 1**：`REG_OP` 与上一讲提到的 `OP_ADD`（`*_def.cpp` 中）各注册什么？为什么图模式两个都要？

**答案**：`OP_ADD` 注册的是算子的**算子库定义**（op_host 侧，供 tiling、kernel 选择，服务 eager/aclnn 执行路径）；`REG_OP` 注册的是算子的**图原型**（op_graph 侧，供 GE 构图、图优化、shape/dtype 推导）。图模式既要 GE 认识这个算子（REG_OP），又要最终能执行它（OP_ADD 注册后的 host 交付件 + op_kernel），所以两个都不可少。

**练习 2**：一个有三个输入（其中一个是可选的 scale）和一个属性的量化算子，proto 应该怎么写？

**答案**：参照指南的速查表，写成类似：

```cpp
REG_OP(MyQuant)
    .INPUT(x, TensorType({DT_FLOAT}))
    .INPUT(weight, TensorType({DT_FLOAT}))
    .OPTIONAL_INPUT(scale, TensorType({DT_FLOAT}))
    .REQUIRED_ATTR(group_size, Int)
    .OUTPUT(y, TensorType({DT_INT8}))
    .OP_END_FACTORY_REG(MyQuant)
```

（示例代码，仅演示关键字用法。）

### 4.3 GE 图模式调用样例：test_geir_add_example.cpp 精读

#### 4.3.1 概念说明

这个 283 行的样例展示了用 GE C++ API 调用算子的最小完整闭环。它的主干可以概括为七步：

1. `GEInitialize`：初始化图引擎（设 deviceId 和 graphRunMode）。
2. 构造 `Graph` 对象。
3. 在图里放节点：`op::Data` 输入节点 + `op::AddExample` 算子节点，并连边。
4. `graph.SetInputs().SetOutputs()`：声明图的入口和出口。
5. `new Session` + `session->AddGraph()`：建会话并把图交给 GE 编译。
6. `session->RunGraph()`：喂输入 Tensor，同步执行，取回输出 Tensor。
7. 输出落盘 + `GEFinalize` 收尾。

样例里还定义了三个宏 `ADD_INPUT` / `ADD_CONST_INPUT` / `ADD_OUTPUT`，把"造 Data 节点、生成全 2 数据、连到算子输入"的重复动作压缩成一行，这是工程上的便利手段，不是 GE API 的一部分。

#### 4.3.2 核心流程

```text
main()
 ├─ GEInitialize({ge.exec.deviceId=0, ge.graphRunMode=1})
 ├─ CreateOppInGraph()
 │    ├─ op::AddExample("add1")          ← 算子节点（依赖 proto.h 的 REG_OP）
 │    ├─ ADD_INPUT(1, x1, ...)           ← op::Data 节点 + 全 2 输入数据
 │    ├─ ADD_INPUT(2, x2, ...)           ← 第二个输入
 │    ├─ ADD_OUTPUT(1, y, ...)           ← 声明输出描述
 │    └─ outputs.push_back(add1)         ← add1 作为图出口
 ├─ graph.SetInputs(inputs).SetOutputs(outputs)
 ├─ session = new Session(); session->AddGraph(0, graph, {})
 ├─ aclgrphDumpGraph(graph, "./dump")    ← 把图结构 dump 成文本（调试利器）
 ├─ session->RunGraph(0, input, output)  ← 编译+执行
 ├─ 把输入/输出 Tensor 写成 .bin 文件
 └─ delete session; GEFinalize()
```

注意与 eager 模式的一大差别：`RunGraph` 是**同步**的，返回时输出 Tensor 已经写好，不需要像 aclnn 那样手动 `aclrtSynchronizeStream`。

#### 4.3.3 源码精读

**算子节点与输入连边**。[test_geir_add_example.cpp:L159-L174](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_geir_add_example.cpp#L159-L174) 是 `CreateOppInGraph` 函数：第 164 行 `auto add1 = op::AddExample("add1");` 创建算子节点——这个工厂函数之所以存在，正是因为样例 include 了 proto 头（第 20 行 `#include "../op_graph/add_example_proto.h"`）；第 165 行定义输入 shape `{32, 4, 4, 4}`；随后两个 `ADD_INPUT` 宏分别挂上 x1、x2。

**ADD_INPUT 宏展开后的关键动作**。[L29-L47](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_geir_add_example.cpp#L29-L47) 依次做了：创建 `op::Data` 占位节点 → 构造 `TensorDesc`（shape + FORMAT_ND + dtype，并 `SetPlacement(kPlacementHost)` 表示数据先在 Host 上）→ `GenOnesData` 把每个元素填成数值 2 → `update_input_desc_x` 更新描述 → `graph.AddOp` 把节点加入图 → `add1.set_input_x1(...)` 连边。其中连边调用 `set_input_<输入名>` 的输入名正是 proto 里 `REG_OP` 声明的 `x1`/`x2`——**proto 定义与构图代码在这里对上**。

**GE 初始化**。[L183-L188](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_geir_add_example.cpp#L183-L188) 用两个全局选项初始化 GE：`ge.exec.deviceId=0` 指定设备，`ge.graphRunMode=1` 声明图运行模式。

**建会话、编译、执行**。[L215-L236](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_geir_add_example.cpp#L215-L236)：`new Session(build_options)` 创建会话；`session->AddGraph(graph_id, graph, graph_options)` 把图注册进会话（此时触发图编译，InferShape/InferDataType 在这一阶段被调用）；`session->RunGraph(graph_id, input, output)` 执行并回填输出。中间第 233 行的 `aclgrphDumpGraph(graph, "./dump", ...)` 把图结构导出为文本，是排查"图为什么没按我想的连"的第一工具。

**输出落盘与收尾**。[L256-L265](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_geir_add_example.cpp#L256-L265) 把每个输出 Tensor 按字节写成 `tc_ge_irrun_test_0008_npu_output_N.bin`；[L275-L281](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_geir_add_example.cpp#L275-L281) `delete session` 并 `GEFinalize` 释放 GE 全局资源。样例没有打印数值结果，验证要看 bin 文件（见下面实践）。

#### 4.3.4 代码实践

**实践目标**：跑通图模式样例并亲眼确认输出正确。

**操作步骤**：

1. 在已安装 add_example 算子包的环境执行：`bash build.sh --run_example add_example graph`
2. 执行成功后，到运行目录找 `tc_ge_irrun_test_0008_npu_output_0.bin`。
3. 用 python 检查输出（示例代码）：

```python
# 示例代码：把输出 bin 按 int32 解释后统计唯一值
import struct
data = open("tc_ge_irrun_test_0008_npu_output_0.bin", "rb").read()
vals = struct.unpack(f"<{len(data)//4}i", data)
print(set(vals), len(vals))   # 期望每个 4 字节字的位模式都是 2+2 的结果
```

4. 再打开 `dump/` 目录下的图文本，找到 `AddExample` 节点，确认它的两个输入分别连着两个 Data 节点。

**需要观察的现象**：输出 bin 共 32×4×4×4×4 = 8192 字节；所有元素位模式一致（两个输入都是逐元素填充值 2，加法结果应处处相同）。

**预期结果**：输出元素按 int32 位模式解释为 4（输入 2 + 2）。由于 `GenOnesData` 以 int32 视角填充数据，float 解释下会是极小的非规约数，所以用 int32 视角核对位模式最直观。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把样例中的输入 shape 从 `{32, 4, 4, 4}` 改成 `{8, 8}`，还需要改哪些地方？

**答案**：宏 `ADD_INPUT` 内部按传入的 shape 计算 `GenOnesData` 的数据量和 `TensorDesc`，所以两个 `ADD_INPUT` 和一个 `ADD_OUTPUT` 只要都传同一个新 shape 变量即可全部生效——样例里 shape 只在 `CreateOppInGraph` 的 `xShape` 一处定义（第 165 行），改这一处即可；输出 bin 大小应变为 8×8×4 = 256 字节。

**练习 2**：`RunGraph` 返回后输出数据在哪里？和 eager 模式取结果的方式有何不同？

**答案**：输出在 `RunGraph` 的第三个出参 `std::vector<ge::Tensor>& output` 里，GE 已把设备上的结果回填到这些 Host 侧 Tensor；eager 模式则需要用户自己 `aclrtMemcpy` 从 Device 内存拷回。图模式把同步与拷回都封装在 `RunGraph` 内部了。

**练习 3**：如果 `RunGraph` 失败，样例里最先值得看的两个信息是什么？

**答案**：一是 `./dump` 目录下 `aclgrphDumpGraph` 导出的图文本，确认图结构（节点、连边、dtype）是否符合预期；二是 `main` 末尾通过 `GEGetErrorMsgV2()` / `GEGetWarningMsgV2()` 取回并打印的 GE 错误/告警信息（[test_geir_add_example.cpp:L267-L272](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_geir_add_example.cpp#L267-L272)）。

## 5. 综合实践

**任务：在图中串接两个 AddExample 节点，计算 \(( (x_1 + x_2) + x_3 \)，观察两次相加的结果。**

这个任务综合了本讲全部知识点：proto 的输入名连边（4.2）、构图代码结构（4.3）、输出验证方法（4.3.4）。

**操作步骤**：

1. 复制一份样例：`cp examples/add_example/examples/test_geir_add_example.cpp examples/add_example/examples/test_geir_add3_example.cpp`（注意新文件名要去掉 `test_geir_` 前缀和 `.cpp` 后缀后作为 `--example_name`，即 `add3_example`）。
2. 修改 `CreateOppInGraph`。现有 `ADD_INPUT` 宏硬编码了 `add1.set_input_...`，所以第二个节点需要手写连边（示例代码）：

```cpp
// 示例代码：在 CreateOppInGraph 中追加第二个算子节点
auto add2 = op::AddExample("add2");           // 第二次加法
ADD_INPUT(3, x1, inDtype, xShape);            // 宏内部仍连到 add1，见下方说明
```

   由于宏的局限，更清晰的做法是不用宏，直接为 add2 手写三步：新建 `op::Data("placeholder3")` 节点并 `graph.AddOp`；`add2.set_input_x1(add1);`（把第一次加法的结果作为 add2 的第一个输入——这正是"串接"）；`add2.set_input_x2(placeholder3);`。最后把 `outputs.push_back(add1)` 改为 `outputs.push_back(add2)`，并给 add2 补 `update_output_desc_y`（可参照 `ADD_OUTPUT` 宏的写法，但注意宏同样硬编码了 `add1`，需手写 `add2.update_output_desc_y(...)`）。
3. 运行：`bash build.sh --run_example add_example graph --example_name=add3_example`
4. 用 4.3.4 的 python 片段检查 `output_0.bin`。

**预期结果**：图结构为 `Data1 + Data2 → add1 → add2 ← Data3`，输出元素 = 2 + 2 + 2 的位模式（按 int32 视角即 6）；`dump/` 图文本中应能看到两个 `AddExample` 节点，且 add2 的 x1 输入来自 add1 的输出边。具体运行输出**待本地验证**。

**思考延伸**：如果你把 add2 换成另一个算子（比如仓库里已有的某个 elementwise 算子），需要额外做什么？——答案：那个算子的 `op_graph/*_proto.h` 也必须已注册，且它的输入名要与连边调用的 `set_input_<名>` 一致。

## 6. 本讲小结

- 图模式（GEIR）与 aclnn eager 的本质差异：图模式先描述整张计算图再交给 GE 统一编译优化执行，eager 逐算子即时下发；`--run_example` 的 mode 参数分别为 `graph` 与 `eager`，graph 模式无需指定 vendor。
- `op_graph` 交付件通过 `REG_OP` 宏把算子的输入/输出/属性清单注册进 GE，是算子入图的"报关单"；`op::AddExample` 工厂函数正来自这份 proto 头。
- 图编译阶段靠 `IMPL_OP_INFERSHAPE` 注册的 InferShape / InferDataType 推导输出描述；add_example 样例把两者合写在 `op_host/add_example_infershape.cpp` 中。
- 图模式调用骨架七步：GEInitialize → 构图（Data 节点 + 算子节点连边）→ SetInputs/SetOutputs → Session/AddGraph → RunGraph（同步，结果直接回填 Host Tensor）→ 落盘 → GEFinalize。
- `aclgrphDumpGraph` 导出的图文本和 `GEGetErrorMsgV2` 是图模式排障的两件首选工具。
- 构图连边用的 `set_input_x1` 等方法名由 proto 中的输入名决定，proto 定义与调用代码一一对应。

## 7. 下一步学习建议

下一讲（u2-l3）将学习第三种调用方式：通过 `torch_extension` 工程把 ops-nn 算子封装成 PyTorch Python API，适合从 PyTorch 业务侧集成。建议在继续之前：

1. 打开 `dump/` 目录完整读一遍图文本，理解 GE 眼中的图长什么样。
2. 浏览 1~2 个生产算子（如 `activation/gelu`）的 `op_graph` 目录，对照本讲看真实算子的 proto 会多出哪些属性声明。
3. 阅读文档 [docs/zh/invocation/quick_op_invocation.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md) 的"GE图模式"一节，了解独立 CMake 工程方式编译图模式样例的做法。
