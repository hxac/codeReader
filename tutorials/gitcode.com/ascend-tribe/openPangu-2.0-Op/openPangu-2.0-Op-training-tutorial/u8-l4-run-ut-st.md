# 构建与运行 UT/ST：build.sh -u 与 pytest

## 1. 本讲目标

前两讲（u8-l2、u8-l3）分别讲了「怎么写一个 tiling UT 用例」和「怎么写一个 st 精度用例」。本讲回答最后一个问题：**这些用例是怎么被构建系统找到、编译成可执行文件、并在机器上真正跑起来的**。

学完本讲，你应该能够：

1. 说清楚 `bash build.sh -u --ophost -n <算子> -c <芯片>` 这条命令在脚本内部走过的完整分支：参数解析 → UT 模式判定 → 用例目录检查 → CMake 变量注入 → 构建 → **编译完成即自动运行**。
2. 解释 `transformer_op_host_ut` / `transformer_op_api_ut` 两个目标是如何由 `cmake/ut.cmake` 与各算子 `tests/ut` 目录装配出来的，为什么新增算子 UT「零注册」（u8-l1 的结论）在源码上成立。
3. 手动重跑一个已编译好的 UT 可执行文件时，知道必须补哪个环境变量。
4. 说清楚 st 侧 `pytest.ini` + `conftest.py` 组成的「硬件资源门禁」是如何在收集期筛选用例的，以及为什么直接 `pytest <测试文件>` 可能绕过这道门禁。
5. 拿到一台新机器时，能按清单排查「编译不过 / 用例被静默跳过 / import 失败」三类问题。

## 2. 前置知识

- **UT 与 ST 的分工**（u8-l1/u8-l3 已建立）：UT 在宿主机上验证流程与分支（tiling 是否返回正确 tilingKey、aclnn 第一段是否返回成功），**不需要 NPU**；ST 在真机上验证数值精度（MARE/MERE/RMSE 对比 CPU golden），**必须要 NPU**。
- **faker 与 stub**（u3-l4、u8-l1）：UT 之所以能脱离硬件运行，是因为 `TilingContext` 由 faker 伪造、下游 aclnn/rt 接口由 stub 替身。
- **build.sh 的基本形态**（u1-l4）：薄壳脚本，把 `-c`（芯片）、`-n`（算子白名单）等参数翻译成 CMake 的 `-D` 变量，主流程是 `set_env → clean → cmake_config → build`。
- **pytest 的三个钩子**：`pytest_addoption`（注册命令行参数）、`pytest_configure`（注册 marker）、`pytest_collection_modifyitems`（收集完成后、运行前修改用例列表）。conftest.py 是 pytest 约定的「本地插件文件」。
- **rootdir 与 conftest 加载规则**：pytest 只会加载「被收集文件祖先链（向上截止到 rootdir）」上的 conftest.py。这个规则是本讲第 3 个模块的关键伏笔。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/build.sh` | 总入口。`-u` 开 UT 模式，`--ophost`/`--opapi` 选 UT 目标，负责前置检查与 CMake 变量注入 |
| `ascendc/CMakeLists.txt` | `ENABLE_TEST` 时拉入 gtest/json 等三方依赖，并把 `src/tests/ut/framework_normal` 挂进构建树 |
| `ascendc/cmake/ut.cmake` | UT「总线」：定义 `add_optiling_ut_modules` 等装配函数，用 glob 收集各算子 `test_*_tiling.cpp` 等用例源码 |
| `ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt` | 定义 `transformer_op_host_ut` 可执行目标，并在 POST_BUILD 阶段自动运行它 |
| `ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp` | UT 的 gtest main：全局 Environment 里向注册表注入 libophost.so |
| `ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt` | 定义 `transformer_op_api_ut`，含 runtime_stubs.cpp 生成与运行后清理 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/CMakeLists.txt` | 算子侧挂接点：一行 `add_modules_ut_sources` 把本目录用例交给总线 |
| `ascendc/src/tests/st/pytest.ini` | st 侧 pytest 配置：注册 `resources` marker |
| `ascendc/src/tests/st/conftest.py` | st 侧资源门禁：`--device`/`--nodes`/`--npus-per-node` 三个参数 + 收集期筛选 |
| `ascendc/src/tests/requirements.txt` | src/tests 下唯一的 python 依赖清单（仅 tensorflow 一项） |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py` | st 用例实例：本讲综合实践的运行对象 |

## 4. 核心概念与源码讲解

### 4.1 build.sh 的 UT 分支：从 `-u` 到 CMake 变量

#### 4.1.1 概念说明

`build.sh` 在 u1-l4 里被描述为「算子包编译的薄壳」。当加上 `-u` 后，它的身份变成「**单测构建器**」：不再产出 run 包，而是产出并运行一个 gtest 可执行文件。

这里有一个最容易踩的语义陷阱：`--ophost` / `--opapi` 这两个参数**有两种身份**——

- 不加 `-u` 时：它们是「库构建」开关（只编 host 侧库 / 只编 api 侧库）；
- 加 `-u` 时：它们退化为「UT 目标选择器」（编哪个 UT 可执行文件）。

原因在主流程的 `if/elif` 顺序：`ENABLE_TEST` 分支排在 `ENABLE_CREATE_LIB` 分支之前，一旦 `-u` 生效，库构建分支永远不会走到。

#### 4.1.2 核心流程

`bash build.sh -u -n ai_infra_aggregate_hidden -c ascend910_93 --ophost` 的执行流程：

```text
1. 参数解析（while case 循环）
   -u        → ENABLE_TEST=TRUE
   -n X      → ascend_op_name="ai_infra_aggregate_hidden"
   -c Y      → ascend_compute_unit="ascend910_93"
   --ophost  → OP_HOST=TRUE（同时 BUILD_LIBS+=ophost_transformer、ENABLE_CREATE_LIB=TRUE，但 UT 模式下用不到）

