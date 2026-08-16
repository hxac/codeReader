# u2-l1 add_example 算子目录全景

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `add_example` 教学算子六个子目录（`op_host`、`op_kernel`、`op_kernel_aicpu`、`op_graph`、`examples`、`tests`）各自的职责边界。
- 理解 `op_host/config` 目录下二进制描述文件（`*_binary.json`）与 ini 配置（`*_simplified_key.ini`）的作用。
- 对照 README 复述一个算子从「build.sh 编译 → 安装包生成 → run_example 运行」的完整链路。

本讲是「全景导览」：我们刻意不深入任何一个文件的每一行，而是建立「每个文件放在哪里、被谁编译、被谁调用」的地图感。后续 u2-l2、u2-l3 会逐层精读。

## 2. 前置知识

本讲建立在前两讲的基础上，先快速回顾会用到的前置概念：

- **五层算子范式**（u1-l2）：一个标准算子目录通常包含 `op_host`（Host 侧算子信息库）、`op_api`（aclnn Eager 接口）、`op_kernel`（Device 侧 Ascend C 核函数）、`op_graph`（图模式扩展）、`tests`/`examples`（验证与示例）。
- **build.sh 构建入口**（u1-l4）：`--ops=<算子名>` 指定只编译某个算子，`--soc=<soc_version>` 指定目标芯片，`--run_example` 一键编译并运行示例。
- **Host 侧 / Device 侧**：Host 指 CPU 侧，负责算子注册、shape 推导、切分策略（tiling）等「元数据」计算；Device 指 NPU 上的计算单元，负责真正的数值计算。
- **aclnn 两段式调用**：Eager 模式下先调 `aclnnXxxGetWorkspaceSize`（准备阶段），再调 `aclnnXxx`（执行阶段）。本讲只需记住这个骨架，u3-l1 会展开。
- **SoC 与 soc_version**：`ascend910b` 对应 A2 系列、`ascend910_93` 对应 A3 系列、`ascend950` 对应 A5 系列（950 系列）。

另外两个本讲新出现的术语：

- **opc 二进制编译**：CANN 提供的离线编译工具，能把 Ascend C 源码预编译成二进制 kernel（`.o`/`.json` 描述），供运行时直接加载，省去在线编译时间。
- **simplified key**：算子二进制的一种「简化匹配模式」，让运行时用更粗粒度的 key 匹配预编译产物，减少二进制变体数量。

## 3. 本讲源码地图

本讲涉及的关键文件（均位于 `examples/add_example/` 下）：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/README.md) | 算子说明书：产品支持、计算公式、参数表、快速启动命令 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/CMakeLists.txt) | 算子根 CMake：自动发现并 add_subdirectory 各子目录 |
| [op_host/add_example_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp) | 算子原型定义：输入输出、dtype/format、AICore 配置 |
| [op_host/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/CMakeLists.txt) | 把 def/infershape/tiling 源码注册进构建系统 |
| [op_host/config/ascend910b/add_example_simplified_key.ini](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/config/ascend910b/add_example_simplified_key.ini) | opc 编译时的 simplified_key_mode 配置 |
| [op_host/config/ascend910b/add_example_binary.json](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/config/ascend910b/add_example_binary.json) | 预编译二进制 kernel 的描述信息 |
| [op_host/add_example_infershape.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_infershape.cpp) | Host 侧 shape 推导 |
| [op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp) | Host 侧 tiling 切分策略计算 |
| [op_kernel/add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.cpp) | Device 侧 Ascend C 核函数入口 |
| [op_kernel_aicpu/add_example.json](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example.json) | AICPU 版本算子的描述信息 |
| [op_graph/add_example_proto.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_proto.h) | 图模式算子原型声明（REG_OP） |
| [op_graph/add_example_graph_infer.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_graph_infer.cpp) | 图模式 dtype 推导（InferDataType） |
| [examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp) | aclnn Eager 调用示例程序 |
| [examples/test_geir_add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_geir_add_example.cpp) | GE 图模式调用示例程序 |
| [tests/ut/op_host/test_add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp) | tiling 单元测试 |
| [tests/ut/op_kernel/add_example_data/gen_data.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py) | kernel 测试数据生成脚本 |

