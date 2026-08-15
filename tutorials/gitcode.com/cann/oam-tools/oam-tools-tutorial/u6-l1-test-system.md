# 测试体系：UT/ST 组织与 build.sh -u 联动

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `bash build.sh -u --component <name>` 这条命令背后完整的驱动链：build.sh 如何解析参数、如何决定跳过/执行 C++ 构建、最终如何调用 `scripts/run_tests.sh`。
2. 理解 `--component`、`--ut`、`--st` 三个参数如何映射到 8 个测试用例组（asys_ut/asys_st、msaicerr_ut/msaicerr_st、msprof_ut、install_st/upgrade_st/uninstall_st）。
3. 读懂 asys/msaicerr 的 pytest 测试组织方式（conftest 路径注入、`AssertTest` 基类、参数化与 mocker 打桩）。
4. 理解 msprof 的 gtest 用例如何被 CMake 构建并通过 `msprof_ut_targets.txt` 清单自动交付给 run_tests.sh 执行。
5. 自己动手仿写一个 asys 参数校验（arg_checker）的 UT 用例并跑通。

## 2. 前置知识

- **UT 与 ST**：UT（Unit Test，单元测试）测一个函数/类本身的行为，不依赖完整环境；ST（System Test，系统测试）把工具当成黑盒，通过命令行真实执行来验证端到端行为。oam-tools 里 asys 的 ST 是真的去跑 `asys.py` 主入口，而 UT 只 import 被测模块。
- **pytest**：Python 最主流的测试框架。核心概念：`test_*.py` 文件自动收集、`Test*` 类中的 `test_*` 方法即用例、`@pytest.mark.parametrize` 做数据驱动、`conftest.py` 提供共享 fixture 与公共函数、`mocker`（pytest-mock 插件）用于打桩替换函数返回值。
- **gtest**：Google 的 C++ 测试框架，用 `TEST(套件名, 用例名)` 宏定义用例，输出形如 `[ RUN ]`、`[ OK ]`、`[ PASSED ]` 的标记行——run_tests.sh 正是靠 grep 这些标记来统计通过/失败数。
- **覆盖率**：pytest 侧用 `coverage.py`（按 `--source` 圈定统计分母）；msprof 的 C++ 侧用 gcov + lcov + genhtml 工具链。
- **.run 包**：CANN 系的自解压安装包（详见 u1-l2、u6-l3）。install/upgrade/uninstall 三组 ST 的被测对象就是 build_out/ 下最新生成的 `cann-oam-tools_*.run`。
- 建议先回顾 u1-l4（build.sh -u 的使用）与 u2-l5（asys collect 子系统，本讲实践会接触到它的测试目录）。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| [build.sh](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh) | 构建总入口；`-u` 开启测试、`--component/--ut/--st` 透传给 run_tests.sh |
| [scripts/run_tests.sh](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh) | 测试调度中枢：参数解析、用例矩阵、按框架执行、结果解析与覆盖率 |
| [test/ut/asys/](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys/testcase) | asys 单元测试，按子命令目录组织（cmdline/collect/health/…） |
| [test/ut/msaicerr/](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/msaicerr/testcase) | msaicerr 单元测试，扁平 `test_*_ut.py` 布局 |
| [test/ut/msprof/](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/msprof/CMakeLists.txt) | msprof gtest 用例的 CMake 组织与运行清单生成 |
| [test/st/](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st) | 系统测试：asys、msaicerr、install、upgrade、uninstall 五组 |
| [test/st/conftest.py](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/conftest.py) | ST 共享 fixture：定位 .run 包、提供隔离安装目录 |

## 4. 核心概念与源码讲解

### 4.1 驱动链：build.sh -u 如何唤起 run_tests.sh

#### 4.1.1 概念说明

u1-l4 讲过 `bash build.sh -u` 可以跑测试，但没有拆开这条链路。build.sh 在这里扮演"参数翻译官 + 前置构建决策者"两个角色：它把 shell 层的 `-u/--component/--ut/--st/--cov` 翻译成 run_tests.sh 的参数，并决定测试前要不要先做一次完整的 C++ 构建。回顾 u1-l2 的结论：asys、msaicerr 是纯 Python 组件，跑它们的 UT 时可以走"快车道"跳过整个 C++ 构建——这个决策就发生在这段代码里。

#### 4.1.2 核心流程

