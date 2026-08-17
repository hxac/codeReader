# u8-l6 测试体系与单元测试编写

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 SHMEM 单元测试（UT）的编译开关（`USE_UNIT_TEST` / `build.sh -uttests`）与三层 CMake 的组织方式，能独立把 UT 编出来。
2. 讲清楚 `tests/unittest` 中 host / device 两棵子树与被测模块的对应关系，以及「一个用例 = host 测试体 + device kernel」的双侧结构。
3. 读懂 `main_test.cpp` 提供的公共骨架：命令行参数、`test_*_init` 初始化家族、`test_mutil_task` 的 fork 多进程编排。
4. 掌握两类「引擎相关接口」的门控测试写法：
   - 编译期门控：`ACLSHMEMI_RDMA_K_BACKEND_XSCALE` 宏 + `GTEST_SKIP`（以 `TestShmemRdmaQpSpecificApis` 为例）；
   - 运行时平台门控：`aclrtGetSocName()` 检测 Ascend950 + `GTEST_SKIP`，并在 kernel 内主动注入 UDMA topo 位来验证引擎选择守卫（本轮新增的 `TestShmemUDMAHighLevelLocalRma`）。
5. 参照现有套路，为一个尚未覆盖的 heap 或 sync 接口补一个最小 googletest 用例并编译通过。

本讲是「二次开发」单元的收口讲：前面 u5、u8 各讲从不同视角读过引擎与机制，本讲回答「改完之后怎么验证」。

## 2. 前置知识

- **googletest 基础**：SHMEM 的 UT 用 googletest 框架。`TEST(套件名, 用例名)` 注册一个用例；`EXPECT_*` 失败后继续执行，`ASSERT_*` 失败立即终止当前函数（在多进程用例里用于提前退出）；`GTEST_SKIP()` 让用例「跳过而非失败」，这是平台门控的关键武器。
- **为什么 UT 必须多进程**：SHMEM 是多 PE 集体通信库，`aclshmemx_init_attr` / `aclshmem_malloc` / `finalize` 都是集体操作（见 u1-l4、u2-l4）。单进程测不全，所以测试骨架用 `fork()` 拉起 N 个子进程模拟 N 个 PE。
- **host / device 双侧结构**：host 侧用例（`*_test.cpp`，gcc 编译）负责初始化、下发 kernel、校验数据；device 侧 kernel（`*_kernel.cpp`，bisheng/AscendC 编译）在 AICore 上执行真正的通信调用。两者通过 `tests/unittest/include/unittest/` 下的头文件声明桥接。
- **平台门控的两种手段**：
  - 编译期：CMake 按芯片/后端定义宏（如 `ACLSHMEMI_RDMA_K_BACKEND_XSCALE`），代码用 `#if` 或 `constexpr bool` 分支；
  - 运行期：用 ACL 运行时 API `aclrtGetSocName()` 查询当前芯片型号字符串，不匹配就 `GTEST_SKIP()`。
- **topo_list 与引擎分派**（承接 u4-l2、u5-l1）：高阶 RMA 接口按 `topo_list[pe]` 的引擎位图选引擎，优先级 SDMA→UDMA→MTE→ROCE；位图在初始化时由 entity 层的 `CanReachDataOperators` 生成并镜像到 device。本讲的关键用例就是对这套分派逻辑做「故障注入」测试。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tests/unittest/CMakeLists.txt` | UT 顶层 CMake：挂载 device/host/include 三个子目录，链接出 `aclshmem_unittest` 可执行文件 |
| `tests/unittest/host/CMakeLists.txt` | host 侧测试目标：按 SOC 类型过滤源码与用例、探测 CANN 能力 |
| `tests/unittest/device/CMakeLists.txt` | device 侧 kernel 目标：用 bisheng 编译所有 `*_kernel.cpp` |
| `tests/unittest/host/main_test.cpp` | 测试入口 `main` + 公共骨架（初始化家族、fork 编排、HBM 泄漏检查） |
| `tests/unittest/host/init/init_host_test.cpp` | init 模块用例：含 `TestSetRdmaQpNumBeforeInit` 等参数校验与编译期 `#if` 范例 |
| `tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp` | QP-specific ROCE 接口用例：编译期后端门控的代表作 |
| `tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp` | UDMA 用例集，本轮新增 `TestShmemUDMAHighLevelLocalRma`（平台门控 + topo 注入） |
| `tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp` | UDMA 测试 kernel，本轮新增 `UDMAHighLevelLocalRmaTest` |
| `tests/unittest/include/unittest/udma_mem_kernel.h` | kernel 启动函数的 host 侧声明（桥接层） |
| `src/device/gm2gm/shmem_device_rma.hpp` | 被测对象之一：高阶接口的引擎分派宏（UDMA 分支含 `pe != mype` 守卫） |
| `src/host/entity/mem_entity_default.cpp` | 被测对象之二：`CanReachDataOperators` 按 rank 通告引擎可达性 |
| `scripts/run.sh` | UT 运行入口：拼装 5 个位置参数、执行 `aclshmem_unittest`、可选收集覆盖率 |
| `scripts/build.sh` | 构建入口，`-uttests` 开关打开 UT 编译 |

## 4. 核心概念与源码讲解

### 4.1 UT 构建体系：编译开关与三层 CMake

#### 4.1.1 概念说明

SHMEM 的 UT 不是独立工程，而是宿主工程的可选子目录：只有显式打开 `USE_UNIT_TEST` 开关，`tests/unittest` 才会参与编译。UT 目标把 `src/host` 的产品源码直接编进测试目标（带覆盖率插桩），而不是链接 `libshmem.so`——这样覆盖率统计（lcov）才能精确到产品源码行。

UT 分三层：

| 层 | 目录 | 产物 | 编译器 |
| --- | --- | --- | --- |
| device | `tests/unittest/device` | `libaclshmem_unittest_device.so`（测试 kernel） | bisheng（AscendC） |
| host | `tests/unittest/host` | `libaclshmem_unittest_host.a`（产品源码 + 用例，OBJECT 库） | gcc/g++ |
| 顶层 | `tests/unittest` | `aclshmem_unittest` 可执行文件 | g++ 链接 |