2. set_ut_mode()
   ENABLE_TEST=TRUE 时进入；默认 UT_TEST_ALL=TRUE
   --ophost 命中 → OP_HOST_UT=TRUE、UT_TEST_ALL=FALSE
   （--opapi 同理 → OP_API_UT=TRUE）

3. check_opapi_test_exists() / check_ophost_test_exists()
   对 -n 列出的每个算子：先 find 算子目录，再检查 tests/ut/op_host(或 op_api) 是否存在
   缺目录 → 直接 log ERROR + exit 1（编译前快速失败）

4. 注入 CMake 变量
   -DASCEND_COMPUTE_UNIT=...  -DASCEND_OP_NAME=...
   -DOP_HOST_UT=TRUE          -DENABLE_TEST=TRUE
   -DTESTS_UT_OPS_TEST=TRUE   -DENABLE_UT_EXEC=TRUE
   并 export BASE_PATH / BUILD_PATH（gtest main 里要用）

5. set_env（source setenv.bash、校验 bisheng）→ clean → cd build

6. ENABLE_TEST 主分支 → cmake_config + build_ut
   build_ut 按 OP_HOST_UT/OP_API_UT 构建 transformer_op_host_ut / transformer_op_api_ut
   （ENABLE_UT_EXEC=TRUE 使得构建完成后立即执行该可执行文件）
```

#### 4.1.3 源码精读

**参数解析：`-u` 与 `--ophost` 各自落到的变量**

[ascendc/build.sh:291-294](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L291-L294) 中 `-u|--test` 只做一件事：`ENABLE_TEST=TRUE`。它是整个 UT 模式的总开关。

[ascendc/build.sh:337-341](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L337-L341) 中 `--ophost` 同时设置了三个变量；注意 `ENABLE_CREATE_LIB=TRUE` 在 UT 模式下是「无效副作用」，因为主流程先判断 `ENABLE_TEST`。

**UT 模式判定：`set_ut_mode`**

[ascendc/build.sh:178-192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L178-L192)：默认 `UT_TEST_ALL=TRUE`（全量模式：host UT + api UT 两遍都跑）；一旦出现 `--ophost` 或 `--opapi`，`UT_TEST_ALL` 被压回 `FALSE`，只构建指定的那一个目标。这就是「加 `--ophost` 更快」的来源。

**前置检查：算子必须有 UT 目录，否则编译前就失败**

[ascendc/build.sh:231-255](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L231-L255)：`check_ophost_test_exists` 用 [find_op_dir（L194-197）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L194-L197) 在 `src/ops-transformer` 下按目录名精确匹配算子，然后要求 `<算子>/tests/ut/op_host` 存在，否则 `exit 1`。注意 `-path "*/${op_name}"` 是精确后缀匹配，所以 `-n ai_infra_aggregate_hidden` 不会误匹配到 `ai_infra_aggregate_hidden_grad` 目录。

对照的 [check_opapi_test_exists（L200-228）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L200-L228) 逻辑更宽容：算子**有 `op_api` 实现但没写 UT** 才报错；连 op_api 实现都没有（如 aggregate_hidden）只打一行 `Info: ... do not have op_api impl`，并把 `OP_API_UT` 置回 `FALSE`——后果见 4.4 的排查清单。

**CMake 变量注入与 BUILD_PATH 导出**

[ascendc/build.sh:389-405](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L389-L405)：`ENABLE_TEST` 分支追加三个 `-D`，其中 `-DENABLE_UT_EXEC=TRUE` 是「编译即运行」的开关；随后 `export BASE_PATH` / `BUILD_PATH` 指向 ascendc 根目录与 build 目录——4.2 会看到 gtest main 正是读 `BUILD_PATH` 来定位 libophost so。

**主流程分支**

[ascendc/build.sh:458-478](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L458-L478) 是 UT 模式的调度核心，三种情况：

1. `-u` 但既没 `-n` 也没 `--ophost/--opapi`（`UT_TEST_ALL=TRUE`）：先强制 `-DOP_HOST_UT=TRUE -DOP_API_UT=FALSE` 配置并构建 host UT，再交换成 `-DOP_HOST_UT=FALSE -DOP_API_UT=TRUE` 二次配置构建 api UT——**两次独立的 cmake configure**，所以全量 UT 慢。
2. `-u` 且 `UT_TEST_ALL=FALSE`（本讲场景）：走 `cmake_config` + `build_ut`。
3. `-u` 但没 `-n` 且 `UT_TEST_ALL=FALSE`：理论上不可达（不加 `--ophost/--opapi` 时 UT_TEST_ALL 恒为 TRUE），脚本防御性地打 `no ops-transformer ops to UT`。

而 [L479-480](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L479-L480) 的 `elif ENABLE_CREATE_LIB → build_lib` 只有在不带 `-u` 时才会执行，印证了 4.1.1 的「双身份」结论。

#### 4.1.4 代码实践

**实践目标**：不动手编译，先用「纸面推演 + 语法检查」确认你理解了 `-u --ophost` 的分支走向。

**操作步骤**：

1. 语法检查（不需要任何昇腾环境，任何有 bash 的机器都能做）：
   ```bash
   bash -n ascendc/build.sh && echo "syntax ok"
   ```
2. 纸面推演下表，然后逐行到 build.sh 里找到对应行号验证：

| 命令 | ENABLE_TEST | UT_TEST_ALL | OP_HOST_UT | OP_API_UT | 最终构建目标 |
| --- | --- | --- | --- | --- | --- |
| `bash build.sh -u -n X -c ascend910_93 --ophost` | TRUE | FALSE | TRUE | FALSE | transformer_op_host_ut |
| `bash build.sh -u -n X -c ascend910_93 --opapi` | TRUE | FALSE | FALSE | 视算子有无 op_api UT | transformer_op_api_ut 或无 |
| `bash build.sh -u` | TRUE | TRUE | TRUE（第一遍） | TRUE（第二遍） | 两个都建 |
| `bash build.sh --ophost`（无 -u） | 未设 | — | — | — | ophost_transformer 库（build_lib 分支） |

3. 用 help 对照参数名：[build.sh:46-65](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L46-L65) 运行 `bash ascendc/build.sh -h`，确认 `-u|--test Unit Test.` 这一行确实存在。

**需要观察的现象**：`bash -n` 应输出 `syntax ok`；上表第三行的「两遍 configure」必须能指出对应 L462-L474 的两次 `cmake ..`。

**预期结果**：能不看讲义复述「`--ophost` 在有无 `-u` 时的两种身份」。

**待本地验证**：`bash -h` 的实际输出排版。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bash build.sh -u -n sparse_lightning_indexer_grad_kl_loss_enhance -c ascend910_93 --ophost` 大概率能跑，而把算子换成某个没有 `tests/ut/op_host` 目录的算子会立刻失败？