```text
bash build.sh -u --component asys
  ├─ checkopts 解析参数
  │    ├─ -u            → ENABLE_UT="on", EXEC_TEST="on"
  │    ├─ --component X → TEST_COMPONENT=X
  │    └─ --ut / --st   → RUN_UT_ONLY / RUN_ST_ONLY
  ├─ 快车道判定：ENABLE_UT+EXEC_TEST 开 且 组件是 asys/msaicerr
  │    → 跳过 build_oam_tools，仅 cmake -P asys.cmake 生成 chip_handler.py
  ├─ （否则）完整 cmake + make 构建
  └─ source setenv.bash 后拼装参数调用 scripts/run_tests.sh
```

#### 4.1.3 源码精读

`-u` 是测试开关，同时置起 `ENABLE_UT` 与 `EXEC_TEST` 两个变量（后者可被单独关闭，用于"编译测试目标但不执行"的场景）：

[build.sh:113-117](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L113-L117) — `-u` 选项把 `ENABLE_UT` 和 `EXEC_TEST` 都置为 on，测试的总开关由此打开。

[build.sh:168-179](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L168-L179) — `--component`、`--ut`、`--st` 三个选项分别写入 `TEST_COMPONENT`、`RUN_UT_ONLY`、`RUN_ST_ONLY`，它们只是暂存，最终透传给 run_tests.sh。

快车道判定：`is_python_only_component` 只认 asys 和 msaicerr，命中则跳过整个 C++ 构建，但必须先 `cmake -P asys.cmake` 生成 `chip_handler.py`（u2-l4 讲过这个文件不构建就不存在，asys 无法运行）：

[build.sh:329-356](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L329-L356) — `is_python_only_component` 判定组件是否纯 Python；`generate_asys_chip_handler` 用 `cmake -P` 脚本模式单独生成 chip_handler.py；`skip_build` 逻辑决定是否跳过 `build_oam_tools`。

最后是参数翻译与委托执行：

