# 运行算子示例：Eager 与 Graph 两种方式

## 1. 本讲目标

学完本讲，你应该能够：

1. 使用 `bash build.sh --run_example` 一键编译并运行一个算子的示例程序，区分 eager 与 graph 两种模式。
2. 独立读懂并仿写 aclnn Eager 示例：掌握 `GetWorkspaceSize` + `Run` 两段式调用的完整骨架（初始化 → 构造输入 → 两段调用 → 同步取回 → 释放资源）。
3. 独立读懂 GE 图模式示例：掌握「创建算子节点 → 组图 → AddGraph → RunGraph」的构图执行骨架。
4. 理解两种调用方式各自依赖哪些库、走的是算子交付五层范式中的哪几层。

## 2. 前置知识

在开始之前，你需要先理解以下几个概念（前面几讲已铺垫，这里复习并补充）：

- **Eager（直调）模式**：不建计算图，Host 侧程序通过 C 语言 API（前缀为 `aclnn`）直接触发一次算子执行。它依赖算子目录中的 `op_api` 层（aclnn 接口实现）和 `op_host` 层（tiling、infershape），执行时由 aclnn 框架在后台完成 tiling 计算和 kernel 下发。
- **GE 图模式**：GE（Graph Engine，图引擎）是 CANN 的图执行框架。程序先用算子的 IR（Intermediate Representation，中间表示）原型在内存里"画"一张计算图，再把整张图交给 GE 编译、调度、执行。它依赖算子目录中的 `op_graph` 层（proto 原型声明）。
- **两段式 API**：每个 aclnn 算子接口都拆成两段。第一段 `aclnnXxxGetWorkspaceSize` 做参数校验、shape 推导、tiling 计算，并返回执行所需的 workspace（设备侧临时内存）大小；第二段 `aclnnXxx` 真正把任务下发到 stream 上。这样设计是为了把"重逻辑的准备工作"与"异步的任务下发"解耦。
- **aclTensor 与 device 内存**：NPU 上的计算数据放在 device 侧内存中，Host 侧用 `aclTensor` 结构体描述一块 device 数据的元信息（shape、dtype、strides、地址）。`CreateAclTensor` 这类辅助函数完成"Host 数据 → device 内存 → aclTensor 描述"的三步封装。
- **stream（任务流）**：NPU 任务异步执行，`aclrtSynchronizeStream` 用于阻塞等待某条 stream 上的所有任务完成，之后才能读取结果。

本讲依赖前面两讲的内容：u1-l4 讲过 build.sh 的参数体系，本讲的 `--run_example` 是其中一个入口；u2-l1~u2-l3 讲过 add_example 的目录结构、host 三件套与 kernel，本讲是它们的"运行验证"环节。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp) | Eager 模式示例：aclnn 两段式直调 AddExample 算子 |
| [examples/add_example/examples/test_geir_add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp) | Graph 模式示例：GE 构图执行 AddExample 算子 |
| [examples/add_example/op_graph/add_example_proto.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_proto.h) | 算子 IR 原型声明，graph 示例 `#include` 它来获得 `op::AddExample` 类型 |
| [build.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh) | `--run_example` 选项的解析与示例的编译执行逻辑 |
| [docs/zh/invocation/quick_op_invocation.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/invocation/quick_op_invocation.md) | 官方算子调用指导：快速调用与业务集成两种场景 |
| [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/QUICKSTART.md) | 快速上手文档，第 5 节演示 run_example |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 run_example 工具**（先讲，因为它是你最先敲的命令）、**4.2 aclnn Eager 调用**、**4.3 GE 图调用**。

### 4.1 run_example 工具：一键编译执行示例

#### 4.1.1 概念说明

`--run_example` 是 build.sh 提供的"算子样例一键执行"入口：你只需给出算子名和调用方式，脚本会自动找到该算子 `examples/` 目录下对应的示例源文件，用 g++ 现场编译成可执行文件，然后立刻运行它。它对应官方文档中"快速调用算子"场景——**无需自己搭建调用工程**，适合快速体验和验证算子功能。