注意一个容易混淆的点：`examples/add_example/examples/` 这个子目录是「算子自带的调用示例程序」，而顶层的 `examples/` 目录是「仓库的教学算子区」，两层 `examples` 含义不同。

## 4. 核心概念与源码讲解

### 4.1 算子目录范式：CMake 如何把一个算子拼进构建

#### 4.1.1 概念说明

u1-l2 讲过「五层范式」，本模块回答一个更机械的问题：**这些子目录是怎么被构建系统找到的？**

答案是一种「自动发现」约定：算子根目录的 CMakeLists 用 `file(GLOB)` 列出所有子目录，只要子目录里有自己的 `CMakeLists.txt`，就被 `add_subdirectory` 递归纳入构建。这意味着**新增一层交付件时，通常不需要改算子根 CMake**——放下带 CMakeLists 的目录即可。

#### 4.1.2 核心流程

```text
build.sh --ops=add_example
  └─ CMake 进入 examples/add_example/
       └─ 根 CMakeLists：GLOB 所有子目录
            ├─ 有 CMakeLists.txt 的子目录 → add_subdirectory
            │    ├─ op_host/      → 注册 host 侧源码与配置
            │    ├─ op_graph/     → 注册图模式源码
            │    ├─ op_kernel_aicpu/ → 注册 AICPU kernel
            │    └─ tests/        → 仅当 ENABLE_TEST 开启时纳入
            └─ 无 CMakeLists.txt 的目录（op_kernel/、examples/）→ 由仓库统一的
               kernel/示例构建逻辑按命名约定处理，不走这条递归路径
```

关键细节：`tests` 目录在未开启 `ENABLE_TEST` 时会被显式剔除——这就是为什么日常 `--ophost` 编译不会连测试一起编。

#### 4.1.3 源码精读

算子根 CMakeLists 的全部有效逻辑只有 9 行：

