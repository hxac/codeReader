# 单元测试体系：UT 框架与 infershape/tiling UT

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 ops-nn 的 UT（Unit Test，单元测试）体系由哪几类测试组成、分别验证哪一层交付件。
2. 熟练使用 `build.sh` 的 `-u` 系列命令：全量 UT、按层（ophost/opapi/opkernel）UT、单算子 UT、只编不跑、指定 soc 编译。
3. 理解 UT 用例文件是如何被 CMake 按命名约定**自动收集**的——新写一个 `test_*_xxx.cpp` 放对目录即可生效，无需登记。
4. 掌握 infershape UT 与 tiling UT 两条 host 侧测试的编写套路：构造 `InfershapeContextPara` / `TilingContextPara`、设定预期、调用 `ExecuteTestCase`。
5. 能在本地跑通 add_example 的全部 host 侧 UT，并读懂（以及故意触发）gtest 的失败报告。

## 2. 前置知识

- **什么是 UT**：在不上真机（NPU）的情况下，把算子的某一段 Host 侧逻辑（shape 推导、tiling 计算）当作普通 C++ 函数来测。官方文档明确说明「UT 验证无需 NPU 环境」，这是它与 aclnn 调用验证（需要真机）的最大区别。
- **googletest（gtest）**：Google 的 C++ 单元测试框架。核心概念只有三个：
  - `TEST_F(TestSuiteName, CaseName)` 定义一个用例；
  - `EXPECT_EQ` / `ASSERT_EQ` 做断言（EXPECT 失败后继续跑，ASSERT 失败立即中止当前用例）；
  - 运行后输出 `[ PASSED ]` / `[ FAILED ]` 汇总。
- **Faker（上下文伪造器）**：tiling/infershape 函数的正常运行需要框架传入 `gert::TilingContext` / `gert::InferShapeContext`，里面装着 shape、dtype、平台信息等。UT 环境里没有真框架，于是仓库提供了一批 *Faker* 类来手工伪造这些上下文。你在 u3-l2（infershape）和 u4-l1/u4-l2（tiling）已经见过真实的 `gert::TilingContext` 与 TilingData，本讲就是把它们「装进测试」。
- **前两讲的衔接**：u3-l2 讲过 `IMPL_OP_INFERSHAPE` 把推导函数注册进注册表，u4-l2 讲过 tiling 函数经注册表被框架回调。UT 执行器正是**从同一个注册表里取出你注册的函数**直接调用——所以 UT 测的就是真实交付件代码，而不是测试替身。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/install/compile.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md) | 本地验证一节：`-u` 系列命令的官方清单与成功输出样例 |
| [docs/zh/develop/aicore_develop_guide.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md) | 算子开发指南的「UT 验证」章节：UT 目录约定与三类 UT 的编写指导 |
| [build.sh](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh) | `-u` 参数解析、互斥校验、UT target 的组装与编译执行 |
| [tests/ut/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/CMakeLists.txt) | 顶层 UT 工程入口：自动收集子目录 |
| [cmake/ut.cmake](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/ut.cmake) | `add_modules_ut_sources` 等函数：按文件名通配符收集用例源码 |
| [examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp) | Infershape UT 样例 |
| [examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp) | Tiling UT 样例（本讲精读对象） |
| [tests/ut/common/tiling_context_faker.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_context_faker.h) | `TilingContextPara`（用例参数包）与 `TilingContextFaker` 定义 |
| [tests/ut/common/tiling_case_executor.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.cpp) | tiling UT 执行器：伪造上下文、回调真实 tiling 函数、逐项断言 |
| [tests/ut/common/infershape_case_executor.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/infershape_case_executor.h) | infershape UT 执行器入口声明 |
| [examples/add_example/tests/ut/op_kernel/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_kernel/CMakeLists.txt) | kernel UT 的注册方式（`AddOpTestCase`，留给 u7-l2 详讲） |
| [tests/requirements.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/requirements.txt) | UT 依赖的 Python 包清单（如 tensorflow，供 kernel UT 生成对标数据用） |

## 4. 核心概念与源码讲解

### 4.1 UT 体系全景与 build.sh 的 -u 命令族

#### 4.1.1 概念说明

