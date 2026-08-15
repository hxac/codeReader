# 第一次运行算子：AddExample 全流程实操

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立完成 CANN 环境变量配置（`source set_env.sh`、`LD_LIBRARY_PATH`）与自定义算子包的安装。
2. 按照 `docs/QUICKSTART.md` 的流程，完成 AddExample 算子的「编译 → 安装 → 运行」闭环。
3. 读懂 `test_aclnn_add_example.cpp`（aclnn 两段式调用）和 `test_geir_add_example.cpp`（GE 图模式调用）两个样例的执行链路。
4. 知道运行失败时（如 `error 161001`）该回到哪个环节排查。

本讲是纯实操讲：上一讲（u1-l3）我们读懂了 build.sh 和 CMake 编译体系的源码，这一讲我们把这套体系真正跑起来，跑的对象就是仓库自带的最小算子 `examples/add_example`。

## 2. 前置知识

- **算子包（run 包）**：上一讲我们了解到，`--ops` 参数会走「自定义算子包」路线，编译产物是 `build_out` 目录下一个自解压的 `.run` 文件。它里面装的是算子的二进制、算子信息（json）和 aclnn 接口库。
- **安装路径 `opp/vendors`**：run 包安装后，算子会被放入 `${ASCEND_HOME_PATH}/opp/vendors/` 目录。`ASCEND_HOME_PATH` 是 CANN 软件的安装目录，默认为 `/usr/local/Ascend/cann`。运行样例前，必须把 vendors 下算子包的 `op_api/lib` 加入 `LD_LIBRARY_PATH`，否则程序找不到 aclnn 接口的动态库。
- **两段式接口（aclnn）**：每个 aclnn 算子接口拆成两段——`aclnnXxxGetWorkspaceSize` 负责校验参数、推导执行计划并告知需要多大的 workspace（中转内存）；`aclnnXxx` 负责真正把任务下发到 NPU 上执行。本讲先从「使用者」角度体验它，下一单元（u2-l1）会专门精讲。
- **图模式（geir）**：除了逐个调用单算子接口，还可以用 GE（Graph Engine）把算子拼成一张计算图，整图编译后一次执行。AddExample 同时提供了这两种样例。
- **soc_version**：芯片版本字符串。Atlas A2 系列对应 `ascend910b`，Atlas A3 系列对应 `ascend910_93`，950 系列对应 `ascend950`。**编译时用什么 `--soc`，运行时也必须传同一个值**。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md) | 官方快速入门指南，本讲实操流程的权威依据，覆盖编译运行、算子开发、算子调试、算子验证四个阶段 |
| [examples/add_example/README.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md) | AddExample 算子说明书：支持产品、计算公式 \( y = x1 + x2 \)、参数表、两种调用方式入口 |
| [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp) | aclnn 两段式调用的可执行样例，本讲精读的主对象 |
| [examples/add_example/examples/test_geir_add_example.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp) | GE 图模式调用样例：构图、编译、执行、结果落盘 |
| [examples/add_example/op_graph/add_example_proto.h](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_graph/add_example_proto.h) | AddExample 的算子原型（IR）定义，图模式识别算子的依据 |
| [scripts/build_options.sh](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh) | build.sh 的参数解析脚本，`--run_example` 子命令在这里定义 |

## 4. 核心概念与源码讲解

### 4.1 QUICKSTART 的「编译 → 安装 → 运行」闭环

#### 4.1.1 概念说明

`docs/QUICKSTART.md` 是官方设计的最短上手路径，它把算子开发拆成四个阶段：**编译运行 → 算子开发 → 算子调试 → 算子验证**。本讲聚焦第一阶段，即先不改任何代码，把仓库里的 AddExample 原样跑通，验证「环境可用、流程正确」。后三个阶段（改 Kernel、加打印、改输入）会在后续讲义（u4-l1、u7-l3）展开。

#### 4.1.2 核心流程

QUICKSTART 第一阶段的五步流程：

