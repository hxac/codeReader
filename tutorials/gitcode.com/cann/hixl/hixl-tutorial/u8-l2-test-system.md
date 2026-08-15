# u8-l2 测试体系与单测编写

## 1. 本讲目标

HIXL 是一个深度绑定昇腾硬件的通信库：真实的建链要靠 HCCS/RDMA，真实的传输要靠 AICPU 内核。那么在没有 NPU 的开发机上，这个仓库的测试是怎么跑起来的？本讲回答这个问题。学完后你应该能够：

1. 说出 `tests/` 目录下五个 C++ 测试套件（llm_datadist、adxl、channel_pool、hixl、fabric_mem）的划分依据与各自的二进制产物。
2. 读懂 `tests/run_test.sh` 的完整流程：参数解析 → 桩环境构建 → 并行执行 → 超时看门狗 → Python 测试 → 覆盖率统计。
3. 理解「stub 桩 + `--wrap` 链接器插桩」这套让真实引擎代码脱离硬件运行的技术。
4. 掌握 gtest + gmock 单测的 Arrange-Act-Assert 结构，能模仿现有用例为新功能写出测试骨架。

## 2. 前置知识

- **gtest / gmock**：Google 的 C++ 测试框架与打桩框架。`TEST_F(套件名, 用例名)` 定义一个用例，`EXPECT_EQ` 断言相等（失败继续）、`ASSERT_EQ` 断言相等（失败立即中止本用例）；gmock 用 `MOCK_METHOD` 生成可编程的假实现。
- **Arrange-Act-Assert（AAA）**：单测的三段式写法——准备（构造对象、装配桩、注册内存）、执行（调用被测接口）、断言（验证返回值与副作用）。
- **桩（stub）**：用一个「长得像真库、但不碰硬件」的假实现替换真实的 `libascendcl`、`libhccl`、`libdcmi` 等昇腾系统库，使被测代码以为自己在真机上运行。u1-l2 已提过 `tests/run_test.sh` 用桩环境构建，本讲深入其内部机制。
- **`--wrap` 链接器选项**：GNU ld 的一个插桩手段，`-Wl,--wrap=dlopen` 会把对 `dlopen` 的调用改写到 `__wrap_dlopen`，测试代码可以在包裹函数里决定转发给真函数还是返回桩路径——这是本仓库桩体系的核心机关。
- **fixture（测试夹具）**：gtest 的 `::testing::Test` 派生类，`SetUp()` 在每个用例前执行、`TearDown()` 在每个用例后执行，用于统一安装/卸载桩与清理全局状态。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tests/run_test.sh](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh) | 测试总入口：构建、并行执行、超时看门狗、Python 测试、覆盖率 |
| [tests/cpp/CMakeLists.txt](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/CMakeLists.txt) | C++ 测试工程挂载点，只做 `add_subdirectory` |
| [tests/cpp/llm_datadist/CMakeLists.txt](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/CMakeLists.txt) | llm_datadist 套件的源文件、桩、`--wrap` 配置 |
| [tests/cpp/hixl/CMakeLists.txt](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/CMakeLists.txt) | hixl 套件（含 fabric_mem 子套件）的构建配置，桩链接与 RPATH |
| [tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc) | LLM-DataDist 公开 API 级单测（本讲精读样本） |
| [tests/cpp/adxl/adxl_engine_unittest.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/adxl/adxl_engine_unittest.cc) | ADXL 引擎单测，展示 fixture 辅助方法封装风格 |
| [tests/depends/llm_datadist/src/llm_datadist_test_helper.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/depends/llm_datadist/src/llm_datadist_test_helper.h) | 公共测试工具：初始化选项拼装、KV Cache 注册 |
| [tests/python/test_utils.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/python/test_utils.py) | Python 单测样例（纯逻辑，无需硬件） |
| [tests/python/test_hixl_engine_api.py](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/python/test_hixl_engine_api.py) | hixl Python 绑定的 API 级测试（依赖桩 .so） |

另有 `tests/depends/` 目录（aicpu、ascendcl、dcmi、dsmi、hccl、runtime、slog 等子目录）存放全部桩实现，是理解「无硬件跑测试」的钥匙。

## 4. 核心概念与源码讲解

### 4.1 测试工程总览：ENABLE_TEST 与五个套件

#### 4.1.1 概念说明