**答案**：`check_ophost_test_exists`（build.sh:231-255）在 cmake 之前逐个检查 `-n` 清单里算子的 `tests/ut/op_host` 目录，缺失即 `exit 1`；sparse_lightning 有该目录（u8-l1 统计的 17 个 op_host 用例之一），所以能通过检查。

**练习 2**：`bash build.sh -u -n ai_infra_aggregate_hidden --opapi` 会发生什么？

**答案**：`set_ut_mode` 先置 `OP_API_UT=TRUE`，但 `check_opapi_test_exists` 发现该算子既无 `op_api` 目录也无 `tests/ut/op_api`，走 `Info: ... do not have op_api impl` 分支并把 `OP_API_UT` 置回 `FALSE`；随后 `build_ut`（build.sh:109-118）两个 if 都不命中，**什么都不构建**，只完成了一次 cmake configure。这是「看似成功实则没跑任何用例」的典型场景。

**练习 3**：`-n` 的匹配是精确的还是前缀的？如何验证？

**答案**：精确后缀匹配。`find_op_dir` 用 `find ... -type d -path "*/${op_name}"`，glob 中 `op_name` 后没有通配符，因此 `ai_infra_aggregate_hidden` 不会命中 `ai_infra_aggregate_hidden_grad` 目录（后者路径后缀是 `_grad`）。

### 4.2 CMake 装配与「编译即运行」：transformer_op_host_ut 的诞生

#### 4.2.1 概念说明

build.sh 只负责「下命令」，真正把散落在 19 个算子目录里的用例源文件聚合成一个可执行文件的，是 CMake 侧的三层结构：

- **根 CMakeLists**：`ENABLE_TEST` 时拉入 gtest/json/metadef 等依赖，并把 `src/tests/ut/framework_normal` 挂进构建树；
- **cmake/ut.cmake（总线）**：提供 `add_optiling_ut_modules` / `add_modules_ut_sources` 等函数，用 **glob** 收集用例源码；
- **每个算子的 `tests/ut/op_host/CMakeLists.txt`（挂接点）**：只有两行有效代码，把本目录交给总线。

另一个关键设计是 **ENABLE_UT_EXEC**：UT 可执行文件不是编译完躺着等你去跑，而是通过 CMake 的 `POST_BUILD` 自定义命令，**链接成功的那一刻立即被执行**。所以 build.sh 里找不到任何「运行 UT」的命令——运行被折叠进了构建。

#### 4.2.2 核心流程

```text
根 CMakeLists (ENABLE_TEST=TRUE)
  ├── 引入 gtest/json/metadef/... 三方查找模块
  └── add_subdirectory(src/tests/ut/framework_normal)     # 仅当 UT 类变量任一为 TRUE
        ├── op_host/CMakeLists.txt
        │     ├── add_optiling_ut_modules()      # 建用例静态库壳（含 faker/executor 公共件）
        │     ├── add_infershape_ut_modules()
        │     ├── libophost_transformer_ut.so    # 把 optiling/opsproto 对象聚成 so
        │     ├── transformer_op_host_ut         # gtest 可执行文件（main 在 test_op_host_main.cpp）
        │     └── POST_BUILD: ./transformer_op_host_ut   # ENABLE_UT_EXEC 时
        └── （op_api 同构，另加 runtime_stubs.cpp 生成与运行后清理）

各算子 tests/ut/**/CMakeLists.txt
  └── add_modules_ut_sources(...)
        glob: test_*_tiling.cpp → op_tiling 用例
        glob: test_*_infershape.cpp → infershape 用例
        glob: test_aclnn_*.cpp → op_api 用例
        并按 ASCEND_OP_NAME 白名单过滤（不在 -n 清单里的算子直接 return）
```

#### 4.2.3 源码精读

**根 CMakeLists 的 ENABLE_TEST 门**

[ascendc/CMakeLists.txt:25-41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L25-L41)：只有 `ENABLE_TEST` 时才 include 一串 `Find*.cmake`（gtest、json、metadef、platform、Python 等）——这解释了为什么 UT 构建对 CANN 包内的组件更「挑」。而 [L289-293](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L289-L293) 是 UT 构建树的唯一入口：`UT_TEST_ALL OR OP_HOST_UT OR OP_API_UT OR OP_KERNEL_UT OR OP_GRAPH_UT` 任一为 TRUE 才 `add_subdirectory(src/tests/ut/framework_normal)`。另外 [L158-171](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L158-L171) 显示 ENABLE_TEST 时 opsproto 会改用 `-O0 -g --coverage` 编译——UT 构建是按「可调试、可统计覆盖率」优化的，与发布构建不同。

**总线：glob 收集 + 白名单过滤**

[ascendc/cmake/ut.cmake:37-76](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L37-L76) 的 `add_optiling_ut_modules` 建了三个目标：公共对象库（只编 `tiling_context_faker.cpp` + `tiling_case_executor.cpp`，即 u8-l1 讲过的 faker 与八步执行器）、用例对象库（初始只有 [empty.cpp 占位](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/empty.cpp)）、以及聚合两者的静态库。真正把算子用例塞进对象库的是 [add_modules_ut_sources 的 tiling 分支（L202-218）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L202-L218)：