ops-nn 的 UT 按被测交付件分成五类 target：

| UT 类别 | 构建开关 | 被测对象 | 是否需要 NPU |
| --- | --- | --- | --- |
| op_host UT | `--ophost` / `-u` 默认包含 | infershape、tiling（Host 侧纯 C++） | 否 |
| op_graph UT | `--opgraph` / `-u` 默认包含 | 图融合 pass | 否 |
| op_api UT | `--opapi` / `-u` 默认包含 | aclnn 适配层参数校验 | 否 |
| op_kernel UT | `--opkernel` / `-u` 默认包含 | AI Core kernel 仿真执行 | 否（走 CPU 仿真，下一讲详讲） |
| aicpu op_kernel UT | `--opkernel_aicpu_test` | AI CPU kernel | 否 |

`-u` 不带其他开关时表示「全部都跑」；带 `--ophost` 等开关则只跑对应一类。

#### 4.1.2 核心流程

build.sh 处理 UT 的流程：

```text
解析参数（-u / --ophost / --opapi / --opgraph / --opkernel / --noexec / --ops= / --soc=）
    ↓
互斥校验：--pkg、--opkernel(交付件编译) 不能与 -u 系列同时出现
    ↓
set_ut_mode()：根据开关把 UT_TARGES 填成 nn_op_host_ut / nn_op_graph_ut / ... 五个 target 名
    ↓
进入 build 目录重新 cmake（并清掉旧的 op_info_cfg json，避免多次执行互相干扰）
    ↓
cmake --build . --target ${UT_TARGES[@]}（编译 + CMake 自动在构建后执行测试）
    ↓
gtest 输出 [  PASSED  ] / [  FAILED  ] 汇总
```

#### 4.1.3 源码精读

**① 官方命令清单**。[docs/zh/install/compile.md:245-259](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L245-L259) 给出六种用法：单算子按层执行（方式 1）、全量执行（方式 2）、只编不执行 `--noexec`（方式 3）、按层执行（方式 4）、按层只编不执行（方式 5）、指定 soc 编译（方式 6）。注意方式 1 就是本讲实践要用的形态：

```bash
pip3 install -r tests/requirements.txt   # 先装 UT 依赖
bash build.sh -u --ophost --ops=add_example
```

成功标志见 [docs/zh/install/compile.md:262-271](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L262-L271)：终端出现 `[  PASSED  ] ${n} tests.` 和 `[100%] Built target nn_op_host_ut`。

**② build.sh 的帮助与互斥规则**。[build.sh:234-250](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L234-L250) 列出 Test Options：`-u` 编译并执行全部 UT、`--noexec` 只编译不执行、`--ophost`/`--opgraph`/`--opapi`/`--opkernel` 与 `-u` 组合等价于只跑该层，还有 `--ut_mode=<debug|fast>` 与 kernel UT 专用的 `--ut_timeout=<N>`（默认 120 秒）。[build.sh:456-462](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L456-L462) 说明互斥约束：`--pkg`（出算子包）和交付件编译模式都不能与 `-u` 同时使用——**编包和跑测是两条互斥路径**，这与 u1-l2 讲过的「`--pkg` 与 `--run_example` 互不触发」一脉相承。

**③ set_ut_mode 组装 target**。[build.sh:610-665](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L610-L665)：进入 UT 模式后先 `ENABLE_CUSTOM=FALSE`、`UT_TEST_ALL=TRUE`；只要显式指定了某一层（如 `--ophost`），就把 `UT_TEST_ALL` 降为 FALSE，只向 `UT_TARGES` 追加该层的 target（如 `nn_op_host_ut`）；若五个开关一个都没选则直接报错退出。最终在 [build.sh:1489](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1489) 用一条 `cmake --build . --target ${UT_TARGES[@]}` 同时编译并触发测试；[build.sh:1459](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1459) 在每次 UT 前删除 `build/tbe/op_info_cfg/ai_core/` 下的旧 json，强制重新生成，避免上一次执行的残留干扰本次。

#### 4.1.4 代码实践

