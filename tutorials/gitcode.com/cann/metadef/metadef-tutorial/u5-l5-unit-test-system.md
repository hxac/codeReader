# 单元测试体系与测试技巧

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 metadef 单元测试的目录组织方式：`tests/ut` 下四个测试目标（`ut_metadef`、`ut_register`、`ut_exe_meta_device`、`ut_sc_check`）各自测什么、如何被 CMake 组织。
2. 掌握本仓 gtest 用法惯例：测试类的命名（`UtestXxx`）、夹具写法、断言风格、以及「新枚举值必须同步补测试」的看护约定。
3. 理解 stub（桩）机制：为什么单测要链接 `slog_stub`、`mmpa_stub` 而不是真实的昇腾日志/平台库，`stub_module` 如何完成「有真库用真库、没真库用桩」的替换。
4. 亲手新增一个测试文件，让它被 `bash tests/run_test.sh -u` 跑到。

## 2. 前置知识

- **gtest（Google Test）**：C++ 最流行的单元测试框架。核心概念三个：
  - `TEST_F(测试类名, 用例名)` 定义一个用例；
  - `ASSERT_*` 断言失败立即终止当前用例，`EXPECT_*` 失败仅记录、继续执行；
  - `SetUp()` / `TearDown()` 是每个用例执行前后的钩子（夹具）。
- **ctest**：CMake 自带的测试执行器。CMakeLists 里用 `add_test()` 登记测试，命令行用 `ctest -L 标签` 按标签筛选执行。
- **stub（桩）**：用一个「假实现」替换真实依赖库。metadef 的运行时依赖日志库 slog、平台封装 mmpa、runtime 等，这些库需要昇腾环境；单测要在普通 x86 服务器上跑，就把它们替换成桩库。
- **GLOB 收集源码**：`file(GLOB_RECURSE ...)` 让 CMake 按通配符自动收集源文件，新增 `.cc` 文件无需修改 CMakeLists（配合 `CONFIGURE_DEPENDS` 重新运行 cmake 时会刷新收集结果）。
- 本讲承接 u1-l2（build.sh 与 run_test.sh 的整体流程）和 u5-l4（ABI 守护测试——那本身就是本仓单测最重要的用例之一）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tests/run_test.sh` | 测试入口脚本：配置 cmake、make 四个 ut 目标、ctest 执行并产出报告 |
| `tests/CMakeLists.txt` | 测试侧总开关：注入 ASan/LSan 编译选项，构建六个桩库并建立 `stub_module` 映射 |
| `tests/ut/CMakeLists.txt` | 仅 4 行有效内容：挂载四个子目录 |
| `tests/ut/base/CMakeLists.txt` | 定义 `ut_metadef` 目标：glob 收集 testcase、链接库清单、注册 ctest |
| `tests/ut/register/CMakeLists.txt` | 定义 `ut_register` 目标：register 链路的单测，多链接 `rt2_registry_static`（whole-archive） |
| `tests/ut/base/testcase/types_unittest.cc` | 本讲精读样本：`ge::Format`/`ge::DataType` 工具函数的测试 |
| `tests/ut/base/testcase/func_counter.h` / `.cc` | 测试辅助设施：统计对象构造/拷贝/移动/析构次数的计数器 |
| `cmake/test_funcs.cmake` | `stub_module` 等 CMake 工具函数的定义处 |
| `tests/ut/sc_check/testcase/sc_check_unittest.cc` | 「仓库目录文件数不超过 50」的结构看护测试 |

## 4. 核心概念与源码讲解

### 4.1 run_test.sh：从一条命令到测试报告

#### 4.1.1 概念说明

`run_test.sh` 是本仓唯一的测试入口。它做的事情可以概括为三步：

1. **配置**：以 UT 模式重新跑一遍 cmake（与 `build.sh` 的发布构建隔离，落在独立目录 `build_gcov/`）；
2. **构建**：`make` 四个测试可执行文件；
3. **执行**：`ctest` 按标签筛选并运行，失败则整体退出码非 0。

#### 4.1.2 核心流程

```text
bash tests/run_test.sh -u
  └─ checkopts: 解析 -u/-c/-j/-v，校验 ASCEND_HOME_PATH 环境变量
  └─ build_metadef:
       ENABLE_METADEF_UT=on → BUILD_RELATIVE_PATH=build_gcov, CMAKE_BUILD_TYPE=GCOV
       cmake 配置到 build_gcov/
       make ut_metadef ut_register ut_exe_meta_device ut_sc_check
  └─ ctest --verbose -j N -L ut --test-dir build_gcov --output-log build_gcov/ctest_ut.log
  └─ (可选 -c) lcov + genhtml 生成覆盖率报告 cov/html