平台裁剪贯穿构建期：非 Ascend950 平台会把 UDMA 传输源码、topo 及 `transport/device_udma` 下的用例整目录剔除（见下文 host CMake 的过滤规则）；`host/mem/udma_mem` 这类数据面用例文件则在所有平台都参与编译，靠用例内部的运行时门控（4.4 节）在非 950 上跳过执行。

#### 4.1.2 核心流程

```text
bash scripts/build.sh -uttests
        │  追加 -DUSE_UNIT_TEST=ON、BUILD_TYPE=Debug、先编 googletest
        ▼
根 CMakeLists.txt: if(USE_UNIT_TEST) → add_subdirectory(tests/unittest)
        ▼
tests/unittest/CMakeLists.txt
        ├─ add_subdirectory(device)  → bisheng 编 *_kernel.cpp → aclshmem_unittest_device
        ├─ add_subdirectory(host)    → gcc 编 src/host/**(带平台过滤) + *_test.cpp → aclshmem_unittest_host
        └─ add_subdirectory(include) → 测试公共头
        ▼
链接: aclshmem_unittest = host对象 + device so + gtest + gmock + gcov + shmem_utils
        ▼
运行: bash scripts/run.sh → build/bin/aclshmem_unittest <5个位置参数> --gtest_filter=...
```

#### 4.1.3 源码精读