1. **实践目标**：跑通 add_example 的 host 侧（infershape + tiling）UT。
2. **操作步骤**（待本地验证，需已按 u1-l2 配好 CANN 环境与编译依赖）：

   ```bash
   pip3 install -r tests/requirements.txt
   bash build.sh -u --ophost --ops=add_example
   ```

3. **需要观察的现象**：终端滚动输出 gtest 的 `[ RUN      ] AddExampleInfershape.xxx` 与 `[ RUN      ] AddExampleTiling.xxx`，最后出现 `[  PASSED  ] 3 tests.`（1 个 infershape 用例 + 2 个 tiling 用例）与 `[100%] Built target nn_op_host_ut`。
4. **预期结果**：3 个用例全部 PASSED。若想扩大范围，可改用 `bash build.sh -u --ops=add_example`（不指定层，五类 UT 全跑，kernel UT 也会被编译执行，耗时更长）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bash build.sh --pkg -u` 会报错？
**答案**：`--pkg` 走「编译产出算子 run 包」路径，`-u` 走「编译并执行测试」路径，build.sh 在 [build.sh:456-457](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L456-L457) 显式互斥校验两者不能共存，目的之一是避免打包产物与测试中间产物互相污染构建目录。

**练习 2**：只想检查 UT 代码能否编译通过（例如 CI 里先编译后集中执行），该加什么参数？
**答案**：加 `--noexec`，即 `bash build.sh -u --noexec --ophost --ops=add_example`（见 [docs/zh/install/compile.md:249-250](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/compile.md#L249-L250)）。

### 4.2 UT 工程组织：用例如何被自动收集

#### 4.2.1 概念说明

ops-nn 上千个算子、每个算子若干 UT 文件，如果都要手工登记清单将不可维护。仓库的解法是**「目录 + 文件名通配」双约定**：

- **目录约定**：算子工程的 `tests/ut/op_host/` 下放 host 侧用例，`tests/ut/op_kernel/` 下放 kernel 用例；仓库级公共设施在 `tests/ut/common/`（执行器、faker）。
- **文件名约定**：`test_*_infershape.cpp`、`test_*_tiling.cpp`、`test_aclnn_*.cpp`、`test_*_pass.cpp`、`test_<op_name>.cpp` 各自被对应的收集函数用 `file(GLOB)` 捞走。

只要文件名和目录对得上，**新写用例零登记、自动编入**——这与 u6-l3 讲过的大类 CMakeLists 用 `file(GLOB)` 收集算子目录是同一设计哲学。

#### 4.2.2 核心流程

```text
tests/ut/CMakeLists.txt 递归 add_subdirectory（跳过 common）
    ↓
算子工程 tests/ut/op_host/CMakeLists.txt 调 add_modules_ut_sources(DIR 当前目录)
    ↓
cmake/ut.cmake 按文件名 GLOB：
    test_*_tiling.cpp     → 归入 ${OP_TILING_MODULE_NAME}_cases_obj
    test_*_infershape.cpp → 归入 ${OP_INFERSHAPE_MODULE_NAME}_cases_obj
    test_aclnn_*.cpp      → 归入 ${OP_API_MODULE_NAME}_cases_obj
    test_*_pass.cpp       → 归入 ${OP_GRAPH_MODULE_NAME}_cases_obj
    ↓
链接成 nn_op_host_ut 等 gtest 可执行目标
```

#### 4.2.3 源码精读

**① 顶层自动收集**。[tests/ut/CMakeLists.txt:12-17](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/CMakeLists.txt#L12-L17)：GLOB 当前目录所有子目录，只要里面有 `CMakeLists.txt` 且不叫 `common`，就 `add_subdirectory`——所以新增算子 UT 目录同样无需登记。

**② 算子侧一行接入**。[examples/add_example/tests/ut/op_host/CMakeLists.txt:11-15](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/CMakeLists.txt#L11-L15)：当 `UT_TEST_ALL` 或 `OP_HOST_UT` 打开时，把当前目录同时挂到 tiling 模块和 infershape 模块两个用例目标上：

```cmake
if(UT_TEST_ALL OR OP_HOST_UT)
    add_modules_ut_sources(HOSTNAME ${OP_TILING_MODULE_NAME} MODE PRIVATE DIR ${CMAKE_CURRENT_SOURCE_DIR})
    add_modules_ut_sources(HOSTNAME ${OP_INFERSHAPE_MODULE_NAME} MODE PRIVATE DIR ${CMAKE_CURRENT_SOURCE_DIR})