#### 4.1.2 核心流程

```text
bash build.sh --run_example <op> <mode> [pkg_mode] [--soc=...] [--simulator=...]
        │
        ├─ 1. 参数解析：set_example_opt 依次摘取 算子名 / 模式 / 包模式
        ├─ 2. main 末尾分发：ENABLE_RUN_EXAMPLE=TRUE 时调用 build_example
        └─ 3. build_example：
              ├─ 按 mode 确定 pattern（eager → test_aclnn_，graph → test_geir_）
              ├─ find 在源码树中搜索 <op>/examples/[arch35/]<pattern>*.cpp
              ├─ g++ 编译该 cpp（eager 链接 libopapi_transformer 等；graph 链接 libgraph 等）
              └─ 运行生成的可执行文件（simulator 模式下改用 cannsim record 运行）
```

两种包模式的区别：

| 包模式 | 命令形态 | 链接的算子库 | 前提 |
|--------|----------|--------------|------|
| 整包模式（不传 pkg_mode） | `--run_example add_example eager` | `libopapi_transformer.so`（安装的 ops-transformer 整包） | 已安装 ops-transformer 包 |
| 自定义包模式（`cust`） | `--run_example add_example eager cust --vendor_name=custom` | `libcust_opapi.so`（`opp/vendors/custom_transformer` 下） | 已用 `--pkg --ops=add_example` 打出并安装自定义算子包 |

#### 4.1.3 源码精读

**参数解析**。`--run_example` 后面最多跟三个位置参数（算子名、模式、包模式），由 `set_example_opt` 逐个摘取，`step` 计数器配合 `shift $step` 跳过已消费的参数：

- [build.sh:1694-1699](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1694-L1699)：主循环遇到 `--run_example` 时置位 `ENABLE_RUN_EXAMPLE` 并解析后续参数。
- [build.sh:1218-1231](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1218-L1231)：`set_example_opt` 函数体——以 `-` 开头的参数（如 `--soc=...`）不会被误认成位置参数。

**分发入口**。全部选项解析完后，main 末尾判断标志位，这一段展示了 run_example 是"独占式"命令（执行完即 `exit`）：

- [build.sh:2210-2221](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2210-L2221)：`ENABLE_RUN_EXAMPLE` 为真时调用 `build_example`，并根据返回码区分"没有该模式示例"（返回 2）、"执行失败"与"成功"。

**查找与编译**。`build_example` 函数是核心：

- [build.sh:592-596](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L592-L596)：按模式确定文件名前缀 pattern——eager 找 `test_aclnn_*.cpp`，graph 找 `test_geir_*.cpp`。**这意味着：示例文件名就是约定**，想给算子加可运行的示例，必须按这个前缀命名。
- [build.sh:613-624](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L613-L624)：`--soc=ascend950` 时优先在 `examples/arch35/` 下找专属示例，找不到再回落到共享的 `examples/` 目录；其他 SoC 一律使用共享示例。这与 u1-l4 讲过的 soc→arch 映射（ascend950→arch35）一致。
- [build.sh:656-661](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L656-L661)：整包模式下的编译命令——g++ 单文件编译，头文件来自 CANN include 与 aclnnop 头文件目录，链接 `libopapi_math`、`libopapi_transformer`、`libascendcl`、`libnnopbase` 等库，产物名为 `test_aclnn_<算子名>`，落在 `build/` 目录。
- [build.sh:674-681](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L674-L681)：cust 模式改为从 `opp/vendors/<vendor>_transformer` 取头文件和 `libcust_opapi.so`，并用 `-Wl,-rpath` 保证运行时能找到自定义库。
- [build.sh:687-691](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L687-L691)：运行环节——普通环境直接执行；`--simulator=camodel --soc=ascend950` 时改用 `cannsim record -s Ascend950` 包裹执行并生成报告，这就是无 NPU 硬件时用仿真器跑示例的入口。
- [build.sh:726-727](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L726-L727)：graph 模式的编译运行——链接的是 GE 侧的 `libgraph`、`libge_runner`、`libgraph_base`、`libge_compiler`，而非 aclnn 算子库。**两种模式链接的库完全不同，直观体现了两条调用路径的分层**。

