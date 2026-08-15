# 单元测试体系与 UT 框架

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 ops-transformer 中 ophost / opapi / opgraph / opkernel 四类单元测试（UT）分别验证哪一层交付件、分别用什么机制在 CPU 上跑起来。
2. 独立读懂并仿写 tiling UT、infershape UT、op_api UT 和 kernel UT 的用例，包括 `expectTilingData` 字符串、`*` 通配符、平台参数默认值这些容易踩坑的细节。
3. 掌握 `--ophost_test` / `--opapi_test` / `--opgraph_test` / `--opkernel_test` 与 `--ops`、`--soc`、`--noexec`、`--cov` 的组合用法。
4. 理解新旧两套 UT 框架（framework_normal / framework_special）是如何被自动判定的。
5. 看懂 `tests/test_config.yaml` 的字段语义，解释它在 CI「按变更文件裁剪测试」中的作用。

本讲承接 u6-l1「从零开发一个 AICore 算子」：那一讲我们开发了自己的算子 my_sum，这一讲为它补齐最小 UT 集，形成「写完即测」的闭环。

## 2. 前置知识

- **gtest**：Google 的 C++ 单元测试框架。用例通过 `TEST_F(测试类名, 用例名)` 宏声明，`EXPECT_EQ` / `ASSERT_EQ` 做断言（前者失败继续执行，后者失败立即终止当前用例）。本仓库所有 UT 都基于 gtest。
- **UT 与 ST 的关系**（承接 u3-l4）：UT（单元测试）在 **x86 CPU 上**直接编译运行被测代码，不需要 NPU 实机，是开发期的快速反馈环和 CI 门禁；ST / pytest / aclnn 调用验证则需要真机。官方开发指南明确写着「UT 验证无需 NPU 环境」。
- **faker（伪造上下文）与 executor（执行器）**：算子的 tiling / infershape 函数本来由 CANN 框架在真实执行流中调用，UT 框架用 `TilingContextFaker` 等类在内存里「伪造」出框架才会提供的上下文对象，再用 `ExecuteTestCase` 把被测函数跑起来并逐项比对结果。这是本仓库 host 侧 UT 的核心设计模式。
- **四层交付件回顾**（承接 u1-l2）：op_host（def / infershape / tiling）、op_api（aclnn 接口）、op_graph（proto / graph_infer / fusion_pass）、op_kernel（AscendC 核函数）。四类 UT 与这四层一一对应。
- **tiling data / tiling key 回顾**（承接 u2-l2、u2-l3）：tiling data 是 host 填、device 读的结构体「数据合同」；tiling key 是运行期路由到不同 kernel 二进制变体的整数。UT 中两者都要断言。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp` | tiling UT 标准样例（教学算子） |
| `examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp` | infershape UT 标准样例 |
| `examples/add_example/tests/ut/op_kernel/test_add_example.cpp` | kernel UT 标准样例（ICPU_RUN_KF） |
| `examples/add_example/tests/ut/op_kernel/CMakeLists.txt` | kernel UT 的 `AddOpTestCase` 接线 |
| `examples/add_example/tests/ut/op_host/CMakeLists.txt` | ophost UT 的 `add_modules_ut_sources` 接线 |
| `tests/ut/framework_normal/common/tiling_context_faker.h` | tiling UT 框架：上下文参数与默认平台信息 |
| `tests/ut/framework_normal/common/tiling_case_executor.cpp` | tiling UT 框架：执行与五项断言 |
| `tests/ut/framework_normal/common/infer_shape_case_executor.h` | infershape UT 框架接口 |
| `moe/moe_fused_topk/tests/ut/op_host/op_api/test_aclnn_moe_fused_topk.cpp` | op_api UT 工业样例 |
| `examples/add_example/tests/ut/op_kernel_aicpu/test_add_example.cpp` | AICPU UT 样例（RUN_KERNEL） |
| `cmake/ut.cmake` | 四类 UT 的源码收集规则（按文件名 GLOB） |
| `build.sh` | `--xxx_test` 选项解析、`set_ut_mode`、`build_ut` |
| `tests/test_config.yaml` | CI 按变更文件裁剪 UT / example 的配置表 |
| `docs/zh/develop/aicore_develop_guide.md` | 官方 UT 编写指南（455 行起「算子验证」章节） |

## 4. 核心概念与源码讲解

### 4.1 UT 框架全景与运行入口

#### 4.1.1 概念说明

本仓库的 UT 体系可以概括为「**四个测试目标 + 两套框架 + 一个构建入口**」：

- **四个测试目标**：`--ophost_test`、`--opapi_test`、`--opgraph_test`、`--opkernel_test` 分别编译并运行四个 cmake target——`transformer_op_host_ut`、`transformer_op_api_ut`、`transformer_op_graph_ut`、`transformer_op_kernel_ut`，对应算子的四层交付件。
- **两套框架**：`tests/ut/framework_normal`（新框架，绝大多数算子）和 `tests/ut/framework_special`（旧框架）。二者**不是按算子手动选择**的，而是 cmake 自动扫描每个算子 `tests/CMakeLists.txt` 的内容判定的（见 4.1.3）。
- **一个构建入口**：和编库、打包一样，全部经 `build.sh` 进入，最终落到 cmake target。

为什么 host 侧 UT 能不依赖 NPU？因为被测的 tiling / infershape / op_api 校验逻辑本来就是 **host 侧的纯 C++ 代码**，只要把依赖的运行时接口（平台信息查询、acl 运行时）替换成内存中的假对象或 stub，就能在 x86 上原样执行。

#### 4.1.2 核心流程

一次 `bash build.sh --ophost_test --ops=add_example --noexec` 的完整链路：

```text
build.sh 参数解析
  --ophost_test  → ENABLE_TEST=TRUE, OP_HOST=TRUE        （选项翻译）
  --ops=add_example → ascend_op_name="add_example"        （裁剪范围）
  --noexec       → 只编译不执行
        ↓