endif()
```

**③ 按名字分拣**。[cmake/ut.cmake:324-356](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/ut.cmake#L324-L356)：`add_modules_ut_sources` 在 `${MODULE_DIR}` 下 GLOB `test_*_tiling.cpp` 与 `test_*_infershape.cpp`（指定了 `ASCEND_COMPUTE_UNIT` 时还会额外捞芯片子目录里的同名文件，服务于 u9-l4 将讲的多架构场景），再按 hostname 中是否含 "tiling"/"infershape" 字样分拣进对应 `..._cases_obj` 目标；后续段落同样处理 `test_aclnn_*.cpp` 与 `test_*_pass.cpp`。若该目标尚不存在，会先调用 `add_optiling_ut_modules` / `add_infershape_ut_modules` 创建——这意味着**只有目录里真的存在匹配文件时才会触发编译目标创建**，目录可以为空而不报错。

**④ kernel UT 的另一种注册**。[examples/add_example/tests/ut/op_kernel/CMakeLists.txt:16-18](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_kernel/CMakeLists.txt#L16-L18) 用的是函数 `AddOpTestCase(add_example "ascend910B1" "")`，参数依次是算子名、支持的 soc 版本、编译宏（如 `-DDTYPE_X=float`）。它与 host UT 的 GLOB 机制不同，细节留给 u7-l2。

#### 4.2.4 代码实践

1. **实践目标**：验证「命名即注册」机制。
2. **操作步骤**：在 `examples/add_example/tests/ut/op_host/` 下新建 `test_add_example_tiling_extra.cpp`，内容先照抄 `test_add_example_tiling.cpp`（只改测试类名和用例名，避免重复定义）。重新执行 `bash build.sh -u --ophost --ops=add_example --noexec`，观察 cmake 输出中 `Debug<add_modules_ut_sources>` 打印的源文件列表。
3. **需要观察的现象**：日志里 `OPHOST_TILING_SRCS` 同时包含新旧两个 `test_*_tiling.cpp`；用例总数比原来多。
4. **预期结果**：新文件被自动编入，无需改任何 CMakeLists。（注意：实验后请删除该文件，本讲禁止修改源码目录以外的约定时也应恢复现场；正式添加用例时才保留。）

#### 4.2.5 小练习与答案

**练习**：如果把 tiling 用例文件改名为 `test_add_example_tiling_v2_backup.cpp.bak`，它还会被编入 UT 吗？
**答案**：不会。GLOB 模式是 `test_*_tiling.cpp`，`.bak` 后缀不匹配 `*.cpp` 结尾的模式，文件会被忽略——这也是临时备份 UT 文件的安全做法。

### 4.3 Infershape UT：三步一个用例

#### 4.3.1 概念说明

Infershape UT 验证的是 `*_infershape.cpp` 里注册的推导函数（u3-l2 精读过）：给定输入描述，断言返回码与输出 shape 是否符合预期。它的编写成本极低——不用真框架，一个「参数包 + 预期值 + 一行执行」就是完整用例。

#### 4.3.2 核心流程

```text
1. 构造 gert::InfershapeContextPara（算子名 + 输入/输出描述列表）
2. 写期望：expectResult（GRAPH_SUCCESS/GRAPH_FAILED）+ expectOutputShape
3. ExecuteTestCase(...)：
     faker 组装 InferShapeContext → 从注册表取算子的 InferShape 回调 → 真实执行
     → 逐维比对输出 shape → gtest 断言
```

#### 4.3.3 源码精读

**① 样例全文结构**。[examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp:22-36](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_infershape.cpp#L22-L36)：

```cpp
gert::InfershapeContextPara infershapeContextPara(
    "AddExample",
    {
        {{{1, -1, -1, 64}, {1, -1, -1, 64}}, ge::DT_FLOAT16, ge::FORMAT_ND}, // 输入1
        {{{1, -1, -1, 64}, {1, -1, -1, 64}}, ge::DT_FLOAT16, ge::FORMAT_ND}, // 输入2
    },
    {
        {{{}, {}}, ge::DT_FLOAT16, ge::FORMAT_ND}, // 输出（shape 留空，等推导填充）
    });