官方文档中的完整参数说明见 [docs/zh/invocation/quick_op_invocation.md:38-53](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/invocation/quick_op_invocation.md#L38-L53)（注意：mode 为 graph 时不指定 pkg_mode 和 vendor_name）；QUICKSTART 的最小可抄命令见 [docs/QUICKSTART.md:94-102](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/QUICKSTART.md#L94-L102)。

#### 4.1.4 代码实践

**实践目标**：跑通 add_example 的 eager 与 graph 两种模式，建立对 run_example 的手感。

**操作步骤**：

1. 确认环境就绪（u1-l3）：已装 CANN toolkit、已 `source set_env.sh`；有 NPU 直接跑，没有则使用 `--simulator` 路径。
2. 自定义包模式（配合你此前 `--pkg` 打出的自定义算子包）：
   ```bash
   bash build.sh --run_example add_example eager cust --vendor_name=custom
   bash build.sh --run_example add_example graph
   ```
3. 整包模式（已安装 ops-transformer 整包时）：
   ```bash
   bash build.sh --run_example add_example eager
   ```
4. 仿真模式（Ascend 950PR，无实体卡）：
   ```bash
   bash build.sh --run_example add_example eager --simulator=camodel --soc=ascend950
   ```

**需要观察的现象**：

- 编译阶段日志会打印 `Start compile and run example file: .../test_aclnn_add_example.cpp`，随后是 g++ 命令本身——注意它链接了哪些库。
- eager 运行结束打印 2048 行逐元素结果（1 + 1 = 2.000000），最后有 `execute samples success`。
- graph 模式会打印一串 `[XIR]` 日志，最终出现 `Finalize ir graph session success`；当前目录下还会生成 `./dump` 图文本、以及 `tc_ge_irrun_test_0008_npu_input_*.bin` / `output_*.bin` 数据文件。

**预期结果**：两种模式均以退出码 0 结束。若返回码为 2，脚本会提示 `do not have eager/graph example`——可以用一个没有 graph 示例的算子（如去 `examples/` 下只有 `test_aclnn_` 文件的算子）故意触发一次，观察报错。本实践涉及真机/仿真执行，具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 graph 模式不需要传 `cust` 和 `--vendor_name`？

**答案**：graph 模式由 GE 图引擎根据环境变量（`ASCEND_OPP_PATH` 等）自动加载已安装的算子包（无论自定义包还是内置包），编译时只链接 GE 框架库（libgraph、libge_runner 等），不直接链接某个 vendor 的 aclnn 动态库，因此无需区分包来源。这一点在 [docs/zh/invocation/quick_op_invocation.md:470-472](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/invocation/quick_op_invocation.md#L470-L472) 中有明确说明。

**练习 2**：如果你想给某算子新增一个只属于 ascend950 的 eager 示例，文件应放在哪里、叫什么名字？

**答案**：放在 `<算子目录>/examples/arch35/` 下，文件名以 `test_aclnn_` 开头（如 `test_aclnn_my_op_v2.cpp`）。因为 [build.sh:613-619](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L613-L619) 中 `--soc=ascend950` 时会先在 `arch35` 子目录里找 `test_aclnn_*.cpp`。

---

### 4.2 aclnn Eager 调用：两段式 API 骨架

#### 4.2.1 概念说明

Eager 示例展示了"不用任何框架、纯 C++ 直调一个 NPU 算子"的完整写法。它是所有 aclnn 算子调用的模板：把 `aclnnAddExample` 换成 `aclnnFlashAttentionScore` 等，骨架完全不变，变的只是输入构造和参数个数。理解这份 160 行的文件，你就具备了调用本仓库任何 eager 算子的能力。

两段式的设计动机：第一段 `GetWorkspaceSize` 在 Host 侧完成所有"重活"（校验、infershape、tiling 计算，正是 u2-l2 讲过的 host 三件套被触发的时刻），并告诉你需要多大 workspace；第二段 `Run` 只做异步下发。调用方可以在两段之间自主管理 workspace 内存（复用、池化），也可在同一条 stream 上连续下发多个算子形成流水。

#### 4.2.2 核心流程

```text
main()
 ├─ ① 初始化：aclInit → aclrtSetDevice → aclrtCreateStream
 ├─ ② 构造输入/输出：Host 数据 --aclrtMemcpy--> device 内存 --aclCreateTensor--> aclTensor
 ├─ ③ 第一段：aclnnAddExampleGetWorkspaceSize(x, y, out, &workspaceSize, &executor)
 ├─    （按 workspaceSize 用 aclrtMalloc 申请 workspace）
 ├─ ④ 第二段：aclnnAddExample(workspaceAddr, workspaceSize, executor, stream)
 ├─ ⑤ 同步：aclrtSynchronizeStream(stream)
 ├─ ⑥ 取回结果：aclrtMemcpy(device → host) 并打印
 └─ ⑦ 清理：aclDestroyTensor / aclrtFree / aclrtDestroyStream / aclrtResetDevice / aclFinalize
```

其中 ③④ 是两段式本体，①②⑤⑥⑦ 是任何 aclnn 调用都适用的"固定写法"（源码注释里也正是这么标的）。

#### 4.2.3 源码精读

**初始化与张量构造（固定写法）**：

- [test_aclnn_add_example.cpp:51-61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L51-L61)：`Init` 函数三步——`aclInit` 初始化 ACL 上下文、`aclrtSetDevice` 绑定设备号、`aclrtCreateStream` 创建任务流。
- [test_aclnn_add_example.cpp:63-85](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L63-L85)：`CreateAclTensor` 模板函数——先 `aclrtMalloc` 申请 device 内存（优先大页 `ACL_MEM_MALLOC_HUGE_FIRST`），再 `aclrtMemcpy` 把 Host 数据搬上去，然后手工计算连续 tensor 的 strides，最后 `aclCreateTensor` 生成 aclTensor 描述。注意 strides 的计算方式：从倒数第二维往前逐维累乘，这是"连续内存布局"的标准求法。

**构造输入输出**：

- [test_aclnn_add_example.cpp:95-115](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L95-L115)：三个 shape 均为 `{32, 4, 4, 4}`（共 2048 个元素）的 fp32 张量——两个输入初始化为全 1，输出张量的初始值会被计算结果覆盖。**这一段是改写算子调用时的主要修改点**：换 shape、换 dtype、换初始数据都在这里。

**两段式调用（本模块核心）**：

- [test_aclnn_add_example.cpp:118-130](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L118-L130)：先声明 `workspaceSize` 与 `executor`（`aclOpExecutor*`，由第一段产出、第二段消费的执行器句柄）；调用 `aclnnAddExampleGetWorkspaceSize(selfX, selfY, out, &workspaceSize, &executor)`；仅当 `workspaceSize > 0` 时申请 workspace——有的算子不需要临时空间，这里的判空避免了无谓分配。
- [test_aclnn_add_example.cpp:132-138](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L132-L138)：第二段 `aclnnAddExample(workspaceAddr, workspaceSize, executor, stream)` 把任务异步发到 stream 上，随后 `aclrtSynchronizeStream` 阻塞等待完成。**在下发和同步之间插入其他 Host 工作是实现异步重叠的地方**。

**取回与清理**：

- [test_aclnn_add_example.cpp:37-49](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L37-L49)：`PrintOutResult` 用 `ACL_MEMCPY_DEVICE_TO_HOST` 方向的 `aclrtMemcpy` 把结果搬回 Host 再逐元素打印。
- [test_aclnn_add_example.cpp:143-159](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L143-L159)：按申请的逆序释放——销毁三个 aclTensor、释放四块 device 内存（含 workspace）、销毁 stream、ResetDevice、`aclFinalize`。官方文档 [docs/zh/invocation/quick_op_invocation.md:170-252](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/invocation/quick_op_invocation.md#L170-L252) 还给出了用 `std::unique_ptr` 自动管理这些资源的改良版写法，推荐实际工程采用。

**一个重要的"陷阱"提示**：add_example 的 def 文件中输入输出 dtype 只注册了 `DT_FLOAT` 和 `DT_INT32`（见 [add_example_def.cpp:30](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L30)）。示例源码中也留有注释"当前样例算子未进行 shape、dtype 全泛化"。如果你构造 fp16 输入，会在 `GetWorkspaceSize` 阶段被参数校验拦下——这正是 u2-l2 说的"def 是框架校验第一道关卡"的直接体现。

#### 4.2.4 代码实践

**实践目标**：仿照示例写一个最小程序，把输入换成自己构造的 1×8 向量并打印输出；顺带亲手验证 dtype 校验关卡。

**操作步骤**：

1. 复制示例：`cp examples/add_example/examples/test_aclnn_add_example.cpp /tmp/test_my_add.cpp`（改自己的副本，不动源码树）。
2. 修改 `/tmp/test_my_add.cpp` 中 main 的三处 shape 与数据（示例代码，非项目原有代码）：
   ```cpp
   std::vector<int64_t> selfXShape = {1, 8};
   std::vector<float> selfXHostData = {1, 2, 3, 4, 5, 6, 7, 8};
   // selfY / out 同理：shape 改 {1, 8}，selfY 数据可给 {8, 7, 6, 5, 4, 3, 2, 1}
   ```
3. 先做一个"预期失败的实验"：把 `CreateAclTensor` 的 `aclDataType::ACL_FLOAT` 换成 `ACL_FLOAT16`、Host 数据换成 `std::vector<__fp16>`，重新编译运行，观察第一段接口返回的错误码。
4. 改回 fp32 后编译运行。可以手写一条 g++ 命令（参考 [build.sh:656-661](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L656-L661) 的链接参数，头文件目录用 `$ASCEND_HOME_PATH/include` 与 `$ASCEND_HOME_PATH/include/aclnnop`，链接 `-lopapi_transformer -lascendcl -lnnopbase`），或按 [docs/zh/invocation/quick_op_invocation.md:254-367](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/invocation/quick_op_invocation.md#L254-L367) 的 CMakeLists 搭一个最小工程。

**需要观察的现象**：

- fp16 实验：`aclnnAddExampleGetWorkspaceSize failed. ERROR: <非0码>`，程序在第一段就返回——不会到第二段。
- fp32 正常路径：打印 8 行结果，依次为 `9.000000, 9.000000, ... 9.000000`（两个输入逐元素相加）。

**预期结果**：fp16 被拒绝的原因是 def 中 DataType 白名单没有 `DT_FLOAT16`；fp32 输出 8 个 9。运行结果**待本地验证**（尤其注意：add_example 的 tiling 参数是为教学写死的，超小 shape 是否在所有分支下都正确执行也值得在观察时留意）。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `aclrtSynchronizeStream` 直接 `PrintOutResult`，会发生什么？

**答案**：第二段 API 只是异步下发，NPU 可能尚未算完，此时 `aclrtMemcpy` 读到的 device 内存是旧值（输出 tensor 构造时的初始数据 1.0），得到错误结果；甚至可能读到未定义数据。同步是"异步下发模型"下读取结果前必须的一步。

**练习 2**：`executor` 这个变量为什么在两段之间不需要调用方做任何管理？

**答案**：`aclOpExecutor` 是 aclnn 框架内部的对象，由第一段接口创建并填充（内部持有 tiling 结果、kernel 选择等执行计划），调用方只透传给第二段。它的生命周期由框架管理，调用方仅需把它当作不透明句柄。

**练习 3**：为什么 workspace 的申请放在两段之间而不是程序开头一次性分配？

**答案**：workspace 大小只有第一段算完 tiling 之后才知道——它取决于输入 shape、dtype 和 tiling 策略，无法预先确定。两段式正是为了让"计算需要多少临时空间"与"使用临时空间执行"分离，调用方还能在多次调用间复用同一块 workspace。

---

### 4.3 GE 图调用：构图与执行骨架

#### 4.3.1 概念说明

图模式把算子调用从"一次函数调用"升级为"一张计算图"：程序用算子 IR 原型（`op_graph` 层提供的 `REG_OP` 声明）在 Host 内存中组装出 DAG（有向无环图），交给 GE 的 Session 编译执行。单算子图看起来"多此一举"，但当多个算子组成一张图时，GE 可以做整图调度、内存复用与图融合（fusion pass，u6-l2 会展开），这是训练/推理框架（如 PyTorch 图模式、ONNX 下发）使用 NPU 的实际路径。

Eager 与 Graph 的关键差异对照：

| 维度 | Eager（aclnn） | Graph（GE） |
|------|----------------|-------------|
| 依赖的算子交付层 | op_api + op_host | op_graph（proto）+ op_host |
| 头文件 | `aclnnop/aclnn_add_example.h` | `graph.h`、`ge_api.h` + 算子自身 `*_proto.h` |
| 链接库 | `libopapi_transformer`、`libascendcl` | `libgraph`、`libge_runner`、`libge_compiler` |
| tiling 发生时机 | GetWorkspaceSize 阶段（调用方可见） | GE 整图编译阶段（框架内部） |
| 典型用户 | 自研 C++ 业务、快速验证 | 接入图引擎的上层框架 |

#### 4.3.2 核心流程

```text
main()
 ├─ ① Graph graph("name") 创建图对象
 ├─ ② ge::GEInitialize(global_options) 初始化图引擎
 ├─ ③ CreateOppInGraph：
 │     op::AddExample add1("add1")            -- 用 IR 原型实例化算子节点
 │     ADD_INPUT(1, x1, ...) / ADD_INPUT(2, x2, ...)   -- Data 节点作输入 placeholder，绑定 Host 数据
 │     ADD_OUTPUT(1, y, ...)                  -- 声明输出 desc
 │     add1.set_input_x1/set_input_x2(...)    -- 把 placeholder 连到算子输入边
 ├─ ④ graph.SetInputs(inputs).SetOutputs(outputs) 圈定图边界
 ├─ ⑤ new ge::Session(...) + session->AddGraph(graph_id, graph, ...) 添加图
 ├─ ⑥ session->RunGraph(graph_id, input, output) 编译并执行整图
 ├─ ⑦ 从 output Tensor 取结果（示例写入 .bin 文件）
 └─ ⑧ delete session; ge::GEFinalize()
```

#### 4.3.3 源码精读

**IR 原型是构图的前提**。graph 示例第 29 行 `#include "../op_graph/add_example_proto.h"` 引入算子原型，其中用 `REG_OP` 宏声明了算子的图接口：

- [add_example_proto.h:35-39](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_proto.h#L35-L39)：`REG_OP(AddExample)` 声明两个输入 `x1`、`x2` 和一个输出 `y`。有了它，C++ 里才能写 `op::AddExample`、`add1.set_input_x1(...)`——这些成员函数由宏根据 INPUT/OUTPUT 声明自动生成。

**构图环节**：

- [test_geir_add_example.cpp:38-56](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp#L38-L56)：`ADD_INPUT` 宏的展开体——为每个输入创建一个 `op::Data` placeholder 节点、生成全量数据（`GenOnesData`）、设置 TensorDesc（shape/format/dtype/host placement）、加入图、并通过 `add1.set_input_##intputName(...)` 连边。宏用 `##` 拼接成员名（`set_input_x1`），这是让"声明式构图"代码紧凑的常用手法。
- [test_geir_add_example.cpp:168-183](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp#L168-L183)：`CreateOppInGraph`——`op::AddExample("add1")` 实例化算子节点，shape 仍为 `{32,4,4,4}`，两个 `ADD_INPUT` + 一个 `ADD_OUTPUT`，最后把 `add1` 放进 outputs。**给其他算子写 graph 示例时，基本只需改这个函数**。
- [test_geir_add_example.cpp:216-218](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp#L216-L218)：`graph.SetInputs(inputs).SetOutputs(outputs)` 声明图的输入输出边界——GE 只从这些边界节点进数据、出结果。

**执行环节**：

- [test_geir_add_example.cpp:191-198](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp#L191-L198)：`GEInitialize` 传入全局选项（deviceId、graphRunMode）。
- [test_geir_add_example.cpp:224-242](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp#L224-L242)：创建 `ge::Session`、`AddGraph` 把图注册进会话，`aclgrphDumpGraph` 把图 dump 成文本（调试构图问题的第一工具——看 GE 眼里的图长什么样）。
- [test_geir_add_example.cpp:244-251](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp#L244-L251)：`session->RunGraph(graph_id, input, output)` 一步完成整图编译与执行——tiling、内存规划、kernel 下发都在这一步内部由 GE 完成，调用方不再可见（对比 eager 的两段式，这正是抽象层级提升的代价与收益）。
- [test_geir_add_example.cpp:265-274](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp#L265-L274)：从 `output` 向量取结果 Tensor，示例选择写入 `.bin` 文件（可直接用 `gen_data.py/compare_data.py` 风格的脚本比对）。
- [test_geir_add_example.cpp:283-290](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp#L283-L290)：`GEFinalize` 收尾，与 `GEInitialize` 配对。

#### 4.3.4 代码实践

**实践目标**：通过"读 + 改"两个动作理解 graph 示例的骨架，重点体会"改一个算子的 graph 示例只需改 CreateOppInGraph"。

**操作步骤**：

1. 运行 `bash build.sh --run_example add_example graph`（见 4.1.4），确认生成 `./dump` 与 `tc_ge_irrun_test_0008_npu_output_0.bin`。
2. 打开 dump 出的图文本文件，找到 `AddExample` 节点，确认它有两个输入边、一个输出边，与 proto 声明一致。
3. 用 `python3` 或 `od -f` 查看 `tc_ge_irrun_test_0008_npu_output_0.bin` 的前几个 float（2048 个元素，值应为 2.0——两个全 1 输入相加）。
4. （源码阅读型实践）假设要把示例改成两个 `op::AddExample` 节点串联（add1 的输出 y 作为 add2 的 x1）：指出需要修改 `CreateOppInGraph` 中的哪些行——新增一次 `op::AddExample("add2")` 实例化，为 add2 再做一次 `ADD_INPUT`（或直接用 add1 的输出节点连边），并把 `ADD_OUTPUT` 挂到 add2 的 y 上。

**需要观察的现象**：dump 文本中算子节点、Data placeholder 节点及其连边关系；output bin 文件大小应为 2048 × 4 字节。

**预期结果**：图中能定位到 `AddExample`；output 数据全为 2.0。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：graph 示例中没有出现 `aclnnAddExampleGetWorkspaceSize`，tiling 是什么时候算的？

**答案**：在 `session->RunGraph` 内部的整图编译阶段，由 GE 调用 op_host 层注册的 tiling 函数完成（u2-l2 讲过的 `IMPL_TILING` 注册就是给这两种路径共用的）。调用方完全不可见，这是图模式与 eager 模式在"tiling 可见性"上的本质区别。

**练习 2**：`op::Data` 节点（placeholder）和 `op::Const` 节点（`ADD_CONST_INPUT` 宏创建的）有什么区别？

**答案**：`op::Data` 是图的输入边界节点，数据在 `RunGraph` 时由 `input` 向量动态喂入，同一张图可以换不同数据反复执行；`op::Const` 把数据以属性形式固化进图（见 [test_geir_add_example.cpp:58-77](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp#L58-L77) 中 `SetAttr("value", ...)`），图编译后不可变。前者适合变化的输入，后者适合权重类常量。

**练习 3**：为什么 eager 示例链接 `libopapi_transformer.so` 而 graph 示例不链接任何算子库？

**答案**：eager 调用直接绑定算子的 aclnn 符号（`aclnnAddExample` 等），必须在编译期链接对应动态库；graph 示例只使用 GE 框架 API（libgraph/libge_runner），算子原型和 kernel 是 GE 在运行时根据环境变量从已安装的算子包（opp 目录）中动态加载的，所以编译期不需要链接算子库。

## 5. 综合实践

**任务：给 add_example 写一个"双算子接力"的最小调用程序，并分别在两种模式下理解同一算子的不同触发路径。**

1. **Eager 路径**（动手编码）：基于 4.2.4 的 `/tmp/test_my_add.cpp` 副本，扩展为两次调用接力——第一段的输出 tensor 作为第二段调用的输入 x1，输入改为两个 1×8 fp32 向量，最终打印两步结果（例如 `(a+b)+(a+b)` 即 2(a+b)）。需要为第二个输出新申请 device 内存与 aclTensor，注意在结尾补上释放。
2. **Graph 路径**（源码阅读 + 设计）：按 4.3.4 步骤 4 的方案，在纸上或编辑器里写出两个 `AddExample` 串联的 `CreateOppInGraph` 改法，不必真编译（也可复制到 /tmp 尝试）。
3. **对照总结**：写一份简短对照笔记，回答：两种模式下 tiling 分别在何处发生？分别依赖算子五层范式中的哪几层？分别链接什么库？什么场景该选哪种？
4. **验证**：在有 NPU 或 simulator 的环境下运行两种模式的原始示例确认环境可用；你的改写程序输出若为 `2(a+b)` 逐元素值则正确（**待本地验证**）。

## 6. 本讲小结

- `bash build.sh --run_example <op> <mode> [pkg_mode]` 是"免搭工程"的算子样例执行入口：脚本按 `test_aclnn_`/`test_geir_` 前缀约定找到示例源文件，g++ 现场编译并运行，ascend950 优先使用 `examples/arch35/` 下的专属示例。
- Eager 模式的骨架是"初始化 → 构造 aclTensor → `GetWorkspaceSize`（校验+infershape+tiling+算 workspace）→ 申请 workspace → `Run`（异步下发）→ 同步 → 拷回结果 → 释放"，其中两段式把准备工作与任务下发解耦。
- Graph 模式的骨架是"IR 原型实例化算子节点 → placeholder 连边组图 → `Session::AddGraph` → `RunGraph` 整图编译执行"，tiling 与 kernel 选择被封装进 GE 内部，算子库在运行时动态加载。
- 两种模式链接的库、依赖的交付层（op_api/op_host vs op_graph）完全不同，示例文件名前缀是脚本识别的唯一约定。
- add_example 的 dtype 白名单只有 fp32/int32，构造不支持的输入会在第一段 API 被参数校验拦截——def 文件是算子能力的事实边界。

## 7. 下一步学习建议

本讲之后你已经"会跑、会调"一个算子。下一讲 u2-l5 将看 add_example 的第三种实现形态——AICPU 算子，理解 AICore 与 AICPU 的选型差异。随后进入第三单元：u3-l1 会系统展开两阶段 API 背后的 workspace 机制、返回码体系与非连续 tensor 处理（本讲只是预演）；u3-l3 讲 torch_extension 如何把 aclnn 包装成 PyTorch API。建议持续阅读的源码：任选一个业务算子（如 `attention/flash_attention_score`）的 `examples/` 目录，对照本讲骨架找出"变的部分"（参数构造）与"不变的部分"（两段式流程），检验自己的掌握程度。