set_ut_mode()                                              （build.sh:1150）
  UT_TEST_ALL=FALSE, OP_HOST_UT=TRUE
  UT_TARGETS += transformer_op_host_ut
        ↓
cmake_config -DENABLE_TEST=TRUE ...                        （build/ 下配置）
  CMake 侧打开 add_modules_ut_sources / AddOpTestCase 收集逻辑
        ↓
build_ut()                                                 （build.sh:2092）
  依次构建 UT_TARGETS 中真实存在的 target
  （UTEST_FRAMEWORK_OLD 的旧 target 先编，再编新框架 target）
        ↓
若未指定 --noexec：运行测试二进制并输出 gtest 结果
```

#### 4.1.3 源码精读

**① 选项解析**——`--ophost_test` 本质是「打开测试模式 + 打开 ophost 维度」两个开关的组合：

[build.sh:1836-1854](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1836-L1854) 中四个 `--xxx_test` 分支都先置 `ENABLE_TEST=TRUE`，再置对应层的 `OP_HOST` / `OP_API` / `OP_GRAPH` / `OP_KERNEL` 为 TRUE。帮助文本在 [build.sh:150-155](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L150-L155) 列出了四个测试选项及等价写法（`--ophost -u` 等同 `--ophost_test`）。

**② set_ut_mode 决定要构建哪些 target**：

[build.sh:1150-1203](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1150-L1203) 是测试模式的核心决策函数，几个关键行为：

- 传了任何 `--xxx_test` 就把 `UT_TEST_ALL` 置 FALSE，只构建显式选择的层；什么都不传直接 `-u` 则全量。
- [build.sh:1199-1202](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1199-L1202)：跑 opkernel UT 时**必须带 `--ops`**，否则打 Warning 并退化为只测默认的两个算子（`recurrent_gated_delta_rule,chunk_gated_delta_rule`）——因为 kernel UT 要按算子逐个编译 CPU 版核函数，无法便宜地全量跑。
- [build.sh:1155-1162](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1155-L1162)：CI 传入 `PR_CHANGED_FILES`（变更文件清单）时，强制只跑 ophost + opapi 两类 UT——这是 test_config.yaml 裁剪机制的入口（见 4.5）。

**③ 新旧框架自动判定**：

[cmake/func.cmake:186-194](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/func.cmake#L186-L194) 读取每个算子 `tests/CMakeLists.txt` 的文件内容，若匹配到 `OpsTest_Level2_AddOp` 字样则置 `UTEST_FRAMEWORK_OLD`（旧框架 framework_special），否则置 `UTEST_FRAMEWORK_NEW`（新框架 framework_normal）。也就是说：**决定用哪套框架的是 tests/CMakeLists.txt 里写了什么函数，而不是目录名**。[cmake/variables.cmake:82-87](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/variables.cmake#L82-L87) 把两个开关默认置 FALSE，并定义 `UT_PATH` 指向 framework_normal——新算子一律走新框架。

**④ build_ut 的双框架构建**：

[build.sh:2092-2121](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2092-L2121) 先 `cmake ${CUSTOM_OPTION} ..` 配置，再依次查询 `UTEST_FRAMEWORK_OLD/NEW` 两个缓存变量：旧框架编单一 target，新框架逐个检查 `UT_TARGETS` 里的 target 是否存在（`cmake --build . --target help | grep`），存在才编、不存在跳过——这解释了为什么某个算子没有 op_api 层时 `--opapi_test` 也不会报错。

**⑤ 框架公共库一览**：[tests/ut/framework_normal/common/](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/common) 目录下除了本讲精读的 tiling / infer_shape faker 与 executor，还有 `infer_datatype_context_faker`（graph_infer 用）、`mc2_tiling_case_executor`、`op_api_csv_case_loader`（CSV 批量用例）、`softmax_tiling_mocker` 等工具——写新 UT 前先来这里找现成积木。

#### 4.1.4 代码实践

**实践 A：跑通一次最小 ophost UT**

1. 实践目标：确认本机（无需 NPU，只需已安装 CANN toolkit 并 source 环境）能编译并运行 add_example 的 ophost UT。
2. 操作步骤：
   - `bash build.sh --help test` 查看 Test Options 分组（帮助分组用法见 [build.sh:140-161](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L140-L161)，`--help` 后跟分组名可看子帮助，如 `bash build.sh --help ophost_test`）。
   - `bash build.sh --ophost_test --ops=add_example` 编译并运行。
3. 需要观察的现象：日志中出现 `Start to build ut`、`Building target: transformer_op_host_ut.`，最后 gtest 输出 `[ PASSED ] 2 tests`（tiling 2 个用例）加 `[ PASSED ] 1 test`（infershape 1 个用例）。
4. 预期结果：全绿通过。若提示 googletest 缺失，回顾 u1-l3：UT 依赖需先执行 `install_deps.sh` 安装。
5. 若无法在本地运行（无 CANN 环境），标注：**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`bash build.sh --ophost -u --noexec --ops=add_example` 和 `bash build.sh --ophost_test --noexec --ops=add_example` 有区别吗？

**答案**：没有区别。`-u|--test` 只置 `ENABLE_TEST=TRUE`，配合已置位的 `OP_HOST=TRUE`，`set_ut_mode` 会推导出 `OP_HOST_UT=TRUE`，与 `--ophost_test`（同时置两个开关）殊途同归；`--noexec` 都表示只编译不执行。这是 build.sh 帮助文本中明示的等价写法。

**练习 2**：为什么跑 opkernel UT 时 build.sh 会警告「Please use --ops」？

**答案**：见 [build.sh:1199-1202](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1199-L1202)。kernel UT 走 `AddOpTestCase`，要为每个算子把 AscendC 核函数编译成 CPU 可执行版本并链接 tikicpulib，代价高，无法全仓全量跑；不带 `--ops` 时退化为只测两个默认算子。

### 4.2 op_host UT：tiling 与 infershape 用例精读

#### 4.2.1 概念说明

op_host UT 验证宿主侧两个「纯函数」：

- **tiling UT**：给定一组输入张量描述（shape/dtype/format）和属性，tiling 函数能否成功执行，并且产出的 **tiling key、tiling data、workspace 大小**与预期一致。它守护的是 u2-l2 所说的「执行计划」。
- **infershape UT**：给定输入 shape，输出 shape 推导是否正确，尤其要覆盖含 `-1` 的动态 shape。它守护的是「动态量尺」。

二者共用同一套「faker + executor」模式，且都放在算子的 `tests/ut/op_host/` 目录下，靠**文件名约定**被收集：`test_*_tiling.cpp` 与 `test_*_infershape.cpp`（见 4.2.3 ⑤）。

#### 4.2.2 核心流程

一个 tiling 用例的三段式：

```text
1. 构造上下文 TilingContextPara(op名, {输入描述}, {输出描述}, {属性}, &compileInfo)
   —— TensorDescription = {{{originShape}, {storageShape}}, dtype, format}
