# 测试体系——UT 与 ST

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 HCCL `test/` 目录下 UT（单元测试）与 ST（系统测试）的分工、目录组织与运行方式（`build.sh --ut` / `--st`）。
2. 读懂一个 ST 测试用例（`all_gather_v_testcase.cc`）：它如何用「仿真世界 + 打桩」驱动真实的 HCCL 算子代码路径。
3. 理解 `hccl_verifier` 的语义校验机制：Task 图如何生成、`semantics_checker` 如何用「模拟执行」验证输出内存的数据搬运是否符合算子语义。
4. 掌握本轮新增的 `test/ut/common/alg_parse` 单元测试：`HcclAlgoParser` 如何解析 `HCCL_ALGO` 新格式，`UpdateCostModelWithAlgo` 如何把解析结果过滤/刷新进 CostModel。

## 2. 前置知识

- **UT / ST**：UT（Unit Test，单元测试）只编译被测的少数源文件（必要时用桩头文件替代重依赖），在主机 CPU 上跑，不需要 NPU。ST（System Test，系统测试）在 HCCL 语境里特指「算法分析器」测试：把算子执行链路中的 hcomm/runtime 依赖打桩，跑通完整单算子流程后对产生的任务序列做校验——它不需要真实多机集群，但走的是**真实的生产代码路径**。
- **googletest**：两类测试都用 googletest 框架。`TEST_F(FixtureName, case_name)` 定义一个挂载 fixture 的用例；`EXPECT_EQ/EXPECT_NE/ASSERT_EQ` 是断言（EXPECT 失败继续执行，ASSERT 失败立即返回）。
- **桩（stub）**：用测试自带的假实现顶替真实依赖。HCCL 的 ST 对 hcomm 接口打桩从而截获「本应真正搬数据」的任务描述；UT 则用 `stub/` 目录下的轻量头文件替代真实头文件。
- **弱符号覆盖**：回顾 u4-l2，`HcclGetDeviceType` 以弱符号导出，测试可以用强符号定义直接覆盖它——本讲的 UT 就用了这一手法把设备类型固定为 910_93。
- **CostModel / algName**：回顾 u8 系列前置认知——新选择器 SelectorEngine 靠 CostModel 给每个候选算法（形如 `AicpuAllReduceSoleMesh2Die` 的 algName）估算代价选优；`HCCL_ALGO` 环境变量则通过 `HcclAlgoParser` 解析后用 `UpdateCostModelWithAlgo` 把不想用的算法 count 置 0 过滤掉。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `test/README.md` | 测试总目录说明：ST/UT 分类、目录结构、运行命令 |
| `build.sh` | `-u/--ut` 与 `-s/--st` 选项的编译与运行入口 |
| `test/st/algorithm/testcase/all_gather_v_testcase.cc` | ST 样例：AllGatherV 各种拓扑/数据量的测试用例 |
| `test/st/algorithm/testcase/v_testcase_common.h` | ST 公共骨架 `RunVMultilevelTest`：建仿真世界、多线程下发算子、收集 Task 队列、调校验 |
| `test/st/algorithm/utils/src/hccl_verifier/hccl_verifier.h/.cc` | 校验器门面：`CheckAllGatherV` 等按算子类型组装 checker |
| `test/st/algorithm/utils/src/hccl_verifier/semantics_check/task_check_op_semantics.cc` | 语义校验引擎：Task 图 → 模拟执行 → 内存语义 |
| `test/st/algorithm/utils/src/hccl_verifier/semantics_check/reduce_semantics_checker.cc` | Reduce 算子的最终语义断言（本讲精读样本） |
| `test/st/algorithm/utils/ut/testcase/main.cc` | ST 的 gtest main 入口（含编译命令注释） |
| `test/ut/CMakeLists.txt` | UT 子目录注册：`common/prepare_ut_env`、`common/alg_parse`、`reduce_scatter_birs` |
| `test/ut/common/alg_parse/CMakeLists.txt` | 本轮新增：alg_parse UT 的编译配置 |
| `test/ut/common/alg_parse/alg_parse_test.cc` | 本轮新增：`HcclAlgoParser` 解析测试 |
| `test/ut/common/alg_parse/update_cost_model_test.cc` | 本轮新增：`UpdateCostModelWithAlgo` 过滤/刷新测试 |
| `test/ut/common/alg_parse/stub/alg_parse.h` | 本轮新增：UT 用的轻量桩头文件（含 CostModel 定义） |
| `src/common/alg_parse.h` | 被测的生产代码：`HcclAlgoParser`/`UpdateCostModelWithAlgo` 声明 |
| `docs/zh/build/build.md` | 官方构建文档：LLT 测试与上板测试章节 |

## 4. 核心概念与源码讲解

### 4.1 测试体系总览与运行入口

#### 4.1.1 概念说明

