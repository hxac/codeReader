# u8-l1 UT 框架 framework_normal 总览：faker 与执行器

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 op_host / op_api / op_kernel 三类单元测试（UT）各自验证算子的哪一层、各自依赖什么运行环境。
2. 解释 faker（伪造者）如何在不依赖真实 GE 图引擎与 NPU 驱动的宿主机上，构造出一个足以骗过 tiling 函数的 `TilingContext`。
3. 跟踪一个 tiling UT 用例从 `main` 入口 → 环境 SetUp → 用例构造 → faker 组装 → tiling 函数执行 → gtest 断言的完整执行路径。
4. 对照 CMake 说明「新增一个算子的 UT」需要挂接哪些构建点（这是本讲综合实践的核心）。

本讲是第 8 单元（测试体系）的第一篇，是 u8-l2（编写 tiling 用例）与 u8-l4（运行 UT/ST）的地基。

## 2. 前置知识

### 2.1 UT 与 ST 的区别

- **UT（Unit Test，单元测试）**：在宿主机（x86 CPU）上编译运行的 C++ gtest 程序，验证一个函数或一个模块的逻辑分支，不需要真实 NPU 硬件参与计算。本仓库 UT 主要验证 tiling 切分、InferShape 推导、aclnn 参数校验这些 **Host 侧逻辑**。
- **ST（System Test，系统级精度测试）**：在真实 NPU 上跑完整算子，与 CPU 高精度参考实现对比数值误差（第 u8-l3 讲的主题）。

一句话：UT 验证「流程与分支对不对」，ST 验证「算得准不准」。

### 2.2 为什么需要 faker（伪造者）

回顾 u2-l3：tiling 函数的注册签名是 `ge::graphStatus TilingFunc(gert::TilingContext*)`。真实的 `TilingContext` 由图引擎（GE）在算子编译时构造，里面装着输入张量描述、平台信息（核数、UB 大小）、属性等。问题在于：跑 UT 时既没有 GE，也没有 NPU 驱动，谁来提供这个 context？

答案就是 **faker**：用 CANN 提供的 builder 基类手工拼装出一个结构与真品完全一致的假 context。tiling 函数是「签名保真」的——它只调用 `TilingContext` 的公开接口（`GetInputShape`、`GetPlatformInfo`、`SetTilingKey` 等），只要假 context 的这些接口返回合理数据，tiling 函数就会照常工作。这与 u3-l4 讲过的 stub（桩）思想一致，区别在于：

- **stub** 替换的是「被测代码调用的下游依赖」（如 `l0op::transpose`、`rtStreamSynchronize`）；
- **faker** 伪造的是「被测代码的输入环境」（TilingContext 本身）。

### 2.3 gtest 三件套

本仓库全部 UT 基于 GoogleTest，需要认识三个概念：

- `TEST_F(TestSuiteName, CaseName)`：定义一个挂载 fixture 的用例；
- `testing::Environment`：全局环境，`main` 之前执行一次 `SetUp`（本讲会看到它承担 .so 加载）；
- `ASSERT_EQ` / `EXPECT_EQ`：断言，前者失败立即返回，后者失败继续执行。

### 2.4 与前两讲的衔接