std::vector<std::vector<int64_t>> expectOutputShape = { {1, -1, -1, 64} };
ExecuteTestCase(infershapeContextPara, ge::GRAPH_SUCCESS, expectOutputShape);
```

每个张量描述是一个三元组 `{shape, originShape}, dtype, format`；`-1` 表示动态维度（u3-l2 讲过 infershape 对 -1 做透传），所以期望输出就是 `{1, -1, -1, 64}`。

**② 执行器接口**。[tests/ut/common/infershape_case_executor.h:16-18](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/infershape_case_executor.h#L16-L18) 声明了唯一的入口：

```cpp
void ExecuteTestCase(gert::InfershapeContextPara& infershapeContextPara,
                     ge::graphStatus expectResult = ge::GRAPH_FAILED,
                     const std::vector<std::vector<int64_t>>& expectOutputShape = {});
```

注意默认期望是 `GRAPH_FAILED`——**不传期望就等于断言失败**，倒逼用例作者显式写清预期，这是防御式默认值设计。u3-l2 已展示过 `InfershapeContextPara` + `ExecuteTestCase` 的完整机制，此处不重复。

**③ 官方编写指导**。[docs/zh/develop/aicore_develop_guide.md:480-543](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L480-L543) 给出通用模板：包含 `gtest/gtest.h` + faker/executor 头文件、测试类继承 `testing::Test`、命名建议 `${OpName}InfershapeTest` / `test_case_xxx`；并特别提醒——若输入在 def 文件中标记为 `ValueDepend`，UT 必须同时传入该输入的**真实数据值**（`TensorDescription` 追加 `true` 与 `constValue` 指针，见 [tests/ut/common/tiling_context_faker.h:26-34](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_context_faker.h#L26-L34) 的构造签名）。

#### 4.3.4 代码实践

1. **实践目标**：为 add_example 增加一个 INT32 标量场景的 infershape 用例（作为源码阅读型实践，写在纸上或临时分支，待本地验证）。
2. **操作步骤**：复制 `add_example_infershape_test1` 用例，把 dtype 改为 `ge::DT_INT32`、shape 改为 `{{1}, {1}}`，期望输出改为 `{1}`。
3. **需要观察的现象**：重新跑 `bash build.sh -u --ophost --ops=add_example` 后该用例出现在运行列表中并通过。
4. **预期结果**：PASSED——因为 add_example 的 infershape 是逐维复制策略（u3-l2），标量输入 `{1}` 会被原样复制为输出 `{1}`。若失败，说明对推导策略的理解有偏差，应回到 `add_example_infershape.cpp` 核对。

#### 4.3.5 小练习与答案

**练习**：为什么用例中输出张量的 shape 写成 `{{}, {}}`（空）？
**答案**：输出 shape 正是被测函数要推导的结果，测试前它不存在；faker 只需要输出槽位的 dtype/format 信息来构造上下文，shape 留空由推导函数填充，随后执行器拿填充结果与 `expectOutputShape` 逐维比对。

### 4.4 Tiling UT：参数包、执行器与断言内幕

#### 4.4.1 概念说明

Tiling UT 验证 `*_tiling.cpp` 的切分逻辑（u4-l1/u4-l2 精读过两级切分与 TilingData 契约）。它比 infershape UT 多两个关注点：

- **平台参数可注入**：核数、UB 大小是 tiling 的关键输入，`TilingContextPara` 允许直接指定（默认 coreNum=64、ubSize=262144），从而可以在用例里精确复现某个芯片场景。
- **TilingData 以字符串比对**：TilingData 是 POD 字节块（u4-l2），执行器把它按 int64 逐元素转成 `"a b c "` 形式的字符串再与期望字符串 `EXPECT_EQ`——字段顺序就是结构体声明顺序。

#### 4.4.2 核心流程

```text
用例侧：
  TilingContextPara(算子名, 输入描述, 输出描述, attrs, compileInfo, coreNum, ubSize, tilingDataSize)
  + 期望值（expectTilingKey / expectTilingData 字符串 / expectWorkspaces）
  → ExecuteTestCase(para, expectResult, expectTilingKey, expectTilingData, expectWorkspaces)