```

#### 4.1.3 源码精读

脚本用 `getopt` 解析参数，`-u` 把 `ENABLE_METADEF_UT` 置 on 并关闭 `GE_ONLY`：

[tests/run_test.sh:39-67](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L39-L67)
脚本解析命令行选项：`-u` 打开 UT 开关，`-c` 打开覆盖率开关，二者都会把 `GE_ONLY` 关掉（即同时编译图编译相关的完整代码）。

UT 模式会切换到独立的构建目录与构建类型：

[tests/run_test.sh:130-151](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L130-L151)
当 UT 或覆盖率开关打开时，构建目录改为 `build_gcov`、构建类型改为 `GCOV`，随后 `make` 四个测试目标。注意四个目标名就是四个可执行文件名，与下文 CMakeLists 一一对应。

执行阶段用 `ctest` 的标签筛选：

[tests/run_test.sh:175-184](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/run_test.sh#L175-L184)
`ctest -L ut` 只跑标签含 `ut` 的测试项；`ASAN_OPTIONS=detect_container_overflow=0` 是配合 AddressSanitizer 的选项（容器溢出检测在 gtest 场景误报较多，故关闭）；失败时打印 `!!! UT FAILED, PLEASE CHECK YOUR CHANGES !!!` 并以非 0 退出——这就是 CI 判定测试失败的依据。

#### 4.1.4 代码实践

1. **实践目标**：看清一次完整 UT 运行经历了哪些阶段、报告落在哪里。
2. **操作步骤**（需要已 source CANN 环境的 Linux 服务器）：
   ```bash
   export ASCEND_HOME_PATH=/usr/local/Ascend/ascend-toolkit/latest  # 按实际路径
   bash tests/run_test.sh -u -j8
   ```
3. **需要观察的现象**：输出中依次出现 `Metadef llt build start` → cmake 参数回显 → make 编译 → `ctest` 逐项输出 `Test #N: ut_metadef ...`。
4. **预期结果**：末尾打印 `---------------- Metadef llt finished ----------------`；`build_gcov/ctest_ut.log` 与 `${CMAKE_INSTALL_PREFIX}/report/ut/*.xml`（gtest 的 XML 报告）生成。若环境不可用，此步「待本地验证」，可用下文的「只加一个测试文件再编译」方式部分验证。

#### 4.1.5 小练习与答案

**练习 1**：`run_test.sh -u` 和 `build.sh` 的产物目录有何不同？为什么分开？

**答案**：`build.sh` 落在 `build/` 且默认 `ENABLE_METADEF_UT=off`，产出发布用的 `.run` 安装包；`run_test.sh -u` 强制切到 `build_gcov/`、构建类型 `GCOV` 并编译测试目标。分开是为了让带覆盖率/ASan 插桩的测试构建不污染发布构建。

**练习 2**：`ctest` 是怎么知道要跑哪些测试的？

**答案**：各 `tests/ut/*/CMakeLists.txt` 里用 `add_test(NAME ut_metadef COMMAND ut_metadef ...)` 登记测试并打上 `LABELS "ut;ut_metadef"`，`ctest -L ut` 按标签筛选执行。

### 4.2 tests/ut 的目录组织：四个测试目标

#### 4.2.1 概念说明

`tests/ut` 按「被测模块」分四个子目录，每个子目录产出一个独立的 gtest 可执行文件。分类标准不是随意的，而是按链接产物划分——测试目标链接什么库，就测什么模块。