HIXL 的测试不走产品构建的旁路，而是顶层 CMake 的一个独立分支：`ENABLE_TEST=ON` 时（见 [CMakeLists.txt:17](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/CMakeLists.txt#L17) 的 `option(ENABLE_TEST "Enable test" OFF)`），顶层只挂载 `tests/` 目录而跳过 `src/` 的设备子工程——测试二进制直接把**产品源码 .cc 文件编进自己**，而不是链接产品库。这带来一个重要性质：测试与实现同源编译，不存在「库版本与测试版本不一致」的问题。

C++ 测试按被测组件划分为五个套件，每个套件产出一个独立的可执行文件。

#### 4.1.2 核心流程

```text
顶层 CMakeLists.txt
  └─ ENABLE_TEST=ON → 只 add_subdirectory(tests)
       └─ tests/cpp/CMakeLists.txt
            ├─ add_subdirectory(llm_datadist)  → llm_datadist_test
            ├─ add_subdirectory(adxl)          → adxl_test + channel_pool_test
            └─ add_subdirectory(hixl)          → hixl_test
                 └─ add_subdirectory(fabric_mem) → fabric_mem_test
```

套件与二进制的对应关系（由 run_test.sh 的 `get_cpp_test_bin` 决定）：

| 套件名 | 二进制路径 | 被测对象 |
| --- | --- | --- |
| llm_datadist | `build_test/tests/cpp/llm_datadist/llm_datadist_test` | LLM-DataDist 公开 API、链路管理、Cache、rank table |
| adxl | `build_test/tests/cpp/adxl/adxl_test` | ADXL 引擎、channel manager、slot pool、统计 |
| channel_pool | `build_test/tests/cpp/adxl/channel_pool_test` | 通道池的系统/单元测试 |
| hixl | `build_test/tests/cpp/hixl/hixl_test` | HIXL Engine、CS、proxy、endpoint 生成 |
| fabric_mem | `build_test/tests/cpp/hixl/fabric_mem/fabric_mem_test` | FabricMem 内存体系与传输服务 |

#### 4.1.3 源码精读

挂载点只有三行，是套件划分的最直接证据：[tests/cpp/CMakeLists.txt:11-13](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/CMakeLists.txt#L11-L13) 依次 `add_subdirectory(llm_datadist)`、`add_subdirectory(adxl)`、`add_subdirectory(hixl)`。

以 llm_datadist 套件为例，[tests/cpp/llm_datadist/CMakeLists.txt:11-21](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/CMakeLists.txt#L11-L21) 列出 9 个测试源文件（含 API 级单测、链路管理器单测、rank table 生成器单测等），随后 [第 28-52 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/CMakeLists.txt#L28-L52) 用 `file(GLOB ...)` 把 `src/hixl/**` 与 `src/llm_datadist/**` 的产品源码整体编进 `llm_datadist_test`（[第 54-58 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/CMakeLists.txt#L54-L58)）——注意测试文件、桩文件、产品源码三者拼成同一个 `add_executable`。

hixl 套件同理，[tests/cpp/hixl/CMakeLists.txt:11-42](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/CMakeLists.txt#L11-L42) 列出约 28 个测试文件，覆盖 proxy（`dcmi_proxy_ut.cc` 等）、cs（`transfer_pool_ut.cc` 等）、engine（`hixl_engine_unittest.cc` 等）三个层次，与单元三、单元四讲过的源码目录一一对应；fabric_mem 作为其子目录追加第五个套件（[第 155 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/CMakeLists.txt#L155)）。

#### 4.1.4 代码实践

1. **实践目标**：建立「套件名 → 测试文件 → 被测源码」的映射直觉。
2. **操作步骤**：打开 `tests/cpp/hixl/CMakeLists.txt` 的 `HIXL_TEST_FILES` 列表，挑 `cs/msg_receiver_ut.cc` 与 `engine/hixl_options_unittest.cc` 两个文件，分别回答：它们测的是 `src/hixl/` 下哪个目录的代码？再对照单元三/单元四的讲义目录验证。
3. **需要观察的现象**：测试文件的相对路径（`cs/`、`engine/`、`proxy/`）与 `src/hixl/` 的子目录名几乎完全同构——这是本仓库「测试镜像源码结构」的组织约定。
4. **预期结果**：`msg_receiver_ut.cc` 对应 `src/hixl/cs/msg_receiver.cc`（u4-l2 讲过的拆包器），`hixl_options_unittest.cc` 对应 `src/hixl/engine/hixl_options.cc`（u2-l1 讲过的选项解析）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 run_test.sh 里指定 `-s adxl` 会同时跑 `adxl` 和 `channel_pool` 两个套件？

**答案**：因为 channel_pool 的两个测试文件（`channel_pool_system_test.cc`、`channel_pool_unit_test.cc`）物理上位于 `tests/cpp/adxl/` 目录下（见 [tests/cpp/adxl/CMakeLists.txt:22-25](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/adxl/CMakeLists.txt#L22-L25)），是 adxl 目录里的第二个二进制；run_test.sh 的 `select_cpp_suites`（[tests/run_test.sh:85-95](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L85-L95)）把 adxl 展开为 `(adxl channel_pool)`，保证选 adxl 时其目录下的全部套件都被执行。

**练习 2**：新增一个 HIXL 引擎特性时，测试文件应放进哪个套件？需要改哪几个地方？

**答案**：放进 `tests/cpp/hixl/engine/`（引擎层），需要：① 新建 `*_unittest.cc`；② 在 `tests/cpp/hixl/CMakeLists.txt` 的 `HIXL_TEST_FILES` 追加该文件；③ 无需改 run_test.sh（hixl 套件已注册）。若被测代码在 `src/llm_datadist/` 下则同理改 `tests/cpp/llm_datadist/CMakeLists.txt`。

### 4.2 run_test.sh：构建、并行执行与超时看门狗

#### 4.2.1 概念说明

`tests/run_test.sh` 是所有测试的唯一入口（u1-l2 已介绍其用法），本讲深入它的执行模型。它解决三个问题：

1. **选择性执行**：`-t cpp|py` 选语言，`-s 套件名` 选套件，`-f changed-files-file` 在 CI 中跳过纯文档改动。
2. **隔离与并行**：五个 C++ 套件后台并行执行、互不阻塞，每个套件有独立的日志文件与 600 秒超时看门狗。
3. **可观测性**：gtest 输出 XML 报告到 `build_out/report/`，日志按套件落盘，失败时红色高亮提示。

#### 4.2.2 核心流程

```text
main
 ├─ checkopts            # 解析 -t/-s/-c/--asan/-j/-f 等参数
 ├─ check_changed_files  # 纯 docs/examples/README 改动 → exit 200 跳过测试
 └─ run
     ├─ 在 build_test/ 目录 cmake -D ENABLE_TEST=ON ... && make
     ├─ (C++) 对每个套件：
     │     后台启动 "<二进制> --gtest_output=xml:report/<suite>_test.xml" > <suite>.log &
     │     同时启动一个 monitor 子 shell：sleep 600 后若进程仍存活 → kill 并写 .timeout 文件
     ├─ wait 所有测试进程；逐个回收 monitor、打印日志、聚合失败标志
     ├─ (Py) 拷贝桩 .so → 设 PYTHONPATH/LD_LIBRARY_PATH → coverage run -m unittest discover python
     └─ (可选, -c) lcov + genhtml 生成覆盖率报告
```

#### 4.2.3 源码精读

- **套件选择**：[tests/run_test.sh:72-83](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L72-L83) 的 `set_test_suite` 白名单校验五个合法套件名，非法值直接打印 usage 退出。
- **CI 跳过逻辑**：[tests/run_test.sh:235-304](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L235-L304) 的 `check_changed_files` 逐行检查变更文件，若全部属于 `docs/`、`examples/`、`.claude/` 等非代码路径则返回跳过（main 中 `exit 200`）——这是给 CI 用的省时机关。
- **二进制定位**：[tests/run_test.sh:363-381](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L363-L381) 的 `get_cpp_test_bin` 是「套件名 → 二进制路径」的映射表，与 4.1 节的表格一致。
- **并行执行与看门狗**：[tests/run_test.sh:390-425](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L390-L425) 的 `run_cpp_test_parallel` 把测试进程与一个「sleep 600 → 检查存活 → kill」的 monitor 子 shell 同时挂后台（`CPP_TEST_TIMEOUT_SECONDS=600` 定义于 [第 388 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L388)）。monitor 内的 trap 保证 monitor 被杀时也能回收 sleep，不留孤儿进程（注释见 [第 402-405 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L402-L405)）。
- **结果聚合**：[tests/run_test.sh:431-454](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L431-L454) 逐个 `wait` 测试进程，任何非零退出置 `HIXL_PARALLEL_FAILED=1`，最终整体 `exit 1`——一次失败即全量失败，不静默吞掉。
- **gtest XML 报告**：运行命令固定携带 `--gtest_output=xml:${report_dir}/${suite}_test.xml`（[第 395 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L395)），供 CI 解析逐用例结果。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证套件选择与报告落盘。
2. **操作步骤**：在有 CANN 三方依赖的环境执行 `bash tests/run_test.sh -t cpp -s llm_datadist -v`；结束后查看 `build_out/report/` 目录。
3. **需要观察的现象**：`report/` 下出现 `llm_datadist_test.xml`（gtest 报告）与 `llm_datadist.log`（运行日志）；若超时还会出现 `llm_datadist.timeout`。也可以直接 `bash tests/run_test.sh -h` 查看完整参数表（无需任何硬件）。
4. **预期结果**：跳过构建的话 `-h` 立即打印 usage；完整运行结果**待本地验证**（本讲义编写环境未构建）。

#### 4.2.5 小练习与答案

**练习 1**：为什么每个套件要配一个 600 秒的看门狗，而不是依赖 CI 自身的任务超时？

**答案**：通信库的测试常出现「死等对端」类挂死（如控制面 socket 无响应、channel 状态轮询不返回）。CI 级超时会杀掉整个测试任务，丢失其余套件的结果与现场；per-suite 看门狗只杀挂死的那个二进制、写入 `.timeout` 文件并打印其日志（[tests/run_test.sh:409-417](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L409-L417)），其余套件继续执行完毕，定位信息更完整。

**练习 2**：`-f` 参数传入的变更文件列表满足什么条件时脚本会跳过测试？退出码是多少？

**答案**：所有变更文件都属于 `docs/`、`examples/`、`.claude/`、`.opencode/`、`.agents/` 目录，或文件名恰为 `README.md`/`CONTRIBUTING.md`/`AGENTS.md` 时跳过（[tests/run_test.sh:297-303](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L297-L303)），main 中以 `exit 200` 退出（[第 556-560 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L556-L560)）——用非 0/非 1 的特殊码区分「主动跳过」与「失败」。

### 4.3 桩体系：让引擎代码在无 NPU 环境运行

#### 4.3.1 概念说明

测试要把 `src/hixl/`、`src/llm_datadist/` 的**真实产品代码**编进来，而这些代码会调用 `aclrtMalloc`、`HcommChannelCreate`、`dlopen("libdcmi.so")` 等昇腾系统接口。桩体系分三层解决：

1. **链接期桩（stub 库）**：`tests/depends/` 下每个系统库目录（ascendcl、hccl、runtime、dcmi、dsmi、slog、msprof、aicpu、error_manager）都提供 `*_stub` 库，直接替代真实 .so 参与链接（如 [tests/cpp/llm_datadist/CMakeLists.txt:95-107](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/CMakeLists.txt#L95-L107) 链接的 `ascendcl_stub`、`hccl_stub` 等）。
2. **运行期 dlopen 劫持（`--wrap`）**：产品代码里 proxy 层用 `dlopen` 动态加载 libdcmi 等（u3-l5）；测试用 `-Wl,--wrap=dlopen -Wl,--wrap=dlsym ...` 把这些调用改道到桩路径，并配 `BUILD_RPATH` 让桩目录优先于系统 CANN 安装目录（[tests/cpp/hixl/CMakeLists.txt:147-153](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/CMakeLists.txt#L147-L153)，llm_datadist 套件同样配置于 [tests/cpp/llm_datadist/CMakeLists.txt:110-114](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/CMakeLists.txt#L110-L114)）。
3. **可编程 mock（gmock）**：对需要在用例间改变行为的接口（如返回不同 SoC 名称），用 gmock 类替换，例如 `llm::AutoCommResRuntimeMock` 可注入自定义 `aclrtGetSocName` 返回值。

#### 4.3.2 核心流程

```text
被测产品代码调用 aclrtMalloc(...)
  → 链接到 ascendcl_stub（桩实现，返回伪造的成功/地址）
被测产品代码调用 dlopen("libdcmi.so")
  → __wrap_dlopen 改写路径 → BUILD_RPATH 指向 tests/depends/dcmi → 加载桩 libdcmi.so
测试用例需要特定行为（如 SoC = Ascend910B1）
  → 用例内派生 AutoCommResRuntimeMock 覆写方法 → SetInstance 注入
```

Python 测试同理：run_test.sh 把构建出的桩 `.so`（`llm_datadist_wrapper.so`、`hixl.so` 等）拷进 `src/python/` 包目录，再用 `LD_LIBRARY_PATH` 指向 `build_test/tests/depends/` 下各桩目录（[tests/run_test.sh:457-466](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L457-L466)），随后 `coverage run -m unittest discover python`（[第 471-473 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L471-L473)）。

#### 4.3.3 源码精读

- hixl 套件的桩链接清单：[tests/cpp/hixl/CMakeLists.txt:117-145](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/CMakeLists.txt#L117-L145) 链接 `slog_stub`、`mmpa_stub`、`ascendcl_stub`、`runtime_stub`、`dcmi_stub`、`drvdsmi_host_stub`、`hixl_ascend_hal_stub`、`hccl_stub`、`aicpu_stub`、`error_manager_stub` 等十余个桩库，同时链接真实的 `GTest::gtest GTest::gtest_main` 与 `GTest::gmock`。
- `--wrap` 与 RPATH 的注释直接说明了意图：[tests/cpp/hixl/CMakeLists.txt:147-153](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/CMakeLists.txt#L147-L153) —— `--disable-new-dtags` 强制使用 DT_RPATH 优先于 `LD_LIBRARY_PATH`，「确保测试桩优先于系统 CANN 安装的同名库被加载」。
- 公共测试工具头文件：[tests/depends/llm_datadist/src/llm_datadist_test_helper.h:46-62](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/depends/llm_datadist/src/llm_datadist_test_helper.h#L46-L62) 的 `InitTestLlmDataDist` 拼装带 `llm.LocalCommRes` JSON 的初始化选项并以 `EXPECT_EQ(dist.Initialize(options), ge::SUCCESS)` 断言成功——把「初始化一个测试实例」这件在每个用例里都要做的事收拢成一行。
- [tests/depends/llm_datadist/src/llm_datadist_test_helper.h:65-78](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/depends/llm_datadist/src/llm_datadist_test_helper.h#L65-L78) 的 `SetupKvCache` 用**主机内存的 `std::vector` 地址**充当 device tensor 地址注册 KV Cache——在桩环境里 `RegisterKvCache` 不校验地址是否真在 device 上，这正是无硬件测试的可行前提。

#### 4.3.4 代码实践

1. **实践目标**：数清一个套件依赖多少个桩，理解「测试自包含」的含义。
2. **操作步骤**：阅读 [tests/cpp/hixl/CMakeLists.txt:117-145](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/CMakeLists.txt#L117-L145)，数出 `_stub` 后缀的库名；再 `ls tests/depends/` 对照每个桩的实现目录。
3. **需要观察的现象**：桩目录名（dcmi、dsmi、ascend_hal、hccl、aicpu、msprof…）与单元三 u3-l5 讲过的 proxy 层封装的系统库一一对应——proxy 层封装了哪些库，测试就要桩掉哪些库。
4. **预期结果**：约 10 个桩库；`tests/depends/` 下有 15 个左右子目录，多出的（`sys_api`、`python` 等）服务于其他套件或 Python 测试。

#### 4.3.5 小练习与答案

**练习 1**：为什么 hixl_test 要同时用「链接期 stub」和「`--wrap=dlopen`」两套手段，只用一套行不行？

**答案**：不行，两者覆盖不同的调用形态。编译期就可见符号的调用（如直接 `#include` 头文件后调 `aclrtMalloc`）由链接期 stub 库满足；而 proxy 层通过 `dlopen("libdcmi.so")` + `dlsym` 动态解析的符号（u3-l5 讲过的 DCMI/HAL 加载方式）链接期不可见，必须在运行期劫持 dlopen/dlsym 并用 RPATH 把桩目录排到搜索最前，两套手段互补。

**练习 2**：`SetupKvCache` 直接拿 `vector::data()` 当 device 地址注册，产品代码为何不报错？

**答案**：注册路径上的 `aclrtMemcpy` 等真正触碰内存的调用全部落在桩实现里（桩不搬数据、直接返回成功），`RegisterKvCache` 自身只做参数解析与登记（u6-l3 讲过的账本逻辑），不校验地址的物理归属；因此用例里对「传输后数据」的断言验证的是**控制流与返回值**，而非真实数据搬运。

### 4.4 gtest 单测解剖：llm_datadist_v2_api_unittest 的 AAA 结构

#### 4.4.1 概念说明

[tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc) 是 LLM-DataDist 的 API 级测试：站在公开类 `LlmDataDist` 的用户视角，把「初始化 → 注册 → 建链 → Push/Pull → 解链 → 注销 → Finalize」的完整合同（u6-l4 总结过的顺序合同）逐用例验证。它展示了三个值得模仿的工程习惯：

1. **fixture 收拢环境装配**：`SetUp`/`TearDown` 统一安装与卸载桩。
2. **辅助函数消重复**：双端初始化、KV Cache 注册、建链抽成 `SetupKvCachesAndLink` 等复用单元。
3. **一个用例一条业务路径**：`TestLocalCommResA2` 走 Cache 粒度传输，`TestLocalCommResA3` 走 Blocks 粒度，`TestLocalCommResA3LinkFailed` 专测失败路径。

#### 4.4.2 核心流程

以 `TestLocalCommResA2` 为例的 AAA 拆解：

```text
[Arrange]
  fixture SetUp：安装 mmpa/runtime 桩、初始化 CommAdapter
  构造两个 LlmDataDist（cluster 1 = kPrompt、cluster 2 = kDecoder）
  InitTestLlmDataDist(...)：拼 LocalCommRes JSON、断言 Initialize 成功
  SetupKvCachesAndLink(...)：双端注册 KV Cache、LinkLlmClusters 建链
[Act]
  llm_datadist_d.PullKvCache(cache_index, dst_cache)        # decoder 拉
  llm_datadist_p.PushKvCache(src_cache, dst_index, 0, -1, ext)  # prompt 推
[Assert]
  每一步返回值都用 EXPECT_EQ(..., ge::SUCCESS) 当场断言
[Cleanup]
  Unlink → UnregisterKvCache ×2 → Finalize ×2
```

注意一个风格事实：本用例把断言**穿插**在 Arrange/Act 中（每步操作立即断言返回值），而不是堆积在结尾——对顺序敏感的 API 合同测试这样写，失败时能立刻定位到第一个违约步骤。

#### 4.4.3 源码精读

- **fixture**：[llm_datadist_v2_api_unittest.cc:30-44](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc#L30-L44) `LlmDataDistUTest` 的 `SetUp` 依次 `MockMmpaForHcclApi::Install()`、`AutoCommResRuntimeMock::Install()`、`CommAdapter::GetInstance().Initialize()`；`TearDown` 严格逆序拆解。桩的安装/卸载必须在 fixture 层做，保证单个用例崩溃也不污染下一个用例。
- **复用单元**：[第 46-78 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc#L46-L78) 定义 `KvCacheTestContext` 结构体与 `SetupKvCachesAndLink` 内联函数——注释明确说明这是从 A2/A3 两个用例中提取的公共逻辑（「This extracts the common logic shared by TestLocalCommResA2 and TestLocalCommResA3」）。
- **Cache 粒度用例**：[第 80-110 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc#L80-L110) 完整覆盖 Pull（第 94 行）与 Push（第 101 行，`KvCacheExtParam{{0,0},{0,0},2,{}}` 即 u6-l4 讲过的层区间参数）。
- **Blocks 粒度用例**：[第 112-142 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc#L112-L142) 把 `PullKvCache/PushKvCache` 换成 `PullKvBlocks/PushKvBlocks`（`{0}` 单块配对），其余结构完全同构——两个用例并排读，正好对照出 Cache 与 Blocks 两套接口的差异。
- **行为注入示例**：[第 144-151 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc#L144-L151) 在用例**内部**派生 `AutoCommResV1RuntimeMock` 并覆写 `aclrtGetSocName` 返回 `"Ascend910B1"`，再 `SetInstance` 注入——这就是 gmock 的按用例编程能力，用来驱动产品代码走不同的 SoC 分支。
- **失败路径用例**：[第 215 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc#L215) 起的 `TestLocalCommResA3LinkFailed` 传入非法 `llm.LocalCommRes` JSON，断言建链返回错误——测试不止测幸福路径。

作为对照，adxl 套件的 fixture 展示了另一种组织：[adxl_engine_unittest.cc:71-134](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/adxl/adxl_engine_unittest.cc#L71-L134) 把「初始化两个引擎」「注册 int32 内存」封装为 fixture 的成员函数（`SetupEngines`、`RegisterInt32Mem`、`SetupInt32ConnectedEngines`），并在 `TearDown` 中特意恢复被心跳用例改小的全局配置（[第 90-94 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/adxl/adxl_engine_unittest.cc#L90-L94) 注释解释了不恢复会引发的连锁故障）——全局状态的「用后即还原」是多用例共存纪律的范例。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：吃透一个用例的 AAA 结构，并模仿写出新场景的用例骨架。
2. **操作步骤**：
   - 精读 `TestLocalCommResA3`（[llm_datadist_v2_api_unittest.cc:112-142](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/llm_datadist/llm_datadist_v2_api_unittest.cc#L112-L142)），在纸上标出 Arrange / Act / Assert / Cleanup 四段的起止行。
   - 模仿写一个「Push 层区间部分传输」用例骨架（示例代码，非仓库原有，建议放在同文件末尾练习，不提交）：

   ```cpp
   // 示例代码：练习骨架 —— 验证 PushKvCache 层区间参数（u6-l4：src/dst 层区间须等宽）
   TEST_F(LlmDataDistUTest, TestPushKvCacheLayerRange) {
     // Arrange：双实例 + 初始化 + 注册 + 建链（全部复用现有 helper）
     LlmDataDist llm_datadist_p(1U, LlmRole::kPrompt);
     LlmDataDist llm_datadist_d(2U, LlmRole::kDecoder);
     InitTestLlmDataDist(llm_datadist_p, "0", "1.1.1.1", true);
     InitTestLlmDataDist(llm_datadist_d, "1", "1.1.1.2", false);
     CacheDesc kv_desc{};
     kv_desc.num_tensors = 10;          // tensor_num_per_layer 默认 2 → 最大层索引 4
     kv_desc.data_type = DT_INT32;
     kv_desc.shape = {4, 16};
     auto ctx = SetupKvCachesAndLink(llm_datadist_d, llm_datadist_p, kv_desc);

     // Act：只传第 1~2 层（src/dst 等宽的部分层区间）
     CacheIndex dst_index{2U, ctx.d_setup.cache_id, 0U, {}};
     Cache src_cache{};
     src_cache.cache_id = ctx.p_setup.cache_id;
     KvCacheExtParam ext_param{{1, 2}, {1, 2}, 2, {}};
     auto st = llm_datadist_p.PushKvCache(src_cache, dst_index, 0, -1, ext_param);

     // Assert：等宽区间应成功
     EXPECT_EQ(st, ge::SUCCESS);

     // Cleanup：按 u6-l4 顺序合同收尾
     std::vector<ge::Status> rets;
     EXPECT_EQ(llm_datadist_d.UnlinkLlmClusters({ctx.cluster_info}, rets), ge::SUCCESS);
     EXPECT_EQ(llm_datadist_p.UnregisterKvCache(ctx.p_setup.cache_id), ge::SUCCESS);
     EXPECT_EQ(llm_datadist_d.UnregisterKvCache(ctx.d_setup.cache_id), ge::SUCCESS);
     llm_datadist_p.Finalize();
     llm_datadist_d.Finalize();
   }
   ```

   - 进一步思考变体：把 dst 层区间改成 `{0, 3}`（与 src 不等宽），预期返回什么？（提示：回顾 u6-l4 的约束。）
3. **需要观察的现象**：骨架中 Arrange/Cleanup 两段几乎与现有用例逐行相同，真正属于「新场景」的只有 Act 与 ExtParam 构造——这正是 helper 抽象的价值。
4. **预期结果**：等宽区间用例预期 `ge::SUCCESS`；不等宽区间预期被参数校验拒绝。两者均**待本地验证**（需在测试环境编译运行 `bash tests/run_test.sh -t cpp -s llm_datadist` 后用 `--gtest_filter=LlmDataDistUTest.TestPushKvCacheLayerRange` 单跑确认）。

#### 4.4.5 小练习与答案

**练习 1**：`TestLocalCommResA2` 里两个 `LlmDataDist` 实例分别传 cluster id 1 和 2，`CacheIndex` 却写 `{1U, ...}` 与 `{2U, ...}`，这两个数字各指什么？

**答案**：`LlmDataDist` 构造参数是**本端** cluster id；`CacheIndex`（u6-l1/u6-l4 讲过的三级寻址）首字段是**对端** cluster id。Pull 时 decoder（cluster 2）用 `{1U, p_setup.cache_id, ...}` 指向 prompt 端（cluster 1）的 Cache，Push 时 prompt 用 `{2U, ...}` 指向 decoder 端——方向与 u6-l4「源在前、目的地在后」的参数规则一致。

**练习 2**：为什么 `SetupKvCachesAndLink` 里建链只对 `llm_datadist_d`（decoder）调用一次 `LinkLlmClusters`？

**答案**：`LinkLlmClusters` 是双向握手（u6-l2 讲过的 `ExchangeInfoProcess` 两端对称执行），client 端发起连接请求即可完成两端建链，对端无需（也不应）再发起；这也与 u6-l4 的样例合同一致——两个样例进程里只有一侧调用 Link。

**练习 3**：如果用例之间共享的 `StatisticManager` 全局统计不清理，会发生什么？参照 adxl 的做法说明。

**答案**：前一个用例注册的统计通道会残留到后一个用例，导致计数混入、断言污染。adxl 的 fixture 因此在 `SetUp` 与 `TearDown` 里都调用 `ClearStatisticChannels()`（[adxl_engine_unittest.cc:73-98](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/adxl/adxl_engine_unittest.cc#L73-L98)），对心跳等全局配置也在 TearDown 统一还原——原则是「全局状态用前清、用后还」。

### 4.5 Python 测试：unittest discover 与桩 .so

#### 4.5.1 概念说明

Python 侧测试位于 `tests/python/`，共 11 个文件，分两类：

1. **纯逻辑测试**：如 `test_utils.py` 只测参数校验函数的抛异常行为，不碰任何 .so，任何机器可跑。
2. **绑定 API 测试**：如 `test_hixl_engine_api.py`（u7-l1 讲过的 hixl 模块）与 `test_cache_manager.py`，需要先有编译好的桩 `hixl.so`/`llm_datadist_wrapper.so`——由 run_test.sh 在运行前拷入并通过 `PYTHONPATH`/`LD_LIBRARY_PATH` 装配（见 4.3 节引用的 [tests/run_test.sh:457-466](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L457-L466)）。

执行方式是 `coverage run -m unittest discover python`：unittest 递归发现 `tests/python/` 下所有 `test_*.py`，逐模块逐用例执行，coverage 顺带采集 `src/` 下的 Python 代码覆盖率。

#### 4.5.2 核心流程

```text
run_test.sh (ENABLE_PY_TEST=ON)
  ├─ 拷贝 build_test/tests/depends/python/{llm_datadist_wrapper.so, metadef_wrapper.so, hixl.so}
  │   → src/python/llm_datadist/llm_datadist/ 与 src/python/hixl_py/
  ├─ cp -r tests/python ./（复制到 build 目录，避免污染源码树）
  ├─ export PYTHONPATH=src/python/llm_datadist/:src/python/hixl_py/
  ├─ export LD_LIBRARY_PATH=build_test/tests/depends/{hixl,llm_datadist,slog,...}/
  ├─ coverage run -m unittest discover python   # ASan 开启时加 LD_PRELOAD + detect_leaks=0
  └─ 清理拷入的 .so，还原环境变量
```

#### 4.5.3 源码精读

- **纯逻辑测试样例**：[tests/python/test_utils.py:22-44](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/python/test_utils.py#L22-L44) 的 `TensorUt` 用 `self.assertRaises(ValueError)` 上下文管理器逐个验证 `check_uint64` 等校验函数对非法输入的拒绝——Python 版 AAA：Arrange（构造非法值）→ Act（调用校验）→ Assert（断言抛异常）。
- **绑定 API 测试组织**：[tests/python/test_hixl_engine_api.py:46-101](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/python/test_hixl_engine_api.py#L46-L101) 按主题分类测试类（常量、枚举、数据类），[第 152-197 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/python/test_hixl_engine_api.py#L152-L197) 的 `HixlInitializeFinalizeTest` 则逐方法验证 Initialize/Finalize 的 Python 侧行为（如 `test_repeated_initialize_returns_success` 对应 u2-l1 讲过的幂等语义）。
- **失败处理**：[tests/run_test.sh:475-480](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L475-L480) Python 测试非零退出同样红色告警并 `exit 1`，且无论成败都删除拷入的 `.so`（[第 481-482 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L481-L482)），不把临时产物留在源码树。

#### 4.5.4 代码实践

1. **实践目标**：验证纯逻辑 Python 测试无需任何硬件即可运行。
2. **操作步骤**：在仓库根目录执行 `python3 -m unittest tests.python.test_utils -v`（或在 `tests/` 目录下 `python3 -m unittest discover python -p test_utils.py -v`）。
3. **需要观察的现象**：`test_check_exception` 等用例逐条通过，输出 `OK`；全程无 NPU 依赖、无 .so 依赖。
4. **预期结果**：全部通过（该测试只 import `llm_datadist.utils.utils` 的纯 Python 校验函数）。**待本地验证**（需仓库内 Python 环境可用）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 run_test.sh 运行 Python 测试前要先 `cp` 桩 .so 进 `src/python/`，结束后又删除？

**答案**：Python 包通过 `import hixl` / `import llm_datadist` 加载同目录下的扩展模块；平时源码树里没有这些 .so（它们是测试构建产物），所以运行前必须就位。但源码树应保持干净（产物不入库），故结束（无论成败）后删除——临拷临删是「源码树零污染」的惯例。

**练习 2**：ASan 开启时 Python 测试为什么要设 `ASAN_OPTIONS=detect_leaks=0`（[tests/run_test.sh:469-471](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/run_test.sh#L469-L471)）？

**答案**：Python 解释器自身与第三方库常存在「进程退出时才结算」的泄漏告警，与被测代码无关；若开启泄漏检测，这些噪声会导致 ASan 把整个测试判失败。关闭 detect_leaks 让 ASan 只在真正的越界/悬垂访问上报警，聚焦被测的 C++ 扩展模块。

## 5. 综合实践

**任务：为「ConnectAsync 状态机」补一个单测骨架并走完添加流程。**

结合 u2-l4（异步建链七态状态机）与本讲知识，完成一次完整的「为新功能补测试」演练：

1. **选择落点**：被测对象是 `src/hixl/engine/connect_pool_executor.cc` 与 `hixl_client.cc` 的异步建链逻辑，套件归属 hixl（引擎层），测试文件应新建为 `tests/cpp/hixl/engine/connect_pool_async_unittest.cc`（示例文件名）。
2. **注册构建**：在 [tests/cpp/hixl/CMakeLists.txt:11-42](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/hixl/CMakeLists.txt#L11-L42) 的 `HIXL_TEST_FILES` 列表中追加该文件。
3. **编写骨架**：fixture 参照 [adxl_engine_unittest.cc:71-98](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/tests/cpp/adxl/adxl_engine_unittest.cc#L71-L98) 的 SetUp/TearDown 对称拆装；用例按 AAA 组织：
   - Arrange：初始化一个带端口（server 角色）与一个不带端口（client 角色）的引擎，注册双方内存（参照 u2-l3 的注册顺序合同）；
   - Act：client 调 `ConnectAsync` 后立即 `GetAsyncConnectStatus`，再轮询至终态；
   - Assert：首次查询应得到 `CONNECT_PENDING` 或 `CONNECTING`（非终态），最终应迁移到 `CONNECTED`；
   - Cleanup：`DisconnectAsync` + 轮询 + `Finalize`。
4. **运行验证**：`bash tests/run_test.sh -t cpp -s hixl`，或直接执行 `build_test/tests/cpp/hixl/hixl_test --gtest_filter=*ConnectAsync*` 观察状态迁移。
5. **记录**：把「文件落点 → CMake 注册 → 骨架 → 运行命令 → 结果」整理成一页 checklist，作为团队新增单测的 SOP。若本机无构建环境，前三步（落点、注册、骨架）仍可完成，运行结果标注「待本地验证」。

## 6. 本讲小结

- HIXL 的 C++ 测试分五个套件（llm_datadist / adxl / channel_pool / hixl / fabric_mem），每个套件一个独立 gtest 二进制，**产品源码直接编进测试可执行文件**，测试目录结构与 `src/` 子目录镜像同构。
- `tests/run_test.sh` 是唯一测试入口：支持 `-t/-s` 选择语言与套件、`-f` 让纯文档改动跳过测试（exit 200）；五个 C++ 套件并行执行，每个配 600 秒 per-suite 看门狗，gtest 结果落 XML 报告。
- 无 NPU 跑测试靠三层桩体系：链接期 `*_stub` 桩库替代昇腾系统库、`-Wl,--wrap=dlopen/dlsym` + DT_RPATH 劫持运行期动态加载、gmock（如 `AutoCommResRuntimeMock`）实现按用例编程行为注入。
- API 级单测的标准形态是「fixture 装环境 + helper 消重复 + 断言穿插每一步 + 失败路径单列用例」，`TestLocalCommResA2/A3` 并排对照即可看清 Cache 与 Blocks 两套接口的差异。
- Python 测试用 `unittest discover` 驱动，桩 .so 临拷临删；全局状态的纪律是「用前清、用后还」（adxl fixture 的统计清理与心跳还原是范例）。
- 为新功能补测试的固定动作：测试文件放进镜像目录 → 在对应套件 CMake 的 `*_TEST_FILES` 注册 → 按 AAA 写用例 → 套件级运行验证。

## 7. 下一步学习建议

- **u8-l3 Profiling 与统计**：本讲提到 adxl 用例会改写心跳周期并必须还原，下一讲顺藤摸瓜学习统计埋点与性能数据采集机制。
- **u8-l6 二次开发与贡献流程**：把本讲的「新增单测 SOP」并入完整贡献 checklist（pre-commit、编译、测试三道关）。
- **源码延伸阅读**：`tests/cpp/hixl/cs/transfer_pool_ut.cc` 与 `tests/cpp/adxl/channel_pool_system_test.cc` 展示了并发场景下更复杂的多线程断言写法；`tests/depends/` 下任一 stub 目录（如 `dcmi/`）配合 u3-l5 的 proxy 层讲义，可以看清「桩与真库的接口签名如何保持一致」。