执行器内部（DO_TILING 宏）：
  ① 按 desc 构造 gert::Tensor，IrInstanceNum 声明输入输出实例数（默认全 1）
  ② 注入 attrs（按 AnyValue 类型分派）
  ③ 拼 compileInfo JSON（UB_SIZE/CORE_NUM/socVersion）→ 初始化 fe::PlatFormInfos 平台信息
  ④ 从 DefaultOpImplSpaceRegistryV2 注册表取出该算子的 tiling 函数指针
  ⑤ 真实调用 tilingFunc(tilingContext)
断言阶段：
  EXPECT_EQ(返回码) → 比对 workspace 列表 → 比对 tilingKey → 把 TilingData 转 int64 字符串比对
```

#### 4.4.3 源码精读

**① 用例本体**。[examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp:26-51](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp#L26-L51)（float 分支）：

```cpp
gert::TilingContextPara tilingContextPara(
    "AddExample",
    { /* 两个输入 {32,4,4,4} DT_FLOAT FORMAT_ND */ },
    { /* 一个输出 {32,4,4,4} DT_FLOAT FORMAT_ND */ },
    { /* attrs 为空 */ },
    &compileInfo,
    64,     // tiling 阶段可用的核数
    262144, // tiling 阶段可用的 UB 大小（实际到手比指定值少 256 字节）
    4096);  // TilingData 允许的最大字节数