- [scripts/build.sh:L546-L552](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/scripts/build.sh#L546-L552)：`-uttests` 入口。先 `fn_build_googletest` 编译测试框架，切到 Debug 构建，克隆 catlass 依赖，最后把 `-DUSE_UNIT_TEST=ON` 追加进 CMake 选项。
- [CMakeLists.txt:L66](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/CMakeLists.txt#L66)：`option(USE_UNIT_TEST ... OFF)`，UT 默认关闭，普通构建不编测试。
- [CMakeLists.txt:L434-L436](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/CMakeLists.txt#L434-L436)：开关生效点，挂载 `tests/unittest` 子目录。
- [tests/unittest/CMakeLists.txt:L12-L14](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/CMakeLists.txt#L12-L14)：顶层依次挂 device、host、include 三个子目录。
- [tests/unittest/CMakeLists.txt:L19-L25](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/CMakeLists.txt#L19-L25)：把 host OBJECT 库链接成 `aclshmem_unittest` 可执行文件，链接期加 `-rdynamic`（供 dlopen 的 bootstrap 插件回查符号）。
- [tests/unittest/CMakeLists.txt:L27-L41](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/CMakeLists.txt#L27-L41)：按 CANN 新旧版本决定额外链接 `ascendc_runtime` 等库；最终统一链 `gtest gmock gcov aclshmem_unittest_include shmem_utils`。
- [tests/unittest/host/CMakeLists.txt:L12-L17](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/CMakeLists.txt#L12-L17)：用 `GLOB_RECURSE` 收编 `src/host/**` 产品源码（排除 python_wrapper、bootstrap），**非 Ascend950 时剔除 `host/transport/device_udma`**——UDMA 引擎源码根本不进非 950 的 UT。
- [tests/unittest/host/CMakeLists.txt:L34-L39](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/CMakeLists.txt#L34-L39)：**用例文件靠约定发现**——凡是 `tests/unittest/host/**` 下以 `_test.cpp` 结尾的文件自动编入，无需手工登记；非 950 再剔除 `user_buffer_heap_test`、`topo/`、`transport/device_udma/` 三处用例。这决定了「新增用例 = 新建 `_test.cpp` 文件」的最小改动路径。
- [tests/unittest/host/CMakeLists.txt:L52-L61](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/CMakeLists.txt#L52-L61)：用 `check_cxx_source_compiles` 探测当前 CANN 的 HCOMM 头是否带 `channelName` 字段，结果存入 `ACLSHMEM_UT_HCOMM_HAS_CHANNEL_NAME`。
- [tests/unittest/host/CMakeLists.txt:L80-L85](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/CMakeLists.txt#L80-L85)：探测结果转成编译宏并打印提示——「UDMA 多 QP 数据面用例启用/不执行将直接通过」，这是**构建期能力探测 + 运行期优雅降级**的组合拳。
- [tests/unittest/device/CMakeLists.txt:L11](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/CMakeLists.txt#L11)：device 侧同样按约定发现：`*_kernel.cpp` 自动编入 `aclshmem_unittest_device`。
- [tests/unittest/device/CMakeLists.txt:L13-L16](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/CMakeLists.txt#L13-L16)：kernel 目标强制用 bisheng 编译，产出共享库并链入 `$<TARGET_OBJECTS:aclshmem_device>`（产品 device 源码对象）。

#### 4.1.4 代码实践

1. **实践目标**：亲手编出 UT 产物，并观察平台裁剪日志。
2. **操作步骤**：
   - 在具备 CANN 环境（且 `ASCEND_HOME_PATH` 已 source）的机器上执行：
     ```bash
     bash scripts/build.sh -uttests                          # 910 等默认平台
     bash scripts/build.sh -uttests -soc_type Ascend950 \
          -enable_rdma -rdma_backend XSCALE                 # 950 + XSCALE 后端
     ```
   - 观察两遍构建的 CMake 输出差异，重点找 `UDMA`、`HCOMM channelName`、`USE_UNIT_TEST` 三类状态消息。
   - 确认产物存在：`ls build/bin/aclshmem_unittest build/lib*/libaclshmem_unittest_device.so`。
3. **需要观察的现象**：默认平台构建中不出现 `host/transport/device_udma` 的编译行；950 构建会出现 `UDMA multi-QP data-path tests enabled` 或 `... pass without execution` 之一（取决于 CANN 版本）。
4. **预期结果**：两种平台均能产出 `aclshmem_unittest`；差异只在被编入的源码集合与编译宏。
5. 本机无 NPU 环境时只能验证到「编译是否通过」，运行行为**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 UT 要把 `src/host` 的 `.cpp` 直接编进测试目标，而不是链接已装好的 `libshmem.so`？
  **答案**：直接编源码可以加 `-fprofile-arcs -ftest-coverage`（见 [tests/unittest/host/CMakeLists.txt:L88](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/CMakeLists.txt#L88)），让 `scripts/run.sh` 里的 lcov 能统计产品源码的行/分支覆盖率；链接 so 则只能测到接口边界，无法归因到具体源码行。
- **练习 2**：新建一个测试文件 `tests/unittest/host/mem/foo/my_api_test.cpp`，需要改哪些 CMake 才能被编入？
  **答案**：不需要改任何 CMake。文件名以 `_test.cpp` 结尾且位于 `tests/unittest/host/` 下，会被 [tests/unittest/host/CMakeLists.txt:L34](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/CMakeLists.txt#L34) 的 `GLOB_RECURSE` 自动收编；唯一要注意的是别落在非 950 被剔除的目录（`topo/`、`transport/device_udma/`）里。
- **练习 3**：`-uttests` 为什么强制 `BUILD_TYPE=Debug`？
  **答案**：Debug 构建保留断言与调试符号，且与覆盖率插桩配合；`scripts/run.sh` 也按 Debug+gcov 的产物路径收集覆盖率。release 优化会干扰断点式调试与覆盖率归因。

### 4.2 测试公共骨架：main_test.cpp 的进程编排

#### 4.2.1 概念说明

`main_test.cpp` 是所有 host 用例共享的「测试操作系统」。它做四件事：

1. **解析运行参数**：`main` 从 argv 读 5 个位置参数——rank 数、ip:port、每节点 NPU 数、起始 rank、起始 NPU 编号，存入全局变量（`test_global_ranks`、`test_gnpu_num` 等），供所有用例取用。
2. **提供初始化家族**：`test_init` / `test_rdma_init` / `test_sdma_init` / `test_udma_init` / `test_cross_init` / `test_multi_instance_init`，把「aclInit → SetDevice → CreateStream → 填属性 → aclshmemx_init_attr」的固定序列封装成一行调用，差别只在 `data_op_engine_type` 位掩码。
3. **fork 多进程编排**：`test_mutil_task(func, mem_size, n)` 用 `fork()` 把同一个测试函数复制成 n 个进程模拟 n 个 PE，子进程以信号上报成败（`SIGUSR1` 成功 / `SIGUSR2` 失败），父进程 `waitpid` 逐个验收。
4. **HBM 泄漏检查**：`main` 在跑用例前后各采样一次 HBM 占用（`HbmLeakChecker`），若用例全过但内存泄漏，整体仍判失败。

#### 4.2.2 核心流程

`test_mutil_task` 的协作协议：

```text
父进程                                     n 个子进程（各代表一个 PE）
  │ fork() × n ──────────────────────────▶ │
  │                                         │ func(rank_id + test_first_rank, ...)
  │                                         │ 执行测试体（内部各自 init/malloc/finalize）
  │                                         │ raise(HasFailure() ? SIGUSR2 : SIGUSR1)
  │ waitpid 逐个收割 ◀──────────────────── │
  │ 收到信号 ≠ SIGUSR1 → 父进程 FAIL()
```

为什么用信号而不是退出码：fork 出的子进程继承了 googletest 的失败状态，`::testing::Test::HasFailure()` 在子进程内可查；但 `exit()` 会绕过部分清理，且多个子进程并发写 gtest 内部状态不安全，因此子进程只跑测试体、用信号回报，gtest 的结果汇总全部留在父进程。

#### 4.2.3 源码精读

- [tests/unittest/host/main_test.cpp:L586-L610](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/main_test.cpp#L586-L610)：`main` 依次读 5 个位置参数，随后 `InitGoogleTest` + `RUN_ALL_TESTS`；末尾的 `HbmLeakChecker::CheckAfter()` 决定「用例全过但泄漏」时返回 1。
- [tests/unittest/host/main_test.cpp:L62-L86](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/main_test.cpp#L62-L86)：`test_init` 标准骨架——校验 rank 数是 2 的幂、`aclInit`、按 `rank_id % test_gnpu_num + test_first_npu` 选设备、建 stream、关 TLS、`ACLSHMEMX_INIT_WITH_DEFAULT` 初始化。每个 `EXPECT_EQ` 都在子进程内生效。
- [tests/unittest/host/main_test.cpp:L203-L228](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/main_test.cpp#L203-L228)：`test_udma_init`——与 `test_init` 唯一实质差别是 `data_op_engine_type = ACLSHMEM_DATA_OP_UDMA`（L222），UDMA 系用例的统一入口，4.4 节的新用例就用它。
- [tests/unittest/host/main_test.cpp:L230-L237](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/main_test.cpp#L230-L237)：`test_finalize` 逆序收尾：`aclshmem_finalize` → 销毁 stream → `aclrtResetDevice` → `aclFinalize`，与 u1-l4 讲的骨架完全一致。
- [tests/unittest/host/main_test.cpp:L253-L277](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/main_test.cpp#L253-L277)：`test_mutil_task` 本体。注意 L262 传给测试体的 `i + test_first_rank`（PE 编号）和 L263 的 `raise(HasFailure() ? SIGUSR2 : SIGUSR1)`；父进程 L268-275 验收信号，非 `SIGUSR1` 一律 `FAIL()`。

#### 4.2.4 代码实践

1. **实践目标**：跑通「一个用例」，理解 5 个位置参数与过滤器的对应关系。
2. **操作步骤**（需 NPU 环境，待本地验证）：
   ```bash
   cd <仓库根目录>
   bash scripts/run.sh -ranks 2 -gnpus 2 -test_filter TestShmemInit
   ```
   对照 [scripts/run.sh:L131-L136](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/scripts/run.sh#L131-L136)：脚本最终执行
   `./bin/aclshmem_unittest <RANK_SIZE> <IPPORT> <GNPU_NUM> <FIRST_RANK> <FIRST_NPU> --gtest_filter=...`，5 个位置参数正是 4.2.3 中 `main` 解析的那 5 个。
3. **需要观察的现象**：`test_detail.xml` 里该用例的状态；`-ranks` 传非 2 的幂时 `test_init` 会打印 `[TEST] input rank_size ... is not the power of 2`。
4. **预期结果**：Ascend950 机器上 `TestShmemInit` 通过；非 2 的幂的 rank 数直接失败。
5. `-test_filter` 的拼接逻辑在 [scripts/run.sh:L110-L124](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/scripts/run.sh#L110-L124)：会把入参包成 `*参数*` 的模糊匹配，并剥掉 `;&|` 等危险字符。

#### 4.2.5 小练习与答案

- **练习 1**：`test_mutil_task` 里子进程为什么不能直接把失败传给 gtest 汇总？
  **答案**：gtest 的失败记录在进程内存里，fork 出的子进程各自一份；若让子进程各自打印，结果无法聚合。所以协议改为：子进程用 `HasFailure()` 查本地状态并 `raise(SIGUSR1/SIGUSR2)`，父进程 `waitpid` 解析信号统一 `FAIL()`（[main_test.cpp:L266-L276](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/main_test.cpp#L266-L276)）。
- **练习 2**：`test_udma_init` 里设备号为什么是 `rank_id % test_gnpu_num + test_first_npu`？
  **答案**：多机场景下 rank_id 是全局 PE 编号；`% test_gnpu_num` 折算到本节点第几张卡，`+ test_first_npu` 支持从指定卡号起跑（对应 `run.sh` 的 `-fnpu` 参数），使 UT 能在共享集群上「占一段卡」运行。
- **练习 3**：HBM 泄漏检查失败但用例全过时，`main` 返回什么？
  **答案**：返回 1（[main_test.cpp:L605-L609](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/main_test.cpp#L605-L609)），即整体按失败处理——防止「用例绿了、显存漏了」的假阳性。

### 4.3 引擎相关测试的门控写法（一）：编译期门控与参数校验

#### 4.3.1 概念说明

引擎相关接口有个天然矛盾：接口签名全平台一致，但能力只属于特定平台/后端。UT 的解法是**门控（gating）**——用例在任何平台都注册、都能跑，但不满足条件时显式 `GTEST_SKIP()` 而不是失败。这样一份测试代码可以随同一个二进制走遍所有平台，测试报告里「skipped」与「failed」语义分明。

本节看两个入门级范例：

1. **`TestSetRdmaQpNumBeforeInit`**：纯参数校验，不碰设备、不 fork，是最容易模仿的「最小 UT」模板。
2. **`TestShmemRdmaQpSpecificApis`**：QP-specific ROCE 接口只支持 XSCALE 后端。后端在**编译期**由宏 `ACLSHMEMI_RDMA_K_BACKEND_XSCALE` 决定（见 u5-l7），用例把宏翻译成 `constexpr bool`，运行时据此 `GTEST_SKIP()`。

#### 4.3.2 核心流程

编译期门控的信息流：

```text
build.sh -rdma_backend XSCALE (-soc_type Ascend950)
   ▼
根 CMakeLists: add_compile_definitions(ACLSHMEMI_RDMA_K_BACKEND_XSCALE=1)
   ▼
测试文件: #if defined(...) → constexpr bool kQpSpecificBackendSupported = true/false
   ▼
TEST 入口: 不支持则 GTEST_SKIP()（用例显示 skipped，不算失败）
   ▼
支持则 test_mutil_task fork 出多 PE，kernel 侧按 qp_idx 直驱指定 QP
```

#### 4.3.3 源码精读

- [CMakeLists.txt:L214-L233](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/CMakeLists.txt#L214-L233)：后端选择逻辑。950 上必须显式给 `-rdma_backend`，`XSCALE` 在 L222 变成全局编译宏 `ACLSHMEMI_RDMA_K_BACKEND_XSCALE=1`，`HNS_1825` 对应另一个宏。
- [tests/unittest/host/init/init_host_test.cpp:L574-L583](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/init/init_host_test.cpp#L574-L583)：`TestSetRdmaQpNumBeforeInit`——对 `aclshmemx_set_qp_num` 做「init 前可改、边界值拒绝」的校验：合法档位 `{1,2,4,8,MAX}` 全部 `SUCCESS`，`0` 与 `MAX+1` 均 `INVALID_VALUE`。**无需设备、无需 fork**，是参数校验类 UT 的标准模板（正例一组 + 边界一组）。
- [tests/unittest/host/init/init_host_test.cpp:L962-L966](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/init/init_host_test.cpp#L962-L966)：另一种写法——在多实例用例体内直接 `#if defined(ACLSHMEMI_RDMA_K_BACKEND_XSCALE)` 分支，XSCALE 配 4 条 QP、其他后端配 1 条。适合「同一行为在不同后端参数不同」的场合。
- [tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp:L28-L32](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp#L28-L32)：把编译宏固化成 `constexpr bool kQpSpecificBackendSupported`，用例逻辑与宏判断解耦。
- [tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp:L76-L102](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp#L76-L102)：测试体三段式——L79 **init 前**调 `aclshmemx_set_qp_num(ROCE, 2)`（承接 u2-l2 的「init 后冻结」语义）；L82 `test_rdma_init` 建链；L90-L97 依次下发 4 个 kernel（raw/tensor × put/get 的 QP 变体）并逐一校验数据。
- [tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp:L105-L114](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp#L105-L114)：`TEST` 入口的门控样板：`if (!kQpSpecificBackendSupported) GTEST_SKIP() << "RDMA QP-specific APIs only support XSCALE backend.";`——HNS_1825 后端的二进制上该用例稳定显示 skipped。

#### 4.3.4 代码实践

1. **实践目标**：观察同一份测试二进制在不同后端下的 skipped/通过差异；体会「编译期宏 → 运行时 SKIP」的转换。
2. **操作步骤**（需 950 环境，待本地验证）：
   ```bash
   # 构建两个后端的 UT（分别用独立 build 目录，注意 build.sh 默认清空 build/）
   bash scripts/build.sh -uttests -soc_type Ascend950 -enable_rdma -rdma_backend XSCALE
   bash scripts/run.sh -test_filter TestShmemRdmaQpSpecificApis
   # 换 HNS_1825 重复上述两步
   ```
3. **需要观察的现象**：XSCALE 下用例状态为 `PASSED`（或按环境 `SKIPPED`，若 CANN 不支持相应数据面）；HNS_1825 下状态为 `SKIPPED`，跳过原因正是 L108 的消息文本。
4. **预期结果**：任何后端下该用例都不会 `FAILED`——这正是门控的意义。
5. 若本地只有非 950 环境：观察构建期是否就因源码裁剪而根本不含 UDMA 用例，记录 CMake 状态行。

#### 4.3.5 小练习与答案

- **练习 1**：`TestSetRdmaQpNumBeforeInit` 为什么敢在没有任何 init 的情况下直接调 `aclshmemx_set_qp_num`？
  **答案**：QP 数是进程级配置，生命周期规则就是「init 前可设置」（u2-l2）：未初始化时设置返回 `SUCCESS`，非法值返回 `INVALID_VALUE`，两者都不依赖集群状态，所以无需 fork、无需设备。测试顺序 `{1,2,4,8,MAX}` 后再补 `0` 与 `MAX+1` 的边界，是典型的「合法档 + 越界档」组合。
- **练习 2**：`GTEST_SKIP()` 与直接 `return` 有什么区别？
  **答案**：`GTEST_SKIP()` 让用例在报告中标记为 skipped（显式「此处未测」），并携带原因字符串；直接 `return` 会被记为 passed，掩盖「平台不支持所以没测」这一事实，属于测试假阳性。
- **练习 3**：如果某新接口只在「950 + XSCALE」可用，门控条件该怎么写？
  **答案**：编译期宏已经隐含了 950（`ACLSHMEMI_RDMA_K_BACKEND_XSCALE` 只在 `SOC_TYPE STREQUAL "Ascend950"` 时定义，见 [CMakeLists.txt:L215-L222](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/CMakeLists.txt#L215-L222)），因此用例侧照抄 qp_specific 的两行——宏转 `constexpr bool` + 不满足 `GTEST_SKIP()`——即可；若还依赖 CANN 版本能力，再叠加 4.1.3 中 `ACLSHMEM_UT_HCOMM_HAS_CHANNEL_NAME` 式的构建期探测宏。

### 4.4 平台门控与 topo 注入（二）：TestShmemUDMAHighLevelLocalRma（本轮新增）

#### 4.4.1 概念说明

本模块讲本轮（commit `1e7fffb`，fix(udma): route local RMA through MTE）新增的用例，它是「测守卫逻辑」的教科书案例。

背景：高阶 put/get 按 SDMA→UDMA→MTE→ROCE 分派，而 **UDMA 不支持自发送（self-send）**。修复加了两层防御（承接 u4-l2、u5-l1、u5-l4）：

| 层 | 位置 | 防御内容 |
| --- | --- | --- |
| 第 1 层（topo 层） | `MemEntityDefault::CanReachDataOperators` | 不为本 rank 通告 UDMA 位，本 rank 仅 MTE 可达 |
| 第 2 层（分派层） | `ACLSHMEM_UDMA_TRANSPORT_ENABLED` 宏 | 追加 `pe != mype` 守卫，topo 位即使异常也回退 MTE |

难点在于：第 1 层防御正常工作时，本 rank 的 topo 位上根本没有 UDMA 位，第 2 层守卫「永远轮不到被触发」。要测第 2 层，必须**故意破坏第 1 层**——在 kernel 里把本 PE 的 UDMA topo 位强行置 1（故障注入），再调用高阶接口，验证数据仍被正确拷贝（说明走了 MTE 而非 UDMA）。同时用例用 `aclrtGetSocName()` 做运行时平台门控，非 Ascend950 直接 `GTEST_SKIP()`。

#### 4.4.2 核心流程

```text
TEST(TestMemApi, TestShmemUDMAHighLevelLocalRma)
  │ aclrtGetSocName() 含 "Ascend950"？ 否 → GTEST_SKIP
  ▼ test_mutil_task fork 出 test_gnpu_num 个 PE
每个 PE:
  │ test_udma_init（引擎位掩码 = UDMA）
  │ 断言第 1 层：topo_list[my_rank] 有 MTE 位、无 UDMA 位   ← 正向验证 entity 层
  │ aclshmem_malloc 8 段×64B 对称缓冲；偶数段写入规律数据
  │ 下发 kernel UDMAHighLevelLocalRmaTest
  │     ├─ 读 device_state->topo_list[my_pe]，强行 OR 上 UDMA 位（注入）
  │     ├─ dcci_cacheline 刷cache，保证分派逻辑从 GM 读到新值
  │     ├─ 对 my_pe 依次：putmem / getmem / putmem_nbi+mte_quiet / getmem_nbi+mte_quiet
  │     └─ 恢复原 topo 字节
  ▼ 同步流、拷回 host
  │ 逐字节校验：每个偶数段(源) == 相邻奇数段(目的)            ← 验证拷贝经 MTE 正确完成
  ▼ free + test_finalize
```

为什么「拷贝成功 + `aclshmemx_mte_quiet()` 能等到完成」就等于「走了 MTE」？因为若分派错误地选中 UDMA，自发送不被支持，拷贝不会完成，`mte_quiet` 也不会使数据就位，逐字节校验必然失败。用 MTE 的 quiet 收尾本身就是在「押注」引擎选择正确。

#### 4.4.3 源码精读

先看被测的两层防御：

- [src/device/gm2gm/shmem_device_rma.hpp:L26-L30](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L26-L30)：高阶接口的引擎分派宏。UDMA 版本在 L30 追加了 `((PE) != (STATE)->mype)` 守卫——本轮修复的核心一行：即使 `topo_list[pe]` 带 UDMA 位，本 PE 的 put/get 也永不选 UDMA，回落到后面的 MTE 分支。
- [src/host/entity/mem_entity_default.cpp:L926-L945](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp#L926-L945)：`CanReachDataOperators` 按 `remoteRank` 计算可达引擎集；L939-L942 在原有条件上增加 `remoteRank != options_.rankId`，即 **UDMA 位只通告给远端 rank**，本 rank 最多拿到 MTE/SDMA/RDMA 位（L929-L931 保证本地 MTE 可达）。
- [include/host/shmem_host_def.h:L129-L134](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/host/shmem_host_def.h#L129-L134)：`topo_list` 使用的引擎位定义，`ACLSHMEM_TRANSPORT_UDMA = 1 << 3`。

再看用例本体（host 侧）：

- [tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp:L443-L452](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp#L443-L452)：`TEST` 入口。L445-L448 是**运行时平台门控**：`aclrtGetSocName()` 返回空或不含 `Ascend950` 即 `GTEST_SKIP()`；随后 `test_mutil_task` 以 1 GiB 对称堆 fork 出 `test_gnpu_num` 个 PE。
- [tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp:L400-L439](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp#L400-L439)：每个 PE 的测试体。L404 用 `test_udma_init`（引擎位掩码只开 UDMA）；L415-L422 准备 8×64 字节缓冲，只在偶数段填 `region*17+offset+1` 的规律数据并整体拷入对称堆；L424 下发 kernel；L427-L435 逐字节校验「偶数段内容 == 下一奇数段内容」。
- [tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp:L407-L409](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp#L407-L409)：**第 1 层防御的正向断言**——初始化完成后，`g_state.topo_list[rank]` 必须含 `ACLSHMEM_TRANSPORT_MTE` 且不含 `ACLSHMEM_TRANSPORT_UDMA`。注意这里直接读了库内部全局 `g_state`（头文件 `shmemi_host_common.h` 暴露），host 用例因此能对库内状态做白盒断言。

然后是用例本体（device 侧）：

- [tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp:L145-L175](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L145-L175)：kernel `UDMAHighLevelLocalRmaTest`。L150-L151 经 `aclshmemi_get_state()` 取 device 侧全局状态并保存本 PE 的原始 topo 字节；L153-L155 **故障注入**：`topo_list[my_pe] |= ACLSHMEM_TRANSPORT_UDMA` 后调 `dcci_cacheline` 刷缓存行——不刷的话修改可能停在 cache，分派逻辑从 GM 读到的还是旧值，注入就失效了。
- [tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp:L157-L171](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L157-L171)：8 段缓冲排布成 4 组「源/目的」，对 `my_pe` 依次执行阻塞 `aclshmem_putmem` / `aclshmem_getmem`，非阻塞 `putmem_nbi` / `getmem_nbi`，两个 nbi 后各跟一次 `aclshmemx_mte_quiet()`——用 MTE 的 quiet 等待完成，正是「期望引擎为 MTE」的行为验证。
- [tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp:L173-L174](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L173-L174)：**恢复现场**：把 topo 字节写回原值并再次刷缓存，保证注入不泄漏到后续用例。
- [tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp:L177-L180](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L177-L180)：host 侧启动函数 `test_udma_highlevel_local_rma`，用 AscendC 的 `<<<block_dim, nullptr, stream>>>` 语法把 kernel 挂到 stream 上。
- [tests/unittest/include/unittest/udma_mem_kernel.h:L28](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/include/unittest/udma_mem_kernel.h#L28)：启动函数在 host 侧的声明——host 用例与 device kernel 之间唯一的桥。新增「host 用例 + kernel」组合时，必须在这里（或对应头文件）补声明，否则 host 目标链接不到。

#### 4.4.4 代码实践

1. **实践目标**：单独运行新用例，并对照源码走一遍「注入 → 分派 → 校验」链路。
2. **操作步骤**（Ascend950 环境；其他环境为**待本地验证**）：
   ```bash
   bash scripts/run.sh -test_filter TestShmemUDMAHighLevelLocalRma
   ```
   然后做三处源码对照：
   - 在 [shmem_device_rma.hpp:L29-L30](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L29-L30) 找到 `(PE) != (STATE)->mype`，回答：若删掉这个条件，用例会在哪一步失败？
   - 在 [mem_entity_default.cpp:L939-L942](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp#L939-L942) 找到 `remoteRank != options_.rankId`，回答：若删掉它，host 侧 L409 的哪个断言会先红？
   - 找出 kernel 里两次 `dcci_cacheline` 分别服务于「注入」和「恢复」的哪一侧。
3. **需要观察的现象**：950 上用例通过；非 950 上该用例文件仍参与编译并注册，但入口处被 `GTEST_SKIP()` 拦下，报告中显示 skipped 且原因文本为 `UDMA high-level local RMA test requires Ascend950`。
4. **预期结果**：
   - 删掉分派宏守卫 → 数据拷贝经 UDMA 自发送不会完成 → 逐字节校验（L427-L435）失败；
   - 删掉 entity 层条件 → `topo_list[rank]` 带上 UDMA 位 → L409 的 `EXPECT_EQ(... & ACLSHMEM_TRANSPORT_UDMA, 0)` 失败（该断言正是为第 1 层防御设置的「哨兵」）。
   （以上推演基于源码逻辑，实际破坏性实验请勿在共享环境尝试。）

#### 4.4.5 小练习与答案

- **练习 1**：为什么这个用例要「主动注入错误的 topo 位」，而不是直接相信第 1 层防御？
  **答案**：第 1 层（entity 层）生效时本 rank 位图上没有 UDMA 位，第 2 层（分派宏守卫）的分支永远走不到，等于没被测过。注入把第 2 层逼到「topo 位为真但 pe == mype」的对抗场景，验证守卫独立成立——这是纵深防御测试的通用手法：逐层制造「上一层失效」的现场。
- **练习 2**：`dcci_cacheline` 若被删掉，用例可能出现什么形态的失败？
  **答案**：可能「偶发通过」。topo 修改停留在 cache 而 GM 里仍是旧值时，分派逻辑读到无 UDMA 位、照样走 MTE，用例通过但**没有测到守卫**（伪通过）；只有 GM 真的看到 UDMA 位、且守卫把它挡回 MTE，才是有效通过。可见该用例的正确性依赖 cache 一致性处理，这也是它写成「注入 + 刷写 + 恢复」三段的原因。
- **练习 3**：kernel 里 `aclshmemx_mte_quiet()` 换成 `aclshmemx_udma_quiet()` 会怎样？
  **答案**：语义上就不再自洽——期望路径是 MTE，却去等 UDMA 的队列完成。按 u4-l5 讲的引擎各自保序规则，等待错误引擎的 quiet 无法保证 MTE 拷贝可见，校验可能读到旧值；这个「quiet 必须与预期引擎配套」的细节本身就是被测行为的组成部分。

### 4.5 为新接口补一个最小 UT 的标准套路

#### 4.5.1 概念说明

把前三个模块收敛成可复用的「新增用例检查单」。SHMEM 的 host 用例有非常固定的形态：

| 步骤 | 要点 | 参照 |
| --- | --- | --- |
| 1 选位置 | 文件名必须 `*_test.cpp`，放在与被测模块对应的子目录（host/mem、host/sync、host/init…） | 4.1.3 GLOB 规则 |
| 2 写测试体 | 匿名 namespace 内的普通函数 `void test_xxx(int rank_id, int n_ranks, uint64_t local_mem_size)`，内部 init → 业务 → 校验 → finalize | 4.2.3 / 4.4.3 |
| 3 注册用例 | `TEST(套件名, 用例名)` 里只做门控 + `test_mutil_task(test_xxx, mem, n)` 两件事 | 4.3.3 / 4.4.3 |
| 4 按需门控 | 平台/后端相关才加：编译宏转 bool 或 `aclrtGetSocName()` + `GTEST_SKIP()` | 4.3 / 4.4 |
| 5 涉及 kernel | device 侧新建/扩展 `*_kernel.cpp`，并在 `tests/unittest/include/unittest/` 头文件补启动函数声明 | 4.4.3 桥接头 |
| 6 验证 | `build.sh -uttests` 编译；`run.sh -test_filter <用例名>` 运行 | 4.1.4 / 4.2.4 |

纯参数校验类接口（不碰设备）可以退化成 4.3.3 的 `TestSetRdmaQpNumBeforeInit` 形态，连 fork 都不需要。

#### 4.5.2 核心流程

```text
确定被测接口
  ├─ 纯参数校验? ────────────→ 直接 TEST 内 EXPECT（模板：TestSetRdmaQpNumBeforeInit）
  └─ 需要 PE 协作?
        ├─ 只用 host 接口 ────→ 测试体 + test_mutil_task（模板：signal/p2p 系用例）
        └─ 需要 kernel 参与 ──→ 加 kernel + 头文件声明（模板：TestShmemUDMAHighLevelLocalRma）
              └─ 平台限定? ─→ aclrtGetSocName / 编译宏 + GTEST_SKIP
```

#### 4.5.3 源码精读

- [tests/unittest/host/sync/signal/signal_host_test.cpp:L355-L376](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/sync/signal/signal_host_test.cpp#L355-L376)：sync 类用例的极简模板——`TEST(TEST_SYNC_API, test_signal_le_all_pes)` 三行：取 `test_gnpu_num`、定 16 MiB 堆、`test_mutil_task`。六个比较运算（EQ/NE/GT/GE/LT/LE）各一个用例，互相独立。
- [tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp:L105-L114](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp#L105-L114)：带编译期门控的注册样板。
- [tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp:L443-L452](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp#L443-L452)：带运行时平台门控的注册样板。
- [tests/unittest/include/unittest/udma_mem_kernel.h:L28](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/include/unittest/udma_mem_kernel.h#L28)：host/device 桥接声明的落点——新增 kernel 启动函数最容易漏改的就是这一行。

#### 4.5.4 代码实践

1. **实践目标**：为目前 host UT 尚未直接覆盖的 `aclshmemx_get_heap_base`（声明于 [include/host/mem/shmem_host_heap.h:L114](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/host/mem/shmem_host_heap.h#L114)）补一个最小用例并编译通过。
2. **操作步骤**：
   - 新建 `tests/unittest/host/mem/heap/get_heap_base_test.cpp`（文件名以 `_test.cpp` 结尾即可被自动收编，无需改 CMake）。
   - 写入以下内容（**示例代码**，未随仓库提交）：

   ```cpp
   // 示例代码：tests/unittest/host/mem/heap/get_heap_base_test.cpp
   #include <gtest/gtest.h>
   #include "acl/acl.h"
   #include "host/init/shmem_host_init.h"
   #include "host/mem/shmem_host_heap.h"
   #include "shmemi_host_common.h"
   #include "unittest_main_test.h"

   namespace {
   void test_get_heap_base_and_offset(int rank_id, int n_ranks, uint64_t local_mem_size)
   {
       const int32_t device_id = rank_id % test_gnpu_num + test_first_npu;
       aclrtStream stream = nullptr;
       test_init(rank_id, n_ranks, local_mem_size, &stream);  // 注意：test_init 返回 void，内部已含 EXPECT
       ASSERT_NE(stream, nullptr);

       // 断言 1：堆基址非空，且与库内全局状态一致（白盒断言，仿 TestShmemInit 的写法）
       void* heap_base = aclshmemx_get_heap_base(DEVICE_SIDE);
       ASSERT_NE(heap_base, nullptr);
       EXPECT_EQ(heap_base, g_state.heap_base);

       // 断言 2：本 rank 在基址表中的入口就是自己的堆基址（承接 u2-l4 的基址表机制）
       EXPECT_EQ(g_state.p2p_device_heap_base[rank_id], heap_base);

       // 断言 3：malloc 的返回值落在本 rank 堆区间内，堆内偏移可复算
       const size_t buf_size = 1024;
       void* buf = aclshmem_malloc(buf_size);
       ASSERT_NE(buf, nullptr);
       const uint64_t local_offset = static_cast<uint64_t>(static_cast<uint8_t*>(buf) -
                                                           static_cast<uint8_t*>(heap_base));
       EXPECT_LT(local_offset, local_mem_size + ACLSHMEM_EXTRA_SIZE);

       aclshmem_free(buf);
       test_finalize(stream, device_id);
   }
   } // namespace

   TEST(TestMemApi, TestShmemGetHeapBaseAndOffset)
   {
       const int process_count = test_gnpu_num;
       const uint64_t local_mem_size = 1024UL * 1024UL * 64;
       test_mutil_task(test_get_heap_base_and_offset, local_mem_size, process_count);
   }
   ```

   说明：示例刻意只使用真实存在的符号（`g_state.heap_base`、`g_state.p2p_device_heap_base`、`ACLSHMEM_EXTRA_SIZE` 均随 `shmemi_host_common.h` 可见，且 `TestShmemInit` 已有同类断言先例）。若想进一步跨 PE 比对「同序同大小分配得到相同堆内偏移」，需要子进程间交换数据——可参照 [tests/unittest/host/main_test.cpp:L314-L465](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/main_test.cpp#L314-L465) 的 `test_mutil_task_with_uid` 用管道分发数据的编排方式，属于进阶改造。
   - 编译验证：
     ```bash
     bash scripts/build.sh -uttests          # 至少通过编译（本任务无 NPU 时的最低要求）
     bash scripts/run.sh -test_filter TestShmemGetHeapBaseAndOffset   # 有 NPU 时验证运行
     ```
3. **需要观察的现象**：编译期该文件被 `aclshmem_unittest_host` 收编（可在构建日志中搜到 `get_heap_base_test.cpp` 的编译行）；运行期各 PE 打印/断言通过。
4. **预期结果**：编译零错误；运行时 `heap_base == g_state.heap_base` 且各 PE 偏移一致（承接 u2-l4 的对称堆语义）。
5. 无 NPU 环境时只要求「编译通过」，运行结果**待本地验证**。

#### 4.5.5 小练习与答案

- **练习 1**：你的新用例依赖 `g_state` 这个库内部全局变量，这样做是否合理？依据是什么？
  **答案**：合理且有先例。UT 目标把 `src/host` 源码直接编进测试库（4.1.3），`shmemi_host_common.h` 暴露的 `g_state` 因此可见；`TestShmemUDMAHighLevelLocalRma` 就在 host 侧断言了 `g_state.topo_list[rank]`（L407-L409），`TestShmemInit` 也断言过 `g_state.mype/heap_base`。白盒断言是这套 UT 的既定风格。
- **练习 2**：如果新接口只在 Ascend950 上有意义，你的 `TEST` 入口该抄哪一段？
  **答案**：抄 [udma_mem_host_test.cpp:L445-L448](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp#L445-L448) 的运行时门控：`aclrtGetSocName()` 判空 + `find("Ascend950")` + `GTEST_SKIP()`；若依赖构建期才确定的 RDMA 后端，则改抄 qp_specific 的编译宏转 bool 写法。
- **练习 3**：新用例为什么必须保证 `test_finalize` 一定被执行，即使中途断言失败？
  **答案**：`ASSERT_*` 失败会直接从测试体返回，若此时已 init 而未 finalize，堆与控制面资源会遗留，既影响同进程后续用例，也会触发 `main` 里的 HBM 泄漏检查（4.2.3）把整个运行判失败。这也是现有用例把关键失败点用 `ASSERT_*` 前置、清理放在校验之后集中处理的原因。

## 5. 综合实践

**任务：给「同步接口 + 引擎门控」补一条完整 UT 链。**

以 4.5.4 的用例为起点，完成下面三步，把本讲内容串起来：

1. **编译层**：用 `build.sh -uttests` 在默认平台与 `Ascend950` 两种配置下分别编译，记录你的用例文件出现在哪些配置的编译日志里，并用 4.1 的知识解释差异（提示：`*_test.cpp` 的 GLOB 规则 + 非 950 目录剔除规则）。
2. **门控层**：给你的用例加上「仅 Ascend950 执行」的运行时门控（抄 `TestShmemUDMAHighLevelLocalRma` 的 `aclrtGetSocName()` 写法），在非 950 环境确认报告里是 skipped 而不是 failed。
3. **行为层（选做，需 950 环境）**：仿照 `UDMAHighLevelLocalRmaTest` 的「注入-验证-恢复」三段式，写一个最小 kernel：在 kernel 内读取 `aclshmemi_get_state()->topo_list[某远端pe]` 并打印（或经 GM 缓冲带回 host），对照 host 侧 `g_state.topo_list` 的断言，验证 u5-l1 讲的「entity 层可达性镜像到 device」确实成立。注意：对**远端** pe 的 topo 位不要做注入（那会破坏真实路由），只读不写。

交付物：新增/修改的测试文件清单、两种平台的编译日志摘录、（若有硬件）`run.sh -test_filter` 的用例结果。

## 6. 本讲小结

- UT 由 `build.sh -uttests` 打开（`-DUSE_UNIT_TEST=ON`），`tests/unittest` 分 device（bisheng 编 `*_kernel.cpp`）/ host（gcc 编产品源码 + `*_test.cpp`）/ 顶层（链接 `aclshmem_unittest`）三层；用例文件靠文件名约定自动发现，非 950 平台在构建期整目录剔除 UDMA 相关源码与用例。
- `main_test.cpp` 是共享骨架：`main` 解析 5 个位置参数（rank 数、ip:port、NPU 数、起始 rank、起始 NPU）；`test_*_init` 家族封装「aclInit→SetDevice→init_attr」固定序列（按引擎分版本）；`test_mutil_task` 用 fork + `SIGUSR1/SIGUSR2` 信号协议编排多 PE；HBM 泄漏检查把「显存泄漏」也纳入失败判据。
- 引擎相关接口的门控有两种写法：编译期宏（`ACLSHMEMI_RDMA_K_BACKEND_XSCALE`，来自 `-rdma_backend` 构建参数）转 `constexpr bool`，或运行时 `aclrtGetSocName()` 检测芯片，两者最终都落到 `GTEST_SKIP()`——用例全平台注册、不满足即显式跳过，不产生假阳性。
- 本轮新增的 `TestShmemUDMAHighLevelLocalRma` 是「纵深防御测试」范本：host 侧断言第 1 层（entity 层不给本 rank 通告 UDMA 位），kernel 侧强行注入 UDMA topo 位（`dcci_cacheline` 刷写保证可见）逼出第 2 层（分派宏的 `pe != mype` 守卫），再用 `mte_quiet` + 逐字节校验确认本 PE 拷贝确实经 MTE 完成，最后恢复 topo 现场。
- 新增用例的标准套路：`*_test.cpp` 放对应子目录 → 匿名 namespace 测试体（init→校验→finalize）→ `TEST` 入口只做门控与 `test_mutil_task` → 涉及 kernel 时补 `tests/unittest/include/unittest/` 声明 → `build.sh -uttests` 编译、`run.sh -test_filter` 运行。
- `run.sh` 是运行入口：把 `-ranks/-gnpus/-fnpu/-ipport/-test_filter` 翻译成可执行文件的 5 个位置参数加 `--gtest_filter`，跑完还能用 lcov 收集 `src/host` 的分支覆盖率。

## 7. 下一步学习建议

- **u8-l7（端到端通算融合）与 u8-l8（性能测试）**：UT 验证「正确性」，perftest 系示例验证「性能」，两者合成完整的回归手段；建议对照本讲的引擎门控思想，看 perftest 如何按引擎/消息档位组织用例。
- **动手方向**：按 4.5 / 第 5 节的检查单，为你自己业务用到的接口（某个 AMO 变体、stream 接口、team 切分）各补一条 UT，练习把「编译期过滤 / 运行时门控 / 白盒断言」三种手段组合使用。
- **源码延伸阅读**：`tests/unittest/host/sync/handle_wait/handle_wait_host_test.cpp`（含另一种 `GTEST_SKIP()` 用法）、`tests/unittest/host/transport/composite_transport_manager_test.cpp`（不依赖设备的纯逻辑单测）、以及 `tests/unittest/host/main_test.cpp` 中基于管道分发 uniqueid 的 `test_mutil_task_with_uid`（比 `test_mutil_task` 更复杂的进程编排，可对照 u2-l3 的 UniqueID 模式）。