| 目录 | 可执行文件 | 被测范围 | 关键链接 |
| --- | --- | --- | --- |
| `tests/ut/base` | `ut_metadef` | 基础数据类型、graph 侧、context builder、plugin 等（17 个测试文件） | `metadef opp_registry error_manager slog_stub mmpa_stub` |
| `tests/ut/register` | `ut_register` | 算子注册链路（OpDef/Factory/op_impl_registry/opp 包） | 追加 `exe_graph tilingdata_base` 与 whole-archive 的 `rt2_registry_static` |
| `tests/ut/exe_meta_device` | `ut_exe_meta_device` | 设备侧元数据（单入口 `main.cc`） | `exe_meta_device` |
| `tests/ut/sc_check` | `ut_sc_check` | 仓库结构看护（目录文件数检查），非功能测试 | 仅 `slog_stub c_sec mmpa_stub` 等 |

#### 4.2.2 核心流程

```text
tests/ut/CMakeLists.txt（挂 4 个子目录）
  ├─ base           → ut_metadef         ┐
  ├─ exe_meta_device→ ut_exe_meta_device ├─ 每个目标：
  ├─ sc_check       → ut_sc_check        │   glob 收集 .cc → add_executable
  └─ register       → ut_register        ┘   → add_test + LABELS "ut"
```

#### 4.2.3 源码精读

顶层只做挂载：