uint64_t expectTilingKey = 0;                 // float → key 0（u4-l1 讲过的类型分派）
string expectTilingData = "2048 32 10912 ";   // totalNum blockFactor ubFactor
std::vector<size_t> expectWorkspaces = {0};
ExecuteTestCase(tilingContextPara, ge::GRAPH_SUCCESS, expectTilingKey, expectTilingData, expectWorkspaces);
```

**期望字符串是怎么算出来的**（衔接 u4-l1 的切分公式）：

- 输入总量 totalNum = 32×4×4×4 = **2048**；
- blockFactor = ⌈2048 / 64 核⌉ = **32**；
- ubFactor = FloorAlign(FloorDiv((262144 − 256) / (4 字节 × 6 块)), 32) = FloorAlign(⌊261888/24⌋, 32) = FloorAlign(10912, 32) = **10912**。

三个数恰好对应 [examples/add_example/op_kernel/add_example_tiling_data.h:21-25](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_data.h#L21-L25) 中结构体的声明顺序 `totalNum, blockFactor, ubFactor`——**改结构体字段顺序会悄无声息地打破所有 tiling UT**，这也是 u4-l2 强调「TilingData 字段只能尾部追加」的原因之一。第二个用例 [test_add_example_tiling.cpp:53-73](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp#L53-L73) 换成 DT_INT32、`expectTilingKey = 1`，其余参数走默认值（不传 coreNum/ubSize 即用默认 64/262144）。

**② 参数包定义**。[tests/ut/common/tiling_context_faker.h:55-67](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_context_faker.h#L55-L67) 是 `TilingContextPara` 的主构造函数：`opName + 输入/输出描述 + attrs + compileInfo + coreNum(默认64) + ubSize(默认262144) + tilingDataSize(默认4096)`；同文件还提供无 attrs 版本与带 `inputInstanceNum/outputInstanceNum` 版本（[tests/ut/common/tiling_context_faker.h:98-112](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_context_faker.h#L98-L112)），后者服务于 dynamic 输入（一个输入位多个实例，u3-l1 的 `ParamType=DYNAMIC`）。

**③ 执行器：伪造上下文并调真实函数**。[tests/ut/common/tiling_case_executor.cpp:19-59](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.cpp#L19-L59)（`DO_TILING` 宏前半段）：按描述逐个 `make_unique<gert::Tensor>` 并交给 `TilingContextFaker`，`IrInstanceNum` 默认每个输入输出算 1 个实例；[tests/ut/common/tiling_case_executor.cpp:60-104](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.cpp#L60-L104) 注入 attrs 时按 `AnyValue::ValueType`（BOOL/INT/FLOAT/STRING 及各种 LIST）分派转换。[tests/ut/common/tiling_case_executor.cpp:116-152](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.cpp#L116-L152) 是平台信息伪造的核心：把 `ubSize/coreNum` 拼成一段 compileInfo JSON，再由 `GetPlatFormInfos` 解析出 `SoCInfo`、`AICoreSpec` 等资源塞进 `TilingContext` 的 `PlatformInfo`——所以 tiling 函数里 `GetPlatformInfo()` 拿到的「芯片规格」其实是用例参数拼出来的，这正是「UT 无需 NPU」的实现机制。socVersion 取自编译期宏 `BUILD_SOC_VERSION`（[tests/ut/common/tiling_case_executor.cpp:126](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.cpp#L126)），这也解释了 `build.sh -u` 为什么支持 `--soc=`：**换 soc 重编，UT 里的平台判断分支才会走到对应路径**。

**④ 取函数并调用**。[tests/ut/common/tiling_case_executor.cpp:153-164](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.cpp#L153-L164)：从 `DefaultOpImplSpaceRegistryV2` 取算子实现结构，拿出 `functionStruct->tiling` 函数指针直接调用。找不到算子名会 `throw std::invalid_argument`——**算子名写错（与 OP_ADD 注册名不一致）时用例会异常终止而非断言失败**，排查时注意这个特征。

**⑤ 断言顺序**。[tests/ut/common/tiling_case_executor.cpp:243-276](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.cpp#L243-L276)：先 `EXPECT_EQ(tilingRet, expectResult)`；期望失败（GRAPH_FAILED）即提前返回——**只验证报错路径时不必提供其余期望**；然后依次 `ASSERT_EQ` workspace 个数与每个大小、tilingKey；最后 `to_string<int64_t>` 把 TilingData 原始字节转成空格分隔字符串与期望比对。头文件 [tests/ut/common/tiling_case_executor.h:19](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.h#L19) 还定义了哨兵字符串 `EMPTY_EXPECT_TILING_DATA`：期望传它即跳过 TilingData 比对（不想为字段顺序买单时的逃生门）。另有 `ExecuteTiling`（[tests/ut/common/tiling_case_executor.cpp:284-307](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.cpp#L284-L307)）只执行不比对，把 TilingData 字节、blockNum 原样返回给调用者复用——kernel UT 会拿它的产物去驱动 kernel 仿真（u7-l2 详讲）。

#### 4.4.4 代码实践

1. **实践目标**：跑通 add_example tiling UT，并人为制造一次断言失败，学会读 gtest 失败报告。
2. **操作步骤**（待本地验证）：

   ```bash
   # ① 正常跑
   bash build.sh -u --ophost --ops=add_example

   # ② 人为制造失败：把 test_add_example_tiling.cpp 第 48 行的
   #    string expectTilingData = "2048 32 10912 ";
   #    改成 string expectTilingData = "2048 32 10913 ";   // 只改末位
   #    再跑一次，观察报告；看完改回原值恢复现场
   bash build.sh -u --ophost --ops=add_example
   ```

3. **需要观察的现象**：第二次运行时 `AddExampleTiling.add_example_0` 变为 `[  FAILED  ]`，报告会打印期望串与实际串的差分（gtest 对 string 的 EXPECT_EQ 会输出两边的完整值），例如实际 `"2048 32 10912 "` 与期望 `"2048 32 10913 "` 在第三个数上不一致；汇总会出现 `1 FAILED`，且 build.sh 以非零码退出（[build.sh:1496-1499](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1496-L1499) 会打印 `UT build/test failed`）。
4. **预期结果**：失败定位精确到用例名与哪个字段错——这就是字符串化比对的可读性收益：三个 int64 直接对应 `totalNum/blockFactor/ubFactor`，一眼看出是 ubFactor 算错还是核切分算错。

#### 4.4.5 小练习与答案

**练习 1**：把用例的 `coreNum` 从 64 改成 32，期望 TilingData 字符串应变成什么？
**答案**：`"2048 64 10912 "`。totalNum 不变（2048）；blockFactor = ⌈2048/32⌉ = 64；ubFactor 只由 UB 容量决定（262144 未变），仍是 10912。

**练习 2**：某算子 tiling 有 6 个 int64 字段但用例只关心 tilingKey，不关心字段值，怎么写最省事？
**答案**：调用三参数重载 `ExecuteTestCase(para, expectResult, expectTilingKey, expectWorkspaces)`——它内部转调 `EMPTY_EXPECT_TILING_DATA` 跳过 TilingData 比对（见 [tests/ut/common/tiling_case_executor.cpp:278-282](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.cpp#L278-L282)）。

**练习 3**：为什么 UT 里 `GetPlatformInfo()` 拿到的核数正好是用例传入的 64？
**答案**：因为执行器在 [tests/ut/common/tiling_case_executor.cpp:133-150](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/tests/ut/common/tiling_case_executor.cpp#L133-L150) 把 `coreNum/ubSize` 参数拼进 compileInfo JSON，再解析成 `SoCInfo/AICoreSpec` 资源注入伪造的 `TilingContext`。平台信息在 UT 里是「参数拼出来的」，不是真实硬件查询。

## 5. 综合实践

**任务：为 add_example 补一个「双核小规模」tiling UT 并完成验证闭环。**

1. 在 `examples/add_example/tests/ut/op_host/test_add_example_tiling.cpp` 中仿照现有用例新增 `TEST_F(AddExampleTiling, add_example_2)`：输入输出 shape 用 `{8, 8}`（totalNum = 64）、dtype 任选 float、`coreNum` 显式传 `2`。
2. 动笔前先**手算**期望值：blockFactor = ⌈64/2⌉ = 32；ubFactor 与规模无关，仍为 10912；期望字符串 `"64 32 10912 "`；expectTilingKey 按所选 dtype 定 0 或 1；workspace 仍为 `{0}`。
3. 执行 `bash build.sh -u --ophost --ops=add_example` 验证新用例 PASSED。
4. 若手算与实际不一致，对照 [examples/add_example/op_host/add_example_tiling.cpp:214-222](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L214-L222) 的切分公式找原因（提示：注意 usedCoreNum 的反推逻辑会写进 SetBlockDim，而 blockFactor 字段本身就是 ⌈total/coreNum⌉）。
5. 完成后删除实验用例恢复源码现场（本讲不改交付件代码），把手算过程记录为笔记——这套「手算切分 → UT 对账」流程就是贡献新算子时（u9-l3）证明 tiling 正确性的标准动作。

## 6. 本讲小结

- UT 分五类（op_host / op_graph / op_api / op_kernel / aicpu op_kernel），`-u` 全跑、`--ophost` 等单选、`--noexec` 只编不跑、`--ops=` 圈定算子、`--soc=` 指定芯片；`--pkg` 与 `-u` 互斥。
- 用例收集是「目录 + 文件名通配」双约定：`test_*_infershape.cpp` / `test_*_tiling.cpp` 等由 `cmake/ut.cmake` 的 `add_modules_ut_sources` 自动 GLOB，新用例零登记。
- Infershape UT 三步成用例：构造 `InfershapeContextPara`、写期望 shape、`ExecuteTestCase`；默认期望是 `GRAPH_FAILED`，倒逼显式声明预期。
- Tiling UT 的平台信息（核数/UB/socVersion）由执行器用参数拼 JSON 伪造注入，因此无需 NPU；TilingData 按 int64 字符串逐字段比对，字段顺序即结构体声明顺序。
- 执行器从 `DefaultOpImplSpaceRegistryV2` 注册表取真实 tiling 函数调用——UT 测的是真实交付件代码；算子名写错会以异常而非断言失败的形式暴露。

## 7. 下一步学习建议

下一讲 **u7-l2「Kernel UT 与 ST 验证」**将深入 `tests/ut/op_kernel`：`AddOpTestCase` 的注册参数、`kernel_run_context_facker.h` / `kernel_ut_data_helper` 如何构造 kernel 输入数据、CPU 仿真执行与精度对账方法。建议先通读 [examples/add_example/tests/ut/op_kernel/test_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/tests/ut/op_kernel/test_add_example.cpp)（仅 70 行）预习 kernel 用例的样子，再对照本讲的 `ExecuteTiling` 思考「tiling 产物如何喂给 kernel 仿真」。