2. 声明预期：expectTilingKey / expectTilingData（空格分隔的字符串）/ expectWorkspaces
3. ExecuteTestCase(...) 依次断言：
   ① tiling 返回值 == expectResult
   ② workspace 各段大小 == expectWorkspaces
   ③ tilingKey == expectTilingKey（传 UINT64_MAX 可跳过）
   ④ tiling data 按 int64 逐字段转成字符串后 == expectTilingData（'*' 为通配）
```

infershape 用例同构，只是断言换成输出 shape 列表。

#### 4.2.3 源码精读

**① tiling UT 全貌**：

[examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp:35-56](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp#L35-L56) 是 fp32 用例：输入两个 `{1, 2, 8, 16}` 的 FLOAT 张量，期望 `expectTilingKey = 0`、`expectTilingData = "256 8 "`、`expectWorkspaces = {1024*1024*16}`。`256` 是元素总数（1×2×8×16），对应 tiling data 结构体的第一个成员。

[examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp:58-79](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp#L58-L79) 是 int32 用例，唯一差别是 dtype 换成 `DT_INT32` 后 `expectTilingKey = 1`——这直接印证了 u2-l2 的结论：**tiling key 按 dtype 路由不同的 kernel 变体**。两个用例合起来就是在守护这条路由规则。

**② expectTilingData 字符串与 tiling data 结构体的对应**：

[examples/add_example/op_kernel/add_example_tiling_data.h:19-22](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L22) 定义 `AddExampleTilingData { int64_t totalLength; int64_t tileNum; }`。executor 会把 raw tiling data 按 `int64` 逐个转成十进制、空格拼接后与期望字符串比对，所以 `"256 8 "` 就是 `totalLength=256, tileNum=8`。**成员声明顺序 = 字符串顺序**，写用例时必须对着结构体逐字段核对。

**③ 断言执行器与两个实用技巧**：

[tests/ut/framework_normal/common/tiling_case_executor.cpp:326-370](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/common/tiling_case_executor.cpp#L326-L370) 是 `ExecuteTestCase` 的实现，其中：
- [第 351-355 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/common/tiling_case_executor.cpp#L351-L355)：`expectTilingKey == UINT64_MAX` 时跳过 key 校验——写用例初期还不确定 key 值时可以先用它。
- [第 240-252 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/common/tiling_case_executor.cpp#L240-L252) 的 `GetMask`：期望字符串里写 `'*'` 的字段不参与比对（如 `"2 64 * "` 表示第三个字段任意）——对「与平台相关的字段」非常实用。

接口声明在 [tests/ut/framework_normal/common/tiling_case_executor.h:26-31](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/common/tiling_case_executor.h#L26-L31)，其中 `ExecuteTiling` 变体只执行不比对、把结果填进 `TilingInfo` 返回——kernel UT 可以用它自动生成 tiling 数据（见 4.4）。

**④ 平台参数默认值（重要！）**：

[tests/ut/framework_normal/common/tiling_context_faker.h:52-60](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/common/tiling_context_faker.h#L52-L60) 构造函数的默认平台信息是 `socVersion="Ascend910B"`、`coreNum=64`、`ubSize=262144`（256KB）、`tilingDataSize=4096`。你的 tiling 实现若依赖核数或 UB 大小做切分，期望值必须按这组默认值推算；要模拟别的 SoC 就显式传入（如 add_example 用例第 33 行的 `soc_version_infos` 变量所示，Short_SoC_version 为 Ascend910B）。`TensorDescription` / `OpAttr` 的定义在同文件 [第 22-49 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/common/tiling_context_faker.h#L22-L49)，其中 `isConst/constValue` 两个参数用于 def 中标记 `ValueDepend` 的输入——UT 必须为这类输入提供真实数据值。

**⑤ 用例如何被收集（命名是硬约定）**：

[examples/add_example/tests/ut/op_host/CMakeLists.txt:11-14](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_host/CMakeLists.txt#L11-L14) 调用 `add_modules_ut_sources` 分别注册 tiling 与 infershape 用例目录。收集规则在 [cmake/ut.cmake:263-264](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/ut.cmake#L263-L264)（GLOB `test_*_tiling.cpp`）和 [cmake/ut.cmake:282-283](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/ut.cmake#L282-L283)（GLOB `test_*_infershape.cpp`）：文件名不对就不会被编译进 target，且不报任何错。另外 [cmake/ut.cmake:288-335](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/ut.cmake#L288-L335) 处理 `arch22/arch35` 等 SoC 架构子目录下的同名规则，并按 `--soc` 裁剪。

**⑥ infershape UT**：

[examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp:29-44](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp#L29-L44)：输入是含动态维的 `{{1, -1, -1, 64}}`，输出描述给空 shape `{{}, {}}`（表示「待推导」），期望输出 `{1, -1, -1, 64}`。executor 接口见 [tests/ut/framework_normal/common/infer_shape_case_executor.h:16-18](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/common/infer_shape_case_executor.h#L16-L18)。

**⑦ 官方编写指南**：[docs/zh/develop/aicore_develop_guide.md:547-623](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L547-L623) 给出了 tiling UT 的模板与「组织结构与命名建议」；[第 483-545 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L483-L545) 是 infershape UT 的对应章节。

#### 4.2.4 代码实践

**实践 B：预测并验证 expectTilingData**

1. 实践目标：建立「shape → tiling data 字符串」的手推能力。
2. 操作步骤：
   - 把 `test_add_example_tiling.cpp` 第一个用例的输入输出 shape 从 `{1, 2, 8, 16}` 改为 `{2, 4, 16, 32}`（同步改输入、输出、storageShape 四处），`expectTilingData` 先按你的推算改好。
   - 重新运行 `bash build.sh --ophost_test --ops=add_example`。
3. 需要观察的现象：gtest 对 tiling data 的比对结果。
4. 预期结果：元素总数 2×4×16×32 = 4096，add_example 的 tiling 把 totalLength 设为元素总数、tileNum 固定为 8，因此期望字符串应为 `"4096 8 "`，用例通过。若把期望写成 `"256 8 "` 会得到 `EXPECT_EQ` 失败并打印实际字符串，可据此校正。
5. 本实践需要本地 CANN 环境验证；无法运行时标注：**待本地验证**（推算过程本身不依赖环境）。

#### 4.2.5 小练习与答案

**练习 1**：某算子的 tiling data 结构体是 `{int64_t baseM; int64_t baseN; int64_t useBias;}`，tiling 实现里 baseM 依赖 UB 大小推算、另外两个字段确定。期望字符串怎么写最稳？

**答案**：利用通配符写成 `"* <baseN> <useBias> "`——`'*'` 位置的字段被 `GetMask` 标记后跳过比对（[tiling_case_executor.cpp:240-252](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/common/tiling_case_executor.cpp#L240-L252)），既避免硬编码平台相关值，又仍然锁定关键字段。

**练习 2**：tiling UT 中不传任何平台参数时，你的 tiling 函数看到的 coreNum / ubSize 是多少？来自哪里？

**答案**：coreNum=64、ubSize=262144 字节，来自 [tiling_context_faker.h:52-60](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/common/tiling_context_faker.h#L52-L60) 构造函数的默认参数（socVersion 默认 Ascend910B）。官方指南还提醒：UB 实际可用值会比指定值少 256 字节。

**练习 3**：为什么 add_example 的 tiling UT 要写 fp32 和 int32 两个几乎相同的用例？

**答案**：因为该算子的 tiling key 按 dtype 分支（fp32→0，int32→1），两个用例分别锁定两条路由，防止后续改 tiling 时破坏任一 dtype 的变体选择——tiling key 选错会导致运行期加载错误的 kernel 二进制。

### 4.3 op_api 与 op_graph UT

#### 4.3.1 概念说明

- **op_api UT** 验证 aclnn 接口的第一段 `GetWorkspaceSize`：参数校验、dtype/format 约束、infershape 与 tiling 的触发。它通过 `op_api_ut_common` 框架 + stub 掉的 acl 运行时（[tests/ut/framework_normal/op_api/stub](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/ut/framework_normal/op_api/stub)）在 CPU 上运行，**不需要真机**，正好覆盖 u3-l1 讲过的「校验漏斗」逻辑。
- **op_graph UT** 验证图模式交付件：以 `test_*_pass.cpp` 命名的 fusion pass 用例（承接 u6-l2 的 REG_FUSION_PASS）等。

#### 4.3.2 核心流程

op_api UT 的典型骨架（`OP_API_UT` 宏流）：

```text
SetUpTestCase: op::SetPlatformSocVersion(ASCEND910B)      // 指定模拟 SoC
用例内:
  1. TensorDesc(dims, dtype, format).ValueRange(lo, hi)   // 描述每个输入/输出
  2. OP_API_UT(aclnnXxx, INPUT(...), OUTPUT(...))          // 打包两段式调用
  3. ut.TestGetWorkspaceSize(&workspace_size)              // 只执行第一段
  4. EXPECT_EQ(aclRet, ACLNN_ERR_PARAM_INVALID / SUCCESS) // 断言返回码