[tests/ut/CMakeLists.txt:11-14](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/CMakeLists.txt#L11-L14)
四个 `add_subdirectory` 一一对应四个测试目标，没有任何其他逻辑——真正的差异都在各子目录的 CMakeLists 里。

`ut_metadef` 用 glob 自动收集测试源文件：

[tests/ut/base/CMakeLists.txt:23-31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/CMakeLists.txt#L23-L31)
`file(GLOB_RECURSE ... CONFIGURE_DEPENDS)` 递归收集 `tests/ut/base/testcase/*.cc`，另收集 `tests/depends/faker` 下的假实现（伪造的 KernelRunContext 装配器等，供 context 相关测试使用）。这意味着**新增测试文件不需要改 CMakeLists**，放入目录重新 cmake 即可。

编译选项带有覆盖率插桩与告警即错误：

[tests/ut/base/CMakeLists.txt:33-44](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/CMakeLists.txt#L33-L44)
`--coverage -fprofile-arcs -ftest-coverage` 为 lcov 覆盖率统计服务；`-Wall -Wfloat-equal -Werror` 让浮点相等比较这类写法直接编译失败；`-fno-access-control` 是测试常用技巧——让编译器放行对被测类 private/protected 成员的直接访问，从而可以白盒测试私有实现；`google=ascend_private` 把 protobuf 的 `google` 命名空间重命名，避免与 CANN 包内版本冲突。

链接清单揭示了「桩替换」的秘密：

[tests/ut/base/CMakeLists.txt:46-53](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/CMakeLists.txt#L46-L53)
注意链接的是 `slog_stub`、`mmpa_stub` 而非真实的 `slog`、`mmpa`——这正是下一节 stub 机制的落点。

`ut_register` 的特殊之处是 whole-archive 链接静态注册库：

[tests/ut/register/CMakeLists.txt:61-63](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/CMakeLists.txt#L61-L63)
`rt2_registry_static` 以 `--whole-archive` 方式链入。原因是注册发生在「静态对象构造期」（u4-l3/u4-l4 讲过），静态库里未被引用的目标文件默认不会被链接器保留，注册代码就会被丢弃；whole-archive 强制保留全部对象，保证注册确实发生。

`ut_sc_check` 是「看护仓库结构」的元测试：

[tests/ut/sc_check/testcase/sc_check_unittest.cc:35-45](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/sc_check/testcase/sc_check_unittest.cc#L35-L45)
它不测任何函数行为，而是递归遍历 `inc/` 与 `base/` 目录，断言每个目录的直属子目录与文件数之和不超过 50（对应线上代码检查的非拦截项）。这解释了为什么本仓目录层级如此深、每个目录文件数不多——有测试在守护这个约定。

#### 4.2.4 代码实践

1. **实践目标**：确认「新增测试文件零 CMake 改动」这条结论。
2. **操作步骤**：在 `tests/ut/base/testcase/` 下新建一个空测试文件 `my_smoke_unittest.cc`：
   ```cpp
   // 示例代码
   #include <gtest/gtest.h>
   TEST(MySmoke, Trivial) { EXPECT_EQ(1 + 1, 2); }
   ```
   重新执行 `bash tests/run_test.sh -u -j8`。
3. **需要观察的现象**：编译日志中出现 `my_smoke_unittest.cc` 被编译进 `ut_metadef`；`ctest` 输出里 `ut_metadef` 的用例总数比之前多 1。
4. **预期结果**：`MySmoke.Trivial` 通过。观察完删除该文件即可（不要提交）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ut_register` 需要 `--whole-archive` 而 `ut_metadef` 不需要？

**答案**：register 测试依赖「静态对象构造期注册」的副作用，而静态库默认丢弃未被直接引用的对象文件，会把注册代码丢掉；`ut_metadef` 主要测试普通函数行为，链接的是动态库 `metadef`/`opp_registry`，动态库整体加载、无此问题。

**练习 2**：`-fno-access-control` 解决了什么问题？有什么代价？

**答案**：让测试代码可以直接访问被测类的 private/protected 成员，便于对内部实现做白盒断言（本仓大量 Impl 类都靠它测）；代价是测试与实现细节强耦合，重构时测试也要跟着改。

### 4.3 gtest 用法惯例：精读 types_unittest.cc

#### 4.3.1 概念说明

`types_unittest.cc` 是本仓测试写法的典型样本：一个被测头文件对应一个 `*_unittest.cc`，测试类命名 `Utest + 被测名`，全部断言用 `ASSERT_EQ`/`EXPECT_EQ`。它还有一条重要惯例：**枚举边界值用断言锁死**——`ASSERT_EQ(FORMAT_END, 55)` 与 `EXPECT_EQ(DT_MAX, 43)` 把枚举总数固化为 ABI 契约。

#### 4.3.2 核心流程

```text
包含被测头文件（#include "external/graph/types.h"）
  → 在 ge 命名空间内定义夹具类 UtestTypes（SetUp/TearDown 留空）
  → 每个工具函数一个 TEST_F：GetFormatName / GetFormatFromSub / GetPrimaryFormat /
    GetSubFormat / GetSizeByDataType / GetSizeInBytes / GetC0ValueFromFormat / Promote
  → 尾部用 FORMAT_END / DT_MAX 断言锁死枚举总数
```

#### 4.3.3 源码精读

测试夹具的定义：

[tests/ut/base/testcase/types_unittest.cc:16-22](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc#L16-L22)
测试类放在 `ge` 命名空间内（与被测函数同命名空间，可省 `ge::` 前缀直接调用 `GetFormatName` 等），继承 `testing::Test`，`SetUp`/`TearDown` 留空——本测试无共享资源需要准备和清理，但仍保留钩子位置，是全仓统一格式。

「新格式必须补测试」的看护约定：

[tests/ut/base/testcase/types_unittest.cc:71-78](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc#L71-L78)
最后一行 `ASSERT_EQ(FORMAT_END, 55)` 带着注释 `// if add formats definition, add ut here`：`FORMAT_END` 的值被硬编码为 55，任何人往 `Format` 枚举尾部加值都会使 `FORMAT_END` 变成 56，此断言立刻失败，提醒开发者同时补上 `GetFormatName` 的对照断言。这是 u2-l1 讲过的「枚举取值是 ABI 契约」在测试层的落地。

位域编码的逆向验证：

[tests/ut/base/testcase/types_unittest.cc:81-94](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc#L81-L94)
`GetFormatFromSub(10, 8)` 断言等于 `0x80a`：主格式占低字节（0x0a=10）、sub-format 移到中间字节（8<<8=0x800），直接用十六进制字面量验证 u2-l1 讲的 32 位位域编码，再由 `GetPrimaryFormat(0x804)` 断言取回 `FORMAT_FRACTAL_Z`，完成编码/解码闭环。

小对象哨兵值与非法输入：

[tests/ut/base/testcase/types_unittest.cc:136-153](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc#L136-L153)
覆盖三类边界：`DT_UNDEFINED`/`DT_MAX`/`static_cast<DataType>(-1)` 返回 -1（非法与不定长）；小于 1 字节的类型（`DT_INT4` 等）返回 `kDataTypeSizeBitOffset + 比特数`（比特大小编码）；最后 `EXPECT_EQ(DT_MAX, 43)` 同样锁死 DataType 枚举总数。

移動语义测试的组织方式：

[tests/ut/base/testcase/types_unittest.cc:191-213](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc#L191-L213)
`Promote` 用例连续三次移动构造/赋值，每步都断言「被移出的对象变空、接收方持有全部数据」——这是本仓测移动语义的标准句式，配合下一节的 `FuncCounter` 还能进一步数清拷贝/移动次数。

#### 4.3.4 代码实践

1. **实践目标**：掌握「读断言反推行为」的阅读法。
2. **操作步骤**：打开 [inc/external/graph/types.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h) 中的 `GetFormatFromSub` 与 `GetSubFormat` 的 inline 实现，对照 `GetFormatFromSub` / `GetSubFormat` 两个用例逐行演算位运算。
3. **需要观察的现象**：`0x804` = 低字节 `0x04`（`FORMAT_FRACTAL_Z`）+ 中间字节 `0x08`（sub-format=8）。
4. **预期结果**：手算结果与每条 `ASSERT_EQ` 一致；特别注意 `GetSubFormat(0xffffff)` 返回 `0xffff` 说明 sub-format 字段宽 16 位。

#### 4.3.5 小练习与答案

**练习 1**：`ASSERT_EQ` 与 `EXPECT_EQ` 在本文件中如何分工？

**答案**：`GetFormatName` 用例用 `ASSERT_EQ`（名字错一条后面全错，继续没意义，立即终止）；`GetSizeByDataType` 用 `EXPECT_EQ`（一次失败不影响其他类型大小的验证，期望收集全部结果）。

**练习 2**：如果有人往 `DataType` 枚举中间插入一个新值，哪些测试会失败？为什么这很有价值？

**答案**：`GetSizeByDataType` 全部排在插入点之后的断言、`DT_MAX == 43`、以及 `GetSizeInBytes` 相关断言都会失败。这恰好拦截了「枚举中间插值导致 ABI 破坏」——正是 u5-l4 强调的只能尾部追加的规则。

### 4.4 stub 机制与测试辅助设施

#### 4.4.1 概念说明

metadef 的实现代码依赖 slog（日志）、mmpa（平台封装）、runtime（运行时）等需要昇腾环境的库。单测要在无 NPU 的普通服务器跑通，就必须用**桩库**替换它们：接口签名一致、实现是「空转或可控记录」的假动态库。本仓的桩分三层设施：

1. **桩库本身**：`tests/depends/{slog,mmpa,platform,runtime}` 下的 `*_stub.cc`；
2. **替换映射**：`stub_module()` CMake 函数——被测库的 CMakeLists 写 `target_link_libraries(... slog)`，测试构建时若真实目标 `slog` 不存在，就建一个同名 INTERFACE 库转发到 `slog_stub`；
3. **测试辅助类**：`func_counter.h`（统计特殊成员函数调用次数）、`faker`（伪造框架侧对象）。

#### 4.4.2 核心流程

```text
tests/CMakeLists.txt
  ├─ add_subdirectory(depends/slog)  → slog_stub 动态库
  ├─ ...（mmpa/platform/runtime 同理）
  └─ stub_module(slog slog_stub)     → 若无真实 slog 目标，
                                       建 INTERFACE 目标 slog → slog_stub
被测产物（metadef 等）链接 "slog" → 实际解析到 slog_stub
```

#### 4.4.3 源码精读

测试总开关先注入内存检查插桩：

[tests/CMakeLists.txt:11-14](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/CMakeLists.txt#L11-L14)
只要 UT/ST/覆盖率任一开关打开，全局追加 `-fsanitize=address -fsanitize=leak -fsanitize-recover=address`——所有测试天然带 ASan+LSan，内存越界与泄漏直接让用例失败。

桩库映射的建立：

[tests/CMakeLists.txt:20-25](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/CMakeLists.txt#L20-L25)
六条 `stub_module` 调用把 `slog`、`unified_dlog`、`mmpa`、`platform`、`runtime` 等名字都映射到对应桩库，其中两个日志名共享 `slog_stub`。

映射函数的实现：

[cmake/test_funcs.cmake:23-29](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/cmake/test_funcs.cmake#L23-L29)
`stub_module` 的逻辑很短：如果同名真实目标已经存在（例如完整 CANN 环境里找到了真库）就直接返回用真库；否则建一个 INTERFACE 库顶替名字、转发链接到桩。这就是「有真库用真库、没真库用桩」的完整实现。

桩库的内部样貌（以日志桩为例）：

[tests/depends/slog/src/slog_stub.cc:18-31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/depends/slog/src/slog_stub.cc#L18-L31)
日志桩从 `ASCEND_GLOBAL_LOG_LEVEL` 环境变量读日志级别（默认只输出 error），`Log()` 按级别过滤后输出——既有最小可观察性，又不刷屏影响测试输出。

测试辅助设施 `FuncCounter`：

[tests/ut/base/testcase/func_counter.h:16-42](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/func_counter.h#L16-L42)
一个纯计数结构体：六种特殊成员函数（构造/析构/拷贝构造/移动构造/拷贝赋值/移动赋值）各自递增静态计数。测试中让它作为 AnyValue、Tensor 等容器的元素类型，配合 `Clear()` 与 `GetTimes()` 就能断言「这次操作发生了 2 次移动、0 次拷贝」，是验证 u2-l3/u2-l4 讲过的深拷贝/移动语义的量化工具。静态成员的定义在 [tests/ut/base/testcase/func_counter.cc:12-18](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/func_counter.cc#L12-L18)（头文件声明、.cc 定义，避免多处初始化）。

#### 4.4.4 代码实践

1. **实践目标**：用 `FuncCounter` 量化一次 `std::vector` 扩容的拷贝/移动行为，体会「计数器式测试」。
2. **操作步骤**（示例代码，可作为新测试文件主体）：
   ```cpp
   // 示例代码
   #include "func_counter.h"
   #include <gtest/gtest.h>
   #include <vector>
   namespace ge {
   class UtestFuncCounterDemo : public testing::Test {};
   TEST_F(UtestFuncCounterDemo, VectorPushBack) {
     FuncCounter::Clear();
     std::vector<FuncCounter> v;
     v.reserve(2);
     v.emplace_back();
     v.emplace_back();
     EXPECT_EQ(FuncCounter::GetTimes()[0], 2U);  // 2 次直接构造
   }
   }  // namespace ge
   ```
3. **需要观察的现象**：去掉 `reserve(2)` 再跑，构造次数会大于 2（扩容触发搬移）。
4. **预期结果**：`GetTimes()` 返回 `{construct, destruct, copy_construct, move_construct, copy_assign, move_assign}`，可逐一断言。**待本地验证**具体数值（与 libstdc++ 版本有关）。

#### 4.4.5 小练习与答案

**练习 1**：`stub_module(slog slog_stub)` 为什么先检查 `if (TARGET ${module})`？

**答案**：完整 CANN 构建环境下可能已经定义了真实的 `slog` 目标（预编译库），此时应直连真库；只有目标不存在（纯单测环境）才用 INTERFACE 桩顶替，避免重定义冲突。

**练习 2**：`FuncCounter::AllTimesZero()` 适合在什么场景使用？

**答案**：容器销毁后断言所有计数归零（构造次数 == 析构次数且赋值计数为 0），用于验证「没有对象泄漏或残留」——例如 AnyValue 清空、TensorData 释放之后。

## 5. 综合实践

给 `ge::Format` 工具函数补一个未覆盖的测试文件，走完「写测试 → 被收集 → 被执行」全流程：

1. 阅读 [tests/ut/base/testcase/types_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc)，确认现有用例已覆盖 `GetFormatName`、`GetFormatFromSub`、`GetPrimaryFormat`、`GetSubFormat`、`GetSizeByDataType`、`GetSizeInBytes`、`GetC0Value`。
2. 翻 [inc/external/graph/types.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h) 找一个未被覆盖的 inline 函数（如 `HasSubFormat` 只在 `GetSubFormat` 用例里顺带测过，可单独成例；或选 `GetC0Format`/`GetFormatFromSubAndC0` 组合更多输入）。
3. 新建 `tests/ut/base/testcase/format_utils_unittest.cc`（示例代码骨架）：
   ```cpp
   #include "external/graph/types.h"
   #include <gtest/gtest.h>
   namespace ge {
   class UtestFormatUtils : public testing::Test {
    protected:
     void SetUp() {}
     void TearDown() {}
   };
   TEST_F(UtestFormatUtils, HasSubFormatMoreCases) {
     EXPECT_EQ(HasSubFormat(FORMAT_ND), false);
     EXPECT_EQ(HasSubFormat(GetFormatFromSub(FORMAT_ND, 1)), true);
   }
   }  // namespace ge
   ```
4. **挂载**：由于 [tests/ut/base/CMakeLists.txt:23-25](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/CMakeLists.txt#L23-L25) 用 `GLOB_RECURSE CONFIGURE_DEPENDS` 收集源文件，**无需修改任何 CMakeLists**，重新运行 cmake 即自动纳入——这一点与规格中「挂入 CMakeLists」的传统做法不同，是本仓的特殊设计，务必记住。
5. 执行 `bash tests/run_test.sh -u -j8`，在 `ctest` 输出与 `report/ut/ut_metadef.xml` 中确认 `UtestFormatUtils.HasSubFormatMoreCases` 出现且通过（无环境时待本地验证）。
6. 收尾：测试通过后按仓库惯例把文件留在工作区供 review（或按需删除），不要顺手修改任何 `inc/`/`base/` 源码。

## 6. 本讲小结

- `tests/run_test.sh -u` 三步走：cmake 配置到独立目录 `build_gcov/` → make 四个测试目标 → `ctest -L ut` 执行，失败即非 0 退出并被 CI 拦截。
- `tests/ut` 按被测产物分四个目标：`ut_metadef`（基础类型与框架）、`ut_register`（注册链路，靠 whole-archive 保住静态注册代码）、`ut_exe_meta_device`（设备侧元数据）、`ut_sc_check`（目录文件数 ≤ 50 的结构看护）。
- gtest 惯例：夹具类 `UtestXxx` 与被测同命名空间、`ASSERT_EQ` 截断 / `EXPECT_EQ` 收集、用 `FORMAT_END == 55`、`DT_MAX == 43` 这类硬编码断言把枚举总数固化为 ABI 契约。
- stub 三层设施：`tests/depends` 桩库 + `stub_module` 的「有真库用真库、没真库用桩」INTERFACE 替换 + `func_counter.h`/`faker` 等辅助；测试构建全局注入 ASan+LSan。
- 新增测试零 CMake 成本：`.cc` 文件放进 `tests/ut/base/testcase/` 即被 `GLOB_RECURSE CONFIGURE_DEPENDS` 自动收集，重新跑 `run_test.sh -u` 就能执行。

## 7. 下一步学习建议

下一讲 u5-l6 是全手册收官的综合实践：为假想算子 MyAdd 完成 OpDef 定义、OpImplFunctions 实现（InferShape + Tiling）、用 OpTilingContextBuilder 构建上下文驱动 Tiling 单测——届时你会同时用到本讲的「新增测试文件」技巧与 u5-l1 的 Builder 体系。在此之前，建议再通读一遍 [tests/ut/base/testcase/context_builder_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc)，它是「测试驱动上下文构建」最完整的范本。