HCCL 的 `test/` 目录分为两大类（见 [test/README.md:1-71](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/README.md#L1-L71)）：

- **ST（系统测试）**：核心是「算法分析器」。原理是：对 HCCL 单算子运行流程的依赖（hcomm 和 runtime 接口）**打桩**，在算法执行过程中截获所有 rank 的 Task 序列，把这些 Task 组成一张**有向无环图**，再基于图算法做两类校验——内存读写冲突校验与语义校验。目录在 `test/st/algorithm/`，下设 `testcase/`（用例）、`utils/src/`（打桩与校验工具：`aicpu/`、`hccl_depends_stub/`、`hccl_proxy/`、`sim_world/`、`hccl_verifier/` 等）。
- **UT（单元测试）**：目录在 `test/ut/`，本轮包含三个子项目：`common/prepare_ut_env`（UT 环境准备）、**本轮新增的 `common/alg_parse`**、以及 `reduce_scatter_birs`（experimental 的 birs 算法测试，对应 u7-l3）。

一句话区分：**UT 测「一个编译单元内的函数逻辑」，ST 测「整条算子执行链路产出的任务序列是否正确」**。

#### 4.1.2 核心流程

运行入口在 `build.sh`：

- `bash build.sh --ut`（即 `-u`）：`build_ut()` 在 `build/` 目录里 cmake 配置并编译整个工程（`-DENABLE_UT=on`），随后 `run_ut()` 用 `find` 在构建目录里**逐个执行所有可执行文件**（UT 不走 ctest，直接跑二进制）。
- `bash build.sh --st`（即 `-s`）：`run_st()` 调用 `test/st/algorithm/build.sh` 独立编译 ST 用例，导出 `LD_LIBRARY_PATH`（指向打桩库所在目录），最后用 **ctest 并发执行**用例。

UT 依赖 googletest（建议 release-1.14.0），见 [docs/zh/build/build.md:145-158](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/docs/zh/build/build.md#L145-L158)（文档中把这类主机测试称为 LLT 测试）。

#### 4.1.3 源码精读

`build_ut()` 组织 CMake 参数并整体编译：

- [build.sh:468-498](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/build.sh#L468-L498) —— `build_ut()` 在 `BUILD_DIR` 中执行 `cmake ${CMAKE_ARGS} ..` 与 `cmake --build .`，通过 `-DENABLE_UT/-DENABLE_ST` 把开关传给 CMake，并设置 `LLT_KILL_TIME=1200`（单测超时杀进程的时限）。
- [build.sh:500-512](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/build.sh#L500-L512) —— `run_ut()` 用 `find "$ut_dir" -type f -executable` 找出全部 UT 可执行文件并逐个执行；未开启 `-u` 时提示 `sh build.sh with parameter -u or --ut to enable it`。
- [build.sh:514-545](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/build.sh#L514-L545) —— `run_st()` 调 ST 自己的构建脚本，把 `utils/src`、`hccl_verifier`、`hccl_depends_stub`、`aicpu` 四个桩库目录加入 `LD_LIBRARY_PATH`，再 `run_ctest "st"` 并发跑用例。

UT 工程的注册点（本轮新增了 alg_parse 一行）：

- [test/ut/CMakeLists.txt:11-13](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/CMakeLists.txt#L11-L13) —— 顶层 UT CMake 通过 `add_subdirectory` 挂载三个 UT 子项目：`common/prepare_ut_env`、`common/alg_parse`（新增）、`reduce_scatter_birs`。

ST 的 gtest 入口则带着一条「如何手工编译」的注释：

- [test/st/algorithm/utils/ut/testcase/main.cc:14-20](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/ut/testcase/main.cc#L14-L20) —— 标准 `InitGoogleTest + RUN_ALL_TESTS()`；第 14 行注释给出了不经 build.sh 的手工编译命令：`cmake ../cmake/superbuild/ -DHOST_PACKAGE=ut -DBUILD_MOD=hccl_checker_ops && make -j16`。

#### 4.1.4 代码实践

1. **实践目标**：不动任何源码，跑通 UT 编译与执行链路，看清「build.sh → CMake → 可执行文件」三级关系。
2. **操作步骤**：
   - 确认已安装 googletest（建议 release-1.14.0，见 `docs/zh/build/build.md` 的依赖说明）；
   - 在仓库根目录执行 `bash build.sh -u`；
   - 观察输出中 `Executing: alg_parse_test` 等行（`run_ut()` 逐个执行 UT 二进制的打印）。
3. **需要观察的现象**：CMake 配置阶段会打印 `Final Ascend Path` 与 UT 文件列表；构建产物出现在 `build/` 下的 `test/` 子目录。
4. **预期结果**：所有 UT 二进制执行返回 0，末尾打印 `PASSED`。若本机缺少 CANN 安装环境导致部分目标编译失败，可只关注 `alg_parse_test` 目标本身。**待本地验证**（取决于本机是否有完整 CANN 依赖）。
5. 若无法运行，可退化为源码阅读型实践：对照 [build.sh:500-512](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/build.sh#L500-L512) 回答「UT 与 ST 的执行器有何不同」（UT 直接 find+执行，ST 走 ctest 并发）。

#### 4.1.5 小练习与答案

**练习 1**：`run_ut()` 为什么可以「找到什么执行什么」，而 `run_st()` 要先设置 `LD_LIBRARY_PATH`？

**答案**：UT 只链接被测源文件与 gtest，符号在编译期已全部落进可执行文件；ST 的可执行文件依赖 `utils/src`、`hccl_verifier`、`hccl_depends_stub`、`aicpu` 等打桩动态库（见 build.sh:533-537），运行期必须通过 `LD_LIBRARY_PATH` 找到这些 .so，否则算子链路里的桩函数解析不到。

**练习 2**：想给 UT 新增一个子目录 `test/ut/common/foo`，至少要改哪两个文件？

**答案**：新建 `test/ut/common/foo/CMakeLists.txt`（参考 alg_parse 的写法，`add_executable` + `run_llt_test`），并在 [test/ut/CMakeLists.txt](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/CMakeLists.txt#L11-L13) 中追加一行 `add_subdirectory(common/foo)`。

### 4.2 ST：testcase 如何驱动真实算子链路

#### 4.2.1 概念说明

ST 的用例文件看起来像「普通调用 HCCL API 的程序」：设置拓扑、设置数据描述、发起 `HcclAllGatherV`。差别在于它运行在**仿真世界（SimWorld）**里——`aclrtMalloc`、`HcclCommInitClusterInfo`、hcomm 数据面原语都被桩接管，于是：

- 算子入口 → Selector → Executor → Template 这条**生产代码链路被完整执行**；
- 真正「搬数据」的动作被记录成 Task（LOCAL_COPY / WRITE / READ / REDUCE 等）进入每个 rank 的任务队列；
- 用例结束后把这些队列交给校验器（4.3 节）。

也就是说，ST 同时做到了「驱动真实通信逻辑」与「无需真实 NPU 和集群」。

#### 4.2.2 核心流程

一个 V 类（变长）算子 ST 用例的执行骨架：

```text
RunVMultilevelTest(topoMeta, vDataDes, ...)
  ├─ SimWorld::Global()->Init(topoMeta, DEV_TYPE_950)   # 按拓扑元数据建仿真世界
  ├─ setenv("HCCL_OP_EXPANSION_MODE", "AI_CPU")          # 固定走 AICPU 展开模式
  ├─ 对每个 rank 启动一个线程：
  │    aclrtSetDevice → aclrtCreateStream
  │    → HcclCommInitClusterInfo("./ranktable.json", rankId, &comm)   # 建通信域（桩）
  │    → dispatchFn(...)                                 # 用例自定义：发起 HcclAllGatherV
  │    → HcclCommDestroy
  ├─ join 全部线程
  ├─ taskQueues = SimTaskQueue::Global()->GetAllRankTaskQueues()      # 收集全部 Task
  └─ verifyFn(taskQueues, rankSize, vDataDes)            # 交给语义校验器
```

`TopoMeta` 是嵌套向量描述的拓扑：`{{{0,1},{2,3}}}` 表示两个 pod、每个 pod 两卡、共 1 个超节点（2 层网络）；再加一层嵌套就是 3 层拓扑。

#### 4.2.3 源码精读

- [test/st/algorithm/testcase/v_testcase_common.h:37-89](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/testcase/v_testcase_common.h#L37-L89) —— `RunVMultilevelTest` 模板函数：第 42 行 `SimWorld::Global()->Init(topoInfo, HcclDevType::DEV_TYPE_950)` 以 950（A5）设备语义初始化仿真世界；第 56-78 行为每个 rank 起线程走「绑卡 → 建流 → `HcclCommInitClusterInfo` → dispatch → 销毁」流程；第 84-86 行取出所有 rank 的任务队列并调用校验函数，`EXPECT_TRUE(res == HCCL_SUCCESS)` 把校验结果变成 gtest 断言。
- [test/st/algorithm/testcase/all_gather_v_testcase.cc:36-46](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/testcase/all_gather_v_testcase.cc#L36-L46) —— 测试 fixture：`SetUp` 里 `ResetAlgEnvConfigInitState()` 保证每个用例重新解析环境变量（呼应 u4-l3 的 `AlgEnvConfig` 单次初始化守卫）；`TearDown` 清理本用例设置的环境变量。
- [test/st/algorithm/testcase/all_gather_v_testcase.cc:48-61](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/testcase/all_gather_v_testcase.cc#L48-L61) —— `AllGatherVDispatch`：按本 rank 的 `counts[rankId]` 分配 sendBuf、按 totalCount 分配 recvBuf，然后调用真实的 `HcclAllGatherV`（u2-l1 讲过的变长算子，counts/displs 为数组）。分配用的是 `aclrtMalloc`——在 ST 里同样被桩接管。
- [test/st/algorithm/testcase/all_gather_v_testcase.cc:63-66](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/testcase/all_gather_v_testcase.cc#L63-L66) —— `RunAllGatherVMultilevel` 把 dispatch 与校验函数 `CheckAllGatherV` 绑定后交给公共骨架。
- [test/st/algorithm/testcase/all_gather_v_testcase.cc:104-147](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/testcase/all_gather_v_testcase.cc#L104-L147) —— 一组覆盖不同拓扑形态的用例：3 层 2 pod 8 卡（第 104-113 行）、非对称拓扑（第 138-147 行，各 pod/server 的 rank 数不同）。第 101-102 行注释明确记录了两个已知限制（模拟器只有 2 层网络、非等长 AllGatherV 的既有 bug），这是「用注释圈定测试边界」的好范例。

#### 4.2.4 代码实践

1. **实践目标**：读懂一个 ST 用例的「拓扑 → 数据 → 断言」三要素，并能推断它测的是什么。
2. **操作步骤**：
   - 打开 `test/st/algorithm/testcase/all_gather_v_testcase.cc`，逐个用例列出三元组：`topoMeta`（几层、几个 pod、各几卡）、`counts/displs`（等长还是变长）、`dataType`；
   - 对照 [v_testcase_common.h:26-35](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/testcase/v_testcase_common.h#L26-L35) 的 `AnalyseRankSize`，手工累加每个用例的 rankSize，验证它与 counts 数组长度一致。
3. **需要观察的现象**：等长用例（如 `..._4rank_int32_equal_test`）的 displs 是等差数列；非对称用例的 displs 间距逐段变小。
4. **预期结果**：`st_all_gather_v_a5_asymmetric_fp32_test` 的 rankSize 为 14（2+3 与 4+5 的两组嵌套），与第 142-143 行 counts/displs 各 14 个元素一致。
5. 如需实际运行：`bash build.sh -s`（需 CANN 主机环境）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 fixture 的 `SetUp` 必须调用 `ResetAlgEnvConfigInitState()`？

**答案**：`AlgEnvConfig` 是 `thread_local` 且带「只初始化一次」守卫（u4-l3）；ST 多个用例共享进程且用例可能修改 `HCCL_OP_EXPANSION_MODE` 等环境变量，不重置状态会导致后续用例读到上一个用例的解析结果，产生用例间耦合。

**练习 2**：用例为什么「每个 rank 一个线程」而不是「每个 rank 一个进程」？

**答案**：真实部署中 rank 分属不同进程/节点，但 ST 的校验器需要**在同一进程内**收集全部 rank 的 Task 队列拼成一张全局图（`SimTaskQueue::Global()` 是进程级单例）；线程模型既模拟了各 rank 并发下发，又让桩与队列天然共享同一地址空间。

### 4.3 ST：hccl_verifier 与 semantics_checker 语义校验

#### 4.3.1 概念说明

校验器分两层：

- **门面层 `hccl_verifier`**：提供 `CheckAllGatherV`、`CheckReduce` 等按算子命名的入口，负责构造 `Checker` 与 `TaskCheckOpSemantics` 并启动校验。
- **语义引擎 `TaskCheckOpSemantics`**：把所有 rank 的 Task 组成有向无环图后**模拟执行**——每个 Task（Write/Read/LocalCopy/LocalReduce/ReadReduce/WriteReduce）被翻译成一对「源内存片 → 目的内存片」的搬运（`SliceOpPair`），搬运时在「内存语义表」（每段输出内存记录它当前装着哪些源 rank 的哪些数据、是否做过归约、用什么归约算子）上打标记。全部 Task 执行完后，再由**每算子一个的 semantics_checker** 对最终内存语义表做断言：输出是否恰好由全部 rank 的输入按算子语义构成。

例如 Reduce 语义：root 的输出每一段都必须恰好包含来自全部 rankSize 个 rank 的输入、归约类型等于入参 reduceType、各段地址连续且总长等于数据量——这正是 `reduce_semantics_checker.cc` 逐条检查的内容。

#### 4.3.2 核心流程

```text
CheckAllGatherV(taskQueues, rankSize, vDataDes)
  └─ Checker.GenAndCheckGraph(taskQueues, opSemanticsChecker)
       ├─ 生成 Task DAG（含内存冲突检查）
       └─ TaskCheckOpSemantics::Execute()
            ├─ InitInputBuffer()          # 给每个 rank 的输入内存登记初始语义（来自自己）
            ├─ GenMemSemantics()          # BFS 模拟执行 DAG：
            │     每个 Task → GetSliceOpPair() 翻译成 SliceOpPair 列表
            │     → ProcessSliceOpPair() 按 OVERRIDE/REDUCE 更新目标内存语义
            └─ 按 opType 分发最终断言：
                  HCCL_CMD_REDUCE     → TaskCheckReduceSemantics(...)
                  HCCL_CMD_ALLGATHER_V→ TaskCheckAllGatherVSemantics(...)
                  ...（每算子一个 checker）
```

#### 4.3.3 源码精读

- [test/st/algorithm/utils/src/hccl_verifier/hccl_verifier.cc:154-161](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/hccl_verifier.cc#L154-L161) —— `CheckAllGatherV` 门面：构造 `TaskCheckOpSemantics`，设置算子类型 `HCCL_CMD_ALLGATHER_V` 与变长数据描述 `vDataDes`，然后交给 `checker.GenAndCheckGraph`。[hccl_verifier.h:147](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/hccl_verifier.h#L147) 是它的声明，同文件还声明了 Reduce/Scatter/All2All 等一族入口。
- [test/st/algorithm/utils/src/hccl_verifier/semantics_check/task_check_op_semantics.cc:505-566](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/semantics_check/task_check_op_semantics.cc#L505-L566) —— `GetSliceOpPair`：把七种 Task 类型翻译成 `SliceOpPair`（源 rank/内存片 + 目的 rank/内存片 + OVERRIDE 或 REDUCE）。例如 `WRITE` 是「本 rank 内存片 → 覆盖远端 rank 内存片」，`READ_REDUCE` 是「远端内存片 → 归约进本 rank 内存片」。
- [task_check_op_semantics.cc:647-686](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/semantics_check/task_check_op_semantics.cc#L647-L686) —— `GenMemSemantics`：对 Task DAG 做队列驱动的模拟执行；第 670 行 `IsReadyForSimulate` 保证父节点全部执行完才处理当前节点（即按 DAG 拓扑序），第 675 行处理每个节点更新内存语义表。
- [task_check_op_semantics.cc:688-742](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/semantics_check/task_check_op_semantics.cc#L688-L742) —— `Execute`：初始化输入语义（Broadcast/Scatter 只有 root 有输入，第 690-694 行）→ 生成内存语义 → 第 709-736 行按 `opType_` 的 switch 分发到每算子的最终断言函数，如 `HCCL_CMD_REDUCE` 调 `TaskCheckReduceSemantics`。
- [test/st/algorithm/utils/src/hccl_verifier/semantics_check/reduce_semantics_checker.cc:18-91](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/semantics_check/reduce_semantics_checker.cc#L18-L91) —— `TaskCheckReduceSemantics` 的四条断言：①输出段必须从地址 0 连续排布（第 33-40 行）；②多源段的归约类型必须等于入参 `reduceType`（第 42-48 行）；③每个输出段的源必须恰好覆盖全部 rankSize 个 rank（第 50-55 行）且首尾 rank 编号为 0 和 rankSize−1（第 57-62 行）；④每个源必须是 INPUT 且源地址与本段对齐（第 64-79 行）；最后总长必须等于 `dataSize`（第 82-88 行）。
- 同目录下的 [allgather_v_semantics_checker.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/semantics_check/allgather_v_semantics_checker.cc) 等 13 对 `*_semantics_checker.h/.cc` 构成完整的按算子断言族，注册关系见 [task_check_op_semantics.cc:15-26](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/semantics_check/task_check_op_semantics.cc#L15-L26) 的 include 列表。

#### 4.3.4 代码实践

1. **实践目标**：理解「语义校验 = 模拟执行 + 按算子断言」，并能指出 AllGatherV 的断言与 Reduce 的断言差在哪。
2. **操作步骤**：
   - 精读 `reduce_semantics_checker.cc` 的四条断言（行号见上）；
   - 打开 `allgather_v_semantics_checker.cc`，找出它与 Reduce 断言的两个本质差异（提示：AllGatherV 不检查 reduceType；其每段输出的源 rank 数由 `counts`/`displs` 决定而非恒等于 rankSize）；
   - 回到 4.2 的调用链，确认 `CheckAllGatherV` 是在所有 rank 线程 join 之后才执行的。
3. **需要观察的现象**：语义校验失败的报错都带 `[rankId:%u]` 前缀并打印 `cur buffer semantic is %s`（语义块的自描述），可直接定位是哪个 rank 的哪段输出不符合预期。
4. **预期结果**：能写出一句总结——「语义校验不比较具体数值，而是比较『输出的数据来源结构』，因此它对任意数据分布都成立，且能发现漏搬、重复归约、地址错位等逻辑错误」。
5. 运行层面：`bash build.sh -s` 后在 ctest 输出中观察 `st_all_gather_v_*` 用例。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：语义校验为什么用「数据来源结构」而不是「随机数据实际归约结果」来比对？

**答案**：模拟执行记录的是每段输出内存「由哪些源 rank 的哪段输入构成、是否归约」，这等价于对算子数学定义的检查（如 AllReduce ≡ 全体输入的归约），与具体数值无关；同时 [task_check_op_semantics.cc:472-485](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/semantics_check/task_check_op_semantics.cc#L472-L485) 注释也指出：随机数据做 reduce 可能概率性溢出，来源结构比对避免了这类数值噪声。

**练习 2**：`Execute()` 里为什么要单独处理 Broadcast/Scatter 的 `InitInputBuffer(root)`？

**答案**：这两个算子只有 root rank 有输入（第 690-694 行与 [task_check_op_semantics.cc:45-59](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/semantics_check/task_check_op_semantics.cc#L45-L59)）：只为 root 登记初始输入语义，其余 rank 的输出最终应全部来源于 root 的这段输入。

### 4.4 新增 UT：alg_parse 解析与 CostModel 刷新测试

#### 4.4.1 概念说明

本轮提交新增了 `test/ut/common/alg_parse` 目录，为 u8 系列的「新选择器配置链路」补上单测，被测对象是生产代码 [src/common/alg_parse.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc)，其头文件 [src/common/alg_parse.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.h) 定义了三个核心结构：

- `HcclAlgo`：一个算法（template）条目——`algoType`（驼峰算法名）+ `enable`（`not()` 取非时为 false）；
- `HcclAlgoExecutor`：一段配置——`opType`（空=全局）+ `executorType`（sole/sequence/parallel/concur/pipeline）+ 按 level 升序的 `algoList`；
- `HcclAlgoParser`：解析器——`Parser()` 把 `HCCL_ALGO` 字符串解析进 `executorList`（**越靠后优先级越高**）。

以及刷新函数 `UpdateCostModelWithAlgo(parser, model, engineTypes)`：按规则把解析结果作用到 CostModel 上——反向遍历（后配置优先）、按 OpType 匹配（已匹配的 OpType 不再参与）、`enable=false` 的算法 count 置 0（排除）。

两份测试分别覆盖「解析正确性」与「刷新正确性」。

值得注意的工程手法是**桩头文件**：`test/ut/common/alg_parse/stub/alg_parse.h` 与真实头文件同 include guard，但剥掉了 `op_common.h`/`cost_model.h` 等重依赖，自带 `CostModel`/`CostModelParam` 结构定义和 `GetEnv`/日志宏的假实现；CMake 里 `stub/` 排在 include 路径**最前面**从而遮蔽真实头，使 UT 只需编译 `alg_parse.cc` 一个源文件。

#### 4.4.2 核心流程

`HCCL_ALGO` 配置串的语法（由测试用例可完整反推）：

```text
<opType>:<executor>{<algo>[,not(<algo>)][,level0=<algo>,level1=<algo>...]};<下一段>
简写：<algo> 单独出现 ≡ sole{<algo>}
```

刷新规则（对应 [alg_parse.h:64-77](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.h#L64-L77) 的注释）：

```text
反向遍历 executorList（后面的优先级高）:
  对每段配置，在 CostModel 的算法名集合中找同时满足
    前缀 ∈ engineTypes 候选引擎 + opType 匹配 + executorType 匹配 + 逐 level 算法匹配（not 为"不等于"）
  的算法：
    命中 → count 保持/置 1（参与比价）
    同 OpType 下未命中 → count = 0（被过滤）
    not(executor{...}) → 仅被点名的算法 count=0，同 OpType 其他算法不受影响
    全局段（opType 为空）→ 对所有 OpType 生效
```

#### 4.4.3 源码精读

生产侧数据结构（被测对象契约）：

- [src/common/alg_parse.h:29-58](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.h#L29-L58) —— `HcclAlgo`/`HcclAlgoExecutor`/`HcclAlgoParser` 三个结构与 `Parser()` 入口；第 50 行注释明确「executorList 按配置顺序存储，越靠后优先级越高」。
- [src/common/alg_parse.h:60-77](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.h#L60-L77) —— `FilterCmByHcclAlgo`（带候选引擎前缀重载）与 `UpdateCostModelWithAlgo` 的声明及刷新规则注释。

测试一：解析（`alg_parse_test.cc`）：

- [test/ut/common/alg_parse/alg_parse_test.cc:16-20](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/alg_parse_test.cc#L16-L20) —— 测试文件自定义强符号 `HcclGetDeviceType` 直接返回 `DEV_TYPE_910_93`：利用 u4-l2 讲过的弱符号覆盖机制，把「HCCL_ALGO 仅在 910_93（A3）解析」的设备门控（u4-l3）固定在放行态，无需真实设备。
- [alg_parse_test.cc:31-61](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/alg_parse_test.cc#L31-L61) —— `ParseCorrectCase1`：解析含显式 opType、多算法、简写段、`not()` 取非的多段配置，逐字段断言 `executorList` 各段的 opType/executorType/algoList/enable；第 56-60 行验证 `not(meshoneshot)` 被展开为 `sole{meshoneshot}` 且 `enable=false`。
- [alg_parse_test.cc:194-210](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/alg_parse_test.cc#L194-L210) —— `ParseShorthand`：裸算法名一律展开为 `sole{...}`。
- [alg_parse_test.cc:151-226](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/alg_parse_test.cc#L151-L226) —— 错误族：非法 opType、括号未闭合、花括号内出现分号、非法 algoType/executorType 均要求 `Parser` 返回非 SUCCESS（呼应「尽早失败」的解析原则）。

测试二：刷新（`update_cost_model_test.cc`）：

- [update_cost_model_test.cc:22-55](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/update_cost_model_test.cc#L22-L55) —— 辅助函数：`BuildCostModel` 用一组 algName 构造所有 count=1 的 CostModel；`GetAlgoCount` 按名查 count（0=被排除、1=正常、-1=不存在）——count 就是「是否存活于候选集」的布尔位。
- [update_cost_model_test.cc:61-77](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/update_cost_model_test.cc#L61-L77) —— 标准算法名样本表 `ALGO_NAMES`：15 个形如 `AicpuAllReduceSoleMesh2Die` 的名字，刻意覆盖全部 ALGO_TYPES（Mesh/Mesh2Die/MeshOneShot/…/NHR/NHRMultiLink）与 EXECUTOR_TYPES（Sole/Sequence/Parallel/Concur），这是三维命名体系（u8-l3）的直接体现。
- [update_cost_model_test.cc:96-111](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/update_cost_model_test.cc#L96-L111) —— `PositiveExactMatch`：`allReduce:sole{mesh2die}` 命中 `AicpuAllReduceSoleMesh2Die`（count=1），同 OpType 的另两个算法被排除（count=0），**其他 OpType 的算法不受影响**。
- [update_cost_model_test.cc:171-183](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/update_cost_model_test.cc#L171-L183) —— `PriorityOrder`：两段都匹配 allReduce 时，**后配置的 `parallel{meshmultilink,nhr}` 胜出**，验证「反向遍历、后优先」。
- [update_cost_model_test.cc:292-305](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/update_cost_model_test.cc#L292-L305) —— `NegatedAlgoFuzzyMatch`：`sequence{mesh,not(nhr),nhr}` 是模糊匹配——level1=Mesh 且 level2=NHR 的算法命中，而 level1=NHR 的 `AicpuAllGatherSequenceMeshNHRNHR` 被 not 排除；同 OpType 但不在命中集合里的算法也被排除。
- [test/ut/common/alg_parse/stub/alg_parse.h:49-73](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/stub/alg_parse.h#L49-L73) —— 桩头文件中的 `CostModelParam`（A/B/C 三参数，即 u8-l2 的代价三元组）与 `CostModel`/`CostAlgoParams` 定义，让 UT 摆脱对 `cost_model.h` 的依赖。
- [test/ut/common/alg_parse/CMakeLists.txt:13-20](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/CMakeLists.txt#L13-L20) —— 编译配置：`SOURCE_FILES` 只有 `src/common/alg_parse.cc` 一个生产文件，`add_executable(alg_parse_test ${UT_FILES} ${SOURCE_FILES})`。
- [test/ut/common/alg_parse/CMakeLists.txt:30-37](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/CMakeLists.txt#L30-L37) —— include 顺序：`stub/` 在 `${PROJECT_SOURCE_DIR}/src/common` **之前**，桩头因此遮蔽真实头；第 59 行 `run_llt_test(TARGET alg_parse_test)` 把目标接入统一 UT 运行/超时框架。

#### 4.4.4 代码实践

1. **实践目标**：验证「`HCCL_ALGO` 解析结果如何过滤 CostModel」这条链路，并亲手构造一条会被 not() 排除的配置。
2. **操作步骤**：
   - 阅读 [update_cost_model_test.cc:96-111](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/update_cost_model_test.cc#L96-L111) 与 [alg_parse.h:64-77](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.h#L64-L77) 的规则注释；
   - 在纸面推演配置 `not(nhrmultilink);allReduce:sole{mesh2die}`（即测试 6，第 189-205 行）作用于 `ALGO_NAMES` 后每个算法的 count；
   - 若本地可编译：`bash build.sh -u` 后单独运行 `alg_parse_test` 二进制，或用 gtest 过滤参数只跑 `UpdateCostModelTest.*`。
3. **需要观察的现象**：推演结果应与测试断言一致——`AicpuAllReduceSoleMesh2Die` count=1；同 OpType 其余算法（含被全局 not 点名的 `CcuMSAllReduceSoleNHRMultiLink`）count=0；**非 allReduce 的 OpType 中**，未被任何规则点名的算法 count 不变（仍为 1），但含 `NHRMultiLink` 的 `AicpuAllToAllSoleNHRMultiLink` 因全局 not 被置 0。
4. **预期结果**：第 3 步的每个 count 都能对照 [update_cost_model_test.cc:189-205](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/update_cost_model_test.cc#L189-L205) 的断言逐条解释。
5. 无法运行时，完成纯纸面推演即为达标；运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：配置 `allReduce:pipeline{mesh2die}` 解析成功，但样本 CostModel 里没有 pipeline 的算法，`UpdateCostModelWithAlgo` 会失败吗？

**答案**：不会。[update_cost_model_test.cc:279-285](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/update_cost_model_test.cc#L279-L285)（`NoMatchExecutorType`）专门验证了这一点：pipeline 是合法的 executorType（解析成功），CostModel 无匹配算法时刷新仍返回 SUCCESS——刷新是「过滤」语义而非「必须命中」语义；但一旦存在匹配（`PipelineMatch` 测试第 354-372 行），同 OpType 其他算法就会被排除。

**练习 2**：为什么 `BuildCostModel` 里每个算法初始 count=1，而 `GetAlgoCount` 的文档注释说「0=被排除」？

**答案**：count 是 CostModel 中算法条目的存活标记：1 表示参与后续代价比价，`UpdateCostModelWithAlgo` 把被 `HCCL_ALGO` 规则排除（或同 OpType 未命中）的算法置 0，新选择器 `SelectMinCost` 只在 count 非 0 的条目里选最小代价算法（承接 u8-l1/u8-l2 的认知）。

**练习 3**：UT 为什么能用 stub 头文件遮蔽真实 `alg_parse.h`？风险是什么？

**答案**：因为 CMake 把 `stub/` 放在 include 路径最前（CMakeLists.txt 第 30-37 行），同 include guard `OPS_HCCL_SRC_COMMON_ALG_PARSE` 使编译器先命中桩头；收益是无需拖入 `op_common.h` 等重依赖即可编译单个 .cc；风险是桩与真实头一旦失同步（结构体字段变化），测试可能「测了一个旧契约」——所以桩头的结构定义应与真实头保持逐字段一致，代码评审时需同时看两处。

## 5. 综合实践

**任务：为一个假想的 Reduce ST 用例补全「驱动 + 校验」两侧的追踪说明。**

1. 在 `test/st/algorithm/testcase/` 下找 reduce 相关用例（非 V 类即可），对照 4.2 的骨架标注它的三要素：TopoMeta、数据描述（dataType/dataCount/reduceType/root）、校验函数名。
2. 沿 `RunVMultilevelTest`（或同族骨架）画出该用例的时序：SimWorld 初始化 → 各 rank 线程下发 `HcclReduce` → join → `SimTaskQueue` 收集 → `CheckReduce` → `TaskCheckOpSemantics::Execute`（opType 分发到 `HCCL_CMD_REDUCE`）→ `TaskCheckReduceSemantics` 四条断言。
3. 写出每条断言对应的「真实通信中的错误形态」：例如「srcBufs.size() != rankSize」对应哪种实现 bug（答案示例：某轮 Ring 步骤漏了一次对 root 的 Write-Reduce，导致 root 输出缺少某个 rank 的贡献）。
4. 在 UT 侧，为假想配置 `allReduce:not(sequence{mesh})` 推演 [update_cost_model_test.cc:61-77](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/update_cost_model_test.cc#L61-L77) 中 `AicpuAllReduceSoleMesh2Die`、`CcuMSAllReduceSequenceMeshOneShotNHR`、`AicpuAllGatherSequenceMeshMeshNHR` 三者的 count，并说明依据的规则（反向遍历/OpType 隔离/仅点名排除）。
   参考答案：`not(executor)` 只点名排除 sequence 编排的 allReduce 算法——`CcuMSAllReduceSequenceMeshOneShotNHR` count=0；`AicpuAllReduceSoleMesh2Die` 是 sole 编排不受影响（count=1，对应测试 13 的 `NegatedExecutorMarksOpType` 语义）；`AicpuAllGatherSequenceMeshMeshNHR` 属于 allGather，OpType 隔离（count=1）。

## 6. 本讲小结

- HCCL 测试分 UT 与 ST：`bash build.sh -u` 逐个执行 UT 二进制（只编译被测源文件 + 桩），`bash build.sh -s` 走 ST 独立构建脚本 + `LD_LIBRARY_PATH` 指向打桩库 + ctest 并发执行。
- ST 用例跑在 SimWorld 仿真世界中：算子入口→Selector→Executor→Template 生产链路真实执行，数据面动作被桩记录为各 rank 的 Task 队列；fixture 靠 `ResetAlgEnvConfigInitState` 隔离环境变量状态。
- 语义校验 = `hccl_verifier` 门面 + `TaskCheckOpSemantics` 引擎：Task 组成 DAG 后按拓扑序模拟执行，把每个搬运翻译成 SliceOpPair 并维护「内存语义表」，最后按算子分发到 13 个 `*_semantics_checker` 做「数据来源结构」断言（以 `reduce_semantics_checker` 的四条断言为样本）。
- 本轮新增 `test/ut/common/alg_parse`：`alg_parse_test` 覆盖 `HcclAlgoParser` 对 `HCCL_ALGO` 新格式（opType 前缀、简写展开、not() 取非、容错与报错）的解析；`update_cost_model_test` 覆盖 `UpdateCostModelWithAlgo` 的过滤规则（反向优先、OpType 隔离、count 置 0 排除、模糊匹配）。
- UT 工程两个可复用手法：用同 include guard 的 stub 头文件遮蔽重依赖头；用强符号覆盖弱符号函数（`HcclGetDeviceType`→910_93）满足设备门控。

## 7. 下一步学习建议

- 下一讲起进入 Unit 8（代价模型选择器与 Tuner 插件）：先学 **u8-l1 新选择器 SelectorEngine 与双路径分发**，本讲的 `UpdateCostModelWithAlgo` 正是那条链路上的一环。
- 想深入语义校验，可通读 [test/st/algorithm/utils/src/hccl_verifier/semantics_check/](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/st/algorithm/utils/src/hccl_verifier/semantics_check/task_check_op_semantics.cc) 全部 checker 与 `mem_conflict_check/`（内存冲突校验，本讲未展开），以及 `test/st/algorithm/README.md` 的算法分析器使用指南。
- 想练习写测试，可参照 `alg_parse` 的 CMakeLists 与 stub 手法，给 `src/common/` 下另一个纯逻辑模块（如 `alg_type.cc` 的字符串转换，见 u4-l1）规划一个最小 UT。