[examples/add_example/CMakeLists.txt:L11-L19](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/CMakeLists.txt#L11-L19) —— GLOB 当前目录下的所有子目录；若未开启 `ENABLE_TEST` 就把 `tests` 从列表中移除；然后遍历剩余目录，凡是自带 `CMakeLists.txt` 的都 `add_subdirectory` 纳入构建。

再看 `op_host/CMakeLists.txt`，它展示了 host 侧源码的注册方式：

[examples/add_example/op_host/CMakeLists.txt:L11-L18](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/CMakeLists.txt#L11-L18) —— 定义 `BUILD_OPEN_PROJECT` 与否两个分支，但两个分支都调用了 `add_modules_sources(OPTYPE add_example ACLNNTYPE aclnn)`：这是仓库 cmake 层提供的封装函数（u1-l4 提过的「模块注册」机制），把本算子的 host 源码（def/infershape/tiling，按命名约定收集）挂到 `add_example` 这个 OPTYPE 名下。回忆 u1-l2 的结论：**tiling 文件名必须含 `_tiling` 才会被识别**，这里 `add_example_tiling.cpp` 正是靠这个约定被自动编入。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「自动发现」机制的存在。

**操作步骤**：

1. 打开 `examples/add_example/CMakeLists.txt`，数一数有效代码行数。
2. 对照 `find` 列出的目录树，检查哪些子目录有 `CMakeLists.txt`（op_host、op_graph、op_graph/fusion_pass、op_kernel_aicpu、tests、tests/ut、tests/ut/op_host、tests/ut/op_kernel），哪些没有（op_kernel、examples）。
3. 执行 `bash build.sh --ophost --ops=add_example`，在输出的 cmake 日志里找到「Info: cmake config」行（u1-l4 讲过的观察入口），确认 `ASCEND_OP_NAME` 中包含 `add_example`。

**需要观察的现象**：编译产物中 host 相关动态库/对象被生成在 `build/output` 下；因为没开测试开关，`tests/` 下的用例没有被编译。

**预期结果**：编译成功，且日志中没有任何 `test_add_example` 相关目标。若在无 NPU 的编译态环境（u1-l3 讲过：只装 toolkit 包即可），此步同样能完成。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果删除 `op_graph/CMakeLists.txt`，`--opgraph --ops=add_example` 会发生什么？

答案：根据根 CMakeLists 的判断条件 `if(EXISTS .../CMakeLists.txt)`，`op_graph` 不再被 `add_subdirectory`，图模式源码不会参与编译；由于仓库默认「不传即全量、传了按裁剪」，产物中会缺少该算子的图模式部分（若构建系统要求非空目标，可能落到空包兜底，具体行为待本地验证）。

**练习 2**：为什么 `tests` 目录需要「显式剔除」而不是「显式加入」？

答案：因为发现机制是 GLOB 全部子目录 + 有 CMakeLists 就纳入，默认行为会「多编」；测试只在 `--ophost_test` 等测试开关（它会设置 `ENABLE_TEST`）打开时才需要。显式剔除让默认路径保持干净，这是「白名单反面：默认全量、按需排除」思路的落地。

### 4.2 add_example 教学算子：op_host 层与 config 配置

#### 4.2.1 概念说明

`op_host` 是算子的「身份证 + 参谋部」：

- **def 文件**向框架注册算子原型：有几个输入输出、支持什么数据类型、跑在哪些芯片上、kernel 入口文件叫什么名字。框架（GE/FE）在构图和调度时全靠它。
- **infershape / tiling** 分别负责输出 shape 推导和切分策略，u2-l2 会精读，本讲只定位它们的位置和注册宏。
- **config 目录**按 SoC 分目录（这里是 `ascend910b`）存放两类配置：
  - `*_binary.json`：预编译二进制 kernel 的「索引卡片」，描述每个二进制支持什么 dtype/format/shape。
  - `*_simplified_key.ini`：告诉 opc 工具编译二进制时用哪种 simplified key 模式。

#### 4.2.2 核心流程

一次图模式/Eager 调用中 host 侧信息的流转：

```text
框架拿到算子节点
  └─ 查 def 注册表：AddExample 是什么？输入 x1/x2、输出 y、支持 fp32/int32
       └─ 调 InferShapeAddExample：推出 y 的 shape（本算子 = x1 的 shape）
            └─ 调 AddExampleTilingFunc：按 shape/dtype 算出 tilingKey + tilingData + blockDim
                 └─ 运行时按 tilingKey 匹配 config/ascend910b/add_example_binary.json
                    里登记的二进制（或在线编译 op_kernel 源码）→ 下发 Device 执行
```

#### 4.2.3 源码精读

先看 def 文件的骨架（详细拆解留给 u2-l2）：

[examples/add_example/op_host/add_example_def.cpp:L18-L27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L18-L27) —— 定义 `AddExample` 类继承 `OpDef`，为输入 `x1` 声明「必选输入、支持 FLOAT/INT32、ND 格式、内存自动连续化」等约束。

[examples/add_example/op_host/add_example_def.cpp:L41-L51](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L41-L51) —— AICore 配置块：开启动态 shape、支持动态 rank 等；其中 `ExtendCfgInfo("opFile.value", "add_example")` 是**host 与 device 的连接点**——这个值对应 kernel 入口文件名 `add_example.cpp`；最后通过 `AddConfig` 把同一份配置注册到 `ascend910b`、`ascend910_93`、`ascend950` 三代 SoC 上。

[examples/add_example/op_host/add_example_def.cpp:L54](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L54) —— `OP_ADD(AddExample)` 宏把整个算子定义注册进全局算子信息库，框架由此「认识」这个算子。

再看两个注册入口宏（本讲只认脸，不深入）：

[examples/add_example/op_host/add_example_infershape.cpp:L23-L47](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_infershape.cpp#L23-L47) —— `InferShapeAddExample` 读取输入 x1 的 shape，逐维复制给输出 y，最后由 `IMPL_OP_INFERSHAPE(AddExample).InferShape(...)` 注册。

[examples/add_example/op_host/add_example_tiling.cpp:L102-L141](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L102-L141) —— tiling 分发入口 `AddExampleTilingFunc`：依次取平台信息（UB 大小、核数）、shape 与 dtype、workspace 大小，然后填充 `AddExampleTilingData`、设置 `BLOCK_DIM = 8`，并按 dtype 是 FLOAT 还是 INT32 设置不同的 tilingKey。文末由 `IMPL_OP_OPTILING` 宏注册。

然后是本讲的重点之一——config 目录下两个文件。

**二进制描述 json**：

[examples/add_example/op_host/config/ascend910b/add_example_binary.json:L2-L5](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/config/ascend910b/add_example_binary.json#L2-L5) —— 顶层声明 `op_type: "AddExample"`，`op_list` 数组里每个元素描述一个预编译二进制变体，`bin_filename` 是带哈希后缀的二进制文件名（哈希由算子与关键配置派生，避免重名）。

[examples/add_example/op_host/config/ascend910b/add_example_binary.json:L6-L17](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/config/ascend910b/add_example_binary.json#L6-L17) —— 第一个变体：`dtype: float32`、`format: ND`、`shape: [-2]`。`-2` 是 CANN 的特殊编码，表示「任意维度」，配合 def 里的 `DynamicRankSupportFlag(true)` 实现一份二进制覆盖所有 rank 的输入。json 里共登记了 float32 与 int32 两个变体，正好对应 tiling 里按 dtype 分出的两个 tilingKey 分支。

**ini 配置**：

[examples/add_example/op_host/config/ascend910b/add_example_simplified_key.ini:L12-L13](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/config/ascend910b/add_example_simplified_key.ini#L12-L13)（规则说明见同文件 L1-L11 的注释块）—— 文件头 10 行注释详细说明了规则：该 ini 控制 opc 工具编译二进制 kernel 时 `--simplified_key_mode` 选项的取值；`[AddExample]` 段下 `default=0` 表示所有平台默认使用 simplified_key_mode=0。若某芯片有差异化要求，可增加 `ascendxx=xx` 行覆盖默认值。

为什么这两类文件重要：它们是「二进制交付」形态的入口。业务大算子（如 flash_attention_score）的二进制配置比这里复杂得多，但格式与机制完全同源——看懂 add_example 这两个文件，就拿到了阅读它们的钥匙。

#### 4.2.4 代码实践

**实践目标**：建立「def 声明 ↔ binary.json 登记 ↔ tiling 分支」三者的对应关系。

**操作步骤**：

1. 打开 [README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/README.md)，记录「产品支持情况」表：A3 训练/推理系列、A2 训练/推理系列均支持。
2. 对比 def 文件 L49-L51 的三个 `AddConfig`（ascend910b / ascend910_93 / ascend950）与 README 支持表，找出差异。
3. 数一数 `add_example_binary.json` 中 `op_list` 的变体数量，与 `add_example_tiling.cpp` L130-L139 中按 dtype 设置 tilingKey 的分支数量对比。

**需要观察的现象**：README 只列了 A2/A3 两代产品，而 def 注册了三代 SoC（多出 ascend950，即 950 系列）；binary.json 有 2 个变体，tiling 恰好有 2 个 dtype 分支。

**预期结果**：三者对上：float32 变体 ↔ tilingKey 分支 0、int32 变体 ↔ tilingKey 分支 1。README 的支持表反映的是「产品化交付范围」，def 的 AddConfig 反映的是「代码可编译范围」——后者可以更宽，这是仓库中常见的现象（新 SoC 先打通编译，产品表随后更新）。

#### 4.2.5 小练习与答案

**练习 1**：`binary.json` 中 `"shape": [-2]` 如果改成 `"shape": [4]`，语义会有什么变化？

答案：`[4]` 表示该二进制只匹配固定 4 维（或按约定固定 shape）的输入；配合 def 中的 `DynamicRankSupportFlag(true)` 与 `DynamicShapeSupportFlag(true)` 就互相矛盾了——动态 shape 支持依赖 `-2`（任意维）这类通配编码来让一份二进制覆盖多种输入。改成固定值后，其他 rank 的输入将无法命中该二进制。

**练习 2**：如果想让 add_example 在某新 SoC 上可用，至少要改 def 文件的哪一行？还要考虑什么？

答案：至少要增加一行 `this->AICore().AddConfig("<新soc_version>", aicoreConfig)`。此外还要考虑：op_kernel 侧是否有对应 arch 目录的实现（u4-l3 会讲 arch22/arch35 的隔离方式）、config 目录是否需要新增 `<新soc>` 子目录的 binary/ini 配置，以及 build.sh 的 soc 列表是否支持该芯片。

### 4.3 其余四层速览与「编译到运行」全链路

#### 4.3.1 概念说明

剩下四个子目录各自解决一件事：

- **op_kernel**：Device 侧 Ascend C 核函数，真正做 `y = x1 + x2` 的数值计算。本讲只看入口骨架，u2-l3 精读。
- **op_kernel_aicpu**：同一算子的 AICPU 版本——不跑在向量/矩阵计算核上，而是跑在 NPU 内置的 AI CPU 上，适合调试或不适合 AICore 的逻辑。u2-l5 精读。
- **op_graph**：图模式扩展，包含 proto 声明（供 GE 图构图使用）与图上的 dtype 推导。
- **examples 与 tests**：可执行示例与单元测试，是算子的「验收证据」。

#### 4.3.2 核心流程

README 给出的完整链路（引用其快速启动命令）：

```bash
cd example/add_example
# 假设已准备好环境变量
bash build.sh --soc=${soc_version} --ops=add_example     # ① 编译并打出安装包
bash build.sh --run_example add_example eager cus       # ② 编译并运行 eager 示例
```

①内部发生的事（承接 u1-l4）：build.sh 把 `--ops` 翻译成 `ASCEND_OP_NAME` 缓存变量 → CMake 经 4.1 的自动发现机制收集 op_host/op_graph/op_kernel_aicpu 源码 → kernel 按 `--soc` 指定的 SoC 离线编译 → 产出 `build_out` 下的 `.run` 安装包 → 安装后算子进入 CANN 的自定义算子目录，aclnn 接口可用。

②内部发生的事：编译 `examples/test_aclnn_add_example.cpp` 为可执行文件并直接运行，它的调用骨架是：

```text
aclInit / SetDevice / CreateStream            # 环境初始化
aclrtMalloc + aclrtMemcpy + aclCreateTensor   # 准备 device 侧输入输出
aclnnAddExampleGetWorkspaceSize(...)          # 两段式第一段：算 workspace、生成 executor
aclnnAddExample(workspaceAddr, ..., stream)   # 两段式第二段：下发执行
aclrtSynchronizeStream → 拷回结果 → 释放资源  # 收尾
```

#### 4.3.3 源码精读

**op_kernel 入口**：

[examples/add_example/op_kernel/add_example.cpp:L23-L38](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.cpp#L23-L38) —— `__global__ __aicore__ void add_example(...)` 是 Ascend C 核函数入口：宏 `GET_TILING_DATA_WITH_STRUCT` 从 tiling 参数解出 Host 侧算好的 `AddExampleTilingData`；再按模板参数 `schMode`（即 tilingKey）在编译期分支——FLOAT 走 `AddExample<float>` 实例、INT32 走 `AddExample<int32_t>` 实例，每个实例执行 `Init → Process` 两步。注意入口文件名 `add_example` 与 def 里 `ExtendCfgInfo("opFile.value", "add_example")` 精确对应。

**op_graph 两个文件**：

[examples/add_example/op_graph/add_example_proto.h:L35-L39](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_proto.h#L35-L39) —— `REG_OP(AddExample)` 声明图模式算子原型：两个输入一个输出、支持 DT_FLOAT/DT_INT32，供 GE 构图时创建算子节点。

[examples/add_example/op_graph/add_example_graph_infer.cpp:L24-L36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_graph_infer.cpp#L24-L36) —— `InferDataTypeAddExample` 把输出 dtype 设为与输入一致，由 `IMPL_OP(AddExample).InferDataType(...)` 注册。shape 推导不在这一层——它复用 op_host 的 infershape（u6-l2 会讲两层边界）。

**op_kernel_aicpu 描述文件**：

[examples/add_example/op_kernel_aicpu/add_example.json:L3-L13](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example.json#L3-L13) —— 声明该算子跑在 `DNN_VM_AICPU` 引擎、入口函数 `RunCpuKernel`、产物动态库名 `libtransformer_aicpu_kernels.so`，并给出输入输出类型。这是 AICPU 算子与 def.json（AICore）截然不同的注册方式。

**examples 示例的两段式调用**：

[examples/add_example/examples/test_aclnn_add_example.cpp:L121-L134](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L121-L134) —— 第一段 `aclnnAddExampleGetWorkspaceSize(selfX, selfY, out, &workspaceSize, &executor)` 计算 workspace 并生成执行器；按返回的 workspaceSize 申请 device 内存后，第二段 `aclnnAddExample(workspaceAddr, workspaceSize, executor, stream)` 异步下发到 stream。这就是 aclnn 两段式的活样本。

**tests 的两类测试**：

[examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp:L35-L56](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp#L35-L56) —— tiling UT 用 `TilingContextPara` 伪造一个 tiling 上下文（shape `{1,2,8,16}`、DT_FLOAT），断言期望的 tilingKey、tilingData 字符串（`"256 8 "` 即 totalLength=256、tileNum=8）和 workspace 大小，再由 `ExecuteTestCase` 驱动真实 tiling 函数比对——不需要 NPU 就能验证 host 逻辑。

[examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py:L25-L37](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/add_example_data/gen_data.py#L25-L37) —— kernel 测试的数据生成脚本：用 numpy 构造含特殊值（-65504、nan、inf 等）的输入，用 `np.add` 生成 golden（期望输出），双双落盘为 `.bin` 文件，供上板/simulator 比对（配套的 `compare_data.py` 负责比对）。

#### 4.3.4 代码实践

**实践目标**：从 README 出发走通「编译 → 运行」链路，并把六个子目录串成一个故事。

**操作步骤**：

1. 阅读 [examples/add_example/README.md:L65-L88](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/README.md#L65-L88)：注意「调用说明」表只给了 aclnn 一种调用样例链接（`test_aclnn_add_example.cpp`），但目录里还存在 `test_geir_add_example.cpp`（graph 模式样例，u2-l4 会用）。
2. 在有 NPU 或 simulator 的环境执行：`bash build.sh --soc=ascend910b --ops=add_example`，观察 `build_out` 下生成的 `.run` 包。
3. 接着执行 `bash build.sh --run_example add_example eager cus`，观察输出。

**需要观察的现象**：示例程序打印每个元素的三元组：`first input[i]`、`second input[i]`、`result[i]`（见 `PrintOutResult` 的打印逻辑，[test_aclnn_add_example.cpp:L45-L48](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L45-L48)）；输入是两个全 1 的 `{32,4,4,4}` 向量，因此所有结果应为 2。

**预期结果**：2048 行输出，每行 result 均为 2.000000。若在无 NPU 环境，本实践退化为源码走读：逐行标注 `test_aclnn_add_example.cpp` 的 main 函数九个步骤注释（源码已用 `// 1.` ~ `// 9.` 标出），验证与 4.3.2 的流程图一致。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：add_example 没有独立的 `op_api` 目录，但示例却调用了 `aclnnAddExample` 接口，接口实现从哪来？

答案：aclnn 接口的 C++ 包装由 CANN 框架在安装算子包后依据算子原型自动生成/注册（`#include "aclnnop/aclnn_add_example.h"` 引用的是框架头文件路径，见 [test_aclnn_add_example.cpp:L14](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L14)）。仓库中的 `op_api` 目录存放的是**需要手写复杂校验/分发逻辑**的算子（如 flash_attention_score）；简单算子不需要这一层。

**练习 2**：`op_graph/add_example_graph_infer.cpp` 只推导 dtype，不推导 shape，shape 推导在哪里完成？

答案：在 op_host 的 `add_example_infershape.cpp` 中由 `IMPL_OP_INFERSHAPE(AddExample).InferShape(InferShapeAddExample)` 注册完成。图模式与 Eager 模式共享 host 侧的 infershape 实现，op_graph 层只补充图模式特有的扩展（本算子是 InferDataType）。这是「一份 host 实现、两条调用路径复用」的典型设计。

**练习 3**：为什么 tiling UT 里 `expectTilingData` 是字符串 `"256 8 "`？

答案：测试框架把 tilingData 结构体的内存按字段序列化为空白分隔的字符串再比对（`totalLength=256` 来自 `1*2*8*16`，`tileNum=8` 来自常量 `TILE_NUM`）。用字符串比对可以让断言与具体结构体字段解耦，框架统一处理。

## 5. 综合实践

**任务：为 add_example 制作一张带注释的目录树，并总结其支持范围。**

1. 在仓库根目录执行 `find examples/add_example -type f | sort`，把输出整理成树状图。
2. 为每个文件写一行三段式注释，格式如：`op_host/add_example_def.cpp ｜ 做什么：注册算子原型与 AICore 配置 ｜ 被谁编译：op_host/CMakeLists.txt 经 add_modules_sources 收集 ｜ 被谁调用：OP_ADD 宏注册后由 GE/FE 框架查询`。本讲 4.1–4.3 的源码精读部分已给出大部分答案，可直接对照填写；op_kernel 下的 `add_example.h`、`add_example_tiling_data.h`、`add_example_tiling_key.h` 三个头文件的作用可在 u2-l3 学完后回填。
3. 阅读 [examples/add_example/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/README.md)，写一段不超过 100 字的总结，必须包含：支持的 SoC（结合 def 中实际注册的三代）、支持的 dtype（FLOAT/INT32）、调用方式（aclnn Eager；目录中另备 graph 样例）、shape 约束（tiling 限定 4 维，见 `DIMS_LIMIT = 4`）。
4. 检验：把你总结的「shape 约束」与 README 参数表对照——README 的「约束说明」写的是「无」，但实际上 tiling 代码要求 4 维输入。这是一个很好的教训：**README 面向使用者，源码才是事实**；这也是本手册强调读源码的原因。

## 6. 本讲小结

- 算子根 CMakeLists 用「GLOB + 有 CMakeLists 才 add_subdirectory」的自动发现机制组装子目录，`tests` 仅在 `ENABLE_TEST` 时纳入；host 源码经 `add_modules_sources(OPTYPE ...)` 按命名约定（如 `_tiling`）注册。
- `op_host/add_example_def.cpp` 是算子身份证：输入输出约束、`ExtendCfgInfo("opFile.value", ...)` 指定 kernel 入口文件名、`AddConfig` 注册到 ascend910b/ascend910_93/ascend950 三代 SoC。
- `op_host/config/<soc>/` 下，`*_binary.json` 登记预编译二进制变体（float32/int32 各一份，`shape: [-2]` 表示任意 rank），`*_simplified_key.ini` 控制 opc 编译时的 simplified key 模式。
- `op_kernel` 入口按 tilingKey 模板参数在编译期分支实例化 kernel；`op_graph` 提供 REG_OP 原型与 InferDataType，shape 推导复用 op_host；`op_kernel_aicpu` 是跑在 AI CPU 上的另一套实现，用 json 描述注册。
- 完整链路：`build.sh --soc --ops=add_example` 出 `.run` 安装包 → `--run_example add_example eager cus` 编译并运行示例 → 示例按「初始化 → 构造张量 → 两段式 aclnn 调用 → 同步回收」九步执行。
- README 的支持表（A2/A3）与 def 的 AddConfig（多出 ascend950）可以不一致：前者是产品化交付范围，后者是代码可编译范围。

## 7. 下一步学习建议

下一讲 **u2-l2《op_host 三件套：def、infershape 与 tiling》** 将深入本讲只「认脸」的三个 host 文件：def 的注册 DSL 细节、`DoInferShape`/`InferShape` 的写法约定、tiling 函数如何结合平台信息（UB 大小、核数）产出 tilingData 与 tilingKey。建议先自行通读 [add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp) 并数清它有几次 `OP_CHECK_*` 防御性检查，为精读做铺垫。之后再进入 u2-l3 的 Ascend C kernel 内部。