[build.sh:363-390](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/build.sh#L363-L390) — source 环境后，把 `TEST_COMPONENT/RUN_UT_ONLY/RUN_ST_ONLY/ENABLE_COVERAGE` 逐个翻译成 `--component/--ut/--st/--cov` 追加进 `run_tests_args` 数组，最终 `bash scripts/run_tests.sh "${run_tests_args[@]}"`，返回非 0 则 build.sh 整体失败退出。

#### 4.1.4 代码实践

1. **实践目标**：验证 build.sh → run_tests.sh 的参数透传关系。
2. **操作步骤**：
   - 在仓库根目录执行 `bash build.sh -h`，找到 `-u`、`--component`、`--ut`、`--st` 的帮助文案；
   - 执行 `bash scripts/run_tests.sh -h`，对照两边帮助文案；
   - 在 build.sh 第 385 行的调用前临时加一行 `echo "DEBUG: ${run_tests_args[*]}"`（读完删掉，勿提交）。
3. **需要观察的现象**：`bash build.sh -u --component asys` 时 DEBUG 行应打印 `--component asys`；`bash build.sh -u`（不带 component）时数组为空，即 run_tests.sh 使用默认值 all。
4. **预期结果**：两条命令的帮助信息中组件列表一致（asys、msaicerr、msprof、install、upgrade、uninstall、all）。（执行结果待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bash build.sh -u --component msaicerr` 比 `--component msprof` 快得多？
**答案**：msaicerr 是纯 Python 组件命中 `is_python_only_component` 快车道，跳过整个 C++ 编译，只生成 chip_handler.py；msprof 的 gtest 用例必须完整走 CMake 编译出 UT 二进制才能运行。

**练习 2**：`EXEC_TEST` 与 `ENABLE_UT` 为什么是两个变量？
**答案**：`-u` 同时置起两者；存在其他选项（build.sh:142 附近）可单独把 `EXEC_TEST` 置回 off，用于"让 CMake 编译出测试目标但不执行"的场景（如只需产物、或在受限 CI 环境编译验证）。

### 4.2 run_tests.sh：测试矩阵与调度中枢

#### 4.2.1 概念说明

run_tests.sh 是整个测试体系的"总线"。它维护一张「用例组名 → 测试框架」的映射表，把 `--component`×`--ut/--st` 的选择展开成具体的用例组序列，逐个执行并解析结果。关键设计：**它不关心每个用例组内部怎么写**，只约定两种框架接口——pytest（目录丢给 `python3 -m pytest`）和 gtest（逐个执行 UT 二进制），然后用独立的解析函数从输出日志中提取统计。

#### 4.2.2 核心流程

```text
main
  ├─ parse_args           # component 默认 all；--ut/--st 都没给则两者都跑
  ├─ chip_handler.py 存在性检查（asys/all 时）
  ├─ get_test_cases       # component × (ut, st) 展开成用例组列表
  ├─ ensure_run_package_for_st_cases
  │     # 若含 install/upgrade/uninstall ST 且 build_out/ 无 .run 包
  │     # → 自动 bash build.sh --noexec 先打一个包
  └─ for case in cases: run_test_case
        ├─ 按框架执行（pytest 带 coverage / gtest 按清单逐二进制）
        └─ validate_gtest_result / validate_pytest_result 解析日志
```

#### 4.2.3 源码精读

测试矩阵的定义——8 个用例组，7 个 pytest、1 个 gtest：

[scripts/run_tests.sh:26-37](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L26-L37) — `TEST_CASES` 关联数组声明用例组名与框架的对应：asys/msaicerr 的 UT 与 ST、msprof 的 UT 用 gtest，install/upgrade/uninstall 只有 ST；`VALID_COMPONENTS` 是 `--component` 的白名单。

组件到用例组的展开规则，注意两个"空洞"：install/upgrade/uninstall 没有 UT 对应物，msprof 没有 ST 对应物：

[scripts/run_tests.sh:125-175](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L125-L175) — `get_test_cases` 先把 all 展开成 6 个组件，再按 `RUN_UT/RUN_ST` 双开关把每个组件映射为 `<comp>_ut` 和/或 `<comp>_st`，case 分支里的注释明确标注 install/upgrade/uninstall 无 UT、msprof 无 ST。

组件与测试目录/参数的最终对应表（由 run_test_case 的分发决定）：

| 用例组 | 框架 | 测试目录 | coverage --source | 附加条件 |
| --- | --- | --- | --- | --- |
| asys_ut | pytest | test/ut/asys/testcase | ./src/asys | 需先生成 chip_handler.py |
| asys_st | pytest | test/st/asys/testcase | ./src/asys | 同上 |
| msaicerr_ut | pytest | test/ut/msaicerr/testcase | ./src/msaicerr | — |
| msaicerr_st | pytest | test/st/msaicerr/testcase | ./src/msaicerr | — |
| msprof_ut | gtest | build/msprof_ut_targets.txt 清单 | gcov（--cov 时） | 需先 CMake 构建 |
| install_st / upgrade_st / uninstall_st | pytest（无覆盖率） | test/st/{install,upgrade,uninstall}/testcase | — | 需 build_out/ 有 .run 包 |

对 .run 包的按需自动构建：

[scripts/run_tests.sh:199-236](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L199-L236) — `ensure_run_package_for_st_cases` 发现用例组含 install/upgrade/uninstall 且 `build_out/` 下没有 `cann-oam-tools_*.run` 时，自动调 `bash build.sh --noexec` 先打一个包，取版本号最大的那个；这解释了为什么这三组 ST 可以"裸跑"。

结果解析是这套脚本里最"防御式"的部分——先判崩溃，再数通过：

[scripts/run_tests.sh:238-299](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L238-L299) — `validate_gtest_result` 先做异常退出检测（exit code ≥128 说明被信号杀死、输出含 Segmentation fault / Sanitizer / SIGABRT 即判崩溃），崩溃时用 awk 对账所有 `[ RUN ]` 却没有对应 `[ OK ]`/`[ FAILED ]` 的用例，列出"未完成清单"；正常退出才累加多段 `[ PASSED ]`/`[ FAILED ]` 汇总行的数字。

[scripts/run_tests.sh:347-457](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L347-L457) — `validate_pytest_result` 是 pytest 版同款逻辑：先判崩溃（多了 `Fatal Python error` 标记），再从 pytest 汇总行 `=== N passed, M failed in X.XXs ===` 里 grep 出统计；额外检查 Traceback、ImportError、ModuleNotFoundError 都直接判失败，最后从 coverage 的 `TOTAL` 行提取覆盖率百分比。

主入口里的 asys 前置检查：

[scripts/run_tests.sh:680-688](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L680-L688) — 组件为 asys 或 all 时，先确认 `src/asys/common/chip_handler.py` 存在（它是 cmake configure 从模板生成的），不存在则提示先跑 cmake configure 再退出——这是 u2-l4「构建期注册」设计的测试侧投影。

#### 4.2.4 代码实践

1. **实践目标**：不真正跑完测试，只验证参数展开逻辑。
2. **操作步骤**：
   - 执行 `bash scripts/run_tests.sh --component msprof --ut`，观察输出 `INFO: Running test cases: ...` 一行；
   - 再执行 `bash scripts/run_tests.sh --component install` 与 `bash scripts/run_tests.sh --component msprof --st`；
   - 若环境无 CMake 构建产物，预期会在 msprof_ut 清单检查处失败——这本身就是观察点。
3. **需要观察的现象**：第一个命令的用例组是 `msprof_ut`；第二个是 `install_st`（且会尝试找/打 .run 包）；第三个展开为空，应报 `ERROR: No test cases to run` 退出。
4. **预期结果**：三个命令的行为与 `get_test_cases` 的 case 分支一一对应。（执行结果待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：`--component uninstall --ut` 会发生什么？
**答案**：`get_test_cases` 中 uninstall 在 RUN_UT 分支没有对应项（注释写明 install/upgrade/uninstall have no UT counterpart），展开结果为空数组，main 中 `${#test_cases[@]} -eq 0` 命中，报 `ERROR: No test cases to run` 并 exit 1。

**练习 2**：为什么 gtest/pytest 的解析函数都要先做"崩溃检测"而不是直接数通过数？
**答案**：进程被信号杀死（exit code ≥ 128）或段错误时，框架没有正常收尾，汇总统计不可靠——可能显示"0 failed"但实际一半用例没跑。所以先判崩溃走单独分支（打印错误上下文 + 未完成用例清单），正常退出才信任汇总行；最后还有"`passed=0 且 failed=0` 视为可疑崩溃"的兜底（run_tests.sh:339-342、451-454）。

### 4.3 asys / msaicerr 的 pytest 组织方式

#### 4.3.1 概念说明

asys 和 msaicerr 的 UT 都是标准 pytest 工程，但组织风格不同：

- **asys UT**：`test/ut/asys/testcase/` 下按子命令/模块分目录（`cmdline/`、`collect/`、`health/`、`info/`、`launch/`、`common/`…），目录结构与 `src/asys/` 的包结构镜像对应——找某个模块的测试直接按同名路径找。
- **msaicerr UT**：扁平布局，`test/ut/msaicerr/testcase/` 下约 30 个 `test_*_ut.py`，另含 `proto_parse/` 子目录与 `res/` 测试资源。
- **asys ST**：`test/st/asys/testcase/` 下按子命令一个文件（test_collect.py、test_launch.py、test_health.py…），外加 `data/`（mock 的命令脚本与目录夹具）。

两者的共同点：被测源码不在 Python path 里（src/asys 不是可安装包），所以每个 conftest 都自己算相对路径、把 `src/asys` 或 `src/msaicerr` 塞进 `sys.path`。

#### 4.3.2 核心流程

以 asys UT 的一个用例为例：

```text
pytest 收集 test/ut/asys/testcase/cmdline/test_arg_checker.py
  ├─ conftest.py 提供 ASYS_SRC_PATH 等路径常量
  ├─ 测试文件 sys.path.insert(0, ASYS_SRC_PATH) → 才能 import cmdline.arg_checker
  ├─ 测试类继承 AssertTest（自制的极简断言基类）
  ├─ @pytest.mark.parametrize 展开数据驱动的多组输入
  └─ mocker.patch 替换 os.path.exists 等依赖，隔离文件系统
```

#### 4.3.3 源码精读

conftest 的路径基础设施——所有测试文件共享的"源码定位器"：

[test/ut/asys/testcase/conftest.py:34-45](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys/testcase/conftest.py#L34-L45) — `get_root()` 用 `dirname` 三层上溯定位测试根，`ASYS_SRC_PATH` 指向仓库的 `src/asys/`；同文件 [L28-32](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys/testcase/conftest.py#L28-L32) 的 `pytest_configure` 还注册了忽略 multiprocessing 弃用告警的过滤器。

[test/ut/asys/testcase/conftest.py:207-209](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys/testcase/conftest.py#L207-L209) — `AssertTest` 基类只有一个 `assertTrue` 方法（内部就是 `assert value`）。这是历史风格：让测试类不直接依赖 unittest，保持"纯 pytest + 一个统一断言入口"。

一个典型的参数化 + 打桩用例（本讲实践的原型）：

[test/ut/asys/testcase/cmdline/test_arg_checker.py:23-32](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys/testcase/cmdline/test_arg_checker.py#L23-L32) — 先 `from testcase.conftest import ...` 拿路径常量，`sys.path.insert(0, ASYS_SRC_PATH)` 后直接以顶层包名导入被测的 `cmdline.arg_checker` 中各 `check_arg_*` 校验函数（u2-l2 讲过的第二道防线）。

[test/ut/asys/testcase/cmdline/test_arg_checker.py:51-68](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys/testcase/cmdline/test_arg_checker.py#L51-L68) — 三个数据驱动用例：空串、纯空格、非法字符分别传入 `check_arg_exist_dir`/`check_arg_create_dir`/`check_arg_executable`，断言返回值不等于 `RetCode.SUCCESS`——即"坏输入必须被校验器拦下"。

[test/ut/asys/testcase/cmdline/test_arg_checker.py:71-77](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys/testcase/cmdline/test_arg_checker.py#L71-L77) — 打桩示例：`mocker.patch("os.path.exists", return_value=False)` 让"目录不存在"的场景无需真实文件系统，再 patch 掉 `f.create_dir` 副作用（u2-l2 提到过部分校验器"校验即创建目录"），断言仍返回 SUCCESS。

asys ST 的 conftest 则用"假命令 + 假 HOME"把环境伪装起来：

[test/st/asys/testcase/conftest.py:41-48](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/asys/testcase/conftest.py#L41-L48) — `set_env()` 把 `data/scripts`（一堆 mock 的 shell 命令脚本）插到 PATH 最前、把 HOME 指向测试数据目录，使 asys 真实执行时调到的 `npu-smi` 等外部命令都被替换成可控的假实现；`unset_env()` 在用例结束后还原。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：读懂一个现成 UT，并仿写一个针对 arg_checker 的新用例，用两种方式跑通。
2. **操作步骤**：
   - 通读 [test_arg_checker.py](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys/testcase/cmdline/test_arg_checker.py)（本节示例均为真实代码）；
   - 在该文件末尾追加如下用例（示例代码，测试 `check_arg_tar` 对非法 tar 后缀的拒绝）：

     ```python
     class TestArgCheckerExtra(AssertTest):
         @pytest.mark.parametrize("arg_val", ["xxx.tar.bz2", "tar.gz", "a.txt"])
         def test_tar_invalid_suffix(self, arg_val):
             # --tar 只接受 .tar.gz / .tgz 等合法值，非法后缀应返回非 SUCCESS
             self.assertTrue(check_arg_tar("tar", arg_val) != RetCode.SUCCESS)
     ```

     注意：先打开 [src/asys/cmdline/arg_checker.py](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/cmdline/arg_checker.py) 确认 `check_arg_tar` 的真实合法值集合，若与示例参数不符，以源码为准调整参数；
   - 方式一（快车道）：`bash build.sh -u --component asys --ut`；
   - 方式二（单文件直跑）：先 `mkdir -p build && cd build && cmake .. && cd ..` 生成 chip_handler.py，然后在仓库根目录 `python3 -m pytest test/ut/asys/testcase/cmdline/test_arg_checker.py -v`。
3. **需要观察的现象**：pytest 输出中新用例被参数化展开成 3 条；故意把一个参数改成合法值（如 `.tar.gz`）应看到该条断言失败——证明用例真的在测源码而不是恒真。
4. **预期结果**：新用例全部通过；`build/<case>_output.log` 中 asys_ut 的 passed 数比改动前多。（合法后缀集合以 arg_checker.py 源码为准，执行结果待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：为什么测试文件都要 `sys.path.insert(0, ASYS_SRC_PATH)` 而不是 pip install asys？
**答案**：asys 以源码目录形态随 .run 包发布（u1-l3 讲过软链接 + shebang 的运行方式），不是可安装的 Python 包；测试只能通过手工注入 `sys.path` 让 `import cmdline.arg_checker` 这类顶层导入成立。

**练习 2**：asys 的 UT 和 ST 对外部命令依赖（如 npu-smi）的处理手法有何不同？
**答案**：UT 用 `mocker.patch` 在 Python 层替换函数/方法，被测代码不真正执行外部命令；ST 通过 conftest 的 `set_env()` 把 mock 命令脚本目录插到 PATH 最前面，让真实运行的 asys 进程调到假命令——一个是代码级打桩，一个是环境级伪装。

**练习 3**：msaicerr 的 UT 目录（扁平 `test_*_ut.py`）与 asys 的镜像目录结构各有什么取舍？
**答案**：镜像结构与 src 包对齐、易定位，但目录层级深；扁平结构收集快、一眼看全，但组件变大后命名（`_extra_ut`、`_ascend950` 后缀）承担了分类职责。msaicerr 已出现 `test_*_ascend950.py` 这类后缀文件，说明扁平布局正在靠命名约定扩展。

### 4.4 msprof 的 gtest 组织：CMake 构建 + 清单驱动

#### 4.4.1 概念说明

msprof 是 C++ 组件，它的 UT 不是 pytest 能直接跑的，而是每个子目录编出一个 gtest 可执行文件。这里有个漂亮的解耦设计：**run_tests.sh 对 msprof UT 的具体数量一无所知**——CMake 在构建期把所有 UT 二进制路径写进 `build/msprof_ut_targets.txt` 清单文件，run_tests.sh 只负责逐行读取并执行。新增一个 UT target 不需要改任何 shell 脚本。

#### 4.4.2 核心流程

```text
CMake 配置/构建期                          运行期（run_tests.sh msprof_ut 分支）
  MSPROF_UT_SUBDIRS 列出 11 个子目录         读 build/msprof_ut_targets.txt
  → add_subdirectory 逐个挂载                → 逐行取出二进制路径
  → 收集每个目录的 EXECUTABLE target         → 存在则执行、输出追加进日志
  → file(GENERATE) 写出路径清单              → 任一二进制缺失/失败则 rc=1
                                             → --cov 时再走 gcov 覆盖率收集
```

#### 4.4.3 源码精读

[test/ut/msprof/CMakeLists.txt:52-69](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/msprof/CMakeLists.txt#L52-L69) — 注释明说扩展规则：新增 UT 只需新建子目录（含自己的 CMakeLists 与可执行 target）并把子目录名追加进 `MSPROF_UT_SUBDIRS`，无需改 run_tests.sh；`intf_llt_pub` 接口库（L22-33）统一提供 c++17、gtest/gtest_main、mockcpp 等测试依赖。

[test/ut/msprof/CMakeLists.txt:71-95](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/msprof/CMakeLists.txt#L71-L95) — foreach 逐个 add_subdirectory，用 `BUILDSYSTEM_TARGETS` 目录属性收集子目录里所有 EXECUTABLE target，最后 `file(GENERATE)` 把 `$<TARGET_FILE:...>`（生成器表达式，构建后展开为真实路径）写入 `msprof_ut_targets.txt` 清单。

run_tests.sh 侧的消费逻辑：

[scripts/run_tests.sh:608-635](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L608-L635) — `msprof_ut` 分支：清单文件不存在直接失败（提示先构建）；while 逐行读二进制路径，存在则执行并把输出追加到日志，不存在记错误；`--cov` 时先动态生成覆盖率白名单再走 gcov 收集。

[test/ut/msprof/msprofbin/](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/msprof/msprofbin/test) — 以 msprofbin 子目录为例，`test/` 下是 `input_parser_utest.cpp`、`msprof_manager_utest.cpp` 等 gtest 源文件与 `main.cpp`，其 [CMakeLists.txt](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/msprof/msprofbin/CMakeLists.txt#L20-L40) 直接把生产源码（application.cpp、input_parser 等 u4-l2 精读过的文件）编进 UT 目标，配合 stub 隔离驱动依赖。

C++ 覆盖率的"分母对齐"技巧：

[scripts/run_tests.sh:538-582](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L538-L582) — `gen_msprof_cov_whitelist` 内嵌 Python 从 acp 与 msprofbin 两个生产 CMakeLists 动态解析源文件清单作白名单——因为本仓只编译这两个模块，其余库在 runtime 仓编译，不做白名单会把没编译的代码混进覆盖率分母。

#### 4.4.4 代码实践

1. **实践目标**：理解"清单驱动"如何让 UT 扩展零脚本改动。
2. **操作步骤**：
   - 阅读 [test/ut/msprof/CMakeLists.txt](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/msprof/CMakeLists.txt) 全文（约 95 行）；
   - 若本地已完成 `bash build.sh -u --component msprof --ut`，打开 `build/msprof_ut_targets.txt` 数一数行数；再对照 `MSPROF_UT_SUBDIRS` 的 11 个子目录；
   - 挑一个二进制直接执行，如 `./build/test/ut/msprof/msprofbin/msprof_bin_utest`，观察 gtest 原生输出格式（`[ RUN ]`/`[ OK ]`/`[ PASSED ]` 标记行）。
3. **需要观察的现象**：清单每行是一个绝对/相对路径的二进制；直接执行单个 UT 二进制与 run_tests.sh 执行它看到的是同样的 gtest 标记输出——这正是 `validate_gtest_result` 能用 grep/awk 解析的前提。
4. **预期结果**：清单行数与各子目录 EXECUTABLE target 总数一致；单独执行返回 0 且末尾有 `[  PASSED  ]` 汇总行。（构建需 CANN 环境，执行结果待本地验证）

#### 4.4.5 小练习与答案

**练习 1**：如果新增一个 `test/ut/msprof/foobar/` UT 子目录但忘了加进 `MSPROF_UT_SUBDIRS`，会发生什么？
**答案**：CMake 不会 add_subdirectory 它，它不参与构建，也就不会出现在 `msprof_ut_targets.txt` 清单里——UT 静默地不被运行。这就是为什么 CMakeLists 注释把"追加子目录名"列为必做第 2 步。

**练习 2**：为什么 pytest 用例有 `run_pytest_with_coverage` 而 install/upgrade/uninstall ST 用 `run_pytest_plain`？
**答案**（见 [run_tests.sh:636-644](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/scripts/run_tests.sh#L636-L644)）：install/upgrade/uninstall 的被测对象是 .run 安装包里的 shell/python 产物，不是本仓 `src/` 源码，统计 `--source` 覆盖率没有意义，只跑裸 pytest。

### 4.5 ST 三剑客：install / upgrade / uninstall 与共享 conftest

#### 4.5.1 概念说明

u6-l3 会专门讲打包链路，这里先看它的测试面。install/upgrade/uninstall 三组 ST 共享 [test/st/conftest.py](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/conftest.py) 提供的两个 fixture：`run_package`（定位 .run 包，找不到就跳过整个会话）与 `install_dir`（每次用例一个干净的一次性安装根目录）。这是 pytest fixture 依赖注入的标准用法——测试文件不自己找包、不自己建目录。

#### 4.5.2 核心流程

```text
pytest 收集 test/st/install/testcase/test_install_st.py
  ├─ run_package fixture（session 级）：build_out/ 下 glob 最新 cann-oam-tools_*.run
  │     └─ 找不到 → pytest.skip 跳过整个 session（而非报错）
  ├─ install_dir fixture（函数级）：build_out/ 下 mkdtemp 一个 0755 的一次性目录
  │     └─ 用例结束后 chmod -R u+w 再整树删除
  └─ 用例 subprocess 真实执行 .run 包（--full/--run/--devel/--noexec --extract）
        └─ 断言退出码 0、输出无 [ERROR]、关键产物存在
```

#### 4.5.3 源码精读

[test/st/conftest.py:34-49](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/conftest.py#L34-L49) — `run_package` 是 session 级 fixture：glob `build_out/cann-oam-tools_*.run` 按版本排序取最新；找不到时 `pytest.skip` 提示先 `bash build.sh`——设计取向是"没包就温和跳过，不误报失败"。

[test/st/conftest.py:52-68](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/conftest.py#L52-L68) — `install_dir` fixture 的 docstring 解释了一个真实的坑：.run 安装器（root 运行时）会拒绝权限严于 0755 的祖先目录，而 `tempfile.mkdtemp` 默认建 0700 目录，所以显式 chmod 成 0755；结束时先 `chmod -R u+w`（防止构建期只读产物导致删除失败）再 rmtree。

install 用例断言的三条底线（退出码、无 ERROR、产物存在）：

[test/st/install/testcase/test_install_st.py:18-38](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/install/testcase/test_install_st.py#L18-L38) — 模块 docstring 写明测试契约：覆盖 `--full/--run/--devel/--noexec --extract` 四种命令形态；并刻意说明为什么不断言 `[WARNING]`——安装器把 WARNING 当非致命提示，强行断言会让套件对环境过度敏感。

[test/st/install/testcase/test_install_st.py:65-98](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/install/testcase/test_install_st.py#L65-L98) — `TestInstall` 参数化三种安装形态断言退出码与输出；`TestInstallArtefacts` 验证安装后的关键产物：`share/info/oam_tools/ascend_install.info`、版本头文件、`cann_uninstall.sh` 都必须在盘上。

[test/st/install/testcase/test_install_st.py:142-181](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/st/install/testcase/test_install_st.py#L142-L181) — `TestExtractInstallConsistency` 做树级对账：`--noexec --extract` 的解压目录树应当是 `--full` 安装目录树的子集，特别关注 `tools/profiler/profiler_tool`（msprof whl 解包目标，u4-l1 讲过它的来源）。

#### 4.5.4 代码实践

1. **实践目标**：观察 fixture 依赖注入与"无包即跳过"行为。
2. **操作步骤**：
   - 确保 `build_out/` 下没有 .run 包（或临时移走），执行 `python3 -m pytest test/st/install/testcase -v`；
   - 恢复/构建一个 .run 包（`bash build.sh --noexec`）后再跑同一命令；
   - 打开 `build/build_out` 下 install_st 的输出日志，找到 pytest 汇总行。
3. **需要观察的现象**：第一次运行所有用例标记为 `SKIPPED` 并带出 `No cann-oam-tools*.run found` 的跳过原因；第二次运行用例真实执行 .run 包，`install_dir` 在 build_out/ 下不断新建又清理 `oam_st_*` 目录。
4. **预期结果**：无包时整个 session 跳过而非失败；有包时 TestInstall 的 3 条参数化用例全部 passed。（需真实构建环境，执行结果待本地验证）

#### 4.5.5 小练习与答案

**练习 1**：`run_package` 为什么用 `pytest.skip` 而不是 `pytest.fail`？
**答案**：install/upgrade/uninstall ST 的前置条件是"已构建 .run 包"。在只跑 UT 的 CI 任务（如 `build.sh -u --component asys --ut`）里没有包是正常状态，fail 会制造误报；skip 让用例在有包的完整流水线里才生效。

**练习 2**：`TestExtractInstallConsistency` 防护的是哪类回归？
**答案**：解压（extract）与安装（install）走的释放代码路径若有差异（例如 install 期脚本多删/多放了文件），就会出现"解压目录有、安装目录没有"的条目。该用例用集合差集把这类不一致显式列出来，特别盯住 msprof whl 解包出的 profiler_tool 子树。

## 5. 综合实践

**任务：给 asys 的参数校验补一条带覆盖率验证的 UT 闭环。**

综合运用本讲四个模块的知识，完成一次"写用例 → 两种方式运行 → 看日志与覆盖率"的完整流程：

1. **选题**：打开 `src/asys/cmdline/arg_checker.py`，通读各 `check_arg_*` 函数（回顾 u2-l2：它们是 argparse 之后的第二道语义校验防线），挑一个现有测试未覆盖的输入分支（例如某个校验函数对特殊值的处理）。
2. **写用例**：在 `test/ut/asys/testcase/cmdline/test_arg_checker.py` 中新增一个继承 `AssertTest` 的测试类，用 `@pytest.mark.parametrize` 提供至少 3 组输入，断言返回的 `RetCode` 符合预期（参照 4.3.3 的真实用例写法；先读源码确认预期值，不要猜）。
3. **跑通**：先按 4.3.4 的方式二用 pytest 单文件直跑验证；再用 `bash build.sh -u --component asys --ut` 走完整快车道，确认整体 passed 数增加。
4. **看产物**：打开 `build/asys_ut_output.log`，找到末尾的 pytest 汇总行与 coverage `TOTAL` 行（对照 4.2.3 中 `validate_pytest_result` 解析的两个位置），记录改动前后 `src/asys/cmdline/arg_checker.py` 的覆盖率变化。
5. **收尾**：删除或保留你的用例由你决定；若保留，注意按仓库规范（u6-l2 会讲 pre-commit 与增量检查）补齐文件头版权注释。

## 6. 本讲小结

- `bash build.sh -u` 的驱动链是：build.sh 解析 `-u/--component/--ut/--st/--cov` → 纯 Python 组件走快车道跳过 C++ 构建（仅生成 chip_handler.py）→ 参数翻译后委托 `scripts/run_tests.sh`。
- run_tests.sh 用 `TEST_CASES` 映射表定义 8 个用例组（7 pytest + 1 gtest），`get_test_cases` 把 `--component`×`--ut/--st` 展开为用例序列；install/upgrade/uninstall ST 发现没包会自动 `build.sh --noexec` 打包。
- 结果解析先判崩溃（信号退出、段错误、Sanitizer、Fatal Python error）再数统计，`passed=0 && failed=0` 也视为可疑失败——防御式解析贯穿始终。
- asys/msaicerr 的 pytest 组织靠 conftest 注入 `sys.path`、`AssertTest` 极简断言基类、parametrize 数据驱动与 mocker 打桩；asys ST 用 PATH 前插 mock 脚本做环境级伪装。
- msprof UT 是 CMake 构建 + `msprof_ut_targets.txt` 清单驱动的 gtest 体系，新增 UT 只改 CMake 不改脚本；C++ 覆盖率用动态白名单对齐"本仓实际编译"的分母。
- ST 三剑客共享 `run_package`（无包跳过）与 `install_dir`（0755 一次性安装根）两个 fixture，install ST 同时做命令级断言与解压/安装目录树对账。

## 7. 下一步学习建议

- 下一讲 [u6-l2 工程规范：OAT 检查、pre-commit 与增量代码检查](u6-l2-engineering-practice.md)：本讲实践中你改了测试文件，下一讲学习提交这样的改动前要过哪些检查门禁。
- 若想深挖打包链路，直接预习 u6-l3（.run 包生命周期），把本讲 4.5 节的 install ST 与 `scripts/package` 打包脚本对照阅读。
- 源码延伸阅读：`test/ut/asys/testcase/collect/` 下针对 u2-l5 采集子系统的测试，是"如何为目录搬运型模块写 UT"的好样本；`test/ut/msprof/msprofbin/test/input_parser_utest.cpp` 则展示了 C++ 侧为 u4-l2 的参数解析器写 gtest 的方式。