```text
① 进入配套分支源码目录（源码版本必须与 CANN 版本配套）
② 编译算子包：bash build.sh --pkg --soc=<soc_version> --ops=add_example -j16
   └─ 产物：build_out/cann-ops-cv-*linux*.run
③ 安装算子包：./build_out/cann-ops-cv-*linux*.run
   └─ 安装到 ${ASCEND_HOME_PATH}/opp/vendors/
④ 配置环境变量：export LD_LIBRARY_PATH=.../opp/vendors/custom_cv/op_api/lib:$LD_LIBRARY_PATH
⑤ 运行样例：bash build.sh --run_example add_example eager cust --vendor_name=custom --soc=<soc_version>
   └─ 成功标志：打印 2048 个 1+1=2 的结果
```

#### 4.1.3 源码精读

QUICKSTART 明确给出了编译命令和 soc 取值对照：

- [docs/QUICKSTART.md:44-60](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L44-L60)：编译通用命令 `bash build.sh --pkg --soc=${soc_version} --ops=<算子名>`，并列出 Atlas A2 → `ascend910b`、A3 → `ascend910_93`、950 系列 → `ascend950` 的取值对照；编译成功的标志是 `Self-extractable archive "cann-ops-cv-custom_linux-${arch}.run" successfully created.`，run 包存放在项目根目录 `build_out` 下。
- [docs/QUICKSTART.md:70-84](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L70-L84)：安装命令 `./build_out/cann-ops-cv-*linux*.run`，算子被安装到 `${ASCEND_HOME_PATH}/opp/vendors`；随后必须 `export LD_LIBRARY_PATH=${ASCEND_HOME_PATH}/opp/vendors/custom_cv/op_api/lib:${LD_LIBRARY_PATH}`，让运行时能链接到算子包里的 aclnn 动态库。
- [docs/QUICKSTART.md:86-95](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md#L86-L95)：运行命令格式为 `bash build.sh --run_example <算子名> <运行模式> <包模式>`。文档特别提醒：**运行样例时的 `--soc` 必须与编译算子包时一致，否则报 `error 161001`（如 `aclnnXxxGetWorkspaceSize failed`）**——这是新手最常见的报错。

`--run_example` 的参数含义可以在 build.sh 的参数解析脚本里找到权威定义：

- [scripts/build_options.sh:255-267](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L255-L267)：`--run_example op_type mode[eager:graph] [pkg_mode --vendor_name=name]`，即第二个参数二选一——`eager` 跑 `test_aclnn_xxx.cpp`（单算子直调），`graph` 跑 `test_geir_xxx.cpp`（图模式）；第三个参数 `cust` 表示使用自定义算子包（对应我们 `--ops` 编译出的产物），并可用 `--vendor_name` 指定厂商名。

#### 4.1.4 代码实践

1. **实践目标**：完成 AddExample 的编译、安装与运行，拿到第一份算子执行日志。
2. **操作步骤**（需要一台装有配套 CANN 包的 Atlas 环境，CANNLab 或 Docker 均可）：
   ```bash
   # 0. 配置 CANN 环境变量（默认安装路径）
   source /usr/local/Ascend/cann/set_env.sh

   # 1. 编译 AddExample 算子包（soc 按实际芯片替换）
   bash build.sh --pkg --soc=ascend910b --ops=add_example -j16

   # 2. 安装
   ./build_out/cann-ops-cv-*linux*.run

   # 3. 配置运行时库路径
   export LD_LIBRARY_PATH=${ASCEND_HOME_PATH}/opp/vendors/custom_cv/op_api/lib:${LD_LIBRARY_PATH}

   # 4. 运行 aclnn 样例
   bash build.sh --run_example add_example eager cust --vendor_name=custom --soc=ascend910b
   ```
3. **需要观察的现象**：终端逐行打印 `add_example first input[i] is: 1.000000, second input[i] is: 1.000000, result[i] is: 2.000000`，共 2048 行（shape 为 {32,4,4,4}）。
4. **预期结果**：所有 `result[i]` 均为 `2.000000`，说明算子包部署成功且计算正确。注意记录日志中出现的算子名（AddExample）与输入输出 shape（{32,4,4,4}）。
5. 本讲所有运行结果均为「待本地验证」——是否成功取决于你的环境，请以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：运行时把 `--soc` 故意换成与编译时不同的值（例如编译用 `ascend910b`、运行传 `ascend950`），会出现什么现象？为什么？

**答案**：会报 `error 161001`，典型表现为 `aclnnAddExampleGetWorkspaceSize failed`。因为 vendors 目录下的算子信息是按芯片型号注册的，运行时按另一个 soc 查找算子二进制会查不到，第一段接口（GetWorkspaceSize）直接失败。处理办法是回到编译步骤，用一致的 `--soc` 重新编译安装。

**练习 2**：如果忘记第 3 步的 `export LD_LIBRARY_PATH=...`，直接运行样例，预计会出什么错？

**答案**：程序在启动或调用 aclnn 接口时会因为找不到自定义算子包 `op_api/lib` 下的动态库而报链接/加载错误（典型如 `cannot open shared object file`）。`set_env.sh` 只配置了 CANN 自身库的路径，vendors 下自定义算子包的库路径需要额外导出。

### 4.2 AddExample 算子本体：说明书与原型

#### 4.2.1 概念说明

跑通流程之前先认识主角。AddExample 是仓库自带的教学算子，功能就是逐元素加法 \( y = x1 + x2 \)。它的价值在于交付件精简、语义极简，是验证环境和流程的最佳「试纸」。它的 README 定义了接口契约，`op_graph/add_example_proto.h` 定义了图模式下的算子原型（IR）。

#### 4.2.2 核心流程

一个算子被成功调用，背后有两条信息必须对得上：

```text
README/参数表（人读的契约） ──┐
                              ├──> 算子注册信息（vendors 下的 json）──> 运行时按「算子名 + 输入dtype/shape」匹配实现
proto.h 原型（图引擎读的契约）─┘
```

样例代码构造的每个 aclTensor 的 dtype 和 shape，都必须落在注册信息允许的范围内，否则第一段接口校验失败。

#### 4.2.3 源码精读

- [examples/add_example/README.md:11-19](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md#L11-L19)：功能说明——完成加法计算，公式 \( y = x1 + x2 \)。
- [examples/add_example/README.md:39-59](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md#L39-L59)：参数表——x1、x2 为输入，y 为输出，均支持 FLOAT/INT32、ND 格式。样例里构造 float 张量正是对应这里的 FLOAT 类型。
- [examples/add_example/README.md:66-85](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md#L66-L85)：调用说明——aclnn 调用对应 `test_aclnn_add_example`，图模式调用对应 `test_geir_add_example`，这是本讲 4.3、4.4 两节精读的两个入口。
- [examples/add_example/op_graph/add_example_proto.h:35-39](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_graph/add_example_proto.h#L35-L39)：`REG_OP(AddExample)` 声明了两个输入 x1、x2 和一个输出 y，类型为 `TensorType({DT_FLOAT, DT_INT32})`。图引擎（GE）靠这份原型把样例里 `op::AddExample("add1")` 创建的节点识别成已注册算子。

#### 4.2.4 代码实践

1. **实践目标**：建立「README 参数表 ↔ 样例构造代码 ↔ proto 原型」三者的对照能力。
2. **操作步骤**：打开 README 参数表，再到 `test_aclnn_add_example.cpp` 的第 96–115 行（构造三个张量的代码），逐项核对：张量名（selfX/selfY/out 对应 x1/x2/y）、dtype（`ACL_FLOAT` 对应 FLOAT）、shape（{32,4,4,4}，ND 格式）。最后打开 proto.h 确认 `DT_FLOAT` 在允许列表中。
3. **需要观察的现象**：三者完全一致——这不是巧合，而是算子调用的硬性前提。
4. **预期结果**：能口头回答「如果样例把 dtype 换成 ACL_DOUBLE 会发生什么」（答案：不在注册范围内，第一段接口校验失败）。本实践为源码阅读型，无需运行环境。

#### 4.2.5 小练习与答案

**练习 1**：AddExample 支持 INT32 输入。若要跑一组 INT32 数据，除了改样例中 host 数据的 `std::vector<float>` 为 `std::vector<int32_t>` 外，还要改哪一处？

**答案**：把 `CreateAclTensor` 调用中的 `aclDataType::ACL_FLOAT` 改为 `aclDataType::ACL_INT32`，dtype 描述必须与实际数据一致，且需落在注册信息允许的 INT32 类型上。

**练习 2**：proto.h 中 `REG_OP` 注册的算子名是什么？谁在消费这份注册？

**答案**：注册的算子名是 `AddExample`（REG_OP 宏的参数，同时也是工厂注册名）。消费方是图引擎 GE：图模式样例中 `op::AddExample("add1")` 创建节点后，GE 在算子原型注册表中按名字查到输入输出规格，才能完成构图与整图编译。

### 4.3 aclnn 样例精读：test_aclnn_add_example.cpp

#### 4.3.1 概念说明

这个 162 行的文件是所有 ops-cv 算子 aclnn 样例的模板：仓库里每个算子 `examples/` 下的 `test_aclnn_*.cpp` 都遵循同样的九步骨架。读懂它，以后拿到任何算子都能照着跑。它同时是「两段式接口」的第一次真实亮相。

#### 4.3.2 核心流程

main 函数的九步骨架：

```text
① Init：aclInit → aclrtSetDevice → aclrtCreateStream（运行环境初始化，固定写法）
② CreateAclTensor ×3：host 数据 → aclrtMalloc → aclrtMemcpy(H2D) → aclCreateTensor
③ 声明 workspaceSize 与 aclOpExecutor
④ 第一段：aclnnAddExampleGetWorkspaceSize(selfX, selfY, out, &workspaceSize, &executor)
⑤ 按 workspaceSize 申请 workspace 内存
⑥ 第二段：aclnnAddExample(workspaceAddr, workspaceSize, executor, stream)（异步下发）
⑦ aclrtSynchronizeStream 等待 NPU 执行完成
⑧ PrintOutResult：aclrtMemcpy(D2H) 把结果拷回 host 并打印
⑨ 释放 tensor / 内存 / stream，aclFinalize
```

#### 4.3.3 源码精读

- [examples/add_example/examples/test_aclnn_add_example.cpp:51-61](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L51-L61)：`Init` 函数是固定写法——`aclInit` 初始化 ACL 上下文、`aclrtSetDevice` 指定设备 0、`aclrtCreateStream` 创建任务流。任何 aclnn 样例都从这里开始。
- [examples/add_example/examples/test_aclnn_add_example.cpp:63-85](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L63-L85)：`CreateAclTensor` 模板函数演示了构造 aclTensor 的三步：`aclrtMalloc` 在 device 侧申请内存 → `aclrtMemcpy` 把 host 数据搬上去 → 按连续张量规则算出 strides 后调用 `aclCreateTensor`。strides 的计算从倒数第一维向前累乘（第 76–79 行），这是 ND 连续张量的标准求法。
- [examples/add_example/examples/test_aclnn_add_example.cpp:96-115](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L96-L115)：main 中构造三个 shape 均为 `{32, 4, 4, 4}`、2048 个元素全为 1 的 float 张量：selfX、selfY 是输入，out 是输出（预分配）。
- [examples/add_example/examples/test_aclnn_add_example.cpp:121-134](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L121-L134)：两段式调用的现场——第 122 行 `aclnnAddExampleGetWorkspaceSize(selfX, selfY, out, &workspaceSize, &executor)` 完成参数校验并生成 `aclOpExecutor`（执行计划）；第 127–130 行按返回的 workspaceSize 申请中转内存（为 0 时跳过）；第 133 行 `aclnnAddExample(workspaceAddr, workspaceSize, executor, stream)` 把任务异步下发到 stream。注意第 123 行的错误打印正是 4.1 节提到的 `error 161001` 出现位置。
- [examples/add_example/examples/test_aclnn_add_example.cpp:136-141](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L136-L141)：`aclrtSynchronizeStream` 阻塞等待 NPU 侧执行结束，随后 `PrintOutResult`（定义在第 37–49 行）用 `ACL_MEMCPY_DEVICE_TO_HOST` 把结果搬回 host 并逐元素打印——你在终端看到的 `result[i] is: 2.000000` 就来自第 46 行的 `LOG_PRINT`。
- [examples/add_example/examples/test_aclnn_add_example.cpp:143-159](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L143-L159)：资源清理——销毁三个 aclTensor、释放 device 内存与 workspace、销毁 stream、`aclrtResetDevice` 与 `aclFinalize`。养成样例「谁申请谁释放」的习惯。

#### 4.3.4 代码实践

1. **实践目标**：通过修改输入 shape 与数值，验证算子在不同输入下的行为，同时熟练样例的修改-重跑循环（对应 QUICKSTART 第四阶段）。
2. **操作步骤**（QUICKSTART 第 232–272 行给出了完全一致的示例）：
   - 编辑 `examples/add_example/examples/test_aclnn_add_example.cpp`，把第 98 行 `selfXShape = {32, 4, 4, 4}` 改为 `{8, 8, 8, 8}`，并把第 99 行 `std::vector<float> selfXHostData(2048, 1)` 的填充改为递增序列：`for (int64_t i = 0; i < 4096; ++i) selfXHostData[i] = static_cast<float>(i % 10);`
   - 同理修改 selfY（如固定填 1）与 out 的 shape 为 `{8, 8, 8, 8}`，host 数据长度改为 4096。
   - 由于只改了 example 代码、没改算子实现，**无需重新编译安装算子包**，直接重跑：`bash build.sh --run_example add_example eager cust --vendor_name=custom --soc=<soc_version>`。
3. **需要观察的现象**：输出行数从 2048 变为 4096；每行 `result[i]` 应等于 `(i % 10) + 1`。
4. **预期结果**：抽验若干行，例如 `input[0]=0, second=1 → result=1.000000`，`input[9]=9, second=1 → result=10.000000`。待本地验证。
5. 若修改后忘记同步修改 host 数据长度，`aclrtMemcpy` 会读到越界数据——观察现象并体会「shape 与数据长度必须一致」这条约束。

#### 4.3.5 小练习与答案

**练习 1**：`aclnnAddExample`（第二段）调用返回 `ACL_SUCCESS` 时，结果数据是否已经写回 out？

**答案**：没有。第二段接口只是把任务异步下发到 stream，立即返回。必须调用 `aclrtSynchronizeStream(stream)` 等待 NPU 执行完毕后，out 的 device 内存里才是最终结果，随后才能 D2H 拷贝。

**练习 2**：`CreateAclTensor` 中为什么 strides 要自己计算，而不是由 `aclCreateTensor` 自动推导？

**答案**：`aclCreateTensor` 接受任意 strides，描述的是「逻辑张量视图」；样例构造的是物理连续的 ND 张量，因此按 \( \text{strides}[i] = \text{shape}[i{+}1] \times \text{strides}[i{+}1] \) 从后向前累乘显式给出连续布局。若输入是非连续张量（如切片），strides 会不同——这正是 aclTensor 比「裸指针 + shape」表达能力更强的地方。

**练习 3**：workspace 是什么？本算子 workspaceSize 很可能为 0，为什么代码仍要处理大于 0 的分支？

**答案**：workspace 是算子执行框架在第一段接口里为算子申请的中转内存（如中间结果缓存）。AddExample 数据直接在输入输出间搬运计算，通常不需要 workspace，但框架约定 workspaceSize 由第一段接口动态返回，不同算子/shape 可能大于 0，所以样例写成通用模板：`if (workspaceSize > 0)` 才申请和释放。

### 4.4 图模式样例：test_geir_add_example.cpp

#### 4.4.1 概念说明

aclnn 样例是「单算子直调」：一次调用一个算子。图模式（geir）样例则用 GE 的 C++ API 把算子声明成图上的节点，整图编译后由 `Session::RunGraph` 一次性执行。对 AddExample 这种单节点图而言两者结果相同，但图模式更贴近训练框架（如将 ONNX 模型下沉到 NPU）的真实执行方式。

#### 4.4.2 核心流程

```text
① GEInitialize：初始化图引擎（指定 deviceId、graphRunMode）
② CreateOppInGraph：创建 op::AddExample("add1") 节点，用宏挂上 Data 输入与输出描述
③ graph.SetInputs(...).SetOutputs(...)：确定图的边界
④ new Session + session->AddGraph：创建会话并把图加入会话（触发整图编译）
⑤ session->RunGraph：执行，输入输出以 ge::Tensor 列表传递
⑥ 结果写 bin 文件（tc_ge_irrun_test_*_npu_input/output_*.bin）
⑦ GEFinalize：清理
```

#### 4.4.3 源码精读

- [examples/add_example/examples/test_geir_add_example.cpp:170-185](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L170-L185)：`CreateOppInGraph` 是构图的"自定义代码"段——`auto add1 = op::AddExample("add1")` 创建算子节点（这一行之所以合法，是因为第 29 行引入了 `add_example_proto.h`，其中 `REG_OP` 把 AddExample 注册进了 ge 命名空间）；随后 `ADD_INPUT` 宏（第 38–56 行定义）为每个输入创建 `op::Data` 占位符、生成全 1 数据并连到节点上。
- [examples/add_example/examples/test_geir_add_example.cpp:187-199](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L187-L199)：main 先 `GEInitialize`，global_options 里 `ge.exec.deviceId=0`、`ge.graphRunMode=1` 是图执行的基本配置——对比 aclnn 样例的 `aclrtSetDevice`，图模式把设备选择交给了 GE 的初始化参数。
- [examples/add_example/examples/test_geir_add_example.cpp:226-247](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L226-L247)：`new Session(build_options)` 创建会话、`session->AddGraph(graph_id, graph, ...)` 把构图交给 GE 编译、`session->RunGraph(graph_id, input, output)` 整图执行——对应 aclnn 样例里「两段接口 + Synchronize」的合体，同步等待隐含在 RunGraph 中。
- [examples/add_example/examples/test_geir_add_example.cpp:256-276](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L256-L276)：执行结束后，样例把每个输入/输出的 dtype、shape size 打印到 stdout，并把数据写成 `./tc_ge_irrun_test_0008_npu_input_*.bin` / `..._output_*.bin` 文件（`WriteDataToFile` 定义在第 161–168 行）。图模式样例的结果验证方式与 aclnn 不同：不打印逐元素值，而是落盘二进制供外部比对。
- [examples/add_example/examples/test_geir_add_example.cpp:278-292](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L278-L292)：收尾阶段还会通过 `GEGetErrorMsgV2`/`GEGetWarningMsgV2` 打印图引擎的错误与告警信息，最后 `GEFinalize` 清理——图模式失败时优先看这两条消息。

#### 4.4.4 代码实践

1. **实践目标**：跑通图模式样例，并与 aclnn 样例做一次直观对比。
2. **操作步骤**：在 4.1.4 已安装算子包的基础上执行（`graph` 模式即运行 geir 样例）：
   ```bash
   bash build.sh --run_example add_example graph cust --vendor_name=custom --soc=ascend910b
   # 运行后检查产物
   ls ./tc_ge_irrun_test_*_npu_*.bin
   ```
3. **需要观察的现象**：终端打印 `Run ir compute graph success`、`this is 0th output, output shape size =2048`（shape {32,4,4,4}）以及 Error/Warning message（正常应为空或无实质错误）；工作目录出现输入/输出 bin 文件。
4. **预期结果**：两个 output bin 文件各 8192 字节（2048 个 float32 × 4 字节）；可用 `python3 -c "import struct;d=open('tc_ge_irrun_test_0008_npu_output_0.bin','rb').read();print(struct.unpack('8f',d[:32]))"` 查看前 8 个值，应全为 `2.0`。待本地验证。
5. 对比记录：统计两个样例 main 函数的行数（aclnn 约 75 行、geir 约 105 行）和步骤数，记下你的感受——单算子直调更简洁，图模式胜在可组合多算子。

#### 4.4.5 小练习与答案

**练习 1**：图模式样例为什么 `#include "../op_graph/add_example_proto.h"`（第 29 行）？删掉会怎样？

**答案**：proto.h 里的 `REG_OP(AddExample)...OP_END_FACTORY_REG(AddExample)` 会在 `ge` 命名空间下生成 `op::AddExample` 算子类。删掉后 `op::AddExample("add1")` 无法编译，且即使编译通过，GE 也无法在算子原型注册表中识别该节点。aclnn 样例则不需要它——两套调用走的是两份不同的注册信息。

**练习 2**：aclnn 样例用 `aclrtSynchronizeStream` 等待结果，geir 样例里没有类似调用，为什么？

**答案**：`session->RunGraph` 本身是同步接口，调用返回时图已执行完、输出 Tensor 已填充数据，因此可以直接写文件；而 aclnn 第二段接口是异步下发到 stream，需要显式同步。这是两种调用方式在执行模型上的本质差异。

## 5. 综合实践

**任务：为 AddExample 建立一份「运行档案」。** 综合运用本讲的编译、安装、运行与源码对照知识：

1. 在配套环境中依次完成 4.1.4（aclnn 运行）与 4.4.4（geir 运行）两个实践。
2. 编写一份 `add_example_run_log.md`（放在你自己的笔记目录，不要放进仓库），至少记录：
   - 使用的 `--soc` 取值与芯片型号；
   - 编译产物 run 包的完整文件名与大小；
   - 安装后 `${ASCEND_HOME_PATH}/opp/vendors/` 下新增的目录名；
   - aclnn 样例日志中：算子名、输入 shape（{32,4,4,4}）、元素个数（2048）、首个与最后一个 result 值；
   - geir 样例日志中：`RunGraph` 是否成功、output shape size、bin 文件字节数；
   - 两种调用方式各自的关键 API 名（aclnn：GetWorkspaceSize/执行接口；geir：AddGraph/RunGraph）。
3. 最后做一次「故障注入」：故意以错误的 `--soc` 运行一次，把报错信息（应含 `161001`）也记入档案，并写一句话说明排查路径（回编译步骤核对 soc → 重编译 → 重安装 → 重运行）。

完成后你就拥有了一个可复用的算子验证模板——以后学习任何新算子（如 resize、roi_align），只需替换算子名和 shape 即可复用整套流程。

## 6. 本讲小结

- QUICKSTART 第一阶段的闭环是五步：进配套源码 → `build.sh --pkg --soc --ops` 编译 run 包 → 安装到 `opp/vendors` → 导出 `LD_LIBRARY_PATH` → `build.sh --run_example` 运行样例。
- **编译与运行的 `--soc` 必须一致**，否则报 `error 161001`，出错位置在第一段接口 `aclnnXxxGetWorkspaceSize`。
- aclnn 样例遵循九步固定骨架：Init → 构造 aclTensor → 两段式调用 → 同步 → 拷回结果 → 清理；这套骨架适用于仓库中所有算子的 `test_aclnn_*.cpp`。
- 两段式接口分工：`GetWorkspaceSize` 做校验、生成 `aclOpExecutor` 并告知 workspace 大小；执行接口把任务异步下发到 stream。
- geir 样例走 GE 构图路线：`REG_OP` 原型注册 → `op::AddExample` 建节点 → `Session::AddGraph/RunGraph` 整图编译执行，结果以 bin 文件落盘。
- 只改 example 样例无需重编算子包，直接重跑 `--run_example` 即可。

## 7. 下一步学习建议

下一讲进入第二单元 u2-l1「aclnn 两段式接口与基础数据结构」，我们将从本讲的「会用」深入到「懂原理」：精读 `aclnnResize` 的 op_api 实现，理解 `GetWorkspaceSize` 内部的参数校验顺序、非连续张量处理与 `aclOpExecutor` 的构造过程。建议在此之前：

- 重读本讲 4.3 节的九步骨架，确保能默写两段式接口的函数签名；
- 浏览 [docs/zh/context/two_phase_api.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/context/two_phase_api.md)，官方对两段式接口有更完整的参数级说明；
- 有余力的读者可提前打开 `image/resize_bilinear_v2/op_api/aclnn_resize.cpp` 扫一眼，感受真实算子的 op_api 比教学算子多了哪些检查。