```cmake
file(GLOB OPHOST_TILING_CASES_SRC ${MODULE_DIR}/test_*_tiling.cpp)
target_sources(${MODULE_UT_NAME}_cases_obj ${MODULE_MODE} ${OPHOST_TILING_CASES_SRC)
```

两行代码实现了 u8-l1/u8-l2 说的「零注册」：**文件名 `test_*_tiling.cpp` 本身就是注册方式**。同函数先用 `get_filename_component` 从目录路径反推出算子名，再与 `ASCEND_OP_NAME`（即 `-n` 白名单）比对，不在清单内直接 `return()`——这就是 `-n` 能把编译范围缩小到单算子的机制。infershape（`test_*_infershape.cpp`）与 op_api（`test_aclnn_*.cpp`）分支同理，见 [L220-261](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L220-L261)。

**算子侧挂接点**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/CMakeLists.txt:10-13](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/CMakeLists.txt#L10-L13) 是新增算子 UT 时唯一需要写的 CMake：两行 `add_modules_ut_sources` 分别把本目录交给 tiling 与 infershape 总线。其上层 [tests/ut/CMakeLists.txt:11-17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/CMakeLists.txt#L11-L17) 只做目录遍历转发。

**可执行目标与 POST_BUILD 自动运行**

[ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt:26-30](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt#L26-L30) 先把 `optiling`/`opsproto` 等对象聚成 `libophost_transformer_ut.so`（被测代码本体）；[L44-49](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt#L44-L49) 用 `test_op_host_main.cpp` 建可执行文件（目标名 `${PKG_NAME}_op_host_ut`，`PKG_NAME=transformer` 定义于 [cmake/variables.cmake:9](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/variables.cmake#L9)）；[L56-73](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt#L56-L73) 链接时用 `-Wl,--whole-archive` 包住 `*_cases` 静态库——**必须 whole-archive**，否则 gtest 用例符号没人引用会被链接器丢弃，出现「编译成功但 0 个用例运行」。

[L76-116](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt#L76-L116) 就是「编译即运行」的落点：`ENABLE_UT_EXEC` 时挂 POST_BUILD 命令，等价于在构建目录里执行

```bash
LD_LIBRARY_PATH=$LD_LIBRARY_PATH ./transformer_op_host_ut
```

（ENABLE_ASAN 时还会额外 LD_PRELOAD libasan/libstdc++。）

**gtest main：为什么要 BUILD_PATH 环境变量**

[ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp:17-44](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp#L17-L44)：全局 `OpHostUtEnvironment::SetUp` 先设置伪造的 `soc_version` 平台信息（u8-l1 讲过的「伪造平台 JSON 戏法」的上游），然后 [L29-35](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp#L29-L35) 读环境变量 `BUILD_PATH`，拼出 `libophost_transformer_ut.so` 的路径并注入 `OpImplSpaceRegistryV2` 注册表——**tiling 函数（IMPL_OP_OPTILING 注册的那些）就是这样被 UT 进程发现的**。`BUILD_PATH` 正是 build.sh:400-404 导出的。这也解释了手动重跑 UT 时最容易犯的错（见 4.4）。

**op_api UT 的差异**

[ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt:44-56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt#L44-L56)：算子清单 `ALL_OP` 优先取 `-n` 白名单，否则 glob `attention/*` 下带 CMakeLists 的目录；[L63-89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt#L63-L89) 用 `generate_opapi_stub.py` 生成 `runtime_stubs.cpp`（u3-l4 讲过的 rt* 假实现）并直接编进可执行文件；[L125-168](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_api/CMakeLists.txt#L125-L168) 的 POST_BUILD 在跑完可执行文件后还会调用 `clean_opapi_stub.py` 清理 stub 生成物。

#### 4.2.4 代码实践

**实践目标**：在**没有 NPU、甚至没有完整 CANN 环境**的前提下，把「编译即运行」这条链路在纸面 + 目录层面走通，并搞清楚产物落在哪里。

**操作步骤**：

1. 确认用例与挂接点都在（纯文件检查，不需要环境）：
   ```bash
   ls ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/ut/op_host/
   # 预期：CMakeLists.txt  test_ai_infra_aggregate_hidden_tiling.cpp
   ```
2. 核对 glob 约定：文件名 `test_ai_infra_aggregate_hidden_tiling.cpp` 匹配 `test_*_tiling.cpp` 模式；对照 ut.cmake:216 确认。
3. 在有 CANN 包（u1-l3 的容器）的机器上执行：
   ```bash
   cd ascendc
   bash build.sh -u -n ai_infra_aggregate_hidden -c ascend910_93 --ophost --verbose
   ```
4. 构建结束后定位产物（不需要重跑，只看路径）：
   ```bash
   find build -name "transformer_op_host_ut" -o -name "libophost_transformer_ut.so" | head
   ```

**需要观察的现象**：

- cmake 输出里出现 `CURRENT_DIRS` 遍历日志（来自算子 tests/ut/CMakeLists.txt:12 的 message）；
- 构建日志末尾出现 `Run ops op_host utest`（POST_BUILD 的 COMMENT）；
- gtest 的 `[==========]` / `[ PASSED ]` 统计行与用例总数——aggregate_hidden 现有用例个数应与 u8-l2 读到的用例数一致（**待本地验证**：实际用例条数）。

**预期结果**：`transformer_op_host_ut` 位于 `build/src/tests/ut/framework_normal/op_host/` 下，与 `libophost_transformer_ut.so` 同目录；退出码 0。

**待本地验证**：第 3、4 步的实际输出（本讲义写作环境无 CANN/NPU，未执行）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 build.sh 里没有任何「运行 UT」的命令，UT 却跑了？

**答案**：`-u` 注入了 `-DENABLE_UT_EXEC=TRUE`（build.sh:399），op_host/CMakeLists.txt:76-116 在该变量为真时给 `transformer_op_host_ut` 挂 POST_BUILD 自定义命令，链接成功后立即在构建目录执行该可执行文件。运行被折叠进了构建阶段。

**练习 2**：手动重跑 `./transformer_op_host_ut` 时必须设置什么环境变量？不设会怎样？

**答案**：必须设 `BUILD_PATH` 指向 ascendc 的 build 目录（build.sh:400-404 在脚本里 export 的就是它）。不设的话 `test_op_host_main.cpp:29-33` 会打 `getenv BUILD_PATH failed.` 并 return，libophost so 不会注入注册表，所有依赖 tiling 注册的用例都会失败。完整命令形如：
```bash
cd build/src/tests/ut/framework_normal/op_host
BUILD_PATH=<ascendc绝对路径>/build ./transformer_op_host_ut
```

**练习 3**：如果把 `tests/ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp` 改名为 `aggregate_hidden_tiling_test.cpp`，会发生什么？

**答案**：不再匹配 `test_*_tiling.cpp` 的 glob（ut.cmake:216），该用例被静默排除出 `_cases_obj` 对象库；由于用例符号来自静态库且靠 whole-archive 保留，最终 gtest 报 0 个用例或编译期无任何报错——「文件名即注册」的代价是改名即失踪。u8-l2 强调文件命名是硬约定，根源在此。

### 4.3 ST 的收集与门禁：pytest.ini、conftest 与 requirements

#### 4.3.1 概念说明

ST 侧没有任何 CMake 参与：st 用例是纯 Python 文件，散落在各算子目录 `<算子>/tests/st/test_*.py` 里，运行方式就是 pytest。仓库为它们配了一个**集中的「资源门禁套件」**，位于 `src/tests/st/`：

- `pytest.ini`：把 `resources` 注册为合法 marker（避免未知 marker 告警，也让 `--strict-markers` 可用）；
- `conftest.py`：注册 `--device` / `--nodes` / `--npus-per-node` 三个命令行参数，并在收集期实现「**没有 resources marker 的用例一律 deselect**」。

设计意图：st 用例必须在真机上跑，而真机是稀缺资源（几卡、什么型号）。门禁让一条命令可以按硬件画像筛选用例——例如 910B 三卡环境只跑声明了 `device="npu:910B", npus_per_node<=3` 的用例。u8-l3 已经从「用例作者」视角讲过 marker 的写法，本讲从「运行者」视角讲它的执行机制。

#### 4.3.2 核心流程

```text
pytest 收集所有 test_*.py
   ↓
pytest_collection_modifyitems（conftest.py:83-123）
   对每个用例 item：
     1. 取最近的 resources marker；没有 → deselected（不运行，记为 deselected 条目）
     2. marker 的 device 需求 vs --device 实参：双向 fnmatch 通配，不匹配 → deselected
     3. --nodes 与 marker 的 nodes：精确相等，不等 → deselected
     4. --npus-per-node 与 marker 的 npus_per_node：精确相等，不等 → deselected
     5. 幸存者进入执行
   ↓
（pytest.ini 只负责注册 marker 名；真正的筛选逻辑全在 conftest）
```

注意筛选的默认行为是「**白名单**」：不写 marker 的用例不是「全跑」，而是「全不跑」。这与很多仓库的直觉相反，是 st「静默 deselect」问题的根源。

#### 4.3.3 源码精读

**pytest.ini：一行 marker 注册**

[ascendc/src/tests/st/pytest.ini:9-11](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/pytest.ini#L9-L11) 全部有效内容就是 `markers = resources: hardware resource requirements`。它同时承担另一个隐式职责：**作为 ini 文件锚定 pytest 的 rootdir**——pytest 从「参数共同祖先向上找 ini」确定 rootdir，找到 `src/tests/st/pytest.ini`，rootdir 就定在这里。

**conftest.py：三个参数 + 一个收集钩子**

[ascendc/src/tests/st/conftest.py:18-38](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/conftest.py#L18-L38) 注册 `--device`（如 `npu:910B`、`npu:*`）、`--nodes`、`--npus-per-node`；外层 try/catch 是为了在参数已被其它插件注册时容忍重复。

[conftest.py:40-48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/conftest.py#L40-L48) 的 `pytest_configure` 再注册一次 marker 描述并屏蔽一条 pkg_resources 弃用告警——即使 pytest.ini 没被加载，marker 也有合法定义。

[conftest.py:54-76](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/conftest.py#L54-L76) 的 `device_match` 是双向 fnmatch：`npu:*` 需求匹配任何 CLI 型号，`npu:910B` 需求也匹配 CLI 里的 `*`。CLI 未指定（None）视为通配、永远匹配。

核心在 [conftest.py:83-123](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/conftest.py#L83-L123)：

```python
mark = item.get_closest_marker("resources")
if not mark:                     # 没有 resources marker → 不跑
    deselected.append(item)
    continue
```

随后依次做 device/nodes/npus 三项比对，最后 `config.hook.pytest_deselected(items=deselected)` 把落选者上报为 deselected（终端会显示 `deselected` 计数，而非 error）。

**用例侧的配合**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py:255-258](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L255-L258)：用例类继承 torch_npu 的 `TestCase`，方法上叠 `@pytest.mark.resources(device="npu:*", npus_per_node=1)`——声明「任意 NPU 型号、单卡即可」。运行期真正触发算子调用的是 [L293-295](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L293-L295) 的 `torch.ops.custom.npu_aggregate_hidden(...)`（u6-l2 讲过的 torch 扩展入口），判分收口在 [L299-300](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L299-L300) 的 `precision_check(...)` + assert。另注意 [L12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L12) import 了 `run_tests` 但**从未调用**——本仓库 st 的唯一入口是 pytest，torch_npu 的 run_tests 是残留的 import。

**requirements.txt：一个需要诚实对待的文件**

[ascendc/src/tests/requirements.txt:1](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/requirements.txt#L1) 只有一行 `tensorflow==2.20.0`。全仓库 grep 不到任何脚本引用它，st 用例实际 import 的是 torch、torch_npu、pandas（[test 文件 L9-21](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py#L9-L21)）、omni_training_custom_ops 与 pytest。**结论：装 ST 环境不能只 `pip install -r src/tests/requirements.txt`**，tensorflow 在此的用途「待确认」（疑似历史遗留或为 op_api UT 的 Python 工具链预留）。

#### 4.3.4 代码实践

**实践目标**：不依赖 NPU，用 pytest 的收集期行为验证「资源门禁」是否真的挂在了你的运行路径上。

**操作步骤**：

1. 先只做收集，不执行（收集不需要 NPU，但 import torch_npu 需要 CANN 环境；无环境时跳到第 4 步）：
   ```bash
   cd ascendc/src/tests/st
   pytest --collect-only -q ../../../ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py
   ```
2. 观察输出的 rootdir 行：是否为 `.../ascendc/src/tests/st`；收集到的用例名列表里是否能看到 `test_aggregate_hidden_network_shape1`。
3. 加 `-k` 与资源参数做对比实验（同一台机器，三次）：
   ```bash
   pytest <上述文件路径> -k aggregate_hidden                        # 基准
   pytest <上述文件路径> --device npu:910B                          # 换个具体型号
   pytest <上述文件路径> --device npu:910B --npus-per-node 8        # 卡数不匹配
   ```
4. 无 NPU 环境时的替代实践（纯阅读）：对照 conftest.py:92-119 逐行回答——用例 marker 是 `device="npu:*", npus_per_node=1`，三次实验中 `cli_device`/`cli_npus` 分别是什么？哪些用例会被 `deselected`？

**需要观察的现象**：

- 第 2 步：终端头部 `rootdir: ...` 与 `plugins:` 行；
- 第 3 步：三次运行的 `selected` / `deselected` 计数差异；特别是 `--npus-per-node 8` 那次，用例声明 `npus_per_node=1`，按 conftest.py:115-117 的精确相等判断应被 deselect；
- 若 rootdir **不是** `src/tests/st`（比如直接在算子目录里发起 pytest），收集仍会成功、用例照常运行，但 `--device` 参数会因 conftest 未加载而报 `unrecognized arguments`——这是判断门禁是否生效的试金石。

**预期结果**：门禁生效时，`--device npu:910B` 不会 deselect `device="npu:*"` 的用例（双向通配），`--npus-per-node 8` 会 deselect `npus_per_node=1` 的用例。

**待本地验证**：本讲义写作环境无 CANN/NPU，第 1-3 步未执行；尤其「从 src/tests/st 目录发起、目标是其兄弟子树里的文件」时 conftest 的加载行为随 pytest 版本可能有差异，请以实际 `rootdir`/`unrecognized arguments` 输出为准。

#### 4.3.5 小练习与答案

**练习 1**：一条 st 用例什么 marker 都没加，`pytest` 跑完显示 `1 deselected`，为什么它没执行？

**答案**：conftest.py:95-98（`if not mark: deselected.append(item); continue`）把没有 `resources` marker 的用例一律列入 deselect。这是白名单式门禁：marker 是 st 用例的「准跑证」。补上 `@pytest.mark.resources(device="npu:*", npus_per_node=1)` 即可。

**练习 2**：`--device` 的匹配是精确字符串比较吗？`npu:*` 与 `npu:910B` 谁能匹配谁？

**答案**：不是精确比较，是双向 fnmatch 通配（conftest.py:72-75：`fnmatch.fnmatch(cli_device, d) or fnmatch.fnmatch(d, cli_device)`）。因此用例声明 `npu:*`（需求任意）可匹配 CLI 的 `npu:910B`；反过来用例声明 `npu:910B`、CLI 给 `*` 也能匹配。CLI 完全不传 `--device` 时视为无条件通配（L64-66）。

**练习 3**：`pytest.ini` 与 `conftest.py` 里的 `pytest_configure` 都注册了 `resources` marker，重复吗？

**答案**：不冲突且各有用途。pytest.ini 只在其被选为 rootdir 配置时生效；`pytest_configure`（conftest.py:41-44）的 `config.addinivalue_line("markers", ...)` 是运行时注册，保证即使 ini 未加载 marker 也有定义。两者是「双保险」关系。

### 4.4 常见问题排查：编译依赖、运行环境与静默失败

#### 4.4.1 概念说明

把前三个模块的机制反过来用，就是排查手册。三类问题对应三条链路：

- **编译不过** → build.sh 前置检查 / set_env / CMake 依赖（4.1、4.2）；
- **编译过了但没跑用例** → glob 没命中 / 白名单过滤 / OP_API_UT 被置回 FALSE / whole-archive 之外的符号丢失（4.1、4.2）；
- **ST 用例没执行或 import 失败** → marker 门禁 / rootdir 挂接 / wheel 与 run 包的安装顺序（4.3、u6-l1/u6-l2）。

#### 4.4.2 核心流程（排查决策树）

```text
bash build.sh -u ... 失败？
 ├─ "bisheng compilation tool not found"      → set_env 失败，先 source CANN 的 set_env.sh（build.sh:72-82）
 ├─ "operator directory not found for 'X'"    → -n 的名字与算子目录名不一致（build.sh:209-211）
 ├─ "operator X op_host test not created"     → 该算子没有 tests/ut/op_host 目录（build.sh:246-248）
 └─ cmake 阶段缺 gtest/json/metadef 头        → CANN 包版本与镜像不配套（根 CMakeLists:25-41 的 Find 模块来自包内）

编译成功但 0 个用例？
 ├─ 用例文件名不匹配 test_*_tiling.cpp / test_aclnn_*.cpp   → 改名即失踪（ut.cmake:216/259）
 ├─ 算子不在 -n 白名单                                        → add_modules_ut_sources 直接 return（ut.cmake:207-211）
 └─ --opapi 但算子无 op_api 实现                              → OP_API_UT 被置回 FALSE，build_ut 空转（build.sh:217-224）

pytest 侧？
 ├─ "1 deselected"                                            → 用例缺 resources marker（conftest.py:95-98）
 ├─ "unrecognized arguments: --device"                        → conftest 未加载，检查 rootdir 是否锚定在 src/tests/st
 ├─ "No module named 'omni_training_custom_ops'"              → 先装 torch_ops_extension wheel（u6-l1）
 └─ torch.ops.custom.npu_aggregate_hidden 不存在              → run 包未安装或未 source vendors 的 set_env.bash（u1-l4）
```

#### 4.4.3 源码精读（排查依据索引）

| 症状 | 源码依据 |
| --- | --- |
| bisheng 缺失即退出 | [build.sh:72-82](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L72-L82) |
| 算子目录/UT 目录缺失即退出 | [build.sh:200-228](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L200-L228)、[build.sh:231-255](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L231-L255) |
| 「没有算子可测」的提示分支 | [build.sh:459-461](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L459-L461) |
| glob 文件名约定 | [ut.cmake:216](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L216)、[ut.cmake:234](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L234)、[ut.cmake:259](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L259) |
| -n 白名单过滤 | [ut.cmake:207-211](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/ut.cmake#L207-L211) |
| whole-archive 保住用例符号 | [op_host/CMakeLists.txt:62-64](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/CMakeLists.txt#L62-L64) |
| BUILD_PATH 缺失时的日志 | [test_op_host_main.cpp:29-33](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/ut/framework_normal/op_host/test_op_host_main.cpp#L29-L33) |
| 无 marker 即 deselect | [conftest.py:92-98](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/conftest.py#L92-L98) |
| --device 参数注册处 | [conftest.py:18-38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/conftest.py#L18-L38) |

#### 4.4.4 代码实践

**实践目标**：为一台「什么都没装」的新机器产出一份可核对的前置条件清单，并演练三个最常见故障的判别。

**操作步骤**：

1. 按依赖顺序写出前置条件清单（这是综合实践的半成品，此处先列 UT 部分）：

| # | 前置条件 | 用于 | 对应讲义 |
| --- | --- | --- | --- |
| 1 | 昇腾驱动 + 配套容器（A2/A3/A5 镜像） | 全部 | u1-l3 |
| 2 | `source <CANN>/set_env.sh`（build.sh 会再 source setenv.bash） | UT 编译、ST | u1-l3 |
| 3 | CANN 包内有 bisheng、gtest/json/metadef 等组件 | UT 编译 | u1-l4 |
| 4 | 自定义算子 run 包已安装 + source vendors 的 set_env.bash | ST（torch.ops.custom 符号） | u1-l4 |
| 5 | torch_ops_extension wheel 已 pip install | ST（import omni_training_custom_ops） | u6-l1 |
| 6 | python 侧 torch、torch_npu、pytest、pandas、numpy | ST | 本讲 4.3 |
| 7 | 真实 NPU 设备（npu-smi 可见） | 仅 ST（UT 不需要） | u8-l1 |

2. 故障判别演练（阅读型，逐条写出你的判断依据行号）：
   - 故障 A：`pytest` 输出 `no tests ran`（0 selected / 3 deselected）；
   - 故障 B：`pytest: error: unrecognized arguments: --device`；
   - 故障 C：UT 构建日志有 `Run ops op_host utest`，但 gtest 输出 `[==========] 0 tests`。

**需要观察的现象 / 预期结果**：

- 故障 A → conftest.py:95-98 的白名单门禁（用例缺 marker，或 marker 的 npus_per_node 与 `--npus-per-node` 实参不等，见 L115-117）；
- 故障 B → conftest.py 没有被加载，pytest 的 rootdir 不在 `src/tests/st`（`--device` 只在 conftest 的 `pytest_addoption` 里注册）；
- 故障 C → 用例源文件没进 `_cases_obj`：要么文件名不匹配 ut.cmake:216 的 glob，要么算子被 -n 白名单过滤（ut.cmake:207-211）。

**待本地验证**：三个故障的复现命令与实际终端输出。

#### 4.4.5 小练习与答案

**练习 1**：同事报告「`bash build.sh -u -n X -c ascend910_93 --opapi` 秒过，但一个用例都没跑」。给出最可能的两个原因。

**答案**：(1) 算子 X 没有 op_api 实现（无 op_api 目录），check_opapi_test_exists（build.sh:220-224）打 Info 并把 OP_API_UT 置回 FALSE，build_ut 两个 if 都不命中；(2) 算子有 op_api 实现但没有 `tests/ut/op_api` 目录——不过这种情况会直接 `exit 1` 报 `op_api test not created`，不会「秒过」，所以更可能是 (1)。

**练习 2**：为什么 op_host UT 可以在没有 NPU 的机器上运行，而 ST 不行？各举一处源码依据。

**答案**：op_host UT 是宿主机 gtest 程序，其被测环境由 faker 伪造（tiling_context_faker.cpp 编入 `add_optiling_ut_modules` 的公共对象库，ut.cmake:40-42）、下游依赖由 so/stub 提供（libophost_transformer_ut.so + POST_BUILD 直接 `./transformer_op_host_ut`，op_host/CMakeLists.txt:108-114），全程不触碰设备。ST 用例在测试体内直接 `.to("npu")` 并调用 `torch.ops.custom.npu_aggregate_hidden`（test_ai_infra_aggregate_hidden.py:282-295），需要真实设备与已安装的算子二进制。

**练习 3**：UT 全量模式（`bash build.sh -u`，不带 -n）为什么要做两次 cmake configure？

**答案**：build.sh:462-474 的 UT_TEST_ALL 分支先用 `-DOP_HOST_UT=TRUE -DOP_API_UT=FALSE` 配置并构建 `transformer_op_host_ut`，成功后再交换为 `-DOP_HOST_UT=FALSE -DOP_API_UT=TRUE` 重新配置构建 `transformer_op_api_ut`。两类 UT 的 CMake 装配互斥（op_host/op_api 的 CMakeLists 分别以 OP_HOST_UT/OP_API_UT 为门，op_host/CMakeLists.txt:13、op_api/CMakeLists.txt:13），一次只能开一类，因此分两遍。

## 5. 综合实践

**任务：把一条完整的「UT → 安装 → ST」测试链在本机跑通；无 NPU 时产出完整的前置条件清单与「卡在哪一步」的证据。**

### 步骤一：跑通 op_host UT（需要 CANN 包，不需要 NPU）

```bash
cd ascendc
bash build.sh -u -n ai_infra_aggregate_hidden -c ascend910_93 --ophost
```

记录三样东西：

1. 退出码；
2. gtest 统计行（`[==========] Running N tests` 与 `[ PASSED ] N tests` 的 N）；
3. 产物路径：`find build -name transformer_op_host_ut`。

然后做一次「手动重跑」验证 4.2 的结论：

```bash
cd build/src/tests/ut/framework_normal/op_host
BUILD_PATH=$(cd ../../../.. && pwd) ./transformer_op_host_ut
```

（`build/src/tests/ut/framework_normal/op_host` 向上四级即 `build`；对照 test_op_host_main.cpp:35 的 so 拼路径验证。）

### 步骤二：安装算子包与 wheel（ST 的前置，需要 NPU 容器）

```bash
# 1) 算子 run 包（u1-l4）
cd ascendc && bash build.sh -n ai_infra_aggregate_hidden -c ascend910_93
cd output && chmod +x CANN-omni_training_custom_ops-*.run
./CANN-omni_training_custom_ops-*.run --quiet --install-path=/usr/local/Ascend/ascend-toolkit/latest/opp
source /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/omni_training_custom_transformer/bin/set_env.bash

# 2) torch 扩展 wheel（u6-l1）
cd ../torch_ops_extension && bash build_and_install.sh
```

### 步骤三：运行 ST 并筛选

```bash
cd ascendc/src/tests/st
pytest ../../../ops-transformer/mome/ai_infra_aggregate_hidden/tests/st/test_ai_infra_aggregate_hidden.py \
       -k aggregate_hidden -v
```

记录：rootdir 行、selected/deselected 计数、每个用例的 PASS/FAIL 与 `precision_check` 是否触发 assert（u8-l3 讲过的三方对比在这里落地）。

### 无 NPU 环境时的替代交付

如果当前机器没有 NPU/CANN，完成以下内容并在文末标注「待本地验证」：

1. 4.4.4 的 7 项前置条件清单，标注哪几项缺失（例如：本环境缺第 1/2/3 项 → UT 编译不可行；缺第 1/4/5/7 项 → ST 不可行）；
2. `bash -n ascendc/build.sh` 的语法检查结果；
3. `pytest --collect-only` 在无 torch_npu 环境会因 import 失败而收集报错——记录报错堆栈的首行（`ModuleNotFoundError: No module named 'torch_npu'` 之类），作为「第 6 项前置缺失」的直接证据；
4. 用 4.4.2 的决策树写明：链路分别断在哪一行脚本/哪一条 import。

## 6. 本讲小结

- `-u` 是 UT 模式总开关；`--ophost`/`--opapi` 在 UT 模式下退化为「目标选择器」（主流程 `ENABLE_TEST` 分支优先于 `ENABLE_CREATE_LIB`），全量 `-u` 则做 host→api 两遍 cmake configure。
- build.sh 在 cmake 之前做两道前置检查：算子目录必须找得到、`tests/ut/op_host`（或 op_api 的相应目录）必须存在，缺一即 `exit 1`；`-n` 是精确目录名匹配。
- 「零注册」的机制在 `cmake/ut.cmake`：`add_modules_ut_sources` 按 `test_*_tiling.cpp` / `test_*_infershape.cpp` / `test_aclnn_*.cpp` 的文件名 glob 收集用例，并按 `-n` 白名单过滤；文件改名即从 UT 中静默消失。
- UT 是「编译即运行」：`-DENABLE_UT_EXEC=TRUE` 让 `transformer_op_host_ut` 的 POST_BUILD 命令在链接成功后立即执行；手动重跑必须补 `BUILD_PATH` 环境变量，否则 libophost so 注入不了注册表。
- ST 完全不走 CMake：`src/tests/st` 的 pytest.ini + conftest.py 构成资源门禁，`resources` marker 是白名单准跑证，`--device` 双向 fnmatch、`--nodes`/`--npus-per-node` 精确相等；门禁是否生效取决于 conftest 是否被加载（rootdir 挂接）。
- `src/tests/requirements.txt`（仅 tensorflow）不是 ST 的真实依赖清单；ST 实际依赖 torch/torch_npu/pytest/pandas/omni_training_custom_ops 与已安装的算子 run 包。

## 7. 下一步学习建议

本讲是测试体系（单元 8）的收官，整条「写用例（u8-l2/u8-l3）→ 跑用例（本讲）」链路已闭环。建议：

1. **进入单元 9 的高级主题**：先读 [u9-l1 多芯片与多架构适配](u9-l1-multi-soc-adaptation.md)，把本讲的 `-c ascend910_93` 放到 arch32/arch35 的全景里理解。
2. **源码延伸阅读**：`ascendc/cmake/ut.cmake` 的 `AddOpTestCase`（L265 起）展示了 op_kernel UT 的另一种装配方式（编译期生成 tiling 头文件），可对照 u8-l1 说的「op_kernel UT 基建备而未用」自行评估其完整度。
3. **动手巩固**：按 u8-l2 的练习给 aggregate_hidden 新增一个 tiling 用例后，用本讲的命令重新编译运行，验证「新文件零注册即被收集」——这是检验你是否真正理解 glob 总线的最好实验。
4. **横向对比**：把 pypto 的 st 测试（u7-l4，pytest 参数化驱动）与本讲 ascendc 的 st 门禁对比，理解两类算子工程在测试组织上的同源与差异。