- u2-l3 讲过 tiling 的四项输出契约：blockDim、tilingKey、TilingData 字节流、workspace 大小——本讲的 `ExecuteTestCase` 正是围绕这四项做断言。
- u3-l4 讲过 stub 分层与「op_api UT 链接四层替身」——本讲把那一讲的静态描述落到具体的框架代码上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h#L1-L264) | 定义「用例参数包」`TilingContextPara` 与「伪造器」`TilingContextFaker`，是所有 tiling UT 的造数入口 |
| [ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.h#L1-L35) | 声明执行器：`ExecuteTestCase`（带断言）与 `ExecuteTiling`（只执行收结果） |
| [ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L1-L319) | 执行器实现：核心是 `DO_TILING` 宏（造 Tensor、伪造平台 JSON、查注册表、调 tiling 函数） |
| [ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp#L1-L57) | op_host UT 的 main 入口：全局环境加载算子宿主库 libophost_transformer_ut.so |
| [ascendc/src/tests/ut/framework_normal/op_api/op_api_ut_common/inc/op_api_ut_common/op_api_ut.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/op_api_ut_common/inc/op_api_ut_common/op_api_ut.h#L1-L365) | op_api UT 公共库核心：`OpApiUt` 模板类与 `OP_API_UT`/`INPUT`/`OUTPUT` 宏 |
| [ascendc/src/tests/ut/framework_normal/op_kernel/data_utils.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_kernel/data_utils.h#L1-L30) | op_kernel UT 的数据文件读写工具（读入输入二进制、写回结果） |
| [ascendc/src/tests/ut/framework_normal/op_kernel/test_op_kernel_main.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_kernel/test_op_kernel_main.cpp#L1-L50) | op_kernel UT 的 main 入口：加载 tiling 宿主库供 CPU 侧 kernel 测试取 tiling 结果 |
| [ascendc/cmake/ut.cmake](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L1-L391) | UT 构建的「总线」：聚合各算子用例、定义三类 UT 目标 |
| [ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L1-L512) | 标本用例：ai_infra_aggregate_hidden 的 17 个 tiling UT（正例 + 反例） |

补充目录：`common/` 下还有 infershape 系列 faker（`infer_shape_context_faker.h` 等，机制与 tiling 同构）；`op_api/scripts/` 下有 stub 生成脚本；`op_kernel/scripts/` 下有 tiling 头文件生成脚本。这些在本讲 4.5 节概述。

## 4. 核心概念与源码讲解

### 4.1 UT 体系全景：三类 UT 与 framework_normal 目录

#### 4.1.1 概念说明

一个算子有四层（u1-l2 的心智模型），UT 也按层分三类，各测各的：

| UT 类型 | 被测对象 | 典型断言 | 需要的环境 |
| --- | --- | --- | --- |
| op_host UT | `_tiling.cpp` 的 TilingFunc、`_infershape.cpp` | 返回值（GRAPH_SUCCESS/FAILED）、tilingKey、workspace、TilingData 字节流 | 宿主机 + CANN 头文件/库 + faker 伪造的平台信息，**无需 NPU** |
| op_api UT | `aclnnXxxGetWorkspaceSize` 两段式接口第一段 | 返回码 ACLNN_SUCCESS、不崩溃（空指针反例） | 宿主机 + CANN + stub 桩（u3-l4），**无需 NPU** |
| op_kernel UT | Ascend C Kernel 在 CPU 模拟器上的行为 | 输出与期望对比 | CANN 的 `tikicpulib`（CPU 指令模拟器），**无需 NPU** |

三者共用一个框架目录 `src/tests/ut/framework_normal/`，布局是「common 公共件 + 三个入口目录」：

```text
framework_normal/
├── CMakeLists.txt          # 框架级入口：设置 UT_COMMON_INC，自动递归子目录
├── empty.cpp               # 空文件，给 OBJECT 库当种子源（见 4.4）
├── common/                 # 公共 faker/executor（无 CMakeLists，被 ut.cmake glob 收编）
│   ├── tiling_context_faker.h/.cpp
│   ├── tiling_case_executor.h/.cpp
│   ├── infer_shape_context_faker.h/.cpp …（infershape 同构四件套）
│   └── kernel_run_context_holder.h  # 聚合五种 context 的持有器
├── op_host/                # main + CMakeLists → 可执行 transformer_op_host_ut
├── op_api/                 # main + op_api_ut_common 公共库 + scripts → transformer_op_api_ut
└── op_kernel/              # main + data_utils + scripts（当前仓库内无用例，备而未用）
```

#### 4.1.2 核心流程

三类 UT 的构建开关由 `build.sh` 翻译成 CMake 变量，链路是：

```text
bash build.sh -u --ophost
  └─ ENABLE_TEST=TRUE（-u）
  └─ OP_HOST=TRUE（--ophost）
  └─ set_ut_mode(): OP_HOST_UT=TRUE, UT_TEST_ALL=FALSE
  └─ cmake -DENABLE_TEST=TRUE …
      └─ 顶层 CMakeLists: if(ENABLE_TEST) add_subdirectory(src/tests/ut/framework_normal)
          └─ framework_normal/CMakeLists: 递归 add_subdirectory（op_host/op_api/op_kernel）
              └─ op_host/CMakeLists: 生成 transformer_op_host_ut 并（ENABLE_UT_EXEC 时）自动运行
```

#### 4.1.3 源码精读

`build.sh` 的 `-u` 参数只做一件事——置位 `ENABLE_TEST`：[ascendc/build.sh:L291-L294](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L291-L294) 中 `-u|--test` 分支把 `ENABLE_TEST=TRUE`；而 `set_ut_mode` 在 [ascendc/build.sh:L178-L195](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L178-L195) 决定跑「全部 UT」还是「某一类 UT」（默认 `UT_TEST_ALL=TRUE`，指定 `--ophost` 后 `OP_HOST_UT=TRUE` 且 `UT_TEST_ALL=FALSE`）。

框架目录进入构建的入口在顶层 CMakeLists：[ascendc/CMakeLists.txt:L289-L293](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L289-L293) 在 `ENABLE_TEST` 且任一 UT 开关为真时 `add_subdirectory(src/tests/ut/framework_normal)`。

框架级 CMakeLists 做两件事：把 `common/` 设为全局包含路径 `UT_COMMON_INC`，然后自动把带 CMakeLists 的子目录挂进来：[ascendc/src/tests/ut/framework_normal/CMakeLists.txt:L14-L24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/CMakeLists.txt#L14-L24)。注意 `common/` 自己没有 CMakeLists——它的源文件是由 `ut.cmake` 用 glob 直接收进公共 OBJECT 库的（见 4.4.3）。

#### 4.1.4 代码实践

1. **实践目标**：建立三类 UT 的目录与构建开关的对应关系。
2. **操作步骤**：
   - 执行 `grep -rn "UT_TEST_ALL\|OP_HOST_UT\|OP_API_UT" ascendc/cmake/ut.cmake | head -20`，观察每个函数被哪个开关包裹；
   - 执行 `find ascendc/src/ops-transformer -name "test_*_tiling.cpp" | wc -l` 与 `find ascendc/src/ops-transformer -path "*ut/op_api*" -name "*.cpp" | wc -l`。
3. **需要观察的现象**：tiling 用例 17 个、op_api 用例 8 个（截至当前 HEAD），op_kernel 用例 0 个。
4. **预期结果**：你会得到一张「UT 类型 × 用例数 × 构建开关」的对照表，其中 op_kernel 一列的 0 是重要伏笔（4.5.3 节解释）。本实践为纯只读检索，无需 NPU。

#### 4.1.5 小练习与答案

**练习 1**：为什么 op_host UT 不需要 NPU 也能测 tiling？
**答案**：tiling 函数只做 Host 侧的「作战规划」——读 shape、查平台信息、算切分、写 TilingData（u2-l3），全程不触碰设备。平台信息（核数、UB 大小）由 faker 用伪造的 JSON 提供，因此宿主机即可完整执行。

**练习 2**：`build.sh -u` 与 `build.sh -u --ophost` 的区别是什么？
**答案**：`-u` 只打开 ENABLE_TEST；`set_ut_mode` 默认 `UT_TEST_ALL=TRUE`（先编 op_host 再编 op_api，见 build.sh L460-L478 的分支）；追加 `--ophost` 后 `OP_HOST_UT=TRUE`、`UT_TEST_ALL=FALSE`，只配置并构建 `transformer_op_host_ut` 一个目标，速度更快。

**练习 3**：`framework_normal/common` 目录没有 CMakeLists.txt，它怎么被编进构建？
**答案**：由 `cmake/ut.cmake` 的 `add_optiling_ut_modules` 等函数用 `file(GLOB ...)` 直接把 `${UT_COMMON_INC}/tiling_context_faker.cpp`、`tiling_case_executor.cpp` 收进公共 OBJECT 库（ut.cmake L40-L42），不走 add_subdirectory 机制。

### 4.2 op_host UT 之一：TilingContextPara 与 TilingContextFaker

#### 4.2.1 概念说明

写一个 tiling UT 用例，本质上是回答一个问题：「如果我给 tiling 函数喂这样一组输入描述和平台参数，它应该产出什么？」框架把这个问题拆成两个角色：

- **`TilingContextPara`（参数包）**：一个纯数据结构，用例作者填什么，faker 就伪造什么。它是「用例的规格」。
- **`TilingContextFaker`（伪造器）**：把参数包真正组装成 `gert::TilingContext` 的工人。它继承 CANN 的 `OpTilingContextBuilder`，提供链式 API，最终 `Build()` 出一个 `ContextHolder<TilingContext>`。

用例作者通常只直接接触 `TilingContextPara`；faker 由执行器（4.3）内部驱动。

#### 4.2.2 核心流程

`TilingContextPara` 的内容五组：

```text
① 算子身份：opName_（必须是 _def.cpp 注册的类名，如 "AiInfraAggregateHidden"）
② 张量描述：inputTensorDesc_ / outputTensorDesc_（每个元素是 TensorDescription）
③ 属性：attrs_（OpAttr = 属性名 + AnyValue 万能值）
④ 平台参数：socVersion_（默认 "Ascend910B"）、coreNum_（默认 64）、ubSize_（默认 262144）、
   tilingDataSize_（默认 4096，即伪造的 TilingData 缓冲容量）、socInfoString_（整段自定义平台 JSON）
⑤ 附加项：compileInfo_（编译信息指针，对应 tiling 侧的 ParseCompileInfo 产物）、
   inputInstanceNum_/outputInstanceNum_（动态输入输出的实例数）、deterministicInfo_（确定性开关）
```

`TilingContextFaker` 的链式调用序列（由执行器统一发起）：

```text
SetOpType(opName)                      # 绑定算子类型（查注册表的键）
  .IrInstanceNum(in, out)              # 声明每个 IR 端口的实例数（1=有张量，0=可选口缺席）
  .InputTensors(...) .OutputTensors(...)  # 挂上宿主机侧的假张量
  .Attr(name, value)                   # 逐个追加属性（8 种类型重载）
  .CompileInfo(ptr)                    # 挂编译信息
  .PlatformInfo(&platformInfo)         # 挂平台信息（关键！见 4.3）
  .TilingData(buf) .Workspace(ws)      # 挂输出缓冲（tiling 函数往这里写）
  .Build()                             # 产出 ContextHolder<TilingContext>
```

#### 4.2.3 源码精读

`TensorDescription` 是对「一个张量长什么样」的最小描述：[ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h:L23-L39](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h#L23-L39) 定义了 shape（`gert::StorageShape`，含原始/存储两套形状）、dtype、format、isConst/constValue（常量张量可带真值）、isTensorV2 五个字段。用例里 `{{{S,B,H},{S,B,H}}, ge::DT_BF16, ge::FORMAT_ND}` 这样的字面量就是在构造它。

`TilingContextPara` 主体与平台默认值：[ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h:L173-L187](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h#L173-L187) 列出全部成员——注意三个默认值 `coreNum_=64`、`ubSize_=262144`（256 KB）、`tilingDataSize_=4096`。它们就是「伪造的 910B 平台」：如果你要测 A3（910_93），必须显式传 `socVersion="Ascend910_93"` 和对应核数。文件还提供了 6 个构造函数重载（L49-L171），覆盖「有无属性 × 有无动态实例数 × 有无确定性信息」的组合，方便用例按需简写。

`TilingContextFaker` 的链式 API：[ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h:L189-L261](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_context_faker.h#L189-L261)。其中 `NodeIoNum` 与 `IrInstanceNum` 注释明确标注二选一（L193-L199）；`Attr` 有 bool/int64/float/AscendString 及其 vector 共 8 个重载（L207-L243），全部转调 CANN 基类的 `AppendAttr`；`Build()`（L260）返回 `ContextHolder<TilingContext>`，它持有全部临时对象的生存期——这就是 faker 能安全返回裸指针 `TilingContext*` 的原因。

一个真实用例的参数包写法：[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp:L33-L58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L33-L58)。这条用例声明：算子 `AiInfraAggregateHidden`，两个输入（`[4096,4,768]` 的 BF16 input 与 `[3,768]` 的 BF16 weight）、一个输出、无属性、空属性表 `{}`、挂一个零值初始化的 `CompileInfo`。反例则见 L177-L197 的 `emptyInput` 用例：输入 shape 传 `{}`，期望 tiling 返回 `GRAPH_FAILED`。

#### 4.2.4 代码实践

1. **实践目标**：亲手为一个熟悉的算子写参数包，体会「用例即数据」。
2. **操作步骤**（纯源码阅读 + 手写代码，无需环境）：
   - 打开 `ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_def.cpp`，数出必选输入/输出个数；
   - 仿照 aggregate_hidden 用例，在草稿上为 `AiInfraSinkhornGrad` 写一个最小 `TilingContextPara`（先不追求正确，只求端口数对齐）。
3. **需要观察的现象**：数端口时要区分 REQUIRED 与 OPTIONAL——可选输入在参数包里也占一个 `TensorDescription` 位置，但维度数传 0 即表示「缺席」（执行器把 dimNum==0 的口映射为实例数 0，见 4.3.3）。
4. **预期结果**：写出一份与 def 声明顺序严格一致的 TensorDescription 列表。**待本地验证**：端口顺序与个数是否正确，需在 u8-l2 实际跑通 UT 时确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `opName_` 必须填 `_def.cpp` 里的类名（如 `AiInfraAggregateHidden`）而不是目录名（`ai_infra_aggregate_hidden`）？
**答案**：执行器最终用这个名字去 `OpImplSpaceRegistryV2` 查注册表拿 tiling 函数，而注册表键是 `OP_ADD(类名)` 登记的原型类名（u2-l2）。填目录名会查不到实现，直接空指针崩溃。

**练习 2**：想模拟「A3 芯片、50 个 AIV 核、UB 1MB」，参数包该怎么传？
**答案**：构造 `TilingContextPara` 时显式传 `socVersion="Ascend910_93"`、`coreNum=50`、`ubSize=1048576`（或干脆用 `socInfoString` 传整段平台 JSON 覆盖默认拼装，见 4.3.3 的 socInfoString 分支）。默认值只对应 910B。

**练习 3**：`ContextHolder<TilingContext>` 为什么必须由 faker 返回，而不能让执行器自己 new 一个 TilingContext？
**答案**：TilingContext 内部引用大量临时对象（Tensor、StorageShape、属性数组、平台信息缓冲）。ContextHolder 统一持有这些对象的生存期，保证 context 在整个用例期间有效；手工 new 无法保证内部指针不悬垂。

### 4.3 op_host UT 之二：DO_TILING 执行器与断言契约

#### 4.3.1 概念说明

执行器（executor）是连接「参数包」与「断言」的中枢，对外只有两个函数：

- `ExecuteTestCase(para, expectResult, expectTilingKey, expectTilingData, expectWorkspaces, ...)`：执行 + 断言，用例 90% 场景用它；
- `ExecuteTiling(para, tilingInfo)`：只执行并把结果（tilingKey、blockDim、workspace、TilingData 字节流）拷出来交给调用者自己判——op_kernel UT 与需要二次加工的用例用它。

两个函数共享同一个 `DO_TILING` 宏完成真正的执行，这个宏是整个 op_host UT 框架的心脏。

#### 4.3.2 核心流程

`DO_TILING` 的八步流水：

```text
① 遍历输入描述：dimNum==0 → 该口实例数置 0（可选口缺席）；
   否则造一个 kOnHost 的 gert::Tensor（V1/V2 按标志选择），放入 keepAlive 容器
② 同法处理输出描述
③ 组装 TilingContextFaker：IrInstanceNum + InputTensors/OutputTensors + DeterministicInfo
   + 逐个 Attr（按 AnyValue 的 8 种 ValueType 分派到对应重载）
④ 造输出缓冲：TilingData::CreateCap(tilingDataSize) 与 4096 项的 workspace 连续向量
⑤ faker.SetOpType(opName).CompileInfo(...).PlatformInfo(...).TilingData(...).Workspace(...).Build()
⑥ 伪造平台 JSON：{"hardware_info": {"UB_SIZE": ubSize, "CORE_NUM": coreNum, "socVersion": ...}}
   → GetPlatFormInfos 解析成 socInfos/aicoreSpec/intrinsics 三张表
   → tilingContext->GetPlatformInfo()->SetPlatformRes(...) 逐张塞入
⑦ spaceRegistry->GetOpImpl(opName)->tiling 取出 tiling 函数
⑧ tilingFunc(tilingContext) 真正执行
```

随后 `ExecuteTestCase` 按序断言：返回值 → workspace 逐项 → tilingKey → （可选）TilingData 字节流比对。

#### 4.3.3 源码精读

`TilingInfo` 结果结构与两个入口签名：[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.h:L18-L34](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.h#L18-L34) 中 `TilingInfo` 收纳 tilingKey、workspaceSizes、tilingData 字节流、blockNum 四件套；`ExecuteTestCase` 的默认期望值是 `GRAPH_FAILED`（第二参数缺省值）——这个缺省提醒你：不传期望就是「期望失败」，写正例时千万别漏传 `ge::GRAPH_SUCCESS`。

假张量的构造与缺席判定：[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp:L31-L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L31-L63)。每个输入按 `dimNum()==0` 判缺席；在场则构造 `TensorPlacement::kOnHost` 的张量，常量张量还可挂 `constValue` 真值；`unique_ptr` 的 keepAlive 容器保证 Build 之后指针不失效。

属性分派：[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp:L91-L120](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L91-L120) 按 `AnyValue::ValueType` 的 8 个枚举（VT_BOOL/VT_INT/VT_FLOAT/VT_STRING 及三个 LIST 变体）switch 到 faker 对应的 `Attr` 重载——这就是 u2-l3 里 tiling 侧 `GetAttrs` 能按类型取到值的另一半保障。

**伪造平台的核心戏法**：[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp:L133-L154](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L133-L154)。执行器用字符串模板拼出一段 JSON（UB_SIZE 取自 `para.ubSize_`、CORE_NUM 取自 `para.coreNum_`、socVersion 取自 `para.socVersion_`；若用例给了 `socInfoString_` 则整段覆盖），交给 `GetPlatFormInfos` 解析成三张 `map<string,string>` 后，逐张 `SetPlatformRes` 塞进 context 的平台信息里。tiling 代码里的 `GetCoreCount()`、`GetPlatformInfo()->GetSOCVersion()` 由此全部得到伪造值。字段映射表在 [L191-L253](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L191-L253)：`ai_core_cnt←CORE_NUM`、`ub_size←UB_SIZE`、`l1_size←L1_SIZE` 等；解析失败时回落到内置的 `default_hardward_info`（32 核、256KB UB）。

查表执行：[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp:L155-L160](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L155-L160) 从全局默认注册表按算子名取 `opImpl->tiling` 函数指针并调用——这个注册表正是 4.4.2 里 main 环境预加载 .so 填充的。

断言四连：[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp:L255-L294](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L255-L294)。注意三个细节：(a) 期望失败时直接 return，不再检查后续三项（失败路径本来就没写这些输出）；(b) workspace 逐项 `ASSERT_EQ`；(c) TilingData 比对支持 `*` 通配——[L162-L189](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L162-L189) 的 `to_string<T>` 与 `GetMask` 把字节流按类型格式化成空格分隔的字符串，期望串里写 `*` 的位置跳过比对（用于「这个字段随平台变，别较真」的场景）。

`ExecuteTiling` 的无断言版：[ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp:L296-L319](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/common/tiling_case_executor.cpp#L296-L319) 把 RawTilingData 整块 `memcpy` 进 `unique_ptr<uint8_t[]>`，把 blockDim 采进 `blockNum`——op_kernel UT 正是拿这份字节流回放给 CPU 模拟器上的 kernel（对应 u3-l4 讲过的「切分缓存回放」能力）。

#### 4.3.4 代码实践

1. **实践目标**：把「参数包 → 执行 → 断言」的链条在真实用例上走一遍。
2. **操作步骤**：
   - 打开 [test_ai_infra_aggregate_hidden_tiling.cpp:L177-L197](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp#L177-L197)（emptyInput 反例）；
   - 逐行标注：shape `{}` 如何在 DO_TILING 第①步被判缺席 → tiling 侧 `CheckInputValid` 的哪个 `OP_CHECK_IF` 命中（对照 u2-l3 的 ai_infra_aggregate_hidden_tiling.cpp）→ 返回 `GRAPH_FAILED` → `ExecuteTestCase` 第二参数命中断言。
3. **需要观察的现象**：反例用例仍然传了 `expectWorkspaces={0}` 与 `expectTilingKey=0`，但断言不会走到它们——体会「期望失败即短路」的设计。
4. **预期结果**：你能不看讲义复述出八步流水中该反例分别命中第①步与断言第一关。**待本地验证**：具体命中哪条 OP_CHECK_IF 需对照 tiling 源码行号（u2-l3 已精读，可回查）。

#### 4.3.5 小练习与答案

**练习 1**：`ExecuteTestCase` 的期望 TilingData 串 `"1 2 * 4"` 是什么含义？
**答案**：按调用时传入的 `tilingData2StrFunc`（一般按 TilingData 结构体首字段类型格式化）把实际字节流转成字符串后，前两个元素必须等于 1、2，第三个任意（`*` 通配，由 GetMask 收集跳过位），第四个等于 4。

**练习 2**：为什么需要 `ExecuteTiling` 这个「不assert」的版本？
**答案**：两类场景：(1) op_kernel UT 需要 TilingData 字节流作为 kernel 输入，不需要断言 tiling 本身；(2) 用例想对结果做非等值判断（如「blockDim 不超过核数」）时，拿 TilingInfo 自己写 EXPECT。

**练习 3**：伪造平台 JSON 时 `socInfoString` 与 `ubSize/coreNum` 参数谁优先？
**答案**：`socInfoString_` 非空则整段替换拼接结果（tiling_case_executor.cpp L141-L143），即 socInfoString 优先；两者都不传时用默认 64 核 / 256KB UB / Ascend910B。

### 4.4 从 main 到断言：tiling UT 的聚合与 CMake 挂接

#### 4.4.1 概念说明

前两节解决了「单个用例怎么跑」，本节回答「全仓库 17 个 tiling 用例文件怎么变成一个可执行程序」。关键在于理解一个看似矛盾的事实：

> **`transformer_op_host_ut` 的 main 函数里一行用例代码都没有。**

用例是通过静态库整体链接进可执行文件的，而「算子的 tiling 函数」则根本不在可执行文件里——它在独立的 `libophost_transformer_ut.so` 中，由 main 的全局环境在启动时动态加载注册。这就是「聚合」的全部秘密。

#### 4.4.2 核心流程

一次 `./transformer_op_host_ut` 的完整时间线：

```text
T0  main()：InitGoogleTest + AddGlobalTestEnvironment(new OpHostUtEnvironment)
T1  gtest 调 OpHostUtEnvironment::SetUp()
    ├─ 设置 fe::PlatformInfoManager 的 OptionalInfos（soc_version 占位）
    ├─ 读环境变量 BUILD_PATH
    ├─ dlopen libophost_transformer_ut.so 并加入 OpImplSpaceRegistryV2 注册表
    └─ 设为 DefaultOpImplSpaceRegistryV2（此后执行器第⑦步查的就是它）
T2  RUN_ALL_TESTS()：gtest 逐个调度已链接进来的 TEST_F 用例
T3  每条用例：构造 TilingContextPara → ExecuteTestCase → DO_TILING ①~⑧ → 四连断言
T4  TearDown：ClearSpaceRegistry
```

#### 4.4.3 源码精读

**main 与全局环境**：[ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp:L17-L51](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp#L17-L51)。`SetUp` 先给 `fe::PlatformInfoManager` 塞一个占位的 soc_version 编译信息（L23-L26，满足部分 tiling 代码对 fe 平台缓存的依赖）；随后读 `BUILD_PATH` 环境变量拼出 so 路径 `libophost_transformer_ut.so`（L29-L35），把它包装成 `OppSoDesc` 加入新建的 `OpImplSpaceRegistryV2` 并设为默认注册表（L36-L43）。so 加载时，其中所有 `IMPL_OP_OPTILING` 注册的函数（u2-l3）随静态初始化进入注册表——这就是「环境替 main 完成聚合」的机制。[L53-L57](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp#L53-L57) 是标准的 gtest main 三行。

**op_host/CMakeLists 的三个产物**：[ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt:L13-L46](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt#L13-L46) 依次产出：(a) `add_optiling_ut_modules` 建立的「公共件 OBJECT 库 + 用例 OBJECT 库 + 用例静态库」三件套；(b) `ophost_transformer_ut` SHARED 库——由 `op_host_aclnn`、`opsproto`、`optiling` 三个目标的对象文件拼成，即「全部算子的 def+tiling 源码」；(c) 可执行 `transformer_op_host_ut`（test_op_host_main.cpp）。链接期的关键在 [L56-L73](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt#L56-L73)：`-Wl,--whole-archive` 包住 `${OP_TILING_MODULE_NAME}_cases` 与 infershape cases——**必须 whole-archive，否则链接器会发现没有任何人引用这些 TEST_F 生成的符号而把整块静态库丢弃**，这是 gtest 静态注册模式的标配。[L108-L115](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt#L108-L115) 显示 `ENABLE_UT_EXEC` 开启时构建完自动运行该可执行文件（构建即测试）。

**用例如何从算子目录流进用例库**——挂接链共六站（这是综合实践的答案骨架）：

1. 算子根 CMakeLists 决定 tests 目录是否入图：[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/CMakeLists.txt:L11-L19](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/CMakeLists.txt#L11-L19) 中 `if(NOT ENABLE_TEST AND NOT BENCHMARK) list(REMOVE_ITEM CURRENT_DIRS tests)`——不跑 UT 时 tests 整个被剔除。
2. 算子 `tests/ut/CMakeLists.txt` 递归子目录：[L10-L19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/CMakeLists.txt#L10-L19)。
3. 算子 `tests/ut/op_host/CMakeLists.txt` 报到：[L10-L13](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/CMakeLists.txt#L10-L13) 两行 `add_modules_ut_sources` 分别把本目录登记给 tiling 模块与 infershape 模块。
4. `add_modules_ut_sources` 的登记逻辑：[ascendc/cmake/ut.cmake:L196-L218](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L196-L218)。它先从 `DIR` 向上走三级目录反推算子目录名（L203-L206），再用 `-n` 传进来的白名单 `ASCEND_OP_NAME` 过滤（L207-L211：不在白名单直接 return），最后 **`file(GLOB ... test_*_tiling.cpp)`** 把用例源码塞进 `cases_obj`（L216-L217）。文件名约定 `test_<算子>_tiling.cpp` 在这里是硬性匹配条件。infershape 与 op_api 的对应分支在 [L220-L261](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L220-L261)（分别 glob `test_*_infershape.cpp` 与 `test_aclnn_*.cpp`）。
5. 用例 OBJECT 库的种子与依赖：[ascendc/cmake/ut.cmake:L52-L75](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L52-L75)。`cases_obj` 初始只有一个占位的 `empty.cpp`（`UT_PATH/empty.cpp`，L54）——OBJECT 库创建时必须有至少一个源文件，随后各算子报到时往里 `target_sources` 追加；公共件（faker/executor 的 cpp）则被 glob 进 `common_obj`（L40-L42）。两者合成静态库 `${OP_TILING_MODULE_NAME}_cases`（L72-L75）。
6. 静态库 whole-archive 链入可执行（见上文 op_host/CMakeLists L61-L64）。

#### 4.4.4 代码实践（对应本讲 practice_task 的前半）

1. **实践目标**：画出 UT 框架类图/组装图，并能口头复述聚合机制。
2. **操作步骤**：
   - 画一张两部分图。**运行期类图**：`OpHostUtEnvironment`（testing::Environment）→ 持有 `OpImplSpaceRegistryV2`（内含全部算子的 `opImpl->tiling` 函数指针）；`TilingContextPara` --组装--> `TilingContextFaker`（继承 OpTilingContextBuilder）--Build()--> `ContextHolder<TilingContext>`；`ExecuteTestCase`/`ExecuteTiling` --使用--> 前三者；`TEST_F 用例` --调用--> ExecuteTestCase。
   - **构建期组装图**：17 个 `test_*_tiling.cpp` --glob(add_modules_ut_sources)--> `cases_obj` --+common_obj--> 静态库 --whole-archive--> `transformer_op_host_ut`；同时算子 def/tiling 源码 --> `libophost_transformer_ut.so` --dlopen(环境 SetUp)--> 注册表。
   - 验证你的图：`grep -rn "whole-archive" ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt`；`grep -rn "empty.cpp" ascendc/cmake/ut.cmake`。
3. **需要观察的现象**：两个 grep 分别命中链接 whole-archive 行与 OBJECT 库种子行，与你图上的两条边一一对应。
4. **预期结果**：得到可放进学习笔记的一页图。本实践纯只读，无需 NPU。

#### 4.4.5 小练习与答案

**练习 1**：为什么链接用例静态库必须 `-Wl,--whole-archive`？
**答案**：TEST_F 宏生成的用例类没有任何外部调用者，靠静态初始化向 gtest 注册。普通链接规则会因「无引用」丢弃这些目标文件，导致 0 条用例被收集；whole-archive 强制全量纳入。

**练习 2**：算子的 tiling 函数明明也在仓库里编译，为什么不直接链进可执行文件，而要绕道 .so + dlopen？
**答案**：可执行文件与算子宿主库的构建节奏不同——后者（ophost_transformer_ut.so）由 op_host_aclnn/opsproto/optiling 目标组装，模拟的是真实部署形态（tiling 以 so 形式装载，u1-l4 的 run 包同构）。dlopen 时 so 内 `IMPL_OP_OPTILING` 的静态注册自然生效，main 无需感知任何具体算子，实现「加算子零改动框架」。

**练习 3**：新算子 `ai_infra_scale_mul` 加了 `tests/ut/op_host/test_ai_infra_scale_mul_tiling.cpp`，但忘了给 `tests/ut/op_host/CMakeLists.txt` 写 `add_modules_ut_sources`，症状是什么？
**答案**：编译通过但该文件根本没被 glob 到（glob 的 DIR 参数来自 CMakeLists 的报到调用），可执行里没有这条用例——运行输出少一条用例计数，且不会报错。这是 UT 静默丢失的典型事故。

### 4.5 op_api UT 与 op_kernel UT 的公共设施

#### 4.5.1 概念说明

**op_api UT** 验证 aclnn 两段式接口的第一段（GetWorkspaceSize，u2-l5）：参数校验、空指针防御、executor 组装是否正确。框架公共库 `op_api_ut_common` 提供三件东西：张量描述符（`TensorDesc`/`ScalarDesc`/`ArrayDesc`，声明式造参数）、`OpApiUt` 模板类（驱动两段调用与可选精度比对）、`OP_API_UT` 宏（一行声明一个用例对象）。

**op_kernel UT** 的设计意图是：在 CANN 的 CPU 指令模拟器（tikicpulib）上直接跑设备侧 Kernel。框架为它准备了 `data_utils`（输入/输出二进制文件读写）、tiling 头文件生成脚本（把宿主 tiling 结果转成 kernel 能 include 的头）和自己的 main。**但需要诚实标注：当前仓库内没有任何算子目录包含 `tests/ut/op_kernel` 用例**——`ut.cmake` 里专门为此准备的 `AddOpTestCase` 函数（ut.cmake L270-L390）在全仓库无调用者，属于「备而未用」的基础设施（与 u3-l3 对 tiling_util 的考察结论同类）。因此本节以理解机制为主。

#### 4.5.2 核心流程

`OpApiUt::TestPrecision`（完整精度流）的七步：

```text
① ToJsonFile：把输入/输出描述符序列化成用例 JSON
② GenerateInput：调 python 脚本按 JSON 造随机输入（ValueRange 可控）
③ GenerateGolden：若存在 golden/<op>.py 则生成参考输出
④ GetWorkspaceSize：执行 aclnn 第一段，拿 workspace 与 executor
⑤ api_func_：执行第二段（下发到设备）
⑥ SaveResultFromDevice：取回输出
⑦ CompareGolden：python 比对精度
```

本仓库的实际用法只走到第 ④ 步的变体：8 个 op_api 用例文件全部调用 `TestGetWorkspaceSizeWithNNopbaseInner`（只跑第一段 + 断言返回码），不执行设备计算——印证 u3-l4 的结论「桩使 UT 无硬件可测，但只验证流程与分支，数值精度归 ST」。

#### 4.5.3 源码精读

**声明式造参数**：以 FA 用例为例，[ascendc/src/ops-transformer/attention/flash_attention_score_enhance/tests/ut/op_api/test_aclnn_flash_attention_score_enhance.cpp:L41-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/tests/ut/op_api/test_aclnn_flash_attention_score_enhance.cpp#L41-L53) 用 `TensorDesc({256,2,192}, ACL_BF16, ACL_FORMAT_ND).ValueRange(-1,1)` 一行声明一个张量参数（可选参数直接写 `nullptr`），标量属性用普通 C++ 变量。这套描述符由 `op_api_ut_common/inc/op_api_ut_common/tensor_desc.h` 等文件提供。

**OP_API_UT 宏**：[ascendc/src/tests/ut/framework_normal/op_api/op_api_ut_common/inc/op_api_ut_common/op_api_ut.h:L355-L363](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/op_api_ut_common/inc/op_api_ut_common/op_api_ut.h#L355-L363)。`OP_API_UT(api, INPUT(...), OUTPUT(...))` 展开为 `OpApiUt` 对象：从 gtest 运行时取当前用例名、用 `#api` 字符串化算子名、并以 `api##GetWorkspaceSize` 与 `api` 两个函数指针构造——所以这个宏天然要求接口符合 aclnn 两段式命名约定（u2-l5）。`INPUT`/`OUTPUT` 只是 `make_tuple` 的别名，把异构参数打包成 tuple 供模板逐个转换。

**OpApiUt 模板类的两段驱动**：构造与资源释放见 [L151-L173](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/op_api_ut_common/inc/op_api_ut_common/op_api_ut.h#L151-L173)（析构统一 `ReleaseAclTypes` 释放转换出的 aclTensor 等句柄）；`TestGetWorkspaceSize` 见 [L180-L187](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/op_api_ut_common/inc/op_api_ut_common/op_api_ut.h#L180-L187)（执行第一段后顺手 delete executor）；完整精度流 `TestPrecision` 见 [L195-L239](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/op_api_ut_common/inc/op_api_ut_common/op_api_ut.h#L195-L239)——注意 L210-L214 的 `UT_SKIP_PRECISION` 环境变量可跳过设备段，L216 的 `MallocDeviceMemory` 表明完整流需要真实设备内存。tuple 的逐元素转换/落盘由文件头部的 `ReloadIterator`/`SaveIterator` 递归模板完成（[L33-L127](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/op_api_ut_common/inc/op_api_ut_common/op_api_ut.h#L33-L127)），这是 C++17 编译期遍历异构参数表的惯用法。

**stub 生成脚本**：op_api 可执行在链接期多出一个生成源 `runtime_stubs.cpp`——由 [ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt:L63-L83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt#L63-L83) 的 custom_command 调 [generate_opapi_stub.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/scripts/generate_opapi_stub.py#L22-L80) 生成，脚本会读取 CANN 的 `binary_info_config.json` 并为每个算子注入打桩 `.o/.json` 条目（L52-L80），运行结束后再由 `clean_opapi_stub.py` 清理（op_api/CMakeLists L155、L165）——这正是 u3-l4「运行期假实现冒充 rt* 接口」的落地现场。op_api 的 main 与 op_host 的同构但更薄（环境只打日志）：[test_op_api_main.cpp:L33-L38](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/test_op_api_main.cpp#L33-L38)。

**op_kernel 侧**：[data_utils.h:L24-L29](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_kernel/data_utils.h#L24-L29) 只提供三个日志宏与 `ReadFile`/`WriteFile` 两个函数——CPU 模拟器跑 kernel 的输入输出以二进制文件为载体（`.bin` 造数 → ReadFile 进 buffer → kernel 计算 → WriteFile 回写 → 与期望 diff），实现在 [data_utils.cpp:L11-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_kernel/data_utils.cpp#L11-L46)。它的 main 与 op_host 的结构相同，但加载的是 `libtransformer_op_kernel_ut_tiling.so`（[test_op_kernel_main.cpp:L22-L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_kernel/test_op_kernel_main.cpp#L22-L37)）——先在宿主侧执行 tiling（复用 4.3 的 `ExecuteTiling`），再把 TilingData 喂给模拟器上的 kernel；配套脚本 [gen_tiling_head_file.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_kernel/scripts/gen_tiling_head_file.sh#L11-L16) 负责 source CANN 环境后调 python 把 tiling 结果转成头文件。而这一切的启用入口 `AddOpTestCase`（[ut.cmake:L270-L390](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L270-L390)，含「按 socVersion 逐芯片编 tiling 临时 so + 生成 tiling 头 + 用例 glob」全流程）当前无调用者。

#### 4.5.4 代码实践

1. **实践目标**：确认 op_api UT 在本仓库的真实测试深度，避免「以为测了精度」。
2. **操作步骤**：
   - `grep -rn "TestPrecision" ascendc/src/ops-transformer --include=*.cpp | wc -l`（预期 0）；
   - `grep -rln "TestGetWorkspaceSize" ascendc/src/ops-transformer --include=*.cpp`（预期 8 个文件）；
   - 任选一个命中文件，找到 `OP_API_UT(...)` 的 `INPUT(...)` 元组，与 [aclnn_flash_attention_score_enhance.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h#L1-L1) 的函数签名逐参数对个数。
3. **需要观察的现象**：INPUT 元组的元素个数（含 nullptr 占位）应与 aclnn 函数形参个数一致——顺序契约跨层成立（u6-l2 讲过的「参数顺序契约」在 UT 里同样咬合）。
4. **预期结果**：得出结论「本仓库 op_api UT = GetWorkspaceSize 阶段的流程与防御测试」。纯只读，无需环境。

#### 4.5.5 小练习与答案

**练习 1**：`OP_API_UT` 宏里为什么能用 `api##GetWorkspaceSize` 拼出第一段函数？
**答案**：aclnn 两段式接口有固定命名约定：`aclnnXxx` 与 `aclnnXxxGetWorkspaceSize` 成对（u2-l5）。宏的 token 拼接依赖这一约定，接口不守约宏就拼不出符号，编译期即报错。

**练习 2**：op_api UT 的 `TestPrecision` 与 ST 测试（u8-l3）都做数值比对，区别是什么？
**答案**：TestPrecision 在同一个 C++ 用例内完成「造数→调用→取回→python golden 比对」，面向单接口、依赖 golden 脚本目录，且需真实设备内存；ST 走 pytest 体系，用 torch_npu 高层封装与 CPU float64 参考对打，面向精度等级判定（MARE/MERE/RMSE）。当前仓库 op_api UT 均未启用 TestPrecision。

**练习 3**：`AddOpTestCase` 全仓库无调用者说明了什么工程态度？
**答案**：框架与用例是解耦的两层——框架可以先行铺路（op_kernel CPU 模拟测试的全套管线已就绪），用例按需补齐。阅读公共库时不能默认「存在的基建都在用」，必须像 u3-l3 那样用 grep 核实真实调用关系。

## 5. 综合实践

**任务：产出一份《新增算子 UT 挂接手册》，并画出 UT 框架总装图。**（本讲 practice_task 的完整版）

假设你要给综合实战算子 `ai_infra_scale_mul`（u9-l4）补 op_host UT，请完成：

1. **画总装图**（mermaid 或手绘均可），必须包含四条边：
   - `17 个 test_*_tiling.cpp` --glob--> `transformer_op_tiling_ut_cases 静态库` --whole-archive--> `transformer_op_host_ut`；
   - `算子 def/tiling 源码` --> `libophost_transformer_ut.so` --dlopen(OpHostUtEnvironment::SetUp)--> `OpImplSpaceRegistryV2`；
   - `TilingContextPara` --> `TilingContextFaker` --Build--> `TilingContext` --> `tilingFunc 执行` --> `四连断言`；
   - `build.sh -u --ophost` --> `ENABLE_TEST/OP_HOST_UT` --> `add_subdirectory(framework_normal)`。
2. **写挂接清单**：按 4.4.3 的六站，列出新增算子需要创建/修改的每个文件与关键行内容：
   - 新建 `<op>/tests/ut/CMakeLists.txt`（照抄 aggregate_hidden 的递归模板）；
   - 新建 `<op>/tests/ut/op_host/CMakeLists.txt`（两行 `add_modules_ut_sources`，注意 `UT_NAME` 传 `${OP_TILING_MODULE_NAME}` 与 `${OP_INFERSHAPE_MODULE_NAME}`）；
   - 新建 `<op>/tests/ut/op_host/test_<op>_tiling.cpp`（文件名必须匹配 `test_*_tiling.cpp` glob）；
   - 无需改框架任何文件——验证「加算子零改动框架」的聚合设计。
3. **跑通验证**（有环境时）：`bash build.sh -u -n ai_infra_scale_mul -c ascend910_93 --ophost`，观察构建日志里 `Debug: CURRENT_DIRS` 递归信息与 `Run ops op_host utest` 的用例计数是否 ≥ 你写的用例数；**无 NPU 环境时**：写出清单与图即可，标注「待本地验证」。
4. **自查题**：如果把用例文件命名为 `test_scale_mul.cpp`（缺 `_tiling`），会发生什么？（答案见 4.4.5 练习 3 的同类机制：glob 不命中，用例静默丢失。）

## 6. 本讲小结

- 本仓库 UT 分三类：op_host UT 测 tiling/InferShape（无 NPU 可跑，17 个 tiling 用例文件）、op_api UT 测 aclnn 第一段（8 个用例，仅 GetWorkspaceSize 流程）、op_kernel UT 基建已备（AddOpTestCase）但当前无用例。
- **faker 伪造输入环境，stub 替换下游依赖**：`TilingContextPara` 是用例的数据规格（算子名、张量描述、属性、伪造平台参数），`TilingContextFaker` 继承 CANN builder 把它组装成真 context。
- 执行器 `DO_TILING` 的关键戏法是**伪造平台 JSON**：UB_SIZE/CORE_NUM/socVersion 拼成 JSON 解析后 `SetPlatformRes` 塞进平台信息，tiling 代码的核数与 UB 查询由此全部可控。
- `ExecuteTestCase` 断言四连（返回值→workspace→tilingKey→TilingData 串，支持 `*` 通配）；`ExecuteTiling` 是无断言版，供 op_kernel 回放 TilingData。
- 聚合机制：用例静态库必须 **whole-archive** 链入可执行；算子 tiling 函数走 **so + dlopen + 注册表**（main 的 Environment 完成），加算子对框架零改动；用例文件名 `test_*_tiling.cpp` 是 glob 的硬约定。
- 新增算子 UT 的挂接点共三处文件（tests/ut、tests/ut/op_host 两级 CMakeLists + 用例文件），并受 `-n` 白名单过滤。

## 7. 下一步学习建议

- **u8-l2（编写 Tiling 单元测试）**：拿本讲的参数包与断言契约，亲手为 aggregate_hidden 增补正反用例并编译运行——本讲 4.2/4.3 是它的全部前置。
- **u8-l4（构建与运行 UT/ST）**：把 4.1.2 的构建开关链扩到完整命令行层面，理解 `--ophost/--opapi` 与 pytest 两条测试链的配合。
- 回顾对照：u3-l4（stub 桩机制）与本讲 4.5.3 的 `generate_opapi_stub.py` 是同一件事的两面——建议重读该讲「四层替身」小结。
- 延伸阅读（源码）：`common/infer_shape_context_faker.h` 与 `infer_shape_case_executor.cpp` 是 tiling 框架的同构复刻，测 InferShape 时可对照自学；`common/kernel_run_context_holder.h` 展示了五种 context 的统一持有器写法。