```

#### 4.3.3 源码精读

**① 工业样例**（moe_fused_topk）：

[moe/moe_fused_topk/tests/ut/op_host/op_api/test_aclnn_moe_fused_topk.cpp:24-32](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_fused_topk/tests/ut/op_host/op_api/test_aclnn_moe_fused_topk.cpp#L24-L32) 在 `SetUpTestCase` 里调用 `op::SetPlatformSocVersion(op::SocVersion::ASCEND910B)` 设置模拟芯片型号。

[moe/moe_fused_topk/tests/ut/op_host/op_api/test_aclnn_moe_fused_topk.cpp:56-72](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_fused_topk/tests/ut/op_host/op_api/test_aclnn_moe_fused_topk.cpp#L56-L72) 是完整用例：`TensorDesc(...).ValueRange()` 声明张量并用随机区间自动造数；`OP_API_UT(aclnnMoeFusedTopk, INPUT(...), OUTPUT(...))` 把 aclnn 两段式接口打包成可执行对象；`ut.TestGetWorkspaceSize(&workspace_size)` 只跑第一段并断言返回码。该用例故意构造非法的 mapping_table shape，期望 `ACLNN_ERR_PARAM_INVALID`——**错误注入**是 op_api UT 的主力打法，与 u4-l2 讲的「校验漏斗」一一对应。

**② 目录位置的两种形态**：这个样例放在 `tests/ut/op_host/op_api/` 下而不是 `tests/ut/op_api/`，说明 MoE 这类「aclnn 实现内嵌在 op_host」的算子（承接 u5-l1）UT 位置也随之内嵌。收集规则只认文件名：[cmake/ut.cmake:338-362](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/ut.cmake#L338-L362) GLOB `test_aclnn_*.cpp`，并兼容从 `op_host/op_api` 或 `tests/ut/op_api` 两种父目录反推算子名。

**③ op_graph UT 收集规则**：[cmake/ut.cmake:365-381](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/ut.cmake#L365-L381) GLOB `test_*_pass.cpp` 并调用 `add_opgraph_ut_modules` 搭建图测试环境。注意 test_config.yaml 中 prompt_flash_attention 节点有一行注释「目前框架不支持opgraph_test」而排除了 op_graph 目录（[tests/test_config.yaml:298](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L298)）——op_graph UT 的框架支持仍在完善中，属于四类 UT 里最不成熟的一类。

#### 4.3.4 代码实践

**实践 C：走读一个错误注入用例**

1. 实践目标：理解 op_api UT 如何「不用真机就测到校验分支」。
2. 操作步骤：
   - 通读 [test_aclnn_moe_fused_topk.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_fused_topk/tests/ut/op_host/op_api/test_aclnn_moe_fused_topk.cpp) 的第 1、2 两个用例，找出二者输入的唯一差异（提示：`max_mapping_num` 129 vs 128、mapping_table 第一维 `expert_num` vs `expert_num + 1`）。
   - 对照该算子的 aclnn 实现源码，定位是哪一条 shape 约束抛出了 `ACLNN_ERR_PARAM_INVALID`。
3. 需要观察的现象：两个用例都期望参数非法，但触发的是不同的校验语句。
4. 预期结果：能写出「输入差异 → 被触发的校验行」的对应关系；这是纯源码阅读实践，无需运行环境。
5. 预期结果即结论，可直接完成。

#### 4.3.5 小练习与答案

**练习**：op_api UT 为什么只调 `TestGetWorkspaceSize`，而不执行第二段 `aclnnXxx`？

**答案**：第二段需要把任务异步下发到 NPU stream 并访问真实 device 内存，CPU 环境无法承载；而参数校验、infershape、tiling、workspace 计算全部发生在第一段（承接 u3-l1 的两段式设计），正是 op_api 层逻辑最密集、最值得 UT 覆盖的部分。真机端到端验证交给 ST / pytest（u3-l4）与 aclnn 调用验证。

### 4.4 op_kernel UT 与 AICPU UT

#### 4.4.1 概念说明

- **op_kernel UT** 是四类 UT 中最「神奇」的一个：它用 `tikicpulib`（`ICPU_RUN_KF` 宏）把 AscendC 核函数**编译成 CPU 版本直接执行**，GM/UB 访问被映射到普通内存，从而在 x86 上验证核函数的计算逻辑。数据闭环用 `gen_data.py` 生成输入 bin、`compare_data.py` 比对输出 bin（u3-l4 已详述这两个脚本，此处看它们如何被 UT 调用）。
- **AICPU UT** 更简单：AICPU 算子本来就是标准 C++（u2-l5），用 `RUN_KERNEL(node_def, HOST, ...)` 在本机直接执行 `Compute`。

#### 4.4.2 核心流程

kernel UT 用例六步（官方指南 [aicore_develop_guide.md:657-690](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L657-L690) 的流程）：

```text
1. 设定 shape/dtype（参考 def 信息库）
2. AscendC::GmAlloc 申请 输入/输出/workspace/tiling 四类缓冲
3. 填 tiling data（手动构造，或用 ExecuteTiling 自动生成）
4. ICPU_SET_TILING_KEY(key) + AscendC::SetKernelMode(AIV_MODE)
5. ICPU_RUN_KF(kernel<模板参>, numBlocks, x, y, z, workspace, tiling)
6. WriteFile 落盘 → python3 compare_data.py 比对 → GmFree 释放
```

#### 4.4.3 源码精读

**① 样例主体**：

[examples/add_example/tests/ut/op_kernel/test_add_example.cpp:53-95](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L53-L95) 是完整用例，关键点：

- [第 16 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L16) 直接 `#include "../../../op_kernel/add_example.cpp"`——kernel 是模板函数，包含源文件触发实例化，这是模板 kernel UT 的标准手法。
- [第 61 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L61) 用 `system()` 调 `gen_data.py` 现场生成输入 bin（数据目录在 SetUpTestCase 中拷贝到运行目录，见 [第 36-38 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L36-L38)）。
- [第 74-76 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L74-L76) 手动填 tiling data（totalLength、tileNum 与 ophost UT 的期望字符串严格一致——两份断言守护同一份「数据合同」）。
- [第 78-81 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L78-L81)：`ICPU_SET_TILING_KEY(0)` 后 `ICPU_RUN_KF(add_example<0>, numBlocks, ...)` 在 CPU 上执行核函数。
- [第 93 行](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/test_add_example.cpp#L93) 调 `compare_data.py` 完成比对，脚本退出码即门禁。

**② tiling 数据的自动生成**（比手动填更工程化）：[aicore_develop_guide.md:699-715](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L699-L715) 给出用 `ExecuteTiling(para, tilingInfo)` 复用 ophost UT 框架生成 tiling data、再 memcpy 进 GmAlloc 缓冲的做法——tiling 字段多时首选。

**③ kernel UT 的 CMake 接线**：

[examples/add_example/tests/ut/op_kernel/CMakeLists.txt:11-26](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/CMakeLists.txt#L11-L26) 是唯一需要「按算子定制」的接线：`AddOpTestCase(add_example "Ascend910B1" "" "${add_example_tiling_files}")` 四个参数依次是算子名、支持的 SoC 列表（分号分隔）、自定义编译宏（如 `-DDTYPE_X=float`）、依赖的 tiling 源文件列表（kernel 跑 CPU 版也需要 tiling 代码在场）。收集规则在 [cmake/ut.cmake:390-432](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/ut.cmake#L390-L432)：按 `test_${opName}*.cpp` GLOB 用例、把下划线算子名规范成驼峰 opType、按 SoC 逐个生成 target。

**④ AICPU UT**：

[examples/add_example/tests/ut/op_kernel_aicpu/test_add_example.cpp:29-52](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel_aicpu/test_add_example.cpp#L29-L52) 用 `CREATE_NODEDEF` 宏构造 NodeDef（输入直接给栈上数组指针），`RUN_KERNEL(node_def, HOST, KERNEL_STATUS_OK)` 在本机执行，最后与期望数组逐位比较。注意：examples 下的这个文件**没有对应的 CMakeLists 接线**（该子目录无 CMakeLists.txt，且其 fixture 类名 `TEST_AddExample_UT` 与用例引用的 `TEST_ADD_UT` 不一致），它目前是参考样例；正式的接线方式以 opgen 模板为准——[scripts/opgen/template/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt:30-32](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/template/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt#L30-L32) 在 `UT_TEST_ALL OR OP_KERNEL_AICPU_UT` 时调用 `AddAicpuOpTestCase(add_example)`。`AddAicpuOpTestCase` 的实现在 [cmake/ut.cmake:578-616](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/ut.cmake#L578-L616)，会自动 GLOB `${op_name}_aicpu.cpp` 与 `tests/ut/op_kernel_aicpu/test_${opName}*.cpp` 并链接 gtest/Eigen。

#### 4.4.4 代码实践

**实践 D：跑一次 kernel UT 并观察数据闭环**

1. 实践目标：亲眼看懂「gen_data.py → ICPU_RUN_KF → compare_data.py」闭环。
2. 操作步骤：
   - `bash build.sh --opkernel_test --ops=add_example --soc=Ascend910B1`（soc 参数需与 CMakeLists 中 `AddOpTestCase` 登记的版本一致）。
   - 运行后在测试工作目录找到 `add_example_data/` 下的 `float32_input_add_example.bin` 与 `float32_output_add_example.bin`。
3. 需要观察的现象：gtest 输出中 `system()` 调用 python 脚本的打印、compare_data.py 的「比对通过」信息与退出码。
4. 预期结果：用例通过；两个 bin 文件大小均为 32×4×4×4×4 字节（fp32）。可在运行前手动改 `gen_data.py` 的 shape 参数观察联动。
5. kernel UT 在 CPU 上执行核函数，无需 NPU，但需要本地 CANN toolkit 与 Python/numpy 环境；无法运行时标注：**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：kernel UT 里手动填的 `tilingDatafromBin->totalLength = 32*4*4*4` 和 ophost UT 里的 `expectTilingData = "256 8 "` 是什么关系？

**答案**：两份断言守护同一份 host↔device「数据合同」。ophost UT 断言 host 侧 tiling 函数会**产出** `{totalLength=元素总数, tileNum=8}`；kernel UT 则**假设**这份合同成立、手动填入同样的值去驱动 device 侧核函数。任一侧擅自改结构体或语义，另一侧的 UT 会失败，从而暴露合同破裂。

**练习 2**：为什么 `AddOpTestCase` 需要显式列出 tiling 源文件？

**答案**：kernel UT 编译的是「kernel + 其依赖」的 CPU 版可执行文件，核函数虽然不调用 tiling 计算，但 tiling data 结构体和相关头文件必须参与编译；`AddOpTestCase` 的第 4 个参数把这些 host 侧 tiling 源文件带进用例 target（见 [op_kernel/CMakeLists.txt:17-25](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel/CMakeLists.txt#L17-L25) 的注释与调用）。漏列会导致链接/编译失败或结构体不一致。

### 4.5 测试配置：test_config.yaml 与 CI 裁剪

#### 4.5.1 概念说明

本仓库有数百个算子、每类 UT 还要乘上 SoC 矩阵，CI 若每次全量跑既慢又浪费。`tests/test_config.yaml` 就是**「源码变更 → 触发哪些验证」的声明式映射表**：

- 每个「结点」（通常一个算子）声明自己看护哪些源码路径（`src`）、排除哪些（`exclude`）、命中率后要一起跑哪些算子的验证（`options`）、是否启用 example / ut 验证（`test`）。
- CI 入口是 `build.sh --PR_UT=<变更文件清单>`：解析清单 → 查表得到要跑的算子集合与 SoC 集合 → 只对这些算子跑 ophost + opapi UT。
- 配套的 `tests/test_soc_config.yaml` 再决定「这些变更需要在哪几个 SoC 上跑」。

#### 4.5.2 核心流程

```text
PR 提交（CI 拿到变更文件列表）
        ↓
build.sh --PR_UT=<file>
  parse_changed_files.py  ← test_config.yaml   → 命中的算子集合（options 并集）
  get_soc_version.py      ← test_soc_config.yaml → 需要跑的 SoC 集合
        ↓
set_ut_mode(): PR_CHANGED_FILES 非空 → 只跑 op_host_ut + op_api_ut 两个 target
        ↓
cmake -DASCEND_OP_NAME=<命中的算子> ... 按算子裁剪收集
        ↓
执行 UT；--PR_PKG 同理按命中算子裁剪出包
```

#### 4.5.3 源码精读

**① 字段说明书**（yaml 自带注释就是权威文档）：

[tests/test_config.yaml:25-43](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L25-L43) 逐字段说明了 `module` / `src` / `exclude` / `ut_cov_exclude` / `test`(example、ut) / `options` 的语义，并特别警告：`exclude` 和 `ut_cov_exclude` **对全仓代码生效，慎用正则**。

**② global_config 结点**：

[tests/test_config.yaml:45-57](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L45-L57) 是一级结点示例：`ut_cov_exclude` 全局排除 tests、third_party、gtest、json、build、common 等目录的覆盖率统计。

**③ 标准算子结点**：

[tests/test_config.yaml:108-120](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L108-L120) 是 flash_attention_score 结点：src 看护算子根目录，exclude 掉 docs 与 README（文档改动不触发重跑），`test: ut: True` 表示参与 UT 验证，`options` 只列自己。对比 [tests/test_config.yaml:139-157](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L139-L157) 的 fused_infer_attention_score 结点：它的 src 额外看护 `attention/common/op_host`，options 列出三个算子——因为这三个算子共享公共 host 代码，公共文件一改就要一起回归（options 不会递归触发）。

**④ 带业务注释的活例子**：

[tests/test_config.yaml:1193-1203](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L1193-L1203) 的 und_gen_qkv_rms_norm_rope_cache 结点附了两条中文注释，示范了配置的真实决策：op_kernel 还没有 UT，先在 `ut_cov_exclude` 里排除、补上 UT 后删掉该行；算子只出 ascend950 二进制、没有 950 实机跑 example，故关掉 examples 由 ST 覆盖端到端。读这些注释是理解「为什么这样配」的最快途径。

**⑤ CI 解析链**：

[build.sh:1732-1744](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1732-L1744) 的 `--PR_UT` 分支依次调 `parse_changed_files.py`（算 mc2 相关的包含/排除集合）和 [get_soc_version.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1738)（带 `tests/test_soc_config.yaml` 查 SoC），随后进入 `set_ut_mode` 的 PR 分支（[build.sh:1155-1162](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1155-L1162)）只跑两类 UT；本地全量出包验证用 `--changed_list` 时走 [build.sh:1204-1217](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1204-L1217) 的 `parse_changed_files` → [scripts/ci/parse_changed_ops.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1216)。SoC 配置表的样例见 [tests/test_soc_config.yaml:15-30](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_soc_config.yaml#L15-L30)：`all_soc` 结点让 mc2/common 等公共目录的变更触发 310p/910b/950 三个 SoC 的验证，而 matmul_all_reduce 的 host 变更只触发 ascend310p。

#### 4.5.4 代码实践

**实践 E：当一次「CI 解析器」**

1. 实践目标：不跑 CI 也能人工推演一次变更会触发哪些验证。
2. 操作步骤：
   - 假设一个 PR 只改了 `attention/common/op_host/foo.h` 和 `attention/prompt_flash_attention/README.md` 两个文件。
   - 在 test_config.yaml 中查所有 `src` 包含这两个路径的结点，再套用各结点的 `exclude` 与 `options`。
3. 需要观察的现象：命中结点的 options 并集；README 是否触发验证。
4. 预期结果：`common/op_host` 命中 fused_infer_attention_score 结点（其 src 明确列了 `attention/common/op_host`），options 为 incre_flash_attention、fused_infer_attention_score、prompt_flash_attention 三个算子；README 被 exclude 排除，不触发任何验证。可对照 [test_config.yaml:139-157](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L139-L157) 核对你的答案。
5. 纯配置阅读，可直接完成。

#### 4.5.5 小练习与答案

**练习 1**：`test` 字段里 `ut: False` 和不写 `test` 字段有什么区别？

**答案**：yaml 注释（[test_config.yaml:37-39](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L37-L39)）写明：`example: True` 时验证触发，**未填写的字段默认为 True**；而 `ut: False` 是显式关闭——不仅本结点 src 变更不跑本算子 UT，**其他结点的 options 配置了本算子时也不会触发**。不写 test 字段则默认 example、ut 都是 True。

**练习 2**：为什么 CI 在 PR 模式下只跑 ophost + opapi 两类 UT，不跑 opgraph / opkernel UT？

**答案**：见 [build.sh:1155-1162](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1155-L1162)，PR 模式只追加 `transformer_op_host_ut` 与 `transformer_op_api_ut` 两个 target。这两类是纯 CPU 编译运行、性价比最高的回归防线；opgraph UT 框架尚不完善（见 4.3 的「不支持opgraph_test」注释），opkernel UT 则编译代价高且需逐算子定制，都留给人主动触发或专门流水线执行。

## 5. 综合实践

**任务：为 u6-l1 开发的 my_sum 算子补齐最小 ophost UT 集，并验证可编译。**

前置：my_sum 已按 u6-l1 完成 def / infershape / tiling / kernel 四件套，位于 `examples/my_sum/`（若当时用 `--genop` 生成，骨架里可能已带 tests 目录的透传 CMakeLists，先检查）。

**第一步：搭目录与接线**（以下均为示例代码，仿照 add_example 编写）

```text
examples/my_sum/tests/CMakeLists.txt      # GLOB 透传（照抄 add_example/tests/CMakeLists.txt:11-16）
examples/my_sum/tests/ut/CMakeLists.txt   # GLOB 透传（同上）
examples/my_sum/tests/ut/op_host/CMakeLists.txt
```

op_host 的接线文件只需两行注册（示例代码，对照 [add_example 版本](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_host/CMakeLists.txt#L11-L14)）：

```cmake
if(UT_TEST_ALL OR OP_HOST_UT)
    add_modules_ut_sources(UT_NAME ${OP_TILING_MODULE_NAME} MODE PRIVATE DIR ${CMAKE_CURRENT_SOURCE_DIR})
    add_modules_ut_sources(UT_NAME ${OP_INFERSHAPE_MODULE_NAME} MODE PRIVATE DIR ${CMAKE_CURRENT_SOURCE_DIR})
endif()
```

**第二步：写 infershape UT**（示例代码）。my_sum 沿最后一维求和，输出 shape 应裁掉最后一维：

```cpp
#include <gtest/gtest.h>
#include <iostream>
#include "infer_shape_context_faker.h"
#include "infer_shape_case_executor.h"

class MySumInfershape : public testing::Test {
protected:
    static void SetUpTestCase() {}
    static void TearDownTestCase() {}
};

TEST_F(MySumInfershape, my_sum_infershape_basic)
{
    gert::InfershapeContextPara para(
        "MySum",
        {
            {{{2, -1}, {2, -1}}, ge::DT_FLOAT, ge::FORMAT_ND}, // x: [M, N]
        },
        {
            {{{}, {}}, ge::DT_FLOAT, ge::FORMAT_ND},           // y: 待推导
        });
    std::vector<std::vector<int64_t>> expectOutputShape = { {2} }; // 裁掉最后一维
    ExecuteTestCase(para, ge::GRAPH_SUCCESS, expectOutputShape);
}
```

（若你的 infershape 实现保留最后一维为 1 或别的语义，以你的实现为准修正期望值。）

**第三步：写 tiling UT**（示例代码）：

```cpp
#include <gtest/gtest.h>
#include "tiling_context_faker.h"
#include "tiling_case_executor.h"

class MySumTiling : public testing::Test {};

TEST_F(MySumTiling, my_sum_tiling_basic)
{
    struct MySumCompileInfo {
    } compileInfo;
    gert::TilingContextPara para(
        "MySum",
        { {{{2, 64}, {2, 64}}, ge::DT_FLOAT, ge::FORMAT_ND} },  // 输入
        { {{{2}, {2}}, ge::DT_FLOAT, ge::FORMAT_ND} },          // 输出
        { /* attrs */ },
        &compileInfo);
    uint64_t expectTilingKey = 0;
    // 首次可写 "* * "（全部通配）跑通后，再按 MySumTilingData 成员顺序回填具体值
    string expectTilingData = "* * ";
    std::vector<size_t> expectWorkspaces = {1024 * 1024 * 16};
    ExecuteTestCase(para, ge::GRAPH_SUCCESS, expectTilingKey, expectTilingData, expectWorkspaces);
}
```

技巧提醒：`expectTilingData` 的字段顺序必须与 `MySumTilingData` 成员声明顺序一致；首次不确定时先全用 `*` 通配跑通，再从 gtest 失败信息或调试输出拿到实际值回填；tiling key 不确定时可传 `UINT64_MAX` 跳过校验。

**第四步：验证编译**：

```bash
bash build.sh --ophost_test --ops=my_sum --noexec
```

观察 `Building target: transformer_op_host_ut.` 后编译是否通过；去掉 `--noexec` 再跑一次应看到两个用例 PASSED。（本步骤需要本地 CANN 环境，无法运行时标注：待本地验证。）

**第五步：解释 test_config.yaml 的裁剪作用**（书面作业）：用 4.5 的语言写 5-8 句话，说明若把 my_sum 贡献进仓库（下一讲 u7-l3 的场景），需要在 test_config.yaml 增加什么结点、不配会发生什么（提示：CI 不知道哪些变更要触发 my_sum 的 UT；对照 add_example —— 它在 test_config.yaml 中没有结点，只有 examples/mc2/all_gather_add 一个 examples 域结点，说明 examples 目录下的教学算子不进 CI 看护，只有正式算子域目录下的算子才需要配置）。

## 6. 本讲小结

- 四类 UT 与四层交付件一一对应：ophost UT（tiling + infershape，faker/executor 模式）、opapi UT（stub 运行时 + `OP_API_UT` 宏只测第一段）、opgraph UT（`test_*_pass.cpp`，框架仍在完善）、opkernel UT（`ICPU_RUN_KF` 在 CPU 上跑核函数 + gen/compare 脚本闭环）；另有 AICPU UT 用 `RUN_KERNEL(node_def, HOST)` 直接执行。
- 收集规则靠**文件名硬约定**：`test_*_tiling.cpp`、`test_*_infershape.cpp`、`test_aclnn_*.cpp`、`test_*_pass.cpp`、`test_${opName}*.cpp`；名字不对就静默不被收集。
- `expectTilingData` 是按 tiling data 结构体成员顺序排列的 int64 字符串，`'*'` 可通配不确定字段，`UINT64_MAX` 可跳过 tiling key 校验；平台默认值是 Ascend910B / 64 核 / 256KB UB。
- 新旧 UT 框架由 cmake 扫描 tests/CMakeLists.txt 是否含 `OpsTest_Level2_AddOp` 自动判定，新算子一律走 framework_normal。
- 运行入口统一是 build.sh：`--ophost_test/--opapi_test/--opgraph_test/--opkernel_test` 组合 `--ops`（裁剪算子）、`--soc`、`--noexec`、`--cov`；opkernel UT 必须带 `--ops`。
- test_config.yaml 是「变更文件 → 触发哪些算子的 example/UT 验证 → 在哪些 SoC 上跑」的声明式裁剪表，`--PR_UT` 是它的 CI 入口；未填写的 `test` 字段默认为 True，`ut: False` 会连坐屏蔽其他结点 options 的引用。

## 7. 下一步学习建议

- **u7-l2（CI 流水线与版本发布机制）**：本讲的 test_config.yaml 只回答「跑什么」，下一讲补全「怎么跑」——`--PR_UT/--PR_PKG` 分支、scripts/ci 目录、classify_rule.yaml 与 run/rpm/deb 出包。
- **动手扩展**：为 my_sum 继续补 kernel UT（模仿 `AddOpTestCase` 接线 + `ICPU_RUN_KF` 用例 + gen/compare 脚本），体会 tiling UT 与 kernel UT 守护同一份 tiling data 合同的双保险设计。
- **源码延伸阅读**：通读 [cmake/ut.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/ut.cmake) 全文，关注 `AddOpTestCase` 如何为每个 SoC 生成独立 target；再看一个工业算子的完整 tests 目录（如 `attention/flash_attention_score/tests`），对比它与 add_example 在用例数量与 arch 子目录组织上的差距。
- **官方文档**：[docs/zh/develop/aicore_develop_guide.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md) 的「算子验证」章节（UT 验证 + aclnn 调用验证）是贡献算子前的必读材料。
